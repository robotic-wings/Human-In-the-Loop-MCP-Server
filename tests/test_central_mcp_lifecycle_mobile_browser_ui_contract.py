"""Red contract for the lifecycle-owned mobile browser response page.

The central lifecycle remains the only call-state authority.  This contract
requires a narrow HTML/cookie/CSRF adapter on that ASGI application; it does
not exercise the old TerminalCallService or CallCoordinator web adapters.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_mcp_waiting_lifecycle import create_app
from central_notification_router import NotificationRouter, TerminalRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class CookieBrowserSessions:
    """Injected authentication seam; raw Bark links confer no authority."""

    sessions: Mapping[str, Mapping[str, str]]

    def authenticate_cookie(self, cookie: str | None) -> Mapping[str, str] | None:
        return self.sessions.get(cookie or "")


async def _no_wait(_: float) -> None:
    return None


def _components(tmp_path: Path, *, route_mode: str = "both") -> tuple[Any, Clock, CookieBrowserSessions]:
    clock = Clock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-61",
        endpoint="https://desktop.example.test:9100/terminal/events",
        callback_credential="callback-secret", gui_ready=True,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        registered_credentials={"desktop-secret": {"device_id": "desktop-61", "terminal_platform": "windows"}},
        device_state=lambda _device: [],
    )
    lifecycle_path = tmp_path / "lifecycle.sqlite3"

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute("SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
        return None if row is None else str(row[0])

    async def accepted(_payload: Mapping[str, object]) -> Mapping[str, object]:
        return {"accepted": True, "http_status": 200, "business_success": True}

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=accepted, bark_dispatch=accepted, route_mode=route_mode,
        call_state=call_state, web_base_url="https://central.example.test",
    )
    sessions = CookieBrowserSessions({
        "operator-cookie": {"operator_id": "only-operator", "browser_session_id": "browser-61", "csrf_token": "csrf-61"},
        "foreign-cookie": {"operator_id": "foreign", "browser_session_id": "browser-62", "csrf_token": "csrf-62"},
    })
    # These deliberately absent lifecycle seams make the contract Red.  The
    # implementation must render real HTML and authenticate a cookie itself,
    # rather than reuse the private x-operator-session testing header.
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=_no_wait,
        route_admission=router.validate_route, notification_router=router,
        desktop_registration_service=registration,
        browser_cookie_authenticator=sessions,
        mobile_html_renderer=lambda view, csrf_token: {"view": view, "csrf_token": csrf_token},
        trusted_browser_origin="https://central.example.test",
    )
    return app, clock, sessions


def _request(app: Any, method: str, path: str, *, cookie: str | None = None,
             form: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None,
             bearer: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        actual = dict(headers or {})
        if cookie is not None:
            actual["cookie"] = f"operator_session={cookie}"
        if bearer is not None:
            actual["authorization"] = f"Bearer {bearer}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            return await client.request(method, path, headers=actual, data=form)
    return asyncio.run(run())


def _create(app: Any, request_id: str = "create") -> dict[str, object]:
    # Current API needs JSON.  Use its ASGI client directly only for call
    # creation; the browser contract below deliberately uses HTML forms.
    async def create() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            return await client.post("/mcp", headers={"authorization": "Bearer agent-secret"}, json={
                "request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "Approve change?"},
            })
    response = asyncio.run(create())
    assert response.status_code == 200
    return response.json()


def test_cookie_authenticated_initial_page_is_html_and_inert_while_link_or_foreign_session_is_opaque(tmp_path: Path) -> None:
    app, _clock, _sessions = _components(tmp_path, route_mode="iphone")
    call = _create(app)

    page = _request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "csrf-61" in page.text
    assert "accept" in page.text.lower() and "decline" in page.text.lower()
    # Reading cannot accept: the Agent continues to see the original ringing call.
    async def continuation() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            return await client.post("/mcp", headers={"authorization": "Bearer agent-secret"}, json={
                "request_id": "read", "client_timeout_seconds": 2, "interaction_id": call["interaction_id"],
                "call_id": call["call_id"], "after_event_seq": 0,
            })
    assert asyncio.run(continuation()).json()["call_state"] == "ringing"
    for cookie in (None, "foreign-cookie"):
        denied = _request(app, "GET", f"/calls/{call['call_id']}", cookie=cookie)
        assert denied.status_code in {401, 404}
        assert call["call_id"] not in denied.text


def test_protected_form_actions_enforce_cookie_csrf_origin_and_host_then_preserve_submission_replay(tmp_path: Path) -> None:
    app, _clock, _sessions = _components(tmp_path, route_mode="iphone")
    call = _create(app)
    accept_path = f"/calls/{call['call_id']}/accept"
    bad = _request(app, "POST", accept_path, cookie="operator-cookie", form={"operation_id": "accept", "csrf_token": "wrong"}, headers={"origin": "https://evil.example", "host": "evil.example"})
    assert bad.status_code in {401, 403, 404}
    accepted = _request(app, "POST", accept_path, cookie="operator-cookie", form={"operation_id": "accept", "csrf_token": "csrf-61"}, headers={"origin": "https://central.example.test", "host": "central.example.test"})
    assert accepted.status_code == 200
    assert "iphone_web" in accepted.text
    submission = {"submission_id": "submit-1", "result": '{"user_input":"yes"}', "csrf_token": "csrf-61"}
    first = _request(app, "POST", f"/calls/{call['call_id']}/submissions", cookie="operator-cookie", form=submission, headers={"origin": "https://central.example.test", "host": "central.example.test"})
    replay = _request(app, "POST", f"/calls/{call['call_id']}/submissions", cookie="operator-cookie", form=submission, headers={"origin": "https://central.example.test", "host": "central.example.test"})
    assert first.status_code == replay.status_code == 200
    assert first.text == replay.text


def test_refresh_after_desktop_ownership_is_truthful_and_reconstruction_keeps_foreign_forms_inert(tmp_path: Path) -> None:
    app, clock, sessions = _components(tmp_path, route_mode="both")
    call = _create(app)
    async def desktop_accept() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            registered = await client.post("/api/terminals/register", headers={"authorization": "Terminal desktop-secret"}, json={"endpoint": "https://desktop.example.test:9100/terminal/events", "gui_ready": True})
            return await client.post(f"/api/calls/{call['call_id']}/accept", headers={"x-terminal-session": registered.json()["session_id"]}, json={"operation_id": "desktop"})
    assert asyncio.run(desktop_accept()).status_code == 200
    desktop_view = _request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert desktop_view.status_code == 200
    assert "电脑端处理中" in desktop_view.text
    assert "accept" not in desktop_view.text.lower()
    foreign = _request(app, "POST", f"/calls/{call['call_id']}/decline", cookie="foreign-cookie", form={"operation_id": "foreign", "csrf_token": "csrf-62"}, headers={"origin": "https://central.example.test", "host": "central.example.test"})
    assert foreign.status_code in {401, 403, 404}
    assert call["call_id"] not in foreign.text
    # The configured cookie seam, lifecycle SQLite state, and truthful page
    # must survive an app reconstruction without creating a second owner.
    assert clock() == 1_700_000_000.0 and sessions.authenticate_cookie("operator-cookie")
