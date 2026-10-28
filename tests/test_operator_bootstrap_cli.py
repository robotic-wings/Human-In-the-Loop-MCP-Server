"""Red contract for the central-service sole-operator bootstrap command."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener, _write_gateway_config
from test_iphone_web_acceptance import _create_call, _request


ROOT = Path(__file__).resolve().parents[1]


def _bootstrap(config: Path, operator_id: str, password: str) -> subprocess.CompletedProcess[str]:
    """Exercise the CLI while replacing its secure prompt seam, never argv."""
    program = (
        "import central_service, os, sys; "
        "central_service._read_operator_bootstrap_password = lambda: os.environ['TEST_OPERATOR_PASSWORD']; "
        "central_service.main(['--config', sys.argv[1], 'create-operator', '--operator-id', sys.argv[2]])"
    )
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment["TEST_OPERATOR_PASSWORD"] = password
    return subprocess.run(
        [sys.executable, "-c", program, str(config), operator_id],
        cwd=ROOT,
        text=True,
        capture_output=True,
        env=environment,
        timeout=10,
    )


def _bootstrap_config(tmp_path: Path) -> tuple[Path, Path, Path]:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    credential_file = tmp_path / "sole-operator.json"
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["operator_credentials_file"] = str(credential_file)
    config.write_text(json.dumps(raw), encoding="utf-8")
    return config, certificate, credential_file


def test_create_operator_uses_secure_prompt_and_creates_an_opaque_sole_record(tmp_path: Path) -> None:
    config, _certificate, credentials = _bootstrap_config(tmp_path)
    password = "correct horse battery staple"

    created = _bootstrap(config, "operator-from-bootstrap", password)

    assert created.returncode == 0, created.stderr
    assert password not in created.stdout + created.stderr
    raw = json.loads(credentials.read_text(encoding="utf-8"))
    assert raw["operator_id"] == "operator-from-bootstrap"
    record = raw["password_hash"]
    assert record["algorithm"] == "scrypt"
    assert {"salt", "n", "r", "p", "digest"} <= set(record)
    assert password not in credentials.read_text(encoding="utf-8")
    output = created.stdout + created.stderr
    assert json.dumps(record, sort_keys=True) not in output
    assert str(record["salt"]) not in output
    assert str(record["digest"]) not in output
    assert "--password" not in (created.args if isinstance(created.args, str) else " ".join(created.args))


def test_create_operator_is_atomic_non_overwriting_and_credentials_drive_login_and_calls(tmp_path: Path) -> None:
    config, certificate, credentials = _bootstrap_config(tmp_path)
    first = _bootstrap(config, "operator-from-bootstrap", "correct horse battery staple")
    before = credentials.read_bytes()
    duplicate = _bootstrap(config, "other-operator", "new password should not replace")
    assert first.returncode == 0, first.stderr
    assert duplicate.returncode != 0
    assert credentials.read_bytes() == before

    raw = json.loads(config.read_text(encoding="utf-8"))
    port = raw["listen_port"]
    assert isinstance(port, int)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        good = _request(port, certificate, "POST", "/api/operator/login", body={"password": "correct horse battery staple"})
        wrong = _request(port, certificate, "POST", "/api/operator/login", body={"password": "not the operator password"})
        created = _create_call(port, certificate, "operator-bootstrap-call")
    finally:
        _stop(process)

    assert good[0] == 200
    assert wrong[0] in {400, 401, 403}
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    database = tmp_path / "data" / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        row = connection.execute("SELECT operator_id FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    assert row == ("operator-from-bootstrap",)


def test_bootstrap_preserves_existing_cli_rejections(tmp_path: Path) -> None:
    config, _certificate, _credentials = _bootstrap_config(tmp_path)
    for arguments in ([], ["--stdio"], ["--local"], ["--service"]):
        completed = subprocess.run(
            [sys.executable, "-m", "central_service", *arguments], cwd=ROOT, text=True, capture_output=True, timeout=10
        )
        assert completed.returncode == 2
    assert _bootstrap(config, "operator-from-bootstrap", "safe test password").returncode == 0


def test_create_operator_rejects_password_command_line_options(tmp_path: Path) -> None:
    config, _certificate, credentials = _bootstrap_config(tmp_path)
    completed = subprocess.run(
        [
            sys.executable, "-m", "central_service", "--config", str(config),
            "create-operator", "--operator-id", "operator-from-bootstrap", "--password", "not-accepted",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=10,
    )
    assert completed.returncode == 2
    assert "password command-line options are rejected" in completed.stderr
    assert not credentials.exists()


def test_create_operator_is_atomic_under_concurrent_bootstrap_attempts(tmp_path: Path) -> None:
    config, _certificate, credentials = _bootstrap_config(tmp_path)
    contenders = (
        ("operator-one", "first independent password"),
        ("operator-two", "second independent password"),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        attempts = list(executor.map(lambda item: _bootstrap(config, *item), contenders))

    winners = [item for item, completed in zip(contenders, attempts) if completed.returncode == 0]
    assert len(winners) == 1
    assert sum(completed.returncode != 0 for completed in attempts) == 1
    raw = json.loads(credentials.read_text(encoding="utf-8"))
    assert raw["operator_id"] == winners[0][0]
    record = raw["password_hash"]
    assert record["algorithm"] == "scrypt"
    assert {"salt", "n", "r", "p", "digest"} <= set(record)
