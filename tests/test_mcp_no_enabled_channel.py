"""Red HTTPS contracts for refusing calls when no human channel is enabled."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _call, _free_port, _post, _rpc, _start, _stop, _structured, _wait_for_listener
from test_iphone_browser_drafts import _writer_headers
from test_iphone_task_progress_submission import _accept, _submit
from test_iphone_web_acceptance import _login, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _snapshot(database: Path) -> tuple[int, ...]:
    tables = (
        "calls", "create_operations", "revision_operations", "events", "results",
        "bark_deliveries", "terminal_callback_deliveries",
    )
    with sqlite3.connect(database) as connection:
        return tuple(
            int(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0])
            for table in tables
        )


def _task_request(request_id: str, **arguments: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "task_title": "No route task",
        "task_description": "Do the route-free task",
        "request_id": request_id,
    }
    payload.update(arguments)
    return _rpc("tools/call", {"name": "assign_task_to_human", "arguments": payload}, 9)


def _disable_all_channels(config: Path) -> None:
    raw = json.loads(config.read_text(encoding="utf-8"))
    raw["eligible_desktop_device_ids"] = []
    raw["bark_enabled"] = False
    raw.pop("bark_url", None)
    config.write_text(json.dumps(raw), encoding="utf-8")


def test_tls_no_enabled_channel_prevents_normal_calls_without_durable_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _disable_all_channels(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database)
        alpha_status, alpha_raw, _ = _post(port, certificate, _call("no-channel-alpha"), "test-token-alpha")
        beta_status, beta_raw, _ = _post(port, certificate, _call("no-channel-beta"), "test-token-beta")
        after = _snapshot(database)
    finally:
        _stop(process)

    alpha, beta = _structured(alpha_raw), _structured(beta_raw)
    assert alpha_status == beta_status == 200
    for response in (alpha, beta):
        assert response["success"] is False
        assert response["status"] == "no_enabled_channel"
        assert response["needs_continuation"] is False
        assert response["interaction_id"] is response["call_id"] is None
    assert after == before == (0, 0, 0, 0, 0, 0, 0)


def test_tls_no_enabled_channel_rejects_invalid_auth_and_params_without_disclosure_or_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _disable_all_channels(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database)
        missing_status, missing, _ = _post(port, certificate, _call("missing-token"), None)
        bad_status, bad, _ = _post(port, certificate, _call("bad-token"), "not-a-token")
        invalid_status, invalid, _ = _post(
            port,
            certificate,
            _rpc("tools/call", {"name": "get_user_input", "arguments": {"request_id": "missing-required"}}, 10),
            "test-token-alpha",
        )
        after = _snapshot(database)
    finally:
        _stop(process)

    assert missing_status == bad_status == 401 and missing == bad
    assert invalid_status == 200 and invalid["error"]["code"] == -32602
    assert "channel" not in json.dumps(missing).lower()
    assert "channel" not in json.dumps(invalid).lower()
    assert after == before == (0, 0, 0, 0, 0, 0, 0)


def test_tls_no_enabled_channel_blocks_task_revision_but_enabled_route_still_rings(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        _, control_raw, _ = _post(
            port, certificate, _task_request("enabled-control"), "test-token-alpha"
        )
        control = _structured(control_raw)
        assert control["success"] is True and control["status"] == "ringing"
        control_call = control["call_id"]
        assert isinstance(control_call, str)
        cookie, csrf = _login(port, certificate)
        writer = _headers(cookie, csrf, port)
        assert _accept(port, certificate, control_call, writer)[0] == 200
        writer = _writer_headers(port, certificate, control_call, writer, "no-channel-complete")
        assert _submit(port, certificate, control_call, "no-channel-complete", "completed", "done", writer)[0] == 200
    finally:
        _stop(process)

    _disable_all_channels(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        before = _snapshot(database)
        revision = _task_request(
            "no-channel-revision-request",
            task_id=control["interaction_id"],
            call_id=control_call,
            revision_id="no-channel-revision",
        )
        status, raw, _ = _post(port, certificate, revision, "test-token-alpha")
        after = _snapshot(database)
    finally:
        _stop(process)

    response = _structured(raw)
    assert status == 200
    assert response["success"] is False
    assert response["status"] == "no_enabled_channel"
    assert response["needs_continuation"] is False
    assert response["interaction_id"] is response["call_id"] is None
    assert after == before
