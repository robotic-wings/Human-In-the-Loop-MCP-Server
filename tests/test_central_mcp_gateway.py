"""Red contracts for the initial HTTPS MCP ingress only.

These tests deliberately do not cover long waits or continuation.  They exercise
the public JSON-RPC boundary over a real TLS loopback listener.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import socket
import ssl
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from call_coordinator import CallCoordinator, CreateCall


ROOT = Path(__file__).resolve().parents[1]
HUMAN_TOOLS = {
    "assign_task_to_human",
    "get_user_input",
    "get_user_choice",
    "get_multiline_input",
    "show_confirmation_dialog",
    "show_info_message",
    "get_feedback_from_user",
}


class FrozenClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def now_utc(self) -> datetime:
        return self.now


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _write_certificate(directory: Path) -> tuple[Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=1))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.IPv4Address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    certfile = directory / "test-cert.pem"
    keyfile = directory / "test-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


def _write_gateway_config(directory: Path, port: int) -> tuple[Path, Path]:
    certfile, keyfile = _write_certificate(directory)
    credentials = directory / "agent-credentials.json"
    credentials.write_text(
        json.dumps(
            {"test-token-alpha": "agent-alpha", "test-token-beta": "agent-beta"}
        ),
        encoding="utf-8",
    )
    config = directory / "central-service.json"
    config.write_text(
        json.dumps(
            {
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "web_base_url": f"https://127.0.0.1:{port}",
                "data_dir": str(directory / "data"),
                "https_certfile": str(certfile),
                "https_keyfile": str(keyfile),
                "agent_credentials_file": str(credentials),
            }
        ),
        encoding="utf-8",
    )
    return config, certfile


def _start(config_path: Path) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-m", "central_service", "--config", str(config_path)],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_listener(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        if process.poll() is not None:
            pytest.fail("central service stopped before opening its TLS listener")
        with socket.socket() as probe:
            probe.settimeout(0.1)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.03)
    pytest.fail("central service did not open its TLS listener")


def _stop(process: subprocess.Popen[str]) -> None:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    if process.stdout is not None:
        process.stdout.close()
    if process.stderr is not None:
        process.stderr.close()


def _post(
    port: int, certfile: Path, payload: dict[str, Any], token: str | None
) -> tuple[int, dict[str, Any], float]:
    context = ssl.create_default_context(cafile=str(certfile))
    connection = http.client.HTTPSConnection(
        "127.0.0.1", port, context=context, timeout=2
    )
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    started = time.monotonic()
    connection.request("POST", "/mcp", body=json.dumps(payload), headers=headers)
    response = connection.getresponse()
    header_seconds = time.monotonic() - started
    raw = response.read()
    connection.close()
    try:
        body = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        body = {"jsonrpc": "2.0", "error": {"code": "non_json_response"}}
    return response.status, body, header_seconds


def _rpc(method: str, params: dict[str, Any], request_id: int = 1) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}


def _call(request_id: str, *, caller_id: str | None = None) -> dict[str, Any]:
    arguments: dict[str, Any] = {
        "title": "Need an answer",
        "prompt": "Which option should we use?",
        "request_id": request_id,
    }
    if caller_id is not None:
        arguments["caller_id"] = caller_id
    return _rpc("tools/call", {"name": "get_user_input", "arguments": arguments}, 3)


def _structured(response: dict[str, Any]) -> dict[str, Any]:
    assert "error" not in response
    result = response["result"]
    assert isinstance(result, dict)
    value = result.get("structuredContent")
    assert isinstance(value, dict)
    return value


@pytest.fixture
def gateway(tmp_path: Path):
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        yield port, certificate, config, process
    finally:
        _stop(process)


def test_initialize_and_tools_list_complete_over_tls_within_initial_response_boundary(
    gateway: tuple[int, Path, Path, subprocess.Popen[str]],
) -> None:
    port, certificate, _, _ = gateway
    status, initialized, headers_in = _post(
        port,
        certificate,
        _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}}),
        "test-token-alpha",
    )
    assert status == 200
    assert headers_in < 1
    assert "result" in initialized
    status, listed, headers_in = _post(
        port, certificate, _rpc("tools/list", {}), "test-token-alpha"
    )
    assert status == 200
    assert headers_in < 1
    assert HUMAN_TOOLS <= {tool["name"] for tool in listed["result"]["tools"]}


def test_authenticated_tool_call_is_ringing_and_second_caller_is_busy(
    gateway: tuple[int, Path, Path, subprocess.Popen[str]],
) -> None:
    port, certificate, _, _ = gateway
    status, response, _ = _post(
        port, certificate, _call("create-1"), "test-token-alpha"
    )
    assert status == 200
    created = _structured(response)
    assert created["success"] is True
    assert created["status"] == "ringing"
    assert created["call_state"] == "ringing"
    assert isinstance(created["call_id"], str)
    status, response, _ = _post(port, certificate, _call("create-2"), "test-token-beta")
    assert status == 200
    busy = _structured(response)
    assert busy["success"] is True
    assert busy["status"] == "busy"
    assert busy["needs_continuation"] is False


def test_request_replay_conflict_and_credential_identity_are_public_gateway_behavior(
    gateway: tuple[int, Path, Path, subprocess.Popen[str]],
) -> None:
    port, certificate, _, _ = gateway
    status, first, _ = _post(
        port, certificate, _call("stable", caller_id="forged-b"), "test-token-alpha"
    )
    assert status == 200
    status, replay, _ = _post(
        port,
        certificate,
        _call("stable", caller_id="forged-other"),
        "test-token-alpha",
    )
    assert status == 200
    assert _structured(replay) == _structured(first)
    altered = _call("stable")
    altered["params"]["arguments"]["prompt"] = "A different question"
    status, conflict, _ = _post(port, certificate, altered, "test-token-alpha")
    assert status == 200
    assert _structured(conflict)["status"] == "request_conflict"


def test_missing_and_wrong_credentials_are_indistinguishable_and_have_no_side_effect(
    gateway: tuple[int, Path, Path, subprocess.Popen[str]],
) -> None:
    port, certificate, _, _ = gateway
    missing_status, missing, _ = _post(port, certificate, _call("no-auth"), None)
    wrong_status, wrong, _ = _post(port, certificate, _call("no-auth"), "wrong-token")
    assert missing_status == wrong_status == 401
    assert missing == wrong
    status, valid, _ = _post(port, certificate, _call("no-auth"), "test-token-alpha")
    assert status == 200
    assert _structured(valid)["status"] == "ringing"


def test_restart_preserves_active_occupancy_at_public_mcp_boundary(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    first = _start(config)
    _wait_for_listener(first, port)
    try:
        status, _, _ = _post(port, certificate, _call("persisted"), "test-token-alpha")
        assert status == 200
    finally:
        _stop(first)
    second = _start(config)
    _wait_for_listener(second, port)
    try:
        status, response, _ = _post(
            port, certificate, _call("after-restart"), "test-token-beta"
        )
        assert status == 200
        assert _structured(response)["status"] == "busy"
    finally:
        _stop(second)


def test_startup_settles_preseeded_expired_ringing_before_mcp_admission(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    clock = FrozenClock(datetime(2020, 1, 1, tzinfo=timezone.utc))
    coordinator = CallCoordinator(data_dir / "calls.sqlite3", clock=clock)
    coordinator.create_call(
        CreateCall("operator-default", "seed", "old", "seed-request", "old", 1)
    )
    coordinator.close()
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        status, response, _ = _post(
            port, certificate, _call("new-after-expiry"), "test-token-alpha"
        )
        assert status == 200
        assert _structured(response)["status"] == "ringing"
    finally:
        _stop(process)
