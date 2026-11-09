"""Red contract for the desktop terminal's ringtone and do-not-disturb.

The terminal currently ships ``SilentRing`` (a no-op) and has no ``auto_deny``.
These tests pin the two missing behaviours:

* ``DesktopTerminalConfig`` parses ``ringtone_path`` (with a default), an
  optional ``ringtone_alt_path`` and an ``auto_deny`` boolean defaulting to
  ``false``.
* With ``auto_deny`` enabled, ``DesktopTerminalClient.receive_notification``
  declines the call instead of presenting it or ringing, so the Agent receives
  ``declined``/``human_declined`` and the busy slot is released.
* A real ringtone player replaces ``SilentRing`` with a ``start``/``stop``
  interface that plays a configured file (looping), stops on demand, and falls
  back to the window bell without error when no file is available.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from central_desktop_terminal import DesktopTerminalClient, DesktopTerminalConfig


BASE_CONFIG: dict[str, object] = {
    "central_url": "https://central.test",
    "terminal_credential": "desktop-secret",
    "callback_credential": "callback-secret",
    "listener_host": "127.0.0.1",
    "listener_port": 9100,
}


def test_config_defaults_auto_deny_false_ringtone_alt_null_and_has_default_ringtone() -> None:
    config = DesktopTerminalConfig.from_mapping({**BASE_CONFIG})
    assert config.auto_deny is False
    assert config.ringtone_alt_path is None
    assert isinstance(config.ringtone_path, str) and config.ringtone_path


def test_config_parses_auto_deny_and_ringtone_paths() -> None:
    config = DesktopTerminalConfig.from_mapping({
        **BASE_CONFIG,
        "auto_deny": True,
        "ringtone_path": "/sounds/ring.mp3",
        "ringtone_alt_path": "/sounds/alt.mp3",
    })
    assert config.auto_deny is True
    assert config.ringtone_path == "/sounds/ring.mp3"
    assert config.ringtone_alt_path == "/sounds/alt.mp3"


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
        return dict(self.states.get(call_id, {"success": False, "status": "session_not_found"}))

    def act(self, action: str, call_id: str, session_id: str, operation_id: str) -> dict[str, object]:
        self.actions.append((action, call_id, session_id, operation_id))
        if call_id not in self.states:
            return {"success": False, "status": "session_not_found"}
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
    stopped: list[str] = field(default_factory=list)

    def start(self, call_id: str) -> None:
        self.started.append(call_id)

    def stop(self, call_id: str) -> None:
        self.stopped.append(call_id)


@dataclass
class FakePresenter:
    shown: list[dict[str, object]] = field(default_factory=list)
    closed: list[str] = field(default_factory=list)

    def show(self, state: dict[str, object]) -> None:
        self.shown.append(dict(state))

    def close(self, call_id: str) -> None:
        self.closed.append(call_id)


def _client(*, config: dict[str, object], central: FakeCentral, ring: FakeRing,
            presenter: FakePresenter) -> DesktopTerminalClient:
    return DesktopTerminalClient(
        config=config,
        central=central,
        listener=FakeListener(),
        gui_capability=lambda: True,
        ring=ring,
        presenter=presenter,
    )


def test_auto_deny_declines_without_showing_or_ringing() -> None:
    central = FakeCentral(states={
        "call-1": {"success": True, "call_id": "call-1", "call_state": "ringing", "version": 1},
    })
    ring = FakeRing()
    presenter = FakePresenter()
    client = _client(
        config={"terminal_credential": "desktop-secret", "callback_credential": "callback-secret", "auto_deny": True},
        central=central, ring=ring, presenter=presenter,
    )
    assert client.start()["success"] is True

    result = client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="callback-secret")

    assert presenter.shown == []
    assert ring.started == []
    assert any(action[0] == "decline" for action in central.actions)
    assert result.get("success") is True


def _ringtone_class() -> Any:
    return getattr(importlib.import_module("central_desktop_terminal"), "RingtonePlayer")


class FakePlayer:
    def __init__(self) -> None:
        self.plays: list[tuple[str, bool]] = []
        self.stops: int = 0

    def play(self, path: str, loop: bool = True) -> object:
        self.plays.append((path, loop))
        return object()

    def stop(self, _handle: object) -> None:
        self.stops += 1


def test_ringtone_plays_valid_file_looping_and_stops(tmp_path: Path) -> None:
    RingtonePlayer = _ringtone_class()
    ring_file = tmp_path / "ring.mp3"
    ring_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path=str(ring_file), player=player)
    ring.start("call-1")
    assert player.plays == [(str(ring_file), True)]
    ring.stop("call-1")
    assert player.stops == 1


def test_ringtone_missing_file_falls_back_to_bell_without_error() -> None:
    RingtonePlayer = _ringtone_class()
    bells: list[bool] = []
    player = FakePlayer()

    ring = RingtonePlayer(
        ringtone_path="/nonexistent/missing.mp3", player=player,
        bell=lambda: bells.append(True),
    )
    ring.start("call-1")
    assert bells == [True]
    assert player.plays == []
