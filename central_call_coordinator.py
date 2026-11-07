"""Persistent, GUI-free coordination for a single operator call slot.

This deliberately small module is the first central-service slice.  It owns
only the transactional call state needed by the coordinator contract; HTTP,
MCP, user interfaces and delivery transports remain outside this boundary.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any


JsonMapping = Mapping[str, Any]
Notification = Callable[[dict[str, object]], None]


class CallCoordinator:
    """Coordinate durable ringing, acceptance, decline and expiry state."""

    _ACTIVE_STATES = ("ringing", "accepted")

    def __init__(
        self,
        *,
        database_path: str | Path,
        clock: Callable[[], float],
        notify: Notification,
        ring_timeout_seconds: float = 60,
    ) -> None:
        self._database_path = str(database_path)
        self._clock = clock
        self._notify = notify
        self._ring_timeout_seconds = ring_timeout_seconds
        self._schema_lock = threading.Lock()
        self._initialize_schema()

    def close(self) -> None:
        """Compatibility lifecycle hook; connections are operation-scoped."""

    def create_call(
        self, *, caller_id: str, request_id: str, payload: JsonMapping
    ) -> dict[str, object]:
        canonical_payload = self._canonical_payload(payload)
        notification: dict[str, object] | None = None
        with self._transaction() as connection:
            request = connection.execute(
                "SELECT payload_json, outcome_json FROM requests "
                "WHERE caller_id = ? AND request_id = ?",
                (caller_id, request_id),
            ).fetchone()
            if request is not None:
                if request["payload_json"] == canonical_payload:
                    return self._load_json(request["outcome_json"])
                return self._request_conflict()

            self._expire_due_locked(connection, self._clock())
            active = connection.execute(
                "SELECT call_id FROM calls WHERE call_state IN ('ringing', 'accepted') "
                "LIMIT 1"
            ).fetchone()
            if active is not None:
                outcome = self._busy()
            else:
                call_id = str(uuid.uuid4())
                deadline = self._clock() + self._ring_timeout_seconds
                connection.execute(
                    "INSERT INTO calls (call_id, caller_id, call_state, ring_deadline) "
                    "VALUES (?, ?, 'ringing', ?)",
                    (call_id, caller_id, deadline),
                )
                outcome = {
                    "success": True,
                    "status": "ringing",
                    "call_state": "ringing",
                    "call_id": call_id,
                    "ring_deadline": deadline,
                }
                notification = {"call_id": call_id, "ring_deadline": deadline}
            connection.execute(
                "INSERT INTO requests (caller_id, request_id, payload_json, outcome_json) "
                "VALUES (?, ?, ?, ?)",
                (caller_id, request_id, canonical_payload, self._dump_json(outcome)),
            )
        if notification is not None:
            self._notify(notification)
        return outcome

    def accept(self, call_id: str, device_id: str) -> dict[str, object]:
        with self._transaction() as connection:
            call = self._get_call_locked(connection, call_id)
            if call is None:
                return self._not_found()
            self._settle_call_if_expired_locked(connection, call, self._clock())
            call = self._get_call_locked(connection, call_id)
            assert call is not None
            if call["call_state"] == "accepted":
                if call["owner_device_id"] == device_id:
                    return self._load_json(call["accept_outcome_json"])
                return {"success": False, "status": "already_accepted", "call_state": "accepted"}
            if call["call_state"] == "declined":
                return {"success": False, "status": "already_declined", "call_state": "declined"}
            if call["call_state"] == "not_accepted":
                return self._expired()

            handled_on = "iphone_web" if device_id.startswith("iphone") else "desktop"
            outcome = {
                "success": True,
                "status": "opened",
                "call_state": "accepted",
                "call_id": call_id,
                "handled_on": handled_on,
            }
            connection.execute(
                "UPDATE calls SET call_state = 'accepted', owner_device_id = ?, "
                "accept_outcome_json = ? WHERE call_id = ?",
                (device_id, self._dump_json(outcome), call_id),
            )
            return outcome

    def decline(self, call_id: str, device_id: str) -> dict[str, object]:
        del device_id  # Ownership is relevant after acceptance, not to a ringing decline.
        with self._transaction() as connection:
            call = self._get_call_locked(connection, call_id)
            if call is None:
                return self._not_found()
            self._settle_call_if_expired_locked(connection, call, self._clock())
            call = self._get_call_locked(connection, call_id)
            assert call is not None
            if call["call_state"] == "declined":
                return self._load_json(call["decline_outcome_json"])
            if call["call_state"] == "accepted":
                return {"success": False, "status": "already_accepted", "call_state": "accepted"}
            if call["call_state"] == "not_accepted":
                return self._expired()

            outcome = {
                "success": True,
                "status": "declined",
                "call_state": "declined",
                "reason": "human_declined",
            }
            connection.execute(
                "UPDATE calls SET call_state = 'declined', reason = 'human_declined', "
                "decline_outcome_json = ? WHERE call_id = ?",
                (self._dump_json(outcome), call_id),
            )
            return outcome

    def expire_due(self) -> list[dict[str, object]]:
        with self._transaction() as connection:
            return self._expire_due_locked(connection, self._clock(), include_human_action=True)

    def recover(self) -> list[dict[str, object]]:
        """Settle deadlines after process restart without issuing notifications."""

        with self._transaction() as connection:
            return self._expire_due_locked(connection, self._clock())

    def get_call(self, call_id: str) -> dict[str, object] | None:
        with self._connection() as connection:
            call = self._get_call_locked(connection, call_id)
            if call is None:
                return None
            result: dict[str, object] = {"call_id": call_id, "call_state": call["call_state"]}
            if call["reason"] is not None:
                result["reason"] = call["reason"]
            if call["owner_device_id"] is not None:
                result["owner_device_id"] = call["owner_device_id"]
            return result

    def _initialize_schema(self) -> None:
        with self._schema_lock, self._connection() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS calls (
                    call_id TEXT PRIMARY KEY,
                    caller_id TEXT NOT NULL,
                    call_state TEXT NOT NULL,
                    ring_deadline REAL NOT NULL,
                    owner_device_id TEXT,
                    reason TEXT,
                    accept_outcome_json TEXT,
                    decline_outcome_json TEXT
                );
                CREATE TABLE IF NOT EXISTS requests (
                    caller_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    outcome_json TEXT NOT NULL,
                    PRIMARY KEY (caller_id, request_id)
                );
                CREATE INDEX IF NOT EXISTS calls_active_idx
                    ON calls (call_state);
                """
            )

    def _connection(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA journal_mode = WAL")
        return connection

    @contextmanager
    def _transaction(self):
        connection = self._connection()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
        except BaseException:
            connection.rollback()
            raise
        else:
            connection.commit()
        finally:
            connection.close()

    def _get_call_locked(self, connection: sqlite3.Connection, call_id: str) -> sqlite3.Row | None:
        return connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()

    def _settle_call_if_expired_locked(
        self, connection: sqlite3.Connection, call: sqlite3.Row, now: float
    ) -> None:
        if call["call_state"] == "ringing" and now >= call["ring_deadline"]:
            self._mark_expired_locked(connection, call["call_id"])

    def _expire_due_locked(
        self, connection: sqlite3.Connection, now: float, *, include_human_action: bool = False
    ) -> list[dict[str, object]]:
        due = connection.execute(
            "SELECT call_id FROM calls WHERE call_state = 'ringing' AND ring_deadline <= ?",
            (now,),
        ).fetchall()
        events: list[dict[str, object]] = []
        for call in due:
            call_id = call["call_id"]
            self._mark_expired_locked(connection, call_id)
            event: dict[str, object] = {
                "call_id": call_id,
                "status": "not_accepted",
                "call_state": "not_accepted",
                "reason": "ring_timeout",
            }
            if include_human_action:
                event["human_action"] = False
            events.append(event)
        return events

    @staticmethod
    def _mark_expired_locked(connection: sqlite3.Connection, call_id: str) -> None:
        connection.execute(
            "UPDATE calls SET call_state = 'not_accepted', reason = 'ring_timeout' "
            "WHERE call_id = ? AND call_state = 'ringing'",
            (call_id,),
        )

    @staticmethod
    def _canonical_payload(payload: JsonMapping) -> str:
        return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _dump_json(value: Mapping[str, object]) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @staticmethod
    def _load_json(value: str) -> dict[str, object]:
        return json.loads(value)

    @staticmethod
    def _request_conflict() -> dict[str, object]:
        return {"success": False, "status": "request_conflict", "needs_continuation": False}

    @staticmethod
    def _busy() -> dict[str, object]:
        return {
            "success": True,
            "status": "busy",
            "call_state": None,
            "call_id": None,
            "needs_continuation": False,
            "retryable": True,
            "retry_after_seconds": 30,
        }

    @staticmethod
    def _expired() -> dict[str, object]:
        return {
            "success": False,
            "status": "expired",
            "call_state": "not_accepted",
            "reason": "ring_timeout",
        }

    @staticmethod
    def _not_found() -> dict[str, object]:
        return {"success": False, "status": "session_not_found", "needs_continuation": False}
