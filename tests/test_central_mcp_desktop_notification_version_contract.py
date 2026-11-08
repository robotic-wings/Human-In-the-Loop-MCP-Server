"""Red contract for a versioned central-to-desktop ringing callback.

The central lifecycle owns the call's initial ``ringing`` version.  Its
durable notification outbox must preserve that versioned event so an
independent, pre-registered desktop terminal can safely deduplicate it after
central-service restart.  This does not test desktop registration APIs or GUI
behaviour; it drives the real lifecycle -> router -> registered callback path.
"""

from __future__ import annotations

import asyncio
import importlib
import sqlite3
from pathlib import Path
from typing import Any


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _lifecycle_module() -> Any:
    return importlib.import_module("central_mcp_waiting_lifecycle")


def _router_module() -> Any:
    return importlib.import_module("central_notification_router")


def _call_state(database_path: Path, call_id: str) -> str | None:
    """Read the central lifecycle's durable authority for router suppression."""

    with sqlite3.connect(database_path) as conn:
        row = conn.execute(
            "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
        ).fetchone()
    return None if row is None else str(row[0])


def _make_components(
    tmp_path: Path,
    clock: FakeClock,
    received: list[dict[str, object]],
) -> tuple[Any, Any, Path]:
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    routing_path = tmp_path / "routing.sqlite3"
    router_module = _router_module()
    registry = router_module.TerminalRegistry(database_path=routing_path, clock=clock)
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-61",
        endpoint="https://10.0.0.61:9100/terminal/events",
        callback_credential="central-callback-secret",
        gui_ready=True,
    )

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        received.append(dict(payload))
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        raise AssertionError("desktop-only routing must not invoke Bark")

    router = router_module.NotificationRouter(
        database_path=routing_path,
        clock=clock,
        registry=registry,
        desktop_dispatch=desktop_dispatch,
        bark_dispatch=bark_dispatch,
        route_mode="desktop",
        call_state=lambda call_id: _call_state(lifecycle_path, call_id),
        web_base_url="https://central.example.test",
    )
    lifecycle = _lifecycle_module().WaitingLifecycle(
        lifecycle_path,
        clock,
        router.enqueue,
        60,
        30,
    )
    return lifecycle, router, lifecycle_path


def _create(lifecycle: Any, request_id: str = "ring-request") -> dict[str, object]:
    result = lifecycle.create("agent-a", request_id, {"tool": "text"})
    assert result["status"] == "heartbeat"
    return result


def _expected_payload(ringing: dict[str, object], clock: FakeClock) -> dict[str, object]:
    return {
        "call_id": ringing["call_id"],
        "call_state": "ringing",
        "version": 1,
        "ring_deadline": clock() + 60,
        # These are management-controlled registry values, not Agent input.
        "device_id": "desktop-61",
        "endpoint": "https://10.0.0.61:9100/terminal/events",
        "callback_credential": "central-callback-secret",
        "gui_ready": True,
        "fresh": True,
    }


def test_registered_desktop_receives_one_versioned_durable_initial_ringing_callback(tmp_path: Path) -> None:
    clock, received = FakeClock(), []
    lifecycle, router, _ = _make_components(tmp_path, clock, received)
    ringing = _create(lifecycle)

    asyncio.run(router.dispatch_pending())

    assert received == [_expected_payload(ringing, clock)]
    assert router.delivery_states(str(ringing["call_id"])) == [
        {"channel": "desktop", "state": "delivered"}
    ]
    # A repeated central dispatch is not a second desktop ring.
    asyncio.run(router.dispatch_pending())
    assert received == [_expected_payload(ringing, clock)]


def test_pending_notification_survives_restart_as_the_same_versioned_callback(tmp_path: Path) -> None:
    clock, first_received = FakeClock(), []
    lifecycle, _router, _ = _make_components(tmp_path, clock, first_received)
    ringing = _create(lifecycle)

    # The service restarts before the durable outbox is delivered.  Reopening
    # the central router must send the original event, not reconstruct a
    # versionless or newly-ringing notification.
    restarted_received: list[dict[str, object]] = []
    _restarted_lifecycle, restarted_router, _ = _make_components(
        tmp_path, clock, restarted_received
    )
    asyncio.run(restarted_router.dispatch_pending())
    assert restarted_received == [_expected_payload(ringing, clock)]
    asyncio.run(restarted_router.dispatch_pending())
    assert restarted_received == [_expected_payload(ringing, clock)]


def test_expired_or_terminal_call_never_dispatches_a_late_desktop_callback(tmp_path: Path) -> None:
    clock, received = FakeClock(), []
    lifecycle, router, _ = _make_components(tmp_path, clock, received)
    expired = _create(lifecycle, "expired")
    clock.advance(60)
    asyncio.run(router.dispatch_pending())
    assert received == []
    assert router.delivery_states(str(expired["call_id"])) == [
        {"channel": "desktop", "state": "skipped"}
    ]

    # Terminal state is also authoritative even before the absolute deadline.
    terminal = _create(lifecycle, "terminal")
    accepted = lifecycle.accept(
        call_id=str(terminal["call_id"]),
        device_id="desktop-61",
        handled_on="desktop",
        terminal_platform="windows",
    )
    assert accepted["status"] == "opened"
    asyncio.run(router.dispatch_pending())
    assert received == []
    assert router.delivery_states(str(terminal["call_id"])) == [
        {"channel": "desktop", "state": "skipped"}
    ]
