"""GUI-free HTTP ingress for the central call coordinator.

This deliberately narrow adapter owns configuration validation and the
``POST /mcp`` boundary.  It does not provide a user interface, notification
transport, or the complete MCP wire protocol; those remain separate slices.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import json
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from central_call_coordinator import CallCoordinator


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

        return cls(
            listen_host=listen_host,
            listen_port=port,
            web_base_url=web_base_url,
            data_dir=data_dir,
            agent_credentials=credentials,
            tls_cert_file=required_text("tls_cert_file"),
            tls_key_file=required_text("tls_key_file"),
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

        response = self._dispatch(caller_id, payload)
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [(b"content-type", b"application/json; charset=utf-8")],
            }
        )
        if response.get("needs_continuation"):
            await self._wait(self._wait_leg_seconds(float(timeout)))
        await send(
            {
                "type": "http.response.body",
                "body": json.dumps(response, separators=(",", ":")).encode("utf-8"),
                "more_body": False,
            }
        )

    def _dispatch(self, caller_id: str, request: Mapping[str, object]) -> dict[str, object]:
        request_id = request.get("request_id")
        if not isinstance(request_id, str) or not request_id:
            return {"success": False, "status": "invalid_request", "needs_continuation": False}
        interaction_id = request.get("interaction_id")
        call_id = request.get("call_id")
        if interaction_id is not None or call_id is not None:
            if not isinstance(interaction_id, str) or not isinstance(call_id, str):
                return {"success": False, "status": "missing_call_id", "needs_continuation": False}
            call = self._coordinator.get_call(call_id)
            if call is None:
                return {"success": False, "status": "session_not_found", "needs_continuation": False}
            return self._heartbeat(interaction_id, call_id, call)

        body = request.get("payload")
        if not isinstance(body, Mapping):
            return {"success": False, "status": "invalid_request", "needs_continuation": False}
        outcome = self._coordinator.create_call(
            caller_id=caller_id, request_id=request_id, payload=body
        )
        if outcome.get("status") == "busy":
            return outcome
        call_id_value = outcome["call_id"]
        assert isinstance(call_id_value, str)
        stable_interaction_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hitl:{call_id_value}"))
        return self._heartbeat(
            stable_interaction_id, call_id_value, outcome, caller_id=caller_id
        )

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

    app = create_app(config, clock=lambda: __import__("time").time(), notify=lambda _: None, wait=real_wait)
    uvicorn.run(app, host=config.listen_host, port=config.listen_port, ssl_certfile=config.tls_cert_file, ssl_keyfile=config.tls_key_file)


if __name__ == "__main__":  # pragma: no cover
    main(sys.argv[1:])
