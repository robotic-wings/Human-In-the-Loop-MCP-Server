"""Red contracts for the bounded MCP wait-leg protocol.

This 8ca slice covers only agent-side client timeout legs.  It deliberately
does not prescribe a streaming implementation or cover terminal/progress
liveness, routing, UI, revisions, or result reads.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from call_coordinator import CallCoordinator, CreateCall
from test_central_mcp_gateway import (
    HUMAN_TOOLS,
    _call,
    _free_port,
    _post,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
)
from test_mcp_continuation import _continuation


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _with_timeout(request_id: str, timeout: int) -> dict[str, Any]:
    request = _call(request_id)
    request["params"]["arguments"]["client_timeout_seconds"] = timeout
    return request


def test_controlled_clock_persists_safe_wait_leg_from_request_receipt(tmp_path: Path) -> None:
    database = tmp_path / "calls.sqlite3"
    clock = FakeClock(datetime(2030, 1, 1, tzinfo=UTC))
    service = CallCoordinator(database, clock=clock)
    created = service.create_call(
        CreateCall("operator-default", "agent-alpha", "controlled", "create", "wait", 60)
    )

    service.continue_call(
        "agent-alpha",
        "wait-leg",
        "controlled",
        created["call_id"],
        0,
        "{}",
        client_timeout_seconds=8,
    )

    with sqlite3.connect(database) as connection:
        deadline = connection.execute(
            "SELECT wait_leg_deadline FROM calls WHERE call_id = ?", (created["call_id"],)
        ).fetchone()[0]
    # max(1, 8 - min(30, max(1, 8 // 4))) == 6 seconds from receipt.
    assert deadline == "2030-01-01T00:00:06+00:00"


def test_human_tool_schemas_keep_client_timeout_for_agent_wait_legs_over_tls(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, response, headers_in = _post(
            port,
            certificate,
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            "test-token-alpha",
        )
    finally:
        _stop(process)

    assert headers_in < 1
    schemas = {
        item["name"]: item["inputSchema"]
        for item in response["result"]["tools"]
        if item["name"] in HUMAN_TOOLS
    }
    assert all(
        schemas[name]["properties"]["client_timeout_seconds"] == {"type": "integer"}
        for name in HUMAN_TOOLS
    )


def test_tls_rejects_subtwo_second_timeout_before_admission_or_notification(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        status, invalid_response, headers_in = _post(
            port, certificate, _with_timeout("too-short", 1), "test-token-alpha"
        )
        _, valid_response, _ = _post(
            port, certificate, _with_timeout("valid-after-error", 2), "test-token-alpha"
        )
    finally:
        _stop(process)

    invalid = _structured(invalid_response)
    assert status == 200
    assert headers_in < 1
    assert invalid == {
        "success": False,
        "status": "invalid_client_timeout",
        "needs_continuation": False,
    }
    # The rejected request must not consume the single active-call slot.
    assert _structured(valid_response)["status"] in {"heartbeat", "ringing"}


def test_tls_wait_leg_returns_heartbeat_after_safe_interval_without_extending_ring_deadline(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        started = time.monotonic()
        _, response, _ = _post(
            port, certificate, _with_timeout("safe-leg", 2), "test-token-alpha"
        )
        elapsed = time.monotonic() - started
        heartbeat = _structured(response)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            persisted_ring_deadline = connection.execute(
                "SELECT ring_deadline FROM calls WHERE call_id = ?", (heartbeat["call_id"],)
            ).fetchone()[0]
    finally:
        _stop(process)

    # timeout=2 has the specified safe leg max(1, 2 - 1) == 1.
    assert heartbeat["status"] == "heartbeat"
    assert heartbeat["call_state"] == "ringing"
    assert heartbeat["needs_continuation"] is True
    assert heartbeat["ring_deadline"] == persisted_ring_deadline
    assert 0.8 <= elapsed < 2


def test_only_one_tls_waiter_is_outstanding_and_replay_does_not_start_another_leg(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, opened_response, _ = _post(
            port, certificate, _with_timeout("initial-leg", 2), "test-token-alpha"
        )
        opened = _structured(opened_response)
        request = _continuation(
            "waiting-leg", opened["interaction_id"], opened["call_id"], opened["event_seq"]
        )
        request["params"]["arguments"]["client_timeout_seconds"] = 2
        outcome: dict[str, Any] = {}

        def wait_for_heartbeat() -> None:
            outcome["status"], outcome["response"], _ = _post(
                port, certificate, request, "test-token-alpha"
            )

        waiter = threading.Thread(target=wait_for_heartbeat)
        waiter.start()
        time.sleep(0.1)
        competing = _continuation(
            "competing-leg", opened["interaction_id"], opened["call_id"], opened["event_seq"]
        )
        competing["params"]["arguments"]["client_timeout_seconds"] = 2
        _, competing_response, _ = _post(
            port, certificate, competing, "test-token-alpha"
        )
        waiter.join(timeout=4)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            deadline_before_replay = connection.execute(
                "SELECT wait_leg_deadline FROM calls WHERE call_id = ?", (opened["call_id"],)
            ).fetchone()[0]
        _, replay_response, _ = _post(
            port, certificate, request, "test-token-alpha"
        )
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            deadline_after_replay = connection.execute(
                "SELECT wait_leg_deadline FROM calls WHERE call_id = ?", (opened["call_id"],)
            ).fetchone()[0]
    finally:
        _stop(process)

    assert not waiter.is_alive()
    assert outcome["status"] == 200
    assert _structured(competing_response) == {
        "success": False,
        "status": "already_waiting",
        "needs_continuation": False,
        "retryable": True,
    }
    assert _structured(outcome["response"])["status"] == "heartbeat"
    assert replay_response == outcome["response"]
    assert deadline_after_replay == deadline_before_replay
