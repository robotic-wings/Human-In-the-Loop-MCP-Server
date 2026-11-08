"""GUI-free ASGI boundary for authenticated iPhone response views.

This module deliberately receives browser-session verification and rendering as
dependencies.  A call identifier is not a capability: every operation first
requires the injected operator browser session, which in turn supplies the
durable terminal session used by :class:`TerminalCallService`.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

from central_terminal_call_service import TerminalCallService


_HIDDEN = {"success": False, "status": "session_not_found"}


def create_app(
    *,
    terminal_service: TerminalCallService,
    operator_session_verifier: Any,
    renderer: Any,
) -> Callable[..., Awaitable[None]]:
    """Create the small operator-session-protected response-web adapter."""

    async def app(
        scope: dict[str, Any],
        receive: Callable[..., Awaitable[dict[str, Any]]],
        send: Callable[..., Awaitable[None]],
    ) -> None:
        if scope["type"] != "http":
            return
        headers = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in scope.get("headers", [])
        }
        body = await _body(receive)
        status, result = _dispatch(
            terminal_service, operator_session_verifier, renderer,
            scope["method"], scope["path"], headers, body,
        )
        await _reply(send, status, result)

    return app


async def _body(receive: Callable[..., Awaitable[dict[str, Any]]]) -> object:
    raw = b""
    while True:
        message = await receive()
        raw += message.get("body", b"")
        if not message.get("more_body"):
            break
    if not raw:
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


async def _reply(
    send: Callable[..., Awaitable[None]], status: int, value: Mapping[str, object]
) -> None:
    encoded = json.dumps(value, sort_keys=True).encode("utf-8")
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})
    await send({"type": "http.response.body", "body": encoded})


def _dispatch(
    service: TerminalCallService, verifier: Any, renderer: Any,
    method: str, path: str, headers: Mapping[str, str], body: object,
) -> tuple[int, dict[str, object]]:
    parts = path.strip("/").split("/")
    call_id: str | None = None
    action: str | None = None
    if len(parts) == 2 and parts[0] == "calls":
        call_id = parts[1]
    elif len(parts) == 3 and parts[:2] == ["api", "calls"]:
        call_id = parts[2]
    elif len(parts) == 3 and parts[0] == "calls" and parts[2] in {"accept", "decline"}:
        call_id, action = parts[1], parts[2]
    else:
        return 405, {"success": False, "status": "invalid_request"}

    session_id = _session_id(verifier, headers.get("x-operator-session"))
    if session_id is None:
        return 404, dict(_HIDDEN)
    if action is None:
        if method != "GET":
            return 405, {"success": False, "status": "invalid_request"}
        return _read(service, renderer, call_id, session_id)
    if method != "POST" or not _valid_operation(body):
        return 400, {"success": False, "status": "invalid_request"}
    operation_id = body["operation_id"]
    result = (service.accept if action == "accept" else service.decline)(
        call_id=call_id, session_id=session_id, operation_id=operation_id
    )
    return (200, result) if result.get("success") else (404, dict(_HIDDEN))


def _session_id(verifier: Any, token: str | None) -> str | None:
    verified = verifier.verify(token)
    if not isinstance(verified, Mapping):
        return None
    value = verified.get("terminal_session_id")
    return value if isinstance(value, str) and value else None


def _valid_operation(body: object) -> bool:
    return (
        isinstance(body, dict)
        and set(body) == {"operation_id"}
        and isinstance(body["operation_id"], str)
        and bool(body["operation_id"])
    )


def _read(
    service: TerminalCallService, renderer: Any, call_id: str, session_id: str
) -> tuple[int, dict[str, object]]:
    state = service.read_mobile_state(call_id=call_id, session_id=session_id)
    if not state.get("success"):
        return 404, dict(_HIDDEN)
    view = dict(state)
    # A completed or otherwise late mobile call is not an operator-browser
    # capability.  Do not let a guessed old link disclose it.  Desktop-owned
    # calls are the deliberate exception below: their safe truth view tells a
    # routed phone that the desktop is processing or has processed the call.
    if (
        view.get("call_state") not in {"ringing", "accepted"}
        and view.get("handled_on") != "desktop"
    ):
        return 404, dict(_HIDDEN)
    ringing = view.get("call_state") == "ringing"
    view["can_accept"] = ringing
    view["can_decline"] = ringing
    view["actions"] = ["accept", "decline"] if ringing else []
    if view.get("handled_on") == "desktop":
        view["display_state"] = (
            "handled_on_desktop_processing"
            if view.get("call_state") == "accepted"
            else "handled_on_desktop_processed"
        )
    rendered = renderer.render(view)
    return 200, dict(rendered)
