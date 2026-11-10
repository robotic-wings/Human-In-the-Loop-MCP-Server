"""Red contract for the desktop settings panel's quiet-window group.

The central service persists ``quiet_window_seconds``/``quiet_mode`` but the
desktop settings panel offers no "接听后静音" group to read and change it.
These tests pin a new group: a short/silent mode radio, a duration entry
(minutes, default 5 = 300 seconds), a save button and a ``get_quiet_window``
backfill, mirroring the DND save interaction.
"""

from __future__ import annotations

import importlib
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
        self.state = "normal"

    def get(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

    def grid(self, *_args: object, **_kwargs: object) -> "FakeEntry":
        return self

    def config(self, **kwargs: object) -> "FakeEntry":
        if "state" in kwargs:
            self.state = str(kwargs["state"])
        return self


class FakeButton:
    def __init__(self, text: str, command: Any) -> None:
        self.text = text
        self.command = command

    def grid(self, *_args: object, **_kwargs: object) -> "FakeButton":
        return self


class FakeTk:
    def __init__(self) -> None:
        self.labels: list[str] = []
        self.radiobuttons: list[dict[str, object]] = []
        self.entries: list[FakeEntry] = []
        self.buttons: list[FakeButton] = []
        self.option_menus: list[dict[str, object]] = []
        self.checkbuttons: list[dict[str, object]] = []

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
        self.checkbuttons.append(dict(kwargs))
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
    def set_ringtone(self, _path: str) -> None:
        pass

    def set_short_ringtone(self, _path: str) -> None:
        pass

    def toggle_preview(self) -> bool:
        return False

    def stop_preview(self) -> None:
        pass


class FakeCentral:
    def __init__(self, quiet: dict[str, object] | None = None) -> None:
        self._quiet = quiet if quiet is not None else {"quiet_window_seconds": 300, "quiet_mode": "short"}
        self.get_quiet_window_calls: int = 0
        self.set_quiet_window_calls: list[tuple[int, str]] = []

    def get_dnd(self) -> dict[str, object]:
        return {"enabled": False, "message": ""}

    def set_dnd(self, _enabled: bool, _message: str) -> None:
        pass

    def get_quiet_window(self) -> dict[str, object]:
        self.get_quiet_window_calls += 1
        return dict(self._quiet)

    def set_quiet_window(self, quiet_window_seconds: int, quiet_mode: str) -> None:
        self.set_quiet_window_calls.append((quiet_window_seconds, quiet_mode))


def test_settings_panel_has_quiet_window_group() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    central = FakeCentral()

    build_settings_panel(None, FakeRing(), central, "/sounds", ["a.mp3"], "/sounds/a.mp3", tk)

    assert "接听后静音" in tk.labels
    assert central.get_quiet_window_calls == 1
    assert sorted(str(rb["value"]) for rb in tk.radiobuttons) == ["short", "silent"]
    assert any(e.get() == "5" for e in tk.entries)


def test_quiet_save_button_persists_seconds_and_mode() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    central = FakeCentral({"quiet_window_seconds": 120, "quiet_mode": "silent"})

    build_settings_panel(None, FakeRing(), central, "/sounds", ["a.mp3"], "/sounds/a.mp3", tk)

    save_buttons = [b for b in tk.buttons if b.text == "保存"]
    assert save_buttons, "the quiet group must have a save button"
    save_buttons[-1].command()
    assert central.set_quiet_window_calls
    seconds, mode = central.set_quiet_window_calls[-1]
    assert mode == "silent"
    assert seconds == 120
