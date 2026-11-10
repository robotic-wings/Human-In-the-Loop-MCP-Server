"""Red contract for the owner draft endpoint ``PUT /api/calls/<id>/draft``.

Draft saves are not answer submissions: they persist partial human input with a
monotonic ``draft_version`` so an older autosave can never overwrite a newer
one, and the call stays ``accepted`` throughout.
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
        registered_credentials={
            "desktop-secret": {"device_id": "desktop-owner", "terminal_platform": "windows"},
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
    )
    return app, clock


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, terminal: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        if terminal: headers["x-terminal-session"] = terminal
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def create_task(app: Any, request_id: str = "task-create") -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2,
        "payload": {"tool": "task", "task_description": "Deploy the service"},
    })
    assert response.status_code == 200
    return response.json()


def terminal_session(app: Any, credential: str = "desktop-secret") -> str:
    endpoint = "https://desktop.test/events"
    response = request(app, "POST", "/api/terminals/register", body={"endpoint": endpoint, "gui_ready": True})
    if response.status_code != 201:
        async def register() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
                return await client.post("/api/terminals/register", headers={"authorization": f"Terminal {credential}"}, json={"endpoint": endpoint, "gui_ready": True})
        response = asyncio.run(register())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session,
                       body={"operation_id": "desktop-accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def test_draft_save_is_not_answer_submission(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    response = request(app, "PUT", f"/api/calls/{call['call_id']}/draft", terminal=owner,
                       body={"draft_version": 1, "content": "draft text"})
    assert response.status_code == 200
    assert response.json()["success"] is True

    view = request(app, "GET", f"/api/calls/{call['call_id']}", terminal=owner)
    assert view.status_code == 200
    assert view.json()["call_state"] == "accepted"


def test_stale_draft_version_is_rejected(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    first = request(app, "PUT", f"/api/calls/{call['call_id']}/draft", terminal=owner,
                    body={"draft_version": 1, "content": "newer"})
    assert first.status_code == 200

    stale = request(app, "PUT", f"/api/calls/{call['call_id']}/draft", terminal=owner,
                    body={"draft_version": 0, "content": "older"})
    assert stale.status_code in {400, 404}
    assert stale.json()["success"] is False
    assert stale.json()["status"] == "draft_version_conflict"


def test_foreign_session_cannot_write_draft(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    foreign = terminal_session(app)
    response = request(app, "PUT", f"/api/calls/{call['call_id']}/draft", terminal=foreign,
                       body={"draft_version": 1, "content": "steal"})
    assert response.status_code in {401, 404}
    assert response.json()["success"] is False
    assert response.json()["status"] == "session_not_found"
