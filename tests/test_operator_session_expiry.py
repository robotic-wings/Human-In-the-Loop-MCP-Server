"""Red HTTPS contracts for persisted operator-session absolute expiry."""

from __future__ import annotations

import json
import sqlite3
import socket
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config


def _session_id(cookie: str) -> str:
    name, value = cookie.split("=", 1)
    assert name == "operator_session"
    assert value
    return value


def _expiry_config(tmp_path: Path, port: int, seconds: int | None = 4) -> tuple[Path, Path]:
    config, certificate = _write_operator_config(tmp_path, port)
    raw = json.loads(config.read_text(encoding="utf-8"))
    if seconds is not None:
        raw["operator_session_seconds"] = seconds
    config.write_text(json.dumps(raw), encoding="utf-8")
    return config, certificate


def _cannot_connect(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _session_columns(database: Path) -> set[str]:
    with sqlite3.connect(database) as connection:
        return {row[1] for row in connection.execute("PRAGMA table_info(operator_sessions)")}


def _login_with_headers(port: int, certificate: Path) -> tuple[str, str, dict[str, str]]:
    status, headers, body = _request(
        port, certificate, "POST", "/api/operator/login", body={"password": "correct horse battery staple"}
    )
    assert status == 200
    assert isinstance(body, dict)
    csrf = body["csrf_token"]
    assert isinstance(csrf, str) and csrf
    cookie = headers["set-cookie"].split(";", 1)[0]
    return cookie, csrf, headers


def test_login_persists_configured_absolute_expiry_and_secure_cookie_lifetime(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _expiry_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = datetime.now(UTC)
        cookie, _csrf, headers = _login_with_headers(port, certificate)
        after = datetime.now(UTC)
        database = tmp_path / "data" / "calls.sqlite3"
        assert "expires_at" in _session_columns(database)
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT expires_at FROM operator_sessions WHERE session_id = ?", (_session_id(cookie),)
            ).fetchone()
    finally:
        _stop(process)

    # The cookie lifetime and the durable absolute deadline are deliberately
    # both configured from the same value, rather than relying on browser state.
    assert "Max-Age=4" in headers["set-cookie"]
    assert row is not None
    expires_at = datetime.fromisoformat(row[0])
    assert before + timedelta(seconds=3) <= expires_at <= after + timedelta(seconds=5)


def test_expiry_precedes_cached_csrf_and_survives_restart_without_call_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _expiry_config(tmp_path, port, seconds=60)
    process = _start(config)
    _wait_for_listener(process, port)
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        created = _create_call(port, certificate, "operator-expiry-call")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        expired_cookie, expired_csrf = _login(port, certificate)
        valid_cookie, _valid_csrf = _login(port, certificate)
        assert "expires_at" in _session_columns(database)
        with sqlite3.connect(database) as connection:
            before_call = connection.execute(
                "SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            connection.execute(
                "UPDATE operator_sessions SET expires_at = ? WHERE session_id = ?",
                ((datetime.now(UTC) + timedelta(seconds=60)).isoformat(), _session_id(expired_cookie)),
            )
            connection.commit()

        # A valid absolute deadline survives a service restart; it is not only
        # an in-memory authorization cache.
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        restarted_before_deadline = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": expired_cookie}
        )
        just_before_deadline = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": expired_cookie}
        )
        with sqlite3.connect(database) as connection:
            # Equality is expired: do not allow a timing boundary to extend
            # authority beyond its persisted absolute deadline.
            connection.execute(
                "UPDATE operator_sessions SET expires_at = ? WHERE session_id = ?",
                (datetime.now(UTC).isoformat(), _session_id(expired_cookie)),
            )
            connection.commit()

        # This process has already cached the CSRF token.  It must still
        # consult the durable expiry before accepting either a read or a write.
        stale_read = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": expired_cookie}
        )
        stale_write = _request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            body={"action_id": "expired-session-write"},
            headers={
                "Cookie": expired_cookie,
                "X-CSRF-Token": expired_csrf,
                "Origin": f"https://127.0.0.1:{port}",
            },
        )
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        restarted_read = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": expired_cookie}
        )
        valid_read = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": valid_cookie}
        )
        with sqlite3.connect(database) as connection:
            after_call = connection.execute(
                "SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            expired_row = connection.execute(
                "SELECT session_id FROM operator_sessions WHERE session_id = ?", (_session_id(expired_cookie),)
            ).fetchone()
    finally:
        _stop(process)

    assert restarted_before_deadline[0] == 200
    assert just_before_deadline[0] == 200
    assert stale_read[0] in {401, 403, 404}
    assert stale_write[0] == 403
    assert restarted_read[0] in {401, 403, 404}
    assert valid_read[0] == 200
    assert expired_row is None
    assert before_call == after_call == ("ringing", None, None)


def test_default_session_expiry_is_thirty_days(tmp_path: Path) -> None:
    default_port = _free_port()
    default_config, default_certificate = _expiry_config(tmp_path / "default", default_port, None)
    default_process = _start(default_config)
    _wait_for_listener(default_process, default_port)
    try:
        _cookie, _csrf, headers = _login_with_headers(default_port, default_certificate)
    finally:
        _stop(default_process)
    assert "Max-Age=2592000" in headers["set-cookie"]


def test_invalid_session_expiry_values_fail_before_listening(tmp_path: Path) -> None:
    # An invalid expiry setting is a startup error, not a fallback to either a
    # plaintext listener or the 30-day default.  One loop covers the complete
    # input domain while keeping this a single deterministic Red contract.
    for value in (0, -1, "2592000", None):
        port = _free_port()
        config, _certificate = _expiry_config(tmp_path / f"invalid-{value!s}", port, 4)
        raw = json.loads(config.read_text(encoding="utf-8"))
        raw["operator_session_seconds"] = value
        config.write_text(json.dumps(raw), encoding="utf-8")
        process = _start(config)
        try:
            time.sleep(0.25)
            assert process.poll() is not None, f"invalid value {value!r} opened a listener"
            assert _cannot_connect(port)
        finally:
            if process.poll() is None:
                _stop(process)
            else:
                process.communicate(timeout=3)


def test_legacy_operator_session_row_without_expiry_fails_safe(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _expiry_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "operator-legacy-session")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        database = tmp_path / "data" / "calls.sqlite3"
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO operator_sessions (session_id, csrf) VALUES (?, ?)",
                ("legacy-session-without-expiry", "legacy-csrf"),
            )
            connection.commit()
        legacy_read = _request(
            port,
            certificate,
            "GET",
            f"/api/calls/{call_id}",
            headers={"Cookie": "operator_session=legacy-session-without-expiry"},
        )
        with sqlite3.connect(database) as connection:
            legacy_row = connection.execute(
                "SELECT session_id FROM operator_sessions WHERE session_id = ?",
                ("legacy-session-without-expiry",),
            ).fetchone()
    finally:
        _stop(process)

    assert legacy_read[0] in {401, 403, 404}
    assert legacy_row is None
