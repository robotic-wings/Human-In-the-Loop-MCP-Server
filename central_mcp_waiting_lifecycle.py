"""Durable, GUI-free MCP waiting lifecycle adapter.

This bounded adapter deliberately owns only caller admission and the waiting
event stream.  Human interfaces and notification delivery remain injected
boundaries.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import sqlite3
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any


Clock = Callable[[], float]
Waiter = Callable[[float], Awaitable[None]]
Notifier = Callable[[dict[str, object]], None]
RouteAdmission = Callable[[], Mapping[str, object]]


class WaitingLifecycle:
    def __init__(self, path: str | Path, clock: Clock, notify: Notifier, ring_timeout: float,
                 agent_reconnect_seconds: float) -> None:
        self._path, self._clock, self._notify = str(path), clock, notify
        self._ring_timeout, self._agent_reconnect_seconds = ring_timeout, agent_reconnect_seconds
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript("""
              CREATE TABLE IF NOT EXISTS lifecycle_calls (
                call_id TEXT PRIMARY KEY, interaction_id TEXT NOT NULL, caller_id TEXT NOT NULL,
                call_state TEXT NOT NULL, ring_deadline REAL NOT NULL, reason TEXT
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
        request_columns = {row["name"] for row in conn.execute("PRAGMA table_info(lifecycle_requests)")}
        if "operation_kind" not in request_columns:
            conn.execute("ALTER TABLE lifecycle_requests ADD COLUMN operation_kind TEXT NOT NULL DEFAULT 'create'")

    def create(self, caller_id: str, request_id: str, payload: Mapping[str, object]) -> dict[str, object]:
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
                active = conn.execute("SELECT call_id FROM lifecycle_calls WHERE call_state IN ('ringing', 'accepted', 'in_progress') LIMIT 1").fetchone()
                if active is not None:
                    outcome = {"success": True, "status": "busy", "call_state": None, "call_id": None, "needs_continuation": False}
                else:
                    call_id, interaction_id = str(uuid.uuid4()), str(uuid.uuid4())
                    deadline = self._clock() + self._ring_timeout
                    conn.execute(
                        "INSERT INTO lifecycle_calls "
                        "(call_id, interaction_id, caller_id, call_state, ring_deadline, reason, agent_reconnect_deadline) "
                        "VALUES (?, ?, ?, 'ringing', ?, NULL, NULL)",
                        (call_id, interaction_id, caller_id, deadline),
                    )
                    outcome = self._heartbeat(interaction_id, call_id, "ringing")
                    notification = {"call_id": call_id, "ring_deadline": deadline}
                conn.execute("INSERT INTO lifecycle_requests VALUES (?, ?, ?, ?, 'create')", (caller_id, request_id, encoded, self._dump(outcome)))
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        if notification is not None:
            self._notify(notification)
        return outcome

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

    def accept(self, *, call_id: str, device_id: str, handled_on: str, terminal_platform: str) -> dict[str, object]:
        del device_id
        return self._human_action(call_id, "opened", "accepted", None, {"handled_on": handled_on, "terminal_platform": terminal_platform})

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
            "SELECT * FROM lifecycle_calls WHERE call_state IN ('ringing', 'accepted', 'in_progress') "
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
                 desktop_registration_service: Any | None = None) -> None:
        self.lifecycle, self._credentials, self._wait = lifecycle, dict(credentials), wait
        self._deadline_tick_seconds, self._scheduler_sleep = deadline_tick_seconds, scheduler_sleep
        self._route_admission = route_admission
        self._notification_router = notification_router
        self._desktop_registration_service = desktop_registration_service
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
        if method != "POST" or path != "/mcp":
            await self._send(send, 404, self._error("not_found")); return
        caller = self._caller(scope.get("headers", []))
        if caller is None:
            await self._send(send, 401, self._error("unauthorized")); return
        body = await self._body(receive)
        if body is None:
            await self._send(send, 400, self._error("invalid_request")); return
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
        if path not in {"/api/terminals/heartbeat", "/api/terminals/state"}:
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
        else:
            await self._send(send, 404, self._error("not_found")); return
        await self._send(send, 200 if response.get("success") else 404, response)

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
        return self.lifecycle.create(caller, request_id, payload), not replay

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

    @staticmethod
    async def _body(receive: Any) -> dict[str, object] | None:
        event = await receive()
        if event.get("type") != "http.request": return None
        try: value = json.loads(event.get("body", b""))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError): return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _error(status: str) -> dict[str, object]:
        return {"success": False, "status": status, "needs_continuation": False}

    @staticmethod
    def _terminal_error(status: str) -> dict[str, object]:
        return {"success": False, "status": status}

    async def _send(self, send: Any, status: int, body: Mapping[str, object]) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": json.dumps(body).encode(), "more_body": False})


def create_app(*, database_path: str | Path, credentials: Mapping[str, str], clock: Clock, notify: Notifier,
               wait: Waiter, ring_timeout_seconds: float = 60, agent_reconnect_seconds: float = 30,
               deadline_tick_seconds: float = 1, scheduler_sleep: Waiter = asyncio.sleep,
               route_admission: RouteAdmission | None = None, notification_router: Any | None = None,
               notification_dispatch_tick_seconds: float = 1,
               notification_scheduler_sleep: Waiter = asyncio.sleep,
               desktop_registration_service: Any | None = None) -> Application:
    if deadline_tick_seconds <= 0 or notification_dispatch_tick_seconds <= 0:
        raise ValueError("scheduler tick seconds must be positive")
    return Application(
        WaitingLifecycle(database_path, clock, notify, ring_timeout_seconds, agent_reconnect_seconds),
        credentials,
        wait,
        deadline_tick_seconds,
        scheduler_sleep,
        route_admission if route_admission is not None else (lambda: {"success": True, "status": "ready"}),
        notification_router,
        notification_dispatch_tick_seconds,
        notification_scheduler_sleep,
        desktop_registration_service,
    )
