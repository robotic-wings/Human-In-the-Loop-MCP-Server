"""Red contracts for explicit task-round revision and old-round isolation.

This contract deliberately covers only SPEC 7.1's completed/failed task
resubmission path.  It does not prescribe progress, attachments, drafts, or
human-terminal UI behavior.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_browser_drafts import _writer_headers
from test_iphone_task_progress_submission import _accept, _continuation_for_task, _submit
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_wait_leg import _with_timeout


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "assign_task_to_human"
    request["params"]["arguments"] = {
        "task_title": "Revision task", "task_description": "Original work",
        "context_note": "initial", "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _revision_request(task_id: str, old_call_id: str, revision_id: str, request_id: str, description: str = "Revised work") -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "assign_task_to_human"
    request["params"]["arguments"] = {
        "task_id": task_id, "call_id": old_call_id, "revision_id": revision_id,
        "request_id": request_id, "task_title": "Revision task",
        "task_description": description, "context_note": "revised",
        "client_timeout_seconds": 2,
    }
    return request


def _finish_task(port: int, certificate: Path, call_id: str, status: str, submission_id: str) -> dict[str, str]:
    cookie, csrf = _login(port, certificate)
    owner = _headers(cookie, csrf, port)
    assert _accept(port, certificate, call_id, owner)[0] == 200
    writer = _writer_headers(port, certificate, call_id, owner, "revision-" + submission_id)
    assert _submit(port, certificate, call_id, submission_id, status, "terminal report", writer)[0] == 200
    return writer


def _cancel_request(task_id: str, call_id: str, request_id: str) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {
        "name": "cancel_interaction", "arguments": {
            "interaction_id": task_id, "call_id": call_id, "request_id": request_id,
        },
    }}


def _call_rows(database: Path, task_id: str):
    with sqlite3.connect(database) as connection:
        return connection.execute(
            "SELECT call_id, state FROM calls WHERE interaction_id = ? ORDER BY created_at", (task_id,)
        ).fetchall()


def test_tls_task_revision_creates_one_new_round_replays_and_conflicts_without_reusing_old_events(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        original = _call(port, certificate, "revision-original")
        task_id, old_call_id = original["interaction_id"], original["call_id"]
        assert isinstance(task_id, str) and isinstance(old_call_id, str)
        _finish_task(port, certificate, old_call_id, "completed", "revision-final")
        first_request = _revision_request(task_id, old_call_id, "revision-1", "revision-create")
        _, first_raw, _ = _post(port, certificate, first_request, "test-token-alpha")
        _, replay_raw, _ = _post(port, certificate, first_request, "test-token-alpha")
        _, conflict_raw, _ = _post(port, certificate, _revision_request(task_id, old_call_id, "revision-1", "revision-create", "different"), "test-token-alpha")
        rows = _call_rows(database, task_id)
        with sqlite3.connect(database) as connection:
            events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (rows[-1][0],)).fetchall() if len(rows) == 2 else []
    finally:
        _stop(process)
    first, replay, conflict = _structured(first_raw), _structured(replay_raw), _structured(conflict_raw)
    assert first == replay
    assert first["success"] is True and first["call_state"] == "ringing"
    assert first["interaction_id"] == first["task_id"] == task_id and first["call_id"] != old_call_id
    assert first["event_seq"] == 0 and rows == [(old_call_id, "completed"), (first["call_id"], "ringing")] and events == []
    assert conflict["success"] is False and conflict["status"] == "revision_conflict"


def test_tls_task_revision_rejects_nonfinal_nonlatest_busy_foreign_and_late_old_round_actions_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        active = _call(port, certificate, "revision-active"); active_task, active_call = active["interaction_id"], active["call_id"]
        assert isinstance(active_task, str) and isinstance(active_call, str)
        _, nonfinal_raw, _ = _post(port, certificate, _revision_request(active_task, active_call, "bad-active", "bad-active-request"), "test-token-alpha")
        old_writer = _finish_task(port, certificate, active_call, "failed", "revision-failed")
        _, created_raw, _ = _post(port, certificate, _revision_request(active_task, active_call, "revision-new", "revision-new-request"), "test-token-alpha")
        created = _structured(created_raw); new_call = created["call_id"]; assert isinstance(new_call, str)
        before = _call_rows(database, active_task)
        _, old_continue_raw, _ = _post(port, certificate, _continuation_for_task("late-old-continue", active_task, active_call, 0), "test-token-alpha")
        old_submit = _submit(port, certificate, active_call, "late-old-submit", "failed", "late report", old_writer)
        old_decline = _request(port, certificate, "POST", f"/api/calls/{active_call}/decline", body={"action_id": "late-old-decline"}, headers=old_writer)
        _, old_cancel_raw, _ = _post(port, certificate, _cancel_request(active_task, active_call, "late-old-cancel"), "test-token-alpha")
        _, foreign_raw, _ = _post(port, certificate, _revision_request(active_task, active_call, "foreign", "foreign-request"), "test-token-beta")
        other = _call(port, certificate, "revision-other-busy")
        after = _call_rows(database, active_task)
    finally:
        _stop(process)
    nonfinal, old_continuation, old_cancel, foreign, busy = (_structured(nonfinal_raw), _structured(old_continue_raw), _structured(old_cancel_raw), _structured(foreign_raw), other)
    assert nonfinal["success"] is False and nonfinal["status"] in {"revision_not_allowed", "invalid_revision"}
    assert old_continuation["call_id"] == active_call and old_cancel["call_id"] == active_call
    assert old_submit[0] in {400, 403, 409} and old_decline[0] in {400, 403, 409}
    assert "error" in foreign_raw or foreign["success"] is False
    assert busy["success"] is True and busy["status"] == "busy"
    assert before == after == [(active_call, "failed"), (new_call, "ringing")]
