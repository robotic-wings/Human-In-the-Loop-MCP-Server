"""Red TLS contracts for standalone feedback result transport budgets.

These tests exercise only the agent-facing feedback result representation.  The
existing browser submission tests remain the authority for the feedback form,
writer token, and cross-site request protections.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from central_service import _TOOL_SPECS
from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_iphone_browser_drafts import _writer_headers
from test_iphone_feedback_submission import TYPE_ID, _accept, _submit
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout
from test_terminal_control_api import _wait_for_tls_gateway


MIN_BUDGET = 16_384


def _rows(data_dir: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(data_dir / "calls.sqlite3") as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("calls", "events", "results", "create_operations", "bark_deliveries")
        )


def _feedback_request(request_id: str) -> dict:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_feedback_from_user"
    request["params"]["arguments"] = {
        "request_id": request_id, "client_timeout_seconds": 2,
    }
    return request


def _call_budget(port: int, certificate: Path, request_id: str, budget: object) -> dict:
    request = _feedback_request(request_id)
    request["params"]["arguments"]["max_result_bytes"] = budget
    status, response, _ = _post(port, certificate, request, "test-token-alpha")
    assert status == 200
    return _structured(response)


def _read_page(port: int, certificate: Path, interaction_id: str, result_id: str, cursor: int) -> dict:
    request = {
        "jsonrpc": "2.0", "id": f"feedback-read-{cursor}", "method": "tools/call",
        "params": {"name": "read_interaction_result", "arguments": {
            "interaction_id": interaction_id, "result_id": result_id,
            "cursor": cursor, "max_result_bytes": MIN_BUDGET,
        }},
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def test_feedback_schema_exposes_optional_result_budget() -> None:
    feedback = next(spec for spec in _TOOL_SPECS if spec.name == "get_feedback_from_user")
    assert "max_result_bytes" in dict(feedback.properties)


def test_feedback_invalid_budget_rejects_before_durable_admission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = [_call_budget(port, certificate, f"invalid-feedback-{index}", budget)
                    for index, budget in enumerate((False, "16384", MIN_BUDGET - 1))]
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == [{"success": False, "status": "invalid_result_budget", "needs_continuation": False}] * 3
    assert after == before == (0, 0, 0, 0, 0)


def test_tls_oversized_unicode_feedback_omits_transport_and_pages_authorized_archive(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    feedback = "雪" * MIN_BUDGET
    try:
        created = _call_budget(port, certificate, "oversized-feedback", MIN_BUDGET)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, created["call_id"], headers)[0] == 200
        writer = _writer_headers(port, certificate, created["call_id"], headers, "feedback-budget")
        submitted = _submit(port, certificate, created["call_id"], "oversized-feedback-submit", TYPE_ID, feedback, writer)[2]
        opened = _continuation("feedback-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        continuation = _continuation("feedback-final", created["interaction_id"], created["call_id"], 1)
        continuation["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
        delivered = _structured(response)
        cursor = 0; chunks: list[str] = []
        while True:
            page = _read_page(port, certificate, created["interaction_id"], submitted["result_id"], cursor)
            assert page["success"] is True and isinstance(page["body"], str)
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MIN_BUDGET
            chunks.append(page["body"])
            if page["complete"]:
                assert page["next_cursor"] is None
                break
            assert isinstance(page["next_cursor"], int) and page["next_cursor"] > cursor
            cursor = page["next_cursor"]
    finally:
        _stop(process)
    assert delivered["status"] == "answered"
    assert delivered["result_id"] == submitted["result_id"]
    assert delivered["feedback_text"] is None
    assert delivered["body_omitted_for_size"] is True
    assert "".join(chunks) == feedback


def test_tls_feedback_sufficient_budget_replay_and_foreign_caller_boundary_remain_intact(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _call_budget(port, certificate, "normal-feedback", MIN_BUDGET)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, created["call_id"], headers)[0] == 200
        writer = _writer_headers(port, certificate, created["call_id"], headers, "normal-feedback")
        submitted = _submit(port, certificate, created["call_id"], "normal-feedback-submit", TYPE_ID, "Small canonical feedback", writer)[2]
        opened = _continuation("normal-feedback-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        request = _continuation("normal-feedback-final", created["interaction_id"], created["call_id"], 1)
        request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, request, "test-token-alpha")
        _, replay, _ = _post(port, certificate, request, "test-token-alpha")
        _, foreign, _ = _post(port, certificate, request, "test-token-beta")
    finally:
        _stop(process)
    delivered = _structured(response)
    assert delivered == _structured(replay)
    assert delivered["feedback_text"] == "Small canonical feedback"
    assert delivered["result_id"] == submitted["result_id"]
    assert "structuredContent" not in foreign.get("result", {})
