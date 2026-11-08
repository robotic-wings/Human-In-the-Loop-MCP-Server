"""Red contract for post-heartbeat ringing agent reconnect expiry.

This deliberately narrow contract covers a caller that has already received a
``heartbeat`` while the call is still ringing.  It does not cover opened or
in-progress work, human-terminal liveness, cancellation, or any UI channel.
"""

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


def _module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]], *, ring_timeout: float = 60) -> Any:
    # ``agent_reconnect_seconds`` is an intentional new seam.  The current
    # adapter has no durable reconnect deadline at all, so this Red contract
    # must initially fail rather than accidentally testing a wall-clock wait.
    return _module().create_app(
        database_path=tmp_path / "waiting.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=RecordingWait(),
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


def create(app: Any, request_id: str = "create") -> dict[str, object]:
    created = post(app, {"request_id": request_id, "payload": {"tool": "text"}, "client_timeout_seconds": 8})
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


def test_ringing_heartbeat_continuation_refreshes_durable_reconnect_deadline_without_notification(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    # The refreshed reconnect deadline is reached at t=79 below.  Keep the
    # absolute ringing deadline later than that so this test isolates the
    # reconnect-expiry branch; the next test below covers an earlier ring
    # deadline winning instead.
    app = make_app(tmp_path, clock, notifications, ring_timeout=120)
    call = create(app)
    clock.advance(20)

    refreshed = post(app, continuation(call, "continue"))
    assert refreshed["status"] == "heartbeat"
    assert len(notifications) == 1

    # The continuation refreshes the 30-second post-heartbeat allowance.
    clock.advance(29)
    assert post(app, continuation(call, "still-live"))["status"] == "heartbeat"
    clock.advance(30)
    assert post(app, continuation(call, "expired")) == expected_disconnected(call)


def test_reconnect_expiry_is_immutable_across_restart_releases_busy_and_rejects_late_action(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    first = make_app(tmp_path, clock, notifications)
    call = create(first)
    clock.advance(30)

    restarted_notifications: list[dict[str, object]] = []
    restarted = make_app(tmp_path, clock, restarted_notifications)
    terminal = post(restarted, continuation(call, "after-restart"))
    assert terminal == expected_disconnected(call)
    assert post(restarted, continuation(call, "replay")) == terminal
    assert restarted.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="windows"
    ) == terminal

    next_call = create(restarted, "new-after-release")
    assert next_call["status"] == "heartbeat"
    assert len(notifications) == 2
    assert restarted_notifications == [{"call_id": next_call["call_id"], "ring_deadline": clock() + 60}]


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
