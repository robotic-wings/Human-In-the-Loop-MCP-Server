"""Red HTTPS contracts for the minimal human-operable iPhone call page."""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _accept, _headers
from test_iphone_web_submission import _submit


def _configure_phone_route(config: Path) -> None:
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["bark_enabled"] = True
    raw["bark_url"] = "http://127.0.0.1:1/browser-page"
    config.write_text(json.dumps(raw), encoding="utf-8")


def _html_get(port: int, certificate: Path, path: str, cookie: str | None = None):
    headers = {"Accept": "text/html"}
    if cookie is not None:
        headers["Cookie"] = cookie
    return _request(port, certificate, "GET", path, headers=headers)


def test_tls_call_page_routes_anonymous_to_content_free_login_and_escapes_authorized_summary(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "browser-page-escaped")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute(
                "UPDATE calls SET message = ? WHERE call_id = ?",
                ("<script>alert('unsafe')</script>", call_id),
            )
        anonymous = _html_get(port, certificate, f"/calls/{call_id}")
        cookie, _ = _login(port, certificate)
        authorized = _html_get(port, certificate, f"/calls/{call_id}", cookie)
    finally:
        _stop(process)

    assert anonymous[0] == 200
    assert anonymous[1]["content-type"].startswith("text/html")
    assert "operator" in str(anonymous[2]).lower()
    assert "unsafe" not in str(anonymous[2])
    assert "private page detail" not in str(anonymous[2])
    assert authorized[0] == 200
    assert authorized[1]["content-type"].startswith("text/html")
    assert "&lt;script&gt;alert" in str(authorized[2])
    assert "<script>alert" not in str(authorized[2])
    assert "private page detail" not in str(authorized[2])
    assert f"/api/calls/{call_id}" in str(authorized[2])
    assert f"/api/calls/{call_id}/accept" in str(authorized[2])
    assert f"/api/calls/{call_id}/decline" in str(authorized[2])
    assert "X-CSRF-Token" in str(authorized[2])
    assert "https://" not in str(authorized[2])
    assert "http://" not in str(authorized[2])


def test_tls_call_page_get_refresh_is_side_effect_free_and_keeps_json_polling(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "browser-page-get")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, _ = _login(port, certificate)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            before = connection.execute(
                "SELECT state, version, terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            event_count_before = connection.execute(
                "SELECT count(*) FROM events WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
        first = _html_get(port, certificate, f"/calls/{call_id}", cookie)
        refreshed = _html_get(port, certificate, f"/calls/{call_id}", cookie)
        polling = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            after = connection.execute(
                "SELECT state, version, terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            event_count_after = connection.execute(
                "SELECT count(*) FROM events WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert first[0] == refreshed[0] == 200
    assert polling[0] == 200
    assert isinstance(polling[2], dict) and polling[2]["call"]["state"] == "ringing"
    assert before == after
    assert event_count_before == event_count_after


def test_tls_call_page_controls_drive_existing_protected_actions(tmp_path: Path) -> None:
    """Exercise the endpoint and CSRF token actually rendered for each button."""
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        cookie, _ = _login(port, certificate)
        declined_call = _create_call(port, certificate, "browser-page-rendered-decline")
        declined_id = declined_call["call_id"]
        assert isinstance(declined_id, str)
        decline_page = _html_get(port, certificate, f"/calls/{declined_id}", cookie)
        decline_html = str(decline_page[2])
        decline_csrf = re.search(r'<meta name="X-CSRF-Token" content="([^"]+)">', decline_html)
        decline_endpoint = re.search(r'<button data-action="decline" data-endpoint="([^"]+)">', decline_html)
        assert decline_csrf is not None and decline_endpoint is not None
        declined = _request(
            port, certificate, "POST", decline_endpoint.group(1),
            headers=_headers(cookie, decline_csrf.group(1), port), body={"action_id": "page-decline"},
        )
        accepted_call = _create_call(port, certificate, "browser-page-rendered-accept")
        accepted_id = accepted_call["call_id"]
        assert isinstance(accepted_id, str)
        accept_page = _html_get(port, certificate, f"/calls/{accepted_id}", cookie)
        accept_html = str(accept_page[2])
        csrf = re.search(r'<meta name="X-CSRF-Token" content="([^"]+)">', accept_html)
        accept_endpoint = re.search(r'<button data-action="accept" data-endpoint="([^"]+)">', accept_html)
        assert csrf is not None and accept_endpoint is not None
        accepted = _request(
            port, certificate, "POST", accept_endpoint.group(1),
            headers=_headers(cookie, csrf.group(1), port), body={"action_id": "page-accept"},
        )
    finally:
        _stop(process)

    assert decline_page[0] == 200
    assert declined[0] == 200 and declined[2]["call_state"] == "declined"
    assert accept_page[0] == 200
    assert "fetch(button.dataset.endpoint" in accept_html
    assert "X-CSRF-Token':csrf" in accept_html
    assert accepted[0] == 200 and accepted[2]["call_state"] == "accepted"


def test_tls_call_page_offers_plain_text_only_to_current_iphone_owner_and_is_display_only_otherwise(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "browser-page-owner")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate)
        other_cookie, _ = _login(port, certificate)
        ringing_page = _html_get(port, certificate, f"/calls/{call_id}", owner_cookie)
        accepted = _accept(
            port, certificate, call_id, "browser-page-accept", _headers(owner_cookie, owner_csrf, port)
        )
        owner_page = _html_get(port, certificate, f"/calls/{call_id}", owner_cookie)
        other_page = _html_get(port, certificate, f"/calls/{call_id}", other_cookie)
        submitted = _submit(
            port, certificate, call_id, "browser-page-submit", "final browser text",
            _headers(owner_cookie, owner_csrf, port),
        )
        ended_page = _html_get(port, certificate, f"/calls/{call_id}", owner_cookie)
    finally:
        _stop(process)

    assert ringing_page[0] == 200
    assert ringing_page[1]["content-type"].startswith("text/html")
    assert f"/api/calls/{call_id}/accept" in str(ringing_page[2])
    assert f"/api/calls/{call_id}/decline" in str(ringing_page[2])
    assert "<textarea" not in str(ringing_page[2])
    assert f"/api/calls/{call_id}/submissions" not in str(ringing_page[2])
    assert accepted[0] == 200
    assert submitted[0] == 200
    assert owner_page[0] == other_page[0] == 200
    assert owner_page[1]["content-type"].startswith("text/html")
    assert other_page[1]["content-type"].startswith("text/html")
    assert ended_page[1]["content-type"].startswith("text/html")
    assert "<textarea" in str(owner_page[2])
    assert f"/api/calls/{call_id}/submissions" in str(owner_page[2])
    assert "X-CSRF-Token" in str(owner_page[2])
    assert "<textarea" not in str(other_page[2])
    assert "already" in str(other_page[2]).lower() or "other" in str(other_page[2]).lower()
    assert "<textarea" not in str(ended_page[2])
    assert "answered" in str(ended_page[2]).lower()
