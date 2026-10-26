"""Red TLS contracts for the first authenticated iPhone call page and accept flow."""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import sqlite3
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import (
    _free_port,
    _post,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
)
from test_mcp_wait_leg import _with_timeout


def _scrypt_record(password: str) -> dict[str, object]:
    salt = secrets.token_bytes(16)
    return {
        "algorithm": "scrypt",
        "salt": base64.b64encode(salt).decode("ascii"),
        "n": 16384,
        "r": 8,
        "p": 1,
        "digest": base64.b64encode(
            hashlib.scrypt(password.encode("utf-8"), salt=salt, n=16384, r=8, p=1)
        ).decode("ascii"),
    }


def _write_operator_config(directory: Path, port: int) -> tuple[Path, Path]:
    config, certificate = _write_gateway_config(directory, port)
    operators = directory / "operator-credentials.json"
    operators.write_text(
        json.dumps(
            {
                "operator_id": "operator-default",
                "password_hash": _scrypt_record("correct horse battery staple"),
            }
        ),
        encoding="utf-8",
    )
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["operator_credentials_file"] = str(operators)
    config.write_text(json.dumps(raw), encoding="utf-8")
    return config, certificate


def _request(
    port: int,
    certificate: Path,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    headers: dict[str, str] | None = None,
) -> tuple[int, dict[str, str], object]:
    encoded = b"" if body is None else json.dumps(body).encode("utf-8")
    connection = http.client.HTTPSConnection(
        "127.0.0.1", port, context=ssl.create_default_context(cafile=str(certificate)), timeout=3
    )
    request_headers = {"Accept": "application/json"}
    if body is not None:
        request_headers["Content-Type"] = "application/json"
    if headers:
        request_headers.update(headers)
    connection.request(method, path, body=encoded, headers=request_headers)
    response = connection.getresponse()
    raw = response.read()
    response_headers = {name.lower(): value for name, value in response.getheaders()}
    connection.close()
    try:
        parsed: object = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        parsed = raw.decode("utf-8", errors="replace")
    return response.status, response_headers, parsed


def _login(port: int, certificate: Path) -> tuple[str, str]:
    status, headers, body = _request(
        port,
        certificate,
        "POST",
        "/api/operator/login",
        body={"password": "correct horse battery staple"},
    )
    assert status == 200
    assert isinstance(body, dict)
    csrf_token = body["csrf_token"]
    assert isinstance(csrf_token, str) and csrf_token
    cookie = headers["set-cookie"]
    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert "correct horse battery staple" not in cookie
    return cookie.split(";", 1)[0], csrf_token


def _create_call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["arguments"].update(
        {"title": "Approve change", "prompt": "private page detail"}
    )
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def test_tls_operator_login_is_opaque_and_call_link_is_not_authority(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-login")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        anonymous = _request(port, certificate, "GET", f"/calls/{call_id}")
        unknown = _request(port, certificate, "GET", "/calls/not-a-real-call")
        wrong_login = _request(
            port,
            certificate,
            "POST",
            "/api/operator/login",
            body={"password": "wrong horse battery staple"},
        )
        rejected_read = _request(
            port,
            certificate,
            "GET",
            f"/api/calls/{call_id}",
            headers={"Cookie": wrong_login[1].get("set-cookie", "no-session=1")},
        )
        cookie, _ = _login(port, certificate)
        readable = _request(
            port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie}
        )
    finally:
        _stop(process)

    assert anonymous[0] == unknown[0]
    assert anonymous[2] == unknown[2]
    assert wrong_login[0] in {400, 401, 403}
    assert "set-cookie" not in wrong_login[1]
    assert rejected_read[0] in {401, 403, 404}
    assert readable[0] == 200
    assert "private page detail" not in str(readable[2])


def test_tls_call_read_is_side_effect_free_and_accept_requires_same_origin_csrf(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-csrf")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        summary = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie}
        )
        missing_csrf = _request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            body={"action_id": "missing-csrf"},
            headers={"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}"},
        )
        foreign_origin = _request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            body={"action_id": "foreign-origin"},
            headers={"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        )
        accepted = _request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            body={"action_id": "iphone-accept"},
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": f"https://127.0.0.1:{port}",
            },
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state_before = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
            owner = connection.execute(
                "SELECT owner_device_id, owner_session_id, handled_on, terminal_platform "
                "FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
    finally:
        _stop(process)

    assert summary[0] == 200
    assert isinstance(summary[2], dict)
    assert summary[2]["call"]["state"] == "ringing"
    assert missing_csrf[0] in {400, 403}
    assert foreign_origin[0] in {400, 403}
    assert accepted[0] == 200
    assert state_before == "accepted"
    assert owner[0] == "iphone_web"
    assert owner[1]
    assert owner[2:] == ("iphone_web", "ios")


def test_tls_owner_session_recovers_but_competing_session_cannot_take_over(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-owner")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        first_cookie, first_csrf = _login(port, certificate)
        second_cookie, second_csrf = _login(port, certificate)
        accepted = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept",
            body={"action_id": "first-accept"},
            headers={"Cookie": first_cookie, "X-CSRF-Token": first_csrf, "Origin": f"https://127.0.0.1:{port}"},
        )
        refreshed = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": first_cookie})
        competing = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept",
            body={"action_id": "second-accept"},
            headers={"Cookie": second_cookie, "X-CSRF-Token": second_csrf, "Origin": f"https://127.0.0.1:{port}"},
        )
    finally:
        _stop(process)

    assert accepted[0] == 200
    assert refreshed[0] == 200
    assert "accepted" in str(refreshed[2])
    assert competing[0] in {403, 409}


def test_tls_expired_call_settles_before_web_render_or_accept_and_never_revives(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-expired")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute(
                "UPDATE calls SET ring_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        cookie, csrf = _login(port, certificate)
        rendered = _request(
            port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie}
        )
        late_accept = _request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            body={"action_id": "late-iphone-accept"},
            headers={
                "Cookie": cookie,
                "X-CSRF-Token": csrf,
                "Origin": f"https://127.0.0.1:{port}",
            },
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            final = connection.execute(
                "SELECT state, owner_session_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
    finally:
        _stop(process)

    assert rendered[0] == 200
    assert "not_accepted" in str(rendered[2])
    assert late_accept[0] in {400, 409}
    assert final == ("not_accepted", None)
