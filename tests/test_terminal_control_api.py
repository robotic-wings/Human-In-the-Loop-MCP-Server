"""Red contracts for the pre-registered desktop terminal control boundary.

This slice is deliberately limited to authenticated terminal state, accept, and
decline operations over the central service's TLS listener.  It does not add a
GUI, registration, callbacks, browser/mobile behavior, submissions, or task
workflows.
"""

from __future__ import annotations

import http.client
import json
import ssl
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier, Thread
from typing import Any

from call_coordinator import CallCoordinator, CreateCall
from test_central_mcp_gateway import (
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


def _write_terminal_gateway_config(directory: Path, port: int) -> tuple[Path, Path]:
    config_path, certificate = _write_gateway_config(directory, port)
    terminal_credentials = directory / "terminal-credentials.json"
    terminal_credentials.write_text(
        json.dumps(
            {
                "desktop-token": {
                    "device_id": "desktop-registered",
                    "terminal_platform": "windows",
                },
                "other-desktop-token": {
                    "device_id": "desktop-other",
                    "terminal_platform": "linux",
                },
            }
        ),
        encoding="utf-8",
    )
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["terminal_credentials_file"] = str(terminal_credentials)
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path, certificate


def _terminal_request(
    port: int,
    certificate: Path,
    method: str,
    path: str,
    token: str | None,
    body: dict[str, object] | None = None,
) -> tuple[int, dict[str, Any]]:
    context = ssl.create_default_context(cafile=str(certificate))
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=2)
    headers = {"Accept": "application/json"}
    encoded: str | None = None
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    if body is not None:
        headers["Content-Type"] = "application/json"
        encoded = json.dumps(body)
    connection.request(method, path, body=encoded, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        # The red implementation currently returns the stock HTML 404 page.
        # Preserve the status so the contract failure identifies the missing
        # terminal route rather than a helper parsing detail.
        return status, {"_non_json": raw.decode("utf-8", errors="replace")}


def _create_ringing_call(port: int, certificate: Path, request_id: str) -> dict[str, Any]:
    status, response, _ = _post(port, certificate, _call(request_id), "test-token-alpha")
    assert status == 200
    created = _structured(response)
    assert created["status"] == "ringing"
    return created


def _read_opened_event(
    port: int, certificate: Path, created: dict[str, Any], request_id: str
) -> dict[str, Any]:
    status, response, _ = _post(
        port,
        certificate,
        _continuation(request_id, created["interaction_id"], created["call_id"], 0),
        "test-token-alpha",
    )
    assert status == 200
    return _structured(response)


def test_terminal_and_agent_credentials_are_separated_before_lookup_or_mutation(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_ringing_call(port, certificate, "separation-create")
        terminal_mcp_status, _, _ = _post(
            port, certificate, _call("terminal-mcp"), "desktop-token"
        )
        agent_state_status, _ = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "test-token-alpha"
        )
        agent_accept_status, _ = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "test-token-alpha",
            {"action_id": "agent-action", "owner_session_id": "agent-session"},
        )
        terminal_state_status, state = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
    finally:
        _stop(process)

    assert terminal_mcp_status == 401
    assert agent_state_status == agent_accept_status == 401
    assert terminal_state_status == 200
    call = state["call"]
    assert call == {
        "call_id": created["call_id"],
        "call_state": "ringing",
        "version": 1,
        "ring_deadline": call["ring_deadline"],
    }
    assert "message" not in state
    assert "prompt" not in state


def test_terminal_state_is_read_only_and_accept_derives_desktop_identity_then_persists(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_ringing_call(port, certificate, "accept-create")
        first_state_status, first_state = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
        second_state_status, second_state = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
        accept_status, accepted = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "desktop-token",
            {
                "action_id": "accept-1",
                "owner_session_id": "desktop-session-1",
                "device_id": "forged-device",
                "handled_on": "iphone_web",
                "terminal_platform": "ios",
            },
        )
        opened = _read_opened_event(port, certificate, created, "opened-read")
    finally:
        _stop(process)

    assert first_state_status == second_state_status == 200
    assert first_state == second_state
    assert first_state["call"]["call_state"] == "ringing"
    assert accept_status == 200
    assert accepted["status"] == opened["status"] == "opened"
    assert accepted["owner_device_id"] == opened["owner_device_id"] == "desktop-registered"
    assert accepted["owner_session_id"] == opened["owner_session_id"] == "desktop-session-1"
    assert accepted["handled_on"] == opened["handled_on"] == "desktop"
    assert accepted["terminal_platform"] == opened["terminal_platform"] == "windows"

    restarted = _start(config)
    _wait_for_listener(restarted, port)
    try:
        state_status, state = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
        takeover_status, takeover = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "other-desktop-token",
            {"action_id": "takeover", "owner_session_id": "other-session"},
        )
    finally:
        _stop(restarted)

    assert state_status == 200
    assert state["call"]["owner_device_id"] == "desktop-registered"
    assert state["call"]["owner_session_id"] == "desktop-session-1"
    assert takeover_status == 409
    assert takeover["status"] == "ownership_conflict"


def test_terminal_decline_releases_busy_and_never_supplies_an_answer_value(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_ringing_call(port, certificate, "decline-create")
        declined_status, declined = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/decline",
            "desktop-token",
            {"action_id": "decline-1"},
        )
        event = _read_opened_event(port, certificate, created, "decline-read")
        next_call = _create_ringing_call(port, certificate, "after-decline")
    finally:
        _stop(process)

    assert declined_status == 200
    assert declined["status"] == event["status"] == "declined"
    assert declined["human_action"] is True
    for value in (declined, event):
        assert "user_input" not in value
        assert "selected_choices" not in value
        assert "confirmed" not in value
        assert "acknowledged" not in value
    assert next_call["status"] == "ringing"


def test_terminal_action_id_replays_exactly_and_changed_request_conflicts_without_new_event(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _create_ringing_call(port, certificate, "replay-create")
        body = {"action_id": "accept-replay", "owner_session_id": "desktop-session-1"}
        first_status, first = _terminal_request(
            port, certificate, "POST", f"/api/calls/{created['call_id']}/accept", "desktop-token", body
        )
        replay_status, replay = _terminal_request(
            port, certificate, "POST", f"/api/calls/{created['call_id']}/accept", "desktop-token", body
        )
        conflict_status, conflict = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "desktop-token",
            {"action_id": "accept-replay", "owner_session_id": "different-session"},
        )
        event = _read_opened_event(port, certificate, created, "replay-event")
    finally:
        _stop(process)

    assert first_status == replay_status == 200
    assert replay == first
    assert conflict_status == 409
    assert conflict == {
        "success": False,
        "status": "request_conflict",
        "needs_continuation": False,
    }
    assert event == first


def test_terminal_actions_settle_expiry_before_accept_decline_race_without_revival(
    tmp_path: Path,
) -> None:
    """The HTTP actions must share the coordinator's transactional expiry rule."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    frozen = _FrozenClock(datetime(2020, 1, 1, tzinfo=UTC))
    seed = CallCoordinator(data_dir / "calls.sqlite3", clock=frozen)
    created = seed.create_call(
        CreateCall("operator-default", "agent-alpha", "expired", "expired-create", "old", 1)
    )
    seed.close()
    process = _start(config)
    _wait_for_listener(process, port)
    barrier = Barrier(3)
    outcomes: list[tuple[int, dict[str, Any]]] = []

    def accept() -> None:
        barrier.wait()
        outcomes.append(
            _terminal_request(
                port,
                certificate,
                "POST",
                f"/api/calls/{created['call_id']}/accept",
                "desktop-token",
                {"action_id": "late-accept", "owner_session_id": "late-session"},
            )
        )

    def decline() -> None:
        barrier.wait()
        outcomes.append(
            _terminal_request(
                port,
                certificate,
                "POST",
                f"/api/calls/{created['call_id']}/decline",
                "desktop-token",
                {"action_id": "late-decline"},
            )
        )

    first, second = Thread(target=accept), Thread(target=decline)
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()
    try:
        event = _read_opened_event(port, certificate, created, "expired-event")
    finally:
        _stop(process)

    assert len(outcomes) == 2
    assert all(status == 200 for status, _ in outcomes)
    assert all(result["status"] == "not_accepted" for _, result in outcomes)
    assert event["status"] == "not_accepted"
    assert event["reason"] == "ring_timeout"


class _FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now
