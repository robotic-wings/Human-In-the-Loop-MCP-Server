"""Red contract for central attachment size limits (SPEC 9.1).

The central upload has no limits today.  SPEC 9.1 requires three configurable
bounds, each with a default the operator can override:

* a single file may not exceed ``max_attachment_bytes`` (default 20 MiB);
* the attachments referenced by one submission may not exceed
  ``max_submission_bytes`` (default 50 MiB);
* total stored attachment bytes may not exceed ``max_storage_bytes``
  (default 1 GiB), and a full store must reject rather than evicting old
  results.

These tests stay red until ``upload_attachment`` enforces those bounds.
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


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path, *, max_attachment_bytes: int = 20 * 1024 * 1024,
               max_submission_bytes: int = 50 * 1024 * 1024,
               max_storage_bytes: int = 1024 * 1024 * 1024) -> tuple[Any, Clock]:
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
            row = conn.execute(
                "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
        route_mode="desktop", call_state=call_state, web_base_url="https://central.test",
    )
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        max_attachment_bytes=max_attachment_bytes,
        max_submission_bytes=max_submission_bytes,
        max_storage_bytes=max_storage_bytes,
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
    }, agent="Terminal desktop-secret")
    if response.status_code != 201:
        import asyncio
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
                       body={"operation_id": "accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def upload(app: Any, call: dict[str, object], session: str, content: bytes) -> httpx.Response:
    return request(app, "POST", f"/api/calls/{call['call_id']}/attachments", terminal=session, body={
        "filename": "file.bin", "media_type": "application/octet-stream",
        "content": base64.b64encode(content).decode("ascii"),
    })


def test_single_file_over_limit_rejected(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, max_attachment_bytes=10)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    response = upload(app, call, owner, b"x" * 11)

    assert response.status_code == 200
    assert response.json()["success"] is False
    assert response.json()["status"] == "attachment_too_large"


def test_submission_cumulative_over_limit_rejected(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, max_submission_bytes=20, max_storage_bytes=1000)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    first = upload(app, call, owner, b"x" * 15)
    assert first.json()["success"] is True

    second = upload(app, call, owner, b"x" * 10)
    assert second.status_code == 200
    assert second.json()["success"] is False
    assert second.json()["status"] == "submission_too_large"


def test_disk_usage_over_limit_rejected(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, max_submission_bytes=1000, max_storage_bytes=30)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    first = upload(app, call, owner, b"x" * 20)
    assert first.json()["success"] is True

    second = upload(app, call, owner, b"x" * 20)
    assert second.status_code == 200
    assert second.json()["success"] is False
    assert second.json()["status"] == "storage_full"
