"""Red contract for wiring the desktop terminal's DND to the central authority.

The central service now owns do-not-disturb (``lifecycle_dnd`` +
``/api/terminals/dnd``), but the desktop terminal still keeps a local
``auto_deny`` and never talks to the central DND endpoint.  These tests pin the
two missing client seams: ``CentralHttpDesktopClient.set_dnd``/``get_dnd``, and
a settings panel that drives DND through the central client (with a message
entry and a ``get_dnd`` backfill) instead of local auto-deny.
"""

from __future__ import annotations

import base64
import importlib
from pathlib import Path
from typing import Any

import httpx

from central_desktop_terminal import CentralHttpDesktopClient, DesktopTerminalConfig


def _config() -> DesktopTerminalConfig:
    return DesktopTerminalConfig(
        central_url="https://central.test",
        terminal_credential="desktop-secret",
        callback_credential="callback-secret",
        listener_host="127.0.0.1",
        listener_port=9100,
        verify_tls=True,
    )


def _install_fake_http(monkeypatch: Any) -> list[dict[str, object]]:
    recorder: list[dict[str, object]] = []

    class FakeResponse:
        def __init__(self, data: dict[str, object]) -> None:
            self._data = data

        def json(self) -> dict[str, object]:
            return self._data

    class FakeClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "FakeClient":
            return self

        def __exit__(self, *exc: object) -> bool:
            return False

        def request(self, method: str, url: str, json: object = None,
                    headers: object = None) -> FakeResponse:
            recorder.append({"method": method, "url": url, "json": json, "headers": headers})
            return FakeResponse({"success": True, "enabled": False, "message": ""})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return recorder


def test_central_client_set_dnd_posts_dnd_and_message(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    central.set_dnd(True, "开会中，稍后回复")

    assert recorder, "set_dnd must issue a request"
    request = recorder[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://central.test/api/terminals/dnd"
    assert request["json"] == {"dnd": True, "message": "开会中，稍后回复"}


def test_central_client_get_dnd_reads_dnd(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    central.get_dnd()

    assert recorder, "get_dnd must issue a request"
    request = recorder[0]
    assert request["method"] == "GET"
    assert request["url"] == "https://central.test/api/terminals/dnd"


def test_central_client_set_quiet_window_posts_quiet(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    central.set_quiet_window(120, "silent")

    assert recorder, "set_quiet_window must issue a request"
    request = recorder[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://central.test/api/terminals/quiet"
    assert request["json"] == {"quiet_window_seconds": 120, "quiet_mode": "silent"}


def test_central_client_get_quiet_window_reads_quiet(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    central.get_quiet_window()

    assert recorder, "get_quiet_window must issue a request"
    request = recorder[0]
    assert request["method"] == "GET"
    assert request["url"] == "https://central.test/api/terminals/quiet"


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


class FakeEntry:
    def __init__(self) -> None:
        self._text = ""
        self.pack_calls: list[dict[str, object]] = []
        self.state = "normal"

    def get(self) -> str:
        return self._text

    def set_text(self, text: str) -> None:
        self._text = text

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
        self.labels: list[str] = []
        self.order: list[tuple[str, str]] = []
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
        self.order.append(("entry",))
        return entry

    def Label(self, *_args: object, **kwargs: object) -> FakeWidget:
        text = kwargs.get("text")
        if isinstance(text, str):
            self.labels.append(text)
            self.order.append(("label", text))
        elif kwargs.get("textvariable") is not None:
            self.order.append(("label", ""))
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
    def __init__(self, dnd: dict[str, object] | None = None) -> None:
        self._dnd = dnd if dnd is not None else {"enabled": False, "message": ""}
        self.set_dnd_calls: list[tuple[bool, str]] = []
        self.get_dnd_calls: int = 0

    def set_dnd(self, enabled: bool, message: str) -> None:
        self.set_dnd_calls.append((enabled, message))

    def get_dnd(self) -> dict[str, object]:
        self.get_dnd_calls += 1
        return dict(self._dnd)


def test_build_settings_panel_wires_dnd_to_central_with_message_entry() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    central = FakeCentral({"enabled": False, "message": ""})

    build_settings_panel(None, FakeRing(), central, "/sounds", ["a.mp3", "b.mp3"], "/sounds/a.mp3", tk)

    assert len(tk.entries) == 1
    assert central.get_dnd_calls == 1
    assert tk.checkbuttons[0]["variable"].get() is False
    assert tk.entries[0].get() == ""

    tk.entries[0].set_text("开会中，稍后回复")
    tk.checkbuttons[0]["variable"].set(True)
    assert central.set_dnd_calls == [(True, "开会中，稍后回复")]


def test_message_entry_has_auto_reply_label_before_it() -> None:
    build_settings_panel = _build()
    tk = FakeTk()
    build_settings_panel(None, FakeRing(), FakeCentral(), "/sounds", ["a.mp3"], "/sounds/a.mp3", tk)

    assert "自动回复消息" in tk.labels
    label_index = tk.order.index(("label", "自动回复消息"))
    entry_index = next(index for index, event in enumerate(tk.order) if event[0] == "entry")
    assert label_index < entry_index
