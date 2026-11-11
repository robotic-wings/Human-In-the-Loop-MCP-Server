"""Red contract for request_id length and ASCII bounds (SPEC §7.1)."""

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
    return create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_directory={"desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True}},
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def create(app: Any, request_id: str) -> httpx.Response:
    return request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2,
        "payload": {"tool": "text", "prompt": "hi"},
    })


def request_rows(tmp_path: Path, request_id: str) -> int:
    db = tmp_path / "lifecycle.sqlite3"
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM lifecycle_requests WHERE request_id=?", (request_id,)
        ).fetchone()
    return int(row[0])


def call_rows(tmp_path: Path) -> int:
    db = tmp_path / "lifecycle.sqlite3"
    if not db.exists():
        return 0
    with sqlite3.connect(db) as conn:
        row = conn.execute("SELECT COUNT(*) FROM lifecycle_calls").fetchone()
    return int(row[0])


def test_request_id_longer_than_128_chars_is_rejected(tmp_path: Path) -> None:
    app = components(tmp_path)
    request_id = "x" * 129
    response = create(app, request_id)
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["status"] == "invalid_request"
    assert "interaction_id" not in body
    assert "call_id" not in body
    assert request_rows(tmp_path, request_id) == 0
    assert call_rows(tmp_path) == 0


def test_request_id_with_non_ascii_chars_is_rejected(tmp_path: Path) -> None:
    app = components(tmp_path)
    request_id = "请求-①"
    response = create(app, request_id)
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["status"] == "invalid_request"
    assert "interaction_id" not in body
    assert "call_id" not in body
    assert request_rows(tmp_path, request_id) == 0
    assert call_rows(tmp_path) == 0
