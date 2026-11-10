"""Red contract for the desktop terminal's ringtone preview state machine.

``RingtonePlayer`` gains a preview toggle and the settings panel gains a
"铃声：" label plus a "测试播放" button that flips to "停止播放" while
previewing.  These tests pin the state machine and the panel wiring:

* ``toggle_preview()`` plays (loop) and returns ``True`` when idle, stops and
  returns ``False`` when already previewing, and falls back to the bell
  (returning ``False``) when no ringtone is available.
* ``stop_preview()`` stops and clears the preview handle.
* The panel renders the label and button, toggles the button text on preview,
  and resets the text when the dropdown changes ringtone.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any

from central_desktop_terminal import RingtonePlayer


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


def test_toggle_preview_plays_when_idle_and_stops_when_previewing(tmp_path: Path) -> None:
    RingtonePlayer = _ringtone_class()
    ring_file = tmp_path / "ring.mp3"
    ring_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path=str(ring_file), player=player)
    assert ring.toggle_preview() is True
    assert player.plays == [(str(ring_file), True)]
    assert ring.toggle_preview() is False
    assert player.stops == 1


def test_toggle_preview_without_ringtone_falls_back_to_bell_and_returns_false() -> None:
    RingtonePlayer = _ringtone_class()
    bells: list[bool] = []
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path="/nonexistent/missing.mp3", player=player, bell=lambda: bells.append(True))
    assert ring.toggle_preview() is False
    assert bells == [True]
    assert player.plays == []


def test_stop_preview_stops_and_clears_handle(tmp_path: Path) -> None:
    RingtonePlayer = _ringtone_class()
    ring_file = tmp_path / "ring.mp3"
    ring_file.write_bytes(b"RIFF fake audio")
    player = FakePlayer()

    ring = RingtonePlayer(ringtone_path=str(ring_file), player=player)
    assert ring.toggle_preview() is True
    ring.stop_preview()
    assert player.stops == 1
    assert ring.toggle_preview() is True
    assert len(player.plays) == 2


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
    def __init__(self) -> None:
        self.pack_calls: list[dict[str, object]] = []

    def pack(self, **kwargs: object) -> "FakeWidget":
        self.pack_calls.append(dict(kwargs))
        return self


class FakeEntry:
    def __init__(self) -> None:
        self._text = ""
        self.state = "normal"

    def get(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def pack(self, **kwargs: object) -> "FakeEntry":
        return self

    def config(self, **kwargs: object) -> "FakeEntry":
        if "state" in kwargs:
            self.state = str(kwargs["state"])
        return self


class FakeButton:
    def __init__(self, text: str, command: Any) -> None:
        self.text = text
        self.command = command
        self.pack_calls: list[dict[str, object]] = []

    def config(self, **kwargs: object) -> "FakeButton":
        if "text" in kwargs:
            self.text = str(kwargs["text"])
        return self

    def pack(self, **kwargs: object) -> "FakeButton":
        self.pack_calls.append(dict(kwargs))
        return self


class FakeTk:
    def __init__(self) -> None:
        self.option_menus: list[dict[str, object]] = []
        self.checkbuttons: list[dict[str, object]] = []
        self.entries: list[FakeEntry] = []
        self.labels: list[str] = []
        self.buttons: list[FakeButton] = []

    def StringVar(self, value: str = "") -> FakeVar:
        return FakeVar(value)

    def BooleanVar(self, value: bool = False) -> FakeVar:
        return FakeVar(value)

    def OptionMenu(self, _root: object, variable: FakeVar, *options: str) -> FakeWidget:
        widget = FakeWidget()
        self.option_menus.append({"variable": variable, "options": list(options), "widget": widget})
        return widget

    def Checkbutton(self, _root: object, **kwargs: object) -> FakeWidget:
        widget = FakeWidget()
        self.checkbuttons.append({**dict(kwargs), "widget": widget})
        return widget

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
        self.set_ringtone_calls: list[str] = []
        self.stop_preview_calls: int = 0
        self._previewing = False

    def set_ringtone(self, path: str) -> None:
        self.set_ringtone_calls.append(path)

    def toggle_preview(self) -> bool:
        self._previewing = not self._previewing
        return self._previewing

    def stop_preview(self) -> None:
        self.stop_preview_calls += 1
        self._previewing = False


class FakeCentral:
    def get_dnd(self) -> dict[str, object]:
        return {"enabled": False, "message": ""}

    def set_dnd(self, _enabled: bool, _message: str) -> None:
        pass


def test_settings_panel_has_label_and_preview_button() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    build_settings_panel(None, FakeRing(), FakeCentral(), "/sounds", ["a.mp3"], "/sounds/a.mp3", tk)

    assert "铃声：" in tk.labels
    assert len(tk.buttons) == 1
    assert tk.buttons[0].text == "测试播放"


def test_preview_button_toggles_text() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    build_settings_panel(None, FakeRing(), FakeCentral(), "/sounds", ["a.mp3"], "/sounds/a.mp3", tk)

    button = tk.buttons[0]
    button.command()
    assert button.text == "停止播放"
    button.command()
    assert button.text == "测试播放"


def test_dropdown_change_resets_preview_button_text() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    ring = FakeRing()
    build_settings_panel(None, ring, FakeCentral(), "/sounds", ["a.mp3", "b.mp3"], "/sounds/a.mp3", tk)

    button = tk.buttons[0]
    button.command()
    assert button.text == "停止播放"

    tk.option_menus[0]["variable"].set("b.mp3")
    assert ring.stop_preview_calls == 1
    assert button.text == "测试播放"
