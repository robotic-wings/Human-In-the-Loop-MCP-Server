"""Durable, GUI-free MCP waiting lifecycle adapter.

This bounded adapter deliberately owns only caller admission and the waiting
event stream.  Human interfaces and notification delivery remain injected
boundaries.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import sqlite3
import uuid
from urllib.parse import parse_qs
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any


from central_call_machine import (
    ACTIVE_STATES, CallEventBus, CallMachine, InvalidTransition,
)

Clock = Callable[[], float]
Waiter = Callable[[float], Awaitable[None]]
Notifier = Callable[[dict[str, object]], None]
RouteAdmission = Callable[[], Mapping[str, object]]


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


_HUMAN_LOOP_MANUAL = """HITL MCP Operating Manual (human-in-the-loop toolset)

1. Core model: one call waits
- Each interactive tool (get_user_input / get_number_input / get_user_choice / get_multiline_input / show_confirmation_dialog / show_info_message / get_feedback_from_user / assign_task_to_human) starts one call round. The server parks the call for nearly client_timeout_seconds (default 30; pass longer, e.g. 300, when the human may be away).
- If the human answers within the park window, that SAME call returns the final result. Do not poll, do not fire parallel calls.
- If it returns a heartbeat, follow the continuation protocol below and call once more.

2. Continuation protocol (critical)
- When a response has needs_continuation=true (heartbeat / opened / in_progress): re-call the SAME tool with interaction_id, call_id, and after_event_seq = next_after_event_seq from that response (it is the machine-readable cursor; plain heartbeats repeat your current value).
- The cursor only moves forward: whenever a response contains an event_seq, adopt it; when it is a plain heartbeat (no event), keep your current value exactly. Resetting to an old value re-drains old events forever and hides later ones (accept, progress, cancel) from you.
- Each call already parks server-side for nearly client_timeout_seconds, so there is no need to re-call quickly; and while a round is open (needs_continuation=true) never report the task as done, failed, or abandoned — either keep waiting or end it explicitly with cancel_interaction.
- opened (human accepted) and in_progress (human working, may carry progress reports) both mean "keep waiting": continue the round. Do NOT start a new call (it returns busy).

3. Terminal outcomes (needs_continuation=false ends the round)
- answered: simple answer, in result.
- completed / failed: task final report in report; when resubmittable=true you may issue a fresh assign_task_to_human with a revised task_description. When feedback_requested=true, call get_feedback_from_user to collect the human's note.
- declined (human_declined): the human refused THIS task. Do not reissue it unchanged; re-plan or reword.
- declined (do_not_disturb): "message" — DND is on. Wait, route around it, or wind down politely; do not re-ring repeatedly.
- cancelled: the round was cancelled (possibly by your own cancel_interaction).
- not_accepted (ring_timeout): nobody picked up within the ring window. Retry after retry_after_seconds, or re-plan.
- disconnected (terminal_disconnected): the human accepted but their terminal went away. Retryable after retry_after_seconds.
- unreachable (no_available_channel): the human could not be reached on any channel; check health_check before retrying.
- busy: the single line is occupied (possibly by your own unfinished round). Wait retry_after_seconds and call again.

4. Abandonment guard
- If the human accepted but only heartbeats return for many rounds (accepted with no progress), the round will NOT end on its own. After several stalled rounds, call cancel_interaction with that round's interaction_id/call_id to release the line, then re-plan.

5. Result budget and pagination
- Pass max_result_bytes (>=16384) when creating a call. Oversized results come back with body_omitted_for_size and a result_id; fetch the full body with read_interaction_result (paginate via cursor) and attachments with read_interaction_attachment (paginate via offset_bytes). Never ask the human to resend what the server already holds.

6. Multiple operators
- This service may serve several humans, each addressed by a short callsign. Call list_operators first: it returns every callsign with display name, language, mobile capability, and current do-not-disturb state.
- Pass callsign on the FIRST call of a round (interactive tools) to choose who is called; omit it to target the primary operator. Continuation calls do not need callsign — the round is already bound to its operator.
- Each operator has their OWN busy line, DND and quiet window: one busy operator does not block calls to another, and DND of one operator only declines calls addressed to them.
- unknown_callsign in a response means the callsign is not in the directory; call list_operators to see valid ones.

7. Call discipline
- Retries are safe: re-sending the same call after a network drop replays its recorded outcome instead of duplicating work. You need no request_id.
- One call, one thing: ask with input/choice tools, delegate with assign_task_to_human (task_description = a single actionable instruction). Never ask questions through the task tool.
- Business errors (missing_call_id / session_not_found / invalid_cursor, ...) arrive in structuredContent; protocol errors arrive as JSON-RPC errors.
"""

def _interactive_schema(properties: Mapping[str, object]) -> dict[str, object]:
    """Interactive-tool schema: specific fields first, then shared round fields."""
    common: dict[str, object] = {
        "client_timeout_seconds": {
            "type": "integer",
            "description": "How long this single call parks server-side waiting for the human "
                           "(default 30; use a larger value such as 300 if the human may be away). "
                           "If the human answers within the window, this same call returns their final answer.",
        },
        "callsign": {
            "type": "string",
            "description": "Which operator to address, from list_operators (e.g. \"kefan\"). "
                           "Omit to target the primary operator.",
        },
        "interaction_id": {
            "type": "string",
            "description": "Continuation: the interaction_id from the previous response of this round.",
        },
        "call_id": {
            "type": "string",
            "description": "Continuation: the call_id from the previous response (pass together with interaction_id).",
        },
        "after_event_seq": {
            "type": "integer",
            "description": "Continuation cursor: echo next_after_event_seq from the previous response. "
                           "Keep it unchanged while responses are plain heartbeats.",
        },
        "max_result_bytes": {
            "type": "integer",
            "description": "Result body budget in bytes (minimum 16384). Oversized bodies are omitted "
                           "and served by read_interaction_result instead.",
        },
    }
    return {"type": "object", "properties": {**properties, **common}}


_MCP_TOOLS = (
    {"name": "get_user_input", "kind": "interaction",
     "description": "Ask the operator one short question and WAIT: the call parks server-side for up to "
                    "client_timeout_seconds and returns the operator's answer in this same call. "
                    "Read get_human_loop_prompt before your first interactive call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string", "description": "The question shown to the operator."},
         "required": {"type": "boolean", "description": "Whether an answer is mandatory."}})},
    {"name": "get_number_input", "kind": "interaction",
     "description": "Ask the operator for a number and WAIT: the call parks server-side and returns the "
                    "answer in this same call. Read get_human_loop_prompt before first use.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string", "description": "The question shown to the operator."},
         "required": {"type": "boolean"}})},
    {"name": "get_user_choice", "kind": "interaction",
     "description": "Offer the operator a set of options and WAIT: the call parks server-side and returns "
                    "their selection in this same call. Read get_human_loop_prompt before first use.",
     "inputSchema": _interactive_schema({
         "choices": {"type": "array", "description": "The options to choose from."},
         "prompt": {"type": "string"},
         "multiple": {"type": "boolean", "description": "Allow selecting more than one option."}})},
    {"name": "get_multiline_input", "kind": "interaction",
     "description": "Ask the operator for a long free-form answer and WAIT: the call parks server-side "
                    "and returns the answer in this same call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string"}})},
    {"name": "show_confirmation_dialog", "kind": "interaction",
     "description": "Ask the operator to confirm or deny one proposal and WAIT: the call parks "
                    "server-side and returns their decision in this same call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string", "description": "The proposal to confirm."}})},
    {"name": "show_info_message", "kind": "interaction",
     "description": "Show the operator one message and WAIT for their acknowledgement.",
     "inputSchema": _interactive_schema({
         "message": {"type": "string"}})},
    {"name": "get_feedback_from_user", "kind": "interaction",
     "description": "Collect free-form feedback from the operator and WAIT: the call parks server-side "
                    "and returns the feedback in this same call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string"}})},
    {"name": "assign_task_to_human", "kind": "interaction",
     "description": "Hand the operator one physical/real-world task and WAIT: the call parks server-side; "
                    "the operator accepts, executes, and reports back, and this same call returns their "
                    "final report. Use for doing, not for asking questions.",
     "inputSchema": _interactive_schema({
         "task_description": {"type": "string", "description": "One actionable instruction for the operator."}})},
    {"name": "read_interaction_result", "kind": "read",
     "description": "Read the full body of a saved result (pagination via cursor) when a response came "
                    "back with body_omitted_for_size.",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "result_id": {"type": "string"},
         "cursor": {"type": "string"}, "max_result_bytes": {"type": "integer"}}}},
    {"name": "read_interaction_attachment", "kind": "read",
     "description": "Read one byte chunk of a saved attachment (pagination via offset_bytes).",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "attachment_id": {"type": "string"},
         "offset_bytes": {"type": "integer"}, "max_result_bytes": {"type": "integer"}}}},
    {"name": "list_operators", "kind": "readonly",
     "description": "List the human operators (callsigns) this service can reach, with display "
                    "names, languages, and whether each currently has do-not-disturb on. "
                    "Use a callsign as the callsign argument of interactive tools.",
     "inputSchema": {"type": "object"}},
    {"name": "get_operator_profile", "kind": "readonly",
     "description": "Read one operator's profile (pass callsign; omit for the primary operator): "
                    "who they are and how to address them.",
     "inputSchema": {"type": "object", "properties": {
         "callsign": {"type": "string", "description": "Operator callsign from list_operators."}}}},
    {"name": "health_check", "kind": "readonly",
     "description": "Report HITL service health (storage, desktop online, push channels).",
     "inputSchema": {"type": "object"}},
    {"name": "get_human_loop_prompt", "kind": "readonly",
     "description": "Return the HITL operating manual (wait model, continuation cursor, outcomes). "
                    "Call this before your first interactive tool call.",
     "inputSchema": {"type": "object"}},
    {"name": "cancel_interaction", "kind": "control",
     "description": "End one open round (yours or the human's) to release the single line, e.g. when the "
                    "human accepted but has stalled for many rounds.",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "call_id": {"type": "string"}}}},
)


class WaitingLifecycle:
    def __init__(self, path: str | Path, clock: Clock, notify: Notifier, ring_timeout: float,
                 agent_reconnect_seconds: float, terminal_disconnect_seconds: float = 180,
                 operator_id: str = "only-operator",
                 operator_profile: Mapping[str, object] | None = None,
                 operator_directory: Mapping[str, Mapping[str, object]] | None = None,
                 max_attachment_bytes: int = 20 * 1024 * 1024,
                 max_submission_bytes: int = 50 * 1024 * 1024,
                 max_storage_bytes: int = 1024 * 1024 * 1024) -> None:
        self._path, self._clock, self._notify = str(path), clock, notify
        self._ring_timeout, self._agent_reconnect_seconds = ring_timeout, agent_reconnect_seconds
        self._terminal_disconnect_seconds = terminal_disconnect_seconds
        self._operator_id = operator_id
        self._operator_profile: dict[str, object] = dict(operator_profile or {})
        # Multi-operator directory.  Without configuration a single default
        # operator is synthesised so every historical single-user deployment
        # and test keeps behaving identically.
        if operator_directory:
            self._operators: dict[str, dict[str, object]] = {
                str(callsign): dict(entry) for callsign, entry in operator_directory.items()
            }
        else:
            self._operators = {
                operator_id: {"password": None, "devices": [], "mobile": True, "primary": True,
                              **({"profile": dict(self._operator_profile)} if self._operator_profile else {})}
            }
        primaries = [c for c, e in self._operators.items() if e.get("primary")]
        self._primary_operator = primaries[0] if primaries else next(iter(self._operators))
        self._max_attachment_bytes = max_attachment_bytes
        self._max_submission_bytes = max_submission_bytes
        self._max_storage_bytes = max_storage_bytes
        self._clock_high_water: float | None = None
        self._bus = CallEventBus()
        self._machine = CallMachine(self._bus)
        self._attachments_dir = Path(self._path).parent / "attachments"
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._attachments_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS lifecycle_calls (
                call_id TEXT PRIMARY KEY, interaction_id TEXT NOT NULL, caller_id TEXT NOT NULL,
                call_state TEXT NOT NULL, ring_deadline REAL NOT NULL, reason TEXT,
                payload_json TEXT NOT NULL DEFAULT '{}',
                owner_device_id TEXT, owner_session_id TEXT, handled_on TEXT,
                terminal_platform TEXT, invited_desktop_id TEXT,
                mobile_eligible INTEGER NOT NULL DEFAULT 0,
                operator_callsign TEXT,
                terminal_last_seen_at REAL, terminal_disconnect_deadline REAL,
                version INTEGER NOT NULL DEFAULT 1,
                draft_content TEXT, draft_version INTEGER NOT NULL DEFAULT 0,
                created_at REAL, task_version INTEGER NOT NULL DEFAULT 1
              );
              CREATE TABLE IF NOT EXISTS lifecycle_requests (
                caller_id TEXT NOT NULL, request_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                outcome_json TEXT NOT NULL, operation_kind TEXT NOT NULL DEFAULT 'create',
                PRIMARY KEY (caller_id, request_id)
              );
              CREATE TABLE IF NOT EXISTS lifecycle_events (
                call_id TEXT NOT NULL, event_seq INTEGER NOT NULL, body_json TEXT NOT NULL,
                PRIMARY KEY (call_id, event_seq)
              );
              CREATE TABLE IF NOT EXISTS lifecycle_submissions (
                call_id TEXT NOT NULL, submission_id TEXT NOT NULL, actor TEXT NOT NULL,
                payload_json TEXT NOT NULL, outcome_json TEXT NOT NULL,
                PRIMARY KEY (call_id, submission_id)
              );
              CREATE TABLE IF NOT EXISTS lifecycle_revisions (
                caller_id TEXT NOT NULL, interaction_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                call_id TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY (caller_id, interaction_id, revision_id)
              );
              CREATE TABLE IF NOT EXISTS lifecycle_attachments (
                attachment_id TEXT PRIMARY KEY, call_id TEXT NOT NULL,
                filename TEXT NOT NULL, media_type TEXT NOT NULL,
                byte_count INTEGER NOT NULL, checksum TEXT NOT NULL, stored_path TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS lifecycle_dnd (
                operator_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL DEFAULT 0,
                message TEXT NOT NULL DEFAULT ''
              );
              CREATE TABLE IF NOT EXISTS lifecycle_quiet_window (
                operator_id TEXT PRIMARY KEY,
                quiet_window_seconds INTEGER NOT NULL DEFAULT 300,
                quiet_mode TEXT NOT NULL DEFAULT 'short',
                last_accept_at REAL
              );
            """)
            self._migrate_schema(conn)
            self._settle_due(conn)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_calls)")}
        if "agent_reconnect_deadline" not in columns:
            conn.execute("ALTER TABLE lifecycle_calls ADD COLUMN agent_reconnect_deadline REAL")
        for column, definition in (
            ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
            ("owner_device_id", "TEXT"), ("owner_session_id", "TEXT"),
            ("handled_on", "TEXT"), ("terminal_platform", "TEXT"),
            ("invited_desktop_id", "TEXT"),
            ("mobile_eligible", "INTEGER NOT NULL DEFAULT 0"),
            ("operator_callsign", "TEXT"),
            ("terminal_last_seen_at", "REAL"), ("terminal_disconnect_deadline", "REAL"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
            ("draft_content", "TEXT"), ("draft_version", "INTEGER NOT NULL DEFAULT 0"),
            ("created_at", "REAL"), ("task_version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE lifecycle_calls ADD COLUMN {column} {definition}")
        request_columns = {row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_requests)")}
        if "operation_kind" not in request_columns:
            conn.execute("ALTER TABLE lifecycle_requests ADD COLUMN operation_kind TEXT NOT NULL DEFAULT 'create'")

    def create(self, caller_id: str, request_id: str, payload: Mapping[str, object],
               invited_desktop_id: str | None = None, mobile_eligible: bool = False,
               operator_callsign: str | None = None) -> dict[str, object]:
        target = self.resolve_operator(operator_callsign)
        if target is None:
            return {"success": False, "status": "unknown_callsign",
                    "message": f"no operator with callsign {operator_callsign!r}; call list_operators for the directory",
                    "needs_continuation": False}
        encoded = self._dump(payload)
        notification: dict[str, object] | None = None
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute("SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests WHERE caller_id=? AND request_id=?", (caller_id, request_id)).fetchone()
                if existing is not None:
                    if existing["operation_kind"] != "create" or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                dnd = self._dnd_state(conn, target)
                if dnd["enabled"]:
                    outcome = {
                        "success": True, "status": "declined", "reason": "do_not_disturb",
                        "operator": target,
                        "message": dnd["message"], "human_action": False, "needs_continuation": False,
                    }
                    conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'create')", (caller_id, request_id, encoded, self._dump(outcome)))
                    conn.commit()
                    return outcome
                quiet = self._quiet_window_state(conn, target)
                last_accept_at = quiet.get("last_accept_at")
                degraded = last_accept_at is not None and (self._clock() - last_accept_at) < quiet["quiet_window_seconds"]
                active = conn.execute(
                    "SELECT call_id FROM lifecycle_calls WHERE call_state IN ('ringing', 'accepted', 'in_progress') "
                    "AND operator_callsign=? LIMIT 1", (target,)).fetchone()
                if active is not None:
                    outcome = {
                        "success": True, "status": "busy", "call_state": None, "call_id": None,
                        "reason": None, "interaction_id": None, "human_action": False,
                        "operator": target,
                        "handled_on": None, "terminal_platform": None, "event_seq": None,
                        "needs_continuation": False, "retryable": True, "retry_after_seconds": 30,
                    }
                else:
                    call_id, interaction_id = str(uuid.uuid4()), str(uuid.uuid4())
                    deadline = self._clock() + self._ring_timeout
                    conn.execute(
                        "INSERT INTO lifecycle_calls "
                        "(call_id, interaction_id, caller_id, call_state, ring_deadline, reason, payload_json, agent_reconnect_deadline, invited_desktop_id, mobile_eligible, operator_callsign, created_at) "
                        "VALUES (?, ?, ?, 'ringing', ?, NULL, ?, NULL, ?, ?, ?, ?)",
                        (call_id, interaction_id, caller_id, deadline, encoded, invited_desktop_id,
                         int(mobile_eligible), target, self._clock()),
                    )
                    outcome = self._heartbeat(interaction_id, call_id, "ringing")
                    outcome["operator"] = target
                    short_title = next(
                        (str(payload.get(key)) for key in ("title", "task_description", "prompt", "message")
                         if isinstance(payload.get(key), str) and payload.get(key).strip()),
                        "Human-in-the-Loop request",
                    )
                    notification = {
                        "call_id": call_id, "ring_deadline": deadline,
                        "title": caller_id, "body": " ".join(short_title.split())[:200],
                    }
                    if degraded:
                        notification["degraded"] = True
                        notification["quiet_mode"] = quiet["quiet_mode"]
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'create')", (caller_id, request_id, encoded, self._dump(outcome)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if notification is not None:
            self._notify(notification)
        return outcome

    def set_dnd(self, enabled: bool, message: str = "", *, operator_id: str | None = None) -> dict[str, object]:
        """Persist one operator's do-not-disturb state and auto-reply."""
        operator_id = operator_id or self._operator_id
        message = message if isinstance(message, str) else ""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO lifecycle_dnd (operator_id, enabled, message) VALUES (?, ?, ?) "
                    "ON CONFLICT(operator_id) DO UPDATE SET enabled=excluded.enabled, message=excluded.message",
                    (operator_id, int(enabled), message),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"success": True, "operator": operator_id, "enabled": bool(enabled), "message": message}

    def get_dnd(self, operator_id: str | None = None) -> dict[str, object]:
        with self._connection() as conn:
            state = self._dnd_state(conn, operator_id or self._operator_id)
        return {"success": True, "operator": operator_id or self._operator_id, **state}

    def _dnd_state(self, conn: sqlite3.Connection, operator_id: str | None = None) -> dict[str, object]:
        row = conn.execute(
            "SELECT enabled, message FROM lifecycle_dnd WHERE operator_id=?", (operator_id or self._operator_id,)
        ).fetchone()
        if row is None:
            return {"enabled": False, "message": ""}
        return {"enabled": bool(row["enabled"]), "message": str(row["message"])}

    def get_quiet_window(self, operator_id: str | None = None) -> dict[str, object]:
        with self._connection() as conn:
            state = self._quiet_window_state(conn, operator_id or self._operator_id)
        return {"success": True, "operator": operator_id or self._operator_id, **state}

    def set_quiet_window(self, quiet_window_seconds: int, quiet_mode: str,
                         operator_id: str | None = None) -> dict[str, object]:
        if isinstance(quiet_window_seconds, bool) or not isinstance(quiet_window_seconds, int) or quiet_window_seconds < 0:
            return self._error("invalid_request")
        if quiet_mode not in {"short", "silent"}:
            return self._error("invalid_request")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO lifecycle_quiet_window (operator_id, quiet_window_seconds, quiet_mode, last_accept_at) "
                    "VALUES (?, ?, ?, NULL) "
                    "ON CONFLICT(operator_id) DO UPDATE SET quiet_window_seconds=excluded.quiet_window_seconds, quiet_mode=excluded.quiet_mode",
                    (operator_id or self._operator_id, quiet_window_seconds, quiet_mode),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"success": True, "quiet_window_seconds": quiet_window_seconds, "quiet_mode": quiet_mode}

    def _quiet_window_state(self, conn: sqlite3.Connection, operator_id: str | None = None) -> dict[str, object]:
        row = conn.execute(
            "SELECT quiet_window_seconds, quiet_mode, last_accept_at FROM lifecycle_quiet_window WHERE operator_id=?",
            (operator_id or self._operator_id,),
        ).fetchone()
        if row is None:
            return {"quiet_window_seconds": 300, "quiet_mode": "short", "last_accept_at": None}
        return {
            "quiet_window_seconds": int(row["quiet_window_seconds"]),
            "quiet_mode": str(row["quiet_mode"]),
            "last_accept_at": row["last_accept_at"],
        }

    def _record_accept(self, conn: sqlite3.Connection, operator_id: str | None = None) -> None:
        conn.execute(
            "INSERT INTO lifecycle_quiet_window (operator_id, quiet_window_seconds, quiet_mode, last_accept_at) "
            "VALUES (?, 300, 'short', ?) "
            "ON CONFLICT(operator_id) DO UPDATE SET last_accept_at=excluded.last_accept_at",
            (operator_id or self._operator_id, self._clock()),
        )

    def has_request(self, caller_id: str, request_id: str) -> bool:
        with self._connection() as conn:
            return conn.execute(
                "SELECT 1 FROM lifecycle_requests WHERE caller_id=? AND request_id=?",
                (caller_id, request_id),
            ).fetchone() is not None

    def continue_call(self, caller_id: str, request_id: str, interaction_id: str, call_id: str,
                      after: int) -> dict[str, object] | None:
        encoded = self._dump({"interaction_id": interaction_id, "call_id": call_id, "after_event_seq": after})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests "
                    "WHERE caller_id=? AND request_id=?",
                    (caller_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["operation_kind"] != "continuation" or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=? AND caller_id=?", (call_id, caller_id)).fetchone()
                if call is None or call["interaction_id"] != interaction_id:
                    return self._error("session_not_found")
                last = conn.execute("SELECT COALESCE(MAX(event_seq), 0) AS n FROM lifecycle_events WHERE call_id=?", (call_id,)).fetchone()["n"]
                if after < 0 or after > last:
                    return self._error("invalid_cursor")
                event = conn.execute("SELECT body_json FROM lifecycle_events WHERE call_id=? AND event_seq>? ORDER BY event_seq LIMIT 1", (call_id, after)).fetchone()
                if event is not None:
                    outcome = self._load(event["body_json"])
                    outcome = self._apply_result_budget(outcome, call["payload_json"])
                elif call["call_state"] in {"ringing", "accepted", "in_progress"}:
                    outcome = self._heartbeat(interaction_id, call_id, call["call_state"])
                else:
                    # Preserve the original immutable terminal event rather
                    # than synthesizing a reduced fallback.  In particular,
                    # cancellation records ``human_action:false`` and that
                    # required control field must survive restart and later
                    # continuation reads even after its event was confirmed.
                    terminal = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    outcome = self._load(terminal["body_json"]) if terminal is not None else self._terminal(
                        interaction_id, call_id, call["call_state"], call["reason"], int(last),
                    )
                conn.execute(
                    "INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'continuation')",
                    (caller_id, request_id, encoded, self._dump(outcome)),
                )
                return outcome
            finally:
                conn.commit()

    def cancel(self, caller_id: str, request_id: str, interaction_id: str, call_id: str) -> dict[str, object]:
        """Cancel exactly one active call round for its original caller.

        Cancellation is an Agent action, not a transport failure: it has an
        immutable terminal event, releases the single busy slot, and shares
        the caller/request-id namespace with every other MCP operation.
        """
        encoded = self._dump({"interaction_id": interaction_id, "call_id": call_id})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests "
                    "WHERE caller_id=? AND request_id=?",
                    (caller_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["operation_kind"] != "cancel" or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute(
                    "SELECT * FROM lifecycle_calls WHERE call_id=? AND caller_id=?",
                    (call_id, caller_id),
                ).fetchone()
                if call is None or call["interaction_id"] != interaction_id:
                    return self._error("session_not_found")
                if call["call_state"] not in {"ringing", "accepted", "in_progress"}:
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    outcome = self._load(event["body_json"]) if event else self._error("expired")
                else:
                    _, seq = self._machine.transition(
                        conn, call_id, "agent_cancel", reason="agent_cancelled", bump_version=False,
                    )
                    outcome = self._terminal(interaction_id, call_id, "cancelled", "agent_cancelled", seq)
                    outcome["human_action"] = False
                    conn.execute(
                        "INSERT INTO lifecycle_events VALUES (?, ?, ?)",
                        (call_id, seq, self._dump(outcome)),
                    )
                conn.execute(
                    "INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'cancel')",
                    (caller_id, request_id, encoded, self._dump(outcome)),
                )
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def response_delivered(self, caller_id: str, response: Mapping[str, object]) -> None:
        """Persist grace only after a newly executed resumable response is sent."""
        status, state = response.get("status"), response.get("call_state")
        # A ringing heartbeat is the normal end of an HTTP wait leg.  It is
        # not evidence that the caller has disconnected: the same agent may
        # immediately continue with this interaction/call pair, and the
        # absolute ring deadline already governs an unanswered invitation.
        # Never arm agent-disconnect expiry for this state.
        if state == "ringing":
            return
        if state not in {"ringing", "accepted", "in_progress"}:
            return
        if status != "heartbeat" and not (status == "opened" and state == "accepted") and not (
            status == "in_progress" and state == "in_progress"
        ):
            return
        interaction_id, call_id = response.get("interaction_id"), response.get("call_id")
        if not isinstance(interaction_id, str) or not isinstance(call_id, str):
            return
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute(
                    "SELECT interaction_id, call_state FROM lifecycle_calls WHERE call_id=? AND caller_id=?",
                    (call_id, caller_id),
                ).fetchone()
                if call is not None and call["interaction_id"] == interaction_id and call["call_state"] == state:
                    grace = 300 if state == "in_progress" else self._agent_reconnect_seconds
                    conn.execute(
                        "UPDATE lifecycle_calls SET agent_reconnect_deadline=? WHERE call_id=?",
                        (self._clock() + grace, call_id),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def settle_deadlines(self) -> None:
        """Run one durable, server-owned expiry pass.

        Request paths also call ``_settle_due`` before acting, but the ASGI
        application owns this public tick so an idle ringing call is settled
        without requiring another caller, terminal action, or restart.
        """
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def apply_delivery_decisions(self, decisions: Mapping[str, object]) -> None:
        """Apply definitive router decisions without letting routing own state.

        ``unreachable`` is emitted only after the router has established that
        every enabled channel definitely failed.  Uncertain and stale
        decisions leave the call alone; settling deadlines first preserves
        the absolute ringing deadline as the earlier terminal outcome.
        """
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                for call_id, decision in decisions.items():
                    if not isinstance(call_id, str) or decision != "unreachable":
                        continue
                    call = conn.execute(
                        "SELECT * FROM lifecycle_calls WHERE call_id=? AND call_state='ringing'",
                        (call_id,),
                    ).fetchone()
                    if call is None:
                        continue
                    _, seq = self._machine.transition(
                        conn, call_id, "unreachable", reason="no_available_channel", bump_version=False,
                    )
                    body = self._terminal(
                        call["interaction_id"], call_id, "unreachable", "no_available_channel", seq,
                    )
                    body["human_action"] = False
                    conn.execute(
                        "INSERT INTO lifecycle_events VALUES (?, ?, ?)",
                        (call_id, seq, self._dump(body)),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def terminal_action(self, *, call_id: str, device_id: str, session_id: str, terminal_platform: str,
                        operation_id: str, action: str) -> dict[str, object]:
        """Apply a desktop session action to the central call authority."""
        if action not in {"accept", "decline"}:
            return self._error("session_not_found")
        encoded = self._dump({"call_id": call_id, "action": action})
        actor = f"terminal:{session_id}"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute("SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests WHERE caller_id=? AND request_id=?", (actor, operation_id)).fetchone()
                kind = f"terminal_{action}"
                if existing is not None:
                    if existing["operation_kind"] != kind or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None:
                    return self._error("session_not_found")
                # A desktop channel is not a transferable invitation: the
                # selected device is persisted with the call and is the only
                # desktop allowed to act on that call.
                if call["invited_desktop_id"] != device_id:
                    return self._error("session_not_found")
                if call["call_state"] != "ringing":
                    event = conn.execute("SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,)).fetchone()
                    outcome = self._load(event["body_json"]) if event else self._error("expired")
                else:
                    status = "opened" if action == "accept" else "declined"
                    now = self._clock()
                    if action == "accept":
                        _, seq = self._machine.transition(
                            conn, call_id, "accept", reason=None,
                            columns={
                                "owner_device_id": device_id, "owner_session_id": session_id,
                                "handled_on": "desktop", "terminal_platform": terminal_platform,
                                "terminal_last_seen_at": now,
                                "terminal_disconnect_deadline": now + self._terminal_disconnect_seconds,
                                "agent_reconnect_deadline": None,
                            },
                        )
                        self._record_accept(conn, str(call["operator_callsign"]) if call["operator_callsign"] else None)
                    else:
                        # A decline ends the invitation without assigning a
                        # processing owner.  Keep its audit endpoint separate
                        # from handled_on / terminal_platform.  The internal
                        # device/session association remains so the declining
                        # desktop receives the terminal state on its next
                        # synchronization and can close its old alert.
                        _, seq = self._machine.transition(
                            conn, call_id, "decline", reason="human_declined",
                            columns={
                                "owner_device_id": device_id, "owner_session_id": session_id,
                                "handled_on": None, "terminal_platform": None,
                                "terminal_last_seen_at": None,
                                "terminal_disconnect_deadline": None,
                                "agent_reconnect_deadline": None,
                            },
                        )
                    outcome: dict[str, object] = {
                        "success": True, "status": status, "call_state": "accepted" if action == "accept" else "declined", "human_action": True,
                        "needs_continuation": action == "accept", "interaction_id": call["interaction_id"],
                        "call_id": call_id, "event_seq": seq,
                    }
                    if action == "accept":
                        outcome.update(owner_device_id=device_id, owner_session_id=session_id,
                                       handled_on="desktop", terminal_platform=terminal_platform)
                    else:
                        outcome.update(reason="human_declined", acted_on="desktop", handled_on=None,
                                       terminal_platform=None, retryable=False)
                    conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, ?)", (actor, operation_id, encoded, self._dump(outcome), kind))
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def browser_action(self, *, call_id: str, operator_id: str, session_id: str,
                       operation_id: str, action: str) -> dict[str, object]:
        """Apply an explicit, authenticated iPhone-web action centrally.

        A browser session is an ownership credential, not a read capability.
        The invitation's mobile eligibility is stored at creation time, so a
        later route configuration change cannot grant a browser authority.
        """
        if action not in {"accept", "decline"}:
            return self._error("session_not_found")
        encoded = self._dump({"call_id": call_id, "action": action})
        actor = f"browser:{session_id}"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests WHERE caller_id=? AND request_id=?",
                    (actor, operation_id),
                ).fetchone()
                kind = f"browser_{action}"
                if existing is not None:
                    if existing["operation_kind"] != kind or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or not bool(call["mobile_eligible"]):
                    return self._error("session_not_found")
                if operator_id != (str(call["operator_callsign"]) if call["operator_callsign"] else self._operator_id):
                    return self._error("session_not_found")
                # A browser opened after a desktop owns the call is a
                # read-only truth view, not a second action surface.
                if call["call_state"] != "ringing":
                    # Deadline settlement is authoritative even when the
                    # request path, rather than the scheduler, observes it.
                    # Return that saved terminal result; a competing human
                    # action (for example after desktop acceptance) remains
                    # opaque and cannot disclose or alter ownership.
                    if call["reason"] == "ring_timeout":
                        event = conn.execute(
                            "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                            (call_id,),
                        ).fetchone()
                        return self._load(event["body_json"]) if event is not None else self._error("expired")
                    return self._error("session_not_found")
                status = "opened" if action == "accept" else "declined"
                now = self._clock()
                if action == "accept":
                    _, seq = self._machine.transition(
                        conn, call_id, "accept", reason=None,
                        columns={
                            "owner_device_id": "iphone_web", "owner_session_id": session_id,
                            "handled_on": "iphone_web", "terminal_platform": "ios",
                            "terminal_last_seen_at": now,
                            "terminal_disconnect_deadline": now + self._terminal_disconnect_seconds,
                            "agent_reconnect_deadline": None,
                        },
                    )
                    self._record_accept(conn, str(call["operator_callsign"]) if call["operator_callsign"] else None)
                else:
                    _, seq = self._machine.transition(
                        conn, call_id, "decline", reason="human_declined",
                        columns={
                            "owner_device_id": "iphone_web", "owner_session_id": session_id,
                            "handled_on": None, "terminal_platform": None,
                            "terminal_last_seen_at": None,
                            "terminal_disconnect_deadline": None,
                            "agent_reconnect_deadline": None,
                        },
                    )
                outcome: dict[str, object] = {
                    "success": True, "status": status, "call_state": "accepted" if action == "accept" else "declined", "human_action": True,
                    "needs_continuation": action == "accept", "interaction_id": call["interaction_id"],
                    "call_id": call_id, "event_seq": seq,
                }
                if action == "accept":
                    outcome.update(owner_device_id="iphone_web", owner_session_id=session_id,
                                   handled_on="iphone_web", terminal_platform="ios")
                else:
                    outcome.update(reason="human_declined", acted_on="iphone_web", handled_on=None,
                                   terminal_platform=None, retryable=False)
                conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, ?)",
                             (actor, operation_id, encoded, self._dump(outcome), kind))
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def submit(self, *, call_id: str, device_id: str, session_id: str,
               submission_id: str, result: Mapping[str, object],
               task_version: int | None = None) -> dict[str, object]:
        """Persist one final answer from the exact session which owns a call."""
        if not submission_id or len(submission_id) > 128:
            return self._error("invalid_request")
        actor = f"owner:{session_id}"
        encoded = self._dump(dict(result))
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or call["owner_device_id"] != device_id or call["owner_session_id"] != session_id:
                    return self._error("session_not_found")
                previous = conn.execute(
                    "SELECT actor, payload_json, outcome_json FROM lifecycle_submissions WHERE call_id=? AND submission_id=?",
                    (call_id, submission_id),
                ).fetchone()
                if previous is not None:
                    if previous["actor"] != actor or previous["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(previous["outcome_json"])
                if call["call_state"] not in {"accepted", "in_progress"}:
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    return self._load(event["body_json"]) if event is not None else self._error("expired")
                if isinstance(task_version, int) and not isinstance(task_version, bool) and task_version != int(call["task_version"]):
                    return self._error("task_version_conflict")
                now = self._clock()
                conn.execute(
                    "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
                    (now, now + self._terminal_disconnect_seconds, call_id),
                )
                status, state, needs_continuation, extras = self._interpret_submission(result)
                _, seq = self._machine.transition(conn, call_id, "submit", target=state, reason=None)
                outcome: dict[str, object] = {
                    "success": True, "status": status, "call_state": state, "human_action": True,
                    "needs_continuation": needs_continuation, "interaction_id": call["interaction_id"], "call_id": call_id,
                    "event_seq": seq, "handled_on": call["handled_on"],
                    "terminal_platform": call["terminal_platform"], "result": dict(result),
                    "result_id": str(uuid.uuid4()),
                }
                outcome.update(extras)
                attachments = dict(result).get("attachments")
                if isinstance(attachments, list):
                    outcome["attachments"] = self._resolve_attachments(conn, call_id, attachments)
                conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                conn.execute("INSERT INTO lifecycle_submissions VALUES (?, ?, ?, ?, ?)",
                             (call_id, submission_id, actor, encoded, self._dump(outcome)))
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def terminal_cancel(self, *, call_id: str, device_id: str, session_id: str,
                        operation_id: str) -> dict[str, object]:
        """Cancel an accepted call from its exact owning terminal session.

        A human cancellation is an immutable terminal event distinct from an
        Agent cancellation: it records ``reason='human_cancelled'`` with
        ``human_action=True`` and releases the single busy slot.
        """
        actor = f"terminal_cancel:{session_id}"
        encoded = self._dump({"call_id": call_id})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests "
                    "WHERE caller_id=? AND request_id=?",
                    (actor, operation_id),
                ).fetchone()
                if existing is not None:
                    if existing["operation_kind"] != "terminal_cancel" or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or call["owner_device_id"] != device_id or call["owner_session_id"] != session_id:
                    return self._error("session_not_found")
                if call["call_state"] not in {"accepted", "in_progress"}:
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    outcome = self._load(event["body_json"]) if event is not None else self._error("expired")
                else:
                    _, seq = self._machine.transition(
                        conn, call_id, "human_cancel", reason="human_cancelled",
                    )
                    outcome: dict[str, object] = {
                        "success": True, "status": "cancelled", "call_state": "cancelled",
                        "reason": "human_cancelled", "human_action": True, "needs_continuation": False,
                        "interaction_id": call["interaction_id"], "call_id": call_id, "event_seq": seq,
                        "handled_on": call["handled_on"], "terminal_platform": call["terminal_platform"],
                    }
                    conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, ?)",
                             (actor, operation_id, encoded, self._dump(outcome), "terminal_cancel"))
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def upload_attachment(self, *, call_id: str, device_id: str, session_id: str,
                          filename: str, media_type: str, content: bytes) -> dict[str, object]:
        """Store one file body centrally and return its server-owned metadata.

        The file entity lives in the central data directory, never on the
        submitting terminal; the returned ``attachment_id`` is the only handle
        the Agent and later submissions may reference.
        """
        if not filename or not isinstance(filename, str):
            return self._error("invalid_request")
        if not isinstance(content, bytes) or not content:
            return self._error("invalid_request")
        media_type = media_type if isinstance(media_type, str) and media_type else "application/octet-stream"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or call["owner_device_id"] != device_id or call["owner_session_id"] != session_id:
                    return self._error("session_not_found")
                if call["call_state"] not in {"accepted", "in_progress"}:
                    return self._error("expired")
                if len(content) > self._max_attachment_bytes:
                    return self._error("attachment_too_large")
                submission_total = int(conn.execute(
                    "SELECT COALESCE(SUM(byte_count), 0) FROM lifecycle_attachments WHERE call_id=?",
                    (call_id,),
                ).fetchone()[0])
                if submission_total + len(content) > self._max_submission_bytes:
                    return self._error("submission_too_large")
                storage_total = int(conn.execute(
                    "SELECT COALESCE(SUM(byte_count), 0) FROM lifecycle_attachments",
                ).fetchone()[0])
                if storage_total + len(content) > self._max_storage_bytes:
                    return self._error("storage_full")
                now = self._clock()
                conn.execute(
                    "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
                    (now, now + self._terminal_disconnect_seconds, call_id),
                )
                attachment_id = str(uuid.uuid4())
                byte_count = len(content)
                checksum = hashlib.sha256(content).hexdigest()
                stored_path = self._attachments_dir / f"{attachment_id}.bin"
                stored_path.write_bytes(content)
                conn.execute(
                    "INSERT INTO lifecycle_attachments "
                    "(attachment_id, call_id, filename, media_type, byte_count, checksum, stored_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (attachment_id, call_id, filename, media_type, byte_count, checksum, str(stored_path)),
                )
                conn.commit()
                return {
                    "success": True, "attachment_id": attachment_id, "filename": filename,
                    "media_type": media_type, "byte_count": byte_count, "checksum": checksum,
                }
            except BaseException:
                conn.rollback()
                raise

    def _attachment_metadata(self, conn: sqlite3.Connection, call_id: str,
                             attachment_id: str) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT attachment_id, filename, media_type, byte_count, checksum "
            "FROM lifecycle_attachments WHERE call_id=? AND attachment_id=?",
            (call_id, attachment_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "attachment_id": row["attachment_id"], "filename": row["filename"],
            "media_type": row["media_type"], "byte_count": row["byte_count"], "checksum": row["checksum"],
        }

    def _resolve_attachments(self, conn: sqlite3.Connection, call_id: str,
                             attachment_ids: Any) -> list[dict[str, object]]:
        resolved: list[dict[str, object]] = []
        if not isinstance(attachment_ids, list):
            return resolved
        for attachment_id in attachment_ids:
            if not isinstance(attachment_id, str):
                continue
            metadata = self._attachment_metadata(conn, call_id, attachment_id)
            if metadata is not None:
                resolved.append(metadata)
        return resolved

    def terminal_heartbeat(self, *, call_id: str, session_id: str) -> dict[str, object]:
        """Refresh liveness only for the exact session which accepted the call."""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or call["owner_session_id"] != session_id:
                    return self._error("session_not_found")
                if call["call_state"] not in {"accepted", "in_progress"}:
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    outcome = self._load(event["body_json"]) if event else self._error("expired")
                    conn.commit()
                    return outcome
                now = self._clock()
                conn.execute(
                    "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
                    (now, now + self._terminal_disconnect_seconds, call_id),
                )
                conn.commit()
                return {"success": True, "status": "heartbeat", "call_state": call["call_state"],
                        "needs_continuation": True}
            except BaseException:
                conn.rollback()
                raise

    def terminal_states(self, device_id: str) -> list[dict[str, object]]:
        now = self._clock()
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                # A terminal's periodic sync proves it is alive: refresh the
                # liveness timestamp of every in-flight call it owns so an
                # actively-connected terminal never has its call disconnected
                # underneath it while the human is still filling a form.
                conn.execute(
                    "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? "
                    "WHERE owner_device_id=? AND call_state IN ('accepted','in_progress')",
                    (now, now + self._terminal_disconnect_seconds, device_id),
                )
                rows = conn.execute(
                    "SELECT call_id, call_state, version FROM lifecycle_calls WHERE owner_device_id=? ORDER BY call_id",
                    (device_id,),
                ).fetchall()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return [{"call_id": row["call_id"], "call_state": row["call_state"], "version": row["version"]} for row in rows]

    def save_draft(self, *, call_id: str, device_id: str, session_id: str,
                   draft_version: int, content: str) -> dict[str, object]:
        """Persist a partial answer draft without changing the call state."""
        if isinstance(draft_version, bool) or not isinstance(draft_version, int) or draft_version < 0:
            return self._error("invalid_request")
        if not isinstance(content, str):
            return self._error("invalid_request")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or call["owner_device_id"] != device_id or call["owner_session_id"] != session_id:
                    return self._error("session_not_found")
                if call["call_state"] not in {"accepted", "in_progress"}:
                    return self._error("expired")
                if draft_version <= int(call["draft_version"]):
                    return self._error("draft_version_conflict")
                now = self._clock()
                conn.execute(
                    "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
                    (now, now + self._terminal_disconnect_seconds, call_id),
                )
                conn.execute(
                    "UPDATE lifecycle_calls SET draft_content=?, draft_version=? WHERE call_id=?",
                    (content, draft_version, call_id),
                )
                conn.commit()
                return {"success": True, "status": "draft_saved", "draft_version": draft_version}
            except BaseException:
                conn.rollback()
                raise

    def resubmit(self, caller_id: str, request_id: str, interaction_id: str, call_id: str,
                 revision_id: str, payload: Mapping[str, object],
                 invited_desktop_id: str | None = None, mobile_eligible: bool = False,
                 operator_callsign: str | None = None) -> dict[str, object]:
        target = self.resolve_operator(operator_callsign)
        if target is None:
            return {"success": False, "status": "unknown_callsign", "needs_continuation": False}
        """Open a new call round for a completed/failed task under a revision."""
        if not isinstance(revision_id, str) or not revision_id or len(revision_id) > 128:
            return self._error("invalid_request")
        if not isinstance(payload, Mapping):
            return self._error("invalid_request")
        encoded = self._dump(payload)
        request_encoded = self._dump({
            "interaction_id": interaction_id, "call_id": call_id,
            "revision_id": revision_id, "payload": dict(payload),
        })
        notification: dict[str, object] | None = None
        busy = {
            "success": True, "status": "busy", "call_state": None, "call_id": None,
            "reason": None, "interaction_id": None, "human_action": False,
            "handled_on": None, "terminal_platform": None, "event_seq": None,
            "needs_continuation": False, "retryable": True, "retry_after_seconds": 30,
        }
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests "
                    "WHERE caller_id=? AND request_id=?",
                    (caller_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["operation_kind"] != "resubmit" or existing["payload_json"] != request_encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                revision = conn.execute(
                    "SELECT call_id, payload_json FROM lifecycle_revisions "
                    "WHERE caller_id=? AND interaction_id=? AND revision_id=?",
                    (caller_id, interaction_id, revision_id),
                ).fetchone()
                if revision is not None:
                    if revision["payload_json"] != encoded:
                        return self._error("revision_conflict")
                    outcome = self._heartbeat(interaction_id, revision["call_id"], "ringing")
                    conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'resubmit')",
                                 (caller_id, request_id, request_encoded, self._dump(outcome)))
                    conn.commit()
                    return outcome
                parent = conn.execute(
                    "SELECT * FROM lifecycle_calls WHERE call_id=? AND interaction_id=? AND caller_id=?",
                    (call_id, interaction_id, caller_id),
                ).fetchone()
                if parent is None:
                    return self._error("session_not_found")
                if parent["call_state"] not in {"completed", "failed"}:
                    return self._error("invalid_request")
                active = conn.execute(
                    "SELECT call_id FROM lifecycle_calls WHERE call_state IN ('ringing', 'accepted', 'in_progress') "
                    "AND operator_callsign=? LIMIT 1", (target,)
                ).fetchone()
                if active is not None:
                    conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'resubmit')",
                                 (caller_id, request_id, request_encoded, self._dump(busy)))
                    conn.commit()
                    return busy
                new_call_id = str(uuid.uuid4())
                deadline = self._clock() + self._ring_timeout
                conn.execute(
                    "INSERT INTO lifecycle_calls "
                    "(call_id, interaction_id, caller_id, call_state, ring_deadline, reason, payload_json, agent_reconnect_deadline, invited_desktop_id, mobile_eligible, operator_callsign, created_at) "
                    "VALUES (?, ?, ?, 'ringing', ?, NULL, ?, NULL, ?, ?, ?, ?)",
                    (new_call_id, interaction_id, caller_id, deadline, encoded,
                     invited_desktop_id, int(mobile_eligible), target, self._clock()),
                )
                conn.execute(
                    "INSERT INTO lifecycle_revisions VALUES (?, ?, ?, ?, ?)",
                    (caller_id, interaction_id, revision_id, new_call_id, encoded),
                )
                outcome = self._heartbeat(interaction_id, new_call_id, "ringing")
                short_title = next(
                    (str(payload.get(key)) for key in ("title", "task_description", "prompt", "message")
                     if isinstance(payload.get(key), str) and payload.get(key).strip()),
                    "Human-in-the-Loop request",
                )
                notification = {
                    "call_id": new_call_id, "ring_deadline": deadline,
                    "title": caller_id, "body": " ".join(short_title.split())[:200],
                }
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'resubmit')",
                             (caller_id, request_id, request_encoded, self._dump(outcome)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if notification is not None:
            self._notify(notification)
        return outcome

    def update_task_description(self, caller_id: str, request_id: str, interaction_id: str, call_id: str,
                                expected_task_version: int, task_description: str | None = None,
                                context_note: str | None = None) -> dict[str, object]:
        """Update an in-progress task's description/context without re-ringing."""
        if isinstance(expected_task_version, bool) or not isinstance(expected_task_version, int) or expected_task_version < 0:
            return self._error("invalid_request")
        request_encoded = self._dump({
            "interaction_id": interaction_id, "call_id": call_id,
            "expected_task_version": expected_task_version,
            "task_description": task_description, "context_note": context_note,
        })
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = conn.execute(
                    "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests "
                    "WHERE caller_id=? AND request_id=?",
                    (caller_id, request_id),
                ).fetchone()
                if existing is not None:
                    if existing["operation_kind"] != "task_update" or existing["payload_json"] != request_encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute(
                    "SELECT * FROM lifecycle_calls WHERE call_id=? AND interaction_id=? AND caller_id=?",
                    (call_id, interaction_id, caller_id),
                ).fetchone()
                if call is None:
                    return self._error("session_not_found")
                if call["call_state"] != "in_progress":
                    return self._error("invalid_request")
                if expected_task_version != int(call["task_version"]):
                    return self._error("task_version_conflict")
                try:
                    payload = self._load(call["payload_json"])
                except (TypeError, ValueError, json.JSONDecodeError):
                    payload = {}
                if not isinstance(payload, dict):
                    payload = {}
                if task_description is not None:
                    payload["task_description"] = task_description
                if context_note is not None:
                    payload["context_note"] = context_note
                conn.execute(
                    "UPDATE lifecycle_calls SET payload_json=?, task_version=task_version+1, version=version+1 WHERE call_id=?",
                    (self._dump(payload), call_id),
                )
                outcome = {"success": True, "status": "task_updated", "task_version": int(call["task_version"]) + 1}
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'task_update')",
                             (caller_id, request_id, request_encoded, self._dump(outcome)))
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def directory_view(self) -> list[dict[str, object]]:
        """Public operator directory for agents: callsigns, profiles, channels."""
        view = []
        for callsign, entry in sorted(self._operators.items()):
            view.append({
                "callsign": callsign,
                "display_name": entry.get("display_name") or callsign,
                "language": entry.get("language"),
                "timezone": entry.get("timezone"),
                "primary": entry is not None and bool(entry.get("primary")),
                "mobile": bool(entry.get("mobile")),
            })
        return view

    def resolve_operator(self, callsign: object) -> str | None:
        """Map an optional callsign to a directory operator (default = primary)."""
        if not isinstance(callsign, str) or not callsign.strip():
            return self._primary_operator
        return callsign.strip() if callsign.strip() in self._operators else None

    def operator_password(self, callsign: str) -> str | None:
        entry = self._operators.get(callsign) or {}
        password = entry.get("password")
        return str(password) if isinstance(password, str) and password else None

    def operator_for_device(self, device_id: str) -> str | None:
        """Reverse lookup: which operator owns this desktop device."""
        for callsign, entry in self._operators.items():
            if device_id in self.operator_devices(callsign):
                return callsign
        return None

    def operator_devices(self, callsign: str) -> list[str]:
        entry = self._operators.get(callsign) or {}
        devices = entry.get("devices")
        return [str(d) for d in devices] if isinstance(devices, list) else []

    def operator_mobile(self, callsign: str) -> bool:
        entry = self._operators.get(callsign) or {}
        return bool(entry.get("mobile"))

    def call_operator(self, call_id: str) -> str | None:
        """The operator (callsign) a call round belongs to."""
        with self._connection() as conn:
            row = conn.execute(
                "SELECT operator_callsign FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return str(row["operator_callsign"]) if row is not None and row["operator_callsign"] else None

    def call_version(self, call_id: str) -> int:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT version FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return int(row["version"]) if row is not None else 0

    def call_outcome_view(self, caller_id: str, interaction_id: str, call_id: str) -> dict[str, object] | None:
        """Read-only agent view: settle deadlines, then return the freshest outcome body.

        Active calls yield a heartbeat; terminal calls yield their last
        immutable event (with the agent-side result budget applied).  This
        is the single snapshot builder the waiting long-poll re-reads.
        """
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute(
                    "SELECT * FROM lifecycle_calls WHERE call_id=? AND caller_id=?",
                    (call_id, caller_id),
                ).fetchone()
                if call is None or call["interaction_id"] != interaction_id:
                    conn.commit()
                    return None
                state = str(call["call_state"])
                if state in ACTIVE_STATES:
                    outcome = self._heartbeat(interaction_id, call_id, state)
                else:
                    terminal = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    outcome = self._load(terminal["body_json"]) if terminal is not None else self._terminal(
                        interaction_id, call_id, state, call["reason"], 0,
                    )
                    outcome = self._apply_result_budget(outcome, call["payload_json"])
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    async def wait_call_outcome(self, caller_id: str, interaction_id: str, call_id: str,
                                timeout: float, waiter: Waiter | None = None) -> dict[str, object] | None:
        """Event-driven long-poll: wait once for a terminal state or the budget.

        Wakes on any transition of the call (human accept/decline/submit,
        agent or human cancel, deadline settlement) via the state machine's
        bus — never by re-entering the RPC layer or synthesising request
        ids.  ``waiter`` is the injected logical sleeper; it races the bus
        so tests with fake clocks fast-forward the budget.  A waiter round
        that neither advances logical time nor fires the bus is treated as
        budget expiry, so no configuration can spin.

        Returns ``None`` when the call is unknown to this caller; otherwise
        the freshest view, heartbeat included when the budget expires
        first.
        """
        sleep: Waiter = waiter if waiter is not None else asyncio.sleep
        deadline = self._clock() + max(0.0, float(timeout))
        while True:
            outcome = self.call_outcome_view(caller_id, interaction_id, call_id)
            if outcome is None:
                return None
            if outcome.get("needs_continuation") is not True:
                return outcome
            remaining = deadline - self._clock()
            if remaining <= 0:
                return outcome
            before = self._clock()
            wait_task = asyncio.ensure_future(self._bus.wait_change(call_id, remaining))
            sleep_task = asyncio.ensure_future(sleep(remaining))
            try:
                await asyncio.wait({wait_task, sleep_task}, return_when=asyncio.FIRST_COMPLETED)
                bus_fired = (
                    wait_task.done() and not wait_task.cancelled()
                    and wait_task.exception() is None
                )
            finally:
                for task in (wait_task, sleep_task):
                    task.cancel()
            if bus_fired:
                continue
            # The sleeper finished first.  If it advanced logical time by
            # (nearly) the requested budget, loop once to observe the
            # expired deadline; an instant fake sleeper advanced almost
            # nothing, which we also treat as budget spent so no caller
            # can spin the re-read loop.
            if self._clock() - before + 1e-6 < remaining:
                return outcome

    def read_interaction_result(self, caller_id: str, interaction_id: str,
                                result_id: object = None, cursor: object = None,
                                max_result_bytes: object = None) -> dict[str, object]:
        offset = 0
        if isinstance(cursor, str):
            try:
                parsed = int(cursor)
            except ValueError:
                parsed = 0
            if parsed >= 0:
                offset = parsed
        with self._connection() as conn:
            call = conn.execute(
                "SELECT call_id FROM lifecycle_calls WHERE interaction_id=? AND caller_id=? LIMIT 1",
                (interaction_id, caller_id),
            ).fetchone()
            if call is None:
                return {"success": False, "status": "session_not_found"}
            call_id = call["call_id"]
            body: object = None
            if isinstance(result_id, str) and result_id:
                found = False
                for row in conn.execute(
                    "SELECT outcome_json FROM lifecycle_submissions WHERE call_id=?",
                    (call_id,),
                ).fetchall():
                    try:
                        outcome = self._load(row["outcome_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if outcome.get("result_id") == result_id:
                        body = outcome.get("result")
                        found = True
                        break
                if not found:
                    return {"success": False, "status": "not_found"}
            else:
                submission = conn.execute(
                    "SELECT outcome_json FROM lifecycle_submissions WHERE call_id=? ORDER BY rowid DESC LIMIT 1",
                    (call_id,),
                ).fetchone()
                if submission is not None:
                    try:
                        body = self._load(submission["outcome_json"]).get("result")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        body = None
            attachments = [
                {"attachment_id": row["attachment_id"], "filename": row["filename"], "media_type": row["media_type"],
                 "byte_count": row["byte_count"], "checksum": row["checksum"]}
                for row in conn.execute(
                    "SELECT attachment_id, filename, media_type, byte_count, checksum FROM lifecycle_attachments WHERE call_id=?",
                    (call_id,),
                ).fetchall()
            ]
        result: dict[str, object] = {
            "success": True, "interaction_id": interaction_id, "call_id": call_id,
            "result": body, "attachments": attachments,
        }
        if isinstance(max_result_bytes, int) and not isinstance(max_result_bytes, bool) and max_result_bytes > 0:
            if isinstance(body, Mapping):
                string_lengths = [len(value) for value in body.values() if isinstance(value, str)]
                total_length = max(string_lengths) if string_lengths else 0
                if len(json.dumps(body, separators=(",", ":"))) > max_result_bytes:
                    sliced = {
                        key: (value[offset:offset + max_result_bytes] if isinstance(value, str) else value)
                        for key, value in dict(body).items()
                    }
                    result["result"] = sliced
                    has_more = offset + max_result_bytes < total_length
                    if has_more:
                        result["has_more"] = True
                        result["next_cursor"] = str(offset + max_result_bytes)
                    else:
                        result["has_more"] = False
        return result

    def read_interaction_attachment(self, caller_id: str, interaction_id: str, attachment_id: str,
                                    offset_bytes: object = 0, max_result_bytes: object = None) -> dict[str, object]:
        with self._connection() as conn:
            call = conn.execute(
                "SELECT call_id FROM lifecycle_calls WHERE interaction_id=? AND caller_id=? LIMIT 1",
                (interaction_id, caller_id),
            ).fetchone()
            if call is None:
                return {"success": False, "status": "session_not_found"}
            attachment = conn.execute(
                "SELECT stored_path, byte_count, checksum FROM lifecycle_attachments "
                "WHERE call_id=? AND attachment_id=?",
                (call["call_id"], attachment_id),
            ).fetchone()
        if attachment is None:
            return {"success": False, "status": "session_not_found"}
        try:
            data = Path(attachment["stored_path"]).read_bytes()
        except OSError:
            return {"success": False, "status": "session_not_found"}
        offset = offset_bytes if isinstance(offset_bytes, int) and not isinstance(offset_bytes, bool) and offset_bytes >= 0 else 0
        limit = max_result_bytes if isinstance(max_result_bytes, int) and not isinstance(max_result_bytes, bool) and max_result_bytes > 0 else None
        chunk = data[offset:]
        if limit is not None:
            chunk = chunk[:limit]
        next_offset = offset + len(chunk)
        return {
            "success": True,
            "data": base64.b64encode(chunk).decode("ascii"),
            "checksum": attachment["checksum"],
            "byte_count": int(attachment["byte_count"]),
            "next_offset_bytes": next_offset,
            "has_more": next_offset < int(attachment["byte_count"]),
        }

    def accept(self, *, call_id: str, device_id: str, handled_on: str,
               terminal_platform: str) -> dict[str, object]:
        """Compatibility helper for existing lifecycle consumers.

        The central desktop HTTP boundary above is the only new action route;
        existing lifecycle callers retain their established ownership event.
        """
        del device_id
        return self._human_action(
            call_id, "opened", "accepted", None,
            {"handled_on": handled_on, "terminal_platform": terminal_platform, "human_action": True},
        )

    def finish(self, *, call_id: str, status: str, result: Mapping[str, object]) -> dict[str, object]:
        return self._human_action(call_id, status, status, None, dict(result))

    def _human_action(self, call_id: str, status: str, state: str, reason: str | None, extra: Mapping[str, object]) -> dict[str, object]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None:
                    return self._error("session_not_found")
                if call["call_state"] not in {"ringing", "accepted", "in_progress"}:
                    event = conn.execute("SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,)).fetchone()
                    return self._load(event["body_json"]) if event else self._error("expired")
                if status == "opened" and call["call_state"] != "ringing":
                    return self._error("already_accepted")
                # A final answer cannot establish ownership by itself.  The
                # accepted event is the durable ownership transition and must
                # precede every terminal result.
                if status != "opened" and call["call_state"] not in {"accepted", "in_progress"}:
                    return self._error("not_accepted")
                if status == "opened":
                    _, seq = self._machine.transition(conn, call_id, "accept", reason=reason, bump_version=False)
                else:
                    _, seq = self._machine.transition(conn, call_id, "submit", target=state, reason=reason, bump_version=False)
                body: dict[str, object] = {"success": True, "status": status, "call_state": state, "event_seq": seq,
                                            "needs_continuation": status in {"opened", "in_progress"}, "interaction_id": call["interaction_id"], "call_id": call_id}
                body.update(extra)
                conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(body)))
                conn.commit()
                return body
            except BaseException:
                conn.rollback()
                raise

    def _settle_due(self, conn: sqlite3.Connection) -> None:
        now = self._clock()
        if self._clock_high_water is not None and (
            now < self._clock_high_water or now - self._clock_high_water > 3600
        ):
            self._settle_clock_anomaly(conn)
            self._clock_high_water = now
            return
        self._clock_high_water = now
        rows = conn.execute("SELECT * FROM lifecycle_calls WHERE call_state='ringing' AND ring_deadline<=?", (now,)).fetchall()
        for call in rows:
            _, seq = self._machine.transition(
                conn, call["call_id"], "ring_expire", reason="ring_timeout", bump_version=False,
            )
            body = self._terminal(call["interaction_id"], call["call_id"], "not_accepted", "ring_timeout", seq)
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))
        rows = conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress') "
            "AND ((terminal_disconnect_deadline IS NOT NULL AND terminal_disconnect_deadline<=?) "
            "OR (agent_reconnect_deadline IS NOT NULL AND agent_reconnect_deadline<=?))",
            (now, now),
        ).fetchall()
        for call in rows:
            terminal_deadline = call["terminal_disconnect_deadline"]
            agent_deadline = call["agent_reconnect_deadline"]
            terminal_due = terminal_deadline is not None and terminal_deadline <= now
            agent_due = agent_deadline is not None and agent_deadline <= now
            if agent_due and (not terminal_due or agent_deadline < terminal_deadline):
                reason = "agent_disconnected"
            else:
                reason = "terminal_disconnected"
            _, seq = self._machine.transition(conn, call["call_id"], "disconnect", reason=reason)
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", reason, seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))

    def _settle_clock_anomaly(self, conn: sqlite3.Connection) -> None:
        """Terminate every affected call with an explicit clock-anomaly reason."""
        for call in conn.execute("SELECT * FROM lifecycle_calls WHERE call_state='ringing'").fetchall():
            _, seq = self._machine.transition(
                conn, call["call_id"], "clock_expire", reason="clock_anomaly", bump_version=False,
            )
            body = self._terminal(call["interaction_id"], call["call_id"], "not_accepted", "clock_anomaly", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))
        for call in conn.execute("SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress')").fetchall():
            _, seq = self._machine.transition(conn, call["call_id"], "clock_expire", reason="clock_anomaly")
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", "clock_anomaly", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))

    @staticmethod
    def _interpret_submission(result: Mapping[str, object]) -> tuple[str, str, bool, dict[str, object]]:
        """Map a task-family result onto its top-level status, state and flags.

        A task report carries its own ``status``; a progress report holds the
        round in ``in_progress`` with ``needs_continuation``.  Any other result
        keeps the established single-answer ``answered`` behaviour.
        """
        status = result.get("status")
        if status == "completed":
            extras: dict[str, object] = {"report": result.get("report"), "resubmittable": True}
            if result.get("feedback_requested") is True:
                extras["feedback_requested"] = True
            return "completed", "completed", False, extras
        if status == "failed":
            extras = {"report": result.get("report"), "resubmittable": True}
            if result.get("feedback_requested") is True:
                extras["feedback_requested"] = True
            return "failed", "failed", False, extras
        if status == "in_progress":
            return "in_progress", "in_progress", True, {"progress": result.get("progress")}
        return "answered", "answered", False, {}

    @staticmethod
    def _heartbeat(interaction_id: str, call_id: str, state: str) -> dict[str, object]:
        return {"success": True, "status": "heartbeat", "call_state": state, "interaction_id": interaction_id, "call_id": call_id, "needs_continuation": True}

    @staticmethod
    def _apply_result_budget(outcome: dict[str, object], payload_json: str) -> dict[str, object]:
        """Omit oversized result bodies only on the Agent read path."""
        result = outcome.get("result")
        if not isinstance(result, Mapping):
            return outcome
        try:
            payload = json.loads(payload_json)
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        max_result_bytes = payload.get("max_result_bytes") if isinstance(payload, dict) else None
        if not isinstance(max_result_bytes, int) or isinstance(max_result_bytes, bool) or max_result_bytes < 16384:
            max_result_bytes = 1_000_000
        if len(json.dumps(result, separators=(",", ":"))) <= max_result_bytes:
            return outcome
        omitted = dict(outcome)
        omitted["body_omitted_for_size"] = True
        omitted_result = dict(result)
        for key, value in list(omitted_result.items()):
            if isinstance(value, str):
                omitted_result[key] = None
        omitted["result"] = omitted_result
        return omitted

    @staticmethod
    def _terminal(interaction_id: str, call_id: str, state: str, reason: str | None, seq: int) -> dict[str, object]:
        retryable = reason in {"ring_timeout", "terminal_disconnected", "agent_disconnected"}
        retry_after = 60 if reason == "ring_timeout" else (30 if retryable else None)
        messages = {
            "ring_timeout": "Nobody picked up within the ring window; not a refusal — retry later if you still need the human.",
            "terminal_disconnected": "The human's terminal went away; the round ended.",
            "agent_disconnected": "The calling agent went away; the round ended.",
            "no_available_channel": "The human could not be reached on any channel.",
            "agent_cancelled": "The round was cancelled by the caller.",
        }
        result: dict[str, object] = {
            "success": True, "status": state, "call_state": state, "reason": reason,
            "event_seq": seq, "needs_continuation": False, "interaction_id": interaction_id, "call_id": call_id,
            "human_action": False, "retryable": retryable, "handled_on": None, "terminal_platform": None,
            "message": messages.get(reason, ""),
        }
        if retry_after is not None:
            result["retry_after_seconds"] = retry_after
        return result

    @staticmethod
    def _error(status: str) -> dict[str, object]:
        return {"success": False, "status": status, "needs_continuation": False}

    @staticmethod
    def _dump(value: Mapping[str, object]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _load(value: str) -> dict[str, object]:
        return json.loads(value)


class Application:
    def __init__(self, lifecycle: WaitingLifecycle, credentials: Mapping[str, str], wait: Waiter,
                 deadline_tick_seconds: float, scheduler_sleep: Waiter,
                 operator_session_secret: bytes | None,
                 route_admission: RouteAdmission, notification_router: Any | None,
                 notification_dispatch_tick_seconds: float, notification_scheduler_sleep: Waiter,
                 desktop_registration_service: Any | None = None,
                 operator_session_verifier: Any | None = None,
                 browser_cookie_authenticator: Any | None = None,
                 mobile_html_renderer: Any | None = None,
                 trusted_browser_origin: str | None = None) -> None:
        self.lifecycle, self._credentials, self._wait = lifecycle, dict(credentials), wait
        self._session_secret = operator_session_secret or b"hitl-default-session-secret"
        self._deadline_tick_seconds, self._scheduler_sleep = deadline_tick_seconds, scheduler_sleep
        self._route_admission = route_admission
        self._notification_router = notification_router
        self._desktop_registration_service = desktop_registration_service
        self._operator_session_verifier = operator_session_verifier
        self._browser_cookie_authenticator = browser_cookie_authenticator
        self._mobile_html_renderer = mobile_html_renderer
        self._trusted_browser_origin = trusted_browser_origin
        self._browser_entries: dict[str, str] = {}
        self._logged_out: set[str] = set()
        self._write_claims: set[tuple[str, str]] = set()
        if notification_router is not None:
            sink_setter = getattr(notification_router, "set_delivery_decision_sink", None)
            if callable(sink_setter):
                sink_setter(self.lifecycle.apply_delivery_decisions)
        self._deadline_task: asyncio.Task[None] | None = None
        self._notification_dispatch_tick_seconds = notification_dispatch_tick_seconds
        self._notification_scheduler_sleep = notification_scheduler_sleep
        self._notification_dispatch_task: asyncio.Task[None] | None = None

    async def dispatch_notification_delivery(self) -> dict[str, object]:
        """Dispatch pending notifications and apply their durable decisions.

        The router is responsible for classifying individual delivery
        attempts; the lifecycle is the authority that turns a definitive
        all-channel failure into the Agent-visible terminal outcome.  Keeping
        the bridge here prevents the router from owning call state while
        ensuring its real dispatch result is not discarded.
        """
        if self._notification_router is None:
            return {"call_decisions": {}}
        result = await self._notification_router.dispatch_pending()
        decisions = result.get("call_decisions") if isinstance(result, Mapping) else None
        if isinstance(decisions, Mapping):
            self.lifecycle.apply_delivery_decisions(decisions)
        return result if isinstance(result, dict) else {"call_decisions": {}}

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") == "lifespan":
            await self._lifespan(receive, send)
            return
        if scope.get("type") != "http":
            await self._send(send, 404, self._error("not_found")); return
        path, method = scope.get("path"), scope.get("method")
        if path.startswith("/api/terminals/"):
            await self._terminal_api(method, path, scope.get("headers", []), receive, send)
            return
        if isinstance(path, str) and path.startswith("/api/calls/"):
            await self._terminal_action_api(method, path, scope.get("headers", []), receive, send)
            return
        if isinstance(path, str) and path.startswith("/calls/"):
            await self._browser_call_api(method, path, scope.get("headers", []), receive, send)
            return
        if path == "/operator/login":
            await self._browser_login_api(method, scope.get("headers", []), receive, send)
            return
        if path == "/operator/logout":
            await self._browser_logout_api(method, scope.get("headers", []), receive, send)
            return
        if method != "POST" or path != "/mcp":
            await self._send(send, 404, self._error("not_found")); return
        caller = self._caller(scope.get("headers", []))
        if caller is None:
            await self._send(send, 401, self._error("unauthorized")); return
        body = await self._body(receive)
        if body is None:
            await self._send(send, 400, self._error("invalid_request")); return
        if "jsonrpc" in body or "method" in body or "id" in body:
            session = self._header(scope.get("headers", []), b"mcp-session-id")
            session_token = session.strip() if isinstance(session, str) and session.strip() else None
            result = self._rpc(caller, body, session_token)
            # Event-driven long-poll: a heartbeat with live call ids means
            # the call is active.  Park this response on the state machine's
            # bus until the call reaches a terminal state or the client's
            # declared budget expires — one wait, no re-entered RPC, no
            # synthesised continuation requests.
            params = body.get("params") if isinstance(body.get("params"), dict) else {}
            args = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            sc = result.get("result", {}).get("structuredContent", {})
            if (isinstance(sc, dict) and sc.get("needs_continuation")
                    and sc.get("status") == "heartbeat"
                    and isinstance(sc.get("interaction_id"), str)
                    and isinstance(sc.get("call_id"), str)):
                timeout = args.get("client_timeout_seconds", 30)
                if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
                    budget = max(1.0, float(timeout) - min(30.0, max(1.0, float(timeout) / 4.0)))
                    outcome = await self.lifecycle.wait_call_outcome(
                        caller, str(sc["interaction_id"]), str(sc["call_id"]), budget, waiter=self._wait,
                    )
                    if isinstance(outcome, dict) and outcome.get("status") != "heartbeat":
                        result["result"]["structuredContent"] = outcome
                        after_arg = args.get("after_event_seq")
                        hint = after_arg if isinstance(after_arg, int) and not isinstance(after_arg, bool) and after_arg >= 0 else 0
                        if isinstance(outcome, dict) and outcome.get("needs_continuation") and isinstance(outcome.get("interaction_id"), str):
                            eseq = outcome.get("event_seq")
                            outcome = dict(outcome)
                            outcome["next_after_event_seq"] = eseq if isinstance(eseq, int) and eseq >= 0 else hint
                        result["result"]["content"] = [{"type": "text", "text": self._rpc_summary(str(params.get("name", "")), outcome, hint)}]
            headers: list[tuple[bytes, bytes]] | None = None
            if isinstance(body, Mapping) and body.get("method") == "initialize" and session_token is None:
                headers = [(b"mcp-session-id", uuid.uuid4().hex.encode("ascii"))]
            await self._send(send, 200, result, extra_headers=headers); return
        timeout = body.get("client_timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 2:
            await self._send(send, 400, self._error("invalid_client_timeout")); return
        response, should_wait = self._dispatch(caller, body)
        await send({"type": "http.response.start", "status": 200 if response.get("success") else 400,
                    "headers": [(b"content-type", b"application/json")]})
        waited = should_wait and response.get("needs_continuation") and response.get("status") == "heartbeat"
        if waited:
            budget = max(1.0, float(timeout) - min(30.0, max(1.0, float(timeout) / 4.0)))
            outcome = await self.lifecycle.wait_call_outcome(
                caller, str(response.get("interaction_id")), str(response.get("call_id")), budget, waiter=self._wait,
            )
            if isinstance(outcome, dict) and outcome.get("status") != "heartbeat":
                response = outcome
        await send({"type": "http.response.body", "body": json.dumps(response, separators=(",", ":")).encode(), "more_body": False})
        if should_wait:
            self.lifecycle.response_delivered(caller, response)

    async def _terminal_api(self, method: object, path: object, headers: object, receive: Any, send: Any) -> None:
        service = self._desktop_registration_service
        if service is None:
            await self._send(send, 404, self._error("not_found")); return
        if method == "POST" and path == "/api/terminals/register":
            body = await self._body(receive)
            if body is None:
                await self._send(send, 400, self._error("invalid_request")); return
            callsign = body.get("callsign")
            password = body.get("password")
            device_id = body.get("device_id")
            platform = body.get("terminal_platform", "unknown")
            endpoint, gui_ready = body.get("endpoint"), body.get("gui_ready")
            if (not isinstance(callsign, str) or not callsign or not isinstance(password, str)
                    or not isinstance(device_id, str) or not isinstance(endpoint, str)
                    or not isinstance(gui_ready, bool) or not isinstance(platform, str)):
                await self._send(send, 400, self._error("invalid_request")); return
            response = service.register(callsign=callsign, password=password, device_id=device_id,
                                         terminal_platform=platform, endpoint=endpoint, gui_ready=gui_ready)
            await self._send(send, 201 if response.get("success") else 401, response); return
        if path not in {"/api/terminals/heartbeat", "/api/terminals/state", "/api/terminals/dnd", "/api/terminals/quiet"}:
            await self._send(send, 404, self._error("not_found")); return
        session = self._terminal_session(headers)
        if session is None:
            await self._send(send, 401, self._terminal_error("session_not_found")); return
        if method == "POST" and path == "/api/terminals/heartbeat":
            body = await self._body(receive)
            gui_ready = body.get("gui_ready") if body is not None else None
            if not isinstance(gui_ready, bool):
                await self._send(send, 400, self._error("invalid_request")); return
            response = service.heartbeat(session_id=session, gui_ready=gui_ready)
        elif method == "GET" and path == "/api/terminals/state":
            response = service.states(session_id=session)
            context = self._terminal_context(session)
            if response.get("success") and context is not None:
                device_id, _platform, _operator = context
                central_states = self.lifecycle.terminal_states(device_id)
                if central_states:
                    reported = response.get("states")
                    merged = {
                        str(item["call_id"]): dict(item)
                        for item in reported if isinstance(reported, list) and isinstance(item, Mapping)
                        and isinstance(item.get("call_id"), str)
                    }
                    merged.update({str(item["call_id"]): item for item in central_states})
                    response = {**response, "states": [merged[key] for key in sorted(merged)]}
                if context is not None:
                    response = {**response, "operator": context[2]}
        elif method == "POST" and path == "/api/terminals/dnd":
            session = self._terminal_session(headers)
            context = self._terminal_context(session) if session is not None else None
            if context is None:
                await self._send(send, 401, self._terminal_error("session_not_found")); return
            session_operator = context[2]
            body = await self._body(receive)
            dnd = body.get("dnd") if body is not None else None
            message = body.get("message") if body is not None else ""
            if not isinstance(dnd, bool):
                await self._send(send, 400, self._error("invalid_request")); return
            if not isinstance(message, str):
                message = ""
            response = self.lifecycle.set_dnd(dnd, message, operator_id=session_operator)
        elif method == "GET" and path == "/api/terminals/dnd":
            session = self._terminal_session(headers)
            context = self._terminal_context(session) if session is not None else None
            if context is None:
                await self._send(send, 401, self._terminal_error("session_not_found")); return
            session_operator = context[2]
            response = self.lifecycle.get_dnd(operator_id=session_operator)
        elif method == "POST" and path == "/api/terminals/quiet":
            session = self._terminal_session(headers)
            context = self._terminal_context(session) if session is not None else None
            if context is None:
                await self._send(send, 401, self._terminal_error("session_not_found")); return
            session_operator = context[2]
            body = await self._body(receive)
            quiet_window_seconds = body.get("quiet_window_seconds") if body is not None else None
            quiet_mode = body.get("quiet_mode") if body is not None else None
            if isinstance(quiet_window_seconds, bool) or not isinstance(quiet_window_seconds, int) or quiet_window_seconds < 0:
                await self._send(send, 400, self._error("invalid_request")); return
            if quiet_mode not in {"short", "silent"}:
                await self._send(send, 400, self._error("invalid_request")); return
            response = self.lifecycle.set_quiet_window(quiet_window_seconds, quiet_mode, operator_id=session_operator)
        elif method == "GET" and path == "/api/terminals/quiet":
            session = self._terminal_session(headers)
            context = self._terminal_context(session) if session is not None else None
            if context is None:
                await self._send(send, 401, self._terminal_error("session_not_found")); return
            session_operator = context[2]
            response = self.lifecycle.get_quiet_window(operator_id=session_operator)
        else:
            await self._send(send, 404, self._error("not_found")); return
        await self._send(send, self._action_status(response), response)

    async def _terminal_action_api(self, method: object, path: object, headers: object, receive: Any, send: Any) -> None:
        if not isinstance(path, str):
            await self._send(send, 404, self._error("not_found")); return
        parts = path.split("/")
        if method == "GET" and len(parts) == 4 and parts[:3] == ["", "api", "calls"]:
            session = self._terminal_session(headers)
            context = self._terminal_context(session) if session is not None else None
            if context is None:
                await self._send(send, 401, self._terminal_error("session_not_found")); return
            response = self._terminal_call_view(parts[3], context[0])
            await self._send(send, self._action_status(response), response); return
        if method == "PUT" and len(parts) == 5 and parts[:3] == ["", "api", "calls"] and parts[4] == "draft":
            session = self._terminal_session(headers)
            context = self._terminal_context(session) if session is not None else None
            if context is None:
                await self._send(send, 401, self._terminal_error("session_not_found")); return
            device_id, _platform, _operator = context
            body = await self._body(receive)
            draft_version = body.get("draft_version") if body is not None else None
            content = body.get("content") if body is not None else None
            if isinstance(draft_version, bool) or not isinstance(draft_version, int) or not isinstance(content, str):
                await self._send(send, 400, self._error("invalid_request")); return
            response = self.lifecycle.save_draft(
                call_id=parts[3], device_id=device_id, session_id=session,
                draft_version=draft_version, content=content,
            )
            await self._send(send, self._action_status(response), response); return
        if method != "POST":
            await self._send(send, 404, self._error("not_found")); return
        if len(parts) != 5 or parts[:3] != ["", "api", "calls"] or parts[4] not in {"accept", "decline", "heartbeat", "submissions", "cancel", "attachments"}:
            await self._send(send, 404, self._error("not_found")); return
        session = self._terminal_session(headers)
        context = self._terminal_context(session) if session is not None else None
        if context is None:
            await self._send(send, 401, self._terminal_error("session_not_found")); return
        device_id, platform, _operator = context
        if parts[4] == "heartbeat":
            await self._body(receive)
            response = self.lifecycle.terminal_heartbeat(call_id=parts[3], session_id=session)
        elif parts[4] == "submissions":
            body = await self._body(receive)
            submission_id = body.get("submission_id") if body is not None else None
            result = body.get("result") if body is not None else None
            if not isinstance(submission_id, str) or not isinstance(result, Mapping):
                await self._send(send, 400, self._error("invalid_request")); return
            task_version = body.get("task_version") if body is not None else None
            response = self.lifecycle.submit(
                call_id=parts[3], device_id=device_id, session_id=session,
                submission_id=submission_id, result=result, task_version=task_version,
            )
        elif parts[4] == "cancel":
            body = await self._body(receive)
            operation_id = body.get("operation_id") if body is not None else None
            if not isinstance(operation_id, str) or not operation_id:
                await self._send(send, 400, self._error("invalid_request")); return
            response = self.lifecycle.terminal_cancel(
                call_id=parts[3], device_id=device_id, session_id=session,
                operation_id=operation_id,
            )
        elif parts[4] == "attachments":
            body = await self._body(receive)
            upload = self._attachment_upload(body)
            if isinstance(upload, str):
                await self._send(send, 400, self._error(upload)); return
            filename, media_type, content = upload
            response = self.lifecycle.upload_attachment(
                call_id=parts[3], device_id=device_id, session_id=session,
                filename=filename, media_type=media_type, content=content,
            )
        else:
            body = await self._body(receive)
            operation_id = body.get("operation_id") if body is not None else None
            if not isinstance(operation_id, str) or not operation_id:
                await self._send(send, 400, self._error("invalid_request")); return
            response = self.lifecycle.terminal_action(
                call_id=parts[3], device_id=device_id, session_id=session, terminal_platform=platform,
                operation_id=operation_id, action=parts[4],
            )
        await self._send(send, self._action_status(response), response)

    def _terminal_call_view(self, call_id: str, device_id: str) -> dict[str, object]:
        """Return the immutable presentation data only to its invited terminal."""
        with self.lifecycle._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self.lifecycle._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if call is None:
            return self._terminal_error("session_not_found")
        state = str(call["call_state"])
        invited = call["invited_desktop_id"] == device_id
        owned = call["owner_device_id"] == device_id
        if not ((state == "ringing" and invited) or owned):
            return self._terminal_error("session_not_found")
        try:
            payload = self.lifecycle._load(call["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        return {
            "success": True, "call_id": call_id, "call_state": state,
            "version": call["version"], "handled_on": call["handled_on"],
            "terminal_platform": call["terminal_platform"],
            "can_accept": state == "ringing" and invited,
            "can_decline": state == "ringing" and invited,
            "payload": payload if isinstance(payload, Mapping) else {},
        }

    async def _browser_call_api(self, method: object, path: object, headers: object, receive: Any, send: Any) -> None:
        if not isinstance(path, str):
            await self._send(send, 404, self._error("not_found")); return
        parts = path.split("/")
        if len(parts) not in {3, 4} or parts[:2] != ["", "calls"] or not isinstance(parts[2], str) or not parts[2]:
            await self._send(send, 404, self._error("not_found")); return
        session = self._browser_session(headers)
        if session is None:
            if method == "GET" and self._browser_login_is_configured():
                entry = uuid.uuid4().hex
                self._browser_entries[entry] = str(path)
                await self._send_html(
                    send, 401, self._render_login_entry(),
                    headers=[(b"set-cookie", self._browser_entry_cookie(entry).encode("ascii"))],
                )
                return
            await self._send(send, 401, self._terminal_error("session_not_found")); return
        operator_id, browser_session_id, csrf_token = session
        cookie_mode = self._browser_cookie_authenticator is not None
        call_id = parts[2]
        if len(parts) == 3 and method == "GET":
            response = self._browser_call_view(call_id, operator_id, browser_session_id)
            if response.get("success") and response.get("can_submit"):
                key = (browser_session_id, call_id)
                if key in self._write_claims:
                    response["can_submit"] = False
                else:
                    self._write_claims.add(key)
            if response.get("success") and cookie_mode:
                await self._send_html(send, 200, self._render_browser_page(response, csrf_token)); return
            await self._send(send, self._action_status(response), response); return
        if len(parts) != 4 or method != "POST" or parts[3] not in {"accept", "decline", "submissions", "attachments"}:
            await self._send(send, 404, self._error("not_found")); return
        if cookie_mode:
            if not self._browser_write_is_trusted(headers):
                await self._send(send, 403, self._terminal_error("session_not_found")); return
            body = await self._form_body(receive)
            if body is None or not hmac.compare_digest(str(body.get("csrf_token", "")), csrf_token):
                await self._send(send, 403, self._terminal_error("session_not_found")); return
        else:
            body = await self._body(receive)
        if parts[3] == "submissions":
            submission_id = body.get("submission_id") if body is not None else None
            result = body.get("result") if body is not None else None
            if isinstance(result, str):
                try:
                    result = json.loads(result)
                except json.JSONDecodeError:
                    result = None
            if not isinstance(submission_id, str) or not isinstance(result, Mapping):
                await self._send(send, 400, self._error("invalid_request")); return
            response = self.lifecycle.submit(
                call_id=call_id, device_id="iphone_web", session_id=browser_session_id,
                submission_id=submission_id, result=result,
            ) if operator_id == (self.lifecycle.call_operator(call_id) or self.lifecycle._operator_id) else self._terminal_error("session_not_found")
            if cookie_mode:
                await self._send_html(send, self._action_status(response), self._render_browser_page(response, csrf_token, response)); return
            await self._send(send, self._action_status(response), response); return
        if parts[3] == "attachments":
            upload = self._attachment_upload(body)
            if isinstance(upload, str):
                await self._send(send, 400, self._error(upload)); return
            filename, media_type, content = upload
            response = self.lifecycle.upload_attachment(
                call_id=call_id, device_id="iphone_web", session_id=browser_session_id,
                filename=filename, media_type=media_type, content=content,
            ) if operator_id == (self.lifecycle.call_operator(call_id) or self.lifecycle._operator_id) else self._terminal_error("session_not_found")
            if cookie_mode:
                await self._send_html(send, self._action_status(response), self._render_browser_page(response, csrf_token, response)); return
            await self._send(send, self._action_status(response), response); return
        operation_id = body.get("operation_id") if body is not None else None
        if not isinstance(operation_id, str) or not operation_id:
            await self._send(send, 400, self._error("invalid_request")); return
        response = self.lifecycle.browser_action(
            call_id=call_id, operator_id=operator_id, session_id=browser_session_id,
            operation_id=operation_id, action=parts[3],
        )
        if parts[3] == "accept" and response.get("success") and response.get("call_state") == "accepted":
            router = self._notification_router
            if router is not None:
                dispatch = getattr(router, "dispatch_desktop_state_change", None)
                if callable(dispatch):
                    await dispatch(call_id, "accepted", self.lifecycle.call_version(call_id), "iphone_web")
        if cookie_mode:
            view = self._browser_call_view(call_id, operator_id, browser_session_id)
            await self._send_html(send, self._action_status(response), self._render_browser_page(view, csrf_token, response))
        else:
            await self._send(send, self._action_status(response), response)

    async def _browser_login_api(self, method: object, headers: object, receive: Any, send: Any) -> None:
        """Establish a browser cookie only from an opaque, same-origin entry."""
        if method != "POST" or not self._browser_login_is_configured():
            await self._send(send, 404, self._error("not_found")); return
        if not self._browser_write_is_trusted(headers):
            await self._send(send, 403, self._terminal_error("session_not_found")); return
        entry = self._cookie(headers, "browser_entry")
        destination = self._browser_entries.get(entry or "")
        body = await self._form_body(receive)
        callsign = body.get("callsign") if body is not None else None
        password = (body.get("password") or body.get("credential")) if body is not None else None
        session_cookie = None
        authenticate = getattr(self._browser_cookie_authenticator, "authenticate_login", None)
        if callable(authenticate) and not callsign:
            session_cookie = authenticate(password)
        elif isinstance(callsign, str) and callsign and isinstance(password, str):
            expected = self.lifecycle.operator_password(callsign)
            import hmac as _hmac
            if expected is not None and _hmac.compare_digest(password, expected):
                session_cookie = self._mint_operator_session(callsign)
        if not isinstance(destination, str) or not isinstance(session_cookie, str) or not session_cookie:
            await self._send(send, 401, self._terminal_error("session_not_found")); return
        del self._browser_entries[entry]
        await self._send_redirect(
            send, destination,
            headers=[(b"set-cookie", self._operator_session_cookie(session_cookie).encode("ascii"))],
        )

    def _browser_login_is_configured(self) -> bool:
        return callable(getattr(self._browser_cookie_authenticator, "authenticate_login", None))

    async def _browser_logout_api(self, method: object, headers: object, receive: Any, send: Any) -> None:
        if method != "POST":
            await self._send(send, 404, self._error("not_found")); return
        await self._body(receive)
        cookie = self._cookie(headers, "operator_session")
        if cookie is not None:
            self._logged_out.add(cookie)
        await self._send_redirect(send, "/")

    @staticmethod
    def _browser_entry_cookie(value: str) -> str:
        return f"browser_entry={value}; Path=/; Secure; HttpOnly; SameSite=Lax"

    @staticmethod
    def _operator_session_cookie(value: str) -> str:
        return f"operator_session={value}; Path=/; Secure; HttpOnly; SameSite=Lax"

    def _mint_operator_session(self, callsign: str) -> str:
        """Mint an HMAC-signed browser session token for one operator."""
        import hmac as _hmac
        payload = json.dumps({
            "operator": callsign,
            "browser_session_id": uuid.uuid4().hex,
            "csrf_token": uuid.uuid4().hex,
            "exp": self.lifecycle._clock() + 30 * 24 * 3600,
        }, sort_keys=True, separators=(",", ":")).encode()
        mac = _hmac.new(self._session_secret, payload, hashlib.sha256).hexdigest()
        return base64.b64encode(payload).decode() + "." + mac

    def _verify_operator_session_token(self, token: str | None) -> dict[str, str] | None:
        import hmac as _hmac
        if not isinstance(token, str) or "." not in token:
            return None
        raw, mac = token.rsplit(".", 1)
        try:
            payload = base64.b64decode(raw.encode())
        except (ValueError, TypeError):
            return None
        expected = _hmac.new(self._session_secret, payload, hashlib.sha256).hexdigest()
        if not _hmac.compare_digest(mac, expected):
            return None
        try:
            data = json.loads(payload)
        except (ValueError, TypeError):
            return None
        if not isinstance(data, Mapping) or data.get("exp", 0) < self.lifecycle._clock():
            return None
        operator = data.get("operator")
        browser_session = data.get("browser_session_id")
        csrf = data.get("csrf_token")
        if not all(isinstance(v, str) and v for v in (operator, browser_session, csrf)):
            return None
        if operator not in self.lifecycle._operators:
            return None
        return {"operator_id": str(operator), "browser_session_id": browser_session, "csrf_token": csrf}

    @staticmethod
    def _render_login_entry() -> str:
        return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
                "<title>登录</title>"
                "<link href=\"https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css\" rel=\"stylesheet\">"
                "<style>body{background:#f5f6f8;padding:16px 0}.card{max-width:400px;width:calc(100% - 32px);margin:60px auto;border:0;border-radius:16px;box-shadow:0 2px 12px rgba(0,0,0,.08)}</style>"
                "</head><body><div class=\"card\"><div class=\"card-body\">"
                "<h5 class=\"mb-3 text-center\">操作者登录</h5>"
                "<form method=\"post\" action=\"/operator/login\">"
                "<div class=\"mb-3\"><label class=\"form-label\" for=\"operator-callsign\">呼号</label>"
                "<input class=\"form-control\" id=\"operator-callsign\" type=\"text\" name=\"callsign\" required autofocus></div>"
                "<div class=\"mb-3\"><label class=\"form-label\" for=\"operator-password\">密码</label>"
                "<input class=\"form-control\" id=\"operator-password\" type=\"password\" name=\"password\" required></div>"
                "<button type=\"submit\" class=\"btn btn-primary w-100\">登录</button>"
                "</form></div></div></body></html>")

    def _browser_call_view(self, call_id: str, operator_id: str,
                           browser_session_id: str | None = None) -> dict[str, object]:
        call_op = self.lifecycle.call_operator(call_id)
        if operator_id != (call_op or self.lifecycle._operator_id):
            return self._terminal_error("session_not_found")
        with self.lifecycle._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self.lifecycle._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if call is None or not bool(call["mobile_eligible"]):
            return self._terminal_error("session_not_found")
        state = str(call["call_state"])
        view: dict[str, object] = {
            "success": True, "call_id": call_id, "call_state": state,
            "handled_on": call["handled_on"], "terminal_platform": call["terminal_platform"],
            "can_accept": state == "ringing", "can_decline": state == "ringing",
            "can_submit": (
                state in {"accepted", "in_progress"}
                and call["handled_on"] == "iphone_web"
                and call["owner_session_id"] == browser_session_id
            ),
        }
        try:
            payload = self.lifecycle._load(call["payload_json"])
        except (TypeError, ValueError, json.JSONDecodeError):
            payload = {}
        # The stored creation payload is presentation data only.  Submission
        # remains centrally authorized and validated; a browser never gets to
        # choose a different call or alter the persisted request.
        view["payload"] = payload if isinstance(payload, Mapping) else {}
        if state in {"accepted", "in_progress"} and call["handled_on"] == "desktop":
            view["display_state"] = "handled_on_desktop_processing"
        elif state in {"answered", "completed", "failed"} and call["handled_on"] == "desktop":
            view["display_state"] = "handled_on_desktop_complete"
        return view

    def _terminal_context(self, session_id: str | None) -> tuple[str, str, str] | None:
        """Session identity: (device_id, terminal_platform, operator_callsign)."""
        service = self._desktop_registration_service
        if service is None or not isinstance(session_id, str):
            return None
        sessions = getattr(service, "_sessions", None)
        if not isinstance(sessions, Mapping):
            return None
        context = sessions.get(session_id)
        if isinstance(context, tuple) and len(context) == 3:
            return context  # device_id, platform, operator
        if isinstance(context, str):
            return context, "unknown", self.lifecycle._primary_operator
        return None

    def _invited_desktop_id(self, callsign: str | None = None) -> str | None:
        router = self._notification_router
        if getattr(router, "_route_mode", None) not in {"desktop", "both"}:
            return None
        registry = getattr(router, "_registry", None) if router is not None else None
        if registry is None:
            return None
        if callsign:
            # Invite the freshest GUI-ready device OWNED by this operator.
            best_device, best_seen = None, -1.0
            for device_id in self.lifecycle.operator_devices(callsign):
                state = registry.desktop_state(device_id)
                if isinstance(state, Mapping) and state.get("gui_ready") and state.get("fresh"):
                    row_seen = getattr(registry, "_store", None)
                    seen = None
                    if row_seen is not None:
                        with row_seen.connection() as conn:
                            row = conn.execute(
                                "SELECT last_seen FROM desktop_terminals WHERE device_id=?", (device_id,)
                            ).fetchone()
                            seen = row["last_seen"] if row is not None else None
                    if seen is not None and float(seen) > best_seen:
                        best_device, best_seen = device_id, float(seen)
            if best_device is not None:
                return best_device
            return None
        ready_desktop = getattr(registry, "ready_desktop", None)
        if not callable(ready_desktop):
            return None
        terminal = ready_desktop()
        device_id = terminal.get("device_id") if isinstance(terminal, Mapping) else None
        return device_id if isinstance(device_id, str) else None

    def _mobile_eligible(self, callsign: str | None = None) -> bool:
        if getattr(self._notification_router, "_route_mode", None) not in {"iphone", "both"}:
            return False
        if callsign:
            return self.lifecycle.operator_mobile(callsign)
        return True

    async def _lifespan(self, receive: Any, send: Any) -> None:
        while True:
            event = await receive()
            if event.get("type") == "lifespan.startup":
                self._deadline_task = asyncio.create_task(self._run_deadline_scheduler())
                self._notification_dispatch_task = asyncio.create_task(self._run_notification_dispatch_scheduler())
                await send({"type": "lifespan.startup.complete"})
            elif event.get("type") == "lifespan.shutdown":
                await self._stop_scheduler("_notification_dispatch_task")
                await self._stop_scheduler("_deadline_task")
                await send({"type": "lifespan.shutdown.complete"})
                return

    async def _stop_scheduler(self, attribute: str) -> None:
        task = getattr(self, attribute)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
            setattr(self, attribute, None)

    async def _run_deadline_scheduler(self) -> None:
        while True:
            await self._scheduler_sleep(self._deadline_tick_seconds)
            self.lifecycle.settle_deadlines()

    async def _run_notification_dispatch_scheduler(self) -> None:
        while True:
            await self._notification_scheduler_sleep(self._notification_dispatch_tick_seconds)
            await self.dispatch_notification_delivery()

    def _rpc(self, caller: str, body: Mapping[str, object], session: str | None = None) -> dict[str, object]:
        rid = body.get("id")
        jsonrpc = body.get("jsonrpc")
        method = body.get("method")
        if jsonrpc != "2.0":
            return self._rpc_error(rid, -32600, "Invalid Request")
        if not isinstance(method, str) or not method:
            return self._rpc_error(rid, -32600, "Invalid Request")
        try:
            if method == "initialize":
                result = self._rpc_initialize()
            elif method == "tools/list":
                result = {"tools": self._tool_catalog()}
            elif method == "tools/call":
                result = self._rpc_tools_call(caller, body.get("params"), rid, session)
            else:
                return self._rpc_error(rid, -32601, "Method not found")
        except _RpcError as error:
            return self._rpc_error(rid, error.code, error.message)
        return {"jsonrpc": "2.0", "id": rid, "result": result}

    @staticmethod
    def _rpc_initialize() -> dict[str, object]:
        return {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "hitl-central-mcp", "version": "1.0.0"},
            "instructions": _HUMAN_LOOP_MANUAL,
        }

    @staticmethod
    def _tool_catalog() -> list[dict[str, object]]:
        return [
            {"name": tool["name"], "description": tool["description"], "inputSchema": tool["inputSchema"]}
            for tool in _MCP_TOOLS
        ]

    @staticmethod
    def _tool_by_name(name: str) -> dict[str, object] | None:
        for tool in _MCP_TOOLS:
            if tool["name"] == name:
                return tool
        return None

    def _rpc_tools_call(self, caller: str, params: object, rid: object, session: str | None = None) -> dict[str, object]:
        if not isinstance(params, Mapping):
            raise _RpcError(-32602, "Invalid params")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise _RpcError(-32602, "Invalid params")
        tool = self._tool_by_name(name)
        if tool is None:
            raise _RpcError(-32602, "Invalid params")
        arguments = params.get("arguments")
        if not isinstance(arguments, Mapping):
            arguments = {}
        kind = tool["kind"]
        if kind == "interaction":
            structured = self._rpc_interaction(caller, name, arguments, rid, session)
        elif kind == "control":
            structured = self._rpc_cancel(caller, arguments, rid, session)
        elif kind == "read":
            structured = self._rpc_read_tool(caller, name, arguments)
        else:
            structured = self._rpc_readonly(name, arguments)
        after = arguments.get("after_event_seq")
        cursor_hint = after if isinstance(after, int) and not isinstance(after, bool) and after >= 0 else 0
        if isinstance(structured, dict) and structured.get("needs_continuation") and isinstance(structured.get("interaction_id"), str):
            seq = structured.get("event_seq")
            structured = dict(structured)
            structured["next_after_event_seq"] = seq if isinstance(seq, int) and seq >= 0 else cursor_hint
        return {
            "content": [{"type": "text", "text": self._rpc_summary(name, structured, cursor_hint)}],
            "isError": False,
            "structuredContent": structured,
        }

    def _rpc_interaction(self, caller: str, name: str, arguments: Mapping[str, object], rid: object, session: str | None = None) -> dict[str, object]:
        request_id = self._rpc_request_id(rid, name, arguments, session)
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        interaction_present = isinstance(interaction_id, str) and bool(interaction_id)
        call_present = isinstance(call_id, str) and bool(call_id)
        if interaction_present != call_present:
            return {"success": False, "status": "missing_call_id", "needs_continuation": False}
        if interaction_present:
            after = arguments.get("after_event_seq", 0)
            if isinstance(after, bool) or not isinstance(after, int):
                raise _RpcError(-32602, "Invalid params")
            result = self.lifecycle.continue_call(caller, request_id, interaction_id, call_id, after)
        else:
            max_result_bytes = arguments.get("max_result_bytes")
            if max_result_bytes is not None and (
                isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int)
                or max_result_bytes < 16384
            ):
                return {"success": False, "status": "invalid_result_budget", "needs_continuation": False}
            callsign = arguments.get("callsign")
            if callsign is not None and not isinstance(callsign, str):
                return {"success": False, "status": "invalid_request", "needs_continuation": False}
            payload = {"tool": name, **{k: v for k, v in dict(arguments).items() if k != "callsign"}}
            result = self.lifecycle.create(
                caller, request_id, payload, self._invited_desktop_id(callsign), self._mobile_eligible(callsign),
                operator_callsign=callsign,
            )
        if result is None:
            result = {"success": False, "status": "session_not_found", "needs_continuation": False}
        return dict(result)

    def _rpc_cancel(self, caller: str, arguments: Mapping[str, object], rid: object, session: str | None = None) -> dict[str, object]:
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        if not isinstance(interaction_id, str) or not interaction_id or not isinstance(call_id, str) or not call_id:
            raise _RpcError(-32602, "Invalid params")
        return dict(self.lifecycle.cancel(caller, self._rpc_request_id(rid, "cancel_interaction", arguments, session), interaction_id, call_id))

    @staticmethod
    def _rpc_request_id(rid: object, name: str, arguments: Mapping[str, object],
                        session: str | None = None) -> str:
        """Namescope a JSON-RPC id with its payload fingerprint.

        JSON-RPC request ids are unique only within one client session, but
        the idempotency store keys them forever.  MCP hosts restart their
        id counter on every reconnect (initialize=1, tools/list=2, ...), so
        a fresh session inevitably reuses small ids.  Hashing the payload
        into the request id keeps genuine retries (same id, same payload)
        replaying the cached outcome while a reused id carrying different
        arguments executes as the new request it actually is, instead of
        poisoning the client with ``request_conflict``.
        """
        fingerprint = hashlib.sha1(
            f"{name}|{json.dumps(dict(arguments), sort_keys=True, separators=(',', ':'))}".encode()
        ).hexdigest()[:8]
        # The MCP streamable-HTTP session id (echoed by the client after
        # initialize) scopes ids to one client session: genuine retries
        # replay, while a fresh session never collides with -- or replays --
        # an earlier session's row even when both reuse small ids with
        # identical payloads.  Clients that never echo a session id fall
        # back to the bare payload fingerprint.
        prefix = f"{session[:12]}-" if isinstance(session, str) and session else ""
        return f"rpc-{prefix}{rid}-{fingerprint}"

    def _rpc_read_tool(self, caller: str, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        if name == "read_interaction_result":
            interaction_id = arguments.get("interaction_id")
            if not isinstance(interaction_id, str) or not interaction_id:
                raise _RpcError(-32602, "Invalid params")
            return self.lifecycle.read_interaction_result(
                caller, interaction_id,
                result_id=arguments.get("result_id"),
                cursor=arguments.get("cursor"),
                max_result_bytes=arguments.get("max_result_bytes"),
            )
        if name == "read_interaction_attachment":
            interaction_id = arguments.get("interaction_id")
            attachment_id = arguments.get("attachment_id")
            if not isinstance(interaction_id, str) or not interaction_id or not isinstance(attachment_id, str) or not attachment_id:
                raise _RpcError(-32602, "Invalid params")
            return self.lifecycle.read_interaction_attachment(
                caller, interaction_id, attachment_id,
                offset_bytes=arguments.get("offset_bytes", 0),
                max_result_bytes=arguments.get("max_result_bytes"),
            )
        raise _RpcError(-32602, "Invalid params")

    def _rpc_readonly(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
        if name == "list_operators":
            listing = self.lifecycle.directory_view()
            for entry in listing:
                dnd = self.lifecycle.get_dnd(operator_id=str(entry["callsign"]))
                entry["dnd"] = bool(dnd.get("enabled"))
                entry["dnd_message"] = dnd.get("message", "")
            return {"success": True, "status": "ok", "result": {"operators": listing}}
        if name == "get_operator_profile":
            callsign = arguments.get("callsign") if isinstance(arguments, Mapping) else None
            target = self.lifecycle.resolve_operator(callsign)
            if target is None:
                return {"success": False, "status": "unknown_callsign", "needs_continuation": False,
                        "message": f"no operator with callsign {callsign!r}; call list_operators"}
            entry = self.lifecycle._operators.get(target, {})
            profile: dict[str, object] = {"operator_id": target, "callsign": target}
            for field in ("display_name", "language", "timezone", "notes"):
                if entry.get(field) is not None:
                    profile[field] = entry[field]
            if not any(k in profile for k in ("display_name", "language", "timezone", "notes")):
                profile.update(self.lifecycle._operator_profile)
            profile["primary"] = bool(entry.get("primary"))
            return {"success": True, "status": "ok", "result": profile}
        if name == "health_check":
            router = self._notification_router
            desktop_online = False
            bark_configured = False
            recent_outcome = None
            if router is not None:
                online = getattr(router, "desktop_online", None)
                desktop_online = bool(online()) if callable(online) else False
                configured = getattr(router, "bark_configured", None)
                bark_configured = bool(configured()) if callable(configured) else False
                recent = getattr(router, "recent_bark_delivery", None)
                recent_outcome = recent() if callable(recent) else None
            return {
                "success": True, "status": "healthy", "storage": "sqlite", "scheduler": "ready",
                "desktop_online": desktop_online,
                "bark": {"configured": bark_configured, "recent_outcome": recent_outcome},
            }
        if name == "get_human_loop_prompt":
            return {"success": True, "status": "ok", "prompt": _HUMAN_LOOP_MANUAL}
        raise _RpcError(-32602, "Invalid params")

    @staticmethod
    def _rpc_summary(name: str, structured: Mapping[str, object], cursor_hint: int = 0) -> str:
        status = structured.get("status", structured.get("call_state", "ok"))
        iid = structured.get("interaction_id")
        cid = structured.get("call_id")
        needs = structured.get("needs_continuation")
        result = structured.get("result")
        if needs and iid and cid:
            # Cursor rule: a response that carries an event advances the
            # cursor to that event_seq; a heartbeat (no new event) keeps the
            # cursor the caller already used.  Never fall back to 0 — a
            # reset re-drains the oldest event forever and hides every later
            # one (accept, progress, cancel) from the agent.
            nxt = structured.get("next_after_event_seq")
            seq = structured.get("event_seq")
            cursor = (nxt if isinstance(nxt, int) and nxt >= 0
                      else seq if isinstance(seq, int) and seq >= 0
                      else max(0, cursor_hint))
            state = structured.get("call_state")
            if status == "opened" or state == "accepted":
                meaning = "The human ACCEPTED this task and is working on it."
            elif status == "in_progress" or state == "in_progress":
                meaning = "The human is still working (progress report included)." if result is not None else "The human is still working."
            else:
                meaning = "The human has not picked up yet. The task is NOT done and NOT failed."
            summary = (
                f"{name}: still waiting ({status}). {meaning} "
                f"Keep waiting: call {name} again with interaction_id={iid}, call_id={cid}, "
                f"after_event_seq={cursor} (keep exactly this value until a response contains a new event_seq), "
                f"client_timeout_seconds=30. Each call already waits server-side; do not re-call immediately and do not conclude the task while it is still open."
            )
            if result is not None:
                summary += f" Partial result: {json.dumps(result, ensure_ascii=False)}"
            return summary
        if result is not None:
            summary = f"{name}: {status}. Result: {json.dumps(result, ensure_ascii=False)}"
            attachments = structured.get("attachments")
            if isinstance(attachments, list) and attachments:
                listed = ", ".join(
                    f"{a.get('filename', '?')} ({a.get('byte_count', '?')} B, id {str(a.get('attachment_id', ''))[:8]})"
                    for a in attachments if isinstance(a, Mapping))
                summary += (f" Attachments: {listed}. Fetch their bytes with read_interaction_attachment "
                            f"(interaction_id + attachment_id, paginate with offset_bytes).")
            return summary
        detail = ""
        reason = structured.get("reason")
        message = structured.get("message")
        if isinstance(reason, str) and reason:
            detail = f" ({reason})"
        if isinstance(message, str) and message:
            detail += f": {message}"
        if structured.get("success") is False:
            guidance = {
                "busy": f"another call holds the single line; wait {structured.get('retry_after_seconds', 30)}s and call again",
                "invalid_cursor": "after_event_seq is past the event history; call again with the newest event_seq you received, or 0",
                "session_not_found": "unknown or expired round; start a fresh call without continuation ids",
                "missing_call_id": "pass interaction_id and call_id together, or omit both to start a new round",
                "request_conflict": "this request was already recorded with different arguments; retry unchanged or start fresh",
                "expired": "the round already ended; start a fresh call",
            }.get(str(status))
            if guidance:
                detail += f" — {guidance}"
        if "desktop_online" in structured:
            bark = structured.get("bark")
            detail += (f" — desktop {'online' if structured.get('desktop_online') else 'OFFLINE'}, "
                       f"bark push {'configured' if isinstance(bark, Mapping) and bark.get('configured') else 'not configured'}")
        return f"{name}: {status}{detail}"

    @staticmethod
    def _rpc_error(rid: object, code: int, message: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def _dispatch(self, caller: str, body: Mapping[str, object]) -> tuple[dict[str, object], bool]:
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return self._error("invalid_request"), False
        if len(request_id) > 128 or not request_id.isascii():
            return self._error("invalid_request"), False
        interaction, call_id = body.get("interaction_id"), body.get("call_id")
        if body.get("operation") == "cancel_interaction":
            if not isinstance(interaction, str) or not isinstance(call_id, str):
                return self._error("session_not_found"), False
            return self.lifecycle.cancel(caller, request_id, interaction, call_id), False
        revision_id = body.get("revision_id")
        if revision_id is not None:
            payload = body.get("payload")
            if not isinstance(interaction, str) or not isinstance(call_id, str) or not isinstance(revision_id, str) or not isinstance(payload, Mapping):
                return self._error("invalid_request"), False
            replay = self.lifecycle.has_request(caller, request_id)
            invited_desktop_id = self._invited_desktop_id() if not replay else None
            mobile_eligible = self._mobile_eligible() if not replay else False
            return self.lifecycle.resubmit(
                caller, request_id, interaction, call_id, revision_id, payload,
                invited_desktop_id, mobile_eligible,
            ), not replay
        expected_task_version = body.get("expected_task_version")
        if expected_task_version is not None:
            task_description = body.get("task_description")
            context_note = body.get("context_note")
            if not isinstance(interaction, str) or not isinstance(call_id, str) or isinstance(expected_task_version, bool) or not isinstance(expected_task_version, int):
                return self._error("invalid_request"), False
            if task_description is not None and not isinstance(task_description, str):
                return self._error("invalid_request"), False
            if context_note is not None and not isinstance(context_note, str):
                return self._error("invalid_request"), False
            return self.lifecycle.update_task_description(
                caller, request_id, interaction, call_id, expected_task_version,
                task_description, context_note,
            ), False
        if interaction is not None or call_id is not None:
            after = body.get("after_event_seq", 0)
            if not isinstance(interaction, str) or not isinstance(call_id, str) or isinstance(after, bool) or not isinstance(after, int):
                return self._error("session_not_found"), False
            replay = self.lifecycle.has_request(caller, request_id)
            return self.lifecycle.continue_call(caller, request_id, interaction, call_id, after), not replay
        payload = body.get("payload")
        if not isinstance(payload, Mapping):
            return self._error("invalid_request"), False
        replay = self.lifecycle.has_request(caller, request_id)
        if not replay and not self._route_is_enabled():
            # Route configuration is a server-side admission check.  In
            # particular, it must not consume a request id, reserve the busy
            # slot, or disclose an active call when no human channel is
            # enabled for this new call.
            return self._error("no_enabled_channel"), False
        invited_desktop_id = self._invited_desktop_id() if not replay else None
        mobile_eligible = self._mobile_eligible() if not replay else False
        return self.lifecycle.create(caller, request_id, payload, invited_desktop_id, mobile_eligible), not replay

    def _route_is_enabled(self) -> bool:
        """Accept only the central configuration's affirmative route result."""
        try:
            result = self._route_admission()
        except Exception:
            return False
        return result.get("success") is True and result.get("status") == "ready"

    def _caller(self, headers: object) -> str | None:
        if not isinstance(headers, list): return None
        for name, value in headers:
            if bytes(name).lower() == b"authorization":
                presented = bytes(value).decode("latin-1")
                if presented.startswith("Bearer "):
                    token = presented[7:]
                    for caller, credential in self._credentials.items():
                        if hmac.compare_digest(token, credential): return caller
        return None

    @staticmethod
    def _header(headers: object, wanted: bytes) -> str | None:
        if not isinstance(headers, list):
            return None
        for name, value in headers:
            if bytes(name).lower() == wanted:
                return bytes(value).decode("latin-1")
        return None

    def _terminal_credential(self, headers: object) -> str | None:
        presented = self._header(headers, b"authorization")
        return presented[9:] if presented is not None and presented.startswith("Terminal ") else None

    def _terminal_session(self, headers: object) -> str | None:
        return self._header(headers, b"x-terminal-session")

    def _browser_session(self, headers: object) -> tuple[str, str, str] | None:
        """Authenticate the browser cookie; never treat a call link as a credential."""
        cookie = self._cookie(headers, "operator_session")
        if cookie in self._logged_out:
            return None
        signed = self._verify_operator_session_token(cookie)
        if isinstance(signed, Mapping):
            return str(signed["operator_id"]), str(signed["browser_session_id"]), str(signed["csrf_token"])
        authenticator = self._browser_cookie_authenticator
        authenticate = getattr(authenticator, "authenticate_cookie", None)
        result = authenticate(cookie) if callable(authenticate) else None
        if isinstance(result, Mapping):
            operator_id, session_id, csrf_token = (
                result.get("operator_id"), result.get("browser_session_id"), result.get("csrf_token"),
            )
            if all(isinstance(value, str) and value for value in (operator_id, session_id, csrf_token)):
                return str(operator_id), str(session_id), str(csrf_token)
            return None
        # Retain the injected header verifier only for older JSON adapter tests;
        # a configured cookie authenticator deliberately disables this fallback.
        if authenticator is not None:
            return None
        legacy = self._operator_session(headers)
        return (*legacy, "") if legacy is not None else None

    def _operator_session(self, headers: object) -> tuple[str, str] | None:
        token = self._header(headers, b"x-operator-session")
        verifier = self._operator_session_verifier
        verify = getattr(verifier, "verify", None)
        result = verify(token) if callable(verify) else verifier(token) if callable(verifier) else None
        if not isinstance(result, Mapping):
            return None
        operator_id, session_id = result.get("operator_id"), result.get("browser_session_id")
        if not isinstance(operator_id, str) or not operator_id or not isinstance(session_id, str) or not session_id:
            return None
        return operator_id, session_id

    def _browser_write_is_trusted(self, headers: object) -> bool:
        origin = self._header(headers, b"origin")
        host = self._header(headers, b"host")
        expected = self._trusted_browser_origin
        if not isinstance(expected, str) or not expected:
            return False
        try:
            expected_host = expected.split("://", 1)[1]
        except IndexError:
            return False
        # The Host header is always present and anchors the request to the
        # configured central origin.  ``Origin`` is an optional second signal:
        # in-app iPhone browsers may omit it on same-origin form posts, and the
        # synchronizer token (login entry cookie / per-form CSRF value) is the
        # durable cross-site defence.  Reject a mismatched Origin only when one
        # is actually supplied.
        if not hmac.compare_digest(host or "", expected_host):
            return False
        if origin is not None and not hmac.compare_digest(origin, expected):
            return False
        return True

    def _render_browser_page(self, view: Mapping[str, object], csrf_token: str,
                             action_response: Mapping[str, object] | None = None) -> str:
        renderer = self._mobile_html_renderer
        rendered = renderer(view, csrf_token) if callable(renderer) else None
        if isinstance(rendered, str):
            return rendered
        call_id = html.escape(str(view.get("call_id", "")), quote=True)
        csrf = html.escape(csrf_token, quote=True)
        display = str(view.get("display_state", ""))
        if display == "handled_on_desktop_processing":
            return "<html><body>已在电脑端处理中</body></html>"
        if display == "handled_on_desktop_complete":
            return "<html><body>已在电脑端处理</body></html>"
        if view.get("success") and view.get("can_accept"):
            return (f"<html><body><form method=\"post\" action=\"/calls/{call_id}/accept\">"
                    f"<input name=\"csrf_token\" value=\"{csrf}\"><input name=\"operation_id\" value=\"{html.escape(str(uuid.uuid4()), quote=True)}\">"
                    "<button>accept</button></form><form method=\"post\" action=\"/calls/"
                    f"{call_id}/decline\"><input name=\"csrf_token\" value=\"{csrf}\">"
                    f"<input name=\"operation_id\" value=\"{html.escape(str(uuid.uuid4()), quote=True)}\"><button>decline</button></form></body></html>")
        if view.get("success") and view.get("can_submit"):
            submission_id = html.escape(str(uuid.uuid4()), quote=True)
            return (f"<html><body data-handled-on=\"iphone_web\"><form method=\"post\" action=\"/calls/{call_id}/submissions\">"
                    f"<input name=\"csrf_token\" value=\"{csrf}\">"
                    f"<input name=\"submission_id\" value=\"{submission_id}\">"
                    "<textarea name=\"result\"></textarea><button>submit</button></form></body></html>")
        if action_response is not None:
            return "<html><body><pre>" + html.escape(json.dumps(action_response, sort_keys=True)) + "</pre></body></html>"
        return "<html><body>当前呼叫不可处理</body></html>"

    @staticmethod
    def _cookie(headers: object, name: str) -> str | None:
        value = Application._header(headers, b"cookie")
        if not isinstance(value, str):
            return None
        for item in value.split(";"):
            key, separator, content = item.strip().partition("=")
            if separator and key == name:
                return content
        return None

    @staticmethod
    async def _body(receive: Any) -> dict[str, object] | None:
        chunks: list[bytes] = []
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                if not chunks: return None
                break
            chunks.append(bytes(event.get("body", b"")))
            if not event.get("more_body"):
                break
        # ASGI delivers large request bodies as multiple http.request
        # chunks; reading only the first truncated every payload above one
        # chunk and reset the connection mid-upload.
        try: value = json.loads(b"".join(chunks))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError): return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _attachment_upload(body: Mapping[str, object] | None) -> tuple[str, str, bytes] | str:
        """Validate and decode an attachment upload body.

        Returns ``(filename, media_type, content)`` on success, or an error
        status string.  A local filesystem path is never an accepted source:
        only inline base64 content is honoured, so a path-only body yields the
        distinct ``local_path_not_accepted`` rejection.
        """
        if body is None:
            return "invalid_request"
        filename = body.get("filename")
        media_type = body.get("media_type")
        content = body.get("content")
        path = body.get("path")
        has_path = isinstance(path, str) and bool(path)
        if not isinstance(filename, str) or not filename:
            return "invalid_request"
        if not isinstance(media_type, str) or not media_type:
            media_type = "application/octet-stream"
        if not isinstance(content, str) or not content:
            return "local_path_not_accepted" if has_path else "invalid_request"
        try:
            decoded = base64.b64decode(content, validate=True)
        except ValueError:
            return "invalid_request"
        if not decoded:
            return "local_path_not_accepted" if has_path else "invalid_request"
        return filename, media_type, decoded

    @staticmethod
    async def _form_body(receive: Any) -> dict[str, object] | None:
        chunks: list[bytes] = []
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                if not chunks: return None
                break
            chunks.append(bytes(event.get("body", b"")))
            if not event.get("more_body"):
                break
        try:
            parsed = parse_qs(b"".join(chunks).decode("utf-8"), keep_blank_values=True)
        except (TypeError, UnicodeDecodeError):
            return None
        return {key: values[-1] for key, values in parsed.items() if values}

    @staticmethod
    def _error(status: str) -> dict[str, object]:
        return {"success": False, "status": status, "needs_continuation": False}

    @staticmethod
    def _action_status(response: Mapping[str, object]) -> int:
        if response.get("success"):
            return 200
        status = response.get("status")
        if status in {"attachment_too_large", "submission_too_large", "storage_full"}:
            return 200
        return 400 if status == "request_conflict" else 404

    @staticmethod
    def _terminal_error(status: str) -> dict[str, object]:
        return {"success": False, "status": status}

    async def _send(self, send: Any, status: int, body: Mapping[str, object],
                    extra_headers: list[tuple[bytes, bytes]] | None = None) -> None:
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), *(extra_headers or [])]})
        await send({"type": "http.response.body", "body": json.dumps(body).encode(), "more_body": False})

    @staticmethod
    async def _send_html(send: Any, status: int, body: str,
                         headers: list[tuple[bytes, bytes]] | None = None) -> None:
        response_headers = [(b"content-type", b"text/html; charset=utf-8"), *(headers or [])]
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": body.encode("utf-8"), "more_body": False})

    @staticmethod
    async def _send_redirect(send: Any, location: str,
                             headers: list[tuple[bytes, bytes]] | None = None) -> None:
        await send({"type": "http.response.start", "status": 303,
                    "headers": [(b"location", location.encode("ascii")), *(headers or [])]})
        await send({"type": "http.response.body", "body": b"", "more_body": False})


def create_app(*, database_path: str | Path, credentials: Mapping[str, str], clock: Clock, notify: Notifier,
               wait: Waiter, ring_timeout_seconds: float = 60, agent_reconnect_seconds: float = 30,
               terminal_disconnect_seconds: float = 180,
               deadline_tick_seconds: float = 1, scheduler_sleep: Waiter = asyncio.sleep,
               route_admission: RouteAdmission | None = None, notification_router: Any | None = None,
               notification_dispatch_tick_seconds: float = 1,
               notification_scheduler_sleep: Waiter = asyncio.sleep,
               desktop_registration_service: Any | None = None,
               operator_session_verifier: Any | None = None,
               browser_cookie_authenticator: Any | None = None,
               mobile_html_renderer: Any | None = None,
               trusted_browser_origin: str | None = None,
               operator_id: str = "only-operator",
               operator_profile: Mapping[str, object] | None = None,
               operator_directory: Mapping[str, Mapping[str, object]] | None = None,
               operator_session_secret: bytes | None = None,
               max_attachment_bytes: int = 20 * 1024 * 1024,
               max_submission_bytes: int = 50 * 1024 * 1024,
               max_storage_bytes: int = 1024 * 1024 * 1024) -> Application:
    if deadline_tick_seconds <= 0 or notification_dispatch_tick_seconds <= 0 or terminal_disconnect_seconds <= 0:
        raise ValueError("scheduler tick seconds must be positive")
    if operator_directory and operator_id == "only-operator":
        primaries = [c for c, e in operator_directory.items() if isinstance(e, Mapping) and e.get("primary")]
        if primaries:
            operator_id = str(primaries[0])
    return Application(
        WaitingLifecycle(
            database_path, clock, notify, ring_timeout_seconds, agent_reconnect_seconds,
            terminal_disconnect_seconds,
            operator_id=operator_id, operator_profile=operator_profile,
            operator_directory=operator_directory,
            max_attachment_bytes=max_attachment_bytes,
            max_submission_bytes=max_submission_bytes,
            max_storage_bytes=max_storage_bytes,
        ),
        credentials,
        wait,
        deadline_tick_seconds,
        scheduler_sleep,
        operator_session_secret,
        route_admission if route_admission is not None else (lambda: {"success": True, "status": "ready"}),
        notification_router,
        notification_dispatch_tick_seconds,
        notification_scheduler_sleep,
        desktop_registration_service,
        operator_session_verifier,
        browser_cookie_authenticator,
        mobile_html_renderer,
        trusted_browser_origin,
    )
