"""Red HTTPS contracts for the central MCP guidance prompt surface."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from test_central_mcp_gateway import (
    _free_port,
    _post,
    _rpc,
    _start,
    _stop,
    _wait_for_listener,
    _write_gateway_config,
)


PROMPT_NAME = "get_human_loop_prompt"
CANONICAL_GUIDANCE = """# Central human-loop MCP protocol

Use the authenticated HTTPS `/mcp` endpoint. It is the only supported transport: stdio and local fallback modes are not supported.

Start an interaction with the relevant tool call. Its first response identifies the durable `interaction_id` and `call_id`. Every continuation and event read must use that same pair together with the last successfully received `after_event_seq`; do not create another call to continue waiting.

Human acceptance produces an `opened` event. Human task progress produces an `in_progress` event. A human answer, completion, failure, decline, timeout, cancellation, or disconnect produces the final event/result. These non-heartbeat events have monotonically increasing `event_seq` values and are durable, so an unacknowledged event can be replayed after a lost response by reusing the cursor.

A `heartbeat` reports current waiting state but creates no event and does not advance `event_seq`. `needs_continuation:true` means either the interaction is still waiting or another durable readable event remains after the current cursor; it does not by itself mean a new call should be created. A final result with no unread event has `needs_continuation:false`.

Final statuses and their structured results are durable. Reading or replaying an event never re-notifies the human or creates another interaction. Malformed or rejected requests continue to return the established structured JSON-RPC or authentication errors."""


def _database_snapshot(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        return {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }


def test_authenticated_prompt_discovery_and_read_return_central_http_continuation_guidance(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        list_status, listed, _ = _post(
            port, certificate, _rpc("prompts/list", {}), "test-token-alpha"
        )
        get_status, prompt, _ = _post(
            port,
            certificate,
            _rpc("prompts/get", {"name": PROMPT_NAME}),
            "test-token-alpha",
        )
    finally:
        _stop(process)

    assert list_status == get_status == 200
    assert listed["result"] == {
        "prompts": [
            {
                "name": PROMPT_NAME,
                "description": "Central HTTP(S) human-loop protocol guidance.",
            }
        ]
    }
    messages = prompt["result"]["messages"]
    assert isinstance(messages, list) and len(messages) == 1
    content = messages[0]["content"]
    assert messages[0]["role"] == "user"
    assert content["type"] == "text"
    assert content["text"] == CANONICAL_GUIDANCE


def test_prompt_requests_validate_params_and_preserve_json_rpc_and_auth_errors(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        malformed_status, malformed, _ = _post(
            port, certificate, _rpc("prompts/get", {}), "test-token-alpha"
        )
        unknown_status, unknown, _ = _post(
            port,
            certificate,
            _rpc("prompts/get", {"name": "not-a-central-prompt"}),
            "test-token-alpha",
        )
        method_status, method, _ = _post(
            port, certificate, _rpc("prompts/delete", {}), "test-token-alpha"
        )
        unauthenticated_status, unauthenticated, _ = _post(
            port, certificate, _rpc("prompts/list", {}), None
        )
    finally:
        _stop(process)

    assert malformed_status == unknown_status == method_status == 200
    assert malformed["error"]["code"] == -32602
    assert unknown["error"]["code"] == -32602
    assert method["error"]["code"] == -32601
    assert unauthenticated_status == 401
    assert "error" in unauthenticated


def test_prompt_reads_do_not_create_calls_results_events_outbox_or_busy_state(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _database_snapshot(database)
        list_status, listed, _ = _post(
            port, certificate, _rpc("prompts/list", {}), "test-token-alpha"
        )
        get_status, prompt, _ = _post(
            port,
            certificate,
            _rpc("prompts/get", {"name": PROMPT_NAME}),
            "test-token-alpha",
        )
        after = _database_snapshot(database)
        tool_status, tool_list, _ = _post(
            port, certificate, _rpc("tools/list", {}), "test-token-alpha"
        )
    finally:
        _stop(process)

    assert list_status == get_status == tool_status == 200
    assert "result" in listed and "result" in prompt and "result" in tool_list
    assert after == before
    assert after["calls"] == 0
    assert after["events"] == 0
    assert after["results"] == 0
    assert after["bark_deliveries"] == 0
    assert after["terminal_callback_deliveries"] == 0
