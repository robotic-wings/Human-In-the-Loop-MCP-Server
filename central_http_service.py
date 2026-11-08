"""GUI-free HTTP ingress for the central call coordinator.

This deliberately narrow adapter owns configuration validation and the
``POST /mcp`` boundary.  It does not provide a user interface, notification
transport, or the complete MCP wire protocol; those remain separate slices.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import httpx
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from central_call_coordinator import CallCoordinator
from central_desktop_registration_sync import DesktopRegistrationStateService
from central_mcp_waiting_lifecycle import Application, create_app as create_lifecycle_asgi_app
from central_notification_router import NotificationRouter, TerminalRegistry


Clock = Callable[[], float]
Notification = Callable[[dict[str, object]], None]
Waiter = Callable[[float], Awaitable[None]]


@dataclass(frozen=True)
class CentralServiceConfig:
    """Portable configuration required by the central HTTP entry."""

    listen_host: str
    listen_port: int
    web_base_url: str
    data_dir: Path
    agent_credentials: Mapping[str, str]
    tls_cert_file: str
    tls_key_file: str
    route_mode: str = "iphone"
    desktop_credentials: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    desktop_endpoints: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    bark_endpoint: str | None = None
    bark_credential: str | None = None
    operator_sessions: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, mapping: Mapping[str, object]) -> "CentralServiceConfig":
        def required_text(name: str) -> str:
            value = mapping.get(name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"{name} is required")
            return value.strip()

        listen_host = required_text("listen_host")
        port = mapping.get("listen_port")
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError("listen_port must be an integer from 1 through 65535")

        web_base_url = required_text("web_base_url")
        parsed_url = urlparse(web_base_url)
        if parsed_url.scheme != "https" or not parsed_url.netloc:
            raise ValueError("web_base_url must be an https URL")

        data_dir_text = required_text("data_dir")
        data_dir = Path(data_dir_text)
        if data_dir.exists() and not data_dir.is_dir():
            raise ValueError("data_dir must name a directory")

        raw_credentials = mapping.get("agent_credentials")
        if not isinstance(raw_credentials, Mapping) or not raw_credentials:
            raise ValueError("agent_credentials must contain at least one credential")
        credentials: dict[str, str] = {}
        for caller_id, credential in raw_credentials.items():
            if not isinstance(caller_id, str) or not caller_id or not isinstance(credential, str) or not credential:
                raise ValueError("agent_credentials must map non-empty caller IDs to credentials")
            credentials[caller_id] = credential

        tls_cert_file = required_text("tls_cert_file")
        tls_key_file = required_text("tls_key_file")

        # The legacy ingress remains constructible for its isolated contract;
        # lifecycle composition validates routes only when that factory is
        # explicitly selected.  A configured desktop route still requires
        # its complete management-owned terminal records.
        route_mode = mapping.get("route_mode", "iphone")
        if route_mode not in {"desktop", "iphone", "both"}:
            raise ValueError("route_mode must be desktop, iphone, or both")

        raw_desktop_credentials = mapping.get("desktop_credentials")
        raw_desktop_endpoints = mapping.get("desktop_endpoints")
        desktop_credentials: dict[str, dict[str, str]] = {}
        desktop_endpoints: dict[str, dict[str, str]] = {}
        if route_mode in {"desktop", "both"}:
            if not isinstance(raw_desktop_credentials, Mapping) or not raw_desktop_credentials:
                raise ValueError("desktop_credentials are required for a desktop route")
            if not isinstance(raw_desktop_endpoints, Mapping) or not raw_desktop_endpoints:
                raise ValueError("desktop_endpoints are required for a desktop route")
            for credential, metadata in raw_desktop_credentials.items():
                if not isinstance(credential, str) or not credential or not isinstance(metadata, Mapping):
                    raise ValueError("desktop_credentials must contain terminal metadata")
                device_id, platform = metadata.get("device_id"), metadata.get("terminal_platform")
                if not isinstance(device_id, str) or not device_id or not isinstance(platform, str) or not platform:
                    raise ValueError("desktop credential metadata is invalid")
                desktop_credentials[credential] = {"device_id": device_id, "terminal_platform": platform}
            for device_id, endpoint in raw_desktop_endpoints.items():
                if not isinstance(device_id, str) or not device_id or not isinstance(endpoint, Mapping):
                    raise ValueError("desktop_endpoints must contain endpoint metadata")
                address, callback = endpoint.get("endpoint"), endpoint.get("callback_credential")
                if not isinstance(address, str) or not address or not isinstance(callback, str) or not callback:
                    raise ValueError("desktop endpoint metadata is invalid")
                desktop_endpoints[device_id] = {"endpoint": address, "callback_credential": callback}
            if {metadata["device_id"] for metadata in desktop_credentials.values()} != set(desktop_endpoints):
                raise ValueError("desktop credential and endpoint devices must match")

        bark_endpoint = mapping.get("bark_endpoint")
        bark_credential = mapping.get("bark_credential")
        if bark_endpoint is not None and (not isinstance(bark_endpoint, str) or not bark_endpoint.strip()):
            raise ValueError("bark_endpoint must be a non-empty URL")
        if bark_credential is not None and (not isinstance(bark_credential, str) or not bark_credential):
            raise ValueError("bark_credential must be non-empty")
        raw_sessions = mapping.get("operator_sessions", {})
        if not isinstance(raw_sessions, Mapping):
            raise ValueError("operator_sessions must be a mapping")
        operator_sessions: dict[str, dict[str, str]] = {}
        for token, session in raw_sessions.items():
            if not isinstance(token, str) or not token or not isinstance(session, Mapping):
                raise ValueError("operator_sessions must contain session metadata")
            operator_id, session_id = session.get("operator_id"), session.get("browser_session_id")
            if not isinstance(operator_id, str) or not operator_id or not isinstance(session_id, str) or not session_id:
                raise ValueError("operator session metadata is invalid")
            operator_sessions[token] = {"operator_id": operator_id, "browser_session_id": session_id}

        return cls(
            listen_host=listen_host,
            listen_port=port,
            web_base_url=web_base_url,
            data_dir=data_dir,
            agent_credentials=credentials,
            tls_cert_file=tls_cert_file,
            tls_key_file=tls_key_file,
            route_mode=route_mode,
            desktop_credentials=desktop_credentials,
            desktop_endpoints=desktop_endpoints,
            bark_endpoint=bark_endpoint.strip() if isinstance(bark_endpoint, str) else None,
            bark_credential=bark_credential,
            operator_sessions=operator_sessions,
        )


class CentralHttpApplication:
    """A small ASGI app with an observable response-start/wait-leg boundary."""

    def __init__(
        self,
        config: CentralServiceConfig,
        *,
        clock: Clock,
        notify: Notification,
        wait: Waiter,
    ) -> None:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        self._config = config
        self._clock = clock
        self._wait = wait
        self._coordinator = CallCoordinator(
            database_path=config.data_dir / "coordinator.sqlite3",
            clock=clock,
            notify=notify,
        )

    async def __call__(self, scope: Mapping[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            raise RuntimeError("central service only supports HTTP")
        if scope.get("method") != "POST" or scope.get("path") != "/mcp":
            await self._send_json(send, 404, {"success": False, "status": "not_found"})
            return

        caller_id = self._caller_from_headers(scope.get("headers", []))
        if caller_id is None:
            await self._send_json(send, 401, {"success": False, "status": "unauthorized"})
            return
        payload = await self._read_json(receive)
        if payload is None:
            await self._send_json(send, 400, {"success": False, "status": "invalid_request"})
            return
        timeout = payload.get("client_timeout_seconds")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout < 2:
            await self._send_json(
                send, 400, {"success": False, "status": "invalid_client_timeout"}
            )
            return

        status, response = self._dispatch(caller_id, payload)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            }
        )
        if status == 200 and response.get("needs_continuation"):
            await self._wait(self._wait_leg_seconds(float(timeout)))
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(response, separators=(",", ":")).encode("utf-8"),
                "more_body": False,
            }
        )

    def _dispatch(
        self, caller_id: str, request: Mapping[str, object]
    ) -> tuple[int, dict[str, object]]:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return 400, {
                "success": False,
                "status": "invalid_request",
                "needs_continuation": False,
            }
        interaction_id = request.get("interaction_id")
        call_id = request.get("call_id")
        if interaction_id is not None or call_id is not None:
            if not isinstance(interaction_id, str) or not isinstance(call_id, str):
                return 400, self._hidden_continuation_error("missing_interaction_id")
            call = self._coordinator.get_call_for_caller(
                caller_id=caller_id, call_id=call_id
            )
            if call is None:
                return 404, self._hidden_continuation_error("session_not_found")
            if interaction_id != self._interaction_id_for(call_id):
                return 400, self._hidden_continuation_error("mismatched_interaction_id")
            return 200, self._heartbeat(interaction_id, call_id, call)

        body = request.get("payload")
        if not isinstance(body, Mapping):
            return 400, {
                "success": False,
                "status": "invalid_request",
                "needs_continuation": False,
            }
        outcome = self._coordinator.create_call(
            caller_id=caller_id, request_id=request_id, payload=body
        )
        if outcome.get("status") == "busy":
            return 200, outcome
        if outcome.get("status") == "request_conflict":
            return 409, outcome
        call_id_value = outcome["call_id"]
        assert isinstance(call_id_value, str)
        stable_interaction_id = self._interaction_id_for(call_id_value)
        return 200, self._heartbeat(
            stable_interaction_id, call_id_value, outcome, caller_id=caller_id
        )

    @staticmethod
    def _interaction_id_for(call_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"hitl:{call_id}"))

    @staticmethod
    def _hidden_continuation_error(status: str) -> dict[str, object]:
        return {"success": False, "status": status, "needs_continuation": False}

    @staticmethod
    def _heartbeat(
        interaction_id: str,
        call_id: str,
        outcome: Mapping[str, object],
        *,
        caller_id: str | None = None,
    ) -> dict[str, object]:
        state = outcome.get("call_state")
        if state == "ringing":
            return {
                "success": True,
                "status": "heartbeat",
                "call_state": "ringing",
                "interaction_id": interaction_id,
                "call_id": call_id,
                "caller_id": caller_id,
                "needs_continuation": True,
            }
        return {
            "success": True,
            "status": str(state or "heartbeat"),
            "call_state": state,
            "interaction_id": interaction_id,
            "call_id": call_id,
            "needs_continuation": state in {"ringing", "accepted"},
        }

    def _caller_from_headers(self, headers: object) -> str | None:
        if not isinstance(headers, list):
            return None
        authorization: str | None = None
        for item in headers:
            if not isinstance(item, tuple) or len(item) != 2:
                continue
            name, value = item
            if bytes(name).lower() == b"authorization":
                authorization = bytes(value).decode("latin-1")
                break
        if authorization is None or not authorization.startswith("Bearer "):
            return None
        presented = authorization.removeprefix("Bearer ")
        for caller_id, credential in self._config.agent_credentials.items():
            if hmac.compare_digest(presented, credential):
                return caller_id
        return None

    @staticmethod
    async def _read_json(receive: Any) -> dict[str, object] | None:
        chunks: list[bytes] = []
        while True:
            event = await receive()
            if event.get("type") != "http.request":
                return None
            body = event.get("body", b"")
            if not isinstance(body, bytes):
                return None
            chunks.append(body)
            if not event.get("more_body", False):
                break
        try:
            value = json.loads(b"".join(chunks))
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    @staticmethod
    def _wait_leg_seconds(timeout: float) -> float:
        return max(1, timeout - min(30, max(1, timeout // 4)))

    @staticmethod
    async def _send_json(send: Any, status: int, payload: Mapping[str, object]) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            }
        )
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                "more_body": False,
            }
        )


def create_app(
    config: CentralServiceConfig,
    *,
    clock: Clock,
    notify: Notification,
    wait: Waiter,
) -> CentralHttpApplication:
    """Build the testable HTTP-only central ingress application."""

    return CentralHttpApplication(config, clock=clock, notify=notify, wait=wait)


def create_lifecycle_app(
    config: CentralServiceConfig,
    *,
    clock: Clock,
    wait: Waiter,
    bark_dispatch: Callable[[dict[str, object]], object],
    desktop_dispatch: Callable[[dict[str, object]], object],
    operator_session_verifier: Any,
    scheduler_sleep: Waiter = asyncio.sleep,
    notification_scheduler_sleep: Waiter = asyncio.sleep,
) -> Application:
    """Compose the one durable lifecycle authority used by the HTTP service.

    Transport callables and browser authentication are injected boundaries so
    production configuration never turns a notification attempt into a second
    call-state authority.
    """
    if operator_session_verifier is None:
        raise ValueError("operator_session_verifier is required")
    config.data_dir.mkdir(parents=True, exist_ok=True)
    database_path = config.data_dir / "lifecycle.sqlite3"
    registry = TerminalRegistry(database_path=database_path, clock=clock)
    for device_id, endpoint in config.desktop_endpoints.items():
        registry.register_desktop(
            actor_role="management",
            device_id=device_id,
            endpoint=endpoint["endpoint"],
            callback_credential=endpoint["callback_credential"],
            gui_ready=False,
        )

    lifecycle_holder: dict[str, Application] = {}

    def lifecycle_call_state(call_id: str) -> str | None:
        """Read state from the lifecycle's durable authority for routing."""
        application = lifecycle_holder.get("app")
        if application is None:
            return None
        with application.lifecycle._connection() as conn:
            row = conn.execute(
                "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return str(row["call_state"]) if row is not None else None

    router = NotificationRouter(
        database_path=database_path,
        clock=clock,
        registry=registry,
        desktop_dispatch=desktop_dispatch,
        bark_dispatch=bark_dispatch,
        route_mode=config.route_mode,
        call_state=lifecycle_call_state,
        web_base_url=config.web_base_url,
    )
    registration = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        registered_credentials=config.desktop_credentials,
        device_state=lambda device_id: lifecycle_holder["app"].lifecycle.terminal_states(device_id),
    )
    app = create_lifecycle_asgi_app(
        database_path=database_path,
        credentials=config.agent_credentials,
        clock=clock,
        notify=router.enqueue,
        wait=wait,
        route_admission=router.validate_route,
        notification_router=router,
        scheduler_sleep=scheduler_sleep,
        notification_scheduler_sleep=notification_scheduler_sleep,
        desktop_registration_service=registration,
        operator_session_verifier=operator_session_verifier,
    )
    lifecycle_holder["app"] = app
    return app


class _ConfiguredOperatorSessionVerifier:
    """Minimal configuration-backed browser-session boundary for the runtime."""

    def __init__(self, sessions: Mapping[str, Mapping[str, str]]) -> None:
        self._sessions = {token: dict(session) for token, session in sessions.items()}

    def verify(self, token: str | None) -> dict[str, str] | None:
        if not isinstance(token, str):
            return None
        for expected, session in self._sessions.items():
            if hmac.compare_digest(token, expected):
                return dict(session)
        return None


def _runtime_dependencies(
    config: CentralServiceConfig,
) -> tuple[Callable[[dict[str, object]], object], Callable[[dict[str, object]], object], _ConfiguredOperatorSessionVerifier]:
    """Build bounded, configuration-backed transport and browser seams."""
    if config.route_mode in {"iphone", "both"} and (not config.bark_endpoint or not config.bark_credential):
        raise ValueError("bark_endpoint and bark_credential are required for an iPhone route")
    if config.route_mode in {"iphone", "both"} and not config.operator_sessions:
        raise ValueError("operator_sessions are required for an iPhone route")

    async def bark_dispatch(payload: dict[str, object]) -> dict[str, object]:
        if not config.bark_endpoint or not config.bark_credential:
            return {"http_status": 503, "business_success": False}
        body = {**payload, "device_key": config.bark_credential}
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.post(config.bark_endpoint, json=body)
        try:
            content = response.json()
        except (ValueError, json.JSONDecodeError):
            content = None
        business_success = bool(isinstance(content, Mapping) and content.get("code") in {200, "200"})
        return {"http_status": response.status_code, "business_success": business_success}

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        endpoint = payload.get("endpoint")
        credential = payload.get("callback_credential")
        if not isinstance(endpoint, str) or not endpoint or not isinstance(credential, str) or not credential:
            return {"accepted": False}
        body = {key: value for key, value in payload.items() if key not in {"endpoint", "callback_credential"}}
        async with httpx.AsyncClient(timeout=2.5) as client:
            response = await client.post(endpoint, json=body, headers={"x-central-callback": credential})
        return {"accepted": 200 <= response.status_code < 300}

    return bark_dispatch, desktop_dispatch, _ConfiguredOperatorSessionVerifier(config.operator_sessions)


def main(argv: list[str] | None = None) -> None:
    """Start the central HTTP entry, rejecting removed legacy modes explicitly."""

    parser = argparse.ArgumentParser(prog="hitl-central-service")
    parser.add_argument("--config", help="JSON configuration file")
    parser.add_argument("--stdio", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--local", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.stdio or args.local:
        parser.error("stdio and local modes have been removed; use the HTTP central service")
    if not args.config:
        parser.error("--config is required for the HTTP central service")
    try:
        config = CentralServiceConfig.from_mapping(
            json.loads(Path(args.config).read_text(encoding="utf-8"))
        )
    except (OSError, ValueError, json.JSONDecodeError) as error:
        parser.error(f"invalid central configuration: {error}")

    try:
        import uvicorn
    except ImportError as error:  # pragma: no cover - deployment dependency
        raise SystemExit("HTTP server dependency unavailable") from error

    async def real_wait(seconds: float) -> None:
        await asyncio.sleep(seconds)

    try:
        bark_dispatch, desktop_dispatch, operator_session_verifier = _runtime_dependencies(config)
    except ValueError as error:
        parser.error(f"invalid central configuration: {error}")

    app = create_lifecycle_app(
        config,
        clock=lambda: __import__("time").time(),
        wait=real_wait,
        bark_dispatch=bark_dispatch,
        desktop_dispatch=desktop_dispatch,
        operator_session_verifier=operator_session_verifier,
    )
    uvicorn.run(app, host=config.listen_host, port=config.listen_port, ssl_certfile=config.tls_cert_file, ssl_keyfile=config.tls_key_file)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
