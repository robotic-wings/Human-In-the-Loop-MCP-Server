"""Red contract for explicit Agent cancellation of one central call round.

Transport loss is not cancellation.  The central MCP boundary must expose the
SPEC's ``cancel_interaction`` operation for the authenticated caller and
durably distinguish its ``agent_cancelled`` outcome from reconnect expiry.
Terminal ownership, browser/desktop behaviour, and task submission are
deliberately outside this contract.
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
    async def __call__(self, _seconds: float) -> None:
        return None


def _module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]]) -> Any:
    return _module().create_app(
        database_path=tmp_path / "agent-cancellation.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=RecordingWait(),
        ring_timeout_seconds=60,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> tuple[int, dict[str, object]]:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            return await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})

    response = asyncio.run(request())
    return response.status_code, response.json()


def create(app: Any, request_id: str, token: str = "token-a") -> dict[str, object]:
    status, body = post(app, {
        "request_id": request_id,
        "payload": {"tool": "text", "prompt": request_id},
        "client_timeout_seconds": 8,
    }, token)
    assert status == 200
    assert body["status"] == "heartbeat"
    return body


def cancel_body(call: dict[str, object], request_id: str) -> dict[str, object]:
    return {
        "operation": "cancel_interaction",
        "request_id": request_id,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "client_timeout_seconds": 8,
    }


def expected_cancelled(call: dict[str, object], event_seq: int) -> dict[str, object]:
    return {
        "success": True,
        "status": "cancelled",
        "call_state": "cancelled",
        "reason": "agent_cancelled",
        "human_action": False,
        "needs_continuation": False,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "event_seq": event_seq,
    }


def continuation_body(call: dict[str, object], request_id: str, after: int = 0) -> dict[str, object]:
    return {
        "request_id": request_id,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "after_event_seq": after,
        "client_timeout_seconds": 8,
    }


def test_original_caller_cancels_ringing_once_replays_and_releases_busy(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    ringing = create(app, "create-ringing")

    status, cancelled = post(app, cancel_body(ringing, "cancel-ringing"))
    assert status == 200
    assert cancelled == expected_cancelled(ringing, 1)
    # The same operation replays the immutable saved outcome rather than
    # appending an event, ringing again, or changing its terminal state.
    assert post(app, cancel_body(ringing, "cancel-ringing")) == (200, cancelled)
    next_call = create(app, "next-after-cancel", "token-b")
    assert next_call["status"] == "heartbeat"
    assert len(notifications) == 2


def test_original_caller_cancels_accepted_call_and_restart_preserves_terminal_result(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    call = create(app, "create-accepted")
    opened = app.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="windows"
    )
    assert opened["status"] == "opened"

    assert post(app, cancel_body(call, "cancel-accepted")) == (200, expected_cancelled(call, 2))
    restarted = make_app(tmp_path, clock, [])
    # A later continuation reads the same saved terminal event and cannot
    # re-open the call after process recovery.
    assert post(restarted, continuation_body(call, "read-after-restart", after=1)) == (200, expected_cancelled(call, 2))
    assert restarted.lifecycle.finish(call_id=str(call["call_id"]), status="completed", result={"report": "late"}) == expected_cancelled(call, 2)


def test_foreign_wrong_or_stale_round_cancellation_is_non_disclosing_and_inert(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    old_call = create(app, "old-call")

    foreign_status, foreign = post(app, cancel_body(old_call, "foreign-cancel"), "token-b")
    assert foreign_status in {400, 404}
    assert foreign["success"] is False
    assert foreign["needs_continuation"] is False
    assert foreign.get("interaction_id") is None
    assert foreign.get("call_id") is None
    assert post(app, continuation_body(old_call, "old-still-ringing")) == (200, {
        "success": True, "status": "heartbeat", "call_state": "ringing",
        "interaction_id": old_call["interaction_id"], "call_id": old_call["call_id"], "needs_continuation": True,
    })

    assert post(app, cancel_body(old_call, "owner-cancel")) == (200, expected_cancelled(old_call, 1))
    new_call = create(app, "new-round", "token-b")
    wrong = dict(cancel_body(old_call, "stale-old-round"))
    wrong["call_id"] = new_call["call_id"]
    stale_status, stale = post(app, wrong, "token-b")
    assert stale_status in {400, 404}
    assert stale["success"] is False
    assert stale.get("call_id") is None
    assert stale.get("interaction_id") is None
    assert post(app, continuation_body(new_call, "new-still-ringing"), "token-b")[1]["status"] == "heartbeat"
    assert len(notifications) == 2


def test_deadline_is_settled_before_late_agent_cancellation(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    ringing = create(app, "deadline-call")
    clock.advance(60)

    status, late = post(app, cancel_body(ringing, "late-cancel"))
    assert status == 200
    assert late == {
        "success": True,
        "status": "not_accepted",
        "call_state": "not_accepted",
        "reason": "ring_timeout",
        "event_seq": 1,
        "needs_continuation": False,
        "interaction_id": ringing["interaction_id"],
        "call_id": ringing["call_id"],
    }
    assert len(notifications) == 1
