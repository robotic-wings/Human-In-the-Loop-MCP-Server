"""Red contract for a dedicated numeric-input MCP tool.

Problem 2 in the owner backlog: the MCP tool catalog has no ``get_number_input``
and ``get_user_input``'s inputSchema carries no ``input_type``.  The chosen
design (option a) adds a distinct ``get_number_input`` interaction tool with a
complete ``inputSchema`` (``prompt`` / ``required``), so an Agent can request a
number without overloading the free-text tool.  These tests stay red until that
tool exists in ``central_mcp_waiting_lifecycle._MCP_TOOLS`` and is dispatched by
``tools/call`` through the same durable create -> accept -> submit -> continue
round that every interaction tool follows.
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


class BrowserVerifier:
    def verify(self, token: str | None) -> dict[str, str] | None:
        if token == "operator":
            return {"operator_id": "only-operator", "browser_session_id": "browser-owner"}
        return None


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path, *, route_mode: str = "desktop") -> tuple[Any, Clock]:
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
        route_mode=route_mode, call_state=call_state, web_base_url="https://central.test",
    )
    return create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_session_verifier=BrowserVerifier(),
    ), clock


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, terminal: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent:
            headers["authorization"] = f"Bearer {agent}"
        if terminal:
            headers["x-terminal-session"] = terminal
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def rpc(app: Any, payload: dict[str, object], token: str = "agent-secret") -> httpx.Response:
    return request(app, "POST", "/mcp", body=payload, agent=token)


def tools_catalog(app: Any) -> dict[str, dict[str, object]]:
    response = rpc(app, {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    tools = body["result"]["tools"]
    return {str(tool["name"]): dict(tool) for tool in tools}


def tool_call(app: Any, name: str, rid: int, arguments: dict[str, object]) -> dict[str, object]:
    response = rpc(app, {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == rid
    result = body["result"]
    assert isinstance(result, dict)
    structured = result["structuredContent"]
    assert isinstance(structured, dict)
    return dict(structured)


def register_terminal(app: Any, credential: str = "desktop-secret") -> str:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.post(
                "/api/terminals/register",
                headers={"authorization": f"Terminal {credential}"},
                json={"endpoint": "https://desktop.test/events", "gui_ready": True},
            )
    response = asyncio.run(run())
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def accept(app: Any, call_id: str, session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call_id}/accept", terminal=session,
                       body={"operation_id": "accept-number"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted", response.text


def submit(app: Any, call_id: str, session: str, result: dict[str, object]) -> None:
    response = request(app, "POST", f"/api/calls/{call_id}/submissions", terminal=session,
                       body={"submission_id": "sub-number", "result": result})
    assert response.status_code == 200, response.text


def test_tools_list_includes_get_number_input_with_complete_schema(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    catalog = tools_catalog(app)

    assert "get_number_input" in catalog, "tools/list must expose get_number_input"

    tool = catalog["get_number_input"]
    assert isinstance(tool.get("description"), str) and tool["description"]
    schema = tool.get("inputSchema")
    assert isinstance(schema, dict)
    assert schema.get("type") == "object"

    properties = schema.get("properties")
    assert isinstance(properties, dict), "inputSchema must declare a properties map"
    assert properties.get("prompt") == {"type": "string"}
    assert properties.get("required") == {"type": "boolean"}


def test_get_number_input_roundtrip_delivers_numeric_answer(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    session = register_terminal(app)

    created = tool_call(app, "get_number_input", 11, {"prompt": "How many?", "required": True})
    assert created["success"] is True
    assert created["call_state"] == "ringing"
    interaction_id = created["interaction_id"]
    call_id = created["call_id"]
    assert isinstance(interaction_id, str) and interaction_id
    assert isinstance(call_id, str) and call_id

    accept(app, str(call_id), session)
    submit(app, str(call_id), session, {"user_input": "42"})

    delivered = tool_call(app, "get_number_input", 12, {
        "interaction_id": interaction_id, "call_id": call_id, "after_event_seq": 1,
    })
    assert delivered["status"] == "answered"
    assert delivered["call_state"] == "answered"
    assert delivered["result"]["user_input"] == "42"
