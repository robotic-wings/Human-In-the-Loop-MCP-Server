"""HITL HTTP transport: ASGI application (terminal, browser, MCP endpoints)."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import uuid
from collections.abc import Awaitable, Callable, Mapping
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from hitl_core import Clock, Notifier, RouteAdmission, Waiter, WaitingLifecycle
from hitl_mcp import McpRpcMixin

class Application(McpRpcMixin):
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
        if isinstance(path, str) and path.startswith("/api/agent/outcomes"):
            await self._agent_outcomes_api(method, path, scope.get("headers", []), receive, send)
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

    async def _agent_outcomes_api(self, method: object, path: object, headers: object,
                                   receive: Any, send: Any) -> None:
        """Transport-neutral terminal-outcome feed (SPEC-chidori §4.3).

        GET /api/agent/outcomes?after=<seq>&limit=N[&undeliverable=1]
        POST /api/agent/outcomes/ack  {seq, status: "delivered"|"undeliverable"}
        """
        caller = self._caller(headers)
        if caller is None:
            await self._send(send, 401, self._error("unauthorized")); return
        if method == "POST" and path == "/api/agent/outcomes/ack":
            body = await self._body(receive)
            if body is None:
                await self._send(send, 400, self._error("invalid_request")); return
            seq, status = body.get("seq"), body.get("status")
            if not isinstance(seq, int) or isinstance(seq, bool) or status not in {"delivered", "undeliverable"}:
                await self._send(send, 400, self._error("invalid_request")); return
            self.lifecycle.ack_delivery(seq, status)
            await self._send(send, 200, {"success": True}); return
        if method != "GET" or path != "/api/agent/outcomes":
            await self._send(send, 404, self._error("not_found")); return
        import urllib.parse as _up
        qs = _up.parse_qs(_up.urlsplit(str(path)).query)
        try:
            after = int(qs.get("after", ["0"])[0])
            limit = max(1, min(500, int(qs.get("limit", ["100"])[0])))
        except ValueError:
            await self._send(send, 400, self._error("invalid_request")); return
        want_undeliverable = qs.get("undeliverable", ["0"])[0] == "1"
        feed = self.lifecycle.outcome_feed(after, limit, caller_id=caller, undeliverable=want_undeliverable)
        await self._send(send, 200, {
            "success": True,
            "outcomes": feed,
            "next_after": (feed[-1]["seq"] if feed else after),
        })

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
        callsign = body.get("callsign")
        if callsign is not None and not isinstance(callsign, str):
            return self._error("invalid_request"), False
        delivery = body.get("delivery")
        if delivery is not None and not isinstance(delivery, Mapping):
            return self._error("invalid_request"), False
        invited_desktop_id = self._invited_desktop_id(callsign) if not replay else None
        mobile_eligible = self._mobile_eligible(callsign) if not replay else False
        return self.lifecycle.create(
            caller, request_id, payload, invited_desktop_id, mobile_eligible,
            operator_callsign=callsign, delivery=delivery,
        ), not replay

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
