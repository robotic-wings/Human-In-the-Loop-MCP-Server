"""Red HTTPS contracts for standalone accepted-iPhone feedback drafts."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _draft, _snapshot, _writer_headers
from test_iphone_feedback_submission import _accept, _call
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _accepted_feedback(port: int, cert: Path, request_id: str, headers: dict[str, str]) -> str:
    created = _call(port, cert, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    assert _accept(port, cert, call_id, headers)[0] == 200
    return call_id


def _feedback_draft_page(source: object, call_id: str) -> str:
    page = str(source)
    assert re.search(
        r'<textarea(?=[^>]*data-feedback-text)(?=[^>]*data-draft-input="feedback")'
        r'(?=[^>]*data-draft-version="1")[^>]*>saved feedback</textarea>', page,
    )
    assert f'data-draft-save data-endpoint="/api/calls/{call_id}/draft"' in page
    assert 'data-draft-status="saved"' in page.lower()
    assert "not-synced" in page and "addEventListener('input'" in page
    assert re.search(
        r"data-feedback-send.*?const feedbackText=document\.querySelector\('textarea\[data-feedback-text\]'\)\.value",
        page,
        re.DOTALL,
    )
    return page


def _assert_stale_client_contract(source: object) -> None:
    page = str(source)
    assert re.search(
        r"draftSave\.addEventListener\('click'.*?response\.ok.*?"
        r"draftStatus\.dataset\.draftStatus='saved'.*?else.*?"
        r"draftStatus\.dataset\.draftStatus='not-synced'.*?(?:conflict|not-synced)",
        page,
        re.DOTALL,
    )
    failed = re.search(r"else\s*\{(?P<body>.*?)\}", page, re.DOTALL)
    assert failed is not None and "draftInput.value=" not in failed.group("body")


def test_tls_accepted_iphone_feedback_draft_hydrates_after_reload_and_restart(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        call_id = _accepted_feedback(port, cert, "feedback-draft-hydrate", owner)
        writer = _writer_headers(port, cert, call_id, owner, "feedback-draft-hydrate")
        before = _snapshot(db, call_id)
        saved = _draft(port, cert, call_id, "PUT", body={"body": "saved feedback", "expected_draft_version": 0}, headers=writer)
        first = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        second = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert saved[0] == 200 and isinstance(saved[2], dict) and saved[2].get("draft_version") == 1
    assert before[:3] == after[:3] == ("accepted", before[1], before[2]) and before[5:] == after[5:]
    assert after[3:5] == (1, "saved feedback")
    assert first[0] == second[0] == 200
    _feedback_draft_page(first[2], call_id); _feedback_draft_page(second[2], call_id)


def test_tls_accepted_iphone_feedback_draft_cas_conflict_preserves_local_text(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        call_id = _accepted_feedback(port, cert, "feedback-draft-cas", owner)
        writer = _writer_headers(port, cert, call_id, owner, "feedback-draft-cas")
        winner = _draft(port, cert, call_id, "PUT", body={"body": "winner", "expected_draft_version": 0}, headers=writer)
        before = _snapshot(db, call_id)
        stale = _draft(port, cert, call_id, "PUT", body={"body": "loser", "expected_draft_version": 0}, headers=writer)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert winner[0] == 200 and stale[0] == 409 and before == after and after[4] == "winner"
    assert page[0] == 200
    _assert_stale_client_contract(page[2])


def test_tls_feedback_draft_controls_are_owner_only_and_expiry_cannot_revive(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        owner_cookie, owner_csrf = _login(port, cert); other_cookie, other_csrf = _login(port, cert)
        owner = _headers(owner_cookie, owner_csrf, port)
        call_id = _accepted_feedback(port, cert, "feedback-draft-deny", owner)
        writer = _writer_headers(port, cert, call_id, owner, "feedback-draft-deny")
        assert _draft(port, cert, call_id, "PUT", body={"body": "private", "expected_draft_version": 0}, headers=writer)[0] == 200
        before = _snapshot(db, call_id)
        denied = (
            _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie}),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 1}, headers=_headers(other_cookie, other_csrf, port)),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 1}, headers=owner),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 1}, headers={**writer, "X-CSRF-Token": "forged-csrf"}),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 1}, headers={**writer, "Origin": "https://evil.example"}),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 1}, headers={**writer, "Host": "evil.example"}),
            _request(port, cert, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer terminal-secret"}),
        )
        after_denied = _snapshot(db, call_id)
        with sqlite3.connect(db) as connection:
            now = datetime.now(UTC)
            connection.execute("UPDATE calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?", ((now - timedelta(seconds=181)).isoformat(), (now - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        expired = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie})
        after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert after_denied == before
    assert all('data-draft-input="feedback"' not in str(item[2]) and "private" not in str(item[2]) for item in denied)
    assert expired[0] == 200 and 'data-draft-input="feedback"' not in str(expired[2])
    assert after[0] == "disconnected" and after[4] == "private" and after[5] == before[5] + 1 and after[6:] == before[6:]


def test_tls_feedback_draft_controls_hide_for_ringing_desktop_ended_and_revoked_writer(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        ringing = _call(port, cert, "feedback-draft-ringing"); ringing_id = ringing["call_id"]; assert isinstance(ringing_id, str)
        ringing_before = _snapshot(db, ringing_id); ringing_page = _request(port, cert, "GET", f"/calls/{ringing_id}", headers={"Cookie": cookie}); ringing_after = _snapshot(db, ringing_id)
        with sqlite3.connect(db) as connection: connection.execute("UPDATE calls SET state='not_accepted' WHERE call_id=?", (ringing_id,)); connection.commit()
        call_id = _accepted_feedback(port, cert, "feedback-draft-negative", owner)
        old_writer = _writer_headers(port, cert, call_id, owner, "feedback-draft-old")
        takeover = _request(port, cert, "POST", f"/api/calls/{call_id}/writer/takeover", body={"page_id": "replacement", "action_id": "takeover"}, headers=owner)
        before_old = _snapshot(db, call_id); revoked = _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 0}, headers=old_writer); after_old = _snapshot(db, call_id)
        with sqlite3.connect(db) as connection: connection.execute("UPDATE calls SET owner_device_id='desktop-1', owner_session_id='desktop-session', handled_on='desktop', terminal_platform='windows' WHERE call_id=?", (call_id,)); connection.commit()
        desktop_before = _snapshot(db, call_id); desktop = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie}); desktop_after = _snapshot(db, call_id)
        with sqlite3.connect(db) as connection: connection.execute("UPDATE calls SET state='answered' WHERE call_id=?", (call_id,)); connection.commit()
        ended_before = _snapshot(db, call_id); ended = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie}); ended_save = _draft(port, cert, call_id, "PUT", body={"body": "revive", "expected_draft_version": 0}, headers=old_writer); ended_after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert ringing_page[0] == 200 and 'data-draft-input="feedback"' not in str(ringing_page[2]) and ringing_after == ringing_before
    assert takeover[0] == 200 and revoked[0] in {401, 403, 409} and after_old == before_old
    assert desktop[0] == 200 and 'data-draft-input="feedback"' not in str(desktop[2]) and desktop_after == desktop_before
    assert ended[0] == 200 and 'data-draft-input="feedback"' not in str(ended[2])
    assert ended_save[0] in {401, 403, 404, 409} and ended_after == ended_before
