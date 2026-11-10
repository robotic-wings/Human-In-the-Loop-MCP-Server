"""Red contract for the desktop terminal's file attachment selection.

The "Attach files" flow is broken: after selecting files, the filenames never
appear in the attachment list, the central ``lifecycle_attachments`` table stays
empty, and ``choose_files`` silently swallows upload failures.  These tests pin
the two required behaviours through ``TkPresentation._answer``'s ``choose_files``
closure:

* a successful upload appends the filename to the list box and collects the
  ``attachment_id``;
* a failed upload reports an error instead of being silently dropped.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from central_desktop_terminal import TkPresentation


class FakeToplevel:
    def title(self, *_args: object) -> None:
        pass

    def destroy(self) -> None:
        pass


class FakeWidget:
    def pack(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self
    def grid(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        return None



class FakeStringVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class FakeEntry:
    def pack(self, *_args: object, **_kwargs: object) -> "FakeEntry":
        return self
    def grid(self, *_args: object, **_kwargs: object) -> "FakeEntry":
        return self


    def get(self) -> str:
        return ""


class FakeListbox:
    def __init__(self) -> None:
        self.inserted: list[str] = []

    def insert(self, _index: object, item: str) -> None:
        self.inserted.append(str(item))

    def pack(self, *_args: object, **_kwargs: object) -> "FakeListbox":
        return self


class FakeFileDialog:
    def __init__(self, files: list[str]) -> None:
        self.files = list(files)

    def askopenfilenames(self, *_args: object, **_kwargs: object) -> list[str]:
        return self.files


class FakeTk:
    def __init__(self, files: list[str]) -> None:
        self.filedialog = FakeFileDialog(files)
        self.buttons: list[dict[str, object]] = []
        self.listboxes: list[FakeListbox] = []
        self.string_vars: list[FakeStringVar] = []
    def LabelFrame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()

    def Frame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()


    def Toplevel(self, *_args: object, **_kwargs: object) -> FakeToplevel:
        return FakeToplevel()

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
        box = FakeListbox()
        self.listboxes.append(box)
        return box

    def Entry(self, *_args: object, **_kwargs: object) -> FakeEntry:
        return FakeEntry()

    def Text(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def OptionMenu(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Checkbutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Radiobutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def BooleanVar(self, *_args: object, **_kwargs: object) -> FakeStringVar:
        return FakeStringVar("")


class FakeCentral:
    def __init__(self, upload_result: dict[str, object]) -> None:
        self._upload_result = dict(upload_result)
        self.upload_calls: list[tuple[str, str, bytes]] = []

    def upload_attachment(self, call_id: str, filename: str, media_type: str, content: bytes) -> dict[str, object]:
        self.upload_calls.append((call_id, filename, content))
        return dict(self._upload_result)

    def submit(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}

    def cancel(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": True}


class FakeTerminal:
    def __init__(self, central: FakeCentral) -> None:
        self._central = central


def _presentation(central: FakeCentral) -> TkPresentation:
    terminal = FakeTerminal(central)
    return TkPresentation(root=object(), client_ref=lambda: terminal)


def _attach_command(monkeypatch: Any, tk: FakeTk, central: FakeCentral) -> Any:
    monkeypatch.setitem(sys.modules, "tkinter", tk)
    presentation = _presentation(central)
    presentation._answer("call-1", {"payload": {"tool": "get_text_input", "prompt": "x"}})
    attach = next(button for button in tk.buttons if button.get("text") == "添加附件")
    return attach["command"]


def test_choose_files_success_adds_filename_to_list(tmp_path: Path, monkeypatch: Any) -> None:
    note = tmp_path / "note.txt"
    note.write_bytes(b"file content")
    central = FakeCentral({"success": True, "attachment_id": "att-1"})
    tk = FakeTk([str(note)])

    choose_files = _attach_command(monkeypatch, tk, central)
    choose_files()

    assert central.upload_calls == [("call-1", "note.txt", b"file content")]
    assert tk.listboxes[0].inserted == [str(note)]


def test_choose_files_failure_reports_error_not_silent(tmp_path: Path, monkeypatch: Any) -> None:
    note = tmp_path / "note.txt"
    note.write_bytes(b"file content")
    central = FakeCentral({"success": False, "status": "invalid_request"})
    tk = FakeTk([str(note)])

    choose_files = _attach_command(monkeypatch, tk, central)
    choose_files()

    assert tk.listboxes[0].inserted == []
    assert tk.string_vars[0].value != ""
