"""A real HTTP closed loop for the independent desktop terminal protocol.

The presenter is deliberately headless: it accepts the routed call and submits
the supplied answer.  This verifies the central service, callback listener,
terminal session, and Agent MCP continuation as one networked flow without
claiming that it substitutes for graphical-platform acceptance.
"""

from __future__ import annotations

import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import uvicorn

from central_desktop_terminal import (
    CentralHttpDesktopClient,
    DesktopTerminalClient,
    DesktopTerminalConfig,
    _TerminalListener,
    _callback_app,
)
from central_http_service import CentralServiceConfig, create_lifecycle_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() >= deadline:
            raise AssertionError("HTTP server did not start")
        time.sleep(0.01)
    return server, thread


class _SilentRing:
    def start(self, call_id: str) -> None:
        del call_id

    def stop(self, call_id: str) -> None:
        del call_id


class _AutoAnswerPresenter:
    """Headless presentation seam which exercises the real terminal client."""

    def __init__(self, client_ref: dict[str, DesktopTerminalClient]) -> None:
        self._client_ref = client_ref
        self.submission: dict[str, object] | None = None

    def show(self, state: dict[str, object]) -> None:
        call_id = str(state["call_id"])
        client = self._client_ref["client"]
        accepted = client.accept(call_id=call_id, operation_id=f"accept-{uuid.uuid4().hex}")
        assert accepted["success"] is True
        self.submission = client._central.submit(
            call_id,
            f"submission-{uuid.uuid4().hex}",
            {"user_input": "desktop closed-loop answer"},
        )
        assert self.submission["success"] is True

    def close(self, call_id: str) -> None:
        del call_id


def test_agent_receives_answer_submitted_by_headless_desktop_over_real_http(tmp_path: Path) -> None:
    """The Agent gets the desktop answer, not merely registration/delivery."""

    central_port, terminal_port = _free_port(), _free_port()
    endpoint = f"http://127.0.0.1:{terminal_port}/terminal/events"
    config = CentralServiceConfig.from_mapping({
        "listen_host": "127.0.0.1",
        "listen_port": central_port,
        "web_base_url": "https://central.test",
        "data_dir": str(tmp_path / "central-data"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "unused-in-test.crt",
        "tls_key_file": "unused-in-test.key",
        "route_mode": "desktop",
        "desktop_credentials": {
            "desktop-secret": {
                "device_id": "desktop-61", "terminal_platform": "windows",
            }
        },
        "desktop_endpoints": {
            "desktop-61": {
                "endpoint": endpoint, "callback_credential": "callback-secret",
            }
        },
    })

    async def wait(seconds: float) -> None:
        import asyncio
        await asyncio.sleep(seconds)

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        callback_endpoint = payload.get("endpoint")
        callback_credential = payload.get("callback_credential")
        assert isinstance(callback_endpoint, str)
        assert isinstance(callback_credential, str)
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"endpoint", "callback_credential"}
        }
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.post(
                callback_endpoint,
                json=body,
                headers={"x-central-callback": callback_credential},
            )
        return {"accepted": 200 <= response.status_code < 300}

    central_app = create_lifecycle_app(
        config,
        clock=time.time,
        wait=wait,
        bark_dispatch=lambda _: {"http_status": 503, "business_success": False},
        desktop_dispatch=desktop_dispatch,
        operator_session_verifier=object(),
    )
    central_server, central_thread = _serve(central_app, central_port)
    terminal_server: uvicorn.Server | None = None
    terminal_thread: threading.Thread | None = None
    try:
        terminal_config = DesktopTerminalConfig(
            central_url=f"http://127.0.0.1:{central_port}",
            terminal_credential="desktop-secret",
            callback_credential="callback-secret",
            listener_host="127.0.0.1",
            listener_port=terminal_port,
            verify_tls=True,
        )
        central = CentralHttpDesktopClient(terminal_config)
        holder: dict[str, DesktopTerminalClient] = {}
        presenter = _AutoAnswerPresenter(holder)
        terminal = DesktopTerminalClient(
            config={
                "terminal_credential": "desktop-secret",
                "callback_credential": "callback-secret",
            },
            central=central,
            listener=_TerminalListener(),
            gui_capability=lambda: True,
            ring=_SilentRing(),
            presenter=presenter,
        )
        holder["client"] = terminal
        terminal_server, terminal_thread = _serve(_callback_app(terminal), terminal_port)
        assert terminal.start() == {"success": True, "status": "ready"}

        with httpx.Client(base_url=f"http://127.0.0.1:{central_port}", timeout=8) as agent:
            opened_or_heartbeat = agent.post(
                "/mcp",
                headers={"authorization": "Bearer agent-secret"},
                json={
                    "request_id": "desktop-closed-loop-create",
                    "client_timeout_seconds": 2,
                    "payload": {"tool": "get_user_input", "prompt": "Answer from desktop"},
                },
            ).json()
            assert opened_or_heartbeat["call_state"] in {"ringing", "accepted", "answered"}
            interaction_id, call_id = opened_or_heartbeat["interaction_id"], opened_or_heartbeat["call_id"]

            result: dict[str, object] | None = None
            after_event_seq = 0
            for attempt in range(8):
                response = agent.post(
                    "/mcp",
                    headers={"authorization": "Bearer agent-secret"},
                    json={
                        "request_id": f"desktop-closed-loop-read-{attempt}",
                        "interaction_id": interaction_id,
                        "call_id": call_id,
                        "after_event_seq": after_event_seq,
                        "client_timeout_seconds": 2,
                    },
                ).json()
                if response.get("status") == "answered":
                    result = response
                    break
                sequence = response.get("event_seq")
                if isinstance(sequence, int):
                    after_event_seq = sequence
                time.sleep(0.1)

        assert presenter.submission is not None
        assert result is not None
        assert result["handled_on"] == "desktop"
        assert result["terminal_platform"] == "windows"
        assert result["result"] == {"user_input": "desktop closed-loop answer"}
    finally:
        if terminal_server is not None:
            terminal_server.should_exit = True
        if terminal_thread is not None:
            terminal_thread.join(timeout=5)
        central_server.should_exit = True
        central_thread.join(timeout=5)
