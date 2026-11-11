"""Red contract for desktop-session actions against the central lifecycle.

The desktop action boundary must act on the same lifecycle database that owns
MCP calls.  It deliberately does not exercise the legacy parallel terminal
coordinator or presentation code.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import NotificationRouter, TerminalRegistry
from central_mcp_waiting_lifecycle import create_app


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _no_wait(_: float) -> None:
    return None


def _components(tmp_path: Path, *, route_mode: str = "desktop") -> tuple[Any, TerminalRegistry, FakeClock]:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-61",
        endpoint="https://10.10.0.61:9100/terminal/events",
        callback_credential="central-callback-secret",
        gui_ready=False,
    )
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-62",
        endpoint="https://10.10.0.62:9100/terminal/events",
        callback_credential="central-callback-secret-62",
        gui_ready=False,
    )
    service = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        operator_directory={
        "desktop-61": {"password": "desktop-secret", "devices": ["desktop-61"], "primary": True},
        "desktop-62": {"password": "desktop-62-secret", "devices": ["desktop-62"]},
        },
        device_state=lambda _device_id: [],
    )
    lifecycle_path = tmp_path / "lifecycle.sqlite3"

    notifications: list[dict[str, object]] = []

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        notifications.append(dict(payload))
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute(
                "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "routing.sqlite3",
        clock=clock,
        registry=registry,
        desktop_dispatch=desktop_dispatch,
        bark_dispatch=bark_dispatch,
        route_mode=route_mode,
        call_state=call_state,
        web_base_url="https://central.example.test",
    )
    app = create_app(
        database_path=lifecycle_path,
        credentials={"agent-a": "agent-secret", "agent-b": "agent-b-secret"},
        clock=clock,
        notify=router.enqueue,
        wait=_no_wait,
        route_admission=router.validate_route,
        notification_router=router,
        desktop_registration_service=service,
    )
    # Test-only observation of the real router dispatch boundary.
    app._contract_notifications = notifications
    return app, registry, clock


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    authorization: str | None = None,
    session: str | None = None,
) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if authorization is not None:
            headers["authorization"] = authorization
        if session is not None:
            headers["x-terminal-session"] = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://central.test"
        ) as client:
            return await client.request(method, path, json=body, headers=headers)

    return asyncio.run(run())


def _register(app: Any, *, credential: str = "desktop-secret") -> str:
    device_suffix = "61" if credential == "desktop-secret" else "62"
    response = _request(
        app,
        "POST",
        "/api/terminals/register",
        body={"callsign": f"desktop-{device_suffix}", "password": credential,
              "device_id": f"desktop-{device_suffix}", "terminal_platform": "windows",
              "endpoint": f"https://10.10.0.{device_suffix}:9100/terminal/events", "gui_ready": True},
    )
    assert response.status_code == 201
    return str(response.json()["session_id"])


def _ringing_call(app: Any, request_id: str = "create-call") -> dict[str, object]:
    response = _request(
        app,
        "POST",
        "/mcp",
        authorization="Bearer agent-secret",
        body={"request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "act"}},
    )
    assert response.status_code == 200
    call = response.json()
    assert call["call_state"] == "ringing"
    return call


def test_desktop_session_reads_its_routed_tool_payload_before_rendering(tmp_path: Path) -> None:
    """The independent terminal gets presentation data via its own session."""
    app, _registry, _clock = _components(tmp_path)
    session = _register(app)
    response = _request(
        app, "POST", "/mcp", authorization="Bearer agent-secret",
        body={
            "request_id": "desktop-tool-payload", "client_timeout_seconds": 2,
            "payload": {"tool": "get_user_choice", "prompt": "Pick one", "choices": ["one", "two"]},
        },
    )
    call = response.json()

    view = _request(app, "GET", f"/api/calls/{call['call_id']}", session=session)
    assert view.status_code == 200
    assert view.json()["can_accept"] is True
    assert view.json()["payload"] == {
        "tool": "get_user_choice", "prompt": "Pick one", "choices": ["one", "two"],
    }

    foreign = _request(app, "GET", f"/api/calls/{call['call_id']}", session="foreign")
    assert foreign.status_code in {401, 404}


def test_desktop_session_accepts_one_routed_ringing_call_with_durable_lifecycle_ownership(
    tmp_path: Path,
) -> None:
    app, registry, _clock = _components(tmp_path)
    session = _register(app)
    call = _ringing_call(app)

    accepted = _request(
        app,
        "POST",
        f"/api/calls/{call['call_id']}/accept",
        session=session,
        body={"operation_id": "desktop-accept-1"},
    )
    assert accepted.status_code == 200
    assert accepted.json() == {
        "success": True,
        "status": "opened",
        "call_state": "accepted",
        "human_action": True,
        "needs_continuation": True,
        "interaction_id": call["interaction_id"],
        "call_id": call["call_id"],
        "event_seq": 1,
        "owner_device_id": "desktop-61",
        "owner_session_id": session,
        "handled_on": "desktop",
        "terminal_platform": "windows",
    }
    replay = _request(
        app, "POST", f"/api/calls/{call['call_id']}/accept", session=session,
        body={"operation_id": "desktop-accept-1"},
    )
    assert replay.json() == accepted.json()
    assert registry.desktop_state("desktop-61") is not None


def test_decline_is_terminal_releases_busy_and_authoritative_terminal_state_is_versioned(tmp_path: Path) -> None:
    app, _registry, _clock = _components(tmp_path)
    session = _register(app)
    call = _ringing_call(app)

    declined = _request(
        app, "POST", f"/api/calls/{call['call_id']}/decline", session=session,
        body={"operation_id": "desktop-decline-1"},
    )
    assert declined.status_code == 200
    assert declined.json()["status"] == "declined"
    assert declined.json()["call_state"] == "declined"
    assert declined.json()["reason"] == "human_declined"
    assert declined.json()["human_action"] is True
    assert declined.json()["needs_continuation"] is False
    assert declined.json()["acted_on"] == "desktop"
    assert declined.json()["handled_on"] is None
    assert declined.json()["terminal_platform"] is None
    assert declined.json()["retryable"] is False
    assert "resubmittable" not in declined.json()
    state = _request(app, "GET", "/api/terminals/state", session=session)
    assert state.status_code == 200
    assert state.json()["states"] == [{
        "call_id": call["call_id"], "call_state": "declined", "version": 2,
    }]
    fresh = _ringing_call(app, "after-decline")
    assert fresh["call_state"] == "ringing"


def test_terminal_action_auth_deadline_route_and_race_are_opaque_or_single_winner(tmp_path: Path) -> None:
    app, _registry, clock = _components(tmp_path)
    owner = _register(app)
    call = _ringing_call(app)
    path = f"/api/calls/{call['call_id']}/accept"
    before = _request(app, "GET", "/api/terminals/state", session=owner).json()

    for session, authorization in ((None, None), ("foreign", None), (None, "Bearer agent-secret")):
        response = _request(
            app, "POST", path, session=session, authorization=authorization,
            body={"operation_id": "forbidden"},
        )
        assert response.status_code in {401, 404}
        assert response.json()["success"] is False
        assert _request(app, "GET", "/api/terminals/state", session=owner).json() == before

    # This call is admitted through the actual central iPhone-only route, so
    # no desktop was invited.  A registered desktop session cannot seize it.
    iphone_app, _iphone_registry, _iphone_clock = _components(tmp_path / "iphone", route_mode="iphone")
    iphone_session = _register(iphone_app)
    iphone_call = _ringing_call(iphone_app, "iphone-only-call")
    excluded = _request(
        iphone_app, "POST", f"/api/calls/{iphone_call['call_id']}/accept", session=iphone_session,
        body={"operation_id": "excluded"},
    )
    assert excluded.status_code in {400, 404}
    assert excluded.json()["success"] is False

    # Both desktops are valid registered sessions, but the desktop route was
    # concretely delivered to its deterministic target desktop-61.  The
    # other desktop cannot accept or decline that invitation.
    second = _register(app, credential="desktop-62-secret")
    asyncio.run(app.dispatch_notification_delivery())
    assert app._contract_notifications == [{
        "call_id": call["call_id"],
        "call_state": "ringing",
        "version": 1,
        "ring_deadline": clock() + 60,
        "device_id": "desktop-61",
        "endpoint": "https://10.10.0.61:9100/terminal/events",
        "callback_credential": "central-callback-secret",
        "gui_ready": True,
        "fresh": True,
    }]
    for action in ("accept", "decline"):
        denied = _request(
            app, "POST", f"/api/calls/{call['call_id']}/{action}", session=second,
            body={"operation_id": f"desktop-62-{action}"},
        )
        assert denied.status_code == 404
        assert denied.json() == {"success": False, "status": "session_not_found", "needs_continuation": False}
        # The original Agent still sees the same ringing call; a wrong
        # desktop cannot alter ownership, busy state, or its delivered event.
        assert _request(app, "GET", "/api/terminals/state", session=owner).json() == before
        assert app._contract_notifications == [app._contract_notifications[0]]

    accepted_owner = _request(
        app, "POST", path, session=owner, body={"operation_id": "desktop-61-after-62"}
    )
    assert accepted_owner.status_code == 200
    assert accepted_owner.json()["owner_device_id"] == "desktop-61"
    assert accepted_owner.json()["owner_session_id"] == owner

    deadline_app, _deadline_registry, deadline_clock = _components(tmp_path / "deadline")
    deadline_owner = _register(deadline_app)
    deadline_call = _ringing_call(deadline_app, "deadline-call")
    deadline_clock.advance(60)
    expired = _request(
        deadline_app,
        "POST",
        f"/api/calls/{deadline_call['call_id']}/accept",
        session=deadline_owner,
        body={"operation_id": "late"},
    )
    assert expired.json()["status"] == "not_accepted"
    assert expired.json()["reason"] == "ring_timeout"

    race_app, _race_registry, _race_clock = _components(tmp_path / "race")
    race_owner = _register(race_app)
    race = _ringing_call(race_app, "race-call")
    accepted = _request(race_app, "POST", f"/api/calls/{race['call_id']}/accept", session=race_owner,
                        body={"operation_id": "race-accept"})
    declined = _request(race_app, "POST", f"/api/calls/{race['call_id']}/decline", session=race_owner,
                        body={"operation_id": "race-decline"})
    assert {accepted.json()["call_state"], declined.json()["call_state"]} <= {"accepted", "declined"}
    assert accepted.json()["call_state"] == declined.json()["call_state"]
