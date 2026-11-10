"""Red contract for a complete MCP server over ``POST /mcp``.

The central ``/mcp`` entry is not a Model Context Protocol server today.  It
accepts a private ``{request_id, payload, client_timeout_seconds}`` shape and
returns ``{success, status, ...}``; the nine interaction tools exist only as a
``payload.tool`` string hint, and the read-only and control tools do not exist
at all.  This file pins the JSON-RPC 2.0 / Streamable HTTP boundary the SPEC's
"MCP is still the Agent's entry" requirement (SPEC §§2.1, 2.4, 7) implies, so
an implementation can satisfy it:

* ``POST /mcp`` is a JSON-RPC 2.0 endpoint.  A successful response is
  ``{"jsonrpc": "2.0", "id": <echoed id>, "result": ...}``; a protocol error is
  ``{"jsonrpc": "2.0", "id": <id|null>, "error": {"code", "message"}}``.
* ``initialize`` returns ``serverInfo`` (name + version) and ``capabilities``.
* ``tools/list`` returns every tool with ``name``, ``description`` and an
  ``inputSchema`` whose JSON Schema type is ``object``.
* ``tools/call`` invokes a tool by ``params.name`` with ``params.arguments``.
  Its result is ``{"content": [...], "isError": false, "structuredContent":
  {...}}``; for interaction tools ``structuredContent`` mirrors the lifecycle
  result verbatim (``success``/``status``/``call_state``/``interaction_id``/
  ``call_id``/``event_seq``/``needs_continuation``/...), and a continuation is
  the same tool name called with ``arguments`` carrying ``interaction_id``,
  ``call_id`` and ``after_event_seq``.
* JSON-RPC errors use the standard codes: ``invalid_request`` (-32600),
  ``method_not_found`` (-32601), ``invalid_params`` (-32602).

The interaction tools reuse the existing durable lifecycle (create call -> wait
/heartbeat -> human submit -> answered/completed/failed/declined/not_accepted/
busy/cancelled/disconnected/unreachable) and preserve the continuation fields.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import httpx
import pytest


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_wait(_: float) -> None:
    return None


def adapter() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


INTERACTION_TOOLS = [
    "get_user_input",
    "get_user_choice",
    "get_multiline_input",
    "show_confirmation_dialog",
    "show_info_message",
    "get_feedback_from_user",
    "assign_task_to_human",
]

READONLY_TOOLS = [
    "get_operator_profile",
    "health_check",
    "get_human_loop_prompt",
]

CONTROL_TOOLS = ["cancel_interaction"]

ALL_TOOLS = INTERACTION_TOOLS + READONLY_TOOLS + CONTROL_TOOLS


def make_app(tmp_path: Path, clock: FakeClock) -> tuple[Any, list[dict[str, object]]]:
    notifications: list[dict[str, object]] = []
    app = adapter().create_app(
        database_path=tmp_path / "mcp.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=no_wait,
    )
    return app, notifications


def post_rpc(app: Any, payload: dict[str, object], token: str = "token-a") -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://mcp.test"
        ) as client:
            return await client.post(
                "/mcp", json=payload, headers={"Authorization": f"Bearer {token}"}
            )

    return asyncio.run(run())


def structured(app: Any, payload: dict[str, object], token: str = "token-a") -> dict[str, object]:
    response = post_rpc(app, payload, token)
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == payload["id"]
    result = body["result"]
    assert isinstance(result, dict)
    return dict(result)


def tool_call(name: str, rid: int, arguments: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": rid, "method": "tools/call", "params": {"name": name, "arguments": arguments}}


def test_initialize_returns_server_info_and_capabilities(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, FakeClock())
    response = post_rpc(
        app,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "test-client", "version": "0.0.0"},
            },
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 1
    result = body["result"]
    assert isinstance(result, dict)
    assert isinstance(result["protocolVersion"], str) and result["protocolVersion"]
    assert isinstance(result["capabilities"], dict)
    assert isinstance(result["serverInfo"], dict)
    assert isinstance(result["serverInfo"]["name"], str) and result["serverInfo"]["name"]
    assert isinstance(result["serverInfo"]["version"], str) and result["serverInfo"]["version"]


def test_tools_list_returns_full_catalog_with_name_description_and_input_schema(
    tmp_path: Path,
) -> None:
    app, _ = make_app(tmp_path, FakeClock())
    response = post_rpc(app, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    assert response.status_code == 200
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 2
    tools = body["result"]["tools"]
    assert isinstance(tools, list)
    by_name: dict[str, dict[str, object]] = {}
    for tool in tools:
        assert isinstance(tool, dict)
        name = tool.get("name")
        assert isinstance(name, str) and name
        assert isinstance(tool.get("description"), str) and tool["description"]
        schema = tool.get("inputSchema")
        assert isinstance(schema, dict)
        assert schema.get("type") == "object"
        by_name[name] = tool
    missing = sorted(set(ALL_TOOLS) - set(by_name))
    assert missing == []


@pytest.mark.parametrize(
    ("payload", "expected_code"),
    [
        ({"id": 1}, -32600),
        ({"jsonrpc": "1.0", "id": 1, "method": "initialize"}, -32600),
        ({"jsonrpc": "2.0", "id": 2, "method": "does/not/exist"}, -32601),
        ({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {}}, -32602),
    ],
)
def test_jsonrpc_error_semantics(
    tmp_path: Path, payload: dict[str, object], expected_code: int
) -> None:
    app, _ = make_app(tmp_path, FakeClock())
    response = post_rpc(app, payload)
    body = response.json()
    assert body.get("jsonrpc") == "2.0"
    assert "result" not in body
    error = body.get("error")
    assert isinstance(error, dict)
    assert error["code"] == expected_code
    assert isinstance(error.get("message"), str) and error["message"]


@pytest.mark.parametrize("name", READONLY_TOOLS)
def test_readonly_tool_call_returns_structured_result(tmp_path: Path, name: str) -> None:
    app, _ = make_app(tmp_path, FakeClock())
    result = structured(app, tool_call(name, 10, {}))
    assert result.get("isError") is False
    content = result.get("content")
    assert isinstance(content, list) and content
    assert any(
        isinstance(block, dict) and block.get("type") == "text" and block.get("text")
        for block in content
    )


def test_interaction_tool_call_creates_a_ringing_call_with_durable_ids(tmp_path: Path) -> None:
    app, notifications = make_app(tmp_path, FakeClock())
    result = structured(
        app,
        tool_call("get_user_input", 11, {"prompt": "Proceed?"}),
    )
    sc = result["structuredContent"]
    assert sc["success"] is True
    assert sc["call_state"] == "ringing"
    assert isinstance(sc["interaction_id"], str) and sc["interaction_id"]
    assert isinstance(sc["call_id"], str) and sc["call_id"]
    assert sc["needs_continuation"] is True
    assert notifications


def test_second_interaction_tool_call_is_busy_while_a_call_is_ringing(tmp_path: Path) -> None:
    app, _ = make_app(tmp_path, FakeClock())
    first = structured(app, tool_call("get_user_input", 11, {"prompt": "Proceed?"}))
    assert first["structuredContent"]["call_state"] == "ringing"

    busy = structured(app, tool_call("get_user_choice", 12, {"choices": ["a", "b"]}))
    sc = busy["structuredContent"]
    assert sc["success"] is True
    assert sc["status"] == "busy"
    assert sc["call_state"] is None
    assert sc["needs_continuation"] is False


def test_interaction_tool_continuation_delivers_human_answer_without_new_call(
    tmp_path: Path,
) -> None:
    app, notifications = make_app(tmp_path, FakeClock())
    created = structured(app, tool_call("get_user_input", 11, {"prompt": "Proceed?"}))
    interaction_id = created["structuredContent"]["interaction_id"]
    call_id = created["structuredContent"]["call_id"]

    accepted = app.lifecycle.accept(
        call_id=call_id, device_id="phone", handled_on="iphone_web", terminal_platform="ios"
    )
    assert accepted["status"] == "opened"

    opened = structured(
        app,
        tool_call(
            "get_user_input",
            13,
            {"interaction_id": interaction_id, "call_id": call_id, "after_event_seq": 0},
        ),
    )
    assert opened["structuredContent"]["status"] == "opened"
    assert opened["structuredContent"]["event_seq"] == 1

    finished = app.lifecycle.finish(
        call_id=call_id, status="answered", result={"user_input": "hello"}
    )
    assert finished["status"] == "answered"

    answered = structured(
        app,
        tool_call(
            "get_user_input",
            14,
            {"interaction_id": interaction_id, "call_id": call_id, "after_event_seq": 1},
        ),
    )
    sc = answered["structuredContent"]
    assert sc["status"] == "answered"
    assert sc["call_state"] == "answered"
    assert sc["event_seq"] == 2
    assert sc["interaction_id"] == interaction_id
    assert sc["call_id"] == call_id
    assert len(notifications) == 1
