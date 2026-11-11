"""Red contract for resubmission re-inviting the human (SPEC §7.1).

Opening a resubmitted round must re-check busy and re-invite the operator, so
the desktop receives a fresh ``ringing`` notification for the new ``call_id``.
The current ``resubmit`` creates the round with ``invited_desktop_id=NULL`` and
no notification, so the human is never re-asked.
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


def components(tmp_path: Path) -> tuple[Any, Clock, list[dict[str, object]]]:
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
    recorded: list[dict[str, object]] = []

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        recorded.append(dict(payload))
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
        operator_directory={"desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True}},
    )
    return app, clock, recorded


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


def test_resubmission_reinvites_the_human(tmp_path: Path) -> None:
    app, _clock, recorded = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    submit_completed(app, call, owner)

    resubmitted = resubmit(app, call, "rev-1", {"tool": "task", "task_description": "redo"})
    assert resubmitted.status_code == 200
    new_call_id = resubmitted.json()["call_id"]

    asyncio.run(app.dispatch_notification_delivery())

    ringing = [r for r in recorded if r.get("call_id") == new_call_id]
    assert ringing, "the resubmitted round must notify the desktop"
    assert ringing[0].get("call_state") == "ringing"
