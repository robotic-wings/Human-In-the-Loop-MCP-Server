"""Red contract for durable, GUI-free central MCP waiting lifecycle.

The future ``central_mcp_waiting_lifecycle`` adapter owns only the HTTP MCP
waiting boundary.  It is deliberately not a browser, terminal, Bark, task,
attachment, cancellation, or production streaming contract.

Public test seam:

``create_app(database_path=..., credentials=..., clock=..., notify=...,
wait=...)`` returns an ASGI app.  The app exposes ``app.lifecycle`` solely as
an injected-adapter seam with ``accept`` and ``finish`` operations.  Those
operations persist immutable agent events; production terminal adapters will
call the same service rather than the test directly.
"""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import time
from pathlib import Path
from typing import Any

import httpx
import pytest


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class RecordingWait:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.seconds: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.seconds.append(seconds)
        self.clock.advance(seconds)


def _adapter() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]], waiter: RecordingWait) -> Any:
    return _adapter().create_app(
        database_path=tmp_path / "waiting.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=waiter,
        ring_timeout_seconds=60,
    )


def post(app: Any, body: dict[str, object], token: str | None = "token-a") -> httpx.Response:
    async def request() -> httpx.Response:
        headers = {} if token is None else {"Authorization": f"Bearer {token}"}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://central.test"
        ) as client:
            return await client.post("/mcp", json=body, headers=headers)

    return asyncio.run(request())


def create(app: Any, request_id: str = "create-1") -> dict[str, object]:
    response = post(
        app,
        {
            "request_id": request_id,
            "payload": {"tool": "get_text_input", "prompt": request_id},
            "client_timeout_seconds": 8,
        },
    )
    assert response.status_code == 200
    return response.json()


def continue_call(app: Any, call: dict[str, object], request_id: str, after_event_seq: int) -> httpx.Response:
    return post(
        app,
        {
            "request_id": request_id,
            "interaction_id": call["interaction_id"],
            "call_id": call["call_id"],
            "after_event_seq": after_event_seq,
            "client_timeout_seconds": 8,
        },
    )


def test_existing_http_adapter_is_gui_free_control() -> None:
    """Passing control; Red failures below are a missing lifecycle adapter."""

    source = inspect.getsource(importlib.import_module("central_http_service")).lower()
    assert "tkinter" not in source


def test_admission_is_authenticated_idempotent_busy_and_starts_headers_before_wait(tmp_path: Path) -> None:
    clock = FakeClock()
    notifications: list[dict[str, object]] = []
    wait = RecordingWait(clock)
    app = make_app(tmp_path, clock, notifications, wait)

    first = create(app)
    replay = create(app)
    busy = post(
        app,
        {"request_id": "other", "payload": {"tool": "confirm"}, "client_timeout_seconds": 8},
        "token-b",
    )

    assert first == replay
    assert first["status"] == "heartbeat"
    assert first["call_state"] == "ringing"
    assert first["needs_continuation"] is True
    assert busy.json()["status"] == "busy"
    assert busy.json()["needs_continuation"] is False
    assert len(notifications) == 1
    assert wait.seconds == [6]


def test_response_headers_precede_observable_wait_leg(tmp_path: Path) -> None:
    class GateWait:
        def __init__(self, response_started: asyncio.Event) -> None:
            self.response_started = response_started
            self.entered = asyncio.Event()
            self.release = asyncio.Event()

        async def __call__(self, _: float) -> None:
            assert self.response_started.is_set(), "wait leg began before response headers"
            self.entered.set()
            await self.release.wait()

    async def exercise() -> None:
        clock, notifications, started = FakeClock(), [], asyncio.Event()
        gate = GateWait(started)
        app = _adapter().create_app(
            database_path=tmp_path / "headers.sqlite3", credentials={"agent-a": "token-a"},
            clock=clock, notify=notifications.append, wait=gate, ring_timeout_seconds=60,
        )
        body = json.dumps({"request_id": "headers", "payload": {"tool": "x"}, "client_timeout_seconds": 8}).encode()
        sent = False

        async def receive() -> dict[str, object]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            await asyncio.sleep(3600)
            raise AssertionError("body should be read once")

        async def send(event: dict[str, object]) -> None:
            if event["type"] == "http.response.start":
                started.set()

        scope = {"type": "http", "method": "POST", "path": "/mcp", "headers": [(b"authorization", b"Bearer token-a")]}
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(gate.entered.wait(), timeout=1)
        gate.release.set()
        await task

    asyncio.run(exercise())


def test_opened_then_final_events_are_persisted_ordered_and_replayable(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications, RecordingWait(clock))
    ringing = create(app)

    opened = app.lifecycle.accept(
        call_id=ringing["call_id"], device_id="iphone", handled_on="iphone_web", terminal_platform="ios"
    )
    final = app.lifecycle.finish(call_id=ringing["call_id"], status="answered", result={"user_input": "yes"})

    assert opened["event_seq"] == 1
    assert final["event_seq"] == 2
    first = continue_call(app, ringing, "read-opened", 0).json()
    second = continue_call(app, ringing, "read-final", 1).json()
    replay = continue_call(app, ringing, "replay-final", 1).json()
    assert (first["status"], first["event_seq"]) == ("opened", 1)
    assert (second["status"], second["event_seq"]) == ("answered", 2)
    assert replay == second
    assert len(notifications) == 1


def test_finish_while_ringing_is_inert_until_an_owner_accepts(tmp_path: Path) -> None:
    """A final answer cannot manufacture ownership of an unanswered call."""

    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications, RecordingWait(clock))
    ringing = create(app)

    rejected = app.lifecycle.finish(
        call_id=ringing["call_id"], status="answered", result={"user_input": "premature"}
    )
    unchanged = continue_call(app, ringing, "read-ringing", 0).json()

    assert rejected["success"] is False
    assert rejected["needs_continuation"] is False
    assert unchanged["status"] == "heartbeat"
    assert unchanged["call_state"] == "ringing"
    assert "event_seq" not in unchanged
    assert len(notifications) == 1

    opened = app.lifecycle.accept(
        call_id=ringing["call_id"], device_id="iphone", handled_on="iphone_web", terminal_platform="ios"
    )
    final = app.lifecycle.finish(
        call_id=ringing["call_id"], status="answered", result={"user_input": "after-accept"}
    )

    assert opened["status"] == "opened"
    assert final["status"] == "answered"
    assert final["event_seq"] == 2


def test_ring_timeout_is_absolute_persisted_and_recovered_without_reringing(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    first = make_app(tmp_path, clock, notifications, RecordingWait(clock))
    ringing = create(first)
    clock.advance(60)

    restarted_notifications: list[dict[str, object]] = []
    restarted = make_app(tmp_path, clock, restarted_notifications, RecordingWait(clock))
    terminal = continue_call(restarted, ringing, "after-restart", 0)

    assert terminal.status_code == 200
    assert terminal.json() == {
        "success": True,
        "status": "not_accepted",
        "call_state": "not_accepted",
        "reason": "ring_timeout",
        "event_seq": 1,
        "needs_continuation": False,
        "interaction_id": ringing["interaction_id"],
        "call_id": ringing["call_id"],
        "human_action": False,
        "retryable": True,
        "retry_after_seconds": 60,
        "handled_on": None,
        "terminal_platform": None,
        "message": "Nobody picked up within the ring window; not a refusal — retry later if you still need the human.",
    }
    assert len(notifications) == 1
    assert restarted_notifications == []


def test_deadline_first_and_invalid_or_foreign_continuations_are_inert_and_hidden(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications, RecordingWait(clock))
    ringing = create(app)
    clock.advance(60)

    late_action = app.lifecycle.accept(
        call_id=ringing["call_id"], device_id="iphone", handled_on="iphone_web", terminal_platform="ios"
    )
    foreign = post(
        app,
        {"request_id": "foreign", "interaction_id": ringing["interaction_id"], "call_id": ringing["call_id"], "after_event_seq": 0, "client_timeout_seconds": 8},
        "token-b",
    )
    invalid_cursor = continue_call(app, ringing, "bad-cursor", 999)

    assert late_action["status"] == "not_accepted"
    for response in (foreign, invalid_cursor):
        assert response.status_code in {400, 404}
        body = response.json()
        assert body["success"] is False
        assert body["needs_continuation"] is False
        assert body.get("call_id") is None
        assert body.get("interaction_id") is None
    assert len(notifications) == 1
