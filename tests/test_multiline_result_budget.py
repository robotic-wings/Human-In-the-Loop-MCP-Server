"""Red contracts for multiline MCP result-budget admission and delivery."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from central_service import _TOOL_SPECS
from test_central_mcp_gateway import _call, _free_port, _post, _start, _stop, _structured
from test_mcp_continuation import _continuation
from test_iphone_multiline_submission import _accept, _submit, _writer_headers
from test_iphone_web_acceptance import _login, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_terminal_control_api import _wait_for_tls_gateway


MIN_BUDGET = 16_384


def _rows(data_dir: Path) -> tuple[int, int, int, int]:
    with sqlite3.connect(data_dir / "calls.sqlite3") as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("calls", "events", "results", "create_operations"))


def _multiline_request(request_id: str) -> dict:
    request = _call(request_id)
    request["params"]["name"] = "get_multiline_input"
    request["params"]["arguments"] = {
        "title": "Budgeted multiline", "prompt": "Provide the complete answer.",
        "default_value": "", "request_id": request_id,
    }
    return request


def _call_budget(port: int, certificate: Path, request_id: str, budget: object) -> dict:
    request = _multiline_request(request_id)
    request["params"]["arguments"]["max_result_bytes"] = budget
    status, response, _ = _post(port, certificate, request, "test-token-alpha")
    assert status == 200
    return _structured(response)


def test_multiline_schema_exposes_optional_result_budget() -> None:
    multiline = next(spec for spec in _TOOL_SPECS if spec.name == "get_multiline_input")
    assert "max_result_bytes" in dict(multiline.properties)


def test_multiline_invalid_budget_rejects_before_durable_admission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = [_call_budget(port, certificate, f"invalid-{index}", budget)
                    for index, budget in enumerate((False, "16384", MIN_BUDGET - 1))]
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == [{"success": False, "status": "invalid_result_budget", "needs_continuation": False}] * 3
    assert after == before == (0, 0, 0, 0)


def test_tls_multiline_oversized_unicode_omits_transport_body_but_keeps_canonical_result(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    body = chr(0x96EA) * MIN_BUDGET
    try:
        created = _call_budget(port, certificate, "oversized-multiline", MIN_BUDGET)
        assert created["status"] == "ringing"
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, created["call_id"], headers)[0] == 200
        writer = _writer_headers(port, certificate, created["call_id"], headers, "multiline-budget")
        status, _, submitted = _submit(port, certificate, created["call_id"], "multiline-oversized-submit", body, {**headers, "X-Writer-Token": writer["X-Writer-Token"]})
        assert status == 200, submitted
        opened = _continuation("multiline-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        continuation = _continuation("multiline-final", created["interaction_id"], created["call_id"], 1)
        continuation["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
        delivered = _structured(response)
    finally:
        _stop(process)
    assert delivered["status"] == "answered"
    assert delivered["result_id"] == submitted["result_id"]
    assert delivered["user_input"] is None
    assert delivered["body_omitted_for_size"] is True


def test_tls_multiline_normal_budget_replay_and_caller_boundary_remain_intact(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _call_budget(port, certificate, "normal-multiline", MIN_BUDGET)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, created["call_id"], headers)[0] == 200
        writer = _writer_headers(port, certificate, created["call_id"], headers, "normal-multiline")
        status, _, submitted = _submit(port, certificate, created["call_id"], "normal-multiline-submit", "small canonical multiline\\nanswer", {**headers, "X-Writer-Token": writer["X-Writer-Token"]})
        assert status == 200, submitted
        opened = _continuation("normal-multiline-opened", created["interaction_id"], created["call_id"], 0)
        _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
        assert _structured(opened_response)["status"] == "opened"
        request = _continuation("normal-multiline-read", created["interaction_id"], created["call_id"], 1)
        request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, request, "test-token-alpha")
        _, replay, _ = _post(port, certificate, request, "test-token-alpha")
        _, foreign, _ = _post(port, certificate, request, "test-token-beta")
    finally:
        _stop(process)
    delivered = _structured(response)
    assert delivered == _structured(replay)
    assert delivered["user_input"] == "small canonical multiline\\nanswer"
    assert delivered["result_id"] == submitted["result_id"]
    assert "structuredContent" not in foreign.get("result", {})
