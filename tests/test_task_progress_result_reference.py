"""Red HTTPS contract for budgeted, retrievable task-progress events only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_iphone_task_final_submission import _accept, _owner_writer_headers, _submit, _task_continuation
from test_iphone_web_acceptance import _login, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_wait_leg import _with_timeout
from test_task_result_budget import MIN_BUDGET, _call, _read, _rows
from test_terminal_control_api import _wait_for_tls_gateway


def _progress_continuation(request_id: str, task_id: str, call_id: str, cursor: int) -> dict:
    request = _task_continuation(request_id, task_id, call_id, cursor)
    request["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
    return request


def test_tls_oversized_unicode_task_progress_is_omitted_but_has_authorized_immutable_result_reference(tmp_path: Path) -> None:
    """A progress event must be replayable and its omitted body must remain readable."""
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    report = "\u8fdb\u5ea6" * MIN_BUDGET
    try:
        created = _call(port, certificate, "oversized-progress", MIN_BUDGET)
        task_id, call_id = created["interaction_id"], created["call_id"]
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        writer = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        submitted = _submit(port, certificate, call_id, "oversized-progress-submit", "in_progress", report, writer)[2]
        with sqlite3.connect(database) as connection:
            before_results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        _, opened, _ = _post(port, certificate, _progress_continuation("progress-opened", task_id, call_id, 0), "test-token-alpha")
        request = _progress_continuation("progress-delivery", task_id, call_id, 1)
        _, delivered_raw, _ = _post(port, certificate, request, "test-token-alpha")
        _, replayed_raw, _ = _post(port, certificate, request, "test-token-alpha")
        delivered, replayed = _structured(delivered_raw), _structured(replayed_raw)
        result_id = delivered["result_id"]; cursor = 0; chunks: list[str] = []
        while True:
            page = _read(port, certificate, task_id, result_id, cursor)
            assert page["success"] is True and isinstance(page["body"], str)
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= MIN_BUDGET
            chunks.append(page["body"])
            if page["complete"]:
                assert page["next_cursor"] is None; break
            assert isinstance(page["next_cursor"], int) and page["next_cursor"] > cursor
            cursor = page["next_cursor"]
        _, foreign, _ = _post(port, certificate, _progress_continuation("progress-foreign", task_id, call_id, 1), "test-token-beta")
        with sqlite3.connect(database) as connection:
            after_results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            deadline = connection.execute("SELECT agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)
    assert _structured(opened)["status"] == "opened"
    assert delivered == replayed and delivered["status"] == delivered["call_state"] == "in_progress"
    assert delivered["report"] is None and delivered["body_omitted_for_size"] is True and isinstance(result_id, str)
    assert "".join(chunks) == report and before_results == 0 and after_results == 1 and operations == 1
    assert deadline is not None and datetime.fromisoformat(deadline) - datetime.now(UTC) > timedelta(seconds=290)
    assert "structuredContent" not in foreign.get("result", {})


def test_task_progress_invalid_budget_still_rejects_before_durable_admission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = [_call(port, certificate, f"progress-invalid-{index}", budget)
                    for index, budget in enumerate((False, "16384", MIN_BUDGET - 1))]
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == [{"success": False, "status": "invalid_result_budget", "needs_continuation": False}] * 3
    assert after == before == (0, 0, 0, 0, 0)
