"""Red contract for lifecycle-owned accepted-terminal liveness.

This stays at the central ASGI/lifecycle boundary.  A desktop program's
availability heartbeat is deliberately distinct from the heartbeat belonging
to the desktop session that accepted an individual call.
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


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _no_wait(_: float) -> None:
    return None


def _components(tmp_path: Path, *, route_mode: str = "desktop", clock: FakeClock | None = None) -> tuple[Any, FakeClock, Path]:
    clock = clock or FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    for suffix in ("61", "62"):
        registry.register_desktop(
            actor_role="management",
            device_id=f"desktop-{suffix}",
            endpoint=f"https://10.10.0.{suffix}:9100/terminal/events",
            callback_credential=f"central-callback-{suffix}",
            gui_ready=False,
        )
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    service = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        registered_credentials={
            "desktop-61-secret": {"device_id": "desktop-61", "terminal_platform": "windows"},
            "desktop-62-secret": {"device_id": "desktop-62", "terminal_platform": "windows"},
        },
        device_state=lambda _device_id: [],
    )

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
        database_path=tmp_path / "routing.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch, route_mode=route_mode,
        call_state=call_state, web_base_url="https://central.example.test",
    )
    # The absence of this accepted-call deadline configuration is intentional
    # Red evidence on the current source.
    app = create_app(
        database_path=lifecycle_path,
        credentials={"agent-a": "agent-secret", "agent-b": "agent-b-secret"},
        clock=clock, notify=router.enqueue, wait=_no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=service,
        terminal_disconnect_seconds=180,
    )
    return app, clock, lifecycle_path


def _request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
             authorization: str | None = None, session: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if authorization is not None:
            headers["authorization"] = authorization
        if session is not None:
            headers["x-terminal-session"] = session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, json=body, headers=headers)

    return asyncio.run(run())


def _register(app: Any, suffix: str = "61") -> str:
    registered = _request(
        app, "POST", "/api/terminals/register", authorization=f"Terminal desktop-{suffix}-secret",
        body={"endpoint": f"https://10.10.0.{suffix}:9100/terminal/events", "gui_ready": True},
    )
    assert registered.status_code == 201
    return str(registered.json()["session_id"])


def _accepted(app: Any, *, request_id: str = "call") -> tuple[dict[str, object], str]:
    owner = _register(app)
    created = _request(
        app, "POST", "/mcp", authorization="Bearer agent-secret",
        body={"request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "act"}},
    ).json()
    accepted = _request(
        app, "POST", f"/api/calls/{created['call_id']}/accept", session=owner,
        body={"operation_id": f"accept-{request_id}"},
    )
    assert accepted.json()["call_state"] == "accepted"
    return created, owner


def _call_row(path: Path, call_id: str) -> dict[str, object]:
    with sqlite3.connect(path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
    assert row is not None
    return dict(row)


def test_only_the_accepted_owner_session_heartbeat_refreshes_the_persisted_call_deadline(tmp_path: Path) -> None:
    app, clock, database = _components(tmp_path)
    call, owner = _accepted(app)
    before = _call_row(database, str(call["call_id"]))
    assert before["terminal_last_seen_at"] == clock()
    assert before["terminal_disconnect_deadline"] == clock() + 180

    clock.advance(170)
    refreshed = _request(app, "POST", f"/api/calls/{call['call_id']}/heartbeat", session=owner, body={})
    assert refreshed.status_code == 200
    assert refreshed.json() == {"success": True, "status": "heartbeat", "call_state": "accepted", "needs_continuation": True}
    after = _call_row(database, str(call["call_id"]))
    assert after["terminal_last_seen_at"] == clock()
    assert after["terminal_disconnect_deadline"] == clock() + 180
    assert after["version"] == before["version"]

    # The original 180 second deadline has passed, but the owner-session
    # heartbeat alone extended this call's deadline.
    clock.advance(11)
    app.lifecycle.settle_deadlines()
    assert _call_row(database, str(call["call_id"]))["call_state"] == "accepted"


def test_global_or_foreign_activity_cannot_keep_an_accepted_owner_session_alive(tmp_path: Path) -> None:
    app, clock, database = _components(tmp_path)
    call, owner = _accepted(app)
    foreign = _register(app, "62")
    before = _call_row(database, str(call["call_id"]))

    # Program availability and read-only state are not a call-session proof.
    assert _request(app, "POST", "/api/terminals/heartbeat", session=owner, body={"gui_ready": True}).status_code == 200
    assert _request(app, "GET", "/api/terminals/state", session=owner).status_code == 200
    continuation = _request(
        app, "POST", "/mcp", authorization="Bearer agent-secret",
        body={"request_id": "agent-continue", "client_timeout_seconds": 2,
              "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": 1},
    )
    assert continuation.status_code == 200
    for session, authorization in ((foreign, None), (None, None), (None, "Bearer agent-secret")):
        denied = _request(app, "POST", f"/api/calls/{call['call_id']}/heartbeat", session=session,
                          authorization=authorization, body={})
        assert denied.status_code in {401, 404}
        assert denied.json()["success"] is False
    assert _call_row(database, str(call["call_id"]))["terminal_disconnect_deadline"] == before["terminal_disconnect_deadline"]

    # The continuation correctly arms its separate Agent-reconnect grace.
    # Remove that independently-tested clock before isolating the terminal
    # deadline below; otherwise it would correctly win at +30 rather than
    # testing the owner-session timeout.  The explicit equal-deadline test
    # covers the specified terminal-before-Agent ordering separately.
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE lifecycle_calls SET agent_reconnect_deadline=NULL WHERE call_id=?", (call["call_id"],))
        conn.commit()

    clock.advance(180)
    # A request path must settle the durable owner-session expiry before a
    # late heartbeat can mutate it.
    late = _request(app, "POST", f"/api/calls/{call['call_id']}/heartbeat", session=owner, body={})
    assert late.json() == {
        "success": True, "status": "disconnected", "call_state": "disconnected",
        "reason": "terminal_disconnected", "human_action": False, "needs_continuation": False,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "event_seq": 2,
        "retryable": True, "retry_after_seconds": 30, "handled_on": None, "terminal_platform": None,
        "message": "接听端已失联，通信已结束。",
    }
    terminal = _call_row(database, str(call["call_id"]))
    assert terminal["version"] == before["version"] + 1
    assert _request(app, "POST", "/mcp", authorization="Bearer agent-b-secret",
                    body={"request_id": "after-terminal-disconnect", "client_timeout_seconds": 2,
                          "payload": {"prompt": "new"}}).json()["call_state"] == "ringing"


def test_restart_and_equal_terminal_agent_deadlines_keep_terminal_disconnect_immutable(tmp_path: Path) -> None:
    app, clock, database = _components(tmp_path)
    call, owner = _accepted(app)
    # Arrange a tie directly in durable state: the specified ordering is
    # terminal-disconnected before agent-disconnected.
    with sqlite3.connect(database) as conn:
        conn.execute("UPDATE lifecycle_calls SET agent_reconnect_deadline=? WHERE call_id=?", (clock() + 180, call["call_id"]))
        conn.commit()
    clock.advance(180)
    # A fresh central lifecycle instance is the restart recovery path.
    restarted, _same_clock, _same_database = _components(tmp_path, clock=clock)
    terminal = _call_row(database, str(call["call_id"]))
    assert terminal["call_state"] == "disconnected"
    replay = _request(
        restarted, "POST", "/mcp", authorization="Bearer agent-secret",
        body={"request_id": "after-restart", "client_timeout_seconds": 2,
              "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": 1},
    )
    assert replay.json()["reason"] == "terminal_disconnected"
    assert replay.json()["human_action"] is False
    app.lifecycle.settle_deadlines()
    assert _call_row(database, str(call["call_id"]))["version"] == terminal["version"]
