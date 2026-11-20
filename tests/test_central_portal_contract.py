"""Contract tests for Central Portal: the shell-side HITL client.

The portal's job is to remove the LLM from the keepalive loop, so its two
critical contracts are:

* the **continuation loop** — every parked leg carries exactly
  interaction_id + call_id + the forward-only ``after_event_seq`` cursor,
  never the original prompt fields, until ``needs_continuation`` clears;
* the **request-id discipline** — a transport retry re-sends the same
  jsonrpc id (the central replays the recorded outcome), while a busy line
  is re-created under a fresh id (a retry under the old id would replay the
  busy outcome forever).
"""

from __future__ import annotations

import importlib
import json

import pytest


def _module():
    return importlib.import_module("central_portal")


class FakeTransport:
    """Scripts JSON-RPC responses; records every request for assertions."""

    def __init__(self, script):
        # script: list of ("headers", body) or Exception instances to raise
        self.script = list(script)
        self.requests: list[dict] = []
        self.headers = {"mcp-session-id": "sess-fake"}

    def send(self, body, session, read_timeout):
        tick = getattr(self, "on_send", None)
        if callable(tick):
            tick()
        self.requests.append({"body": dict(body), "session": session,
                              "read_timeout": read_timeout})
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _rpc_result(structured: dict, text: str = "") -> tuple[dict, dict]:
    return {"mcp-session-id": "sess-fake"}, {
        "jsonrpc": "2.0", "id": 1, "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": structured,
        }}


def _init_result() -> tuple[dict, dict]:
    return {"mcp-session-id": "sess-fake"}, {
        "jsonrpc": "2.0", "id": 1,
        "result": {"protocolVersion": "2025-06-18", "capabilities": {}},
    }


def _portal(script, sleeper=None, clock=None) -> tuple:
    m = _module()
    transport = FakeTransport([_init_result(), *script])
    portal = m.Portal(transport, announce=lambda *_: None,
                      sleeper=sleeper or (lambda seconds: None),
                      clock=clock or (lambda: 0.0))
    portal.initialize()
    return m, portal, transport


def _heartbeat(iid="I1", cid="C1", seq=None) -> tuple[dict, dict]:
    structured = {"status": "heartbeat", "needs_continuation": True,
                  "interaction_id": iid, "call_id": cid}
    if seq is not None:
        structured["event_seq"] = seq
        structured["next_after_event_seq"] = seq
    return _rpc_result(structured)


def _terminal(status="answered", **extra) -> tuple[dict, dict]:
    structured = {"status": status, "needs_continuation": False, **extra}
    return _rpc_result(structured, text=f"{status} summary")


# ── argument parsing ───────────────────────────────────────────────────────

def test_parse_value_json_when_it_parses_else_literal() -> None:
    m = _module()
    assert m.parse_value("true") is True
    assert m.parse_value("42") == 42
    assert m.parse_value('["上午","下午"]') == ["上午", "下午"]
    assert m.parse_value('{"a": 1}') == {"a": 1}
    assert m.parse_value("hello world") == "hello world"
    assert m.parse_value("0042x") == "0042x"
    # the documented escape for a literal string that would parse as JSON
    assert m.parse_value('"true"') == "true"


def test_parse_arg_splits_on_first_equals_and_validates_key() -> None:
    m = _module()
    assert m.parse_arg("prompt=现在方便吗？") == ("prompt", "现在方便吗？")
    assert m.parse_arg("choices=[1,2]") == ("choices", [1, 2])
    with pytest.raises(ValueError):
        m.parse_arg("novalue")
    with pytest.raises(ValueError):
        m.parse_arg("bad-key=1")


def test_build_arguments_merges_json_then_flags_and_warns_unknown() -> None:
    m = _module()
    warnings: list[str] = []
    args = m.build_arguments(
        "get_user_input", ["prompt=hi", "required=true"],
        '{"prompt": "overridden"}', warnings.append)
    assert args == {"prompt": "hi", "required": True}
    args = m.build_arguments("get_user_input", ["bogus=1"], None, warnings.append)
    assert args == {"bogus": 1}
    assert any("bogus" in w for w in warnings)


# ── cursor policy ──────────────────────────────────────────────────────────

def test_cursor_advances_on_event_and_holds_on_heartbeat() -> None:
    m = _module()
    cursor = m.adopt_cursor(None, {"status": "heartbeat"})
    assert cursor == 0                       # never invented, never negative
    cursor = m.adopt_cursor(cursor, {"event_seq": 3, "next_after_event_seq": 3})
    assert cursor == 3                       # event advances
    cursor = m.adopt_cursor(cursor, {"status": "heartbeat"})  # plain heartbeat holds
    assert cursor == 3
    cursor = m.adopt_cursor(cursor, {"event_seq": 7, "next_after_event_seq": 7})
    assert cursor == 7


# ── the continuation loop ──────────────────────────────────────────────────

def test_immediate_terminal_outcome_in_one_leg() -> None:
    m, portal, transport = _portal([_terminal("answered", result={"text": "好"})])
    out = portal.call("get_user_input", {"prompt": "方便吗？"}, leg_seconds=60)
    assert out["outcome"]["status"] == "answered"
    assert out["outcome"]["result"] == {"text": "好"}
    assert out["summary"] == "answered summary"
    tools_calls = [r for r in transport.requests if r["body"].get("method") == "tools/call"]
    assert len(tools_calls) == 1
    arguments = tools_calls[0]["body"]["params"]["arguments"]
    assert arguments["prompt"] == "方便吗？"
    assert arguments["client_timeout_seconds"] == 60


def test_continuation_legs_carry_only_round_fields_and_forward_cursor() -> None:
    script = [
        _heartbeat(seq=0),                       # parked, no pickup
        _heartbeat(seq=3),                       # event: accepted
        _rpc_result({"status": "in_progress", "needs_continuation": True,
                     "interaction_id": "I1", "call_id": "C1",
                     "event_seq": 5, "next_after_event_seq": 5,
                     "result": {"note": "进行中"}}),
        _terminal("completed", result={"report": "done"}),
    ]
    m, portal, transport = _portal(script)
    out = portal.call("assign_task_to_human", {"task_description": "拍照"}, leg_seconds=60)
    assert out["outcome"]["status"] == "completed"
    calls = [r for r in transport.requests if r["body"].get("method") == "tools/call"]
    assert len(calls) == 4
    # create carries the task; every continuation carries ONLY round fields
    assert calls[0]["body"]["params"]["arguments"]["task_description"] == "拍照"
    for call, expected_cursor in zip(calls[1:], (0, 3, 5)):
        arguments = call["body"]["params"]["arguments"]
        assert set(arguments) == {"interaction_id", "call_id",
                                  "after_event_seq", "client_timeout_seconds"}
        assert arguments["interaction_id"] == "I1"
        assert arguments["after_event_seq"] == expected_cursor
    # every logical leg used a fresh jsonrpc id
    ids = [c["body"]["id"] for c in calls]
    assert len(set(ids)) == len(ids)


def test_transport_failure_retries_the_same_request_id() -> None:
    m = _module()
    failure = m.TransportError("connection reset")
    m, portal, transport = _portal([failure, _terminal("answered")])
    sleeps: list[float] = []
    portal.sleep = sleeps.append
    out = portal.call("get_user_input", {"prompt": "?"}, leg_seconds=60)
    assert out["outcome"]["status"] == "answered"
    calls = [r for r in transport.requests if r["body"].get("method") == "tools/call"]
    assert len(calls) == 2
    assert calls[0]["body"]["id"] == calls[1]["body"]["id"]       # idempotent retry
    assert calls[0]["body"]["params"] == calls[1]["body"]["params"]
    assert sleeps and sleeps[0] > 0


def test_create_leg_gives_up_after_bounded_retries_but_open_rounds_do_not() -> None:
    m = _module()
    failure = m.TransportError("connection reset")
    # CREATE: five transport failures exhaust the cap (no round owes patience).
    m2, portal2, transport2 = _portal([failure] * 8)
    with pytest.raises(m2.PortalError) as excinfo:
        portal2.call("health_check", {}, leg_seconds=30)
    assert excinfo.value.exit_code == m2.EXIT_TRANSPORT
    assert excinfo.value.payload["error"] == "unreachable"
    # Once a round is OPEN (first leg answered with a heartbeat), transport
    # failures retry without a cap: the round must not be orphaned.
    m3, portal3, transport3 = _portal([_heartbeat(seq=0), failure, failure,
                                       _terminal("answered")])
    out = portal3.call("get_user_input", {"prompt": "?"}, leg_seconds=60)
    assert out["outcome"]["status"] == "answered"


def test_unauthorized_aborts_without_retry() -> None:
    m = _module()
    script = [m.TransportError("HTTP 401", status=401)]
    m, portal, transport = _portal(script)
    sleeps: list[float] = []
    portal.sleep = sleeps.append
    with pytest.raises(m.PortalError) as excinfo:
        portal.call("health_check", {}, leg_seconds=30)
    assert excinfo.value.exit_code == m.EXIT_TRANSPORT
    assert excinfo.value.payload["error"] == "unauthorized"
    assert not sleeps


def test_busy_retries_with_a_fresh_request_id_then_gets_through() -> None:
    busy = _rpc_result({"status": "busy", "success": False,
                        "needs_continuation": False, "retry_after_seconds": 5})
    m, portal, transport = _portal([busy, busy, _terminal("answered")])
    sleeps: list[float] = []
    portal.sleep = sleeps.append
    out = portal.call("get_user_input", {"prompt": "?"}, leg_seconds=60,
                      busy_wait=100)
    assert out["outcome"]["status"] == "answered"
    calls = [r for r in transport.requests if r["body"].get("method") == "tools/call"]
    assert len(calls) == 3
    ids = [c["body"]["id"] for c in calls]
    assert ids[0] != ids[1] != ids[2]       # busy must re-create, never replay
    assert sleeps == [5.0, 5.0]


def test_busy_gives_up_after_the_budget_and_reports_the_line() -> None:
    busy = _rpc_result({"status": "busy", "success": False,
                        "needs_continuation": False, "retry_after_seconds": 60})
    m, portal, transport = _portal([busy] * 5)

    now = {"t": 0.0}
    def clock() -> float:
        return now["t"]
    def sleeper(seconds: float) -> None:
        now["t"] += seconds                 # time advances with each nap
    portal.clock = clock
    portal.sleep = sleeper

    with pytest.raises(m.PortalError) as excinfo:
        portal.call("get_user_input", {"prompt": "?"}, leg_seconds=60,
                    busy_wait=10)
    assert excinfo.value.exit_code == m.EXIT_BUSY
    assert excinfo.value.payload["busy"] is True
    assert "list_my_interactions" in excinfo.value.payload["hint"]


def test_max_wait_expiry_with_open_round_returns_resume_handle() -> None:
    m, portal, transport = _portal([_heartbeat(seq=2)] * 10)

    now = {"t": 0.0}
    def clock() -> float:
        return now["t"]
    def sleeper(seconds: float) -> None:
        now["t"] += seconds
    portal.clock = clock
    portal.sleep = sleeper
    transport.on_send = lambda: now.__setitem__("t", now["t"] + 1.0)  # each leg takes time

    with pytest.raises(m.PortalError) as excinfo:
        portal.call("assign_task_to_human", {"task_description": "x"},
                    leg_seconds=60, max_wait=5)
    error = excinfo.value
    assert error.exit_code == m.EXIT_DEADLINE
    resume = error.payload["resume"]
    assert resume["interaction_id"] == "I1"
    assert resume["call_id"] == "C1"
    assert resume["after_event_seq"] == 2
    assert resume["command"].startswith("central-portal call assign_task_to_human --resume")


def test_resume_round_starts_from_the_continuation_leg() -> None:
    m, portal, transport = _portal([_heartbeat(seq=9), _terminal("completed")])
    out = portal.call("assign_task_to_human", {}, resume=("I7", "C7", 4),
                      leg_seconds=60)
    assert out["outcome"]["status"] == "completed"
    calls = [r for r in transport.requests if r["body"].get("method") == "tools/call"]
    arguments = calls[0]["body"]["params"]["arguments"]
    assert arguments == {"interaction_id": "I7", "call_id": "C7",
                         "after_event_seq": 4, "client_timeout_seconds": 60}
    # next leg adopts the NEW event cursor, not the resume cursor
    nxt = calls[1]["body"]["params"]["arguments"]
    assert nxt["after_event_seq"] == 9


# ── configuration ──────────────────────────────────────────────────────────

def test_load_config_tolerates_missing_and_broken_files(tmp_path) -> None:
    m = _module()
    assert m.load_config(tmp_path / "absent.json") == {}
    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert m.load_config(broken) == {}
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"url": "https://x:1", "token": "t"}), encoding="utf-8")
    assert m.load_config(good) == {"url": "https://x:1", "token": "t"}


# ── embedded catalog ───────────────────────────────────────────────────────

def test_embedded_catalog_covers_the_full_tool_family() -> None:
    m = _module()
    names = {t["name"] for t in m._CATALOG}
    expected = {
        "get_user_input", "get_number_input", "get_user_choice",
        "get_multiline_input", "show_confirmation_dialog", "show_info_message",
        "get_feedback_from_user", "assign_task_to_human",
        "read_interaction_result", "read_interaction_attachment",
        "list_operators", "get_operator_profile", "health_check",
        "get_human_loop_prompt", "list_my_interactions", "cancel_interaction",
    }
    assert names == expected
    for tool in m._CATALOG:
        assert tool["description"] and "fields" in tool and "examples" in tool
