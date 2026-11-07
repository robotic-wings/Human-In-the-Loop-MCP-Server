"""Durable, GUI-free notification routing for central calls."""

from __future__ import annotations

import asyncio
import inspect
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Callable

Clock = Callable[[], float]
Dispatch = Callable[[dict[str, object]], object]
CallState = Callable[[str], str | None]


class _Store:
    def __init__(self, database_path: str | Path) -> None:
        path = Path(database_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = str(path)
        with self.connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS desktop_terminals (
                    device_id TEXT PRIMARY KEY, endpoint TEXT NOT NULL,
                    callback_credential TEXT NOT NULL, gui_ready INTEGER NOT NULL,
                    last_seen REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS notification_outbox (
                    call_id TEXT NOT NULL, channel TEXT NOT NULL,
                    ring_deadline REAL NOT NULL, state TEXT NOT NULL,
                    created_at REAL NOT NULL, PRIMARY KEY (call_id, channel)
                );
            """)

    @contextmanager
    def connection(self):
        conn = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
        finally:
            conn.close()


class TerminalRegistry:
    """Management-owned desktop notification endpoint records."""

    def __init__(self, *, database_path: str | Path, clock: Clock) -> None:
        self._clock = clock
        self._store = _Store(database_path)

    def register_desktop(self, *, actor_role: str, device_id: str, endpoint: str,
                         callback_credential: str, gui_ready: bool) -> dict[str, object]:
        if actor_role not in {"operator", "management"}:
            raise PermissionError("desktop registration requires management authority")
        with self._store.connection() as conn:
            conn.execute(
                "INSERT INTO desktop_terminals VALUES (?, ?, ?, ?, ?) "
                "ON CONFLICT(device_id) DO UPDATE SET endpoint=excluded.endpoint, "
                "callback_credential=excluded.callback_credential, gui_ready=excluded.gui_ready, "
                "last_seen=excluded.last_seen",
                (device_id, endpoint, callback_credential, int(gui_ready), self._clock()),
            )
        return self.desktop_state(device_id)  # type: ignore[return-value]

    def heartbeat(self, device_id: str, gui_ready: bool) -> dict[str, object] | None:
        with self._store.connection() as conn:
            changed = conn.execute("UPDATE desktop_terminals SET gui_ready=?, last_seen=? WHERE device_id=?",
                                   (int(gui_ready), self._clock(), device_id)).rowcount
        return self.desktop_state(device_id) if changed else None

    def desktop_state(self, device_id: str) -> dict[str, object] | None:
        with self._store.connection() as conn:
            row = conn.execute("SELECT * FROM desktop_terminals WHERE device_id=?", (device_id,)).fetchone()
        return self._state(row)

    def ready_desktop(self) -> dict[str, object] | None:
        with self._store.connection() as conn:
            row = conn.execute("SELECT * FROM desktop_terminals ORDER BY device_id LIMIT 1").fetchone()
        return self._state(row)

    def _state(self, row: sqlite3.Row | None) -> dict[str, object] | None:
        if row is None:
            return None
        return {"device_id": row["device_id"], "endpoint": row["endpoint"],
                "callback_credential": row["callback_credential"], "gui_ready": bool(row["gui_ready"]),
                "fresh": bool(row["gui_ready"]) and self._clock() - row["last_seen"] <= 30}


class NotificationRouter:
    """Persist notification choices before concurrently attempting delivery."""

    _CHANNELS = {"desktop": ("desktop",), "iphone": ("bark",), "both": ("desktop", "bark")}

    def __init__(self, *, database_path: str | Path, clock: Clock, registry: TerminalRegistry,
                 desktop_dispatch: Dispatch, bark_dispatch: Dispatch, route_mode: str,
                 desktop_timeout_seconds: float = 2.5, bark_timeout_seconds: float = 2.5,
                 call_state: CallState | None = None) -> None:
        self._store, self._clock, self._registry = _Store(database_path), clock, registry
        self._desktop_dispatch, self._bark_dispatch, self._route_mode = desktop_dispatch, bark_dispatch, route_mode
        self._desktop_timeout, self._bark_timeout, self._call_state = desktop_timeout_seconds, bark_timeout_seconds, call_state

    def validate_route(self) -> dict[str, object]:
        if self._route_mode not in self._CHANNELS:
            return {"success": False, "status": "no_enabled_channel", "needs_continuation": False}
        return {"success": True, "status": "ready", "needs_continuation": False}

    def enqueue(self, event: dict[str, object]) -> None:
        now = self._clock()
        with self._store.connection() as conn:
            for channel in self._CHANNELS.get(self._route_mode, ()):
                conn.execute("INSERT OR IGNORE INTO notification_outbox VALUES (?, ?, ?, 'pending', ?)",
                             (event["call_id"], channel, event["ring_deadline"], now))

    def delivery_states(self, call_id: str) -> list[dict[str, object]]:
        with self._store.connection() as conn:
            rows = conn.execute("SELECT channel, state FROM notification_outbox WHERE call_id=? ORDER BY channel", (call_id,)).fetchall()
        return [{"channel": row["channel"], "state": row["state"]} for row in rows]

    def recover(self) -> list[str]:
        """Uncertain sends are deliberately not retried after a restart."""
        return []

    async def dispatch_pending(self) -> dict[str, object]:
        with self._store.connection() as conn:
            rows = conn.execute("SELECT call_id, channel, ring_deadline FROM notification_outbox WHERE state='pending'").fetchall()
        await asyncio.gather(*(self._dispatch_row(dict(row)) for row in rows))
        decisions: dict[str, str] = {}
        for call_id in {str(row["call_id"]) for row in rows}:
            states = {entry["state"] for entry in self.delivery_states(call_id)}
            decisions[call_id] = "unreachable" if states and states <= {"failed", "skipped"} else "ringing"
        return {"call_decisions": decisions}

    async def _dispatch_row(self, row: dict[str, object]) -> None:
        call_id, channel = str(row["call_id"]), str(row["channel"])
        state = self._call_state(call_id) if self._call_state else "ringing"
        if self._clock() >= float(row["ring_deadline"]) or state != "ringing":
            self._set_state(call_id, channel, "skipped")
            return
        payload: dict[str, object] = {"call_id": call_id, "ring_deadline": row["ring_deadline"]}
        dispatch, timeout = self._bark_dispatch, self._bark_timeout
        if channel == "desktop":
            terminal = self._registry.ready_desktop()
            # A configured desktop route may still be worth one bounded
            # callback attempt while its readiness record is absent/stale.
            # The transport classifies the result; this does not manufacture
            # a registration or claim the terminal is ready.
            if terminal is not None:
                payload.update(terminal)
            dispatch, timeout = self._desktop_dispatch, self._desktop_timeout
        try:
            response = dispatch(payload)
            if inspect.isawaitable(response):
                response = await asyncio.wait_for(response, timeout)
            delivered = bool(response.get("accepted")) if channel == "desktop" else (
                response.get("http_status") == 200 and bool(response.get("business_success")))
            self._set_state(call_id, channel, "delivered" if delivered else "failed")
        except (asyncio.TimeoutError, TimeoutError):
            self._set_state(call_id, channel, "uncertain")
        except Exception:
            self._set_state(call_id, channel, "failed")

    def _set_state(self, call_id: str, channel: str, state: str) -> None:
        with self._store.connection() as conn:
            conn.execute("UPDATE notification_outbox SET state=? WHERE call_id=? AND channel=?", (state, call_id, channel))
