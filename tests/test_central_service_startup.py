"""Black-box startup contracts for the GUI-free central-service slice."""

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
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
NORMAL_HTTP_STATUSES = {200, 400, 401, 403, 404, 405}


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]
    )
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "test-cert.pem"
    key_path = directory / "test-key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


def _write_config(
    directory: Path, *, port: int, web_base_url: str | None = None
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    cert_path, key_path = _write_certificate(directory)
    config = {
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "web_base_url": web_base_url or f"https://127.0.0.1:{port}",
        "data_dir": str(directory / "data"),
        "https_certfile": str(cert_path),
        "https_keyfile": str(key_path),
    }
    config_path = directory / "central-service.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    return config_path


def _process_command(config_path: Path, *, block_tk: bool) -> list[str]:
    if not block_tk:
        return [sys.executable, "-m", "central_service", "--config", str(config_path)]
    guard = (
        "import builtins,runpy,sys;"
        "original=builtins.__import__;"
        "builtins.__import__=lambda name,*a,**k:"
        "(_ for _ in ()).throw(ImportError('Tk prohibited'))"
        " if name=='tkinter' or name.startswith('tkinter.')"
        " else original(name,*a,**k);"
        "sys.argv=['central_service','--config',sys.argv[1]];"
        "runpy.run_module('central_service',run_name='__main__')"
    )
    return [sys.executable, "-c", guard, str(config_path)]


def _start_service(
    config_path: Path, *, block_tk: bool = False
) -> subprocess.Popen[str]:
    return subprocess.Popen(
        _process_command(config_path, block_tk=block_tk),
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), "DISPLAY": ""},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _wait_for_listener(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"central service exited early:\n{stdout}\n{stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=3)
    pytest.fail(f"central service did not listen on {port}:\n{stdout}\n{stderr}")


def _request_public_route(port: int, cert_path: Path) -> int:
    context = ssl.create_default_context(cafile=str(cert_path))
    connection = http.client.HTTPSConnection(
        "127.0.0.1", port, context=context, timeout=3
    )
    try:
        connection.request("GET", "/calls/not-a-secret-call-id")
        return connection.getresponse().status
    finally:
        connection.close()


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=3)


def _cannot_connect(port: int) -> bool:
    with socket.socket() as probe:
        probe.settimeout(0.2)
        return probe.connect_ex(("127.0.0.1", port)) != 0


def _run(
    config_path: Path | None = None, *arguments: str
) -> subprocess.CompletedProcess[str]:
    command = [sys.executable, "-m", "central_service", *arguments]
    if config_path is not None:
        command.extend(["--config", str(config_path)])
    return subprocess.run(
        command,
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )


def test_central_service_starts_headlessly_without_importing_tk(tmp_path: Path) -> None:
    port = _free_port()
    config_path = _write_config(tmp_path, port=port)
    process = _start_service(config_path, block_tk=True)
    try:
        _wait_for_listener(process, port)
        status = _request_public_route(port, tmp_path / "test-cert.pem")
        assert status in NORMAL_HTTP_STATUSES
    finally:
        _stop(process)


def test_configured_listener_and_public_route_follow_explicit_settings(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config_path = _write_config(tmp_path, port=port)
    process = _start_service(config_path)
    try:
        _wait_for_listener(process, port)
        status = _request_public_route(port, tmp_path / "test-cert.pem")
        assert status in NORMAL_HTTP_STATUSES
    finally:
        _stop(process)


def test_two_isolated_configurations_use_only_their_own_listener(
    tmp_path: Path,
) -> None:
    first_port = _free_port()
    second_port = _free_port()
    first = _write_config(tmp_path / "first", port=first_port)
    second = _write_config(tmp_path / "second", port=second_port)

    first_process = _start_service(first)
    try:
        _wait_for_listener(first_process, first_port)
        assert _cannot_connect(second_port)
        assert _request_public_route(
            first_port, first.parent / "test-cert.pem"
        ) in NORMAL_HTTP_STATUSES
    finally:
        _stop(first_process)

    second_process = _start_service(second)
    try:
        _wait_for_listener(second_process, second_port)
        assert _cannot_connect(first_port)
        assert _request_public_route(
            second_port, second.parent / "test-cert.pem"
        ) in NORMAL_HTTP_STATUSES
    finally:
        _stop(second_process)


@pytest.mark.parametrize(
    "configuration",
    [
        {},
        {
            "listen_host": "127.0.0.1",
            "web_base_url": "https://127.0.0.1:1",
            "data_dir": "data",
            "https_certfile": "missing-cert.pem",
            "https_keyfile": "missing-key.pem",
        },
    ],
)
def test_invalid_configuration_fails_without_creating_a_listener(
    tmp_path: Path, configuration: dict[str, object]
) -> None:
    port = _free_port()
    configuration["listen_port"] = port
    config_path = tmp_path / "invalid.json"
    config_path.write_text(json.dumps(configuration), encoding="utf-8")

    result = _run(config_path)

    assert result.returncode != 0
    assert "config" in (result.stdout + result.stderr).lower()
    assert _cannot_connect(port)


@pytest.mark.parametrize("legacy_argument", ["--stdio", "--local", "--service"])
def test_legacy_modes_are_rejected_without_fallback(legacy_argument: str) -> None:
    result = _run(None, legacy_argument)

    assert result.returncode != 0
    output = (result.stdout + result.stderr).lower()
    assert "rejected" in output or "not supported" in output


def test_no_argument_entrypoint_does_not_start_stdio() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "central_service"],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT)},
        text=True,
        capture_output=True,
        check=False,
        timeout=5,
    )

    assert result.returncode != 0
    assert "config" in (result.stdout + result.stderr).lower()


def test_missing_https_credentials_do_not_create_a_plaintext_listener(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config_path = tmp_path / "plaintext.json"
    config_path.write_text(
        json.dumps(
            {
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "web_base_url": f"http://127.0.0.1:{port}",
                "data_dir": str(tmp_path / "data"),
            }
        ),
        encoding="utf-8",
    )

    result = _run(config_path)

    assert result.returncode != 0
    assert "https" in (result.stdout + result.stderr).lower()
    assert _cannot_connect(port)
