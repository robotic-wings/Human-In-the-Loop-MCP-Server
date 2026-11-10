"""Red contract for task resubmission (SPEC §7.1 revision_id).

A completed/failed task may be resubmitted with a fresh ``revision_id``: the
server keeps the original ``task_id`` (``interaction_id``) but opens a new call
round with a new ``call_id``.  The same ``revision_id`` with the same content
replays the already-created round, while a different content is a conflict.
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


def terminal_session(app: Any) -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    })
    if response.status_code != 201:
        async def register() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
                return await client.post("/api/terminals/register", headers={"authorization": "Terminal desktop-secret"}, json={
                    "endpoint": "https://desktop.test/events", "gui_ready": True,
                })
        response = asyncio.run(register())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session,
                       body={"operation_id": "desktop-accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def submit_completed(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=session, body={
        "submission_id": "sub-1", "result": {"status": "completed", "report": "done"},
    })
    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def resubmit(app: Any, call: dict[str, object], revision_id: str,
             payload: dict[str, object], request_id: str = "resubmit") -> httpx.Response:
    return request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"],
        "revision_id": revision_id, "payload": payload,
    })


def test_resubmission_creates_new_round_with_same_task_id(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit_completed(app, call, owner)

    response = resubmit(app, call, "rev-1", {"tool": "task", "task_description": "redo"})
    assert response.status_code == 200
    body = response.json()
    assert body["call_state"] == "ringing"
    assert body["interaction_id"] == call["interaction_id"]
    assert body["call_id"] != call["call_id"]


def test_same_revision_id_and_content_replays_same_round(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit_completed(app, call, owner)

    first = resubmit(app, call, "rev-1", {"tool": "task", "task_description": "redo"}, "resubmit-1")
    assert first.status_code == 200
    assert first.json()["call_id"] != call["call_id"]
    second = resubmit(app, call, "rev-1", {"tool": "task", "task_description": "redo"}, "resubmit-2")
    assert second.status_code == 200
    assert second.json()["call_id"] == first.json()["call_id"]


def test_same_revision_id_different_content_is_conflict(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit_completed(app, call, owner)

    resubmit(app, call, "rev-1", {"tool": "task", "task_description": "redo"}, "resubmit-1")
    conflict = resubmit(app, call, "rev-1", {"tool": "task", "task_description": "different"}, "resubmit-2")
    assert conflict.status_code in {400, 404}
    assert conflict.json()["success"] is False
    assert conflict.json()["status"] == "revision_conflict"
