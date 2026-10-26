"""Persistent, transport-free coordination for one operator call at a time."""

from __future__ import annotations

import json
import hashlib
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol


class Clock(Protocol):
    def now_utc(self) -> datetime: ...


@dataclass(frozen=True)
class CreateCall:
    operator_id: str
    caller_id: str
    interaction_id: str
    request_id: str
    message: str
    ring_timeout_seconds: int
    desktop_route_device_id: str | None = None


@dataclass(frozen=True)
class AcceptCall:
    call_id: str
    action_id: str
    device_id: str
    session_id: str
    handled_on: str
    terminal_platform: str


@dataclass(frozen=True)
class DeclineCall:
    call_id: str
    action_id: str
    actor_id: str


@dataclass(frozen=True)
class CancelCall:
    call_id: str
    action_id: str
    device_id: str
    session_id: str


class CallCoordinator:
    """SQLite-backed admission and ringing-state coordinator.

    The coordinator deliberately has no transport or human-interface concerns.
    Every state transition is performed in a SQLite immediate transaction so a
    single active call and a single owner are preserved across threads/restarts.
    """

    def __init__(
        self, database_path: Path, *, clock: Clock, terminal_disconnect_seconds: int = 180,
        terminal_presence_seconds: int = 30,
        attachment_max_file_bytes: int = 20 * 1024 * 1024,
        attachment_max_submission_bytes: int = 50 * 1024 * 1024,
        attachment_max_archive_bytes: int = 1024 * 1024 * 1024,
    ) -> None:
        self._clock = clock
        self._terminal_disconnect_seconds = terminal_disconnect_seconds
        self._terminal_presence_seconds = terminal_presence_seconds
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
        self._attachment_directory = database_path.parent / "attachments"
        self._attachment_max_file_bytes = attachment_max_file_bytes
        self._attachment_max_submission_bytes = attachment_max_submission_bytes
        self._attachment_max_archive_bytes = attachment_max_archive_bytes
        self._initialize()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def create_call(self, request: CreateCall) -> dict[str, Any]:
        payload = self._canonical(asdict(request))
        with self._write_transaction() as connection:
            replay = connection.execute(
                "SELECT payload, result FROM create_operations "
                "WHERE caller_id = ? AND request_id = ?",
                (request.caller_id, request.request_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()

            self._settle_all_expired(connection)
            active = connection.execute(
                "SELECT 1 FROM calls WHERE operator_id = ? "
                "AND state IN ('ringing', 'accepted') LIMIT 1",
                (request.operator_id,),
            ).fetchone()
            if active is not None:
                result = self._busy()
            else:
                call_id = str(uuid.uuid4())
                now = self._now()
                deadline = now + timedelta(seconds=request.ring_timeout_seconds)
                connection.execute(
                    "INSERT INTO calls "
                    "(call_id, operator_id, caller_id, interaction_id, message, state, "
                    "created_at, ring_deadline, version, desktop_route_device_id) "
                    "VALUES (?, ?, ?, ?, ?, 'ringing', ?, ?, 1, ?)",
                    (
                        call_id,
                        request.operator_id,
                        request.caller_id,
                        request.interaction_id,
                        request.message,
                        self._stamp(now),
                        self._stamp(deadline),
                        request.desktop_route_device_id,
                    ),
                )
                result = self._envelope(
                    status="ringing",
                    call_state="ringing",
                    interaction_id=request.interaction_id,
                    call_id=call_id,
                    retryable=False,
                )
            connection.execute(
                "INSERT INTO create_operations (caller_id, request_id, payload, result) "
                "VALUES (?, ?, ?, ?)",
                (request.caller_id, request.request_id, payload, self._canonical(result)),
            )
            return result

    def accept_call(self, request: AcceptCall) -> dict[str, Any]:
        return self._act("accept", request.call_id, request.action_id, asdict(request))

    def decline_call(self, request: DeclineCall) -> dict[str, Any]:
        return self._act("decline", request.call_id, request.action_id, asdict(request))

    def terminal_state(self, device_id: str, call_id: str | None = None) -> dict[str, Any] | None:
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(connection, device_id, call_id)
            if row is None:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, row["call_id"])
            if row["state"] not in {"ringing", "accepted"}:
                return self._current_result(connection, row)
            return self._terminal_state_result(row)

    def register_terminal_presence(
        self, *, device_id: str, terminal_platform: str, route_device_id: str,
        callback_url: str | None, instance_id: str, gui_ready: bool,
    ) -> dict[str, Any]:
        """Persist only credential-derived device health; never touch call liveness."""
        now = self._now()
        expires_at = now + timedelta(seconds=self._terminal_presence_seconds)
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO terminal_presence "
                "(device_id, terminal_platform, route_device_id, callback_url, instance_id, "
                "gui_ready, last_seen_at, online_expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET terminal_platform = excluded.terminal_platform, "
                "route_device_id = excluded.route_device_id, callback_url = excluded.callback_url, "
                "instance_id = excluded.instance_id, gui_ready = excluded.gui_ready, "
                "last_seen_at = excluded.last_seen_at, online_expires_at = excluded.online_expires_at",
                (device_id, terminal_platform, route_device_id, callback_url, instance_id,
                 int(gui_ready), self._stamp(now), self._stamp(expires_at)),
            )
            return self._terminal_presence_result(self._presence_or_raise(connection, device_id))

    def terminal_device_heartbeat(self, device_id: str, instance_id: str) -> dict[str, Any]:
        now = self._now()
        expires_at = now + timedelta(seconds=self._terminal_presence_seconds)
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT * FROM terminal_presence WHERE device_id = ?", (device_id,)
            ).fetchone()
            if row is None or row["instance_id"] != instance_id:
                return self._instance_conflict()
            connection.execute(
                "UPDATE terminal_presence SET last_seen_at = ?, online_expires_at = ? WHERE device_id = ?",
                (self._stamp(now), self._stamp(expires_at), device_id),
            )
            return self._terminal_presence_result(self._presence_or_raise(connection, device_id))

    def terminal_presence(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM terminal_presence WHERE device_id = ?", (device_id,)
            ).fetchone()
            return self._terminal_presence_result(row) if row is not None else None

    def terminal_poll_snapshot(
        self, device_id: str, call_id: str | None = None
    ) -> dict[str, Any] | None:
        """Return the content-free registered-terminal call view after expiry settlement."""
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(connection, device_id, call_id)
            if row is None:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, row["call_id"])
            return {
                "call_id": row["call_id"],
                "state": row["state"],
                "version": row["version"],
                "ring_deadline": row["ring_deadline"],
                "terminal_disconnect_deadline": row["terminal_disconnect_deadline"],
            }

    def terminal_action(
        self,
        kind: str,
        call_id: str,
        device_id: str,
        action_id: str,
        session_id: str | None,
        terminal_platform: str,
        legacy_route_device_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Perform a routed terminal action without allowing an outsider to reserve an ID."""
        if kind not in {"accept", "decline", "cancel"}:
            raise ValueError(kind)
        scoped_action_id, action = self._terminal_action_request(
            kind, call_id, device_id, action_id, session_id, terminal_platform
        )
        payload = self._canonical(action)
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(
                connection, device_id, call_id, legacy_route_device_id=legacy_route_device_id
            )
            if row is None:
                return None
            # Route authorization deliberately precedes replay lookup.  An
            # unauthorized credential must neither observe nor reserve an ID.
            if kind == "cancel" and (
                row["owner_device_id"] != device_id or row["owner_session_id"] != session_id
            ):
                return self._ownership_conflict()
            replay = self._action_replay(connection, scoped_action_id, payload)
            if replay is not None:
                return replay
            return self._perform_terminal_action(
                connection, kind, call_id, scoped_action_id, payload, action, row
            )

    def terminal_heartbeat(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        legacy_route_device_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Refresh one accepted terminal session without creating an operation replay."""
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(
                connection, device_id, call_id, legacy_route_device_id=legacy_route_device_id
            )
            if row is None:
                return None
            if (
                row["owner_device_id"] != device_id
                or row["owner_session_id"] != owner_session_id
            ):
                return self._ownership_conflict()
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            self._refresh_terminal_liveness(connection, call_id)
            return self._current_result(connection, self._call_or_raise(connection, call_id))

    def read_terminal_draft(
        self, call_id: str, device_id: str, owner_session_id: str
    ) -> dict[str, Any] | None:
        """Return a draft only to the accepted terminal session that owns it."""
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(connection, device_id, call_id)
            if row is None:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if (
                row["state"] != "accepted"
                or row["owner_device_id"] != device_id
                or row["owner_session_id"] != owner_session_id
            ):
                return None
            return {"body": row["draft_body"], "draft_version": row["draft_version"]}

    def save_terminal_draft(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        body: str,
        expected_draft_version: int,
    ) -> dict[str, Any] | None:
        """CAS-save one opaque draft without changing call, event, or task state."""
        if (
            not isinstance(body, str)
            or not isinstance(expected_draft_version, int)
            or isinstance(expected_draft_version, bool)
            or expected_draft_version < 0
            or len(body.encode("utf-8")) > 256
        ):
            return {"success": False, "status": "invalid_draft"}
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(connection, device_id, call_id)
            if row is None:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return {"success": False, "status": "ownership_conflict"}
            if row["draft_version"] != expected_draft_version:
                return {
                    "success": False,
                    "status": "draft_version_conflict",
                    "draft_version": row["draft_version"],
                }
            draft_version = expected_draft_version + 1
            connection.execute(
                "UPDATE calls SET draft_body = ?, draft_version = ? WHERE call_id = ?",
                (body, draft_version, call_id),
            )
            self._refresh_terminal_liveness(connection, call_id)
            return {
                "success": True,
                "status": "saved",
                "draft": {"body": body, "draft_version": draft_version},
            }

    def submit_terminal_text(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        submission_id: str,
        user_input: str,
        task_version: int = 0,
        attachment_ids: list[str] | None = None,
        legacy_route_device_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Persist one final simple-text answer from the accepted owner session.

        Submission replay is deliberately checked only after route and session
        ownership: an unrelated terminal must not learn whether a submission id
        exists for another call.
        """
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or len(submission_id) > 128
            or not submission_id.isascii()
            or not isinstance(user_input, str)
            or not isinstance(task_version, int)
            or isinstance(task_version, bool)
            or task_version < 0
            or (
                attachment_ids is not None
                and (
                    not isinstance(attachment_ids, list)
                    or any(not isinstance(attachment_id, str) or not attachment_id for attachment_id in attachment_ids)
                )
            )
        ):
            return {"success": False, "status": "invalid_submission"}
        has_attachment_ids = attachment_ids is not None
        attachment_ids = [] if attachment_ids is None else attachment_ids
        payload = self._canonical(
            {
                "user_input": user_input,
                "task_version": task_version,
                "attachment_ids": attachment_ids,
            }
        )
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(
                connection, device_id, call_id, legacy_route_device_id=legacy_route_device_id
            )
            if row is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return None
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations "
                "WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            if row["task_version"] != task_version:
                return {
                    "success": False,
                    "status": "task_version_conflict",
                    "call_state": row["state"],
                    "task_version": row["task_version"],
                    "needs_continuation": False,
                }
            if attachment_ids:
                found = connection.execute(
                    "SELECT attachment_id FROM attachments WHERE call_id = ? "
                    "AND attachment_id IN (%s)" % ", ".join("?" for _ in attachment_ids),
                    (call_id, *attachment_ids),
                ).fetchall()
                if {item["attachment_id"] for item in found} != set(attachment_ids):
                    return {
                        "success": False,
                        "status": "invalid_attachment_reference",
                        "needs_continuation": False,
                    }
            result = {
                "success": True,
                "status": "answered",
                "call_state": "answered",
                "reason": None,
                "human_action": True,
                "needs_continuation": False,
                "interaction_id": row["interaction_id"],
                "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"],
                "terminal_platform": row["terminal_platform"],
                "user_input": user_input,
            }
            if has_attachment_ids:
                result["attachment_ids"] = attachment_ids
            connection.execute(
                "UPDATE calls SET state = 'answered', version = version + 1 WHERE call_id = ?",
                (call_id,),
            )
            self._add_event(connection, call_id, result)
            connection.execute(
                "INSERT INTO submission_operations (call_id, submission_id, payload, result) "
                "VALUES (?, ?, ?, ?)",
                (call_id, submission_id, payload, self._canonical(result)),
            )
            return result

    def upload_terminal_attachment(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        original_name: str,
        media_type: str,
        content: bytes,
        legacy_route_device_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Archive one attachment without completing the accepted call.

        The gateway has already decoded the sole multipart entity.  This
        transaction deliberately authorizes and settles expiry before any
        archive directory is made or bytes are written.
        """
        if (
            not isinstance(original_name, str)
            or not original_name
            or not isinstance(media_type, str)
            or not media_type
            or not isinstance(content, bytes)
        ):
            return {"success": False, "status": "invalid_attachment"}
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(
                connection, device_id, call_id, legacy_route_device_id=legacy_route_device_id
            )
            if row is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "accepted":
                return self._current_result(connection, row)

            byte_count = len(content)
            if byte_count > self._attachment_max_file_bytes:
                return self._attachment_rejected("file_limit_exceeded")
            if byte_count > self._attachment_max_submission_bytes:
                return self._attachment_rejected("submission_limit_exceeded")
            archived_bytes = connection.execute(
                "SELECT COALESCE(SUM(byte_count), 0) FROM attachments"
            ).fetchone()[0]
            occupied_bytes = max(archived_bytes, self._attachment_bytes_on_disk())
            if occupied_bytes + byte_count > self._attachment_max_archive_bytes:
                return self._attachment_rejected("archive_capacity_exceeded")

            attachment_id = str(uuid.uuid4())
            digest = hashlib.sha256(content).hexdigest()
            self._attachment_directory.mkdir(parents=True, exist_ok=True)
            storage_path = self._attachment_directory / attachment_id
            storage_path.write_bytes(content)
            connection.execute(
                "INSERT INTO attachments "
                "(attachment_id, call_id, original_name, media_type, byte_count, sha256, storage_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    attachment_id,
                    call_id,
                    original_name,
                    media_type,
                    byte_count,
                    digest,
                    str(storage_path),
                ),
            )
            self._refresh_terminal_liveness(connection, call_id)
            return {
                "success": True,
                "status": "uploaded",
                "attachment": {
                    "attachment_id": attachment_id,
                    "name": original_name,
                    "media_type": media_type,
                    "byte_count": byte_count,
                    "sha256": digest,
                },
            }

    def reject_terminal_attachment_submission_limit(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        legacy_route_device_id: str | None = None,
    ) -> dict[str, Any] | None:
        """Authorize and settle an oversized multipart before rejecting it.

        The HTTP layer uses this without reading the oversized file payload.
        Keep the same routing, ownership, and expiry ordering as uploads so a
        capacity response cannot disclose a call to another terminal.
        """
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(
                connection, device_id, call_id, legacy_route_device_id=legacy_route_device_id
            )
            if row is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            return self._attachment_rejected("submission_limit_exceeded")

    @staticmethod
    def _attachment_rejected(reason: str) -> dict[str, Any]:
        return {"success": False, "status": "attachment_rejected", "reason": reason}

    def _attachment_bytes_on_disk(self) -> int:
        if not self._attachment_directory.exists():
            return 0
        return sum(
            path.stat().st_size
            for path in self._attachment_directory.rglob("*")
            if path.is_file()
        )

    def _routed_terminal_call(
        self,
        connection: sqlite3.Connection,
        device_id: str,
        call_id: str | None,
        *,
        legacy_route_device_id: str | None = None,
    ) -> sqlite3.Row | None:
        """Look up only a call visible to this terminal credential."""
        if call_id is None:
            return connection.execute(
                "SELECT * FROM calls WHERE desktop_route_device_id = ? "
                "AND state IN ('ringing', 'accepted') ORDER BY created_at LIMIT 1",
                (device_id,),
            ).fetchone()
        if legacy_route_device_id == device_id:
            # Only the configured legacy route can recover an older NULL
            # route. A NULL value never authorizes an arbitrary terminal.
            route_predicate = "(desktop_route_device_id = ? OR desktop_route_device_id IS NULL)"
        else:
            route_predicate = "desktop_route_device_id = ?"
        return connection.execute(
            f"SELECT * FROM calls WHERE call_id = ? AND {route_predicate}",
            (call_id, device_id),
        ).fetchone()

    @staticmethod
    def _terminal_state_result(row: sqlite3.Row) -> dict[str, Any]:
        result: dict[str, Any] = {
            "call_id": row["call_id"],
            "call_state": row["state"],
            "version": row["version"],
            "ring_deadline": row["ring_deadline"],
        }
        if row["owner_device_id"] is not None:
            result["owner_device_id"] = row["owner_device_id"]
            result["owner_session_id"] = row["owner_session_id"]
        return result

    def _terminal_presence_result(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "device_id": row["device_id"],
            "terminal_platform": row["terminal_platform"],
            "route_device_id": row["route_device_id"],
            "callback_url": row["callback_url"],
            "instance_id": row["instance_id"],
            "gui_ready": bool(row["gui_ready"]),
            "online": self._now() < self._parse_stamp(row["online_expires_at"]),
            "last_seen_at": row["last_seen_at"],
            "online_expires_at": row["online_expires_at"],
        }

    @staticmethod
    def _instance_conflict() -> dict[str, Any]:
        return {"success": False, "status": "instance_conflict", "needs_continuation": False}

    @staticmethod
    def _presence_or_raise(connection: sqlite3.Connection, device_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT * FROM terminal_presence WHERE device_id = ?", (device_id,)
        ).fetchone()
        if row is None:
            raise KeyError(device_id)
        return row

    @staticmethod
    def _terminal_action_request(
        kind: str,
        call_id: str,
        device_id: str,
        action_id: str,
        session_id: str | None,
        terminal_platform: str,
    ) -> tuple[str, dict[str, Any]]:
        scoped_action_id = f"terminal:{device_id}:{call_id}:{action_id}"
        if kind == "accept":
            return scoped_action_id, asdict(
                AcceptCall(call_id, scoped_action_id, device_id, session_id or "", "desktop", terminal_platform)
            )
        if kind == "decline":
            return scoped_action_id, asdict(DeclineCall(call_id, scoped_action_id, device_id))
        return scoped_action_id, asdict(CancelCall(call_id, scoped_action_id, device_id, session_id or ""))

    def _action_replay(
        self, connection: sqlite3.Connection, action_id: str, payload: str
    ) -> dict[str, Any] | None:
        replay = connection.execute(
            "SELECT payload, result FROM action_operations WHERE action_id = ?", (action_id,)
        ).fetchone()
        if replay is None:
            return None
        if replay["payload"] == payload:
            return self._decode(replay["result"])
        return self._conflict()

    def _perform_terminal_action(
        self,
        connection: sqlite3.Connection,
        kind: str,
        call_id: str,
        action_id: str,
        payload: str,
        action: dict[str, Any],
        row: sqlite3.Row,
    ) -> dict[str, Any]:
        # Expiry settlement occurs only after authorization and replay lookup.
        self._settle_if_expired(connection, row)
        row = self._call_or_raise(connection, call_id)
        if kind == "cancel":
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            result = self._cancel(connection, row)
        elif row["state"] != "ringing":
            return self._current_result(connection, row)
        else:
            result = self._accept(connection, row, action) if kind == "accept" else self._decline(connection, row)
        # Persist the replay snapshot only after the routed mutation succeeds.
        connection.execute(
            "INSERT INTO action_operations (action_id, payload, result) VALUES (?, ?, ?)",
            (action_id, payload, self._canonical(result)),
        )
        return result

    def continue_call(
        self,
        caller_id: str,
        request_id: str,
        interaction_id: str,
        call_id: str,
        after_event_seq: int,
        payload: str,
    ) -> dict[str, Any]:
        """Read one durable event, or save a nonblocking heartbeat snapshot.

        A continuation is intentionally not a wait.  Its operation record makes
        a lost HTTP response replayable without advancing either cursor or the
        call's absolute ringing deadline.
        """
        operation_payload = self._canonical(
            {
                "interaction_id": interaction_id,
                "call_id": call_id,
                "after_event_seq": after_event_seq,
                "payload": payload,
            }
        )
        with self._write_transaction() as connection:
            row = self._owned_call(connection, caller_id, interaction_id, call_id)
            if row is None:
                return self._session_not_found()

            replay = self._continuation_replay(
                connection, caller_id, request_id, operation_payload
            )
            if replay is not None:
                return replay

            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            result = self._continuation_result(connection, row, after_event_seq)
            self._store_continuation_snapshot(
                connection, caller_id, request_id, operation_payload, result
            )
            return result

    def cancel_interaction(
        self, caller_id: str, interaction_id: str, call_id: str, request_id: str
    ) -> dict[str, Any]:
        """End one active call on behalf of its original authenticated caller.

        The caller-scoped operation snapshot is deliberately separate from
        terminal action replay records: an agent request id must be durable
        across restarts without granting any terminal-style ownership.
        """
        operation_payload = self._canonical(
            {"interaction_id": interaction_id, "call_id": call_id}
        )
        with self._write_transaction() as connection:
            replay = connection.execute(
                "SELECT payload, result FROM cancel_operations "
                "WHERE caller_id = ? AND request_id = ?",
                (caller_id, request_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == operation_payload:
                    return self._decode(replay["result"])
                return self._conflict()
            row = self._owned_call(connection, caller_id, interaction_id, call_id)
            if row is None:
                return self._session_not_found()

            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            result = (
                self._agent_cancel(connection, row)
                if row["state"] in {"ringing", "accepted"}
                else self._current_result(connection, row)
            )
            connection.execute(
                "INSERT INTO cancel_operations (caller_id, request_id, payload, result) "
                "VALUES (?, ?, ?, ?)",
                (caller_id, request_id, operation_payload, self._canonical(result)),
            )
            return result

    def get_call(self, call_id: str) -> dict[str, Any]:
        with self._write_transaction() as connection:
            row = self._call_or_raise(connection, call_id)
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            return self._current_result(connection, row)

    def events_for(self, call_id: str) -> list[dict[str, Any]]:
        with self._write_transaction() as connection:
            row = self._call_or_raise(connection, call_id)
            self._settle_if_expired(connection, row)
            events = connection.execute(
                "SELECT result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
            ).fetchall()
            return [self._decode(event["result"]) for event in events]

    def recover_and_settle(self) -> list[dict[str, Any]]:
        with self._write_transaction() as connection:
            return self._settle_all_expired(connection)

    def _act(
        self, kind: str, call_id: str, action_id: str, action: dict[str, Any]
    ) -> dict[str, Any]:
        payload = self._canonical(action)
        with self._write_transaction() as connection:
            replay = connection.execute(
                "SELECT payload, result FROM action_operations WHERE action_id = ?",
                (action_id,),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()

            row = self._call_or_raise(connection, call_id)
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "ringing":
                return self._current_result(connection, row)

            if kind == "accept":
                result = self._accept(connection, row, action)
            else:
                result = self._decline(connection, row)
            connection.execute(
                "INSERT INTO action_operations (action_id, payload, result) VALUES (?, ?, ?)",
                (action_id, payload, self._canonical(result)),
            )
            return result

    def _accept(
        self, connection: sqlite3.Connection, row: sqlite3.Row, action: dict[str, Any]
    ) -> dict[str, Any]:
        result = self._envelope(
            status="opened",
            call_state="accepted",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            human_action=True,
            needs_continuation=True,
            handled_on=action["handled_on"],
            terminal_platform=action["terminal_platform"],
            event_seq=1,
            owner_device_id=action["device_id"],
            owner_session_id=action["session_id"],
        )
        now = self._now()
        connection.execute(
            "UPDATE calls SET state = 'accepted', owner_device_id = ?, "
            "owner_session_id = ?, handled_on = ?, terminal_platform = ?, "
            "version = version + 1 "
            "WHERE call_id = ?",
            (
                action["device_id"],
                action["session_id"],
                action["handled_on"],
                action["terminal_platform"],
                row["call_id"],
            ),
        )
        self._refresh_terminal_liveness(connection, row["call_id"], now=now)
        self._add_event(connection, row["call_id"], result)
        return result

    def _decline(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = self._envelope(
            status="declined",
            call_state="declined",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            human_action=True,
            retryable=False,
            event_seq=1,
        )
        connection.execute(
            "UPDATE calls SET state = 'declined', version = version + 1 WHERE call_id = ?",
            (row["call_id"],),
        )
        self._add_event(connection, row["call_id"], result)
        return result

    def _cancel(self, connection: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        result = self._envelope(
            status="cancelled",
            call_state="cancelled",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            reason="human_cancelled",
            human_action=True,
            handled_on=row["handled_on"],
            terminal_platform=row["terminal_platform"],
            event_seq=(self._event_seq(connection, row["call_id"]) or 0) + 1,
            owner_device_id=row["owner_device_id"],
            owner_session_id=row["owner_session_id"],
            ring_deadline=row["ring_deadline"],
        )
        connection.execute(
            "UPDATE calls SET state = 'cancelled', version = version + 1 WHERE call_id = ?",
            (row["call_id"],),
        )
        self._add_event(connection, row["call_id"], result)
        return result

    def _agent_cancel(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        result = self._envelope(
            status="cancelled",
            call_state="cancelled",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            reason="agent_cancelled",
            event_seq=(self._event_seq(connection, row["call_id"]) or 0) + 1,
            handled_on=row["handled_on"],
            terminal_platform=row["terminal_platform"],
            owner_device_id=row["owner_device_id"],
            owner_session_id=row["owner_session_id"],
            ring_deadline=row["ring_deadline"],
        )
        connection.execute(
            "UPDATE calls SET state = 'cancelled', version = version + 1 WHERE call_id = ?",
            (row["call_id"],),
        )
        self._add_event(connection, row["call_id"], result)
        return result

    def _settle_all_expired(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute(
            "SELECT * FROM calls WHERE state IN ('ringing', 'accepted')"
        ).fetchall()
        settled: list[dict[str, Any]] = []
        for row in rows:
            result = self._settle_if_expired(connection, row)
            if result is not None:
                settled.append(result)
        return settled

    def _settle_if_expired(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        if row["state"] == "ringing":
            return self._settle_ringing_timeout(connection, row)
        if row["state"] == "accepted":
            return self._settle_terminal_disconnect(connection, row)
        return None

    def _settle_ringing_timeout(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        if self._now() < self._parse_stamp(row["ring_deadline"]):
            return None
        result = self._envelope(
            status="not_accepted",
            call_state="not_accepted",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            reason="ring_timeout",
            retryable=True,
            retry_after_seconds=60,
            event_seq=1,
        )
        connection.execute(
            "UPDATE calls SET state = 'not_accepted', version = version + 1 WHERE call_id = ?",
            (row["call_id"],),
        )
        self._add_event(connection, row["call_id"], result)
        return result

    def _settle_terminal_disconnect(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        if row["terminal_disconnect_deadline"] is None:
            return None
        if self._now() < self._parse_stamp(row["terminal_disconnect_deadline"]):
            return None
        result = self._envelope(
            status="disconnected",
            call_state="disconnected",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            reason="terminal_disconnected",
            event_seq=(self._event_seq(connection, row["call_id"]) or 0) + 1,
            handled_on=row["handled_on"],
            terminal_platform=row["terminal_platform"],
            owner_device_id=row["owner_device_id"],
            owner_session_id=row["owner_session_id"],
            ring_deadline=row["ring_deadline"],
        )
        connection.execute(
            "UPDATE calls SET state = 'disconnected', version = version + 1 WHERE call_id = ?",
            (row["call_id"],),
        )
        self._add_event(connection, row["call_id"], result)
        return result

    def _refresh_terminal_liveness(
        self, connection: sqlite3.Connection, call_id: str, *, now: datetime | None = None
    ) -> None:
        now = now or self._now()
        disconnect_deadline = now + timedelta(seconds=self._terminal_disconnect_seconds)
        connection.execute(
            "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? "
            "WHERE call_id = ?",
            (self._stamp(now), self._stamp(disconnect_deadline), call_id),
        )

    def _current_result(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        if row["state"] in {"declined", "not_accepted", "disconnected", "cancelled", "answered"}:
            event = connection.execute(
                "SELECT result FROM events WHERE call_id = ? ORDER BY event_seq DESC LIMIT 1",
                (row["call_id"],),
            ).fetchone()
            return self._decode(event["result"])
        return self._envelope(
            status=row["state"],
            call_state=row["state"],
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            ring_deadline=row["ring_deadline"],
            needs_continuation=row["state"] == "accepted",
            handled_on=row["handled_on"],
            terminal_platform=row["terminal_platform"],
            event_seq=self._event_seq(connection, row["call_id"]),
            owner_device_id=row["owner_device_id"],
            owner_session_id=row["owner_session_id"],
            version=row["version"],
        )

    @staticmethod
    def _owned_call(
        connection: sqlite3.Connection, caller_id: str, interaction_id: str, call_id: str
    ) -> sqlite3.Row | None:
        """Return a call only when all caller-scoped continuation IDs match."""
        return connection.execute(
            "SELECT * FROM calls WHERE call_id = ? AND interaction_id = ? "
            "AND caller_id = ?",
            (call_id, interaction_id, caller_id),
        ).fetchone()

    def _continuation_replay(
        self,
        connection: sqlite3.Connection,
        caller_id: str,
        request_id: str,
        operation_payload: str,
    ) -> dict[str, Any] | None:
        """Return a saved response or conflict before any state can be changed."""
        replay = connection.execute(
            "SELECT payload, result FROM continuation_operations "
            "WHERE caller_id = ? AND request_id = ?",
            (caller_id, request_id),
        ).fetchone()
        if replay is None:
            return None
        if replay["payload"] == operation_payload:
            return self._decode(replay["result"])
        return self._conflict()

    def _continuation_result(
        self, connection: sqlite3.Connection, row: sqlite3.Row, after_event_seq: int
    ) -> dict[str, Any]:
        """Return the next event, terminal state, or durable heartbeat snapshot."""
        if after_event_seq > (self._event_seq(connection, row["call_id"]) or 0):
            return self._invalid_cursor()
        event = self._event_after(connection, row["call_id"], after_event_seq)
        if event is not None:
            return self._decode(event["result"])
        if row["state"] in {"declined", "not_accepted", "disconnected", "cancelled", "answered"}:
            return self._current_result(connection, row)
        return self._envelope(
            status="heartbeat",
            call_state=row["state"],
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            needs_continuation=True,
            handled_on=row["handled_on"],
            terminal_platform=row["terminal_platform"],
            event_seq=after_event_seq,
            owner_device_id=row["owner_device_id"],
            owner_session_id=row["owner_session_id"],
            version=row["version"],
            ring_deadline=row["ring_deadline"],
        )

    @staticmethod
    def _event_after(
        connection: sqlite3.Connection, call_id: str, after_event_seq: int
    ) -> sqlite3.Row | None:
        return connection.execute(
            "SELECT result FROM events WHERE call_id = ? "
            "AND event_seq > ? ORDER BY event_seq LIMIT 1",
            (call_id, after_event_seq),
        ).fetchone()

    def _store_continuation_snapshot(
        self,
        connection: sqlite3.Connection,
        caller_id: str,
        request_id: str,
        operation_payload: str,
        result: dict[str, Any],
    ) -> None:
        connection.execute(
            "INSERT INTO continuation_operations "
            "(caller_id, request_id, payload, result) VALUES (?, ?, ?, ?)",
            (caller_id, request_id, operation_payload, self._canonical(result)),
        )

    def _add_event(
        self, connection: sqlite3.Connection, call_id: str, result: dict[str, Any]
    ) -> None:
        event_seq = (self._event_seq(connection, call_id) or 0) + 1
        connection.execute(
            "INSERT INTO events (call_id, event_seq, result) VALUES (?, ?, ?)",
            (call_id, event_seq, self._canonical(result)),
        )

    def _event_seq(self, connection: sqlite3.Connection, call_id: str) -> int | None:
        event = connection.execute(
            "SELECT MAX(event_seq) AS event_seq FROM events WHERE call_id = ?", (call_id,)
        ).fetchone()
        return event["event_seq"]

    def _call_or_raise(self, connection: sqlite3.Connection, call_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        if row is None:
            raise KeyError(call_id)
        return row

    def _initialize(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS calls (
                call_id TEXT PRIMARY KEY, operator_id TEXT NOT NULL, caller_id TEXT NOT NULL,
                interaction_id TEXT NOT NULL, message TEXT NOT NULL, state TEXT NOT NULL,
                created_at TEXT NOT NULL, ring_deadline TEXT NOT NULL, version INTEGER NOT NULL,
                owner_device_id TEXT, owner_session_id TEXT, handled_on TEXT,
                terminal_platform TEXT, desktop_route_device_id TEXT,
                terminal_last_seen_at TEXT, terminal_disconnect_deadline TEXT,
                draft_body TEXT, draft_version INTEGER NOT NULL DEFAULT 0,
                task_version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS create_operations (
                caller_id TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY (caller_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS action_operations (
                action_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                call_id TEXT NOT NULL, event_seq INTEGER NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY (call_id, event_seq), FOREIGN KEY (call_id) REFERENCES calls(call_id)
            );
            CREATE TABLE IF NOT EXISTS continuation_operations (
                caller_id TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY (caller_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS cancel_operations (
                caller_id TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY (caller_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS submission_operations (
                call_id TEXT NOT NULL, submission_id TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY (call_id, submission_id),
                FOREIGN KEY (call_id) REFERENCES calls(call_id)
            );
            CREATE TABLE IF NOT EXISTS attachments (
                attachment_id TEXT PRIMARY KEY, call_id TEXT NOT NULL,
                original_name TEXT NOT NULL, media_type TEXT NOT NULL,
                byte_count INTEGER NOT NULL, sha256 TEXT NOT NULL, storage_path TEXT NOT NULL,
                FOREIGN KEY (call_id) REFERENCES calls(call_id)
            );
            CREATE TABLE IF NOT EXISTS terminal_presence (
                device_id TEXT PRIMARY KEY, terminal_platform TEXT NOT NULL,
                route_device_id TEXT NOT NULL, callback_url TEXT, instance_id TEXT NOT NULL,
                gui_ready INTEGER NOT NULL, last_seen_at TEXT NOT NULL, online_expires_at TEXT NOT NULL
            );
            """
        )
        columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(calls)")
        }
        if "desktop_route_device_id" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN desktop_route_device_id TEXT")
        if "terminal_last_seen_at" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN terminal_last_seen_at TEXT")
        if "terminal_disconnect_deadline" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN terminal_disconnect_deadline TEXT")
        if "draft_body" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN draft_body TEXT")
        if "draft_version" not in columns:
            self._connection.execute(
                "ALTER TABLE calls ADD COLUMN draft_version INTEGER NOT NULL DEFAULT 0"
            )
        if "task_version" not in columns:
            self._connection.execute(
                "ALTER TABLE calls ADD COLUMN task_version INTEGER NOT NULL DEFAULT 0"
            )
        self._backfill_legacy_accepted_liveness()
        self._connection.commit()

    def _backfill_legacy_accepted_liveness(self) -> None:
        """Give pre-liveness accepted calls a deadline without extending them at restart."""
        rows = self._connection.execute(
            "SELECT call_id, created_at, terminal_last_seen_at, terminal_disconnect_deadline "
            "FROM calls WHERE state = 'accepted' "
            "AND (terminal_last_seen_at IS NULL OR terminal_disconnect_deadline IS NULL)"
        ).fetchall()
        for row in rows:
            # Older schemas did not persist accepted_at.  created_at is their
            # only durable liveness anchor, so it is deliberately used rather
            # than the current clock, which could revive an overdue call.
            last_seen = row["terminal_last_seen_at"] or row["created_at"]
            deadline = row["terminal_disconnect_deadline"]
            if deadline is None:
                anchor = self._parse_stamp(last_seen)
                deadline = self._stamp(
                    anchor + timedelta(seconds=self._terminal_disconnect_seconds)
                )
            self._connection.execute(
                "UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? "
                "WHERE call_id = ?",
                (last_seen, deadline, row["call_id"]),
            )

    def _write_transaction(self) -> "_Transaction":
        return _Transaction(self._lock, self._connection)

    def _now(self) -> datetime:
        return self._clock.now_utc()

    @staticmethod
    def _stamp(value: datetime) -> str:
        return value.isoformat()

    @staticmethod
    def _parse_stamp(value: str) -> datetime:
        return datetime.fromisoformat(value)

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _decode(value: str) -> dict[str, Any]:
        return json.loads(value)

    @staticmethod
    def _conflict() -> dict[str, Any]:
        return {"success": False, "status": "request_conflict", "needs_continuation": False}

    @staticmethod
    def _ownership_conflict() -> dict[str, Any]:
        return {"success": False, "status": "ownership_conflict", "needs_continuation": False}

    @staticmethod
    def _invalid_cursor() -> dict[str, Any]:
        return {"success": False, "status": "invalid_cursor", "needs_continuation": False}

    @staticmethod
    def _session_not_found() -> dict[str, Any]:
        return {"success": False, "status": "session_not_found", "needs_continuation": False}

    @staticmethod
    def _busy() -> dict[str, Any]:
        return CallCoordinator._envelope(
            status="busy", call_state=None, retryable=True, retry_after_seconds=30
        )

    @staticmethod
    def _envelope(
        *,
        status: str,
        call_state: str | None,
        interaction_id: str | None = None,
        call_id: str | None = None,
        reason: str | None = None,
        human_action: bool = False,
        needs_continuation: bool = False,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        handled_on: str | None = None,
        terminal_platform: str | None = None,
        event_seq: int | None = None,
        owner_device_id: str | None = None,
        owner_session_id: str | None = None,
        version: int | None = None,
        ring_deadline: str | None = None,
    ) -> dict[str, Any]:
        return {
            "success": True,
            "status": status,
            "call_state": call_state,
            "reason": reason,
            "human_action": human_action,
            "needs_continuation": needs_continuation,
            "interaction_id": interaction_id,
            "call_id": call_id,
            "event_seq": event_seq,
            "retryable": retryable,
            "retry_after_seconds": retry_after_seconds,
            "handled_on": handled_on,
            "terminal_platform": terminal_platform,
            "message": None,
            "owner_device_id": owner_device_id,
            "owner_session_id": owner_session_id,
            "version": version,
            "ring_deadline": ring_deadline,
        }


class _Transaction:
    def __init__(self, lock: threading.RLock, connection: sqlite3.Connection) -> None:
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._connection.execute("BEGIN IMMEDIATE")
        except BaseException:
            self._lock.release()
            raise
        return self._connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        try:
            if exc_type is None:
                self._connection.commit()
            else:
                self._connection.rollback()
        finally:
            self._lock.release()
