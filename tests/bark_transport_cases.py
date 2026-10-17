#!/usr/bin/env python3
"""Transport-level Red cases for asynchronous Bark delivery.

Run on a desktop with the existing stdio/Tk driver available::

    .venv/bin/python tests/bark_transport_cases.py
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
from urllib.parse import parse_qs, urlsplit

from e2e_gui_autoclick import ROOT, _drive, _result_data


class _TransportEndpoint(BaseHTTPRequestHandler):
    """A local endpoint which either rejects or deliberately never finishes."""

    attempts: list[tuple[str, float]] = []
    client_closed_at: float | None = None

    def do_GET(self) -> None:  # noqa: N802 - required callback name
        cls = type(self)
        cls.attempts.append((self.path, time.monotonic()))
        if self.path.startswith("/synthetic-credential-key-500"):
            self.send_response(500)
            self.end_headers()
            self.wfile.write(b"failed")
            return

        if self.path.startswith("/synthetic-credential-key-trickle"):
            if len(cls.attempts) > 1:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":200}')
                return
            self.send_response(200)
            self.send_header("Content-Length", "13")
            self.end_headers()
            # Every gap is below the ordinary socket inactivity timeout, but
            # the complete response exceeds the total Bark delivery budget.
            for _ in range(13):
                try:
                    self.wfile.write(b"x")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    cls.client_closed_at = time.monotonic()
                    return
                time.sleep(0.25)
            # A normal HTTP reader may consume the declared body and close
            # without provoking a write error; observe that peer close too.
            self.connection.settimeout(0.05)
            deadline = time.monotonic() + 1
            while time.monotonic() < deadline:
                try:
                    if self.connection.recv(1) == b"":
                        cls.client_closed_at = time.monotonic()
                        return
                except TimeoutError:
                    continue
            return

        if self.path.startswith("/synthetic-credential-key-header-trickle"):
            if len(cls.attempts) > 1:
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"code":200}')
                return
            # Each header byte arrives well inside a normal socket inactivity
            # timeout, but receiving a complete HTTP response takes >3 seconds.
            response = b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: 2\r\n\r\n{}"
            for byte in response:
                try:
                    self.connection.sendall(bytes((byte,)))
                except (BrokenPipeError, ConnectionResetError):
                    cls.client_closed_at = time.monotonic()
                    return
                time.sleep(0.05)
            return

        # Deliberately do not send a response. A bounded client must abandon
        # this accepted TCP connection rather than consuming a worker forever.
        deadline = time.monotonic() + 5
        self.connection.settimeout(0.1)
        while time.monotonic() < deadline:
            try:
                if self.connection.recv(1) == b"":
                    cls.client_closed_at = time.monotonic()
                    return
            except TimeoutError:
                continue
            except (BrokenPipeError, ConnectionResetError):
                cls.client_closed_at = time.monotonic()
                return

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _Endpoint:
    def __init__(self) -> None:
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _TransportEndpoint)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self) -> _Endpoint:
        _TransportEndpoint.attempts.clear()
        _TransportEndpoint.client_closed_at = None
        self.thread.start()
        return self

    def url(self, suffix: str) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/{suffix}"

    def __exit__(self, *_args: object) -> None:
        self.server.shutdown()
        self.server.server_close()


async def _wait_for_attempts(expected: int, timeout: float = 1) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if len(_TransportEndpoint.attempts) >= expected:
            return len(_TransportEndpoint.attempts) == expected
        await asyncio.sleep(0.02)
    return len(_TransportEndpoint.attempts) == expected


async def _with_client(bark_url: str, check: Any) -> str:
    """Start the real stdio server and expose its actual MCP client to a case."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    with tempfile.TemporaryDirectory() as temp_dir:
        config_path = Path(temp_dir) / "config.json"
        logs_dir = Path(temp_dir) / "logs"
        server_log = Path(temp_dir) / "server.log"
        config_path.write_text(json.dumps({"notification": {
            "bark_enabled": True, "bark_url": bark_url, "muted": True,
        }}), encoding="utf-8")
        transport = StdioTransport(
            command=sys.executable,
            args=[str(ROOT / "tests" / "e2e_gui_autoclick.py"), "--server"],
            cwd=str(ROOT),
            keep_alive=False,
            env={**os.environ, "HUMAN_LOOP_CONFIG": str(config_path), "HUMAN_LOOP_LOGS_DIR": str(logs_dir)},
            log_file=server_log,
        )
        async with Client(transport, timeout=30) as client:
            for _ in range(50):
                health = _result_data(await client.call_tool("health_check"))
                if health and health["gui_available"]:
                    break
                await asyncio.sleep(0.05)
            else:
                raise AssertionError("GUI never became ready")
            await check(client)
        diagnostics = server_log.read_text(encoding="utf-8", errors="replace") if server_log.exists() else ""
        persisted = "".join(path.read_text(encoding="utf-8", errors="replace") for path in logs_dir.rglob("*") if path.is_file())
        return diagnostics + persisted


async def run_transport_cases() -> None:
    """Exercise HTTP 500 and held-open delivery without hiding missing attempts."""
    failures: list[str] = []
    synthetic_key = "synthetic-credential-key"

    with _Endpoint() as endpoint:
        async def rejected_delivery(client: Any) -> None:
            result, _ = await _drive(client, "get_user_input", {
                "title": "Bark 500", "prompt": "normal GUI/MCP result", "client_timeout_seconds": 4,
            })
            value = _result_data(result)
            if not value or value.get("success") is not True or value.get("user_input") != "auto text":
                failures.append(f"HTTP 500 changed the GUI/MCP result: {value!r}")
            if synthetic_key in json.dumps(value, ensure_ascii=False):
                failures.append("synthetic credential key leaked to MCP result after HTTP 500")
            if not await _wait_for_attempts(1):
                failures.append("HTTP 500: initial delivery attempt was not observed")
            # Keep the real MCP process alive past the delivery timeout, so a
            # delayed retry at one or two seconds cannot be hidden by teardown.
            await asyncio.sleep(3.2)

        diagnostics = await _with_client(endpoint.url(f"{synthetic_key}-500"), rejected_delivery)
        if synthetic_key in diagnostics:
            failures.append("synthetic credential key leaked to server diagnostics after HTTP 500")
        if len(_TransportEndpoint.attempts) != 1:
            failures.append(f"HTTP 500: expected one attempt/no retry, got {len(_TransportEndpoint.attempts)}")

    with _Endpoint() as endpoint:
        started: float | None = None
        attempt_started: float | None = None
        closed_at: float | None = None

        async def held_delivery(client: Any) -> None:
            nonlocal attempt_started, closed_at, started
            opened = _result_data(await client.call_tool("get_user_input", {
                "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "slow Bark must not block", "client_timeout_seconds": 2,
            }))
            if not opened or opened.get("status") != "opened":
                failures.append(f"slow HTTP changed initial GUI/MCP response: {opened!r}")
                return
            if synthetic_key in json.dumps(opened, ensure_ascii=False):
                failures.append("synthetic credential key leaked to opened MCP result")
            started = time.monotonic()
            heartbeat = _result_data(await client.call_tool("get_user_input", {
                "title": "AUTO_TIMEOUT_HEARTBEAT", "prompt": "slow Bark must not block",
                "interaction_id": opened["interaction_id"], "client_timeout_seconds": 2,
            }))
            elapsed = time.monotonic() - started
            if not heartbeat or heartbeat.get("status") != "heartbeat" or elapsed >= 2:
                failures.append(f"slow HTTP delayed heartbeat ({elapsed:.3f}s): {heartbeat!r}")
            if heartbeat and synthetic_key in json.dumps(heartbeat, ensure_ascii=False):
                failures.append("synthetic credential key leaked to heartbeat MCP result")
            if not await _wait_for_attempts(1):
                failures.append("held-open HTTP: initial delivery attempt was not observed")
            deadline = time.monotonic() + 3.2
            while _TransportEndpoint.client_closed_at is None and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.4)
            if _TransportEndpoint.attempts:
                attempt_started = _TransportEndpoint.attempts[0][1]
            closed_at = _TransportEndpoint.client_closed_at

        diagnostics = await _with_client(endpoint.url(f"{synthetic_key}-held-open"), held_delivery)
        if synthetic_key in diagnostics:
            failures.append("synthetic credential key leaked to server diagnostics after held-open HTTP")
        if len(_TransportEndpoint.attempts) != 1:
            failures.append(f"held-open HTTP: expected one attempt/no retry, got {len(_TransportEndpoint.attempts)}")
        if attempt_started is not None and closed_at is None:
            failures.append("held-open HTTP: client connection did not close within 3 seconds")
        elif attempt_started is not None and closed_at - attempt_started > 3:
            failures.append("held-open HTTP: client connection closed after more than 3 seconds")

    assert not failures, "\n".join(failures)
    print(json.dumps({"case": "bark_transport_failures_are_bounded", "status": "passed"}))


async def run_slow_trickle_case(kind: str = "trickle") -> None:
    """A trickling response must obey the same total timeout as a stalled one."""
    failures: list[str] = []
    synthetic_key = "synthetic-credential-key"
    with _Endpoint() as endpoint:
        attempt_started: float | None = None
        closed_at: float | None = None

        async def trickle_delivery(client: Any) -> None:
            nonlocal attempt_started, closed_at
            result, _ = await _drive(client, "get_user_input", {
                "title": "Bark trickle", "prompt": "GUI/MCP stays normal", "client_timeout_seconds": 4,
            })
            value = _result_data(result)
            if not value or value.get("success") is not True or value.get("user_input") != "auto text":
                failures.append(f"slow {kind} changed the GUI/MCP result: {value!r}")
            if not await _wait_for_attempts(1):
                failures.append(f"slow {kind}: initial delivery attempt was not observed")
                return
            attempt_started = _TransportEndpoint.attempts[0][1]
            deadline = time.monotonic() + 4.8
            while _TransportEndpoint.client_closed_at is None and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            closed_at = _TransportEndpoint.client_closed_at
            if len(_TransportEndpoint.attempts) != 1:
                failures.append(f"slow {kind}: expected one attempt/no retry, got {len(_TransportEndpoint.attempts)}")

            followup, _ = await _drive(client, "get_user_input", {
                "title": "Bark followup", "prompt": "valid followup", "client_timeout_seconds": 4,
            })
            followup_value = _result_data(followup)
            if not followup_value or followup_value.get("success") is not True:
                failures.append(f"slow {kind} prevented a valid followup interaction: {followup_value!r}")
            if not await _wait_for_attempts(2):
                failures.append(f"slow {kind} prevented a valid followup Bark push")

        diagnostics = await _with_client(endpoint.url(f"{synthetic_key}-{kind}"), trickle_delivery)
        if synthetic_key in diagnostics:
            failures.append(f"synthetic credential key leaked to slow-{kind} diagnostics")
        if attempt_started is not None and closed_at is None:
            failures.append(f"slow {kind}: client connection did not close while MCP remained alive")
        elif attempt_started is not None and closed_at - attempt_started > 3:
            failures.append(f"slow {kind}: client connection closed after more than 3 seconds")
        if len(_TransportEndpoint.attempts) != 2:
            failures.append(f"slow {kind}: expected first push plus one followup push, got {len(_TransportEndpoint.attempts)}")
        else:
            followup_query = parse_qs(urlsplit(_TransportEndpoint.attempts[1][0]).query)
            if not followup_query.get("title", [""])[0] or "valid followup" not in followup_query.get("body", [""])[0]:
                failures.append(f"slow {kind}: valid followup request lacked decoded title/body")
    assert not failures, "\n".join(failures)
    print(json.dumps({"case": f"bark_slow_{kind}_is_total_time_bounded", "status": "passed"}))


if __name__ == "__main__":
    if sys.argv[1:] == ["--quick-bark-trickle"]:
        asyncio.run(run_slow_trickle_case())
    elif sys.argv[1:] == ["--quick-bark-slow-headers"]:
        asyncio.run(run_slow_trickle_case("header-trickle"))
    else:
        asyncio.run(run_transport_cases())
