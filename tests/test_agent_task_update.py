"""Red HTTPS contracts for versioned active iPhone task instruction updates."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_iphone_task_progress_submission import _accept, _submit, _continuation_for_task
from test_mcp_wait_leg import _with_timeout


def _call(port: int, certificate: Path, request_id: str) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "assign_task_to_human"
    request["params"]["arguments"] = {
        "task_title": "<Update & task>", "task_description": "<Original & description>",
        "context_note": "<Original & context>", "request_id": request_id,
        "client_timeout_seconds": 2,
    }
    _, response, _ = _post(port, certificate, request, "test-token-alpha")
    return _structured(response)


def _update(request_id: str, task_id: str, call_id: str, cursor: int, version: int, description: object, context: object) -> dict[str, object]:
    request = _continuation_for_task(request_id, task_id, call_id, cursor)
    arguments = request["params"]["arguments"]
    arguments["expected_task_version"] = version
    if description is not None:
        arguments["task_description"] = description
    if context is not None:
        arguments["context_note"] = context
    return request


def _snapshot(database: Path, call_id: str):
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, owner_device_id, owner_session_id, handled_on, terminal_platform, "
            "version, task_version, terminal_last_seen_at, terminal_disconnect_deadline, "
            "agent_reconnect_deadline, message FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        events = connection.execute("SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        operations = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    return call, events, results, operations


def _accepted_progress(port: int, certificate: Path, database: Path, request_id: str):
    created = _call(port, certificate, request_id)
    call_id, task_id = created["call_id"], created["interaction_id"]
    assert isinstance(call_id, str) and isinstance(task_id, str)
    cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port)
    assert _accept(port, certificate, call_id, owner)[0] == 200
    assert _submit(port, certificate, call_id, "progress-" + request_id, "in_progress", "  progress one  ", owner)[0] == 200
    _, delivered, _ = _post(port, certificate, _continuation_for_task("deliver-" + request_id, task_id, call_id, 1), "test-token-alpha")
    assert _structured(delivered)["status"] == "in_progress"
    return call_id, task_id, cookie, owner


def test_tls_agent_update_after_ack_is_versioned_replayed_and_refreshes_phone_page(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        call_id, task_id, cookie, _ = _accepted_progress(port, certificate, database, "update-main")
        before = _snapshot(database, call_id)
        request = _update("update-1", task_id, call_id, 2, 0, "<Changed & description>", "<Changed & context>")
        _, first_raw, _ = _post(port, certificate, request, "test-token-alpha")
        _, replay_raw, _ = _post(port, certificate, request, "test-token-alpha")
        conflict = _update("update-1", task_id, call_id, 2, 0, "other", "other")
        _, conflict_raw, _ = _post(port, certificate, conflict, "test-token-alpha")
        stale = _update("update-stale", task_id, call_id, 2, 0, "stale", "stale")
        _, stale_raw, _ = _post(port, certificate, stale, "test-token-alpha")
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(database, call_id)
    finally:
        _stop(process)
    first, replay, conflicting, stale_result = _structured(first_raw), _structured(replay_raw), _structured(conflict_raw), _structured(stale_raw)
    assert first == replay
    assert first["success"] is True and first["status"] == "updated" and first["call_state"] == "in_progress"
    assert first["task_id"] == first["interaction_id"] == task_id and first["call_id"] == call_id
    assert first["task_version"] == 1 and first["needs_continuation"] is True
    assert conflicting["success"] is False and conflicting["status"] == "request_conflict"
    assert stale_result["success"] is False and stale_result["status"] == "task_version_conflict" and stale_result["task_version"] == 1
    assert before[0] is not None and after[0] is not None
    assert after[0][:5] == before[0][:5] == ("in_progress", "iphone_web", before[0][2], "iphone_web", "ios")
    assert after[0][5] == before[0][5] + 1 and after[0][6] == 1
    assert after[0][7:10] == before[0][7:10] and after[1] == before[1] == [(1, before[1][0][1]), (2, before[1][1][1])]
    assert after[2:] == before[2:] == (0, 1)
    assert page[0] == 200 and "&lt;Changed &amp; description&gt;" in str(page[2]) and "&lt;Changed &amp; context&gt;" in str(page[2])
    assert "Still progressing" in str(page[2]) and "data-task-progress" in str(page[2])


def test_tls_agent_update_requires_ack_owner_alias_and_active_progress_without_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "update-security"); call_id, task_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(task_id, str)
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200
        assert _submit(port, certificate, call_id, "progress-security", "in_progress", "progress", owner)[0] == 200
        before_ack = _snapshot(database, call_id)
        _, no_ack_raw, _ = _post(port, certificate, _update("no-ack", task_id, call_id, 1, 0, "x", "x"), "test-token-alpha")
        _, foreign_raw, _ = _post(port, certificate, _update("foreign", task_id, call_id, 1, 0, "x", "x"), "test-token-beta")
        wrong_alias = _update("wrong-alias", "wrong-task", call_id, 1, 0, "x", "x")
        _, wrong_raw, _ = _post(port, certificate, wrong_alias, "test-token-alpha")
        after_bad = _snapshot(database, call_id)
        _, delivered, _ = _post(port, certificate, _continuation_for_task("deliver-security", task_id, call_id, 1), "test-token-alpha")
        assert _structured(delivered)["event_seq"] == 2
        # A terminal final report wins if it commits first; a subsequent update cannot reopen it.
        assert _submit(port, certificate, call_id, "final-security", "completed", "done", owner)[0] == 200
        before_final_update = _snapshot(database, call_id)
        _, ended_raw, _ = _post(port, certificate, _update("ended-update", task_id, call_id, 2, 0, "x", "x"), "test-token-alpha")
        after_final_update = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert _structured(no_ack_raw)["success"] is False
    assert "error" in foreign_raw or _structured(foreign_raw)["success"] is False
    assert "error" in wrong_raw or _structured(wrong_raw)["success"] is False
    assert after_bad == before_ack
    ended = _structured(ended_raw)
    assert ended["success"] is False and ended["call_state"] == "completed"
    assert after_final_update == before_final_update


def test_tls_agent_update_persists_restart_and_final_requires_new_task_version(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        call_id, task_id, cookie, owner = _accepted_progress(port, certificate, database, "update-restart")
        _, update_raw, _ = _post(port, certificate, _update("restart-update", task_id, call_id, 2, 0, "new", "new context"), "test-token-alpha")
        assert _structured(update_raw)["task_version"] == 1
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        stale_final = _submit(port, certificate, call_id, "stale-final", "completed", "done", owner)
        current_final = _request(port, certificate, "POST", f"/api/calls/{call_id}/submissions", body={"submission_id": "current-final", "status": "completed", "report": "done", "task_version": 1}, headers=owner)
        final_state = _snapshot(database, call_id)
    finally:
        _stop(process)
    assert page[0] == 200 and "new context" in str(page[2])
    assert stale_final[0] == 409 and isinstance(stale_final[2], dict) and stale_final[2]["status"] == "task_version_conflict"
    assert current_final[0] == 200 and isinstance(current_final[2], dict) and current_final[2]["status"] == "completed"
    assert final_state[0] is not None and final_state[0][0] == "completed" and final_state[0][6] == 1 and final_state[1][-1][0] == 3


def test_tls_expired_task_update_settles_before_mutating_instructions_or_task_version(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        call_id, task_id, _, _ = _accepted_progress(port, certificate, database, "update-expiry")
        with sqlite3.connect(database) as connection:
            before = connection.execute(
                "SELECT message, task_version, state, version FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            assert before is not None
            connection.execute(
                "UPDATE calls SET agent_reconnect_deadline = ? WHERE call_id = ?",
                ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            ); connection.commit()
        _, expired_raw, _ = _post(
            port, certificate,
            _update("expired-update", task_id, call_id, 2, 0, "mutated description", "mutated context"),
            "test-token-alpha",
        )
        with sqlite3.connect(database) as connection:
            after = connection.execute(
                "SELECT message, task_version, state, version FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            events = connection.execute("SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)).fetchall()
        replacement = _call(port, certificate, "after-expired-update")
    finally:
        _stop(process)
    expired = _structured(expired_raw)
    assert expired["success"] is True and expired["status"] == expired["call_state"] == "disconnected"
    assert expired["reason"] == "agent_disconnected" and expired["needs_continuation"] is False
    assert after is not None and before is not None
    assert after[0] == before[0] and after[1] == before[1] == 0
    assert after[2] == "disconnected" and after[3] == before[3] + 1 and events == [(1,), (2,), (3,)]
    assert replacement["call_state"] == "ringing"


def test_tls_task_update_allows_each_mutable_field_independently_and_preserves_the_other(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config); database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        call_id, task_id, cookie, _ = _accepted_progress(port, certificate, database, "update-partial")
        context_only = _update("context-only", task_id, call_id, 2, 0, None, "<Changed & context only>")
        _, context_raw, _ = _post(port, certificate, context_only, "test-token-alpha")
        context_replay = _update("context-only", task_id, call_id, 2, 0, None, "<Changed & context only>")
        _, context_replay_raw, _ = _post(port, certificate, context_replay, "test-token-alpha")
        description_only = _update("description-only", task_id, call_id, 2, 1, "<Changed & description only>", None)
        _, description_raw, _ = _post(port, certificate, description_only, "test-token-alpha")
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        state = _snapshot(database, call_id)
    finally:
        _stop(process)
    context_result, context_replayed, description_result = _structured(context_raw), _structured(context_replay_raw), _structured(description_raw)
    assert context_result == context_replayed and context_result["success"] is True and context_result["status"] == "updated"
    assert context_result["task_version"] == 1 and description_result["success"] is True and description_result["status"] == "updated"
    assert description_result["task_version"] == 2
    assert state[0] is not None and state[0][0] == "in_progress" and state[0][6] == 2
    assert "<Changed & description only>" in state[0][10] and "<Changed & context only>" in state[0][10]
    assert page[0] == 200 and "&lt;Changed &amp; description only&gt;" in str(page[2]) and "&lt;Changed &amp; context only&gt;" in str(page[2])
