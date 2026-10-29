"""Red HTTPS contracts for agent-facing final task-report result budgets only."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from central_service import _TOOL_SPECS
from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_iphone_task_final_submission import _accept, _call, _owner_writer_headers, _submit, _task_continuation
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_wait_leg import _with_timeout
from test_terminal_control_api import _wait_for_tls_gateway


MIN_BUDGET = 16_384


def _rows(data_dir: Path) -> tuple[int, int, int, int, int]:
    with sqlite3.connect(data_dir / "calls.sqlite3") as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                     for table in ("calls", "events", "results", "create_operations", "bark_deliveries"))


def _task_request(request_id: str, budget: object | None = None) -> dict:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "assign_task_to_human"
    request["params"]["arguments"] = {
        "task_title": "budget task", "task_description": "return a report",
        "request_id": request_id, "client_timeout_seconds": 2,
    }
    if budget is not None:
        request["params"]["arguments"]["max_result_bytes"] = budget
    return request


def _call(port: int, certificate: Path, request_id: str, budget: object | None = None) -> dict:
    status, response, _ = _post(port, certificate, _task_request(request_id, budget), "test-token-alpha")
    assert status == 200
    return _structured(response)


def _read(port: int, certificate: Path, task_id: str, result_id: str, cursor: int) -> dict:
    request = {"jsonrpc": "2.0", "id": f"task-read-{cursor}", "method": "tools/call", "params": {
        "name": "read_interaction_result", "arguments": {
            "interaction_id": task_id, "result_id": result_id, "cursor": cursor,
            "max_result_bytes": MIN_BUDGET,
        }}}
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def test_task_schema_exposes_optional_result_budget() -> None:
    task = next(spec for spec in _TOOL_SPECS if spec.name == "assign_task_to_human")
    assert "max_result_bytes" in dict(task.properties)


def test_task_invalid_budget_rejects_before_durable_admission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = [_call(port, certificate, f"invalid-task-{index}", budget)
                    for index, budget in enumerate((False, "16384", MIN_BUDGET - 1))]
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == [{"success": False, "status": "invalid_result_budget", "needs_continuation": False}] * 3
    assert after == before == (0, 0, 0, 0, 0)


def test_tls_oversized_unicode_task_final_omits_transport_and_pages_authorized_archive(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    report = "闆" * MIN_BUDGET
    try:
        created = _call(port, certificate, "oversized-task", MIN_BUDGET)
        task_id, call_id = created["interaction_id"], created["call_id"]
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        writer = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        submitted = _submit(port, certificate, call_id, "oversized-task-submit", "completed", report, writer)[2]
        _, opened, _ = _post(port, certificate, _task_continuation("task-opened", task_id, call_id, 0), "test-token-alpha")
        assert _structured(opened)["status"] == "opened"
        request = _task_continuation("task-final", task_id, call_id, 1)
        request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, response, _ = _post(port, certificate, request, "test-token-alpha")
        delivered = _structured(response); cursor = 0; chunks: list[str] = []
        while True:
            page = _read(port, certificate, task_id, submitted["result_id"], cursor)
            assert page["success"] is True and isinstance(page["body"], str)
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MIN_BUDGET
            chunks.append(page["body"])
            if page["complete"]:
                assert page["next_cursor"] is None; break
            assert isinstance(page["next_cursor"], int) and page["next_cursor"] > cursor
            cursor = page["next_cursor"]
    finally:
        _stop(process)
    assert delivered["status"] == "completed" and delivered["result_id"] == submitted["result_id"]
    assert delivered["report"] is None and delivered["body_omitted_for_size"] is True
    assert "".join(chunks) == report


def test_tls_task_progress_final_order_replay_and_foreign_read_boundary_remain_intact(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _call(port, certificate, "task-order-default")
        task_id, call_id = created["interaction_id"], created["call_id"]
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        writer = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        progress = _submit(port, certificate, call_id, "task-progress", "in_progress", "Working", writer)
        final = _submit(port, certificate, call_id, "task-final", "completed", "Done", writer)[2]
        request = _task_continuation("task-order-event", task_id, call_id, 1)
        _, first, _ = _post(port, certificate, request, "test-token-alpha")
        _, replay, _ = _post(port, certificate, request, "test-token-alpha")
        _, foreign, _ = _post(port, certificate, request, "test-token-beta")
    finally:
        _stop(process)
    assert progress[0] == 200 and _structured(first) == _structured(replay)
    assert _structured(first)["status"] == "in_progress" and final["status"] == "completed"
    assert "structuredContent" not in foreign.get("result", {})
