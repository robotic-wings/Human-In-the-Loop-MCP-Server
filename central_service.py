"""Minimal HTTPS entry point for the GUI-free central service.

This module intentionally owns only process startup and the public HTTPS
listener.  Call coordination and terminal workflows belong to later slices.
"""

from __future__ import annotations

import argparse
import base64
import html
import hashlib
import hmac
import json
import secrets
import ssl
import sys
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import MappingProxyType
from typing import Any, NoReturn
from urllib.error import URLError
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlsplit, urlunsplit
from urllib.request import urlopen

from call_coordinator import AcceptCall, CallCoordinator, CreateCall


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
    attachment_max_file_bytes: int
    attachment_max_submission_bytes: int
    attachment_max_archive_bytes: int
    bark_enabled: bool
    bark_url: str | None
    operator_credentials_file: Path | None


_DEFAULT_ATTACHMENT_MAX_FILE_BYTES = 20 * 1024 * 1024
_DEFAULT_ATTACHMENT_MAX_SUBMISSION_BYTES = 50 * 1024 * 1024
_DEFAULT_ATTACHMENT_MAX_ARCHIVE_BYTES = 1024 * 1024 * 1024
_MULTIPART_OVERHEAD_BYTES = 64 * 1024


def _required_string(config: dict[str, object], name: str) -> str:
    value = config.get(name)
    if not isinstance(value, str) or not value.strip():
        raise ConfigurationError(f"config requires a non-empty {name}")
    return value.strip()


def _positive_integer(config: dict[str, object], name: str, default: int) -> int:
    value = config.get(name, default)
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise ConfigurationError(f"config {name} must be a positive integer")
    return value


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
    operator_credentials_value = raw.get("operator_credentials_file")
    if operator_credentials_value is None:
        operator_credentials_file = None
    elif isinstance(operator_credentials_value, str) and operator_credentials_value.strip():
        operator_credentials_file = Path(operator_credentials_value.strip())
        if not operator_credentials_file.is_file():
            raise ConfigurationError("config operator credentials file must exist")
    else:
        raise ConfigurationError("config operator_credentials_file must be a non-empty path")
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
    attachment_max_file_bytes = _positive_integer(
        raw, "attachment_max_file_bytes", _DEFAULT_ATTACHMENT_MAX_FILE_BYTES
    )
    attachment_max_submission_bytes = _positive_integer(
        raw, "attachment_max_submission_bytes", _DEFAULT_ATTACHMENT_MAX_SUBMISSION_BYTES
    )
    attachment_max_archive_bytes = _positive_integer(
        raw, "attachment_max_archive_bytes", _DEFAULT_ATTACHMENT_MAX_ARCHIVE_BYTES
    )
    bark_enabled = raw.get("bark_enabled", False)
    if not isinstance(bark_enabled, bool):
        raise ConfigurationError("config bark_enabled must be a boolean")
    bark_url_value = raw.get("bark_url")
    if bark_enabled:
        if not isinstance(bark_url_value, str) or not bark_url_value.strip():
            raise ConfigurationError("config bark_url is required when bark_enabled")
        bark_url = bark_url_value.strip()
        if urlparse(bark_url).scheme not in {"http", "https"}:
            raise ConfigurationError("config bark_url must be an HTTP(S) URL")
    else:
        bark_url = None
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
        attachment_max_file_bytes,
        attachment_max_submission_bytes,
        attachment_max_archive_bytes,
        bark_enabled,
        bark_url,
        operator_credentials_file,
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
    _tool_spec(
        "cancel_interaction",
        {
            "interaction_id": {"type": "string"},
            "request_id": {"type": "string"},
        },
        ("interaction_id", "call_id", "request_id"),
    ),
    _tool_spec(
        "read_interaction_result",
        {
            "interaction_id": {"type": "string"},
            "result_id": {"type": "string"},
            "cursor": {"type": "integer", "minimum": 0},
            "max_result_bytes": {"type": "integer"},
        },
        ("interaction_id", "result_id", "max_result_bytes"),
    ),
    _tool_spec(
        "read_interaction_attachment",
        {
            "interaction_id": {"type": "string"},
            "attachment_id": {"type": "string"},
            "offset_bytes": {"type": "integer", "minimum": 0},
            "max_result_bytes": {"type": "integer"},
        },
        ("interaction_id", "attachment_id", "max_result_bytes"),
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


@dataclass(frozen=True)
class _OversizedTerminalAttachment:
    owner_session_id: str


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


def _load_operator_credentials(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("operator credentials cannot be read") from error
    if not isinstance(value, dict) or not isinstance(value.get("operator_id"), str):
        raise ConfigurationError("operator credentials are invalid")
    record = value.get("password_hash")
    if not isinstance(record, dict) or record.get("algorithm") != "scrypt":
        raise ConfigurationError("operator credentials are invalid")
    if any(name not in record for name in ("salt", "digest", "n", "r", "p")):
        raise ConfigurationError("operator credentials are invalid")
    return record


class _McpGateway:
    def __init__(
        self, coordinator: CallCoordinator, credentials: dict[str, str], desktop_route_device_id: str | None = None,
        bark_enabled: bool = False,
    ) -> None:
        self._coordinator = coordinator
        self._credentials = credentials
        self._desktop_route_device_id = desktop_route_device_id
        self._bark_enabled = bark_enabled

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

    def may_wait_for_tool_response(self, payload: object) -> bool:
        """Return whether this request can enter a bounded agent wait leg.

        This is deliberately a pure transport hint: the normal tool handler
        remains the authority for validation, admission, and all durable state.
        """
        if not isinstance(payload, dict) or payload.get("method") != "tools/call":
            return False
        params = payload.get("params")
        if not isinstance(params, dict):
            return False
        name = params.get("name")
        arguments = params.get("arguments")
        if not isinstance(name, str) or not isinstance(arguments, dict):
            return False
        spec = _find_tool_spec(name)
        return (
            spec is not None
            and spec.name not in {"read_interaction_result", "read_interaction_attachment", "cancel_interaction"}
            and _valid_client_timeout(arguments.get("client_timeout_seconds"))
        )

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
        if missing:
            return _rpc_error(rpc_id, -32602, "invalid params")
        safe_arguments = {
            field: arguments[field]
            for field, _ in spec.properties
            if field in arguments
        }
        if spec.name == "read_interaction_result":
            return self._read_interaction_result_tool(rpc_id, safe_arguments, caller_id)
        if spec.name == "read_interaction_attachment":
            return self._read_interaction_attachment_tool(rpc_id, safe_arguments, caller_id)
        if not isinstance(request_id, str) or not request_id:
            return _rpc_error(rpc_id, -32602, "invalid params")
        if spec.name == "cancel_interaction":
            return self._cancel_interaction_tool(
                rpc_id, safe_arguments, caller_id, request_id
            )
        interaction_key = "task_id" if spec.name == "assign_task_to_human" else "interaction_id"
        client_timeout_seconds = safe_arguments.get("client_timeout_seconds")
        if client_timeout_seconds is not None and not _valid_client_timeout(client_timeout_seconds):
            return _rpc_result(
                rpc_id,
                {"content": [], "structuredContent": _invalid_client_timeout()},
            )
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
                bark_enabled=self._bark_enabled,
            )
        )
        if client_timeout_seconds is not None and envelope["status"] == "ringing":
            continuation_payload = json.dumps(
                {"tool": spec.name, "arguments": safe_arguments},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            envelope = self._wait_for_leg(
                caller_id,
                request_id,
                interaction_id,
                envelope["call_id"],
                0,
                continuation_payload,
                client_timeout_seconds,
            )
        return _rpc_result(rpc_id, {"content": [], "structuredContent": envelope})

    def _read_interaction_result_tool(
        self, rpc_id: object, arguments: dict[str, object], caller_id: str
    ) -> dict[str, object]:
        result = self._coordinator.read_interaction_result(
            caller_id,
            arguments.get("interaction_id"),
            arguments.get("result_id"),
            arguments.get("cursor", 0),
            arguments.get("max_result_bytes"),
        )
        return _rpc_result(rpc_id, {"content": [], "structuredContent": result})

    def _read_interaction_attachment_tool(
        self, rpc_id: object, arguments: dict[str, object], caller_id: str
    ) -> dict[str, object]:
        result = self._coordinator.read_interaction_attachment(
            caller_id,
            arguments.get("interaction_id"),
            arguments.get("attachment_id"),
            arguments.get("offset_bytes", 0),
            arguments.get("max_result_bytes"),
        )
        return _rpc_result(rpc_id, {"content": [], "structuredContent": result})

    def _cancel_interaction_tool(
        self,
        rpc_id: object,
        arguments: dict[str, object],
        caller_id: str,
        request_id: str,
    ) -> dict[str, object]:
        interaction_id = arguments.get("interaction_id")
        call_id = arguments.get("call_id")
        if (
            not isinstance(interaction_id, str)
            or not interaction_id
            or not isinstance(call_id, str)
            or not call_id
        ):
            return _rpc_error(rpc_id, -32602, "invalid params")
        envelope = self._coordinator.cancel_interaction(
            caller_id, interaction_id, call_id, request_id
        )
        if envelope["status"] == "session_not_found":
            # Unknown and foreign calls must remain indistinguishable.
            return _rpc_error(rpc_id, -32602, "invalid params")
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
        client_timeout_seconds = safe_arguments.get("client_timeout_seconds")
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
        if client_timeout_seconds is not None:
            if not _valid_client_timeout(client_timeout_seconds):
                envelope = _invalid_client_timeout()
            else:
                envelope = self._wait_for_leg(
                    caller_id, request_id, interaction_id, call_id, after_event_seq,
                    continuation_payload, client_timeout_seconds,
                )
        else:
            envelope = self._coordinator.continue_call(
                caller_id, request_id, interaction_id, call_id, after_event_seq, continuation_payload
            )
        if envelope["status"] == "session_not_found":
            # Keep unknown and unauthorized calls indistinguishable.
            return _rpc_error(rpc_id, -32602, "invalid params")
        return _rpc_result(rpc_id, {"content": [], "structuredContent": envelope})

    def _wait_for_leg(
        self,
        caller_id: str,
        request_id: str,
        interaction_id: str,
        call_id: str,
        after_event_seq: int,
        payload: str,
        client_timeout_seconds: int,
    ) -> dict[str, Any]:
        envelope, waiting = self._coordinator.begin_wait_leg(
            caller_id, request_id, interaction_id, call_id, after_event_seq,
            payload, client_timeout_seconds,
        )
        while waiting:
            time.sleep(0.02)
            polled = self._coordinator.poll_wait_leg(
                caller_id, request_id, interaction_id, call_id, after_event_seq, payload
            )
            if polled is not None:
                envelope = polled
                waiting = False
        return envelope


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


def _valid_client_timeout(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 2


def _invalid_client_timeout() -> dict[str, object]:
    return {
        "success": False,
        "status": "invalid_client_timeout",
        "needs_continuation": False,
    }


def _rpc_error(request_id: object, code: int, message: str) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "error": {"code": code, "message": message},
    }


class _TerminalGateway:
    def __init__(
        self,
        coordinator: CallCoordinator,
        credentials: dict[str, _TerminalIdentity],
        legacy_route_device_id: str | None = None,
        *,
        attachment_max_submission_bytes: int = _DEFAULT_ATTACHMENT_MAX_SUBMISSION_BYTES,
    ) -> None:
        self._coordinator = coordinator
        self._credentials = credentials
        self._legacy_route_device_id = legacy_route_device_id
        self.attachment_max_submission_bytes = attachment_max_submission_bytes

    def identity_for(self, authorization: str | None) -> _TerminalIdentity | None:
        if not isinstance(authorization, str) or not authorization.startswith("Bearer "):
            return None
        token = authorization.removeprefix("Bearer ")
        if not token or token.strip() != token:
            return None
        return self._credentials.get(token)

    def state(self, identity: _TerminalIdentity, call_id: str | None = None) -> dict[str, object]:
        presence = self._coordinator.terminal_presence(identity.device_id)
        if presence is None:
            return {"call": self._coordinator.terminal_state(identity.device_id, call_id)}
        return {
            "presence": presence,
            "call": self._coordinator.terminal_poll_snapshot(identity.device_id, call_id),
        }

    def register(self, identity: _TerminalIdentity, payload: object) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(payload, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        instance_id, gui_ready = payload.get("instance_id"), payload.get("gui_ready")
        if not isinstance(instance_id, str) or not instance_id or not isinstance(gui_ready, bool):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        self._coordinator.register_terminal_presence(
            device_id=identity.device_id,
            terminal_platform=identity.terminal_platform,
            route_device_id=identity.device_id,
            callback_url=None,
            instance_id=instance_id,
            gui_ready=gui_ready,
        )
        return HTTPStatus.OK, self.state(identity)

    def device_heartbeat(
        self, identity: _TerminalIdentity, payload: object
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(payload, dict) or not isinstance(payload.get("instance_id"), str) or not payload["instance_id"]:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        result = self._coordinator.terminal_device_heartbeat(identity.device_id, payload["instance_id"])
        if result.get("status") == "instance_conflict":
            return HTTPStatus.CONFLICT, result
        return HTTPStatus.OK, self.state(identity)

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
            self._legacy_route_device_id,
        )
        return self._action_response(result)

    def heartbeat(
        self, identity: _TerminalIdentity, call_id: str, payload: object
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(payload, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        owner_session_id = payload.get("owner_session_id")
        if not isinstance(owner_session_id, str) or not owner_session_id:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        result = self._coordinator.terminal_heartbeat(
            call_id,
            identity.device_id,
            owner_session_id,
            self._legacy_route_device_id,
        )
        return self._action_response(result)

    def save_draft(
        self, identity: _TerminalIdentity, call_id: str, payload: object
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(payload, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        owner_session_id = payload.get("owner_session_id")
        body = payload.get("body")
        expected_draft_version = payload.get("expected_draft_version")
        if (
            not isinstance(owner_session_id, str)
            or not owner_session_id
            or not isinstance(body, str)
            or not isinstance(expected_draft_version, int)
            or isinstance(expected_draft_version, bool)
        ):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        result = self._coordinator.save_terminal_draft(
            call_id, identity.device_id, owner_session_id, body, expected_draft_version
        )
        if result is None:
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if result["status"] in {"ownership_conflict", "draft_version_conflict"}:
            return HTTPStatus.CONFLICT, result
        if result["status"] == "invalid_draft":
            return HTTPStatus.BAD_REQUEST, result
        return HTTPStatus.OK, result

    def submit_text(
        self, identity: _TerminalIdentity, call_id: str, payload: object
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(payload, dict):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        owner_session_id = payload.get("owner_session_id")
        submission_id = payload.get("submission_id")
        user_input = payload.get("user_input")
        task_version = payload.get("task_version", 0)
        attachment_ids = payload.get("attachment_ids")
        if (
            not isinstance(owner_session_id, str)
            or not owner_session_id
            or not isinstance(submission_id, str)
            or not submission_id
            or not isinstance(user_input, str)
            or not isinstance(task_version, int)
            or isinstance(task_version, bool)
            or (attachment_ids is not None and not isinstance(attachment_ids, list))
        ):
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        result = self._coordinator.submit_terminal_text(
            call_id,
            identity.device_id,
            owner_session_id,
            submission_id,
            user_input,
            task_version,
            attachment_ids,
            self._legacy_route_device_id,
        )
        if result is None:
            # Unknown calls, wrong route credentials, and wrong owner sessions
            # intentionally have the same response.
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if result["status"] == "request_conflict":
            return HTTPStatus.CONFLICT, result
        if result["status"] == "task_version_conflict":
            return HTTPStatus.CONFLICT, result
        if result["status"] == "invalid_attachment_reference":
            return HTTPStatus.BAD_REQUEST, result
        if result["status"] == "invalid_submission":
            return HTTPStatus.BAD_REQUEST, result
        if result["status"] != "answered":
            return HTTPStatus.CONFLICT, {
                "success": False,
                "status": result["status"],
                "reason": result.get("reason"),
            }
        return HTTPStatus.OK, result

    def upload_attachment(
        self,
        identity: _TerminalIdentity,
        call_id: str,
        owner_session_id: str,
        original_name: str,
        media_type: str,
        content: bytes,
    ) -> tuple[HTTPStatus, dict[str, object]]:
        result = self._coordinator.upload_terminal_attachment(
            call_id,
            identity.device_id,
            owner_session_id,
            original_name,
            media_type,
            content,
            self._legacy_route_device_id,
        )
        if result is None:
            # Do not disclose whether the call exists or belongs to a different
            # desktop/session.
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if result["status"] == "invalid_attachment":
            return HTTPStatus.BAD_REQUEST, result
        if result["status"] == "attachment_rejected":
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, result
        if result["status"] != "uploaded":
            return HTTPStatus.CONFLICT, {
                "success": False,
                "status": result["status"],
                "reason": result.get("reason"),
            }
        return HTTPStatus.OK, result

    def reject_attachment_submission_limit(
        self, identity: _TerminalIdentity, call_id: str, owner_session_id: str
    ) -> tuple[HTTPStatus, dict[str, object]]:
        result = self._coordinator.reject_terminal_attachment_submission_limit(
            call_id,
            identity.device_id,
            owner_session_id,
            self._legacy_route_device_id,
        )
        if result is None:
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if result["status"] == "attachment_rejected":
            return HTTPStatus.REQUEST_ENTITY_TOO_LARGE, result
        return HTTPStatus.CONFLICT, {
            "success": False,
            "status": result["status"],
            "reason": result.get("reason"),
        }

    def read_draft(
        self, identity: _TerminalIdentity, call_id: str, owner_session_id: str | None
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if not isinstance(owner_session_id, str) or not owner_session_id:
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        draft = self._coordinator.read_terminal_draft(
            call_id, identity.device_id, owner_session_id
        )
        if draft is None:
            # Do not distinguish an unknown call from a call that belongs to
            # another terminal or accepted session.
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        return HTTPStatus.OK, draft

    @staticmethod
    def _action_request(kind: str, payload: object) -> tuple[str, str | None] | None:
        if not isinstance(payload, dict):
            return None
        action_id = payload.get("action_id")
        session_id = payload.get("owner_session_id")
        if not isinstance(action_id, str) or not action_id:
            return None
        if kind in {"accept", "cancel"} and (not isinstance(session_id, str) or not session_id):
            return None
        return action_id, session_id if isinstance(session_id, str) else None

    @staticmethod
    def _action_response(result: dict[str, Any] | None) -> tuple[HTTPStatus, dict[str, object]]:
        if result is None:
            return HTTPStatus.NOT_FOUND, {"error": "not found"}
        if result["status"] in {"request_conflict", "ownership_conflict"}:
            return HTTPStatus.CONFLICT, result
        return HTTPStatus.OK, result


class _OperatorGateway:
    """Small server-side operator session boundary for the phone call page."""

    def __init__(self, coordinator: CallCoordinator, record: dict[str, object], expected_host: str) -> None:
        self._coordinator = coordinator
        self._record = record
        self._expected_host = expected_host
        self._sessions: dict[str, str] = {}
        self._lock = threading.RLock()

    def login(self, payload: object) -> tuple[HTTPStatus, dict[str, object], str | None]:
        password = payload.get("password") if isinstance(payload, dict) else None
        if not isinstance(password, str) or not self._password_matches(password):
            return HTTPStatus.UNAUTHORIZED, {"error": "invalid credentials"}, None
        session = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(32)
        self._coordinator.create_operator_session(session, csrf)
        with self._lock:
            self._sessions[session] = csrf
        return HTTPStatus.OK, {"csrf_token": csrf}, session

    def read(self, session: str | None, call_id: str) -> dict[str, object] | None:
        if not self._session_exists(session):
            return None
        return self._coordinator.operator_call_state(call_id)

    def page(self, session: str | None, call_id: str) -> tuple[dict[str, object] | None, str | None]:
        """Return the authenticated page view and its CSRF value without new state."""
        csrf = self._csrf_for_session(session)
        if csrf is None:
            return None, None
        state = self._coordinator.operator_call_page(call_id)
        if state is not None:
            state["_page_session_matches_owner"] = state.get("owner_session_id") == session
        return state, csrf

    def accept(
        self, session: str | None, csrf: str | None, origin: str | None, host: str | None,
        call_id: str, action_id: object,
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if (
            not self._session_matches(session, csrf)
            or origin != f"https://{self._expected_host}"
            or host != self._expected_host
            or not isinstance(action_id, str)
            or not action_id
        ):
            return HTTPStatus.FORBIDDEN, {"error": "forbidden"}
        result = self._coordinator.accept_call(
            AcceptCall(call_id, f"iphone:{session}:{call_id}:{action_id}", "iphone_web", session or "", "iphone_web", "ios")
        )
        if result["status"] != "opened":
            return HTTPStatus.CONFLICT, result
        return HTTPStatus.OK, result

    def submit(
        self, session: str | None, csrf: str | None, origin: str | None, host: str | None,
        call_id: str, payload: object,
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if (
            not self._session_matches(session, csrf)
            or origin != f"https://{self._expected_host}"
            or host != self._expected_host
            or not isinstance(payload, dict)
        ):
            return HTTPStatus.FORBIDDEN, {"error": "forbidden"}
        submission_id, user_input = payload.get("submission_id"), payload.get("user_input")
        if not isinstance(submission_id, str) or not isinstance(user_input, str) or not user_input:
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        result = self._coordinator.submit_terminal_text(
            call_id, "iphone_web", session or "", submission_id, user_input, operator_owner=True
        )
        if result is None:
            # An authenticated operator may know a ringing call without owning
            # it; do not turn that into a content-bearing response.
            return HTTPStatus.CONFLICT, {"error": "cannot submit"}
        if result["status"] == "invalid_submission":
            return HTTPStatus.BAD_REQUEST, {"error": "invalid request"}
        if result["status"] == "request_conflict":
            return HTTPStatus.CONFLICT, result
        if result["status"] != "answered":
            return HTTPStatus.CONFLICT, {"status": result["status"]}
        return HTTPStatus.OK, result

    def decline(
        self, session: str | None, csrf: str | None, origin: str | None, host: str | None,
        call_id: str, action_id: object,
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if (
            not self._session_matches(session, csrf)
            or origin != f"https://{self._expected_host}"
            or host != self._expected_host
            or not isinstance(action_id, str)
            or not action_id
        ):
            return HTTPStatus.FORBIDDEN, {"error": "forbidden"}
        result = self._coordinator.decline_iphone_call(call_id, session or "", action_id)
        if result is None:
            return HTTPStatus.CONFLICT, {"error": "cannot decline"}
        if result["status"] == "request_conflict":
            return HTTPStatus.CONFLICT, result
        if result["status"] != "declined":
            return HTTPStatus.CONFLICT, result
        return HTTPStatus.OK, result

    def heartbeat(
        self, session: str | None, csrf: str | None, origin: str | None, host: str | None,
        call_id: str,
    ) -> tuple[HTTPStatus, dict[str, object]]:
        if (
            not self._session_matches(session, csrf)
            or origin != f"https://{self._expected_host}"
            or host != self._expected_host
        ):
            return HTTPStatus.FORBIDDEN, {"error": "forbidden"}
        result = self._coordinator.heartbeat_iphone_call(call_id, session or "")
        if result is None:
            return HTTPStatus.CONFLICT, {"error": "cannot heartbeat"}
        if result["status"] != "accepted":
            return HTTPStatus.CONFLICT, result
        return HTTPStatus.OK, result

    def _session_exists(self, session: str | None) -> bool:
        return self._csrf_for_session(session) is not None

    def _session_matches(self, session: str | None, csrf: str | None) -> bool:
        expected = self._csrf_for_session(session)
        return isinstance(csrf, str) and expected is not None and hmac.compare_digest(expected, csrf)

    def _csrf_for_session(self, session: str | None) -> str | None:
        if not isinstance(session, str):
            return None
        with self._lock:
            cached = self._sessions.get(session)
        if cached is not None:
            return cached
        persisted = self._coordinator.operator_session_csrf(session)
        if persisted is not None:
            with self._lock:
                self._sessions[session] = persisted
        return persisted

    def _password_matches(self, password: str) -> bool:
        try:
            salt = base64.b64decode(self._record["salt"], validate=True)
            expected = base64.b64decode(self._record["digest"], validate=True)
            actual = hashlib.scrypt(
                password.encode("utf-8"), salt=salt, n=int(self._record["n"]),
                r=int(self._record["r"]), p=int(self._record["p"]),
            )
        except (KeyError, TypeError, ValueError):
            return False
        return hmac.compare_digest(actual, expected)


class _PublicHandler(BaseHTTPRequestHandler):
    server_version = "HITLCentralService/0.1"
    protocol_version = "HTTP/1.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        operator = self._operator_gateway()
        web_call_id = self._web_call_id(path)
        if operator is not None and web_call_id is not None:
            if path.startswith("/calls/"):
                state, csrf = operator.page(self._operator_session(), web_call_id)
                self._send_html(self._operator_call_page(web_call_id, state, csrf))
                return
            state = operator.read(self._operator_session(), web_call_id)
            if state is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            else:
                self._send_json(HTTPStatus.OK, {"call": state})
            return
        draft_call_id = self._terminal_draft_call_id(path)
        if draft_call_id is not None:
            terminal = self._terminal_gateway()
            identity = (
                terminal.identity_for(self.headers.get("Authorization"))
                if terminal is not None
                else None
            )
            if identity is None:
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
                return
            status, response = terminal.read_draft(
                identity,
                draft_call_id,
                self.headers.get("X-Owner-Session-Id"),
            )
            self._send_json(status, response)
            return
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
        operator = self._operator_gateway()
        # The phone and desktop protocols deliberately share the call action
        # paths.  A device bearer credential selects the desktop protocol;
        # only browser requests without one may enter the operator-session
        # boundary below.  This keeps a configured phone gateway from
        # shadowing existing desktop submissions and actions.
        if self.headers.get("Authorization") and path != "/mcp":
            self._terminal_post(path)
            return
        if operator is not None and path == "/api/operator/login":
            body_ok, payload = self._terminal_request_body()
            if not body_ok:
                return
            status, response, session = operator.login(payload)
            if session is not None:
                self._send_json(
                    status, response,
                    cookie=f"operator_session={session}; Secure; HttpOnly; SameSite=Strict; Path=/",
                )
            else:
                self._send_json(status, response)
            return
        web_accept_id = self._web_accept_call_id(path)
        if operator is not None and web_accept_id is not None:
            body_ok, payload = self._terminal_request_body()
            if not body_ok:
                return
            action_id = payload.get("action_id") if isinstance(payload, dict) else None
            status, response = operator.accept(
                self._operator_session(), self.headers.get("X-CSRF-Token"), self.headers.get("Origin"),
                self.headers.get("Host"), web_accept_id, action_id,
            )
            self._send_json(status, response)
            return
        web_decline_id = self._web_decline_call_id(path)
        if operator is not None and web_decline_id is not None:
            body_ok, payload = self._terminal_request_body()
            if not body_ok:
                return
            action_id = payload.get("action_id") if isinstance(payload, dict) else None
            status, response = operator.decline(
                self._operator_session(), self.headers.get("X-CSRF-Token"), self.headers.get("Origin"),
                self.headers.get("Host"), web_decline_id, action_id,
            )
            self._send_json(status, response)
            return
        web_heartbeat_id = self._web_heartbeat_call_id(path)
        if operator is not None and web_heartbeat_id is not None:
            body_ok, _ = self._terminal_request_body()
            if not body_ok:
                return
            status, response = operator.heartbeat(
                self._operator_session(), self.headers.get("X-CSRF-Token"), self.headers.get("Origin"),
                self.headers.get("Host"), web_heartbeat_id,
            )
            self._send_json(status, response)
            return
        web_submission_id = self._web_submission_call_id(path)
        if operator is not None and web_submission_id is not None:
            body_ok, payload = self._terminal_request_body()
            if not body_ok:
                return
            status, response = operator.submit(
                self._operator_session(), self.headers.get("X-CSRF-Token"), self.headers.get("Origin"),
                self.headers.get("Host"), web_submission_id, payload,
            )
            self._send_json(status, response)
            return
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
        if gateway.may_wait_for_tool_response(payload):
            # A bounded wait result can take a full safe leg.  Commit the HTTP
            # response start first, then deliver the ordinary JSON-RPC result
            # as a chunk when the coordinator resolves that leg.
            self._start_chunked_json(HTTPStatus.OK)
            self._write_chunked_json(gateway.handle(payload, caller_id))
            return
        self._send_json(HTTPStatus.OK, gateway.handle(payload, caller_id))

    def do_PUT(self) -> None:  # noqa: N802
        path = urlsplit(self.path).path
        authenticated = self._terminal_request_identity()
        if authenticated is None:
            return
        terminal, identity = authenticated
        parts = path.split("/")
        if len(parts) != 5 or parts[:3] != ["", "api", "calls"] or parts[4] != "draft" or not parts[3]:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body_ok, payload = self._terminal_request_body()
        if not body_ok:
            return
        status, response = terminal.save_draft(identity, parts[3], payload)
        self._send_json(status, response)

    def _send_json(self, status: HTTPStatus, payload: dict[str, object], cookie: str | None = None) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        if cookie is not None:
            self.send_header("Set-Cookie", cookie)
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _send_html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    @staticmethod
    def _operator_call_page(call_id: str, state: dict[str, object] | None, csrf: str | None) -> str:
        """Render a deliberately small, escaped HTML view over existing APIs."""
        if state is None or csrf is None:
            return "<!doctype html><title>Operator sign in</title><main>Operator sign in required.</main>"
        escaped_call = html.escape(call_id, quote=True)
        message = html.escape(_page_summary(state.get("message")), quote=True)
        call_state = str(state.get("state", state.get("call_state", "unknown")))
        escaped_state = html.escape(call_state, quote=True)
        endpoint = f"/api/calls/{escaped_call}"
        csrf_value = html.escape(csrf, quote=True)
        controls = ""
        heartbeat_script = ""
        if call_state == "ringing":
            controls = (
                f'<button data-action="accept" data-endpoint="{endpoint}/accept">Accept</button>'
                f'<button data-action="decline" data-endpoint="{endpoint}/decline">Decline</button>'
            )
        elif call_state == "accepted":
            if state.get("owner_device_id") == "iphone_web" and state.get("_page_session_matches_owner"):
                # The caller has already authenticated this exact browser
                # session; the form does not create a second ownership path.
                controls = (
                    f'<form data-endpoint="{endpoint}/submissions"><textarea name="user_input"></textarea>'
                    '<input name="submission_id"><button>Submit</button></form>'
                )
                heartbeat_script = (
                    f"const heartbeatEndpoint='{endpoint}/heartbeat';"
                    "const heartbeatTimer=setInterval(async()=>{"
                    "const response=await fetch(heartbeatEndpoint,{method:'POST',credentials:'same-origin',"
                    "headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},body:'{}'});"
                    "if(!response.ok){clearInterval(heartbeatTimer);"
                    "document.querySelector('[data-state]').textContent='Connection lost.';}"
                    "},10000);"
                )
            else:
                controls = "<p>Another device is handling this call.</p>"
        else:
            controls = f"<p>Call is {escaped_state}.</p>"
        return (
            "<!doctype html><meta charset=utf-8><title>Operator call</title><main>"
            f"<p>{message}</p><p data-state=\"{escaped_state}\">{escaped_state}</p>{controls}"
            f"<meta name=\"X-CSRF-Token\" content=\"{csrf_value}\">"
            "<script>"
            "const csrf=document.querySelector('meta[name=\"X-CSRF-Token\"]').content;"
            "document.querySelectorAll('button[data-action]').forEach((button)=>"
            "button.addEventListener('click',async()=>{"
            "const response=await fetch(button.dataset.endpoint,{method:'POST',credentials:'same-origin',"
            "headers:{'Content-Type':'application/json','X-CSRF-Token':csrf},"
            "body:JSON.stringify({action_id:crypto.randomUUID()})});"
            "if(response.ok)location.reload();"
            "}));"
            f"{heartbeat_script}"
            "</script>"
            f"<a href=\"{endpoint}\">Refresh</a></main>"
        )
    def _start_chunked_json(self, status: HTTPStatus) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()

    def _write_chunked_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self.wfile.write(f"{len(encoded):X}\r\n".encode("ascii"))
        self.wfile.write(encoded)
        self.wfile.write(b"\r\n0\r\n\r\n")
        self.wfile.flush()

    def _terminal_gateway(self) -> _TerminalGateway | None:
        terminal = getattr(self.server, "terminal_gateway", None)
        return terminal if isinstance(terminal, _TerminalGateway) else None

    def _operator_gateway(self) -> _OperatorGateway | None:
        operator = getattr(self.server, "operator_gateway", None)
        return operator if isinstance(operator, _OperatorGateway) else None

    def _operator_session(self) -> str | None:
        for item in self.headers.get("Cookie", "").split(";"):
            name, separator, value = item.strip().partition("=")
            if separator and name == "operator_session" and value:
                return value
        return None

    @staticmethod
    def _web_call_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 3 and parts[:2] == ["", "calls"] and parts[2]:
            return parts[2]
        if len(parts) == 4 and parts[:3] == ["", "api", "calls"] and parts[3]:
            return parts[3]
        return None

    @staticmethod
    def _web_accept_call_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 5 and parts[:3] == ["", "api", "calls"] and parts[3] and parts[4] == "accept":
            return parts[3]
        return None

    @staticmethod
    def _web_submission_call_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 5 and parts[:3] == ["", "api", "calls"] and parts[3] and parts[4] == "submissions":
            return parts[3]
        return None

    @staticmethod
    def _web_decline_call_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 5 and parts[:3] == ["", "api", "calls"] and parts[3] and parts[4] == "decline":
            return parts[3]
        return None

    @staticmethod
    def _web_heartbeat_call_id(path: str) -> str | None:
        parts = path.split("/")
        if len(parts) == 5 and parts[:3] == ["", "api", "calls"] and parts[3] and parts[4] == "heartbeat":
            return parts[3]
        return None

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
    def _terminal_draft_call_id(path: str) -> str | None:
        parts = path.split("/")
        if (
            len(parts) == 5
            and parts[:3] == ["", "api", "calls"]
            and parts[3]
            and parts[4] == "draft"
        ):
            return parts[3]
        return None

    @staticmethod
    def _terminal_action_route(path: str) -> tuple[str, str] | None:
        parts = path.split("/")
        if (
            len(parts) != 5
            or parts[:3] != ["", "api", "calls"]
            or parts[4] not in {"accept", "decline", "cancel", "heartbeat"}
        ):
            return None
        return parts[3], parts[4]

    @staticmethod
    def _terminal_submission_call_id(path: str) -> str | None:
        parts = path.split("/")
        if (
            len(parts) == 5
            and parts[:3] == ["", "api", "calls"]
            and parts[3]
            and parts[4] == "submissions"
        ):
            return parts[3]
        return None

    @staticmethod
    def _terminal_attachment_call_id(path: str) -> str | None:
        parts = path.split("/")
        if (
            len(parts) == 5
            and parts[:3] == ["", "api", "calls"]
            and parts[3]
            and parts[4] == "attachments"
        ):
            return parts[3]
        return None

    def _terminal_request_body(self) -> tuple[bool, object]:
        try:
            content_length = int(self.headers.get("Content-Length", ""))
            if content_length < 0 or content_length > 1_000_000:
                raise ValueError
            return True, json.loads(self.rfile.read(content_length).decode("utf-8"))
        except (UnicodeDecodeError, ValueError, json.JSONDecodeError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
            return False, None

    def _terminal_attachment_body(
        self,
    ) -> tuple[bool, tuple[str, str, str, bytes] | _OversizedTerminalAttachment | None]:
        """Decode the one-file multipart shape supported by this small slice."""
        content_type = self.headers.get("Content-Type", "")
        marker = "boundary="
        if not content_type.startswith("multipart/form-data;") or marker not in content_type:
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
            return False, None
        boundary = content_type.split(marker, 1)[1].strip().strip('"')
        try:
            content_length = int(self.headers.get("Content-Length", ""))
            terminal = self._terminal_gateway()
            max_body_bytes = (
                terminal.attachment_max_submission_bytes + _MULTIPART_OVERHEAD_BYTES
                if terminal is not None
                else _DEFAULT_ATTACHMENT_MAX_SUBMISSION_BYTES + _MULTIPART_OVERHEAD_BYTES
            )
            if not boundary or content_length < 0:
                raise ValueError
            if content_length > max_body_bytes:
                prefix = self.rfile.read(min(content_length, _MULTIPART_OVERHEAD_BYTES))
                owner_session_id = self._multipart_owner_session(prefix, boundary)
                if owner_session_id is None:
                    raise ValueError
                return True, _OversizedTerminalAttachment(owner_session_id)
            raw = self.rfile.read(content_length)
            fields: dict[str, str] = {}
            file_part: tuple[str, str, bytes] | None = None
            delimiter = b"--" + boundary.encode("ascii")
            for part in raw.split(delimiter)[1:]:
                if part in {b"", b"--\r\n", b"--"}:
                    continue
                if not part.startswith(b"\r\n"):
                    raise ValueError
                header_bytes, body = part[2:].split(b"\r\n\r\n", 1)
                body = body.removesuffix(b"\r\n")
                headers = header_bytes.decode("utf-8").split("\r\n")
                disposition = next((line for line in headers if line.lower().startswith("content-disposition:")), "")
                if 'name="owner_session_id"' in disposition and 'filename=' not in disposition:
                    fields["owner_session_id"] = body.decode("utf-8")
                    continue
                if 'name="file"' not in disposition or 'filename="' not in disposition or file_part is not None:
                    raise ValueError
                filename = disposition.split('filename="', 1)[1].split('"', 1)[0]
                media_type = next(
                    (line.split(":", 1)[1].strip() for line in headers if line.lower().startswith("content-type:")),
                    "application/octet-stream",
                )
                file_part = (filename, media_type, body)
            if set(fields) != {"owner_session_id"} or file_part is None or not fields["owner_session_id"]:
                raise ValueError
            filename, media_type, content = file_part
            if not filename or not media_type:
                raise ValueError
            return True, (fields["owner_session_id"], filename, media_type, content)
        except (UnicodeDecodeError, UnicodeEncodeError, ValueError):
            self._send_json(HTTPStatus.BAD_REQUEST, {"error": "invalid request"})
            return False, None

    @staticmethod
    def _multipart_owner_session(raw: bytes, boundary: str) -> str | None:
        delimiter = b"--" + boundary.encode("ascii")
        for part in raw.split(delimiter)[1:]:
            part = part.strip(b"\r\n")
            if not part or part == b"--" or b"\r\n\r\n" not in part:
                continue
            headers, body = part.split(b"\r\n\r\n", 1)
            disposition = next(
                (line for line in headers.split(b"\r\n") if line.lower().startswith(b"content-disposition:")),
                b"",
            )
            if b'name="owner_session_id"' not in disposition:
                continue
            try:
                value = body.rstrip(b"\r\n").decode("utf-8")
            except UnicodeDecodeError:
                return None
            return value or None
        return None

    def _terminal_post(self, path: str) -> None:
        authenticated = self._terminal_request_identity()
        if authenticated is None:
            return
        terminal, identity = authenticated
        if path in {"/api/terminals/register", "/api/terminals/heartbeat"}:
            body_ok, payload = self._terminal_request_body()
            if not body_ok:
                return
            if path.endswith("/register"):
                status, response = terminal.register(identity, payload)
            else:
                status, response = terminal.device_heartbeat(identity, payload)
            self._send_json(status, response)
            return
        attachment_call_id = self._terminal_attachment_call_id(path)
        if attachment_call_id is not None:
            body_ok, attachment = self._terminal_attachment_body()
            if not body_ok or attachment is None:
                return
            if isinstance(attachment, _OversizedTerminalAttachment):
                status, response = terminal.reject_attachment_submission_limit(
                    identity, attachment_call_id, attachment.owner_session_id
                )
                self._send_json(status, response)
                return
            owner_session_id, original_name, media_type, content = attachment
            status, response = terminal.upload_attachment(
                identity,
                attachment_call_id,
                owner_session_id,
                original_name,
                media_type,
                content,
            )
            self._send_json(status, response)
            return
        submission_call_id = self._terminal_submission_call_id(path)
        if submission_call_id is not None:
            body_ok, payload = self._terminal_request_body()
            if not body_ok:
                return
            status, response = terminal.submit_text(identity, submission_call_id, payload)
            self._send_json(status, response)
            return
        route = self._terminal_action_route(path)
        if route is None:
            self._send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        body_ok, payload = self._terminal_request_body()
        if not body_ok:
            return
        call_id, kind = route
        if kind == "heartbeat":
            status, response = terminal.heartbeat(identity, call_id, payload)
        else:
            status, response = terminal.action(identity, kind, call_id, payload)
        self._send_json(status, response)

    def log_message(self, _format: str, *_args: object) -> None:
        """Avoid writing request contents to diagnostic output."""


def _page_summary(message: object) -> str:
    """Display a narrow prompt summary instead of a serialized tool request."""
    if not isinstance(message, str):
        return ""
    try:
        decoded = json.loads(message)
    except json.JSONDecodeError:
        return message
    if isinstance(decoded, dict):
        title = decoded.get("title")
        if isinstance(title, str):
            return title
    return "Operator call"


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


class BarkDeliveryScheduler:
    """One-shot persisted Bark sender kept outside the MCP request path."""

    def __init__(self, coordinator: CallCoordinator, bark_url: str, web_base_url: str) -> None:
        self._coordinator = coordinator
        self._bark_url = bark_url
        self._web_base_url = web_base_url.rstrip("/")
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._coordinator.recover_bark_deliveries()
        self._thread = threading.Thread(target=self._run, name="bark-delivery-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopped.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
            self._thread = None

    def _run(self) -> None:
        while not self._stopped.is_set():
            delivery = self._coordinator.claim_next_bark_delivery()
            if delivery is None:
                self._stopped.wait(0.03)
                continue
            if not self._coordinator.authorize_bark_send(delivery["call_id"]):
                continue
            self._coordinator.finish_bark_delivery(
                delivery["call_id"], self._send(delivery["call_id"], delivery["message"])
            )

    def _send(self, call_id: str, message: str) -> str:
        title = _bark_title(message)
        parsed = urlsplit(self._bark_url)
        query = parse_qsl(parsed.query, keep_blank_values=True)
        query.extend(
            (
                ("title", title),
                ("body", "Open the secure response page."),
                ("url", f"{self._web_base_url}/calls/{call_id}"),
                ("call", "1"),
            )
        )
        request_url = urlunsplit(
            (parsed.scheme, parsed.netloc, parsed.path, urlencode(query, quote_via=quote), "")
        )
        try:
            with urlopen(request_url, timeout=2.5) as response:  # noqa: S310 - configured Bark endpoint
                body = response.read()
                if not 200 <= response.status < 300:
                    return "failed"
            try:
                decoded = json.loads(body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                return "failed"
            return "success" if isinstance(decoded, dict) and decoded.get("code") == 200 else "failed"
        except (TimeoutError, URLError, OSError):
            return "timeout"


def _bark_title(message: str) -> str:
    """Extract only the short display title; never send full tool arguments."""
    try:
        arguments = json.loads(message).get("arguments", {})
    except (json.JSONDecodeError, AttributeError):
        return "Human input requested"
    title = arguments.get("title") if isinstance(arguments, dict) else None
    return title if isinstance(title, str) and title else "Human input requested"


def _build_tls_context(config: ServiceConfig) -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=config.certfile, keyfile=config.keyfile)
    return context


def _create_secure_server(
    config: ServiceConfig,
    context: ssl.SSLContext,
    gateway: _McpGateway | None = None,
    terminal_gateway: _TerminalGateway | None = None,
    operator_gateway: _OperatorGateway | None = None,
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
    if operator_gateway is not None:
        server.operator_gateway = operator_gateway  # type: ignore[attr-defined]
    return server


def _serve(config: ServiceConfig) -> None:
    context = _build_tls_context(config)
    credentials = _load_agent_credentials(config.agent_credentials_file)
    terminal_credentials = _load_terminal_credentials(config.terminal_credentials_file)
    operator_record = _load_operator_credentials(config.operator_credentials_file)
    config.data_dir.mkdir(parents=True, exist_ok=True)
    clock = _SystemClock()
    coordinator = CallCoordinator(
        config.data_dir / "calls.sqlite3",
        clock=clock,
        attachment_max_file_bytes=config.attachment_max_file_bytes,
        attachment_max_submission_bytes=config.attachment_max_submission_bytes,
        attachment_max_archive_bytes=config.attachment_max_archive_bytes,
    )
    scheduler = RingingDeadlineScheduler(coordinator, clock)
    bark_scheduler = (
        BarkDeliveryScheduler(coordinator, config.bark_url, config.web_base_url)
        if config.bark_enabled and config.bark_url is not None
        else None
    )
    try:
        coordinator.recover_and_settle()
        scheduler.start()
        if bark_scheduler is not None:
            bark_scheduler.start()
        configured_desktop_route_device_id = (
            config.eligible_desktop_device_ids[0] if config.eligible_desktop_device_ids else None
        )
        gateway = _McpGateway(
            coordinator, credentials, configured_desktop_route_device_id, config.bark_enabled
        )
        terminal_gateway = _TerminalGateway(
            coordinator,
            terminal_credentials,
            configured_desktop_route_device_id,
            attachment_max_submission_bytes=config.attachment_max_submission_bytes,
        )
        operator_gateway = (
            _OperatorGateway(coordinator, operator_record, urlsplit(config.web_base_url).netloc)
            if operator_record is not None
            else None
        )
        server = _create_secure_server(config, context, gateway, terminal_gateway, operator_gateway)
        try:
            server.serve_forever()
        finally:
            server.server_close()
    finally:
        if bark_scheduler is not None:
            bark_scheduler.stop()
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
