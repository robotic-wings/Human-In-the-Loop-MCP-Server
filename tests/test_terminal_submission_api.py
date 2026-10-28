"""Red contracts for an accepted desktop owner's final text submission.

This deliberately covers one terminal-owned simple-text answer only.  It does
not introduce attachments, task reports, progress, feedback, revisions, UI,
callbacks, result budgets, or result-reading APIs.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_mcp_continuation import _continuation
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


def _accept(port: int, certificate: Path, call_id: str, owner_session_id: str) -> None:
    status, opened = _terminal_request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/accept",
        "desktop-token",
        {"action_id": f"accept-{owner_session_id}", "owner_session_id": owner_session_id},
    )
    assert status == 200
    assert opened["status"] == "opened"


def _submission(owner_session_id: str, submission_id: str, text: str) -> dict[str, object]:
    return {
        "owner_session_id": owner_session_id,
        "submission_id": submission_id,
        "user_input": text,
    }


def _submit(
    port: int,
    certificate: Path,
    call_id: str,
    token: str,
    owner_session_id: str,
    submission_id: str,
    text: str,
) -> tuple[int, dict[str, Any]]:
    return _terminal_request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/submissions",
        token,
        _submission(owner_session_id, submission_id, text),
    )


def test_tls_owner_text_submission_persists_final_answer_releases_busy_and_mcp_reads_it(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "submission-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "owner-text")
        submission = _submit(
            port, certificate, call_id, "desktop-token", "owner-text", "text-1", "final text"
        )
        assert submission[0] == 200
        assert {key: value for key, value in submission[1].items() if key != "result_id"} == {
            "success": True,
            "status": "answered",
            "call_state": "answered",
            "reason": None,
            "human_action": True,
            "needs_continuation": False,
            "interaction_id": created["interaction_id"],
            "call_id": call_id,
            "event_seq": 2,
            "handled_on": "desktop",
            "terminal_platform": "windows",
            "user_input": "final text",
        }
        assert isinstance(submission[1]["result_id"], str)
        assert submission[1]["result_id"]
        busy_released = _create_ringing_call(port, certificate, "submission-next")
        _, continuation_response, _ = _post(
            port,
            certificate,
            _continuation("submission-event", created["interaction_id"], call_id, 1),
            "test-token-alpha",
        )
    finally:
        _stop(process)

    assert busy_released["status"] == "ringing"
    assert _structured(continuation_response) == submission[1]


def test_tls_submission_replays_after_restart_conflicts_on_changed_content_and_emits_once(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "submission-replay-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "owner-replay")
        first = _submit(
            port, certificate, call_id, "desktop-token", "owner-replay", "text-replay", "persisted answer"
        )
    finally:
        _stop(process)

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        replay = _submit(
            port, certificate, call_id, "desktop-token", "owner-replay", "text-replay", "persisted answer"
        )
        changed = _submit(
            port, certificate, call_id, "desktop-token", "owner-replay", "text-replay", "different answer"
        )
        database = tmp_path / "data" / "calls.sqlite3"
        with sqlite3.connect(database) as connection:
            events = connection.execute(
                "SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
    finally:
        _stop(process)

    assert replay == first
    assert changed == (409, {"success": False, "status": "request_conflict", "needs_continuation": False})
    assert events == [(1,), (2,)]


def test_tls_submission_denials_are_nonleaking_and_cannot_mutate_an_accepted_call(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "submission-auth-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "owner-auth")
        wrong_session = _submit(
            port, certificate, call_id, "desktop-token", "other-owner", "bad-session", "secret"
        )
        wrong_device = _submit(
            port, certificate, call_id, "other-desktop-token", "owner-auth", "bad-device", "secret"
        )
        unknown = _submit(
            port, certificate, "no-such-call", "other-desktop-token", "owner-auth", "unknown", "secret"
        )
        state = _terminal_request(port, certificate, "GET", f"/api/calls/{call_id}", "desktop-token")
    finally:
        _stop(process)

    assert wrong_session == wrong_device == unknown == (404, {"error": "not found"})
    assert state[0] == 200
    assert state[1]["call"]["call_state"] == "accepted"


def test_expired_accepted_submission_settles_disconnected_without_answer_or_draft_loss(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "submission-expiry-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "owner-expiry")
        saved = _terminal_request(
            port, certificate, "PUT", f"/api/calls/{call_id}/draft", "desktop-token",
            {"owner_session_id": "owner-expiry", "body": "keep this draft", "expected_draft_version": 0},
        )
    finally:
        _stop(process)

    database = tmp_path / "data" / "calls.sqlite3"
    past = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE calls SET terminal_disconnect_deadline = ? WHERE call_id = ?", (past, call_id)
        )

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        late = _submit(
            port, certificate, call_id, "desktop-token", "owner-expiry", "late-text", "must not revive"
        )
        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT state, draft_body, draft_version FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            events = connection.execute(
                "SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
    finally:
        _stop(process)

    assert saved == (200, {"success": True, "status": "saved", "draft": {"body": "keep this draft", "draft_version": 1}})
    assert late == (409, {"success": False, "status": "disconnected", "reason": "terminal_disconnected"})
    assert state == ("disconnected", "keep this draft", 1)
    assert events == [(1,), (2,)]
