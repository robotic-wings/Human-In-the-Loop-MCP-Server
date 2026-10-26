"""Red HTTPS contracts for an accepted iPhone owner's final plain-text answer."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_mcp_continuation import _continuation


def _headers(cookie: str, csrf: str, port: int) -> dict[str, str]:
    return {
        "Cookie": cookie,
        "X-CSRF-Token": csrf,
        "Origin": f"https://127.0.0.1:{port}",
    }


def _accept(port: int, certificate: Path, call_id: str, cookie: str, csrf: str) -> tuple[int, object]:
    status, _, body = _request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/accept",
        body={"action_id": "iphone-accept"},
        headers=_headers(cookie, csrf, port),
    )
    return status, body


def _submit(
    port: int,
    certificate: Path,
    call_id: str,
    submission_id: str,
    user_input: str,
    headers: dict[str, str],
) -> tuple[int, dict[str, str], object]:
    return _request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/submissions",
        body={"submission_id": submission_id, "user_input": user_input},
        headers=headers,
    )


def test_tls_accepted_iphone_owner_submits_once_replays_and_agent_reads_final_event(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-submit-create")
        call_id = created["call_id"]
        interaction_id = created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        assert _accept(port, certificate, call_id, cookie, csrf)[0] == 200

        first = _submit(
            port, certificate, call_id, "iphone-text-1", "final phone answer", _headers(cookie, csrf, port)
        )
        replay = _submit(
            port, certificate, call_id, "iphone-text-1", "final phone answer", _headers(cookie, csrf, port)
        )
        changed = _submit(
            port, certificate, call_id, "iphone-text-1", "changed phone answer", _headers(cookie, csrf, port)
        )
        busy_released = _create_call(port, certificate, "iphone-submit-next")
        _, continuation_response, _ = _post(
            port,
            certificate,
            _continuation("iphone-final-event", interaction_id, call_id, 1),
            "test-token-alpha",
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            call = connection.execute(
                "SELECT state, owner_device_id, handled_on, terminal_platform FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            events = connection.execute(
                "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
    finally:
        _stop(process)

    assert first[0] == 200
    assert first == replay
    assert changed[0] == 409
    assert isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "answered"
    assert first[2]["user_input"] == "final phone answer"
    assert first[2]["handled_on"] == "iphone_web"
    assert first[2]["terminal_platform"] == "ios"
    assert busy_released["status"] == "ringing"
    assert _structured(continuation_response) == first[2]
    assert call == ("answered", "iphone_web", "iphone_web", "ios")
    assert [event[0] for event in events] == [1, 2]
    assert "final phone answer" in events[-1][1]


def test_tls_unaccepted_iphone_submission_is_rejected_without_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        unaccepted = _create_call(port, certificate, "iphone-submit-unaccepted")
        unaccepted_id = unaccepted["call_id"]
        assert isinstance(unaccepted_id, str)
        cookie, csrf = _login(port, certificate)
        unaccepted_attempt = _submit(
            port, certificate, unaccepted_id, "unaccepted-text", "must not save", _headers(cookie, csrf, port)
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            unaccepted_state = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (unaccepted_id,)
            ).fetchone()[0]
            unaccepted_results = connection.execute(
                "SELECT count(*) FROM submission_operations WHERE call_id = ?", (unaccepted_id,)
            ).fetchone()[0]

    finally:
        _stop(process)

    assert unaccepted_attempt[0] in {400, 401, 403, 409}
    assert unaccepted_state == "ringing"
    assert unaccepted_results == 0


def test_tls_iphone_submission_rejects_competing_and_cross_site_requests_without_mutation(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-submit-security")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        first_cookie, first_csrf = _login(port, certificate)
        second_cookie, second_csrf = _login(port, certificate)
        assert _accept(port, certificate, call_id, first_cookie, first_csrf)[0] == 200
        competing = _submit(
            port, certificate, call_id, "competing", "private answer", _headers(second_cookie, second_csrf, port)
        )
        wrong_session = _submit(
            port,
            certificate,
            call_id,
            "wrong-session",
            "private answer",
            {
                "Cookie": "operator_session=not-an-operator-session",
                "X-CSRF-Token": first_csrf,
                "Origin": f"https://127.0.0.1:{port}",
            },
        )
        missing_csrf = _submit(
            port, certificate, call_id, "missing", "private answer", {"Cookie": first_cookie, "Origin": f"https://127.0.0.1:{port}"}
        )
        foreign_origin = _submit(
            port, certificate, call_id, "foreign", "private answer", {"Cookie": first_cookie, "X-CSRF-Token": first_csrf, "Origin": "https://evil.example"}
        )
        forged_host = _submit(
            port, certificate, call_id, "host", "private answer", {**_headers(first_cookie, first_csrf, port), "Host": "evil.example"}
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
            result_count = connection.execute(
                "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert competing[0] in {401, 403, 404, 409}
    assert wrong_session[0] in {401, 403, 404}
    assert missing_csrf[0] in {400, 403}
    assert foreign_origin[0] in {400, 403}
    assert forged_host[0] in {400, 403}
    assert all(
        "private answer" not in str(response[2])
        for response in (competing, wrong_session, missing_csrf, foreign_origin, forged_host)
    )
    assert state == "accepted"
    assert result_count == 0


def test_tls_expired_iphone_submission_settles_before_persisting_body(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-submit-expired")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        assert _accept(port, certificate, call_id, cookie, csrf)[0] == 200
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute(
                "UPDATE calls SET terminal_disconnect_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        late = _submit(
            port, certificate, call_id, "expired-text", "body must not persist", _headers(cookie, csrf, port)
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute(
                "SELECT state, owner_session_id FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            result_count = connection.execute(
                "SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert late[0] == 409
    assert "body must not persist" not in str(late[2])
    assert state == ("disconnected", "" ) or state == ("disconnected", None)
    assert result_count == 0
