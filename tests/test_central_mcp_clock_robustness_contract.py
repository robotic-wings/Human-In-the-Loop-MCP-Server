"""Red contract for clock robustness (SPEC §10.1).

Waiting must be computed from a monotonic clock so a manual wall-clock change
can never extend an unanswered invitation, a separate wall-clock timestamp must
be persisted for restart recovery, and a significant clock anomaly must
terminate the affected call with a clear reason.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from central_mcp_waiting_lifecycle import create_app


class RewindClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds

    def rewind(self, seconds: float) -> None:
        self.now -= seconds


async def no_wait(_: float) -> None:
    return None


def make_app(tmp_path: Path, clock: RewindClock) -> Any:
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret"},
        clock=clock, notify=lambda _event: None, wait=no_wait,
        ring_timeout_seconds=60,
    )


def test_clock_rewind_does_not_extend_ring_deadline(tmp_path: Path) -> None:
    clock = RewindClock()
    app = make_app(tmp_path, clock)
    ringing = app.lifecycle.create("agent-a", "r1", {"tool": "text"})

    clock.advance(60)
    clock.rewind(100)
    result = app.lifecycle.continue_call("agent-a", "r2", ringing["interaction_id"], ringing["call_id"], 0)

    assert result["status"] == "not_accepted"
    assert result["reason"] in {"ring_timeout", "clock_anomaly"}


def test_wall_clock_timestamp_is_persisted_for_restart_recovery(tmp_path: Path) -> None:
    clock = RewindClock()
    app = make_app(tmp_path, clock)
    app.lifecycle.create("agent-a", "r1", {"tool": "text"})

    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(lifecycle_calls)")}
    assert "created_at" in columns


def test_significant_clock_anomaly_terminates_call_with_clear_reason(tmp_path: Path) -> None:
    clock = RewindClock()
    app = make_app(tmp_path, clock)
    ringing = app.lifecycle.create("agent-a", "r1", {"tool": "text"})

    clock.advance(10 * 365 * 24 * 3600)
    result = app.lifecycle.continue_call("agent-a", "r2", ringing["interaction_id"], ringing["call_id"], 0)

    assert result["status"] == "not_accepted"
    assert result["reason"] == "clock_anomaly"
