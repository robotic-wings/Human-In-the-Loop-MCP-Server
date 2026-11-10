"""Red contract for submission carrying task_version (SPEC §7.1)."""

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


def components(tmp_path: Path) -> Any:
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
    return create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
    )


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
        "payload": {"tool": "task", "task_description": "Deploy"},
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


def submit(app: Any, call: dict[str, object], session: str, result: dict[str, object]) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=session, body={
        "submission_id": "sub-1", "result": result,
    })
    assert response.status_code == 200


def update_task(app: Any, call: dict[str, object], expected_task_version: int,
                task_description: str) -> None:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": "update", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"],
        "expected_task_version": expected_task_version, "task_description": task_description,
    })
    assert response.status_code == 200
    assert response.json()["success"] is True


def submit_with_version(app: Any, call: dict[str, object], session: str,
                        result: dict[str, object], task_version: int) -> httpx.Response:
    return request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=session, body={
        "submission_id": "sub-2", "result": result, "task_version": task_version,
    })


def test_stale_submission_task_version_is_rejected(tmp_path: Path) -> None:
    app = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit(app, call, owner, {"status": "in_progress", "progress": "halfway"})
    update_task(app, call, expected_task_version=1, task_description="updated")

    response = submit_with_version(app, call, owner, {"status": "completed", "report": "done"}, task_version=1)
    assert response.status_code in {400, 404}
    assert response.json()["success"] is False
    assert response.json()["status"] == "task_version_conflict"


def test_matching_submission_task_version_succeeds(tmp_path: Path) -> None:
    app = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit(app, call, owner, {"status": "in_progress", "progress": "halfway"})
    update_task(app, call, expected_task_version=1, task_description="updated")

    response = submit_with_version(app, call, owner, {"status": "completed", "report": "done"}, task_version=2)
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert response.json()["status"] == "completed"
