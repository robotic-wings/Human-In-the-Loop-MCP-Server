"""Red contracts for owner-session-bound desktop terminal cancellation.

The cancellation slice deliberately excludes drafts, uploads, submissions,
browser/mobile behavior, and agent cancellation.  It asserts only the
authenticated desktop terminal path required by SPEC §§3, 4, 6.3, 8, 9.3,
and 10.1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from call_coordinator import CallCoordinator
from test_central_mcp_gateway import _free_port, _start, _stop
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_action_operations,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)
from test_terminal_session_liveness import (
    accept_desktop_call,
    create_desktop_call,
    make_clock,
    persisted_call,
)


def _event_rows(database: Path, call_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq",
            (call_id,),
        ).fetchall()
    return [dict(row) for row in rows]


def _cancel(
    service: CallCoordinator,
    call_id: str,
    *,
    action_id: str = "cancel",
    device_id: str = "desktop-registered",
    owner_session_id: str = "owner-session",
    platform: str = "windows",
) -> dict[str, Any] | None:
    return service.terminal_action(
        "cancel", call_id, device_id, action_id, owner_session_id, platform
    )


def test_owner_cancel_is_final_human_cancelled_event_releases_busy_and_has_no_answer_fields(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "cancel.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)
    accepted = persisted_call(database, call_id)

    cancelled = _cancel(service, call_id)
    stored = persisted_call(database, call_id)
    events = service.events_for(call_id)
    next_call = create_desktop_call(service, request_id="after-cancel")
    service.close()

    assert cancelled is not None
    assert cancelled["success"] is True
    assert cancelled["status"] == cancelled["call_state"] == "cancelled"
    assert cancelled["reason"] == "human_cancelled"
    assert cancelled["human_action"] is True
    assert cancelled["needs_continuation"] is False
    assert cancelled["event_seq"] == 2
    assert stored["state"] == "cancelled"
    assert stored["version"] == accepted["version"] + 1
    assert [event["event_seq"] for event in events] == [1, 2]
    assert events[-1] == cancelled
    assert next_call["status"] == "ringing"
    for field in ("user_input", "selected_choices", "confirmed", "acknowledged", "attachments"):
        assert field not in cancelled


def test_cancel_replays_byte_equivalently_after_restart_without_duplicate_event(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "cancel-replay.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)
    first = _cancel(service, call_id, action_id="durable-cancel")
    events_before_restart = _event_rows(database, call_id)
    operation_before_restart = _terminal_action_operations(
        database, "desktop-registered", call_id, "durable-cancel"
    )
    service.close()

    reopened = CallCoordinator(database, clock=clock)
    replay = _cancel(reopened, call_id, action_id="durable-cancel")
    events_after = _event_rows(database, call_id)
    operations = _terminal_action_operations(
        database, "desktop-registered", call_id, "durable-cancel"
    )
    reopened.close()

    assert first is not None
    assert replay == first
    assert events_after == events_before_restart
    assert operations == operation_before_restart
    assert len(operations) == 1


def test_cancel_settles_terminal_disconnect_before_late_mutation_and_survives_recovery(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "cancel-deadline.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)

    clock.advance(180)
    late_cancel = _cancel(service, call_id, action_id="late-cancel")
    late_heartbeat = service.terminal_heartbeat(
        call_id, "desktop-registered", "owner-session"
    )
    events_after_deadline = service.events_for(call_id)
    late_operations = _terminal_action_operations(
        database, "desktop-registered", call_id, "late-cancel"
    )
    service.close()

    reopened = CallCoordinator(database, clock=clock)
    assert reopened.recover_and_settle() == []
    assert reopened.events_for(call_id) == events_after_deadline
    reopened.close()

    assert late_cancel is not None
    assert late_cancel["status"] == "disconnected"
    assert late_cancel["reason"] == "terminal_disconnected"
    assert late_heartbeat == late_cancel
    assert [event["event_seq"] for event in events_after_deadline] == [1, 2]
    assert late_operations == []


def test_cancel_before_deadline_wins_and_recovery_does_not_emit_a_second_event(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "cancel-before-deadline.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)

    clock.advance(179)
    cancelled = _cancel(service, call_id, action_id="predeadline-cancel")
    events_before_restart = service.events_for(call_id)
    service.close()
    clock.advance(2)

    reopened = CallCoordinator(database, clock=clock)
    assert reopened.recover_and_settle() == []
    assert reopened.events_for(call_id) == events_before_restart
    assert reopened.get_call(call_id) == cancelled
    reopened.close()

    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert cancelled["reason"] == "human_cancelled"
    assert [event["event_seq"] for event in events_before_restart] == [1, 2]


def test_tls_terminal_cancel_requires_bearer_route_owner_session_and_action_id_without_leakage(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "cancel-http-create")
        call_id = created["call_id"]
        accepted = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            "desktop-token",
            {"action_id": "cancel-http-accept", "owner_session_id": "owner-session"},
        )
        before, before_events = _terminal_snapshot(database, call_id)
        missing_bearer = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/cancel",
            None,
            {"action_id": "unauth", "owner_session_id": "owner-session"},
        )
        wrong_route = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/cancel",
            "other-desktop-token",
            {"action_id": "wrong-route", "owner_session_id": "owner-session"},
        )
        unknown = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/not-a-real-call/cancel",
            "other-desktop-token",
            {"action_id": "unknown", "owner_session_id": "owner-session"},
        )
        wrong_session = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/cancel",
            "desktop-token",
            {"action_id": "wrong-session", "owner_session_id": "not-owner"},
        )
        missing_action = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/cancel",
            "desktop-token",
            {"owner_session_id": "owner-session"},
        )
        after_denials, after_denial_events = _terminal_snapshot(database, call_id)
        valid = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/cancel",
            "desktop-token",
            {"action_id": "valid-cancel", "owner_session_id": "owner-session"},
        )
    finally:
        _stop(process)

    assert accepted[0] == 200
    assert missing_bearer == (401, {"error": "unauthorized"})
    assert wrong_route == unknown == (404, {"error": "not found"})
    assert wrong_session == (
        409,
        {"success": False, "status": "ownership_conflict", "needs_continuation": False},
    )
    assert missing_action == (400, {"error": "invalid request"})
    assert after_denials == before
    assert after_denial_events == before_events
    assert _terminal_action_operations(database, "desktop-other", call_id, "wrong-route") == []
    assert _terminal_action_operations(database, "desktop-other", "not-a-real-call", "unknown") == []
    assert _terminal_action_operations(database, "desktop-registered", call_id, "wrong-session") == []
    assert valid[0] == 200
    assert valid[1]["status"] == "cancelled"
    assert valid[1]["reason"] == "human_cancelled"


def _terminal_snapshot(database: Path, call_id: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    return persisted_call(database, call_id), _event_rows(database, call_id)
