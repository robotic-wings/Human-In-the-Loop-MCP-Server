"""Persistent, transport-free coordination for one operator call at a time."""

from __future__ import annotations

import json
import hashlib
import base64
import hmac
import secrets
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol


_FEEDBACK_TYPES = {
    "task_too_big": "The task is too large - break it into smaller steps",
}


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
    bark_enabled: bool = False


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
                "AND state IN ('ringing', 'accepted', 'in_progress') LIMIT 1",
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
                if request.bark_enabled:
                    connection.execute(
                        "INSERT INTO bark_deliveries (call_id, state, attempt_count) "
                        "VALUES (?, 'pending', 0)",
                        (call_id,),
                    )
                if request.desktop_route_device_id is not None:
                    connection.execute(
                        "INSERT INTO terminal_callback_deliveries (call_id, state, attempt_count) "
                        "VALUES (?, 'pending', 0)",
                        (call_id,),
                    )
            connection.execute(
                "INSERT INTO create_operations (caller_id, request_id, payload, result) "
                "VALUES (?, ?, ?, ?)",
                (request.caller_id, request.request_id, payload, self._canonical(result)),
            )
            return result

    def revise_task(
        self, *, caller_id: str, interaction_id: str, prior_call_id: str,
        revision_id: str, message: str, ring_timeout_seconds: int,
        desktop_route_device_id: str | None, bark_enabled: bool,
    ) -> dict[str, Any]:
        """Atomically admit one explicitly requested revision of the latest final task round."""
        payload = self._canonical({
            "prior_call_id": prior_call_id, "message": message,
        })
        with self._write_transaction() as connection:
            replay = connection.execute(
                "SELECT payload, result FROM revision_operations "
                "WHERE caller_id = ? AND interaction_id = ? AND revision_id = ?",
                (caller_id, interaction_id, revision_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return {"success": False, "status": "revision_conflict", "needs_continuation": False}
            prior = self._owned_call(connection, caller_id, interaction_id, prior_call_id)
            if prior is None:
                return self._session_not_found()
            self._settle_if_expired(connection, prior)
            prior = self._call_or_raise(connection, prior_call_id)
            latest = connection.execute(
                "SELECT call_id FROM calls WHERE caller_id = ? AND interaction_id = ? "
                "ORDER BY rowid DESC LIMIT 1",
                (caller_id, interaction_id),
            ).fetchone()
            if (
                latest is None or latest["call_id"] != prior_call_id
                or prior["state"] not in {"completed", "failed"}
            ):
                return {"success": False, "status": "revision_not_allowed", "needs_continuation": False}
            active = connection.execute(
                "SELECT 1 FROM calls WHERE operator_id = ? AND state IN ('ringing', 'accepted', 'in_progress') LIMIT 1",
                (prior["operator_id"],),
            ).fetchone()
            if active is not None:
                return self._busy()
            call_id = str(uuid.uuid4())
            now = self._now()
            deadline = now + timedelta(seconds=ring_timeout_seconds)
            connection.execute(
                "INSERT INTO calls (call_id, operator_id, caller_id, interaction_id, message, state, "
                "created_at, ring_deadline, version, desktop_route_device_id) "
                "VALUES (?, ?, ?, ?, ?, 'ringing', ?, ?, 1, ?)",
                (call_id, prior["operator_id"], caller_id, interaction_id, message,
                 self._stamp(now), self._stamp(deadline), desktop_route_device_id),
            )
            result = self._envelope(
                status="ringing", call_state="ringing", interaction_id=interaction_id,
                call_id=call_id, retryable=False, event_seq=0,
            )
            result["task_id"] = interaction_id
            if bark_enabled:
                connection.execute(
                    "INSERT INTO bark_deliveries (call_id, state, attempt_count) VALUES (?, 'pending', 0)",
                    (call_id,),
                )
            if desktop_route_device_id is not None:
                connection.execute(
                    "INSERT INTO terminal_callback_deliveries (call_id, state, attempt_count) VALUES (?, 'pending', 0)",
                    (call_id,),
                )
            connection.execute(
                "INSERT INTO revision_operations (caller_id, interaction_id, revision_id, payload, result) "
                "VALUES (?, ?, ?, ?, ?)",
                (caller_id, interaction_id, revision_id, payload, self._canonical(result)),
            )
            return result

    def claim_next_bark_delivery(self) -> dict[str, str] | None:
        """Atomically claim one fresh phone delivery without changing call state."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT deliveries.call_id, calls.message FROM bark_deliveries AS deliveries "
                "JOIN calls ON calls.call_id = deliveries.call_id "
                "WHERE deliveries.state = 'pending' ORDER BY deliveries.rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            call = self._call_or_raise(connection, row["call_id"])
            self._settle_if_expired(connection, call)
            call = self._call_or_raise(connection, row["call_id"])
            if call["state"] != "ringing" or self._now() >= self._parse_stamp(call["ring_deadline"]):
                connection.execute(
                    "UPDATE bark_deliveries SET state = 'skipped' WHERE call_id = ? AND state = 'pending'",
                    (call["call_id"],),
                )
                return None
            claimed = connection.execute(
                "UPDATE bark_deliveries SET state = 'sending', attempt_count = attempt_count + 1 "
                "WHERE call_id = ? AND state = 'pending'",
                (call["call_id"],),
            ).rowcount
            if claimed != 1:
                return None
            return {"call_id": str(call["call_id"]), "message": str(call["message"])}

    def finish_bark_delivery(self, call_id: str, state: str) -> None:
        if state not in {"success", "failed", "timeout", "uncertain"}:
            raise ValueError("invalid Bark delivery state")
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE bark_deliveries SET state = ? WHERE call_id = ? AND state = 'sending'",
                (state, call_id),
            )

    def authorize_bark_send(self, call_id: str) -> bool:
        """Recheck the durable call immediately before an external Bark request."""
        with self._write_transaction() as connection:
            delivery = connection.execute(
                "SELECT state FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if delivery is None or delivery["state"] != "sending":
                return False
            call = self._call_or_raise(connection, call_id)
            self._settle_if_expired(connection, call)
            call = self._call_or_raise(connection, call_id)
            if call["state"] == "ringing" and self._now() < self._parse_stamp(call["ring_deadline"]):
                return True
            connection.execute(
                "UPDATE bark_deliveries SET state = 'suppressed' "
                "WHERE call_id = ? AND state = 'sending'",
                (call_id,),
            )
            return False

    def recover_bark_deliveries(self) -> None:
        """Never retry an external request whose prior outcome is unknown."""
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE bark_deliveries SET state = 'uncertain' WHERE state = 'sending'"
            )

    def claim_next_terminal_callback_delivery(self) -> dict[str, object] | None:
        """Claim one desktop callback using only the persisted managed target."""
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT deliveries.call_id, calls.desktop_route_device_id, calls.version, "
                "calls.ring_deadline, presence.callback_url "
                "FROM terminal_callback_deliveries AS deliveries "
                "JOIN calls ON calls.call_id = deliveries.call_id "
                "LEFT JOIN terminal_presence AS presence "
                "ON presence.device_id = calls.desktop_route_device_id "
                "WHERE deliveries.state = 'pending' ORDER BY deliveries.rowid LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            call = self._call_or_raise(connection, row["call_id"])
            self._settle_if_expired(connection, call)
            call = self._call_or_raise(connection, row["call_id"])
            if (
                call["state"] != "ringing"
                or self._now() >= self._parse_stamp(call["ring_deadline"])
                or not isinstance(row["callback_url"], str)
                or not row["callback_url"]
            ):
                connection.execute(
                    "UPDATE terminal_callback_deliveries SET state = 'suppressed' "
                    "WHERE call_id = ? AND state = 'pending'",
                    (call["call_id"],),
                )
                return None
            claimed = connection.execute(
                "UPDATE terminal_callback_deliveries SET state = 'sending', attempt_count = attempt_count + 1 "
                "WHERE call_id = ? AND state = 'pending'",
                (call["call_id"],),
            ).rowcount
            if claimed != 1:
                return None
            return {
                "call_id": str(call["call_id"]),
                "device_id": str(call["desktop_route_device_id"]),
                "callback_url": str(row["callback_url"]),
                "version": int(call["version"]),
                "ring_deadline": str(call["ring_deadline"]),
            }

    def authorize_terminal_callback_send(self, call_id: str) -> bool:
        """Recheck authoritative state immediately before callback I/O."""
        with self._write_transaction() as connection:
            delivery = connection.execute(
                "SELECT state FROM terminal_callback_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if delivery is None or delivery["state"] != "sending":
                return False
            call = self._call_or_raise(connection, call_id)
            self._settle_if_expired(connection, call)
            call = self._call_or_raise(connection, call_id)
            if call["state"] == "ringing" and self._now() < self._parse_stamp(call["ring_deadline"]):
                return True
            connection.execute(
                "UPDATE terminal_callback_deliveries SET state = 'suppressed' "
                "WHERE call_id = ? AND state = 'sending'",
                (call_id,),
            )
            return False

    def finish_terminal_callback_delivery(self, call_id: str, state: str) -> None:
        if state not in {"success", "failed", "timeout", "uncertain"}:
            raise ValueError("invalid terminal callback delivery state")
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE terminal_callback_deliveries SET state = ? "
                "WHERE call_id = ? AND state = 'sending'",
                (state, call_id),
            )

    def recover_terminal_callback_deliveries(self) -> None:
        """A callback interrupted during I/O is not safe to replay."""
        with self._write_transaction() as connection:
            connection.execute(
                "UPDATE terminal_callback_deliveries SET state = 'uncertain' WHERE state = 'sending'"
            )

    def accept_call(self, request: AcceptCall) -> dict[str, Any]:
        return self._act("accept", request.call_id, request.action_id, asdict(request))

    def decline_call(self, request: DeclineCall) -> dict[str, Any]:
        return self._act("decline", request.call_id, request.action_id, asdict(request))

    def decline_iphone_call(
        self, call_id: str, session_id: str, action_id: str
    ) -> dict[str, Any] | None:
        """Decline a persisted phone-routed ringing call from its browser session.

        Phone route authority is durable call state (the one-shot Bark intent),
        not merely the presence of a valid operator login.  Authorization is
        deliberately checked before replay lookup so a phone-excluded call
        cannot reveal or reserve an action identity.
        """
        scoped_action_id = f"iphone:{session_id}:{call_id}:{action_id}"
        action = asdict(DeclineCall(call_id, scoped_action_id, "iphone_web"))
        payload = self._canonical(action)
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            replay = self._action_replay(connection, scoped_action_id, payload)
            if replay is not None:
                return replay
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "ringing":
                if row["state"] in {"accepted", "declined"}:
                    return self._conflict()
                return self._current_result(connection, row)
            result = self._decline(connection, row, acted_on="iphone_web")
            connection.execute(
                "INSERT INTO action_operations (action_id, payload, result) VALUES (?, ?, ?)",
                (scoped_action_id, payload, self._canonical(result)),
            )
            return result

    def cancel_iphone_call(
        self, call_id: str, session_id: str, action_id: str, cancelled: object,
        feedback_requested: object = False,
    ) -> dict[str, Any] | None:
        """Cancel one phone-routed task from its accepted browser session."""
        scoped_action_id = f"iphone:{session_id}:{call_id}:{action_id}"
        action = asdict(CancelCall(call_id, scoped_action_id, "iphone_web", session_id))
        action["cancelled"] = cancelled
        action["feedback_requested"] = feedback_requested
        payload = self._canonical(action)
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None or (
                row["owner_device_id"] != "iphone_web"
                or row["owner_session_id"] != session_id
            ):
                return None
            replay = connection.execute(
                "SELECT payload, result FROM cancel_operations WHERE caller_id = ? AND request_id = ?",
                (f"iphone:{session_id}:{call_id}", action_id),
            ).fetchone()
            if replay is not None:
                return self._decode(replay["result"]) if replay["payload"] == payload else self._conflict()
            if cancelled is not True or not isinstance(feedback_requested, bool):
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] not in {"accepted", "in_progress"}:
                return self._current_result(connection, row)
            if not self._is_task_request(row["message"]):
                return {"success": False, "status": "invalid_submission"}
            result = self._cancel(connection, row, feedback_requested=feedback_requested)
            connection.execute(
                "INSERT INTO cancel_operations (caller_id, request_id, payload, result) VALUES (?, ?, ?, ?)",
                (f"iphone:{session_id}:{call_id}", action_id, payload, self._canonical(result)),
            )
            return result

    def heartbeat_iphone_call(self, call_id: str, session_id: str) -> dict[str, Any] | None:
        """Refresh only the accepted iPhone browser session's liveness."""
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] not in {"accepted", "in_progress"}:
                return self._current_result(connection, row)
            if (
                row["owner_device_id"] != "iphone_web"
                or row["owner_session_id"] != session_id
            ):
                return self._ownership_conflict()
            self._refresh_terminal_liveness(connection, call_id)
            return self._current_result(connection, self._call_or_raise(connection, call_id))

    def browser_writer(
        self, call_id: str, session_id: str, page_id: object, action_id: object,
        *, takeover: bool = False,
    ) -> dict[str, Any] | None:
        """Acquire or replace the one durable writable page for an iPhone call."""
        if not all(isinstance(value, str) and value for value in (session_id, page_id, action_id)):
            return {"success": False, "status": "invalid_writer"}
        with self._write_transaction() as connection:
            row = self._iphone_writer_call(connection, call_id, session_id)
            if row is None:
                return None
            lease = connection.execute(
                "SELECT page_id, writer_token FROM browser_writers WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if lease is not None and lease["page_id"] != page_id and not takeover:
                return {"success": True, "writable": False, "writer_token": None, "page_id": page_id}
            if lease is not None and lease["page_id"] == page_id and not takeover:
                return {"success": True, "writable": True, "writer_token": lease["writer_token"], "page_id": page_id}
            token = secrets.token_urlsafe(32)
            connection.execute(
                "INSERT INTO browser_writers (call_id, session_id, page_id, writer_token) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(call_id) DO UPDATE SET session_id = excluded.session_id, page_id = excluded.page_id, writer_token = excluded.writer_token",
                (call_id, session_id, page_id, token),
            )
            return {"success": True, "writable": True, "writer_token": token, "page_id": page_id}

    def browser_writer_status(
        self, call_id: str, session_id: str, page_id: object, writer_token: object,
    ) -> dict[str, Any] | None:
        """Return writable status only for the exact durable page authority."""
        if not all(isinstance(value, str) and value for value in (session_id, page_id, writer_token)):
            return {"success": False, "status": "invalid_writer"}
        with self._write_transaction() as connection:
            if self._iphone_writer_call(connection, call_id, session_id) is None:
                return None
            lease = connection.execute(
                "SELECT session_id, page_id, writer_token FROM browser_writers WHERE call_id = ?", (call_id,)
            ).fetchone()
            if lease is None or not (
                lease["session_id"] == session_id and lease["page_id"] == page_id
                and hmac.compare_digest(lease["writer_token"], writer_token)
            ):
                return {"success": False, "status": "writer_conflict"}
            return {"success": True, "writable": True, "writer_token": lease["writer_token"], "page_id": page_id}

    def browser_writer_matches(self, call_id: str, session_id: str, writer_token: object) -> bool:
        """Gate browser writes without preempting terminal replay/expiry settlement."""
        if not isinstance(writer_token, str) or not writer_token:
            return False
        with self._write_transaction() as connection:
            # A lease can only be created while the call is accepted and
            # phone-routed.  Once it exists, retain its authority long enough
            # for the underlying submission/cancel operation to replay a
            # durable terminal result or settle an expiry itself.
            lease = connection.execute(
                "SELECT session_id, writer_token FROM browser_writers "
                "WHERE call_id = ? AND session_id = ?",
                (call_id, session_id),
            ).fetchone()
            return bool(lease is not None and hmac.compare_digest(lease["writer_token"], writer_token))

    def _iphone_writer_call(
        self, connection: sqlite3.Connection, call_id: str, session_id: str,
    ) -> sqlite3.Row | None:
        """Settle first, then prove accepted phone-route ownership for a writer lease."""
        try:
            row = self._call_or_raise(connection, call_id)
        except KeyError:
            return None
        if connection.execute("SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)).fetchone() is None:
            return None
        self._settle_if_expired(connection, row)
        row = self._call_or_raise(connection, call_id)
        if (
            row["state"] not in {"accepted", "in_progress"}
            or row["owner_device_id"] != "iphone_web"
            or row["owner_session_id"] != session_id
        ):
            return None
        return row

    def create_operator_session(self, session_id: str, csrf: str, lifetime_seconds: int) -> None:
        expires_at = self._stamp(self._now() + timedelta(seconds=lifetime_seconds))
        with self._write_transaction() as connection:
            connection.execute(
                "INSERT INTO operator_sessions (session_id, csrf, expires_at) VALUES (?, ?, ?)",
                (session_id, csrf, expires_at),
            )

    def operator_session_csrf(self, session_id: str | None) -> str | None:
        if not isinstance(session_id, str):
            return None
        with self._write_transaction() as connection:
            row = connection.execute(
                "SELECT csrf, expires_at FROM operator_sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            if row is None:
                return None
            # Rows from before absolute browser-session expiry are deliberately
            # fail-closed: neither a cache nor a restart may grant authority.
            try:
                expired = row["expires_at"] is None or self._parse_stamp(row["expires_at"]) <= self._now()
            except (TypeError, ValueError):
                expired = True
            if expired:
                connection.execute("DELETE FROM operator_sessions WHERE session_id = ?", (session_id,))
                return None
            return row["csrf"]

    def delete_operator_session(self, session_id: str) -> bool:
        """Remove exactly one browser session without touching call state."""
        with self._write_transaction() as connection:
            deleted = connection.execute(
                "DELETE FROM operator_sessions WHERE session_id = ?", (session_id,)
            )
            return deleted.rowcount == 1

    def terminal_state(self, device_id: str, call_id: str | None = None) -> dict[str, Any] | None:
        with self._write_transaction() as connection:
            row = self._routed_terminal_call(connection, device_id, call_id)
            if row is None:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, row["call_id"])
            if row["state"] not in {"ringing", "accepted", "in_progress"}:
                return self._current_result(connection, row)
            return self._terminal_state_result(row)

    def operator_call_state(self, call_id: str) -> dict[str, Any] | None:
        """Return the authenticated operator's content-free call view."""
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            state = self._terminal_state_result(row)
            state["state"] = row["state"]
            return state

    def operator_call_page(self, call_id: str) -> dict[str, Any] | None:
        """Return the minimal authenticated browser display view for one call."""
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            state = self._terminal_state_result(row)
            state["state"] = row["state"]
            state["message"] = row["message"]
            return state

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

    def health_snapshot(
        self, *, desktop_route_device_id: str | None, bark_enabled: bool,
    ) -> dict[str, Any]:
        """Read aggregate operational state without mutating or disclosing configuration."""
        with self._lock:
            try:
                self._connection.execute("SELECT 1").fetchone()
                configured = desktop_route_device_id is not None
                online = False
                if desktop_route_device_id is not None:
                    row = self._connection.execute(
                        "SELECT gui_ready, online_expires_at FROM terminal_presence WHERE device_id = ?",
                        (desktop_route_device_id,),
                    ).fetchone()
                    online = (
                        row is not None and bool(row["gui_ready"])
                        and self._parse_stamp(row["online_expires_at"]) > self._now()
                    )
                desktop = {
                    "configured": configured,
                    "online": online,
                    "status": "online" if online else ("offline" if configured else "not_configured"),
                }
                if not bark_enabled:
                    bark = {"configured": False, "status": "not_configured",
                            "recent_delivery_status": "not_configured"}
                else:
                    row = self._connection.execute(
                        "SELECT state FROM bark_deliveries ORDER BY rowid DESC LIMIT 1"
                    ).fetchone()
                    recent = str(row["state"]) if row is not None else "not_delivered"
                    degraded = recent in {"failed", "timeout", "uncertain"}
                    bark = {"configured": True, "status": "degraded" if degraded else "healthy",
                            "recent_delivery_status": recent}
            except sqlite3.Error:
                return {
                    "success": True,
                    "status": "degraded",
                    "storage": {"status": "unavailable"},
                    "desktop_terminal": {
                        "configured": desktop_route_device_id is not None,
                        "online": False,
                        "status": "unavailable",
                    },
                    "bark": {
                        "configured": bark_enabled,
                        "status": "unavailable" if bark_enabled else "not_configured",
                        "recent_delivery_status": "unavailable" if bark_enabled else "not_configured",
                    },
                }
            return {
                "success": True,
                "status": "degraded" if bark["status"] == "degraded" else "healthy",
                "storage": {"status": "healthy"},
                "desktop_terminal": desktop,
                "bark": bark,
            }

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
            row = self._draft_terminal_call(connection, device_id, call_id)
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
            row = self._draft_terminal_call(connection, device_id, call_id)
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

    def _draft_terminal_call(
        self, connection: sqlite3.Connection, device_id: str, call_id: str,
    ) -> sqlite3.Row | None:
        """Apply the durable route authority for the two draft namespaces."""
        if device_id == "iphone_web":
            return connection.execute(
                "SELECT calls.* FROM calls JOIN bark_deliveries "
                "ON bark_deliveries.call_id = calls.call_id "
                "WHERE calls.call_id = ?",
                (call_id,),
            ).fetchone()
        return self._routed_terminal_call(connection, device_id, call_id)

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
        *,
        operator_owner: bool = False,
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
                    or len(set(attachment_ids)) != len(attachment_ids)
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
            if operator_owner:
                try:
                    row = self._call_or_raise(connection, call_id)
                except KeyError:
                    return None
            else:
                row = self._routed_terminal_call(
                    connection, device_id, call_id, legacy_route_device_id=legacy_route_device_id
                )
                if row is None:
                    return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return None
            if (
                self._is_confirmation_request(row["message"])
                or self._is_single_choice_request(row["message"])
                or self._multiple_choice_options(row["message"]) is not None
                or self._numeric_input_type(row["message"]) is not None
                or self._is_multiline_request(row["message"])
                or self._is_info_request(row["message"])
                or self._is_feedback_request(row["message"])
            ):
                return {"success": False, "status": "invalid_submission"}
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
                "result_id": str(uuid.uuid4()),
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

    def numeric_input_type_for_call(self, call_id: str) -> str | None:
        """Return the stored numeric input kind without changing call state."""
        with self._lock:
            row = self._connection.execute(
                "SELECT message FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return self._numeric_input_type(row["message"]) if row is not None else None

    def multiple_choice_options_for_call(self, call_id: str) -> list[str] | None:
        """Return stored multi-choice options without changing call state."""
        with self._lock:
            row = self._connection.execute(
                "SELECT message FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return self._multiple_choice_options(row["message"]) if row is not None else None

    def multiline_default_for_call(self, call_id: str) -> str | None:
        """Return the stored multiline default without changing call state."""
        with self._lock:
            row = self._connection.execute(
                "SELECT message FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
            return self._multiline_default(row["message"]) if row is not None else None

    def submit_terminal_confirmation(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        submission_id: str,
        confirmed: bool,
    ) -> dict[str, Any] | None:
        """Persist one typed confirmation answer from its accepted iPhone owner."""
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or len(submission_id) > 128
            or not submission_id.isascii()
            or not isinstance(confirmed, bool)
        ):
            return {"success": False, "status": "invalid_submission"}
        payload = self._canonical({"confirmed": confirmed})
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return None
            if not self._is_confirmation_request(row["message"]):
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations "
                "WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()
            if row["state"] != "accepted":
                return self._current_result(connection, row)
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
                "confirmed": confirmed,
                "result_id": str(uuid.uuid4()),
            }
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

    def submit_terminal_info_acknowledgement(
        self, call_id: str, device_id: str, owner_session_id: str,
        submission_id: str, acknowledged: object,
    ) -> dict[str, Any] | None:
        """Persist the sole affirmative acknowledgement for an information call."""
        if (
            not isinstance(submission_id, str) or not submission_id or len(submission_id) > 128
            or not submission_id.isascii() or not isinstance(acknowledged, bool)
        ):
            return {"success": False, "status": "invalid_submission"}
        payload = self._canonical({"acknowledged": acknowledged})
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            if not self._is_info_request(row["message"]):
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                return self._decode(replay["result"]) if replay["payload"] == payload else self._conflict()
            if acknowledged is not True:
                return {"success": False, "status": "invalid_submission"}
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            result = {
                "success": True, "status": "answered", "call_state": "answered",
                "reason": None, "human_action": True, "needs_continuation": False,
                "interaction_id": row["interaction_id"], "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"], "terminal_platform": row["terminal_platform"],
                "acknowledged": True, "result_id": str(uuid.uuid4()),
            }
            connection.execute("UPDATE calls SET state = 'answered', version = version + 1 WHERE call_id = ?", (call_id,))
            self._add_event(connection, call_id, result)
            connection.execute(
                "INSERT INTO submission_operations (call_id, submission_id, payload, result) VALUES (?, ?, ?, ?)",
                (call_id, submission_id, payload, self._canonical(result)),
            )
            return result

    def submit_terminal_single_choice(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        submission_id: str,
        selected_choices: object,
    ) -> dict[str, Any] | None:
        """Persist exactly one saved choice from its accepted iPhone owner."""
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or len(submission_id) > 128
            or not submission_id.isascii()
            or not isinstance(selected_choices, list)
            or len(selected_choices) != 1
            or not isinstance(selected_choices[0], str)
        ):
            return {"success": False, "status": "invalid_submission"}
        selected = selected_choices[0]
        payload = self._canonical({"selected_choices": [selected]})
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                return None
            choices = self._single_choice_options(row["message"])
            if choices is None or selected not in choices:
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations "
                "WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()
            if row["state"] != "accepted":
                return self._current_result(connection, row)
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
                "selected_choices": [selected],
                "result_id": str(uuid.uuid4()),
            }
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

    def submit_terminal_multiple_choice(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        submission_id: str,
        selected_choices: object,
    ) -> dict[str, Any] | None:
        """Persist an ordered multi-choice answer from its accepted iPhone owner."""
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or len(submission_id) > 128
            or not submission_id.isascii()
            or not isinstance(selected_choices, list)
            or not selected_choices
            or any(not isinstance(choice, str) for choice in selected_choices)
            or len(set(selected_choices)) != len(selected_choices)
        ):
            return {"success": False, "status": "invalid_submission"}
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            choices = self._multiple_choice_options(row["message"])
            if choices is None or any(choice not in choices for choice in selected_choices):
                return {"success": False, "status": "invalid_submission"}
            canonical_choices = [choice for choice in choices if choice in selected_choices]
            payload = self._canonical({"selected_choices": canonical_choices})
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations "
                "WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            result = {
                "success": True, "status": "answered", "call_state": "answered",
                "reason": None, "human_action": True, "needs_continuation": False,
                "interaction_id": row["interaction_id"], "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"], "terminal_platform": row["terminal_platform"],
                "selected_choices": canonical_choices, "result_id": str(uuid.uuid4()),
            }
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

    def submit_terminal_multiline_input(
        self, call_id: str, device_id: str, owner_session_id: str,
        submission_id: str, user_input: object,
    ) -> dict[str, Any] | None:
        """Persist trimmed multiline text from its accepted iPhone owner."""
        if (
            not isinstance(submission_id, str) or not submission_id or len(submission_id) > 128
            or not submission_id.isascii() or not isinstance(user_input, str)
        ):
            return {"success": False, "status": "invalid_submission"}
        normalized = user_input.strip()
        if not normalized:
            return {"success": False, "status": "invalid_submission"}
        payload = self._canonical({"user_input": normalized})
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            if self._multiline_default(row["message"]) is None:
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                return self._decode(replay["result"]) if replay["payload"] == payload else self._conflict()
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            result = {
                "success": True, "status": "answered", "call_state": "answered",
                "reason": None, "human_action": True, "needs_continuation": False,
                "interaction_id": row["interaction_id"], "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"], "terminal_platform": row["terminal_platform"],
                "user_input": normalized, "character_count": len(normalized),
                "line_count": len(normalized.splitlines()), "result_id": str(uuid.uuid4()),
            }
            connection.execute("UPDATE calls SET state = 'answered', version = version + 1 WHERE call_id = ?", (call_id,))
            self._add_event(connection, call_id, result)
            connection.execute(
                "INSERT INTO submission_operations (call_id, submission_id, payload, result) VALUES (?, ?, ?, ?)",
                (call_id, submission_id, payload, self._canonical(result)),
            )
            return result

    def submit_terminal_feedback(
        self, call_id: str, device_id: str, owner_session_id: str,
        submission_id: str, feedback_type: object, feedback_text: object,
    ) -> dict[str, Any] | None:
        """Persist canonical standalone feedback from its accepted iPhone owner."""
        if (
            not isinstance(submission_id, str) or not submission_id or len(submission_id) > 128
            or not submission_id.isascii() or not isinstance(feedback_type, str)
            or not isinstance(feedback_text, str)
        ):
            return {"success": False, "status": "invalid_submission"}
        normalized_text = feedback_text.strip()
        if not normalized_text:
            return {"success": False, "status": "invalid_submission"}
        payload = self._canonical({"feedback_type": feedback_type, "feedback_text": normalized_text})
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            if not self._is_feedback_request(row["message"]):
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                return self._decode(replay["result"]) if replay["payload"] == payload else self._conflict()
            if feedback_type not in _FEEDBACK_TYPES:
                return {"success": False, "status": "invalid_submission"}
            if row["state"] != "accepted":
                return self._current_result(connection, row)
            result = {
                "success": True, "status": "answered", "call_state": "answered",
                "reason": None, "human_action": True, "needs_continuation": False,
                "interaction_id": row["interaction_id"], "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"], "terminal_platform": row["terminal_platform"],
                "feedback_type": feedback_type, "feedback_type_label": _FEEDBACK_TYPES[feedback_type],
                "feedback_text": normalized_text, "result_id": str(uuid.uuid4()),
            }
            connection.execute("UPDATE calls SET state = 'answered', version = version + 1 WHERE call_id = ?", (call_id,))
            self._add_event(connection, call_id, result)
            connection.execute(
                "INSERT INTO submission_operations (call_id, submission_id, payload, result) VALUES (?, ?, ?, ?)",
                (call_id, submission_id, payload, self._canonical(result)),
            )
            return result

    def submit_terminal_task_final(
        self, call_id: str, device_id: str, owner_session_id: str,
        submission_id: str, status: object, report: object, task_version: object = 0,
        feedback_requested: object = False, attachment_ids: object = None,
    ) -> dict[str, Any] | None:
        """Persist one completed or failed terminal report from its task owner."""
        if (
            not isinstance(submission_id, str) or not submission_id or len(submission_id) > 128
            or not submission_id.isascii() or not isinstance(status, str)
            or status not in {"completed", "failed"}
            or not isinstance(report, str)
            or not isinstance(task_version, int)
            or isinstance(task_version, bool)
            or task_version < 0
            or not isinstance(feedback_requested, bool)
            or (
                attachment_ids is not None
                and (
                    not isinstance(attachment_ids, list)
                    or any(not isinstance(attachment_id, str) or not attachment_id for attachment_id in attachment_ids)
                    or len(set(attachment_ids)) != len(attachment_ids)
                )
            )
        ):
            return {"success": False, "status": "invalid_submission"}
        normalized_report = report.strip()
        if not normalized_report:
            return {"success": False, "status": "invalid_submission"}
        has_attachment_ids = attachment_ids is not None
        attachment_ids = [] if attachment_ids is None else attachment_ids
        payload = self._canonical({
            "status": status, "report": normalized_report, "task_version": task_version,
            "feedback_requested": feedback_requested, "attachment_ids": attachment_ids,
        })
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            if not self._is_task_request(row["message"]):
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                return self._decode(replay["result"]) if replay["payload"] == payload else self._conflict()
            if row["state"] not in {"accepted", "in_progress"}:
                newer = connection.execute(
                    "SELECT 1 FROM calls WHERE interaction_id = ? AND rowid > "
                    "(SELECT rowid FROM calls WHERE call_id = ?) LIMIT 1",
                    (row["interaction_id"], call_id),
                ).fetchone()
                if newer is not None:
                    return {"success": False, "status": "superseded", "needs_continuation": False}
                return self._current_result(connection, row)
            if row["task_version"] != task_version:
                return {
                    "success": False, "status": "task_version_conflict",
                    "call_state": row["state"], "task_version": row["task_version"],
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
                "success": True, "status": status, "call_state": status,
                "reason": None, "human_action": True, "needs_continuation": False,
                "interaction_id": row["interaction_id"], "task_id": row["interaction_id"],
                "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"], "terminal_platform": row["terminal_platform"],
                "report": normalized_report, "resubmittable": True, "result_id": str(uuid.uuid4()),
            }
            if feedback_requested:
                result["feedback_requested"] = True
            if has_attachment_ids:
                result["attachment_ids"] = attachment_ids
            connection.execute("UPDATE calls SET state = ?, version = version + 1 WHERE call_id = ?", (status, call_id))
            self._add_event(connection, call_id, result)
            connection.execute(
                "INSERT INTO submission_operations (call_id, submission_id, payload, result) VALUES (?, ?, ?, ?)",
                (call_id, submission_id, payload, self._canonical(result)),
            )
            return result

    def update_active_task(
        self,
        caller_id: str,
        request_id: str,
        interaction_id: object,
        call_id: object,
        after_event_seq: object,
        expected_task_version: object,
        task_description: object,
        context_note: object,
    ) -> dict[str, Any]:
        """Atomically revise acknowledged active task instructions for its caller."""
        if (
            not all(isinstance(value, str) and value for value in (caller_id, request_id, interaction_id, call_id))
            or len(request_id) > 128 or not request_id.isascii()
            or not isinstance(after_event_seq, int) or isinstance(after_event_seq, bool) or after_event_seq < 0
            or not isinstance(expected_task_version, int) or isinstance(expected_task_version, bool) or expected_task_version < 0
            or (task_description is not None and not isinstance(task_description, str))
            or (context_note is not None and not isinstance(context_note, str))
            or (task_description is None and context_note is None)
        ):
            return {"success": False, "status": "invalid_request", "needs_continuation": False}
        operation_payload = self._canonical({
            "kind": "task_update", "interaction_id": interaction_id, "call_id": call_id,
            "after_event_seq": after_event_seq, "expected_task_version": expected_task_version,
            "task_description": task_description, "context_note": context_note,
        })
        with self._write_transaction() as connection:
            row = self._owned_call(connection, caller_id, interaction_id, call_id)
            if row is None:
                return self._session_not_found()
            replay = self._continuation_replay(connection, caller_id, request_id, operation_payload)
            if replay is not None:
                return replay
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            if row["state"] != "in_progress":
                if row["state"] in {"completed", "failed", "answered", "cancelled", "declined", "not_accepted"}:
                    return {
                        "success": False, "status": "invalid_update", "call_state": row["state"],
                        "task_version": row["task_version"], "needs_continuation": False,
                    }
                return self._current_result(connection, row)
            if row["agent_progress_event_seq"] != after_event_seq:
                return {"success": False, "status": "invalid_update", "call_state": row["state"], "needs_continuation": False}
            if row["task_version"] != expected_task_version:
                return {
                    "success": False, "status": "task_version_conflict", "call_state": row["state"],
                    "task_version": row["task_version"], "needs_continuation": False,
                }
            updated_message = self._task_message_with_update(row["message"], task_description, context_note)
            if updated_message is None:
                return {"success": False, "status": "invalid_update", "needs_continuation": False}
            connection.execute(
                "UPDATE calls SET message = ?, task_version = task_version + 1, version = version + 1 WHERE call_id = ?",
                (updated_message, call_id),
            )
            result = self._envelope(
                status="updated", call_state="in_progress", interaction_id=interaction_id, call_id=call_id,
                needs_continuation=True, handled_on=row["handled_on"], terminal_platform=row["terminal_platform"],
                event_seq=self._event_seq(connection, call_id), owner_device_id=row["owner_device_id"],
                owner_session_id=row["owner_session_id"], version=row["version"] + 1,
            )
            result["task_id"] = interaction_id
            result["task_version"] = expected_task_version + 1
            self._store_continuation_snapshot(connection, caller_id, request_id, operation_payload, result)
            return result

    def submit_terminal_task_progress(
        self, call_id: str, device_id: str, owner_session_id: str,
        submission_id: str, report: object,
    ) -> dict[str, Any] | None:
        """Persist one nonterminal task progress event from its accepted owner."""
        if (
            not isinstance(submission_id, str) or not submission_id or len(submission_id) > 128
            or not submission_id.isascii() or not isinstance(report, str)
        ):
            return {"success": False, "status": "invalid_submission"}
        normalized_report = report.strip()
        if not normalized_report:
            return {"success": False, "status": "invalid_submission"}
        payload = self._canonical({"status": "in_progress", "report": normalized_report})
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            if not self._is_task_request(row["message"]):
                return {"success": False, "status": "invalid_submission"}
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                return self._decode(replay["result"]) if replay["payload"] == payload else self._conflict()
            if row["state"] not in {"accepted", "in_progress"}:
                return self._current_result(connection, row)
            result = {
                "success": True, "status": "in_progress", "call_state": "in_progress",
                "reason": None, "human_action": True, "needs_continuation": True,
                "interaction_id": row["interaction_id"], "task_id": row["interaction_id"],
                "call_id": row["call_id"],
                "event_seq": (self._event_seq(connection, call_id) or 0) + 1,
                "handled_on": row["handled_on"], "terminal_platform": row["terminal_platform"],
                "report": normalized_report,
                "result_id": str(uuid.uuid4()),
            }
            connection.execute(
                "UPDATE calls SET state = 'in_progress', version = version + 1 WHERE call_id = ?",
                (call_id,),
            )
            self._add_event(connection, call_id, result)
            connection.execute(
                "INSERT INTO submission_operations (call_id, submission_id, payload, result) VALUES (?, ?, ?, ?)",
                (call_id, submission_id, payload, self._canonical(result)),
            )
            return result

    def submit_terminal_numeric_input(
        self,
        call_id: str,
        device_id: str,
        owner_session_id: str,
        submission_id: str,
        user_input: object,
    ) -> dict[str, Any] | None:
        """Persist a normalized integer or float from its accepted iPhone owner."""
        if (
            not isinstance(submission_id, str)
            or not submission_id
            or len(submission_id) > 128
            or not submission_id.isascii()
            or not isinstance(user_input, str)
        ):
            return {"success": False, "status": "invalid_submission"}
        with self._write_transaction() as connection:
            try:
                row = self._call_or_raise(connection, call_id)
            except KeyError:
                return None
            routed = connection.execute(
                "SELECT 1 FROM bark_deliveries WHERE call_id = ?", (call_id,)
            ).fetchone()
            if routed is None:
                return None
            input_type = self._numeric_input_type(row["message"])
            typed_input = self._parse_numeric_input(input_type, user_input)
            if typed_input is None:
                return {"success": False, "status": "invalid_submission"}
            if row["owner_device_id"] != device_id or row["owner_session_id"] != owner_session_id:
                if self._is_terminal_disconnect_owner(connection, row, device_id, owner_session_id):
                    return self._current_result(connection, row)
                return None
            payload = self._canonical({"input_type": input_type, "user_input": typed_input})
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            replay = connection.execute(
                "SELECT payload, result FROM submission_operations "
                "WHERE call_id = ? AND submission_id = ?",
                (call_id, submission_id),
            ).fetchone()
            if replay is not None:
                if replay["payload"] == payload:
                    return self._decode(replay["result"])
                return self._conflict()
            if row["state"] != "accepted":
                return self._current_result(connection, row)
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
                "input_type": input_type,
                "user_input": typed_input,
                "result_id": str(uuid.uuid4()),
            }
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

    def read_interaction_result(
        self, caller_id: str, interaction_id: object, result_id: object,
        cursor: object, max_result_bytes: object,
    ) -> dict[str, Any]:
        """Read an immutable final body page without changing durable state."""
        if not self._valid_read_request(interaction_id, result_id, cursor, max_result_bytes):
            return self._invalid_read_request(max_result_bytes)
        assert isinstance(interaction_id, str) and isinstance(result_id, str)
        assert isinstance(cursor, int) and isinstance(max_result_bytes, int)
        with self._lock:
            row = self._connection.execute(
                "SELECT call_id FROM calls WHERE interaction_id = ? AND caller_id = ?",
                (interaction_id, caller_id),
            ).fetchone()
            if row is None:
                return self._not_found_read()
            result = self._result_for_id(row["call_id"], result_id)
            if result is None:
                return self._not_found_read()
            body = result.get("user_input")
            if not isinstance(body, str):
                body = result.get("feedback_text")
            if not isinstance(body, str):
                body = result.get("report")
            attachments = self._result_attachments(row["call_id"], result)
            if isinstance(body, str):
                if cursor > len(body):
                    return self._not_found_read()
                page = self._body_page(result_id, body, cursor, max_result_bytes, attachments)
            else:
                choices = result.get("selected_choices")
                if (
                    not isinstance(choices, list)
                    or any(not isinstance(choice, str) for choice in choices)
                    or cursor > len(choices)
                ):
                    return self._not_found_read()
                page = self._choice_page(result_id, choices, cursor, max_result_bytes, attachments)
            return page if page is not None else self._not_found_read()

    def read_interaction_attachment(
        self, caller_id: str, interaction_id: object, attachment_id: object,
        offset_bytes: object, max_result_bytes: object,
    ) -> dict[str, Any]:
        """Read archive bytes by raw-byte offset without touching call state."""
        if not self._valid_read_request(interaction_id, attachment_id, offset_bytes, max_result_bytes):
            return self._invalid_read_request(max_result_bytes)
        assert isinstance(interaction_id, str) and isinstance(attachment_id, str)
        assert isinstance(offset_bytes, int) and isinstance(max_result_bytes, int)
        with self._lock:
            item = self._connection.execute(
                "SELECT a.* FROM attachments a JOIN calls c ON c.call_id = a.call_id "
                "WHERE c.interaction_id = ? AND c.caller_id = ? AND a.attachment_id = ?",
                (interaction_id, caller_id, attachment_id),
            ).fetchone()
            if item is None or offset_bytes > item["byte_count"]:
                return self._not_found_read()
            try:
                content = Path(item["storage_path"]).read_bytes()
            except OSError:
                return self._not_found_read()
            if len(content) != item["byte_count"]:
                return self._not_found_read()
            return self._attachment_page(item, content, offset_bytes, max_result_bytes)

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

            return self._archive_accepted_attachment(
                connection, call_id, original_name, media_type, content
            )

    def upload_iphone_attachment(
        self, call_id: str, owner_session_id: str, original_name: str,
        media_type: str, content: bytes,
    ) -> dict[str, Any] | None:
        """Archive one accepted phone-owner file without creating a result."""
        if (
            not isinstance(original_name, str) or not original_name
            or not isinstance(media_type, str) or not media_type
            or not isinstance(content, bytes)
        ):
            return {"success": False, "status": "invalid_attachment"}
        with self._write_transaction() as connection:
            row = self._iphone_writer_call(connection, call_id, owner_session_id)
            if row is None:
                return None
            return self._archive_accepted_attachment(
                connection, call_id, original_name, media_type, content
            )

    def read_iphone_pending_attachments(
        self, call_id: str, owner_session_id: str,
    ) -> list[dict[str, Any]] | None:
        """Read this accepted iPhone owner's archive metadata without liveness effects."""
        with self._lock:
            row = self._connection.execute(
                "SELECT state, owner_device_id, owner_session_id FROM calls WHERE call_id = ?",
                (call_id,),
            ).fetchone()
            if row is None or not (
                row["state"] in {"accepted", "in_progress"}
                and row["owner_device_id"] == "iphone_web"
                and row["owner_session_id"] == owner_session_id
            ):
                return None
            rows = self._connection.execute(
                "SELECT attachment_id, original_name, media_type, byte_count, sha256 "
                "FROM attachments WHERE call_id = ? ORDER BY rowid",
                (call_id,),
            ).fetchall()
            return [{
                "attachment_id": item["attachment_id"], "name": item["original_name"],
                "media_type": item["media_type"], "byte_count": item["byte_count"],
                "sha256": item["sha256"],
            } for item in rows]

    def _archive_accepted_attachment(
        self, connection: sqlite3.Connection, call_id: str, original_name: str,
        media_type: str, content: bytes,
    ) -> dict[str, Any]:
        """Apply shared archive quotas and persist a single accepted-owner file."""
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
            (attachment_id, call_id, original_name, media_type, byte_count, digest, str(storage_path)),
        )
        self._refresh_terminal_liveness(connection, call_id)
        return {
            "success": True, "status": "uploaded",
            "attachment": {
                "attachment_id": attachment_id, "name": original_name,
                "media_type": media_type, "byte_count": byte_count, "sha256": digest,
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

    def reject_iphone_attachment_submission_limit(
        self, call_id: str, owner_session_id: str,
    ) -> dict[str, Any] | None:
        with self._write_transaction() as connection:
            if self._iphone_writer_call(connection, call_id, owner_session_id) is None:
                return None
            return self._attachment_rejected("submission_limit_exceeded")

    @staticmethod
    def _valid_read_request(
        interaction_id: object, item_id: object, cursor: object, max_result_bytes: object
    ) -> bool:
        return (
            isinstance(interaction_id, str) and bool(interaction_id)
            and isinstance(item_id, str) and bool(item_id)
            and isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= 0
            and isinstance(max_result_bytes, int) and not isinstance(max_result_bytes, bool)
            and max_result_bytes >= 16_384
        )

    @staticmethod
    def _invalid_read_request(max_result_bytes: object) -> dict[str, Any]:
        if (
            not isinstance(max_result_bytes, int)
            or isinstance(max_result_bytes, bool)
            or max_result_bytes < 16_384
        ):
            return {
                "success": False,
                "status": "invalid_result_budget",
                "needs_continuation": False,
            }
        return {"success": False, "status": "session_not_found", "needs_continuation": False}

    @staticmethod
    def _not_found_read() -> dict[str, Any]:
        return {"success": False, "status": "session_not_found", "needs_continuation": False}

    def _result_for_id(self, call_id: str, result_id: str) -> dict[str, Any] | None:
        archived = self._connection.execute(
            "SELECT result FROM results WHERE call_id = ? AND result_id = ?",
            (call_id, result_id),
        ).fetchone()
        if archived is not None:
            return self._decode(archived["result"])
        rows = self._connection.execute(
            "SELECT result FROM submission_operations WHERE call_id = ?", (call_id,)
        ).fetchall()
        for row in rows:
            result = self._decode(row["result"])
            if result.get("result_id") == result_id:
                return result
        return None

    def _result_attachments(self, call_id: str, result: dict[str, Any]) -> list[dict[str, Any]]:
        attachment_ids = result.get("attachment_ids")
        if not isinstance(attachment_ids, list):
            return []
        metadata: list[dict[str, Any]] = []
        for attachment_id in attachment_ids:
            if not isinstance(attachment_id, str):
                continue
            row = self._connection.execute(
                "SELECT attachment_id, original_name, media_type, byte_count, sha256 "
                "FROM attachments WHERE call_id = ? AND attachment_id = ?",
                (call_id, attachment_id),
            ).fetchone()
            if row is not None:
                metadata.append({
                    "attachment_id": row["attachment_id"], "name": row["original_name"],
                    "media_type": row["media_type"], "byte_count": row["byte_count"],
                    "sha256": row["sha256"],
                })
        return metadata

    def _body_page(
        self, result_id: str, body: str, cursor: int, budget: int,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        def page(end: int) -> dict[str, Any]:
            complete = end == len(body)
            return {
                "success": True, "result_id": result_id, "body": body[cursor:end],
                "full_length": len(body), "complete": complete,
                "next_cursor": None if complete else end, "attachments": attachments,
            }
        return self._largest_budgeted_page(cursor, len(body), budget, page)

    def _choice_page(
        self, result_id: str, choices: list[str], cursor: int, budget: int,
        attachments: list[dict[str, Any]],
    ) -> dict[str, Any] | None:
        """Page canonical ordered selections without splitting Unicode text."""
        def page(end: int) -> dict[str, Any]:
            complete = end == len(choices)
            return {
                "success": True, "result_id": result_id, "body": choices[cursor:end],
                "full_length": len(choices), "complete": complete,
                "next_cursor": None if complete else end, "attachments": attachments,
            }
        return self._largest_budgeted_page(cursor, len(choices), budget, page)

    def _attachment_page(
        self, item: sqlite3.Row, content: bytes, offset: int, budget: int
    ) -> dict[str, Any]:
        def page(end: int) -> dict[str, Any]:
            complete = end == len(content)
            return {
                "success": True, "attachment_id": item["attachment_id"],
                "data_base64": base64.b64encode(content[offset:end]).decode("ascii"),
                "full_length": len(content), "sha256": item["sha256"], "complete": complete,
                "next_offset_bytes": None if complete else end,
            }
        result = self._largest_budgeted_page(offset, len(content), budget, page)
        return result if result is not None else self._not_found_read()

    def _largest_budgeted_page(
        self, start: int, length: int, budget: int, build: Any
    ) -> dict[str, Any] | None:
        if len(self._canonical(build(start)).encode("utf-8")) > budget:
            return None
        low, high = start, length
        while low < high:
            middle = (low + high + 1) // 2
            if len(self._canonical(build(middle)).encode("utf-8")) <= budget:
                low = middle
            else:
                high = middle - 1
        return build(low)

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
                "AND state IN ('ringing', 'accepted', 'in_progress') ORDER BY created_at LIMIT 1",
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
            "task_version": row["task_version"],
        }
        if row["owner_device_id"] is not None:
            result["owner_device_id"] = row["owner_device_id"]
            result["owner_session_id"] = row["owner_session_id"]
        result["handled_on"] = row["handled_on"]
        result["terminal_platform"] = row["terminal_platform"]
        return result

    @staticmethod
    def _is_confirmation_request(message: object) -> bool:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return False
        return isinstance(request, dict) and request.get("tool") == "show_confirmation_dialog"

    @staticmethod
    def _is_info_request(message: object) -> bool:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return False
        return isinstance(request, dict) and request.get("tool") == "show_info_message"

    @staticmethod
    def _is_feedback_request(message: object) -> bool:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return False
        return isinstance(request, dict) and request.get("tool") == "get_feedback_from_user"

    @staticmethod
    def _is_task_request(message: object) -> bool:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return False
        return isinstance(request, dict) and request.get("tool") == "assign_task_to_human"

    @staticmethod
    def _task_message_with_update(
        message: object, task_description: str | None, context_note: str | None
    ) -> str | None:
        """Return the canonical task request with only its mutable instructions revised."""
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return None
        if not isinstance(request, dict) or request.get("tool") != "assign_task_to_human":
            return None
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            return None
        arguments = dict(arguments)
        if task_description is not None:
            arguments["task_description"] = task_description
        if context_note is not None:
            arguments["context_note"] = context_note
        request = dict(request)
        request["arguments"] = arguments
        return CallCoordinator._canonical(request)

    @staticmethod
    def _single_choice_options(message: object) -> list[str] | None:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return None
        if not isinstance(request, dict) or request.get("tool") != "get_user_choice":
            return None
        arguments = request.get("arguments")
        if not isinstance(arguments, dict) or arguments.get("allow_multiple") is not False:
            return None
        choices = arguments.get("choices")
        if not isinstance(choices, list) or not choices or any(not isinstance(choice, str) for choice in choices):
            return None
        return choices

    @staticmethod
    def _multiple_choice_options(message: object) -> list[str] | None:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return None
        if not isinstance(request, dict) or request.get("tool") != "get_user_choice":
            return None
        arguments = request.get("arguments")
        if not isinstance(arguments, dict) or arguments.get("allow_multiple") is not True:
            return None
        choices = arguments.get("choices")
        if not isinstance(choices, list) or not choices or any(not isinstance(choice, str) for choice in choices):
            return None
        return choices

    @staticmethod
    def _multiline_default(message: object) -> str | None:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return None
        if not isinstance(request, dict) or request.get("tool") != "get_multiline_input":
            return None
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            return None
        default_value = arguments.get("default_value", "")
        return default_value if isinstance(default_value, str) else None

    @classmethod
    def _is_multiline_request(cls, message: object) -> bool:
        return cls._multiline_default(message) is not None

    @classmethod
    def _is_single_choice_request(cls, message: object) -> bool:
        return cls._single_choice_options(message) is not None

    @staticmethod
    def _numeric_input_type(message: object) -> str | None:
        try:
            request = json.loads(message) if isinstance(message, str) else None
        except json.JSONDecodeError:
            return None
        if not isinstance(request, dict) or request.get("tool") != "get_user_input":
            return None
        arguments = request.get("arguments")
        if not isinstance(arguments, dict):
            return None
        input_type = arguments.get("input_type")
        return input_type if input_type in {"integer", "float"} else None

    @staticmethod
    def _parse_numeric_input(input_type: str | None, user_input: str) -> int | float | None:
        value = user_input.strip()
        if not value:
            return None
        try:
            if input_type == "integer":
                return int(value)
            if input_type == "float":
                return float(value)
        except ValueError:
            return None
        return None

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
            if row["state"] not in {"accepted", "in_progress"}:
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
        client_timeout_seconds: int | None = None,
    ) -> dict[str, Any]:
        """Read one durable event, or save a nonblocking heartbeat snapshot.

        A continuation is intentionally not a wait.  Its operation record makes
        a lost HTTP response replayable without advancing either cursor or the
        call's absolute ringing deadline.
        """
        if client_timeout_seconds is not None:
            result, waiting = self.begin_wait_leg(
                caller_id, request_id, interaction_id, call_id, after_event_seq,
                payload, client_timeout_seconds,
            )
            return result
        operation_payload = self._continuation_payload(
            interaction_id, call_id, after_event_seq, payload
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
            if result["status"] == "opened" or (
                result["status"] == "heartbeat" and result.get("call_state") != "in_progress"
            ):
                self._start_agent_reconnect_grace(connection, call_id)
            elif result["status"] == "in_progress":
                self._start_progress_agent_reconnect_grace(connection, call_id, result)
            self._store_continuation_snapshot(
                connection, caller_id, request_id, operation_payload, result
            )
            return result

    def begin_wait_leg(
        self,
        caller_id: str,
        request_id: str,
        interaction_id: str,
        call_id: str,
        after_event_seq: int,
        payload: str,
        client_timeout_seconds: object,
    ) -> tuple[dict[str, Any], bool]:
        """Claim one bounded wait leg, returning a response only when it is ready."""
        leg_seconds = self._safe_leg_seconds(client_timeout_seconds)
        if leg_seconds is None:
            return self._invalid_client_timeout(), False
        operation_payload = self._continuation_payload(
            interaction_id, call_id, after_event_seq, payload
        )
        with self._write_transaction() as connection:
            row = self._owned_call(connection, caller_id, interaction_id, call_id)
            if row is None:
                return self._session_not_found(), False
            replay = self._continuation_replay(
                connection, caller_id, request_id, operation_payload
            )
            if replay is not None:
                return replay, False
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            result = self._continuation_result(connection, row, after_event_seq)
            if result["status"] != "heartbeat":
                self._finish_wait_leg(
                    connection, caller_id, request_id, operation_payload, call_id, result
                )
                return result, False
            if row["wait_request_id"] is not None:
                if row["wait_request_id"] != request_id:
                    return self._already_waiting(), False
                # A duplicate HTTP delivery of the still-running operation
                # shares its original persisted deadline; it must not restart
                # the leg before the first response is snapshotted.
                return result, True
            deadline = self._now() + timedelta(seconds=leg_seconds)
            connection.execute(
                "UPDATE calls SET wait_leg_deadline = ?, wait_request_id = ? WHERE call_id = ?",
                (self._stamp(deadline), request_id, call_id),
            )
            return result, True

    def poll_wait_leg(
        self,
        caller_id: str,
        request_id: str,
        interaction_id: str,
        call_id: str,
        after_event_seq: int,
        payload: str,
    ) -> dict[str, Any] | None:
        """Return a ready wait-leg response, or None while its persisted leg remains open."""
        operation_payload = self._continuation_payload(
            interaction_id, call_id, after_event_seq, payload
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
            if row["wait_request_id"] != request_id:
                return self._already_waiting()
            self._settle_if_expired(connection, row)
            row = self._call_or_raise(connection, call_id)
            result = self._continuation_result(connection, row, after_event_seq)
            if result["status"] == "heartbeat" and self._now() < self._parse_stamp(
                row["wait_leg_deadline"]
            ):
                return None
            self._finish_wait_leg(
                connection, caller_id, request_id, operation_payload, call_id, result
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
                if row["state"] in {"ringing", "accepted", "in_progress"}
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

    def _decline(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, acted_on: str | None = None
    ) -> dict[str, Any]:
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
            "UPDATE calls SET state = 'declined', acted_on = ?, version = version + 1 WHERE call_id = ?",
            (acted_on, row["call_id"]),
        )
        result["acted_on"] = acted_on
        self._add_event(connection, row["call_id"], result)
        return result

    def _cancel(
        self, connection: sqlite3.Connection, row: sqlite3.Row, *, feedback_requested: bool = False,
    ) -> dict[str, Any]:
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
        if self._is_task_request(row["message"]):
            result["task_id"] = row["interaction_id"]
        if feedback_requested:
            result["feedback_requested"] = True
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
            "SELECT * FROM calls WHERE state IN ('ringing', 'accepted', 'in_progress')"
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
            return self._settle_ringing_timeout(connection, row) or self._settle_agent_disconnect(
                connection, row
            )
        if row["state"] in {"accepted", "in_progress"}:
            return self._settle_terminal_disconnect(connection, row) or self._settle_agent_disconnect(
                connection, row
            )
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
        if row["handled_on"] == "iphone_web":
            connection.execute(
                "UPDATE calls SET state = 'disconnected', owner_session_id = NULL, "
                "version = version + 1 WHERE call_id = ?",
                (row["call_id"],),
            )
        else:
            connection.execute(
                "UPDATE calls SET state = 'disconnected', version = version + 1 WHERE call_id = ?",
                (row["call_id"],),
            )
        self._add_event(connection, row["call_id"], result)
        return result

    def _settle_agent_disconnect(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        if row["agent_reconnect_deadline"] is None:
            return None
        if self._now() < self._parse_stamp(row["agent_reconnect_deadline"]):
            return None
        result = self._envelope(
            status="disconnected",
            call_state="disconnected",
            interaction_id=row["interaction_id"],
            call_id=row["call_id"],
            reason="agent_disconnected",
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

    def _start_agent_reconnect_grace(
        self, connection: sqlite3.Connection, call_id: str, *, seconds: int = 30
    ) -> None:
        deadline = self._now() + timedelta(seconds=seconds)
        connection.execute(
            "UPDATE calls SET agent_reconnect_deadline = ? WHERE call_id = ?",
            (self._stamp(deadline), call_id),
        )

    def _start_progress_agent_reconnect_grace(
        self, connection: sqlite3.Connection, call_id: str, result: dict[str, Any]
    ) -> None:
        """Start D11's longer grace once for each durably delivered progress event."""
        event_seq = result.get("event_seq")
        if not isinstance(event_seq, int):
            return
        row = self._call_or_raise(connection, call_id)
        if row["agent_progress_event_seq"] == event_seq:
            return
        result_id = result.get("result_id")
        if isinstance(result_id, str):
            connection.execute(
                "INSERT OR IGNORE INTO results (result_id, call_id, result) VALUES (?, ?, ?)",
                (result_id, call_id, self._canonical(result)),
            )
        deadline = self._now() + timedelta(seconds=300)
        connection.execute(
            "UPDATE calls SET agent_reconnect_deadline = ?, agent_progress_event_seq = ? WHERE call_id = ?",
            (self._stamp(deadline), event_seq, call_id),
        )

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
        if row["state"] in {"declined", "not_accepted", "disconnected", "cancelled", "answered", "completed", "failed"}:
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
            needs_continuation=row["state"] in {"accepted", "in_progress"},
            handled_on=row["handled_on"],
            terminal_platform=row["terminal_platform"],
            event_seq=self._event_seq(connection, row["call_id"]),
            owner_device_id=row["owner_device_id"],
            owner_session_id=row["owner_session_id"],
            version=row["version"],
        )

    def _is_terminal_disconnect_owner(
        self, connection: sqlite3.Connection, row: sqlite3.Row,
        device_id: str, owner_session_id: str,
    ) -> bool:
        """Authorize only the pre-expiry iPhone owner to read its terminal result."""
        if (
            row["state"] != "disconnected"
            or row["handled_on"] != "iphone_web"
            or device_id != "iphone_web"
        ):
            return False
        event = connection.execute(
            "SELECT result FROM events WHERE call_id = ? ORDER BY event_seq DESC LIMIT 1",
            (row["call_id"],),
        ).fetchone()
        if event is None:
            return False
        result = self._decode(event["result"])
        return (
            result.get("reason") == "terminal_disconnected"
            and result.get("owner_session_id") == owner_session_id
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

    @staticmethod
    def _continuation_payload(
        interaction_id: str, call_id: str, after_event_seq: int, payload: str
    ) -> str:
        return CallCoordinator._canonical(
            {
                "interaction_id": interaction_id,
                "call_id": call_id,
                "after_event_seq": after_event_seq,
                "payload": payload,
            }
        )

    @staticmethod
    def _safe_leg_seconds(client_timeout_seconds: object) -> int | None:
        if (
            not isinstance(client_timeout_seconds, int)
            or isinstance(client_timeout_seconds, bool)
            or client_timeout_seconds < 2
        ):
            return None
        return max(
            1,
            client_timeout_seconds
            - min(30, max(1, client_timeout_seconds // 4)),
        )

    def _continuation_result(
        self, connection: sqlite3.Connection, row: sqlite3.Row, after_event_seq: int
    ) -> dict[str, Any]:
        """Return the next event, terminal state, or durable heartbeat snapshot."""
        if after_event_seq > (self._event_seq(connection, row["call_id"]) or 0):
            return self._invalid_cursor()
        event = self._event_after(connection, row["call_id"], after_event_seq)
        if event is not None:
            return self._decode(event["result"])
        if row["state"] in {"declined", "not_accepted", "disconnected", "cancelled", "answered", "completed", "failed"}:
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

    def _finish_wait_leg(
        self,
        connection: sqlite3.Connection,
        caller_id: str,
        request_id: str,
        operation_payload: str,
        call_id: str,
        result: dict[str, Any],
    ) -> None:
        if result["status"] == "opened" or (
            result["status"] == "heartbeat" and result.get("call_state") != "in_progress"
        ):
            self._start_agent_reconnect_grace(connection, call_id)
        elif result["status"] == "in_progress":
            self._start_progress_agent_reconnect_grace(connection, call_id, result)
        self._store_continuation_snapshot(
            connection, caller_id, request_id, operation_payload, result
        )
        connection.execute(
            "UPDATE calls SET wait_request_id = NULL WHERE call_id = ?", (call_id,)
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
                acted_on TEXT,
                terminal_last_seen_at TEXT, terminal_disconnect_deadline TEXT,
                agent_reconnect_deadline TEXT, agent_progress_event_seq INTEGER,
                wait_leg_deadline TEXT, wait_request_id TEXT,
                draft_body TEXT, draft_version INTEGER NOT NULL DEFAULT 0,
                task_version INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS create_operations (
                caller_id TEXT NOT NULL, request_id TEXT NOT NULL, payload TEXT NOT NULL,
                result TEXT NOT NULL, PRIMARY KEY (caller_id, request_id)
            );
            CREATE TABLE IF NOT EXISTS revision_operations (
                caller_id TEXT NOT NULL, interaction_id TEXT NOT NULL, revision_id TEXT NOT NULL,
                payload TEXT NOT NULL, result TEXT NOT NULL,
                PRIMARY KEY (caller_id, interaction_id, revision_id)
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
            CREATE TABLE IF NOT EXISTS results (
                result_id TEXT PRIMARY KEY, call_id TEXT NOT NULL, result TEXT NOT NULL,
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
            CREATE TABLE IF NOT EXISTS bark_deliveries (
                call_id TEXT PRIMARY KEY, state TEXT NOT NULL, attempt_count INTEGER NOT NULL,
                FOREIGN KEY (call_id) REFERENCES calls(call_id)
            );
            CREATE TABLE IF NOT EXISTS terminal_callback_deliveries (
                call_id TEXT PRIMARY KEY, state TEXT NOT NULL, attempt_count INTEGER NOT NULL,
                FOREIGN KEY (call_id) REFERENCES calls(call_id)
            );
            CREATE TABLE IF NOT EXISTS operator_sessions (
                session_id TEXT PRIMARY KEY, csrf TEXT NOT NULL, expires_at TEXT
            );
            CREATE TABLE IF NOT EXISTS browser_writers (
                call_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, page_id TEXT NOT NULL,
                writer_token TEXT NOT NULL, FOREIGN KEY (call_id) REFERENCES calls(call_id)
            );
            """
        )
        columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(calls)")
        }
        session_columns = {
            row["name"] for row in self._connection.execute("PRAGMA table_info(operator_sessions)")
        }
        if "expires_at" not in session_columns:
            self._connection.execute("ALTER TABLE operator_sessions ADD COLUMN expires_at TEXT")
        if "desktop_route_device_id" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN desktop_route_device_id TEXT")
        if "acted_on" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN acted_on TEXT")
        if "terminal_last_seen_at" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN terminal_last_seen_at TEXT")
        if "terminal_disconnect_deadline" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN terminal_disconnect_deadline TEXT")
        if "agent_reconnect_deadline" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN agent_reconnect_deadline TEXT")
        if "agent_progress_event_seq" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN agent_progress_event_seq INTEGER")
        if "wait_leg_deadline" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN wait_leg_deadline TEXT")
        if "wait_request_id" not in columns:
            self._connection.execute("ALTER TABLE calls ADD COLUMN wait_request_id TEXT")
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
    def _invalid_client_timeout() -> dict[str, Any]:
        return {"success": False, "status": "invalid_client_timeout", "needs_continuation": False}

    @staticmethod
    def _already_waiting() -> dict[str, Any]:
        return {
            "success": False,
            "status": "already_waiting",
            "needs_continuation": False,
            "retryable": True,
        }

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
