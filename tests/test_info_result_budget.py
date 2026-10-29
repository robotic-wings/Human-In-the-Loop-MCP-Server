"""Red TLS contracts for information-acknowledgement result budgets."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from central_service import _TOOL_SPECS
from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_iphone_browser_drafts import _writer_headers
from test_iphone_info_submission import _accept, _headers, _login, _page, _submit
from test_iphone_web_acceptance import _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout
from test_terminal_control_api import _wait_for_tls_gateway

MIN_BUDGET = 16_384


def _rows(data_dir: Path) -> tuple[int, int, int, int, int, int]:
    with sqlite3.connect(data_dir / "calls.sqlite3") as connection:
        return tuple(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] for table in ("calls", "events", "results", "create_operations", "bark_deliveries", "submission_operations"))


def _budgeted_call(port: int, certificate: Path, request_id: str, budget: object | None) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "show_info_message"
    arguments: dict[str, object] = {"title": "Budgeted information", "message": "<script>safe summary</script>", "request_id": request_id, "client_timeout_seconds": 2}
    if budget is not None:
        arguments["max_result_bytes"] = budget
    request["params"]["arguments"] = arguments
    status, response, _ = _post(port, certificate, request, "test-token-alpha")
    assert status == 200
    return _structured(response)


def _acknowledge_and_read(port: int, certificate: Path, created: dict[str, object], budget: int, suffix: str) -> tuple[dict[str, object], dict[str, object]]:
    call_id = created["call_id"]; interaction_id = created["interaction_id"]
    assert isinstance(call_id, str) and isinstance(interaction_id, str)
    cookie, csrf = _login(port, certificate); headers = _headers(cookie, csrf, port)
    assert _accept(port, certificate, call_id, headers)[0] == 200
    endpoint, page_csrf = _page(_request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})[2], call_id)
    assert endpoint == f"/api/calls/{call_id}/submissions"
    writer = _writer_headers(port, certificate, call_id, _headers(cookie, page_csrf, port), f"info-budget-{suffix}")
    submitted = _submit(port, certificate, call_id, f"info-budget-submit-{suffix}", True, writer)
    assert submitted[0] == 200 and isinstance(submitted[2], dict)
    opened = _continuation(f"info-budget-opened-{suffix}", interaction_id, call_id, 0)
    _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
    assert _structured(opened_response)["status"] == "opened"
    continuation = _continuation(f"info-budget-final-{suffix}", interaction_id, call_id, 1)
    continuation["params"]["arguments"]["max_result_bytes"] = budget
    _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
    return submitted[2], _structured(response)


def test_info_schema_exposes_optional_result_budget() -> None:
    info = next(spec for spec in _TOOL_SPECS if spec.name == "show_info_message")
    assert "max_result_bytes" in dict(info.properties)


@pytest.mark.parametrize("budget", [False, "16384", MIN_BUDGET - 1])
def test_info_invalid_budget_rejects_before_durable_admission(tmp_path: Path, budget: object) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = _budgeted_call(port, certificate, f"invalid-info-{budget!s}", budget)
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == {"success": False, "status": "invalid_result_budget", "needs_continuation": False}
    assert after == before == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("budget", [None, MIN_BUDGET])
def test_tls_info_default_and_minimum_budget_preserve_acknowledged_result(tmp_path: Path, budget: int | None) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    effective_budget = 1_000_000 if budget is None else budget
    try:
        created = _budgeted_call(port, certificate, f"info-minimum-{budget}", budget)
        submitted, delivered = _acknowledge_and_read(port, certificate, created, effective_budget, str(budget))
    finally:
        _stop(process)
    assert delivered["status"] == delivered["call_state"] == "answered"
    assert delivered["acknowledged"] is True and delivered["result_id"] == submitted["result_id"]
    assert delivered["needs_continuation"] is False and delivered.get("body_omitted_for_size") is not True


def test_tls_info_budgeted_replay_is_snapshot_without_duplicate_submission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _budgeted_call(port, certificate, "info-replay", MIN_BUDGET)
        submitted, delivered = _acknowledge_and_read(port, certificate, created, MIN_BUDGET, "replay")
        call_id = created["call_id"]; interaction_id = created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        replay = _continuation("info-budget-final-replay", interaction_id, call_id, 1)
        replay["params"]["arguments"]["max_result_bytes"] = MIN_BUDGET
        _, replay_response, _ = _post(port, certificate, replay, "test-token-alpha")
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            submissions = connection.execute("SELECT COUNT(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)
    assert delivered == _structured(replay_response)
    assert delivered["acknowledged"] is True and delivered["status"] == "answered"
    assert delivered["result_id"] == submitted["result_id"] and submissions == 1
