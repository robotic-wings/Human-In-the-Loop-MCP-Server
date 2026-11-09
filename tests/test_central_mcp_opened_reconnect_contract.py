"""Red contract for the agent reconnect grace after an ``opened`` event.

This slice deliberately starts after the terminal has already atomically
accepted a ringing call.  It does not cover task progress, terminal liveness,
or any human-facing endpoint.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import httpx


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingWait:
    def __init__(self) -> None:
        self.legs: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.legs.append(seconds)


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]], *, wait: Any | None = None) -> Any:
    module = importlib.import_module("central_mcp_waiting_lifecycle")
    return module.create_app(
        database_path=tmp_path / "opened.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=wait or RecordingWait(),
        ring_timeout_seconds=120,
        agent_reconnect_seconds=30,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> dict[str, object]:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            return await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})

    response = asyncio.run(request())
    assert response.status_code in {200, 400, 404}
    return response.json()


def create(app: Any, request_id: str = "create") -> dict[str, object]:
    result = post(app, {"request_id": request_id, "payload": {"tool": "text"}, "client_timeout_seconds": 8})
    assert result["status"] == "heartbeat"
    return result


def continuation(call: dict[str, object], request_id: str, after: int = 0) -> dict[str, object]:
    return {
        "request_id": request_id,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "after_event_seq": after,
        "client_timeout_seconds": 8,
    }


def accept_and_read_opened(app: Any, call: dict[str, object], request_id: str = "read-opened") -> dict[str, object]:
    accepted = app.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="windows"
    )
    assert accepted["status"] == "opened"
    opened = post(app, continuation(call, request_id))
    assert opened["status"] == "opened"
    assert opened["call_state"] == "accepted"
    assert opened["event_seq"] == 1
    return opened


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
        "event_seq": 2,
    }


def test_opened_delivery_and_new_accepted_heartbeat_arm_response_bound_grace(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    wait = RecordingWait()
    app = make_app(tmp_path, clock, notifications, wait=wait)
    call = create(app)
    accept_and_read_opened(app, call)

    # The opened event is returned to the original caller first.  A new valid
    # continuation during its 30-second allowance enters an accepted wait leg;
    # the resulting heartbeat body refreshes the next 30-second allowance.
    clock.advance(29)
    refreshed = post(app, continuation(call, "accepted-heartbeat", after=1))
    assert refreshed["status"] == "heartbeat"
    assert refreshed["call_state"] == "accepted"
    assert wait.legs == [6, 6]
    assert notifications == [{"call_id": call["call_id"], "ring_deadline": 1_700_000_120.0, "title": "agent-a", "body": "Human-in-the-Loop request"}]

    clock.advance(30)
    assert post(app, continuation(call, "expired", after=1)) == expected_disconnected(call)


def test_opened_replay_foreign_and_invalid_reads_are_inert(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    call = create(app)
    opened = accept_and_read_opened(app, call, "opened-once")

    clock.advance(29)
    # Replaying a delivered opened response cannot re-arm its grace.  Nor can
    # a foreign caller or malformed cursor reveal/refresh this accepted call.
    assert post(app, continuation(call, "opened-once")) == opened
    foreign = post(app, continuation(call, "foreign"), token="token-b")
    invalid = post(app, continuation(call, "invalid", after=99))
    for denied in (foreign, invalid):
        assert denied["success"] is False
        assert denied["needs_continuation"] is False
        assert denied.get("call_id") is None
        assert denied.get("interaction_id") is None

    clock.advance(2)
    assert post(app, continuation(call, "expiry", after=1)) == expected_disconnected(call)
    assert len(notifications) == 1


def test_opened_expiry_survives_restart_releases_busy_and_rejects_late_finalization(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    first = make_app(tmp_path, clock, notifications)
    call = create(first, "first-call")
    accept_and_read_opened(first, call)
    clock.advance(30)

    restarted_notifications: list[dict[str, object]] = []
    restarted = make_app(tmp_path, clock, restarted_notifications)
    terminal = post(restarted, continuation(call, "after-restart", after=1))
    assert terminal == expected_disconnected(call)
    assert restarted.lifecycle.finish(call_id=str(call["call_id"]), status="completed", result={"report": "late"}) == terminal

    # Agent-disconnected calls release the sole busy slot and restart does not
    # repeat the original terminal notification.
    next_call = create(restarted, "next-call")
    assert next_call["status"] == "heartbeat"
    assert len(notifications) == 1
    assert restarted_notifications == [{"call_id": next_call["call_id"], "ring_deadline": clock() + 120, "title": "agent-a", "body": "Human-in-the-Loop request"}]
