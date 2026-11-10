"""Red contract for the fifth-round SPEC gaps (§7/§5.4/§8)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from central_desktop_terminal import CentralHttpDesktopClient, DesktopTerminalConfig
from central_mcp_waiting_lifecycle import create_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


async def no_wait(_: float) -> None:
    return None


def make_app(tmp_path: Path) -> Any:
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret"},
        clock=Clock(), notify=lambda _event: None, wait=no_wait,
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def rpc_call(app: Any, name: str, arguments: dict[str, object], rid: int = 1) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": name, "arguments": arguments},
    })
    assert response.status_code == 200, response.text
    return dict(response.json()["result"]["structuredContent"])


def test_missing_call_id_is_rejected(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    result = rpc_call(app, "get_user_input", {"interaction_id": "some-interaction"})
    assert result["success"] is False
    assert result["status"] == "missing_call_id"


def test_human_loop_prompt_covers_protocol_semantics(tmp_path: Path) -> None:
    app = make_app(tmp_path)
    result = rpc_call(app, "get_human_loop_prompt", {})
    prompt = result.get("prompt", "")
    for keyword in ("interaction_id", "call_id", "after_event_seq", "needs_continuation",
                    "request_id", "max_result_bytes"):
        assert keyword in prompt, f"prompt missing {keyword}"


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
            return FakeResponse({"success": True, "session_id": "persisted-session"})

    monkeypatch.setattr(httpx, "Client", FakeClient)
    return recorder


def test_restart_recovers_owner_session_without_registering(tmp_path: Path, monkeypatch: Any) -> None:
    config = _config()
    recorder = _install_fake_http(monkeypatch)

    client1 = CentralHttpDesktopClient(config)
    client1.authenticate_desktop("desktop-secret")

    client2 = CentralHttpDesktopClient(config)
    client2.authenticate_desktop("desktop-secret")

    registers = [r for r in recorder if r["url"].endswith("/api/terminals/register")]
    assert len(registers) == 1
