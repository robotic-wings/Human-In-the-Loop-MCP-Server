"""Minimal HTTPS entry point for the GUI-free central service.

This module intentionally owns only process startup and the public HTTPS
listener.  Call coordination and terminal workflows belong to later slices.
"""

from __future__ import annotations

import argparse
import json
import ssl
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn
from urllib.parse import urlparse, urlsplit

from call_coordinator import CallCoordinator, CreateCall


class ConfigurationError(ValueError):
    """Raised when startup configuration cannot safely create the listener."""


@dataclass(frozen=True)
class ServiceConfig:
    listen_host: str
    listen_port: int
    web_base_url: str
    data_dir: Path
    certfile: Path
    keyfile: Path
    agent_credentials_file: Path | None
    terminal_credentials_file: Path | None
    eligible_desktop_device_ids: tuple[str, ...]


def _required_string(config: dict[str, object], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"config requires a non-empty {name}")
    return value.strip()


def _load_config(config_path: Path) -> ServiceConfig:
    try:
        raw = json.loads(config_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise ConfigurationError(f"config cannot be read: {error}") from error
    except json.JSONDecodeError as error:
        raise ConfigurationError(f"config is not valid JSON: {error.msg}") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("config must be a JSON object")

    host = _required_string(raw, "listen_host")
    base_url = _required_string(raw, "web_base_url")
    data_dir = Path(_required_string(raw, "data_dir"))
    certfile = Path(_required_string(raw, "https_certfile"))
    keyfile = Path(_required_string(raw, "https_keyfile"))
    credentials_value = raw.get("agent_credentials_file")
    if credentials_value is None:
        credentials_file = None
    elif isinstance(credentials_value, str) and credentials_value.strip():
        credentials_file = Path(credentials_value.strip())
        if not credentials_file.is_file():
            raise ConfigurationError("config agent credentials file must exist")
    else:
        raise ConfigurationError("config agent_credentials_file must be a non-empty path")
    terminal_credentials_value = raw.get("terminal_credentials_file")
    if terminal_credentials_value is None:
        terminal_credentials_file = None
    elif isinstance(terminal_credentials_value, str) and terminal_credentials_value.strip():
        terminal_credentials_file = Path(terminal_credentials_value.strip())
        if not terminal_credentials_file.is_file():
            raise ConfigurationError("config terminal credentials file must exist")
    else:
        raise ConfigurationError("config terminal_credentials_file must be a non-empty path")
    eligible = raw.get("eligible_desktop_device_ids", [])
    if not isinstance(eligible, list) or any(
        not isinstance(device_id, str) or not device_id for device_id in eligible
    ):
        raise ConfigurationError("config eligible_desktop_device_ids must be a string list")
    port = raw.get("listen_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigurationError("config listen_port must be an integer from 1 to 65535")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("config web_base_url must be an HTTPS URL")
    if not certfile.is_file() or not keyfile.is_file():
        raise ConfigurationError("config HTTPS certificate and key files must exist")
    return ServiceConfig(
        host,
        port,
        base_url,
        data_dir,
        certfile,
        keyfile,
        credentials_file,
        terminal_credentials_file,
        tuple(eligible),
    )


class _SystemClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


@dataclass(frozen=True)
class ToolSpec:
    """One immutable source for a tool's public schema and gateway validation."""

    name: str
    properties: tuple[tuple[str, object], ...]
    required: tuple[str, ...]

    def input_schema(self) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {
                name: _thaw_schema(schema) for name, schema in self.properties
            },
            "required": list(self.required),
        }


def _freeze_schema(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {name: _freeze_schema(item) for name, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_schema(item) for item in value)
    return value


def _thaw_schema(value: object) -> object:
    if isinstance(value, Mapping):
        return {name: _thaw_schema(item) for name, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_schema(item) for item in value]
    return value


def _tool_spec(
    name: str, properties: dict[str, dict[str, object]], required: tuple[str, ...]
) -> ToolSpec:
    properties = {
        **properties,
        "call_id": {"type": "string"},
        "after_event_seq": {"type": "integer", "minimum": 0},
    }
    return ToolSpec(
        name,
        tuple(
            (property_name, _freeze_schema(schema))
            for property_name, schema in properties.items()
        ),
        required,
    )


_TOOL_SPECS = (
    _tool_spec(
        "assign_task_to_human",
        {
            "task_title": {"type": "string"},
            "task_description": {"type": "string"},
            "task_id": {"type": "string"},
            "client_timeout_seconds": {"type": "integer"},
            "max_result_bytes": {"type": "integer"},
            "context_note": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("task_title", "task_description", "request_id"),
    ),
    _tool_spec(
        "get_user_input",
        {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "default_value": {"type": "string"},
            "input_type": {
                "type": "string",
                "enum": ["text", "integer", "float"],
            },
            "client_timeout_seconds": {"type": "integer"},
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("title", "prompt", "request_id"),
    ),
    _tool_spec(
        "get_user_choice",
        {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "choices": {"type": "array", "items": {"type": "string"}},
            "allow_multiple": {"type": "boolean"},
            "client_timeout_seconds": {"type": "integer"},
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("title", "prompt", "choices", "request_id"),
    ),
    _tool_spec(
        "get_multiline_input",
        {
            "title": {"type": "string"},
            "prompt": {"type": "string"},
            "default_value": {"type": "string"},
            "client_timeout_seconds": {"type": "integer"},
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("title", "prompt", "request_id"),
    ),
    _tool_spec(
        "show_confirmation_dialog",
        {
            "title": {"type": "string"},
            "message": {"type": "string"},
            "client_timeout_seconds": {"type": "integer"},
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("title", "message", "request_id"),
    ),
    _tool_spec(
        "show_info_message",
        {
            "title": {"type": "string"},
            "message": {"type": "string"},
            "client_timeout_seconds": {"type": "integer"},
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("title", "message", "request_id"),
    ),
    _tool_spec(
        "get_feedback_from_user",
        {
            "client_timeout_seconds": {"type": "integer"},
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("request_id",),
    ),
)


def _find_tool_spec(name: str) -> ToolSpec | None:
    return next((spec for spec in _TOOL_SPECS if spec.name == name), None)


def _load_agent_credentials(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("config agent credentials cannot be read") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("config agent credentials must be a JSON object")
    credentials: dict[str, str] = {}
    for token, caller_id in raw.items():
        if (
            not isinstance(token, str)
            or not token
            or not isinstance(caller_id, str)
            or not caller_id
        ):
            raise ConfigurationError("config agent credentials contain invalid entries")
        credentials[token] = caller_id
    return credentials


@dataclass(frozen=True)
class _TerminalIdentity:
    device_id: str
    terminal_platform: str


def _load_terminal_credentials(path: Path | None) -> dict[str, _TerminalIdentity]:
    if path is None:
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("config terminal credentials cannot be read") from error
    if not isinstance(raw, dict):
        raise ConfigurationError("config terminal credentials must be a JSON object")
    credentials: dict[str, _TerminalIdentity] = {}
    for token, value in raw.items():
        if not isinstance(token, str) or not token or not isinstance(value, dict):
            raise ConfigurationError("config terminal credentials contain invalid entries")
        device_id, platform = value.get("device_id"), value.get("terminal_platform")
        if not isinstance(device_id, str) or not device_id or not isinstance(platform, str) or not platform:
            raise ConfigurationError("config terminal credentials contain invalid entries")
        credentials[token] = _TerminalIdentity(device_id, platform)
    return credentials


class _McpGateway:
    def __init__(
        self, coordinator: CallCoordinator, credentials: dict[str, str], desktop_route_device_id: str | None = None
    ) -> None:
        self._coordinator = coordinator
        self._credentials = credentials
        self._desktop_route_device_id = desktop_route_device_id

    def caller_for(self, authorization: str | None) -> str | None:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return None
        token = authorization.removeprefix("Bearer ")
        if not token or token.strip() != token:
            return None
        return self._credentials.get(token)

    def handle(self, payload: object, caller_id: str) -> dict[str, object]:
        if not isinstance(payload, dict):
            return _rpc_error(None, -32600, "invalid request")
        request_id = payload.get("id")
        if payload.get("jsonrpc") != "2.0" or not isinstance(payload.get("method"), str):
            return _rpc_error(request_id, -32600, "invalid request")
        method = payload["method"]
        if method == "initialize":
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "hitl-central-service", "version": "0.1"},
                },
            )
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": _tool_definitions()})
        if method == "tools/call":
            return self._call_tool(request_id, payload.get("params"), caller_id)
        return _rpc_error(request_id, -32601, "method not found")

    def _call_tool(
        self, rpc_id: object, params: object, caller_id: str
    ) -> dict[str, object]:
        if not isinstance(params, dict):
            return _rpc_error(rpc_id, -32602, "invalid params")
        name = params.get("name")
        arguments = params.get("arguments")
        spec = _find_tool_spec(name) if isinstance(name, str) else None
        if spec is None or not isinstance(arguments, dict):
            return _rpc_error(rpc_id, -32602, "invalid params")
        missing = [field for field in spec.required if field not in arguments]
        request_id = arguments.get("request_id")
        if missing or not isinstance(request_id, str) or not request_id:
            return _rpc_error(rpc_id, -32602, "invalid params")
        safe_arguments = {
            field: arguments[field]
            for field, _ in spec.properties
            if field in arguments
        }
        interaction_key = "task_id" if spec.name == "assign_task_to_human" else "interaction_id"
        call_id = safe_arguments.get("call_id")
        after_event_seq = safe_arguments.get("after_event_seq")
        if call_id is not None or after_event_seq is not None:
            return self._continue_tool_call(
                rpc_id,
                spec,
                safe_arguments,
                interaction_key,
                caller_id,
                request_id,
            )
        interaction_id = safe_arguments.get(interaction_key)
        if not isinstance(interaction_id, str) or not interaction_id:
            interaction_id = request_id
        message = json.dumps(
            {"tool": spec.name, "arguments": safe_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        envelope = self._coordinator.create_call(
            CreateCall(
                operator_id="operator-default",
                caller_id=caller_id,
                interaction_id=interaction_id,
                request_id=request_id,
                message=message,
                ring_timeout_seconds=60,
                desktop_route_device_id=self._desktop_route_device_id,
            )
        )
        return _rpc_result(rpc_id, {"content": [], "structuredContent": envelope})

    def _continue_tool_call(
        self,
        rpc_id: object,
        spec: ToolSpec,
        safe_arguments: dict[str, object],
        interaction_key: str,
        caller_id: str,
        request_id: str,
    ) -> dict[str, object]:
        """Validate transport arguments, then preserve coordinator envelopes verbatim."""
        interaction_id = safe_arguments.get(interaction_key)
        call_id = safe_arguments.get("call_id")
        after_event_seq = safe_arguments.get("after_event_seq")
        if (
            not isinstance(interaction_id, str)
            or not interaction_id
            or not isinstance(call_id, str)
            or not call_id
            or not isinstance(after_event_seq, int)
            or isinstance(after_event_seq, bool)
            or after_event_seq < 0
        ):
            return _rpc_error(rpc_id, -32602, "invalid params")
        continuation_payload = json.dumps(
            {"tool": spec.name, "arguments": safe_arguments},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        envelope = self._coordinator.continue_call(
            caller_id,
            request_id,
            interaction_id,
            call_id,
            after_event_seq,
            continuation_payload,
        )
        if envelope["status"] == "session_not_found":
            # Keep unknown and unauthorized calls indistinguishable.
            return _rpc_error(rpc_id, -32602, "invalid params")
        return _rpc_result(rpc_id, {"content": [], "structuredContent": envelope})


def _tool_definitions() -> list[dict[str, object]]:
    return [
        {
            "name": spec.name,
            "description": "Request human interaction through the central service.",
            "inputSchema": spec.input_schema(),
        }
        for spec in _TOOL_SPECS
    ]


def _rpc_result(request_id: object, result: dict[str, object]) -> dict[str, object]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class _TerminalGateway:
    def __init__(self, coordinator: CallCoordinator, credentials: dict[str, _TerminalIdentity]) -> None:
        self._coordinator = coordinator
        self._credentials = credentials

    def identity_for(self, authorization: str | None) -> _TerminalIdentity | None:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return None
        token = authorization.removeprefix("Bearer ")
        if not token or token.strip() != token:
            return None
        return self._credentials.get(token)

    def state(self, identity: _TerminalIdentity, call_id: str | None = None) -> dict[str, object]:
        return {"call": self._coordinator.terminal_state(identity.device_id, call_id)}

    def action(
        self, identity: _TerminalIdentity, kind: str, call_id: str, payload: object
    ) -> tuple[HTTPStatus, dict[str, object]]:
        request = self._action_request(kind, payload)
        if request is None:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        action_id, session_id = request
        result = self._coordinator.terminal_action(
            kind,
            call_id,
            identity.device_id,
            action_id,
            session_id if isinstance(session_id, str) else None,
            identity.terminal_platform,
        )
        return self._action_response(result)

    @staticmethod
    def _action_request(kind: str, payload: object) -> tuple[str, str | None] | None:
        if not isinstance(payload, dict):
            return None
        action_id = payload.get("action_id")
        session_id = payload.get("owner_session_id")
        if not isinstance(action_id, str) or not action_id:
            return None
        if kind == "accept" and (not isinstance(session_id, str) or not session_id):
            return None
        return action_id, session_id if isinstance(session_id, str) else None

    @staticmethod
    def _action_response(result: dict[str, Any] | None) -> tuple[HTTPStatus, dict[str, object]]:
        if result is None:
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if result["status"] in {"request_conflict", "ownership_conflict"}:
            return HTTPStatus.CONFLICT, result
        return HTTPStatus.OK, result


class _PublicHandler(BaseHTTPRequestHandler):
    server_version = "HITLCentralService/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        authenticated = self._terminal_request_identity()
        if authenticated is None:
            return
        terminal, identity = authenticated
        if path == "/api/terminals/state":
            self._send_json(HTTPStatus.OK, terminal.state(identity))
            return
        call_id = self._terminal_state_call_id(path)
        if call_id is not None:
            state = terminal.state(identity, call_id)
            if state["call"] is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self._send_json(HTTPStatus.OK, state)
            return
        self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        if path != "/mcp":
            self._terminal_post(path)
            return
        gateway = getattr(self.server, "mcp_gateway", None)
        if not isinstance(gateway, _McpGateway):
            self._send_json(HTTPStatus.UNAUTHORIZED, _unauthorized_response())
            return
        caller_id = gateway.caller_for(self.headers.get("Authorization"))
        if caller_id is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, _unauthorized_response())
            return
        try:
            content_length = int(self.headers.get("Content-Length", ""))
            if content_length < 0 or content_length > 1_000_000:
                raise ValueError
            raw = self.rfile.read(content_length)
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._send_json(
                HTTPStatus.BAD_REQUEST, _rpc_error(None, -32700, "parse error")
            )
            return
        self._send_json(HTTPStatus.OK, gateway.handle(payload, caller_id))

    def _send_json(self, status: HTTPStatus, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _terminal_gateway(self) -> _TerminalGateway | None:
        terminal = getattr(self.server, "terminal_gateway", None)
        return terminal if isinstance(terminal, _TerminalGateway) else None

    def _terminal_request_identity(self) -> tuple[_TerminalGateway, _TerminalIdentity] | None:
        terminal = self._terminal_gateway()
        if terminal is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        identity = terminal.identity_for(self.headers.get("Authorization"))
        if identity is None:
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "unauthorized"})
            return None
        return terminal, identity

    @staticmethod
    def _terminal_state_call_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 4 and parts[:3] == ["", "api", "calls"] and parts[3]:
            return parts[3]
        return None

    @staticmethod
    def _terminal_action_route(path: str) -> tuple[str, str] | None:
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "api", "calls"] or parts[4] not in {"accept", "decline"}:
            return None
        return parts[3], parts[4]

    def _terminal_request_body(self) -> tuple[bool, object]:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
            if content_length < 0 or content_length > 1_000_000:
                raise ValueError
            return True, json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
            return False, None

    def _terminal_post(self, path: str) -> None:
        authenticated = self._terminal_request_identity()
        if authenticated is None:
            return
        terminal, identity = authenticated
        route = self._terminal_action_route(path)
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body_ok, payload = self._terminal_request_body()
        if not body_ok:
            return
        call_id, kind = route
        status, response = terminal.action(identity, kind, call_id, payload)
        self._send_json(status, response)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid writing request contents to diagnostic output."""


def _unauthorized_response() -> dict[str, object]:
    return _rpc_error(None, -32001, "unauthorized")


class RingingDeadlineScheduler:
    """Bounded polling worker that settles persisted ringing deadlines."""

    def __init__(
        self,
        coordinator: CallCoordinator,
        clock: object,
        *,
        poll_interval_seconds: float = 0.25,
    ) -> None:
        self._coordinator = coordinator
        self._clock = clock
        self._poll_interval_seconds = poll_interval_seconds
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def tick(self) -> list[dict[str, Any]]:
        return self._coordinator.recover_and_settle()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="ringing-deadline-scheduler", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=self._poll_interval_seconds + 1)
            self._thread = None

    def _run(self) -> None:
        while not self._stopped.is_set():
            self.tick()
            self._stopped.wait(self._poll_interval_seconds)


def _build_tls_context(config: ServiceConfig) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=config.certfile, keyfile=config.keyfile)
    return context


def _create_secure_server(
    config: ServiceConfig,
    context: ssl.SSLContext,
    gateway: _McpGateway | None = None,
    terminal_gateway: _TerminalGateway | None = None,
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), _PublicHandler)
    try:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    except BaseException:
        server.server_close()
        raise
    if gateway is not None:
        server.mcp_gateway = gateway  # type: ignore[attr-defined]
    if terminal_gateway is not None:
        server.terminal_gateway = terminal_gateway  # type: ignore[attr-defined]
    return server


def _serve(config: ServiceConfig) -> None:
    context = _build_tls_context(config)
    credentials = _load_agent_credentials(config.agent_credentials_file)
    terminal_credentials = _load_terminal_credentials(config.terminal_credentials_file)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    clock = _SystemClock()
    coordinator = CallCoordinator(config.data_dir / "calls.sqlite3", clock=clock)
    scheduler = RingingDeadlineScheduler(coordinator, clock)
    try:
        coordinator.recover_and_settle()
        scheduler.start()
        route_device_id = config.eligible_desktop_device_ids[0] if config.eligible_desktop_device_ids else None
        gateway = _McpGateway(coordinator, credentials, route_device_id)
        terminal_gateway = _TerminalGateway(coordinator, terminal_credentials)
        server = _create_secure_server(config, context, gateway, terminal_gateway)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    finally:
        scheduler.stop()
        coordinator.close()


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(2)


def _parse_config_path(arguments: list[str]) -> Path:
    legacy_modes = {"--stdio", "--local", "--service"}
    if any(argument in legacy_modes for argument in arguments):
        _fail("legacy stdio/local/service modes are rejected and not supported")
    parser = argparse.ArgumentParser(description="HITL central HTTPS service")
    parser.add_argument("--config", type=Path, help="path to central-service JSON config")
    parsed = parser.parse_args(arguments)
    if parsed.config is None:
        _fail("config is required; no default listener or stdio mode exists")
    return parsed.config


def main(argv: list[str] | None = None) -> None:
    arguments = sys.argv[1:] if argv is None else argv
    try:
        config = _load_config(_parse_config_path(arguments))
        _serve(config)
    except ConfigurationError as error:
        _fail(f"configuration error: {error}")
    except ssl.SSLError as error:
        _fail(f"configuration error: HTTPS setup failed: {error}")
    except OSError as error:
        _fail(f"configuration error: unable to create HTTPS listener: {error}")


if __name__ == "__main__":
    main()
