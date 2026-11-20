"""HITL durable core: the lifecycle authority.

This module is the single authority over a "round" — one human-in-the-loop
call — and it is written around one explicit model (see SPEC-refactor.md):

* **Ownership** is established at accept and is immutable. A round's channel
  is ``handled_on`` ("desktop" or "iphone_web"); before accept it is ``None``.
* **Liveness** is expressed by two independent leases:
  - the *owner-channel lease* (``terminal_disconnect_deadline``), which exists
    ONLY for the desktop channel — a desktop terminal that stops heartbeating
    is gone. A mobile browser is stateless and has no such lease.
  - the *agent lease* (``agent_reconnect_deadline``), armed whenever a
    resumable heartbeat is delivered to the agent. Its expiry settles only a
    mobile round (``agent_disconnected``); on the desktop the owner-channel
    lease is the settlement authority, so a live terminal overrides the agent
    lease and the human's work is never discarded on the agent's account.
* **Settlement** is one deterministic rule table (``_settle_due``), not a set
  of scattered per-field branches.

Transport layers (MCP JSON-RPC, Chidori-native tools) are thin views over
this module. The public method surface is the wire protocol; it is stable.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from central_call_machine import ACTIVE_STATES, CallEventBus, CallMachine, InvalidTransition

Clock = Callable[[], float]
Waiter = Callable[[float], Awaitable[None]]
Notifier = Callable[[dict[str, object]], None]
RouteAdmission = Callable[[], Mapping[str, object]]

_DESKTOP = "desktop"
_MOBILE = "iphone_web"
_ACTIVE = frozenset({"ringing", "accepted", "in_progress"})


class WaitingLifecycle:
    def __init__(self, path: str | Path, clock: Clock, notify: Notifier, ring_timeout: float,
                 agent_reconnect_seconds: float, terminal_disconnect_seconds: float = 180,
                 operator_id: str = "only-operator",
                 operator_profile: Mapping[str, object] | None = None,
                 operator_directory: Mapping[str, Mapping[str, object]] | None = None,
                 max_attachment_bytes: int = 20 * 1024 * 1024,
                 max_submission_bytes: int = 50 * 1024 * 1024,
                 max_storage_bytes: int = 1024 * 1024 * 1024) -> None:
        self._path = str(path)
        self._clock = clock
        self._notify = notify
        self._ring_timeout = ring_timeout
        self._agent_reconnect_seconds = agent_reconnect_seconds
        self._terminal_disconnect_seconds = terminal_disconnect_seconds
        self._operator_id = operator_id
        self._operator_profile = dict(operator_profile or {})
        if operator_directory:
            self._operators = {str(c): dict(e) for c, e in operator_directory.items()}
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
            conn.executescript(_SCHEMA)
            self._migrate_schema(conn)
            self._settle_due(conn)

    # ── storage ────────────────────────────────────────────────────
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
        for column, definition in _CALL_COLUMNS:
            if column not in columns:
                conn.execute(f"ALTER TABLE lifecycle_calls ADD COLUMN {column} {definition}")
        request_columns = {row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_requests)")}
        if "operation_kind" not in request_columns:
            conn.execute("ALTER TABLE lifecycle_requests ADD COLUMN operation_kind TEXT NOT NULL DEFAULT 'create'")

    # ── ownership / lease model ────────────────────────────────────
    @staticmethod
    def _owner_channel(call: sqlite3.Row) -> str | None:
        return str(call["handled_on"]) if call["handled_on"] else None

    def _accept_columns(self, channel: str, owner_id: str, session_id: str, platform: str,
                        now: float) -> dict[str, object]:
        """The ownership→lease mapping, the single place it is decided.

        Only the desktop channel carries an owner-channel lease. A mobile
        browser is stateless — the human may type offline for minutes and
        still submit — so it has no ``terminal_disconnect_deadline``.
        """
        if channel == _DESKTOP:
            return {
                "owner_device_id": owner_id, "owner_session_id": session_id,
                "handled_on": _DESKTOP, "terminal_platform": platform,
                "terminal_last_seen_at": now,
                "terminal_disconnect_deadline": now + self._terminal_disconnect_seconds,
                "agent_reconnect_deadline": None,
            }
        return {
            "owner_device_id": _MOBILE, "owner_session_id": session_id,
            "handled_on": _MOBILE, "terminal_platform": platform,
            "terminal_last_seen_at": None,
            "terminal_disconnect_deadline": None,
            "agent_reconnect_deadline": None,
        }

    def _refresh_owner_lease(self, conn: sqlite3.Connection, call_id: str) -> None:
        conn.execute(
            "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
            (self._clock(), self._clock() + self._terminal_disconnect_seconds, call_id),
        )

    def _refresh_owner_lease_for_device(self, conn: sqlite3.Connection, device_id: str) -> None:
        now = self._clock()
        conn.execute(
            "UPDATE lifecycle_calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? "
            "WHERE owner_device_id=? AND call_state IN ('accepted','in_progress')",
            (now, now + self._terminal_disconnect_seconds, device_id),
        )

    def _arm_agent_lease(self, conn: sqlite3.Connection, call_id: str, state: str) -> None:
        # D11 floor: in-progress human work survives at least 300s of agent
        # silence; a larger configured lease raises it for slow-polling agents.
        grace = (max(300.0, self._agent_reconnect_seconds)
                 if state == "in_progress" else self._agent_reconnect_seconds)
        conn.execute(
            "UPDATE lifecycle_calls SET agent_reconnect_deadline=? WHERE call_id=?",
            (self._clock() + grace, call_id),
        )

    # ── operator directory ─────────────────────────────────────────
    def resolve_operator(self, callsign: object) -> str | None:
        if not isinstance(callsign, str) or not callsign.strip():
            return self._primary_operator
        return callsign.strip() if callsign.strip() in self._operators else None

    def directory_view(self) -> list[dict[str, object]]:
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

    def operator_password(self, callsign: str) -> str | None:
        entry = self._operators.get(callsign) or {}
        password = entry.get("password")
        return str(password) if isinstance(password, str) and password else None

    def operator_devices(self, callsign: str) -> list[str]:
        entry = self._operators.get(callsign) or {}
        devices = entry.get("devices")
        return [str(d) for d in devices] if isinstance(devices, list) else []

    def operator_mobile(self, callsign: str) -> bool:
        entry = self._operators.get(callsign) or {}
        return bool(entry.get("mobile"))

    def call_operator(self, call_id: str) -> str | None:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT operator_callsign FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return str(row["operator_callsign"]) if row is not None and row["operator_callsign"] else None

    # ── DND / quiet ────────────────────────────────────────────────
    def set_dnd(self, enabled: bool, message: str = "", *, operator_id: str | None = None) -> dict[str, object]:
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

    # ── create / resubmit ──────────────────────────────────────────
    def create(self, caller_id: str, request_id: str, payload: Mapping[str, object],
               invited_desktop_id: str | None = None, mobile_eligible: bool = False,
               operator_callsign: str | None = None,
               delivery: Mapping[str, object] | None = None) -> dict[str, object]:
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
                existing = self._request_row(conn, caller_id, request_id)
                if existing is not None:
                    if existing["operation_kind"] != "create" or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                if self._dnd_state(conn, target)["enabled"]:
                    outcome = {
                        "success": True, "status": "declined", "reason": "do_not_disturb",
                        "operator": target, "message": self._dnd_state(conn, target)["message"],
                        "human_action": False, "needs_continuation": False,
                    }
                    self._record_request(conn, caller_id, request_id, encoded, outcome, "create")
                    conn.commit()
                    return outcome
                quiet = self._quiet_window_state(conn, target)
                degraded = quiet.get("last_accept_at") is not None and \
                    (self._clock() - quiet["last_accept_at"]) < quiet["quiet_window_seconds"]
                if self._busy_call(conn, target) is not None:
                    outcome = self._busy_outcome(target)
                    self._record_request(conn, caller_id, request_id, encoded, outcome, "create")
                    conn.commit()
                    return outcome
                call_id, interaction_id = str(uuid.uuid4()), str(uuid.uuid4())
                deadline = self._clock() + self._ring_timeout
                conn.execute(
                    "INSERT INTO lifecycle_calls "
                    "(call_id, interaction_id, caller_id, call_state, ring_deadline, reason, payload_json, agent_reconnect_deadline, invited_desktop_id, mobile_eligible, operator_callsign, delivery_json, created_at) "
                    "VALUES (?, ?, ?, 'ringing', ?, NULL, ?, NULL, ?, ?, ?, ?, ?)",
                    (call_id, interaction_id, caller_id, deadline, encoded, invited_desktop_id,
                     int(mobile_eligible), target,
                     json.dumps(dict(delivery), sort_keys=True, separators=(",", ":")) if delivery else None,
                     self._clock()),
                )
                outcome = self._heartbeat(interaction_id, call_id, "ringing")
                outcome["operator"] = target
                notification = self._ring_notification(call_id, caller_id, payload, deadline, mobile_eligible, degraded, quiet)
                self._record_request(conn, caller_id, request_id, encoded, outcome, "create")
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if notification is not None:
            self._notify(notification)
        return outcome

    def resubmit(self, caller_id: str, request_id: str, interaction_id: str, call_id: str,
                 revision_id: str, payload: Mapping[str, object],
                 invited_desktop_id: str | None = None, mobile_eligible: bool = False,
                 operator_callsign: str | None = None) -> dict[str, object]:
        target = self.resolve_operator(operator_callsign)
        if target is None:
            return {"success": False, "status": "unknown_callsign", "needs_continuation": False}
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
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._request_row(conn, caller_id, request_id)
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
                    self._record_request(conn, caller_id, request_id, request_encoded, outcome, "resubmit")
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
                if self._busy_call(conn, target) is not None:
                    outcome = self._busy_outcome(target)
                    self._record_request(conn, caller_id, request_id, request_encoded, outcome, "resubmit")
                    conn.commit()
                    return outcome
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
                notification = self._ring_notification(new_call_id, caller_id, payload, deadline, mobile_eligible, False, None)
                self._record_request(conn, caller_id, request_id, request_encoded, outcome, "resubmit")
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
                existing = self._request_row(conn, caller_id, request_id)
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
                self._record_request(conn, caller_id, request_id, request_encoded, outcome, "task_update")
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    # ── agent read / continue / cancel ─────────────────────────────
    def has_request(self, caller_id: str, request_id: str) -> bool:
        with self._connection() as conn:
            return self._request_row(conn, caller_id, request_id) is not None

    def list_open_calls(self, caller_id: str) -> list[dict[str, object]]:
        """Enumerate a caller's active (not-yet-settled) rounds.

        This is the rediscovery path: an agent whose context was compacted
        has lost the ``interaction_id``/``call_id`` it needs to continue a
        round, and no other server-side handle points back at it. Each entry
        carries enough to resume (``interaction_id`` + ``call_id``; the
        continuation cursor is the returned event stream's high-water mark).
        """
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                rows = conn.execute(
                    "SELECT call_id, interaction_id, call_state, payload_json, operator_callsign, created_at "
                    "FROM lifecycle_calls WHERE caller_id=? AND call_state IN ('ringing','accepted','in_progress') "
                    "ORDER BY created_at",
                    (caller_id,),
                ).fetchall()
                result: list[dict[str, object]] = []
                for row in rows:
                    tool = None
                    try:
                        payload = self._load(row["payload_json"])
                        if isinstance(payload, dict):
                            tool = payload.get("tool")
                    except (TypeError, ValueError, json.JSONDecodeError):
                        tool = None
                    result.append({
                        "interaction_id": row["interaction_id"],
                        "call_id": row["call_id"],
                        "call_state": row["call_state"],
                        "tool": tool,
                        "operator": row["operator_callsign"],
                        "created_at": row["created_at"],
                    })
                conn.commit()
                return result
            except BaseException:
                conn.rollback()
                raise

    def continue_call(self, caller_id: str, request_id: str, interaction_id: str, call_id: str,
                      after: int) -> dict[str, object] | None:
        encoded = self._dump({"interaction_id": interaction_id, "call_id": call_id, "after_event_seq": after})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._request_row(conn, caller_id, request_id)
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
                elif call["call_state"] in _ACTIVE:
                    outcome = self._heartbeat(interaction_id, call_id, call["call_state"])
                else:
                    terminal = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    outcome = self._load(terminal["body_json"]) if terminal is not None else self._terminal(
                        interaction_id, call_id, call["call_state"], call["reason"], int(last),
                    )
                self._record_request(conn, caller_id, request_id, encoded, outcome, "continuation")
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def cancel(self, caller_id: str, request_id: str, interaction_id: str, call_id: str) -> dict[str, object]:
        encoded = self._dump({"interaction_id": interaction_id, "call_id": call_id})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._request_row(conn, caller_id, request_id)
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
                if call["call_state"] not in _ACTIVE:
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
                    conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                self._record_request(conn, caller_id, request_id, encoded, outcome, "cancel")
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def response_delivered(self, caller_id: str, response: Mapping[str, object]) -> None:
        status, state = response.get("status"), response.get("call_state")
        if state not in _ACTIVE or state == "ringing":
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
                    self._arm_agent_lease(conn, call_id, state)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def settle_deadlines(self) -> None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def apply_delivery_decisions(self, decisions: Mapping[str, object]) -> None:
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
                    body = self._terminal(call["interaction_id"], call_id, "unreachable", "no_available_channel", seq)
                    body["human_action"] = False
                    conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(body)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    # ── human actions ──────────────────────────────────────────────
    def terminal_action(self, *, call_id: str, device_id: str, session_id: str, terminal_platform: str,
                        operation_id: str, action: str) -> dict[str, object]:
        if action not in {"accept", "decline"}:
            return self._error("session_not_found")
        encoded = self._dump({"call_id": call_id, "action": action})
        actor = f"terminal:{session_id}"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._request_row(conn, actor, operation_id)
                kind = f"terminal_{action}"
                if existing is not None:
                    if existing["operation_kind"] != kind or existing["payload_json"] != encoded:
                        return self._error("request_conflict")
                    return self._load(existing["outcome_json"])
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None:
                    return self._error("session_not_found")
                if call["invited_desktop_id"] != device_id:
                    return self._error("session_not_found")
                if call["call_state"] != "ringing":
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,),
                    ).fetchone()
                    outcome = self._load(event["body_json"]) if event else self._error("expired")
                else:
                    status = "opened" if action == "accept" else "declined"
                    now = self._clock()
                    if action == "accept":
                        _, seq = self._machine.transition(
                            conn, call_id, "accept", reason=None,
                            columns=self._accept_columns(_DESKTOP, device_id, session_id, terminal_platform, now),
                        )
                        self._record_accept(conn, str(call["operator_callsign"]) if call["operator_callsign"] else None)
                    else:
                        _, seq = self._machine.transition(
                            conn, call_id, "decline", reason="human_declined",
                            columns={
                                "owner_device_id": device_id, "owner_session_id": session_id,
                                "handled_on": None, "terminal_platform": None,
                                "terminal_last_seen_at": None, "terminal_disconnect_deadline": None,
                                "agent_reconnect_deadline": None,
                            },
                        )
                    outcome = {"success": True, "status": status, "call_state": "accepted" if action == "accept" else "declined",
                               "human_action": True, "needs_continuation": action == "accept",
                               "interaction_id": call["interaction_id"], "call_id": call_id, "event_seq": seq}
                    if action == "accept":
                        outcome.update(owner_device_id=device_id, owner_session_id=session_id,
                                       handled_on=_DESKTOP, terminal_platform=terminal_platform)
                    else:
                        outcome.update(reason="human_declined", acted_on=_DESKTOP, handled_on=None,
                                       terminal_platform=None, retryable=False)
                    conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                self._record_request(conn, actor, operation_id, encoded, outcome, kind)
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def browser_action(self, *, call_id: str, operator_id: str, session_id: str,
                       operation_id: str, action: str) -> dict[str, object]:
        if action not in {"accept", "decline"}:
            return self._error("session_not_found")
        encoded = self._dump({"call_id": call_id, "action": action})
        actor = f"browser:{session_id}"
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._request_row(conn, actor, operation_id)
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
                if call["call_state"] != "ringing":
                    if call["reason"] == "ring_timeout":
                        event = conn.execute(
                            "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,),
                        ).fetchone()
                        return self._load(event["body_json"]) if event is not None else self._error("expired")
                    return self._error("session_not_found")
                status = "opened" if action == "accept" else "declined"
                now = self._clock()
                if action == "accept":
                    # Mobile is stateless — no owner-channel lease (§2.3).
                    _, seq = self._machine.transition(
                        conn, call_id, "accept", reason=None,
                        columns=self._accept_columns(_MOBILE, _MOBILE, session_id, "ios", now),
                    )
                    self._record_accept(conn, str(call["operator_callsign"]) if call["operator_callsign"] else None)
                else:
                    _, seq = self._machine.transition(
                        conn, call_id, "decline", reason="human_declined",
                        columns={
                            "owner_device_id": _MOBILE, "owner_session_id": session_id,
                            "handled_on": None, "terminal_platform": None,
                            "terminal_last_seen_at": None, "terminal_disconnect_deadline": None,
                            "agent_reconnect_deadline": None,
                        },
                    )
                outcome = {"success": True, "status": status, "call_state": "accepted" if action == "accept" else "declined",
                           "human_action": True, "needs_continuation": action == "accept",
                           "interaction_id": call["interaction_id"], "call_id": call_id, "event_seq": seq}
                if action == "accept":
                    outcome.update(owner_device_id=_MOBILE, owner_session_id=session_id,
                                   handled_on=_MOBILE, terminal_platform="ios")
                else:
                    outcome.update(reason="human_declined", acted_on=_MOBILE, handled_on=None,
                                   terminal_platform=None, retryable=False)
                conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                self._record_request(conn, actor, operation_id, encoded, outcome, kind)
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def submit(self, *, call_id: str, device_id: str, session_id: str,
               submission_id: str, result: Mapping[str, object],
               task_version: int | None = None) -> dict[str, object]:
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
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,),
                    ).fetchone()
                    return self._load(event["body_json"]) if event is not None else self._error("expired")
                if isinstance(task_version, int) and not isinstance(task_version, bool) and task_version != int(call["task_version"]):
                    return self._error("task_version_conflict")
                if self._owner_channel(call) == _DESKTOP:
                    self._refresh_owner_lease(conn, call_id)
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
        actor = f"terminal_cancel:{session_id}"
        encoded = self._dump({"call_id": call_id})
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                existing = self._request_row(conn, actor, operation_id)
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
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,),
                    ).fetchone()
                    outcome = self._load(event["body_json"]) if event is not None else self._error("expired")
                else:
                    _, seq = self._machine.transition(conn, call_id, "human_cancel", reason="human_cancelled")
                    outcome = {
                        "success": True, "status": "cancelled", "call_state": "cancelled",
                        "reason": "human_cancelled", "human_action": True, "needs_continuation": False,
                        "interaction_id": call["interaction_id"], "call_id": call_id, "event_seq": seq,
                        "handled_on": call["handled_on"], "terminal_platform": call["terminal_platform"],
                    }
                    conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                self._record_request(conn, actor, operation_id, encoded, outcome, "terminal_cancel")
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

    def terminal_heartbeat(self, *, call_id: str, session_id: str) -> dict[str, object]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None or call["owner_session_id"] != session_id:
                    return self._error("session_not_found")
                if call["call_state"] not in {"accepted", "in_progress"}:
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,),
                    ).fetchone()
                    outcome = self._load(event["body_json"]) if event else self._error("expired")
                    conn.commit()
                    return outcome
                if self._owner_channel(call) == _DESKTOP:
                    self._refresh_owner_lease(conn, call_id)
                conn.commit()
                return {"success": True, "status": "heartbeat", "call_state": call["call_state"], "needs_continuation": True}
            except BaseException:
                conn.rollback()
                raise

    def terminal_states(self, device_id: str) -> list[dict[str, object]]:
        """Rounds a terminal must know about: owned rounds plus rings invited to it.

        Owned rows (accepted, in_progress and any already-settled rounds this
        device still owns) keep the restarted terminal's answer surfaces in
        step with central authority; invited ringing rows make the poll — not
        only the push — able to present a call whose notification delivery
        was acknowledged by a process that died before showing it.
        """
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                self._refresh_owner_lease_for_device(conn, device_id)
                rows = conn.execute(
                    "SELECT call_id, call_state, version FROM lifecycle_calls "
                    "WHERE owner_device_id=? OR (invited_desktop_id=? AND call_state='ringing') ORDER BY call_id",
                    (device_id, device_id),
                ).fetchall()
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return [{"call_id": row["call_id"], "call_state": row["call_state"], "version": row["version"]} for row in rows]

    def reattach_owner_sessions(self, device_id: str, session_id: str) -> dict[str, object]:
        """Rebind this device's in-progress rounds to its current live session.

        A restarted terminal registers a fresh session while its device keeps
        owning rounds accepted earlier.  Owner identity (device + channel) is
        established at accept and immutable (I2); only the per-process session
        handle moves here, and only for the authenticated session of the very
        same device.  Rounds whose owner lease already expired settle first
        and are never resurrected.
        """
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                changed = conn.execute(
                    "UPDATE lifecycle_calls SET owner_session_id=? "
                    "WHERE owner_device_id=? AND call_state IN ('accepted','in_progress') AND owner_session_id<>?",
                    (session_id, device_id, session_id),
                ).rowcount
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return {"success": True, "status": "reattached", "reattached": int(changed or 0)}

    def save_draft(self, *, call_id: str, device_id: str, session_id: str,
                   draft_version: int, content: str) -> dict[str, object]:
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
                if self._owner_channel(call) == _DESKTOP:
                    self._refresh_owner_lease(conn, call_id)
                conn.execute(
                    "UPDATE lifecycle_calls SET draft_content=?, draft_version=? WHERE call_id=?",
                    (content, draft_version, call_id),
                )
                conn.commit()
                return {"success": True, "status": "draft_saved", "draft_version": draft_version}
            except BaseException:
                conn.rollback()
                raise

    def accept(self, *, call_id: str, device_id: str, handled_on: str,
               terminal_platform: str) -> dict[str, object]:
        """Accept a ringing call and establish ownership, exactly as the
        production accept paths (``terminal_action`` / ``browser_action``) do:
        the owner channel and its lease are written onto the call row, not
        merely stamped onto the opened event. ``device_id`` names the owning
        session."""
        channel = handled_on if handled_on in (_DESKTOP, _MOBILE) else _DESKTOP
        session_id = device_id
        platform = terminal_platform if channel == _DESKTOP else "ios"
        owner_id = device_id if channel == _DESKTOP else _MOBILE
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute("SELECT * FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
                if call is None:
                    return self._error("session_not_found")
                if call["call_state"] != "ringing":
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1",
                        (call_id,),
                    ).fetchone()
                    return self._load(event["body_json"]) if event is not None else self._error("expired")
                columns = self._accept_columns(channel, owner_id, session_id, platform, self._clock())
                _, seq = self._machine.transition(conn, call_id, "accept", reason=None, columns=columns)
                self._record_accept(conn, str(call["operator_callsign"]) if call["operator_callsign"] else None)
                outcome = {
                    "success": True, "status": "opened", "call_state": "accepted",
                    "human_action": True, "needs_continuation": True,
                    "interaction_id": call["interaction_id"], "call_id": call_id,
                    "event_seq": seq, "owner_device_id": owner_id,
                    "owner_session_id": session_id, "handled_on": channel,
                    "terminal_platform": platform,
                }
                conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(outcome)))
                conn.commit()
                return outcome
            except BaseException:
                conn.rollback()
                raise

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
                if call["call_state"] not in _ACTIVE:
                    event = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,),
                    ).fetchone()
                    return self._load(event["body_json"]) if event else self._error("expired")
                if status == "opened" and call["call_state"] != "ringing":
                    return self._error("already_accepted")
                if status != "opened" and call["call_state"] not in {"accepted", "in_progress"}:
                    return self._error("not_accepted")
                if status == "opened":
                    _, seq = self._machine.transition(conn, call_id, "accept", reason=reason, bump_version=False)
                else:
                    _, seq = self._machine.transition(conn, call_id, "submit", target=state, reason=reason, bump_version=False)
                body: dict[str, object] = {"success": True, "status": status, "call_state": state, "event_seq": seq,
                                            "needs_continuation": status in {"opened", "in_progress"},
                                            "interaction_id": call["interaction_id"], "call_id": call_id}
                body.update(extra)
                conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call_id, seq, self._dump(body)))
                conn.commit()
                return body
            except BaseException:
                conn.rollback()
                raise

    # ── attachments ────────────────────────────────────────────────
    def upload_attachment(self, *, call_id: str, device_id: str, session_id: str,
                          filename: str, media_type: str, content: bytes) -> dict[str, object]:
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
                if self._owner_channel(call) == _DESKTOP:
                    self._refresh_owner_lease(conn, call_id)
                attachment_id = str(uuid.uuid4())
                stored_path = self._attachments_dir / f"{attachment_id}.bin"
                stored_path.write_bytes(content)
                checksum = hashlib.sha256(content).hexdigest()
                conn.execute(
                    "INSERT INTO lifecycle_attachments "
                    "(attachment_id, call_id, filename, media_type, byte_count, checksum, stored_path) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (attachment_id, call_id, filename, media_type, len(content), checksum, str(stored_path)),
                )
                conn.commit()
                return {"success": True, "attachment_id": attachment_id, "filename": filename,
                        "media_type": media_type, "byte_count": len(content), "checksum": checksum}
            except BaseException:
                conn.rollback()
                raise

    def _attachment_metadata(self, conn: sqlite3.Connection, call_id: str, attachment_id: str) -> dict[str, object] | None:
        row = conn.execute(
            "SELECT attachment_id, filename, media_type, byte_count, checksum "
            "FROM lifecycle_attachments WHERE call_id=? AND attachment_id=?",
            (call_id, attachment_id),
        ).fetchone()
        if row is None:
            return None
        return {
            "attachment_id": row["attachment_id"], "filename": row["filename"],
            "media_type": row["media_type"], "byte_count": int(row["byte_count"]),
            "checksum": row["checksum"],
        }

    def _resolve_attachments(self, conn: sqlite3.Connection, call_id: str, attachment_ids: Any) -> list[dict[str, object]]:
        if not isinstance(attachment_ids, list):
            return []
        resolved = []
        for attachment_id in attachment_ids:
            if not isinstance(attachment_id, str):
                continue
            metadata = self._attachment_metadata(conn, call_id, attachment_id)
            if metadata is not None:
                resolved.append(metadata)
        return resolved

    # ── agent reads ────────────────────────────────────────────────
    def read_interaction_result(self, caller_id: str, interaction_id: object,
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
            call_id: str | None = None
            if isinstance(interaction_id, str) and interaction_id:
                call = conn.execute(
                    "SELECT call_id, caller_id FROM lifecycle_calls WHERE interaction_id=?", (interaction_id,)
                ).fetchone()
                if call is None or call["caller_id"] != caller_id:
                    return {"success": False, "status": "session_not_found"}
                call_id = call["call_id"]
            elif isinstance(result_id, str) and result_id:
                for row in conn.execute(
                    "SELECT s.call_id, s.outcome_json, c.caller_id "
                    "FROM lifecycle_submissions s JOIN lifecycle_calls c ON c.call_id = s.call_id",
                ).fetchall():
                    try:
                        outcome = self._load(row["outcome_json"])
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
                    if outcome.get("result_id") == result_id:
                        if row["caller_id"] != caller_id:
                            return {"success": False, "status": "session_not_found"}
                        call_id = row["call_id"]
                        break
                if call_id is None:
                    return {"success": False, "status": "not_found"}
            else:
                return {"success": False, "status": "session_not_found"}
            body: object = None
            if isinstance(result_id, str) and result_id:
                found = False
                for row in conn.execute("SELECT outcome_json FROM lifecycle_submissions WHERE call_id=?", (call_id,)).fetchall():
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
                    "SELECT outcome_json FROM lifecycle_submissions WHERE call_id=? ORDER BY rowid DESC LIMIT 1", (call_id,)
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
        result: dict[str, object] = {"success": True, "call_id": call_id, "result": body, "attachments": attachments}
        if isinstance(interaction_id, str) and interaction_id:
            result["interaction_id"] = interaction_id
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

    def read_interaction_attachment(self, caller_id: str, interaction_id: object, attachment_id: str,
                                    offset_bytes: object = 0, max_result_bytes: object = None) -> dict[str, object]:
        with self._connection() as conn:
            row = conn.execute(
                "SELECT a.call_id, a.stored_path, a.byte_count, a.checksum, c.caller_id, c.interaction_id "
                "FROM lifecycle_attachments a JOIN lifecycle_calls c ON c.call_id = a.call_id "
                "WHERE a.attachment_id=?",
                (attachment_id,),
            ).fetchone()
            if row is None:
                return {"success": False, "status": "not_found"}
            if row["caller_id"] != caller_id:
                return {"success": False, "status": "session_not_found"}
            if isinstance(interaction_id, str) and interaction_id and interaction_id != row["interaction_id"]:
                return {"success": False, "status": "session_not_found"}
            attachment = {"stored_path": row["stored_path"], "byte_count": row["byte_count"], "checksum": row["checksum"]}
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

    # ── feed / view ────────────────────────────────────────────────
    def ack_delivery(self, seq: int, status: str) -> None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO lifecycle_delivery_status (seq, status, updated_at) VALUES (?, ?, ?) "
                    "ON CONFLICT(seq) DO UPDATE SET status=excluded.status, updated_at=excluded.updated_at",
                    (seq, status, self._clock()),
                )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def outcome_feed(self, after: int, limit: int, *, caller_id: str | None = None,
                     undeliverable: bool = False) -> list[dict[str, object]]:
        result: list[dict[str, object]] = []
        with self._connection() as conn:
            cursor = after
            while len(result) < limit:
                batch = conn.execute(
                    "SELECT e.rowid AS seq, e.body_json, c.call_id, c.interaction_id, c.caller_id, "
                    "c.operator_callsign, c.delivery_json, c.payload_json, d.status AS ack_status "
                    "FROM lifecycle_events e JOIN lifecycle_calls c ON c.call_id = e.call_id "
                    "LEFT JOIN lifecycle_delivery_status d ON d.seq = e.rowid "
                    "WHERE e.rowid > ? AND (? IS NULL OR c.caller_id = ?) "
                    "ORDER BY e.rowid LIMIT ?",
                    (cursor, caller_id, caller_id, limit * 3),
                ).fetchall()
                if not batch:
                    break
                cursor = batch[-1]["seq"]
                for row in batch:
                    if len(result) >= limit:
                        break
                    try:
                        body = json.loads(row["body_json"])
                    except (TypeError, ValueError):
                        continue
                    if body.get("needs_continuation") is not False and body.get("status") != "in_progress":
                        continue
                    if (row["ack_status"] == "undeliverable") != undeliverable:
                        continue
                    delivery: dict[str, object] | None = None
                    tool: str | None = None
                    for column, into in (("delivery_json", "d"), ("payload_json", "t")):
                        raw = row[column]
                        if not raw:
                            continue
                        try:
                            loaded = json.loads(raw)
                        except (TypeError, ValueError):
                            continue
                        if not isinstance(loaded, dict):
                            continue
                        if into == "d":
                            delivery = loaded
                        elif isinstance(loaded.get("tool"), str):
                            tool = str(loaded["tool"])
                    result.append({
                        "seq": int(row["seq"]), "call_id": row["call_id"],
                        "interaction_id": row["interaction_id"],
                        "operator": row["operator_callsign"], "tool": tool,
                        "delivery": delivery, "outcome": body,
                    })
                if len(batch) < limit * 3:
                    break
        return result

    def call_version(self, call_id: str) -> int:
        with self._connection() as conn:
            row = conn.execute("SELECT version FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
        return int(row["version"]) if row is not None else 0

    def call_outcome_view(self, caller_id: str, interaction_id: str, call_id: str) -> dict[str, object] | None:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._settle_due(conn)
                call = conn.execute(
                    "SELECT * FROM lifecycle_calls WHERE call_id=? AND caller_id=?", (call_id, caller_id)
                ).fetchone()
                if call is None or call["interaction_id"] != interaction_id:
                    conn.commit()
                    return None
                state = str(call["call_state"])
                if state in ACTIVE_STATES:
                    outcome = self._heartbeat(interaction_id, call_id, state)
                else:
                    terminal = conn.execute(
                        "SELECT body_json FROM lifecycle_events WHERE call_id=? ORDER BY event_seq DESC LIMIT 1", (call_id,)
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
                bus_fired = wait_task.done() and not wait_task.cancelled() and wait_task.exception() is None
            finally:
                for task in (wait_task, sleep_task):
                    task.cancel()
            if bus_fired:
                continue
            if self._clock() - before + 1e-6 < remaining:
                return outcome

    # ── settlement ─────────────────────────────────────────────────
    def _settle_due(self, conn: sqlite3.Connection) -> None:
        """One deterministic settlement rule table (§2.4 of SPEC-refactor.md).

        * ringing + ring_deadline passed → ring_expire (not_accepted)
        * accepted/in_progress + owner-channel lease passed → terminal_disconnected
          (desktop only — a live terminal keeps the round open regardless of
          the agent lease, so a slow agent never discards the human's work)
        * accepted/in_progress + mobile + agent lease passed → agent_disconnected
          (mobile has no owner lease, so the agent lease is its only liveness
          signal)
        """
        now = self._clock()
        if self._clock_high_water is not None and (
            now < self._clock_high_water or now - self._clock_high_water > 3600
        ):
            self._settle_clock_anomaly(conn)
            self._clock_high_water = now
            return
        self._clock_high_water = now
        for call in conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_state='ringing' AND ring_deadline<=?", (now,)
        ).fetchall():
            _, seq = self._machine.transition(conn, call["call_id"], "ring_expire", reason="ring_timeout", bump_version=False)
            body = self._terminal(call["interaction_id"], call["call_id"], "not_accepted", "ring_timeout", seq)
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))
        # Desktop: the owner-channel lease is the liveness authority. A live
        # terminal means the human is present, so the round must NOT settle
        # just because the agent stopped polling — a slow turn or a compacted
        # context is not an abandonment. Only the terminal's expiry settles a
        # desktop round, so the human's work is never discarded on the
        # agent's account.
        for call in conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress') "
            "AND terminal_disconnect_deadline IS NOT NULL AND terminal_disconnect_deadline<=?",
            (now,),
        ).fetchall():
            _, seq = self._machine.transition(conn, call["call_id"], "disconnect", reason="terminal_disconnected")
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", "terminal_disconnected", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))

        # Mobile: stateless, no owner lease. The agent lease is the only
        # liveness signal, so a mobile round whose agent stopped polling
        # settles as agent_disconnected.
        for call in conn.execute(
            "SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress') "
            "AND handled_on=? AND agent_reconnect_deadline IS NOT NULL AND agent_reconnect_deadline<=?",
            (_MOBILE, now),
        ).fetchall():
            _, seq = self._machine.transition(conn, call["call_id"], "disconnect", reason="agent_disconnected")
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", "agent_disconnected", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))

    def _settle_clock_anomaly(self, conn: sqlite3.Connection) -> None:
        for call in conn.execute("SELECT * FROM lifecycle_calls WHERE call_state='ringing'").fetchall():
            _, seq = self._machine.transition(conn, call["call_id"], "clock_expire", reason="clock_anomaly", bump_version=False)
            body = self._terminal(call["interaction_id"], call["call_id"], "not_accepted", "clock_anomaly", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))
        for call in conn.execute("SELECT * FROM lifecycle_calls WHERE call_state IN ('accepted', 'in_progress')").fetchall():
            if call["handled_on"] == _MOBILE:
                # A clock jump invalidates *deadline-based* settlement. A
                # mobile round has no owner-channel lease and, if the agent
                # never polled, no agent lease either — it is stateless and
                # must not be settled by a clock anomaly it does not depend on.
                continue
            _, seq = self._machine.transition(conn, call["call_id"], "clock_expire", reason="clock_anomaly")
            body = self._terminal(call["interaction_id"], call["call_id"], "disconnected", "clock_anomaly", seq)
            body["human_action"] = False
            conn.execute("INSERT INTO lifecycle_events VALUES (?, ?, ?)", (call["call_id"], seq, self._dump(body)))

    # ── helpers ────────────────────────────────────────────────────
    def _busy_call(self, conn: sqlite3.Connection, target: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT call_id FROM lifecycle_calls WHERE call_state IN ('ringing', 'accepted', 'in_progress') "
            "AND operator_callsign=? LIMIT 1", (target,)
        ).fetchone()

    def _busy_outcome(self, target: str) -> dict[str, object]:
        return {
            "success": True, "status": "busy", "call_state": None, "call_id": None,
            "reason": None, "interaction_id": None, "human_action": False,
            "operator": target, "handled_on": None, "terminal_platform": None, "event_seq": None,
            "needs_continuation": False, "retryable": True, "retry_after_seconds": 30,
        }

    def _ring_notification(self, call_id: str, caller_id: str, payload: Mapping[str, object],
                           deadline: float, mobile_eligible: bool, degraded: bool,
                           quiet: dict[str, object] | None) -> dict[str, object]:
        short_title = next(
            (str(payload.get(key)) for key in ("title", "task_description", "prompt", "message")
             if isinstance(payload.get(key), str) and payload.get(key).strip()),
            "Human-in-the-Loop request",
        )
        notification = {
            "call_id": call_id, "ring_deadline": deadline,
            "title": caller_id, "body": " ".join(short_title.split())[:200],
            "mobile_eligible": bool(mobile_eligible),
        }
        if degraded:
            notification["degraded"] = True
            notification["quiet_mode"] = quiet["quiet_mode"] if quiet else "short"
        return notification

    def _request_row(self, conn: sqlite3.Connection, caller_id: str, request_id: str) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT payload_json, outcome_json, operation_kind FROM lifecycle_requests WHERE caller_id=? AND request_id=?",
            (caller_id, request_id),
        ).fetchone()

    def _record_request(self, conn: sqlite3.Connection, caller_id: str, request_id: str,
                        payload_json: str, outcome: dict[str, object], kind: str) -> None:
        conn.execute(
            "INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, ?)",
            (caller_id, request_id, payload_json, self._dump(outcome), kind),
        )

    @staticmethod
    def _interpret_submission(result: Mapping[str, object]) -> tuple[str, str, bool, dict[str, object]]:
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
        return {"success": True, "status": "heartbeat", "call_state": state,
                "interaction_id": interaction_id, "call_id": call_id, "needs_continuation": True}

    @staticmethod
    def _apply_result_budget(outcome: dict[str, object], payload_json: str) -> dict[str, object]:
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


_SCHEMA = """
CREATE TABLE IF NOT EXISTS lifecycle_calls (
  call_id TEXT PRIMARY KEY, interaction_id TEXT NOT NULL, caller_id TEXT NOT NULL,
  call_state TEXT NOT NULL, ring_deadline REAL NOT NULL, reason TEXT,
  payload_json TEXT NOT NULL DEFAULT '{}',
  owner_device_id TEXT, owner_session_id TEXT, handled_on TEXT,
  terminal_platform TEXT, invited_desktop_id TEXT,
  mobile_eligible INTEGER NOT NULL DEFAULT 0,
  operator_callsign TEXT,
  delivery_json TEXT,
  terminal_last_seen_at REAL, terminal_disconnect_deadline REAL,
  agent_reconnect_deadline REAL,
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
CREATE TABLE IF NOT EXISTS lifecycle_delivery_status (
  seq INTEGER PRIMARY KEY, status TEXT NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS lifecycle_quiet_window (
  operator_id TEXT PRIMARY KEY,
  quiet_window_seconds INTEGER NOT NULL DEFAULT 300,
  quiet_mode TEXT NOT NULL DEFAULT 'short',
  last_accept_at REAL
);
"""

_CALL_COLUMNS = (
    ("payload_json", "TEXT NOT NULL DEFAULT '{}'"),
    ("owner_device_id", "TEXT"), ("owner_session_id", "TEXT"),
    ("handled_on", "TEXT"), ("terminal_platform", "TEXT"),
    ("invited_desktop_id", "TEXT"),
    ("mobile_eligible", "INTEGER NOT NULL DEFAULT 0"),
    ("operator_callsign", "TEXT"), ("delivery_json", "TEXT"),
    ("terminal_last_seen_at", "REAL"), ("terminal_disconnect_deadline", "REAL"),
    ("version", "INTEGER NOT NULL DEFAULT 1"),
    ("draft_content", "TEXT"), ("draft_version", "INTEGER NOT NULL DEFAULT 0"),
    ("created_at", "REAL"), ("task_version", "INTEGER NOT NULL DEFAULT 1"),
)
