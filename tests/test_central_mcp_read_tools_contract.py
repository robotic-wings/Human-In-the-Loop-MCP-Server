"""Red contract for the read-only result/attachment tools (SPEC §9.1).

``read_interaction_result`` and ``read_interaction_attachment`` let a caller
page the full body and attachment bytes of an already-saved result, so an
over-budget result is never truncated into a forgery.  These tests stay red
until both tools exist and enforce caller-level authorisation.
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
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret", "agent-b": "agent-b-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_directory={"desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True}},
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


def rpc_structured(app: Any, name: str, arguments: dict[str, object], *, agent: str = "agent-secret",
                   rid: int = 1) -> Any:
    response = request(app, "POST", "/mcp", agent=agent, body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    body = response.json()
    if "result" in body:
        return body["result"]["structuredContent"]
    return body.get("error")


def tools_list(app: Any) -> list[dict[str, object]]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert response.status_code == 200
    return list(response.json()["result"]["tools"])


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


def upload(app: Any, call: dict[str, object], session: str, content: bytes) -> dict[str, object]:
    import base64
    response = request(app, "POST", f"/api/calls/{call['call_id']}/attachments", terminal=session, body={
        "filename": "file.bin", "media_type": "application/octet-stream",
        "content": base64.b64encode(content).decode("ascii"),
    })
    assert response.status_code == 200
    return response.json()


def test_tools_list_includes_read_tools(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    names = {tool["name"] for tool in tools_list(app)}
    assert "read_interaction_result" in names
    assert "read_interaction_attachment" in names


def test_read_interaction_result_returns_full_body_and_attachments(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    uploaded = upload(app, call, owner, b"\x00\x01\x02data")
    request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "read-submit",
        "result": {"status": "completed", "report": "the full report", "attachments": [uploaded["attachment_id"]]},
    })

    result = rpc_structured(app, "read_interaction_result", {
        "interaction_id": call["interaction_id"],
    })
    assert isinstance(result, dict) and result.get("success") is True


def test_read_interaction_attachment_returns_chunk_with_checksum(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    uploaded = upload(app, call, owner, b"\x00\x01\x02data")

    result = rpc_structured(app, "read_interaction_attachment", {
        "interaction_id": call["interaction_id"], "attachment_id": uploaded["attachment_id"],
        "offset_bytes": 0, "max_result_bytes": 1000,
    })
    assert isinstance(result, dict) and result.get("success") is True


def test_foreign_caller_cannot_read_result(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "foreign-read", "result": {"status": "completed", "report": "secret"},
    })

    result = rpc_structured(app, "read_interaction_result", {
        "interaction_id": call["interaction_id"], "result_id": "placeholder",
    }, agent="agent-b-secret")
    assert not (isinstance(result, dict) and result.get("success") is True)


def test_read_result_attachment_list_includes_filename_and_media_type(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    uploaded = upload(app, call, owner, b"\x00\x01\x02data")
    request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "read-filename",
        "result": {"status": "completed", "report": "done", "attachments": [uploaded["attachment_id"]]},
    })

    result = rpc_structured(app, "read_interaction_result", {"interaction_id": call["interaction_id"]})
    assert isinstance(result, dict) and result.get("success") is True
    attachments = result.get("attachments", [])
    assert attachments, "read result must list attachments"
    assert "filename" in attachments[0]
    assert "media_type" in attachments[0]


def test_read_result_with_unknown_result_id_is_not_found(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "read-unknown", "result": {"status": "completed", "report": "done"},
    })

    result = rpc_structured(app, "read_interaction_result", {
        "interaction_id": call["interaction_id"], "result_id": "nonexistent-result-id",
    })
    assert isinstance(result, dict) and result.get("success") is False


def test_read_result_pagination_returns_has_more_and_next_cursor(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)
    request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "read-page", "result": {"user_input": "x" * 10000},
    })

    result = rpc_structured(app, "read_interaction_result", {
        "interaction_id": call["interaction_id"], "max_result_bytes": 100,
    })
    assert isinstance(result, dict) and result.get("success") is True
    assert "has_more" in result
    assert "next_cursor" in result
