"""Red HTTPS contract for the rendered operator logout affordance."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _accept as _iphone_accept
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_terminal_control_api import _terminal_request


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, version, owner_device_id, owner_session_id, terminal_last_seen_at "
            "FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        assert call is not None
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
    return (*call, events, results)


def _logout_controls(page: object) -> None:
    source = str(page)
    assert 'data-operator-logout' in source
    assert 'data-endpoint="/api/operator/logout"' in source
    assert "X-CSRF-Token" in source


def _desktop_config(config: Path) -> None:
    credentials = config.parent / "terminal-credentials.json"
    credentials.write_text(json.dumps({"desktop-token": {"device_id": "desktop-registered", "terminal_platform": "windows"}}), encoding="utf-8")
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["terminal_credentials_file"] = str(credentials)
    raw["eligible_desktop_device_ids"] = ["desktop-registered"]
    _configure_phone_route(config)
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["terminal_credentials_file"] = str(credentials)
    raw["eligible_desktop_device_ids"] = ["desktop-registered"]
    config.write_text(json.dumps(raw), encoding="utf-8")


def test_tls_every_authenticated_call_page_renders_relative_csrf_logout_control(tmp_path: Path) -> None:
    pages: list[object] = []
    for kind in ("ringing", "iphone", "desktop"):
        port = _free_port()
        config, certificate = _write_operator_config(tmp_path / kind, port)
        if kind == "desktop":
            _desktop_config(config)
        else:
            _configure_phone_route(config)
        process = _start(config)
        _wait_for_listener(process, port)
        try:
            cookie, csrf = _login(port, certificate)
            call_id = _create_call(port, certificate, f"logout-page-{kind}")["call_id"]
            assert isinstance(call_id, str)
            if kind == "iphone":
                _iphone_accept(port, certificate, call_id, _headers(cookie, csrf, port))
            if kind == "desktop":
                assert _terminal_request(port, certificate, "POST", f"/api/calls/{call_id}/accept", "desktop-token", {"action_id": "desktop-accept", "owner_session_id": "desktop-owner"})[0] == 200
            page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        finally:
            _stop(process)
        assert page[0] == 200
        pages.append(page[2])
    for page in pages:
        _logout_controls(page)


def test_tls_rendered_logout_invalidates_only_caller_and_old_cookie_cannot_mutate_after_restart(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        call_id = _create_call(port, certificate, "logout-rendered-action")["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        other_cookie, _ = _login(port, certificate)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        before = _snapshot(database, call_id)
        logged_out = _request(port, certificate, "POST", "/api/operator/logout", body={}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": f"https://127.0.0.1:{port}"})
        old_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        old_api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        old_actions = [
            _request(port, certificate, "POST", f"/api/calls/{call_id}/{suffix}", body={"action_id": f"old-{suffix}"}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": f"https://127.0.0.1:{port}"})
            for suffix in ("writer/acquire", "heartbeat", "cancel", "accept", "decline")
        ]
        old_draft = _request(port, certificate, "PUT", f"/api/calls/{call_id}/draft", body={"body": "no", "expected_draft_version": 0}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": f"https://127.0.0.1:{port}"})
        old_upload = _request(port, certificate, "POST", f"/api/calls/{call_id}/attachments", body={}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": f"https://127.0.0.1:{port}"})
        old_submit = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "old", "user_input": "no"}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": f"https://127.0.0.1:{port}"})
        other_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie})
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        old_after_restart = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert logged_out[0] == 200 and "Max-Age=0" in logged_out[1]["set-cookie"]
    assert old_page[0] == old_after_restart[0] == 200
    assert old_api[0] in {401, 403, 404}
    assert "Operator sign in required" in str(old_page[2]) and "Operator sign in required" in str(old_after_restart[2])
    # The upload fixture intentionally uses JSON rather than multipart, so a
    # 400 is also a denied, non-mutating outcome after session invalidation.
    assert all(response[0] in {400, 401, 403, 404} for response in (*old_actions, old_draft, old_upload, old_submit))
    assert other_page[0] == 200
    assert before == after


def test_tls_logout_rejects_cross_site_or_read_only_requests_without_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        call_id = _create_call(port, certificate, "logout-negative-boundaries")["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        before = _snapshot(database, call_id)
        cases = (
            _request(port, certificate, "GET", "/api/operator/logout", headers={"Cookie": cookie}),
            _request(port, certificate, "POST", "/api/operator/logout", body={}, headers={"Cookie": cookie}),
            _request(port, certificate, "POST", "/api/operator/logout", body={}, headers={"Cookie": cookie, "X-CSRF-Token": "bad", "Origin": f"https://127.0.0.1:{port}"}),
            _request(port, certificate, "POST", "/api/operator/logout", body={}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": "https://evil.example"}),
            _request(port, certificate, "POST", "/api/operator/logout", body={}, headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": f"https://127.0.0.1:{port}", "Host": "forged.invalid"}),
            _request(port, certificate, "POST", "/api/operator/logout", body={}, headers={"Authorization": "Bearer terminal-token"}),
        )
        after = _snapshot(database, call_id)
        still_readable = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert before == after
    assert all(status in {400, 401, 403, 404, 405} for status, _, _ in cases)
    assert still_readable[0] == 200
