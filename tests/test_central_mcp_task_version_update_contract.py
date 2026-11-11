"""Red contract for active task-description updates (SPEC §7.1).

A task in ``in_progress`` may have its description/context updated by the Agent
with a matching ``expected_task_version``; the server bumps ``task_version``
and ``version``, updates the stored description, and does not re-ring.  A stale
``expected_task_version`` is a ``task_version_conflict``.
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


def components(tmp_path: Path) -> tuple[Any, list[dict[str, object]]]:
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
    notifications: list[dict[str, object]] = []

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
        clock=clock, notify=lambda e: (notifications.append(e), router.enqueue(e)), wait=no_wait,
        route_admission=router.validate_route, notification_router=router,
        desktop_registration_service=registration,
    )
    return app, notifications


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


def submit(app: Any, call: dict[str, object], session: str, result: dict[str, object]) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=session, body={
        "submission_id": "in-progress", "result": result,
    })
    assert response.status_code == 200


def update_task(app: Any, call: dict[str, object], expected_task_version: int,
                task_description: str | None = None, request_id: str = "update") -> httpx.Response:
    body: dict[str, object] = {
        "request_id": request_id, "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"],
        "expected_task_version": expected_task_version,
    }
    if task_description is not None:
        body["task_description"] = task_description
    return request(app, "POST", "/mcp", agent="agent-secret", body=body)


def _task_version(tmp_path: Path, call_id: str) -> int:
    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        row = conn.execute("SELECT task_version FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
    return int(row[0])


def test_task_version_column_exists(tmp_path: Path) -> None:
    app, _ = components(tmp_path)
    app.lifecycle.create("agent-a", "r1", {"tool": "task"})
    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(lifecycle_calls)")}
    assert "task_version" in columns


def test_task_description_update_increments_version_without_rerunning(tmp_path: Path) -> None:
    app, notifications = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit(app, call, owner, {"status": "in_progress", "progress": "halfway"})
    before = _task_version(tmp_path, str(call["call_id"]))

    response = update_task(app, call, expected_task_version=before, task_description="updated description")
    assert response.status_code == 200
    assert response.json()["success"] is True

    after = _task_version(tmp_path, str(call["call_id"]))
    assert after == before + 1
    assert len(notifications) == 1


def test_stale_expected_task_version_is_conflict(tmp_path: Path) -> None:
    app, _ = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit(app, call, owner, {"status": "in_progress", "progress": "halfway"})

    response = update_task(app, call, expected_task_version=0, task_description="stale")
    assert response.status_code in {400, 404}
    assert response.json()["success"] is False
    assert response.json()["status"] == "task_version_conflict"
