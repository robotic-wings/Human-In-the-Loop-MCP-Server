"""Contract tests for the explicit call state machine and event bus.

The machine is the single authority for ``call_state`` transitions; these
tests pin its transition table, version-bump policy, seq allocation, the
per-waiter event bus (including the cross-event-loop regression), and the
event-driven ``wait_call_outcome`` long-poll that replaced synthesised
continuation requests.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

import pytest


def _machine_module() -> Any:
    return importlib.import_module("central_call_machine")


def _lifecycle_module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self._now

    @property
    def _now(self) -> float:
        return self.now


def _fresh_db(tmp_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(tmp_path / "machine.sqlite3"))
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE lifecycle_calls ("
        "call_id TEXT PRIMARY KEY, interaction_id TEXT NOT NULL, caller_id TEXT NOT NULL,"
        "call_state TEXT NOT NULL, ring_deadline REAL NOT NULL, reason TEXT,"
        "payload_json TEXT NOT NULL DEFAULT '{}', owner_device_id TEXT, owner_session_id TEXT,"
        "handled_on TEXT, terminal_platform TEXT, invited_desktop_id TEXT,"
        "mobile_eligible INTEGER NOT NULL DEFAULT 0, terminal_last_seen_at REAL,"
        "terminal_disconnect_deadline REAL, agent_reconnect_deadline REAL,"
        "version INTEGER NOT NULL DEFAULT 1, draft_content TEXT, draft_version INTEGER NOT NULL DEFAULT 0,"
        "created_at REAL, task_version INTEGER NOT NULL DEFAULT 1)"
    )
    conn.execute(
        "CREATE TABLE lifecycle_events (call_id TEXT NOT NULL, event_seq INTEGER NOT NULL,"
        "body_json TEXT NOT NULL, PRIMARY KEY (call_id, event_seq))"
    )
    conn.execute(
        "INSERT INTO lifecycle_calls (call_id, interaction_id, caller_id, call_state, ring_deadline)"
        " VALUES ('c1', 'i1', 'agent-a', 'ringing', 9e9)"
    )
    conn.commit()
    return conn


def test_legal_transition_updates_state_reason_version_and_seq(tmp_path: Path) -> None:
    machine_mod = _machine_module()
    machine = machine_mod.CallMachine(machine_mod.CallEventBus())

    async def plan() -> None:
        machine_mod.CallEventBus().notify("c1")  # no waiters: harmless

    asyncio.run(plan())
    with _fresh_db(tmp_path) as conn:
        row, seq = machine.transition(conn, "c1", "decline", reason="human_declined")
        assert row["call_state"] == "declined"
        assert row["reason"] == "human_declined"
        assert int(row["version"]) == 2
        assert seq == 1
        stored = conn.execute("SELECT call_state, reason, version FROM lifecycle_calls WHERE call_id='c1'").fetchone()
        assert stored["call_state"] == "declined" and stored["reason"] == "human_declined"
        assert int(stored["version"]) == 2


def test_double_terminal_transition_is_rejected(tmp_path: Path) -> None:
    machine_mod = _machine_module()
    machine = machine_mod.CallMachine(machine_mod.CallEventBus())
    with _fresh_db(tmp_path) as conn:
        machine.transition(conn, "c1", "decline", reason="human_declined")
        with pytest.raises(machine_mod.InvalidTransition):
            machine.transition(conn, "c1", "decline", reason="human_declined")
        with pytest.raises(machine_mod.InvalidTransition):
            machine.transition(conn, "c1", "agent_cancel", reason="again")


def test_accept_from_non_ringing_is_illegal(tmp_path: Path) -> None:
    machine_mod = _machine_module()
    machine = machine_mod.CallMachine(machine_mod.CallEventBus())
    with _fresh_db(tmp_path) as conn:
        conn.execute("UPDATE lifecycle_calls SET call_state='accepted' WHERE call_id='c1'")
        conn.commit()
        with pytest.raises(machine_mod.InvalidTransition):
            machine.transition(conn, "c1", "accept")


def test_submit_target_restriction(tmp_path: Path) -> None:
    machine_mod = _machine_module()
    machine = machine_mod.CallMachine(machine_mod.CallEventBus())
    with _fresh_db(tmp_path) as conn:
        conn.execute("UPDATE lifecycle_calls SET call_state='accepted' WHERE call_id='c1'")
        conn.commit()
        with pytest.raises(machine_mod.InvalidTransition):
            machine.transition(conn, "c1", "submit", target="ringing")
        row, seq = machine.transition(conn, "c1", "submit", target="completed")
        assert row["call_state"] == "completed"
        assert seq == 1


def test_bus_wakes_waiter_on_notify() -> None:
    machine_mod = _machine_module()
    bus = machine_mod.CallEventBus()

    async def plan() -> None:
        wait_task = asyncio.ensure_future(bus.wait_change("c1", 5))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        bus.notify("c1")
        await wait_task
        assert True  # woke instead of timing out

    asyncio.run(plan())


def test_bus_timeout_returns_silently() -> None:
    machine_mod = _machine_module()
    bus = machine_mod.CallEventBus()

    async def plan() -> None:
        await bus.wait_change("missing-call", 0.01)

    asyncio.run(plan())


def test_bus_waiter_does_not_leak_across_event_loops() -> None:
    """Regression: a waiter cancelled on a closed loop must not poison later loops."""
    machine_mod = _machine_module()
    bus = machine_mod.CallEventBus()

    async def abandoned() -> None:
        task = asyncio.ensure_future(bus.wait_change("c1", 5))
        await asyncio.sleep(0)
        task.cancel()

    asyncio.run(abandoned())  # loop opens and closes with a pending waiter

    async def later() -> None:
        await bus.wait_change("c1", 0.05)  # fresh loop: must not raise
        bus.notify("c1")
        await bus.wait_change("c1", 0.05)

    asyncio.run(later())
    assert not bus._waiters


def test_wait_call_outcome_returns_terminal_result_in_one_wait(tmp_path: Path) -> None:
    lifecycle_mod = _lifecycle_module()
    clock = FakeClock()
    notifications: list[dict[str, object]] = []

    async def instant_wait(_seconds: float) -> None:
        return None

    lifecycle = lifecycle_mod.WaitingLifecycle(
        tmp_path / "wait.sqlite3", clock, notifications.append, 60, 30,
    )
    created = lifecycle.create("agent-a", "r1", {"tool": "get_user_input", "prompt": "hi"})
    assert created["status"] == "heartbeat"
    interaction_id, call_id = created["interaction_id"], created["call_id"]

    async def plan() -> None:
        # Real sleeper: the budget genuinely lasts, so the bus wakeup from
        # the human transitions below is what ends this wait.
        wait_task = asyncio.ensure_future(
            lifecycle.wait_call_outcome("agent-a", interaction_id, call_id, 5)
        )
        await asyncio.sleep(0.05)
        accepted = lifecycle._human_action(
            call_id, "opened", "accepted", None,
            {"handled_on": "desktop", "terminal_platform": "windows", "human_action": True},
        )
        assert accepted["status"] == "opened"
        finished = lifecycle.finish(call_id=call_id, status="answered", result={"user_input": "blue pen"})
        assert finished["status"] == "answered"
        outcome = await wait_task
        assert outcome is not None
        assert outcome["status"] == "answered"
        assert outcome["needs_continuation"] is False
        # The legacy finish() path merges the result flat into the event body.
        assert outcome["user_input"] == "blue pen"

    asyncio.run(plan())


def test_wait_call_outcome_budget_expiry_returns_heartbeat(tmp_path: Path) -> None:
    lifecycle_mod = _lifecycle_module()
    clock = FakeClock()

    async def instant_wait(_seconds: float) -> None:
        return None

    lifecycle = lifecycle_mod.WaitingLifecycle(tmp_path / "budget.sqlite3", clock, lambda _n: None, 60, 30)
    created = lifecycle.create("agent-a", "r1", {"tool": "get_user_input", "prompt": "hi"})
    outcome = asyncio.run(
        lifecycle.wait_call_outcome(
            "agent-a", created["interaction_id"], created["call_id"], 5, waiter=instant_wait,
        )
    )
    assert outcome is not None
    assert outcome["status"] == "heartbeat"
    assert outcome["call_state"] == "ringing"
