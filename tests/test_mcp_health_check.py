"""Red HTTPS contracts for the authenticated central health snapshot.

This surface deliberately reports only central operational condition.  It is
not an archive, configuration, or interaction-inspection endpoint.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from test_bark_delivery_outbox import (
    _BarkReceiver,
    _configure_phone_bark,
    _start_receiver,
    _stop_receiver,
    _wait_for_delivery_state,
)
from test_central_mcp_gateway import (
    _free_port,
    _post,
    _rpc,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
)
from test_mcp_wait_leg import _with_timeout


HEALTH_CHECK = "health_check"


def _snapshot(database: Path, config: Path) -> tuple[dict[str, int], bytes]:
    with sqlite3.connect(database) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
    return counts, config.read_bytes()


def _health_call(
    port: int, certificate: Path, token: str | None, arguments: dict[str, object] | None = None
) -> tuple[int, dict[str, Any]]:
    status, response, _ = _post(
        port,
        certificate,
        _rpc("tools/call", {"name": HEALTH_CHECK, "arguments": arguments or {}}),
        token,
    )
    return status, response


def test_authenticated_health_check_lists_and_returns_a_non_secret_read_only_snapshot(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database, config)
        list_status, listed, _ = _post(
            port, certificate, _rpc("tools/list", {}), "test-token-alpha"
        )
        health_status, health = _health_call(port, certificate, "test-token-alpha")
        after = _snapshot(database, config)
    finally:
        _stop(process)

    assert list_status == health_status == 200
    assert HEALTH_CHECK in {tool["name"] for tool in listed["result"]["tools"]}
    assert _structured(health) == {
        "success": True,
        "status": "healthy",
        "storage": {"status": "healthy"},
        "scheduler": {"status": "running"},
        "desktop_terminal": {
            "configured": False,
            "online": False,
            "status": "not_configured",
        },
        "bark": {
            "configured": False,
            "status": "not_configured",
            "recent_delivery_status": "not_configured",
        },
    }
    assert after == before
    rendered = json.dumps(health, sort_keys=True).lower()
    for forbidden in ("test-token", "agent-alpha", "calls.sqlite", "data_dir", "web_base_url"):
        assert forbidden not in rendered


def test_health_check_requires_authentication_and_rejects_a_foreign_identity_claim_without_disclosure(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        unauthenticated_status, unauthenticated = _health_call(port, certificate, None)
        unknown_status, unknown = _health_call(port, certificate, "foreign-token")
        foreign_status, foreign = _health_call(
            port, certificate, "test-token-beta", {"caller_id": "agent-alpha"}
        )
    finally:
        _stop(process)

    assert unauthenticated_status == unknown_status == 401
    for response in (unauthenticated, unknown):
        assert response["error"] == {"code": -32001, "message": "unauthorized"}
    assert foreign_status == 200
    assert foreign["error"]["code"] == -32602
    rendered = json.dumps(foreign, sort_keys=True).lower()
    assert "agent-alpha" not in rendered
    assert "storage" not in rendered and "bark" not in rendered


def test_health_check_reports_degraded_bark_delivery_without_exposing_its_url_or_mutating_state(
    tmp_path: Path,
) -> None:
    port = _free_port()
    receiver, receiver_thread, bark_url = _start_receiver()
    _BarkReceiver.response_body = b'{"code":400,"message":"rejected"}'
    config, certificate = _write_gateway_config(tmp_path, port)
    _configure_phone_bark(config, bark_url)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        create_status, created, _ = _post(
            port, certificate, _with_timeout("health-bark-failure", 2), "test-token-alpha"
        )
        assert create_status == 200
        assert _structured(created)["call_state"] == "ringing"
        assert _BarkReceiver.started.wait(timeout=2)
        _BarkReceiver.release.set()
        assert _wait_for_delivery_state(database, {"failed"}) == ("failed", 1)
        before = _snapshot(database, config)
        health_status, health = _health_call(port, certificate, "test-token-alpha")
        after = _snapshot(database, config)
    finally:
        _stop(process)
        _stop_receiver(receiver, receiver_thread)

    assert health_status == 200
    report = _structured(health)
    assert report["success"] is True
    assert report["status"] == "degraded"
    assert report["storage"] == {"status": "healthy"}
    assert report["scheduler"] == {"status": "running"}
    assert report["bark"] == {
        "configured": True,
        "status": "degraded",
        "recent_delivery_status": "failed",
    }
    assert after == before
    rendered = json.dumps(report, sort_keys=True).lower()
    assert bark_url not in rendered
    assert "device-key" not in rendered
