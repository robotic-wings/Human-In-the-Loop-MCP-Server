"""Explicit call lifecycle state machine and change-notification bus.

This module is the single authority for ``lifecycle_calls.call_state``
transitions.  Every mutation path — desktop accept/decline, iPhone-web
accept/decline, owner submission, agent/human cancellation, deadline
settlement, clock-anomaly recovery and delivery-unreachable decisions —
must route through :meth:`CallMachine.transition`, which

1. validates the ``(event, current_state)`` pair against the transition
   table,
2. applies the row update (state, reason, version bump, extra columns),
3. allocates the next ``lifecycle_events`` sequence number, and
4. wakes every coroutine waiting on that call via the event bus.

Waiting is event-driven: :class:`CallEventBus.wait_change` parks a
coroutine until the next transition on the call (or timeout).  Because
the whole service runs on a single asyncio loop and the wait protocol is
"read snapshot, then register waiter" with no suspension point between
the two steps, a transition can never be missed.
"""

from __future__ import annotations

import asyncio
import sqlite3
from typing import Mapping

RINGING = "ringing"
ACCEPTED = "accepted"
IN_PROGRESS = "in_progress"
ANSWERED = "answered"
COMPLETED = "completed"
FAILED = "failed"
DECLINED = "declined"
CANCELLED = "cancelled"
NOT_ACCEPTED = "not_accepted"
DISCONNECTED = "disconnected"
UNREACHABLE = "unreachable"

ACTIVE_STATES = frozenset({RINGING, ACCEPTED, IN_PROGRESS})
TERMINAL_STATES = frozenset({
    ANSWERED, COMPLETED, FAILED, DECLINED, CANCELLED, NOT_ACCEPTED, DISCONNECTED, UNREACHABLE,
})

SUBMIT_TARGETS = frozenset({IN_PROGRESS, ANSWERED, COMPLETED, FAILED})

# event -> {allowed source state -> target state}
# ``submit`` resolves its target from the interpreted result, restricted
# to SUBMIT_TARGETS; the machine validates that set separately.
TRANSITIONS: Mapping[str, Mapping[str, str]] = {
    "accept": {RINGING: ACCEPTED},
    "decline": {RINGING: DECLINED},
    "submit": {ACCEPTED: "", IN_PROGRESS: ""},
    "agent_cancel": {RINGING: CANCELLED, ACCEPTED: CANCELLED, IN_PROGRESS: CANCELLED},
    "human_cancel": {ACCEPTED: CANCELLED, IN_PROGRESS: CANCELLED},
    "ring_expire": {RINGING: NOT_ACCEPTED},
    "disconnect": {ACCEPTED: DISCONNECTED, IN_PROGRESS: DISCONNECTED},
    "unreachable": {RINGING: UNREACHABLE},
    "clock_expire": {RINGING: NOT_ACCEPTED, ACCEPTED: DISCONNECTED, IN_PROGRESS: DISCONNECTED},
}


class InvalidTransition(Exception):
    """Raised when an event is not legal for the call's current state."""

    def __init__(self, call_id: str, event: str, current_state: str) -> None:
        self.call_id, self.event, self.current_state = call_id, event, current_state
        super().__init__(f"event {event!r} illegal for call {call_id!r} in state {current_state!r}")


class CallEventBus:
    """Per-call change signal for event-driven waiting.

    Each waiter registers a fresh, current-loop ``asyncio.Event``;
    ``notify`` sets and drops every registered waiter for the call.  No
    event object survives its own wait, so callers that live on different
    event loops (one ``asyncio.run`` per request, test harnesses, worker
    restarts) never observe a cross-loop binding error.  Readers always
    re-check durable state after waking, so the bus carries no payload.
    """

    def __init__(self) -> None:
        self._waiters: dict[str, list[asyncio.Event]] = {}

    def notify(self, call_id: str) -> None:
        for event in self._waiters.pop(call_id, []):
            event.set()

    async def wait_change(self, call_id: str, timeout: float) -> None:
        """Suspend until the next transition on ``call_id`` or timeout."""
        event = asyncio.Event()
        self._waiters.setdefault(call_id, []).append(event)
        try:
            await asyncio.wait_for(event.wait(), timeout)
        except asyncio.TimeoutError:
            pass
        finally:
            waiters = self._waiters.get(call_id)
            if waiters is not None:
                try:
                    waiters.remove(event)
                except ValueError:
                    pass
                if not waiters:
                    self._waiters.pop(call_id, None)


class CallMachine:
    """Owns every ``call_state`` mutation on ``lifecycle_calls``."""

    def __init__(self, bus: CallEventBus) -> None:
        self.bus = bus

    def transition(
        self,
        conn: sqlite3.Connection,
        call_id: str,
        event: str,
        *,
        target: str | None = None,
        reason: str | None = None,
        columns: Mapping[str, object] | None = None,
        bump_version: bool = True,
    ) -> tuple[sqlite3.Row, int]:
        """Apply one validated transition and allocate its event seq.

        Returns the refreshed call row and the sequence number the caller
        must use when appending the transition's event body.  The caller
        owns the surrounding transaction; the bus notification is fired
        here, which is safe because waiters only ever re-read committed
        state after their coroutine resumes — by then this synchronous
        method (including its caller's commit) has completed.
        """
        row = conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)
        ).fetchone()
        if row is None:
            raise InvalidTransition(call_id, event, "<missing>")
        current = str(row["call_state"])
        allowed = TRANSITIONS.get(event)
        if allowed is None or current not in allowed:
            raise InvalidTransition(call_id, event, current)
        if event == "submit":
            if target not in SUBMIT_TARGETS:
                raise InvalidTransition(call_id, event, current)
            new_state = str(target)
        else:
            new_state = allowed[current]
        assignments = ["call_state=?", "reason=?"]
        values: list[object] = [new_state, reason]
        if bump_version:
            assignments.append("version=version+1")
        for column, value in (columns or {}).items():
            assignments.append(f"{column}=?")
            values.append(value)
        values.append(call_id)
        conn.execute(
            f"UPDATE lifecycle_calls SET {', '.join(assignments)} WHERE call_id=?", values
        )
        seq = int(conn.execute(
            "SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?",
            (call_id,),
        ).fetchone()["n"])
        self.bus.notify(call_id)
        fresh = conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)
        ).fetchone()
        return fresh, seq
