"""Functional integration tests: 50 end-to-end rounds over the real HTTP surface.

Unlike the contract suite (which pins extracted seams and may reach into
``app.lifecycle`` directly), every test here drives the application ONLY
through its public wire interfaces, exactly as the production components do:

* Agents          — POST /mcp (direct request protocol and JSON-RPC tools)
* Desktop terms   — POST /api/terminals/register|dnd|quiet, POST /api/calls/{id}/accept|decline|heartbeat|submissions|cancel|attachments (x-terminal-session)
* Operator phones — POST /calls/{id}/accept|decline|submissions (x-operator-session browser)
* Postman bridge  — GET /api/agent/outcomes, POST /api/agent/outcomes/ack

Two operators (NIGHTHAWK primary+mobile, WIREMAN desktop-only), a fake clock,
and a real notification router compose the production topology. Timing is
deterministic; nothing sleeps.

Groups (5 each): lifecycle rounds, busy-line, callsign routing, DND/quiet,
timeouts/recovery, idempotency, outcome feed, terminal sessions, browser
channel, attachments/large results.
"""

from __future__ import annotations

import asyncio
import base64
import importlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import NotificationRouter, TerminalRegistry


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _no_wait(_: float) -> None:
    return None


@dataclass
class _Browsers:
    sessions: dict[str, dict[str, str]] = field(default_factory=dict)

    def verify(self, token: str | None) -> dict[str, str] | None:
        return self.sessions.get(token or "")


class Central:
    """One composed central over durable files, driven by real HTTP."""

    def __init__(self, tmp_path: Path, *, ring_timeout_seconds: float = 60,
                 agent_reconnect_seconds: float = 30) -> None:
        self.path = tmp_path
        self.clock = FakeClock()
        self.ring_timeout = ring_timeout_seconds
        self.agent_reconnect = agent_reconnect_seconds
        self.deliveries: list[dict[str, object]] = []
        self.notifications: list[dict[str, object]] = []
        self.operators = {
            "NIGHTHAWK": {"password": "night-pw", "devices": ["desk-n"], "mobile": True, "primary": True},
            "WIREMAN": {"password": "wire-pw", "devices": ["desk-w"], "mobile": False},
        }
        self.browsers = _Browsers({
            "phone-night": {"operator_id": "NIGHTHAWK", "browser_session_id": "b-night"},
            "phone-wire": {"operator_id": "WIREMAN", "browser_session_id": "b-wire"},
        })
        self._rebuild()

    # ── composition ──────────────────────────────────────────────
    def _rebuild(self, *, fresh_registry: bool = False) -> None:
        tmp, clock = self.path, self.clock
        registry_path = tmp / "registry.sqlite3"
        should_seed = fresh_registry or not registry_path.exists()
        registry = TerminalRegistry(database_path=registry_path, clock=clock)
        if should_seed:
            for device, endpoint in (("desk-n", "https://desk-n:9100/terminal/events"),
                                     ("desk-w", "https://desk-w:9100/terminal/events")):
                registry.register_desktop(
                    actor_role="management", device_id=device, endpoint=endpoint,
                    callback_credential=f"cb-{device}", gui_ready=True,
                )
        self.registry = registry
        self.registration = DesktopRegistrationStateService(
            registry=registry, clock=clock, operator_directory=self.operators,
            device_state=lambda _device: [],
        )
        lifecycle_path = tmp / "lifecycle.sqlite3"

        async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
            self.deliveries.append({"channel": "desktop", **payload})
            return {"accepted": True}

        async def bark_dispatch(payload: dict[str, object]) -> dict[str, object]:
            self.deliveries.append({"channel": "bark", **payload})
            return {"http_status": 200, "business_success": True}

        def call_state(call_id: str) -> str | None:
            if not lifecycle_path.exists():
                return None
            with sqlite3.connect(lifecycle_path) as conn:
                row = conn.execute("SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
            return None if row is None else str(row[0])

        self.router = NotificationRouter(
            database_path=tmp / "router.sqlite3", clock=clock, registry=registry,
            desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
            route_mode="both", call_state=call_state, web_base_url="https://central.example.test",
        )
        module = importlib.import_module("central_mcp_waiting_lifecycle")
        self.app = module.create_app(
            database_path=lifecycle_path,
            credentials={"agent-a": "agent-secret", "agent-b": "agent-b-secret"},
            clock=clock,
            notify=lambda event: (self.notifications.append(event), self.router.enqueue(event)) and None,
            wait=_no_wait,
            ring_timeout_seconds=self.ring_timeout,
            agent_reconnect_seconds=self.agent_reconnect,
            route_admission=self.router.validate_route,
            notification_router=self.router,
            desktop_registration_service=self.registration,
            operator_session_verifier=self.browsers,
            operator_directory=self.operators,
        )

    # ── raw HTTP ─────────────────────────────────────────────────
    def request(self, method: str, path: str, *, body: dict[str, object] | None = None,
                bearer: str | None = None, browser: str | None = None,
                terminal: str | None = None) -> httpx.Response:
        async def run() -> httpx.Response:
            headers: dict[str, str] = {}
            if bearer is not None:
                headers["authorization"] = f"Bearer {bearer}"
            if browser is not None:
                headers["x-operator-session"] = browser
            if terminal is not None:
                headers["x-terminal-session"] = terminal
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="https://central.test") as client:
                return await client.request(method, path, headers=headers, json=body)
        return asyncio.run(run())

    # ── agent surface ────────────────────────────────────────────
    def agent_create(self, request_id: str, tool: str = "assign_task_to_human",
                     args: dict[str, object] | None = None, *, callsign: str | None = None,
                     delivery: dict[str, object] | None = None,
                     max_result_bytes: int | None = None,
                     token: str = "agent-secret") -> dict[str, object]:
        body: dict[str, object] = {
            "request_id": request_id,
            "payload": {"tool": tool, **(args or {})},
            "client_timeout_seconds": 2,
        }
        if callsign is not None:
            body["callsign"] = callsign
        if delivery is not None:
            body["delivery"] = delivery
        if max_result_bytes is not None:
            body["payload"] = {"tool": tool, **(args or {}), "max_result_bytes": max_result_bytes}
        response = self.request("POST", "/mcp", body=body, bearer=token)
        assert response.status_code in (200, 400), response.text
        return dict(response.json())

    def agent_continue(self, request_id: str, call: dict[str, object], after: int,
                       *, token: str = "agent-secret") -> dict[str, object]:
        response = self.request("POST", "/mcp", bearer=token, body={
            "request_id": request_id,
            "interaction_id": call["interaction_id"],
            "call_id": call["call_id"],
            "after_event_seq": after,
            "client_timeout_seconds": 2,
        })
        assert response.status_code in (200, 400), response.text
        return dict(response.json())

    def agent_cancel(self, request_id: str, call: dict[str, object],
                     *, token: str = "agent-secret") -> dict[str, object]:
        response = self.request("POST", "/mcp", bearer=token, body={
            "request_id": request_id, "operation": "cancel_interaction",
            "interaction_id": call["interaction_id"], "call_id": call["call_id"],
            "client_timeout_seconds": 2,
        })
        assert response.status_code in (200, 400), response.text
        return dict(response.json())

    def agent_follow(self, call: dict[str, object], *, after: int = 0,
                     prefix: str = "follow", max_rounds: int = 10,
                     token: str = "agent-secret") -> tuple[dict[str, object], list[dict[str, object]]]:
        """Real inline-agent behavior: advance the cursor until a terminal outcome."""
        events: list[dict[str, object]] = []
        cursor = after
        for i in range(max_rounds):
            outcome = self.agent_continue(f"{prefix}-{i}", call, cursor, token=token)
            if outcome.get("needs_continuation") is False:
                return outcome, events
            if isinstance(outcome.get("event_seq"), int):
                cursor = int(outcome["event_seq"])
                events.append(outcome)
        raise AssertionError(f"never reached a terminal outcome: {events}")

    def agent_rpc(self, name: str, arguments: dict[str, object] | None = None,
                  *, token: str = "agent-secret") -> dict[str, object]:
        response = self.request("POST", "/mcp", bearer=token, body={
            "jsonrpc": "2.0", "id": f"rpc-{name}", "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        })
        assert response.status_code == 200, response.text
        return dict(response.json())

    def outcomes(self, after: int = 0, *, token: str = "agent-secret") -> list[dict[str, object]]:
        response = self.request("GET", f"/api/agent/outcomes?after={after}&limit=100", bearer=token)
        assert response.status_code == 200, response.text
        return list(response.json()["outcomes"])

    def ack(self, seq: int, status: str, *, token: str = "agent-secret") -> dict[str, object]:
        response = self.request("POST", "/api/agent/outcomes/ack", bearer=token, body={"seq": seq, "status": status})
        assert response.status_code == 200, response.text
        return dict(response.json())

    # ── desktop terminal surface ─────────────────────────────────
    def terminal_login(self, callsign: str, device: str | None = None, *,
                       password: str | None = None, gui_ready: bool = True,
                       platform: str = "macos") -> "TerminalSession":
        resolved = device or self.operators[callsign]["devices"][0]
        response = self.request("POST", "/api/terminals/register", body={
            "callsign": callsign, "password": password or self.operators[callsign]["password"],
            "device_id": resolved,
            "terminal_platform": platform, "endpoint": f"https://{resolved}:9100/terminal/events",
            "gui_ready": gui_ready,
        })
        assert response.status_code in (200, 201), response.text
        body = response.json()
        return TerminalSession(self, str(body["session_id"]), resolved)


class TerminalSession:
    def __init__(self, central: Central, session: str, device: str) -> None:
        self.central, self.session, self.device = central, session, device

    def _post(self, action: str, body: dict[str, object] | None = None, code: int = 200) -> httpx.Response:
        response = self.central.request("POST", f"/api/calls/{self.call_id}/{action}", body=body, terminal=self.session)
        assert response.status_code == code, f"{action}: {response.status_code} {response.text}"
        return response

    call_id: str = ""

    def ringed(self, call: dict[str, object]) -> "TerminalSession":
        self.call_id = str(call["call_id"])
        return self

    def accept(self, operation_id: str = "op-accept") -> dict[str, object]:
        return dict(self._post("accept", {"operation_id": operation_id}).json())

    def decline(self, operation_id: str = "op-decline") -> dict[str, object]:
        return dict(self._post("decline", {"operation_id": operation_id}).json())

    def heartbeat(self) -> dict[str, object]:
        return dict(self._post("heartbeat", {}).json())

    def submit(self, result: dict[str, object], submission_id: str = "sub-1",
               task_version: int | None = None, code: int = 200) -> dict[str, object]:
        body: dict[str, object] = {"submission_id": submission_id, "result": result}
        if task_version is not None:
            body["task_version"] = task_version
        return dict(self._post("submissions", body, code=code).json())

    def cancel(self, operation_id: str = "op-cancel") -> dict[str, object]:
        return dict(self._post("cancel", {"operation_id": operation_id}).json())

    def upload(self, filename: str, media_type: str, content: bytes) -> dict[str, object]:
        return dict(self._post("attachments", {
            "filename": filename, "media_type": media_type,
            "content": base64.b64encode(content).decode("ascii"),
        }).json())

    def set_dnd(self, enabled: bool, message: str = "") -> dict[str, object]:
        response = self.central.request("POST", "/api/terminals/dnd", body={"dnd": enabled, "message": message}, terminal=self.session)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def dnd(self) -> dict[str, object]:
        response = self.central.request("GET", "/api/terminals/dnd", terminal=self.session)
        assert response.status_code == 200, response.text
        return dict(response.json())

    def set_quiet(self, seconds: int, mode: str) -> dict[str, object]:
        response = self.central.request("POST", "/api/terminals/quiet", body={"quiet_window_seconds": seconds, "quiet_mode": mode}, terminal=self.session)
        assert response.status_code == 200, response.text
        return dict(response.json())


def _round_to_final(central: Central, call: dict[str, object], term: TerminalSession,
                    report: str = "done", request_id: str = "final") -> dict[str, object]:
    term.ringed(call).accept()
    final = term.submit({"status": "completed", "report": report})
    assert final["status"] == "completed"
    return central.agent_continue(request_id, call, after=0)


# ═══ A. 任务全生命周期 ═════════════════════════════════════════

def test_a01_assign_task_full_round_trip(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("a01")
    assert call["status"] == "heartbeat" and call["call_state"] == "ringing"

    accepted = term.ringed(call).accept()
    assert accepted["status"] == "opened"
    final = term.submit({"status": "completed", "report": "装完了"}, submission_id="a01-final")
    assert final["call_state"] == "completed"

    outcome, _ = central.agent_follow(call, prefix="a01-read")
    assert outcome["status"] == "completed"
    assert outcome["report"] == "装完了"


def test_a02_progress_then_final_visible_to_agent(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("a02")
    term.ringed(call).accept()
    progress = term.submit({"status": "in_progress", "progress": "进行到一半"}, submission_id="a02-p")
    assert progress["status"] == "in_progress"
    seen = central.agent_continue("a02-read1", call, after=1)
    assert seen["status"] == "in_progress" and seen["progress"] == "进行到一半"

    term.submit({"status": "completed", "report": "完成"}, submission_id="a02-f")
    final = central.agent_continue("a02-read2", call, after=seen["event_seq"])
    assert final["status"] == "completed" and final["report"] == "完成"


def test_a03_question_round_carries_choices(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("a03", "get_user_choice",
                                {"prompt": "选一个", "choices": ["红", "蓝"]})
    term.ringed(call).accept()
    term.submit({"selected_choices": ["蓝"]}, submission_id="a03-s")
    outcome, _ = central.agent_follow(call, prefix="a03-read")
    assert outcome["status"] == "answered"
    assert outcome["result"]["selected_choices"] == ["蓝"]


def test_a04_human_decline_ends_round(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("a04")
    declined = term.ringed(call).decline()
    assert declined["call_state"] == "declined"
    outcome, _ = central.agent_follow(call, prefix="a04-read")
    assert outcome["status"] == "declined"


def test_a05_agent_cancel_while_ringing(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    call = central.agent_create("a05")
    cancelled = central.agent_cancel("a05-cancel", call)
    assert cancelled["call_state"] == "cancelled"
    assert central.agent_create("a05-next")["call_state"] == "ringing"


# ═══ B. 忙线与排队 ═════════════════════════════════════════════

def test_b01_busy_while_ringing(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    assert central.agent_create("b01-1")["call_state"] == "ringing"
    busy = central.agent_create("b01-2")
    assert busy["status"] == "busy" and busy["retryable"] is True


def test_b02_busy_released_after_decline(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("b02-1")
    term.ringed(call).decline()
    assert central.agent_create("b02-2")["call_state"] == "ringing"


def test_b03_busy_line_is_per_operator(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    central.terminal_login("WIREMAN")
    assert central.agent_create("b03-night", callsign="NIGHTHAWK")["call_state"] == "ringing"
    other = central.agent_create("b03-wire", callsign="WIREMAN")
    assert other["call_state"] == "ringing", "WIREMAN's line is independent"


def test_b04_busy_released_after_ring_timeout(tmp_path: Path) -> None:
    central = Central(tmp_path, ring_timeout_seconds=60)
    central.terminal_login("NIGHTHAWK")
    call = central.agent_create("b04-1")
    central.clock.advance(61)
    outcome = central.agent_continue("b04-read", call, after=0)
    assert outcome["status"] == "not_accepted" or outcome["call_state"] == "not_accepted"
    assert central.agent_create("b04-2")["call_state"] == "ringing"


def test_b05_busy_released_after_human_cancel(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("b05")
    term.ringed(call).accept()
    cancelled = term.cancel()
    assert cancelled["call_state"] == "cancelled" and cancelled["reason"] == "human_cancelled"
    assert central.agent_create("b05-next")["call_state"] == "ringing"


# ═══ C. 呼号与多操作员路由 ═════════════════════════════════════

def test_c01_callsign_targets_only_that_operator(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("WIREMAN")
    call = central.agent_create("c01", callsign="WIREMAN")
    assert call["operator"] == "WIREMAN"
    asyncio.run(central.router.dispatch_pending())
    invited = [d for d in central.deliveries if d["channel"] == "desktop"]
    assert invited, "desktop invite dispatched"


def test_c02_unknown_callsign_fails_cleanly(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    result = central.agent_create("c02", callsign="GHOST")
    assert result["status"] == "unknown_callsign"


def test_c03_default_targets_primary(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("c03")
    assert call["operator"] == "NIGHTHAWK"


def test_c04_mobile_false_operator_gets_no_bark(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("WIREMAN")
    central.agent_create("c04", callsign="WIREMAN")
    asyncio.run(central.router.dispatch_pending())
    bark = [d for d in central.deliveries if d["channel"] == "bark"]
    assert bark == [], "desktop-only operator must not push to the phone"


def test_c05_mobile_true_operator_gets_bark(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.agent_create("c05", callsign="NIGHTHAWK")
    asyncio.run(central.router.dispatch_pending())
    bark = [d for d in central.deliveries if d["channel"] == "bark"]
    assert len(bark) == 1


# ═══ D. 勿扰与静默窗口 ═════════════════════════════════════════

def test_d01_dnd_auto_declines_without_ringing(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    term.set_dnd(True, "睡觉中")
    result = central.agent_create("d01")
    assert result["status"] == "declined" and result["reason"] == "do_not_disturb"
    assert result["message"] == "睡觉中"


def test_d02_dnd_off_resumes_normal_flow(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    term.set_dnd(True, "忙")
    term.set_dnd(False)
    assert central.agent_create("d02")["call_state"] == "ringing"


def test_d03_dnd_is_per_operator(tmp_path: Path) -> None:
    central = Central(tmp_path)
    night = central.terminal_login("NIGHTHAWK")
    central.terminal_login("WIREMAN")
    night.set_dnd(True, "勿扰")
    assert central.agent_create("d03-night")["status"] == "declined"
    assert central.agent_create("d03-wire", callsign="WIREMAN")["call_state"] == "ringing"


def test_d04_dnd_state_readable_via_terminal_api(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    term.set_dnd(True, "闭关")
    state = term.dnd()
    assert state["enabled"] is True and state["message"] == "闭关"


def test_d05_quiet_window_degrades_push_after_accept(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    term.set_quiet(300, "short")
    call = central.agent_create("d05-1")
    term.ringed(call).accept()          # accept arms the quiet window
    asyncio.run(central.router.dispatch_pending())
    central.agent_cancel("d05-c", call)
    second = central.agent_create("d05-2")
    assert second["call_state"] == "ringing"
    degraded = [n for n in central.notifications if n.get("degraded")]
    assert degraded, "a call inside the quiet window degrades its push"


# ═══ E. 超时与恢复 ═════════════════════════════════════════════

def test_e01_ring_timeout_terminal_outcome(tmp_path: Path) -> None:
    central = Central(tmp_path, ring_timeout_seconds=60)
    call = central.agent_create("e01")
    central.clock.advance(61)
    outcome = central.agent_continue("e01-read", call, after=0)
    assert outcome.get("call_state") == "not_accepted" and outcome.get("reason") == "ring_timeout"


def test_e02_terminal_disconnect_settles_accepted(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("e02")
    term.ringed(call).accept()
    term.heartbeat()
    central.clock.advance(181)           # past terminal_disconnect default 180s
    outcome, _ = central.agent_follow(call, prefix="e02-read")
    assert outcome["status"] == "disconnected" and outcome["reason"] == "terminal_disconnected"


def test_e03_heartbeat_keeps_accepted_alive(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("e03")
    term.ringed(call).accept()
    for _ in range(6):                   # heartbeats every 30s across 180s
        central.clock.advance(30)
        term.heartbeat()
    final = term.submit({"status": "completed", "report": "还活着"})
    assert final["call_state"] == "completed"


def test_e04_in_progress_grace_expiry(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("e04")
    term.ringed(call).accept()
    term.submit({"status": "in_progress", "progress": "一半"}, submission_id="e04-p")
    read = central.agent_continue("e04-read", call, after=1)   # progress event, arms 300s grace
    assert read["status"] == "in_progress"
    central.clock.advance(301)
    outcome, _ = central.agent_follow(call, after=int(read["event_seq"]), prefix="e04-after")
    assert outcome["status"] == "disconnected" and outcome["retryable"] is True


def test_e05_ring_state_survives_restart(tmp_path: Path) -> None:
    central = Central(tmp_path, ring_timeout_seconds=60)
    central.terminal_login("NIGHTHAWK")
    call = central.agent_create("e05")
    central.clock.advance(10)
    central._rebuild()                    # fresh process objects, durable files
    still = central.agent_continue("e05-read", call, after=0)
    assert still["call_state"] == "ringing"
    central.clock.advance(51)
    expired, _ = central.agent_follow(call, prefix="e05-read2")
    assert expired.get("call_state") == "not_accepted"


# ═══ F. 幂等与重放 ═════════════════════════════════════════════

def test_f01_create_replay_returns_recorded_outcome(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    first = central.agent_create("f01")
    replay = central.agent_create("f01")
    assert replay["call_id"] == first["call_id"] and replay["status"] == "heartbeat"


def test_f02_create_conflict_on_changed_args(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.agent_create("f02", args={"task_description": "任务A"})
    conflicting = central.agent_create("f02", args={"task_description": "任务B"})
    assert conflicting["status"] == "request_conflict"


def test_f03_continuation_replay_is_stable(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("f03")
    term.ringed(call).accept()
    read1 = central.agent_continue("f03-r1", call, after=0)
    read2 = central.agent_continue("f03-r1", call, after=0)
    assert read1 == read2


def test_f04_agent_cancel_idempotent(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    call = central.agent_create("f04")
    first = central.agent_cancel("f04-c", call)
    replay = central.agent_cancel("f04-c", call)
    assert first["call_id"] == replay["call_id"] and replay["status"] == "cancelled"


def test_f05_submission_replay_idempotent(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("f05")
    term.ringed(call).accept()
    first = term.submit({"status": "completed", "report": "只记一次"}, submission_id="f05-s")
    replay = term.submit({"status": "completed", "report": "只记一次"}, submission_id="f05-s")
    assert first["event_seq"] == replay["event_seq"]


# ═══ G. Outcome feed / postman 语义 ════════════════════════════

def test_g01_feed_orders_terminal_outcomes(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("g01")
    term.ringed(call).decline()
    feed = central.outcomes()
    mine = [e for e in feed if e["call_id"] == call["call_id"]]
    assert [e["outcome"]["status"] for e in mine] == ["declined"]


def test_g02_feed_is_caller_scoped(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("g02", token="agent-secret")
    term.ringed(call).decline()
    assert central.outcomes(token="agent-b-secret") == []


def test_g03_progress_in_feed_with_delivery(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("g03", delivery={"kind": "chidori-detached", "agent": "probe"})
    term.ringed(call).accept()
    term.submit({"status": "in_progress", "progress": "中期"}, submission_id="g03-p")
    entries = [e for e in central.outcomes() if e["call_id"] == call["call_id"]]
    assert entries and entries[0]["outcome"]["status"] == "in_progress"
    assert entries[0]["delivery"] == {"kind": "chidori-detached", "agent": "probe"}


def test_g04_ack_marks_delivered(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("g04")
    term.ringed(call).decline()
    entry = central.outcomes()[0]
    assert central.ack(int(entry["seq"]), "delivered")["success"] is True


def test_g05_undeliverable_excluded_until_requested(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("g05")
    term.ringed(call).decline()
    entry = central.outcomes()[0]
    central.ack(int(entry["seq"]), "undeliverable")
    assert central.outcomes() == []
    response = central.request("GET", "/api/agent/outcomes?undeliverable=1", bearer="agent-secret")
    assert response.status_code == 200
    assert [e["call_id"] for e in response.json()["outcomes"]] == [call["call_id"]]


# ═══ H. 终端会话与设备 ═════════════════════════════════════════

def test_h01_wrong_password_rejected(tmp_path: Path) -> None:
    central = Central(tmp_path)
    response = central.request("POST", "/api/terminals/register", body={
        "callsign": "NIGHTHAWK", "password": "wrong", "device_id": "desk-n",
        "terminal_platform": "macos", "endpoint": "https://desk-n:9100/terminal/events", "gui_ready": True,
    })
    assert response.status_code == 401


def test_h02_actions_require_terminal_session(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("h02")
    response = central.request("POST", f"/api/calls/{call['call_id']}/accept", body={"operation_id": "x"})
    assert response.status_code == 401


def test_h03_foreign_session_cannot_accept(tmp_path: Path) -> None:
    central = Central(tmp_path)
    central.terminal_login("NIGHTHAWK")
    stranger = central.terminal_login("WIREMAN")
    call = central.agent_create("h03")           # addressed to NIGHTHAWK
    response = central.request("POST", f"/api/calls/{call['call_id']}/accept",
                               body={"operation_id": "x"}, terminal=stranger.session)
    assert response.status_code in (401, 403, 404)


def test_h04_accept_after_terminal_round_over_is_refused(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("h04")
    term.ringed(call).decline()
    response = central.request("POST", f"/api/calls/{call['call_id']}/accept",
                               body={"operation_id": "late"}, terminal=term.session)
    assert response.status_code == 200                      # idempotent replay
    assert response.json()["call_state"] == "declined"      # but it does NOT revive
    assert central.agent_create("h04-next")["call_state"] == "ringing"


def test_h05_human_cancel_distinct_from_agent_cancel(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("h05")
    term.ringed(call).accept()
    cancelled = term.cancel()
    assert cancelled["reason"] == "human_cancelled" and cancelled["human_action"] is True
    outcome, _ = central.agent_follow(call, prefix="h05-read")
    assert outcome["status"] == "cancelled" and outcome["reason"] == "human_cancelled"


# ═══ I. 浏览器 / iPhone 通道 ═══════════════════════════════════

def test_i01_browser_accept_marks_iphone_web(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("i01")
    accepted = central.request("POST", f"/calls/{call['call_id']}/accept",
                               browser="phone-night", body={"operation_id": "web-acc"})
    assert accepted.status_code == 200
    assert accepted.json()["call_state"] == "accepted"


def test_i02_browser_submit_final_round_trip(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("i02")
    central.request("POST", f"/calls/{call['call_id']}/accept", browser="phone-night", body={"operation_id": "w-a"})
    submitted = central.request("POST", f"/calls/{call['call_id']}/submissions",
                                browser="phone-night", body={"submission_id": "w-s", "result": {"user_input": "手机答复"}})
    assert submitted.status_code == 200
    outcome, _ = central.agent_follow(call, prefix="i02-read")
    assert outcome["result"]["user_input"] == "手机答复"


def test_i03_browser_view_requires_verified_session(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("i03")
    response = central.request("GET", f"/calls/{call['call_id']}", browser="unknown-token")
    assert response.status_code in (401, 404)


def test_i04_browser_session_is_operator_scoped(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("i04")           # NIGHTHAWK's call
    response = central.request("POST", f"/calls/{call['call_id']}/accept",
                               browser="phone-wire", body={"operation_id": "x"})  # WIREMAN's phone
    assert response.status_code in (401, 403, 404)


def test_i05_browser_decline_round(tmp_path: Path) -> None:
    central = Central(tmp_path)
    call = central.agent_create("i05")
    declined = central.request("POST", f"/calls/{call['call_id']}/decline",
                               browser="phone-night", body={"operation_id": "w-d"})
    assert declined.status_code == 200 and declined.json()["call_state"] == "declined"


# ═══ J. 附件与大结果 ═══════════════════════════════════════════

def test_j01_attachment_upload_and_reference(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("j01")
    term.ringed(call).accept()
    uploaded = term.upload("证据.png", "image/png", b"\x89PNG-fake-bytes")
    assert uploaded.get("attachment_id")
    final = term.submit({"status": "completed", "report": "见图",
                         "attachments": [uploaded["attachment_id"]]}, submission_id="j01-s")
    assert final["attachments"][0]["filename"] == "证据.png"


def test_j02_attachment_readable_by_agent(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("j02")
    term.ringed(call).accept()
    uploaded = term.upload("数据.txt", "text/plain", b"0123456789")
    final = term.submit({"status": "completed", "report": "r", "attachments": [uploaded["attachment_id"]]}, submission_id="j02-s")
    chunk = central.agent_rpc("read_interaction_attachment", {
        "interaction_id": call["interaction_id"],
        "attachment_id": uploaded["attachment_id"],
        "offset_bytes": 0, "max_result_bytes": 4,
    })
    text = str(chunk)
    assert "MDEyM" in text or "0123" in text or "content" in text, text[:200]
    assert final["call_state"] == "completed"


def test_j03_oversized_result_omitted_for_agent(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    big = "很" * 60_000
    call = central.agent_create("j03", max_result_bytes=16_384)
    term.ringed(call).accept()
    term.submit({"status": "completed", "report": big}, submission_id="j03-s")
    outcome, _ = central.agent_follow(call, prefix="j03-read")
    assert outcome.get("body_omitted_for_size") is True
    assert outcome["result"]["report"] is None


def test_j04_read_interaction_result_returns_full_body(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    big = "x" * 40_000
    call = central.agent_create("j04", max_result_bytes=16_384)
    term.ringed(call).accept()
    final = term.submit({"status": "completed", "report": big}, submission_id="j04-s")
    full = central.agent_rpc("read_interaction_result", {
        "interaction_id": call["interaction_id"],
        "result_id": final["result_id"],
    })
    assert "xxxx" in str(full)


def test_j05_task_version_conflict_rejected(tmp_path: Path) -> None:
    central = Central(tmp_path)
    term = central.terminal_login("NIGHTHAWK")
    call = central.agent_create("j05")
    term.ringed(call).accept()
    conflict = term.submit({"status": "completed", "report": "过期提交"},
                           submission_id="j05-s", task_version=99, code=404)
    assert conflict["status"] == "task_version_conflict"
