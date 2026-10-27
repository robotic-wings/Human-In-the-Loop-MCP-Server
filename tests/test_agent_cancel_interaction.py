"""Red contracts for explicit original-caller ``cancel_interaction`` support."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from call_coordinator import CallCoordinator, CreateCall
from test_central_mcp_gateway import (
    _call,
    _free_port,
    _post,
    _rpc,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
)
from test_mcp_continuation import _continuation


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _new_call(service: CallCoordinator, *, request_id: str = "create") -> dict[str, Any]:
    return service.create_call(
        CreateCall(
            "operator-default", "agent-alpha", f"interaction-{request_id}", request_id,
            "cancel me", 60, "desktop-registered",
        )
    )


def _accepted_call(service: CallCoordinator, *, request_id: str = "accepted") -> dict[str, Any]:
    created = _new_call(service, request_id=request_id)
    accepted = service.terminal_action(
        "accept", created["call_id"], "desktop-registered", f"accept-{request_id}",
        "owner-session", "windows",
    )
    assert accepted is not None and accepted["status"] == "opened"
    return created


def _event_rows(database: Path, call_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def _cancel_rpc(interaction_id: str, call_id: str, request_id: str) -> dict[str, Any]:
    return _rpc(
        "tools/call",
        {
            "name": "cancel_interaction",
            "arguments": {
                "interaction_id": interaction_id,
                "call_id": call_id,
                "request_id": request_id,
            },
        },
        41,
    )


def test_original_caller_cancels_ringing_and_accepted_calls_once_releases_busy_and_has_no_answer_fields(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 10, 26, tzinfo=UTC))
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    ringing = _new_call(service, request_id="ringing")
    cancelled_ringing = service.cancel_interaction(
        "agent-alpha", ringing["interaction_id"], ringing["call_id"], "cancel-ringing"
    )
    replacement = _accepted_call(service, request_id="accepted")
    accepted_events_before = service.events_for(replacement["call_id"])
    cancelled_accepted = service.cancel_interaction(
        "agent-alpha", replacement["interaction_id"], replacement["call_id"], "cancel-accepted"
    )
    accepted_events_after = service.events_for(replacement["call_id"])
    after = _new_call(service, request_id="after-cancel")
    service.close()

    for cancelled in (cancelled_ringing, cancelled_accepted):
        assert cancelled["success"] is True
        assert cancelled["status"] == cancelled["call_state"] == "cancelled"
        assert cancelled["reason"] == "agent_cancelled"
        assert cancelled["human_action"] is False
        assert cancelled["needs_continuation"] is False
        for field in ("user_input", "selected_choices", "confirmed", "acknowledged", "attachments"):
            assert field not in cancelled
    assert cancelled_ringing["event_seq"] == 1
    assert cancelled_accepted["event_seq"] == 2
    assert [event["event_seq"] for event in accepted_events_before] == [1]
    assert accepted_events_after[-1] == cancelled_accepted
    assert len(accepted_events_after) == 2
    assert after["status"] == "ringing"


def test_agent_cancel_replays_after_restart_conflicts_on_changed_content_and_cannot_target_other_caller(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 10, 26, tzinfo=UTC))
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = _new_call(service, request_id="durable")
    first = service.cancel_interaction(
        "agent-alpha", created["interaction_id"], created["call_id"], "stable-cancel"
    )
    events_before = _event_rows(database, created["call_id"])
    service.close()

    reopened = CallCoordinator(database, clock=clock)
    replay = reopened.cancel_interaction(
        "agent-alpha", created["interaction_id"], created["call_id"], "stable-cancel"
    )
    conflict = reopened.cancel_interaction(
        "agent-alpha", "different-interaction", created["call_id"], "stable-cancel"
    )
    second = reopened.create_call(
        CreateCall("operator-default", "agent-beta", "beta-interaction", "beta-create", "other", 60)
    )
    wrong_caller = reopened.cancel_interaction(
        "agent-alpha", second["interaction_id"], second["call_id"], "wrong-caller"
    )
    unknown = reopened.cancel_interaction(
        "agent-alpha", "unknown", "not-a-real-call", "unknown-cancel"
    )
    events_after = _event_rows(database, created["call_id"])
    reopened.close()

    assert replay == first
    assert conflict == {"success": False, "status": "request_conflict", "needs_continuation": False}
    assert wrong_caller == unknown
    assert wrong_caller["status"] == "session_not_found"
    assert events_after == events_before


def test_agent_cancel_settles_expiry_before_mutation_and_never_revives_a_call(tmp_path: Path) -> None:
    clock = FakeClock(datetime(2026, 10, 26, tzinfo=UTC))
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    ringing = _new_call(service, request_id="expired-ringing")
    clock.advance(60)
    late_ringing_cancel = service.cancel_interaction(
        "agent-alpha", ringing["interaction_id"], ringing["call_id"], "late-ringing"
    )
    accepted = _accepted_call(service, request_id="expired-accepted")
    clock.advance(180)
    late_accepted_cancel = service.cancel_interaction(
        "agent-alpha", accepted["interaction_id"], accepted["call_id"], "late-accepted"
    )
    service.close()

    assert late_ringing_cancel["status"] == "not_accepted"
    assert late_ringing_cancel["reason"] == "ring_timeout"
    assert late_accepted_cancel["status"] == "disconnected"
    assert late_accepted_cancel["reason"] == "terminal_disconnected"


def test_tls_mcp_lists_and_executes_original_caller_cancel_without_leaking_other_calls(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        status, listed, _ = _post(port, certificate, _rpc("tools/list", {}), "test-token-alpha")
        assert status == 200
        assert "cancel_interaction" in {tool["name"] for tool in listed["result"]["tools"]}
        status, created_response, _ = _post(port, certificate, _call("mcp-create"), "test-token-alpha")
        assert status == 200
        created = _structured(created_response)
        valid_status, valid, _ = _post(
            port, certificate,
            _cancel_rpc(created["interaction_id"], created["call_id"], "mcp-cancel"),
            "test-token-alpha",
        )
        continuation_status, continuation, _ = _post(
            port,
            certificate,
            _continuation("cancel-read", created["interaction_id"], created["call_id"], 0),
            "test-token-alpha",
        )
        post_cancel_status, post_cancel, _ = _post(port, certificate, _call("mcp-after"), "test-token-beta")
        wrong_status, wrong, _ = _post(
            port, certificate,
            _cancel_rpc(created["interaction_id"], created["call_id"], "wrong-caller"),
            "test-token-beta",
        )
        unknown_status, unknown, _ = _post(
            port, certificate, _cancel_rpc("unknown", "not-a-real-call", "unknown-cancel"), "test-token-beta"
        )
    finally:
        _stop(process)

    cancelled = _structured(valid)
    assert valid_status == 200
    assert cancelled["status"] == "cancelled"
    assert cancelled["reason"] == "agent_cancelled"
    assert continuation_status == 200
    assert _structured(continuation) == cancelled
    assert post_cancel_status == 200
    assert _structured(post_cancel)["status"] == "ringing"
    assert wrong_status == unknown_status == 200
    assert wrong == unknown
    assert wrong["error"] == {"code": -32602, "message": "invalid params"}


def test_tls_mcp_rejects_invalid_cancel_request_ids_without_mutating_the_exact_call(
    tmp_path: Path,
) -> None:
    """Cancellation identifiers have the same bounded ASCII contract as other MCP operations."""
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        status, created_response, _ = _post(port, certificate, _call("bounded-cancel"), "test-token-alpha")
        assert status == 200
        created = _structured(created_response)
        call_id = created["call_id"]
        interaction_id = created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        invalid_results = []
        for request_id in ("x" * 129, "non-ascii-\u00e9"):
            status, response, _ = _post(
                port, certificate, _cancel_rpc(interaction_id, call_id, request_id), "test-token-alpha"
            )
            invalid_results.append((status, response))
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
            operations = connection.execute(
                "SELECT count(*) FROM cancel_operations WHERE caller_id = ?", ("agent-alpha",)
            ).fetchone()
            events = connection.execute(
                "SELECT event_seq FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
    finally:
        _stop(process)

    assert all(status == 200 for status, _ in invalid_results)
    assert all(response["error"] == {"code": -32602, "message": "invalid params"} for _, response in invalid_results)
    assert state == ("ringing",)
    assert operations == (0,)
    assert events == []
