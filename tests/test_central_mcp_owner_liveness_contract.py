"""Red contract for owner-liveness refresh on valid operations (SPEC §8).

Upload, draft and submit are valid owner operations that must refresh this
round's ``terminal_last_seen_at``; a read from the other end must not.
"""

from __future__ import annotations

import asyncio
import base64
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


class BrowserVerifier:
    def __init__(self) -> None:
        self.sessions = {
            "operator": {"operator_id": "only-operator", "browser_session_id": "browser-owner"},
        }

    def verify(self, token: str | None) -> dict[str, str] | None:
        return self.sessions.get(token or "")


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path) -> tuple[Any, Clock]:
    clock = Clock()
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
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
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_session_verifier=BrowserVerifier(),
    )
    return app, clock


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, terminal: str | None = None, browser: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        if terminal: headers["x-terminal-session"] = terminal
        if browser: headers["x-operator-session"] = browser
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def create_task(app: Any, request_id: str = "task-create") -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2,
        "payload": {"tool": "task", "task_description": "Deploy"},
    })
    assert response.status_code == 200
    return response.json()


def terminal_session(app: Any) -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "callsign": "desktop-owner", "password": "desktop-secret", "device_id": "desktop-owner", "terminal_platform": "windows",
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    })
    if response.status_code != 201:
        async def register() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
                return await client.post("/api/terminals/register", json={
                    "callsign": "desktop-owner", "password": "desktop-secret", "device_id": "desktop-owner", "terminal_platform": "windows",
                    "endpoint": "https://desktop.test/events", "gui_ready": True,
                })
        response = asyncio.run(register())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session,
                       body={"operation_id": "desktop-accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def _terminal_last_seen_at(tmp_path: Path, call_id: str) -> float:
    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        row = conn.execute(
            "SELECT terminal_last_seen_at FROM lifecycle_calls WHERE call_id=?", (call_id,)
        ).fetchone()
    return float(row[0])


def test_submit_refreshes_owner_liveness(tmp_path: Path) -> None:
    app, clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    before = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    clock.advance(10)
    request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "sub-1", "result": {"status": "completed", "report": "done"},
    })
    after = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    assert after > before


def test_upload_refreshes_owner_liveness(tmp_path: Path) -> None:
    app, clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    before = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    clock.advance(10)
    request(app, "POST", f"/api/calls/{call['call_id']}/attachments", terminal=owner, body={
        "filename": "file.bin", "media_type": "application/octet-stream",
        "content": base64.b64encode(b"data").decode("ascii"),
    })
    after = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    assert after > before


def test_save_draft_refreshes_owner_liveness(tmp_path: Path) -> None:
    app, clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    before = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    clock.advance(10)
    request(app, "PUT", f"/api/calls/{call['call_id']}/draft", terminal=owner, body={
        "draft_version": 1, "content": "draft",
    })
    after = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    assert after > before


def test_other_end_read_does_not_refresh_liveness(tmp_path: Path) -> None:
    app, clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    before = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    clock.advance(10)
    request(app, "GET", f"/calls/{call['call_id']}", browser="operator")
    after = _terminal_last_seen_at(tmp_path, str(call["call_id"]))
    assert after == before
