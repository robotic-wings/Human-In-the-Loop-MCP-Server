"""Regression tests for the agent-feedback fixes (P0/P1/P2).

P0 — read_interaction_attachment/read_interaction_result must be reachable
     by their globally-unique id ALONE; a round that completes in one shot
     never surfaces interaction_id, so requiring it made attachments
     unreadable after the round ended.
P1 — a JSON-RPC long-poll that delivers an opened/in_progress heartbeat arms
     the agent-reconnect deadline, so an aborted parked call settles cleanly
     as agent_disconnected instead of holding the busy line.
P2 — continuation needs only the cursor fields; the original payload is not
     re-sent or required.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path

from tests.test_hitl_functional_integration import Central, TerminalSession


def _complete_with_attachment(central: Central, term: TerminalSession, content: bytes,
                              filename: str = "证据.png") -> tuple[dict[str, object], str]:
    call = central.agent_create("regress")
    term.ringed(call).accept()
    uploaded = term.upload(filename, "application/octet-stream", content)
    attachment_id = str(uploaded["attachment_id"])
    final = term.submit(
        {"status": "completed", "report": "见图", "attachments": [attachment_id]},
        submission_id="regress-final",
    )
    return call, attachment_id, final


def test_read_attachment_by_id_alone_after_completion(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call, attachment_id, final = _complete_with_attachment(central, term, b"\x89PNG real bytes")
    assert final["call_state"] == "completed"

    read = central.agent_rpc("read_interaction_attachment", {"attachment_id": attachment_id})
    sc = read["result"]["structuredContent"]
    assert sc["success"] is True, sc
    assert base64.b64decode(sc["data"]) == b"\x89PNG real bytes"
    assert sc["has_more"] is False


def test_read_attachment_with_mismatched_interaction_id_rejected(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    _call, attachment_id, _final = _complete_with_attachment(central, term, b"x")

    read = central.agent_rpc("read_interaction_attachment", {
        "attachment_id": attachment_id, "interaction_id": "bogus-interaction",
    })
    assert read["result"]["structuredContent"]["status"] == "session_not_found"


def test_read_attachment_scoped_to_caller(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    _call, attachment_id, _final = _complete_with_attachment(central, term, b"secret")
    foreign = central.agent_rpc("read_interaction_attachment", {"attachment_id": attachment_id}, token="agent-b-secret")
    assert foreign["result"]["structuredContent"]["status"] == "session_not_found"


def test_read_oversized_result_by_result_id_alone(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    big = "z" * 40_000
    call = central.agent_create("regress-big", max_result_bytes=16_384)
    term.ringed(call).accept()
    final = term.submit({"status": "completed", "report": big}, submission_id="big-final")
    result_id = final["result_id"]

    read = central.agent_rpc("read_interaction_result", {"result_id": result_id})
    sc = read["result"]["structuredContent"]
    assert sc["success"] is True, sc
    assert sc["result"]["report"] == big


def test_terminal_summary_exposes_round_ids(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("regress-summary")
    term.ringed(call).accept()
    term.submit({"status": "completed", "report": "done"}, submission_id="s")

    # JSON-RPC continuation reads the completed event (after the opened one).
    resp = central.request("POST", "/mcp", bearer="agent-secret", body={
        "jsonrpc": "2.0", "id": 9, "method": "tools/call",
        "params": {"name": "assign_task_to_human",
                   "arguments": {"interaction_id": call["interaction_id"], "call_id": call["call_id"],
                                 "after_event_seq": 1, "client_timeout_seconds": 5}},
    })
    content = resp.json()["result"]["content"][0]["text"]
    assert f"interaction_id={call['interaction_id']}" in content, content
    assert f"call_id={call['call_id']}" in content, content


def test_aborted_round_settles_cleanly(tmp_path: Path) -> None:
    central = Central(tmp_path, agent_reconnect_seconds=30)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("regress-abort")
    term.ringed(call).accept()

    # The agent's continuation delivers the opened heartbeat, which arms the
    # reconnect deadline. There is no further poll (the client aborted).
    resp = central.request("POST", "/mcp", bearer="agent-secret", body={
        "jsonrpc": "2.0", "id": 7, "method": "tools/call",
        "params": {"name": "assign_task_to_human",
                   "arguments": {"interaction_id": call["interaction_id"], "call_id": call["call_id"],
                                 "after_event_seq": 0, "client_timeout_seconds": 5}},
    })
    assert resp.json()["result"]["structuredContent"]["call_state"] == "accepted"

    # Deadline is armed (non-null) after delivering the opened heartbeat.
    import sqlite3
    with sqlite3.connect(central.path / "lifecycle.sqlite3") as conn:
        row = conn.execute(
            "SELECT agent_reconnect_deadline FROM lifecycle_calls WHERE call_id=?", (call["call_id"],)
        ).fetchone()
    assert row[0] is not None, "opened heartbeat must arm the agent-reconnect deadline"

    central.clock.advance(31)
    outcome, _ = central.agent_follow(call, prefix="regress-after")
    assert outcome["status"] == "disconnected"
    assert outcome["reason"] == "agent_disconnected"
    # busy line released
    assert central.agent_create("regress-next")["call_state"] == "ringing"


def test_continuation_needs_only_cursor_fields(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    call = central.agent_create("regress-cont")

    # Continuation with ONLY the cursor fields — no task_description.
    resp = central.request("POST", "/mcp", bearer="agent-secret", body={
        "jsonrpc": "2.0", "id": 8, "method": "tools/call",
        "params": {"name": "assign_task_to_human",
                   "arguments": {"interaction_id": call["interaction_id"], "call_id": call["call_id"],
                                 "after_event_seq": 0, "client_timeout_seconds": 5}},
    })
    assert resp.status_code == 200
    sc = resp.json()["result"]["structuredContent"]
    assert sc["call_state"] == "ringing" and sc["needs_continuation"] is True
