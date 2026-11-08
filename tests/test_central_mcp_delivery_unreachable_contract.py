"""Red contract for definitive notification-delivery failure.

The notification router can distinguish a definitely failed route from an
uncertain delivery attempt.  That decision is useful only when the central
MCP lifecycle durably records it: an Agent must receive ``unreachable``
rather than a call which rings forever or a transport exception.
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


class RecordingWait:
    async def __call__(self, _seconds: float) -> None:
        return None


def _module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]]) -> Any:
    return _module().create_app(
        database_path=tmp_path / "delivery-unreachable.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=RecordingWait(),
        ring_timeout_seconds=60,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> dict[str, object]:
    async def request() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            response = await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        return response.json()

    return asyncio.run(request())


def create(app: Any, request_id: str, token: str = "token-a") -> dict[str, object]:
    return post(app, {"request_id": request_id, "payload": {"tool": "text"}, "client_timeout_seconds": 8}, token)


def continuation(app: Any, ringing: dict[str, object], request_id: str) -> dict[str, object]:
    return post(app, {"request_id": request_id, "interaction_id": ringing["interaction_id"], "call_id": ringing["call_id"], "after_event_seq": 0, "client_timeout_seconds": 8})


def test_definitive_all_channel_failure_is_one_durable_unreachable_event_and_releases_busy(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    app = make_app(tmp_path, clock, notifications)
    ringing = create(app, "ringing")

    # This is the central delivery boundary: the router supplies this value
    # only after every enabled channel has definitively failed.
    app.lifecycle.apply_delivery_decisions({ringing["call_id"]: "unreachable"})

    result = continuation(app, ringing, "read-unreachable")
    assert result == {"success": True, "status": "unreachable", "call_state": "unreachable", "reason": "no_available_channel", "human_action": False, "event_seq": 1, "needs_continuation": False, "interaction_id": ringing["interaction_id"], "call_id": ringing["call_id"]}
    # The same operation is an immutable replay, and terminal delivery frees
    # the sole operator slot without a second notification for the old call.
    assert continuation(app, ringing, "read-unreachable") == result
    next_call = create(app, "new-call", "token-b")
    assert next_call["status"] == "heartbeat"
    assert len(notifications) == 2


def test_uncertain_delivery_waits_but_due_ring_deadline_wins_over_late_unreachable(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    app = make_app(tmp_path, clock, notifications)
    ringing = create(app, "ringing")

    # Router uncertainty is explicitly not evidence that all channels are
    # unavailable.  It must leave the original absolute deadline untouched.
    app.lifecycle.apply_delivery_decisions({ringing["call_id"]: "ringing"})
    assert continuation(app, ringing, "still-ringing")["status"] == "heartbeat"

    clock.now += 60
    # Request-path settlement is authoritative: a late dispatcher decision
    # cannot replace the earlier ring timeout with unreachable.
    app.lifecycle.apply_delivery_decisions({ringing["call_id"]: "unreachable"})
    terminal = continuation(app, ringing, "read-timeout")
    assert terminal["status"] == "not_accepted"
    assert terminal["reason"] == "ring_timeout"
    assert terminal["event_seq"] == 1


def test_repeated_or_stale_delivery_decisions_survive_restart_without_resurrection_or_rering(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    app = make_app(tmp_path, clock, notifications)
    ringing = create(app, "ringing")
    app.lifecycle.apply_delivery_decisions({ringing["call_id"]: "unreachable"})

    # A repeat cannot append a second event.  A restarted service also must
    # not revive or notify the old call when it sees the stale decision.
    app.lifecycle.apply_delivery_decisions({ringing["call_id"]: "unreachable"})
    restarted = make_app(tmp_path, clock, notifications)
    restarted.lifecycle.apply_delivery_decisions({ringing["call_id"]: "unreachable"})
    terminal = continuation(restarted, ringing, "after-restart")
    assert terminal["status"] == "unreachable"
    assert terminal["reason"] == "no_available_channel"
    assert terminal["event_seq"] == 1
    assert len(notifications) == 1
