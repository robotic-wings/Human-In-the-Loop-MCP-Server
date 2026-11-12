"""HITL MCP surface: tool catalog, operating manual and the JSON-RPC mixin."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

class _RpcError(Exception):
    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code, self.message = code, message


_HUMAN_LOOP_MANUAL = """HITL MCP Operating Manual (human-in-the-loop toolset)

1. Core model: one call waits
- Each interactive tool (get_user_input / get_number_input / get_user_choice / get_multiline_input / show_confirmation_dialog / show_info_message / get_feedback_from_user / assign_task_to_human) starts one call round. The server parks the call for nearly client_timeout_seconds (default 30; pass longer, e.g. 300, when the human may be away).
- If the human answers within the park window, that SAME call returns the final result. Do not poll, do not fire parallel calls.
- If it returns a heartbeat, follow the continuation protocol below and call once more.

2. Continuation protocol (critical)
- When a response has needs_continuation=true (heartbeat / opened / in_progress): re-call the SAME tool with interaction_id, call_id, and after_event_seq = next_after_event_seq from that response (it is the machine-readable cursor; plain heartbeats repeat your current value).
- The cursor only moves forward: whenever a response contains an event_seq, adopt it; when it is a plain heartbeat (no event), keep your current value exactly. Resetting to an old value re-drains old events forever and hides later ones (accept, progress, cancel) from you.
- Each call already parks server-side for nearly client_timeout_seconds, so there is no need to re-call quickly; and while a round is open (needs_continuation=true) never report the task as done, failed, or abandoned — either keep waiting or end it explicitly with cancel_interaction.
- opened (human accepted) and in_progress (human working, may carry progress reports) both mean "keep waiting": continue the round. Do NOT start a new call (it returns busy).

3. Terminal outcomes (needs_continuation=false ends the round)
- answered: simple answer, in result.
- completed / failed: task final report in report; when resubmittable=true you may issue a fresh assign_task_to_human with a revised task_description. When feedback_requested=true, call get_feedback_from_user to collect the human's note.
- declined (human_declined): the human refused THIS task. Do not reissue it unchanged; re-plan or reword.
- declined (do_not_disturb): "message" — DND is on. Wait, route around it, or wind down politely; do not re-ring repeatedly.
- cancelled: the round was cancelled (possibly by your own cancel_interaction).
- not_accepted (ring_timeout): nobody picked up within the ring window. Retry after retry_after_seconds, or re-plan.
- disconnected (terminal_disconnected): the human accepted but their terminal went away. Retryable after retry_after_seconds.
- unreachable (no_available_channel): the human could not be reached on any channel; check health_check before retrying.
- busy: the single line is occupied (possibly by your own unfinished round). Wait retry_after_seconds and call again.

4. Abandonment guard
- If the human accepted but only heartbeats return for many rounds (accepted with no progress), the round will NOT end on its own. After several stalled rounds, call cancel_interaction with that round's interaction_id/call_id to release the line, then re-plan.

5. Result budget and pagination
- Pass max_result_bytes (>=16384) when creating a call. Oversized results come back with body_omitted_for_size and a result_id; fetch the full body with read_interaction_result (paginate via cursor) and attachments with read_interaction_attachment (paginate via offset_bytes). Never ask the human to resend what the server already holds.

6. Multiple operators
- This service may serve several humans, each addressed by a short callsign. Call list_operators first: it returns every callsign with display name, language, mobile capability, and current do-not-disturb state.
- Pass callsign on the FIRST call of a round (interactive tools) to choose who is called; omit it to target the primary operator. Continuation calls do not need callsign — the round is already bound to its operator.
- Each operator has their OWN busy line, DND and quiet window: one busy operator does not block calls to another, and DND of one operator only declines calls addressed to them.
- unknown_callsign in a response means the callsign is not in the directory; call list_operators to see valid ones.

7. Call discipline
- Retries are safe: re-sending the same call after a network drop replays its recorded outcome instead of duplicating work. You need no request_id.
- One call, one thing: ask with input/choice tools, delegate with assign_task_to_human (task_description = a single actionable instruction). Never ask questions through the task tool.
- Business errors (missing_call_id / session_not_found / invalid_cursor, ...) arrive in structuredContent; protocol errors arrive as JSON-RPC errors.
"""

def _interactive_schema(properties: Mapping[str, object]) -> dict[str, object]:
    """Interactive-tool schema: specific fields first, then shared round fields."""
    common: dict[str, object] = {
        "client_timeout_seconds": {
            "type": "integer",
            "description": "How long this single call parks server-side waiting for the human "
                           "(default 30; use a larger value such as 300 if the human may be away). "
                           "If the human answers within the window, this same call returns their final answer.",
        },
        "callsign": {
            "type": "string",
            "description": "Which operator to address, from list_operators (e.g. \"kefan\"). "
                           "Omit to target the primary operator.",
        },
        "interaction_id": {
            "type": "string",
            "description": "Continuation: the interaction_id from the previous response of this round.",
        },
        "call_id": {
            "type": "string",
            "description": "Continuation: the call_id from the previous response (pass together with interaction_id).",
        },
        "after_event_seq": {
            "type": "integer",
            "description": "Continuation cursor: echo next_after_event_seq from the previous response. "
                           "Keep it unchanged while responses are plain heartbeats.",
        },
        "max_result_bytes": {
            "type": "integer",
            "description": "Result body budget in bytes (minimum 16384). Oversized bodies are omitted "
                           "and served by read_interaction_result instead.",
        },
    }
    return {"type": "object", "properties": {**properties, **common}}


_MCP_TOOLS = (
    {"name": "get_user_input", "kind": "interaction",
     "description": "Ask the operator one short question and WAIT: the call parks server-side for up to "
                    "client_timeout_seconds and returns the operator's answer in this same call. "
                    "Read get_human_loop_prompt before your first interactive call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string", "description": "The question shown to the operator."},
         "required": {"type": "boolean", "description": "Whether an answer is mandatory."}})},
    {"name": "get_number_input", "kind": "interaction",
     "description": "Ask the operator for a number and WAIT: the call parks server-side and returns the "
                    "answer in this same call. Read get_human_loop_prompt before first use.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string", "description": "The question shown to the operator."},
         "required": {"type": "boolean"}})},
    {"name": "get_user_choice", "kind": "interaction",
     "description": "Offer the operator a set of options and WAIT: the call parks server-side and returns "
                    "their selection in this same call. Read get_human_loop_prompt before first use.",
     "inputSchema": _interactive_schema({
         "choices": {"type": "array", "description": "The options to choose from."},
         "prompt": {"type": "string"},
         "multiple": {"type": "boolean", "description": "Allow selecting more than one option."}})},
    {"name": "get_multiline_input", "kind": "interaction",
     "description": "Ask the operator for a long free-form answer and WAIT: the call parks server-side "
                    "and returns the answer in this same call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string"}})},
    {"name": "show_confirmation_dialog", "kind": "interaction",
     "description": "Ask the operator to confirm or deny one proposal and WAIT: the call parks "
                    "server-side and returns their decision in this same call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string", "description": "The proposal to confirm."}})},
    {"name": "show_info_message", "kind": "interaction",
     "description": "Show the operator one message and WAIT for their acknowledgement.",
     "inputSchema": _interactive_schema({
         "message": {"type": "string"}})},
    {"name": "get_feedback_from_user", "kind": "interaction",
     "description": "Collect free-form feedback from the operator and WAIT: the call parks server-side "
                    "and returns the feedback in this same call.",
     "inputSchema": _interactive_schema({
         "prompt": {"type": "string"}})},
    {"name": "assign_task_to_human", "kind": "interaction",
     "description": "Hand the operator one physical/real-world task and WAIT: the call parks server-side; "
                    "the operator accepts, executes, and reports back, and this same call returns their "
                    "final report. Use for doing, not for asking questions.",
     "inputSchema": _interactive_schema({
         "task_description": {"type": "string", "description": "One actionable instruction for the operator."}})},
    {"name": "read_interaction_result", "kind": "read",
     "description": "Read the full body of a saved result (pagination via cursor) when a response came "
                    "back with body_omitted_for_size.",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "result_id": {"type": "string"},
         "cursor": {"type": "string"}, "max_result_bytes": {"type": "integer"}}}},
    {"name": "read_interaction_attachment", "kind": "read",
     "description": "Read one byte chunk of a saved attachment (pagination via offset_bytes).",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "attachment_id": {"type": "string"},
         "offset_bytes": {"type": "integer"}, "max_result_bytes": {"type": "integer"}}}},
    {"name": "list_operators", "kind": "readonly",
     "description": "List the human operators (callsigns) this service can reach, with display "
                    "names, languages, and whether each currently has do-not-disturb on. "
                    "Use a callsign as the callsign argument of interactive tools.",
     "inputSchema": {"type": "object"}},
    {"name": "get_operator_profile", "kind": "readonly",
     "description": "Read one operator's profile (pass callsign; omit for the primary operator): "
                    "who they are and how to address them.",
     "inputSchema": {"type": "object", "properties": {
         "callsign": {"type": "string", "description": "Operator callsign from list_operators."}}}},
    {"name": "health_check", "kind": "readonly",
     "description": "Report HITL service health (storage, desktop online, push channels).",
     "inputSchema": {"type": "object"}},
    {"name": "get_human_loop_prompt", "kind": "readonly",
     "description": "Return the HITL operating manual (wait model, continuation cursor, outcomes). "
                    "Call this before your first interactive tool call.",
     "inputSchema": {"type": "object"}},
    {"name": "cancel_interaction", "kind": "control",
     "description": "End one open round (yours or the human's) to release the single line, e.g. when the "
                    "human accepted but has stalled for many rounds.",
     "inputSchema": {"type": "object", "properties": {
         "interaction_id": {"type": "string"}, "call_id": {"type": "string"}}}},
)


class McpRpcMixin:
    """JSON-RPC tools/call dispatch, mixed into the transport Application."""

    def _rpc(self, caller: str, body: Mapping[str, object], session: str | None = None) -> dict[str, object]:
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
                result = self._rpc_tools_call(caller, body.get("params"), rid, session)
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
            "instructions": _HUMAN_LOOP_MANUAL,
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

    def _rpc_tools_call(self, caller: str, params: object, rid: object, session: str | None = None) -> dict[str, object]:
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
            structured = self._rpc_interaction(caller, name, arguments, rid, session)
        elif kind == "control":
            structured = self._rpc_cancel(caller, arguments, rid, session)
        elif kind == "read":
            structured = self._rpc_read_tool(caller, name, arguments)
        else:
            structured = self._rpc_readonly(name, arguments)
        after = arguments.get("after_event_seq")
        cursor_hint = after if isinstance(after, int) and not isinstance(after, bool) and after >= 0 else 0
        if isinstance(structured, dict) and structured.get("needs_continuation") and isinstance(structured.get("interaction_id"), str):
            seq = structured.get("event_seq")
            structured = dict(structured)
            structured["next_after_event_seq"] = seq if isinstance(seq, int) and seq >= 0 else cursor_hint
        return {
            "content": [{"type": "text", "text": self._rpc_summary(name, structured, cursor_hint)}],
            "isError": False,
            "structuredContent": structured,
        }

    def _rpc_interaction(self, caller: str, name: str, arguments: Mapping[str, object], rid: object, session: str | None = None) -> dict[str, object]:
        request_id = self._rpc_request_id(rid, name, arguments, session)
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        interaction_present = isinstance(interaction_id, str) and bool(interaction_id)
        call_present = isinstance(call_id, str) and bool(call_id)
        if interaction_present != call_present:
            return {"success": False, "status": "missing_call_id", "needs_continuation": False}
        if interaction_present:
            after = arguments.get("after_event_seq", 0)
            if isinstance(after, bool) or not isinstance(after, int):
                raise _RpcError(-32602, "Invalid params")
            result = self.lifecycle.continue_call(caller, request_id, interaction_id, call_id, after)
        else:
            max_result_bytes = arguments.get("max_result_bytes")
            if max_result_bytes is not None and (
                isinstance(max_result_bytes, bool) or not isinstance(max_result_bytes, int)
                or max_result_bytes < 16384
            ):
                return {"success": False, "status": "invalid_result_budget", "needs_continuation": False}
            callsign = arguments.get("callsign")
            if callsign is not None and not isinstance(callsign, str):
                return {"success": False, "status": "invalid_request", "needs_continuation": False}
            payload = {"tool": name, **{k: v for k, v in dict(arguments).items() if k != "callsign"}}
            result = self.lifecycle.create(
                caller, request_id, payload, self._invited_desktop_id(callsign), self._mobile_eligible(callsign),
                operator_callsign=callsign,
            )
        if result is None:
            result = {"success": False, "status": "session_not_found", "needs_continuation": False}
        return dict(result)

    def _rpc_cancel(self, caller: str, arguments: Mapping[str, object], rid: object, session: str | None = None) -> dict[str, object]:
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        if not isinstance(interaction_id, str) or not interaction_id or not isinstance(call_id, str) or not call_id:
            raise _RpcError(-32602, "Invalid params")
        return dict(self.lifecycle.cancel(caller, self._rpc_request_id(rid, "cancel_interaction", arguments, session), interaction_id, call_id))

    @staticmethod
    def _rpc_request_id(rid: object, name: str, arguments: Mapping[str, object],
                        session: str | None = None) -> str:
        """Namescope a JSON-RPC id with its payload fingerprint.

        JSON-RPC request ids are unique only within one client session, but
        the idempotency store keys them forever.  MCP hosts restart their
        id counter on every reconnect (initialize=1, tools/list=2, ...), so
        a fresh session inevitably reuses small ids.  Hashing the payload
        into the request id keeps genuine retries (same id, same payload)
        replaying the cached outcome while a reused id carrying different
        arguments executes as the new request it actually is, instead of
        poisoning the client with ``request_conflict``.
        """
        fingerprint = hashlib.sha1(
            f"{name}|{json.dumps(dict(arguments), sort_keys=True, separators=(',', ':'))}".encode()
        ).hexdigest()[:8]
        # The MCP streamable-HTTP session id (echoed by the client after
        # initialize) scopes ids to one client session: genuine retries
        # replay, while a fresh session never collides with -- or replays --
        # an earlier session's row even when both reuse small ids with
        # identical payloads.  Clients that never echo a session id fall
        # back to the bare payload fingerprint.
        prefix = f"{session[:12]}-" if isinstance(session, str) and session else ""
        return f"rpc-{prefix}{rid}-{fingerprint}"

    def _rpc_read_tool(self, caller: str, name: str, arguments: Mapping[str, object]) -> dict[str, object]:
        if name == "read_interaction_result":
            interaction_id = arguments.get("interaction_id")
            if not isinstance(interaction_id, str) or not interaction_id:
                raise _RpcError(-32602, "Invalid params")
            return self.lifecycle.read_interaction_result(
                caller, interaction_id,
                result_id=arguments.get("result_id"),
                cursor=arguments.get("cursor"),
                max_result_bytes=arguments.get("max_result_bytes"),
            )
        if name == "read_interaction_attachment":
            interaction_id = arguments.get("interaction_id")
            attachment_id = arguments.get("attachment_id")
            if not isinstance(interaction_id, str) or not interaction_id or not isinstance(attachment_id, str) or not attachment_id:
                raise _RpcError(-32602, "Invalid params")
            return self.lifecycle.read_interaction_attachment(
                caller, interaction_id, attachment_id,
                offset_bytes=arguments.get("offset_bytes", 0),
                max_result_bytes=arguments.get("max_result_bytes"),
            )
        raise _RpcError(-32602, "Invalid params")

    def _rpc_readonly(self, name: str, arguments: Mapping[str, object] | None = None) -> dict[str, object]:
        if name == "list_operators":
            listing = self.lifecycle.directory_view()
            for entry in listing:
                dnd = self.lifecycle.get_dnd(operator_id=str(entry["callsign"]))
                entry["dnd"] = bool(dnd.get("enabled"))
                entry["dnd_message"] = dnd.get("message", "")
            return {"success": True, "status": "ok", "result": {"operators": listing}}
        if name == "get_operator_profile":
            callsign = arguments.get("callsign") if isinstance(arguments, Mapping) else None
            target = self.lifecycle.resolve_operator(callsign)
            if target is None:
                return {"success": False, "status": "unknown_callsign", "needs_continuation": False,
                        "message": f"no operator with callsign {callsign!r}; call list_operators"}
            entry = self.lifecycle._operators.get(target, {})
            profile: dict[str, object] = {"operator_id": target, "callsign": target}
            for field in ("display_name", "language", "timezone", "notes"):
                if entry.get(field) is not None:
                    profile[field] = entry[field]
            if not any(k in profile for k in ("display_name", "language", "timezone", "notes")):
                profile.update(self.lifecycle._operator_profile)
            profile["primary"] = bool(entry.get("primary"))
            return {"success": True, "status": "ok", "result": profile}
        if name == "health_check":
            router = self._notification_router
            desktop_online = False
            bark_configured = False
            recent_outcome = None
            if router is not None:
                online = getattr(router, "desktop_online", None)
                desktop_online = bool(online()) if callable(online) else False
                configured = getattr(router, "bark_configured", None)
                bark_configured = bool(configured()) if callable(configured) else False
                recent = getattr(router, "recent_bark_delivery", None)
                recent_outcome = recent() if callable(recent) else None
            return {
                "success": True, "status": "healthy", "storage": "sqlite", "scheduler": "ready",
                "desktop_online": desktop_online,
                "bark": {"configured": bark_configured, "recent_outcome": recent_outcome},
            }
        if name == "get_human_loop_prompt":
            return {"success": True, "status": "ok", "prompt": _HUMAN_LOOP_MANUAL}
        raise _RpcError(-32602, "Invalid params")

    @staticmethod
    def _rpc_summary(name: str, structured: Mapping[str, object], cursor_hint: int = 0) -> str:
        status = structured.get("status", structured.get("call_state", "ok"))
        iid = structured.get("interaction_id")
        cid = structured.get("call_id")
        needs = structured.get("needs_continuation")
        result = structured.get("result")
        if needs and iid and cid:
            # Cursor rule: a response that carries an event advances the
            # cursor to that event_seq; a heartbeat (no new event) keeps the
            # cursor the caller already used.  Never fall back to 0 — a
            # reset re-drains the oldest event forever and hides every later
            # one (accept, progress, cancel) from the agent.
            nxt = structured.get("next_after_event_seq")
            seq = structured.get("event_seq")
            cursor = (nxt if isinstance(nxt, int) and nxt >= 0
                      else seq if isinstance(seq, int) and seq >= 0
                      else max(0, cursor_hint))
            state = structured.get("call_state")
            if status == "opened" or state == "accepted":
                meaning = "The human ACCEPTED this task and is working on it."
            elif status == "in_progress" or state == "in_progress":
                meaning = "The human is still working (progress report included)." if result is not None else "The human is still working."
            else:
                meaning = "The human has not picked up yet. The task is NOT done and NOT failed."
            summary = (
                f"{name}: still waiting ({status}). {meaning} "
                f"Keep waiting: call {name} again with interaction_id={iid}, call_id={cid}, "
                f"after_event_seq={cursor} (keep exactly this value until a response contains a new event_seq), "
                f"client_timeout_seconds=30. Each call already waits server-side; do not re-call immediately and do not conclude the task while it is still open."
            )
            if result is not None:
                summary += f" Partial result: {json.dumps(result, ensure_ascii=False)}"
            return summary
        if result is not None:
            summary = f"{name}: {status}. Result: {json.dumps(result, ensure_ascii=False)}"
            attachments = structured.get("attachments")
            if isinstance(attachments, list) and attachments:
                listed = ", ".join(
                    f"{a.get('filename', '?')} ({a.get('byte_count', '?')} B, id {str(a.get('attachment_id', ''))[:8]})"
                    for a in attachments if isinstance(a, Mapping))
                summary += (f" Attachments: {listed}. Fetch their bytes with read_interaction_attachment "
                            f"(interaction_id + attachment_id, paginate with offset_bytes).")
            return summary
        detail = ""
        reason = structured.get("reason")
        message = structured.get("message")
        if isinstance(reason, str) and reason:
            detail = f" ({reason})"
        if isinstance(message, str) and message:
            detail += f": {message}"
        if structured.get("success") is False:
            guidance = {
                "busy": f"another call holds the single line; wait {structured.get('retry_after_seconds', 30)}s and call again",
                "invalid_cursor": "after_event_seq is past the event history; call again with the newest event_seq you received, or 0",
                "session_not_found": "unknown or expired round; start a fresh call without continuation ids",
                "missing_call_id": "pass interaction_id and call_id together, or omit both to start a new round",
                "request_conflict": "this request was already recorded with different arguments; retry unchanged or start fresh",
                "expired": "the round already ended; start a fresh call",
            }.get(str(status))
            if guidance:
                detail += f" — {guidance}"
        if "desktop_online" in structured:
            bark = structured.get("bark")
            detail += (f" — desktop {'online' if structured.get('desktop_online') else 'OFFLINE'}, "
                       f"bark push {'configured' if isinstance(bark, Mapping) and bark.get('configured') else 'not configured'}")
        return f"{name}: {status}{detail}"

    @staticmethod
    def _rpc_error(rid: object, code: int, message: str) -> dict[str, object]:
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": code, "message": message}}
