"""Red contract for the GUI-free HTTP-only central-service adapter.

The adapter does not exist yet.  This file defines its intentionally small
public boundary so an implementation can be exercised without a GUI, a real
network listener, or a production deployment:

* ``CentralServiceConfig.from_mapping(mapping)`` validates the portable
  ``listen_host``, ``listen_port``, ``web_base_url``, ``data_dir``, agent
  credential mapping, and TLS certificate/key *references*. This test does
  not load a real certificate or start TLS.
* ``create_app(config, *, clock, notify, wait)`` returns an ASGI application.
  ``notify`` is the outbound notification seam; ``wait(seconds)`` is an async
  clock/test seam used only to make the MCP wait-leg observable without a
  wall-clock sleep.
* ``POST /mcp`` accepts a Bearer credential plus the deliberately minimal JSON
  ingress used in this slice: ``request_id``, ``payload`` and
  ``client_timeout_seconds``. The credential mapping, not a body value,
  determines ``caller_id``. A continuation additionally supplies
  ``interaction_id`` and ``call_id``. This is an adapter contract, not a
  claim that the final MCP wire representation is complete.
* ``main(argv)`` starts only the central HTTP entry and rejects legacy
  ``--stdio`` and ``--local`` options explicitly.

The bounded tests below cover only SPEC §§2.1, 2.4, 6.1, 7, 7.1 and 10.3:
configuration, credential-derived central ingress, coordinator replay/busy
delegation, streamed response headers and wait-leg calculation, restart replay,
and old-entry rejection. They do not claim production credential storage, real
TLS deployment, Bark, desktop/mobile UX, attachments, or full MCP compliance.

Continuation authorization additionally covers SPEC §§7.1 and 9.3: both IDs
must identify the same call owned by the credential-derived caller. An invalid
pair is a non-success, non-disclosing error; it must not reveal call state,
results, or identifiers, emit a notification, or begin a wait leg.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest


def _adapter():
    """Import lazily so the legacy-coordinator control still runs in Red."""

    return importlib.import_module("central_http_service")


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingWait:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        self.clock.advance(seconds)


class HeaderGateWait:
    """Keep a wait leg pending until the ASGI response-start event is sent."""

    def __init__(self) -> None:
        self.seconds: list[float] = []
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.completed_at: float | None = None

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        self.entered.set()
        await self.release.wait()
        self.completed_at = time.monotonic()


def config_mapping(tmp_path: Path, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "web_base_url": "https://10.0.0.1:8765",
        "data_dir": str(tmp_path / "central-data"),
        "agent_credentials": {"agent-a": "token-a", "agent-b": "token-b"},
        "tls_cert_file": "test-cert-reference.pem",
        "tls_key_file": "test-key-reference.pem",
    }
    config.update(overrides)
    return config


def make_app(
    tmp_path: Path,
    clock: FakeClock,
    notifications: list[dict[str, object]],
    waiter: Callable[[float], Awaitable[None]],
):
    adapter = _adapter()
    config = adapter.CentralServiceConfig.from_mapping(config_mapping(tmp_path))
    return adapter.create_app(
        config,
        clock=clock,
        notify=notifications.append,
        wait=waiter,
    )


def post(
    app: Any, payload: dict[str, object], bearer_token: str | None = "token-a"
) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            headers = (
                {"Authorization": f"Bearer {bearer_token}"}
                if bearer_token is not None
                else {}
            )
            return await client.post("/mcp", json=payload, headers=headers)

    return asyncio.run(request())


def test_legacy_coordinator_control_remains_gui_free(tmp_path: Path) -> None:
    """Passing control: the already-integrated coordinator remains GUI-free."""

    coordinator_module = importlib.import_module("central_call_coordinator")
    assert "tkinter" not in inspect.getsource(coordinator_module).lower()
    assert not (tmp_path / "unused").exists()


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"listen_host": ""}, "listen_host"),
        ({"listen_port": 0}, "listen_port"),
        ({"web_base_url": "not-a-url"}, "web_base_url"),
        ({"data_dir": ""}, "data_dir"),
        ({"agent_credentials": {}}, "agent_credentials"),
        ({"tls_cert_file": ""}, "tls_cert_file"),
        ({"tls_key_file": ""}, "tls_key_file"),
    ],
)
def test_config_rejects_missing_or_invalid_environment_neutral_values(
    tmp_path: Path, override: dict[str, object], field: str
) -> None:
    adapter = _adapter()
    with pytest.raises(ValueError, match=field):
        adapter.CentralServiceConfig.from_mapping(config_mapping(tmp_path, **override))


def test_config_rejects_an_existing_file_as_data_directory(tmp_path: Path) -> None:
    adapter = _adapter()
    data_file = tmp_path / "not-a-directory"
    data_file.write_text("not a directory", encoding="utf-8")
    with pytest.raises(ValueError, match="data_dir"):
        adapter.CentralServiceConfig.from_mapping(
            config_mapping(tmp_path, data_dir=str(data_file))
        )


def test_adapter_source_has_no_gui_or_legacy_server_dependency() -> None:
    adapter = _adapter()
    source = inspect.getsource(adapter).lower()
    assert "tkinter" not in source
    assert "human_loop_server" not in source


def test_invalid_timeout_is_rejected_before_notification(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)

    response = post(
        make_app(tmp_path, clock, notifications, waiter),
        {
            "request_id": "bad-timeout",
            "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
            "client_timeout_seconds": 1,
        },
    )

    assert response.status_code == 400
    assert response.json()["status"] == "invalid_client_timeout"
    assert notifications == []
    assert waiter.seconds == []


def test_mcp_ingress_replays_request_and_returns_busy_without_queueing(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)
    app = make_app(tmp_path, clock, notifications, waiter)
    initial = {
        "caller_id": "spoofed-body-identity",
        "request_id": "call-1",
        "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
        "client_timeout_seconds": 2,
    }

    first = post(app, initial)
    replay = post(app, {**initial, "caller_id": "another-spoofed-body-value"})
    busy = post(
        app,
        {
            **initial,
            "caller_id": "agent-a",
            "request_id": "call-2",
            "payload": {"tool": "get_confirmation", "prompt": "Busy?"},
        },
        bearer_token="token-b",
    )

    assert first.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["status"] == "heartbeat"
    assert first.json()["call_state"] == "ringing"
    assert first.json()["caller_id"] == "agent-a"
    assert busy.status_code == 200
    assert busy.json() == {
        "success": True,
        "status": "busy",
        "call_state": None,
        "call_id": None,
        "needs_continuation": False,
        "retryable": True,
        "retry_after_seconds": 30,
    }
    assert len(notifications) == 1


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_mcp_rejects_missing_or_invalid_agent_credential_before_notification(
    tmp_path: Path, token: str | None
) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)
    response = post(
        make_app(tmp_path, clock, notifications, waiter),
        {
            "caller_id": "spoofed-agent-a",
            "request_id": "unauthorized",
            "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
            "client_timeout_seconds": 2,
        },
        bearer_token=token,
    )
    assert response.status_code == 401
    assert notifications == []
    assert waiter.seconds == []


def test_response_headers_arrive_within_one_second_and_wait_leg_is_safe(
    tmp_path: Path,
) -> None:
    """Observe ``http.response.start`` separately from the delayed body leg."""

    async def exercise() -> tuple[HeaderGateWait, float, list[dict[str, object]]]:
        import json

        clock = FakeClock()
        gate = HeaderGateWait()
        app = make_app(tmp_path, clock, [], gate)
        body = json.dumps(
            {
                "request_id": "call-1",
                "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
                "client_timeout_seconds": 8,
            }
        ).encode()
        sent_request = False
        events: list[dict[str, object]] = []
        response_started = asyncio.Event()
        started_at: float | None = None

        async def receive() -> dict[str, object]:
            nonlocal sent_request
            if not sent_request:
                sent_request = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            raise AssertionError("request body has already been delivered")

        async def send(event: dict[str, object]) -> None:
            nonlocal started_at
            events.append(event)
            if event["type"] == "http.response.start":
                started_at = time.monotonic()
                response_started.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/mcp",
            "raw_path": b"/mcp",
            "query_string": b"",
            "headers": [
                (b"content-type", b"application/json"),
                (b"authorization", b"Bearer token-a"),
            ],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        launch = time.monotonic()
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        await asyncio.wait_for(response_started.wait(), timeout=1)
        assert started_at is not None
        assert started_at - launch < 1
        assert gate.completed_at is None
        gate.release.set()
        await task
        return gate, started_at, events

    gate, _, events = asyncio.run(exercise())
    assert gate.seconds == [6]
    assert gate.completed_at is not None
    assert events[0]["type"] == "http.response.start"


def test_continuation_uses_same_call_and_never_notifies_again_after_restart(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    first_notifications: list[dict[str, object]] = []
    first_waiter = RecordingWait(clock)
    first_app = make_app(tmp_path, clock, first_notifications, first_waiter)
    initial = post(
        first_app,
        {
            "request_id": "call-1",
            "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
            "client_timeout_seconds": 4,
        },
    ).json()

    restarted_notifications: list[dict[str, object]] = []
    restarted_waiter = RecordingWait(clock)
    restarted_app = make_app(tmp_path, clock, restarted_notifications, restarted_waiter)
    continuation = post(
        restarted_app,
        {
            "request_id": "continue-1",
            "interaction_id": initial["interaction_id"],
            "call_id": initial["call_id"],
            "after_event_seq": 0,
            "client_timeout_seconds": 4,
        },
    )

    assert continuation.status_code == 200
    assert initial["interaction_id"] != initial["call_id"]
    assert initial["interaction_id"]
    assert initial["call_id"]
    assert continuation.json()["interaction_id"] == initial["interaction_id"]
    assert continuation.json()["call_id"] == initial["call_id"]
    assert continuation.json()["status"] == "heartbeat"
    assert first_notifications and restarted_notifications == []
    assert restarted_waiter.seconds == [3]


def assert_hidden_continuation_error(response: httpx.Response) -> None:
    """Bound an invalid continuation to a non-success, non-disclosing result."""

    assert response.status_code in {400, 404}
    body = response.json()
    assert body["success"] is False
    assert body["status"] in {
        "session_not_found",
        "missing_interaction_id",
        "mismatched_interaction_id",
    }
    for key in ("interaction_id", "call_id", "call_state", "result_id"):
        assert body.get(key) is None


def start_call(
    app: Any, request_id: str, *, bearer_token: str = "token-a"
) -> dict[str, object]:
    response = post(
        app,
        {
            "request_id": request_id,
            "payload": {"tool": "get_text_input", "prompt": request_id},
            "client_timeout_seconds": 4,
        },
        bearer_token=bearer_token,
    )
    assert response.status_code == 200
    return response.json()


def test_other_agent_cannot_continue_a_genuine_call_or_learn_its_identifiers(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)
    app = make_app(tmp_path, clock, notifications, waiter)
    initial = start_call(app, "call-1")
    notification_count, wait_count = len(notifications), len(waiter.seconds)

    response = post(
        app,
        {
            "request_id": "continue-by-other-agent",
            "interaction_id": initial["interaction_id"],
            "call_id": initial["call_id"],
            "after_event_seq": 0,
            "client_timeout_seconds": 4,
        },
        bearer_token="token-b",
    )

    assert_hidden_continuation_error(response)
    assert len(notifications) == notification_count
    assert len(waiter.seconds) == wait_count


def test_owner_cannot_continue_with_an_arbitrary_interaction_id(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)
    app = make_app(tmp_path, clock, notifications, waiter)
    initial = start_call(app, "call-1")
    notification_count, wait_count = len(notifications), len(waiter.seconds)

    response = post(
        app,
        {
            "request_id": "bad-interaction-id",
            "interaction_id": "not-the-created-interaction",
            "call_id": initial["call_id"],
            "after_event_seq": 0,
            "client_timeout_seconds": 4,
        },
    )

    assert_hidden_continuation_error(response)
    assert len(notifications) == notification_count
    assert len(waiter.seconds) == wait_count


def test_owner_cannot_mix_interaction_and_call_ids_from_two_calls(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)
    app = make_app(tmp_path, clock, notifications, waiter)
    first = start_call(app, "call-1")
    clock.advance(60)
    second = start_call(app, "call-2")
    notification_count, wait_count = len(notifications), len(waiter.seconds)

    response = post(
        app,
        {
            "request_id": "mixed-pair",
            "interaction_id": first["interaction_id"],
            "call_id": second["call_id"],
            "after_event_seq": 0,
            "client_timeout_seconds": 4,
        },
    )

    assert first["interaction_id"] != second["interaction_id"]
    assert_hidden_continuation_error(response)
    assert len(notifications) == notification_count
    assert len(waiter.seconds) == wait_count


@pytest.mark.parametrize("legacy_option", ["--stdio", "--local"])
def test_main_explicitly_rejects_legacy_entry_modes(legacy_option: str) -> None:
    adapter = _adapter()
    with pytest.raises(SystemExit) as exited:
        adapter.main([legacy_option])
    assert exited.value.code != 0
