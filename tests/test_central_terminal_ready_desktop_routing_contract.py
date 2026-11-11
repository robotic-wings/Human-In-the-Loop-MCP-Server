"""Red contract for ready-desktop invitation routing.

``TerminalRegistry.ready_desktop`` currently picks the first terminal by
alphabetical ``device_id`` without any readiness filter, so an invitation can
be routed to a GUI-off desktop while a fresh, ready desktop exists.  These
tests stay red until ``ready_desktop`` returns a genuinely fresh terminal (or
``None`` when none is fresh) and the call-creation path consequently invites
no desktop instead of a non-ready one.
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
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _register(registry: TerminalRegistry, device_id: str, gui_ready: bool) -> None:
    registry.register_desktop(
        actor_role="operator",
        device_id=device_id,
        endpoint=f"https://{device_id}.test/terminal/events",
        callback_credential=f"callback-{device_id}",
        gui_ready=gui_ready,
    )


def test_ready_desktop_selects_the_fresh_ready_terminal_not_the_first_alphabetical(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "routing.sqlite3", clock=clock)
    _register(registry, "desktop-61", gui_ready=False)
    _register(registry, "desktop-mac", gui_ready=True)

    ready = registry.ready_desktop()
    assert ready is not None
    assert ready["device_id"] == "desktop-mac"
    assert ready["gui_ready"] is True
    assert ready["fresh"] is True


def test_ready_desktop_returns_none_when_no_terminal_is_fresh(tmp_path: Path) -> None:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "routing.sqlite3", clock=clock)
    _register(registry, "desktop-stale", gui_ready=True)
    clock.advance(31)
    assert registry.ready_desktop() is None


def test_ready_desktop_returns_none_when_only_gui_off_terminal_exists(tmp_path: Path) -> None:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "routing.sqlite3", clock=clock)
    _register(registry, "desktop-61", gui_ready=False)
    assert registry.ready_desktop() is None


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path, *, route_mode: str = "desktop") -> tuple[Any, FakeClock]:
    clock = FakeClock()
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    _register(registry, "desktop-owner", gui_ready=False)
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        operator_directory={
            "desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True},
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
            row = conn.execute(
                "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
        route_mode=route_mode, call_state=call_state, web_base_url="https://central.test",
    )
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_directory={"desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True}},
    )
    return app, clock


def create_call(app: Any, request_id: str = "choice-create") -> dict[str, object]:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.post("/mcp", headers={"authorization": "Bearer agent-secret"}, json={
                "request_id": request_id, "client_timeout_seconds": 2,
                "payload": {"tool": "get_text_input", "prompt": "act"},
            })
    response = asyncio.run(run())
    assert response.status_code == 200
    return response.json()


def test_call_creation_does_not_invite_a_non_ready_desktop(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, route_mode="desktop")
    assert app._invited_desktop_id() is None

    call = create_call(app)
    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        row = conn.execute(
            "SELECT invited_desktop_id FROM lifecycle_calls WHERE call_id=?", (call["call_id"],)
        ).fetchone()
    assert row[0] is None
