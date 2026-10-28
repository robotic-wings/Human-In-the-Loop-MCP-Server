"""Red HTTPS contracts for accepted iPhone information acknowledgement."""

from __future__ import annotations

import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_browser_drafts import _writer_headers
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "show_info_message"
    request["params"]["arguments"] = {
        "title": "Information", "message": "<script>safe summary</script>",
        "request_id": request_id, "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "info-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, acknowledged: object, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "acknowledged": acknowledged}, headers=headers)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf: str | None = None; self.endpoint: str | None = None; self.ok: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if tag == "button" and values.get("data-acknowledged") == "true": self.endpoint = values.get("data-endpoint")
        if tag == "button" and values.get("data-acknowledged") == "true": self.ok.append(values)


def _page(html: object, call_id: str) -> tuple[str, str]:
    source = str(html); parser = _Page(); parser.feed(source)
    assert parser.endpoint == f"/api/calls/{call_id}/submissions"
    assert isinstance(parser.csrf, str) and parser.csrf
    assert len(parser.ok) == 1
    assert parser.ok[0].get("data-endpoint") == parser.endpoint
    assert "textarea" not in source and "data-confirmed" not in source
    assert "<script>safe summary</script>" not in source and "&lt;script&gt;safe summary&lt;/script&gt;" in source
    assert "crypto.randomUUID()" in source
    assert re.search(r"(?s)button\.addEventListener\(\s*['\"]click['\"]\s*,\s*\(\)\s*=>\s*\{.*?(?:const|let)\s+acknowledged\s*=\s*button\.dataset\.acknowledged\s*===\s*['\"]true['\"].*?JSON\.stringify\(\{[^}]*acknowledged.*?fetch\(\s*button\.dataset\.endpoint.*?credentials\s*:\s*['\"]same-origin['\"].*?['\"]X-CSRF-Token['\"]\s*:\s*csrf", source)
    return parser.endpoint, parser.csrf


def _opaque(body: object, call_id: str) -> None:
    rendered = repr(body).lower()
    assert not any(value in rendered for value in (call_id.lower(), "status", "call_state", "reason", "result", "acknowledged", "handled_on", "terminal_platform", "terminal", "disconnect", "owner", "route", "accepted", "answered"))


def test_tls_accepted_iphone_info_acknowledges_replays_and_delivers_agent_event(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-answer"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        endpoint, page_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id); protected = _writer_headers(port, certificate, call_id, _headers(cookie, page_csrf, port), "info-answer")
        first = _submit(port, certificate, call_id, "info-1", True, protected); replay = _submit(port, certificate, call_id, "info-1", True, protected); conflict = _submit(port, certificate, call_id, "info-1", False, protected)
        _, continuation, _ = _post(port, certificate, _continuation("info-event", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform, acted_on FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally: _stop(process)
    assert first[0] == replay[0] == 200 and conflict[0] == 409 and first[2] == replay[2]
    assert isinstance(first[2], dict) and first[2]["status"] == first[2]["call_state"] == "answered" and first[2]["acknowledged"] is True
    assert first[2]["handled_on"] == "iphone_web" and first[2]["terminal_platform"] == "ios" and "acted_on" not in first[2]
    assert _structured(continuation) == first[2] and state == ("answered", "iphone_web", "ios", None) and operations == 1 and [event[0] for event in events] == [1, 2]
    assert '"acknowledged":true' in events[-1][1] and '"declined"' not in events[-1][1]


def test_tls_iphone_info_rejects_invalid_foreign_and_readonly_writers_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-security"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); preaccept = _submit(port, certificate, call_id, "pre", True, owner); assert _accept(port, certificate, call_id, owner)[0] == 200
        with sqlite3.connect(database) as connection: before = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone(); opened = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
        bad = (_submit(port, certificate, call_id, "false", False, owner), _submit(port, certificate, call_id, "malformed", "true", owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "wrong", "user_input": "text"}, headers=owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "mixed", "acknowledged": True, "confirmed": False}, headers=owner), _submit(port, certificate, call_id, "foreign", True, _headers(other_cookie, other_csrf, port)), _submit(port, certificate, call_id, "cross", True, {"Cookie": owner_cookie, "X-CSRF-Token": owner_csrf, "Origin": "https://evil.example"}))
        with sqlite3.connect(database) as connection: after = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally: _stop(process)
    assert page[0] == 200 and preaccept[0] in {400, 401, 403, 409} and all(item[0] in {400, 401, 403, 404, 409} for item in bad)
    assert before is not None and before[0] == "accepted" and after == before and operations == 0 and events == opened == [(1,)]
    _opaque(bad[4][2], call_id)


def test_tls_info_route_expiry_and_accept_submit_race_are_authoritative(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        excluded = _call(port, certificate, "info-route"); excluded_id = excluded["call_id"]; assert isinstance(excluded_id, str); cookie, csrf = _login(port, certificate); denied = _submit(port, certificate, excluded_id, "route", True, _headers(cookie, csrf, port));
        with sqlite3.connect(database) as connection: route_state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (excluded_id,)).fetchone(); route_ops = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (excluded_id,)).fetchone()[0]
    finally: _stop(process)
    assert denied[0] in {400, 401, 403, 404, 409} and route_state == ("ringing",) and route_ops == 0
    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "expiry", port); _configure_phone_route(config); database = tmp_path / "expiry" / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-expiry"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str); cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200; protected = _writer_headers(port, certificate, call_id, headers, "info-expiry")
        with sqlite3.connect(database) as connection: connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        _stop(process); process = _start(config); _wait_for_listener(process, port); late = _submit(port, certificate, call_id, "late", True, protected); _, event, _ = _post(port, certificate, _continuation("info-terminal", interaction_id, call_id, 1), "test-token-alpha"); next_call = _call(port, certificate, "info-after-expiry")
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert late[0] == 409 and isinstance(late[2], dict) and late[2]["status"] == late[2]["call_state"] == "disconnected" and late[2]["reason"] == "terminal_disconnected"
    assert _structured(event) == late[2] and state == ("disconnected",) and operations == 0 and next_call["call_state"] == "ringing"


def test_tls_info_accept_submit_race_has_one_authoritative_transition(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-race"); call_id = created["call_id"]; assert isinstance(call_id, str); cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as pool: accepted, submitted = pool.submit(_accept, port, certificate, call_id, headers), pool.submit(_submit, port, certificate, call_id, "race", True, headers); accept_result, submit_result = accepted.result(), submitted.result()
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally: _stop(process)
    assert accept_result[0] == 200
    if submit_result[0] == 200: assert state == ("answered",) and operations == 1 and events == [(1,), (2,)]
    else: assert submit_result[0] in {400, 401, 403, 404, 409} and state == ("accepted",) and operations == 0 and events == [(1,)]
