"""Red TLS contracts for the authenticated read-only MCP operator profile."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import (
    _free_port,
    _post,
    _rpc,
    _start,
    _stop,
    _structured,
    _wait_for_listener,
    _write_gateway_config,
)
from test_iphone_web_acceptance import _write_operator_config


OPERATOR_PROFILE = "get_operator_profile"


def _snapshot(database: Path, config: Path) -> tuple[dict[str, int], bytes, bytes | None]:
    with sqlite3.connect(database) as connection:
        tables = [
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name"
            )
            if not str(row[0]).startswith("sqlite_")
        ]
        counts = {
            table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
            for table in tables
        }
    raw = json.loads(config.read_text(encoding="utf-8"))
    credential_value = raw.get("operator_credentials_file")
    credentials = (
        Path(credential_value).read_bytes()
        if isinstance(credential_value, str)
        else None
    )
    return counts, config.read_bytes(), credentials


def _profile_call(
    port: int, certificate: Path, token: str | None, arguments: object = None
) -> tuple[int, dict[str, Any]]:
    value = {} if arguments is None else arguments
    status, response, _ = _post(
        port,
        certificate,
        _rpc("tools/call", {"name": OPERATOR_PROFILE, "arguments": value}),
        token,
    )
    return status, response


def test_authenticated_operator_profile_is_advertised_returns_only_configured_id_and_never_mutates(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database, config)
        list_status, listed, _ = _post(
            port, certificate, _rpc("tools/list", {}), "test-token-alpha"
        )
        profile_status, profile = _profile_call(port, certificate, "test-token-alpha")
        after = _snapshot(database, config)
    finally:
        _stop(process)

    assert list_status == profile_status == 200
    assert OPERATOR_PROFILE in {tool["name"] for tool in listed["result"]["tools"]}
    assert _structured(profile) == {"operator_id": "operator-default"}
    assert after == before
    rendered = json.dumps(profile, sort_keys=True).lower()
    for forbidden in (
        "password_hash", "scrypt", "test-token", "agent-alpha", "callback",
        "bark", "session", "csrf", "calls.sqlite", "data_dir", "web_base_url",
    ):
        assert forbidden not in rendered


def test_operator_profile_default_gateway_is_explicitly_not_configured_without_creating_state(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_gateway_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database, config)
        status, profile = _profile_call(port, certificate, "test-token-alpha")
        after = _snapshot(database, config)
    finally:
        _stop(process)

    assert status == 200
    assert _structured(profile) == {"operator_id": None, "status": "not_configured"}
    assert after == before
    assert "operator-default" not in json.dumps(profile, sort_keys=True).lower()


def test_operator_profile_rejects_arguments_and_foreign_selection_and_requires_bearer_auth(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database, config)
        missing_status, missing = _profile_call(port, certificate, None)
        bad_status, bad = _profile_call(port, certificate, "foreign-token")
        arguments_status, arguments = _profile_call(
            port, certificate, "test-token-beta", {"operator_id": "operator-default"}
        )
        foreign_status, foreign = _profile_call(
            port, certificate, "test-token-beta", {"operator_id": "other-operator"}
        )
        after = _snapshot(database, config)
    finally:
        _stop(process)

    assert missing_status == bad_status == 401
    assert missing["error"] == bad["error"] == {"code": -32001, "message": "unauthorized"}
    for response in (arguments, foreign):
        assert response["error"] == {"code": -32602, "message": "invalid params"}
        rendered = json.dumps(response, sort_keys=True).lower()
        assert "operator-default" not in rendered and "other-operator" not in rendered
        assert "password_hash" not in rendered and "scrypt" not in rendered
    assert arguments_status == foreign_status == 200
    assert after == before
