"""Red contract for a central, operator-level do-not-disturb state.

Do-not-disturb is currently a desktop-terminal-local behaviour that is not
synchronised to the phone.  The central service must become the single
authority: a persisted ``{enabled, message}`` state (default ``enabled=false``,
``message=""``) exposed through ``POST /api/terminals/dnd`` /
``GET /api/terminals/dnd``.  When DND is enabled an Agent call creation returns
``declined``/``do_not_disturb`` carrying the stored auto-reply ``message``,
without notifying desktop or Bark.  These tests stay red until that central
DND exists.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_mcp_waiting_lifecycle import create_app
from central_notification_router import TerminalRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path, clock: Clock) -> tuple[Any, list[dict[str, object]]]:
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-owner",
        endpoint="https://desktop.test/events", callback_credential="callback-secret", gui_ready=False,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        registered_credentials={
            "desktop-secret": {"device_id": "desktop-owner", "terminal_platform": "windows"},
        }, device_state=lambda _device: [],
    )
    notifications: list[dict[str, object]] = []
    app = create_app(
        database_path=tmp_path / "lifecycle.sqlite3", credentials={"agent-a": "agent-secret"},
        clock=clock, notify=notifications.append, wait=no_wait,
        desktop_registration_service=registration,
    )
    return app, notifications


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
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    }, authorization="Terminal desktop-secret")
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def set_dnd(app: Any, session: str, enabled: bool, message: str = "") -> httpx.Response:
    return request(app, "POST", "/api/terminals/dnd", body={"dnd": enabled, "message": message}, terminal=session)


def get_dnd(app: Any, session: str) -> httpx.Response:
    return request(app, "GET", "/api/terminals/dnd", terminal=session)


def create_call(app: Any, rid: int = 1) -> dict[str, object]:
    response = request(app, "POST", "/mcp", body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": "get_user_input", "arguments": {"prompt": "proceed?"}},
    }, agent="agent-secret")
    assert response.status_code == 200, response.text
    body = response.json()
    return dict(body["result"]["structuredContent"])


def test_dnd_defaults_off_with_empty_message_and_survives_restart(tmp_path: Path) -> None:
    clock = Clock()
    app1, _ = components(tmp_path, clock)
    session1 = register_terminal(app1)

    state = get_dnd(app1, session1).json()
    assert state["enabled"] is False
    assert state["message"] == ""

    stored = set_dnd(app1, session1, True, "开会中，稍后回复").json()
    assert stored["enabled"] is True
    assert stored["message"] == "开会中，稍后回复"

    app2, _ = components(tmp_path, clock)
    session2 = register_terminal(app2)
    persisted = get_dnd(app2, session2).json()
    assert persisted["enabled"] is True
    assert persisted["message"] == "开会中，稍后回复"


def test_dnd_on_returns_declined_do_not_disturb_with_message_and_no_notification(tmp_path: Path) -> None:
    clock = Clock()
    app, notifications = components(tmp_path, clock)
    session = register_terminal(app)
    set_dnd(app, session, True, "开会中，稍后回复")

    result = create_call(app)
    assert result["success"] is True
    assert result["status"] == "declined"
    assert result["reason"] == "do_not_disturb"
    assert result["message"] == "开会中，稍后回复"
    assert result["needs_continuation"] is False
    assert notifications == []


def test_dnd_off_restores_normal_ringing_call(tmp_path: Path) -> None:
    clock = Clock()
    app, _ = components(tmp_path, clock)
    session = register_terminal(app)

    set_dnd(app, session, True, "busy")
    assert create_call(app)["status"] == "declined"

    set_dnd(app, session, False)
    result = create_call(app, rid=2)
    assert result["call_state"] == "ringing"
    assert result["status"] == "heartbeat"
