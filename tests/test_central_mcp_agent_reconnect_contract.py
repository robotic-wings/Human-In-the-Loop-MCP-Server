"""Regression contract: ringing HTTP wait legs are not agent disconnects."""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import httpx


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingWait:
    """Records wait legs without advancing the injected server clock."""

    def __init__(self) -> None:
        self.legs: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.legs.append(seconds)


class FirstLegAdvancingWait(RecordingWait):
    """Advances only the initial wait leg to model heartbeat delivery time."""

    def __init__(self, clock: FakeClock) -> None:
        super().__init__()
        self._clock = clock

    async def __call__(self, seconds: float) -> None:
        await super().__call__(seconds)
        if len(self.legs) == 1:
            self._clock.advance(seconds)


def _module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(
    tmp_path: Path,
    clock: FakeClock,
    notifications: list[dict[str, object]],
    *,
    ring_timeout: float = 60,
    wait: Any | None = None,
) -> Any:
    # ``agent_reconnect_seconds`` is an intentional new seam.  The current
    # adapter has no durable reconnect deadline at all, so this Red contract
    # must initially fail rather than accidentally testing a wall-clock wait.
    return _module().create_app(
        database_path=tmp_path / "waiting.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=wait or RecordingWait(),
        ring_timeout_seconds=ring_timeout,
        agent_reconnect_seconds=30,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> dict[str, object]:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            return await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})

    response = asyncio.run(request())
    assert response.status_code in {200, 400, 404}
    return response.json()


def create(app: Any, request_id: str = "create", *, client_timeout: float = 8) -> dict[str, object]:
    created = post(
        app,
        {"request_id": request_id, "payload": {"tool": "text"}, "client_timeout_seconds": client_timeout},
    )
    assert created["status"] == "heartbeat"
    return created


def continuation(call: dict[str, object], request_id: str, after: int = 0) -> dict[str, object]:
    return {
        "request_id": request_id,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "after_event_seq": after,
        "client_timeout_seconds": 8,
    }


def expected_disconnected(call: dict[str, object]) -> dict[str, object]:
    return {
        "success": True,
        "status": "disconnected",
        "call_state": "disconnected",
        "reason": "agent_disconnected",
        "human_action": False,
        "needs_continuation": False,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "event_seq": 1,
    }


def test_ringing_wait_legs_remain_ringing_until_the_absolute_ring_deadline(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    # Keep the absolute ringing deadline later than the old 30-second
    # reconnect interval.  A normal heartbeat/continuation must not turn this
    # unanswered call into agent_disconnected.
    app = make_app(tmp_path, clock, notifications, ring_timeout=120)
    call = create(app)
    clock.advance(20)

    refreshed = post(app, continuation(call, "continue"))
    assert refreshed["status"] == "heartbeat"
    assert len(notifications) == 1

    # The call is still ringing well after the former 30-second disconnect
    # boundary because the Agent is using the same interaction/call pair.
    clock.advance(29)
    assert post(app, continuation(call, "still-live"))["status"] == "heartbeat"
    clock.advance(30)
    still_ringing = post(app, continuation(call, "still-ringing-after-old-grace"))
    assert still_ringing["status"] == "heartbeat"
    assert still_ringing["call_state"] == "ringing"


def test_reconnect_grace_starts_when_the_heartbeat_is_delivered_not_when_wait_begins(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    wait = FirstLegAdvancingWait(clock)
    app = make_app(tmp_path, clock, notifications, ring_timeout=120, wait=wait)

    # A 120-second client limit has a 90-second safe wait leg.  The initial
    # heartbeat therefore arrives at t=90, and only then may its 30-second
    # reconnect allowance begin; a deadline set when the request began would
    # already be expired at t=90.
    call = create(app, client_timeout=120)
    assert wait.legs == [90]
    assert clock() == 1_700_000_090.0
    immediate = post(app, continuation(call, "immediate-after-delivery"))
    assert immediate["status"] == "heartbeat"
    assert immediate["call_state"] == "ringing"

    # The original absolute ring deadline still wins at t=120.  The point of
    # this case is that the immediate continuation at t=90 is not falsely
    # settled as an already-expired agent reconnect deadline.
    clock.advance(30)
    terminal = post(app, continuation(call, "absolute-ring-wins"))
    assert terminal["status"] == "not_accepted"
    assert terminal["reason"] == "ring_timeout"


def test_replayed_initial_heartbeat_does_not_create_a_ringing_disconnect_deadline(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications, ring_timeout=120)

    # Retrying the identical create operation remains a response replay and
    # must not make a second notification.
    call = create(app, "replayed-create")
    clock.advance(29)
    replayed = create(app, "replayed-create")
    assert replayed == call
    assert notifications == [{"call_id": call["call_id"], "ring_deadline": 1_700_000_120.0, "title": "agent-a", "body": "Human-in-the-Loop request"}]

    # At t=31 a valid continuation remains a normal ringing heartbeat.
    clock.advance(2)
    assert post(app, continuation(call, "after-replay-old-grace"))["call_state"] == "ringing"


def test_replayed_continuation_does_not_end_an_unanswered_ringing_call(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications, ring_timeout=120)
    call = create(app, "continuation-replay-create")

    # Retrying an identical continuation is only a response replay and must
    # not deliver a second notification.
    clock.advance(20)
    request = continuation(call, "continuation-replay")
    first = post(app, request)
    assert first["status"] == "heartbeat"
    clock.advance(29)
    replayed = post(app, request)
    assert replayed == first
    assert len(notifications) == 1

    # A different valid continuation after the former grace boundary remains
    # ringing until the actual ring deadline.
    clock.advance(2)
    assert post(app, continuation(call, "after-continuation-replay-old-grace"))["call_state"] == "ringing"


def test_request_id_cannot_be_reused_across_create_and_continuation_without_wait_or_deadline_mutation(
    tmp_path: Path,
) -> None:
    clock, notifications = FakeClock(), []
    wait = RecordingWait()
    app = make_app(tmp_path, clock, notifications, ring_timeout=120, wait=wait)
    call = create(app, "same-id")
    assert wait.legs == [6]

    # ``request_id`` is unique for the caller across operations, rather than
    # merely inside the create or continuation operation table.  Reusing the
    # create ID for a distinct continuation payload is a conflict: it must not
    # perform another wait or treat that request as fresh reconnect liveness.
    clock.advance(29)
    conflicting = post(app, continuation(call, "same-id"))
    assert conflicting["success"] is False
    assert conflicting["status"] == "request_conflict"
    assert conflicting["needs_continuation"] is False
    assert wait.legs == [6]
    assert len(notifications) == 1

    # The rejected cross-operation reuse cannot turn the normal ringing wait
    # into a disconnect. A fresh ID at t=31 remains ringing.
    clock.advance(2)
    assert post(app, continuation(call, "after-cross-operation-conflict"))["call_state"] == "ringing"


def test_restart_preserves_ringing_until_its_absolute_deadline(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    first = make_app(tmp_path, clock, notifications)
    call = create(first)
    clock.advance(30)

    restarted_notifications: list[dict[str, object]] = []
    restarted = make_app(tmp_path, clock, restarted_notifications)
    still_ringing = post(restarted, continuation(call, "after-restart"))
    assert still_ringing["call_state"] == "ringing"

    # The original call only releases the busy slot at its real ring deadline.
    clock.advance(30)
    terminal = post(restarted, continuation(call, "ring-deadline"))
    assert terminal["status"] == "not_accepted"
    assert terminal["reason"] == "ring_timeout"
    next_call = create(restarted, "new-after-release")
    assert next_call["status"] == "heartbeat"
    # The original app's injected notifier records only its own initial
    # invitation.  The restarted app owns the notifier used for the newly
    # admitted call.
    assert notifications == [{"call_id": call["call_id"], "ring_deadline": 1_700_000_060.0, "title": "agent-a", "body": "Human-in-the-Loop request"}]
    assert restarted_notifications == [{"call_id": next_call["call_id"], "ring_deadline": clock() + 60, "title": "agent-a", "body": "Human-in-the-Loop request"}]


def test_foreign_or_invalid_continuation_cannot_refresh_and_earlier_ring_deadline_wins(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications, ring_timeout=20)
    call = create(app)

    foreign = post(app, continuation(call, "foreign"), token="token-b")
    invalid = post(app, continuation(call, "invalid", after=999))
    for denied in (foreign, invalid):
        assert denied["success"] is False
        assert denied["needs_continuation"] is False
        assert denied.get("call_id") is None
        assert denied.get("interaction_id") is None

    # The original 20-second ring deadline is earlier than reconnect expiry,
    # so request-path settlement must retain the original timeout reason.
    clock.advance(20)
    terminal = post(app, continuation(call, "ring-wins"))
    assert terminal["status"] == "not_accepted"
    assert terminal["call_state"] == "not_accepted"
    assert terminal["reason"] == "ring_timeout"
    assert terminal["needs_continuation"] is False
    assert len(notifications) == 1
