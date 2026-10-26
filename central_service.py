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
    port = raw.get("listen_port")
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        raise ConfigurationError("config listen_port must be an integer from 1 to 65535")
    parsed = urlparse(base_url)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ConfigurationError("config web_base_url must be an HTTPS URL")
    if not certfile.is_file() or not keyfile.is_file():
        raise ConfigurationError("config HTTPS certificate and key files must exist")
    return ServiceConfig(
        host, port, base_url, data_dir, certfile, keyfile, credentials_file
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


class _McpGateway:
    def __init__(self, coordinator: CallCoordinator, credentials: dict[str, str]) -> None:
        self._coordinator = coordinator
        self._credentials = credentials

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
            interaction_id = safe_arguments.get(interaction_key)
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
            if not envelope["success"]:
                return _rpc_error(rpc_id, -32602, "invalid params")
            return _rpc_result(rpc_id, {"content": [], "structuredContent": envelope})
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
            )
        )
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


class _PublicHandler(BaseHTTPRequestHandler):
    server_version = "HITLCentralService/0.1"

    def do_GET(self) -> None:  # noqa: N802
        if urlsplit(self.path).path.startswith("/calls/"):
            self.send_error(HTTPStatus.NOT_FOUND, "call not found")
            return
        self.send_error(HTTPStatus.NOT_FOUND, "route not found")

    def do_POST(self) -> None:  # noqa: N802
        if urlsplit(self.path).path != "/mcp":
            self.send_error(HTTPStatus.NOT_FOUND, "route not found")
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
    config: ServiceConfig, context: ssl.SSLContext, gateway: _McpGateway | None = None
) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer((config.listen_host, config.listen_port), _PublicHandler)
    try:
        server.socket = context.wrap_socket(server.socket, server_side=True)
    except BaseException:
        server.server_close()
        raise
    if gateway is not None:
        server.mcp_gateway = gateway  # type: ignore[attr-defined]
    return server


def _serve(config: ServiceConfig) -> None:
    context = _build_tls_context(config)
    credentials = _load_agent_credentials(config.agent_credentials_file)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    coordinator = CallCoordinator(config.data_dir / "calls.sqlite3", clock=_SystemClock())
    scheduler = RingingDeadlineScheduler(coordinator, _SystemClock())
    try:
        coordinator.recover_and_settle()
        scheduler.start()
        gateway = _McpGateway(coordinator, credentials)
        server = _create_secure_server(config, context, gateway)
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
