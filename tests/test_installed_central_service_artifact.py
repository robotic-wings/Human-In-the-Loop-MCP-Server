"""Installed-distribution contracts for the HTTP-only central service."""

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
import venv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


ROOT = Path(__file__).resolve().parents[1]
CENTRAL_SCRIPT = "hitl-central-service"
LEGACY_SCRIPTS = {"hitl-mcp-server", "hitl_mcp_server", "hitl-management-console"}


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _write_certificate(directory: Path) -> tuple[Path, Path]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
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
    certfile = directory / "service-cert.pem"
    keyfile = directory / "service-key.pem"
    certfile.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    keyfile.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return certfile, keyfile


def _write_config(directory: Path, port: int) -> tuple[Path, Path, Path]:
    directory.mkdir(parents=True, exist_ok=True)
    certfile, keyfile = _write_certificate(directory)
    credentials = directory / "agents.json"
    credentials.write_text(json.dumps({"artifact-agent-token": "artifact-agent"}))
    data_dir = directory / "persistent-data"
    config = directory / "central-service.json"
    config.write_text(
        json.dumps(
            {
                "listen_host": "127.0.0.1",
                "listen_port": port,
                "web_base_url": f"https://127.0.0.1:{port}",
                "data_dir": str(data_dir),
                "https_certfile": str(certfile),
                "https_keyfile": str(keyfile),
                "agent_credentials_file": str(credentials),
            }
        ),
        encoding="utf-8",
    )
    return config, certfile, data_dir


def _venv_python(environment: Path) -> Path:
    return environment / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def _venv_scripts(environment: Path) -> Path:
    return environment / ("Scripts" if os.name == "nt" else "bin")


@pytest.fixture(scope="module")
def installed_artifact(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    root = tmp_path_factory.mktemp("installed-central-artifact")
    wheelhouse = root / "wheelhouse"
    wheelhouse.mkdir()
    built = subprocess.run(
        [sys.executable, "-m", "pip", "wheel", "--no-deps", "--wheel-dir", str(wheelhouse), str(ROOT)],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheels = list(wheelhouse.glob("hitl_mcp_server-*.whl"))
    assert len(wheels) == 1

    environment = root / "venv"
    venv.EnvBuilder(with_pip=True, system_site_packages=True).create(environment)
    python = _venv_python(environment)
    installed = subprocess.run(
        [str(python), "-m", "pip", "install", "--no-deps", str(wheels[0])],
        cwd=root,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    return environment, wheels[0]


def _command(environment: Path, script: str, *arguments: str) -> list[str]:
    scripts = _venv_scripts(environment)
    executable = scripts / (f"{script}.exe" if os.name == "nt" else script)
    assert executable.is_file(), f"installed artifact lacks required {script} entrypoint"
    return [str(executable), *arguments]


def _wait_for_listener(process: subprocess.Popen[str], port: int) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            pytest.fail(f"installed central service exited early:\n{stdout}\n{stderr}")
        with socket.socket() as probe:
            probe.settimeout(0.2)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    process.terminate()
    stdout, stderr = process.communicate(timeout=3)
    pytest.fail(f"installed central service did not listen:\n{stdout}\n{stderr}")


def _stop(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.terminate()
    process.communicate(timeout=5)


def _mcp_tools_list(port: int, certfile: Path) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPSConnection(
        "127.0.0.1",
        port,
        context=ssl.create_default_context(cafile=str(certfile)),
        timeout=3,
    )
    try:
        connection.request(
            "POST",
            "/mcp",
            body=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": "Bearer artifact-agent-token",
            },
        )
        response = connection.getresponse()
        return response.status, json.loads(response.read().decode("utf-8"))
    finally:
        connection.close()


def _plaintext_http_bytes(port: int) -> bytes:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2) as connection:
            connection.settimeout(2)
            connection.sendall(
                b"GET /mcp HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            return connection.recv(128)
    except OSError:
        return b""


def test_installed_distribution_ships_only_central_service_artifact(
    installed_artifact: tuple[Path, Path],
) -> None:
    environment, _ = installed_artifact
    scripts = {path.stem for path in _venv_scripts(environment).iterdir()}
    assert CENTRAL_SCRIPT in scripts
    assert LEGACY_SCRIPTS.isdisjoint(scripts)

    imported = subprocess.run(
        [
            str(_venv_python(environment)),
            "-c",
            "import central_service,call_coordinator; print(central_service.__file__); print(call_coordinator.__file__)",
        ],
        cwd=environment.parent,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert str(ROOT) not in imported.stdout

    for legacy_module in ("human_loop_server", "management_console"):
        rejected = subprocess.run(
            [str(_venv_python(environment)), "-c", f"import {legacy_module}"],
            cwd=environment.parent,
            env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
            text=True,
            capture_output=True,
            check=False,
            timeout=10,
        )
        assert rejected.returncode != 0
        assert "ModuleNotFoundError" in rejected.stderr


def test_installed_central_service_uses_configured_tls_sqlite_and_authenticated_mcp(
    installed_artifact: tuple[Path, Path], tmp_path: Path
) -> None:
    environment, wheel = installed_artifact
    port = _free_port()
    config, certfile, data_dir = _write_config(tmp_path, port)
    process = subprocess.Popen(
        _command(environment, CENTRAL_SCRIPT, "--config", str(config)),
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        _wait_for_listener(process, port)
        status, body = _mcp_tools_list(port, certfile)
        assert status == 200
        assert "result" in body
        assert not _plaintext_http_bytes(port).startswith(b"HTTP/")
        assert (data_dir / "calls.sqlite3").is_file()
    finally:
        _stop(process)

    marker = data_dir / "must-survive-reinstall.txt"
    marker.write_text("do not mutate", encoding="utf-8")
    reinstalled = subprocess.run(
        [str(_venv_python(environment)), "-m", "pip", "install", "--force-reinstall", "--no-deps", str(wheel)],
        cwd=tmp_path,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert reinstalled.returncode == 0, reinstalled.stdout + reinstalled.stderr
    assert marker.read_text(encoding="utf-8") == "do not mutate"


@pytest.mark.parametrize("arguments", [(), ("--stdio",)])
def test_installed_entrypoint_rejects_missing_config_and_stdio_without_listener(
    installed_artifact: tuple[Path, Path], arguments: tuple[str, ...]
) -> None:
    environment, _ = installed_artifact
    port = _free_port()
    result = subprocess.run(
        _command(environment, CENTRAL_SCRIPT, *arguments),
        cwd=environment.parent,
        env={key: value for key, value in os.environ.items() if key != "PYTHONPATH"},
        text=True,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode != 0
    assert "config" in (result.stdout + result.stderr).lower() or "rejected" in (result.stdout + result.stderr).lower()
    with socket.socket() as probe:
        probe.settimeout(0.2)
        assert probe.connect_ex(("127.0.0.1", port)) != 0
