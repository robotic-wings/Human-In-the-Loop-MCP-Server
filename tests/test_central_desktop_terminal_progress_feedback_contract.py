"""Red contract for the desktop terminal's task progress feedback.

Clicking "Report progress" currently gives no visible feedback on success: the
window is unchanged, and a failure only sets a subtle error label.  These tests
pin the required feedback through ``TkPresentation._answer``'s ``report_progress``
closure:

* a successful progress report shows a clear visible status (「进度已上报」);
* a failed report shows an error;
* reporting progress keeps the task in ``in_progress`` (the dialog is not
  destroyed).
"""

from __future__ import annotations

import sys
from typing import Any

from central_desktop_terminal import TkPresentation


class FakeToplevel:
    def __init__(self) -> None:
        self.destroyed = False

    def title(self, *_args: object) -> None:
        pass

    def destroy(self) -> None:
        self.destroyed = True


class FakeWidget:
    def pack(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self


class FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeText:
    def __init__(self) -> None:
        self.text = ""

    def get(self, *_args: object) -> str:
        return self.text

    def pack(self, *_args: object, **_kwargs: object) -> "FakeText":
        return self


class FakeEntry:
    def pack(self, *_args: object, **_kwargs: object) -> "FakeEntry":
        return self

    def get(self) -> str:
        return ""


class FakeListbox:
    def insert(self, *_args: object) -> None:
        pass

    def pack(self, *_args: object, **_kwargs: object) -> "FakeListbox":
        return self


class FakeFileDialog:
    def askopenfilenames(self, *_args: object, **_kwargs: object) -> list[str]:
        return []


class FakeTk:
    def __init__(self) -> None:
        self.filedialog = FakeFileDialog()
        self.buttons: list[dict[str, object]] = []
        self.string_vars: list[FakeStringVar] = []
        self.toplevels: list[FakeToplevel] = []

    def Toplevel(self, *_args: object, **_kwargs: object) -> FakeToplevel:
        toplevel = FakeToplevel()
        self.toplevels.append(toplevel)
        return toplevel

    def Label(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Button(self, _root: object, **kwargs: object) -> FakeWidget:
        self.buttons.append(dict(kwargs))
        return FakeWidget()

    def StringVar(self, value: str = "") -> FakeStringVar:
        var = FakeStringVar(value)
        self.string_vars.append(var)
        return var

    def Listbox(self, *_args: object, **_kwargs: object) -> FakeListbox:
        return FakeListbox()

    def Entry(self, *_args: object, **_kwargs: object) -> FakeEntry:
        return FakeEntry()

    def Text(self, *_args: object, **_kwargs: object) -> FakeText:
        return FakeText()

    def OptionMenu(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Checkbutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Radiobutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def BooleanVar(self, *_args: object, **_kwargs: object) -> FakeStringVar:
        return FakeStringVar("")


class FakeCentral:
    def __init__(self, submit_result: dict[str, object]) -> None:
        self._submit_result = dict(submit_result)
        self.submit_calls: list[tuple[str, dict[str, object]]] = []

    def submit(self, call_id: str, _submission_id: str, result: dict[str, object],
               _attachment_ids: list[str] | None = None) -> dict[str, object]:
        self.submit_calls.append((call_id, result))
        return dict(self._submit_result)

    def upload_attachment(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True, "attachment_id": "att-1"}

    def cancel(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}


class FakeTerminal:
    def __init__(self, central: FakeCentral) -> None:
        self._central = central


def _report_progress_command(monkeypatch: Any, central: FakeCentral) -> tuple[FakeTk, Any]:
    tk = FakeTk()
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    terminal = FakeTerminal(central)
    presentation = TkPresentation(root=object(), client_ref=lambda: terminal)
    presentation._answer("call-1", {"payload": {"tool": "assign_task_to_human", "task_description": "deploy"}})
    button = next(b for b in tk.buttons if b.get("text") == "Report progress")
    return tk, button["command"]


def test_report_progress_success_shows_visible_feedback(monkeypatch: Any) -> None:
    central = FakeCentral({"success": True})
    tk, report_progress = _report_progress_command(monkeypatch, central)

    report_progress()

    assert central.submit_calls, "report progress must submit"
    assert central.submit_calls[0][1]["status"] == "in_progress"
    assert any(var.value == "进度已上报" for var in tk.string_vars), "successful progress must show visible feedback"


def test_report_progress_failure_shows_error(monkeypatch: Any) -> None:
    central = FakeCentral({"success": False})
    tk, report_progress = _report_progress_command(monkeypatch, central)

    report_progress()

    assert tk.string_vars[0].value != "", "failed progress must show an error"


def test_report_progress_does_not_close_the_task_window(monkeypatch: Any) -> None:
    central = FakeCentral({"success": True})
    tk, report_progress = _report_progress_command(monkeypatch, central)

    report_progress()

    assert tk.toplevels[0].destroyed is False, "reporting progress must not close the task window"
