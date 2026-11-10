"""Red contract for security and session gaps (SPEC §7/§9.3/§5.4/§10.2)."""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
from pathlib import Path
from typing import Any, Mapping

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_http_service import CentralServiceConfig, _ConfiguredOperatorSessionVerifier, create_lifecycle_app
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


def components(tmp_path: Path) -> Any:
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
    return create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
    )


def configured_app(tmp_path: Path) -> Any:
    config = CentralServiceConfig.from_mapping({
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "web_base_url": "https://central.example.test",
        "data_dir": str(tmp_path / "configured"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "test-cert.pem",
        "tls_key_file": "test-key.pem",
        "route_mode": "iphone",
        "bark_endpoint": "https://bark.example.test/push",
        "bark_credential": "bark-secret",
        "operator_sessions": {
            "operator-cookie": {"operator_id": "only-operator", "browser_session_id": "browser-61", "csrf_token": "csrf-61"},
        },
        "operator_login_credentials": {"operator-login-secret": "operator-cookie"},
    })
    verifier = _ConfiguredOperatorSessionVerifier(config.operator_sessions)

    async def delivered(_payload: Any) -> dict[str, object]:
        return {"accepted": True, "http_status": 200, "business_success": True}

    return create_lifecycle_app(
        config, clock=Clock(), wait=no_wait, bark_dispatch=delivered,
        desktop_dispatch=delivered, operator_session_verifier=verifier,
        browser_cookie_authenticator=verifier,
        scheduler_sleep=no_wait, notification_scheduler_sleep=no_wait,
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, cookie: str | None = None,
            form: Mapping[str, str] | None = None, headers: Mapping[str, str] | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        actual = dict(headers or {})
        if agent: actual["authorization"] = f"Bearer {agent}"
        if cookie: actual["cookie"] = f"operator_session={cookie}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            return await client.request(method, path, headers=actual, json=body, data=form)
    return asyncio.run(run())


def rpc_call(app: Any, name: str, arguments: dict[str, object], rid: int = 1) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    return dict(response.json()["result"]["structuredContent"])


def _create_call(app: Any, request_id: str = "call") -> dict[str, object]:
    async def run() -> dict[str, object]:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.example.test") as client:
            response = await client.post("/mcp", headers={"authorization": "Bearer agent-secret"}, json={
                "request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "x"},
            })
        return response.json()
    return asyncio.run(run())


def test_health_check_reports_recent_bark_delivery(tmp_path: Path) -> None:
    app = components(tmp_path)
    result = rpc_call(app, "health_check", {})
    assert "bark" in result
    assert "recent_outcome" in result["bark"] or "last_delivery" in result["bark"]


def test_operator_password_is_stored_as_salted_hash(tmp_path: Path) -> None:
    module = importlib.import_module("central_http_service")
    hash_fn = getattr(module, "hash_operator_password", None)
    assert callable(hash_fn), "a management command must generate a salted operator password hash"
    digest = hash_fn("correct horse battery staple")
    assert isinstance(digest, str) and digest
    assert digest != "correct horse battery staple"


def test_logout_invalidates_current_session(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    logout = request(app, "POST", "/operator/logout", cookie="operator-cookie",
                     headers={"host": "central.example.test"})
    assert logout.status_code in {200, 302, 303}

    after = request(app, "GET", "/calls/some-call", cookie="operator-cookie")
    assert after.status_code in {401, 404}


def test_single_write_tab_per_browser_session(tmp_path: Path) -> None:
    app = configured_app(tmp_path)
    call = _create_call(app, "tab-call")
    accept = request(app, "POST", f"/calls/{call['call_id']}/accept", cookie="operator-cookie",
                     form={"operation_id": "tab-accept", "csrf_token": "csrf-61"},
                     headers={"origin": "https://central.example.test", "host": "central.example.test"})
    assert accept.status_code == 200

    first = request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    second = request(app, "GET", f"/calls/{call['call_id']}", cookie="operator-cookie")
    assert first.status_code == 200
    assert second.status_code == 200

    first_writable = 'action="/calls/' in first.text and "submit" in first.text.lower()
    second_writable = 'action="/calls/' in second.text and "submit" in second.text.lower()
    assert first_writable != second_writable
