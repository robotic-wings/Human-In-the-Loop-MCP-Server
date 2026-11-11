"""Red contract for the lifecycle-owned mobile browser response page.

The central lifecycle remains the only call-state authority.  This contract
requires a narrow HTML/cookie/CSRF adapter on that ASGI application; it does
not exercise the old TerminalCallService or CallCoordinator web adapters.
"""

from __future__ import annotations

import asyncio
import re
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import httpx
import pytest

from central_http_service import CentralServiceConfig, _ConfiguredOperatorSessionVerifier, create_lifecycle_app
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


@dataclass
class RuntimeState:
    """Durable paths plus separately owned, already-authenticated seams."""

    root: Path
    clock: Clock
    registry: TerminalRegistry
    registration: DesktopRegistrationStateService
    sessions: CookieBrowserSessions


async def _no_wait(_: float) -> None:
    return None


def _components(tmp_path: Path, *, route_mode: str = "both") -> tuple[Any, RuntimeState]:
    clock = Clock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-61",
        endpoint="https://desktop.example.test:9100/terminal/events",
        callback_credential="callback-secret", gui_ready=True,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        operator_directory={"desktop-61": {"password": "desktop-secret", "devices": ["desktop-61"], "primary": True}},
        device_state=lambda _device: [],
    )
    state = RuntimeState(tmp_path, clock, registry, registration, CookieBrowserSessions({
        "operator-cookie": {"operator_id": "only-operator", "browser_session_id": "browser-61", "csrf_token": "csrf-61"},
        "foreign-cookie": {"operator_id": "foreign", "browser_session_id": "browser-62", "csrf_token": "csrf-62"},
    }))
    return _make_app(state, route_mode=route_mode), state


def _make_app(state: RuntimeState, *, route_mode: str) -> Any:
    lifecycle_path = state.root / "lifecycle.sqlite3"

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute("SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
        return None if row is None else str(row[0])

    async def accepted(_payload: Mapping[str, object]) -> Mapping[str, object]:
        return {"accepted": True, "http_status": 200, "business_success": True}

    router = NotificationRouter(
        database_path=state.root / "router.sqlite3", clock=state.clock, registry=state.registry,
        desktop_dispatch=accepted, bark_dispatch=accepted, route_mode=route_mode,
        call_state=call_state, web_base_url="https://central.example.test",
    )
    # These deliberately absent lifecycle seams make the contract Red.  The
    # implementation must render real HTML and authenticate a cookie itself,
    # rather than reuse the private x-operator-session testing header.
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=state.clock, notify=router.enqueue, wait=_no_wait,
        route_admission=router.validate_route, notification_router=router,
        desktop_registration_service=state.registration,
        browser_cookie_authenticator=state.sessions,
        mobile_html_renderer=lambda view, csrf_token: {"view": view, "csrf_token": csrf_token},
        trusted_browser_origin="https://central.example.test",
    )
    return app


def _request(app: Any, method: str, path: str, *, cookie: str | None = None,
             raw_cookie: str | None = None,
             form: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None,
             bearer: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        actual = dict(headers or {})
        assert cookie is None or raw_cookie is None
        if cookie is not None:
            actual["cookie"] = f"operator_session={cookie}"
        if raw_cookie is not None:
            actual["cookie"] = raw_cookie
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


def _configured_lifecycle_app(tmp_path: Path, *, operator_id: str = "only-operator") -> Any:
    """Exercise the central runtime factory, not a direct lifecycle seam."""
    config = CentralServiceConfig.from_mapping({
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "web_base_url": "https://central.example.test",
        "data_dir": str(tmp_path / "configured-central"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "test-cert.pem",
        "tls_key_file": "test-key.pem",
        "route_mode": "iphone",
        "bark_endpoint": "https://bark.example.test/push",
        "bark_credential": "bark-secret",
        "operator_sessions": {
            "operator-cookie": {
                "operator_id": operator_id,
                "browser_session_id": "browser-61",
                "csrf_token": "csrf-61",
            },
            "foreign-cookie": {
                "operator_id": "foreign",
                "browser_session_id": "browser-62",
                "csrf_token": "csrf-62",
            },
        },
        # This is a protected configuration reference for the operator's
        # login credential, deliberately distinct from the browser cookie.
        # The contract does not prescribe its storage implementation.
        "operator_login_credentials": {
            "operator-login-secret": "operator-cookie",
        },
    })
    verifier = _ConfiguredOperatorSessionVerifier(config.operator_sessions)

    async def delivered(_payload: Mapping[str, object]) -> Mapping[str, object]:
        return {"accepted": True, "http_status": 200, "business_success": True}

    # Passing the configured verifier through both browser seams is the
    # production composition requirement.  The lifecycle must therefore use
    # cookie HTML mode rather than silently falling back to x-operator-session.
    return create_lifecycle_app(
        config, clock=Clock(), wait=_no_wait, bark_dispatch=delivered,
        desktop_dispatch=delivered, operator_session_verifier=verifier,
        browser_cookie_authenticator=verifier,
        scheduler_sleep=_no_wait, notification_scheduler_sleep=_no_wait,
    )


def _strip_operation_ids(text: str) -> str:
    """Neutralise the fresh-per-render operation_id so an inert read still compares byte-identically."""

    return re.sub(r'name="operation_id" value="[^"]*"', 'name="operation_id" value=""', text)


def test_cookie_authenticated_initial_page_is_html_and_inert_while_link_or_foreign_session_is_opaque(tmp_path: Path) -> None:
    app, _state = _components(tmp_path, route_mode="iphone")
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


def test_fallback_accept_page_operation_ids_are_unique_uuids(tmp_path: Path) -> None:
    app, _state = _components(tmp_path, route_mode="iphone")
    call = _create(app)
    page = _request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert page.status_code == 200
    ids = re.findall(r'name="operation_id" value="([^"]+)"', page.text)
    assert len(ids) == 2
    for op in ids:
        uuid.UUID(op)
    assert ids[0] != ids[1]


def test_protected_form_actions_enforce_cookie_csrf_origin_and_host_then_preserve_submission_replay(tmp_path: Path) -> None:
    app, _state = _components(tmp_path, route_mode="iphone")
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
    app, state = _components(tmp_path, route_mode="both")
    call = _create(app)
    async def desktop_accept() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            registered = await client.post("/api/terminals/register", json={"callsign": "desktop-61", "password": "desktop-secret", "device_id": "desktop-61", "terminal_platform": "windows", "endpoint": "https://desktop.example.test:9100/terminal/events", "gui_ready": True})
            return await client.post(f"/api/calls/{call['call_id']}/accept", headers={"x-terminal-session": registered.json()["session_id"]}, json={"operation_id": "desktop"})
    assert asyncio.run(desktop_accept()).status_code == 200
    desktop_view = _request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert desktop_view.status_code == 200
    assert "电脑端处理中" in desktop_view.text
    assert "accept" not in desktop_view.text.lower()
    foreign = _request(app, "POST", f"/calls/{call['call_id']}/decline", cookie="foreign-cookie", form={"operation_id": "foreign", "csrf_token": "csrf-62"}, headers={"origin": "https://central.example.test", "host": "central.example.test"})
    assert foreign.status_code in {401, 403, 404}
    assert call["call_id"] not in foreign.text
    # Recreate only the lifecycle application/router over the same durable
    # paths.  The browser-authentication and terminal-registration seams are
    # retained: this is not a claim that those separate session stores are
    # durable.  The restarted app must neither re-ring nor grant the browser
    # a second owner or a writable form for this desktop-owned call.
    restarted = _make_app(state, route_mode="both")
    restarted_view = _request(restarted, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert restarted_view.status_code == 200
    assert "电脑端处理中" in restarted_view.text
    assert "accept" not in restarted_view.text.lower()
    restarted_foreign = _request(restarted, "POST", f"/calls/{call['call_id']}/decline", cookie="foreign-cookie", form={"operation_id": "foreign-after-restart", "csrf_token": "csrf-62"}, headers={"origin": "https://central.example.test", "host": "central.example.test"})
    assert restarted_foreign.status_code in {401, 403, 404}
    assert call["call_id"] not in restarted_foreign.text
    with sqlite3.connect(state.root / "router.sqlite3") as conn:
        deliveries = conn.execute("SELECT count(*) FROM notification_outbox WHERE call_id=?", (call["call_id"],)).fetchone()[0]
    assert deliveries == 2


def test_configured_runtime_composition_uses_cookie_html_not_legacy_header_json(tmp_path: Path) -> None:
    """Configured iPhone routing must activate the protected browser path."""
    app = _configured_lifecycle_app(tmp_path)
    call = _create(app, request_id="configured-cookie-call")

    page = _request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "csrf-61" in page.text
    assert "accept" in page.text.lower()

    # A legacy header cannot bypass the configured cookie session, and neither
    # a missing nor a foreign cookie can reveal or mutate this call.
    header_only = _request(
        app, "GET", f"/calls/{call['call_id']}",
        headers={"x-operator-session": "operator-cookie"},
    )
    assert header_only.status_code in {401, 404}
    for cookie in (None, "foreign-cookie"):
        denied = _request(app, "GET", f"/calls/{call['call_id']}", cookie=cookie)
        assert denied.status_code in {401, 404}
        assert call["call_id"] not in denied.text

    bad_origin = _request(
        app, "POST", f"/calls/{call['call_id']}/accept", cookie="operator-cookie",
        form={"operation_id": "accept", "csrf_token": "csrf-61"},
        headers={"origin": "https://evil.example", "host": "evil.example"},
    )
    assert bad_origin.status_code in {401, 403, 404}
    assert _strip_operation_ids(_request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie").text) == _strip_operation_ids(page.text)


def test_configured_bark_call_link_establishes_authenticated_cookie_without_exposing_credentials(tmp_path: Path) -> None:
    """A Bark link starts a protected browser entry flow, not a header-only API.

    An iPhone has no preexisting cookie when it opens the central call link.
    It must authenticate through a server-side login entry, receive a secure
    cookie, and then return to the lifecycle-owned ringing view.  The raw link
    and unauthenticated login page must not disclose credentials, browser
    session, CSRF value, or call state.
    """
    app = _configured_lifecycle_app(tmp_path)
    call = _create(app, request_id="configured-login-entry")
    call_path = f"/calls/{call['call_id']}"

    records = app._notification_router.outbox_records(str(call["call_id"]))
    bark_payload = next(record["payload"] for record in records if record["channel"] == "bark")
    assert bark_payload == {
        "call_id": call["call_id"],
        "url": f"https://central.example.test{call_path}",
        "call": "1",
        "title": "agent-a",
        "body": "Approve change?",
    }
    assert "operator-login-secret" not in str(bark_payload)
    assert "operator-cookie" not in str(bark_payload)

    # A bare link gives an unauthenticated browser a generic HTML login entry,
    # never session_not_found JSON or a call state.  Its opaque entry value is
    # the only continuation state supplied to the login form.
    entry = _request(app, "GET", call_path)
    assert entry.status_code == 401
    assert entry.headers["content-type"].startswith("text/html")
    assert "<form" in entry.text.lower()
    assert "operator-login-secret" not in entry.text
    assert "operator-cookie" not in entry.text
    assert "csrf-61" not in entry.text
    assert "ringing" not in entry.text.lower()
    assert call["call_id"] not in entry.text
    entry_cookie = entry.headers.get("set-cookie", "")
    assert "browser_entry=" in entry_cookie
    assert "httponly" in entry_cookie.lower()
    assert "secure" in entry_cookie.lower()
    assert "samesite=lax" in entry_cookie.lower()
    entry_token = entry_cookie.split("browser_entry=", 1)[1].split(";", 1)[0]

    # The login write is bound to the central HTTPS origin.  A foreign Origin
    # cannot mint a browser cookie or advance this call.
    rejected = _request(
        app, "POST", "/operator/login", raw_cookie=f"browser_entry={entry_token}",
        form={"credential": "operator-login-secret"},
        headers={"origin": "https://evil.example", "host": "evil.example"},
    )
    assert rejected.status_code in {401, 403, 404}
    assert "operator_session=" not in rejected.headers.get("set-cookie", "")

    established = _request(
        app, "POST", "/operator/login", raw_cookie=f"browser_entry={entry_token}",
        form={"credential": "operator-login-secret"},
        headers={"origin": "https://central.example.test", "host": "central.example.test"},
    )
    assert established.status_code in {302, 303}
    assert established.headers["location"] == call_path
    session_cookie = established.headers["set-cookie"]
    assert "operator_session=" in session_cookie
    assert "httponly" in session_cookie.lower()
    assert "secure" in session_cookie.lower()
    assert "samesite=lax" in session_cookie.lower()
    session_token = session_cookie.split("operator_session=", 1)[1].split(";", 1)[0]
    assert session_token != "operator-login-secret"

    # Following the redirect authenticates the same operator into the true
    # lifecycle state.  Reading remains inert and yields the existing CSRF
    # protected explicit action form rather than implicit acceptance.
    page = _request(app, "GET", call_path, cookie=session_token)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "csrf-61" in page.text
    assert "accept" in page.text.lower()
    assert _strip_operation_ids(_request(app, "GET", call_path, cookie=session_token).text) == _strip_operation_ids(page.text)

    foreign = _request(app, "GET", call_path, cookie="foreign-cookie")
    assert foreign.status_code in {401, 404}
    assert call["call_id"] not in foreign.text


def test_login_entry_succeeds_without_origin_header_for_inapp_browser(tmp_path: Path) -> None:
    """An in-app iPhone browser may omit Origin on a same-origin form post.

    The one-time login-entry cookie plus the always-present Host header are the
    durable cross-site defences; a missing Origin must not turn a valid login
    into ``session_not_found``.
    """
    app = _configured_lifecycle_app(tmp_path)
    call = _create(app, request_id="no-origin-login")
    call_path = f"/calls/{call['call_id']}"

    entry = _request(app, "GET", call_path)
    entry_token = entry.headers["set-cookie"].split("browser_entry=", 1)[1].split(";", 1)[0]

    established = _request(
        app, "POST", "/operator/login", raw_cookie=f"browser_entry={entry_token}",
        form={"credential": "operator-login-secret"},
        headers={"host": "central.example.test"},
    )
    assert established.status_code in {302, 303}
    assert established.headers["location"] == call_path


def test_configured_operator_identity_is_derived_from_login_sessions_not_hardcoded(tmp_path: Path) -> None:
    """A real operator identity must own its call, not a hardcoded literal.

    The production deployment names one operator.  Deriving that identity from
    the configured login sessions makes a non-``only-operator`` operator id
    read its own call instead of turning a successful login into an opaque
    ``session_not_found`` after the redirect.
    """
    app = _configured_lifecycle_app(tmp_path, operator_id="kefan")
    call = _create(app, request_id="named-operator-call")
    entry = _request(app, "GET", f"/calls/{call['call_id']}")
    entry_token = entry.headers["set-cookie"].split("browser_entry=", 1)[1].split(";", 1)[0]
    login = _request(
        app, "POST", "/operator/login", raw_cookie=f"browser_entry={entry_token}",
        form={"credential": "operator-login-secret"},
        headers={"origin": "https://central.example.test", "host": "central.example.test"},
    )
    assert login.status_code in {302, 303}
    cookie = login.headers["set-cookie"].split("operator_session=", 1)[1].split(";", 1)[0]
    page = _request(app, "GET", f"/calls/{call['call_id']}", cookie=cookie)
    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "accept" in page.text.lower()


def test_configured_login_credentials_must_target_a_single_operator(tmp_path: Path) -> None:
    """Two login credentials naming different operators is a config error."""
    with pytest.raises(ValueError):
        CentralServiceConfig.from_mapping({
            "listen_host": "127.0.0.1",
            "listen_port": 8443,
            "web_base_url": "https://central.example.test",
            "data_dir": str(tmp_path / "multi-operator"),
            "agent_credentials": {"agent-a": "agent-secret"},
            "tls_cert_file": "test-cert.pem",
            "tls_key_file": "test-key.pem",
            "route_mode": "iphone",
            "bark_endpoint": "https://bark.example.test/push",
            "bark_credential": "bark-secret",
            "operator_sessions": {
                "operator-cookie": {"operator_id": "operator-a", "browser_session_id": "browser-61", "csrf_token": "csrf-61"},
                "other-cookie": {"operator_id": "operator-b", "browser_session_id": "browser-62", "csrf_token": "csrf-62"},
            },
            "operator_login_credentials": {
                "login-a": "operator-cookie",
                "login-b": "other-cookie",
            },
        })


def test_mobile_accept_dispatches_accepted_callback_to_desktop(tmp_path: Path) -> None:
    recorded: list[dict[str, object]] = []

    async def record_desktop(payload: Mapping[str, object]) -> Mapping[str, object]:
        recorded.append(dict(payload))
        return {"accepted": True}

    async def noop_bark(_payload: Mapping[str, object]) -> Mapping[str, object]:
        return {"http_status": 200, "business_success": True}

    config = CentralServiceConfig.from_mapping({
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "web_base_url": "https://central.example.test",
        "data_dir": str(tmp_path / "accepted-callback"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "test-cert.pem",
        "tls_key_file": "test-key.pem",
        "route_mode": "both",
        "desktop_credentials": {"desktop-secret": {"device_id": "desktop-61", "terminal_platform": "windows"}},
        "desktop_endpoints": {"desktop-61": {"endpoint": "https://desktop.example.test:9100/terminal/events", "callback_credential": "callback-secret"}},
        "bark_endpoint": "https://bark.example.test/push",
        "bark_credential": "bark-secret",
        "operator_sessions": {
            "operator-cookie": {"operator_id": "desktop-61", "browser_session_id": "browser-61", "csrf_token": "csrf-61"},
        },
        "operator_login_credentials": {"operator-login-secret": "operator-cookie"},
    })
    verifier = _ConfiguredOperatorSessionVerifier(config.operator_sessions)
    app = create_lifecycle_app(
        config, clock=Clock(), wait=_no_wait, bark_dispatch=noop_bark,
        desktop_dispatch=record_desktop, operator_session_verifier=verifier,
        browser_cookie_authenticator=verifier,
        scheduler_sleep=_no_wait, notification_scheduler_sleep=_no_wait,
    )

    async def register() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            return await client.post("/api/terminals/register", json={"callsign": "desktop-61", "password": "desktop-secret", "device_id": "desktop-61", "terminal_platform": "windows", "endpoint": "https://desktop.example.test:9100/terminal/events", "gui_ready": True})
    assert asyncio.run(register()).status_code == 201

    call = _create(app, request_id="accepted-callback-call")

    asyncio.run(app.dispatch_notification_delivery())
    assert recorded, "the initial ringing callback must be dispatched to the desktop"
    initial = recorded[0]
    assert initial.get("call_state") == "ringing"

    accept = _request(
        app, "POST", f"/calls/{call['call_id']}/accept", cookie="operator-cookie",
        form={"operation_id": "mobile-accept-1", "csrf_token": "csrf-61"},
        headers={"origin": "https://central.example.test", "host": "central.example.test"},
    )
    assert accept.status_code == 200

    asyncio.run(app.dispatch_notification_delivery())
    accepted = [r for r in recorded if r.get("call_state") == "accepted"]
    assert accepted, "mobile accept must dispatch an accepted callback to the desktop"
    assert accepted[0].get("version", 0) > initial.get("version", 0)
    assert accepted[0].get("handled_on") == "iphone_web", (
        "the accepted desktop callback must carry handled_on so the desktop can "
        "show the handled hint even when read_call now returns session_not_found"
    )
