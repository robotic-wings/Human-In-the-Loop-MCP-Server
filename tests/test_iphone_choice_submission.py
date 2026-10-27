"""Red HTTPS contracts for an accepted iPhone single-choice answer."""

from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout


CHOICES = ["<script>Alpha</script>", "Beta", "Gamma"]


def _choice_call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_user_choice"
    request["params"]["arguments"] = {
        "title": "Pick one",
        "prompt": "<script>pick exactly one</script>",
        "choices": CHOICES,
        "allow_multiple": False,
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "choice-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, choices: object, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "selected_choices": choices}, headers=headers)


class _ChoicePageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.endpoint: str | None = None
        self.csrf: str | None = None
        self.choices: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "X-CSRF-Token":
            self.csrf = values.get("content")
        if tag == "input" and values.get("type") == "radio" and values.get("data-choice"):
            self.choices.add(values["data-choice"])
        if values.get("data-endpoint"):
            self.endpoint = values["data-endpoint"]


def _parsed_choice_page(html: object, call_id: str) -> tuple[str, str, set[str]]:
    source = str(html)
    parser = _ChoicePageParser()
    parser.feed(source)
    assert parser.endpoint == f"/api/calls/{call_id}/submissions"
    assert isinstance(parser.csrf, str) and parser.csrf
    assert parser.choices == set(CHOICES)
    assert "<script>pick exactly one</script>" not in source
    assert "&lt;script&gt;pick exactly one&lt;/script&gt;" in source
    assert "<script>Alpha</script>" not in source
    assert "&lt;script&gt;Alpha&lt;/script&gt;" in source
    assert "fetch(" in source
    assert "credentials:'same-origin'" in source
    assert "selected_choices" in source
    assert "crypto.randomUUID()" in source
    assert re.search(r"(?:const|let)\s+selected\s*=\s*[^;]+\.dataset\.choice", source)
    assert re.search(r"JSON\.stringify\(\{[^}]*selected_choices", source)
    assert "button.dataset.endpoint" in source
    return parser.endpoint, parser.csrf, parser.choices


def test_tls_accepted_iphone_single_choice_is_answered_replayable_and_agent_readable(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _choice_call(port, certificate, "iphone-choice-answer")
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        ringing_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        polling = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(database) as connection:
            before = connection.execute("SELECT state, version, terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            before_events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        assert _accept(port, certificate, call_id, headers)[0] == 200
        owner_page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        endpoint, rendered_csrf, rendered_choices = _parsed_choice_page(owner_page[2], call_id)
        assert rendered_choices == set(CHOICES)
        selected = "Beta"
        rendered_headers = _headers(cookie, rendered_csrf, port)
        first = _request(port, certificate, "POST", endpoint, body={"submission_id": "choice-beta-1", "selected_choices": [selected]}, headers=rendered_headers)
        replay = _request(port, certificate, "POST", endpoint, body={"submission_id": "choice-beta-1", "selected_choices": [selected]}, headers=rendered_headers)
        conflict = _request(port, certificate, "POST", endpoint, body={"submission_id": "choice-beta-1", "selected_choices": ["Gamma"]}, headers=rendered_headers)
        _, continuation, _ = _post(port, certificate, _continuation("choice-event", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            after = connection.execute("SELECT state, version, terminal_last_seen_at, handled_on, terminal_platform, acted_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)

    assert ringing_page[0] == polling[0] == owner_page[0] == 200
    assert "textarea" not in str(ringing_page[2])
    assert "selected_choices" not in str(ringing_page[2])
    assert before[:2] == ("ringing", 1)
    assert before_events == 0
    assert first[0] == replay[0] == 200
    assert first[2] == replay[2]
    assert conflict[0] == 409
    assert isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "answered"
    assert first[2]["human_action"] is True
    assert first[2]["selected_choices"] == [selected]
    assert first[2]["handled_on"] == "iphone_web"
    assert first[2]["terminal_platform"] == "ios"
    assert "acted_on" not in first[2]
    assert _structured(continuation) == first[2]
    assert after[0] == "answered"
    assert after[3:] == ("iphone_web", "ios", None)
    assert [event[0] for event in events] == [1, 2]
    assert '"selected_choices":["Beta"]' in events[-1][1]


def test_tls_iphone_single_choice_rejects_wrong_payloads_and_untrusted_writers_without_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _choice_call(port, certificate, "iphone-choice-security")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate)
        other_cookie, other_csrf = _login(port, certificate)
        owner_headers = _headers(owner_cookie, owner_csrf, port)
        preaccept = _submit(port, certificate, call_id, "preaccept", ["Beta"], owner_headers)
        assert _accept(port, certificate, call_id, owner_headers)[0] == 200
        foreign = _submit(port, certificate, call_id, "foreign", ["Beta"], {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"})
        other = _submit(port, certificate, call_id, "other", ["Beta"], _headers(other_cookie, other_csrf, port))
        nonmember = _submit(port, certificate, call_id, "nonmember", ["outside"], owner_headers)
        multiple = _submit(port, certificate, call_id, "multiple", ["Beta", "Gamma"], owner_headers)
        text = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "text", "user_input": "not a choice"}, headers=owner_headers)
        confirmation = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "confirmation", "confirmed": False}, headers=owner_headers)
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
            submissions = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert preaccept[0] in {400, 401, 403, 409}
    assert foreign[0] in {400, 403}
    assert other[0] in {400, 401, 403, 404, 409}
    assert nonmember[0] == multiple[0] == text[0] == confirmation[0] == 400
    assert state == "accepted"
    assert submissions == events == 0


def test_tls_iphone_single_choice_is_denied_when_phone_route_is_disabled(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _choice_call(port, certificate, "iphone-choice-no-phone-route")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        denied = _submit(port, certificate, call_id, "no-phone-route", ["Beta"], _headers(cookie, csrf, port))
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            submissions = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert denied[0] in {400, 403, 409}
    assert state == ("ringing", None, None, None)
    assert submissions == events == 0


def test_tls_expired_accepted_iphone_single_choice_settles_disconnect_before_submission(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _choice_call(port, certificate, "iphone-choice-expiry")
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id))
            connection.commit()
        late = _submit(port, certificate, call_id, "expired-choice", ["Beta"], headers)
        _, continuation, _ = _post(port, certificate, _continuation("expired-choice-event", interaction_id, call_id, 1), "test-token-alpha")
        next_call = _choice_call(port, certificate, "iphone-choice-after-expiry")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            submissions = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
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
    assert '"selected_choices"' not in result
    assert next_call["status"] == "heartbeat"
    assert next_call["call_state"] == "ringing"


def test_tls_iphone_accept_and_single_choice_submission_race_has_one_authoritative_transition(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _choice_call(port, certificate, "iphone-choice-race")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted_future = executor.submit(_accept, port, certificate, call_id, headers)
            submitted_future = executor.submit(_submit, port, certificate, call_id, "race-choice", ["Beta"], headers)
            accepted = accepted_future.result()
            submitted = submitted_future.result()
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
            submissions = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)

    assert accepted[0] == 200
    assert submitted[0] in {400, 403, 409, 200}
    if submitted[0] == 200:
        assert state == ("answered", "iphone_web", "ios")
        assert submissions == 1
        assert [event[0] for event in events] == [1, 2]
        assert '"selected_choices":["Beta"]' in events[-1][1]
    else:
        assert state == ("accepted", "iphone_web", "ios")
        assert submissions == 0
        assert [event[0] for event in events] == [1]
