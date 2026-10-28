"""Red HTTPS contracts for normal-text and multiline draft-save failure UI."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _draft, _snapshot, _writer_headers
from test_iphone_multiline_submission import _call as _multiline_call
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _accept(port: int, cert: Path, call_id: str, headers: dict[str, str]) -> None:
    status, _, _ = _request(port, cert, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "failure-ui-accept"}, headers=headers)
    assert status == 200


def _create(kind: str, port: int, cert: Path, request_id: str) -> dict[str, object]:
    return _create_call(port, cert, request_id) if kind == "text" else _multiline_call(port, cert, request_id)


def _draft_input(kind: str) -> str:
    return f'data-draft-input="{kind}"'


def _assert_failure_ui(page: object, kind: str) -> None:
    source = str(page)
    assert _draft_input(kind) in source
    assert "addEventListener('input'" in source and "not-synced" in source
    # Bind these checks to the exact Save-draft handler, rather than to an unrelated
    # else branch on the page. The branch therefore represents a failed PUT from
    # this textarea, and its absence of an assignment preserves the local edit.
    handler = re.search(
        r"draftSave\.addEventListener\('click',async\(\)=>\{(?P<body>.*?)\}\);"
        r"document\.querySelectorAll\(",
        source,
        re.DOTALL,
    )
    assert handler is not None
    body = handler.group("body")
    assert "const response=await fetch(draftSave.dataset.endpoint" in body
    failed = re.search(
        r"if\(response\.ok\)\{.*?draftStatus\.dataset\.draftStatus='saved'.*?\}"
        r"else\{(?P<body>.*?)\}",
        body,
        re.DOTALL,
    )
    assert failed is not None
    assert "draftStatus.dataset.draftStatus='not-synced'" in failed.group("body")
    assert any(message in failed.group("body") for message in ("conflict", "not-synced"))
    assert "draftInput.value=" not in failed.group("body")


@pytest.mark.parametrize(("kind", "saved"), (("text", "saved text"), ("multiline", "saved line one\nline two")))
def test_tls_accepted_iphone_text_and_multiline_stale_draft_save_shows_conflict_and_preserves_local_text(
    tmp_path: Path, kind: str, saved: str,
) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path / kind, port); _configure_phone_route(config)
    db = tmp_path / kind / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        created = _create(kind, port, cert, f"{kind}-failure-ui")
        call_id = created["call_id"]; assert isinstance(call_id, str)
        _accept(port, cert, call_id, owner); writer = _writer_headers(port, cert, call_id, owner, f"{kind}-failure-ui")
        winner = _draft(port, cert, call_id, "PUT", body={"body": saved, "expected_draft_version": 0}, headers=writer)
        before = _snapshot(db, call_id)
        stale = _draft(port, cert, call_id, "PUT", body={"body": "local loser", "expected_draft_version": 0}, headers=writer)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert winner[0] == 200 and stale[0] == 409
    assert before == after and after[4] == saved
    assert page[0] == 200
    _assert_failure_ui(page[2], kind)


@pytest.mark.parametrize("kind", ("text", "multiline"))
def test_tls_accepted_iphone_text_and_multiline_draft_success_hydration_reload_restart_and_no_submission(
    tmp_path: Path, kind: str,
) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path / kind, port); _configure_phone_route(config)
    db = tmp_path / kind / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        created = _create(kind, port, cert, f"{kind}-success-ui")
        call_id = created["call_id"]; assert isinstance(call_id, str)
        _accept(port, cert, call_id, owner); writer = _writer_headers(port, cert, call_id, owner, f"{kind}-success-ui")
        before = _snapshot(db, call_id)
        saved = _draft(port, cert, call_id, "PUT", body={"body": "persisted body", "expected_draft_version": 0}, headers=writer)
        first = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        second = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert saved[0] == 200 and isinstance(saved[2], dict) and saved[2].get("draft_version") == 1
    assert before[:3] == after[:3] == ("accepted", before[1], before[2])
    assert after[3:5] == (1, "persisted body") and before[5:] == after[5:]
    assert first[0] == second[0] == 200
    for page in (first[2], second[2]):
        rendered = str(page)
        assert _draft_input(kind) in rendered and "persisted body" in rendered and 'data-draft-status="saved"' in rendered.lower()


def test_tls_text_multiline_draft_failure_boundaries_are_nonmutating_and_nonwritable(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        owner_cookie, owner_csrf = _login(port, cert); other_cookie, other_csrf = _login(port, cert)
        owner = _headers(owner_cookie, owner_csrf, port)
        created = _create("text", port, cert, "text-failure-boundary"); call_id = created["call_id"]; assert isinstance(call_id, str)
        ringing_before = _snapshot(db, call_id); ringing = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); ringing_after = _snapshot(db, call_id)
        _accept(port, cert, call_id, owner); writer = _writer_headers(port, cert, call_id, owner, "text-failure-boundary")
        assert _draft(port, cert, call_id, "PUT", body={"body": "winner", "expected_draft_version": 0}, headers=writer)[0] == 200
        before = _snapshot(db, call_id)
        denied = (
            _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie}),
            _draft(port, cert, call_id, "PUT", body={"body": "x", "expected_draft_version": 1}, headers=_headers(other_cookie, other_csrf, port)),
            _draft(port, cert, call_id, "PUT", body={"body": "x", "expected_draft_version": 1}, headers=owner),
            _draft(port, cert, call_id, "PUT", body={"body": "x", "expected_draft_version": 1}, headers={**writer, "X-CSRF-Token": "bad"}),
            _draft(port, cert, call_id, "PUT", body={"body": "x", "expected_draft_version": 1}, headers={**writer, "Origin": "https://evil.example"}),
            _draft(port, cert, call_id, "PUT", body={"body": "x", "expected_draft_version": 1}, headers={**writer, "Host": "evil.example"}),
            _request(port, cert, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer terminal-secret"}),
        )
        after_denied = _snapshot(db, call_id)
        with sqlite3.connect(db) as connection:
            now = datetime.now(UTC)
            connection.execute("UPDATE calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?", ((now - timedelta(seconds=181)).isoformat(), (now - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        disconnected = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); after = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert ringing_after == ringing_before and 'data-draft-input="text"' not in str(ringing[2])
    assert after_denied == before
    assert all('data-draft-input="text"' not in str(item[2]) and "winner" not in str(item[2]) for item in denied)
    assert disconnected[0] == 200 and 'data-draft-input="text"' not in str(disconnected[2])
    assert after[0] == "disconnected" and after[4] == "winner" and after[5] == before[5] + 1 and after[6:] == before[6:]
