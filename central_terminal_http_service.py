"""Small GUI-free ASGI boundary for authenticated terminal call actions."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from central_terminal_call_service import TerminalCallService


def create_app(*, terminal_service: TerminalCallService) -> Callable[..., Awaitable[None]]:
    async def app(scope: dict[str, Any], receive: Callable[..., Awaitable[dict[str, Any]]], send: Callable[..., Awaitable[None]]) -> None:
        if scope["type"] != "http":
            return
        method, path = scope["method"], scope["path"]
        headers = {key.decode().lower(): value.decode() for key, value in scope.get("headers", [])}
        body = b""
        while True:
            message = await receive()
            body += message.get("body", b"")
            if not message.get("more_body"):
                break
        payload: dict[str, Any] | None
        try:
            payload = json.loads(body) if body else None
        except json.JSONDecodeError:
            payload = None

        status, result = _dispatch(terminal_service, method, path, headers, payload)
        encoded = json.dumps(result, sort_keys=True).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": encoded})
    return app


def _dispatch(service: TerminalCallService, method: str, path: str, headers: dict[str, str], body: dict[str, Any] | None) -> tuple[int, dict[str, object]]:
    if path == "/api/terminal/sessions" and method == "POST":
        authorization = headers.get("authorization", "")
        if not authorization.startswith("Terminal "):
            return 401, {"success": False, "status": "terminal_not_authenticated"}
        result = service.authenticate(credential=authorization.removeprefix("Terminal "))
        return (201 if result.get("success") else 401), result
    parts = path.strip("/").split("/")
    if len(parts) == 2 and parts[0] == "calls" and method == "GET":
        return _read(service, parts[1], headers)
    if len(parts) == 3 and parts[:2] == ["api", "calls"] and method == "GET":
        return _read(service, parts[2], headers)
    if len(parts) == 4 and parts[:2] == ["api", "calls"] and parts[3] in {"accept", "decline"}:
        if method != "POST" or not isinstance(body, dict) or set(body) != {"operation_id"} or not isinstance(body["operation_id"], str):
            return 400, {"success": False, "status": "invalid_request"}
        session = headers.get("x-terminal-session")
        if not session:
            return 404, {"success": False, "status": "session_not_found"}
        result = (service.accept if parts[3] == "accept" else service.decline)(call_id=parts[2], session_id=session, operation_id=body["operation_id"])
        return (200 if result.get("success") else 404), result
    return 405, {"success": False, "status": "invalid_request"}


def _read(service: TerminalCallService, call_id: str, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    session = headers.get("x-terminal-session")
    if not session:
        return 404, {"success": False, "status": "session_not_found"}
    result = service.read_mobile_state(call_id=call_id, session_id=session)
    return (200 if result.get("success") else 404), result
