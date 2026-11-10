"""Red contract for the desktop terminal's short ringtone startup + persistence.

``RingtonePlayer`` must accept ``short_ringtone_path`` at construction (default
empty) so ``start(call_id, "short")`` plays the configured short file (non-loop)
rather than falling back to the bell.  The chosen short ringtone must round-trip
through a local state file (``save_short_ringtone``/``load_short_ringtone``).
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


def _module() -> Any:
    return importlib.import_module("central_desktop_terminal")


class FakePlayer:
    def __init__(self) -> None:
        self.plays: list[tuple[str, bool]] = []

    def play(self, path: str, loop: bool = True) -> object:
        self.plays.append((path, loop))
        return object()

    def stop(self, _handle: object) -> None:
        return None


def test_ringtone_player_short_ringtone_path_constructor_and_short_start(tmp_path: Path) -> None:
    RingtonePlayer = _module().RingtonePlayer
    short_file = tmp_path / "short.mp3"
    short_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path="/nonexistent/long.mp3", short_ringtone_path=str(short_file), player=player)
    ring.start("call-1", "short")

    assert player.plays == [(str(short_file), False)], (
        "short start must play the configured short_ringtone_path without looping"
    )


def test_short_ringtone_save_load_round_trip(tmp_path: Path) -> None:
    module = _module()
    state_file = tmp_path / "terminal-state.json"

    module.save_short_ringtone(str(state_file), "/sounds/short.mp3")
    assert module.load_short_ringtone(str(state_file)) == "/sounds/short.mp3"


def test_short_ringtone_load_defaults_empty_when_absent(tmp_path: Path) -> None:
    module = _module()
    assert module.load_short_ringtone(str(tmp_path / "missing.json")) == ""
