"""Red contract for the first GUI-free central call-coordination slice.

The production module deliberately does not exist on the restored baseline.  These
tests define the small, persistent coordinator API that will replace no part of the
legacy GUI yet: ``central_call_coordinator.CallCoordinator`` with ``create_call``,
``accept``, ``decline`` and ``expire_due``.  Returned values are mappings so the
HTTP/MCP layers can later serialise them without leaking ORM objects.
"""

from __future__ import annotations

import ast
import importlib
import inspect
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
    create(coordinator)

    conflict = create(coordinator, prompt="A different question")

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

    accepted = coordinator.accept(call_id=call_id, device_id="iphone-1")
    declined = coordinator.decline(call_id=call_id, device_id="desktop-1")

    assert accepted["status"] == "opened"
    assert accepted["call_state"] == "accepted"
    assert accepted["handled_on"] == "iphone_web"
    assert declined == {
        "success": False,
        "status": "already_accepted",
        "call_state": "accepted",
    }


def test_deadline_is_strict_and_expiry_is_persisted_as_not_accepted(
    coordinator: CallCoordinator, clock: FakeClock
) -> None:
    created = create(coordinator)
    call_id = created["call_id"]
    clock.advance(60)

    # A request path must settle expiry even if a scheduler has not run yet.
    late_accept = coordinator.accept(call_id=call_id, device_id="iphone-1")
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
