"""Red HTTPS contracts for accepted iPhone multiline input."""

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


DEFAULT = "<script>draft</script>\nline two"
NORMALIZED = "first line\n\nlast line"


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_multiline_input"
    request["params"]["arguments"] = {"title": "Long answer", "prompt": "<script>write safely</script>", "default_value": DEFAULT, "request_id": request_id, "client_timeout_seconds": 2}
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "multiline-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, user_input: object, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "user_input": user_input}, headers=headers)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf: str | None = None; self.endpoint: str | None = None; self.textarea: dict[str, str] | None = None; self._in_textarea = False; self.textarea_value = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if tag == "textarea" and values.get("data-input-kind") == "multiline": self.textarea = values; self._in_textarea = True
        if values.get("data-endpoint"): self.endpoint = values["data-endpoint"]

    def handle_endtag(self, tag: str) -> None:
        if tag == "textarea": self._in_textarea = False

    def handle_data(self, data: str) -> None:
        if self._in_textarea: self.textarea_value += data


def _page(html: object, call_id: str) -> tuple[str, str]:
    source = str(html); parser = _Page(); parser.feed(source)
    assert parser.endpoint == f"/api/calls/{call_id}/submissions"
    assert isinstance(parser.csrf, str) and parser.csrf
    assert parser.textarea is not None and parser.textarea.get("data-default") == DEFAULT
    assert parser.textarea_value == DEFAULT
    assert "<script>write safely</script>" not in source and "&lt;script&gt;write safely&lt;/script&gt;" in source
    assert "<script>draft</script>" not in source and "&lt;script&gt;draft&lt;/script&gt;" in source
    assert "crypto.randomUUID()" in source
    assert re.search(r"(?s)button\.addEventListener\(\s*['\"]click['\"]\s*,\s*\(\)\s*=>\s*\{.*?textarea\.value.*?fetch\(\s*button\.dataset\.endpoint.*?credentials\s*:\s*['\"]same-origin['\"].*?['\"]X-CSRF-Token['\"]\s*:\s*csrf.*?user_input", source)
    return parser.endpoint, parser.csrf


def _opaque(body: object, call_id: str) -> None:
    rendered = repr(body).lower()
    assert not any(value in rendered for value in (call_id.lower(), "status", "call_state", "reason", "result", "user_input", "handled_on", "terminal_platform", "terminal", "disconnect", "owner", "route", "accepted", "answered"))


def test_tls_accepted_iphone_multiline_trims_replays_and_delivers_agent_result(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multiline-answer"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        endpoint, page_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id); protected = _headers(cookie, page_csrf, port)
        first = _submit(port, certificate, call_id, "multiline-1", "  " + NORMALIZED + "  ", protected); replay = _submit(port, certificate, call_id, "multiline-1", NORMALIZED, protected); conflict = _submit(port, certificate, call_id, "multiline-1", "different", protected)
        _, continuation, _ = _post(port, certificate, _continuation("multiline-event", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert first[0] == replay[0] == 200 and conflict[0] == 409 and first[2] == replay[2]
    assert isinstance(first[2], dict) and first[2]["status"] == first[2]["call_state"] == "answered"
    assert first[2]["user_input"] == NORMALIZED and first[2]["character_count"] == len(NORMALIZED) and first[2]["line_count"] == 3
    assert first[2]["handled_on"] == "iphone_web" and first[2]["terminal_platform"] == "ios" and _structured(continuation) == first[2]
    assert state is not None and state[0] == "answered" and state[3:] == ("iphone_web", "ios")
    assert operations == 1 and events == [(1,), (2,)]


def test_tls_iphone_multiline_rejects_invalid_foreign_and_wrong_form_writers_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multiline-security"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port)
        preaccept = _submit(port, certificate, call_id, "pre", "text", owner); assert _accept(port, certificate, call_id, owner)[0] == 200
        with sqlite3.connect(database) as connection:
            accepted_state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            opened = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
        bad = (_submit(port, certificate, call_id, "empty", " \n ", owner), _submit(port, certificate, call_id, "non-string", ["text"], owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "choice", "selected_choices": ["Beta"]}, headers=owner), _submit(port, certificate, call_id, "other", "text", _headers(other_cookie, other_csrf, port)), _submit(port, certificate, call_id, "cross", "text", {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"}))
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert preaccept[0] in {400, 401, 403, 409} and all(item[0] in {400, 401, 403, 404, 409} for item in bad)
    assert accepted_state is not None and accepted_state[0] == "accepted" and opened == [(1,)]
    assert state == accepted_state and operations == 0 and events == opened
    _opaque(bad[3][2], call_id)


def test_tls_multiline_route_expiry_and_accept_submit_race_are_authoritative(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        excluded = _call(port, certificate, "multiline-route"); excluded_id = excluded["call_id"]; assert isinstance(excluded_id, str); cookie, csrf = _login(port, certificate); route_denied = _submit(port, certificate, excluded_id, "route", "text", _headers(cookie, csrf, port))
        with sqlite3.connect(database) as connection: excluded_state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (excluded_id,)).fetchone(); excluded_ops = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (excluded_id,)).fetchone()[0]
    finally:
        _stop(process)
    assert route_denied[0] in {400, 401, 403, 404, 409} and excluded_state == ("ringing",) and excluded_ops == 0

    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "expiry", port); _configure_phone_route(config); database = tmp_path / "expiry" / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multiline-expiry"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str); cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection: connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        _stop(process); process = _start(config); _wait_for_listener(process, port); late = _submit(port, certificate, call_id, "late", "text", headers); _, event, _ = _post(port, certificate, _continuation("multiline-terminal", interaction_id, call_id, 1), "test-token-alpha"); next_call = _call(port, certificate, "multiline-after-expiry")
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)
    assert late[0] == 409 and isinstance(late[2], dict) and late[2]["status"] == late[2]["call_state"] == "disconnected" and late[2]["reason"] == "terminal_disconnected"
    assert _structured(event) == late[2] and state == ("disconnected",) and operations == 0 and next_call["call_state"] == "ringing"


def test_tls_multiline_accept_submit_race_has_one_authoritative_transition(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multiline-race"); call_id = created["call_id"]; assert isinstance(call_id, str); cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as pool: accepted, submitted = pool.submit(_accept, port, certificate, call_id, headers), pool.submit(_submit, port, certificate, call_id, "race", "text", headers); accept_result, submit_result = accepted.result(), submitted.result()
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert accept_result[0] == 200
    if submit_result[0] == 200: assert state == ("answered",) and operations == 1 and events == [(1,), (2,)]
    else: assert submit_result[0] in {400, 401, 403, 404, 409} and state == ("accepted",) and operations == 0 and events == [(1,)]
