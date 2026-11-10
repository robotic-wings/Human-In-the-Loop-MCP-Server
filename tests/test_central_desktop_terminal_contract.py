"""Red contract for a standalone, GUI-free desktop terminal client.

``central_desktop_terminal`` is intentionally a portable client boundary, not
an MCP server and not a platform GUI toolkit.  Platform-specific notification,
window, ring and GUI-availability behavior is injected.  The client speaks
only to the central terminal HTTP/session API and its authenticated listener
acknowledgement seam.

These simulations establish common protocol semantics only.  They do not
replace the required macOS, Windows and Linux GUI/device acceptance work.
"""

from __future__ import annotations

import ast
import importlib
import inspect
from dataclasses import dataclass, field
from typing import Any

from central_terminal_http_service import create_app


def _module():
    """Keep the independent terminal-HTTP control runnable in Red."""

    return importlib.import_module("central_desktop_terminal")


@dataclass
class FakeCentral:
    available: bool = True
    issued_sessions: int = 0
    reads: list[tuple[str, str]] = field(default_factory=list)
    actions: list[tuple[str, str, str, str]] = field(default_factory=list)
    states: dict[str, dict[str, object]] = field(default_factory=dict)

    def authenticate_desktop(self, credential: str) -> dict[str, object]:
        if not self.available or credential != "desktop-secret":
            return {"success": False, "status": "terminal_not_authenticated"}
        self.issued_sessions += 1
        return {"success": True, "session_id": f"desktop-session-{self.issued_sessions}"}

    def read_call(self, call_id: str, session_id: str) -> dict[str, object]:
        self.reads.append((call_id, session_id))
        if not self.available:
            raise ConnectionError("central unavailable")
        return dict(self.states.get(call_id, {"success": False, "status": "session_not_found"}))

    def act(self, action: str, call_id: str, session_id: str, operation_id: str) -> dict[str, object]:
        self.actions.append((action, call_id, session_id, operation_id))
        if not self.available:
            raise ConnectionError("central unavailable")
        if call_id not in self.states:
            return {"success": False, "status": "session_not_found"}
        if action == "accept":
            self.states[call_id] = {
                "success": True,
                "call_id": call_id,
                "call_state": "accepted",
                "handled_on": "desktop",
                "terminal_platform": "linux",
                "can_accept": False,
                "can_decline": False,
            }
            return {"success": True, "status": "opened", "call_id": call_id}
        self.states[call_id] = {"success": True, "call_id": call_id, "call_state": "declined"}
        return {"success": True, "status": "declined", "call_id": call_id}


@dataclass
class FakeListener:
    acknowledgements: list[dict[str, object]] = field(default_factory=list)

    def acknowledge(self, event: dict[str, object]) -> None:
        self.acknowledgements.append(dict(event))


@dataclass
class FakeRing:
    started: list[str] = field(default_factory=list)
    modes: list[str] = field(default_factory=list)
    stopped: list[str] = field(default_factory=list)

    def start(self, call_id: str, mode: str = "long") -> None:
        self.started.append(call_id)
        self.modes.append(mode)

    def stop(self, call_id: str) -> None:
        self.stopped.append(call_id)


@dataclass
class FakePresenter:
    shown: list[dict[str, object]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)
    hints: list[tuple[str, str]] = field(default_factory=list)

    def show(self, state: dict[str, object]) -> None:
        self.shown.append(dict(state))

    def close(self, call_id: str) -> None:
        self.closed.append(call_id)

    def show_handled_hint(self, call_id: str, hint: str) -> None:
        self.hints.append((call_id, hint))


def _client(
    *, central: FakeCentral, gui_ready: bool = True, callback_credential: str | None = "central-callback-secret"
):
    config: dict[str, object] = {
        "central_url": "https://10.0.0.1:8443",
        "terminal_credential": "desktop-secret",
        "listener_address": "10.0.0.61",
        "listener_port": 9100,
        "platform": "linux",
    }
    if callback_credential is not None:
        config["callback_credential"] = callback_credential
    return _module().DesktopTerminalClient(
        config=config,
        central=central,
        listener=FakeListener(),
        gui_capability=lambda: gui_ready,
        ring=FakeRing(),
        presenter=FakePresenter(),
    )


def _ringing(call_id: str, *, version: int = 1) -> dict[str, object]:
    return {
        "success": True,
        "call_id": call_id,
        "call_state": "ringing",
        "version": version,
        "can_accept": True,
        "can_decline": True,
        "handled_on": None,
        "terminal_platform": None,
    }


def test_central_terminal_http_adapter_is_gui_free_and_not_an_mcp_import_control() -> None:
    """Passing control: the existing central endpoint remains the sole API."""

    source = inspect.getsource(create_app)
    tree = ast.parse(source)
    assert "tkinter" not in source.lower()
    assert "fastmcp" not in source.lower()
    assert any(isinstance(node, ast.FunctionDef) and node.name == "create_app" for node in tree.body)


def test_portable_config_reports_gui_or_central_unavailability_honestly() -> None:
    central = FakeCentral()
    no_gui = _client(central=central, gui_ready=False)
    assert no_gui.start() == {"success": False, "status": "gui_unavailable"}
    assert central.issued_sessions == 0

    offline = _client(central=FakeCentral(available=False))
    assert offline.start() == {"success": False, "status": "central_unavailable"}
    assert offline.status()["gui_ready"] is True
    assert offline.status()["online"] is False


def test_notification_is_authenticated_acknowledged_and_deduped_by_call_and_version() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    assert client.start()["success"] is True
    event = {"call_id": "call-1", "version": 1, "event_type": "ringing"}

    assert client.receive_notification(event, callback_credential="central-callback") == {"success": False, "status": "notification_not_authenticated"}
    first = client.receive_notification(event, callback_credential="central-callback-secret")
    replay = client.receive_notification(event, callback_credential="central-callback-secret")
    assert first == {"success": True, "status": "presented", "call_id": "call-1"}
    assert replay == {"success": True, "status": "duplicate", "call_id": "call-1"}
    assert len(central.reads) == 1
    assert client.listener.acknowledgements == [event]
    assert client.ring.started == ["call-1"]
    assert len(client.presenter.shown) == 1


def test_notification_requires_distinct_configured_central_callback_credential() -> None:
    """A terminal bearer credential must never authorize central callbacks."""

    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    event = {"call_id": "call-1", "version": 1, "event_type": "ringing"}

    # Missing, terminal-credential and arbitrary callback values are all
    # rejected before acknowledgement, state read, GUI presentation or ringing.
    for credential in (None, "desktop-secret", "forged-callback"):
        assert client.receive_notification(event, callback_credential=credential) == {
            "success": False,
            "status": "notification_not_authenticated",
        }
    assert client.listener.acknowledgements == []
    assert central.reads == []
    assert client.presenter.shown == []
    assert client.ring.started == []
    assert central.states["call-1"]["call_state"] == "ringing"

    assert client.receive_notification(event, callback_credential="central-callback-secret")[
        "success"
    ] is True
    assert client.listener.acknowledgements == [event]


def test_missing_or_empty_callback_credential_is_inert_configuration_failure() -> None:
    """Callback authentication cannot silently fall back to another secret."""

    for callback_credential in (None, ""):
        central = FakeCentral(states={"call-1": _ringing("call-1")})
        client = _client(central=central, callback_credential=callback_credential)
        assert client.start() == {"success": False, "status": "invalid_config"}
        assert client.receive_notification(
            {"call_id": "call-1", "version": 1},
            callback_credential="central-callback-secret",
        ) == {"success": False, "status": "notification_not_authenticated"}
        assert central.issued_sessions == 0
        assert central.reads == []
        assert central.actions == []
        assert client.listener.acknowledgements == []
        assert client.presenter.shown == []
        assert client.ring.started == []
        assert central.states["call-1"]["call_state"] == "ringing"


def test_accept_decline_and_dismiss_use_only_bound_terminal_session_and_whole_call_semantics() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="central-callback-secret")

    opened = client.accept(call_id="call-1", operation_id="accept-1")
    assert opened == client.accept(call_id="call-1", operation_id="accept-1")
    assert central.actions == [("accept", "call-1", "desktop-session-1", "accept-1")]
    assert client.presenter.closed == ["call-1"]
    assert client.ring.stopped == ["call-1"]

    central.states["call-2"] = _ringing("call-2")
    client.receive_notification({"call_id": "call-2", "version": 1}, callback_credential="central-callback-secret")
    assert client.dismiss(call_id="call-2", operation_id="dismiss-1") == {"success": True, "status": "declined", "call_id": "call-2"}
    assert central.actions[-1] == ("decline", "call-2", "desktop-session-1", "dismiss-1")
    assert central.states["call-2"]["call_state"] == "declined"

    # The explicit decline command is not merely a local notification dismiss:
    # it must call the authenticated central decline operation and end the
    # whole eligible call.
    central.states["call-3"] = _ringing("call-3")
    client.receive_notification({"call_id": "call-3", "version": 1}, callback_credential="central-callback-secret")
    declined = client.decline(call_id="call-3", operation_id="decline-1")
    assert declined == {"success": True, "status": "declined", "call_id": "call-3"}
    assert central.actions[-1] == ("decline", "call-3", "desktop-session-1", "decline-1")
    assert central.states["call-3"]["call_state"] == "declined"


def test_operation_replay_is_scoped_to_call_id_not_only_terminal_session() -> None:
    """The same operation ID on separate calls cannot replay/cross-close."""

    central = FakeCentral(
        states={"call-a": _ringing("call-a"), "call-b": _ringing("call-b")}
    )
    client = _client(central=central)
    assert client.start()["success"] is True
    client.receive_notification(
        {"call_id": "call-a", "version": 1},
        callback_credential="central-callback-secret",
    )
    client.receive_notification(
        {"call_id": "call-b", "version": 1},
        callback_credential="central-callback-secret",
    )

    first = client.accept(call_id="call-a", operation_id="shared-operation")
    # The established public response carries additional desktop diagnostics in
    # the current adapter.  Assert its stable fields so this first operation
    # can succeed, then make the second-call assertion expose only an
    # incorrectly session-wide operation cache.
    assert first["success"] is True
    assert first["status"] == "opened"
    assert first["call_id"] == "call-a"
    second = client.accept(call_id="call-b", operation_id="shared-operation")
    assert second["success"] is True
    assert second["status"] == "opened"
    assert second["call_id"] == "call-b"
    assert central.actions == [
        ("accept", "call-a", "desktop-session-1", "shared-operation"),
        ("accept", "call-b", "desktop-session-1", "shared-operation"),
    ]
    assert central.states["call-a"]["call_state"] == "accepted"
    assert central.states["call-b"]["call_state"] == "accepted"
    assert client.presenter.closed == ["call-a", "call-b"]
    assert client.ring.stopped == ["call-a", "call-b"]


def test_sync_wake_and_remote_acceptance_close_stale_desktop_alert_without_takeover() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="central-callback-secret")

    # Another eligible terminal won centrally.  A wake/sync reads authority,
    # stops local ringing and never tries to inherit that owner session.
    central.states["call-1"] = {
        "success": True,
        "call_id": "call-1",
        "call_state": "accepted",
        "handled_on": "iphone_web",
        "terminal_platform": "ios",
        "can_accept": False,
        "can_decline": False,
    }
    assert client.sync() == {"success": True, "status": "synchronized", "call_ids": ["call-1"]}
    assert client.ring.stopped == ["call-1"]
    assert client.presenter.closed == ["call-1"]
    assert client.accept(call_id="call-1", operation_id="late") == {"success": False, "status": "session_not_found"}


def test_remote_acceptance_shows_handled_on_phone_hint_not_silent_close() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="central-callback-secret")

    central.states["call-1"] = {
        "success": True,
        "call_id": "call-1",
        "call_state": "accepted",
        "handled_on": "iphone_web",
        "terminal_platform": "ios",
        "can_accept": False,
        "can_decline": False,
    }
    client.receive_notification({"call_id": "call-1", "version": 2}, callback_credential="central-callback-secret")

    assert client.presenter.hints, "remote acceptance must show a handled-on-phone hint"
    assert client.presenter.hints[0][0] == "call-1"
    assert client.presenter.closed == ["call-1"]


def test_remote_acceptance_with_unreadable_call_uses_event_state_for_handled_hint() -> None:
    """After mobile accepts, the desktop cannot read the call (session_not_found).

    The callback event itself still carries the authoritative ``call_state`` and
    ``handled_on``.  The terminal must fall back to the event rather than return
    ``session_not_found``, so the "handled on phone" hint is shown and the local
    alert closes.
    """
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    client.receive_notification(
        {"call_id": "call-1", "version": 1},
        callback_credential="central-callback-secret",
    )

    central.states["call-1"] = {"success": False, "status": "session_not_found"}
    event = {"call_id": "call-1", "version": 2, "call_state": "accepted", "handled_on": "iphone_web"}
    client.receive_notification(event, callback_credential="central-callback-secret")

    assert client.presenter.hints, "event-only handled_on must still show the handled-on-phone hint"
    assert client.presenter.hints[0][0] == "call-1"
    assert client.presenter.closed == ["call-1"]
    assert client.ring.stopped == ["call-1"]


def test_central_loss_stops_ringing_and_restart_reauthenticates_before_resync() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="central-callback-secret")
    central.available = False
    assert client.sync() == {"success": False, "status": "central_unavailable"}
    assert client.ring.stopped == ["call-1"]

    central.available = True
    restarted = _client(central=central)
    assert restarted.start()["success"] is True
    assert central.issued_sessions == 2
    assert restarted.sync()["success"] is True
    assert restarted.accept(call_id="call-1", operation_id="takeover") == {"success": False, "status": "session_not_found"}


def test_desktop_public_semantics_are_platform_neutral_and_client_never_exposes_mcp() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="central-callback-secret")
    result = client.accept(call_id="call-1", operation_id="accept-1")
    assert result["handled_on"] == "desktop"
    assert result["terminal_platform"] == "linux"
    assert not hasattr(client, "create_mcp_app")
    assert not hasattr(client, "serve_mcp")
    assert not hasattr(client, "proxy_mcp")


def test_desktop_config_has_short_ringtone_path_defaulting_empty() -> None:
    config = _module().DesktopTerminalConfig.from_mapping({
        "central_url": "https://10.0.0.1:8443",
        "terminal_credential": "desktop-secret",
        "callback_credential": "callback-secret",
        "listener_host": "10.0.0.61",
        "listener_port": 9100,
    })
    assert getattr(config, "short_ringtone_path", None) == ""


def test_short_quiet_mode_rings_short_and_still_presents() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    event = {"call_id": "call-1", "version": 1, "call_state": "ringing", "quiet_mode": "short"}
    result = client.receive_notification(event, callback_credential="central-callback-secret")
    assert result["status"] == "presented"
    assert client.ring.modes == ["short"]
    assert client.ring.started == ["call-1"]
    assert client.presenter.shown


def test_silent_quiet_mode_presents_without_ringing() -> None:
    central = FakeCentral(states={"call-1": _ringing("call-1")})
    client = _client(central=central)
    client.start()
    event = {"call_id": "call-1", "version": 1, "call_state": "ringing", "quiet_mode": "silent"}
    result = client.receive_notification(event, callback_credential="central-callback-secret")
    assert result["status"] == "presented"
    assert client.ring.started == []
    assert client.presenter.shown
