"""Red HTTPS contracts for explicit accepted-iPhone task cancellation only."""

from __future__ import annotations

import sqlite3
import json
from concurrent.futures import ThreadPoolExecutor
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
        "task_title": "<Cancel & task>",
        "task_description": "<Do & cancel safely>",
        "context_note": "<Original & iPhone owner>",
        "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "task-cancel-accept"}, headers=headers)


def _cancel(port: int, certificate: Path, call_id: str, action_id: str, headers: dict[str, str], *, cancelled: object = True):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": action_id, "cancelled": cancelled}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, status: str, report: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "status": status, "report": report}, headers=headers)


def _continuation_for_task(request_id: str, task_id: str, call_id: str, cursor: int) -> dict[str, object]:
    request = _continuation(request_id, task_id, call_id, cursor)
    arguments = request["params"]["arguments"]
    arguments["task_id"] = arguments.pop("interaction_id")
    return request


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf = None; self.cancel: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if tag == "button" and values.get("data-task-cancel") == "true": self.cancel.append(values)


def _page(html: object, call_id: str) -> str:
    source = str(html); parser = _Page(); parser.feed(source); endpoint = f"/api/calls/{call_id}/cancel"
    assert "&lt;Cancel &amp; task&gt;" in source and "&lt;Do &amp; cancel safely&gt;" in source and "&lt;Original &amp; iPhone owner&gt;" in source
    assert parser.csrf and len(parser.cancel) == 1 and parser.cancel[0].get("data-endpoint") == endpoint
    assert re.search(r'''(?sx)
        (?:const|let)\s+(?P<button>[A-Za-z_$][\w$]*)\s*=\s*document\.querySelector\(\s*['"]button\[data-task-cancel\]['"]\s*\)
        .*?(?P=button)\.addEventListener\(\s*['"]click['"]
        .*?JSON\.stringify\(\{[^}]*action_id[^}]*cancelled\s*:\s*true
        .*?fetch\(\s*(?P=button)\.dataset\.endpoint
        .*?['"]X-CSRF-Token['"]\s*:\s*csrf
        .*?credentials\s*:\s*['"]same-origin['"]
    ''', source)
    return parser.csrf


def test_tls_accepted_iphone_task_page_has_explicit_csrf_cancel_action(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "cancel-page"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)
    assert page[0] == 200 and _page(page[2], call_id)


def test_tls_accepted_iphone_task_cancel_is_durable_replayable_and_releases_busy(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "cancel-main"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        first = _cancel(port, certificate, call_id, "task-cancel", headers)
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        replay = _cancel(port, certificate, call_id, "task-cancel", headers); conflict = _cancel(port, certificate, call_id, "task-cancel", headers, cancelled=False)
        assert first[0] == replay[0] == 200 and first[2] == replay[2] and conflict[0] == 409 and isinstance(first[2], dict)
        _, event, _ = _post(port, certificate, _continuation_for_task("cancel-event", task_id, call_id, 1), "test-token-alpha")
        display = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        next_call = _call(port, certificate, "cancel-after")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, handled_on, terminal_platform FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
            operations = connection.execute("SELECT count(*) FROM action_operations WHERE action_id LIKE ?", (f"%:{call_id}:%",)).fetchone()[0]
    finally:
        _stop(process)
    assert first[2]["status"] == first[2]["call_state"] == "cancelled" and first[2]["reason"] == "human_cancelled" and first[2]["human_action"] is True
    assert first[2]["task_id"] == first[2]["interaction_id"] == task_id and first[2]["handled_on"] == "iphone_web" and first[2]["terminal_platform"] == "ios"
    assert first[2].get("feedback_requested") is not True and _structured(event) == first[2]
    assert state == ("cancelled", "iphone_web", "ios") and events == [(1,), (2,)] and operations == 1 and next_call["call_state"] == "ringing"
    assert display[0] == 200 and "data-task-cancel" not in str(display[2]) and "data-task-report" not in str(display[2])


def test_tls_iphone_task_cancel_is_owner_route_expiry_first_and_race_safe(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "cancel-security"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200
        with sqlite3.connect(database) as connection:
            before = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
        cross_site = {**owner, "Origin": "https://evil.example"}
        rejected = [_cancel(port, certificate, call_id, "foreign", _headers(other_cookie, other_csrf, port)), _cancel(port, certificate, call_id, "cross-site", cross_site), _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": "mixed", "cancelled": True, "status": "completed"}, headers=owner)]
        with sqlite3.connect(database) as connection:
            after = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); after_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        expired = _cancel(port, certificate, call_id, "expired", owner); after_expiry = _call(port, certificate, "cancel-after-expiry")
        assert expired[0] == 409 and isinstance(expired[2], dict) and expired[2]["status"] == expired[2]["call_state"] == "disconnected" and expired[2]["reason"] == "terminal_disconnected" and after_expiry["call_state"] == "ringing"
        admitted_id = after_expiry["call_id"]; assert isinstance(admitted_id, str)
        assert _accept(port, certificate, admitted_id, owner)[0] == 200
        cleanup = _cancel(port, certificate, admitted_id, "post-expiry-cleanup", owner)
        assert cleanup[0] == 200 and isinstance(cleanup[2], dict) and cleanup[2]["status"] == "cancelled"

        raced = _call(port, certificate, "cancel-race"); race_id = raced["call_id"]; assert isinstance(race_id, str); race_cookie, race_csrf = _login(port, certificate); race_headers = _headers(race_cookie, race_csrf, port); assert _accept(port, certificate, race_id, race_headers)[0] == 200
        with ThreadPoolExecutor(max_workers=2) as executor:
            cancel = executor.submit(_cancel, port, certificate, race_id, "race-cancel", race_headers)
            final = executor.submit(_submit, port, certificate, race_id, "race-final", "completed", "done", race_headers)
            cancel_response, final_response = cancel.result(), final.result()
        with sqlite3.connect(database) as connection:
            race_state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (race_id,)).fetchone()[0]
            race_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (race_id,)).fetchall()
    finally:
        _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in rejected) and after == before and after_events == events == [(1,)]
    assert call_id.lower() not in repr(rejected[0][2]).lower()
    assert {cancel_response[0], final_response[0]} <= {200, 400, 409} and race_state in {"cancelled", "completed"} and race_events == [(1,), (2,)]


def test_tls_task_cancel_and_progress_race_has_ordered_events_and_no_feedback(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "cancel-progress-race"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        with ThreadPoolExecutor(max_workers=2) as executor:
            cancelling = executor.submit(_cancel, port, certificate, call_id, "cancel-progress-race", headers)
            progressing = executor.submit(_submit, port, certificate, call_id, "cancel-progress", "in_progress", "Race progress", headers)
            cancel_response, progress_response = cancelling.result(), progressing.result()
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
            events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
            action_operations = connection.execute("SELECT count(*) FROM action_operations WHERE action_id LIKE ?", (f"%:{call_id}:%",)).fetchone()[0]
            submission_operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
        next_call = _call(port, certificate, "cancel-progress-after")
    finally:
        _stop(process)
    assert cancel_response[0] in {200, 409} and progress_response[0] in {200, 400, 409}
    assert state == "cancelled" and action_operations == 1 and submission_operations in {0, 1}
    assert [(seq,) for seq, _ in events] in ([(1,), (2,)], [(1,), (2,), (3,)])
    event_results = [json.loads(result) for _, result in events]
    assert event_results[0]["status"] == "opened" and event_results[-1]["status"] == event_results[-1]["call_state"] == "cancelled"
    assert all(item.get("feedback_requested") is not True for item in event_results) and next_call["call_state"] == "ringing"
    if len(event_results) == 3:
        assert event_results[1]["status"] == event_results[1]["call_state"] == "in_progress" and submission_operations == 1
    else:
        assert submission_operations == 0
