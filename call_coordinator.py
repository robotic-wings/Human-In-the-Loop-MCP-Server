"""Persistent, transport-free coordination for one operator call at a time."""

from __future__ import annotations

import json
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


class CallCoordinator:
    """SQLite-backed admission and ringing-state coordinator.

    The coordinator deliberately has no transport or human-interface concerns.
    Every state transition is performed in a SQLite immediate transaction so a
    single active call and a single owner are preserved across threads/restarts.
    """

    def __init__(self, database_path: Path, *, clock: Clock) -> None:
        self._clock = clock
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA journal_mode=WAL")
        self._connection.execute("PRAGMA foreign_keys=ON")
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
                    "created_at, ring_deadline, version) VALUES (?, ?, ?, ?, ?, 'ringing', ?, ?, 1)",
                    (
                        call_id,
                        request.operator_id,
                        request.caller_id,
                        request.interaction_id,
                        request.message,
                        self._stamp(now),
                        self._stamp(deadline),
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
        connection.execute(
            "UPDATE calls SET state = 'accepted', owner_device_id = ?, "
            "owner_session_id = ?, handled_on = ?, terminal_platform = ?, version = version + 1 "
            "WHERE call_id = ?",
            (
                action["device_id"],
                action["session_id"],
                action["handled_on"],
                action["terminal_platform"],
                row["call_id"],
            ),
        )
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

    def _settle_all_expired(self, connection: sqlite3.Connection) -> list[dict[str, Any]]:
        rows = connection.execute("SELECT * FROM calls WHERE state = 'ringing'").fetchall()
        settled: list[dict[str, Any]] = []
        for row in rows:
            result = self._settle_if_expired(connection, row)
            if result is not None:
                settled.append(result)
        return settled

    def _settle_if_expired(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any] | None:
        if row["state"] != "ringing" or self._now() < self._parse_stamp(row["ring_deadline"]):
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

    def _current_result(
        self, connection: sqlite3.Connection, row: sqlite3.Row
    ) -> dict[str, Any]:
        if row["state"] in {"declined", "not_accepted"}:
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
        )

    def _add_event(
        self, connection: sqlite3.Connection, call_id: str, result: dict[str, Any]
    ) -> None:
        connection.execute(
            "INSERT INTO events (call_id, event_seq, result) VALUES (?, 1, ?)",
            (call_id, self._canonical(result)),
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
                terminal_platform TEXT
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
            """
        )
        self._connection.commit()

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
            "ring_deadline": ring_deadline,
        }


class _Transaction:
    def __init__(self, lock: threading.RLock, connection: sqlite3.Connection) -> None:
        self._lock = lock
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        if exc_type is None:
            self._connection.commit()
        else:
            self._connection.rollback()
        self._lock.release()
