"""HTTPS contracts for an explicit authenticated iPhone decline."""

from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_mcp_continuation import _continuation


def _configure_phone_route(config: Path) -> None:
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["bark_enabled"] = True
    # The receiver is intentionally unreachable: delivery outcome is outside
    # this decline slice, while the persisted Bark route is the phone-routing
    # authority under test.
    raw["bark_url"] = "http://127.0.0.1:1/phone-route"
    config.write_text(json.dumps(raw), encoding="utf-8")


def _headers(cookie: str, csrf: str, port: int) -> dict[str, str]:
    return {
        "Cookie": cookie,
        "X-CSRF-Token": csrf,
        "Origin": f"https://127.0.0.1:{port}",
    }


def _decline(
    port: int, certificate: Path, call_id: str, action_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], object]:
    return _request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/decline",
        body={"action_id": action_id},
        headers=headers,
    )


def _accept(
    port: int, certificate: Path, call_id: str, action_id: str, headers: dict[str, str]
) -> tuple[int, dict[str, str], object]:
    return _request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/accept",
        body={"action_id": action_id},
        headers=headers,
    )


def test_tls_iphone_decline_is_explicit_idempotent_and_agent_readable(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-decline-create")
        call_id = created["call_id"]
        interaction_id = created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        opened_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        read_state = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        first = _decline(port, certificate, call_id, "iphone-decline-1", _headers(cookie, csrf, port))
        replay = _decline(port, certificate, call_id, "iphone-decline-1", _headers(cookie, csrf, port))
        different_action = _decline(port, certificate, call_id, "iphone-decline-2", _headers(cookie, csrf, port))
        _, continuation, _ = _post(
            port,
            certificate,
            _continuation("iphone-decline-event", interaction_id, call_id, 0),
            "test-token-alpha",
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            call = connection.execute(
                "SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform "
                "FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            columns = {item[1] for item in connection.execute("PRAGMA table_info(calls)")}
            action_count = connection.execute(
                "SELECT count(*) FROM action_operations WHERE action_id LIKE ?",
                (f"iphone:%:{call_id}:%",),
            ).fetchone()[0]
            events = connection.execute(
                "SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
    finally:
        _stop(process)

    assert opened_page[0] == read_state[0] == 200
    assert isinstance(read_state[2], dict)
    assert read_state[2]["call"]["state"] == "ringing"
    assert first[0] == replay[0] == 200
    assert first[2] == replay[2]
    assert isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "declined"
    assert first[2]["human_action"] is True
    assert first[2]["handled_on"] is None
    assert first[2]["acted_on"] == "iphone_web"
    assert different_action[0] == 409
    assert _structured(continuation) == first[2]
    assert "acted_on" in columns
    assert call == ("declined", None, None, None, None)
    assert action_count == 1
    assert events == [(1,)]


def test_tls_iphone_decline_requires_same_origin_and_accept_decline_has_one_winner(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-decline-security")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        anonymous = _decline(port, certificate, call_id, "anonymous", {})
        missing_csrf = _decline(
            port, certificate, call_id, "missing", {"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}"}
        )
        foreign_origin = _decline(
            port,
            certificate,
            call_id,
            "foreign",
            {"Cookie": cookie, "X-CSRF-Token": csrf, "Origin": "https://evil.example"},
        )
        forged_host = _decline(
            port,
            certificate,
            call_id,
            "host",
            {**_headers(cookie, csrf, port), "Host": "evil.example"},
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            before_race = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
        with ThreadPoolExecutor(max_workers=2) as executor:
            accept = executor.submit(_accept, port, certificate, call_id, "race-accept", _headers(cookie, csrf, port))
            decline = executor.submit(_decline, port, certificate, call_id, "race-decline", _headers(cookie, csrf, port))
            accept_result, decline_result = accept.result(), decline.result()
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            after_race = connection.execute(
                "SELECT state, handled_on FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            columns = {item[1] for item in connection.execute("PRAGMA table_info(calls)")}
            event_count = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert anonymous[0] in {401, 403, 404}
    assert missing_csrf[0] in {400, 403}
    assert foreign_origin[0] in {400, 403}
    assert forged_host[0] in {400, 403}
    assert before_race == "ringing"
    assert sorted((accept_result[0], decline_result[0])) == [200, 409]
    assert after_race[0] in {"accepted", "declined"}
    assert event_count == 1
    if after_race[0] == "accepted":
        assert after_race[1:] == ("iphone_web",)
    else:
        assert "acted_on" in columns
        assert after_race[1:] == (None,)


def test_tls_late_iphone_decline_settles_expiry_without_revival(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-decline-expired")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            connection.execute(
                "UPDATE calls SET ring_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        late = _decline(port, certificate, call_id, "late-decline", _headers(cookie, csrf, port))
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute(
                "SELECT state, handled_on FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            columns = {item[1] for item in connection.execute("PRAGMA table_info(calls)")}
            events = connection.execute(
                "SELECT result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
    finally:
        _stop(process)

    assert late[0] == 409
    assert isinstance(late[2], dict)
    assert late[2]["status"] == late[2]["call_state"] == "not_accepted"
    assert "acted_on" in columns
    assert state == ("not_accepted", None)
    assert len(events) == 1
    assert "declined" not in events[0][0]


def test_tls_iphone_decline_is_denied_when_persisted_phone_route_is_disabled(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_call(port, certificate, "iphone-decline-no-phone-route")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        denied = _decline(port, certificate, call_id, "no-phone-route", _headers(cookie, csrf, port))
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute(
                "SELECT state, owner_device_id, owner_session_id, handled_on FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            actions = connection.execute(
                "SELECT count(*) FROM action_operations"
            ).fetchone()[0]
            events = connection.execute(
                "SELECT count(*) FROM events WHERE call_id = ?", (call_id,)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert denied[0] in {403, 409}
    assert state == ("ringing", None, None, None)
    assert actions == 0
    assert events == 0
