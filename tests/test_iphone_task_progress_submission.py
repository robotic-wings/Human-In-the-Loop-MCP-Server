"""Red HTTPS contracts for accepted iPhone task progress reports only."""

from __future__ import annotations

import sqlite3
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
    request["params"]["arguments"] = {"task_title": "<Progress & task>", "task_description": "<Do & report>", "context_note": "<Keep & working>", "request_id": request_id, "client_timeout_seconds": 2}
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _accept(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "task-progress-accept"}, headers=headers)


def _submit(port: int, certificate: Path, call_id: str, submission_id: str, status: object, report: object, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": submission_id, "status": status, "report": report}, headers=headers)


def _heartbeat(port: int, certificate: Path, call_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/heartbeat", body={}, headers=headers)


def _continuation_for_task(request_id: str, task_id: str, call_id: str, cursor: int) -> dict[str, object]:
    request = _continuation(request_id, task_id, call_id, cursor)
    arguments = request["params"]["arguments"]
    arguments["task_id"] = arguments.pop("interaction_id")
    return request


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf = None; self.textareas = 0; self.progress: list[dict[str, str]] = []
    def handle_starttag(self, tag, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if tag == "textarea": self.textareas += 1
        if tag == "button" and values.get("data-task-progress") == "true": self.progress.append(values)


def _page(html: object, call_id: str) -> str:
    source = str(html); parser = _Page(); parser.feed(source); endpoint = f"/api/calls/{call_id}/submissions"
    assert "&lt;Progress &amp; task&gt;" in source and "&lt;Do &amp; report&gt;" in source and "&lt;Keep &amp; working&gt;" in source
    assert parser.csrf and parser.textareas == 1 and len(parser.progress) == 1 and parser.progress[0].get("data-endpoint") == endpoint
    assert "credentials: 'same-origin'" in source and "X-CSRF-Token" in source
    assert re.search(r'''(?sx)
        (?:const|let)\s+(?P<button>[A-Za-z_$][\w$]*)\s*=\s*document\.querySelector\(\s*['"]button\[data-task-progress\]['"]\s*\)
        .*?(?P=button)\.addEventListener\(\s*['"]click['"]
        .*?(?:document\.querySelector\([^)]*textarea|report)
        .*?JSON\.stringify\(\{[^}]*status\s*:\s*['"]in_progress['"][^}]*report
        .*?fetch\(\s*(?P=button)\.dataset\.endpoint
        .*?['"]X-CSRF-Token['"]\s*:\s*csrf
        .*?credentials\s*:\s*['"]same-origin['"]
    ''', source)
    return parser.csrf


def test_tls_accepted_iphone_task_progress_persists_replays_and_keeps_page_writable(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "progress-main"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        page_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id); protected = _headers(cookie, page_csrf, port)
        first = _submit(port, certificate, call_id, "progress-1", "in_progress", "  Waiting for access.  ", protected); replay = _submit(port, certificate, call_id, "progress-1", "in_progress", "Waiting for access.", protected); conflict = _submit(port, certificate, call_id, "progress-1", "in_progress", "Different", protected)
        _, event, _ = _post(port, certificate, _continuation_for_task("progress-event", task_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection:
            before_heartbeat = connection.execute("SELECT terminal_last_seen_at, terminal_disconnect_deadline, agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        heartbeat = _heartbeat(port, certificate, call_id, protected)
        with sqlite3.connect(database) as connection:
            after_heartbeat = connection.execute("SELECT terminal_last_seen_at, terminal_disconnect_deadline, agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        page_after = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform, agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone(); events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert first[0] == replay[0] == 200 and first[2] == replay[2] and conflict[0] == 409 and isinstance(first[2], dict)
    assert first[2]["status"] == first[2]["call_state"] == "in_progress" and first[2]["report"] == "Waiting for access." and first[2]["human_action"] is True and first[2]["task_id"] == first[2]["interaction_id"] == task_id and first[2].get("result_id") is None and first[2].get("resubmittable") is not True
    assert _structured(event) == first[2] and page_after[0] == 200 and _page(page_after[2], call_id)
    assert heartbeat[0] == 200 and isinstance(heartbeat[2], dict) and heartbeat[2]["call_state"] == "in_progress"
    assert before_heartbeat is not None and after_heartbeat is not None and after_heartbeat[0] > before_heartbeat[0] and after_heartbeat[1] > before_heartbeat[1] and after_heartbeat[2] == before_heartbeat[2]
    assert state[:5] == ("in_progress", "iphone_web", state[2], "iphone_web", "ios") and state[5] is not None and events == [(1,), (2,)] and operations == 1 and results == 0


def test_tls_task_progress_reconnect_grace_is_300_seconds_and_replay_does_not_extend(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "progress-grace"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200; assert _submit(port, certificate, call_id, "progress-grace", "in_progress", "Still working", headers)[0] == 200
        delivery_started = datetime.now(UTC)
        _, delivered, _ = _post(port, certificate, _continuation_for_task("progress-deliver", task_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection: first_deadline = connection.execute("SELECT agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
        _, replayed, _ = _post(port, certificate, _continuation_for_task("progress-replay", task_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection: second_deadline = connection.execute("SELECT agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
        _, waiting, _ = _post(port, certificate, _continuation_for_task("progress-waiting", task_id, call_id, 2), "test-token-alpha")
        with sqlite3.connect(database) as connection: third_deadline = connection.execute("SELECT agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally: _stop(process)
    assert _structured(delivered)["status"] == "in_progress" and _structured(delivered)["event_seq"] == 2 and _structured(replayed) == _structured(delivered)
    assert first_deadline == second_deadline == third_deadline and first_deadline is not None
    assert timedelta(seconds=299) <= datetime.fromisoformat(first_deadline) - delivery_started <= timedelta(seconds=302)
    assert _structured(waiting)["status"] in {"heartbeat", "waiting"}


def test_tls_task_progress_rejects_invalid_foreign_and_expired_agent_releases_busy(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "progress-security"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200
        with sqlite3.connect(database) as connection: before = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall()
        cross_site = dict(owner); cross_site["Origin"] = "https://evil.example"
        bad = [_submit(port, certificate, call_id, "empty", "in_progress", "   ", owner), _submit(port, certificate, call_id, "wrong", "other", "x", owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "mixed", "status": "in_progress", "report": "x", "confirmed": False}, headers=owner), _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "cross-site", "status": "in_progress", "report": "x"}, headers=cross_site), _submit(port, certificate, call_id, "foreign", "in_progress", "x", _headers(other_cookie, other_csrf, port))]
        with sqlite3.connect(database) as connection: after = connection.execute("SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?", (call_id,)).fetchone(); operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; after_events = connection.execute("SELECT event_seq FROM events WHERE call_id = ?", (call_id,)).fetchall(); connection.execute("UPDATE calls SET state = 'in_progress', agent_reconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        _stop(process); process = _start(config); _wait_for_listener(process, port); _, terminal, _ = _post(port, certificate, _continuation_for_task("progress-expired", task_id, call_id, 1), "test-token-alpha"); next_call = _call(port, certificate, "progress-after-expiry")
    finally: _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in bad) and after == before and operations == 0 and after_events == events == [(1,)]
    opaque = repr(bad[-1][2]).lower(); assert not any(word in opaque for word in (call_id.lower(), "status", "call_state", "reason", "result", "task", "owner", "accepted"))
    assert _structured(terminal)["status"] == _structured(terminal)["call_state"] == "disconnected" and _structured(terminal)["reason"] == "agent_disconnected" and next_call["call_state"] == "ringing"


def test_tls_task_accept_progress_race_has_no_duplicate_progress_event_or_operation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "progress-race"); call_id = created["call_id"]; assert isinstance(call_id, str); cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
        with ThreadPoolExecutor(max_workers=2) as executor:
            accepted_future = executor.submit(_accept, port, certificate, call_id, headers)
            submitted_future = executor.submit(_submit, port, certificate, call_id, "progress-race", "in_progress", "Race progress", headers)
            accepted = accepted_future.result(); submitted = submitted_future.result()
        with sqlite3.connect(database) as connection: state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]; operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]; events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally: _stop(process)
    assert accepted[0] == 200 and submitted[0] in {200, 400, 409}
    assert (state == "in_progress" and operations == 1 and events == [(1,), (2,)]) or (state == "accepted" and operations == 0 and events == [(1,)])
