"""Red contract for the GUI-free HTTP-only central-service adapter.

The adapter does not exist yet.  This file defines its intentionally small
public boundary so an implementation can be exercised without a GUI, a real
network listener, or a production deployment:

* ``CentralServiceConfig.from_mapping(mapping)`` validates the portable
  ``listen_host``, ``listen_port``, ``web_base_url`` and ``data_dir`` values.
* ``create_app(config, *, clock, notify, wait)`` returns an ASGI application.
  ``notify`` is the outbound notification seam; ``wait(seconds)`` is an async
  clock/test seam used only to make the MCP wait-leg observable without a
  wall-clock sleep.
* ``POST /mcp`` accepts the deliberately minimal JSON ingress used in this
  slice: ``caller_id``, ``request_id``, ``payload`` and
  ``client_timeout_seconds``.  A continuation additionally supplies
  ``interaction_id`` and ``call_id``.  This is an adapter contract, not a
  claim that the final MCP wire representation is complete.
* ``main(argv)`` starts only the central HTTP entry and rejects legacy
  ``--stdio`` and ``--local`` options explicitly.

The bounded tests below cover only SPEC §§2.1, 2.4, 6.1, 7, 7.1 and 10.3:
configuration, central ingress, coordinator replay/busy delegation, response
headers and wait-leg calculation, restart replay, and old-entry rejection.
They do not claim authentication, HTTPS, Bark, desktop/mobile UX, attachments,
or full MCP protocol compliance.
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


def config_mapping(tmp_path: Path, **overrides: object) -> dict[str, object]:
    config: dict[str, object] = {
        "listen_host": "127.0.0.1",
        "listen_port": 8765,
        "web_base_url": "https://10.0.0.1:8765",
        "data_dir": str(tmp_path / "central-data"),
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


def post(app: Any, payload: dict[str, object]) -> httpx.Response:
    async def request() -> httpx.Response:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/mcp", json=payload)

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
    ],
)
def test_config_rejects_missing_or_invalid_environment_neutral_values(
    tmp_path: Path, override: dict[str, object], field: str
) -> None:
    adapter = _adapter()
    with pytest.raises(ValueError, match=field):
        adapter.CentralServiceConfig.from_mapping(config_mapping(tmp_path, **override))


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
            "caller_id": "agent-a",
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
        "caller_id": "agent-a",
        "request_id": "call-1",
        "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
        "client_timeout_seconds": 2,
    }

    first = post(app, initial)
    replay = post(app, initial)
    busy = post(
        app,
        {
            **initial,
            "caller_id": "agent-b",
            "request_id": "call-2",
            "payload": {"tool": "get_confirmation", "prompt": "Busy?"},
        },
    )

    assert first.status_code == 200
    assert replay.json() == first.json()
    assert first.json()["status"] == "heartbeat"
    assert first.json()["call_state"] == "ringing"
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


def test_response_headers_arrive_within_one_second_and_wait_leg_is_safe(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait(clock)
    app = make_app(tmp_path, clock, notifications, waiter)
    started = time.monotonic()
    response = post(
        app,
        {
            "caller_id": "agent-a",
            "request_id": "call-1",
            "payload": {"tool": "get_text_input", "prompt": "Proceed?"},
            "client_timeout_seconds": 8,
        },
    )
    elapsed = time.monotonic() - started

    assert response.status_code == 200
    assert elapsed < 1
    assert waiter.seconds == [6]
    assert response.json()["status"] == "heartbeat"
    assert response.json()["needs_continuation"] is True
    assert response.json()["interaction_id"] == response.json()["call_id"]


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
            "caller_id": "agent-a",
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
            "caller_id": "agent-a",
            "request_id": "continue-1",
            "interaction_id": initial["interaction_id"],
            "call_id": initial["call_id"],
            "after_event_seq": 0,
            "client_timeout_seconds": 4,
        },
    )

    assert continuation.status_code == 200
    assert continuation.json()["call_id"] == initial["call_id"]
    assert continuation.json()["status"] == "heartbeat"
    assert first_notifications and restarted_notifications == []
    assert restarted_waiter.seconds == [3]


@pytest.mark.parametrize("legacy_option", ["--stdio", "--local"])
def test_main_explicitly_rejects_legacy_entry_modes(legacy_option: str) -> None:
    adapter = _adapter()
    with pytest.raises(SystemExit) as exited:
        adapter.main([legacy_option])
    assert exited.value.code != 0
