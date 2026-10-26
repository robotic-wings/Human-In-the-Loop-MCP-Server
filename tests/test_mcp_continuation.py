"""Red contracts for nonblocking MCP continuation and ringing settlement only.

This slice deliberately excludes long waits, reconnect grace, human terminals,
notifications, submissions, cancellation, tasks, revisions, and browser APIs.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from typing import Any

import pytest

from call_coordinator import CallCoordinator, CreateCall, DeclineCall
from test_central_mcp_gateway import (
    HUMAN_TOOLS,
    _call,
    _post,
    _rpc,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
    _free_port,
)


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def _clock() -> FakeClock:
    return FakeClock(datetime(2030, 1, 1, tzinfo=UTC))


def _create(request_id: str = "create") -> CreateCall:
    return CreateCall(
        operator_id="operator-default",
        caller_id="agent-alpha",
        interaction_id="interaction-alpha",
        request_id=request_id,
        message="Need a durable event.",
        ring_timeout_seconds=60,
    )


def _continuation(
    request_id: str,
    interaction_id: str,
    call_id: str,
    after_event_seq: int,
) -> dict[str, Any]:
    return _rpc(
        "tools/call",
        {
            "name": "get_user_input",
            "arguments": {
                "title": "Need an answer",
                "prompt": "Which option should we use?",
                "interaction_id": interaction_id,
                "call_id": call_id,
                "after_event_seq": after_event_seq,
                "request_id": request_id,
            },
        },
        40,
    )


def test_all_human_tool_schemas_advertise_continuation_identifiers_over_tls(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        status, response, headers_in = _post(
            port, certificate, _rpc("tools/list", {}), "test-token-alpha"
        )
    finally:
        _stop(process)

    assert status == 200
    assert headers_in < 1
    schemas = {
        item["name"]: item["inputSchema"]
        for item in response["result"]["tools"]
        if item["name"] in HUMAN_TOOLS
    }
    assert set(schemas) == HUMAN_TOOLS
    for schema in schemas.values():
        properties = schema["properties"]
        assert properties["call_id"] == {"type": "string"}
        assert properties["after_event_seq"] == {"type": "integer", "minimum": 0}


def test_known_call_requires_interaction_and_call_identifiers_at_tls_boundary(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, created_response, _ = _post(
            port, certificate, _call("create"), "test-token-alpha"
        )
        created = _structured(created_response)
        missing_call = _call("continuation-missing-call")
        missing_call["params"]["arguments"]["interaction_id"] = created[
            "interaction_id"
        ]
        missing_call["params"]["arguments"]["after_event_seq"] = 0
        _, without_call, _ = _post(
            port, certificate, missing_call, "test-token-alpha"
        )
        missing_interaction = _continuation(
            "continuation-missing-interaction", "", created["call_id"], 0
        )
        _, without_interaction, _ = _post(
            port, certificate, missing_interaction, "test-token-alpha"
        )
    finally:
        _stop(process)

    assert without_call["error"]["code"] == -32602
    assert without_interaction["error"]["code"] == -32602


def test_authenticated_continuation_heartbeat_replays_without_extending_deadline(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, created_response, _ = _post(
            port, certificate, _call("create"), "test-token-alpha"
        )
        created = _structured(created_response)
        request = _continuation(
            "continue-1", created["interaction_id"], created["call_id"], 0
        )
        _, first, _ = _post(port, certificate, request, "test-token-alpha")
        _, replay, _ = _post(port, certificate, request, "test-token-alpha")
    finally:
        _stop(process)

    heartbeat = _structured(first)
    assert heartbeat == _structured(replay)
    assert heartbeat["success"] is True
    assert heartbeat["status"] == "heartbeat"
    assert heartbeat["call_state"] == "ringing"
    assert heartbeat["interaction_id"] == created["interaction_id"]
    assert heartbeat["call_id"] == created["call_id"]
    assert heartbeat["event_seq"] == 0
    assert heartbeat["needs_continuation"] is True


def test_wrong_and_unknown_callers_receive_the_same_nonrevealing_continuation_error(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, created_response, _ = _post(
            port, certificate, _call("create"), "test-token-alpha"
        )
        created = _structured(created_response)
        wrong = _continuation(
            "wrong-caller", created["interaction_id"], created["call_id"], 0
        )
        unknown = _continuation(
            "unknown-call", "no-such-interaction", "no-such-call", 0
        )
        _, wrong_response, _ = _post(port, certificate, wrong, "test-token-beta")
        _, unknown_response, _ = _post(
            port, certificate, unknown, "test-token-beta"
        )
    finally:
        _stop(process)

    assert wrong_response == unknown_response
    assert "structuredContent" not in wrong_response.get("result", {})


def test_continuation_returns_earliest_event_and_survives_a_lost_response(
    tmp_path: Path,
) -> None:
    clock = _clock()
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    created = service.create_call(_create())
    call_id = created["call_id"]
    service.decline_call(DeclineCall(call_id, "decline", "operator-default"))

    first = service.continue_call(
        "agent-alpha", "read-1", "interaction-alpha", call_id, 0, "{}"
    )
    lost_response_retry = service.continue_call(
        "agent-alpha", "read-2", "interaction-alpha", call_id, 0, "{}"
    )

    assert first["event_seq"] == 1
    assert first["status"] == "declined"
    assert lost_response_retry == first


def test_continuation_rejects_over_max_cursor_and_wrong_caller_without_leakage(
    tmp_path: Path,
) -> None:
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=_clock())
    created = service.create_call(_create())
    call_id = created["call_id"]
    service.decline_call(DeclineCall(call_id, "decline", "operator-default"))

    too_far = service.continue_call(
        "agent-alpha", "cursor-too-far", "interaction-alpha", call_id, 2, "{}"
    )
    wrong = service.continue_call(
        "agent-beta", "wrong", "interaction-alpha", call_id, 0, "{}"
    )
    unknown = service.continue_call(
        "agent-beta", "unknown", "missing", "missing", 0, "{}"
    )

    assert too_far == {
        "success": False,
        "status": "invalid_cursor",
        "needs_continuation": False,
    }
    assert wrong == unknown


def test_manual_scheduler_settles_absolute_ringing_deadline_without_mcp_request(
    tmp_path: Path,
) -> None:
    from central_service import RingingDeadlineScheduler

    clock = _clock()
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    created = service.create_call(_create())
    scheduler = RingingDeadlineScheduler(service, clock)
    scheduler.tick()
    assert service.get_call(created["call_id"])["call_state"] == "ringing"

    clock.advance(60)
    scheduler.tick()
    settled = service.events_for(created["call_id"])
    assert len(settled) == 1
    assert settled[0]["status"] == "not_accepted"
    assert settled[0]["reason"] == "ring_timeout"


def test_restart_and_scheduler_action_race_create_one_terminal_event_no_revival(
    tmp_path: Path,
) -> None:
    clock = _clock()
    service = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    created = service.create_call(_create())
    call_id = created["call_id"]
    service.close()
    clock.advance(60)

    reopened = CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)
    reopened.recover_and_settle()
    assert reopened.get_call(call_id)["call_state"] == "not_accepted"
    reopened.close()

    race_dir = tmp_path / "race"
    race_dir.mkdir()
    race_clock = _clock()
    race = CallCoordinator(race_dir / "calls.sqlite3", clock=race_clock)
    racing = race.create_call(_create())
    race_clock.advance(60)
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def settle() -> None:
        barrier.wait()
        outcomes.extend(race.recover_and_settle())

    def decline() -> None:
        barrier.wait()
        outcomes.append(
            race.decline_call(
                DeclineCall(racing["call_id"], "race-decline", "operator-default")
            )
        )

    first, second = Thread(target=settle), Thread(target=decline)
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()

    current = race.get_call(racing["call_id"])
    assert current["call_state"] in {"declined", "not_accepted"}
    assert len(race.events_for(racing["call_id"])) == 1
    assert all(item["call_state"] == current["call_state"] for item in outcomes)
