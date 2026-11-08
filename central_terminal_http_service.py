"""Small GUI-free ASGI boundary for authenticated terminal call actions."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_terminal_call_service import TerminalCallService


def create_app(
    *,
    terminal_service: TerminalCallService,
    desktop_registration_service: DesktopRegistrationStateService | None = None,
) -> Callable[..., Awaitable[None]]:
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

        status, result = _dispatch(
            terminal_service, desktop_registration_service, method, path, headers, payload
        )
        encoded = json.dumps(result, sort_keys=True).encode()
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
        await send({"type": "http.response.body", "body": encoded})
    return app


def _dispatch(
    service: TerminalCallService,
    desktop_registration_service: DesktopRegistrationStateService | None,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> tuple[int, dict[str, object]]:
    if desktop_registration_service is not None:
        registration = _dispatch_desktop_registration(
            desktop_registration_service, method, path, headers, body
        )
        if registration is not None:
            return registration
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


def _dispatch_desktop_registration(
    service: DesktopRegistrationStateService,
    method: str,
    path: str,
    headers: dict[str, str],
    body: dict[str, Any] | None,
) -> tuple[int, dict[str, object]] | None:
    if path == "/api/terminals/register" and method == "POST":
        if not _is_gui_ready_body(body, include_endpoint=True):
            return 400, {"success": False, "status": "invalid_request"}
        credential = _terminal_credential(headers)
        if credential is None:
            return 401, {"success": False, "status": "terminal_not_authenticated"}
        result = service.register(
            credential=credential, endpoint=str(body["endpoint"]), gui_ready=bool(body["gui_ready"])
        )
        if not result.get("success"):
            return _registration_failure(result)
        return 201, {
            "success": True,
            "status": "registered",
            "session_id": result["session_id"],
        }
    if path == "/api/terminals/heartbeat" and method == "POST":
        if not _is_gui_ready_body(body, include_endpoint=False):
            return 400, {"success": False, "status": "invalid_request"}
        return _desktop_session_result(
            service.heartbeat(session_id=headers.get("x-terminal-session", ""), gui_ready=bool(body["gui_ready"]))
        )
    if path == "/api/terminals/state" and method == "GET":
        return _desktop_session_result(service.states(session_id=headers.get("x-terminal-session", "")))
    return None


def _terminal_credential(headers: dict[str, str]) -> str | None:
    authorization = headers.get("authorization", "")
    if not authorization.startswith("Terminal "):
        return None
    credential = authorization.removeprefix("Terminal ")
    return credential or None


def _is_gui_ready_body(body: dict[str, Any] | None, *, include_endpoint: bool) -> bool:
    required = {"endpoint", "gui_ready"} if include_endpoint else {"gui_ready"}
    return (
        isinstance(body, dict)
        and set(body) == required
        and isinstance(body.get("gui_ready"), bool)
        and (not include_endpoint or isinstance(body.get("endpoint"), str))
    )


def _registration_failure(result: dict[str, object]) -> tuple[int, dict[str, object]]:
    if result.get("status") == "terminal_not_authenticated":
        return 401, {"success": False, "status": "terminal_not_authenticated"}
    return 404, {"success": False, "status": "endpoint_not_preconfigured"}


def _desktop_session_result(result: dict[str, object]) -> tuple[int, dict[str, object]]:
    if result.get("success"):
        return 200, result
    return 404, {"success": False, "status": "session_not_found"}


def _read(service: TerminalCallService, call_id: str, headers: dict[str, str]) -> tuple[int, dict[str, object]]:
    session = headers.get("x-terminal-session")
    if not session:
        return 404, {"success": False, "status": "session_not_found"}
    result = service.read_mobile_state(call_id=call_id, session_id=session)
    return (200 if result.get("success") else 404), result
