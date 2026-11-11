"""Red contract: terminal registration never returns credentials or callback secrets."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import TerminalRegistry
from central_mcp_waiting_lifecycle import create_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


async def _wait(_: float) -> None:
    return None


def _app(tmp_path: Path) -> Any:
    clock = Clock()
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
        operator_directory={
            "desktop-61": {"password": "desktop-secret", "devices": ["desktop-61"], "primary": True},
        },
        device_state=lambda _device_id: [{"call_id": "call-1", "call_state": "ringing", "version": 1}],
    )
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret"},
        clock=clock,
        notify=lambda _event: None,
        wait=_wait,
        desktop_registration_service=service,
    )


def _request(app: Any, method: str, path: str, *, headers: dict[str, str] | None = None,
             body: dict[str, object] | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://central.test"
        ) as client:
            return await client.request(method, path, headers=headers, json=body)

    return asyncio.run(run())


def _register(app: Any) -> httpx.Response:
    return _request(
        app,
        "POST",
        "/api/terminals/register",
        body={"callsign": "desktop-61", "password": "desktop-secret", "device_id": "desktop-61",
              "terminal_platform": "windows",
              "endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True},
    )


def test_successful_asgi_terminal_registration_returns_only_public_session_fields(tmp_path: Path) -> None:
    app = _app(tmp_path)
    response = _register(app)

    assert response.status_code == 201
    payload = response.json()
    assert payload["success"] is True
    assert payload["status"] == "registered"
    assert isinstance(payload["session_id"], str) and payload["session_id"]
    assert set(payload) == {"success", "status", "session_id", "operator"}
    encoded = response.text
    for secret in ("central-callback-secret", "desktop-secret", "agent-secret"):
        assert secret not in encoded


def test_registration_session_remains_usable_without_disclosing_credential(tmp_path: Path) -> None:
    app = _app(tmp_path)
    registration = _register(app).json()
    session = str(registration["session_id"])

    heartbeat = _request(
        app, "POST", "/api/terminals/heartbeat",
        headers={"x-terminal-session": session}, body={"gui_ready": True},
    )
    state = _request(app, "GET", "/api/terminals/state", headers={"x-terminal-session": session})
    assert heartbeat.status_code == 200 and heartbeat.json()["success"] is True
    assert state.status_code == 200 and state.json()["success"] is True


def test_agent_and_foreign_terminal_requests_remain_opaque(tmp_path: Path) -> None:
    app = _app(tmp_path)
    denied = _request(
        app, "POST", "/api/terminals/register", headers={"authorization": "Bearer agent-secret"},
        body={"endpoint": "https://10.10.0.61:9100/terminal/events", "gui_ready": True},
    )
    foreign = _request(app, "GET", "/api/terminals/state", headers={"x-terminal-session": "foreign"})
    assert denied.status_code in {400, 401}
    # Either schema rejection (missing identity) or auth failure; both are
    # opaque and disclose nothing about configured credentials.
    assert denied.json()["success"] is False
    assert denied.json()["status"] in {"invalid_request", "terminal_not_authenticated"}
    assert foreign.status_code == 404
    assert foreign.json() == {"success": False, "status": "session_not_found"}
