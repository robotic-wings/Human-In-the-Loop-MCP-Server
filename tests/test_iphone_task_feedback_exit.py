"""Red HTTPS contract for D10 task-to-feedback atomic exit only."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
import re

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_task_final_submission import _accept, _call, _task_continuation
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_wait_leg import _with_timeout


def _exit(port: int, certificate: Path, call_id: str, submission_id: str, status: str, report: str, headers: dict[str, str], *, feedback_requested: bool = True):
    return _request(
        port, certificate, "POST", f"/api/calls/{call_id}/submissions",
        body={"submission_id": submission_id, "status": status, "report": report, "feedback_requested": feedback_requested}, headers=headers,
    )


def _owner_writer_headers(port: int, certificate: Path, call_id: str, cookie: str, csrf: str) -> dict[str, str]:
    headers = _headers(cookie, csrf, port)
    acquired = _request(port, certificate, "POST", f"/api/calls/{call_id}/writer/acquire", body={"page_id": "feedback-exit", "action_id": f"writer-{call_id}"}, headers=headers)
    assert acquired[0] == 200 and isinstance(acquired[2], dict) and isinstance(acquired[2].get("writer_token"), str)
    return {**headers, "X-Writer-Token": acquired[2]["writer_token"]}


class _TaskPage(HTMLParser):
    def __init__(self) -> None:
        super().__init__(); self.csrf = None; self.actions: list[dict[str, str]] = []

    def handle_starttag(self, tag, attrs) -> None:
        values = {key: value or "" for key, value in attrs}
        if tag == "meta" and values.get("name") == "X-CSRF-Token": self.csrf = values.get("content")
        if tag == "button" and values.get("data-task-feedback-exit"): self.actions.append(values)


def _task_feedback_page(html: object, call_id: str) -> str:
    source = str(html); parser = _TaskPage(); parser.feed(source)
    assert parser.csrf and {item["data-task-feedback-exit"] for item in parser.actions} == {"completed", "failed", "cancelled", "withdraw"}
    endpoint = f"/api/calls/{call_id}/submissions"
    assert {item.get("data-endpoint") for item in parser.actions if item["data-task-feedback-exit"] != "withdraw"} == {endpoint}
    assert re.search(r'''(?sx)
        data-task-feedback-exit.*?feedback_requested
        .*?JSON\.stringify\(\{[^}]*submission_id[^}]*status[^}]*report[^}]*feedback_requested\s*:\s*true
        .*?credentials\s*:\s*['"]same-origin['"]
    ''', source)
    assert re.search(r'''(?sx)data-task-feedback-exit=["']withdraw["'].*?(?:remove|hidden|cancel)''', source)
    return parser.csrf


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute("SELECT state, owner_device_id, owner_session_id, version, task_version FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        return (call, connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0], connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0], connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0])


def _cancel_feedback_exit(port: int, certificate: Path, call_id: str, action_id: str, headers: dict[str, str]):
    return _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": action_id, "cancelled": True, "feedback_requested": True}, headers=headers)


def _feedback_call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "get_feedback_from_user"
    request["params"]["arguments"] = {"prompt": "Later independent feedback", "request_id": request_id, "client_timeout_seconds": 2}
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def test_tls_accepted_iphone_task_page_offers_atomic_feedback_exit_and_withdraw(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-page"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)
    assert page[0] == 200 and _task_feedback_page(page[2], call_id)


def test_tls_task_feedback_exit_is_atomic_durable_replayable_and_releases_busy(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-main"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200; headers = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        first = _exit(port, certificate, call_id, "feedback-exit", "completed", "Finished before feedback", headers)
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        replay = _exit(port, certificate, call_id, "feedback-exit", "completed", "Finished before feedback", headers)
        conflict = _exit(port, certificate, call_id, "feedback-exit", "failed", "Finished before feedback", headers)
        _, event, _ = _post(port, certificate, _task_continuation("feedback-exit-event", task_id, call_id, 1), "test-token-alpha")
        next_call = _call(port, certificate, "feedback-exit-next")
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]
            calls_after = connection.execute("SELECT count(*) FROM calls").fetchone()[0]
    finally:
        _stop(process)
    assert first[0] == replay[0] == 200 and first[2] == replay[2] and conflict[0] == 409
    assert isinstance(first[2], dict) and first[2]["status"] == first[2]["call_state"] == "completed" and first[2]["feedback_requested"] is True
    assert _structured(event) == first[2] and state == "completed" and next_call["call_state"] == "ringing" and calls_after == 2


def test_tls_task_feedback_exit_failed_is_terminal_not_in_progress(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-failed"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200; headers = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        result = _exit(port, certificate, call_id, "feedback-failed", "failed", "stop for feedback", headers)
    finally:
        _stop(process)
    assert result[0] == 200 and isinstance(result[2], dict)
    assert result[2]["status"] == result[2]["call_state"] == "failed" and result[2]["feedback_requested"] is True and result[2].get("reason") is None


def test_tls_task_feedback_exit_cancelled_is_atomic_human_cancelled_and_releases_busy(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-cancelled"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200; headers = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        cancelled = _cancel_feedback_exit(port, certificate, call_id, "feedback-cancelled", headers)
        next_call = _call(port, certificate, "feedback-exit-after-cancelled")
    finally:
        _stop(process)
    assert cancelled[0] == 200 and isinstance(cancelled[2], dict)
    assert cancelled[2]["status"] == cancelled[2]["call_state"] == "cancelled" and cancelled[2]["reason"] == "human_cancelled" and cancelled[2]["feedback_requested"] is True
    assert next_call["call_state"] == "ringing"


def test_tls_task_feedback_exit_denials_and_withdraw_leave_task_unchanged(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-security"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200; owner = _owner_writer_headers(port, certificate, call_id, owner_cookie, owner_csrf)
        before = _snapshot(database, call_id)
        denied = [
            _exit(port, certificate, call_id, "missing-csrf", "completed", "x", {"Cookie": owner_cookie, "Host": f"127.0.0.1:{port}", "Origin": f"https://127.0.0.1:{port}"}),
            _exit(port, certificate, call_id, "other-session", "completed", "x", _headers(other_cookie, other_csrf, port)),
            _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "stale-version", "status": "completed", "report": "x", "task_version": 1, "feedback_requested": True}, headers=owner),
            _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "mixed", "status": "in_progress", "report": "x", "feedback_requested": True}, headers=owner),
            _exit(port, certificate, call_id, "origin", "completed", "x", {**owner, "Origin": "https://evil.example"}),
        ]
        after = _snapshot(database, call_id)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie})
    finally:
        _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in denied) and after == before
    assert page[0] == 200 and "data-task-feedback-exit" in str(page[2])


def test_tls_task_feedback_withdraw_is_local_and_does_not_change_the_accepted_task(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-withdraw"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        before = _snapshot(database, call_id); page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie}); after = _snapshot(database, call_id)
    finally:
        _stop(process)
    source = str(page[2])
    assert before == after and page[0] == 200
    assert re.search(r'''(?sx)data-task-feedback-exit=["']withdraw["'].*?addEventListener\(['"]click['"].*?(?!fetch\()''', source)


def test_tls_task_feedback_exit_stale_version_and_untrusted_contexts_have_no_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-context"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); base = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, base)[0] == 200; owner = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        before = _snapshot(database, call_id)
        malformed = {"submission_id": "stale-task-version", "status": "completed", "report": "x", "task_version": 99, "feedback_requested": True}
        denied = (
            _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body=malformed, headers=owner),
            _exit(port, certificate, call_id, "no-writer", "completed", "x", {key: value for key, value in owner.items() if key != "X-Writer-Token"}),
            _exit(port, certificate, call_id, "stale-writer", "completed", "x", {**owner, "X-Writer-Token": "stale"}),
            _exit(port, certificate, call_id, "other", "completed", "x", _headers(other_cookie, other_csrf, port)),
            _exit(port, certificate, call_id, "no-csrf", "completed", "x", {"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}", "X-Writer-Token": owner["X-Writer-Token"]}),
            _exit(port, certificate, call_id, "host", "completed", "x", {**owner, "Host": "evil.example"}),
            _exit(port, certificate, call_id, "bearer", "completed", "x", {"Authorization": "Bearer terminal-secret"}),
        )
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert all(item[0] in {400, 401, 403, 404, 409} for item in denied) and after == before


def test_tls_task_feedback_exit_ended_call_is_rejected_without_new_feedback_call(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-ended"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); base = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, base)[0] == 200; owner = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        ended = _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": "end-first", "cancelled": True}, headers=owner); assert ended[0] == 200
        before = _snapshot(database, call_id); rejected = _exit(port, certificate, call_id, "after-ended", "completed", "x", owner); after = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert rejected[0] in {400, 401, 403, 404, 409} and after == before


def test_tls_task_feedback_exit_ringing_route_and_expired_denials_do_not_create_feedback_call(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "ringing", port); _configure_phone_route(config); database = tmp_path / "ringing" / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        ringing = _call(port, certificate, "feedback-exit-ringing"); ringing_id = ringing["call_id"]; assert isinstance(ringing_id, str); cookie, csrf = _login(port, certificate)
        before_ringing = _snapshot(database, ringing_id); rejected_ringing = _exit(port, certificate, ringing_id, "ringing", "completed", "x", _headers(cookie, csrf, port)); after_ringing = _snapshot(database, ringing_id)
    finally:
        _stop(process)
    port = _free_port(); config, certificate = _write_operator_config(tmp_path / "route", port); database = tmp_path / "route" / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        route = _call(port, certificate, "feedback-exit-route"); route_id = route["call_id"]; assert isinstance(route_id, str); cookie, csrf = _login(port, certificate)
        before_route = _snapshot(database, route_id); rejected_route = _exit(port, certificate, route_id, "route", "completed", "x", _headers(cookie, csrf, port)); after_route = _snapshot(database, route_id)
    finally:
        _stop(process)
    assert rejected_ringing[0] in {400, 401, 403, 404, 409} and after_ringing == before_ringing
    assert rejected_route[0] in {400, 401, 403, 404, 409} and after_route == before_route


def test_tls_later_independent_feedback_is_normally_admitted_but_busy_while_task_is_accepted(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-busy"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, headers)[0] == 200
        later = _feedback_call(port, certificate, "later-feedback-busy")
    finally:
        _stop(process)
    assert later["status"] == "busy" and later["needs_continuation"] is False and later["call_id"] is None


def test_tls_task_feedback_exit_expiry_persists_terminal_event_without_feedback_call(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "feedback-exit-expired"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); base = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, base)[0] == 200; owner = _owner_writer_headers(port, certificate, call_id, cookie, csrf)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        late = _exit(port, certificate, call_id, "expired", "completed", "x", owner)
        _, event, _ = _post(port, certificate, _task_continuation("feedback-expired-event", task_id, call_id, 1), "test-token-alpha")
        with sqlite3.connect(database) as connection: rows = connection.execute("SELECT result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
    finally:
        _stop(process)
    assert late[0] == 409 and isinstance(late[2], dict) and late[2]["status"] == "disconnected" and late[2].get("feedback_requested") is not True
    assert _structured(event) == late[2] and len(rows) == 2 and json.loads(rows[-1][0]).get("feedback_requested") is not True
