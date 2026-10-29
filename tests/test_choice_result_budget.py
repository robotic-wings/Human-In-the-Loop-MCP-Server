"""Red contracts for choice-result transport budgets.

The tests use the real TLS gateway and SQLite archive.  They intentionally
cover only ``get_user_choice`` and leave browser, attachment, and timing
behavior to their existing contracts.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from central_service import _TOOL_SPECS
from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_iphone_browser_drafts import _writer_headers
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


def _choice_request(request_id: str, choices: list[str] | None = None) -> dict:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_user_choice"
    request["params"]["arguments"] = {
        "title": "Budgeted choices",
        "prompt": "Select every applicable value.",
        "choices": choices or ["Alpha", "Beta", "Gamma"],
        "allow_multiple": True,
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    return request


def _call_budget(port: int, certificate: Path, request_id: str, budget: object) -> dict:
    request = _choice_request(request_id)
    request["params"]["arguments"]["max_result_bytes"] = budget
    status, response, _ = _post(port, certificate, request, "test-token-alpha")
    assert status == 200
    return _structured(response)


def _oversized_choices() -> list[str]:
    # The ordered selected list itself exceeds the minimum UTF-8 transport budget.
    return [f"雪-{index:05d}" for index in range(3_000)]


def test_choice_schema_exposes_optional_result_budget() -> None:
    choice = next(spec for spec in _TOOL_SPECS if spec.name == "get_user_choice")
    assert "max_result_bytes" in dict(choice.properties)


def test_choice_invalid_budget_rejects_before_durable_admission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = [_call_budget(port, certificate, f"invalid-choice-{index}", budget)
                    for index, budget in enumerate((False, "16384", MIN_BUDGET - 1))]
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == [{"success": False, "status": "invalid_result_budget", "needs_continuation": False}] * 3
    assert after == before == (0, 0, 0, 0, 0)


def test_tls_oversized_unicode_choices_omit_transport_list_and_keep_authorized_archive(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    choices = _oversized_choices()
    try:
        request = _choice_request("oversized-choice", choices)
        request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, created_response, _ = _post(port, certificate, request, "test-token-alpha")
        created = _structured(created_response)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _request(port, certificate, "POST", f"/api/calls/{created['call_id']}/accept", body={"action_id": "choice-budget"}, headers=headers)[0] == 200
        writer = _writer_headers(port, certificate, created["call_id"], headers, "choice-budget")
        submitted = _request(port, certificate, "POST", f"/api/calls/{created['call_id']}/submissions", body={"submission_id": "oversized-choice-submit", "selected_choices": choices}, headers=writer)[2]
        opened = _continuation("choice-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        continuation = _continuation("choice-final", created["interaction_id"], created["call_id"], 1)
        continuation["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
        delivered = _structured(response)
        read = {
            "jsonrpc": "2.0", "id": "choice-read", "method": "tools/call",
            "params": {"name": "read_interaction_result", "arguments": {
                "interaction_id": created["interaction_id"], "result_id": submitted["result_id"],
                "cursor": 0, "max_result_bytes": MIN_BUDGET,
            }},
        }
        _, read_response, _ = _post(port, certificate, read, "test-token-alpha")
    finally:
        _stop(process)
    assert delivered["status"] == "answered"
    assert delivered["result_id"] == submitted["result_id"]
    assert delivered["selected_choices"] is None
    assert delivered["body_omitted_for_size"] is True
    archived = _structured(read_response)
    assert archived["body"] == choices


def test_tls_choice_sufficient_budget_replay_and_foreign_caller_boundary_remain_intact(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created_request = _choice_request("normal-choice")
        created_request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, created_response, _ = _post(port, certificate, created_request, "test-token-alpha")
        created = _structured(created_response)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _request(port, certificate, "POST", f"/api/calls/{created['call_id']}/accept", body={"action_id": "normal-choice"}, headers=headers)[0] == 200
        writer = _writer_headers(port, certificate, created["call_id"], headers, "normal-choice")
        submitted = _request(port, certificate, "POST", f"/api/calls/{created['call_id']}/submissions", body={"submission_id": "normal-choice-submit", "selected_choices": ["Alpha", "Gamma"]}, headers=writer)[2]
        opened = _continuation("normal-choice-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        continuation = _continuation("normal-choice-read", created["interaction_id"], created["call_id"], 1)
        continuation["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
        _, replay, _ = _post(port, certificate, continuation, "test-token-alpha")
        _, foreign, _ = _post(port, certificate, continuation, "test-token-beta")
    finally:
        _stop(process)
    delivered = _structured(response)
    assert delivered == _structured(replay)
    assert delivered["selected_choices"] == ["Alpha", "Gamma"]
    assert delivered["result_id"] == submitted["result_id"]
    assert "structuredContent" not in foreign.get("result", {})
