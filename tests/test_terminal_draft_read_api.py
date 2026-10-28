"""Red HTTPS retrieval contracts for owner-session-scoped terminal drafts.

The draft remains deliberately opaque: this file exercises only the persisted
``{body, draft_version}`` read representation and its authorization boundary.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import ssl
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from call_coordinator import CallCoordinator, CreateCall
from test_central_mcp_gateway import _free_port, _start, _stop
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


# This is the sole, explicit HTTP credential for the accepted terminal session.
OWNER_SESSION_HEADER = "X-Owner-Session-Id"


def _draft_read_request(
    port: int,
    certificate: Path,
    path: str,
    token: str,
    owner_session_id: str,
) -> tuple[int, dict[str, Any]]:
    context = ssl.create_default_context(cafile=str(certificate))
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=3)
    connection.request(
        "GET",
        path,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            OWNER_SESSION_HEADER: owner_session_id,
        },
    )
    response = connection.getresponse()
    payload = json.loads(response.read().decode("utf-8"))
    connection.close()
    return response.status, payload


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _accepted_call(service: CallCoordinator) -> str:
    created = service.create_call(
        CreateCall(
            "operator-1", "agent-1", "draft-read-interaction", "draft-read-create",
            "draft read", 60, "desktop-registered",
        )
    )
    accepted = service.terminal_action(
        "accept", created["call_id"], "desktop-registered", "draft-read-accept",
        "draft-owner", "windows",
    )
    assert accepted is not None and accepted["status"] == "opened"
    return created["call_id"]


def _call_row(database: Path, call_id: str) -> dict[str, Any]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    assert row is not None
    return dict(row)


def test_tls_draft_read_restores_exact_canonical_draft_after_restart_and_keeps_state_content_free(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "draft-read-restart")
        call_id = created["call_id"]
        assert _terminal_request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", "desktop-token",
            {"action_id": "draft-read-accept", "owner_session_id": "draft-read-owner"},
        )[0] == 200
        assert _terminal_request(
            port, certificate, "PUT", f"/api/calls/{call_id}/draft", "desktop-token",
            {"owner_session_id": "draft-read-owner", "body": "persisted over TLS", "expected_draft_version": 0},
        )[0] == 200
    finally:
        _stop(process)

    restarted = _start(config)
    _wait_for_tls_gateway(restarted, port, certificate)
    try:
        draft = _draft_read_request(
            port, certificate, f"/api/calls/{call_id}/draft", "desktop-token", "draft-read-owner"
        )
        state = _terminal_request(port, certificate, "GET", f"/api/calls/{call_id}", "desktop-token")
    finally:
        _stop(restarted)

    assert draft == (200, {"body": "persisted over TLS", "draft_version": 1})
    assert state[0] == 200
    assert "draft" not in state[1]["call"]
    assert "persisted over TLS" not in json.dumps(state[1])


def test_tls_draft_read_returns_empty_canonical_object_for_accepted_owner_without_a_save(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "draft-read-empty")
        call_id = created["call_id"]
        assert _terminal_request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", "desktop-token",
            {"action_id": "draft-read-empty-accept", "owner_session_id": "empty-owner"},
        )[0] == 200
        draft = _draft_read_request(
            port, certificate, f"/api/calls/{call_id}/draft", "desktop-token", "empty-owner"
        )
    finally:
        _stop(process)

    assert draft == (200, {"body": None, "draft_version": 0})


def test_tls_draft_read_hides_call_and_draft_from_wrong_bearer_route_or_owner_session(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "draft-read-auth")
        call_id = created["call_id"]
        assert _terminal_request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", "desktop-token",
            {"action_id": "draft-read-auth-accept", "owner_session_id": "auth-owner"},
        )[0] == 200
        assert _terminal_request(
            port, certificate, "PUT", f"/api/calls/{call_id}/draft", "desktop-token",
            {"owner_session_id": "auth-owner", "body": "must remain secret", "expected_draft_version": 0},
        )[0] == 200
        owner = _draft_read_request(
            port, certificate, f"/api/calls/{call_id}/draft", "desktop-token", "auth-owner"
        )
        wrong_session = _draft_read_request(
            port, certificate, f"/api/calls/{call_id}/draft", "desktop-token", "other-owner"
        )
        wrong_bearer = _draft_read_request(
            port, certificate, f"/api/calls/{call_id}/draft", "other-desktop-token", "auth-owner"
        )
        unknown = _draft_read_request(
            port, certificate, "/api/calls/not-a-real-call/draft", "desktop-token", "auth-owner"
        )
    finally:
        _stop(process)

    assert owner == (200, {"body": "must remain secret", "draft_version": 1})
    assert wrong_session == wrong_bearer == unknown == (404, {"error": "not found"})
    for response in (wrong_session, wrong_bearer, unknown):
        assert "must remain secret" not in json.dumps(response[1])
        assert "draft_version" not in response[1]


def test_draft_read_settles_expiry_and_is_read_only_for_liveness_versions_events_results_and_busy_slot(
    tmp_path: Path,
) -> None:
    clock = FakeClock(datetime(2026, 10, 26, tzinfo=UTC))
    database = tmp_path / "calls.sqlite3"
    service = CallCoordinator(database, clock=clock)
    call_id = _accepted_call(service)
    assert service.save_terminal_draft(call_id, "desktop-registered", "draft-owner", "saved", 0)["success"]
    before = _call_row(database, call_id)
    events_before = service.events_for(call_id)

    clock.advance(10)
    readable = service.read_terminal_draft(call_id, "desktop-registered", "draft-owner")
    after_read = _call_row(database, call_id)
    events_after_read = service.events_for(call_id)
    busy = service.create_call(
        CreateCall("operator-1", "agent-2", "busy-interaction", "busy-request", "busy", 60, "desktop-registered")
    )
    clock.advance(170)
    expired_read = service.read_terminal_draft(call_id, "desktop-registered", "draft-owner")
    settled = service.get_call(call_id)
    after_expiry = _call_row(database, call_id)
    service.close()

    assert readable == {"body": "saved", "draft_version": 1}
    for field in ("terminal_last_seen_at", "terminal_disconnect_deadline", "version", "draft_version"):
        assert after_read[field] == before[field]
    assert events_after_read == events_before
    assert busy["status"] == "busy"
    assert expired_read is None
    assert settled["status"] == "disconnected"
    assert settled["reason"] == "terminal_disconnected"
    assert after_expiry["draft_body"] == "saved"
    assert after_expiry["version"] == before["version"] + 1
