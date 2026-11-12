"""Contract: in-progress reports must reach the agent outcome feed.

A human on an accepted task can submit a progress report (the terminal's
报告进度 button). Central records it as a durable event with
``needs_continuation=True`` — the round is not over. The agent side has two
consumers for call events:

* an inline/MCP agent re-polls ``continue_call`` and sees the event (already
  covered by the in-progress reconnect contract);
* a detached delivery agent (Chidori) parks on a single signal and relies on
  the postman bridge, which reads ``GET /api/agent/outcomes``.

This contract pins the second path: a progress event for a call created with
``delivery={"kind": "chidori-detached", ...}`` MUST appear in the outcome
feed so the postman can wake the agent mid-round. Before the fix the feed
filtered on ``needs_continuation is False`` and silently dropped every
progress report — the terminal showed 进度已上报 while the agent slept until
the final answer.
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
        self.legs: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.legs.append(seconds)


def make_app(tmp_path: Path, clock: FakeClock) -> Any:
    module = importlib.import_module("central_mcp_waiting_lifecycle")
    return module.create_app(
        database_path=tmp_path / "progress-feed.sqlite3",
        credentials={"agent-a": "token-a"},
        clock=clock,
        notify=lambda _event: None,
        wait=RecordingWait(),
        ring_timeout_seconds=600,
        agent_reconnect_seconds=30,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> dict[str, object]:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            return await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    return response.json()


def outcomes(app: Any, after: int = 0) -> list[dict[str, object]]:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            return await client.get(
                f"/api/agent/outcomes?after={after}&limit=50",
                headers={"Authorization": "Bearer token-a"},
            )

    response = asyncio.run(request())
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["success"] is True
    return list(body["outcomes"])


def create_detached_call(app: Any) -> dict[str, object]:
    created = post(app, {
        "request_id": "create-detached",
        "payload": {"tool": "assign_task_to_human", "task_description": "装系统"},
        "client_timeout_seconds": 8,
        "delivery": {"kind": "chidori-detached", "agent": "progress-probe"},
    })
    assert created["status"] == "heartbeat"
    return created


def test_progress_report_reaches_outcome_feed_for_detached_delivery(tmp_path: Path) -> None:
    app = make_app(tmp_path, FakeClock())
    call = create_detached_call(app)

    accepted = app.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="macos"
    )
    assert accepted["status"] == "opened"

    progress = app.lifecycle.finish(
        call_id=str(call["call_id"]), status="in_progress", result={"progress": "进度：系统盘装了一半"}
    )
    assert progress["status"] == "in_progress"

    feed = outcomes(app)
    entries = [e for e in feed if e["call_id"] == call["call_id"]]
    assert entries, "progress event must reach the outcome feed so the postman can wake the detached agent"
    entry = entries[0]
    assert entry["outcome"]["status"] == "in_progress"
    assert entry["outcome"]["needs_continuation"] is True
    assert "系统盘装了一半" in str(entry["outcome"].get("progress") or entry["outcome"].get("result"))
    assert entry["delivery"] == {"kind": "chidori-detached", "agent": "progress-probe"}


def test_final_outcome_follows_progress_in_feed(tmp_path: Path) -> None:
    """After progress wakes the agent, the final answer must still arrive."""
    app = make_app(tmp_path, FakeClock())
    call = create_detached_call(app)
    app.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="macos"
    )
    app.lifecycle.finish(call_id=str(call["call_id"]), status="in_progress", result={"progress": "halfway"})
    done = app.lifecycle.finish(
        call_id=str(call["call_id"]), status="completed", result={"status": "completed", "report": "装好了"}
    )
    assert done["call_state"] == "completed"

    feed = outcomes(app)
    mine = [e for e in feed if e["call_id"] == call["call_id"]]
    states = [e["outcome"]["status"] for e in mine]
    assert states == ["in_progress", "completed"], f"feed must carry progress then final in order: {states}"
    assert mine[-1]["outcome"]["needs_continuation"] is False


def test_accept_event_never_floods_feed(tmp_path: Path) -> None:
    """Only progress and terminal outcomes belong in the feed — not opens."""
    app = make_app(tmp_path, FakeClock())
    call = create_detached_call(app)
    app.lifecycle.accept(
        call_id=str(call["call_id"]), device_id="desktop-1", handled_on="desktop", terminal_platform="macos"
    )
    feed = outcomes(app)
    assert feed == [], f"accept/opened events must stay out of the feed: {feed}"
