"""Red contract for desktop registration over the central GUI-free ASGI API.

This slice exercises only the central HTTP protocol around the already durable
desktop registry.  It deliberately excludes the desktop listener/UI, callback
transport, iPhone pages, TLS and deployment.
"""

from __future__ import annotations

import asyncio
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import TerminalRegistry
from central_terminal_call_service import TerminalCallService


@dataclass
class FakeClock:
    now: float = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _module():
    return importlib.import_module("central_terminal_http_service")


def _registration_service(tmp_path: Path, clock: FakeClock):
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-61",
        endpoint="https://10.10.0.61:9100/terminal/events",
        callback_credential="central-callback-secret",
        gui_ready=False,
    )
    states = {
        "desktop-61": [
            {"call_id": "call-ringing", "call_state": "ringing", "version": 7}
        ]
    }
    service = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        registered_credentials={
            "desktop-secret": {"device_id": "desktop-61", "terminal_platform": "windows"},
        },
        device_state=lambda device_id: [dict(item) for item in states.get(device_id, [])],
    )
    return registry, service


def _app(service: DesktopRegistrationStateService):
    # The terminal-call service is retained for the existing call endpoints;
    # registration is a separate, injected central authority.
    terminal_service = TerminalCallService(
        database_path=":memory:", coordinator=object(), clock=lambda: 0.0,
        registered_credentials={}, on_state_update=lambda _: None,
    )
    return _module().create_app(
        terminal_service=terminal_service,
        desktop_registration_service=service,
    )


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    credential: str | None = None,
    session: str | None = None,
) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if credential is not None:
            headers["authorization"] = f"Terminal {credential}"
        if session is not None:
            headers["x-terminal-session"] = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://central.test"
        ) as client:
            return await client.request(method, path, json=body, headers=headers)

    return asyncio.run(run())


def _register(app: Any, *, credential: str = "desktop-secret") -> str:
    response = _request(
        app,
        "POST",
        "/api/terminals/register",
        credential=credential,
        body={"endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "registered"
    assert set(payload) == {"success", "status", "session_id"}
    assert isinstance(payload["session_id"], str) and payload["session_id"]
    return str(payload["session_id"])


def _hidden(response: httpx.Response) -> None:
    assert response.status_code in {401, 404}
    assert response.json() == {"success": False, "status": "session_not_found"}


def test_terminal_register_requires_terminal_credential_and_preconfigured_endpoint_without_mutation(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry, service = _registration_service(tmp_path, clock)
    app = _app(service)
    before = registry.desktop_state("desktop-61")
    body = {"endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True}

    for credential in (None, "agent-secret", "forged-secret"):
        response = _request(app, "POST", "/api/terminals/register", credential=credential, body=body)
        assert response.status_code == 401
        assert response.json() == {"success": False, "status": "terminal_not_authenticated"}
        assert registry.desktop_state("desktop-61") == before

    mismatch = _request(
        app,
        "POST",
        "/api/terminals/register",
        credential="desktop-secret",
        body={"endpoint": "https://attacker.invalid/events", "gui_ready": True},
    )
    assert mismatch.status_code == 404
    assert mismatch.json() == {"success": False, "status": "endpoint_not_preconfigured"}
    assert registry.desktop_state("desktop-61") == before

    session = _register(app)
    assert session
    state = registry.desktop_state("desktop-61")
    assert state is not None and state["gui_ready"] is True and state["fresh"] is True


def test_unregistered_device_denial_is_non_disclosing_and_never_creates_endpoint(tmp_path: Path) -> None:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    service = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        registered_credentials={"unknown-desktop-secret": {"device_id": "unknown-desktop"}},
        device_state=lambda _: [],
    )
    app = _app(service)

    response = _request(
        app,
        "POST",
        "/api/terminals/register",
        credential="unknown-desktop-secret",
        body={"endpoint": "https://attacker.invalid/events", "gui_ready": True},
    )
    assert response.status_code == 404
    assert response.json() == {"success": False, "status": "endpoint_not_preconfigured"}
    assert registry.desktop_state("unknown-desktop") is None


def test_heartbeat_then_authoritative_state_read_are_session_bound_inert_and_versioned(tmp_path: Path) -> None:
    clock = FakeClock()
    _, service = _registration_service(tmp_path, clock)
    app = _app(service)
    session = _register(app)

    heartbeat = _request(
        app, "POST", "/api/terminals/heartbeat", session=session, body={"gui_ready": True}
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["success"] is True
    assert heartbeat.json()["fresh"] is True

    state = _request(app, "GET", "/api/terminals/state", session=session)
    assert state.status_code == 200
    assert state.json() == {
        "success": True,
        "states": [{"call_id": "call-ringing", "call_state": "ringing", "version": 7}],
    }
    # A state read cannot refresh the device lease; only the heartbeat above can.
    clock.now += 30.01
    assert _request(app, "GET", "/api/terminals/state", session=session).json()["success"] is True
    assert service._registry.desktop_state("desktop-61")["fresh"] is False  # type: ignore[attr-defined,index]


def test_heartbeat_freshness_is_durable_across_registry_restart(tmp_path: Path) -> None:
    clock = FakeClock()
    registry, service = _registration_service(tmp_path, clock)
    app = _app(service)
    session = _register(app)
    clock.now += 29
    assert _request(
        app, "POST", "/api/terminals/heartbeat", session=session, body={"gui_ready": True}
    ).status_code == 200

    restarted = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    assert restarted.desktop_state("desktop-61")["fresh"] is True  # type: ignore[index]
    clock.now += 30.01
    assert restarted.desktop_state("desktop-61")["fresh"] is False  # type: ignore[index]
    assert registry.desktop_state("desktop-61")["gui_ready"] is True  # type: ignore[index]


def test_absent_foreign_and_expired_sessions_do_not_read_or_mutate_terminal_state(tmp_path: Path) -> None:
    clock = FakeClock()
    registry, service = _registration_service(tmp_path, clock)
    app = _app(service)
    owner = _register(app)
    before = registry.desktop_state("desktop-61")

    # A session from another process/service and an expired/revoked session
    # are both opaque foreign capabilities to this central instance.
    for session in (None, "foreign-service-session", "expired-or-forged-session"):
        _hidden(_request(app, "POST", "/api/terminals/heartbeat", session=session, body={"gui_ready": False}))
        _hidden(_request(app, "GET", "/api/terminals/state", session=session))
        assert registry.desktop_state("desktop-61") == before

    # The valid session remains usable; denial must not invalidate it.
    assert _request(app, "GET", "/api/terminals/state", session=owner).status_code == 200
