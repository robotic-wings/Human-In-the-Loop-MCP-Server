"""Red contract for the desktop terminal's explicit human cancellation.

The central service already exposes ``POST /api/calls/<id>/cancel`` (body
``{"operation_id": ...}``) which drives ``terminal_cancel`` to the durable
``cancelled`` / ``human_cancelled`` outcome.  The desktop terminal still lacks
the terminal-side half: ``CentralHttpDesktopClient`` has no ``cancel`` request
and the accepted-call answer dialog has no cancel button, so a person who has
already answered cannot end that round except by disconnecting.  These tests
stay red until that terminal-side path exists.
"""

from __future__ import annotations

import sys
from typing import Any

import httpx

from central_desktop_terminal import CentralHttpDesktopClient, DesktopTerminalConfig, TkPresentation


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
            return FakeResponse({"success": True, "status": "cancelled", "call_state": "cancelled",
                                 "reason": "human_cancelled"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return recorder


def test_cancel_posts_operation_id_to_call_cancel_endpoint_with_terminal_session(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    central._session_id = "desktop-session-1"
    recorder = _install_fake_http(monkeypatch)

    result = central.cancel(call_id="call-1", operation_id="cancel-op-1")

    assert recorder, "cancel must issue a request"
    request = recorder[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://central.test/api/calls/call-1/cancel"
    assert request["json"] == {"operation_id": "cancel-op-1"}
    assert request["headers"]["x-terminal-session"] == "desktop-session-1"
    assert result["status"] == "cancelled"


class FakeWidget:
    def pack(self, *args: object, **kwargs: object) -> "FakeWidget":
        return self

    def destroy(self) -> None:
        pass

    def title(self, *_args: object) -> None:
        pass

    def protocol(self, *_args: object) -> None:
        pass

    def bell(self) -> None:
        pass

    def get(self, *_args: object, **_kwargs: object) -> str:
        return ""


class FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class FakeTk:
    def __init__(self) -> None:
        self.buttons: list[dict[str, object]] = []

    def Toplevel(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Label(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Button(self, _dialog: object, **kwargs: object) -> FakeWidget:
        self.buttons.append(dict(kwargs))
        return FakeWidget()

    def StringVar(self, value: str = "") -> FakeStringVar:
        return FakeStringVar(value)

    def Entry(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Listbox(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Text(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def OptionMenu(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Checkbutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Radiobutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def BooleanVar(self, value: bool = False) -> FakeStringVar:
        return FakeStringVar("true" if value else "false")


class FakeCentral:
    def __init__(self) -> None:
        self.cancel_calls: list[tuple[str, str]] = []

    def submit(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}

    def upload_attachment(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True, "attachment_id": "att-1"}

    def cancel(self, call_id: str, operation_id: str) -> dict[str, object]:
        self.cancel_calls.append((call_id, operation_id))
        return {"success": True, "status": "cancelled", "call_state": "cancelled"}


class FakeTerminal:
    def __init__(self) -> None:
        self._central = FakeCentral()


def test_answer_dialog_exposes_cancel_button_that_ends_the_round(monkeypatch: Any) -> None:
    fake_tk = FakeTk()
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    terminal = FakeTerminal()
    presentation = TkPresentation(root=FakeWidget(), client_ref=lambda: terminal)

    presentation._answer("call-1", {"payload": {"tool": "get_text_input", "prompt": "x"}})

    cancel_button = next(
        (button for button in fake_tk.buttons if str(button.get("text", "")) == "Cancel"),
        None,
    )
    assert cancel_button is not None, "answer dialog must expose a cancel button"

    command = cancel_button.get("command")
    assert callable(command), "cancel button must be wired to a callback"
    command()

    assert terminal._central.cancel_calls, "cancel button must end the accepted round centrally"
    assert terminal._central.cancel_calls[0][0] == "call-1"
    assert terminal._central.cancel_calls[0][1]
