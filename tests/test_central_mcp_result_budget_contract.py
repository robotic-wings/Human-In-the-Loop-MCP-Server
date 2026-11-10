"""Red contract for the result budget ``max_result_bytes`` (SPEC §9.1).

Interaction tools must accept an optional ``max_result_bytes`` budget, reject a
budget below the 16,384-byte minimum before creating a call, and, when a human
answer exceeds the budget, save the full content and return
``body_omitted_for_size`` plus a ``result_id`` instead of a truncated forgery.
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


def make_app(tmp_path: Path, clock: Clock) -> Any:
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret"},
        clock=clock, notify=lambda _event: None, wait=no_wait,
    )


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


def rpc_call(app: Any, name: str, arguments: dict[str, object], rid: int = 1) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    return dict(response.json()["result"]["structuredContent"])


def tools_list(app: Any) -> list[dict[str, object]]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
    })
    assert response.status_code == 200
    return list(response.json()["result"]["tools"])


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


def test_sub_minimum_budget_is_rejected_before_call_creation(tmp_path: Path) -> None:
    app = make_app(tmp_path, Clock())
    result = rpc_call(app, "get_user_input", {"prompt": "x", "max_result_bytes": 1000})
    assert result["success"] is False
    assert result["status"] == "invalid_result_budget"


def test_interaction_tool_input_schema_includes_max_result_bytes(tmp_path: Path) -> None:
    app = make_app(tmp_path, Clock())
    tools = tools_list(app)
    text_tool = next(t for t in tools if t["name"] == "get_user_input")
    assert "max_result_bytes" in text_tool["inputSchema"].get("properties", {})


def test_oversized_result_is_saved_with_omission_marker_and_result_id(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    created = rpc_call(app, "get_user_input", {"prompt": "long answer", "max_result_bytes": 16384})
    assert created["call_state"] == "ringing"

    accept_desktop(app, created, owner)
    request(app, "POST", f"/api/calls/{created['call_id']}/submissions", terminal=owner, body={
        "submission_id": "budget-submit", "result": {"user_input": "x" * 20000},
    })

    delivered = rpc_call(app, "get_user_input", {
        "interaction_id": created["interaction_id"], "call_id": created["call_id"], "after_event_seq": 1,
    }, rid=2)
    assert delivered["body_omitted_for_size"] is True
    assert isinstance(delivered["result_id"], str) and delivered["result_id"]
