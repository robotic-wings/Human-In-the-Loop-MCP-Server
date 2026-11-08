"""Red contract for durable post-progress Agent reconnect handling.

This bounded slice starts after a terminal has accepted a call.  It specifies
only the central event stream and Agent reconnect clock for a persisted
``in_progress`` report; task content, terminal liveness, and human interfaces
remain outside this contract.
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
    # D11 fixes the progress grace at 300 seconds for this first contract.
    # The assertions below exercise that duration through the existing app
    # boundary rather than stopping at a constructor-only configuration seam.
    return module.create_app(
        database_path=tmp_path / "progress.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=wait or RecordingWait(),
        ring_timeout_seconds=600,
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
    result = post(app, {"request_id": request_id, "payload": {"tool": "task"}, "client_timeout_seconds": 8})
    assert result["status"] == "heartbeat"
    return result


def continuation(call: dict[str, object], request_id: str, after: int) -> dict[str, object]:
    return {
        "request_id": request_id,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "after_event_seq": after,
        "client_timeout_seconds": 8,
    }


def accept_and_read_opened(app: Any, call: dict[str, object]) -> None:
    accepted = app.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="windows"
    )
    assert accepted["status"] == "opened"
    opened = post(app, continuation(call, "opened", 0))
    assert opened["status"] == "opened"
    assert opened["event_seq"] == 1


def report_progress(app: Any, call: dict[str, object]) -> dict[str, object]:
    progress = app.lifecycle.finish(
        call_id=str(call["call_id"]), status="in_progress", result={"progress": "halfway"}
    )
    assert progress["status"] == "in_progress"
    assert progress["call_state"] == "in_progress"
    assert progress["event_seq"] == 2
    return progress


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
        "event_seq": 3,
    }


def test_progress_delivery_replaces_opened_grace_with_response_bound_300_second_grace(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    wait = RecordingWait()
    app = make_app(tmp_path, clock, notifications, wait=wait)
    call = create(app)
    accept_and_read_opened(app, call)
    report_progress(app, call)

    # The original caller first reads the durable progress event.  Delivery of
    # that body (not the terminal's report action) starts the D11 allowance.
    progress = post(app, continuation(call, "read-progress", 1))
    assert progress["status"] == "in_progress"
    assert progress["call_state"] == "in_progress"
    assert progress["event_seq"] == 2
    assert progress["needs_continuation"] is True

    # A newly executed same-caller continuation before t+300 produces a
    # resumable in-progress heartbeat; its delivered body refreshes grace.
    clock.advance(299)
    refreshed = post(app, continuation(call, "progress-heartbeat", 2))
    assert refreshed["status"] == "heartbeat"
    assert refreshed["call_state"] == "in_progress"
    assert refreshed["needs_continuation"] is True
    # Initial creation and the newly executed in-progress continuation each
    # wait.  Persisted opened/progress events are returned immediately.
    assert wait.legs == [6, 6]

    clock.advance(300)
    assert post(app, continuation(call, "after-refreshed-expiry", 2)) == expected_disconnected(call)
    assert len(notifications) == 1


def test_progress_response_replays_and_foreign_or_invalid_reads_do_not_extend_300_second_grace(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    call = create(app)
    accept_and_read_opened(app, call)
    report_progress(app, call)
    progress_request = continuation(call, "read-progress-once", 1)
    progress = post(app, progress_request)
    assert progress["status"] == "in_progress"

    clock.advance(299)
    # An exact operation replay is a persisted response, not fresh liveness.
    assert post(app, progress_request) == progress
    foreign = post(app, continuation(call, "foreign", 2), token="token-b")
    invalid = post(app, continuation(call, "invalid", 99))
    for denied in (foreign, invalid):
        assert denied["success"] is False
        assert denied["needs_continuation"] is False
        assert denied.get("call_id") is None
        assert denied.get("interaction_id") is None

    # None of the inert reads moved the original progress-delivery deadline.
    clock.advance(2)
    assert post(app, continuation(call, "after-progress-replay-expiry", 2)) == expected_disconnected(call)
    assert len(notifications) == 1


def test_progress_expiry_is_durable_releases_busy_and_late_finalization_replays_terminal_outcome(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    first = make_app(tmp_path, clock, notifications)
    call = create(first, "first")
    accept_and_read_opened(first, call)
    report_progress(first, call)
    assert post(first, continuation(call, "deliver-progress", 1))["status"] == "in_progress"
    clock.advance(300)

    restarted_notifications: list[dict[str, object]] = []
    restarted = make_app(tmp_path, clock, restarted_notifications)
    terminal = post(restarted, continuation(call, "after-restart", 2))
    assert terminal == expected_disconnected(call)
    assert restarted.lifecycle.finish(call_id=str(call["call_id"]), status="completed", result={"report": "late"}) == terminal

    # Expiry releases the one active call slot.  It cannot re-ring the old
    # interaction, but a distinct new call is admitted through the restarted
    # notifier.
    next_call = create(restarted, "next")
    assert next_call["status"] == "heartbeat"
    assert len(notifications) == 1
    assert restarted_notifications == [{"call_id": next_call["call_id"], "ring_deadline": clock() + 600}]
