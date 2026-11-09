"""Red contract for the desktop terminal's ringtone picker revision.

The ringtone selection is revised from a file chooser to a preset ringtones
folder with a dropdown.  These tests pin the three new seams:

* ``DesktopTerminalConfig`` gains ``ringtones_dir`` (a configurable directory,
  defaulting to a ``ringtones`` directory next to the working directory).
* A scan function returns the sorted ``*.mp3`` filenames in that directory
  (empty or mp3-free directories yield an empty list).
* ``RingtonePlayer.set_ringtone`` changes the file the player uses, and
  ``DesktopTerminalClient.set_auto_deny`` toggles the do-not-disturb behaviour.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from central_desktop_terminal import DesktopTerminalClient, DesktopTerminalConfig, RingtonePlayer


BASE_CONFIG: dict[str, object] = {
    "central_url": "https://central.test",
    "terminal_credential": "desktop-secret",
    "callback_credential": "callback-secret",
    "listener_host": "127.0.0.1",
    "listener_port": 9100,
}


def test_config_parses_ringtones_dir() -> None:
    config = DesktopTerminalConfig.from_mapping({**BASE_CONFIG, "ringtones_dir": "/custom/ringtones"})
    assert config.ringtones_dir == "/custom/ringtones"


def test_config_default_ringtones_dir_is_nonempty() -> None:
    config = DesktopTerminalConfig.from_mapping({**BASE_CONFIG})
    assert isinstance(config.ringtones_dir, str) and config.ringtones_dir


def _scan() -> Any:
    return getattr(importlib.import_module("central_desktop_terminal"), "scan_ringtones")


def test_scan_ringtones_lists_sorted_mp3_filenames(tmp_path: Path) -> None:
    scan_ringtones = _scan()
    (tmp_path / "beta.mp3").write_bytes(b"x")
    (tmp_path / "alpha.mp3").write_bytes(b"x")
    (tmp_path / "notes.txt").write_bytes(b"x")
    (tmp_path / "subdir").mkdir()
    assert scan_ringtones(str(tmp_path)) == ["alpha.mp3", "beta.mp3"]


def test_scan_ringtones_empty_or_mp3_free_directory_returns_empty(tmp_path: Path) -> None:
    scan_ringtones = _scan()
    empty = tmp_path / "empty"
    empty.mkdir()
    assert scan_ringtones(str(empty)) == []
    only_text = tmp_path / "only-text"
    only_text.mkdir()
    (only_text / "readme.txt").write_bytes(b"x")
    assert scan_ringtones(str(only_text)) == []


class FakePlayer:
    def __init__(self) -> None:
        self.plays: list[tuple[str, bool]] = []
        self.stops: int = 0

    def play(self, path: str, loop: bool = True) -> object:
        self.plays.append((path, loop))
        return object()

    def stop(self, _handle: object) -> None:
        self.stops += 1


def test_set_ringtone_changes_the_playback_path(tmp_path: Path) -> None:
    old = tmp_path / "old.mp3"
    old.write_bytes(b"x")
    new = tmp_path / "new.mp3"
    new.write_bytes(b"x")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path=str(old), player=player)
    ring.set_ringtone(str(new))
    ring.start("call-1")
    assert player.plays == [(str(new), True)]


@dataclass
class FakeCentral:
    issued_sessions: int = 0
    actions: list[tuple[str, str, str, str]] = field(default_factory=list)
    states: dict[str, dict[str, object]] = field(default_factory=dict)

    def authenticate_desktop(self, credential: str) -> dict[str, object]:
        self.issued_sessions += 1
        return {"success": True, "session_id": f"desktop-session-{self.issued_sessions}"}

    def read_call(self, call_id: str, session_id: str) -> dict[str, object]:
        return dict(self.states.get(call_id, {"success": False, "status": "session_not_found"}))

    def act(self, action: str, call_id: str, session_id: str, operation_id: str) -> dict[str, object]:
        self.actions.append((action, call_id, session_id, operation_id))
        self.states[call_id] = {"success": True, "call_id": call_id, "call_state": "declined"}
        return {"success": True, "status": "declined", "call_id": call_id}


@dataclass
class FakeListener:
    def acknowledge(self, _event: dict[str, object]) -> None:
        pass


@dataclass
class FakeRing:
    started: list[str] = field(default_factory=list)

    def start(self, call_id: str) -> None:
        self.started.append(call_id)

    def stop(self, call_id: str) -> None:
        pass


@dataclass
class FakePresenter:
    shown: list[dict[str, object]] = field(default_factory=list)

    def show(self, state: dict[str, object]) -> None:
        self.shown.append(dict(state))

    def close(self, call_id: str) -> None:
        pass


def test_set_auto_deny_toggles_decline_behaviour() -> None:
    central = FakeCentral(states={
        "call-1": {"success": True, "call_id": "call-1", "call_state": "ringing", "version": 1},
    })
    ring = FakeRing()
    presenter = FakePresenter()
    client = DesktopTerminalClient(
        config={"terminal_credential": "desktop-secret", "callback_credential": "callback-secret"},
        central=central, listener=FakeListener(), gui_capability=lambda: True,
        ring=ring, presenter=presenter,
    )
    assert client.start()["success"] is True

    client.set_auto_deny(True)
    result = client.receive_notification({"call_id": "call-1", "version": 1}, callback_credential="callback-secret")

    assert presenter.shown == []
    assert ring.started == []
    assert any(action[0] == "decline" for action in central.actions)
    assert result.get("status") == "declined"
