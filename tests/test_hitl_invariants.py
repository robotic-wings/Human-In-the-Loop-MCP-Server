"""Property tests for the HITL invariant model (SPEC-invariants.md).

These are NOT single-case tests. Each one asserts an invariant holds across a
systematic sweep of the state space — every reachable round shape, every way a
round can end, every clock position — so a future "missing case" fails loudly
here instead of surfacing in production.

I4 settlement completeness/determinism and I5 busy-line are new; the other
invariants are pinned by the rewrite-spec and attachment-continuation suites.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from tests.test_hitl_functional_integration import Central


def _events(central: Central, call_id: str) -> list[dict[str, object]]:
    with sqlite3.connect(central.path / "lifecycle.sqlite3") as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq", (call_id,)
        ).fetchall()
    return [json.loads(r["body_json"]) for r in rows]


def _terminal_events(events: list[dict[str, object]]) -> list[dict[str, object]]:
    return [e for e in events if e.get("needs_continuation") is False]


def _assert_exactly_one_terminal(central: Central, call: dict[str, object]) -> dict[str, object]:
    events = _events(central, str(call["call_id"]))
    terminals = _terminal_events(events)
    assert len(terminals) == 1, f"expected exactly one terminal event, got {terminals}"
    assert terminals[0]["event_seq"] == events[-1]["event_seq"], "terminal event must be the last"
    return terminals[0]


def _accept(central: Central, call: dict[str, object], channel: str):
    if channel == "desktop":
        term = central.terminal_login("NIGHTHAWK")
        term.ringed(call)
        term.accept()
        return term
    central.request("POST", f"/calls/{call['call_id']}/accept",
                    browser="phone-night", body={"operation_id": "acc"})
    return None


def _progress(central: Central, call: dict[str, object], channel: str, term) -> None:
    if channel == "desktop":
        term.submit({"status": "in_progress", "progress": "half"}, submission_id="prog")
    else:
        central.request("POST", f"/calls/{call['call_id']}/submissions",
                        browser="phone-night",
                        body={"submission_id": "prog",
                              "result": {"status": "in_progress", "progress": "half"}})


def _agent_poll_once(central: Central, call: dict[str, object]) -> None:
    central.request("POST", "/mcp", bearer="agent-secret", body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "assign_task_to_human",
                   "arguments": {"interaction_id": call["interaction_id"], "call_id": call["call_id"],
                                 "after_event_seq": 0, "client_timeout_seconds": 5}},
    })


# ── I4: settlement completeness & determinism ─────────────────────

SETTLEMENT_CASES = [
    # name, accept channel, agent polled, progress submitted, advance, expected reason
    ("ringing-unanswered", None, False, False, 400, "not_accepted", "ring_timeout"),
    ("desktop-accepted-no-agent", "desktop", False, False, 181, "disconnected", "terminal_disconnected"),
    ("mobile-accepted-no-agent", "mobile", False, False, 400, None, None),  # stays active
    ("desktop-accepted-agent-polled", "desktop", True, False, 31, "disconnected", "agent_disconnected"),
    ("mobile-accepted-agent-polled", "mobile", True, False, 31, "disconnected", "agent_disconnected"),
    ("desktop-in-progress", "desktop", False, True, 181, "disconnected", "terminal_disconnected"),
    ("mobile-in-progress", "mobile", False, True, 400, None, None),  # no lease, stays active
]


@pytest.mark.parametrize(
    "name,channel,polled,progress,advance,expected_state,expected_reason",
    SETTLEMENT_CASES,
)
def test_settlement_is_complete_and_deterministic(tmp_path, name, channel, polled, progress,
                                                  advance, expected_state, expected_reason):
    central = Central(tmp_path, ring_timeout_seconds=60, agent_reconnect_seconds=30)
    call = central.agent_create(f"inv-{name}")

    term = None
    if channel is not None:
        term = _accept(central, call, channel)
    if progress:
        _progress(central, call, channel, term)
    if polled:
        _agent_poll_once(central, call)

    central.clock.advance(advance)
    outcome = central.agent_continue(f"read-{name}", call, after=0)

    if expected_state is None:
        # The round is legitimately still active: no settlement must have happened.
        assert outcome["call_state"] in ("accepted", "in_progress"), outcome
        events = _events(central, str(call["call_id"]))
        assert _terminal_events(events) == [], f"{name} must not settle, got {_terminal_events(events)}"
        return

    terminal = _assert_exactly_one_terminal(central, call)
    assert terminal["call_state"] == expected_state, (name, terminal)
    assert terminal["reason"] == expected_reason, (name, terminal)


def test_mobile_round_never_settles_terminal_disconnected(tmp_path):
    """I3: the mobile channel has no owner lease — at ANY clock position."""
    central = Central(tmp_path)
    call = central.agent_create("mobile-lease")
    _accept(central, call, "mobile")

    for _ in range(10):
        central.clock.advance(600)
        outcome = central.agent_continue("poll", call, after=0)
        assert outcome.get("reason") != "terminal_disconnected", outcome
        events = _events(central, str(call["call_id"]))
        reasons = [e.get("reason") for e in _terminal_events(events)]
        assert "terminal_disconnected" not in reasons, reasons


# ── I5: busy line ─────────────────────────────────────────────────

def _decline(central, call):
    central.terminal_login("NIGHTHAWK").ringed(call).decline()


def _ring_timeout(central, call):
    central.clock.advance(61)


def _agent_cancel(central, call):
    central.agent_cancel("cancel", call)


def _human_cancel(central, call):
    term = central.terminal_login("NIGHTHAWK")
    term.ringed(call).accept()
    term.cancel()


def _submit(central, call):
    term = central.terminal_login("NIGHTHAWK")
    term.ringed(call).accept()
    term.submit({"status": "completed", "report": "done"})


ENDINGS = [
    ("decline", _decline),
    ("ring-timeout", _ring_timeout),
    ("agent-cancel", _agent_cancel),
    ("human-cancel", _human_cancel),
    ("submit", _submit),
]


@pytest.mark.parametrize("name,finish", ENDINGS)
def test_busy_line_released_by_every_ending(tmp_path, name, finish):
    central = Central(tmp_path, ring_timeout_seconds=60)
    first = central.agent_create(f"busy-{name}")
    assert central.agent_create(f"busy-{name}-2")["status"] == "busy"  # line held
    finish(central, first)
    # line released: a fresh create succeeds
    assert central.agent_create(f"busy-{name}-3")["call_state"] == "ringing"


def test_never_two_active_rounds_per_operator(tmp_path):
    central = Central(tmp_path)
    for i in range(5):
        created = central.agent_create(f"no-double-{i}")
        assert created["call_state"] == "ringing"
        second = central.agent_create(f"no-double-{i}-b")
        assert second["status"] == "busy"
        central.agent_cancel(f"cancel-{i}", created)
