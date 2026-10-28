"""Red HTTPS contracts for accepted iPhone final task reports only."""

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


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "assign_task_to_human"
    request["params"]["arguments"] = {
        "task_title": "<Task & title>",
        "task_description": "<Do the & work>",
        "context_note": "<Context & note>",
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "task-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, status: object, report: object, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "status": status, "report": report}, headers=headers)


def _owner_writer_headers(port: int, certificate: Path, call_id: str, cookie: str, csrf: str) -> dict[str, str]:
    headers = _headers(cookie, csrf, port)
    acquired = _request(port, certificate, "POST", f"/api/calls/{call_id}/writer/acquire", body={"page_id": "task-final", "action_id": f"writer-{call_id}"}, headers=headers)
    assert acquired[0] == 200 and isinstance(acquired[2], dict) and acquired[2].get("writable") is True and isinstance(acquired[2].get("writer_token"), str) and acquired[2]["writer_token"]
    return {**headers, "X-Writer-Token": acquired[2]["writer_token"]}


def _task_continuation(request_id: str, task_id: str, call_id: str, after_event_seq: int) -> dict[str, object]:
    request = _continuation(request_id, task_id, call_id, after_event_seq)
    arguments = request["params"]["arguments"]
    arguments["task_id"] = arguments.pop("interaction_id")
    return request


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf = None; self.textareas = 0; self.actions: list[dict[str, str]] = []
    def handle_starttag(self, tag, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if tag == "textarea": self.textareas += 1
        if tag == "button" and values.get("data-task-final"): self.actions.append(values)


def _page(html: object, call_id: str) -> str:
    source = str(html); parser = _Page(); parser.feed(source); endpoint = f"/api/calls/{call_id}/submissions"
    assert "&lt;Task &amp; title&gt;" in source and "&lt;Do the &amp; work&gt;" in source and "&lt;Context &amp; note&gt;" in source
    assert parser.csrf and parser.textareas == 1 and len(parser.actions) == 2
    assert {item.get("data-task-final") for item in parser.actions} == {"completed", "failed"}
    assert {item.get("data-endpoint") for item in parser.actions} == {endpoint}
    assert "credentials: 'same-origin'" in source and "X-CSRF-Token" in source
    for final_status in ("completed", "failed"):
        assert re.search(fr'''(?sx)
            (?:const|let)\s+(?P<button>[A-Za-z_$][\w$]*)\s*=\s*
            document\.querySelector\(\s*['"]button\[data-task-final=["']{final_status}["']\]['"]\s*\)
            .*?(?P=button)\.addEventListener\(\s*['"]click['"]
            .*?(?:const|let)\s+(?P<status>[A-Za-z_$][\w$]*)\s*=\s*(?P=button)\.dataset\.taskFinal
            .*?(?:document\.querySelector\([^)]*textarea|report)
            .*?JSON\.stringify\(\{{[^}}]*(?P=status)[^}}]*report
            .*?fetch\(\s*(?P=button)\.dataset\.endpoint
            .*?['"]X-CSRF-Token['"]\s*:\s*csrf
            .*?credentials\s*:\s*['"]same-origin['"]
        ''', source)
    return parser.csrf


def test_tls_accepted_iphone_task_completed_persists_replays_and_delivers_agent_event(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-completed"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        with sqlite3.connect(database) as connection: before = connection.execute("SELECT state, owner_device_id, owner_session_id, version FROM calls WHERE call_id = ?", (call_id,)).fetchone(); events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
        page_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id); protected = _owner_writer_headers(port, certificate, call_id, cookie, page_csrf)
        with sqlite3.connect(database) as connection: assert connection.execute("SELECT state, owner_device_id, owner_session_id, version FROM calls WHERE call_id = ?", (call_id,)).fetchone() == before; assert connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall() == events
        first = _submit(port, certificate, call_id, "task-final", "completed", "  Finished safely.  ", protected); replay = _submit(port, certificate, call_id, "task-final", "completed", "Finished safely.", protected); conflict = _submit(port, certificate, call_id, "task-final", "failed", "Finished safely.", protected)
        _, event, _ = _post(port, certificate, _task_continuation("task-event", task_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert first[0] == replay[0] == 200 and first[2] == replay[2] and conflict[0] == 409
    assert isinstance(first[2], dict) and first[2]["task_id"] == task_id and first[2]["interaction_id"] == task_id and first[2]["status"] == first[2]["call_state"] == "completed" and first[2]["report"] == "Finished safely." and first[2]["human_action"] is True and first[2]["handled_on"] == "iphone_web" and first[2]["terminal_platform"] == "ios" and first[2]["resubmittable"] is True and first[2]["result_id"]
    assert _structured(event) == first[2] and state == ("completed", "iphone_web", "ios") and operations == 1


def test_tls_accepted_iphone_task_failed_is_a_distinct_human_terminal_report(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-failed"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200; headers = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        failed = _submit(port, certificate, call_id, "task-failed", "failed", "  Access was denied.  ", headers); _, event, _ = _post(port, certificate, _task_continuation("task-failed-event", task_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert failed[0] == 200 and isinstance(failed[2], dict)
    assert failed[2]["status"] == failed[2]["call_state"] == "failed" and failed[2]["report"] == "Access was denied." and failed[2]["human_action"] is True and failed[2]["resubmittable"] is True and failed[2].get("reason") is None
    assert _structured(event) == failed[2] and state == ("failed", "iphone_web", "ios") and operations == 1


def test_tls_iphone_task_rejects_invalid_foreign_route_and_late_writers_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-security"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200; protected = _owner_writer_headers(port, certificate, call_id, owner_cookie, owner_csrf)
        with sqlite3.connect(database) as connection: before = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
        bad = [_submit(port, certificate, call_id, "no-writer", "completed", "x", owner), _submit(port, certificate, call_id, "bad-status", "other", "x", protected), _submit(port, certificate, call_id, "blank", "completed", "   ", protected), _submit(port, certificate, call_id, "type", "completed", 1, protected), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "wrong-form", "status": "completed", "report": "x", "confirmed": False}, headers=protected), _submit(port, certificate, call_id, "foreign", "completed", "x", _headers(other_cookie, other_csrf, port)), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "cross", "status": "completed", "report": "x"}, headers={**protected, "Origin": "https://evil.example"})]
        with sqlite3.connect(database) as connection: after = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; after_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall(); connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        late = _submit(port, certificate, call_id, "late", "completed", "x", protected); _, terminal, _ = _post(port, certificate, _task_continuation("task-terminal", task_id, call_id, 1), "test-token-alpha"); next_call = _call(port, certificate, "task-after-expiry")
    finally: _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in bad) and after == before and operations == 0 and after_events == events == [(1,)]
    opaque = repr(bad[-2][2]).lower(); assert not any(word in opaque for word in (call_id.lower(), "status", "call_state", "reason", "result", "task", "owner", "route", "accepted"))
    assert late[0] == 409 and isinstance(late[2], dict) and late[2]["status"] == late[2]["call_state"] == "disconnected" and late[2]["reason"] == "terminal_disconnected" and _structured(terminal) == late[2] and next_call["call_state"] == "ringing"
