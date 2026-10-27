"""Red HTTPS contracts for an accepted iPhone owner's liveness heartbeat."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation


def _accept(
    port: int, certificate: Path, call_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], object]:
    return _request(
        port, certificate, "POST", f"/api/calls/{call_id}/accept",
        body={"action_id": "heartbeat-accept"}, headers=headers,
    )


def _heartbeat(
    port: int, certificate: Path, call_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/heartbeat", body={}, headers=headers)


def _call_row(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, owner_device_id, owner_session_id, terminal_last_seen_at, "
            "terminal_disconnect_deadline, ring_deadline, version, task_version, draft_version "
            "FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
    assert row is not None
    return row


def _business_evidence(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        events = connection.execute(
            "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
        ).fetchall()
        submissions = connection.execute(
            "SELECT submission_id, result FROM submission_operations WHERE call_id = ? ORDER BY submission_id",
            (call_id,),
        ).fetchall()
        actions = connection.execute(
            "SELECT action_id, result FROM action_operations WHERE action_id LIKE ? ORDER BY action_id",
            (f"%:{call_id}:%",),
        ).fetchall()
    return tuple(events), tuple(submissions), tuple(actions)


def test_tls_accepted_iphone_owner_heartbeat_refreshes_only_liveness(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-heartbeat-owner")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        assert _accept(port, certificate, call_id, _headers(cookie, csrf, port))[0] == 200
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?",
                (
                    "2000-01-01T00:00:00+00:00",
                    (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                    call_id,
                ),
            )
            connection.commit()
        before = _call_row(database, call_id)
        business_before = _business_evidence(database, call_id)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        poll = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        after_reads = _call_row(database, call_id)
        refreshed = _heartbeat(port, certificate, call_id, _headers(cookie, csrf, port))
        after = _call_row(database, call_id)
        business_after = _business_evidence(database, call_id)
    finally:
        _stop(process)

    assert page[0] == poll[0] == 200
    assert before == after_reads
    assert refreshed[0] == 200
    assert isinstance(refreshed[2], dict)
    assert refreshed[2]["status"] == refreshed[2]["call_state"] == "accepted"
    assert after[:3] == ("accepted", "iphone_web", before[2])
    assert after[3] != before[3]
    assert after[4] != before[4]
    assert after[5:] == before[5:]
    assert business_after == business_before


def test_tls_iphone_heartbeat_requires_current_owner_session_and_same_origin(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-heartbeat-security")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate)
        other_cookie, other_csrf = _login(port, certificate)
        preaccept = _heartbeat(port, certificate, call_id, _headers(owner_cookie, owner_csrf, port))
        assert _call_row(database, call_id)[0] == "ringing"
        assert _accept(port, certificate, call_id, _headers(owner_cookie, owner_csrf, port))[0] == 200
        before = _call_row(database, call_id)
        attempts = (
            _heartbeat(port, certificate, call_id, {}),
            _heartbeat(port, certificate, call_id, {"Cookie": owner_cookie, "Origin": f"https://127.0.0.1:{port}"}),
            _heartbeat(port, certificate, call_id, {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"}),
            _heartbeat(port, certificate, call_id, {**_headers(owner_cookie, owner_csrf, port), "Host": "evil.example"}),
            _heartbeat(port, certificate, call_id, _headers(other_cookie, other_csrf, port)),
        )
        after = _call_row(database, call_id)
    finally:
        _stop(process)

    assert preaccept[0] in {400, 401, 403, 404, 409}
    assert all(status in {400, 401, 403, 404, 409} for status, _, _ in attempts)
    assert after == before


def test_tls_late_iphone_heartbeat_settles_disconnect_and_releases_busy_after_restart(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-heartbeat-expiry")
        call_id = created["call_id"]
        interaction_id = created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        assert _accept(port, certificate, call_id, _headers(cookie, csrf, port))[0] == 200
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?",
                (
                    (datetime.now(UTC) - timedelta(seconds=181)).isoformat(),
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    call_id,
                ),
            )
            connection.commit()
    finally:
        _stop(process)

    process = _start(config)
    _wait_for_listener(process, port)
    try:
        late = _heartbeat(port, certificate, call_id, _headers(cookie, csrf, port))
        after_original_owner = _call_row(database, call_id)
        restarted_cookie, restarted_csrf = _login(port, certificate)
        takeover = _heartbeat(port, certificate, call_id, _headers(restarted_cookie, restarted_csrf, port))
        after_new_session = _call_row(database, call_id)
        _, continuation, _ = _post(
            port, certificate, _continuation("iphone-heartbeat-terminal", interaction_id, call_id, 1), "test-token-alpha"
        )
        next_call = _create_call(port, certificate, "iphone-heartbeat-after-expiry")
        final = _call_row(database, call_id)
    finally:
        _stop(process)

    assert late[0] == 409
    assert isinstance(late[2], dict)
    assert late[2]["status"] == late[2]["call_state"] == "disconnected"
    assert late[2]["reason"] == "terminal_disconnected"
    assert _structured(continuation) == late[2]
    assert takeover[0] in {400, 401, 403, 404, 409}
    assert after_new_session == after_original_owner
    assert final[0] == "disconnected"
    assert final[1] == "iphone_web"
    assert next_call["status"] == "heartbeat"
    assert next_call["call_state"] == "ringing"


def test_tls_phone_excluded_call_cannot_be_refreshed_by_operator_session(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-heartbeat-no-phone-route")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        before = _call_row(database, call_id)
        denied = _heartbeat(port, certificate, call_id, _headers(cookie, csrf, port))
        after = _call_row(database, call_id)
    finally:
        _stop(process)

    assert denied[0] in {400, 401, 403, 404, 409}
    assert after == before
    assert after[0] == "ringing"
