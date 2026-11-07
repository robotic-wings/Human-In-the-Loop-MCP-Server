"""GUI-free persistent ownership boundary for terminal call actions."""

from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Mapping

from central_call_coordinator import CallCoordinator


class TerminalCallService:
    def __init__(self, *, database_path: str | Path, coordinator: CallCoordinator,
                 clock: Callable[[], float], registered_credentials: Mapping[str, Mapping[str, str]],
                 on_state_update: Callable[[dict[str, object]], None]) -> None:
        self._path, self._coordinator, self._clock = str(database_path), coordinator, clock
        self._credentials, self._on_update = dict(registered_credentials), on_state_update
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS terminal_sessions (
                session_id TEXT PRIMARY KEY, device_id TEXT NOT NULL,
                terminal_category TEXT NOT NULL, terminal_platform TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS terminal_calls (
                call_id TEXT PRIMARY KEY, channels TEXT NOT NULL
              );
              CREATE TABLE IF NOT EXISTS terminal_operations (
                session_id TEXT NOT NULL, operation_id TEXT NOT NULL, outcome TEXT NOT NULL,
                PRIMARY KEY (session_id, operation_id)
              );
            """)

    @contextmanager
    def _connection(self):
        conn = sqlite3.connect(self._path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()

    def authenticate(self, *, credential: str) -> dict[str, object]:
        metadata = self._credentials.get(credential)
        if metadata is None:
            return {"success": False, "status": "terminal_not_authenticated"}
        session_id = str(uuid.uuid4())
        with self._connection() as conn:
            conn.execute("INSERT INTO terminal_sessions VALUES (?, ?, ?, ?)",
                         (session_id, metadata["device_id"], metadata["terminal_category"], metadata["terminal_platform"]))
        return {"success": True, "session_id": session_id}

    def session_state(self, *, session_id: str) -> dict[str, object] | None:
        with self._connection() as conn:
            row = conn.execute("SELECT device_id, terminal_category, terminal_platform FROM terminal_sessions WHERE session_id=?", (session_id,)).fetchone()
        return dict(row) if row else None

    def track_call(self, *, call_id: str, enabled_channels: set[str]) -> None:
        with self._connection() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO terminal_calls (call_id, channels) VALUES (?, ?)",
                (call_id, ",".join(sorted(enabled_channels))),
            )

    def read_mobile_state(self, *, call_id: str, session_id: str) -> dict[str, object]:
        session = self.session_state(session_id=session_id)
        if session is None or session["terminal_category"] != "iphone_web" or not self._eligible(call_id, "bark"):
            return self._hidden()
        call = self._coordinator.get_call(call_id)
        if call is None:
            return self._hidden()
        state = str(call["call_state"])
        if state == "accepted" and not self._is_owner(call_id, session_id):
            return self._hidden()
        result: dict[str, object] = {"success": True, "call_id": call_id, "call_state": state,
                                     "can_accept": state == "ringing", "can_decline": state == "ringing"}
        if state == "accepted":
            result["can_write"] = True
        if "handled_on" in call:
            result["handled_on"] = call["handled_on"]
        if "terminal_platform" in call:
            result["terminal_platform"] = call["terminal_platform"]
        return result

    def accept(self, *, call_id: str, session_id: str, operation_id: str) -> dict[str, object]:
        session = self.session_state(session_id=session_id)
        if session is None or not self._eligible_for_session(call_id, session):
            return self._hidden()
        if self._owner_is_other_mobile_session(call_id, session_id, session):
            return self._hidden()
        replay = self._replay(session_id, operation_id)
        if replay is not None:
            return replay
        # This private seam runs before the coordinator transaction so a
        # binding failure cannot create a coordinator-only accepted call.  The
        # actual non-secret binding is written by CallCoordinator.accept in
        # the same durable transaction as the accepted state.
        self._persist_owner(call_id, session_id)
        outcome = self._coordinator.accept(
            call_id,
            str(session["device_id"]),
            str(session["terminal_category"]),
            str(session["terminal_platform"]),
            owner_session_id=session_id,
        )
        self._save(session_id, operation_id, outcome)
        if outcome.get("success") is True:
            self._on_update({"call_id": call_id, "call_state": "accepted"})
        return outcome

    def decline(self, *, call_id: str, session_id: str, operation_id: str) -> dict[str, object]:
        session = self.session_state(session_id=session_id)
        if session is None or not self._eligible_for_session(call_id, session):
            return self._hidden()
        if self._owner_is_other_mobile_session(call_id, session_id, session):
            return self._hidden()
        replay = self._replay(session_id, operation_id)
        if replay is not None:
            return replay
        outcome = self._coordinator.decline(call_id, str(session["device_id"]))
        self._save(session_id, operation_id, outcome)
        if outcome.get("success") is True:
            self._on_update({"call_id": call_id, "call_state": "declined"})
        return outcome

    def _eligible_for_session(self, call_id: str, session: Mapping[str, object]) -> bool:
        channel = "bark" if session["terminal_category"] == "iphone_web" else "desktop"
        return self._eligible(call_id, channel)

    def _eligible(self, call_id: str, channel: str) -> bool:
        with self._connection() as conn:
            row = conn.execute("SELECT channels FROM terminal_calls WHERE call_id=?", (call_id,)).fetchone()
        return row is not None and channel in str(row["channels"]).split(",")

    def _is_owner(self, call_id: str, session_id: str) -> bool:
        return self._coordinator.owner_session_matches(
            call_id=call_id, session_id=session_id
        )

    def _persist_owner(self, call_id: str, session_id: str) -> None:
        """Failure seam for binding preparation; durable write is coordinator-owned."""

        del call_id, session_id

    def _owner_is_other_mobile_session(
        self, call_id: str, session_id: str, session: Mapping[str, object]
    ) -> bool:
        """Keep accepted iPhone calls bound to their original browser session.

        The existing desktop race contract retains its coordinator-level
        ``already_accepted`` result.  Mobile read/write surfaces, which carry
        the browser session capability, must not disclose or transfer an
        accepted call to a distinct browser session.
        """
        return (
            session["terminal_category"] == "iphone_web"
            and not self._is_owner(call_id, session_id)
            and self._call_is_accepted(call_id)
        )

    def _call_is_accepted(self, call_id: str) -> bool:
        call = self._coordinator.get_call(call_id)
        return call is not None and call.get("call_state") == "accepted"

    def _replay(self, session_id: str, operation_id: str) -> dict[str, object] | None:
        import json
        with self._connection() as conn:
            row = conn.execute("SELECT outcome FROM terminal_operations WHERE session_id=? AND operation_id=?", (session_id, operation_id)).fetchone()
        return json.loads(row["outcome"]) if row else None

    def _save(self, session_id: str, operation_id: str, outcome: dict[str, object]) -> None:
        import json
        with self._connection() as conn:
            conn.execute("INSERT OR IGNORE INTO terminal_operations VALUES (?, ?, ?)",
                         (session_id, operation_id, json.dumps(outcome, sort_keys=True)))

    @staticmethod
    def _hidden() -> dict[str, object]:
        return {"success": False, "status": "session_not_found"}
