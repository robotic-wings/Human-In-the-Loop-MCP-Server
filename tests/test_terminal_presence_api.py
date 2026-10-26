"""Red contracts for configured desktop presence and safe terminal polling.

This slice is limited to authenticated configured desktop devices.  It does not
cover callbacks, delivery, GUI, discovery, browser/mobile UI, or answer and
attachment workflows.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from call_coordinator import CallCoordinator
from test_central_mcp_gateway import _free_port, _start, _stop
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)
from test_terminal_session_liveness import (
    accept_desktop_call,
    create_desktop_call,
    make_clock,
    persisted_call,
)


def _register(
    port: int, certificate: Path, token: str, *, instance_id: str, gui_ready: bool
) -> tuple[int, dict[str, Any]]:
    return _terminal_request(
        port,
        certificate,
        "POST",
        "/api/terminals/register",
        token,
        {
            "instance_id": instance_id,
            "gui_ready": gui_ready,
            # These are deliberately hostile body values: identity, platform,
            # routing, and callback all come from managed configuration.
            "device_id": "body-forged-device",
            "terminal_platform": "ios",
            "route_device_id": "body-forged-route",
            "callback_url": "https://body.example.invalid/terminal/events",
        },
    )


def _device_heartbeat(
    port: int, certificate: Path, token: str, instance_id: str
) -> tuple[int, dict[str, Any]]:
    return _terminal_request(
        port,
        certificate,
        "POST",
        "/api/terminals/heartbeat",
        token,
        {"instance_id": instance_id},
    )


def test_tls_registration_derives_managed_identity_and_replacement_instance_rejects_old_heartbeat(
    tmp_path: Path,
) -> None:
    """Only configured bearer identity may establish/re-establish presence."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    try:
        _wait_for_tls_gateway(process, port, certificate)
        missing_bearer = _register(port, certificate, None, instance_id="first", gui_ready=True)
        first = _register(port, certificate, "desktop-token", instance_id="first", gui_ready=True)
        replacement = _register(
            port, certificate, "desktop-token", instance_id="replacement", gui_ready=False
        )
        stale = _device_heartbeat(port, certificate, "desktop-token", "first")
        current = _device_heartbeat(port, certificate, "desktop-token", "replacement")
        other = _register(port, certificate, "other-desktop-token", instance_id="other", gui_ready=True)
    finally:
        _stop(process)

    assert missing_bearer == (401, {"error": "unauthorized"})
    for status, registered, instance_id, gui_ready in (
        (first[0], first[1], "first", True),
        (replacement[0], replacement[1], "replacement", False),
        (current[0], current[1], "replacement", False),
    ):
        assert status == 200
        assert registered["presence"] == {
            "device_id": "desktop-registered",
            "terminal_platform": "windows",
            "route_device_id": "desktop-registered",
            "callback_url": None,
            "instance_id": instance_id,
            "gui_ready": gui_ready,
            "online": True,
            "last_seen_at": registered["presence"]["last_seen_at"],
            "online_expires_at": registered["presence"]["online_expires_at"],
        }
    assert stale == (
        409,
        {"success": False, "status": "instance_conflict", "needs_continuation": False},
    )
    # A second configured credential can register only its own configured
    # device identity; it cannot turn the hostile body fields into discovery
    # or overwrite desktop-registered's managed route.
    assert other[0] == 200
    assert other[1]["presence"]["device_id"] == "desktop-other"
    assert other[1]["presence"]["terminal_platform"] == "linux"


def test_device_presence_is_durable_and_never_refreshes_accepted_call_liveness(
    tmp_path: Path,
) -> None:
    """Device health has its own instance/deadline and cannot keep a call alive."""
    clock = make_clock()
    database = tmp_path / "presence.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service, request_id="presence-call")
    call_id = created["call_id"]
    accept_desktop_call(service, call_id)
    accepted = persisted_call(database, call_id)

    registered = service.register_terminal_presence(
        device_id="desktop-registered",
        terminal_platform="windows",
        route_device_id="desktop-registered",
        callback_url=None,
        instance_id="instance-a",
        gui_ready=True,
    )
    clock.advance(10)
    heartbeat = service.terminal_device_heartbeat("desktop-registered", "instance-a")
    after_health = persisted_call(database, call_id)
    stale = service.terminal_device_heartbeat("desktop-registered", "old-instance")
    service.close()

    assert registered["online"] is True
    assert heartbeat == registered | {
        "last_seen_at": clock.now_utc().isoformat(),
        "online_expires_at": (clock.now_utc() + timedelta(seconds=30)).isoformat(),
    }
    assert stale == {"success": False, "status": "instance_conflict", "needs_continuation": False}
    for field in (
        "version",
        "owner_device_id",
        "owner_session_id",
        "terminal_last_seen_at",
        "terminal_disconnect_deadline",
    ):
        assert after_health[field] == accepted[field]

    reopened = CallCoordinator(database, clock=clock)
    restored = reopened.terminal_presence("desktop-registered")
    # The successful heartbeat at t+10 established its own t+40 presence
    # deadline.  Presence remains live before that deadline and expires only
    # after it; neither observation can change the accepted call liveness.
    clock.advance(29)
    still_online = reopened.terminal_presence("desktop-registered")
    clock.advance(2)
    expired = reopened.terminal_presence("desktop-registered")
    final_call = persisted_call(database, call_id)
    reopened.close()

    assert restored["instance_id"] == "instance-a"
    assert restored["online"] is True
    assert still_online["online"] is True
    assert expired["online"] is False
    assert final_call["state"] == "accepted"
    assert final_call["terminal_disconnect_deadline"] == accepted["terminal_disconnect_deadline"]


def test_tls_authoritative_polling_is_route_scoped_safe_and_settles_expiry_before_snapshot(
    tmp_path: Path,
) -> None:
    """Polling is a settled, non-disclosing state calibration rather than a call lease."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    try:
        _wait_for_tls_gateway(process, port, certificate)
        registered = _register(port, certificate, "desktop-token", instance_id="polling", gui_ready=True)
        created = _create_ringing_call(port, certificate, "presence-poll-create")
        call_id = created["call_id"]
        own = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
        specific = _terminal_request(
            port, certificate, "GET", f"/api/calls/{call_id}", "desktop-token"
        )
        wrong_route = _terminal_request(
            port, certificate, "GET", f"/api/calls/{call_id}", "other-desktop-token"
        )
        unknown = _terminal_request(
            port, certificate, "GET", "/api/calls/no-such-call", "other-desktop-token"
        )
    finally:
        _stop(process)

    assert registered[0] == 200
    assert own[0] == specific[0] == 200
    assert wrong_route == unknown == (404, {"error": "not found"})
    for snapshot in (own[1], specific[1]):
        assert snapshot["presence"]["device_id"] == "desktop-registered"
        assert snapshot["presence"]["instance_id"] == "polling"
        assert snapshot["call"] == {
            "call_id": call_id,
            "state": "ringing",
            "version": snapshot["call"]["version"],
            "ring_deadline": snapshot["call"]["ring_deadline"],
            "terminal_disconnect_deadline": None,
        }
        encoded = str(snapshot).lower()
        assert "message" not in encoded and "prompt" not in encoded and "body" not in encoded


def test_controlled_terminal_polling_settles_ring_timeout_and_late_accept_cannot_revive(
    tmp_path: Path,
) -> None:
    """A terminal poll settles a stored deadline before exposing its call view."""
    clock = make_clock()
    database = tmp_path / "polling-expiry.sqlite3"
    service = CallCoordinator(database, clock=clock)
    created = create_desktop_call(service, request_id="polling-expiry")
    call_id = created["call_id"]
    ringing = persisted_call(database, call_id)

    # This goes through the same credential-derived device scope used by the
    # TLS terminal boundary.  A controllable gateway clock is not exposed by
    # that subprocess fixture, so expiry itself is exercised at the public
    # coordinator terminal-poll operation.
    clock.advance(61)
    settled_snapshot = service.terminal_state("desktop-registered", call_id)
    after_poll = persisted_call(database, call_id)
    late_accept = service.terminal_action(
        "accept",
        call_id,
        "desktop-registered",
        "late-accept-after-poll",
        "late-session",
        "windows",
    )
    repeated_snapshot = service.terminal_state("desktop-registered", call_id)
    final = persisted_call(database, call_id)
    events = service.events_for(call_id)
    service.close()

    assert settled_snapshot is not None
    assert settled_snapshot["call_id"] == call_id
    assert settled_snapshot["call_state"] == "not_accepted"
    assert settled_snapshot["status"] == "not_accepted"
    assert settled_snapshot["reason"] == "ring_timeout"
    assert after_poll["state"] == "not_accepted"
    assert after_poll["version"] == ringing["version"] + 1

    assert late_accept is not None
    assert late_accept["status"] == "not_accepted"
    assert late_accept["reason"] == "ring_timeout"
    assert repeated_snapshot == settled_snapshot
    assert final["state"] == "not_accepted"
    assert final["version"] == after_poll["version"]
    assert events == [late_accept]
