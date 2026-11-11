"""Red contract for sixth-round SPEC gaps (§9.1 pagination, §5.4 single instance)."""

from __future__ import annotations

import asyncio
import importlib
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
            agent: str | None = None, terminal: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        if terminal: headers["x-terminal-session"] = terminal
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def rpc_call(app: Any, name: str, arguments: dict[str, object], rid: int = 1) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    return dict(response.json()["result"]["structuredContent"])


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


def submit(app: Any, call: dict[str, object], session: str, result: dict[str, object]) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=session, body={
        "submission_id": "sub-1", "result": result,
    })
    assert response.status_code == 200


def test_read_result_pagination_advances_cursor_and_reconstructs_body(tmp_path: Path) -> None:
    app = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    original = "中文内容" * 600
    submit(app, call, owner, {"status": "completed", "report": original})

    chunks: list[str] = []
    cursor: object = None
    rid = 2
    for _ in range(20):
        arguments: dict[str, object] = {
            "interaction_id": call["interaction_id"], "max_result_bytes": 500,
        }
        if cursor is not None:
            arguments["cursor"] = cursor
        page = rpc_call(app, "read_interaction_result", arguments, rid=rid)
        rid += 1
        assert page.get("success") is True
        text = page.get("result", {}).get("report", "") if isinstance(page.get("result"), dict) else ""
        chunks.append(text)
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
    else:
        raise AssertionError("pagination never reached has_more=false")

    assert "".join(chunks) == original


def test_single_instance_lock_rejects_second_acquisition(tmp_path: Path) -> None:
    module = importlib.import_module("central_desktop_terminal")
    acquire = getattr(module, "acquire_instance_lock", None)
    assert callable(acquire), "a single-instance lock guard must exist"

    lock_path = tmp_path / "instance.lock"
    assert acquire(lock_path) is True
    assert acquire(lock_path) is False
