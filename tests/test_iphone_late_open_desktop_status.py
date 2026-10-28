"""Red HTTPS contracts for late iPhone pages of desktop-owned calls.

This is deliberately a browser rendering/read-only contract.  It does not
exercise desktop callbacks or change terminal submission semantics.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _accept as _iphone_accept
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_terminal_control_api import _terminal_request


def _desktop_config(config: Path) -> None:
    credentials = config.parent / "terminal-credentials.json"
    credentials.write_text(
        json.dumps({"desktop-token": {"device_id": "desktop-registered", "terminal_platform": "windows"}}),
        encoding="utf-8",
    )
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["terminal_credentials_file"] = str(credentials)
    raw["eligible_desktop_device_ids"] = ["desktop-registered"]
    config.write_text(json.dumps(raw), encoding="utf-8")


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, version, owner_device_id, owner_session_id, terminal_last_seen_at, "
            "terminal_disconnect_deadline, draft_version, task_version FROM calls WHERE call_id = ?",
            (call_id,),
        ).fetchone()
        assert call is not None
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        submissions = connection.execute(
            "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
        ).fetchone()[0]
    return (*call, events, results, submissions)


def _desktop_owned_call(tmp_path: Path, request_id: str):
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _desktop_config(config)
    process = _start(config)
    _wait_for_listener(process, port)
    created = _create_call(port, certificate, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    accepted = _terminal_request(
        port, certificate, "POST", f"/api/calls/{call_id}/accept", "desktop-token",
        {"action_id": f"accept-{request_id}", "owner_session_id": "desktop-owner"},
    )
    assert accepted[0] == 200 and accepted[1]["handled_on"] == "desktop"
    cookie, csrf = _login(port, certificate)
    return port, certificate, process, call_id, cookie, csrf, tmp_path / "data" / "calls.sqlite3"


def _assert_readonly_desktop_page(source: object, call_id: str) -> None:
    page = str(source)
    assert f"/api/calls/{call_id}/writer" not in page
    assert f"/api/calls/{call_id}/submissions" not in page
    assert "data-writer-status" not in page
    assert "data-attachment-form" not in page
    assert "data-action=\"accept\"" not in page
    assert "data-action=\"decline\"" not in page


def test_tls_late_authenticated_iphone_page_says_desktop_is_processing_and_reads_are_authoritative(tmp_path: Path) -> None:
    port, certificate, process, call_id, cookie, _, database = _desktop_owned_call(tmp_path, "late-desktop-processing")
    try:
        before = _snapshot(database, call_id)
        first = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        reload = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET state = 'in_progress' WHERE call_id = ?", (call_id,))
            connection.commit()
        in_progress_before = _snapshot(database, call_id)
        in_progress_api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        in_progress_reload = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert first[0] == api[0] == reload[0] == in_progress_api[0] == in_progress_reload[0] == 200
    assert isinstance(api[2], dict) and isinstance(api[2].get("call"), dict)
    assert api[2]["call"]["owner_device_id"] == "desktop-registered"
    assert api[2]["call"]["handled_on"] == "desktop"
    assert in_progress_api[2]["call"]["state"] == "in_progress"
    assert in_progress_api[2]["call"]["handled_on"] == "desktop"
    assert isinstance(in_progress_api[2]["call"]["version"], int)
    assert before != in_progress_before and in_progress_before == after
    _assert_readonly_desktop_page(first[2], call_id)
    _assert_readonly_desktop_page(reload[2], call_id)
    assert "desktop is processing" in str(first[2]).lower()


def test_tls_late_iphone_reload_after_desktop_submission_says_desktop_handled_without_premature_success(tmp_path: Path) -> None:
    port, certificate, process, call_id, cookie, _, database = _desktop_owned_call(tmp_path, "late-desktop-handled")
    try:
        submitted = _terminal_request(
            port, certificate, "POST", f"/api/calls/{call_id}/submissions", "desktop-token",
            {"owner_session_id": "desktop-owner", "submission_id": "desktop-final", "user_input": "desktop answer"},
        )
        before = _snapshot(database, call_id)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        reload = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert submitted[0] == 200 and submitted[1]["status"] == "answered"
    assert api[0] == 200 and api[2]["call"]["state"] == "answered"
    assert api[2]["call"]["handled_on"] == "desktop" and isinstance(api[2]["call"]["version"], int)
    assert before == after
    _assert_readonly_desktop_page(page[2], call_id)
    _assert_readonly_desktop_page(reload[2], call_id)
    source = str(page[2]).lower()
    assert "desktop handled" in source or "handled on desktop" in source
    assert "submitted" not in source


def test_tls_desktop_status_page_preserves_iphone_owner_and_denies_untrusted_readers(tmp_path: Path) -> None:
    port, certificate, process, call_id, cookie, _, database = _desktop_owned_call(tmp_path, "late-desktop-denial")
    try:
        other_cookie, _ = _login(port, certificate)
        before = _snapshot(database, call_id)
        anonymous = _request(port, certificate, "GET", f"/calls/{call_id}")
        foreign = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie})
        forged_host = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie, "Host": "forged.invalid"})
        bearer = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer desktop-token"})
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert before == after
    assert anonymous[0] in {200, 401, 403} and "Operator sign in required" in str(anonymous[2])
    assert foreign[0] in {200, 401, 403} and "desktop is processing" not in str(foreign[2]).lower()
    assert forged_host[0] in {400, 401, 403}
    assert bearer[0] in {200, 401, 403} and "desktop is processing" not in str(bearer[2]).lower()


def test_tls_iphone_owner_rendering_remains_writable_and_non_desktop_generic_paths_remain_unchanged(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "late-desktop-iphone-preserved")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        _iphone_accept(port, certificate, call_id, _headers(cookie, csrf, port))
        owner = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute("UPDATE calls SET state = 'cancelled' WHERE call_id = ?", (call_id,))
            connection.commit()
        terminal = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert owner[0] == terminal[0] == 200
    assert f'/api/calls/{call_id}/writer' in str(owner[2])
    assert f'/api/calls/{call_id}/submissions' in str(owner[2])
    assert "Another device is handling this call." not in str(owner[2])
    assert 'data-state="cancelled"' in str(terminal[2])
    assert "Another device is handling this call." not in str(terminal[2])


def test_tls_route_excluded_operator_read_stays_generic_and_cannot_revive_desktop_call(tmp_path: Path) -> None:
    # This configuration deliberately has no iPhone route.  A logged-in
    # operator can make an authenticated read, but it must not gain a desktop
    # status rendering, writer, takeover, or state-changing path.
    port, certificate, process, call_id, cookie, _, database = _desktop_owned_call(tmp_path, "late-desktop-route-excluded")
    try:
        before = _snapshot(database, call_id)
        accepted_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        accepted_api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        submitted = _terminal_request(
            port, certificate, "POST", f"/api/calls/{call_id}/submissions", "desktop-token",
            {"owner_session_id": "desktop-owner", "submission_id": "route-excluded-final", "user_input": "desktop answer"},
        )
        handled_before = _snapshot(database, call_id)
        handled_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        handled_api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        handled_after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert accepted_page[0] == accepted_api[0] == handled_page[0] == handled_api[0] == 200
    assert submitted[0] == 200 and before != handled_before == handled_after
    for page in (accepted_page[2], handled_page[2]):
        source = str(page).lower()
        _assert_readonly_desktop_page(page, call_id)
        assert "desktop is processing" not in source and "desktop handled" not in source
        assert "handled on desktop" not in source
    assert isinstance(accepted_api[2], dict) and isinstance(handled_api[2], dict)
    assert accepted_api[2]["call"]["state"] == "accepted"
    assert handled_api[2]["call"]["state"] == "answered"
