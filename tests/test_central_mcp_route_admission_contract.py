"""Red contract for route admission before central MCP call creation.

The configured human route is a central-service admission condition.  A bad
route is not a busy call, notification attempt, or wait leg: it is an
``no_enabled_channel`` protocol error before a call exists.
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
    def __init__(self) -> None:
        self.calls: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.calls.append(seconds)


class RouteAdmission:
    """Mutable central settings seam; never supplied by an Agent request."""

    def __init__(self, result: dict[str, object]) -> None:
        self.result = result
        self.calls = 0

    def __call__(self) -> dict[str, object]:
        self.calls += 1
        return dict(self.result)


def _adapter() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(tmp_path: Path, route: RouteAdmission, notifications: list[dict[str, object]], waiter: RecordingWait) -> Any:
    return _adapter().create_app(
        database_path=tmp_path / "route-admission.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=FakeClock(),
        notify=notifications.append,
        wait=waiter,
        ring_timeout_seconds=60,
        # The missing production seam must be central configuration, not a
        # caller-supplied callback or routing choice.
        route_admission=route,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> tuple[int, dict[str, object]]:
    async def request() -> tuple[int, dict[str, object]]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            response = await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})
        return response.status_code, response.json()

    return asyncio.run(request())


def create(app: Any, request_id: str, token: str = "token-a") -> tuple[int, dict[str, object]]:
    return post(app, {"request_id": request_id, "payload": {"tool": "text"}, "client_timeout_seconds": 8}, token)


def test_no_enabled_route_is_rejected_before_call_persistence_notification_busy_or_wait(tmp_path: Path) -> None:
    route = RouteAdmission({"success": False, "status": "no_enabled_channel", "needs_continuation": False})
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait()
    app = make_app(tmp_path, route, notifications, waiter)

    status, rejected = create(app, "disabled")

    assert status == 400
    assert rejected == {"success": False, "status": "no_enabled_channel", "needs_continuation": False}
    assert "call_id" not in rejected
    assert route.calls == 1
    assert notifications == []
    assert waiter.calls == []
    # A rejected create claims no busy slot: a later valid create is admitted.
    route.result = {"success": True, "status": "ready", "needs_continuation": False}
    status, admitted = create(app, "after-disabled", "token-b")
    assert status == 200
    assert admitted["status"] == "heartbeat"
    assert len(notifications) == 1


def test_valid_route_notifies_once_and_later_route_changes_apply_only_to_new_creates(tmp_path: Path) -> None:
    route = RouteAdmission({"success": True, "status": "ready", "needs_continuation": False})
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait()
    app = make_app(tmp_path, route, notifications, waiter)

    status, ringing = create(app, "first")
    assert status == 200
    assert ringing["status"] == "heartbeat"
    assert len(notifications) == 1

    # The existing route is frozen with its admitted call; changing settings
    # cannot cancel, mutate, or re-notify it.
    route.result = {"success": False, "status": "no_enabled_channel", "needs_continuation": False}
    _, existing = post(app, {"request_id": "read-first", "interaction_id": ringing["interaction_id"], "call_id": ringing["call_id"], "after_event_seq": 0, "client_timeout_seconds": 8})
    assert existing["status"] == "heartbeat"
    assert len(notifications) == 1

    # Release the admitted call through the injected terminal boundary, then
    # a new create observes the changed central route before it can ring.
    app.lifecycle.accept(call_id=ringing["call_id"], device_id="phone", handled_on="iphone_web", terminal_platform="ios")
    app.lifecycle.finish(call_id=ringing["call_id"], status="answered", result={"user_input": "done"})
    status, rejected = create(app, "second", "token-b")
    assert status == 400
    assert rejected["status"] == "no_enabled_channel"
    assert len(notifications) == 1


def test_repeated_or_foreign_malformed_creates_do_not_disclose_or_mutate_an_active_call(tmp_path: Path) -> None:
    route = RouteAdmission({"success": True, "status": "ready", "needs_continuation": False})
    notifications: list[dict[str, object]] = []
    waiter = RecordingWait()
    app = make_app(tmp_path, route, notifications, waiter)
    _, ringing = create(app, "active")

    route.result = {"success": False, "status": "no_enabled_channel", "needs_continuation": False}
    first_status, first = create(app, "bad-a")
    replay_status, replay = create(app, "bad-a")
    foreign_status, foreign = create(app, "bad-b", "token-b")

    assert (first_status, replay_status, foreign_status) == (400, 400, 400)
    assert first == replay == {"success": False, "status": "no_enabled_channel", "needs_continuation": False}
    assert foreign == first
    assert all("call_id" not in value and ringing["call_id"] not in str(value) for value in (first, foreign))
    assert len(notifications) == 1
    assert waiter.calls == [6]
