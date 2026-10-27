"""Red HTTPS contracts for accepted iPhone integer and float input."""

from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

import pytest

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout


def _numeric_call(
    port: int, certificate: Path, request_id: str, input_type: str, default_value: str | None = " 7 "
) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_user_input"
    request["params"]["arguments"] = {
        "title": "Numeric value",
        "prompt": "<script>enter a numeric value</script>",
        "input_type": input_type,
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    if default_value is not None:
        request["params"]["arguments"]["default_value"] = default_value
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "numeric-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, user_input: object, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "user_input": user_input}, headers=headers)


class _NumericPageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.endpoint: str | None = None
        self.csrf: str | None = None
        self.input_type: str | None = None
        self.default: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "X-CSRF-Token":
            self.csrf = values.get("content")
        if tag == "input" and values.get("data-input-type") in {"integer", "float"}:
            self.input_type = values.get("data-input-type")
            self.default = values.get("value")
        if values.get("data-endpoint"):
            self.endpoint = values["data-endpoint"]


def _parsed_numeric_page(
    html: object, call_id: str, input_type: str, expected_default: str = " 7 "
) -> tuple[str, str]:
    source = str(html)
    parser = _NumericPageParser()
    parser.feed(source)
    assert parser.endpoint == f"/api/calls/{call_id}/submissions"
    assert isinstance(parser.csrf, str) and parser.csrf
    assert parser.input_type == input_type
    assert parser.default == expected_default
    assert "<script>enter a numeric value</script>" not in source
    assert "&lt;script&gt;enter a numeric value&lt;/script&gt;" in source
    assert "crypto.randomUUID()" in source
    assert re.search(
        r"(?s)button\.addEventListener\(\s*['\"]click['\"]\s*,\s*\(\)\s*=>\s*\{"
        r".*?(?:const|let)\s+user_input\s*=\s*input\.value\s*;"
        r".*?fetch\(\s*button\.dataset\.endpoint\s*,\s*\{"
        r".*?credentials\s*:\s*['\"]same-origin['\"]"
        r".*?['\"]X-CSRF-Token['\"]\s*:\s*csrf"
        r".*?body\s*:\s*JSON\.stringify\(\{[^}]*user_input",
        source,
    )
    return parser.endpoint, parser.csrf


@pytest.mark.parametrize(
    ("input_type", "raw", "typed", "submission_id"),
    [("integer", " 42 ", 42, "integer-42"), ("float", " 3.50 ", 3.5, "float-3-5")],
)
def test_tls_accepted_iphone_numeric_input_is_typed_replayable_and_agent_readable(
    tmp_path: Path, input_type: str, raw: str, typed: int | float, submission_id: str
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, certificate, f"numeric-{input_type}", input_type)
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        ringing_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        assert _accept(port, certificate, call_id, headers)[0] == 200
        owner_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        endpoint, rendered_csrf = _parsed_numeric_page(owner_page[2], call_id, input_type)
        rendered_headers = _headers(cookie, rendered_csrf, port)
        first = _request(port, certificate, "POST", endpoint, body={"submission_id": submission_id, "user_input": raw}, headers=rendered_headers)
        replay = _request(port, certificate, "POST", endpoint, body={"submission_id": submission_id, "user_input": str(typed)}, headers=rendered_headers)
        conflict = _request(port, certificate, "POST", endpoint, body={"submission_id": submission_id, "user_input": "99"}, headers=rendered_headers)
        _, continuation, _ = _post(port, certificate, _continuation(f"numeric-event-{input_type}", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform, acted_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)

    assert ringing_page[0] == owner_page[0] == 200
    assert "textarea" not in str(ringing_page[2])
    assert first[0] == replay[0] == 200
    assert first[2] == replay[2]
    assert conflict[0] == 409
    assert isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "answered"
    assert first[2]["human_action"] is True
    assert first[2]["input_type"] == input_type
    assert first[2]["user_input"] == typed
    assert first[2]["handled_on"] == "iphone_web"
    assert first[2]["terminal_platform"] == "ios"
    assert "acted_on" not in first[2]
    assert _structured(continuation) == first[2]
    assert state == ("answered", "iphone_web", "ios", None)
    assert [event[0] for event in events] == [1, 2]
    assert f'"user_input":{typed}' in events[-1][1]


@pytest.mark.parametrize(
    ("input_type", "raw", "typed", "submission_id"),
    [("integer", " 8 ", 8, "integer-no-default"), ("float", " 2.25 ", 2.25, "float-no-default")],
)
def test_tls_accepted_iphone_numeric_input_without_default_renders_empty_control_and_submits_typed_result(
    tmp_path: Path, input_type: str, raw: str, typed: int | float, submission_id: str
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, certificate, f"numeric-no-default-{input_type}", input_type, default_value=None)
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        owner_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        endpoint, rendered_csrf = _parsed_numeric_page(owner_page[2], call_id, input_type, expected_default="")
        submitted = _request(
            port,
            certificate,
            "POST",
            endpoint,
            body={"submission_id": submission_id, "user_input": raw},
            headers=_headers(cookie, rendered_csrf, port),
        )
        _, continuation, _ = _post(
            port, certificate, _continuation(f"numeric-no-default-event-{input_type}", interaction_id, call_id, 1), "test-token-alpha"
        )
    finally:
        _stop(process)

    assert owner_page[0] == submitted[0] == 200
    assert isinstance(submitted[2], dict)
    assert submitted[2]["status"] == submitted[2]["call_state"] == "answered"
    assert submitted[2]["input_type"] == input_type
    assert submitted[2]["user_input"] == typed
    assert submitted[2]["handled_on"] == "iphone_web"
    assert submitted[2]["terminal_platform"] == "ios"
    assert _structured(continuation) == submitted[2]


def test_tls_iphone_numeric_input_rejects_invalid_types_and_untrusted_writers_without_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, certificate, "numeric-security", "integer")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate)
        other_cookie, other_csrf = _login(port, certificate)
        headers = _headers(owner_cookie, owner_csrf, port)
        preaccept = _submit(port, certificate, call_id, "preaccept", "42", headers)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection:
            accepted_state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            opened = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
        blank = _submit(port, certificate, call_id, "blank", "   ", headers)
        unparseable = _submit(port, certificate, call_id, "bad", "forty two", headers)
        foreign_payload = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "confirm", "confirmed": False}, headers=headers)
        choice_payload = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "choice", "selected_choices": ["Beta"]}, headers=headers)
        foreign = _submit(port, certificate, call_id, "foreign", "42", {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"})
        other = _submit(port, certificate, call_id, "other", "42", _headers(other_cookie, other_csrf, port))
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)

    assert preaccept[0] in {400, 401, 403, 409}
    assert blank[0] == unparseable[0] == foreign_payload[0] == choice_payload[0] == 400
    assert foreign[0] in {400, 403}
    assert other[0] in {400, 401, 403, 404, 409}
    assert accepted_state is not None and accepted_state[0] == "accepted"
    assert opened and [event[0] for event in opened] == [1]
    assert state == accepted_state
    assert operations == 0
    assert events == opened
    assert '"user_input"' not in events[0][1]


def test_tls_iphone_numeric_input_route_excluded_does_not_mutate(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        excluded = _numeric_call(port, certificate, "numeric-route-excluded", "float")
        excluded_id = excluded["call_id"]
        assert isinstance(excluded_id, str)
        cookie, csrf = _login(port, certificate)
        denied = _submit(port, certificate, excluded_id, "no-route", "3.5", _headers(cookie, csrf, port))
        with sqlite3.connect(database) as connection:
            excluded_state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (excluded_id,)).fetchone()[0]
            excluded_operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (excluded_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert denied[0] in {400, 403, 409}
    assert excluded_state == "ringing"
    assert excluded_operations == 0


def test_tls_expired_accepted_iphone_numeric_input_settles_disconnect_before_submission(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, certificate, "numeric-expiry", "float")
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id))
            connection.commit()

        # Restarting after the persisted deadline makes startup recovery settle the
        # call before the original owner's protected submission reaches the route.
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        with sqlite3.connect(database) as connection:
            settled = connection.execute("SELECT state, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        late = _submit(port, certificate, call_id, "expired-numeric", " 3.5 ", headers)
        other_cookie, other_csrf = _login(port, certificate)
        foreign = _submit(port, certificate, call_id, "foreign-expired", " 3.5 ", _headers(other_cookie, other_csrf, port))
        _, continuation, _ = _post(port, certificate, _continuation("numeric-expired-event", interaction_id, call_id, 1), "test-token-alpha")
        next_call = _numeric_call(port, certificate, "numeric-after-expiry", "integer")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            result = connection.execute("SELECT result FROM events WHERE call_id = ? ORDER BY event_seq DESC", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert late[0] == 409
    assert isinstance(late[2], dict)
    assert late[2]["status"] == late[2]["call_state"] == "disconnected"
    assert late[2]["reason"] == "terminal_disconnected"
    assert _structured(continuation) == late[2]
    assert settled == ("disconnected", None)
    assert foreign[0] in {400, 401, 403, 404, 409}
    assert state == ("disconnected", "iphone_web", None, "iphone_web")
    assert operations == 0
    assert '"user_input"' not in result
    assert next_call["status"] == "heartbeat"
    assert next_call["call_state"] == "ringing"


def test_tls_iphone_accept_and_numeric_submit_race_has_one_authoritative_transition(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, certificate, "numeric-race", "integer")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted_future = executor.submit(_accept, port, certificate, call_id, headers)
            submitted_future = executor.submit(_submit, port, certificate, call_id, "race-numeric", " 42 ", headers)
            accepted, submitted = accepted_future.result(), submitted_future.result()
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert accepted[0] == 200
    assert submitted[0] in {400, 403, 409, 200}
    if submitted[0] == 200:
        assert state == ("answered", "iphone_web", "ios")
        assert operations == 1
        assert [event[0] for event in events] == [1, 2]
        assert '"user_input":42' in events[-1][1]
    else:
        assert state == ("accepted", "iphone_web", "ios")
        assert operations == 0
        assert [event[0] for event in events] == [1]
