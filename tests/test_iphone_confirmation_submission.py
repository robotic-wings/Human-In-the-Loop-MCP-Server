"""Red HTTPS contracts for an accepted iPhone confirmation answer."""

from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout


def _confirmation_call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "show_confirmation_dialog"
    request["params"]["arguments"] = {
        "title": "Dangerous action",
        "message": "<script>must be escaped</script>",
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "confirm-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, confirmed: bool, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "confirmed": confirmed}, headers=headers)


@pytest.mark.parametrize(
    ("confirmed", "submission_id"),
    [(False, "confirm-no-1"), (True, "confirm-yes-1")],
)
def test_tls_accepted_iphone_confirmation_is_answered_replayable_and_agent_readable(
    tmp_path: Path, confirmed: bool, submission_id: str
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _confirmation_call(port, certificate, f"iphone-confirm-{confirmed}")
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        ringing_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        assert _accept(port, certificate, call_id, _headers(cookie, csrf, port))[0] == 200
        owner_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        first = _submit(port, certificate, call_id, submission_id, confirmed, _headers(cookie, csrf, port))
        replay = _submit(port, certificate, call_id, submission_id, confirmed, _headers(cookie, csrf, port))
        conflict = _submit(port, certificate, call_id, submission_id, not confirmed, _headers(cookie, csrf, port))
        _, continuation, _ = _post(port, certificate, _continuation(f"confirm-event-{confirmed}", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform, acted_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)

    assert ringing_page[0] == owner_page[0] == 200
    assert "textarea" not in str(ringing_page[2])
    assert "<script>must be escaped</script>" not in str(owner_page[2])
    assert "&lt;script&gt;must be escaped&lt;/script&gt;" in str(owner_page[2])
    assert "confirmed" in str(owner_page[2])
    assert first[0] == replay[0] == 200
    assert first[2] == replay[2]
    assert conflict[0] == 409
    assert isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "answered"
    assert first[2]["human_action"] is True
    assert first[2]["confirmed"] is confirmed
    assert first[2]["handled_on"] == "iphone_web"
    assert first[2]["terminal_platform"] == "ios"
    assert "acted_on" not in first[2]
    assert _structured(continuation) == first[2]
    assert state == ("answered", "iphone_web", "ios", None)
    assert [event[0] for event in events] == [1, 2]
    assert f'"confirmed":{str(confirmed).lower()}' in events[-1][1]
    assert '"declined"' not in events[-1][1]


def test_tls_iphone_confirmation_rejects_nonowner_cross_site_and_text_payloads(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _confirmation_call(port, certificate, "iphone-confirm-security")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate)
        other_cookie, other_csrf = _login(port, certificate)
        before_accept = _submit(port, certificate, call_id, "preaccept", False, _headers(owner_cookie, owner_csrf, port))
        assert _accept(port, certificate, call_id, _headers(owner_cookie, owner_csrf, port))[0] == 200
        foreign = _submit(port, certificate, call_id, "foreign", False, {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"})
        other = _submit(port, certificate, call_id, "other", True, _headers(other_cookie, other_csrf, port))
        text = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "text", "user_input": "not a confirmation"}, headers=_headers(owner_cookie, owner_csrf, port))
        malformed = _submit(port, certificate, call_id, "bad", "false", _headers(owner_cookie, owner_csrf, port))
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
            submissions = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert before_accept[0] in {400, 401, 403, 409}
    assert foreign[0] in {400, 403}
    assert other[0] in {400, 401, 403, 404, 409}
    assert text[0] == malformed[0] == 400
    assert state == "accepted"
    assert submissions == 0


def test_tls_iphone_confirmation_is_denied_when_persisted_phone_route_is_disabled(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _confirmation_call(port, certificate, "iphone-confirm-no-phone-route")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        denied = _submit(port, certificate, call_id, "no-phone-route", False, _headers(cookie, csrf, port))
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute(
                "SELECT state, owner_device_id, owner_session_id, handled_on FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            submissions = connection.execute(
                "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
            events = connection.execute(
                "SELECT count(*) FROM events WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert denied[0] in {400, 403, 409}
    assert state == ("ringing", None, None, None)
    assert submissions == 0
    assert events == 0


def test_tls_expired_accepted_iphone_confirmation_settles_disconnect_before_submission(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _confirmation_call(port, certificate, "iphone-confirm-expiry")
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?",
                (
                    (datetime.now(UTC) - timedelta(seconds=181)).isoformat(),
                    (datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
                    call_id,
                ),
            )
            connection.commit()
        late = _submit(port, certificate, call_id, "expired-confirm", False, headers)
        _, continuation, _ = _post(
            port, certificate, _continuation("expired-confirm-event", interaction_id, call_id, 1), "test-token-alpha"
        )
        next_call = _confirmation_call(port, certificate, "iphone-confirm-after-expiry")
        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT state, owner_device_id, owner_session_id, handled_on FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            submissions = connection.execute(
                "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
            result = connection.execute("SELECT result FROM events WHERE call_id = ? ORDER BY event_seq DESC", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert late[0] == 409
    assert isinstance(late[2], dict)
    assert late[2]["status"] == late[2]["call_state"] == "disconnected"
    assert late[2]["reason"] == "terminal_disconnected"
    assert _structured(continuation) == late[2]
    assert state == ("disconnected", "iphone_web", None, "iphone_web")
    assert submissions == 0
    assert '"confirmed"' not in result
    assert next_call["status"] == "heartbeat"
    assert next_call["call_state"] == "ringing"


def test_tls_iphone_accept_and_typed_confirmation_race_has_one_authoritative_transition(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _confirmation_call(port, certificate, "iphone-confirm-race")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted_future = executor.submit(_accept, port, certificate, call_id, headers)
            submitted_future = executor.submit(_submit, port, certificate, call_id, "race-confirm", False, headers)
            accepted = accepted_future.result()
            submitted = submitted_future.result()
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
            submissions = connection.execute(
                "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert accepted[0] == 200
    assert submitted[0] in {400, 403, 409, 200}
    if submitted[0] == 200:
        assert state == ("answered", "iphone_web", "ios")
        assert submissions == 1
        assert [event[0] for event in events] == [1, 2]
        assert '"confirmed":false' in events[-1][1]
    else:
        assert state == ("accepted", "iphone_web", "ios")
        assert submissions == 0
        assert [event[0] for event in events] == [1]
