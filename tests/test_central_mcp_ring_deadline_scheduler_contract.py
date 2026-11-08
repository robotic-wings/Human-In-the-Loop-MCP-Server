"""Red contract for server-owned ringing-deadline settlement.

The central MCP ASGI application, rather than a later caller, terminal action,
or restart, owns the ringing timer.  ``scheduler_sleep`` is an injected test
seam: production supplies its bounded async timer while this contract advances
the authoritative clock deterministically.
"""

from __future__ import annotations

import asyncio
import importlib
import json
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


class ManualTimer:
    """An injected scheduler timer; a test explicitly advances its clock/tick."""

    def __init__(self) -> None:
        self.delays: list[float] = []
        self.waiting = asyncio.Event()
        self._release: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.waiting.set()
        await self._release.get()
        self.waiting.clear()

    async def tick(self) -> None:
        await self._release.put(None)


def _adapter() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]], timer: ManualTimer) -> Any:
    return _adapter().create_app(
        database_path=tmp_path / "deadline.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=lambda _: asyncio.sleep(0),
        ring_timeout_seconds=60,
        deadline_tick_seconds=1,
        scheduler_sleep=timer,
    )


async def post(app: Any, body: dict[str, object], token: str = "token-a") -> dict[str, object]:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
        response = await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    return response.json()


async def start_lifespan(app: Any) -> tuple[asyncio.Task[None], asyncio.Queue[dict[str, object]], list[dict[str, object]]]:
    incoming: asyncio.Queue[dict[str, object]] = asyncio.Queue()
    sent: list[dict[str, object]] = []

    async def receive() -> dict[str, object]:
        return await incoming.get()

    async def send(event: dict[str, object]) -> None:
        sent.append(event)

    task = asyncio.create_task(app({"type": "lifespan"}, receive, send))
    await incoming.put({"type": "lifespan.startup"})
    for _ in range(100):
        if any(event["type"] == "lifespan.startup.complete" for event in sent):
            return task, incoming, sent
        await asyncio.sleep(0)
    raise AssertionError("lifespan startup did not complete")


async def stop_lifespan(task: asyncio.Task[None], incoming: asyncio.Queue[dict[str, object]], sent: list[dict[str, object]]) -> None:
    await incoming.put({"type": "lifespan.shutdown"})
    await asyncio.wait_for(task, timeout=1)
    assert any(event["type"] == "lifespan.shutdown.complete" for event in sent)


def test_lifespan_timer_settles_ring_deadline_without_any_followup_request_or_restart(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock, notifications, timer = FakeClock(), [], ManualTimer()
        app = make_app(tmp_path, clock, notifications, timer)
        task, incoming, sent = await start_lifespan(app)
        await asyncio.wait_for(timer.waiting.wait(), timeout=1)

        ringing = await post(app, {"request_id": "ring", "payload": {"tool": "text"}, "client_timeout_seconds": 8})
        assert ringing["status"] == "heartbeat"
        assert notifications == [{"call_id": ringing["call_id"], "ring_deadline": clock() + 60}]

        clock.advance(60)
        await timer.tick()
        for _ in range(100):
            event = app.lifecycle.continue_call("agent-a", "read-terminal", ringing["interaction_id"], ringing["call_id"], 0)
            if event and event.get("status") == "not_accepted":
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("scheduler did not persist the expired ringing call")

        assert event == {
            "success": True,
            "status": "not_accepted",
            "call_state": "not_accepted",
            "reason": "ring_timeout",
            "event_seq": 1,
            "needs_continuation": False,
            "interaction_id": ringing["interaction_id"],
            "call_id": ringing["call_id"],
        }
        assert notifications == [{"call_id": ringing["call_id"], "ring_deadline": 1_700_000_060.0}]
        await stop_lifespan(task, incoming, sent)

    asyncio.run(exercise())


def test_repeated_ticks_and_restart_do_not_duplicate_expiry_or_rering_and_busy_is_released(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock, first_notifications, timer = FakeClock(), [], ManualTimer()
        first = make_app(tmp_path, clock, first_notifications, timer)
        task, incoming, sent = await start_lifespan(first)
        await asyncio.wait_for(timer.waiting.wait(), timeout=1)
        ringing = await post(first, {"request_id": "ring", "payload": {"tool": "text"}, "client_timeout_seconds": 8})
        clock.advance(60)
        await timer.tick()
        await asyncio.sleep(0)
        await timer.tick()
        await asyncio.sleep(0)
        assert first_notifications == [{"call_id": ringing["call_id"], "ring_deadline": 1_700_000_060.0}]
        await stop_lifespan(task, incoming, sent)

        restarted_notifications: list[dict[str, object]] = []
        restarted_timer = ManualTimer()
        restarted = make_app(tmp_path, clock, restarted_notifications, restarted_timer)
        restarted_task, restarted_incoming, restarted_sent = await start_lifespan(restarted)
        await asyncio.wait_for(restarted_timer.waiting.wait(), timeout=1)
        assert restarted.lifecycle.continue_call("agent-a", "after-restart", ringing["interaction_id"], ringing["call_id"], 0)["event_seq"] == 1
        next_call = await post(restarted, {"request_id": "next", "payload": {"tool": "confirm"}, "client_timeout_seconds": 8}, "token-b")
        assert next_call["status"] == "heartbeat"
        assert restarted_notifications == [{"call_id": next_call["call_id"], "ring_deadline": clock() + 60}]
        await stop_lifespan(restarted_task, restarted_incoming, restarted_sent)

    asyncio.run(exercise())


def test_lifespan_shutdown_cleans_up_runner_without_altering_request_path_deadline_precedence(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock, notifications, timer = FakeClock(), [], ManualTimer()
        app = make_app(tmp_path, clock, notifications, timer)
        task, incoming, sent = await start_lifespan(app)
        await asyncio.wait_for(timer.waiting.wait(), timeout=1)
        ringing = await post(app, {"request_id": "ring", "payload": {"tool": "text"}, "client_timeout_seconds": 8})
        await stop_lifespan(task, incoming, sent)

        # The server-owned runner is gone, but normal deadline-first request
        # settlement remains authoritative and must not manufacture a re-ring.
        clock.advance(60)
        terminal = app.lifecycle.accept(call_id=ringing["call_id"], device_id="phone", handled_on="iphone_web", terminal_platform="ios")
        assert terminal["status"] == "not_accepted"
        assert notifications == [{"call_id": ringing["call_id"], "ring_deadline": 1_700_000_060.0}]

    asyncio.run(exercise())
