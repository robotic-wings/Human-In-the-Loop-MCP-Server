"""Red HTTPS contract for uploaded attachments in accepted iPhone task finals.

This is intentionally the task-final browser seam only.  Upload/archive
mechanics and ordinary-text attachment references are established elsewhere.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _rpc, _start, _stop, _structured, _wait_for_listener
from test_iphone_browser_attachment_upload import _upload_browser
from test_iphone_task_final_submission import _accept, _call, _owner_writer_headers, _task_continuation
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _submit(port: int, certificate: Path, call_id: str, headers: dict[str, str], submission_id: str, status: str, report: str, attachment_ids: object, task_version: object = 0):
    return _request(
        port, certificate, "POST", f"/api/calls/{call_id}/submissions",
        body={"submission_id": submission_id, "status": status, "report": report,
              "task_version": task_version, "attachment_ids": attachment_ids}, headers=headers,
    )


def _accepted_task_owner(tmp_path: Path, request_id: str):
    """Create the task interaction whose final-report UI this contract covers."""
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config); _wait_for_listener(process, port)
    created = _call(port, certificate, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    cookie, csrf = _login(port, certificate)
    owner = _headers(cookie, csrf, port)
    assert _accept(port, certificate, call_id, owner)[0] == 200
    writer = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
    return port, config, certificate, process, call_id, cookie, owner, writer


def _business_snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, version, task_version, terminal_last_seen_at, owner_device_id, owner_session_id, ring_deadline FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
        attachments = connection.execute("SELECT attachment_id FROM attachments WHERE call_id = ? ORDER BY rowid", (call_id,)).fetchall()
    return (*call, events, results, operations, attachments)


def test_tls_accepted_iphone_task_final_serializes_rehydrates_and_durably_returns_ordered_attachment_references(tmp_path: Path) -> None:
    port, config, certificate, process, call_id, cookie, owner, writer = _accepted_task_owner(tmp_path, "task-final-attached")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        first = _upload_browser(port, certificate, call_id, writer, filename="first.txt", content=b"first")
        second = _upload_browser(port, certificate, call_id, writer, filename="second.txt", content=b"second")
        first_id = first[1]["attachment"]["attachment_id"]; second_id = second[1]["attachment"]["attachment_id"]
        before = _business_snapshot(database, call_id)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after_read = _business_snapshot(database, call_id)
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        recovered = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after_restart_read = _business_snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            task_id = connection.execute("SELECT interaction_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
        completed = _submit(port, certificate, call_id, writer, "completed-attached", "completed", "done", [second_id, first_id])
        replay = _submit(port, certificate, call_id, writer, "completed-attached", "completed", "done", [second_id, first_id])
        changed = _submit(port, certificate, call_id, writer, "completed-attached", "completed", "done", [first_id, second_id])
        if completed[0] == 200:
            _, event, _ = _post(port, certificate, _task_continuation("attached-event", task_id, call_id, 1), "test-token-alpha")
            _, read_response, _ = _post(port, certificate, _rpc("tools/call", {"name": "read_interaction_result", "arguments": {"interaction_id": task_id, "result_id": completed[2]["result_id"], "max_result_bytes": 16_384}}, 201), "test-token-alpha")
    finally:
        _stop(process)

    source, restart_source = str(page[2]), str(recovered[2])
    assert first[0] == second[0] == 200 and before == after_read == after_restart_read
    assert first_id in source and second_id in source and first_id in restart_source and second_id in restart_source
    assert re.search(r'data-task-final="completed".*?attachment_ids:pendingAttachmentIds', source, re.DOTALL)
    assert re.search(r'data-task-final="failed".*?attachment_ids:pendingAttachmentIds', source, re.DOTALL)
    assert completed[0] == replay[0] == 200 and completed[2] == replay[2]
    assert completed[2]["attachment_ids"] == [second_id, first_id] and changed[0] == 409
    assert _structured(event)["attachment_ids"] == [second_id, first_id]
    assert _structured(read_response)["attachments"] == [
        {"attachment_id": second_id, "name": "second.txt", "media_type": "text/plain", "byte_count": 6, "sha256": second[1]["attachment"]["sha256"]},
        {"attachment_id": first_id, "name": "first.txt", "media_type": "text/plain", "byte_count": 5, "sha256": first[1]["attachment"]["sha256"]},
    ]


def test_tls_accepted_iphone_task_failed_attachment_references_validate_same_call_and_replay_immutably(tmp_path: Path) -> None:
    port, config, certificate, process, prior_id, _cookie, _owner, prior_writer = _accepted_task_owner(tmp_path, "task-final-foreign-source")
    try:
        foreign = _upload_browser(port, certificate, prior_id, prior_writer, content=b"foreign")
        foreign_id = foreign[1]["attachment"]["attachment_id"]
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute("UPDATE calls SET state = 'cancelled' WHERE call_id = ?", (prior_id,)); connection.commit()
        created = _call(port, certificate, "task-final-failed-attached")
        call_id = created["call_id"]
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        writer = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        upload = _upload_browser(port, certificate, call_id, writer, filename="failure.txt", content=b"failure")
        attachment_id = upload[1]["attachment"]["attachment_id"]
        database = tmp_path / "data" / "calls.sqlite3"; before = _business_snapshot(database, call_id)
        rejected = [
            _submit(port, certificate, call_id, writer, "missing", "failed", "report", ["missing"]),
            _submit(port, certificate, call_id, writer, "foreign", "failed", "report", [foreign_id]),
            _submit(port, certificate, call_id, writer, "duplicate", "failed", "report", [attachment_id, attachment_id]),
            _submit(port, certificate, call_id, writer, "malformed", "failed", "report", [42]),
            _submit(port, certificate, call_id, writer, "stale-version", "failed", "report", [attachment_id], 1),
            _submit(port, certificate, call_id, writer, "invalid-status", "other", "report", [attachment_id]),
        ]
        after = _business_snapshot(database, call_id)
        failed = _submit(port, certificate, call_id, writer, "failed-attached", "failed", "report", [attachment_id])
        failed_replay = _submit(port, certificate, call_id, writer, "failed-attached", "failed", "report", [attachment_id])
        failed_changed = _submit(port, certificate, call_id, writer, "failed-attached", "failed", "changed", [attachment_id])
    finally:
        _stop(process)
    assert foreign[0] == upload[0] == 200
    assert [item[2] for item in rejected[:4]] == [
        {"success": False, "status": "invalid_attachment_reference", "needs_continuation": False},
        {"success": False, "status": "invalid_attachment_reference", "needs_continuation": False},
        {"success": False, "status": "invalid_submission"},
        {"success": False, "status": "invalid_submission"},
    ]
    assert rejected[4][2] == {"success": False, "status": "task_version_conflict", "call_state": "accepted", "task_version": 0, "needs_continuation": False}
    assert rejected[5][2] == {"success": False, "status": "invalid_submission"}
    assert before == after and after[0] == "accepted"
    assert failed[0] == failed_replay[0] == 200 and failed[2] == failed_replay[2]
    assert failed[2]["status"] == failed[2]["call_state"] == "failed" and failed[2]["attachment_ids"] == [attachment_id]
    assert failed_changed[0] == 409


def test_tls_task_final_attachment_references_reject_untrusted_or_terminal_contexts_without_mutation(tmp_path: Path) -> None:
    port, _config, certificate, process, call_id, cookie, owner, writer = _accepted_task_owner(tmp_path, "task-final-denials")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        upload = _upload_browser(port, certificate, call_id, writer, content=b"evidence")
        attachment_id = upload[1]["attachment"]["attachment_id"]
        other_cookie, other_csrf = _login(port, certificate)
        before = _business_snapshot(database, call_id)
        denied = [
            _submit(port, certificate, call_id, owner, "no-writer", "completed", "report", [attachment_id]),
            _submit(port, certificate, call_id, {**writer, "X-Writer-Token": "stale"}, "stale", "completed", "report", [attachment_id]),
            _submit(port, certificate, call_id, {"Cookie": other_cookie, "X-CSRF-Token": other_csrf, "Origin": f"https://127.0.0.1:{port}", "X-Writer-Token": writer["X-Writer-Token"]}, "other", "completed", "report", [attachment_id]),
            _submit(port, certificate, call_id, {**writer, "Origin": "https://evil.example"}, "origin", "completed", "report", [attachment_id]),
            _submit(port, certificate, call_id, {**writer, "Host": "evil.example"}, "host", "completed", "report", [attachment_id]),
            _submit(port, certificate, call_id, {"Authorization": "Bearer terminal-secret"}, "bearer", "completed", "report", [attachment_id]),
        ]
        after = _business_snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        # A read is the established authoritative expiry settlement path; the
        # later write must not revive or mutate that terminal result.
        _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        expired = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "expired", "status": "completed", "report": "report"}, headers=writer)
        settled = _business_snapshot(database, call_id)
    finally:
        _stop(process)
    assert upload[0] == 200 and all(item[0] in {400, 401, 403, 404, 409} for item in denied)
    assert before == after and expired[0] in {400, 409} and settled[0] == "disconnected"
