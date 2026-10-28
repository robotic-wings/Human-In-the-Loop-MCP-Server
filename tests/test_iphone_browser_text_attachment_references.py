"""Red HTTPS contract for iPhone ordinary-text submissions with uploaded files.

This slice is deliberately limited to simple text answers.  It makes an
already archived browser upload selectable for the final answer; it does not
redesign archival, task reports, or result paging.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _rpc, _start, _stop, _structured, _wait_for_listener
from test_iphone_browser_attachment_upload import _accepted_owner, _snapshot, _upload_browser
from test_iphone_multiline_submission import _call as _multiline_call
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _submit(port: int, certificate: Path, call_id: str, headers: dict[str, str], submission_id: str, body: str, attachment_ids: list[str]):
    return _request(
        port, certificate, "POST", f"/api/calls/{call_id}/submissions",
        body={"submission_id": submission_id, "user_input": body, "attachment_ids": attachment_ids},
        headers=headers,
    )


def test_tls_browser_text_submission_serializes_only_uploaded_attachment_ids_and_rehydrates_pending_metadata(tmp_path: Path) -> None:
    """The accepted owner page, rather than a hidden route, owns this workflow."""
    port, config, certificate, process, call_id, cookie, _owner, writer = _accepted_owner(tmp_path, "browser-text-attachment")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        uploaded = _upload_browser(port, certificate, call_id, writer, filename="evidence.txt", content=b"proof")
        attachment = uploaded[1]["attachment"]
        with sqlite3.connect(database) as connection:
            seen_before_read = connection.execute("SELECT terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
        first_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(database) as connection:
            seen_after_read = connection.execute("SELECT terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        recovered_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(database) as connection:
            seen_after_restart_read = connection.execute("SELECT terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert uploaded[0] == 200
    assert seen_before_read == seen_after_read == seen_after_restart_read
    source = str(first_page[2])
    recovered = str(recovered_page[2])
    # A successful upload must become an identified pending reference, survive a
    # reload, and be the exact ordered list carried by the ordinary text submit.
    assert attachment["attachment_id"] in source and attachment["attachment_id"] in recovered
    assert re.search(r"pendingAttachmentIds.*?uploaded\.attachment\.attachment_id", source, re.DOTALL)
    assert re.search(
        r"button\[data-text-input\].*?JSON\.stringify\(\{"
        r"submission_id:crypto\.randomUUID\(\),user_input:user_input,attachment_ids:pendingAttachmentIds\}\)",
        source,
        re.DOTALL,
    )
    # Failed uploads may not manufacture a selectable/referenceable ID.
    assert re.search(r"response\.ok.*?pendingAttachmentIds\.push", source, re.DOTALL)


def test_tls_browser_text_attachment_references_are_ordered_idempotent_and_same_call_only(tmp_path: Path) -> None:
    """Protect the existing coordinator seam while the browser gateway is added."""
    port, _config, certificate, process, call_id, _cookie, _owner, writer = _accepted_owner(tmp_path, "browser-text-reference-api")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        first = _upload_browser(port, certificate, call_id, writer, filename="one.txt", content=b"one")
        second = _upload_browser(port, certificate, call_id, writer, filename="two.txt", content=b"two")
        first_id = first[1]["attachment"]["attachment_id"]; second_id = second[1]["attachment"]["attachment_id"]
        submitted = _submit(port, certificate, call_id, writer, "attached-once", "answer", [second_id, first_id])
        replay = _submit(port, certificate, call_id, writer, "attached-once", "answer", [second_id, first_id])
        changed = _submit(port, certificate, call_id, writer, "attached-once", "answer", [first_id, second_id])
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert first[0] == second[0] == submitted[0] == replay[0] == 200
    assert submitted[2] == replay[2] and submitted[2]["attachment_ids"] == [second_id, first_id]
    assert changed[0] == 409 and state == ("answered",) and events == 2


def test_tls_browser_text_attached_result_event_and_authorized_read_expose_metadata(tmp_path: Path) -> None:
    port, _config, certificate, process, call_id, _cookie, _owner, writer = _accepted_owner(tmp_path, "browser-result-metadata")
    try:
        upload = _upload_browser(port, certificate, call_id, writer, filename="evidence.txt", content=b"evidence")
        attachment_id = upload[1]["attachment"]["attachment_id"]
        submitted = _submit(port, certificate, call_id, writer, "metadata", "answer", [attachment_id])
        result_id = submitted[2]["result_id"]
        status, response, _ = _post(
            port, certificate,
            _rpc("tools/call", {"name": "read_interaction_result", "arguments": {
                "interaction_id": submitted[2]["interaction_id"], "result_id": result_id, "max_result_bytes": 16_384,
            }}, 77), "test-token-alpha",
        )
    finally:
        _stop(process)

    read = _structured(response)
    assert upload[0] == submitted[0] == status == 200
    assert submitted[2]["attachment_ids"] == [attachment_id]
    assert read["attachments"] == [{
        "attachment_id": attachment_id, "name": "evidence.txt", "media_type": "text/plain",
        "byte_count": 8, "sha256": upload[1]["attachment"]["sha256"],
    }]


def test_tls_browser_text_attachment_reference_rejections_leave_accepted_busy_call_unchanged(tmp_path: Path) -> None:
    port, _config, certificate, process, call_id, _cookie, _owner, writer = _accepted_owner(tmp_path, "browser-reference-reject")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        upload = _upload_browser(port, certificate, call_id, writer, content=b"one")
        attachment_id = upload[1]["attachment"]["attachment_id"]
        before = _snapshot(database, call_id)
        rejected = [
            _submit(port, certificate, call_id, writer, "missing", "answer", ["missing-id"]),
            _submit(port, certificate, call_id, writer, "duplicate", "answer", [attachment_id, attachment_id]),
            _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "malformed", "user_input": "answer", "attachment_ids": [42]}, headers=writer),
        ]
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert all(item[0] in {400, 409} for item in rejected)
    assert before == after and after[0] == "accepted"


def test_tls_browser_text_attachment_references_reject_untrusted_contexts_and_nontext_forms(tmp_path: Path) -> None:
    port, config, certificate, process, call_id, cookie, owner, writer = _accepted_owner(tmp_path, "browser-reference-deny")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        upload = _upload_browser(port, certificate, call_id, writer, content=b"one")
        attachment_id = upload[1]["attachment"]["attachment_id"]
        other_cookie, other_csrf = _login(port, certificate)
        before = _snapshot(database, call_id)
        denied = [
            _submit(port, certificate, call_id, owner, "no-writer", "answer", [attachment_id]),
            _submit(port, certificate, call_id, {**writer, "X-Writer-Token": "stale"}, "stale", "answer", [attachment_id]),
            _submit(port, certificate, call_id, {"Cookie": other_cookie, "X-CSRF-Token": other_csrf, "Origin": f"https://127.0.0.1:{port}", "X-Writer-Token": writer["X-Writer-Token"]}, "other", "answer", [attachment_id]),
            _submit(port, certificate, call_id, {**writer, "Origin": "https://evil.example"}, "origin", "answer", [attachment_id]),
            _submit(port, certificate, call_id, {**writer, "Host": "evil.example"}, "host", "answer", [attachment_id]),
            _submit(port, certificate, call_id, {"Authorization": "Bearer terminal-secret"}, "bearer", "answer", [attachment_id]),
        ]
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in denied)
    assert before == after

    # A non-text form must not silently accept attachment IDs through the text path.
    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "multiline", port); _configure_phone_route(config)
    process = _start(config); _wait_for_listener(process, port)
    try:
        created = _multiline_call(port, certificate, "multiline-attachment-ids"); multi_id = created["call_id"]
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        _request(port, certificate, "POST", f"/api/calls/{multi_id}/accept", body={"action_id": "accept"}, headers=headers)
        from test_iphone_browser_drafts import _writer_headers
        multi_writer = _writer_headers(port, certificate, multi_id, headers, "nontext-attachment")
        nontext = _request(port, certificate, "POST", f"/api/calls/{multi_id}/submissions", body={"submission_id": "nontext", "user_input": "line", "attachment_ids": ["not-allowed"]}, headers=multi_writer)
    finally:
        _stop(process)
    assert nontext[0] in {400, 409}


def test_tls_browser_text_attachment_references_reject_missing_csrf_foreign_ringing_route_and_expiry(tmp_path: Path) -> None:
    """Every rejected context leaves an answer/event/busy transition unavailable."""
    # A same-API foreign archived ID cannot be substituted into a later call.
    port, _config, certificate, process, first_id, _cookie, _owner, first_writer = _accepted_owner(tmp_path, "browser-foreign-source")
    try:
        foreign_upload = _upload_browser(port, certificate, first_id, first_writer, content=b"foreign")
        foreign_id = foreign_upload[1]["attachment"]["attachment_id"]
        # Archive provenance is setup only: release the single busy line without
        # giving this browser test a second terminal workflow.
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute("UPDATE calls SET state = 'cancelled' WHERE call_id = ?", (first_id,)); connection.commit()
        created = _create_call(port, certificate, "browser-foreign-target"); call_id = created["call_id"]
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port)
        _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "accept-target"}, headers=owner)
        from test_iphone_browser_drafts import _writer_headers
        writer = _writer_headers(port, certificate, call_id, owner, "foreign-target")
        database = tmp_path / "data" / "calls.sqlite3"; before = _snapshot(database, call_id)
        foreign = _submit(port, certificate, call_id, writer, "foreign", "answer", [foreign_id])
        missing_csrf = _submit(port, certificate, call_id, {"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}", "X-Writer-Token": writer["X-Writer-Token"]}, "csrf", "answer", [])
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert foreign[0] in {400, 409} and missing_csrf[0] in {400, 401, 403, 409}
    assert before == after and after[0] == "accepted"

    # Phone route is an authorization boundary both before acceptance and when excluded.
    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "ringing", port); _configure_phone_route(config)
    process = _start(config); _wait_for_listener(process, port)
    database = tmp_path / "ringing" / "data" / "calls.sqlite3"
    try:
        call_id = _create_call(port, certificate, "browser-reference-ringing")["call_id"]
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port); before = _snapshot(database, call_id)
        ringing = _submit(port, certificate, call_id, {**owner, "X-Writer-Token": "not-issued"}, "ringing", "answer", [])
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert ringing[0] in {400, 401, 403, 404, 409} and before == after

    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "excluded", port)
    process = _start(config); _wait_for_listener(process, port)
    database = tmp_path / "excluded" / "data" / "calls.sqlite3"
    try:
        call_id = _create_call(port, certificate, "browser-reference-excluded")["call_id"]
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port); before = _snapshot(database, call_id)
        excluded = _submit(port, certificate, call_id, {**owner, "X-Writer-Token": "not-issued"}, "excluded", "answer", [])
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert excluded[0] in {400, 401, 403, 404, 409} and before == after

    port, _config, certificate, process, call_id, _cookie, _owner, writer = _accepted_owner(tmp_path / "expired", "browser-reference-expired")
    database = tmp_path / "expired" / "data" / "calls.sqlite3"
    try:
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        expired = _submit(port, certificate, call_id, writer, "expired", "answer", [])
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert expired[0] in {400, 401, 403, 404, 409}
    assert after[0] == "disconnected" and after[5:] == (2, 0, 0)
