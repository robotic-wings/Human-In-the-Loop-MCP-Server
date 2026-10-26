"""Red contracts for durable agent continuation/reconnect expiry.

This first 8ca slice deliberately covers only caller continuation state.  It
does not exercise terminal liveness, progress grace, routing, or UI behavior.
"""

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


def _clock() -> FakeClock:
    return FakeClock(datetime(2030, 1, 1, tzinfo=UTC))


def _create(request_id: str = "create") -> CreateCall:
    return CreateCall(
        operator_id="operator-default",
        caller_id="agent-alpha",
        interaction_id="interaction-alpha",
        request_id=request_id,
        message="Keep this continuation durable.",
        ring_timeout_seconds=60,
        desktop_route_device_id="desktop-registered",
    )


def _agent_deadline(database: Path, call_id: str) -> str | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT agent_reconnect_deadline FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
    assert row is not None
    return row[0]


def test_after_event_cursor_delivers_opened_once_then_heartbeats_at_confirmed_cursor(
    tmp_path: Path,
) -> None:
    clock = _clock()
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    created = service.create_call(_create())
    opened = service.terminal_action(
        "accept",
        created["call_id"],
        "desktop-registered",
        "accept",
        "owner-session",
        "windows",
    )
    assert opened is not None and opened["status"] == "opened"

    first = service.continue_call(
        "agent-alpha", "opened-read", "interaction-alpha", created["call_id"], 0, "{}"
    )
    confirmed = service.continue_call(
        "agent-alpha", "confirmed-cursor", "interaction-alpha", created["call_id"], 1, "{}"
    )

    assert first == opened
    assert first["event_seq"] == 1
    assert confirmed["status"] == "heartbeat"
    assert confirmed["event_seq"] == 1


def test_opened_event_persists_the_same_30_second_agent_reconnect_grace(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = service.create_call(_create())
    opened = service.terminal_action(
        "accept",
        created["call_id"],
        "desktop-registered",
        "accept",
        "owner-session",
        "windows",
    )
    assert opened is not None and opened["status"] == "opened"

    delivered = service.continue_call(
        "agent-alpha", "opened-read", "interaction-alpha", created["call_id"], 0, "{}"
    )

    assert delivered == opened
    assert _agent_deadline(database, created["call_id"]) == "2030-01-01T00:00:30+00:00"


def test_heartbeat_deadline_is_durable_replay_safe_and_new_continuation_starts_fresh_leg(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = service.create_call(_create())
    call_id = created["call_id"]

    first = service.continue_call(
        "agent-alpha", "first-heartbeat", "interaction-alpha", call_id, 0, "{}"
    )
    first_deadline = _agent_deadline(database, call_id)
    clock.advance(10)
    replay = service.continue_call(
        "agent-alpha", "first-heartbeat", "interaction-alpha", call_id, 0, "{}"
    )
    replay_deadline = _agent_deadline(database, call_id)
    next_leg = service.continue_call(
        "agent-alpha", "next-leg", "interaction-alpha", call_id, 0, "{}"
    )
    next_deadline = _agent_deadline(database, call_id)

    assert first["status"] == next_leg["status"] == "heartbeat"
    assert replay == first
    assert first_deadline == replay_deadline
    assert first_deadline == "2030-01-01T00:00:30+00:00"
    assert next_deadline == "2030-01-01T00:00:40+00:00"


def test_late_continuation_settles_agent_disconnect_releases_busy_and_never_revives(
    tmp_path: Path,
) -> None:
    clock = _clock()
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    created = service.create_call(_create())
    call_id = created["call_id"]
    service.continue_call(
        "agent-alpha", "first-heartbeat", "interaction-alpha", call_id, 0, "{}"
    )
    clock.advance(31)

    late = service.continue_call(
        "agent-alpha", "late", "interaction-alpha", call_id, 0, "{}"
    )
    replacement = service.create_call(_create("replacement"))
    stale = service.continue_call(
        "agent-alpha", "stale", "interaction-alpha", call_id, 0, "{}"
    )

    assert late["status"] == late["call_state"] == "disconnected"
    assert late["reason"] == "agent_disconnected"
    assert replacement["status"] == "ringing"
    assert stale == late


def test_restart_preserves_agent_reconnect_deadline_and_late_continuation_settles_it(
    tmp_path: Path,
) -> None:
    clock = _clock()
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = service.create_call(_create())
    call_id = created["call_id"]
    service.continue_call(
        "agent-alpha", "first-heartbeat", "interaction-alpha", call_id, 0, "{}"
    )
    persisted_deadline = _agent_deadline(database, call_id)
    service.close()
    clock.advance(31)

    reopened = CallCoordinator(database, clock=clock)
    assert _agent_deadline(database, call_id) == persisted_deadline
    late = reopened.continue_call(
        "agent-alpha", "late-after-restart", "interaction-alpha", call_id, 0, "{}"
    )

    assert late["status"] == "disconnected"
    assert late["reason"] == "agent_disconnected"


def test_tls_heartbeat_persists_agent_reconnect_deadline_in_sqlite(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, created_response, _ = _post(
            port, certificate, _call("create"), "test-token-alpha"
        )
        created = _structured(created_response)
        _, continuation_response, _ = _post(
            port,
            certificate,
            _continuation("heartbeat", created["interaction_id"], created["call_id"], 0),
            "test-token-alpha",
        )
        heartbeat = _structured(continuation_response)
        deadline = _agent_deadline(tmp_path / "data" / "calls.sqlite3", created["call_id"])
    finally:
        _stop(process)

    assert heartbeat["status"] == "heartbeat"
    assert deadline is not None
