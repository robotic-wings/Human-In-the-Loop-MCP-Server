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


Clock = Callable[[], float]
Waiter = Callable[[float], Awaitable[None]]
Notifier = Callable[[dict[str, object]], None]
RouteAdmission = Callable[[], Mapping[str, object]]


class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


_MCP_TOOLS = (
    {"name": "get_user_input", "kind": "interaction",
     "description": "Request a single text or numeric answer from the operator.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "required": {"type": "boolean"},
         "interaction_id": {"type": "string"}, "call_id": {"type": "string"},
         "after_event_seq": {"type": "integer"}}}},
    {"name": "get_number_input", "kind": "interaction",
     "description": "Request a numeric answer from the operator.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "required": {"type": "boolean"},
         "interaction_id": {"type": "string"}, "call_id": {"type": "string"},
         "after_event_seq": {"type": "integer"}}}},
    {"name": "get_user_choice", "kind": "interaction",
     "description": "Ask the operator to choose one or more options.",
     "inputSchema": {"type": "object", "properties": {
         "choices": {"type": "array"}, "prompt": {"type": "string"},
         "multiple": {"type": "boolean"}, "interaction_id": {"type": "string"},
         "call_id": {"type": "string"}, "after_event_seq": {"type": "integer"}}}},
    {"name": "get_multiline_input", "kind": "interaction",
     "description": "Request a free-form multi-line answer from the operator.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "interaction_id": {"type": "string"},
         "call_id": {"type": "string"}, "after_event_seq": {"type": "integer"}}}},
    {"name": "show_confirmation_dialog", "kind": "interaction",
     "description": "Ask the operator to confirm or deny a proposal.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "interaction_id": {"type": "string"},
         "call_id": {"type": "string"}, "after_event_seq": {"type": "integer"}}}},
    {"name": "show_info_message", "kind": "interaction",
     "description": "Present information and require the operator to acknowledge it.",
     "inputSchema": {"type": "object", "properties": {
         "message": {"type": "string"}, "interaction_id": {"type": "string"},
         "call_id": {"type": "string"}, "after_event_seq": {"type": "integer"}}}},
    {"name": "get_feedback_from_user", "kind": "interaction",
     "description": "Request unstructured feedback from the operator.",
     "inputSchema": {"type": "object", "properties": {
         "prompt": {"type": "string"}, "interaction_id": {"type": "string"},
         "call_id": {"type": "string"}, "after_event_seq": {"type": "integer"}}}},
    {"name": "assign_task_to_human", "kind": "interaction",
     "description": "Assign a task for the operator to perform and report on.",
     "inputSchema": {"type": "object", "properties": {
         "task_description": {"type": "string"}, "interaction_id": {"type": "string"},
         "call_id": {"type": "string"}, "after_event_seq": {"type": "integer"}}}},
    {"name": "get_operator_profile", "kind": "readonly",
     "description": "Read the operator profile without requesting human action.",
     "inputSchema": {"type": "object"}},
    {"name": "health_check", "kind": "readonly",
     "description": "Report central service health without requesting human action.",
     "inputSchema": {"type": "object"}},
    {"name": "get_human_loop_prompt", "kind": "readonly",
     "description": "Return the human-in-the-loop protocol guidance.",
     "inputSchema": {"type": "object"}},
    {"name": "cancel_interaction", "kind": "control",
     "description": "Cancel an existing call round.",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "call_id": {"type": "string"}}}},
)


class WaitingLifecycle:
    def __init__(self, path: str | Path, clock: Clock, notify: Notifier, ring_timeout: float,
                 agent_reconnect_seconds: float, terminal_disconnect_seconds: float = 180,
                 operator_id: str = "only-operator") -> None:
        self._path, self._clock, self._notify = str(path), clock, notify
        self._ring_timeout, self._agent_reconnect_seconds = ring_timeout, agent_reconnect_seconds
        self._terminal_disconnect_seconds = terminal_disconnect_seconds
        self._operator_id = operator_id
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
                terminal_last_seen_at REAL, terminal_disconnect_deadline REAL,
                version INTEGER NOT NULL DEFAULT 1
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
            ("terminal_last_seen_at", "REAL"), ("terminal_disconnect_deadline", "REAL"),
            ("version", "INTEGER NOT NULL DEFAULT 1"),
        ):
            if column not in columns:
                conn.execute(f"ALTER TABLE lifecycle_calls ADD COLUMN {column} {definition}")
        request_columns = {row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_requests)")}
        if "operation_kind" not in request_columns:
            conn.execute("ALTER TABLE lifecycle_requests ADD COLUMN operation_kind TEXT NOT NULL DEFAULT 'create'")

    def create(self, caller_id: str, request_id: str, payload: Mapping[str, object],
               invited_desktop_id: str | None = None, mobile_eligible: bool = False) -> dict[str, object]:
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
                dnd = self._dnd_state(conn)
                if dnd["enabled"]:
                    outcome = {
                        "success": True, "status": "declined", "reason": "do_not_disturb",
                        "message": dnd["message"], "human_action": False, "needs_continuation": False,
                    }
                    conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'create')", (caller_id, request_id, encoded, self._dump(outcome)))
                    conn.commit()
                    return outcome
                active = conn.execute("SELECT call_id FROM lifecycle_calls WHERE call_state IN ('ringing', 'accepted', 'in_progress') LIMIT 1").fetchone()
                if active is not None:
                    outcome = {
                        "success": True, "status": "busy", "call_state": None, "call_id": None,
                        "reason": None, "interaction_id": None, "human_action": False,
                        "handled_on": None, "terminal_platform": None, "event_seq": None,
                        "needs_continuation": False,
                    }
                else:
                    call_id, interaction_id = str(uuid.uuid4()), str(uuid.uuid4())
                    deadline = self._clock() + self._ring_timeout
                    conn.execute(
                        "INSERT INTO lifecycle_calls "
                        "(call_id, interaction_id, caller_id, call_state, ring_deadline, reason, payload_json, agent_reconnect_deadline, invited_desktop_id, mobile_eligible) "
                        "VALUES (?, ?, ?, 'ringing', ?, NULL, ?, NULL, ?, ?)",
                        (call_id, interaction_id, caller_id, deadline, encoded, invited_desktop_id, int(mobile_eligible)),
                    )
                    outcome = self._heartbeat(interaction_id, call_id, "ringing")
                    short_title = next(
                        (str(payload.get(key)) for key in ("title", "task_description", "prompt", "message")
                         if isinstance(payload.get(key), str) and payload.get(key).strip()),
                        "Human-in-the-Loop request",
                    )
                    notification = {
                        "call_id": call_id, "ring_deadline": deadline,
                        "title": caller_id, "body": " ".join(short_title.split())[:200],
                    }
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'create')", (caller_id, request_id, encoded, self._dump(outcome)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if notification is not None:
            self._notify(notification)
        return outcome

    def set_dnd(self, enabled: bool, message: str) -> dict[str, object]:
        """Persist the operator-level do-not-disturb state and auto-reply."""
        message = message if isinstance(message, str) else ""
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO lifecycle_dnd (operator_id, enabled, message) VALUES (?, ?, ?) "
                    "ON CONFLICT(operator_id) DO UPDATE SET enabled=excluded.enabled, message=excluded.message",
                    (self._operator_id, int(enabled), message),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"success": True, "enabled": bool(enabled), "message": message}

    def get_dnd(self) -> dict[str, object]:
        with self._connection() as conn:
            state = self._dnd_state(conn)
        return {"success": True, **state}

    def _dnd_state(self, conn: sqlite3.Connection) -> dict[str, object]:
        row = conn.execute(
            "SELECT enabled, message FROM lifecycle_dnd WHERE operator_id=?", (self._operator_id,)
        ).fetchone()
        if row is None:
            return {"enabled": False, "message": ""}
        return {"enabled": bool(row["enabled"]), "message": str(row["message"])}

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
                    conn.execute(
                        "UPDATE lifecycle_calls SET call_state='cancelled', reason='agent_cancelled' WHERE call_id=?",
                        (call_id,),
                    )
                    seq = int(conn.execute(
                        "SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?",
                        (call_id,),
                    ).fetchone()["n"])
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
                    conn.execute(
                        "UPDATE lifecycle_calls SET call_state='unreachable', reason='no_available_channel' "
                        "WHERE call_id=?",
                        (call_id,),
                    )
                    seq = int(conn.execute(
                        "SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?",
                        (call_id,),
                    ).fetchone()["n"])
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
                    state, status, reason = ("accepted", "opened", None) if action == "accept" else ("declined", "declined", "human_declined")
                    if action == "accept":
                        conn.execute(
                            "UPDATE lifecycle_calls SET call_state=?, reason=?, owner_device_id=?, owner_session_id=?, handled_on='desktop', terminal_platform=?, terminal_last_seen_at=?, terminal_disconnect_deadline=?, agent_reconnect_deadline=?, version=version+1 WHERE call_id=?",
                            (state, reason, device_id, session_id, terminal_platform, self._clock(),
                             self._clock() + self._terminal_disconnect_seconds, None, call_id),
                        )
                    else:
                        # A decline ends the invitation without assigning a
                        # processing owner.  Keep its audit endpoint separate
                        # from handled_on / terminal_platform.  The internal
                        # device/session association remains so the declining
                        # desktop receives the terminal state on its next
                        # synchronization and can close its old alert.
                        conn.execute(
                            "UPDATE lifecycle_calls SET call_state=?, reason=?, owner_device_id=?, owner_session_id=?, handled_on=NULL, terminal_platform=NULL, terminal_last_seen_at=NULL, terminal_disconnect_deadline=NULL, agent_reconnect_deadline=NULL, version=version+1 WHERE call_id=?",
                            (state, reason, device_id, session_id, call_id),
                        )
                    seq = int(conn.execute("SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call_id,)).fetchone()["n"])
                    outcome: dict[str, object] = {
                        "success": True, "status": status, "call_state": state, "human_action": True,
                        "needs_continuation": action == "accept", "interaction_id": call["interaction_id"],
                        "call_id": call_id, "event_seq": seq,
                    }
                    if action == "accept":
                        outcome.update(owner_device_id=device_id, owner_session_id=session_id,
                                       handled_on="desktop", terminal_platform=terminal_platform)
                    else:
                        outcome.update(reason=reason, acted_on="desktop", handled_on=None,
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
        if action not in {"accept", "decline"} or operator_id != self._operator_id:
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
                state, status, reason = (
                    ("accepted", "opened", None) if action == "accept"
                    else ("declined", "declined", "human_declined")
                )
                now = self._clock()
                if action == "accept":
                    conn.execute(
                        "UPDATE lifecycle_calls SET call_state=?, reason=?, owner_device_id=?, owner_session_id=?, handled_on='iphone_web', terminal_platform='ios', terminal_last_seen_at=?, terminal_disconnect_deadline=?, agent_reconnect_deadline=?, version=version+1 WHERE call_id=?",
                        (state, reason, "iphone_web", session_id, now,
                         now + self._terminal_disconnect_seconds, None, call_id),
                    )
                else:
                    conn.execute(
                        "UPDATE lifecycle_calls SET call_state=?, reason=?, owner_device_id=?, owner_session_id=?, handled_on=NULL, terminal_platform=NULL, terminal_last_seen_at=NULL, terminal_disconnect_deadline=NULL, agent_reconnect_deadline=NULL, version=version+1 WHERE call_id=?",
                        (state, reason, "iphone_web", session_id, call_id),
                    )
                seq = int(conn.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call_id,)
                ).fetchone()["n"])
                outcome: dict[str, object] = {
                    "success": True, "status": status, "call_state": state, "human_action": True,
                    "needs_continuation": action == "accept", "interaction_id": call["interaction_id"],
                    "call_id": call_id, "event_seq": seq,
                }
                if action == "accept":
                    outcome.update(owner_device_id="iphone_web", owner_session_id=session_id,
                                   handled_on="iphone_web", terminal_platform="ios")
                else:
                    outcome.update(reason=reason, acted_on="iphone_web", handled_on=None,
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
               submission_id: str, result: Mapping[str, object]) -> dict[str, object]:
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
                status, state, needs_continuation, extras = self._interpret_submission(result)
                conn.execute(
                    "UPDATE lifecycle_calls SET call_state=?, reason=NULL, version=version+1 WHERE call_id=?",
                    (state, call_id),
                )
                seq = int(conn.execute(
                    "SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call_id,)
                ).fetchone()["n"])
                outcome: dict[str, object] = {
                    "success": True, "status": status, "call_state": state, "human_action": True,
                    "needs_continuation": needs_continuation, "interaction_id": call["interaction_id"], "call_id": call_id,
                    "event_seq": seq, "handled_on": call["handled_on"],
                    "terminal_platform": call["terminal_platform"], "result": dict(result),
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
                    conn.execute(
                        "UPDATE lifecycle_calls SET call_state='cancelled', reason='human_cancelled', version=version+1 WHERE call_id=?",
                        (call_id,),
                    )
                    seq = int(conn.execute(
                        "SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?",
                        (call_id,),
                    ).fetchone()["n"])
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
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT call_id, call_state, version FROM lifecycle_calls WHERE owner_device_id=? ORDER BY call_id",
                (device_id,),
            ).fetchall()
        return [{"call_id": row["call_id"], "call_state": row["call_state"], "version": row["version"]} for row in rows]

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
                conn.execute("UPDATE lifecycle_calls SET call_state=?, reason=? WHERE call_id=?", (state, reason, call_id))
                seq = int(conn.execute("SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call_id,)).fetchone()["n"])
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
        rows = conn.execute("SELECT * FROM lifecycle_calls WHERE call_state='ringing' AND ring_deadline<=?", (now,)).fetchall()
        for call in rows:
            conn.execute("UPDATE lifecycle_calls SET call_state='not_accepted', reason='ring_timeout' WHERE call_id=?", (call["call_id"],))
            seq = int(conn.execute("SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call["call_id"],)).fetchone()["n"])
            body = self._terminal(call["interaction_id"], call["call_id"], "not_accepted", "ring_timeout", seq)
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))
        rows = conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress') "
            "AND terminal_disconnect_deadline IS NOT NULL AND terminal_disconnect_deadline<=?",
            (now,),
        ).fetchall()
        for call in rows:
            conn.execute(
                "UPDATE lifecycle_calls SET call_state='disconnected', reason='terminal_disconnected', version=version+1 WHERE call_id=?",
                (call["call_id"],),
            )
            seq = int(conn.execute("SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call["call_id"],)).fetchone()["n"])
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", "terminal_disconnected", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))
        rows = conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress') "
            "AND agent_reconnect_deadline IS NOT NULL AND agent_reconnect_deadline<=?",
            (now,),
        ).fetchall()
        for call in rows:
            conn.execute(
                "UPDATE lifecycle_calls SET call_state='disconnected', reason='agent_disconnected' WHERE call_id=?",
                (call["call_id"],),
            )
            seq = int(conn.execute("SELECT COALESCE(MAX(event_seq), 0) + 1 AS n FROM lifecycle_events WHERE call_id=?", (call["call_id"],)).fetchone()["n"])
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", "agent_disconnected", seq)
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
            return "completed", "completed", False, {"report": result.get("report"), "resubmittable": True}
        if status == "failed":
            return "failed", "failed", False, {"report": result.get("report"), "resubmittable": True}
        if status == "in_progress":
            return "in_progress", "in_progress", True, {"progress": result.get("progress")}
        return "answered", "answered", False, {}

    @staticmethod
    def _heartbeat(interaction_id: str, call_id: str, state: str) -> dict[str, object]:
        return {"success": True, "status": "heartbeat", "call_state": state, "interaction_id": interaction_id, "call_id": call_id, "needs_continuation": True}

    @staticmethod
    def _terminal(interaction_id: str, call_id: str, state: str, reason: str | None, seq: int) -> dict[str, object]:
        return {"success": True, "status": state, "call_state": state, "reason": reason, "event_seq": seq, "needs_continuation": False, "interaction_id": interaction_id, "call_id": call_id}

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
                 route_admission: RouteAdmission, notification_router: Any | None,
                 notification_dispatch_tick_seconds: float, notification_scheduler_sleep: Waiter,
                 desktop_registration_service: Any | None = None,
                 operator_session_verifier: Any | None = None,
                 browser_cookie_authenticator: Any | None = None,
                 mobile_html_renderer: Any | None = None,
                 trusted_browser_origin: str | None = None) -> None:
        self.lifecycle, self._credentials, self._wait = lifecycle, dict(credentials), wait
        self._deadline_tick_seconds, self._scheduler_sleep = deadline_tick_seconds, scheduler_sleep
        self._route_admission = route_admission
        self._notification_router = notification_router
        self._desktop_registration_service = desktop_registration_service
        self._operator_session_verifier = operator_session_verifier
        self._browser_cookie_authenticator = browser_cookie_authenticator
        self._mobile_html_renderer = mobile_html_renderer
        self._trusted_browser_origin = trusted_browser_origin
        self._browser_entries: dict[str, str] = {}
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
        if method != "POST" or path != "/mcp":
            await self._send(send, 404, self._error("not_found")); return
        caller = self._caller(scope.get("headers", []))
        if caller is None:
            await self._send(send, 401, self._error("unauthorized")); return
        body = await self._body(receive)
        if body is None:
            await self._send(send, 400, self._error("invalid_request")); return
        if "jsonrpc" in body or "method" in body or "id" in body:
            await self._send(send, 200, self._rpc(caller, body)); return
        timeout = body.get("client_timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 2:
            await self._send(send, 400, self._error("invalid_client_timeout")); return
        response, should_wait = self._dispatch(caller, body)
        await send({"type": "http.response.start", "status": 200 if response.get("success") else 400,
                    "headers": [(b"content-type", b"application/json")]})
        waited = should_wait and response.get("needs_continuation") and response.get("status") == "heartbeat"
        if waited:
            await self._wait(max(1, float(timeout) - min(30, max(1, float(timeout) // 4))))
        await send({"type": "http.response.body", "body": json.dumps(response, separators=(",", ":")).encode(), "more_body": False})
        if should_wait:
            self.lifecycle.response_delivered(caller, response)

    async def _terminal_api(self, method: object, path: object, headers: object, receive: Any, send: Any) -> None:
        service = self._desktop_registration_service
        if service is None:
            await self._send(send, 404, self._error("not_found")); return
        if method == "POST" and path == "/api/terminals/register":
            body = await self._body(receive)
            credential = self._terminal_credential(headers)
            if body is None or credential is None:
                await self._send(send, 401, self._terminal_error("terminal_not_authenticated")); return
            endpoint, gui_ready = body.get("endpoint"), body.get("gui_ready")
            if not isinstance(endpoint, str) or not isinstance(gui_ready, bool):
                await self._send(send, 400, self._error("invalid_request")); return
            response = service.register(credential=credential, endpoint=endpoint, gui_ready=gui_ready)
            await self._send(send, 201 if response.get("success") else 401, response); return
        if path not in {"/api/terminals/heartbeat", "/api/terminals/state", "/api/terminals/dnd"}:
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
                device_id, _platform = context
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
        elif method == "POST" and path == "/api/terminals/dnd":
            body = await self._body(receive)
            dnd = body.get("dnd") if body is not None else None
            message = body.get("message") if body is not None else ""
            if not isinstance(dnd, bool):
                await self._send(send, 400, self._error("invalid_request")); return
            if not isinstance(message, str):
                message = ""
            response = self.lifecycle.set_dnd(dnd, message)
        elif method == "GET" and path == "/api/terminals/dnd":
            response = self.lifecycle.get_dnd()
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
        if method != "POST":
            await self._send(send, 404, self._error("not_found")); return
        if len(parts) != 5 or parts[:3] != ["", "api", "calls"] or parts[4] not in {"accept", "decline", "heartbeat", "submissions", "cancel", "attachments"}:
            await self._send(send, 404, self._error("not_found")); return
        session = self._terminal_session(headers)
        context = self._terminal_context(session) if session is not None else None
        if context is None:
            await self._send(send, 401, self._terminal_error("session_not_found")); return
        device_id, platform = context
        if parts[4] == "heartbeat":
            await self._body(receive)
            response = self.lifecycle.terminal_heartbeat(call_id=parts[3], session_id=session)
        elif parts[4] == "submissions":
            body = await self._body(receive)
            submission_id = body.get("submission_id") if body is not None else None
            result = body.get("result") if body is not None else None
            if not isinstance(submission_id, str) or not isinstance(result, Mapping):
                await self._send(send, 400, self._error("invalid_request")); return
            response = self.lifecycle.submit(
                call_id=parts[3], device_id=device_id, session_id=session,
                submission_id=submission_id, result=result,
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
            ) if operator_id == self.lifecycle._operator_id else self._terminal_error("session_not_found")
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
            ) if operator_id == self.lifecycle._operator_id else self._terminal_error("session_not_found")
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
        credential = body.get("credential") if body is not None else None
        authenticate = getattr(self._browser_cookie_authenticator, "authenticate_login", None)
        session_cookie = authenticate(credential) if callable(authenticate) else None
        if not isinstance(destination, str) or not isinstance(session_cookie, str) or not session_cookie:
            await self._send(send, 401, self._terminal_error("session_not_found")); return
        del self._browser_entries[entry]
        await self._send_redirect(
            send, destination,
            headers=[(b"set-cookie", self._operator_session_cookie(session_cookie).encode("ascii"))],
        )

    def _browser_login_is_configured(self) -> bool:
        return callable(getattr(self._browser_cookie_authenticator, "authenticate_login", None))

    @staticmethod
    def _browser_entry_cookie(value: str) -> str:
        return f"browser_entry={value}; Path=/; Secure; HttpOnly; SameSite=Lax"

    @staticmethod
    def _operator_session_cookie(value: str) -> str:
        return f"operator_session={value}; Path=/; Secure; HttpOnly; SameSite=Lax"

    @staticmethod
    def _render_login_entry() -> str:
        return ("<html><body><form method=\"post\" action=\"/operator/login\">"
                "<label for=\"operator-credential\">操作者登录凭证"
                "<input id=\"operator-credential\" type=\"password\" name=\"credential\" required>"
                "</label><button type=\"submit\">登录</button></form></body></html>")

    def _browser_call_view(self, call_id: str, operator_id: str,
                           browser_session_id: str | None = None) -> dict[str, object]:
        if operator_id != self.lifecycle._operator_id:
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

    def _terminal_context(self, session_id: str | None) -> tuple[str, str] | None:
        service = self._desktop_registration_service
        if service is None or not isinstance(session_id, str):
            return None
        sessions = getattr(service, "_sessions", None)
        credentials = getattr(service, "_credentials", None)
        if not isinstance(sessions, Mapping) or not isinstance(credentials, Mapping):
            return None
        device_id = sessions.get(session_id)
        if not isinstance(device_id, str):
            return None
        for metadata in credentials.values():
            if isinstance(metadata, Mapping) and metadata.get("device_id") == device_id:
                platform = metadata.get("terminal_platform")
                return device_id, str(platform) if isinstance(platform, str) else "unknown"
        return None

    def _invited_desktop_id(self) -> str | None:
        router = self._notification_router
        if getattr(router, "_route_mode", None) not in {"desktop", "both"}:
            return None
        registry = getattr(router, "_registry", None) if router is not None else None
        ready_desktop = getattr(registry, "ready_desktop", None)
        if not callable(ready_desktop):
            return None
        terminal = ready_desktop()
        device_id = terminal.get("device_id") if isinstance(terminal, Mapping) else None
        return device_id if isinstance(device_id, str) else None

    def _mobile_eligible(self) -> bool:
        return getattr(self._notification_router, "_route_mode", None) in {"iphone", "both"}

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

    def _rpc(self, caller: str, body: Mapping[str, object]) -> dict[str, object]:
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
                result = self._rpc_tools_call(caller, body.get("params"), rid)
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

    def _rpc_tools_call(self, caller: str, params: object, rid: object) -> dict[str, object]:
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
            structured = self._rpc_interaction(caller, name, arguments, rid)
        elif kind == "control":
            structured = self._rpc_cancel(caller, arguments, rid)
        else:
            structured = self._rpc_readonly(name)
        return {
            "content": [{"type": "text", "text": self._rpc_summary(name, structured)}],
            "isError": False,
            "structuredContent": structured,
        }

    def _rpc_interaction(self, caller: str, name: str, arguments: Mapping[str, object], rid: object) -> dict[str, object]:
        request_id = f"rpc-{rid}"
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        if isinstance(interaction_id, str) and interaction_id and isinstance(call_id, str) and call_id:
            after = arguments.get("after_event_seq", 0)
            if isinstance(after, bool) or not isinstance(after, int):
                raise _RpcError(-32602, "Invalid params")
            result = self.lifecycle.continue_call(caller, request_id, interaction_id, call_id, after)
        else:
            payload = {"tool": name, **dict(arguments)}
            result = self.lifecycle.create(
                caller, request_id, payload, self._invited_desktop_id(), self._mobile_eligible(),
            )
        if result is None:
            result = {"success": False, "status": "session_not_found", "needs_continuation": False}
        return dict(result)

    def _rpc_cancel(self, caller: str, arguments: Mapping[str, object], rid: object) -> dict[str, object]:
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        if not isinstance(interaction_id, str) or not interaction_id or not isinstance(call_id, str) or not call_id:
            raise _RpcError(-32602, "Invalid params")
        return dict(self.lifecycle.cancel(caller, f"rpc-{rid}", interaction_id, call_id))

    def _rpc_readonly(self, name: str) -> dict[str, object]:
        if name == "get_operator_profile":
            return {"success": True, "status": "ok", "operator_id": self.lifecycle._operator_id}
        if name == "health_check":
            return {"success": True, "status": "healthy", "storage": "sqlite", "scheduler": "ready"}
        if name == "get_human_loop_prompt":
            return {"success": True, "status": "ok", "prompt": "Use tools/call to request human action; continue with interaction_id and call_id."}
        raise _RpcError(-32602, "Invalid params")

    @staticmethod
    def _rpc_summary(name: str, structured: Mapping[str, object]) -> str:
        status = structured.get("status", structured.get("call_state", "ok"))
        return f"{name}: {status}"

    @staticmethod
    def _rpc_error(rid: object, code: int, message: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}

    def _dispatch(self, caller: str, body: Mapping[str, object]) -> tuple[dict[str, object], bool]:
        request_id = body.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return self._error("invalid_request"), False
        interaction, call_id = body.get("interaction_id"), body.get("call_id")
        if body.get("operation") == "cancel_interaction":
            if not isinstance(interaction, str) or not isinstance(call_id, str):
                return self._error("session_not_found"), False
            return self.lifecycle.cancel(caller, request_id, interaction, call_id), False
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
        authenticator = self._browser_cookie_authenticator
        cookie = self._cookie(headers, "operator_session")
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
                    f"<input name=\"csrf_token\" value=\"{csrf}\"><input name=\"operation_id\" value=\"accept\">"
                    "<button>accept</button></form><form method=\"post\" action=\"/calls/"
                    f"{call_id}/decline\"><input name=\"csrf_token\" value=\"{csrf}\">"
                    "<input name=\"operation_id\" value=\"decline\"><button>decline</button></form></body></html>")
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
        event = await receive()
        if event.get("type") != "http.request": return None
        try: value = json.loads(event.get("body", b""))
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
        event = await receive()
        if event.get("type") != "http.request":
            return None
        try:
            parsed = parse_qs(bytes(event.get("body", b"")).decode("utf-8"), keep_blank_values=True)
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
        return 400 if response.get("status") == "request_conflict" else 404

    @staticmethod
    def _terminal_error(status: str) -> dict[str, object]:
        return {"success": False, "status": status}

    async def _send(self, send: Any, status: int, body: Mapping[str, object]) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
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
               operator_id: str = "only-operator") -> Application:
    if deadline_tick_seconds <= 0 or notification_dispatch_tick_seconds <= 0 or terminal_disconnect_seconds <= 0:
        raise ValueError("scheduler tick seconds must be positive")
    return Application(
        WaitingLifecycle(database_path, clock, notify, ring_timeout_seconds, agent_reconnect_seconds,
                         terminal_disconnect_seconds, operator_id),
        credentials,
        wait,
        deadline_tick_seconds,
        scheduler_sleep,
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
