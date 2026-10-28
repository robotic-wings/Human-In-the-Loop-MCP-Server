"""Red HTTPS contracts for authoritative active-iPhone call-page reconciliation."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _accept
from test_iphone_browser_writer import _writer
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _row(db: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(db) as connection:
        call = connection.execute(
            "SELECT state, version, owner_session_id, terminal_last_seen_at, ring_deadline, "
            "task_version, draft_version FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        assert call is not None
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        actions = connection.execute("SELECT count(*) FROM action_operations").fetchone()[0]
        submissions = connection.execute(
            "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
        ).fetchone()[0]
    return (*call, events, results, actions, submissions)


def _accepted_page(tmp_path: Path, request_id: str):
    port = _free_port()
    config, cert = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    created = _create_call(port, cert, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    cookie, csrf = _login(port, cert)
    headers = _headers(cookie, csrf, port)
    _accept(port, cert, call_id, headers)
    return port, config, cert, process, call_id, cookie, csrf, headers


def test_tls_active_iphone_page_polls_authoritative_relative_state_every_two_seconds(tmp_path: Path) -> None:
    """Polling must reconcile server state, not merely refresh a heartbeat or local UI."""
    port, _, cert, process, call_id, cookie, _, _ = _accepted_page(tmp_path, "page-authoritative-poll")
    try:
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert f"const authoritativeStateEndpoint='{f'/api/calls/{call_id}'}'" in source
    assert "setInterval(async()=>" in source
    assert "authoritativePollTimer" in source
    assert "},2000);" in source
    assert "fetch(authoritativeStateEndpoint,{method:'GET',credentials:'same-origin'})" in source
    assert "reconcileAuthoritativeCallState" in source
    assert "clearInterval(authoritativePollTimer)" in source
    assert "Connection/state unavailable." in source
    poll_source = source.split("authoritativePollTimer", 1)[1]
    assert "writerEndpoint+'/acquire'" not in poll_source
    assert "writerEndpoint+'/takeover'" not in poll_source
    # All terminal/ownership states must replace stale writer/form controls rather than
    # claiming success from an old page after another endpoint changed the call.
    for state in ("desktop", "answered", "declined", "disconnected", "cancelled", "completed", "failed"):
        assert state in source
    assert "setWriterState(false)" in source
    assert "location.reload()" in source


def test_tls_authoritative_poll_reads_are_side_effect_free_and_invalid_reads_cannot_refresh_owner(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, csrf, headers = _accepted_page(tmp_path, "page-authoritative-read")
    db = tmp_path / "data" / "calls.sqlite3"
    try:
        acquired = _writer(
            port, cert, call_id, "acquire", body={"page_id": "poll-owner", "action_id": "poll-acquire"}, headers=headers,
        )
        assert acquired[0] == 200 and isinstance(acquired[2], dict)
        before = _row(db, call_id)
        reads = (
            _request(port, cert, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie}),
            _request(port, cert, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie, "Host": "forged.invalid"}),
            _request(port, cert, "GET", "/api/calls/not-a-real-call", headers={"Cookie": cookie}),
            _request(port, cert, "GET", f"/api/calls/{call_id}"),
            _request(port, cert, "GET", f"/api/calls/{call_id}", headers={"Cookie": "operator_session=forged"}),
        )
        session_id = cookie.split("=", 1)[1]
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE operator_sessions SET expires_at = ? WHERE session_id = ?",
                (datetime.now(UTC).isoformat(), session_id),
            )
            connection.commit()
        expired = _request(port, cert, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        after = _row(db, call_id)
    finally:
        _stop(process)

    assert reads[0][0] == 200
    assert reads[1][0] in {400, 401, 403}
    assert reads[2][0] in {403, 404}
    assert reads[3][0] in {401, 403}
    assert reads[4][0] in {401, 403, 404}
    assert expired[0] in {401, 403, 404}
    for response in (*reads[1:], expired):
        assert "private page detail" not in str(response[2])
    assert before == after
    # A polling read is not an implicit heartbeat or writer acquisition.
    assert before[3] == after[3]


def test_tls_stale_same_session_writer_remains_readonly_without_poll_reacquisition(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, _, headers = _accepted_page(tmp_path, "page-authoritative-takeover")
    try:
        first = _writer(port, cert, call_id, "acquire", body={"page_id": "old", "action_id": "old-acquire"}, headers=headers)
        assert first[0] == 200 and isinstance(first[2], dict)
        old_token = first[2].get("writer_token")
        assert isinstance(old_token, str)
        takeover = _writer(port, cert, call_id, "takeover", body={"page_id": "new", "action_id": "new-takeover"}, headers=headers)
        assert takeover[0] == 200 and isinstance(takeover[2], dict)
        stale = _writer(port, cert, call_id, "status", body={"page_id": "old", "writer_token": old_token}, headers=headers)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert stale[0] in {401, 403, 404, 409}
    assert page[0] == 200


def test_tls_active_page_polling_reconciliation_is_restart_safe_and_does_not_acquire_writer(tmp_path: Path) -> None:
    port, config, cert, process, call_id, cookie, _, _ = _accepted_page(tmp_path, "page-authoritative-restart")
    db = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _row(db, call_id)
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        reloaded = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _row(db, call_id)
    finally:
        _stop(process)

    assert reloaded[0] == 200
    assert before == after
