"""Red contract for the busy admission result's control fields.

A rejected competing call returns ``busy`` without fabricating an active
call.  The SPEC requires the busy result to carry the same null control
fields as any pre-call rejection (``reason``, ``interaction_id``,
``handled_on``, ``terminal_platform``, ``event_seq``) and a false
``human_action`` marker.  These tests stay red until the ``create`` busy
branch in ``central_mcp_waiting_lifecycle`` fills those fields.
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
    async def __call__(self, _seconds: float) -> None:
        return None


def make_app(tmp_path: Path, clock: FakeClock, notifications: list[dict[str, object]]) -> Any:
    module = importlib.import_module("central_mcp_waiting_lifecycle")
    return module.create_app(
        database_path=tmp_path / "busy.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock,
        notify=notifications.append,
        wait=RecordingWait(),
        ring_timeout_seconds=60,
    )


def post(app: Any, body: dict[str, object], token: str = "token-a") -> httpx.Response:
    async def request() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://central.test"
        ) as client:
            return await client.post("/mcp", json=body, headers={"Authorization": f"Bearer {token}"})

    return asyncio.run(request())


def create(app: Any, request_id: str) -> dict[str, object]:
    response = post(app, {
        "request_id": request_id,
        "payload": {"tool": "get_text_input", "prompt": request_id},
        "client_timeout_seconds": 8,
    })
    assert response.status_code == 200
    return response.json()


def test_busy_result_carries_null_control_fields_and_false_human_action(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    assert create(app, "first")["call_state"] == "ringing"

    busy = post(app, {
        "request_id": "second", "payload": {"tool": "confirm"}, "client_timeout_seconds": 8,
    }, token="token-b")

    assert busy.status_code == 200
    body = busy.json()
    assert body["success"] is True
    assert body["status"] == "busy"
    assert body["reason"] is None
    assert body["interaction_id"] is None
    assert body["human_action"] is False
    assert body["handled_on"] is None
    assert body["terminal_platform"] is None
    assert body["event_seq"] is None
    assert body["call_state"] is None
    assert body["call_id"] is None
    assert body["needs_continuation"] is False


def test_busy_result_is_idempotent_and_does_not_fabricate_an_active_call(tmp_path: Path) -> None:
    clock, notifications = FakeClock(), []
    app = make_app(tmp_path, clock, notifications)
    assert create(app, "first")["call_state"] == "ringing"

    busy_body = {
        "request_id": "second", "payload": {"tool": "confirm"}, "client_timeout_seconds": 8,
    }
    first = post(app, busy_body, token="token-b").json()
    replay = post(app, busy_body, token="token-b").json()

    assert first == replay
    assert first["status"] == "busy"
    assert first["reason"] is None
    assert first["interaction_id"] is None
    assert first["human_action"] is False
    assert first["handled_on"] is None
    assert first["terminal_platform"] is None
    assert first["event_seq"] is None
    assert first["call_id"] is None
    # The busy result must not advance the ringing call or ring again.
    assert len(notifications) == 1
