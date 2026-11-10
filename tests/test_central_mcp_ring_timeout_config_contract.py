"""Red contract for operator-configurable ringing timeout.

SPEC §1.2 D2 makes the unanswered-invitation deadline a configurable central
setting with a 60-second default.  Today that value is hardcoded: the lifecycle
factory ``central_mcp_waiting_lifecycle.create_app`` defaults
``ring_timeout_seconds=60``, ``CentralServiceConfig`` has no such field, and
``create_lifecycle_app`` never forwards it.  This file pins the three required
behaviours so the implementation can satisfy them:

1. ``CentralServiceConfig.from_mapping`` reads ``ring_timeout_seconds`` and
   stores it on the config object (for example 30).
2. Absent the key, the default is 60.
3. ``create_lifecycle_app`` uses the configured value: a call created under
   ``ring_timeout_seconds=30`` carries ``ring_deadline == now + 30``, not the
   hardcoded 60.  A controlled clock observes the deadline through the
   lifecycle's durable ``ring_deadline`` column, which is the single authority
   the expiry pass reads.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from typing import Any

import httpx


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_wait(_: float) -> None:
    return None


class BrowserVerifier:
    def verify(self, token: str | None) -> dict[str, str] | None:
        if token == "operator-session":
            return {"operator_id": "only-operator", "browser_session_id": "browser-1"}
        return None


def module() -> Any:
    return importlib.import_module("central_http_service")


def config_mapping(tmp_path: Path, **overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "listen_host": "127.0.0.1",
        "listen_port": 8443,
        "web_base_url": "https://central.test:8443",
        "data_dir": str(tmp_path / "central-data"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "test-cert.pem",
        "tls_key_file": "test-key.pem",
    }
    value.update(overrides)
    return value


def runtime(tmp_path: Path, clock: FakeClock, **config_overrides: object) -> Any:
    adapter = module()
    config = adapter.CentralServiceConfig.from_mapping(config_mapping(tmp_path, **config_overrides))
    return adapter.create_lifecycle_app(
        config,
        clock=clock,
        wait=no_wait,
        bark_dispatch=lambda payload: {"http_status": 200, "business_success": True},
        desktop_dispatch=lambda payload: {"accepted": True},
        operator_session_verifier=BrowserVerifier(),
        scheduler_sleep=no_wait,
        notification_scheduler_sleep=no_wait,
    )


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent:
            headers["authorization"] = f"Bearer {agent}"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def create_call(app: Any) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": "ring-timeout-create", "client_timeout_seconds": 2,
        "payload": {"prompt": "ring timeout checkpoint"},
    })
    assert response.status_code == 200
    return response.json()


def ring_deadline_of(app: Any, call_id: str) -> float:
    with app.lifecycle._connection() as conn:
        row = conn.execute(
            "SELECT ring_deadline FROM lifecycle_calls WHERE call_id=?", (call_id,)
        ).fetchone()
    assert row is not None
    return float(row["ring_deadline"])


def test_from_mapping_stores_configured_ring_timeout_seconds(tmp_path: Path) -> None:
    config = module().CentralServiceConfig.from_mapping(
        config_mapping(tmp_path, ring_timeout_seconds=30)
    )
    assert config.ring_timeout_seconds == 30


def test_from_mapping_defaults_ring_timeout_seconds_to_60(tmp_path: Path) -> None:
    config = module().CentralServiceConfig.from_mapping(config_mapping(tmp_path))
    assert config.ring_timeout_seconds == 60


def test_lifecycle_app_sets_ring_deadline_from_configured_ring_timeout(tmp_path: Path) -> None:
    clock = FakeClock()
    app = runtime(tmp_path, clock, ring_timeout_seconds=30)
    call = create_call(app)
    assert ring_deadline_of(app, call["call_id"]) == clock() + 30
