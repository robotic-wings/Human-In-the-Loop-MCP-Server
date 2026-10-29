"""Red contracts for ordinary-text MCP result-budget admission and delivery.

This uses the real TLS gateway and SQLite archive.  It deliberately does not
cover any other tool, browser UI, attachment, or quota behavior.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from test_central_mcp_gateway import _call, _free_port, _post, _start, _stop, _structured
from test_mcp_continuation import _continuation
from test_terminal_attachment_upload_api import _accept
from test_terminal_control_api import (
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


MIN_BUDGET = 16_384


def _rows(data_dir: Path) -> tuple[int, int, int]:
    """An independent durable snapshot for rejected initial tool calls."""
    database = data_dir / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("calls", "events", "results")
        )


def _create_with_budget(port: int, certificate: Path, request_id: str, budget: int) -> dict:
    request = _call(request_id)
    request["params"]["arguments"]["max_result_bytes"] = budget
    status, response, _ = _post(port, certificate, request, "test-token-alpha")
    assert status == 200
    return _structured(response)


def test_tls_ordinary_text_invalid_result_budget_rejects_before_call_or_event_mutation(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = _create_with_budget(port, certificate, "budget-too-small", MIN_BUDGET - 1)
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)

    assert rejected == {
        "success": False,
        "status": "invalid_result_budget",
        "needs_continuation": False,
    }
    assert after == before == (0, 0, 0)


def test_tls_ordinary_text_oversized_final_omits_wire_body_but_keeps_canonical_result(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    body = "x" * (MIN_BUDGET * 2)
    try:
        created = _create_with_budget(port, certificate, "oversized-final", MIN_BUDGET)
        assert created["status"] == "ringing"
        _accept(port, certificate, created["call_id"], "budget-owner")
        status, submitted = _terminal_request(
            port, certificate, "POST", f"/api/calls/{created['call_id']}/submissions",
            "desktop-token", {
                "owner_session_id": "budget-owner", "submission_id": "oversized-submit",
                "task_version": 0, "user_input": body,
            },
        )
        assert status == 200
        assert submitted["result_id"]
        # Acceptance creates the durable opened event first.  Acknowledging it
        # makes the next read exercise the final-result transport budget.
        opened = _continuation(
            "oversized-opened", created["interaction_id"], created["call_id"], 0,
        )
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        continuation = _continuation(
            "oversized-read", created["interaction_id"], created["call_id"], 1,
        )
        continuation["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
        delivered = _structured(response)
    finally:
        _stop(process)

    assert delivered["status"] == "answered"
    assert delivered["result_id"] == submitted["result_id"]
    assert delivered["user_input"] is None
    assert delivered["body_omitted_for_size"] is True
    assert delivered["result_id"]


def test_tls_ordinary_text_normal_budget_control_and_caller_boundary_remain_intact(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_with_budget(port, certificate, "normal-budget", MIN_BUDGET)
        _accept(port, certificate, created["call_id"], "normal-owner")
        status, submitted = _terminal_request(
            port, certificate, "POST", f"/api/calls/{created['call_id']}/submissions",
            "desktop-token", {
                "owner_session_id": "normal-owner", "submission_id": "normal-submit",
                "task_version": 0, "user_input": "small canonical answer",
            },
        )
        assert status == 200
        opened = _continuation("normal-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        request = _continuation("normal-read", created["interaction_id"], created["call_id"], 1)
        request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, request, "test-token-alpha")
        _, replay, _ = _post(port, certificate, request, "test-token-alpha")
        _, foreign, _ = _post(port, certificate, request, "test-token-beta")
    finally:
        _stop(process)

    delivered = _structured(response)
    assert delivered == _structured(replay)
    assert delivered["user_input"] == "small canonical answer"
    assert delivered["result_id"] == submitted["result_id"]
    assert "structuredContent" not in foreign.get("result", {})
