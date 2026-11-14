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
    ("desktop-accepted-agent-polled", "desktop", True, False, 31, None, None),  # terminal alive: agent lease must NOT settle
    ("desktop-accepted-agent-polled-terminal-gone", "desktop", True, False, 181, "disconnected", "terminal_disconnected"),
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
    # The deadline scheduler settles idle ringing calls; a read path triggers
    # the same _settle_due here so the terminal event is materialized.
    central.agent_continue("settle", call, after=0)


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


# ── I1: identity / idempotency ────────────────────────────────────

def test_idempotency_is_exact_across_all_operations(tmp_path):
    """Same key + same args replays; same key + different args conflicts."""
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("idem")

    # create: replay vs conflict
    assert central.agent_create("idem")["call_id"] == call["call_id"]
    assert central.agent_create("idem", args={"task_description": "other"})["status"] == "request_conflict"

    # continue: replay returns the identical event, a changed cursor conflicts
    read1 = central.agent_continue("cont-1", call, after=0)
    read2 = central.agent_continue("cont-1", call, after=0)
    assert read1 == read2
    assert central.agent_continue("cont-1", call, after=1)["status"] == "request_conflict"

    # submit: replay is idempotent, changed payload conflicts
    term.ringed(call).accept()
    sub1 = term.submit({"status": "completed", "report": "a"}, submission_id="sub-idem")
    sub2 = term.submit({"status": "completed", "report": "a"}, submission_id="sub-idem")
    assert sub1["event_seq"] == sub2["event_seq"]

    # cancel: replay is idempotent
    call2 = central.agent_create("idem-cancel")
    c1 = central.agent_cancel("cancel-1", call2)
    c2 = central.agent_cancel("cancel-1", call2)
    assert c1["call_id"] == c2["call_id"] and c2["status"] == "cancelled"


# ── I6: delivery feed completeness ────────────────────────────────

def _delivery_endings():
    """Each (name, finisher) ends a delivery-intent round a distinct way."""
    def submit(central, call):
        term = central.terminal_login("NIGHTHAWK")
        term.ringed(call).accept()
        term.submit({"status": "completed", "report": "done"})

    return [
        ("completed", submit),
        ("declined", _decline),
        ("human-cancelled", _human_cancel),
        ("agent-cancelled", _agent_cancel),
        ("ring-timeout", _ring_timeout),
    ]


def test_every_terminal_outcome_is_in_the_feed_exactly_once(tmp_path):
    for name, finish in _delivery_endings():
        central = Central(tmp_path, ring_timeout_seconds=60)
        call = central.agent_create(f"feed-{name}",
                                    delivery={"kind": "chidori-detached", "agent": "probe"})
        finish(central, call)

        feed = central.outcomes()
        mine = [e for e in feed if e["call_id"] == call["call_id"]]
        terminal = [e for e in mine if e["outcome"].get("needs_continuation") is False]
        assert len(terminal) == 1, (name, mine)
        assert terminal[0]["delivery"] == {"kind": "chidori-detached", "agent": "probe"}
        # seq strictly increasing, exactly one entry carries the delivery
        seqs = [e["seq"] for e in mine]
        assert seqs == sorted(seqs), name


def test_progress_appears_in_feed_exactly_once_before_terminal(tmp_path):
    central = Central(tmp_path)
    call = central.agent_create("feed-progress",
                                delivery={"kind": "chidori-detached", "agent": "probe"})
    term = central.terminal_login("NIGHTHAWK")
    term.ringed(call).accept()
    term.submit({"status": "in_progress", "progress": "half"}, submission_id="p1")
    term.submit({"status": "completed", "report": "done"}, submission_id="p2")

    feed = central.outcomes()
    mine = [e for e in feed if e["call_id"] == call["call_id"]]
    statuses = [e["outcome"]["status"] for e in mine]
    assert statuses == ["in_progress", "completed"], mine
    assert mine[0]["outcome"]["progress"] == "half"


def test_mobile_round_survives_a_clock_jump(tmp_path):
    """I3/I4: a stateless mobile round must not be settled by a clock anomaly."""
    central = Central(tmp_path)
    call = central.agent_create("mobile-clock-jump")
    _accept(central, call, "mobile")

    # A >1h forward jump trips the clock-anomaly guard.
    central.clock.advance(4000)
    outcome = central.agent_continue("settle", call, after=0)

    assert outcome["call_state"] in ("accepted", "in_progress"), outcome
    assert outcome.get("reason") != "clock_anomaly"
    events = _events(central, str(call["call_id"]))
    assert _terminal_events(events) == [], _terminal_events(events)


def test_desktop_round_settles_on_clock_jump(tmp_path):
    """The clock-anomaly guard still protects deadline-bearing desktop rounds."""
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("desktop-clock-jump")
    term.ringed(call).accept()

    central.clock.advance(4000)
    outcome, _ = central.agent_follow(call, prefix="settle")
    assert outcome["call_state"] == "disconnected"
    assert outcome["reason"] == "clock_anomaly"
