"""Red contract for desktop-session actions against the central lifecycle.

The desktop action boundary must act on the same lifecycle database that owns
MCP calls.  It deliberately does not exercise the legacy parallel terminal
coordinator or presentation code.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import TerminalRegistry
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


def _components(tmp_path: Path) -> tuple[Any, TerminalRegistry, FakeClock]:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-61",
        endpoint="https://10.10.0.61:9100/terminal/events",
        callback_credential="central-callback-secret",
        gui_ready=False,
    )
    service = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        registered_credentials={
            "desktop-secret": {"device_id": "desktop-61", "terminal_platform": "windows"}
        },
        device_state=lambda _device_id: [],
    )
    app = create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret", "agent-b": "agent-b-secret"},
        clock=clock,
        notify=lambda _event: None,
        wait=_no_wait,
        desktop_registration_service=service,
    )
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


def _register(app: Any) -> str:
    response = _request(
        app,
        "POST",
        "/api/terminals/register",
        authorization="Terminal desktop-secret",
        body={"endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True},
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

    # A route excluded from this desktop is indistinguishable from an unknown
    # target and cannot take ownership.
    excluded = _request(
        app, "POST", f"/api/calls/{call['call_id']}/accept", session=owner,
        body={"operation_id": "excluded", "route": "iphone"},
    )
    assert excluded.status_code in {400, 404}
    assert excluded.json()["success"] is False

    clock.advance(60)
    expired = _request(app, "POST", path, session=owner, body={"operation_id": "late"})
    assert expired.json()["status"] == "not_accepted"
    assert expired.json()["reason"] == "ring_timeout"

    race = _ringing_call(app, "race-call")
    accepted = _request(app, "POST", f"/api/calls/{race['call_id']}/accept", session=owner,
                        body={"operation_id": "race-accept"})
    declined = _request(app, "POST", f"/api/calls/{race['call_id']}/decline", session=owner,
                        body={"operation_id": "race-decline"})
    assert {accepted.json()["call_state"], declined.json()["call_state"]} <= {"accepted", "declined"}
    assert accepted.json()["call_state"] == declined.json()["call_state"]
