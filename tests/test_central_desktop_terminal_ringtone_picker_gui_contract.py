"""Contract for the desktop terminal ringtone picker GUI wiring.

The contract seams ``scan_ringtones`` and ``RingtonePlayer.set_ringtone`` exist,
and ``main()`` must render a settings panel.  These tests pin an extractable,
headless-testable build function that the main window calls:

``build_settings_panel(root, ring, central, ringtones_dir, mp3s, initial_ringtone, tk)``

It must create a ``tk.OptionMenu`` whose options are the ``*.mp3`` filenames
and a ``tk.Checkbutton`` labelled 勿扰模式（来电自动拒绝）, preselect the
filename matching ``initial_ringtone`` (or the first filename), and wire a
ringtone change to ``ring.set_ringtone(os.path.join(ringtones_dir, <selected
filename>))``.  Do-not-disturb is driven through ``central`` (covered by the
central DND contract) rather than a local toggle.
"""

from __future__ import annotations

import importlib
import os
from typing import Any


def _build() -> Any:
    return getattr(importlib.import_module("central_desktop_terminal"), "build_settings_panel")


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
    def grid(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        return None



class FakeEntry:
    def __init__(self) -> None:
        self._text = ""
        self.pack_calls: list[dict[str, object]] = []
        self.state = "normal"

    def get(self) -> str:
        return self._text

    def pack(self, **kwargs: object) -> "FakeEntry":
        self.pack_calls.append(dict(kwargs))
        return self
    def grid(self, *_args: object, **_kwargs: object) -> "FakeEntry":
        return self


    def config(self, **kwargs: object) -> "FakeEntry":
        if "state" in kwargs:
            self.state = str(kwargs["state"])
        return self

    def bind(self, _event: str, _callback: Any) -> str:
        return "bind-id"


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
    def grid(self, *_args: object, **_kwargs: object) -> "FakeButton":
        return self



class FakeTk:
    def __init__(self) -> None:
        self.option_menus: list[dict[str, object]] = []
        self.checkbuttons: list[dict[str, object]] = []
        self.entries: list[FakeEntry] = []
        self.buttons: list[FakeButton] = []
    def LabelFrame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()

    def Frame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()


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

    def Label(self, *_args: object, **_kwargs: object) -> FakeWidget:
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
    def __init__(self) -> None:
        self.set_dnd_calls: list[tuple[bool, str]] = []
        self.get_dnd_calls: int = 0

    def set_dnd(self, enabled: bool, message: str) -> None:
        self.set_dnd_calls.append((enabled, message))

    def get_dnd(self) -> dict[str, object]:
        self.get_dnd_calls += 1
        return {"enabled": False, "message": ""}


def test_build_settings_panel_creates_option_menu_and_checkbutton() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    build_settings_panel(None, FakeRing(), FakeCentral(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/beta.mp3", tk)

    assert len(tk.option_menus) == 1
    assert tk.option_menus[0]["options"] == ["alpha.mp3", "beta.mp3"]
    assert len(tk.checkbuttons) == 1
    assert tk.checkbuttons[0]["text"] == "来电自动拒绝"


def test_initial_selection_matches_ringtone_path_or_first() -> None:
    build_settings_panel = _build()
    matching = FakeTk()
    build_settings_panel(None, FakeRing(), FakeCentral(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/beta.mp3", matching)
    assert matching.option_menus[0]["variable"].get() == "beta.mp3"

    missing = FakeTk()
    build_settings_panel(None, FakeRing(), FakeCentral(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/missing.mp3", missing)
    assert missing.option_menus[0]["variable"].get() == "alpha.mp3"


def test_option_menu_change_calls_set_ringtone_with_full_path() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    ring = FakeRing()
    build_settings_panel(None, ring, FakeCentral(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/alpha.mp3", tk)

    tk.option_menus[0]["variable"].set("beta.mp3")
    assert ring.set_ringtone_calls == [os.path.join("/sounds", "beta.mp3")]
