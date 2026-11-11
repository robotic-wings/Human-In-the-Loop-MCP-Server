"""Red contract for desktop-terminal task progress reporting.

The central submission route already maps ``result.status == "in_progress"`` to
``in_progress`` with ``needs_continuation`` (the round stays owned, see
``test_central_mcp_task_status_submission_contract.py``).  The desktop terminal
still lacks the human half: ``TkPresentation``'s task branch offers only
``completed``/``failed``, its answer dialog has no "report progress" action, and
its submit callback always destroys the dialog after a successful submission.
These tests stay red until the task dialog exposes a "report progress" action
that submits ``{status: "in_progress", progress: ...}`` without ending the task,
and ``CentralHttpDesktopClient.submit`` forwards that progress result to the
central submissions endpoint verbatim.
"""

from __future__ import annotations

import sys
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
                "success": True, "status": "in_progress", "call_state": "in_progress",
                "needs_continuation": True, "progress": "halfway done",
            })

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return recorder


def test_submit_forwards_progress_status_and_preserves_continuation(monkeypatch: Any) -> None:
    central = CentralHttpDesktopClient(_config())
    recorder = _install_fake_http(monkeypatch)

    response = central.submit(
        call_id="call-1", submission_id="sub-1",
        result={"status": "in_progress", "progress": "halfway done"},
    )

    assert recorder, "submit must issue a request"
    request = recorder[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://central.test/api/calls/call-1/submissions"
    assert request["json"]["submission_id"] == "sub-1"
    assert request["json"]["result"]["status"] == "in_progress"
    assert request["json"]["result"]["progress"] == "halfway done"
    assert response["call_state"] == "in_progress"
    assert response["needs_continuation"] is True


class FakeWidget:
    def __init__(self) -> None:
        self.destroyed = False

    def pack(self, *args: object, **kwargs: object) -> "FakeWidget":
        return self
    def grid(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        return None


    def destroy(self) -> None:
        self.destroyed = True

    def title(self, *_args: object) -> None:
        pass

    def protocol(self, *_args: object) -> None:
        pass

    def bell(self) -> None:
        pass

    def get(self, *_args: object, **_kwargs: object) -> str:
        return ""

    def insert(self, *_args: object, **_kwargs: object) -> None:
        pass


class FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self._value = value

    def get(self) -> str:
        return self._value

    def set(self, value: str) -> None:
        self._value = value


class FakeText(FakeWidget):
    def __init__(self) -> None:
        super().__init__()
        self._text = ""

    def get(self, start: object, end: object = None) -> str:
        del start, end
        return self._text


class FakeTk:
    def __init__(self) -> None:
        self.buttons: list[dict[str, object]] = []
        self.option_menus: list[dict[str, object]] = []
        self.toplevels: list[FakeWidget] = []
    def LabelFrame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()

    def Frame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()


    def Toplevel(self, *_args: object, **_kwargs: object) -> FakeWidget:
        widget = FakeWidget()
        self.toplevels.append(widget)
        return widget

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

    def Text(self, *_args: object, **_kwargs: object) -> FakeText:
        return FakeText()

    def OptionMenu(self, _dialog: object, variable: FakeStringVar, *choices: object) -> FakeWidget:
        self.option_menus.append({"variable": variable, "choices": list(choices)})
        return FakeWidget()

    def Checkbutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Radiobutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def BooleanVar(self, value: bool = False) -> FakeStringVar:
        return FakeStringVar("true" if value else "false")


class FakeCentral:
    def __init__(self) -> None:
        self.submissions: list[dict[str, object]] = []

    def submit(self, call_id: str, submission_id: str, result: object,
               attachment_ids: object = None) -> dict[str, object]:
        self.submissions.append({
            "call_id": call_id, "submission_id": submission_id,
            "result": dict(result) if isinstance(result, dict) else {},
            "attachment_ids": list(attachment_ids) if attachment_ids else [],
        })
        return {"success": True, "status": "in_progress", "call_state": "in_progress", "needs_continuation": True}

    def upload_attachment(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True, "attachment_id": "att-1"}


class FakeTerminal:
    def __init__(self) -> None:
        self._central = FakeCentral()


def _progress_buttons(fake_tk: FakeTk) -> list[dict[str, object]]:
    return [
        button for button in fake_tk.buttons
        if "progress" in str(button.get("text", "")).lower() or "进度" in str(button.get("text", ""))
    ]


def _open_task_dialog(monkeypatch: Any) -> tuple[FakeTk, FakeTerminal]:
    fake_tk = FakeTk()
    monkeypatch.setitem(sys.modules, "tkinter", fake_tk)
    terminal = FakeTerminal()
    presentation = TkPresentation(root=FakeWidget(), client_ref=lambda: terminal)
    presentation._answer("call-1", {"payload": {"tool": "task", "task_description": "Deploy the service"}})
    return fake_tk, terminal


def test_task_dialog_exposes_report_progress_action_that_submits_in_progress(monkeypatch: Any) -> None:
    fake_tk, terminal = _open_task_dialog(monkeypatch)
    progress_buttons = _progress_buttons(fake_tk)
    assert progress_buttons, "task dialog must expose a report-progress action"

    command = progress_buttons[0].get("command")
    assert callable(command)
    command()

    assert terminal._central.submissions, "reporting progress must submit to the central service"
    submitted = terminal._central.submissions[-1]
    assert submitted["result"]["status"] == "in_progress"
    assert "progress" in submitted["result"]


def test_report_progress_does_not_close_task_dialog(monkeypatch: Any) -> None:
    fake_tk, _terminal = _open_task_dialog(monkeypatch)
    progress_buttons = _progress_buttons(fake_tk)
    assert progress_buttons, "task dialog must expose a report-progress action"

    dialog = fake_tk.toplevels[-1]
    command = progress_buttons[0].get("command")
    assert callable(command)
    command()

    assert not dialog.destroyed, "reporting progress must not end the task or close the dialog"
