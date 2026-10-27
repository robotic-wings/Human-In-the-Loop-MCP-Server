"""Red HTTPS contracts for standalone iPhone feedback (not task feedback)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
import re

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout


TYPE_ID = "task_too_big"
TYPE_LABEL = "The task is too large - break it into smaller steps"


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_feedback_from_user"
    request["params"]["arguments"] = {"request_id": request_id, "client_timeout_seconds": 2}
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "feedback-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, feedback_type: object, feedback_text: object, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "feedback_type": feedback_type, "feedback_text": feedback_text}, headers=headers)


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf = None; self.endpoint = None; self.options: list[dict[str, str]] = []; self.textarea = False; self.send: list[dict[str, str]] = []
    def handle_starttag(self, tag, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if values.get("data-endpoint"): self.endpoint = values["data-endpoint"]
        if tag == "option": self.options.append(values)
        if tag == "textarea": self.textarea = True
        if tag == "button" and values.get("data-feedback-send") == "true": self.send.append(values)


def _page(html: object, call_id: str) -> tuple[str, str]:
    parser = _Page(); source = str(html); parser.feed(source)
    endpoint = f"/api/calls/{call_id}/submissions"
    assert parser.endpoint == endpoint and parser.csrf
    assert parser.textarea and len(parser.send) == 1
    assert parser.send[0].get("data-endpoint") == endpoint
    assert (TYPE_ID, TYPE_LABEL) in [(item.get("value"), item.get("data-label")) for item in parser.options]
    assert "credentials: 'same-origin'" in source and "X-CSRF-Token" in source
    assert re.search(r'''(?sx)
        (?:const|let)\s+(?P<send>[A-Za-z_$][\w$]*)\s*=\s*
        document\.querySelector\(\s*['"]button\[data-feedback-send\]['"]\s*\)
        .*?(?P=send)\.addEventListener\(\s*['"]click['"]
        .*?(?P=send)\.dataset\.endpoint
        .*?(?:document\.querySelector\([^)]*select|feedbackType)
        .*?(?:document\.querySelector\([^)]*textarea|feedbackText)
        .*?JSON\.stringify\(\{[^}]*feedback_type[^}]*feedback_text
        .*?fetch\(\s*(?P=send)\.dataset\.endpoint
        .*?['"]X-CSRF-Token['"]\s*:\s*csrf
        .*?credentials\s*:\s*['"]same-origin['"]
    ''', source)
    return endpoint, parser.csrf


def test_tls_accepted_iphone_feedback_page_persists_canonical_replays_and_delivers_agent_event(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-answer"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection: read_before = connection.execute("SELECT state, owner_device_id, owner_session_id, version FROM calls WHERE call_id = ?", (call_id,)).fetchone(); read_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
        _, page_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id); protected = _headers(cookie, page_csrf, port)
        with sqlite3.connect(database) as connection: assert connection.execute("SELECT state, owner_device_id, owner_session_id, version FROM calls WHERE call_id = ?", (call_id,)).fetchone() == read_before; assert connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall() == read_events
        first = _submit(port, certificate, call_id, "feedback-1", TYPE_ID, "  Please split this task.  ", protected); replay = _submit(port, certificate, call_id, "feedback-1", TYPE_ID, "Please split this task.", protected); conflict = _submit(port, certificate, call_id, "feedback-1", "other", "Please split this task.", protected)
        _, continuation, _ = _post(port, certificate, _continuation("feedback-event", interaction_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert first[0] == replay[0] == 200 and first[2] == replay[2] and conflict[0] == 409
    assert isinstance(first[2], dict) and first[2]["status"] == "answered" and first[2]["feedback_type"] == TYPE_ID and first[2]["feedback_type_label"] == TYPE_LABEL and first[2]["feedback_text"] == "Please split this task."
    assert _structured(continuation) == first[2] and state == ("answered", "iphone_web", "ios") and operations == 1


def test_tls_iphone_feedback_rejects_invalid_foreign_route_and_expiry_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-security"); call_id = created["call_id"]; assert isinstance(call_id, str); owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200
        with sqlite3.connect(database) as connection: before = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
        bad = [_submit(port, certificate, call_id, "unknown", "unknown", "x", owner), _submit(port, certificate, call_id, "blank", TYPE_ID, "   ", owner), _submit(port, certificate, call_id, "type", TYPE_ID, 1, owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "label", "feedback_type": TYPE_ID, "feedback_type_label": "Other", "feedback_text": "x"}, headers=owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "mixed", "feedback_type": TYPE_ID, "feedback_text": "x", "confirmed": False}, headers=owner), _submit(port, certificate, call_id, "foreign", TYPE_ID, "x", _headers(other_cookie, other_csrf, port))]
        with sqlite3.connect(database) as connection: after = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; after_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
    finally: _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in bad) and after == before and operations == 0 and after_events == events == [(1,)]
    opaque = repr(bad[-1][2]).lower(); assert not any(word in opaque for word in (call_id.lower(), "status", "call_state", "reason", "result", "feedback", "owner", "route", "accepted"))


def test_tls_feedback_route_exclusion_and_restart_expiry_release_busy(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        excluded = _call(port, certificate, "feedback-route"); call_id = excluded["call_id"]; assert isinstance(call_id, str); cookie, csrf = _login(port, certificate)
        denied = _submit(port, certificate, call_id, "route", TYPE_ID, "x", _headers(cookie, csrf, port))
    finally: _stop(process)
    assert denied[0] in {400, 401, 403, 404, 409}
    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "expiry", port); _configure_phone_route(config); database = tmp_path / "expiry" / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-expiry"); call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str); cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection: connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        _stop(process); process = _start(config); _wait_for_listener(process, port); late = _submit(port, certificate, call_id, "late", TYPE_ID, "x", headers); _, event, _ = _post(port, certificate, _continuation("feedback-terminal", interaction_id, call_id, 1), "test-token-alpha"); next_call = _call(port, certificate, "feedback-after-expiry")
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert late[0] == 409 and isinstance(late[2], dict) and late[2]["status"] == late[2]["call_state"] == "disconnected" and late[2]["reason"] == "terminal_disconnected"
    assert _structured(event) == late[2] and state == ("disconnected",) and operations == 0 and next_call["call_state"] == "ringing"
