"""Red contract for the short-ringtone preview state machine.

``RingtonePlayer`` gains ``toggle_short_preview()``/``stop_short_preview()``
(non-loop preview, ``True`` when it starts playing / ``False`` when it stops),
and long/short previews are mutually exclusive: starting one stops the other.
The settings panel's short-ringtone row gains a "试听" button wired to
``toggle_short_preview``.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any


def _ringtone_class() -> Any:
    return getattr(importlib.import_module("central_desktop_terminal"), "RingtonePlayer")


def _build() -> Any:
    return getattr(importlib.import_module("central_desktop_terminal"), "build_settings_panel")


class FakePlayer:
    def __init__(self) -> None:
        self.plays: list[tuple[str, bool]] = []
        self.stops: int = 0

    def play(self, path: str, loop: bool = True) -> object:
        self.plays.append((path, loop))
        return object()

    def stop(self, _handle: object) -> None:
        self.stops += 1


def test_toggle_short_preview_plays_non_loop_and_toggles(tmp_path: Path) -> None:
    RingtonePlayer = _ringtone_class()
    short_file = tmp_path / "short.mp3"
    short_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path="/nonexistent/long.mp3", short_ringtone_path=str(short_file), player=player)
    assert ring.toggle_short_preview() is True
    assert player.plays == [(str(short_file), False)]
    assert ring.toggle_short_preview() is False
    assert player.stops == 1


def test_stop_short_preview_stops_and_clears_handle(tmp_path: Path) -> None:
    RingtonePlayer = _ringtone_class()
    short_file = tmp_path / "short.mp3"
    short_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path="/nonexistent/long.mp3", short_ringtone_path=str(short_file), player=player)
    assert ring.toggle_short_preview() is True
    ring.stop_short_preview()
    assert player.stops == 1
    assert ring.toggle_short_preview() is True
    assert len(player.plays) == 2


def test_long_and_short_preview_are_mutually_exclusive(tmp_path: Path) -> None:
    RingtonePlayer = _ringtone_class()
    long_file = tmp_path / "long.mp3"
    long_file.write_bytes(b"RIFF fake audio")
    short_file = tmp_path / "short.mp3"
    short_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path=str(long_file), short_ringtone_path=str(short_file), player=player)

    assert ring.toggle_preview() is True
    assert ring.toggle_short_preview() is True
    assert player.stops == 1, "starting the short preview must stop the long preview"
    assert player.plays[-1] == (str(short_file), False)

    assert ring.toggle_preview() is True
    assert player.stops == 2, "starting the long preview must stop the short preview"
    assert player.plays[-1] == (str(long_file), True)


class FakeVar:
    def __init__(self, value: object = None) -> None:
        self._value = value
        self._traces: list[Any] = []

    def get(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._value = value
        for callback in list(self._traces):
            callback()

    def trace_add(self, _mode: str, callback: Any) -> str:
        self._traces.append(callback)
        return "trace-id"


class FakeWidget:
    def pack(self, **kwargs: object) -> "FakeWidget":
        return self

    def grid(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self


class FakeEntry:
    def get(self) -> str:
        return ""

    def grid(self, *_args: object, **_kwargs: object) -> "FakeEntry":
        return self

    def config(self, **kwargs: object) -> "FakeEntry":
        return self


class FakeButton:
    def __init__(self, text: str, command: Any) -> None:
        self.text = text
        self.command = command

    def grid(self, *_args: object, **_kwargs: object) -> "FakeButton":
        return self


class FakeTk:
    def __init__(self) -> None:
        self.buttons: list[FakeButton] = []
        self.labels: list[str] = []
        self.option_menus: list[dict[str, object]] = []
        self.entries: list[FakeEntry] = []
        self.radiobuttons: list[dict[str, object]] = []

    def LabelFrame(self, _root: object, **kwargs: object) -> FakeWidget:
        return FakeWidget()

    def StringVar(self, value: str = "") -> FakeVar:
        return FakeVar(value)

    def BooleanVar(self, value: bool = False) -> FakeVar:
        return FakeVar(value)

    def OptionMenu(self, _root: object, variable: FakeVar, *options: str) -> FakeWidget:
        self.option_menus.append({"variable": variable, "options": list(options)})
        return FakeWidget()

    def Checkbutton(self, _root: object, **kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Radiobutton(self, _root: object, **kwargs: object) -> FakeWidget:
        self.radiobuttons.append(dict(kwargs))
        return FakeWidget()

    def Entry(self, *_args: object, **_kwargs: object) -> FakeEntry:
        entry = FakeEntry()
        self.entries.append(entry)
        return entry

    def Label(self, *_args: object, **kwargs: object) -> FakeWidget:
        text = kwargs.get("text")
        if isinstance(text, str):
            self.labels.append(text)
        return FakeWidget()

    def Button(self, _root: object, **kwargs: object) -> FakeButton:
        button = FakeButton(str(kwargs.get("text", "")), kwargs.get("command"))
        self.buttons.append(button)
        return button


class FakeRing:
    def __init__(self) -> None:
        self.toggle_short_preview_calls: int = 0

    def set_ringtone(self, _path: str) -> None:
        pass

    def set_short_ringtone(self, _path: str) -> None:
        pass

    def toggle_preview(self) -> bool:
        return False

    def stop_preview(self) -> None:
        pass

    def toggle_short_preview(self) -> bool:
        self.toggle_short_preview_calls += 1
        return True


class FakeCentral:
    def get_dnd(self) -> dict[str, object]:
        return {"enabled": False, "message": ""}

    def set_dnd(self, _enabled: bool, _message: str) -> None:
        pass

    def get_quiet_window(self) -> dict[str, object]:
        return {"quiet_window_seconds": 300, "quiet_mode": "short"}

    def set_quiet_window(self, _seconds: int, _mode: str) -> None:
        pass


def test_settings_panel_short_row_has_preview_button() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    ring = FakeRing()

    build_settings_panel(None, ring, FakeCentral(), "/sounds", ["a.mp3"], "/sounds/a.mp3", tk, "a.mp3")

    short_preview_buttons = [b for b in tk.buttons if b.text == "试听"]
    assert short_preview_buttons, "the short ringtone row must have a 试听 preview button"
    short_preview_buttons[0].command()
    assert ring.toggle_short_preview_calls == 1
