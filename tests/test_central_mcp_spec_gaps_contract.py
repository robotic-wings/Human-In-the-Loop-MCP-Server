"""Red contract for SPEC gaps in terminal results, settlement order and health.

These pin three SPEC §3/§7 requirements the current implementation omits:

* terminal results carry the full field set
  (``human_action``/``retryable``/``retry_after_seconds``/``handled_on``/
  ``terminal_platform``/``message``);
* ``busy`` is retryable with a suggested delay, and ``declined`` is not
  retryable;
* ``_settle_due`` picks the earliest expired disconnect deadline (agent
  disconnect before terminal disconnect when both are due and agent is
  earlier);
* ``health_check`` reports desktop-online status and Bark configuration.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_mcp_waiting_lifecycle import create_app
from central_notification_router import NotificationRouter, TerminalRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_wait(_: float) -> None:
    return None


def make_app(tmp_path: Path, clock: Clock) -> Any:
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock, notify=lambda _event: None, wait=no_wait,
    )


def components(tmp_path: Path, clock: Clock) -> Any:
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-owner",
        endpoint="https://desktop.test/events", callback_credential="callback-secret", gui_ready=False,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        operator_directory={
            "desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True},
        }, device_state=lambda _device: [],
    )
    lifecycle_path = tmp_path / "lifecycle.sqlite3"

    async def desktop_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute("SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
        route_mode="both", call_state=call_state, web_base_url="https://central.test",
    )
    return create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_directory={"desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True}},
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, terminal: str | None = None,
            authorization: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        if terminal: headers["x-terminal-session"] = terminal
        if authorization: headers["authorization"] = authorization
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def register_terminal(app: Any) -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "callsign": "desktop-owner", "password": "desktop-secret", "device_id": "desktop-owner", "terminal_platform": "windows",
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    }, authorization="Terminal desktop-secret")
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def create_call(app: Any, caller: str = "agent-a", request_id: str = "create") -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret" if caller == "agent-a" else "agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2,
        "payload": {"tool": "get_text_input", "prompt": "proceed?"},
    })
    assert response.status_code == 200
    return response.json()


def rpc_call(app: Any, name: str, arguments: dict[str, object], rid: int = 1) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    return dict(response.json()["result"]["structuredContent"])


def test_not_accepted_ring_timeout_has_complete_terminal_fields(tmp_path: Path) -> None:
    clock = Clock()
    app = make_app(tmp_path, clock)
    ringing = app.lifecycle.create("agent-a", "r1", {"tool": "text"})
    clock.advance(60)
    result = app.lifecycle.continue_call("agent-a", "r2", ringing["interaction_id"], ringing["call_id"], 0)

    assert result["status"] == "not_accepted"
    assert result["reason"] == "ring_timeout"
    assert result["human_action"] is False
    assert result["retryable"] is True
    assert result["retry_after_seconds"] == 60
    assert result["handled_on"] is None
    assert result["terminal_platform"] is None
    assert isinstance(result["message"], str) and result["message"]


def test_busy_result_is_retryable_with_retry_after(tmp_path: Path) -> None:
    clock = Clock()
    app = make_app(tmp_path, clock)
    app.lifecycle.create("agent-a", "r1", {"tool": "text"})
    busy = app.lifecycle.create("agent-b", "r2", {"tool": "text"})

    assert busy["status"] == "busy"
    assert busy["retryable"] is True
    assert isinstance(busy["retry_after_seconds"], int)


def test_declined_result_is_not_retryable(tmp_path: Path) -> None:
    clock = Clock()
    app = components(tmp_path, clock)
    session = register_terminal(app)
    call = create_call(app)
    response = request(app, "POST", f"/api/calls/{call['call_id']}/decline", terminal=session, body={"operation_id": "decline-1"})
    assert response.status_code == 200
    assert response.json()["status"] == "declined"
    assert response.json()["retryable"] is False


def test_settlement_uses_earliest_disconnect_deadline(tmp_path: Path) -> None:
    clock = Clock()
    app = make_app(tmp_path, clock)
    ringing = app.lifecycle.create("agent-a", "r1", {"tool": "text"})
    app.lifecycle.accept(call_id=ringing["call_id"], device_id="desktop", handled_on="desktop", terminal_platform="windows")

    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        conn.execute(
            "UPDATE lifecycle_calls SET terminal_disconnect_deadline=?, agent_reconnect_deadline=? WHERE call_id=?",
            (clock() + 100, clock() + 50, ringing["call_id"]),
        )
    clock.advance(200)

    result = app.lifecycle.continue_call("agent-a", "r2", ringing["interaction_id"], ringing["call_id"], after=1)
    assert result["reason"] == "agent_disconnected"


def test_health_check_reports_desktop_online_and_bark_config(tmp_path: Path) -> None:
    clock = Clock()
    app = components(tmp_path, clock)
    register_terminal(app)

    result = rpc_call(app, "health_check", {})

    assert result["success"] is True
    assert "desktop_online" in result
    assert isinstance(result["desktop_online"], bool)
    assert "bark" in result
    assert isinstance(result["bark"]["configured"], bool)
