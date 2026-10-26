"""Red contracts for central-service one-shot Bark delivery persistence.

These tests deliberately exercise a local Bark-shaped receiver through the
real central HTTPS listener.  They do not prescribe a worker implementation.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from test_central_mcp_gateway import (
    _free_port,
    _post,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
)
from test_mcp_wait_leg import _with_timeout


class _BarkReceiver(BaseHTTPRequestHandler):
    """A local receiver which can hold a delivery open across SQLite checks."""

    deliveries: list[dict[str, str]] = []
    started = threading.Event()
    release = threading.Event()
    response_code = 200
    response_body = b'{"code":200,"message":"accepted"}'

    def do_GET(self) -> None:  # noqa: N802
        self._record()

    def do_POST(self) -> None:  # noqa: N802
        self._record()

    def _record(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        type(self).deliveries.append({"path": self.path, "body": body})
        type(self).started.set()
        type(self).release.wait(timeout=5)
        encoded = type(self).response_body
        self.send_response(type(self).response_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        try:
            self.wfile.write(encoded)
        except BrokenPipeError:
            pass

    def log_message(self, _format: str, *args: object) -> None:
        return


def _configure_phone_bark(config_path: Path, bark_url: str) -> None:
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    raw["bark_enabled"] = True
    raw["bark_url"] = bark_url
    config_path.write_text(json.dumps(raw), encoding="utf-8")


def _wait_for_delivery_state(database: Path, expected: set[str]) -> tuple[str, int]:
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        with sqlite3.connect(database) as connection:
            row = connection.execute(
                "SELECT state, attempt_count FROM bark_deliveries ORDER BY rowid"
            ).fetchone()
        if row is not None and row[0] in expected:
            return str(row[0]), int(row[1])
        time.sleep(0.03)
    raise AssertionError(f"Bark delivery did not reach one of {expected!r}")


def _start_receiver() -> tuple[ThreadingHTTPServer, threading.Thread, str]:
    _BarkReceiver.deliveries = []
    _BarkReceiver.started = threading.Event()
    _BarkReceiver.release = threading.Event()
    _BarkReceiver.response_code = 200
    _BarkReceiver.response_body = b'{"code":200,"message":"accepted"}'
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), _BarkReceiver)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    return receiver, thread, f"http://127.0.0.1:{receiver.server_port}/device-key"


def _stop_receiver(receiver: ThreadingHTTPServer, thread: threading.Thread) -> None:
    _BarkReceiver.release.set()
    receiver.shutdown()
    receiver.server_close()
    thread.join(timeout=3)


def test_tls_phone_call_persists_one_bark_intent_then_success_without_sensitive_payload(
    tmp_path: Path,
) -> None:
    receiver, receiver_thread, bark_url = _start_receiver()
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    _configure_phone_bark(config, bark_url)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        request = _with_timeout("bark-once", 2)
        request["params"]["arguments"].update(
            {"title": "Bark title", "prompt": "private prompt must stay off Bark"}
        )
        _, response, _ = _post(port, certificate, request, "test-token-alpha")
        heartbeat = _structured(response)
        assert _BarkReceiver.started.wait(timeout=2)

        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            rows = connection.execute(
                "SELECT call_id, state, attempt_count FROM bark_deliveries"
            ).fetchall()
        assert rows == [(heartbeat["call_id"], "sending", 1)]

        _BarkReceiver.release.set()
        state, attempts = _wait_for_delivery_state(
            tmp_path / "data" / "calls.sqlite3", {"success"}
        )
    finally:
        _stop(process)
        _stop_receiver(receiver, receiver_thread)

    assert state == "success"
    assert attempts == 1
    assert len(_BarkReceiver.deliveries) == 1
    delivered = _BarkReceiver.deliveries[0]
    payload = unquote(urlsplit(delivered["path"]).path + "?" + urlsplit(delivered["path"]).query)
    payload += delivered["body"]
    assert "Bark title" in payload
    assert f"https://127.0.0.1:{port}/calls/{heartbeat['call_id']}" in payload
    assert "private prompt must stay off Bark" not in payload
    assert "test-token-alpha" not in payload


def test_timeout_or_uncertain_bark_attempt_is_persisted_and_not_resent_after_restart(
    tmp_path: Path,
) -> None:
    receiver, receiver_thread, bark_url = _start_receiver()
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    _configure_phone_bark(config, bark_url)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, response, _ = _post(
            port, certificate, _with_timeout("bark-uncertain", 2), "test-token-alpha"
        )
        assert _structured(response)["status"] == "heartbeat"
        assert _BarkReceiver.started.wait(timeout=2)
        state, attempts = _wait_for_delivery_state(
            tmp_path / "data" / "calls.sqlite3", {"timeout", "uncertain"}
        )
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        time.sleep(0.35)
    finally:
        _stop(process)
        _stop_receiver(receiver, receiver_thread)

    assert state in {"timeout", "uncertain"}
    assert attempts == 1
    assert len(_BarkReceiver.deliveries) == 1


def test_bark_business_failure_is_not_recorded_as_success(tmp_path: Path) -> None:
    receiver, receiver_thread, bark_url = _start_receiver()
    _BarkReceiver.response_body = b'{"code":400,"message":"rejected"}'
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    _configure_phone_bark(config, bark_url)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, response, _ = _post(
            port, certificate, _with_timeout("bark-business-failed", 2), "test-token-alpha"
        )
        heartbeat = _structured(response)
        assert heartbeat["status"] == "heartbeat"
        assert _BarkReceiver.started.wait(timeout=2)
        _BarkReceiver.release.set()
        state, attempts = _wait_for_delivery_state(
            tmp_path / "data" / "calls.sqlite3", {"failed"}
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            call_state = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (heartbeat["call_id"],)
            ).fetchone()[0]
            terminal_events = connection.execute(
                "SELECT COUNT(*) FROM events WHERE call_id = ?", (heartbeat["call_id"],)
            ).fetchone()[0]
    finally:
        _stop(process)
        _stop_receiver(receiver, receiver_thread)

    assert state == "failed"
    assert attempts == 1
    assert call_state == "ringing"
    assert terminal_events == 0
