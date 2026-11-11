"""Red contract for the central quiet-window terminal HTTP route.

The lifecycle persists the quiet window (``lifecycle_quiet_window``) but exposes
no terminal route to read or change it, so the desktop settings panel cannot
configure it.  These tests pin ``GET /api/terminals/quiet`` (x-terminal-session
authenticated, returns ``{quiet_window_seconds, quiet_mode}``) and
``POST /api/terminals/quiet`` (validates ``quiet_window_seconds >= 0`` and
``quiet_mode in {short, silent}``, persisting the result).
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


async def no_wait(_: float) -> None:
    return None


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
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3", credentials={"agent-a": "agent-secret"},
        clock=clock, notify=lambda _event: None, wait=no_wait,
        desktop_registration_service=registration,
        operator_directory={"desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True}},
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            terminal: str | None = None, authorization: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
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


def get_quiet(app: Any, session: str) -> httpx.Response:
    return request(app, "GET", "/api/terminals/quiet", terminal=session)


def set_quiet(app: Any, session: str, seconds: object, mode: object) -> httpx.Response:
    return request(app, "POST", "/api/terminals/quiet", terminal=session,
                   body={"quiet_window_seconds": seconds, "quiet_mode": mode})


def test_quiet_route_requires_terminal_session(tmp_path: Path) -> None:
    app = components(tmp_path, Clock())
    response = request(app, "GET", "/api/terminals/quiet")
    assert response.status_code == 401


def test_quiet_route_get_returns_defaults(tmp_path: Path) -> None:
    app = components(tmp_path, Clock())
    session = register_terminal(app)
    state = get_quiet(app, session).json()
    assert state["quiet_window_seconds"] == 300
    assert state["quiet_mode"] == "short"


def test_quiet_route_post_validates_and_persists(tmp_path: Path) -> None:
    clock = Clock()
    app = components(tmp_path, clock)
    session = register_terminal(app)

    stored = set_quiet(app, session, 120, "silent")
    assert stored.status_code == 200, stored.text
    assert stored.json()["quiet_window_seconds"] == 120
    assert stored.json()["quiet_mode"] == "silent"

    persisted = get_quiet(app, session).json()
    assert persisted["quiet_window_seconds"] == 120
    assert persisted["quiet_mode"] == "silent"


def test_quiet_route_rejects_invalid_seconds_and_mode(tmp_path: Path) -> None:
    app = components(tmp_path, Clock())
    session = register_terminal(app)

    for seconds, mode in [(-1, "short"), (True, "short"), (300, "loud"), (300, 7)]:
        response = set_quiet(app, session, seconds, mode)
        assert response.status_code == 400, response.text
        assert response.json()["success"] is False
        assert response.json()["status"] == "invalid_request"
