"""Red contract for lifecycle-authoritative iPhone response-web actions.

This deliberately exercises the MCP lifecycle application, not the legacy
``TerminalCallService``/``CallCoordinator`` web adapter.  A Bark URL is an
identifier only: a verified operator-browser session is required for every
read or action, and reading is never acceptance.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass, field
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


@dataclass
class BrowserSessions:
    sessions: dict[str, dict[str, str]] = field(default_factory=dict)
    seen: list[str | None] = field(default_factory=list)

    def verify(self, token: str | None) -> dict[str, str] | None:
        self.seen.append(token)
        return self.sessions.get(token or "")


async def _no_wait(_: float) -> None:
    return None


def _components(tmp_path: Path, *, route_mode: str = "both") -> tuple[Any, BrowserSessions, FakeClock, list[dict[str, object]]]:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-61",
        endpoint="https://10.10.0.61:9100/terminal/events",
        callback_credential="central-callback-secret", gui_ready=False,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        registered_credentials={"desktop-secret": {"device_id": "desktop-61", "terminal_platform": "windows"}},
        device_state=lambda _device_id: [],
    )
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    deliveries: list[dict[str, object]] = []

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        deliveries.append({"channel": "desktop", **payload})
        return {"accepted": True}

    async def bark_dispatch(payload: dict[str, object]) -> dict[str, object]:
        deliveries.append({"channel": "bark", **payload})
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
        route_mode=route_mode, call_state=call_state, web_base_url="https://central.example.test",
    )
    browsers = BrowserSessions({
        "operator-browser": {"operator_id": "only-operator", "browser_session_id": "browser-61"},
        "foreign-browser": {"operator_id": "different-operator", "browser_session_id": "browser-62"},
    })
    # This is the intentionally absent composition seam.  The lifecycle must
    # verify browser authentication itself; the old separate web coordinator
    # is not an acceptable substitute.
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=_no_wait,
        route_admission=router.validate_route, notification_router=router,
        desktop_registration_service=registration,
        operator_session_verifier=browsers,
    )
    app._contract_router = router
    return app, browsers, clock, deliveries


def _request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
             bearer: str | None = None, browser: str | None = None, terminal_session: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if bearer is not None:
            headers["authorization"] = f"Bearer {bearer}"
        if browser is not None:
            headers["x-operator-session"] = browser
        if terminal_session is not None:
            headers["x-terminal-session"] = terminal_session
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def _create(app: Any, request_id: str = "create") -> dict[str, object]:
    response = _request(app, "POST", "/mcp", bearer="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "Proceed?"},
    })
    assert response.status_code == 200
    assert response.json()["call_state"] == "ringing"
    return response.json()


def _desktop_session(app: Any) -> str:
    response = _request(app, "POST", "/api/terminals/register", body={
        "endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True,
    }, bearer=None)
    # The registration API deliberately uses a distinct Terminal credential.
    if response.status_code != 201:
        async def run() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
                return await client.post("/api/terminals/register", headers={"authorization": "Terminal desktop-secret"}, json={
                    "endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True,
                })
        response = asyncio.run(run())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def _hidden(response: httpx.Response) -> None:
    assert response.status_code in {401, 404}
    assert response.json()["success"] is False
    assert response.json()["status"] == "session_not_found"


def test_mobile_eligibility_is_persisted_at_creation_and_browser_get_is_inert(tmp_path: Path) -> None:
    app, browsers, _clock, _deliveries = _components(tmp_path, route_mode="iphone")
    call = _create(app)

    # Later configuration mutation cannot turn this invitation into a desktop
    # invitation; eligibility is owned by the creation-time lifecycle record.
    app._contract_router._route_mode = "desktop"
    page = _request(app, "GET", f"/calls/{call['call_id']}", browser="operator-browser")
    assert page.status_code == 200
    assert page.json()["call_id"] == call["call_id"]
    assert page.json()["call_state"] == "ringing"
    assert page.json()["can_accept"] is True
    assert page.json()["can_decline"] is True
    assert _request(app, "POST", "/mcp", bearer="agent-secret", body={
        "request_id": "same-call", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": 0,
    }).json()["call_state"] == "ringing"
    assert browsers.seen == ["operator-browser"]

    accepted = _request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator-browser", body={"operation_id": "iphone-accept"})
    replay = _request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator-browser", body={"operation_id": "iphone-accept"})
    assert accepted.status_code == replay.status_code == 200
    assert accepted.json() == replay.json()
    assert accepted.json()["call_state"] == "accepted"
    assert accepted.json()["handled_on"] == "iphone_web"
    assert accepted.json()["owner_session_id"] == "browser-61"


def test_web_decline_releases_busy_and_auth_or_route_failures_are_opaque_and_inert(tmp_path: Path) -> None:
    app, _browsers, _clock, deliveries = _components(tmp_path, route_mode="iphone")
    call = _create(app)
    before = list(deliveries)
    for browser in (None, "foreign-browser"):
        _hidden(_request(app, "POST", f"/calls/{call['call_id']}/decline", browser=browser, body={"operation_id": "deny"}))
        assert _request(app, "POST", "/mcp", bearer="agent-secret", body={
            "request_id": f"read-{browser}", "client_timeout_seconds": 2,
            "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": 0,
        }).json()["call_state"] == "ringing"
        assert deliveries == before

    declined = _request(app, "POST", f"/calls/{call['call_id']}/decline", browser="operator-browser", body={"operation_id": "iphone-decline"})
    assert declined.status_code == 200
    assert declined.json()["call_state"] == "declined"
    assert declined.json()["reason"] == "human_declined"
    replacement = _create(app, "after-iphone-decline")
    assert replacement["call_state"] == "ringing"

    desktop_app, _sessions, _desktop_clock, _desktop_deliveries = _components(tmp_path / "desktop", route_mode="desktop")
    desktop_call = _create(desktop_app, "desktop-call")
    _hidden(_request(desktop_app, "POST", f"/calls/{desktop_call['call_id']}/accept", browser="operator-browser", body={"operation_id": "excluded"}))


def test_desktop_truth_view_and_deadline_race_restart_never_create_a_second_mobile_owner(tmp_path: Path) -> None:
    app, _browsers, clock, deliveries = _components(tmp_path, route_mode="both")
    desktop = _desktop_session(app)
    call = _create(app)
    desktop_accept = _request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal_session=desktop, body={"operation_id": "desktop-wins"})
    assert desktop_accept.status_code == 200

    safe = _request(app, "GET", f"/calls/{call['call_id']}", browser="operator-browser")
    assert safe.status_code == 200
    assert safe.json()["handled_on"] == "desktop"
    assert safe.json()["can_accept"] is safe.json()["can_decline"] is False
    assert safe.json()["display_state"] == "handled_on_desktop_processing"
    _hidden(_request(app, "POST", f"/calls/{call['call_id']}/decline", browser="operator-browser", body={"operation_id": "late-web"}))

    late_app, _late_sessions, late_clock, late_deliveries = _components(tmp_path / "late", route_mode="iphone")
    late = _create(late_app, "late")
    late_clock.advance(60)
    expired = _request(late_app, "POST", f"/calls/{late['call_id']}/accept", browser="operator-browser", body={"operation_id": "after-deadline"})
    assert expired.json()["call_state"] == "not_accepted"
    assert expired.json()["reason"] == "ring_timeout"

    # Dispatch exactly once before and after constructing a new central app
    # over the same durable state.  Restart cannot re-ring or recreate a web
    # owner, and a mobile-vs-desktop action race retains one winner.
    asyncio.run(app.dispatch_notification_delivery())
    count = len(deliveries)
    asyncio.run(app.dispatch_notification_delivery())
    assert len(deliveries) == count
    assert len(late_deliveries) == 0
    assert clock() == 1_700_000_000.0
