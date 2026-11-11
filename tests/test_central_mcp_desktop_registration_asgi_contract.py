"""Red contract for composing desktop registration into the central ASGI app.

The independently runnable desktop must register and synchronize against the
same GUI-free central application that owns MCP call lifecycle.  This contract
does not exercise desktop callbacks, human call actions, or any GUI surface.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import TerminalRegistry


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lifecycle_module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def _components(tmp_path: Path) -> tuple[Any, DesktopRegistrationStateService, TerminalRegistry, FakeClock]:
    clock = FakeClock()
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
        operator_directory={
            "desktop-61": {"password": "desktop-secret", "devices": ["desktop-61"], "primary": True},
        },
        device_state=lambda device_id: [dict(state) for state in states.get(device_id, [])],
    )
    app = _lifecycle_module().create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret"},
        clock=clock,
        notify=lambda _: None,
        wait=lambda _: _sleep_never(),
        desktop_registration_service=service,
    )
    return app, service, registry, clock


async def _sleep_never(_: float) -> None:
    return None


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
        body={"callsign": "desktop-61", "password": "desktop-secret", "device_id": "desktop-61",
              "terminal_platform": "windows",
              "endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True},
    )
    assert response.status_code == 201
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "registered"
    assert isinstance(payload["session_id"], str)
    return str(payload["session_id"])


def test_lifecycle_asgi_composes_pre_registered_terminal_registration_without_agent_credential_confusion(
    tmp_path: Path,
) -> None:
    app, _service, registry, _clock = _components(tmp_path)
    before = registry.desktop_state("desktop-61")
    body = {"callsign": "desktop-61", "password": "forged-secret", "device_id": "desktop-61",
            "terminal_platform": "windows",
            "endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True}

    denied = _request(
        app, "POST", "/api/terminals/register", authorization="Bearer agent-secret", body=body
    )
    assert denied.status_code == 401
    assert denied.json() == {"success": False, "status": "terminal_not_authenticated"}
    assert registry.desktop_state("desktop-61") == before

    session = _register(app)
    assert session
    registered = registry.desktop_state("desktop-61")
    assert registered is not None and registered["gui_ready"] is True and registered["fresh"] is True

    # MCP has a separate Agent credential boundary; terminal registration does
    # not weaken it or reinterpret Terminal credentials as Agent credentials.
    mcp_denied = _request(
        app,
        "POST",
        "/mcp",
        authorization="Terminal desktop-secret",
        body={"request_id": "terminal-not-agent", "client_timeout_seconds": 2, "payload": {}},
    )
    assert mcp_denied.status_code == 401
    assert mcp_denied.json()["status"] == "unauthorized"


def test_session_heartbeat_refreshes_registry_but_authoritative_state_read_does_not(tmp_path: Path) -> None:
    app, _service, registry, clock = _components(tmp_path)
    session = _register(app)

    heartbeat = _request(
        app, "POST", "/api/terminals/heartbeat", session=session, body={"gui_ready": True}
    )
    assert heartbeat.status_code == 200
    assert heartbeat.json()["success"] is True
    clock.advance(30.01)
    state = _request(app, "GET", "/api/terminals/state", session=session)
    assert state.status_code == 200
    assert state.json() == {
        "success": True,
        "device_id": "desktop-61",
        "operator": "desktop-61",
        "states": [{"call_id": "call-ringing", "call_state": "ringing", "version": 7}],
    }
    # State synchronization is read-only: it cannot revive an expired desktop
    # listener lease after the last authenticated heartbeat.
    current = registry.desktop_state("desktop-61")
    assert current is not None and current["fresh"] is False


def test_missing_or_foreign_terminal_sessions_are_opaque_and_non_mutating(tmp_path: Path) -> None:
    app, _service, registry, _clock = _components(tmp_path)
    owner = _register(app)
    before = registry.desktop_state("desktop-61")

    for session in (None, "foreign-session"):
        heartbeat = _request(
            app, "POST", "/api/terminals/heartbeat", session=session, body={"gui_ready": False}
        )
        state = _request(app, "GET", "/api/terminals/state", session=session)
        for response in (heartbeat, state):
            assert response.status_code in {401, 404}
            assert response.json() == {"success": False, "status": "session_not_found"}
        assert registry.desktop_state("desktop-61") == before

    # A valid owner session remains usable after foreign probes.
    assert _request(app, "GET", "/api/terminals/state", session=owner).status_code == 200
