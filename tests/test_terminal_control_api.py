"""Red contracts for the pre-registered desktop terminal control boundary.

This slice is deliberately limited to authenticated terminal state, accept, and
decline operations over the central service's TLS listener.  It does not add a
GUI, registration, callbacks, browser/mobile behavior, submissions, or task
workflows.
"""

from __future__ import annotations

import http.client
import json
import sqlite3
import ssl
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from typing import Any

from call_coordinator import AcceptCall, CallCoordinator, CreateCall, DeclineCall
from test_central_mcp_gateway import (
    _call,
    _free_port,
    _post,
    _rpc,
    _start,
    _stop,
    _structured,
    _write_gateway_config,
)
from test_mcp_continuation import _continuation


def _write_terminal_gateway_config(
    directory: Path, port: int, *, eligible_desktop_device_ids: list[str] | None = None
) -> tuple[Path, Path]:
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
    config["eligible_desktop_device_ids"] = (
        eligible_desktop_device_ids or ["desktop-registered"]
    )
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path, certificate


def _set_eligible_desktop_device_ids(config_path: Path, device_ids: list[str]) -> None:
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["eligible_desktop_device_ids"] = device_ids
    config_path.write_text(json.dumps(config), encoding="utf-8")


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


def _wait_for_tls_gateway(process: Any, port: int, certificate: Path) -> None:
    """Wait for authenticated MCP/TLS readiness without masking HTTP failures."""
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AssertionError("central service stopped before its TLS gateway was ready")
        try:
            status, response, _ = _post(
                port,
                certificate,
                _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
                "test-token-alpha",
            )
        except OSError:
            time.sleep(0.03)
            continue
        # A listener can transiently accept TCP while its application layer is
        # still unavailable.  Retry only that explicit readiness condition;
        # endpoint, authentication, and other contract responses must remain
        # visible to the test.
        if status == 503:
            time.sleep(0.03)
            continue
        if status != 200:
            raise AssertionError(f"central service rejected authenticated TLS readiness: {status}")
        if "error" in response or "result" not in response:
            raise AssertionError("central service returned malformed authenticated TLS readiness")
        return
    raise AssertionError("central service did not accept a TLS request after restart")


def _create_ringing_call(port: int, certificate: Path, request_id: str) -> dict[str, Any]:
    status, response, _ = _post(port, certificate, _call(request_id), "test-token-alpha")
    assert status == 200
    created = _structured(response)
    assert created["status"] == "ringing"
    return created


def _seed_legacy_ringing_call(
    database: Path,
    *,
    operator_id: str,
    interaction_id: str,
    request_id: str,
    legacy_null_route: bool = True,
) -> dict[str, Any]:
    """Persist a routed active row, optionally with its migration-era NULL route."""
    database.parent.mkdir(parents=True, exist_ok=True)
    coordinator = CallCoordinator(database, clock=_WallClock())
    try:
        created = coordinator.create_call(
            CreateCall(
                operator_id,
                "agent-alpha",
                interaction_id,
                request_id,
                "legacy prompt",
                60,
                desktop_route_device_id="desktop-registered",
            )
        )
    finally:
        coordinator.close()
    if legacy_null_route:
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET desktop_route_device_id = NULL WHERE call_id = ?",
                (created["call_id"],),
            )
    return created


def _persisted_terminal_snapshot(
    database: Path, call_id: str
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Read durable call and event state independently of the endpoint response."""
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        call = connection.execute(
            "SELECT * FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq",
            (call_id,),
        ).fetchall()
    assert call is not None
    return dict(call), [dict(event) for event in events]


def _terminal_action_operations(
    database: Path, device_id: str, call_id: str, action_id: str
) -> list[dict[str, Any]]:
    """Read the exact replay key derived from a terminal credential and action."""
    scoped_action_id = f"terminal:{device_id}:{call_id}:{action_id}"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            "SELECT action_id, payload, result FROM action_operations WHERE action_id = ?",
            (scoped_action_id,),
        ).fetchall()
    return [dict(row) for row in rows]


class _WallClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


def _read_opened_event(
    port: int,
    certificate: Path,
    created: dict[str, Any],
    request_id: str,
    after_event_seq: int = 0,
) -> dict[str, Any]:
    status, response, _ = _post(
        port,
        certificate,
        _continuation(
            request_id,
            created["interaction_id"],
            created["call_id"],
            after_event_seq,
        ),
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
    _wait_for_tls_gateway(process, port, certificate)
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
    _wait_for_tls_gateway(process, port, certificate)
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
    finally:
        _stop(process)

    assert first_state_status == second_state_status == 200
    assert first_state == second_state
    assert first_state["call"]["call_state"] == "ringing"
    assert accept_status == 200
    assert accepted["status"] == "opened"
    assert accepted["owner_device_id"] == "desktop-registered"
    assert accepted["owner_session_id"] == "desktop-session-1"
    assert accepted["handled_on"] == "desktop"
    assert accepted["terminal_platform"] == "windows"

    restarted = _start(config)
    _wait_for_tls_gateway(restarted, port, certificate)
    try:
        opened = _read_opened_event(port, certificate, created, "opened-after-restart")
        after_opened = _read_opened_event(
            port, certificate, created, "opened-after-event", opened["event_seq"]
        )
        state_status, state = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
        takeover_payload = {"action_id": "takeover", "owner_session_id": "other-session"}
        takeover = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "other-desktop-token",
            takeover_payload,
        )
        unknown_takeover = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/no-such-call/accept",
            "other-desktop-token",
            takeover_payload,
        )
        state_after_takeover_status, state_after_takeover = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
    finally:
        _stop(restarted)

    assert state_status == 200
    assert state["call"]["owner_device_id"] == "desktop-registered"
    assert state["call"]["owner_session_id"] == "desktop-session-1"
    assert opened == accepted
    assert opened["event_seq"] == 1
    assert after_opened["status"] == "heartbeat"
    assert after_opened["event_seq"] == opened["event_seq"]
    assert after_opened["call_state"] == "accepted"
    assert takeover == unknown_takeover == (404, {"error": "not found"})
    assert state_after_takeover_status == 200
    assert state_after_takeover["call"]["owner_device_id"] == "desktop-registered"
    assert state_after_takeover["call"]["owner_session_id"] == "desktop-session-1"


def test_terminal_decline_releases_busy_and_never_supplies_an_answer_value(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
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
    finally:
        _stop(process)

    restarted = _start(config)
    _wait_for_tls_gateway(restarted, port, certificate)
    try:
        event = _read_opened_event(port, certificate, created, "decline-after-restart")
        terminal_replay = _read_opened_event(
            port, certificate, created, "decline-after-event", event["event_seq"]
        )
        next_call = _create_ringing_call(port, certificate, "after-decline")
    finally:
        _stop(restarted)

    assert declined_status == 200
    assert event == declined
    assert declined["status"] == "declined"
    assert event["event_seq"] == terminal_replay["event_seq"] == 1
    assert terminal_replay["status"] == "declined"
    assert declined["human_action"] is True
    for value in (declined, event):
        assert "user_input" not in value
        assert "selected_choices" not in value
        assert "confirmed" not in value
        assert "acknowledged" not in value
    assert next_call["status"] == "ringing"


def test_persisted_desktop_route_denies_other_device_without_leaking_or_mutating_call(
    tmp_path: Path,
) -> None:
    """A call's eligible desktop is fixed at creation, not inferred from a token."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(
        tmp_path, port, eligible_desktop_device_ids=["desktop-registered"]
    )
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "persisted-route-create")
    finally:
        _stop(process)

    restarted = _start(config)
    _wait_for_tls_gateway(restarted, port, certificate)
    try:
        other_state = _terminal_request(
            port, certificate, "GET", f"/api/calls/{created['call_id']}", "other-desktop-token"
        )
        random_state = _terminal_request(
            port, certificate, "GET", "/api/calls/not-a-real-call", "other-desktop-token"
        )
        other_accept = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "other-desktop-token",
            {"action_id": "other-accept", "owner_session_id": "other-session"},
        )
        random_accept = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/not-a-real-call/accept",
            "other-desktop-token",
            {"action_id": "other-accept-random", "owner_session_id": "other-session"},
        )
        other_decline = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/decline",
            "other-desktop-token",
            {"action_id": "other-decline"},
        )
        random_decline = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/not-a-real-call/decline",
            "other-desktop-token",
            {"action_id": "other-decline-random"},
        )
        registered_before_status, registered_before = _terminal_request(
            port, certificate, "GET", "/api/terminals/state", "desktop-token"
        )
        after_denials = _read_opened_event(
            port, certificate, created, "route-denials-leave-no-event"
        )
        registered_accept_status, registered_accept = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{created['call_id']}/accept",
            "desktop-token",
            {"action_id": "registered-accept", "owner_session_id": "registered-session"},
        )
    finally:
        _stop(restarted)

    assert other_state == random_state
    assert other_accept == random_accept
    assert other_decline == random_decline
    assert registered_before_status == 200
    assert registered_before["call"] == {
        "call_id": created["call_id"],
        "call_state": "ringing",
        "version": 1,
        "ring_deadline": registered_before["call"]["ring_deadline"],
    }
    assert after_denials["status"] == "heartbeat"
    assert after_denials["call_state"] == "ringing"
    assert after_denials["event_seq"] == 0
    assert registered_accept_status == 200
    assert registered_accept["status"] == "opened"
    assert registered_accept["owner_device_id"] == "desktop-registered"


def test_action_id_is_scoped_to_credential_device_and_routed_call(
    tmp_path: Path,
) -> None:
    """An unauthorized action cannot reserve an ID or collide across devices."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(
        tmp_path, port, eligible_desktop_device_ids=["desktop-registered"]
    )
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        first = _create_ringing_call(port, certificate, "first-routed-call")
        denied_first = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{first['call_id']}/decline",
            "other-desktop-token",
            {"action_id": "shared-decline"},
        )
        denied_random = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/not-a-real-call/decline",
            "other-desktop-token",
            {"action_id": "shared-decline"},
        )
        first_decline_status, first_decline = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{first['call_id']}/decline",
            "desktop-token",
            {"action_id": "shared-decline"},
        )
    finally:
        _stop(process)

    assert denied_first == denied_random
    assert first_decline_status == 200
    assert first_decline["status"] == "declined"
    assert first_decline["call_id"] == first["call_id"]

    _set_eligible_desktop_device_ids(config, ["desktop-other"])
    restarted = _start(config)
    _wait_for_tls_gateway(restarted, port, certificate)
    try:
        second = _create_ringing_call(port, certificate, "second-routed-call")
        second_decline_status, second_decline = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{second['call_id']}/decline",
            "other-desktop-token",
            {"action_id": "shared-decline"},
        )
    finally:
        _stop(restarted)

    assert second_decline_status == 200
    assert second_decline["status"] == "declined"
    assert second_decline["call_id"] == second["call_id"]


def test_legacy_null_desktop_route_is_nonleaking_immutable_and_does_not_reserve_actions(
    tmp_path: Path,
) -> None:
    """A migration-era NULL route must not grant another terminal an action lease."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    legacy_accept = _seed_legacy_ringing_call(
        database,
        operator_id="legacy-accept-operator",
        interaction_id="legacy-accept-interaction",
        request_id="legacy-accept-create",
    )
    legacy_decline = _seed_legacy_ringing_call(
        database,
        operator_id="legacy-decline-operator",
        interaction_id="legacy-decline-interaction",
        request_id="legacy-decline-create",
    )
    accept_before = _persisted_terminal_snapshot(database, legacy_accept["call_id"])
    decline_before = _persisted_terminal_snapshot(database, legacy_decline["call_id"])
    accept_action_before = _terminal_action_operations(
        database, "desktop-other", legacy_accept["call_id"], "legacy-accept"
    )
    decline_action_before = _terminal_action_operations(
        database, "desktop-other", legacy_decline["call_id"], "legacy-decline"
    )

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        denied_accept = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{legacy_accept['call_id']}/accept",
            "other-desktop-token",
            {"action_id": "legacy-accept", "owner_session_id": "other-session"},
        )
        random_accept = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/no-such-legacy-accept/accept",
            "other-desktop-token",
            {"action_id": "legacy-accept", "owner_session_id": "other-session"},
        )
        denied_decline = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{legacy_decline['call_id']}/decline",
            "other-desktop-token",
            {"action_id": "legacy-decline"},
        )
        random_decline = _terminal_request(
            port,
            certificate,
            "POST",
            "/api/calls/no-such-legacy-decline/decline",
            "other-desktop-token",
            {"action_id": "legacy-decline"},
        )
        accept_after_denial = _persisted_terminal_snapshot(database, legacy_accept["call_id"])
        decline_after_denial = _persisted_terminal_snapshot(database, legacy_decline["call_id"])
        accept_action_after = _terminal_action_operations(
            database, "desktop-other", legacy_accept["call_id"], "legacy-accept"
        )
        decline_action_after = _terminal_action_operations(
            database, "desktop-other", legacy_decline["call_id"], "legacy-decline"
        )
        registered_accept = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{legacy_accept['call_id']}/accept",
            "desktop-token",
            {"action_id": "legacy-accept", "owner_session_id": "registered-session"},
        )
        registered_decline = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{legacy_decline['call_id']}/decline",
            "desktop-token",
            {"action_id": "legacy-decline"},
        )
    finally:
        _stop(process)

    assert denied_accept == random_accept
    assert denied_decline == random_decline
    assert accept_after_denial == accept_before
    assert decline_after_denial == decline_before
    assert accept_action_before == accept_action_after == []
    assert decline_action_before == decline_action_after == []
    # The denied requests used these exact IDs first; an eligible terminal can
    # still carry out the original operation instead of seeing a replay/conflict.
    assert registered_accept[0] == 200
    assert registered_accept[1]["status"] == "opened"
    assert registered_decline[0] == 200
    assert registered_decline[1]["status"] == "declined"


def test_wrongly_routed_terminal_cannot_distinguish_unknown_ringing_or_accepted_calls(
    tmp_path: Path,
) -> None:
    """State and actions conceal every call state from a different terminal."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    ringing = _seed_legacy_ringing_call(
        database,
        operator_id="routed-ringing-operator",
        interaction_id="routed-ringing-interaction",
        request_id="routed-ringing-create",
        legacy_null_route=False,
    )
    accepted = _seed_legacy_ringing_call(
        database,
        operator_id="routed-accepted-operator",
        interaction_id="routed-accepted-interaction",
        request_id="routed-accepted-create",
        legacy_null_route=False,
    )

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        accepted_by_registered = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{accepted['call_id']}/accept",
            "desktop-token",
            {"action_id": "accept-fixture", "owner_session_id": "registered-session"},
        )
        state_unknown = _terminal_request(
            port, certificate, "GET", "/api/calls/no-such-call", "other-desktop-token"
        )
        state_ringing = _terminal_request(
            port,
            certificate,
            "GET",
            f"/api/calls/{ringing['call_id']}",
            "other-desktop-token",
        )
        state_accepted = _terminal_request(
            port,
            certificate,
            "GET",
            f"/api/calls/{accepted['call_id']}",
            "other-desktop-token",
        )
        action_responses: dict[str, list[tuple[int, dict[str, Any]]]] = {}
        for action in ("accept", "decline"):
            body: dict[str, object] = {"action_id": f"hidden-{action}"}
            if action == "accept":
                body["owner_session_id"] = "other-session"
            action_responses[action] = [
                _terminal_request(
                    port,
                    certificate,
                    "POST",
                    f"/api/calls/{call_id}/{action}",
                    "other-desktop-token",
                    body,
                )
                for call_id in ("no-such-call", ringing["call_id"], accepted["call_id"])
            ]
    finally:
        _stop(process)

    assert accepted_by_registered[0] == 200
    assert accepted_by_registered[1]["status"] == "opened"
    assert state_unknown == state_ringing == state_accepted
    for responses in action_responses.values():
        assert responses[0] == responses[1] == responses[2]


def test_deadline_settlement_races_terminal_accept_with_one_durable_winner_no_revival(
    tmp_path: Path,
) -> None:
    """Deadline recovery and an authenticated desktop accept share one transaction."""
    clock = _MutableClock(datetime(2020, 1, 1, tzinfo=UTC))
    database = tmp_path / "calls.sqlite3"
    coordinator = CallCoordinator(database, clock=clock)
    created = coordinator.create_call(
        CreateCall("operator-default", "agent-alpha", "deadline-race", "create", "prompt", 60)
    )
    clock.advance(seconds=60)
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def settle_deadline() -> None:
        barrier.wait()
        outcomes.extend(coordinator.recover_and_settle())

    def terminal_accept() -> None:
        barrier.wait()
        outcomes.append(
            coordinator.accept_call(
                AcceptCall(
                    call_id=created["call_id"],
                    action_id="accept-at-deadline",
                    device_id="desktop-registered",
                    session_id="session-at-deadline",
                    handled_on="desktop",
                    terminal_platform="windows",
                )
            )
        )

    settlement, accept = Thread(target=settle_deadline), Thread(target=terminal_accept)
    settlement.start()
    accept.start()
    barrier.wait()
    settlement.join()
    accept.join()
    final = coordinator.get_call(created["call_id"])
    events = coordinator.events_for(created["call_id"])
    coordinator.close()

    # Whichever transaction acquires SQLite first settles expiry.  If the
    # terminal action wins it returns the timeout result itself, so recovery
    # correctly has no independent result to append.
    assert 1 <= len(outcomes) <= 2
    assert all(result["status"] == "not_accepted" for result in outcomes)
    assert final["call_state"] == "not_accepted"
    assert final["owner_device_id"] is None
    assert final["owner_session_id"] is None
    assert len(events) == 1
    assert events[0]["status"] == "not_accepted"
    assert events[0]["reason"] == "ring_timeout"
    assert events[0]["event_seq"] == 1

    reopened = CallCoordinator(database, clock=clock)
    late_accept = reopened.accept_call(
        AcceptCall(
            call_id=created["call_id"],
            action_id="accept-after-restart",
            device_id="desktop-registered",
            session_id="late-session",
            handled_on="desktop",
            terminal_platform="windows",
        )
    )
    reopened_events = reopened.events_for(created["call_id"])
    reopened.close()

    assert late_accept["status"] == "not_accepted"
    assert late_accept["owner_device_id"] is None
    assert late_accept["owner_session_id"] is None
    assert reopened_events == events


def test_terminal_action_id_replays_exactly_and_changed_request_conflicts_without_new_event(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
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
    """Terminal actions cannot beat a deadline merely because scheduling is late."""
    clock = _MutableClock(datetime(2020, 1, 1, tzinfo=UTC))
    database = tmp_path / "expiry-actions.sqlite3"
    coordinator = CallCoordinator(database, clock=clock)
    created = coordinator.create_call(
        CreateCall("operator-default", "agent-alpha", "expiry-actions", "create", "old", 1)
    )
    clock.advance(seconds=1)
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def accept() -> None:
        barrier.wait()
        outcomes.append(
            coordinator.accept_call(
                AcceptCall(
                    call_id=created["call_id"],
                    action_id="late-accept",
                    device_id="desktop-registered",
                    session_id="late-session",
                    handled_on="desktop",
                    terminal_platform="windows",
                )
            )
        )

    def decline() -> None:
        barrier.wait()
        outcomes.append(
            coordinator.decline_call(
                DeclineCall(
                    call_id=created["call_id"],
                    action_id="late-decline",
                    actor_id="desktop-registered",
                )
            )
        )

    first, second = Thread(target=accept), Thread(target=decline)
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()
    final = coordinator.get_call(created["call_id"])
    events = coordinator.events_for(created["call_id"])
    coordinator.close()

    assert len(outcomes) == 2
    assert all(result["status"] == "not_accepted" for result in outcomes)
    assert final["call_state"] == "not_accepted"
    assert final["owner_device_id"] is None
    assert final["owner_session_id"] is None
    assert len(events) == 1
    assert events[0]["status"] == "not_accepted"
    assert events[0]["reason"] == "ring_timeout"
    assert events[0]["event_seq"] == 1

    reopened = CallCoordinator(database, clock=clock)
    late_accept = reopened.accept_call(
        AcceptCall(
            call_id=created["call_id"],
            action_id="late-accept-after-restart",
            device_id="desktop-registered",
            session_id="restart-session",
            handled_on="desktop",
            terminal_platform="windows",
        )
    )
    assert late_accept["status"] == "not_accepted"
    assert reopened.events_for(created["call_id"]) == events
    reopened.close()


def test_terminal_endpoint_actions_reject_expired_accept_decline_race_without_revival(
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
    _wait_for_tls_gateway(process, port, certificate)
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


class _MutableClock(_FrozenClock):
    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)
