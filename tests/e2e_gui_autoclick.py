#!/usr/bin/env python3
"""Unattended, GUI-level MCP smoke and edge-case runner.

Run manually on a machine with a graphical desktop::

    .venv/bin/python tests/e2e_gui_autoclick.py

The runner starts the real server over stdio.  Its server child enables a
test-only Tk hook that finds rendered buttons and invokes them on Tk's main
thread.  Production startup never imports or enables this hook.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
import tkinter as tk
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
TEST_LOG_DIR = "/private/tmp/hitl-unattended-e2e-logs"
SERVER_LOG = Path("/private/tmp/hitl-unattended-e2e-server.log")


def _widget_tree(widget):
    """Yield a Tk widget and all of its descendants."""
    try:
        children = widget.winfo_children()
    except tk.TclError:
        return
    yield widget
    for child in children:
        yield from _widget_tree(child)


def run_vanished_widget_case() -> None:
    """Ensure a widget destroyed during traversal simply ends that branch."""
    root = tk.Tk()
    vanished = tk.Frame(root)
    vanished.destroy()
    try:
        assert list(_widget_tree(vanished)) == []
    finally:
        root.destroy()
    print(json.dumps({"case": "vanished_widget_ends_traversal", "status": "passed"}))


def _click_button(window, label: str) -> None:
    """Use Tk's button API rather than calling the dialog's private callbacks."""
    for widget in _widget_tree(window):
        try:
            if widget.winfo_class() == "Button" and widget.cget("text") == label:
                widget.focus_set()
                widget.event_generate("<ButtonPress-1>")
                widget.event_generate("<ButtonRelease-1>")
                widget.invoke()
                return
        except Exception:
            continue
    raise RuntimeError(f"Could not find rendered button {label!r}")


def _rendered_button_state(window, label: str) -> str:
    """Return a rendered button's state, failing if the button is absent."""
    for widget in _widget_tree(window):
        try:
            if widget.winfo_class() == "Button" and widget.cget("text") == label:
                return str(widget.cget("state"))
        except Exception:
            continue
    raise AssertionError(f"Could not find rendered button {label!r}")


def _rendered_list_values(window) -> list[str]:
    """Return all entries visibly rendered in list boxes below ``window``."""
    values = []
    for widget in _widget_tree(window):
        try:
            if widget.winfo_class() == "Listbox":
                values.extend(str(value) for value in widget.get(0, "end"))
        except Exception:
            continue
    return values


def _rendered_text_values(window) -> list[str]:
    """Return text visibly rendered in text areas below ``window``."""
    values = []
    for widget in _widget_tree(window):
        try:
            if widget.winfo_class() == "Text":
                values.append(str(widget.get("1.0", "end-1c")))
        except Exception:
            continue
    return values


def _choose_task_feedback_disposition(root, choice: str = "Completed") -> None:
    """Click the modal disposition choice spawned by a task feedback button."""
    for widget in _widget_tree(root):
        try:
            if widget.winfo_class() == "Toplevel" and widget.title() == "Report the task first?":
                _click_button(widget, choice)
                return
        except Exception:
            continue
    root.after(10, lambda: _choose_task_feedback_disposition(root, choice))


def _dismiss_task_feedback_disposition(root) -> None:
    """Close the modal without choosing, emulating its window-close affordance."""
    for widget in _widget_tree(root):
        try:
            if widget.winfo_class() == "Toplevel" and widget.title() == "Report the task first?":
                close_handler = widget.protocol("WM_DELETE_WINDOW")
                assert close_handler
                widget.tk.call(close_handler)
                return
        except Exception:
            continue
    root.after(10, lambda: _dismiss_task_feedback_disposition(root))


class TkAutoClicker:
    """A test-only driver for windows emitted by ``human_loop_server``.

    Every action is scheduled on the Tk main thread.  It deliberately acts on
    widgets (text entry, list selection, and Button.invoke) so this exercises
    the same GUI callbacks that a person uses.
    """

    def __init__(self, app: Any):
        self.app = app
        self.handled: set[tuple[str, int]] = set()
        self.task_attempts: dict[str, int] = {}

    def start(self) -> None:
        threading.Thread(target=self._wait_for_root, daemon=True).start()

    def _wait_for_root(self) -> None:
        # The stdio server offers no route to automate its real GUI, so this
        # test-only hook waits for the Tk root before scheduling widget actions.
        while self.app._dialog_runner.root is None:
            time.sleep(0.02)
        self.app._dialog_runner.root.after(20, self._tick)

    def _tick(self) -> None:
        for session in list(self.app._sessions.values()):
            dialog = session.dialog
            if dialog is None:
                continue
            key = (session.id, id(dialog))
            if key in self.handled:
                continue
            self.handled.add(key)
            self._act(session, dialog)
        root = self.app._dialog_runner.root
        if root is not None:
            root.after(40, self._tick)

    def _act(self, session: Any, dialog: Any) -> None:
        title = session.params.get("title") or session.task_title
        if isinstance(dialog, self.app.NotificationWindow):
            cancel_titles = {
                "AUTO_CANCEL", "AUTO_INFO_NOTIFICATION_CANCEL", "AUTO_CONFIRM_NOTIFICATION_CANCEL",
                "AUTO_TASK_NOTIFICATION_CANCEL",
            }
            if title == "AUTO_NOTIFICATION_AUTOCLOSE":
                return
            _click_button(dialog.win, "Cancel" if title in cancel_titles else "View")
            return

        if isinstance(dialog, self.app.InputDialog):
            if title == "AUTO_INPUT_CANCEL":
                _click_button(dialog._top, "Cancel")
                return
            if title == "AUTO_INPUT_DEFAULT":
                _click_button(dialog._top, "OK")
                return
            if title == "AUTO_TIMEOUT_ONE_SECOND":
                return
            dialog.entry.delete(0, "end")
            if session.params.get("input_type") == "integer":
                # Exercise the rendered validation state before providing a valid value.
                dialog.entry.insert(0, "not-an-integer")
                dialog._validate()
                assert str(dialog.ok_button.cget("state")) == "disabled"
                dialog.entry.delete(0, "end")
                dialog.entry.insert(0, "42")
            elif session.params.get("input_type") == "float":
                dialog.entry.insert(0, "3.5")
            else:
                dialog.entry.insert(0, "auto text")
            dialog._validate()
            if title == "AUTO_TIMEOUT_HEARTBEAT":
                dialog._top.after(1300, lambda: _click_button(dialog._top, "OK"))
                return
            if title == "AUTO_INPUT_FEEDBACK_MODAL_CANCEL":
                # The confirmation is cancelled, so the original interaction must
                # remain open and be cancellable in the normal way.
                dialog._top.after(30, lambda: _click_button(dialog._top, "Cancel"))
                _click_button(dialog._top, "Give feedback to AI")
                return
            if title in {"AUTO_INPUT_FEEDBACK", "AUTO_INPUT_FEEDBACK_DISCARD"}:
                _click_button(dialog._top, "Give feedback to AI")
                return
            _click_button(dialog._top, "OK")
            return

        if isinstance(dialog, self.app.ChoiceBody):
            if title == "AUTO_CHOICE_CANCEL":
                _click_button(dialog._top, "Cancel")
                return
            if title == "AUTO_CHOICE_EMPTY":
                _click_button(dialog._top, "OK")
                return
            dialog.listbox.selection_clear(0, "end")
            if session.params.get("allow_multiple"):
                dialog.listbox.selection_set(0)
                dialog.listbox.selection_set(2)
            else:
                dialog.listbox.selection_set(1)
            _click_button(dialog._top, "OK")
            return

        if isinstance(dialog, self.app.MultilineBody):
            if title == "AUTO_MULTILINE_CANCEL":
                _click_button(dialog._top, "Cancel")
                return
            if title == "AUTO_MULTILINE_DEFAULT":
                _click_button(dialog._top, "OK")
                return
            dialog.text_widget.insert("1.0", "first line\nsecond line")
            _click_button(dialog._top, "OK")
            return

        if isinstance(dialog, self.app.ConfirmBody):
            _click_button(dialog._top, "Yes" if title == "AUTO_CONFIRM_YES" else "No")
            return

        if isinstance(dialog, self.app.InfoBody):
            _click_button(dialog._top, "OK")
            return

        if isinstance(dialog, self.app.FeedbackBody):
            if session.client_timeout_seconds == 5:
                _click_button(dialog._top, "Cancel")
                return
            dialog.combo.set("The task the AI designed is too large - break it into smaller steps")
            dialog.text_widget.insert("1.0", "Automated feedback")
            _click_button(dialog._top, "Send")
            return

        if isinstance(dialog, self.app.HumanTaskDialog):
            attempt = self.task_attempts.get(session.id, 0)
            self.task_attempts[session.id] = attempt + 1
            if title == "AUTO_BARK_DORMANT":
                dialog.body_text.insert("1.0", f"Bark task update {attempt + 1}")
                if attempt < 2:
                    _click_button(dialog.dialog, "Still progressing")
                    if attempt == 1:
                        # Test-only time setup: the public third continuation must
                        # re-toast after this simulated 180-second idle interval.
                        dialog.dialog.after(20, lambda: setattr(
                            session, "last_human_action_at", time.monotonic() - 181))
                else:
                    _click_button(dialog.dialog, "Completed")
                return
            if title == "AUTO_TASK_AUTOCLOSE":
                if attempt == 0:
                    # The client intentionally stops heartbeating after this
                    # draft and attachment are present.
                    dialog.body_text.insert("1.0", "draft preserved across watchdog recovery")
                    self.app._e2e_attachment_path = str(ROOT / "notify.wav")
                    _click_button(dialog.dialog, "Attach files/images…")
                    assert any(value.startswith("notify.wav  (") for value in _rendered_list_values(dialog.dialog))
                    return
                assert "draft preserved across watchdog recovery" in _rendered_text_values(dialog.dialog)
                assert any(value.startswith("notify.wav  (") for value in _rendered_list_values(dialog.dialog))
                _click_button(dialog.dialog, "Completed")
                return
            task_feedback_choices = {
                "AUTO_TASK_FEEDBACK": "Completed",
                "AUTO_TASK_FEEDBACK_FAILED": "Failed",
                "AUTO_TASK_FEEDBACK_IN_PROGRESS": "Still progressing",
                "AUTO_TASK_FEEDBACK_CANCEL": "Cancel the task",
            }
            if title in task_feedback_choices and attempt == 0:
                dialog.body_text.insert("1.0", "task report before feedback")
                choice = task_feedback_choices[title]
                dialog.dialog.after(10, lambda: _choose_task_feedback_disposition(self.app._dialog_runner.root, choice))
                _click_button(dialog.dialog, "Give feedback to AI")
                return
            if title == "AUTO_TASK_FEEDBACK_MODAL_CANCEL":
                dialog.body_text.insert("1.0", "task report retained after modal cancel")
                dialog.dialog.after(10, lambda: _dismiss_task_feedback_disposition(self.app._dialog_runner.root))
                dialog.dialog.after(35, lambda: _click_button(dialog.dialog, "Completed"))
                _click_button(dialog.dialog, "Give feedback to AI")
                return
            if title == "AUTO_TASK_EMPTY_INTERIM":
                _click_button(dialog.dialog, "Still progressing")
                dialog.body_text.insert("1.0", "progress note after the empty-state warning")
                _click_button(dialog.dialog, "Still progressing")
                return
            if title == "AUTO_TASK_WINDOW_CLOSE":
                dialog.body_text.insert("1.0", "draft preserved on close")
                close_handler = dialog.dialog.protocol("WM_DELETE_WINDOW")
                assert close_handler
                dialog.dialog.tk.call(close_handler)
                return
            if title == "AUTO_TASK_ATTACHMENTS_DISABLED":
                assert not any(
                    widget.winfo_class() == "Button" and widget.cget("text") == "Attach files/images…"
                    for widget in _widget_tree(dialog.dialog)
                )
                dialog.body_text.insert("1.0", "attachments are disabled")
            elif title == "AUTO_TASK_SIZE_GUARD":
                dialog.body_text.insert("1.0", "x" * 200)
                dialog.body_text.event_generate("<KeyRelease>")
                assert all(_rendered_button_state(dialog.dialog, label) == "disabled" for label in ("Completed", "Failed", "Still progressing"))
                dialog.body_text.delete("1.0", "end")
                dialog.body_text.insert("1.0", "ok")
                dialog.body_text.event_generate("<KeyRelease>")
                assert all(_rendered_button_state(dialog.dialog, label) == "normal" for label in ("Completed", "Failed", "Still progressing"))
            elif title == "AUTO_TASK_ATTACHMENT_INLINE":
                self.app._e2e_attachment_path = str(ROOT / "pyproject.toml")
                _click_button(dialog.dialog, "Attach files/images…")
                assert any(value.startswith("pyproject.toml  (") for value in _rendered_list_values(dialog.dialog))
                assert _rendered_button_state(dialog.dialog, "Completed") == "normal"
                dialog.body_text.insert("1.0", "small file attached")
            elif title == "AUTO_TASK_ATTACHMENT_OVERSIZE":
                self.app._e2e_attachment_path = str(ROOT / "notify.wav")
                _click_button(dialog.dialog, "Attach files/images…")
                assert any(value.startswith("notify.wav  (") for value in _rendered_list_values(dialog.dialog))
                assert all(_rendered_button_state(dialog.dialog, label) == "normal" for label in ("Completed", "Failed", "Still progressing"))
                dialog.body_text.insert("1.0", "oversized attachment submitted as a path reference")
            else:
                dialog.body_text.insert("1.0", f"automated task report {attempt + 1}")
            if title == "AUTO_TASK_FAILED":
                _click_button(dialog.dialog, "Failed")
            elif title == "AUTO_TASK_IN_PROGRESS" and attempt == 0:
                _click_button(dialog.dialog, "Still progressing")
            else:
                _click_button(dialog.dialog, "Completed")
            return

        raise RuntimeError(f"Unhandled dialog type: {type(dialog).__name__}")


def run_server(*, attachments_enabled: bool = True, gui_start_delay_seconds: float = 0) -> None:
    """Entry point for the stdio server child, not for production use."""
    sys.path.insert(0, str(ROOT))
    import human_loop_server as app

    # This makes a full watchdog cycle take 5–6 seconds during the unattended
    # test.  The production module has no environment-controlled override.
    app.AUTO_CLOSE_AFTER_SECONDS = 2
    if not attachments_enabled:
        default_getter = app.human_loop_config.get_task_defaults

        def no_attachments_defaults():
            defaults = dict(default_getter())
            defaults["attachments_enabled"] = False
            return defaults

        app.human_loop_config.get_task_defaults = no_attachments_defaults
    app._e2e_attachment_path = str(ROOT / "pyproject.toml")
    app.filedialog.askopenfilenames = lambda **_kwargs: (app._e2e_attachment_path,)
    def feedback_disposition(*_args, **kwargs):
        parent = kwargs.get("parent")
        title = parent.title() if parent is not None else ""
        if title == "AUTO_INPUT_FEEDBACK_DISCARD":
            return False
        if title == "AUTO_INPUT_FEEDBACK_MODAL_CANCEL":
            return None
        return True

    app.messagebox.askyesnocancel = feedback_disposition
    app.messagebox.showwarning = lambda *_args, **_kwargs: None
    TkAutoClicker(app).start()
    if gui_start_delay_seconds:
        original_run = app._dialog_runner.run

        def delayed_run():
            # Test-only deterministic version of the brief period after the MCP
            # transport accepts calls but before Tk has created its shared root.
            time.sleep(gui_start_delay_seconds)
            original_run()

        app._dialog_runner.run = delayed_run
    app.main()


def _result_data(result: Any) -> Any:
    data = getattr(result, "data", None)
    if data is not None:
        return data
    return None


def _result_texts(result: Any) -> list[str]:
    """Return text blocks, unpacking FastMCP's list-to-JSON serialization."""
    texts = []
    for block in getattr(result, "content", []):
        value = getattr(block, "text", "")
        if not isinstance(value, str):
            continue
        texts.append(value)
        try:
            wrapped = json.loads(value)
            if isinstance(wrapped, list):
                texts.extend(item for item in wrapped if isinstance(item, str))
        except json.JSONDecodeError:
            pass
    return texts


def _task_summary(result: Any) -> dict[str, Any]:
    for block in getattr(result, "content", []):
        text = getattr(block, "text", "")
        candidates = [text]
        # FastMCP 3 may serialize a list-valued tool result into one TextContent
        # containing a JSON list of strings.  Older clients expose its first
        # string directly.  Accept both client-side representations.
        try:
            wrapped = json.loads(text)
            if isinstance(wrapped, list):
                candidates.extend(item for item in wrapped if isinstance(item, str))
        except (TypeError, json.JSONDecodeError):
            pass
        for candidate in candidates:
            if "\n\n{" in candidate:
                return json.loads(candidate.split("\n\n", 1)[1])
    raise AssertionError(f"No task summary in result: {result!r}")


async def _drive(client: Any, name: str, args: dict[str, Any]) -> tuple[Any, list[Any]]:
    """Follow server heartbeat/opened responses until a terminal result."""
    continuation_key = "task_id" if name == "assign_task_to_human" else "interaction_id"
    trace = []
    for _ in range(20):
        result = await client.call_tool(name, args, timeout=30)
        value = _result_data(result)
        trace.append(value if value is not None else _task_summary(result))
        if not isinstance(value, dict) or not value.get("needs_continuation"):
            return result, trace
        args[continuation_key] = value[continuation_key]
    raise AssertionError(f"{name} exceeded heartbeat limit: {trace!r}")


async def _until_status(client: Any, name: str, args: dict[str, Any], status: str) -> dict[str, Any]:
    """Follow only heartbeats, stopping as soon as the requested status arrives."""
    continuation_key = "task_id" if name == "assign_task_to_human" else "interaction_id"
    for _ in range(20):
        result = await client.call_tool(name, args, timeout=30)
        value = _result_data(result)
        if not isinstance(value, dict):
            raise AssertionError(f"Expected a keepalive response, got {result!r}")
        if value.get("status") == status:
            return value
        if not value.get("needs_continuation"):
            raise AssertionError(f"Expected {status!r}, got terminal {value!r}")
        args[continuation_key] = value[continuation_key]
    raise AssertionError(f"{name} did not return {status!r}")


async def _attachments_disabled_case() -> dict[str, Any]:
    """Run the disabled-attachments configuration in an isolated server process."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(ROOT / ".venv/bin/python"),
        args=[str(Path(__file__).resolve()), "--server-no-attachments"],
        cwd=str(ROOT),
        keep_alive=False,
        env={**os.environ, "HUMAN_LOOP_LOGS_DIR": TEST_LOG_DIR},
        log_file=SERVER_LOG,
    )
    async with Client(transport, timeout=30) as client:
        for _ in range(50):
            health = _result_data(await client.call_tool("health_check"))
            if health and health["gui_available"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("GUI never became ready with attachments disabled")
        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_ATTACHMENTS_DISABLED",
            "task_description": "Complete this task without attachments.",
            "client_timeout_seconds": 4,
        })
        task = _task_summary(result)
        assert task["status"] == "completed" and task["attachments"] == []
        return {"case": "task_attachments_disabled", "status": "passed", "trace": trace}


async def run_one_second_timeout_case() -> None:
    """Verify that a timeout below the safe minimum is rejected."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(ROOT / ".venv/bin/python"),
        args=[str(Path(__file__).resolve()), "--server"],
        cwd=str(ROOT),
        keep_alive=False,
        env={**os.environ, "HUMAN_LOOP_LOGS_DIR": TEST_LOG_DIR},
        log_file=SERVER_LOG,
    )
    async with Client(transport, timeout=30) as client:
        for _ in range(50):
            health = _result_data(await client.call_tool("health_check"))
            if health and health["gui_available"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("GUI never became ready")
        rejected = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_TIMEOUT_ONE_SECOND", "prompt": "do not answer", "client_timeout_seconds": 1,
        }))
        assert rejected["success"] is False
        assert rejected["status"] == "invalid_client_timeout"
        assert rejected["error_code"] == "client_timeout_seconds_too_low"
        assert rejected["minimum_client_timeout_seconds"] == 2
        assert rejected["needs_continuation"] is False
    print(json.dumps({
        "case": "one_second_timeout_rejected",
        "status": "passed",
    }, ensure_ascii=False))


async def run_gui_startup_race_case() -> None:
    """Require a bounded readiness wait before the retryable startup response."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(ROOT / ".venv/bin/python"),
        args=[str(Path(__file__).resolve()), "--server-delayed-gui"],
        cwd=str(ROOT),
        keep_alive=False,
        env={**os.environ, "HUMAN_LOOP_LOGS_DIR": TEST_LOG_DIR},
        log_file=SERVER_LOG,
    )
    async with Client(transport, timeout=30) as client:
        started = time.monotonic()
        result = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_GUI_STARTUP_RACE", "prompt": "call before Tk is ready", "client_timeout_seconds": 4,
        }))
        elapsed = time.monotonic() - started
        assert 0.5 <= elapsed < 3
        assert result["success"] is False
        assert result["status"] == "gui_starting"
        assert result["retryable"] is True
        assert result["needs_continuation"] is False
    print(json.dumps({
        "case": "interactive_call_before_gui_ready_is_retryable",
        "status": "passed", "elapsed": elapsed,
    }, ensure_ascii=False))


class _BarkRecorder(BaseHTTPRequestHandler):
    """Local Bark-shaped endpoint; records decoded requests without a phone key."""

    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - required by BaseHTTPRequestHandler
        type(self).requests.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"code":200}')

    def log_message(self, _format: str, *_args: object) -> None:
        return


async def run_bark_lifecycle_case() -> None:
    """Every interactive MCP tool sends one encoded, non-blocking Bark request."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    recorder = ThreadingHTTPServer(("127.0.0.1", 0), _BarkRecorder)
    recorder_thread = threading.Thread(target=recorder.serve_forever, daemon=True)
    recorder_thread.start()
    _BarkRecorder.requests.clear()
    bark_url = f"http://127.0.0.1:{recorder.server_port}/device%2Fkey"
    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.json"
            config_path.write_text(json.dumps({"notification": {
                "bark_enabled": True, "bark_url": bark_url, "muted": True,
            }}), encoding="utf-8")
            transport = StdioTransport(
                command=sys.executable, args=[str(Path(__file__).resolve()), "--server"],
                cwd=str(ROOT), keep_alive=False,
                env={**os.environ, "HUMAN_LOOP_CONFIG": str(config_path), "HUMAN_LOOP_LOGS_DIR": TEST_LOG_DIR},
                log_file=SERVER_LOG,
            )
            async with Client(transport, timeout=30) as client:
                failures: list[str] = []

                async def wait_for_count(expected: int) -> bool:
                    deadline = time.monotonic() + 3
                    while time.monotonic() < deadline:
                        if len(_BarkRecorder.requests) >= expected:
                            return len(_BarkRecorder.requests) == expected
                        await asyncio.sleep(0.02)
                    return len(_BarkRecorder.requests) == expected
                for _ in range(50):
                    health = _result_data(await client.call_tool("health_check"))
                    if health and health["gui_available"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("GUI never became ready")

                # Health/profile/invalid input must not create a phone request.
                await client.call_tool("get_operator_profile")
                invalid = _result_data(await client.call_tool("get_user_input", {
                    "title": "AUTO_TIMEOUT_ONE_SECOND", "prompt": "not a notification", "client_timeout_seconds": 1,
                }))
                assert invalid["success"] is False
                assert _BarkRecorder.requests == []

                async def drive_and_capture(name: str, arguments: dict[str, Any]) -> None:
                    before = len(_BarkRecorder.requests)
                    result, trace = await _drive(client, name, arguments)
                    # A Bark delivery alone is insufficient evidence: every
                    # interactive endpoint must still finish with its normal
                    # public MCP result after the real Tk action.
                    terminal = _result_data(result)
                    if name == "assign_task_to_human":
                        terminal = _task_summary(result)
                    if not trace or not isinstance(terminal, dict):
                        failures.append(f"{name}: missing terminal MCP result or trace")
                    elif name == "assign_task_to_human":
                        if (terminal.get("status") != "completed" or
                                terminal.get("body") != "automated task report 1" or
                                terminal.get("needs_continuation") is not False):
                            failures.append(f"{name}: unexpected task summary: {terminal!r}")
                    elif terminal.get("success") is not True:
                        failures.append(f"{name}: unsuccessful terminal result: {terminal!r}")
                    elif name == "get_user_input" and terminal.get("user_input") != "auto text":
                        failures.append(f"{name}: missing normal input result: {terminal!r}")
                    elif name == "get_user_choice" and terminal.get("selected_choices") != ["two"]:
                        failures.append(f"{name}: missing normal choice result: {terminal!r}")
                    elif name == "get_multiline_input" and terminal.get("user_input") != "first line\nsecond line":
                        failures.append(f"{name}: missing normal multiline result: {terminal!r}")
                    elif name == "show_confirmation_dialog" and terminal.get("confirmed") is not False:
                        failures.append(f"{name}: missing normal confirmation result: {terminal!r}")
                    elif name == "show_info_message" and terminal.get("acknowledged") is not True:
                        failures.append(f"{name}: missing normal acknowledgement result: {terminal!r}")
                    elif name == "get_feedback_from_user" and not terminal.get("feedback_type"):
                        failures.append(f"{name}: missing normal feedback result: {terminal!r}")
                    if not await wait_for_count(before + 1):
                        failures.append(f"{name}: expected one Bark request, got {len(_BarkRecorder.requests) - before}")

                await drive_and_capture("get_user_input", {
                    "title": "Bark 标题 & /", "prompt": "正文 & / ? 雪", "client_timeout_seconds": 4,
                })
                await drive_and_capture("get_user_choice", {
                    "title": "Bark choice", "prompt": "choose", "choices": ["one", "two"], "client_timeout_seconds": 4,
                })
                await drive_and_capture("get_multiline_input", {
                    "title": "Bark multiline", "prompt": "write", "client_timeout_seconds": 4,
                })
                await drive_and_capture("show_confirmation_dialog", {
                    "title": "Bark confirm", "message": "confirm", "client_timeout_seconds": 4,
                })
                await drive_and_capture("show_info_message", {
                    "title": "Bark info", "message": "info", "client_timeout_seconds": 4,
                })
                await drive_and_capture("get_feedback_from_user", {"client_timeout_seconds": 4})
                await drive_and_capture("assign_task_to_human", {
                    "task_title": "Bark task", "task_description": "complete", "client_timeout_seconds": 4,
                })

                # A heartbeat continues the existing toast/window and cannot push again.
                heartbeat_baseline = len(_BarkRecorder.requests)
                opened = _result_data(await client.call_tool("get_user_input", {
                    "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "wait", "client_timeout_seconds": 2,
                }))
                assert opened["status"] == "opened"
                started = time.monotonic()
                heartbeat = _result_data(await client.call_tool("get_user_input", {
                    "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "wait",
                    "interaction_id": opened["interaction_id"], "client_timeout_seconds": 2,
                }))
                assert heartbeat["status"] == "heartbeat"
                assert time.monotonic() - started < 2
                expected_after_open = heartbeat_baseline + 1
                if not await wait_for_count(expected_after_open):
                    failures.append("opened: new toast did not deliver exactly once")
                await asyncio.sleep(0.15)
                if len(_BarkRecorder.requests) != expected_after_open:
                    failures.append("heartbeat: continuation created a duplicate Bark request")

                # A fresh config read controls the next toast: disabled means no
                # delivery; a changed URL reaches its new key path without a restart.
                # Settle the preceding heartbeat's request first, so a delayed
                # prior request cannot be attributed to the disabled case.
                await asyncio.sleep(0.15)
                changed = json.loads(config_path.read_text(encoding="utf-8"))
                changed["notification"]["bark_enabled"] = False
                config_path.write_text(json.dumps(changed), encoding="utf-8")
                before_disabled = len(_BarkRecorder.requests)
                disabled_result, disabled_trace = await _drive(client, "show_info_message", {
                    "title": "Bark disabled", "message": "do not deliver", "client_timeout_seconds": 4,
                })
                disabled = _result_data(disabled_result)
                if (not disabled_trace or not isinstance(disabled, dict) or
                        disabled.get("success") is not True or disabled.get("acknowledged") is not True):
                    failures.append("disabled Bark: info dialog did not return its normal terminal result")
                await asyncio.sleep(0.2)
                if len(_BarkRecorder.requests) != before_disabled:
                    failures.append("disabled Bark: a later toast still delivered")
                changed["notification"].update({"bark_enabled": True, "bark_url": bark_url + "-changed"})
                config_path.write_text(json.dumps(changed), encoding="utf-8")
                before_changed = len(_BarkRecorder.requests)
                changed_result, changed_trace = await _drive(client, "show_info_message", {
                    "title": "Bark changed", "message": "deliver to new URL", "client_timeout_seconds": 4,
                })
                changed_terminal = _result_data(changed_result)
                if (not changed_trace or not isinstance(changed_terminal, dict) or
                        changed_terminal.get("success") is not True or changed_terminal.get("acknowledged") is not True):
                    failures.append("changed Bark URL: info dialog did not return its normal terminal result")
                if not await wait_for_count(before_changed + 1):
                    failures.append("changed Bark URL: next toast did not deliver exactly once")
                elif urlsplit(_BarkRecorder.requests[-1]).path != "/device%2Fkey-changed":
                    failures.append("changed Bark URL: request did not use the changed key path")

                await asyncio.sleep(0.15)

        if len(_BarkRecorder.requests) != 9:
            failures.append(f"total: expected nine requests, got {len(_BarkRecorder.requests)}")
        parsed = [urlsplit(request) for request in _BarkRecorder.requests]
        if [part.path for part in parsed[:-1]] != ["/device%2Fkey"] * 8:
            failures.append("key path was not preserved for initial URL")
        if not parsed or parsed[-1].path != "/device%2Fkey-changed":
            failures.append("changed URL key path was not used")
        decoded = [parse_qs(part.query) for part in parsed]
        if not all(values.get("title", [""])[0] and values.get("body", [""])[0] for values in decoded):
            failures.append("title/body were absent or empty")
        if not any(values.get("title", [""])[0] == "The assistant needs some input" and "正文 & / ? 雪" in values.get("body", [""])[0]
                   for values in decoded):
            failures.append("Unicode/reserved preview was not decoded correctly")
        assert not failures, "\n".join(failures)
    finally:
        recorder.shutdown()
        recorder.server_close()
    print(json.dumps({"case": "bark_all_interactive_tools", "status": "passed"}, ensure_ascii=False))


async def run_tests() -> None:
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    transport = StdioTransport(
        command=str(ROOT / ".venv/bin/python"),
        args=[str(Path(__file__).resolve()), "--server"],
        cwd=str(ROOT),
        keep_alive=False,
        env={**os.environ, "HUMAN_LOOP_LOGS_DIR": TEST_LOG_DIR},
        log_file=SERVER_LOG,
    )
    report: list[dict[str, Any]] = []
    async with Client(transport, timeout=30) as client:
        # Do not hide the known GUI-startup race: explicitly wait until health
        # reports ready, then begin user-equivalent interactions.
        for _ in range(50):
            health = _result_data(await client.call_tool("health_check"))
            if health and health["gui_available"]:
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("GUI never became ready")
        report.append({"case": "health", "status": "passed"})

        profile = _result_data(await client.call_tool("get_operator_profile"))
        assert profile["platform"] == "darwin"
        report.append({"case": "operator_profile", "status": "passed"})

        prompt_names = {prompt.name for prompt in await client.list_prompts()}
        assert "get_human_loop_prompt" in prompt_names
        prompt = await client.get_prompt("get_human_loop_prompt")
        assert getattr(prompt, "messages", None)
        report.append({"case": "human_loop_prompt", "status": "passed"})
        tool_names = {tool.name for tool in await client.list_tools()}
        assert set(health["prompts_available"]) == prompt_names
        assert prompt_names.isdisjoint(health["tools_available"])
        assert set(health["tools_available"]) == tool_names
        report.append({"case": "health_capabilities_match_discovery", "status": "passed"})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INPUT", "prompt": "automated text", "input_type": "text", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["user_input"] == "auto text"
        report.append({"case": "input", "status": "passed", "trace": trace})

        reused = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_INPUT", "prompt": "terminal id must not revive",
            "interaction_id": _result_data(result)["interaction_id"], "client_timeout_seconds": 4,
        }))
        assert reused["status"] == "session_not_found" and reused["needs_continuation"] is False
        report.append({"case": "simple_terminal_id_rejected", "status": "passed"})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INPUT_DEFAULT", "prompt": "keep the default", "default_value": "seed value",
            "input_type": "text", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["user_input"] == "seed value"
        report.append({"case": "input_default", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_FLOAT", "prompt": "automated float", "input_type": "float", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["user_input"] == 3.5
        report.append({"case": "float_input", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INPUT_CANCEL", "prompt": "cancel after opening", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["cancelled"] is True and _result_data(result)["user_input"] is None
        report.append({"case": "input_dialog_cancel", "status": "passed", "trace": trace})

        opened = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "respond after one minimum-duration heartbeat",
            "client_timeout_seconds": 2,
        }))
        assert opened["status"] == "opened"
        started = time.monotonic()
        heartbeat = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "respond after one minimum-duration heartbeat",
            "interaction_id": opened["interaction_id"], "client_timeout_seconds": 2,
        }))
        elapsed = time.monotonic() - started
        assert heartbeat["status"] == "heartbeat" and 0 < elapsed < 2
        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "respond after one minimum-duration heartbeat",
            "interaction_id": opened["interaction_id"],
            "client_timeout_seconds": 2,
        })
        assert _result_data(result)["user_input"] == "auto text"
        report.append({"case": "minimum_timeout_heartbeat", "status": "passed", "elapsed": elapsed, "trace": [opened, heartbeat, *trace]})

        rejected = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_TIMEOUT_ONE_SECOND", "prompt": "do not answer", "client_timeout_seconds": 1,
        }))
        assert rejected["status"] == "invalid_client_timeout"
        assert rejected["error_code"] == "client_timeout_seconds_too_low"
        assert rejected["minimum_client_timeout_seconds"] == 2
        assert rejected["needs_continuation"] is False
        report.append({"case": "one_second_timeout_rejected", "status": "passed"})

        first = _result_data(await client.call_tool("get_user_input", {
            "title": "AUTO_NOTIFICATION_AUTOCLOSE", "prompt": "leave notification untouched",
            "client_timeout_seconds": 4,
        }))
        assert first["status"] == "heartbeat" and first["needs_continuation"] is True
        await asyncio.sleep(3)
        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_NOTIFICATION_AUTOCLOSE", "prompt": "leave notification untouched",
            "interaction_id": first["interaction_id"], "client_timeout_seconds": 4,
        })
        assert _result_data(result)["cancelled"] is True
        report.append({"case": "notification_silent_autoclose", "status": "passed", "trace": [first, *trace]})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INPUT_FEEDBACK", "prompt": "submit then request feedback", "client_timeout_seconds": 4,
        })
        feedback_request = _result_data(result)
        assert feedback_request["user_input"] == "auto text" and feedback_request["feedback_requested"] is True
        report.append({"case": "input_requests_feedback", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        feedback = _result_data(result)
        assert feedback["feedback_type"] == "task_too_big" and feedback["feedback_text"] == "Automated feedback"
        report.append({"case": "requested_feedback_follow_up", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INPUT_FEEDBACK_DISCARD", "prompt": "discard answer then request feedback", "client_timeout_seconds": 4,
        })
        discarded = _result_data(result)
        assert discarded["cancelled"] is True and discarded["user_input"] is None
        assert discarded["feedback_requested"] is True
        report.append({"case": "input_feedback_discards_answer", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        feedback = _result_data(result)
        assert feedback["feedback_type"] == "task_too_big" and feedback["feedback_text"] == "Automated feedback"
        report.append({"case": "discarded_input_feedback_follow_up", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INPUT_FEEDBACK_MODAL_CANCEL", "prompt": "cancel feedback confirmation", "client_timeout_seconds": 4,
        })
        feedback_cancelled = _result_data(result)
        assert feedback_cancelled["cancelled"] is True and "feedback_requested" not in feedback_cancelled
        report.append({"case": "input_feedback_confirmation_cancel_keeps_dialog", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 5})
        feedback = _result_data(result)
        assert feedback["cancelled"] is True and feedback["feedback_type"] is None
        report.append({"case": "feedback_dialog_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_INTEGER", "prompt": "automated integer", "input_type": "integer", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["user_input"] == 42
        report.append({"case": "integer_validation", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_choice", {
            "title": "AUTO_CHOICE", "prompt": "automated multi-choice", "choices": ["Alpha", "Beta", "Gamma"],
            "allow_multiple": True, "client_timeout_seconds": 4,
        })
        assert _result_data(result)["selected_choices"] == ["Alpha", "Gamma"]
        report.append({"case": "multiple_choice", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_choice", {
            "title": "AUTO_CHOICE_SINGLE", "prompt": "automated single choice",
            "choices": ["Alpha", "Beta", "Gamma"], "allow_multiple": False, "client_timeout_seconds": 4,
        })
        assert _result_data(result)["selected_choice"] == "Beta"
        report.append({"case": "single_choice", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_choice", {
            "title": "AUTO_CHOICE_EMPTY", "prompt": "no available choices", "choices": [], "client_timeout_seconds": 4,
        })
        assert _result_data(result)["cancelled"] is True and _result_data(result)["selected_choices"] == []
        report.append({"case": "empty_choice_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_choice", {
            "title": "AUTO_CHOICE_CANCEL", "prompt": "cancel after opening", "choices": ["only option"],
            "client_timeout_seconds": 4,
        })
        assert _result_data(result)["cancelled"] is True
        report.append({"case": "choice_dialog_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_multiline_input", {
            "title": "AUTO_MULTILINE", "prompt": "automated multiline", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["user_input"] == "first line\nsecond line"
        report.append({"case": "multiline", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_multiline_input", {
            "title": "AUTO_MULTILINE_DEFAULT", "prompt": "keep the default", "default_value": "seed\nvalue",
            "client_timeout_seconds": 4,
        })
        assert _result_data(result)["user_input"] == "seed\nvalue"
        report.append({"case": "multiline_default", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_multiline_input", {
            "title": "AUTO_MULTILINE_CANCEL", "prompt": "cancel after opening", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["cancelled"] is True and _result_data(result)["user_input"] is None
        report.append({"case": "multiline_dialog_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "show_confirmation_dialog", {
            "title": "AUTO_CONFIRM", "message": "automated no", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["confirmed"] is False
        report.append({"case": "confirmation_no", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "show_confirmation_dialog", {
            "title": "AUTO_CONFIRM_YES", "message": "automated yes", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["confirmed"] is True and _result_data(result)["response"] == "yes"
        report.append({"case": "confirmation_yes", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "show_info_message", {
            "title": "AUTO_INFO", "message": "automated acknowledgement", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["acknowledged"] is True
        report.append({"case": "info", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "show_info_message", {
            "title": "AUTO_INFO_NOTIFICATION_CANCEL", "message": "dismiss notification", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["success"] is True and _result_data(result)["acknowledged"] is False
        report.append({"case": "info_notification_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "show_confirmation_dialog", {
            "title": "AUTO_CONFIRM_NOTIFICATION_CANCEL", "message": "dismiss notification", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["confirmed"] is False and _result_data(result)["cancelled"] is True
        report.append({"case": "confirmation_notification_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        feedback = _result_data(result)
        assert feedback["feedback_type"] == "task_too_big"
        assert feedback["feedback_text"] == "Automated feedback"
        report.append({"case": "feedback", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_user_input", {
            "title": "AUTO_CANCEL", "prompt": "automated cancellation", "client_timeout_seconds": 4,
        })
        assert _result_data(result)["cancelled"] is True
        report.append({"case": "notification_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_NOTIFICATION_CANCEL", "task_description": "Decline from the notification.",
            "client_timeout_seconds": 4,
        })
        declined = _result_data(result)
        assert declined["status"] == "failed" and declined["reason"] == "declined_via_notification"
        report.append({"case": "task_notification_decline", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_EMPTY_INTERIM", "task_description": "Try an empty interim update first.",
            "client_timeout_seconds": 4,
        })
        interim_guard = _task_summary(result)
        assert interim_guard["status"] == "in_progress"
        assert interim_guard["body"] == "progress note after the empty-state warning"
        report.append({"case": "task_empty_interim_guard", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_WINDOW_CLOSE", "task_description": "Close the task window without reporting completion.",
            "client_timeout_seconds": 4,
        })
        closed = _task_summary(result)
        assert closed["status"] == "cancelled" and closed["body"] == "draft preserved on close"
        report.append({"case": "task_window_close_cancel", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_IN_PROGRESS",
            "task_description": "Provide an interim report, then finish the task.",
            "client_timeout_seconds": 4,
        })
        interim = _task_summary(result)
        assert interim["status"] == "in_progress"
        result, completion_trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_IN_PROGRESS",
            "task_description": "Provide an interim report, then finish the task.",
            "task_id": interim["task_id"],
            "client_timeout_seconds": 4,
        })
        task = _task_summary(result)
        assert task["status"] == "completed"
        assert any(item.get("status") == "in_progress" for item in trace)
        report.append({"case": "task_in_progress_resume", "status": "passed", "trace": trace + completion_trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_RESUBMIT",
            "task_description": "Submit once, then submit a corrected replacement.",
            "client_timeout_seconds": 4,
        })
        initial = _task_summary(result)
        assert initial["status"] == "completed" and initial["resubmittable"] is True
        result, replacement_trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_RESUBMIT",
            "task_description": "Submit once, then submit a corrected replacement.",
            "task_id": initial["task_id"],
            "client_timeout_seconds": 4,
        })
        replacement = _task_summary(result)
        assert replacement["status"] == "completed"
        assert replacement["body"] == "automated task report 2"
        report.append({"case": "task_resubmit", "status": "passed", "trace": trace + replacement_trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FAILED", "task_description": "Report an automated failure.",
            "client_timeout_seconds": 4,
        })
        failed = _task_summary(result)
        assert failed["status"] == "failed" and failed["resubmittable"] is True
        report.append({"case": "task_failed_resubmittable", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FEEDBACK", "task_description": "Submit a report, then request feedback.",
            "client_timeout_seconds": 4,
        })
        feedback_task = _task_summary(result)
        assert feedback_task["status"] == "completed" and feedback_task["body"] == "task report before feedback"
        assert any("The human clicked 'Give feedback to AI'" in text for text in _result_texts(result))
        report.append({"case": "task_requests_feedback", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        feedback = _result_data(result)
        assert feedback["feedback_type"] == "task_too_big"
        report.append({"case": "task_requested_feedback_follow_up", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FEEDBACK_FAILED", "task_description": "Report failed, then give feedback.",
            "client_timeout_seconds": 4,
        })
        feedback_failed = _task_summary(result)
        assert feedback_failed["status"] == "failed" and feedback_failed["body"] == "task report before feedback"
        assert any("The human clicked 'Give feedback to AI'" in text for text in _result_texts(result))
        result, feedback_trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        assert _result_data(result)["feedback_type"] == "task_too_big"
        report.append({"case": "task_feedback_failed_disposition", "status": "passed", "trace": trace + feedback_trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FEEDBACK_CANCEL", "task_description": "Cancel the task, then give feedback.",
            "client_timeout_seconds": 4,
        })
        feedback_cancelled_task = _task_summary(result)
        assert feedback_cancelled_task["status"] == "cancelled" and feedback_cancelled_task["needs_continuation"] is False
        assert any("The human clicked 'Give feedback to AI'" in text for text in _result_texts(result))
        result, feedback_trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        assert _result_data(result)["feedback_type"] == "task_too_big"
        report.append({"case": "task_feedback_cancel_disposition", "status": "passed", "trace": trace + feedback_trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FEEDBACK_IN_PROGRESS", "task_description": "Report progress, then give feedback.",
            "client_timeout_seconds": 4,
        })
        feedback_interim = _task_summary(result)
        assert feedback_interim["status"] == "in_progress" and feedback_interim["needs_continuation"] is True
        assert any("The human clicked 'Give feedback to AI'" in text for text in _result_texts(result))
        result, feedback_trace = await _drive(client, "get_feedback_from_user", {"client_timeout_seconds": 4})
        assert _result_data(result)["feedback_type"] == "task_too_big"
        result, completion_trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FEEDBACK_IN_PROGRESS", "task_description": "Finish after the feedback step.",
            "task_id": feedback_interim["task_id"], "client_timeout_seconds": 4,
        })
        assert _task_summary(result)["status"] == "completed"
        report.append({"case": "task_feedback_interim_disposition", "status": "passed", "trace": trace + feedback_trace + completion_trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_FEEDBACK_MODAL_CANCEL", "task_description": "Back out of feedback status selection.",
            "client_timeout_seconds": 4,
        })
        feedback_modal_cancel = _task_summary(result)
        assert feedback_modal_cancel["status"] == "completed"
        assert feedback_modal_cancel["body"] == "task report retained after modal cancel"
        assert not any("The human clicked 'Give feedback to AI'" in text for text in _result_texts(result))
        report.append({"case": "task_feedback_modal_cancel_keeps_window", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_ADVISORY",
            "task_description": "1. Inspect the room.\n2. Photograph the room.\nWhat did you find?",
            "client_timeout_seconds": 4,
        })
        advisory_task = _task_summary(result)
        assert advisory_task["advisory"]
        assert any(item.get("advisory") for item in trace)
        report.append({"case": "task_composite_advisory", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_SIZE_GUARD", "task_description": "Exercise the size guard before submitting.",
            "max_result_bytes": 4200, "client_timeout_seconds": 4,
        })
        sized = _task_summary(result)
        assert sized["status"] == "completed" and sized["body"] == "ok"
        report.append({"case": "task_size_guard_recovery", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_ATTACHMENT_INLINE", "task_description": "Attach a small report.",
            "client_timeout_seconds": 4,
        })
        attached = _task_summary(result)
        assert len(attached["attachments"]) == 1
        manifest = attached["attachments"][0]
        assert manifest["name"] == "pyproject.toml"
        assert manifest["size_bytes"] == (ROOT / "pyproject.toml").stat().st_size
        assert manifest["inlined"] is True
        report.append({"case": "task_small_attachment_inline", "status": "passed", "trace": trace})

        result, trace = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_ATTACHMENT_OVERSIZE",
            "task_description": "Attach a file too large for the configured result budget.",
            "client_timeout_seconds": 4,
        })
        oversized = _task_summary(result)
        assert oversized["status"] == "completed"
        assert oversized["attachments_omitted_for_size"] is True
        assert len(oversized["attachments"]) == 1
        manifest = oversized["attachments"][0]
        assert manifest["name"] == "notify.wav"
        assert manifest["size_bytes"] == (ROOT / "notify.wav").stat().st_size
        assert manifest["inlined"] is False
        assert manifest["path_reference"] == str(ROOT / "notify.wav")
        report.append({"case": "task_oversized_attachment_path_reference", "status": "passed", "trace": trace})

        missing = _result_data(await client.call_tool("assign_task_to_human", {
            "task_title": "AUTO_UNKNOWN", "task_description": "This must not create a window.",
            "task_id": "not-a-live-session", "client_timeout_seconds": 4,
        }))
        assert missing["status"] == "session_not_found" and missing["needs_continuation"] is False
        report.append({"case": "task_unknown_id_rejected", "status": "passed"})

        opened = await _until_status(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_AUTOCLOSE",
            "task_description": "Leave open while the agent disconnects.",
            "client_timeout_seconds": 4,
        }, "opened")
        await asyncio.sleep(7)  # 3-second leg + 2-second test-only watchdog + margin
        result, _ = await _drive(client, "assign_task_to_human", {
            "task_title": "AUTO_TASK_AUTOCLOSE",
            "task_description": "Leave open while the agent disconnects.",
            "task_id": opened["task_id"],
            "client_timeout_seconds": 4,
        })
        task = _task_summary(result)
        assert task["status"] == "completed"
        assert task["task_id"] == opened["task_id"]
        assert task["body"] == "draft preserved across watchdog recovery"
        assert [attachment["name"] for attachment in task["attachments"]] == ["notify.wav"]
        report.append({"case": "task_watchdog_resumes_draft_and_attachment", "status": "passed"})

        entries = [json.loads(path.read_text(encoding="utf-8")) for path in Path(TEST_LOG_DIR).glob("*/entry.json")]
        assert any(entry["tool"] == "get_user_input" and entry["status"] == "answered" for entry in entries)
        assert any(entry["task_title"] == "AUTO_TASK_ATTACHMENT_INLINE" and entry["attachments"] for entry in entries)
        report.append({"case": "log_archive_records", "status": "passed", "entry_count": len(entries)})

    report.append(await _attachments_disabled_case())

    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if sys.argv[1:] == ["--server"]:
        run_server()
    elif sys.argv[1:] == ["--server-no-attachments"]:
        run_server(attachments_enabled=False)
    elif sys.argv[1:] == ["--server-delayed-gui"]:
        run_server(gui_start_delay_seconds=3)
    elif sys.argv[1:] == ["--quick-one-second-timeout"]:
        asyncio.run(run_one_second_timeout_case())
    elif sys.argv[1:] == ["--quick-gui-startup-race"]:
        asyncio.run(run_gui_startup_race_case())
    elif sys.argv[1:] == ["--quick-vanished-widget"]:
        run_vanished_widget_case()
    elif sys.argv[1:] == ["--quick-bark-lifecycle"]:
        asyncio.run(run_bark_lifecycle_case())
    else:
        asyncio.run(run_tests())
