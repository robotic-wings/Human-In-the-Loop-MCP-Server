#!/usr/bin/env python3
"""Runtime Red contracts for Bark task re-notification.

This is intentionally an unattended GUI/MCP test, rather than a mock of the
server internals.  Run it on a desktop with ``.venv/bin/python
tests/bark_runtime_cases.py``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

# Reuse the established real-Tk driver and its public MCP result helpers.  The
# child process replaces its driver class before calling run_server().
sys.path.insert(0, str(ROOT / "tests"))
import e2e_gui_autoclick as e2e  # noqa: E402


class _Recorder(BaseHTTPRequestHandler):
    """A local Bark-shaped endpoint; no device key or external network is used."""

    requests: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
        type(self).requests.append(self.path)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b'{"code":200}')

    def log_message(self, _format: str, *_args: object) -> None:
        return


class BarkRuntimeDriver(e2e.TkAutoClicker):
    """Drive the three public task legs and retain rendered-widget evidence."""

    @staticmethod
    def _record_widget(kind: str, session_id: str) -> None:
        """Cross the stdio child boundary with test-only rendered evidence."""
        event_path = os.environ.get("BARK_RUNTIME_EVENTS")
        if event_path:
            with open(event_path, "a", encoding="utf-8") as stream:
                stream.write(json.dumps({"kind": kind, "session_id": session_id}) + "\n")

    def _act(self, session: Any, dialog: Any) -> None:
        title = session.params.get("title") or session.task_title
        if title != "AUTO_BARK_DORMANT":
            return super()._act(session, dialog)

        if isinstance(dialog, self.app.NotificationWindow):
            # This observes the real rendered toast and drives its public View
            # button, exactly as the regular unattended driver does.
            assert e2e._rendered_button_state(dialog.win, "View") == "normal"
            assert e2e._rendered_button_state(dialog.win, "Cancel") == "normal"
            self._record_widget("toast", session.id)
            e2e._click_button(dialog.win, "View")
            return

        if isinstance(dialog, self.app.HumanTaskDialog):
            attempt = self.task_attempts.get(session.id, 0)
            self.task_attempts[session.id] = attempt + 1
            self._record_widget("task_window", session.id)
            assert e2e._rendered_button_state(dialog.dialog, "Still progressing") == "normal"
            dialog.body_text.insert("1.0", f"Bark runtime update {attempt + 1}")
            if attempt < 2:
                e2e._click_button(dialog.dialog, "Still progressing")
                if attempt == 1:
                    # Test setup only: the next same-ID call represents a
                    # continuation after more than the public 180-second idle
                    # threshold.  The runtime remains responsible for deciding
                    # to render the next toast.
                    dialog.dialog.after(20, lambda: setattr(
                        session, "last_human_action_at", time.monotonic() - 181))
            else:
                e2e._click_button(dialog.dialog, "Completed")
            return
        raise AssertionError(f"unexpected Bark runtime widget {type(dialog).__name__}")


def run_server() -> None:
    """Install the dedicated driver before starting the ordinary test server."""
    e2e.TkAutoClicker = BarkRuntimeDriver
    e2e.run_server()


def _transport(config_path: Path, logs_dir: Path, events_path: Path | None = None):
    from fastmcp.client.transports import StdioTransport

    env = {**os.environ, "HUMAN_LOOP_CONFIG": str(config_path),
           "HUMAN_LOOP_LOGS_DIR": str(logs_dir)}
    if events_path is not None:
        env["BARK_RUNTIME_EVENTS"] = str(events_path)
    return StdioTransport(
        command=sys.executable,
        args=[str(Path(__file__).resolve()), "--server"],
        cwd=str(ROOT),
        keep_alive=False,
        env=env,
        log_file=logs_dir / "server.log",
    )


async def _ready(client: Any) -> None:
    for _ in range(60):
        health = e2e._result_data(await client.call_tool("health_check"))
        if health and health.get("gui_available"):
            return
        await asyncio.sleep(0.05)
    raise AssertionError("GUI never became ready")


async def _task_leg(client: Any, args: dict[str, Any]) -> dict[str, Any]:
    """Return a task result, following only the public `opened` handshake."""
    for _ in range(8):
        result = await client.call_tool("assign_task_to_human", args, timeout=30)
        value = e2e._result_data(result)
        if value is None:
            value = e2e._task_summary(result)
        assert isinstance(value, dict), value
        if value.get("status") != "opened":
            return value
        args["task_id"] = value["task_id"]
    raise AssertionError("toast View never reached a task result")


async def _wait_for_requests(expected: int) -> bool:
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        if len(_Recorder.requests) == expected:
            return True
        await asyncio.sleep(0.02)
    return len(_Recorder.requests) == expected


async def _dormant_task_case(bark_url: str, logs_dir: Path) -> None:
    """Immediate reopen stays quiet; an idle reopen renders and pushes once."""
    from fastmcp import Client

    with tempfile.TemporaryDirectory() as temp:
        config = Path(temp) / "config.json"
        config.write_text(json.dumps({"notification": {
            "bark_enabled": True, "bark_url": bark_url, "muted": True,
        }}), encoding="utf-8")
        events = Path(temp) / "rendered-events.jsonl"
        events.touch()
        failures: list[str] = []
        async with Client(_transport(config, logs_dir, events), timeout=30) as client:
            await _ready(client)
            args = {"task_title": "AUTO_BARK_DORMANT", "task_description": "Report one update.",
                    "client_timeout_seconds": 4}
            first = await _task_leg(client, args)
            assert first["status"] == "in_progress" and first["needs_continuation"] is True
            task_id = first["task_id"]
            if not await _wait_for_requests(1):
                failures.append(f"first task toast: expected one request, got {_Recorder.requests!r}")

            # Capture before reopening: sampling after the call would accept a
            # duplicate immediate-continuation request as the new baseline.
            first_delivery_count = len(_Recorder.requests)
            second = await _task_leg(client, {**args, "task_id": task_id})
            assert second["status"] == "in_progress" and second["task_id"] == task_id
            await asyncio.sleep(0.12)
            if len(_Recorder.requests) != first_delivery_count:
                failures.append("immediate same-ID reopen: made a duplicate request")

            # The driver schedules the artificial idle timestamp after the
            # submission callback has recorded the ordinary human action.
            await asyncio.sleep(0.08)
            third = await _task_leg(client, {**args, "task_id": task_id})
            assert third["status"] == "completed" and third["task_id"] == task_id
            if not await _wait_for_requests(first_delivery_count + 1):
                failures.append(f"idle same-ID reopen: expected one new request, got {_Recorder.requests!r}")
        observed = [json.loads(line) for line in events.read_text(encoding="utf-8").splitlines()]
        toasts = [event for event in observed if event["kind"] == "toast"]
        task_windows = [event for event in observed if event["kind"] == "task_window"]
        if len(toasts) != 2:
            failures.append(f"expected two rendered toasts, saw {toasts!r}")
        if len(task_windows) != 3:
            failures.append(f"expected three rendered task windows, saw {task_windows!r}")
        if any(event["session_id"] != task_id for event in observed):
            failures.append(f"rendered widgets did not retain the same task id: {observed!r}")
        assert not failures, "\n".join(failures)


async def _invalid_notification_case(notification: dict[str, Any], logs_dir: Path) -> None:
    """Bad persisted Bark values neither crash a notification nor make a request."""
    from fastmcp import Client

    with tempfile.TemporaryDirectory() as temp:
        config = Path(temp) / "config.json"
        config.write_text(json.dumps({"notification": notification}), encoding="utf-8")
        before = len(_Recorder.requests)
        async with Client(_transport(config, logs_dir), timeout=30) as client:
            await _ready(client)
            result, _trace = await e2e._drive(client, "show_info_message", {
                "title": "AUTO_INVALID_BARK", "message": "The local endpoint must remain unused.",
                "client_timeout_seconds": 4,
            })
            value = e2e._result_data(result)
            assert value and value.get("success") is True, value
            # Keep the transport alive while observing the bounded no-push
            # interval; server teardown must not hide a late async delivery.
            await asyncio.sleep(0.2)
            assert len(_Recorder.requests) == before, _Recorder.requests


async def run_cases() -> None:
    recorder = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=recorder.serve_forever, daemon=True)
    thread.start()
    _Recorder.requests.clear()
    bark_url = f"http://127.0.0.1:{recorder.server_port}/local-test-key"
    failures: list[str] = []
    scenarios = [
        ("dormant_task", lambda logs: _dormant_task_case(bark_url, logs)),
        ("invalid_enabled_string", lambda logs: _invalid_notification_case(
            {"bark_enabled": "yes", "bark_url": bark_url, "muted": True}, logs)),
        ("invalid_url_number", lambda logs: _invalid_notification_case(
            {"bark_enabled": True, "bark_url": 17, "muted": True}, logs)),
        ("invalid_url_object", lambda logs: _invalid_notification_case(
            {"bark_enabled": True, "bark_url": {"url": bark_url}, "muted": True}, logs)),
    ]
    try:
        for name, scenario in scenarios:
            with tempfile.TemporaryDirectory(prefix="bark-runtime-logs-") as temp:
                try:
                    await scenario(Path(temp))
                    print(json.dumps({"case": name, "status": "passed"}))
                except Exception as exc:
                    failures.append(f"{name}: {exc!r}")
                    print(json.dumps({"case": name, "status": "failed", "error": repr(exc)}))
    finally:
        recorder.shutdown()
        recorder.server_close()
    assert not failures, "\n".join(failures)


if __name__ == "__main__":
    if sys.argv[1:] == ["--server"]:
        run_server()
    else:
        asyncio.run(run_cases())
