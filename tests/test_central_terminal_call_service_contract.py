"""Red contract for GUI-free central terminal call ownership.

This bounded contract defines ``central_terminal_call_service``.  It bridges
the durable coordinator and notification-router seams without providing a web
login, TLS/CSRF protection, form submission, Bark, or desktop/mobile GUI.

The proposed ``TerminalCallService`` is constructed with a SQLite path, a
``CallCoordinator``, a clock, a mapping of registered terminal credentials,
and an ``on_state_update`` seam.  ``authenticate`` creates a durable terminal
session from a credential whose device/category/platform metadata is supplied
by the registered terminal, never inferred from a display string.  A creator
calls ``track_call(call_id, enabled_channels)`` exactly once after the router
has frozen the per-call route.  The service offers ``read_mobile_state``,
``accept`` and ``decline``; mutating calls require a valid session and
operation ID.

The tests deliberately cover only the central state boundary behind future
terminal HTTP/web handlers.  They do not claim login, TLS, browser security,
forms, attachments, native notifications, Bark delivery, or real-device
behavior.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import threading
from pathlib import Path

import pytest

from central_call_coordinator import CallCoordinator


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _terminal_module():
    """Lazy import retains a passing GUI-free coordinator control in Red."""

    return importlib.import_module("central_terminal_call_service")


def _coordinator(tmp_path: Path, clock: FakeClock) -> CallCoordinator:
    return CallCoordinator(
        database_path=tmp_path / "calls.sqlite3",
        clock=clock,
        notify=lambda _: None,
        ring_timeout_seconds=60,
    )


def _create_call(coordinator: CallCoordinator) -> str:
    result = coordinator.create_call(
        caller_id="agent-a",
        request_id="create-1",
        payload={"tool": "get_text_input", "prompt": "Proceed?"},
    )
    assert result["status"] == "ringing"
    return str(result["call_id"])


def _service(
    tmp_path: Path,
    clock: FakeClock,
    coordinator: CallCoordinator,
    updates: list[dict[str, object]],
):
    return _terminal_module().TerminalCallService(
        database_path=tmp_path / "terminal-sessions.sqlite3",
        coordinator=coordinator,
        clock=clock,
        registered_credentials={
            "iphone-session-secret": {
                "device_id": "opaque-mobile-device",
                "terminal_category": "iphone_web",
                "terminal_platform": "ios",
            },
            "desktop-session-secret": {
                "device_id": "opaque-desktop-device",
                "terminal_category": "desktop",
                "terminal_platform": "linux",
            },
        },
        on_state_update=updates.append,
    )


def _session(service, credential: str) -> str:
    result = service.authenticate(credential=credential)
    assert result["success"] is True
    return str(result["session_id"])


def test_existing_coordinator_control_is_gui_free_and_exposes_explicit_metadata(tmp_path: Path) -> None:
    """Passing control independent of the absent terminal-call module."""

    source = inspect.getsource(CallCoordinator)
    tree = ast.parse(source)
    assert "tkinter" not in source.lower()
    accept = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "CallCoordinator")
    assert "terminal_category" in ast.unparse(accept)
    assert "terminal_platform" in ast.unparse(accept)


def test_authenticated_session_binds_opaque_device_to_explicit_category_and_platform(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    service = _service(tmp_path, clock, _coordinator(tmp_path, clock), updates)

    denied = service.authenticate(credential="forged-or-unknown")
    assert denied == {"success": False, "status": "terminal_not_authenticated"}

    session_id = _session(service, "iphone-session-secret")
    details = service.session_state(session_id=session_id)
    assert details == {
        "device_id": "opaque-mobile-device",
        "terminal_category": "iphone_web",
        "terminal_platform": "ios",
    }
    assert "iphone" not in details["device_id"]


def test_mobile_read_is_route_eligible_read_only_and_does_not_implicitly_accept(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator = _coordinator(tmp_path, clock)
    call_id = _create_call(coordinator)
    service = _service(tmp_path, clock, coordinator, updates)
    mobile_session = _session(service, "iphone-session-secret")
    service.track_call(call_id=call_id, enabled_channels={"desktop", "bark"})

    view = service.read_mobile_state(call_id=call_id, session_id=mobile_session)
    assert view == {
        "success": True,
        "call_id": call_id,
        "call_state": "ringing",
        "can_accept": True,
        "can_decline": True,
    }
    assert coordinator.get_call(call_id)["call_state"] == "ringing"
    assert updates == []

    isolated = _coordinator(tmp_path / "desktop-only", clock)
    # Use a single independent coordinator for the desktop-only call fixture.
    desktop_only = _create_call(isolated)
    desktop_service = _service(tmp_path / "desktop-only", clock, isolated, [])
    desktop_service.track_call(call_id=desktop_only, enabled_channels={"desktop"})
    hidden = desktop_service.read_mobile_state(call_id=desktop_only, session_id=_session(desktop_service, "iphone-session-secret"))
    assert hidden == {"success": False, "status": "session_not_found"}
    assert isolated.get_call(desktop_only)["call_state"] == "ringing"


def test_accept_decline_race_has_one_winner_and_decline_is_whole_call(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator = _coordinator(tmp_path, clock)
    call_id = _create_call(coordinator)
    service = _service(tmp_path, clock, coordinator, updates)
    service.track_call(call_id=call_id, enabled_channels={"desktop", "bark"})
    mobile_session = _session(service, "iphone-session-secret")
    desktop_session = _session(service, "desktop-session-secret")
    barrier = threading.Barrier(2)
    results: list[dict[str, object]] = []

    def accept() -> None:
        barrier.wait()
        results.append(service.accept(call_id=call_id, session_id=mobile_session, operation_id="accept-1"))

    def decline() -> None:
        barrier.wait()
        results.append(service.decline(call_id=call_id, session_id=desktop_session, operation_id="decline-1"))

    first, second = threading.Thread(target=accept), threading.Thread(target=decline)
    first.start(); second.start(); first.join(); second.join()
    assert len(results) == 2
    final = coordinator.get_call(call_id)
    assert final["call_state"] in {"accepted", "declined"}
    assert sum(result.get("success") is True for result in results) == 1

    # A winning decline ends this whole call, not merely the device's alert.
    if final["call_state"] == "declined":
        assert service.read_mobile_state(call_id=call_id, session_id=mobile_session)["can_accept"] is False


def test_operations_replay_for_same_session_but_other_terminal_cannot_take_over(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator = _coordinator(tmp_path, clock)
    call_id = _create_call(coordinator)
    service = _service(tmp_path, clock, coordinator, updates)
    service.track_call(call_id=call_id, enabled_channels={"desktop", "bark"})
    mobile_session = _session(service, "iphone-session-secret")
    desktop_session = _session(service, "desktop-session-secret")

    accepted = service.accept(call_id=call_id, session_id=mobile_session, operation_id="accept-1")
    assert service.accept(call_id=call_id, session_id=mobile_session, operation_id="accept-1") == accepted
    takeover = service.accept(call_id=call_id, session_id=desktop_session, operation_id="accept-2")
    assert takeover == {"success": False, "status": "already_accepted", "call_state": "accepted"}
    assert coordinator.get_call(call_id)["owner_device_id"] == "opaque-mobile-device"
    assert coordinator.get_call(call_id)["terminal_platform"] == "ios"
    assert len(updates) == 1


def test_late_operations_and_foreign_sessions_are_non_disclosing(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator = _coordinator(tmp_path, clock)
    call_id = _create_call(coordinator)
    service = _service(tmp_path, clock, coordinator, updates)
    service.track_call(call_id=call_id, enabled_channels={"bark"})
    mobile_session = _session(service, "iphone-session-secret")
    desktop_session = _session(service, "desktop-session-secret")

    # Desktop is not an eligible route and must learn no call metadata.
    assert service.read_mobile_state(call_id=call_id, session_id=desktop_session) == {
        "success": False, "status": "session_not_found"
    }
    assert service.read_mobile_state(call_id="unknown-call", session_id=desktop_session) == {
        "success": False, "status": "session_not_found"
    }
    clock.advance(60)
    late = service.accept(call_id=call_id, session_id=mobile_session, operation_id="late-accept")
    assert late["success"] is False
    assert late["status"] == "expired"
    assert updates == []


def test_owner_session_and_state_update_persist_across_restart_after_accept(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator = _coordinator(tmp_path, clock)
    call_id = _create_call(coordinator)
    service = _service(tmp_path, clock, coordinator, updates)
    service.track_call(call_id=call_id, enabled_channels={"desktop", "bark"})
    session_id = _session(service, "iphone-session-secret")
    accepted = service.accept(call_id=call_id, session_id=session_id, operation_id="accept-1")
    assert accepted["status"] == "opened"
    assert updates == [{"call_id": call_id, "call_state": "accepted"}]

    reopened = _service(tmp_path, clock, coordinator, [])
    assert reopened.session_state(session_id=session_id)["device_id"] == "opaque-mobile-device"
    recovered = reopened.read_mobile_state(call_id=call_id, session_id=session_id)
    assert recovered["call_state"] == "accepted"
    assert recovered["handled_on"] == "iphone_web"
    assert recovered["terminal_platform"] == "ios"
