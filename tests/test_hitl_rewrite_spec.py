"""The rewrite's new contract — the model the old code gets wrong.

These tests pin the ownership/liveness model that replaces the scattered
deadline columns. They are RED against the pre-rewrite code and GREEN against
the rewrite. They encode the two accidents observed in production:

1. A mobile (iPhone-web) accepted round must NOT be settled as
   ``terminal_disconnected`` while the human is typing — the browser is
   stateless and has no "disconnect"; there is no desktop-terminal lease to
   expire.
2. The same mobile round must still settle as ``agent_disconnected`` when the
   agent abandons it (its own lease expires).
"""

from __future__ import annotations

from pathlib import Path

from tests.test_hitl_functional_integration import Central


def test_mobile_accepted_round_not_settled_by_terminal_disconnect(tmp_path: Path) -> None:
    """A human typing on the phone must outlive the desktop-terminal lease."""
    central = Central(tmp_path, agent_reconnect_seconds=30)
    call = central.agent_create("mobile-typing")

    # Accept on the phone (browser session).
    accepted = central.request("POST", f"/calls/{call['call_id']}/accept",
                               browser="phone-night", body={"operation_id": "mobile-acc"})
    assert accepted.status_code == 200
    assert accepted.json()["call_state"] == "accepted"

    # The human types for a long time — far beyond any desktop-terminal lease.
    central.clock.advance(400)

    # No terminal_disconnected: the browser is stateless and the round is
    # still open for the human to submit. A single read must show it open
    # (not settled); there is no terminal event to "follow" to.
    outcome = central.agent_continue("mobile-read", call, after=0)
    assert outcome["call_state"] in ("accepted", "in_progress"), outcome
    assert outcome["status"] != "disconnected", outcome

    # The human can still finish.
    final = central.request("POST", f"/calls/{call['call_id']}/submissions",
                            browser="phone-night",
                            body={"submission_id": "mobile-sub",
                                  "result": {"status": "completed", "report": "改完"}})
    assert final.status_code == 200
    assert final.json()["call_state"] == "completed"


def test_mobile_round_still_settles_on_agent_abandon(tmp_path: Path) -> None:
    """Mobile ownership removes the desktop lease, not the agent lease."""
    central = Central(tmp_path, agent_reconnect_seconds=30)
    call = central.agent_create("mobile-abandon")

    central.request("POST", f"/calls/{call['call_id']}/accept",
                    browser="phone-night", body={"operation_id": "mobile-acc"})

    # The agent receives the opened heartbeat once, then never polls again
    # (abandoned). Its lease expires.
    central.request("POST", "/mcp", bearer="agent-secret", body={
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": "assign_task_to_human",
                   "arguments": {"interaction_id": call["interaction_id"],
                                 "call_id": call["call_id"], "after_event_seq": 0,
                                 "client_timeout_seconds": 5}},
    })
    central.clock.advance(31)

    outcome, _ = central.agent_follow(call, prefix="mobile-abandon-read")
    assert outcome["status"] == "disconnected"
    assert outcome["reason"] == "agent_disconnected"
    # busy line released
    assert central.agent_create("mobile-next")["call_state"] == "ringing"


def test_desktop_round_still_settles_on_terminal_disconnect(tmp_path: Path) -> None:
    """The desktop channel KEEPS its terminal lease — only mobile loses it."""
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("desktop-disconnect")
    term.ringed(call).accept()

    central.clock.advance(181)  # past terminal_disconnect default 180s
    outcome, _ = central.agent_follow(call, prefix="desktop-read")
    assert outcome["status"] == "disconnected"
    assert outcome["reason"] == "terminal_disconnected"
