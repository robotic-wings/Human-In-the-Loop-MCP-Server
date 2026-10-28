"""Red contracts for bounded, owner-session-scoped terminal drafts.

This slice deliberately treats a draft body as opaque text.  It neither parses
the body nor creates a submission, result, attachment, task update, callback,
or UI workflow.
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


# The narrow canonical draft format is deliberately only opaque body + revision.
# 256 UTF-8 bytes is a modest protocol cap for this first persistence slice.
DRAFT_MAX_UTF8_BYTES = 256


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def make_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 26, tzinfo=UTC))


def create_accepted_desktop_call(service: CallCoordinator, request_id: str = "draft") -> dict[str, Any]:
    created = service.create_call(
        CreateCall(
            "operator-1", "agent-1", f"interaction-{request_id}", request_id,
            "draft persistence", 60, "desktop-registered",
        )
    )
    accepted = service.terminal_action(
        "accept", created["call_id"], "desktop-registered", f"accept-{request_id}",
        "owner-session", "windows",
    )
    assert accepted is not None and accepted["status"] == "opened"
    return created


def call_row(database: Path, call_id: str) -> dict[str, Any]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    assert row is not None
    return dict(row)


def test_owner_session_can_save_and_compare_and_save_canonical_opaque_draft(
    tmp_path: Path,
) -> None:
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=make_clock())
    created = create_accepted_desktop_call(service)
    call_id = created["call_id"]

    first = service.save_terminal_draft(
        call_id, "desktop-registered", "owner-session", "first opaque body", 0
    )
    readable = service.read_terminal_draft(call_id, "desktop-registered", "owner-session")
    stale = service.save_terminal_draft(
        call_id, "desktop-registered", "owner-session", "overwritten body", 0
    )
    retained = service.read_terminal_draft(call_id, "desktop-registered", "owner-session")
    second = service.save_terminal_draft(
        call_id, "desktop-registered", "owner-session", "second opaque body", 1
    )
    too_large = service.save_terminal_draft(
        call_id, "desktop-registered", "owner-session", "x" * (DRAFT_MAX_UTF8_BYTES + 1), 2
    )
    service.close()

    assert first == {"success": True, "status": "saved", "draft": {"body": "first opaque body", "draft_version": 1}}
    assert readable == first["draft"]
    assert stale == {"success": False, "status": "draft_version_conflict", "draft_version": 1}
    assert retained == first["draft"]
    assert second == {"success": True, "status": "saved", "draft": {"body": "second opaque body", "draft_version": 2}}
    assert too_large == {"success": False, "status": "invalid_draft"}


def test_draft_is_owner_session_and_route_scoped_and_expiry_never_revives_it(tmp_path: Path) -> None:
    clock = make_clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_accepted_desktop_call(service, "scoped")
    call_id = created["call_id"]
    saved = service.save_terminal_draft(call_id, "desktop-registered", "owner-session", "secret draft", 0)

    wrong_device_read = service.read_terminal_draft(call_id, "desktop-other", "owner-session")
    wrong_session_read = service.read_terminal_draft(call_id, "desktop-registered", "other-session")
    wrong_route_write = service.save_terminal_draft(call_id, "desktop-other", "owner-session", "leak", 1)
    wrong_session_write = service.save_terminal_draft(call_id, "desktop-registered", "other-session", "leak", 1)
    clock.advance(180)
    expired_read = service.read_terminal_draft(call_id, "desktop-registered", "owner-session")
    expired_write = service.save_terminal_draft(call_id, "desktop-registered", "owner-session", "revive", 1)
    settled = service.get_call(call_id)
    service.close()

    assert saved["success"] is True
    assert wrong_device_read is None
    assert wrong_session_read is None
    assert wrong_route_write is None
    assert wrong_session_write == {"success": False, "status": "ownership_conflict"}
    assert expired_read is None
    assert expired_write["status"] == "disconnected"
    assert settled["status"] == "disconnected"
    assert settled["reason"] == "terminal_disconnected"


def test_only_valid_save_refreshes_owner_liveness_and_preserves_call_event_and_busy_state(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_accepted_desktop_call(service, "liveness")
    call_id = created["call_id"]
    before = call_row(database, call_id)
    events_before = service.events_for(call_id)

    clock.advance(10)
    unauthorized_read = service.read_terminal_draft(call_id, "desktop-registered", "wrong-session")
    device_presence = service.register_terminal_presence(
        device_id="desktop-registered", terminal_platform="windows", route_device_id="desktop-registered",
        callback_url=None, instance_id="draft-device", gui_ready=True,
    )
    device_heartbeat = service.terminal_device_heartbeat("desktop-registered", "draft-device")
    state = service.terminal_state("desktop-registered", call_id)
    after_non_saves = call_row(database, call_id)
    saved = service.save_terminal_draft(call_id, "desktop-registered", "owner-session", "alive", 0)
    after_save = call_row(database, call_id)
    busy = service.create_call(
        CreateCall("operator-1", "agent-2", "interaction-busy", "draft-busy", "other", 60, "desktop-registered")
    )
    events_after = service.events_for(call_id)
    service.close()

    assert unauthorized_read is None
    assert device_presence["online"] is True and device_heartbeat["online"] is True
    assert state is not None and state["call_state"] == "accepted"
    assert after_non_saves["terminal_last_seen_at"] == before["terminal_last_seen_at"]
    assert after_non_saves["terminal_disconnect_deadline"] == before["terminal_disconnect_deadline"]
    assert saved["success"] is True
    assert after_save["terminal_last_seen_at"] == clock.now_utc().isoformat()
    assert after_save["terminal_disconnect_deadline"] == (clock.now_utc() + timedelta(seconds=180)).isoformat()
    assert after_save["version"] == before["version"]
    assert events_after == events_before
    assert busy["status"] == "busy"


def test_live_draft_survives_restart_only_for_original_owner_session(tmp_path: Path) -> None:
    clock = make_clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_accepted_desktop_call(service, "restart")
    call_id = created["call_id"]
    service.save_terminal_draft(call_id, "desktop-registered", "owner-session", "persist me", 0)
    service.close()

    clock.advance(179)
    reopened = CallCoordinator(database, clock=clock)
    original = reopened.read_terminal_draft(call_id, "desktop-registered", "owner-session")
    other_device = reopened.read_terminal_draft(call_id, "desktop-other", "owner-session")
    other_session = reopened.read_terminal_draft(call_id, "desktop-registered", "other-session")
    reopened.close()
    clock.advance(1)
    expired = CallCoordinator(database, clock=clock)
    expired_read = expired.read_terminal_draft(call_id, "desktop-registered", "owner-session")
    result = expired.get_call(call_id)
    expired.close()

    assert original == {"body": "persist me", "draft_version": 1}
    assert other_device is None
    assert other_session is None
    assert expired_read is None
    assert result["status"] == "disconnected"
    assert result["reason"] == "terminal_disconnected"


def test_tls_put_draft_requires_the_accepted_owner_session_and_returns_canonical_draft(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "draft-http")
        call_id = created["call_id"]
        accepted_status, _ = _terminal_request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", "desktop-token",
            {"action_id": "draft-http-accept", "owner_session_id": "draft-http-owner"},
        )
        saved = _terminal_request(
            port, certificate, "PUT", f"/api/calls/{call_id}/draft", "desktop-token",
            {"owner_session_id": "draft-http-owner", "body": "HTTP opaque", "expected_draft_version": 0},
        )
        wrong_session = _terminal_request(
            port, certificate, "PUT", f"/api/calls/{call_id}/draft", "desktop-token",
            {"owner_session_id": "other-owner", "body": "leak", "expected_draft_version": 1},
        )
        wrong_route = _terminal_request(
            port, certificate, "PUT", f"/api/calls/{call_id}/draft", "other-desktop-token",
            {"owner_session_id": "draft-http-owner", "body": "leak", "expected_draft_version": 1},
        )
    finally:
        _stop(process)

    assert accepted_status == 200
    assert saved == (200, {"success": True, "status": "saved", "draft": {"body": "HTTP opaque", "draft_version": 1}})
    assert wrong_session == (409, {"success": False, "status": "ownership_conflict"})
    assert wrong_route == (404, {"error": "not found"})
