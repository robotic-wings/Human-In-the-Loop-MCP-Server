"""Red HTTPS contracts for the accepted iPhone owner's liveness warning."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _accept
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _accepted_owner(tmp_path: Path, request_id: str):
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    created = _create_call(port, certificate, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    owner_cookie, owner_csrf = _login(port, certificate)
    _accept(port, certificate, call_id, _headers(owner_cookie, owner_csrf, port))
    return port, config, certificate, process, call_id, owner_cookie, owner_csrf


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, version, owner_device_id, owner_session_id, terminal_last_seen_at, "
            "terminal_disconnect_deadline, ring_deadline, task_version, draft_version "
            "FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        assert call is not None
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        submissions = connection.execute(
            "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
        ).fetchone()[0]
        actions = connection.execute("SELECT count(*) FROM action_operations").fetchone()[0]
    return (*call, events, results, submissions, actions)


def test_tls_accepted_iphone_owner_page_warns_about_lock_background_liveness_loss(tmp_path: Path) -> None:
    port, _, certificate, process, call_id, cookie, _ = _accepted_owner(tmp_path, "owner-liveness-warning")
    try:
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    # The warning is an owner-page aid, not a promise of a hard-coded timeout.
    assert 'data-owner-liveness-warning' in source
    assert "background" in source.lower() and "lock" in source.lower()
    assert "liveness" in source.lower() and "ends the call" in source.lower()
    assert "180 seconds" not in source.lower()
    assert "submitted" not in source.lower()


def test_tls_liveness_warning_is_owner_only_and_read_only(tmp_path: Path) -> None:
    port, _, certificate, process, call_id, owner_cookie, _ = _accepted_owner(tmp_path, "owner-liveness-boundary")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        other_cookie, _ = _login(port, certificate)
        before = _snapshot(database, call_id)
        owner = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie})
        other = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie})
        api_read = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": owner_cookie})
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert owner[0] == other[0] == api_read[0] == 200
    assert 'data-owner-liveness-warning' not in str(other[2])
    assert before == after


def test_tls_owner_warning_reads_do_not_revive_expired_call_or_leak_to_denied_readers(tmp_path: Path) -> None:
    port, config, certificate, process, call_id, cookie, _ = _accepted_owner(tmp_path, "owner-liveness-expiry")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(),
                 (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        expired = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        anonymous = _request(port, certificate, "GET", f"/calls/{call_id}")
        forged_host = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie, "Host": "forged.invalid"},
        )
        final = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert expired[0] == 200 and 'data-state="disconnected"' in str(expired[2])
    assert final[0] == "disconnected"
    # The HTML route may deliberately return a generic sign-in page rather than
    # an HTTP denial, but it must not disclose the accepted owner's call state.
    assert anonymous[0] in {200, 401, 403}
    assert "Operator sign in required" in str(anonymous[2])
    assert forged_host[0] in {400, 401, 403}
    assert 'data-owner-liveness-warning' not in str(anonymous[2])
    assert 'data-owner-liveness-warning' not in str(forged_host[2])
