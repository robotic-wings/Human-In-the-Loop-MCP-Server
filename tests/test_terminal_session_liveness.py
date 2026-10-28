"""Red contracts for accepted-terminal session liveness and disconnection.

This module deliberately stops at the coordinator and authenticated desktop
terminal HTTP boundary.  It uses a controllable clock for every liveness
deadline and does not cover forms, submissions, callbacks, or UI behavior.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from call_coordinator import CallCoordinator, CreateCall
from test_central_mcp_gateway import _free_port, _start, _stop
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def make_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 26, tzinfo=UTC))


def create_desktop_call(service: CallCoordinator, *, request_id: str = "create") -> dict[str, Any]:
    return service.create_call(
        CreateCall(
            operator_id="operator-1",
            caller_id="agent-1",
            interaction_id=f"interaction-{request_id}",
            request_id=request_id,
            message="terminal liveness",
            ring_timeout_seconds=60,
            desktop_route_device_id="desktop-registered",
        )
    )


def accept_desktop_call(
    service: CallCoordinator, call_id: str, *, action_id: str = "accept", session_id: str = "owner-session"
) -> dict[str, Any]:
    result = service.terminal_action(
        "accept",
        call_id,
        "desktop-registered",
        action_id,
        session_id,
        "windows",
    )
    assert result is not None
    assert result["status"] == "opened"
    return result


def persisted_call(database: Path, call_id: str) -> dict[str, Any]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    assert row is not None
    return dict(row)


def action_rows(database: Path, call_id: str) -> list[tuple[str, str, str]]:
    with sqlite3.connect(database) as connection:
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT action_id, payload, result FROM action_operations "
                "WHERE action_id LIKE ? ORDER BY action_id",
                (f"terminal:%:{call_id}:%",),
            )
        ]


def create_pre_liveness_accepted_database(database: Path, accepted_at: datetime) -> None:
    """Create the calls table shape used before terminal liveness existed."""
    with sqlite3.connect(database) as connection:
        connection.execute(
            """
            CREATE TABLE calls (
                call_id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, caller_id TEXT NOT NULL,
                interaction_id TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, ring_deadline TEXT NOT NULL, version INTEGER NOT NULL,
                owner_device_id TEXT, owner_session_id TEXT, handled_on TEXT,
                terminal_platform TEXT, desktop_route_device_id TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO calls VALUES (?, ?, ?, ?, ?, 'accepted', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "legacy-accepted-call",
                "operator-1",
                "agent-1",
                "legacy-interaction",
                "legacy prompt",
                accepted_at.isoformat(),
                (accepted_at + timedelta(seconds=60)).isoformat(),
                2,
                "desktop-registered",
                "legacy-owner-session",
                "desktop",
                "windows",
                "desktop-registered",
            ),
        )


def test_legacy_accepted_row_gets_finite_deadline_and_settles_without_revival(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "legacy-accepted.sqlite3"
    create_pre_liveness_accepted_database(database, clock.now_utc())

    reopened = CallCoordinator(database, clock=clock)
    migrated = persisted_call(database, "legacy-accepted-call")
    assert migrated["terminal_last_seen_at"] == clock.now_utc().isoformat()
    assert migrated["terminal_disconnect_deadline"] == (
        clock.now_utc() + timedelta(seconds=180)
    ).isoformat()
    assert reopened.recover_and_settle() == []
    assert create_desktop_call(reopened, request_id="legacy-still-busy")["status"] == "busy"

    clock.advance(180)
    settled = reopened.recover_and_settle()
    repeated = reopened.recover_and_settle()
    late_heartbeat = reopened.terminal_heartbeat(
        "legacy-accepted-call", "desktop-registered", "legacy-owner-session"
    )
    next_call = create_desktop_call(reopened, request_id="legacy-after-disconnect")
    reopened.close()

    assert len(settled) == 1
    assert settled[0]["status"] == "disconnected"
    assert settled[0]["reason"] == "terminal_disconnected"
    assert repeated == []
    assert late_heartbeat == settled[0]
    assert next_call["status"] == "ringing"


def test_desktop_accept_persists_owner_liveness_and_survives_predeadline_recovery(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accepted = accept_desktop_call(service, call_id)
    stored = persisted_call(database, call_id)

    assert accepted["owner_device_id"] == "desktop-registered"
    assert accepted["owner_session_id"] == "owner-session"
    assert stored["terminal_last_seen_at"] == clock.now_utc().isoformat()
    assert stored["terminal_disconnect_deadline"] == (clock.now_utc() + timedelta(seconds=180)).isoformat()
    original_ring_deadline = stored["ring_deadline"]
    original_version = stored["version"]

    clock.advance(179)
    service.close()
    reopened = CallCoordinator(database, clock=clock)
    assert reopened.recover_and_settle() == []
    recovered = reopened.get_call(call_id)
    busy = create_desktop_call(reopened, request_id="still-busy")
    reopened.close()

    assert recovered["call_state"] == "accepted"
    assert recovered["owner_device_id"] == "desktop-registered"
    assert recovered["owner_session_id"] == "owner-session"
    assert recovered["ring_deadline"] == original_ring_deadline
    assert recovered["version"] == original_version
    assert busy["status"] == "busy"


def test_coordinator_owner_heartbeat_refreshes_only_owner_liveness(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)
    before = persisted_call(database, call_id)
    actions_before = action_rows(database, call_id)

    clock.advance(10)
    refreshed = service.terminal_heartbeat(
        call_id, "desktop-registered", "owner-session"
    )
    after = persisted_call(database, call_id)
    actions_after = action_rows(database, call_id)

    assert refreshed["success"] is True
    assert refreshed["status"] == "accepted"
    assert refreshed["call_state"] == "accepted"
    assert after["terminal_last_seen_at"] == clock.now_utc().isoformat()
    assert after["terminal_disconnect_deadline"] == (clock.now_utc() + timedelta(seconds=180)).isoformat()
    assert after["owner_device_id"] == before["owner_device_id"] == "desktop-registered"
    assert after["owner_session_id"] == before["owner_session_id"] == "owner-session"
    assert after["ring_deadline"] == before["ring_deadline"]
    assert after["version"] == before["version"]
    assert actions_after == actions_before


def test_terminal_disconnect_settles_once_releases_busy_and_cannot_be_revived(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "request-settlement.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service)
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)
    clock.advance(180)

    settled = service.get_call(call_id)
    assert settled["status"] == "disconnected"
    assert settled["call_state"] == "disconnected"
    assert settled["reason"] == "terminal_disconnected"
    assert settled["human_action"] is False
    assert settled["event_seq"] == 2
    repeated = service.recover_and_settle()
    late_heartbeat = service.terminal_heartbeat(call_id, "desktop-registered", "owner-session")
    next_call = create_desktop_call(service, request_id="after-terminal-disconnect")
    events = service.events_for(call_id)
    service.close()

    assert repeated == []
    assert late_heartbeat == settled
    assert [event["event_seq"] for event in events] == [1, 2]
    assert events[-1] == settled
    assert next_call["status"] == "ringing"

    recovery_database = tmp_path / "recovery-settlement.sqlite3"
    before_deadline = CallCoordinator(recovery_database, clock=clock)
    recovery_created = create_desktop_call(before_deadline, request_id="recovery")
    accept_desktop_call(before_deadline, recovery_created["call_id"], session_id="recovery-session")
    before_deadline.close()
    clock.advance(179)
    reopened = CallCoordinator(recovery_database, clock=clock)
    assert reopened.recover_and_settle() == []
    assert reopened.get_call(recovery_created["call_id"])["call_state"] == "accepted"
    reopened.close()
    clock.advance(1)
    expired_reopen = CallCoordinator(recovery_database, clock=clock)
    recovered = expired_reopen.recover_and_settle()
    assert len(recovered) == 1
    assert recovered[0]["status"] == "disconnected"
    assert recovered[0]["reason"] == "terminal_disconnected"
    assert expired_reopen.recover_and_settle() == []
    assert len(expired_reopen.events_for(recovery_created["call_id"])) == 2
    expired_reopen.close()


def test_tls_owner_heartbeat_is_session_bound_nonleaking_and_nonmutating_on_denial(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "heartbeat-create")
        call_id = created["call_id"]
        accepted_status, accepted = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/accept",
            "desktop-token",
            {"action_id": "heartbeat-accept", "owner_session_id": "owner-session"},
        )
        before = persisted_call(database, call_id)
        actions_before = action_rows(database, call_id)
        valid = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/heartbeat",
            "desktop-token",
            {"owner_session_id": "owner-session"},
        )
        after_valid = persisted_call(database, call_id)
        actions_after_valid = action_rows(database, call_id)
        wrong_route = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/heartbeat",
            "other-desktop-token",
            {"owner_session_id": "owner-session"},
        )
        unknown = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/not-a-real-call/heartbeat",
            "other-desktop-token",
            {"owner_session_id": "owner-session"},
        )
        wrong_session = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/heartbeat",
            "desktop-token",
            {"owner_session_id": "not-the-owner"},
        )
        after_denials = persisted_call(database, call_id)
        actions_after_denials = action_rows(database, call_id)
    finally:
        _stop(process)

    assert accepted_status == 200
    assert accepted["status"] == "opened"
    assert valid[0] == 200
    assert valid[1]["status"] == "accepted"
    assert wrong_route == unknown == (404, {"error": "not found"})
    assert wrong_session == (
        409,
        {"success": False, "status": "ownership_conflict", "needs_continuation": False},
    )
    assert after_valid["owner_device_id"] == before["owner_device_id"] == "desktop-registered"
    assert after_valid["owner_session_id"] == before["owner_session_id"] == "owner-session"
    assert after_valid["ring_deadline"] == before["ring_deadline"]
    assert after_valid["version"] == before["version"]
    assert actions_after_valid == actions_before
    assert after_denials == after_valid
    assert actions_after_denials == actions_after_valid
