"""Red contract for the post-answer quiet window (Phase 1: central service).

After any end answers a call, a new call created within ``quiet_window_seconds``
is "degraded": the notification event carries a ``degraded``/``quiet_mode`` flag
and the Bark push omits ``call=1`` so the phone does not ring loudly again for a
short while.  The central service persists ``quiet_window_seconds`` (default 300),
``quiet_mode`` (``short``/``silent``, default ``short``) and ``last_accept_at``.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_mcp_waiting_lifecycle import create_app
from central_notification_router import NotificationRouter, TerminalRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path) -> tuple[Any, Clock, NotificationRouter, list[dict[str, object]]]:
    clock = Clock()
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-owner",
        endpoint="https://desktop.test/events", callback_credential="callback-secret", gui_ready=False,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        registered_credentials={
            "desktop-secret": {"device_id": "desktop-owner", "terminal_platform": "windows"},
        }, device_state=lambda _device: [],
    )
    notifications: list[dict[str, object]] = []

    async def desktop_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute("SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
        route_mode="both", call_state=call_state, web_base_url="https://central.test",
    )

    def notify(event: dict[str, object]) -> None:
        notifications.append(dict(event))
        router.enqueue(event)

    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=notify, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
    )
    return app, clock, router, notifications


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, terminal: str | None = None,
            authorization: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        if terminal: headers["x-terminal-session"] = terminal
        if authorization: headers["authorization"] = authorization
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def register_terminal(app: Any) -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    }, authorization="Terminal desktop-secret")
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def create_call(app: Any, rid: int) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": f"quiet-create-{rid}", "client_timeout_seconds": 2,
        "payload": {"tool": "text", "prompt": "hi"},
    })
    assert response.status_code == 200, response.text
    return response.json()


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session,
                       body={"operation_id": f"accept-{call['call_id']}"})
    assert response.status_code == 200, response.text
    assert response.json()["call_state"] == "accepted"


def submit_answer(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=session, body={
        "submission_id": f"sub-{call['call_id']}", "result": {"user_input": "done"},
    })
    assert response.status_code == 200, response.text
    assert response.json()["call_state"] == "answered"


def bark_payload(router: NotificationRouter, call_id: str) -> dict[str, object] | None:
    for record in router.outbox_records(call_id):
        if record["channel"] == "bark":
            payload = record["payload"]
            return payload if isinstance(payload, dict) else None
    return None


def test_quiet_window_defaults_and_persists(tmp_path: Path) -> None:
    app1, _clock1, _router1, _ = components(tmp_path)
    state = app1.lifecycle.get_quiet_window()
    assert state["success"] is True
    assert state["quiet_window_seconds"] == 300
    assert state["quiet_mode"] == "short"

    app1.lifecycle.set_quiet_window(quiet_window_seconds=120, quiet_mode="silent")
    updated = app1.lifecycle.get_quiet_window()
    assert updated["quiet_window_seconds"] == 120
    assert updated["quiet_mode"] == "silent"

    app2, _clock2, _router2, _ = components(tmp_path)
    persisted = app2.lifecycle.get_quiet_window()
    assert persisted["quiet_window_seconds"] == 120
    assert persisted["quiet_mode"] == "silent"


def test_accept_records_last_accept_at(tmp_path: Path) -> None:
    app, clock, _router, _ = components(tmp_path)
    session = register_terminal(app)
    assert app.lifecycle.get_quiet_window()["last_accept_at"] is None
    call = create_call(app, 1)
    accept_desktop(app, call, session)
    assert app.lifecycle.get_quiet_window()["last_accept_at"] == 1_700_000_000.0
    assert clock() == 1_700_000_000.0


def test_new_call_within_window_is_degraded(tmp_path: Path) -> None:
    app, clock, _router, notifications = components(tmp_path)
    session = register_terminal(app)
    call = create_call(app, 1)
    accept_desktop(app, call, session)
    submit_answer(app, call, session)

    clock.advance(100)
    create_call(app, 2)
    assert notifications[-1].get("degraded") is True, (
        "a call within the quiet window must be marked degraded"
    )


def test_new_call_after_window_is_not_degraded(tmp_path: Path) -> None:
    app, clock, router, notifications = components(tmp_path)
    session = register_terminal(app)
    call = create_call(app, 1)
    accept_desktop(app, call, session)
    submit_answer(app, call, session)

    clock.advance(400)
    after = create_call(app, 2)
    assert notifications[-1].get("degraded") is not True
    assert bark_payload(router, str(after["call_id"])).get("call") == "1"


def test_bark_payload_drops_call_on_degraded_call(tmp_path: Path) -> None:
    app, clock, router, _notifications = components(tmp_path)
    session = register_terminal(app)

    normal = create_call(app, 1)
    assert bark_payload(router, str(normal["call_id"])).get("call") == "1"
    accept_desktop(app, normal, session)
    submit_answer(app, normal, session)

    clock.advance(100)
    degraded = create_call(app, 2)
    payload = bark_payload(router, str(degraded["call_id"]))
    assert payload is not None
    assert "call" not in payload, (
        "a degraded Bark push must omit call=1 so the phone does not ring loudly"
    )
