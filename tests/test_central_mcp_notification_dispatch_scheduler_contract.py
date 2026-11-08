"""Red contract for lifecycle-owned notification dispatch.

The central service must actively dispatch the notification outbox while it is
running.  A caller must not have to make another MCP request, and a test must
not invoke ``Application.dispatch_notification_delivery`` as a substitute for
the server-owned worker.
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


class ManualTimer:
    """A deterministic scheduler seam which advances only when ticked."""

    def __init__(self) -> None:
        self.waiting = asyncio.Event()
        self.delays: list[float] = []
        self._ticks: asyncio.Queue[None] = asyncio.Queue()

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)
        self.waiting.set()
        await self._ticks.get()
        self.waiting.clear()

    async def tick(self) -> None:
        await self._ticks.put(None)


class NoWait:
    async def __call__(self, _seconds: float) -> None:
        return None


def _module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


async def _definitive_failure(_: dict[str, object]) -> dict[str, object]:
    return {"http_status": 503, "business_success": False}


async def _uncertain(_: dict[str, object]) -> dict[str, object]:
    raise TimeoutError("delivery result is unknown")


def make_app(
    tmp_path: Path,
    clock: FakeClock,
    timer: ManualTimer,
    notifications: list[dict[str, object]],
    *,
    bark_dispatch: Any = _definitive_failure,
) -> tuple[Any, Any]:
    router_module = importlib.import_module("central_notification_router")
    registry = router_module.TerminalRegistry(database_path=tmp_path / "routing.sqlite3", clock=clock)
    router = router_module.NotificationRouter(
        database_path=tmp_path / "routing.sqlite3",
        clock=clock,
        registry=registry,
        desktop_dispatch=_definitive_failure,
        bark_dispatch=bark_dispatch,
        route_mode="both",
        web_base_url="https://central.example.test",
        call_state=lambda _call_id: "ringing",
    )
    app = _module().create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=lambda event: (notifications.append(event), router.enqueue(event)),
        wait=NoWait(),
        ring_timeout_seconds=60,
        notification_router=router,
        # This is deliberately distinct from the deadline scheduler.  The
        # central application needs a lifecycle-owned, injectable dispatch
        # worker, not a manual helper invoked by a caller or test.
        notification_dispatch_tick_seconds=1,
        notification_scheduler_sleep=timer,
    )
    return app, router


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


async def wait_for_state(router: Any, call_id: str, states: set[str]) -> None:
    for _ in range(100):
        if {entry["state"] for entry in router.delivery_states(call_id)} == states:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"dispatch did not reach {states}")


def test_lifespan_dispatches_new_ringing_call_without_agent_continuation_or_manual_dispatch(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock, timer, notifications = FakeClock(), ManualTimer(), []
        app, router = make_app(tmp_path, clock, timer, notifications)
        task, incoming, sent = await start_lifespan(app)
        await asyncio.wait_for(timer.waiting.wait(), timeout=1)

        ringing = await post(app, {"request_id": "ring", "payload": {"tool": "text"}, "client_timeout_seconds": 8})
        assert ringing["status"] == "heartbeat"
        await timer.tick()
        await wait_for_state(router, ringing["call_id"], {"failed"})

        # The worker consumed the router decision exactly once and released
        # busy without a continuation or explicit dispatch method call.
        terminal = app.lifecycle.continue_call("agent-a", "read", ringing["interaction_id"], ringing["call_id"], 0)
        assert terminal["status"] == "unreachable"
        assert terminal["reason"] == "no_available_channel"
        assert terminal["event_seq"] == 1
        next_call = await post(app, {"request_id": "next", "payload": {"tool": "confirm"}, "client_timeout_seconds": 8}, "token-b")
        assert next_call["status"] == "heartbeat"
        assert len(notifications) == 2
        await stop_lifespan(task, incoming, sent)

    asyncio.run(exercise())


def test_repeated_dispatch_ticks_and_restart_do_not_repeat_delivery_or_resurrect_old_call(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock, timer, notifications = FakeClock(), ManualTimer(), []
        first, router = make_app(tmp_path, clock, timer, notifications)
        task, incoming, sent = await start_lifespan(first)
        await asyncio.wait_for(timer.waiting.wait(), timeout=1)
        ringing = await post(first, {"request_id": "ring", "payload": {"tool": "text"}, "client_timeout_seconds": 8})
        await timer.tick()
        await wait_for_state(router, ringing["call_id"], {"failed"})
        await timer.tick()
        await asyncio.sleep(0)
        assert len(notifications) == 1
        await stop_lifespan(task, incoming, sent)

        restarted_timer, restarted_notifications = ManualTimer(), []
        restarted, restarted_router = make_app(tmp_path, clock, restarted_timer, restarted_notifications)
        restarted_task, restarted_incoming, restarted_sent = await start_lifespan(restarted)
        await asyncio.wait_for(restarted_timer.waiting.wait(), timeout=1)
        await restarted_timer.tick()
        await asyncio.sleep(0)
        assert restarted_router.delivery_states(ringing["call_id"]) == [
            {"channel": "bark", "state": "failed"},
            {"channel": "desktop", "state": "failed"},
        ]
        terminal = restarted.lifecycle.continue_call("agent-a", "after-restart", ringing["interaction_id"], ringing["call_id"], 0)
        assert terminal["status"] == "unreachable"
        assert terminal["event_seq"] == 1
        assert restarted_notifications == []
        await stop_lifespan(restarted_task, restarted_incoming, restarted_sent)

    asyncio.run(exercise())


def test_uncertain_dispatch_stays_ringing_and_deadline_first_settlement_remains_authoritative(tmp_path: Path) -> None:
    async def exercise() -> None:
        clock, timer, notifications = FakeClock(), ManualTimer(), []
        app, router = make_app(tmp_path, clock, timer, notifications, bark_dispatch=_uncertain)
        task, incoming, sent = await start_lifespan(app)
        await asyncio.wait_for(timer.waiting.wait(), timeout=1)
        ringing = await post(app, {"request_id": "ring", "payload": {"tool": "text"}, "client_timeout_seconds": 8})
        await timer.tick()
        await wait_for_state(router, ringing["call_id"], {"failed", "uncertain"})
        assert app.lifecycle.continue_call("agent-a", "still-ringing", ringing["interaction_id"], ringing["call_id"], 0)["status"] == "heartbeat"

        clock.advance(60)
        # A request-path operation settles the absolute deadline before any
        # later scheduler activity can reinterpret uncertain delivery.
        terminal = app.lifecycle.accept(call_id=ringing["call_id"], device_id="phone", handled_on="iphone_web", terminal_platform="ios")
        assert terminal["status"] == "not_accepted"
        await timer.tick()
        await asyncio.sleep(0)
        assert app.lifecycle.continue_call("agent-a", "read-timeout", ringing["interaction_id"], ringing["call_id"], 0)["reason"] == "ring_timeout"
        assert len(notifications) == 1
        await stop_lifespan(task, incoming, sent)

    asyncio.run(exercise())
