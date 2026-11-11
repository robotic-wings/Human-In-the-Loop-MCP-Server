"""Contract for the desktop terminal's ringtone playback.

These tests pin the ringtone configuration and the ``RingtonePlayer``
behaviour: a ``start``/``stop`` interface that plays a configured file
(looping), stops on demand, and falls back to the window bell without error
when no file is available.  Local do-not-disturb was removed in favour of the
central DND authority and is no longer covered here.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from central_desktop_terminal import DesktopTerminalConfig


BASE_CONFIG: dict[str, object] = {
    "central_url": "https://central.test",
    "callsign": "desktop-61",
    "operator_password": "desktop-secret",
    "device_id": "desktop-61",
    "terminal_credential": "desktop-secret",
    "callback_credential": "callback-secret",
    "listener_host": "127.0.0.1",
    "listener_port": 9100,
}


def test_config_defaults_ringtone_alt_null_and_has_default_ringtone() -> None:
    config = DesktopTerminalConfig.from_mapping({**BASE_CONFIG})
    assert config.ringtone_alt_path is None
    assert isinstance(config.ringtone_path, str) and config.ringtone_path


def test_config_parses_ringtone_paths() -> None:
    config = DesktopTerminalConfig.from_mapping({
        **BASE_CONFIG,
        "ringtone_path": "/sounds/ring.mp3",
        "ringtone_alt_path": "/sounds/alt.mp3",
    })
    assert config.ringtone_path == "/sounds/ring.mp3"
    assert config.ringtone_alt_path == "/sounds/alt.mp3"


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
