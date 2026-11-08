"""Red contract for the configured central lifecycle runtime.

The public central entry must compose the already-authoritative lifecycle ASGI
application.  It must not retain a second ``CallCoordinator`` authority or a
stdio/local fallback merely because a process is now started from config.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import httpx
import pytest


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


async def no_wait(_: float) -> None:
    return None


class BrowserVerifier:
    def verify(self, token: str | None) -> dict[str, str] | None:
        if token == "operator-session":
            return {"operator_id": "only-operator", "browser_session_id": "browser-1"}
        return None


def module() -> Any:
    return importlib.import_module("central_http_service")


def mapping(tmp_path: Path, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "web_base_url": "https://central.test:8443",
        "data_dir": str(tmp_path / "central-data"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "test-cert.pem",
        "tls_key_file": "test-key.pem",
        "route_mode": "both",
        "desktop_credentials": {
            "desktop-secret": {"device_id": "desktop-1", "terminal_platform": "windows"},
        },
        "desktop_endpoints": {
            "desktop-1": {
                "endpoint": "https://desktop.test/events",
                "callback_credential": "callback-secret",
            },
        },
    }
    value.update(overrides)
    return value


def runtime(tmp_path: Path, *, bark: list[dict[str, object]], desktop: list[dict[str, object]]) -> Any:
    """The requested configured factory; its absence is the intended Red gap."""
    adapter = module()

    async def bark_dispatch(payload: dict[str, object]) -> dict[str, object]:
        bark.append(payload)
        return {"http_status": 200, "business_success": True}

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        desktop.append(payload)
        return {"accepted": True}

    return adapter.create_lifecycle_app(
        adapter.CentralServiceConfig.from_mapping(mapping(tmp_path)),
        clock=Clock(),
        wait=no_wait,
        bark_dispatch=bark_dispatch,
        desktop_dispatch=desktop_dispatch,
        operator_session_verifier=BrowserVerifier(),
        scheduler_sleep=no_wait,
        notification_scheduler_sleep=no_wait,
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, browser: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent:
            headers["authorization"] = f"Bearer {agent}"
        if browser:
            headers["x-operator-session"] = browser
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def create_call(app: Any) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": "runtime-create", "client_timeout_seconds": 2,
        "payload": {"prompt": "Physical checkpoint answer"},
    })
    assert response.status_code == 200
    return response.json()


def test_factory_composes_only_lifecycle_application_with_configured_dependencies(tmp_path: Path) -> None:
    """A runnable central entry has one lifecycle authority, not CallCoordinator."""
    app = runtime(tmp_path, bark=[], desktop=[])
    lifecycle = importlib.import_module("central_mcp_waiting_lifecycle")
    assert isinstance(app, lifecycle.Application)
    assert not hasattr(app, "_coordinator")
    assert app._desktop_registration_service is not None
    assert app._operator_session_verifier is not None


def test_configured_bark_link_is_call_specific_and_reconstruction_never_redispatches(tmp_path: Path) -> None:
    bark: list[dict[str, object]] = []
    first = runtime(tmp_path, bark=bark, desktop=[])
    call = create_call(first)

    asyncio.run(first.dispatch_notification_delivery())
    assert len(bark) == 1
    assert bark[0]["url"] == f"https://central.test:8443/calls/{call['call_id']}"

    reconstructed = runtime(tmp_path, bark=bark, desktop=[])
    asyncio.run(reconstructed.dispatch_notification_delivery())
    assert len(bark) == 1


def test_original_agent_reads_browser_owner_answer_after_configured_reconstruction(tmp_path: Path) -> None:
    first = runtime(tmp_path, bark=[], desktop=[])
    call = create_call(first)
    accepted = request(first, "POST", f"/calls/{call['call_id']}/accept", browser="operator-session", body={"operation_id": "web-accept"})
    assert accepted.status_code == 200
    submitted = request(first, "POST", f"/calls/{call['call_id']}/submissions", browser="operator-session", body={
        "submission_id": "runtime-submit", "result": {"user_input": "from configured runtime"},
    })
    assert submitted.status_code == 200

    reconstructed = runtime(tmp_path, bark=[], desktop=[])
    continued = request(reconstructed, "POST", "/mcp", agent="agent-secret", body={
        "request_id": "runtime-continue", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": 0,
    })
    assert continued.status_code == 200
    assert continued.json()["status"] == "opened"
    assert continued.json()["event_seq"] == 1

    final = request(reconstructed, "POST", "/mcp", agent="agent-secret", body={
        "request_id": "runtime-continue-final", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": 1,
    })
    assert final.status_code == 200
    assert final.json()["status"] == "answered"
    assert final.json()["event_seq"] == 2
    assert final.json()["result"]["user_input"] == "from configured runtime"


@pytest.mark.parametrize("override", [
    {"route_mode": "invalid"},
    {"desktop_credentials": {}},
    {"desktop_endpoints": {}},
])
def test_runtime_config_rejects_missing_or_invalid_lifecycle_dependencies_before_admission(
    tmp_path: Path, override: dict[str, object]
) -> None:
    adapter = module()
    with pytest.raises(ValueError):
        adapter.CentralServiceConfig.from_mapping(mapping(tmp_path, **override))
