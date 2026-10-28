"""Red HTTPS contracts for one writable iPhone browser page per owner session."""

from __future__ import annotations

import sqlite3
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _accept
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _writer(
    port: int,
    cert: Path,
    call_id: str,
    suffix: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
):
    return _request(port, cert, "POST", f"/api/calls/{call_id}/writer/{suffix}", body=body, headers=headers)


def _business_snapshot(db: Path, call_id: str) -> tuple[object, ...]:
    """Only business records; writer arbitration may persist separate lease state."""
    with sqlite3.connect(db) as connection:
        call = connection.execute(
            "SELECT state, owner_session_id, version, task_version, draft_version, draft_body "
            "FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        bark = connection.execute("SELECT count(*) FROM bark_deliveries WHERE call_id = ?", (call_id,)).fetchone()[0]
        callbacks = connection.execute("SELECT count(*) FROM terminal_callback_deliveries WHERE call_id = ?", (call_id,)).fetchone()[0]
        actions = connection.execute("SELECT count(*) FROM action_operations").fetchone()[0]
    return (*call, events, results, bark, callbacks, actions)


def _accepted_owner(tmp_path: Path, request_id: str):
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


def test_tls_accepted_writer_page_exposes_readonly_takeover_and_revocation_ui(tmp_path: Path) -> None:
    """A refreshed same-session page must offer a visible, operable ownership switch."""
    port, _, cert, process, call_id, cookie, _, _ = _accepted_owner(tmp_path, "writer-page-ui")
    db = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _business_snapshot(db, call_id)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _business_snapshot(db, call_id)
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'data-writer-status="' in source
    assert 'data-writer-takeover' in source
    # Acquire must make the first page writable while rendering a second page as read-only,
    # rather than leaving recovery to an otherwise invisible server endpoint.
    assert re.search(
        r"writerEndpoint\+'\/acquire'.*?lease\.writable.*?setWriterState\(true\).*?"
        r"setWriterState\(false\)",
        source,
        re.DOTALL,
    )
    # The user-facing switch has to call takeover, install the replacement token, and make
    # that refreshed page writable.  Route-only implementations therefore cannot satisfy it.
    assert re.search(
        r"data-writer-takeover.*?addEventListener\('click'.*?writerEndpoint\+'\/takeover'.*?"
        r"lease\.writer_token.*?setWriterState\(true\)",
        source,
        re.DOTALL,
    )
    # When takeover revokes the old token, its page must become visibly read-only on the
    # next protected write instead of continuing to present writable controls.
    assert re.search(
        r"response\.status\s*===\s*401.*?response\.status\s*===\s*403.*?setWriterState\(false\)",
        source,
        re.DOTALL,
    )
    assert before == after


def test_tls_browser_writer_first_page_is_writable_peer_is_readonly_and_takeover_revokes(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, csrf, headers = _accepted_owner(tmp_path, "writer-first")
    try:
        first = _writer(port, cert, call_id, "acquire", body={"page_id": "tab-one", "action_id": "writer-one"}, headers=headers)
        second = _writer(port, cert, call_id, "acquire", body={"page_id": "tab-two", "action_id": "writer-two"}, headers=headers)
        assert first[0] == 200 and isinstance(first[2], dict)
        token = first[2].get("writer_token")
        assert isinstance(token, str) and token
        assert second[0] == 200 and isinstance(second[2], dict)
        assert second[2].get("writable") is False and second[2].get("writer_token") is None
        old_status = _writer(port, cert, call_id, "status", body={"page_id": "tab-one", "writer_token": token}, headers=headers)
        takeover = _writer(port, cert, call_id, "takeover", body={"page_id": "tab-two", "action_id": "writer-takeover"}, headers=headers)
        assert takeover[0] == 200 and isinstance(takeover[2], dict)
        replacement = takeover[2].get("writer_token")
        assert isinstance(replacement, str) and replacement and replacement != token
        revoked = _writer(port, cert, call_id, "status", body={"page_id": "tab-one", "writer_token": token}, headers=headers)
        current = _writer(port, cert, call_id, "status", body={"page_id": "tab-two", "writer_token": replacement}, headers=headers)
    finally:
        _stop(process)

    assert old_status[0] == 200 and isinstance(old_status[2], dict) and old_status[2].get("writable") is True
    assert revoked[0] in {401, 403, 404, 409}
    assert current[0] == 200 and isinstance(current[2], dict) and current[2].get("writable") is True


def test_tls_browser_writer_is_atomic_restart_safe_and_has_no_business_side_effects(tmp_path: Path) -> None:
    port, config, cert, process, call_id, cookie, csrf, headers = _accepted_owner(tmp_path, "writer-concurrent")
    db = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _business_snapshot(db, call_id)
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(_writer, port, cert, call_id, "acquire", body={"page_id": page, "action_id": f"acquire-{page}"}, headers=headers)
                for page in ("left", "right")
            ]
            raced = [item.result() for item in futures]
        writable = [item for item in raced if item[0] == 200 and isinstance(item[2], dict) and item[2].get("writable") is True]
        readonly = [item for item in raced if item[0] == 200 and isinstance(item[2], dict) and item[2].get("writable") is False]
        assert len(writable) == len(readonly) == 1
        winner = writable[0][2]
        assert isinstance(winner, dict)
        winner_token = winner.get("writer_token")
        assert isinstance(winner_token, str) and winner_token
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        recovered = _writer(port, cert, call_id, "status", body={"page_id": winner.get("page_id"), "writer_token": winner_token}, headers=headers)
        after = _business_snapshot(db, call_id)
    finally:
        _stop(process)

    assert recovered[0] == 200 and isinstance(recovered[2], dict) and recovered[2].get("writable") is True
    assert before == after


def test_tls_browser_writer_rejects_wrong_context_and_settles_expired_owner_without_leak(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, csrf, headers = _accepted_owner(tmp_path, "writer-deny")
    db = tmp_path / "data" / "calls.sqlite3"
    try:
        other_cookie, other_csrf = _login(port, cert)
        acquired = _writer(port, cert, call_id, "acquire", body={"page_id": "owner", "action_id": "writer-owner"}, headers=headers)
        assert acquired[0] == 200 and isinstance(acquired[2], dict)
        token = acquired[2].get("writer_token")
        assert isinstance(token, str) and token
        denied = (
            _writer(port, cert, call_id, "status", body={"page_id": "owner", "writer_token": token}, headers=_headers(other_cookie, other_csrf, port)),
            _writer(port, cert, call_id, "status", body={"page_id": "owner", "writer_token": "tampered"}, headers=headers),
            _writer(port, cert, call_id, "takeover", body={"page_id": "other", "action_id": "missing-csrf"}, headers={"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}"}),
            _writer(port, cert, call_id, "takeover", body={"page_id": "other", "action_id": "wrong-origin"}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": "https://evil.example"}),
            _writer(port, cert, call_id, "takeover", body={"page_id": "other", "action_id": "wrong-host"}, headers={**headers, "Host": "evil.example"}),
            _writer(port, cert, call_id, "status", body={"page_id": "owner", "writer_token": token}, headers={"Authorization": "Bearer terminal-secret"}),
        )
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        expired = _writer(port, cert, call_id, "status", body={"page_id": "owner", "writer_token": token}, headers=headers)
        final = _business_snapshot(db, call_id)
    finally:
        _stop(process)

    assert all(item[0] in {400, 401, 403, 404, 409} for item in denied)
    assert all("writer_token" not in repr(item[2]) for item in denied)
    assert expired[0] in {401, 403, 404, 409}
    assert final[0] == "disconnected"


def test_tls_browser_writer_rejects_ringing_and_route_excluded_calls_without_leak_or_mutation(tmp_path: Path) -> None:
    """A browser writer lease is only meaningful for an accepted phone-routed call."""
    port = _free_port()
    config, cert = _write_operator_config(tmp_path / "ringing", port)
    _configure_phone_route(config)
    db = tmp_path / "ringing" / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, cert, "writer-ringing")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert)
        before = _business_snapshot(db, call_id)
        ringing = _writer(port, cert, call_id, "acquire", body={"page_id": "ringing", "action_id": "writer-ringing"}, headers=_headers(cookie, csrf, port))
        after = _business_snapshot(db, call_id)
    finally:
        _stop(process)

    port = _free_port()
    config, cert = _write_operator_config(tmp_path / "excluded", port)
    db = tmp_path / "excluded" / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, cert, "writer-route-excluded")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert)
        before_route = _business_snapshot(db, call_id)
        excluded = _writer(port, cert, call_id, "acquire", body={"page_id": "excluded", "action_id": "writer-excluded"}, headers=_headers(cookie, csrf, port))
        after_route = _business_snapshot(db, call_id)
    finally:
        _stop(process)

    assert ringing[0] in {400, 401, 403, 404, 409}
    assert excluded[0] in {400, 401, 403, 404, 409}
    assert "writer_token" not in repr(ringing[2]) and "writer_token" not in repr(excluded[2])
    assert before == after
    assert before_route == after_route
