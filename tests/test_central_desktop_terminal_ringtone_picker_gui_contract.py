"""Red contract for the desktop terminal ringtone picker GUI wiring.

The contract seams ``scan_ringtones``, ``RingtonePlayer.set_ringtone`` and
``DesktopTerminalClient.set_auto_deny`` already exist, but ``main()`` still has
no settings panel: the main window is empty, with no ringtone dropdown and no
do-not-disturb toggle.  These tests pin an extractable, headless-testable
build function that the main window calls:

``build_settings_panel(root, ring, client, ringtones_dir, mp3s, initial_ringtone, auto_deny, tk)``

It must create a ``tk.OptionMenu`` whose options are the ``*.mp3`` filenames
and a ``tk.Checkbutton`` labelled 勿扰模式（来电自动拒绝）, preselect the
filename matching ``initial_ringtone`` (or the first filename), initialise the
checkbutton from ``auto_deny``, and wire a ringtone change to
``ring.set_ringtone(os.path.join(ringtones_dir, <selected filename>))`` and a
checkbutton change to ``client.set_auto_deny``.
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


class FakeTk:
    def __init__(self) -> None:
        self.option_menus: list[dict[str, object]] = []
        self.checkbuttons: list[dict[str, object]] = []

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

    def Label(self, *_args: object, **_kwargs: object) -> object:
        return object()


class FakeRing:
    def __init__(self) -> None:
        self.set_ringtone_calls: list[str] = []

    def set_ringtone(self, path: str) -> None:
        self.set_ringtone_calls.append(path)


class FakeClient:
    def __init__(self) -> None:
        self.set_auto_deny_calls: list[bool] = []

    def set_auto_deny(self, enabled: bool) -> None:
        self.set_auto_deny_calls.append(enabled)


def test_build_settings_panel_creates_option_menu_and_checkbutton() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    build_settings_panel(None, FakeRing(), FakeClient(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/beta.mp3", False, tk)

    assert len(tk.option_menus) == 1
    assert tk.option_menus[0]["options"] == ["alpha.mp3", "beta.mp3"]
    assert len(tk.checkbuttons) == 1
    assert tk.checkbuttons[0]["text"] == "勿扰模式（来电自动拒绝）"
    assert tk.option_menus[0]["widget"].pack_calls == [{"anchor": "w", "padx": 12, "pady": 6}]
    assert tk.checkbuttons[0]["widget"].pack_calls == [{"anchor": "w", "padx": 12, "pady": 6}]


def test_initial_selection_matches_ringtone_path_or_first() -> None:
    build_settings_panel = _build()
    matching = FakeTk()
    build_settings_panel(None, FakeRing(), FakeClient(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/beta.mp3", False, matching)
    assert matching.option_menus[0]["variable"].get() == "beta.mp3"
    assert matching.checkbuttons[0]["variable"].get() is False

    missing = FakeTk()
    build_settings_panel(None, FakeRing(), FakeClient(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/missing.mp3", True, missing)
    assert missing.option_menus[0]["variable"].get() == "alpha.mp3"
    assert missing.checkbuttons[0]["variable"].get() is True


def test_option_menu_change_calls_set_ringtone_with_full_path() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    ring = FakeRing()
    build_settings_panel(None, ring, FakeClient(), "/sounds", ["alpha.mp3", "beta.mp3"], "/sounds/alpha.mp3", False, tk)

    tk.option_menus[0]["variable"].set("beta.mp3")
    assert ring.set_ringtone_calls == [os.path.join("/sounds", "beta.mp3")]


def test_checkbutton_change_calls_set_auto_deny() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    client = FakeClient()
    build_settings_panel(None, FakeRing(), client, "/sounds", ["alpha.mp3"], "/sounds/alpha.mp3", False, tk)

    tk.checkbuttons[0]["variable"].set(True)
    assert client.set_auto_deny_calls == [True]
