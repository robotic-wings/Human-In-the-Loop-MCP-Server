"""Red TLS contracts for accepted iPhone text and multiline browser drafts."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_multiline_submission import _call as _multiline_call
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _accept(port: int, cert: Path, call_id: str, headers: dict[str, str]) -> None:
    status, _, _ = _request(port, cert, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "draft-accept"}, headers=headers)
    assert status == 200


def _draft(port: int, cert: Path, call_id: str, method: str, *, body: dict[str, object] | None = None, headers: dict[str, str] | None = None):
    return _request(port, cert, method, f"/api/calls/{call_id}/draft", body=body, headers=headers)


def _snapshot(db: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(db) as connection:
        call = connection.execute("SELECT state, version, task_version, draft_version, draft_body FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        actions = connection.execute("SELECT count(*) FROM action_operations").fetchone()[0]
    return (*call, events, results, actions)


def test_tls_accepted_iphone_text_and_multiline_drafts_hydrate_and_survive_restart(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        text = _create_call(port, cert, "browser-draft-text")
        text_id = text["call_id"]
        assert isinstance(text_id, str)
        cookie, csrf = _login(port, cert); headers = _headers(cookie, csrf, port)
        _accept(port, cert, text_id, headers)
        # A second accepted call is not permitted by busy-line policy; use its own service instance below.
        first_get = _draft(port, cert, text_id, "GET", headers={"Cookie": cookie})
        saved = _draft(port, cert, text_id, "PUT", body={"body": "text draft", "expected_draft_version": 0}, headers=headers)
        page = _request(port, cert, "GET", f"/calls/{text_id}", headers={"Cookie": cookie})
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        recovered = _draft(port, cert, text_id, "GET", headers={"Cookie": cookie})
    finally:
        _stop(process)
    assert first_get[0] == 200 and isinstance(first_get[2], dict) and first_get[2]["body"] is None
    assert saved[0] == 200 and isinstance(saved[2], dict) and saved[2]["draft_version"] == 1
    assert page[0] == 200 and 'data-draft-input="text"' in str(page[2]) and "text draft" in str(page[2]) and "data-draft-status" in str(page[2])
    assert recovered[0] == 200 and isinstance(recovered[2], dict) and recovered[2]["body"] == "text draft"

    # Multiline is a separate interaction because the first accepted call remains busy.
    port = _free_port(); config, cert = _write_operator_config(tmp_path / "multi", port); _configure_phone_route(config); process = _start(config); _wait_for_listener(process, port)
    try:
        created = _multiline_call(port, cert, "browser-draft-multi")
        call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert); headers = _headers(cookie, csrf, port); _accept(port, cert, call_id, headers)
        saved = _draft(port, cert, call_id, "PUT", body={"body": "line one\nline two", "expected_draft_version": 0}, headers=headers)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)
    assert saved[0] == 200
    assert page[0] == 200 and 'data-draft-input="multiline"' in str(page[2]) and "line one\nline two" in str(page[2]) and "not-synced" in str(page[2]).lower()


def test_tls_iphone_browser_draft_cas_concurrency_limit_and_nonterminal_state(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config); db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _create_call(port, cert, "browser-draft-cas"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert); headers = _headers(cookie, csrf, port); _accept(port, cert, call_id, headers)
        before = _snapshot(db, call_id)
        first = _draft(port, cert, call_id, "PUT", body={"body": "winner", "expected_draft_version": 0}, headers=headers)
        stale = _draft(port, cert, call_id, "PUT", body={"body": "stale", "expected_draft_version": 0}, headers=headers)
        with ThreadPoolExecutor(max_workers=2) as pool:
            raced = [pool.submit(_draft, port, cert, call_id, "PUT", body={"body": item, "expected_draft_version": 1}, headers=headers) for item in ("left", "right")]
            race = [item.result() for item in raced]
        oversized = _draft(port, cert, call_id, "PUT", body={"body": "x" * 257, "expected_draft_version": 2}, headers=headers)
        after = _snapshot(db, call_id)
        _, busy, _ = _post(port, cert, {"jsonrpc": "2.0", "id": "busy", "method": "tools/call", "params": {"name": "get_user_input", "arguments": {"title": "new", "prompt": "new", "request_id": "browser-draft-busy", "client_timeout_seconds": 2}}}, "test-token-alpha")
    finally:
        _stop(process)
    assert first[0] == 200 and stale[0] == 409
    assert sorted(item[0] for item in race) == [200, 409] and oversized[0] in {400, 409, 422}
    assert before[:3] == after[:3] == ("accepted", before[1], before[2])
    assert after[3] == 2 and after[4] in {"left", "right"} and before[5:] == after[5:]
    assert _structured(busy)["status"] == "busy"


def test_tls_iphone_browser_drafts_deny_nonowners_bad_context_route_preaccept_expiry_and_bearer(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config); db = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _create_call(port, cert, "browser-draft-deny"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, cert); other_cookie, other_csrf = _login(port, cert); owner = _headers(owner_cookie, owner_csrf, port)
        preaccept = _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 0}, headers=owner)
        _accept(port, cert, call_id, owner)
        denied = (
            _draft(port, cert, call_id, "GET", headers={"Cookie": other_cookie}),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 0}, headers=_headers(other_cookie, other_csrf, port)),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 0}, headers={"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"}),
            _draft(port, cert, call_id, "PUT", body={"body": "no", "expected_draft_version": 0}, headers={**owner, "Host": "evil.example"}),
            _draft(port, cert, call_id, "GET", headers={"Authorization": "Bearer terminal-secret"}),
        )
        with sqlite3.connect(db) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        expired = _draft(port, cert, call_id, "GET", headers={"Cookie": owner_cookie})
        final = _snapshot(db, call_id)
    finally:
        _stop(process)
    assert preaccept[0] in {400, 401, 403, 404, 409}
    assert all(item[0] in {400, 401, 403, 404, 409} for item in denied)
    assert all("draft_body" not in repr(item[2]) for item in denied)
    assert expired[0] in {401, 403, 404, 409} and final[0] == "disconnected" and final[3:5] == (0, None)
