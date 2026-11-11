"""Red contract for the desktop terminal's attachment upload and submission.

The central service already exposes ``POST /api/calls/<id>/attachments`` and
resolves ``result.attachments`` on submission.  The desktop terminal still
lacks the client-side half: ``CentralHttpDesktopClient`` has no
``upload_attachment`` request, ``submit`` does not merge ``attachment_ids``
into the result, and the answer dialog has no file-selection control or
attachment list.  These tests stay red until that terminal-side path exists.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path
from typing import Any

import httpx

from central_desktop_terminal import CentralHttpDesktopClient, DesktopTerminalConfig, TkPresentation


def _config() -> DesktopTerminalConfig:
    return DesktopTerminalConfig(
        central_url="https://central.test",
        callsign="desktop-61",

        operator_password="desktop-secret",

        device_id="desktop-61",

        terminal_platform="windows",
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
            return FakeResponse({
                "success": True, "attachment_id": "att-123", "filename": "photo.png",
                "media_type": "image/png", "byte_count": 8, "checksum": "abc123",
            })

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return recorder


CONTENT = b"\x00\x01\x02\x03hello-attachment"


def test_upload_attachment_posts_filename_media_type_and_base64_content(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    result = central.upload_attachment(
        call_id="call-1", filename="photo.png", media_type="image/png", content=CONTENT,
    )

    assert recorder, "upload_attachment must issue a request"
    request = recorder[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://central.test/api/calls/call-1/attachments"
    assert request["json"]["filename"] == "photo.png"
    assert request["json"]["media_type"] == "image/png"
    assert request["json"]["content"] == base64.b64encode(CONTENT).decode("ascii")
    assert result["attachment_id"] == "att-123"


def test_submit_merges_attachment_ids_into_result_attachments(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    central.submit(
        call_id="call-1", submission_id="sub-1",
        result={"user_input": "yes"}, attachment_ids=["att-1", "att-2"],
    )

    assert recorder, "submit must issue a request"
    request = recorder[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://central.test/api/calls/call-1/submissions"
    assert request["json"]["submission_id"] == "sub-1"
    assert request["json"]["result"]["user_input"] == "yes"
    assert request["json"]["result"]["attachments"] == ["att-1", "att-2"]


class FakeWidget:
    def pack(self, *args: object, **kwargs: object) -> "FakeWidget":
        return self
    def grid(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        return None


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


class FakeFileDialog:
    def __init__(self) -> None:
        self.selected = ("photo.png",)

    def askopenfilenames(self, *_args: object, **_kwargs: object) -> tuple[str, ...]:
        return self.selected


class FakeTk:
    def __init__(self) -> None:
        self.buttons: list[dict[str, object]] = []
        self.listboxes: list[FakeWidget] = []
        self.filedialog = FakeFileDialog()
    def LabelFrame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()

    def Frame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()


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
        widget = FakeWidget()
        self.listboxes.append(widget)
        return widget

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
    def submit(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}

    def upload_attachment(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True, "attachment_id": "att-1"}


class FakeTerminal:
    def __init__(self) -> None:
        self._central = FakeCentral()


def test_answer_dialog_exposes_file_selection_and_attachment_list(monkeypatch: Any) -> None:
    fake_tk = FakeTk()
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    terminal = FakeTerminal()
    presentation = TkPresentation(root=FakeWidget(), client_ref=lambda: terminal)

    presentation._answer("call-1", {"payload": {"tool": "get_text_input", "prompt": "x"}})

    assert fake_tk.listboxes, "answer dialog must render an attachment list"
    assert any(str(button.get("text", "")) != "提交" for button in fake_tk.buttons), (
        "answer dialog must expose a file-selection button"
    )
