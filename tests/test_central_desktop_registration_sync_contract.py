"""Red contract for durable, GUI-free desktop registration and wake sync.

This bounded adapter sits between the central terminal registry and the
portable desktop presenter.  It deliberately does not start a listener,
render a GUI, use TLS, or implement a phone/Bark workflow.  The caller injects
credentials, clock, durable registry, authoritative device-state reader,
presenter and ringing seams so the contract can exercise central authority
without a platform desktop.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from central_notification_router import TerminalRegistry


def _adapter():
    """Keep the existing registry control runnable during the Red phase."""

    return importlib.import_module("central_desktop_registration_sync")


@dataclass
class FakeClock:
    now: float = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


@dataclass
class FakePresenter:
    shown: list[str] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def show(self, state: dict[str, object]) -> None:
        self.shown.append(str(state["call_id"]))

    def close(self, call_id: str) -> None:
        self.closed.append(call_id)


@dataclass
class FakeRing:
    started: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def start(self, call_id: str) -> None:
        self.started.append(call_id)

    def stop(self, call_id: str) -> None:
        self.stopped.append(call_id)


def _service(tmp_path: Path, clock: FakeClock, states: dict[str, list[dict[str, object]]]):
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    audit: list[str] = []

    def device_state(device_id: str) -> list[dict[str, object]]:
        audit.append(f"device_state:{device_id}")
        return [dict(item) for item in states.get(device_id, [])]

    return _adapter().DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        registered_credentials={
            "desktop-secret": {
                "device_id": "desktop-61",
                "terminal_platform": "linux",
            }
        },
        device_state=device_state,
        audit=audit,
    ), registry, audit


def _client(service: Any, presenter: FakePresenter, ring: FakeRing):
    return _adapter().DesktopRegistrationSyncClient(
        config={
            "terminal_credential": "desktop-secret",
            "listener_endpoint": "https://10.0.0.61:9100/terminal/events",
            "callback_credential": "central-callback-secret",
        },
        central=service,
        presenter=presenter,
        ring=ring,
    )


def _managed_service(
    tmp_path: Path,
    clock: FakeClock,
    states: dict[str, list[dict[str, object]]],
):
    """Create a device whose callback target was set by management, not it."""

    service, registry, audit = _service(tmp_path, clock, states)
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-61",
        endpoint="https://10.0.0.61:9100/terminal/events",
        callback_credential="central-callback-secret",
        gui_ready=False,
    )
    return service, registry, audit


def _ringing(call_id: str, version: int) -> dict[str, object]:
    return {"call_id": call_id, "call_state": "ringing", "version": version}


def test_preconfigured_desktop_credential_authenticates_registration_before_durable_endpoint_write(
    tmp_path: Path,
) -> None:
    clock, states = FakeClock(), {"desktop-61": []}
    service, registry, _ = _managed_service(tmp_path, clock, states)
    managed = registry.desktop_state("desktop-61")

    denied = service.register(
        credential="forged-desktop-secret",
        endpoint="https://10.0.0.61:9100/terminal/events",
        gui_ready=True,
    )
    assert denied == {"success": False, "status": "terminal_not_authenticated"}
    assert registry.desktop_state("desktop-61") == managed

    accepted = service.register(
        credential="desktop-secret",
        endpoint="https://10.0.0.61:9100/terminal/events",
        gui_ready=True,
    )
    assert accepted["success"] is True
    assert isinstance(accepted["session_id"], str)
    # Registration gives the desktop only its opaque session.  The distinct
    # central-to-desktop callback credential remains server-side metadata.
    assert set(accepted) == {"success", "status", "session_id"}
    assert registry.desktop_state("desktop-61") == {
        "device_id": "desktop-61",
        "endpoint": "https://10.0.0.61:9100/terminal/events",
        "callback_credential": "central-callback-secret",
        "gui_ready": True,
        "fresh": True,
    }


def test_gui_ready_heartbeat_is_durable_fresh_for_30_seconds_across_service_restart(tmp_path: Path) -> None:
    clock, states = FakeClock(), {"desktop-61": []}
    service, registry, _ = _managed_service(tmp_path, clock, states)
    registered = service.register(
        credential="desktop-secret",
        endpoint="https://10.0.0.61:9100/terminal/events",
        gui_ready=True,
    )

    clock.now += 30
    assert service.heartbeat(session_id=str(registered["session_id"]), gui_ready=True)["fresh"] is True
    restarted_registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    assert restarted_registry.desktop_state("desktop-61")["fresh"] is True  # type: ignore[index]
    clock.now += 30.01
    assert restarted_registry.desktop_state("desktop-61")["fresh"] is False  # type: ignore[index]


def test_startup_and_wake_record_authenticated_registration_or_heartbeat_before_state_read_and_keep_fresh(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    states = {"desktop-61": [_ringing("call-1", 3)]}
    service, registry, audit = _managed_service(tmp_path, clock, states)
    presenter, ring = FakePresenter(), FakeRing()
    client = _client(service, presenter, ring)

    assert client.start() == {"success": True, "status": "synchronized", "call_ids": ["call-1"]}
    assert audit == [
        "register:desktop-61",
        "heartbeat:desktop-61",
        "device_state:desktop-61",
    ]
    assert registry.desktop_state("desktop-61")["fresh"] is True  # type: ignore[index]

    # At t+29 the original lease is nearly stale. Wake must durably touch it
    # before the authoritative read; a read-only client fails both checks.
    clock.now += 29
    assert client.wake() == {"success": True, "status": "synchronized", "call_ids": ["call-1"]}
    assert audit[-2:] == ["heartbeat:desktop-61", "device_state:desktop-61"]
    restarted_registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    assert restarted_registry.desktop_state("desktop-61")["fresh"] is True  # type: ignore[index]
    clock.now += 29.5
    assert restarted_registry.desktop_state("desktop-61")["fresh"] is True  # type: ignore[index]
    assert presenter.shown == ["call-1"]
    assert ring.started == ["call-1"]


def test_missed_callback_remote_accept_or_end_closes_known_alert_without_action_or_takeover(tmp_path: Path) -> None:
    clock = FakeClock()
    states = {"desktop-61": [_ringing("call-1", 1)]}
    service, _, _ = _managed_service(tmp_path, clock, states)
    presenter, ring = FakePresenter(), FakeRing()
    client = _client(service, presenter, ring)
    client.start()

    # The phone accepted while the desktop slept and its callback was lost.
    # A wake synchronization observes authority; it must only dismiss locally.
    states["desktop-61"] = [
        {"call_id": "call-1", "call_state": "accepted", "version": 2,
         "owner_device_id": "iphone-1"}
    ]
    assert client.wake() == {"success": True, "status": "synchronized", "call_ids": []}
    assert presenter.closed == ["call-1"]
    assert ring.stopped == ["call-1"]
    assert service.actions == []
    assert client.accept(call_id="call-1", operation_id="late") == {
        "success": False,
        "status": "session_not_found",
    }


def test_stale_or_duplicate_versions_never_reopen_or_rering_after_authoritative_dismissal(tmp_path: Path) -> None:
    clock = FakeClock()
    states = {"desktop-61": [_ringing("call-1", 2)]}
    service, _, _ = _managed_service(tmp_path, clock, states)
    presenter, ring = FakePresenter(), FakeRing()
    client = _client(service, presenter, ring)
    client.start()

    # The identical current callback is a duplicate delivery, not a new alert.
    assert client.receive_callback(
        {"call_id": "call-1", "call_state": "ringing", "version": 2},
        callback_credential="central-callback-secret",
    ) == {
        "success": True,
        "status": "duplicate",
        "call_id": "call-1",
    }
    assert presenter.shown == ["call-1"]
    assert ring.started == ["call-1"]

    states["desktop-61"] = [{"call_id": "call-1", "call_state": "declined", "version": 3}]
    client.wake()
    # A delayed ringing callback/version cannot recreate a finished call.
    assert client.receive_callback(
        {"call_id": "call-1", "call_state": "ringing", "version": 2},
        callback_credential="central-callback-secret",
    ) == {
        "success": True,
        "status": "stale",
        "call_id": "call-1",
    }
    assert presenter.shown == ["call-1"]
    assert ring.started == ["call-1"]
    assert presenter.closed == ["call-1"]
    assert ring.stopped == ["call-1"]


def test_device_credential_cannot_replace_management_preconfigured_callback_endpoint(tmp_path: Path) -> None:
    clock, states = FakeClock(), {"desktop-61": []}
    service, registry, _ = _managed_service(tmp_path, clock, states)
    managed = registry.desktop_state("desktop-61")

    attacker = service.register(
        credential="desktop-secret",
        endpoint="https://attacker.invalid/terminal/events",
        gui_ready=True,
    )

    assert attacker == {"success": False, "status": "endpoint_not_preconfigured"}
    assert registry.desktop_state("desktop-61") == managed

    registered = service.register(
        credential="desktop-secret",
        endpoint="https://10.0.0.61:9100/terminal/events",
        gui_ready=True,
    )
    assert registered["success"] is True
    state = registry.desktop_state("desktop-61")
    assert state is not None
    assert state["endpoint"] == "https://10.0.0.61:9100/terminal/events"
    assert state["callback_credential"] == "central-callback-secret"


def test_unregistered_device_cannot_create_callback_target_with_a_valid_credential(tmp_path: Path) -> None:
    clock, states = FakeClock(), {"desktop-61": []}
    service, registry, _ = _service(tmp_path, clock, states)

    denied = service.register(
        credential="desktop-secret",
        endpoint="https://attacker.invalid/terminal/events",
        gui_ready=True,
    )

    assert denied == {"success": False, "status": "endpoint_not_preconfigured"}
    assert registry.desktop_state("desktop-61") is None


def test_callback_requires_separate_central_credential_before_presentation_or_ringing(tmp_path: Path) -> None:
    clock = FakeClock()
    states = {"desktop-61": []}
    service, _, _ = _managed_service(tmp_path, clock, states)
    presenter, ring = FakePresenter(), FakeRing()
    client = _adapter().DesktopRegistrationSyncClient(
        config={
            "terminal_credential": "desktop-secret",
            "listener_endpoint": "https://10.0.0.61:9100/terminal/events",
            "callback_credential": "central-callback-secret",
        },
        central=service,
        presenter=presenter,
        ring=ring,
    )
    event = _ringing("forged", 1)

    assert client.receive_callback(event) == {"success": False, "status": "callback_not_authenticated"}
    assert client.receive_callback(event, callback_credential="desktop-secret") == {
        "success": False,
        "status": "callback_not_authenticated",
    }
    assert presenter.shown == []
    assert ring.started == []

    assert client.receive_callback(event, callback_credential="central-callback-secret") == {
        "success": True,
        "status": "presented",
        "call_id": "forged",
    }
    assert presenter.shown == ["forged"]
    assert ring.started == ["forged"]


def test_missing_callback_credential_configuration_fails_closed_before_presentation_or_ringing(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    service, _, _ = _managed_service(tmp_path, clock, {"desktop-61": []})
    presenter, ring = FakePresenter(), FakeRing()
    client = _adapter().DesktopRegistrationSyncClient(
        config={
            "terminal_credential": "desktop-secret",
            "listener_endpoint": "https://10.0.0.61:9100/terminal/events",
        },
        central=service,
        presenter=presenter,
        ring=ring,
    )

    assert client.receive_callback(
        _ringing("forged-without-local-callback-secret", 1),
        callback_credential="central-callback-secret",
    ) == {"success": False, "status": "callback_not_authenticated"}
    assert presenter.shown == []
    assert ring.started == []
