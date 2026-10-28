"""Red HTTPS contracts for the authenticated iPhone ringing-page countdown."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, version, ring_deadline, terminal_last_seen_at, owner_device_id, "
            "owner_session_id FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        assert call is not None
        counts = tuple(
            connection.execute(query, (call_id,)).fetchone()[0]
            for query in (
                "SELECT count(*) FROM events WHERE call_id = ?",
                "SELECT count(*) FROM results WHERE call_id = ?",
                "SELECT count(*) FROM submission_operations WHERE call_id = ?",
            )
        )
        actions = connection.execute("SELECT count(*) FROM action_operations").fetchone()[0]
    return (*call, *counts, actions)


def _ringing_page(tmp_path: Path, request_id: str):
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    created = _create_call(port, certificate, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    cookie, csrf = _login(port, certificate)
    return port, config, certificate, process, call_id, cookie, csrf


def test_tls_ringing_page_renders_server_deadline_countdown_without_extending_it(tmp_path: Path) -> None:
    port, _, certificate, process, call_id, cookie, _ = _ringing_page(tmp_path, "ring-countdown")
    try:
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'data-ring-deadline=' in source
    assert 'data-remaining-seconds=' in source
    assert "const ringDeadline=" in source
    assert "Math.max(0" in source
    assert "setInterval" in source
    assert "/heartbeat" not in source


def test_tls_ringing_page_polls_relative_authoritative_state_and_reconciles_expiry(tmp_path: Path) -> None:
    port, _, certificate, process, call_id, cookie, _ = _ringing_page(tmp_path, "ring-reconcile")
    try:
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    source = str(page[2])
    endpoint = f"/api/calls/{call_id}"
    assert f"const ringingStateEndpoint='{endpoint}'" in source
    assert "fetch(ringingStateEndpoint,{method:'GET',credentials:'same-origin'})" in source
    assert "ringingPollTimer" in source
    assert "not_accepted" in source and "ring_timeout" in source
    assert "location.reload()" in source
    assert "clearInterval(ringingPollTimer)" in source


def test_tls_ringing_reads_restart_and_expiry_reconcile_without_mutating_or_late_actions(tmp_path: Path) -> None:
    port, config, certificate, process, call_id, cookie, csrf = _ringing_page(tmp_path, "ring-expiry")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _snapshot(database, call_id)
        first = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        poll = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        reloaded = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after_reads = _snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET ring_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        expired = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        page_after_expiry = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        late_accept = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept",
            body={"action_id": "late-countdown-accept"}, headers=_headers(cookie, csrf, port),
        )
        late_decline = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/decline",
            body={"action_id": "late-countdown-decline"}, headers=_headers(cookie, csrf, port),
        )
        final = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert first[0] == poll[0] == reloaded[0] == 200
    assert before == after_reads
    assert expired[0] == 200 and isinstance(expired[2], dict)
    assert expired[2]["call"]["state"] == "not_accepted"
    assert page_after_expiry[0] == 200
    rendered = str(page_after_expiry[2])
    assert 'data-state="not_accepted"' in rendered
    assert "/accept" not in rendered and "/decline" not in rendered
    assert late_accept[0] in {400, 409}
    assert late_decline[0] in {400, 409}
    assert final[0] == "not_accepted" and final[4:6] == (None, None)


def test_tls_ringing_read_and_action_boundaries_do_not_leak_or_mutate(tmp_path: Path) -> None:
    port, _, certificate, process, call_id, cookie, csrf = _ringing_page(tmp_path, "ring-boundaries")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _snapshot(database, call_id)
        anonymous = _request(port, certificate, "GET", f"/api/calls/{call_id}")
        forged_host = _request(
            port, certificate, "GET", f"/api/calls/{call_id}",
            headers={"Cookie": cookie, "Host": "forged.invalid"},
        )
        nonexistent = _request(port, certificate, "GET", "/api/calls/not-a-real-call", headers={"Cookie": cookie})
        missing_csrf = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "missing-csrf"},
            headers={"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}"},
        )
        foreign_origin = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/decline", body={"action_id": "foreign-origin"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        )
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert anonymous[0] in {401, 403}
    assert forged_host[0] in {400, 401, 403}
    assert nonexistent[0] in {403, 404}
    assert missing_csrf[0] in {400, 403}
    assert foreign_origin[0] in {400, 403}
    for response in (anonymous, forged_host, nonexistent, missing_csrf, foreign_origin):
        assert "private page detail" not in str(response[2])
    assert before == after
