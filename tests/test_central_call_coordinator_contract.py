"""Red contract for the first GUI-free central call-coordination slice.

The production module deliberately does not exist on the restored baseline.  These
tests define the small, persistent coordinator API that will replace no part of the
legacy GUI yet: ``central_call_coordinator.CallCoordinator`` with ``create_call``,
``accept``, ``decline`` and ``expire_due``.  Returned values are mappings so the
HTTP/MCP layers can later serialise them without leaking ORM objects.

The request payload boundary is a JSON-like mapping.  Its canonical comparison
representation is UTF-8 JSON with sorted object keys and compact separators
(``json.dumps(payload, sort_keys=True, separators=(",", ":"),
ensure_ascii=False)``). Object insertion order is therefore not significant;
values, arrays, and types remain significant.

Likewise, terminal identity is explicit input: ``terminal_category`` is either
``iphone_web`` or ``desktop``, and ``terminal_platform`` is a reported platform
such as ``ios`` or ``linux``. Neither value may be derived from ``device_id``.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import tempfile
import threading
from pathlib import Path

import pytest

from central_call_coordinator import CallCoordinator


class FakeClock:
    """Controllable UTC-like seconds used to prove deadlines are absolute."""

    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def notifications() -> list[dict[str, object]]:
    return []


@pytest.fixture
def coordinator(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]]):
    return CallCoordinator(
        database_path=tmp_path / "calls.sqlite3",
        clock=clock,
        notify=notifications.append,
        ring_timeout_seconds=60,
    )


def create(coordinator: CallCoordinator, request_id: str = "create-1", **payload):
    body = {"tool": "get_text_input", "prompt": "Proceed?", **payload}
    return coordinator.create_call(
        caller_id="agent-a", request_id=request_id, payload=body
    )


def test_creating_call_persists_one_absolute_deadline_and_dispatches_once(
    coordinator: CallCoordinator, clock: FakeClock, notifications: list[dict[str, object]]
) -> None:
    created = create(coordinator)

    assert created["status"] == "ringing"
    assert created["call_state"] == "ringing"
    assert created["ring_deadline"] == clock() + 60
    assert len(notifications) == 1
    assert notifications[0]["call_id"] == created["call_id"]
    assert notifications[0]["ring_deadline"] == clock() + 60

    clock.advance(10)
    replay = create(coordinator)
    assert replay == created
    assert len(notifications) == 1


def test_same_request_id_with_different_normalized_payload_is_a_conflict(
    coordinator: CallCoordinator,
) -> None:
    first_payload = {
        "tool": "get_text_input",
        "prompt": "Proceed?",
        "constraints": {"required": True, "max_length": 40},
    }
    # Deliberately different construction/insertion order, same canonical JSON.
    same_canonical_payload = {
        "constraints": {"max_length": 40, "required": True},
        "prompt": "Proceed?",
        "tool": "get_text_input",
    }
    assert list(first_payload) != list(same_canonical_payload)
    assert json.dumps(first_payload, sort_keys=True, separators=(",", ":")) == json.dumps(
        same_canonical_payload, sort_keys=True, separators=(",", ":")
    )
    first = coordinator.create_call(
        caller_id="agent-a", request_id="create-1", payload=first_payload
    )
    replay = coordinator.create_call(
        caller_id="agent-a", request_id="create-1", payload=same_canonical_payload
    )
    assert replay == first

    conflict = coordinator.create_call(
        caller_id="agent-a",
        request_id="create-1",
        payload={**same_canonical_payload, "prompt": "A different question"},
    )

    assert conflict == {
        "success": False,
        "status": "request_conflict",
        "needs_continuation": False,
    }


def test_another_agent_is_busy_without_queue_or_extra_notification(
    coordinator: CallCoordinator, notifications: list[dict[str, object]]
) -> None:
    active = create(coordinator)

    busy = coordinator.create_call(
        caller_id="agent-b",
        request_id="other-create-1",
        payload={"tool": "get_confirmation", "prompt": "Can I interrupt?"},
    )

    assert busy == {
        "success": True,
        "status": "busy",
        "call_state": None,
        "call_id": None,
        "needs_continuation": False,
        "retryable": True,
        "retry_after_seconds": 30,
    }
    assert coordinator.get_call(active["call_id"])["call_state"] == "ringing"
    assert len(notifications) == 1


def test_accept_before_deadline_wins_and_late_decline_cannot_cancel_it(
    coordinator: CallCoordinator, clock: FakeClock
) -> None:
    created = create(coordinator)
    call_id = created["call_id"]
    clock.advance(59.999)

    accepted = coordinator.accept(
        call_id=call_id,
        device_id="registered-terminal-42",
        terminal_category="iphone_web",
        terminal_platform="ios",
    )
    declined = coordinator.decline(call_id=call_id, device_id="desktop-1")

    assert accepted["status"] == "opened"
    assert accepted["call_state"] == "accepted"
    assert accepted["handled_on"] == "iphone_web"
    assert declined == {
        "success": False,
        "status": "already_accepted",
        "call_state": "accepted",
    }


@pytest.mark.parametrize(
    ("device_id", "terminal_category", "terminal_platform"),
    [
        ("opaque-device-7", "iphone_web", "ios"),
        ("not-a-desktop-name", "desktop", "linux"),
    ],
)
def test_accept_uses_explicit_terminal_identity_not_device_id_spelling(
    coordinator: CallCoordinator,
    device_id: str,
    terminal_category: str,
    terminal_platform: str,
) -> None:
    call_id = create(coordinator)["call_id"]

    accepted = coordinator.accept(
        call_id=call_id,
        device_id=device_id,
        terminal_category=terminal_category,
        terminal_platform=terminal_platform,
    )

    assert accepted["status"] == "opened"
    assert accepted["handled_on"] == terminal_category
    assert accepted["terminal_platform"] == terminal_platform
    persisted = coordinator.get_call(call_id)
    assert persisted["owner_device_id"] == device_id
    assert persisted["terminal_platform"] == terminal_platform


def test_duplicate_accept_replays_saved_outcome_without_changing_owner(
    coordinator: CallCoordinator,
) -> None:
    call_id = create(coordinator)["call_id"]

    accepted = coordinator.accept(
        call_id=call_id,
        device_id="opaque-owner-a",
        terminal_category="iphone_web",
        terminal_platform="ios",
    )
    replay = coordinator.accept(
        call_id=call_id,
        device_id="opaque-owner-a",
        terminal_category="iphone_web",
        terminal_platform="ios",
    )

    assert replay == accepted
    assert coordinator.get_call(call_id)["call_state"] == "accepted"
    assert coordinator.get_call(call_id)["owner_device_id"] == "opaque-owner-a"


def test_duplicate_decline_replays_saved_outcome_without_state_reversal(
    coordinator: CallCoordinator,
) -> None:
    call_id = create(coordinator)["call_id"]

    declined = coordinator.decline(call_id=call_id, device_id="desktop-1")
    replay = coordinator.decline(call_id=call_id, device_id="desktop-1")

    assert replay == declined
    assert coordinator.get_call(call_id) == {
        "call_id": call_id,
        "call_state": "declined",
        "reason": "human_declined",
    }


def test_separate_sqlite_clients_race_accept_and_decline_with_one_winner(
    tmp_path: Path, clock: FakeClock
) -> None:
    """The database transaction, not local process state, decides the winner."""
    database_path = tmp_path / "calls.sqlite3"
    notifications: list[dict[str, object]] = []
    creator = CallCoordinator(
        database_path=database_path,
        clock=clock,
        notify=notifications.append,
        ring_timeout_seconds=60,
    )
    call_id = create(creator)["call_id"]
    accept_client = CallCoordinator(
        database_path=database_path,
        clock=clock,
        notify=lambda _event: None,
        ring_timeout_seconds=60,
    )
    decline_client = CallCoordinator(
        database_path=database_path,
        clock=clock,
        notify=lambda _event: None,
        ring_timeout_seconds=60,
    )
    barrier = threading.Barrier(3)
    outcomes: dict[str, dict[str, object]] = {}

    def race(name: str, operation) -> None:
        barrier.wait()
        outcomes[name] = operation()

    accept_thread = threading.Thread(
        target=race,
        args=(
            "accept",
            lambda: accept_client.accept(
                call_id=call_id,
                device_id="terminal-without-iphone-in-its-id",
                terminal_category="iphone_web",
                terminal_platform="ios",
            ),
        ),
    )
    decline_thread = threading.Thread(
        target=race,
        args=("decline", lambda: decline_client.decline(call_id, "desktop-1")),
    )
    accept_thread.start()
    decline_thread.start()
    barrier.wait()
    accept_thread.join(timeout=5)
    decline_thread.join(timeout=5)

    assert not accept_thread.is_alive()
    assert not decline_thread.is_alive()
    winners = [
        outcome
        for outcome in outcomes.values()
        if outcome["status"] in {"opened", "declined"}
    ]
    assert len(winners) == 1
    terminal = creator.get_call(call_id)
    assert terminal["call_state"] in {"accepted", "declined"}
    if terminal["call_state"] == "accepted":
        assert terminal["owner_device_id"] == "terminal-without-iphone-in-its-id"
        assert terminal["terminal_platform"] == "ios"
        assert outcomes["decline"]["status"] == "already_accepted"
    else:
        assert terminal["reason"] == "human_declined"
        assert outcomes["accept"]["status"] == "already_declined"


def test_deadline_is_strict_and_expiry_is_persisted_as_not_accepted(
    coordinator: CallCoordinator, clock: FakeClock
) -> None:
    created = create(coordinator)
    call_id = created["call_id"]
    clock.advance(60)

    # A request path must settle expiry even if a scheduler has not run yet.
    late_accept = coordinator.accept(
        call_id=call_id,
        device_id="opaque-late-device",
        terminal_category="iphone_web",
        terminal_platform="ios",
    )
    assert late_accept == {
        "success": False,
        "status": "expired",
        "call_state": "not_accepted",
        "reason": "ring_timeout",
    }
    assert coordinator.get_call(call_id) == {
        "call_id": call_id,
        "call_state": "not_accepted",
        "reason": "ring_timeout",
    }


def test_expire_due_releases_busy_slot_and_returns_terminal_event(
    coordinator: CallCoordinator, clock: FakeClock
) -> None:
    created = create(coordinator)
    clock.advance(60)

    expired = coordinator.expire_due()

    assert expired == [{
        "call_id": created["call_id"],
        "status": "not_accepted",
        "call_state": "not_accepted",
        "reason": "ring_timeout",
        "human_action": False,
    }]
    next_call = coordinator.create_call(
        caller_id="agent-b",
        request_id="other-create-2",
        payload={"tool": "get_confirmation", "prompt": "Now available?"},
    )
    assert next_call["status"] == "ringing"


def test_restart_recovery_keeps_original_deadline_and_expires_without_reringing(
    tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]]
) -> None:
    database_path = tmp_path / "calls.sqlite3"
    first = CallCoordinator(
        database_path=database_path,
        clock=clock,
        notify=notifications.append,
        ring_timeout_seconds=60,
    )
    created = create(first)
    first.close()
    clock.advance(61)

    restarted = CallCoordinator(
        database_path=database_path,
        clock=clock,
        notify=notifications.append,
        ring_timeout_seconds=999,
    )
    recovered = restarted.recover()

    assert recovered == [{
        "call_id": created["call_id"],
        "status": "not_accepted",
        "call_state": "not_accepted",
        "reason": "ring_timeout",
    }]
    assert restarted.get_call(created["call_id"])["call_state"] == "not_accepted"
    assert len(notifications) == 1


def test_close_releases_windows_sqlite_handles_after_persisted_restart(
    clock: FakeClock,
) -> None:
    """Leaving this TemporaryDirectory is the Windows handle-release assertion.

    Windows cannot remove an open SQLite database file.  The reopened coordinator
    must see the saved ringing call before both instances are closed, and then the
    context manager must remove its database directory without ``PermissionError``.
    """
    notifications: list[dict[str, object]] = []
    with tempfile.TemporaryDirectory() as temporary_directory:
        database_path = Path(temporary_directory) / "coordinator.sqlite3"
        first = CallCoordinator(
            database_path=database_path,
            clock=clock,
            notify=notifications.append,
            ring_timeout_seconds=60,
        )
        created = create(first, request_id="windows-handle-create")
        persisted_deadline = created["ring_deadline"]
        first.close()

        restarted = CallCoordinator(
            database_path=database_path,
            clock=clock,
            notify=notifications.append,
            ring_timeout_seconds=60,
        )
        assert restarted.get_call(created["call_id"])["call_state"] == "ringing"
        replay = restarted.create_call(
            caller_id="agent-a",
            request_id="windows-handle-create",
            payload={"tool": "get_text_input", "prompt": "Proceed?"},
        )
        assert replay["ring_deadline"] == persisted_deadline
        restarted.close()


def test_central_coordinator_module_has_no_tk_dependency() -> None:
    module = importlib.import_module("central_call_coordinator")
    source = inspect.getsource(module)
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported.update(
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    )
    assert "tkinter" not in imported
    assert "human_loop_server" not in imported
