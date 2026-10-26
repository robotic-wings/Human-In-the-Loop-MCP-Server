"""Red TLS contracts for final terminal submissions that reference uploaded attachments.

This intentionally covers only the terminal HTTP boundary and durable archive
state.  It does not add result-reading, UI, or task-progress behavior.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _free_port, _start, _stop
from test_terminal_attachment_upload_api import _accept, _attachment_files, _attachment_rows, _upload
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


def _submit(
    port: int,
    certificate: Path,
    call_id: str,
    token: str,
    owner_session_id: str,
    submission_id: str,
    *,
    task_version: int,
    attachment_ids: list[str],
    user_input: str = "final report",
) -> tuple[int, dict[str, Any]]:
    return _terminal_request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/submissions",
        token,
        {
            "owner_session_id": owner_session_id,
            "submission_id": submission_id,
            "task_version": task_version,
            "attachment_ids": attachment_ids,
            "user_input": user_input,
        },
    )


def _accepted(port: int, certificate: Path, request_id: str, owner: str) -> dict[str, Any]:
    created = _create_ringing_call(port, certificate, request_id)
    _accept(port, certificate, created["call_id"], owner)
    return created


def test_tls_final_submission_requires_current_task_version_before_result_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _accepted(port, certificate, "final-version", "version-owner")
        call_id = created["call_id"]
        mismatch = _submit(
            port, certificate, call_id, "desktop-token", "version-owner", "version-mismatch",
            task_version=1, attachment_ids=[],
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()
    finally:
        _stop(process)

    assert mismatch == (409, {"success": False, "status": "task_version_conflict", "call_state": "accepted", "task_version": 0, "needs_continuation": False})
    assert state == ("accepted",)
    assert events == (1,)


def test_tls_final_submission_rejects_nonexistent_and_foreign_attachment_references_before_result(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        prior = _accepted(port, certificate, "final-foreign-prior", "prior-owner")
        prior_id = prior["call_id"]
        uploaded = _upload(port, certificate, prior_id, "desktop-token", "prior-owner")
        assert uploaded[0] == 200
        foreign_id = uploaded[1]["attachment"]["attachment_id"]
        assert _terminal_request(port, certificate, "POST", f"/api/calls/{prior_id}/cancel", "desktop-token", {"action_id": "end-prior", "owner_session_id": "prior-owner"})[0] == 200
        candidate = _accepted(port, certificate, "final-foreign-candidate", "candidate-owner")
        call_id = candidate["call_id"]
        foreign = _submit(port, certificate, call_id, "desktop-token", "candidate-owner", "foreign-ref", task_version=0, attachment_ids=[foreign_id])
        nonexistent = _submit(port, certificate, call_id, "desktop-token", "candidate-owner", "missing-ref", task_version=0, attachment_ids=["not-an-uploaded-attachment"])
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()
    finally:
        _stop(process)

    expected = {"success": False, "status": "invalid_attachment_reference", "needs_continuation": False}
    assert foreign == (400, expected)
    assert nonexistent == (400, expected)
    assert state == ("accepted",)
    assert events == (1,)


def test_tls_final_submission_replays_identical_attached_result_without_archive_or_event_duplication(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _accepted(port, certificate, "final-replay", "replay-owner")
        call_id = created["call_id"]
        uploaded = _upload(port, certificate, call_id, "desktop-token", "replay-owner")
        assert uploaded[0] == 200
        attachment_id = uploaded[1]["attachment"]["attachment_id"]
        first = _submit(port, certificate, call_id, "desktop-token", "replay-owner", "attached-once", task_version=0, attachment_ids=[attachment_id])
        replay = _submit(port, certificate, call_id, "desktop-token", "replay-owner", "attached-once", task_version=0, attachment_ids=[attachment_id])
        changed = _submit(port, certificate, call_id, "desktop-token", "replay-owner", "attached-once", task_version=0, attachment_ids=[], user_input="changed")
        database = tmp_path / "data" / "calls.sqlite3"
        with sqlite3.connect(database) as connection:
            event_count = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()
            operation_count = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()
    finally:
        _stop(process)

    assert first[0] == 200
    assert first[1]["attachment_ids"] == [attachment_id]
    assert replay == first
    assert changed == (409, {"success": False, "status": "request_conflict", "needs_continuation": False})
    assert event_count == (2,)
    assert operation_count == (1,)
    assert len(_attachment_rows(database, call_id)) == 1
    assert len(_attachment_files(tmp_path / "data")) == 1


def test_tls_final_submission_hides_attachment_contract_from_wrong_owner_and_settles_expiry_first(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _accepted(port, certificate, "final-auth-expiry", "expiry-owner")
        call_id = created["call_id"]
        wrong = _submit(port, certificate, call_id, "other-desktop-token", "expiry-owner", "wrong", task_version=0, attachment_ids=["unknown"])
        saved = _terminal_request(port, certificate, "PUT", f"/api/calls/{call_id}/draft", "desktop-token", {"owner_session_id": "expiry-owner", "body": "retain final draft", "expected_draft_version": 0})
    finally:
        _stop(process)

    database = tmp_path / "data" / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("UPDATE calls SET terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id))
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        expired = _submit(port, certificate, call_id, "desktop-token", "expiry-owner", "expired", task_version=0, attachment_ids=["unknown"])
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state, draft_body, draft_version FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()
    finally:
        _stop(process)

    assert wrong == (404, {"error": "not found"})
    assert saved[0] == 200
    assert expired == (409, {"success": False, "status": "disconnected", "reason": "terminal_disconnected"})
    assert state == ("disconnected", "retain final draft", 1)
    assert events == (2,)
