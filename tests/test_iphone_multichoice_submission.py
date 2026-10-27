"""Red HTTPS contracts for accepted iPhone multi-choice answers."""

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


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_user_choice"
    request["params"]["arguments"] = {
        "title": "Pick several",
        "prompt": "<script>pick one or more</script>",
        "choices": CHOICES,
        "allow_multiple": True,
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "multi-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, selected_choices: object, headers: dict[str, str]) -> tuple[int, dict[str, str], object]:
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "selected_choices": selected_choices}, headers=headers)


def _opaque(body: object, call_id: str) -> None:
    if isinstance(body, dict):
        assert not set(body).intersection({"status", "call_state", "reason", "result", "selected_choices", "handled_on", "terminal_platform", "call_id", "owner", "route"})
    rendered = repr(body).lower()
    assert not any(marker in rendered for marker in (call_id.lower(), "status", "call_state", "reason", "result", "selected_choices", "handled_on", "terminal_platform", "terminal", "disconnect", "owner", "route", "answered", "accepted", "ringing"))


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.csrf: str | None = None
        self.endpoint: str | None = None
        self.choices: set[str] = set()

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "meta" and values.get("name") == "X-CSRF-Token":
            self.csrf = values.get("content")
        if tag == "input" and values.get("type") == "checkbox" and values.get("data-choice"):
            self.choices.add(values["data-choice"])
        if values.get("data-endpoint"):
            self.endpoint = values["data-endpoint"]


def _page(html: object, call_id: str) -> tuple[str, str]:
    source = str(html)
    parsed = _Page()
    parsed.feed(source)
    assert parsed.endpoint == f"/api/calls/{call_id}/submissions"
    assert isinstance(parsed.csrf, str) and parsed.csrf
    assert parsed.choices == set(CHOICES)
    assert "<script>pick one or more</script>" not in source
    assert "&lt;script&gt;pick one or more&lt;/script&gt;" in source
    assert "<script>Alpha</script>" not in source
    assert "&lt;script&gt;Alpha&lt;/script&gt;" in source
    assert "crypto.randomUUID()" in source
    assert re.search(
        r"(?s)button\.addEventListener\(\s*['\"]click['\"]\s*,\s*\(\)\s*=>\s*\{.*?"
        r"(?:const|let)\s+selected\s*=.*?checkbox.*?\.checked.*?\.dataset\.choice.*?"
        r"fetch\(\s*button\.dataset\.endpoint\s*,\s*\{.*?credentials\s*:\s*['\"]same-origin['\"]"
        r".*?['\"]X-CSRF-Token['\"]\s*:\s*csrf.*?selected_choices\s*:\s*selected",
        source,
    )
    return parsed.endpoint, parsed.csrf


def test_tls_accepted_iphone_multichoice_canonicalizes_replays_and_delivers_agent_result(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multi-answer"); call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, headers)[0] == 200
        endpoint, rendered_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id)
        rendered_headers = _headers(cookie, rendered_csrf, port)
        first = _submit(port, certificate, call_id, "multi-1", ["Gamma", "<script>Alpha</script>"], rendered_headers)
        replay = _submit(port, certificate, call_id, "multi-1", ["<script>Alpha</script>", "Gamma"], rendered_headers)
        conflict = _submit(port, certificate, call_id, "multi-1", ["Beta", "Gamma"], rendered_headers)
        _, continuation, _ = _post(port, certificate, _continuation("multi-event", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert first[0] == replay[0] == 200 and conflict[0] == 409
    assert first[2] == replay[2] and isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "answered"
    assert first[2]["selected_choices"] == ["<script>Alpha</script>", "Gamma"]
    assert first[2]["handled_on"] == "iphone_web" and first[2]["terminal_platform"] == "ios"
    assert _structured(continuation) == first[2]
    assert [event[0] for event in events] == [1, 2]


def test_tls_iphone_multichoice_rejects_invalid_foreign_and_wrong_form_writers_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multi-security"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port)
        preaccept = _submit(port, certificate, call_id, "pre", ["Beta"], owner); assert _accept(port, certificate, call_id, owner)[0] == 200
        bad = (_submit(port, certificate, call_id, "not-list", {"choice": "Beta"}, owner), _submit(port, certificate, call_id, "non-string", ["Beta", 7], owner), _submit(port, certificate, call_id, "empty", [], owner), _submit(port, certificate, call_id, "dup", ["Beta", "Beta"], owner), _submit(port, certificate, call_id, "foreign", ["outside"], owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "text", "user_input": "Beta"}, headers=owner), _submit(port, certificate, call_id, "other", ["Beta"], _headers(other_cookie, other_csrf, port)), _submit(port, certificate, call_id, "cross", ["Beta"], {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"}))
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert preaccept[0] in {400, 401, 403, 409}
    assert all(response[0] in {400, 401, 403, 404, 409} for response in bad)
    assert state is not None and state[0] == "accepted" and operations == 0 and events == [(1,)]
    _opaque(bad[6][2], call_id)


def test_tls_multichoice_route_exclusion_and_expiry_settlement_are_authoritative(tmp_path: Path) -> None:
    excluded_port = _free_port(); excluded_config, excluded_certificate = _write_operator_config(tmp_path / "excluded", excluded_port)
    excluded_database = tmp_path / "excluded" / "data" / "calls.sqlite3"; excluded_process = _start(excluded_config); _wait_for_listener(excluded_process, excluded_port)
    try:
        excluded = _call(excluded_port, excluded_certificate, "multi-route-excluded"); excluded_call_id = excluded["call_id"]; assert isinstance(excluded_call_id, str)
        excluded_cookie, excluded_csrf = _login(excluded_port, excluded_certificate)
        excluded_submit = _submit(excluded_port, excluded_certificate, excluded_call_id, "route", ["Beta"], _headers(excluded_cookie, excluded_csrf, excluded_port))
        with sqlite3.connect(excluded_database) as connection:
            excluded_state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (excluded_call_id,)).fetchone()
            excluded_operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (excluded_call_id,)).fetchone()[0]
            excluded_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (excluded_call_id,)).fetchall()
    finally:
        _stop(excluded_process)
    assert excluded_submit[0] in {400, 401, 403, 404, 409}
    assert excluded_state == ("ringing",) and excluded_operations == 0 and excluded_events == []

    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multi-expiry"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        late = _submit(port, certificate, call_id, "late", ["Beta", "Gamma"], headers)
        _, terminal_event, _ = _post(port, certificate, _continuation("multi-terminal", interaction_id, call_id, 1), "test-token-alpha")
        next_call = _call(port, certificate, "multi-after-expiry")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert late[0] == 409 and isinstance(late[2], dict)
    assert late[2]["status"] == late[2]["call_state"] == "disconnected" and late[2]["reason"] == "terminal_disconnected"
    assert _structured(terminal_event) == late[2]
    assert state == ("disconnected",) and operations == 0 and events == [(1,), (2,)]
    assert next_call["call_state"] == "ringing"


def test_tls_multichoice_accept_submit_race_allows_only_the_accepted_owner_to_answer(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "multi-race"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as pool:
            accepted = pool.submit(_accept, port, certificate, call_id, headers)
            submitted = pool.submit(_submit, port, certificate, call_id, "race", ["Gamma", "Beta"], headers)
            accept_result, submit_result = accepted.result(), submitted.result()
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert accept_result[0] == 200
    if submit_result[0] == 200:
        assert isinstance(submit_result[2], dict)
        assert submit_result[2]["selected_choices"] == ["Beta", "Gamma"]
        assert state is not None and state[0] == "answered" and operations == 1 and events == [(1,), (2,)]
    else:
        assert submit_result[0] in {400, 401, 403, 404, 409}
        assert state is not None and state[0] == "accepted" and operations == 0 and events == [(1,)]
