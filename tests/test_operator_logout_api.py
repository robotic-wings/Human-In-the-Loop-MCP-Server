"""Red HTTPS contracts for explicit operator-session logout and invalidation."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config


def _session_id(cookie: str) -> str:
    name, value = cookie.split("=", 1)
    assert name == "operator_session"
    assert value
    return value


def _logout(
    port: int,
    certificate: Path,
    cookie: str,
    csrf: str | None,
    *,
    origin: str | None = None,
    host: str | None = None,
) -> tuple[int, dict[str, str], object]:
    headers = {"Cookie": cookie}
    if csrf is not None:
        headers["X-CSRF-Token"] = csrf
    if origin is not None:
        headers["Origin"] = origin
    if host is not None:
        headers["Host"] = host
    return _request(port, certificate, "POST", "/api/operator/logout", body={}, headers=headers)


def test_tls_logout_requires_authenticated_same_origin_csrf_and_invalidates_only_that_session(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "operator-logout-call")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        old_cookie, old_csrf = _login(port, certificate)
        other_cookie, _other_csrf = _login(port, certificate)

        missing_csrf = _logout(port, certificate, old_cookie, None, origin=f"https://127.0.0.1:{port}")
        foreign_origin = _logout(
            port, certificate, old_cookie, old_csrf, origin="https://evil.example"
        )
        forged_host = _logout(
            port,
            certificate,
            old_cookie,
            old_csrf,
            origin=f"https://127.0.0.1:{port}",
            host="evil.example",
        )

        database = tmp_path / "data" / "calls.sqlite3"
        with sqlite3.connect(database) as connection:
            before_call = connection.execute(
                "SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            before_sessions = connection.execute(
                "SELECT session_id FROM operator_sessions ORDER BY session_id"
            ).fetchall()

        logged_out = _logout(
            port,
            certificate,
            old_cookie,
            old_csrf,
            origin=f"https://127.0.0.1:{port}",
        )
        old_read = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": old_cookie}
        )
        old_write = _request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            body={"action_id": "logged-out-write"},
            headers={
                "Cookie": old_cookie,
                "X-CSRF-Token": old_csrf,
                "Origin": f"https://127.0.0.1:{port}",
            },
        )
        other_read = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": other_cookie}
        )

        with sqlite3.connect(database) as connection:
            after_call = connection.execute(
                "SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            after_sessions = connection.execute(
                "SELECT session_id FROM operator_sessions ORDER BY session_id"
            ).fetchall()
    finally:
        _stop(process)

    assert missing_csrf[0] == 403
    assert foreign_origin[0] == 403
    assert forged_host[0] == 403
    assert before_call == after_call == ("ringing", None, None)
    assert logged_out[0] == 200
    clear_cookie = logged_out[1]["set-cookie"]
    assert "operator_session=" in clear_cookie
    assert "Secure" in clear_cookie and "HttpOnly" in clear_cookie and "SameSite=Strict" in clear_cookie
    assert "Max-Age=0" in clear_cookie or "Expires=" in clear_cookie
    assert old_read[0] in {401, 403, 404}
    assert old_write[0] == 403
    assert other_read[0] == 200
    assert (_session_id(old_cookie),) in before_sessions
    assert (_session_id(other_cookie),) in before_sessions
    assert (_session_id(old_cookie),) not in after_sessions
    assert (_session_id(other_cookie),) in after_sessions
