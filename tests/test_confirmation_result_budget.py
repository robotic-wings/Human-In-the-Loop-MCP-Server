"""Red TLS contracts for confirmation-dialog result budgets.

Confirmation results have no large body, but they still use the common public
``max_result_bytes`` admission contract.  In particular, a negative answer is
an answered result, not a declined call.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from central_service import _TOOL_SPECS
from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured
from test_iphone_browser_drafts import _writer_headers
from test_iphone_confirmation_submission import _accept, _confirmation_call, _parsed_confirmation_page, _submit
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation
from test_mcp_wait_leg import _with_timeout
from test_terminal_control_api import _wait_for_tls_gateway


MIN_BUDGET = 16_384


def _rows(data_dir: Path) -> tuple[int, int, int, int, int, int]:
    with sqlite3.connect(data_dir / "calls.sqlite3") as connection:
        return tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("calls", "events", "results", "create_operations", "bark_deliveries", "submission_operations")
        )


def _budgeted_call(port: int, certificate: Path, request_id: str, budget: object) -> dict[str, object]:
    request = _with_timeout(request_id, 2)
    request["params"]["name"] = "show_confirmation_dialog"
    request["params"]["arguments"] = {
        "title": "Confirm budget", "message": "Budgeted confirmation", "request_id": request_id,
        "client_timeout_seconds": 2, "max_result_bytes": budget,
    }
    status, response, _ = _post(port, certificate, request, "test-token-alpha")
    assert status == 200
    return _structured(response)


def _submit_and_read(
    port: int, certificate: Path, created: dict[str, object], confirmed: bool, budget: int, suffix: str
) -> tuple[dict[str, object], dict[str, object]]:
    call_id = created["call_id"]
    interaction_id = created["interaction_id"]
    assert isinstance(call_id, str) and isinstance(interaction_id, str)
    cookie, csrf = _login(port, certificate)
    headers = _headers(cookie, csrf, port)
    assert _accept(port, certificate, call_id, headers)[0] == 200
    page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    endpoint, page_csrf, _ = _parsed_confirmation_page(page[2], call_id)
    writer = _writer_headers(port, certificate, call_id, _headers(cookie, page_csrf, port), f"confirmation-budget-{suffix}")
    submitted = _submit(port, certificate, call_id, f"confirmation-budget-submit-{suffix}", confirmed, writer)
    assert submitted[0] == 200 and isinstance(submitted[2], dict)
    opened = _continuation(f"confirmation-budget-opened-{suffix}", interaction_id, call_id, 0)
    _, opened_response, _ = _post(port, certificate, opened, "test-token-alpha")
    assert _structured(opened_response)["status"] == "opened"
    continuation = _continuation(f"confirmation-budget-final-{suffix}", interaction_id, call_id, 1)
    continuation["params"]["arguments"]["max_result_bytes"] = budget
    _, response, _ = _post(port, certificate, continuation, "test-token-alpha")
    return submitted[2], _structured(response)


def test_confirmation_schema_exposes_optional_result_budget() -> None:
    confirmation = next(spec for spec in _TOOL_SPECS if spec.name == "show_confirmation_dialog")
    assert "max_result_bytes" in dict(confirmation.properties)


@pytest.mark.parametrize("budget", [False, "16384", MIN_BUDGET - 1])
def test_confirmation_invalid_budget_rejects_before_durable_admission(tmp_path: Path, budget: object) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        before = _rows(tmp_path / "data")
        rejected = _budgeted_call(port, certificate, f"invalid-confirmation-{budget!s}", budget)
        after = _rows(tmp_path / "data")
    finally:
        _stop(process)
    assert rejected == {"success": False, "status": "invalid_result_budget", "needs_continuation": False}
    assert after == before == (0, 0, 0, 0, 0, 0)


@pytest.mark.parametrize("confirmed", [True, False])
def test_tls_minimum_confirmation_budget_preserves_yes_and_no_answer_semantics(tmp_path: Path, confirmed: bool) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _budgeted_call(port, certificate, f"minimum-confirmation-{confirmed}", MIN_BUDGET)
        submitted, delivered = _submit_and_read(port, certificate, created, confirmed, MIN_BUDGET, str(confirmed))
    finally:
        _stop(process)
    assert delivered["status"] == delivered["call_state"] == "answered"
    assert delivered["confirmed"] is confirmed
    assert delivered["result_id"] == submitted["result_id"]
    assert delivered["needs_continuation"] is False
    assert delivered.get("body_omitted_for_size") is not True


def test_tls_confirmation_sufficient_budget_replay_is_snapshot_without_duplicate_submission(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _budgeted_call(port, certificate, "confirmation-replay", 1_000_000)
        submitted, delivered = _submit_and_read(port, certificate, created, False, 1_000_000, "replay")
        call_id = created["call_id"]; interaction_id = created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        replay = _continuation("confirmation-budget-final-replay", interaction_id, call_id, 1)
        replay["params"]["arguments"]["max_result_bytes"] = 1_000_000
        _, replay_response, _ = _post(port, certificate, replay, "test-token-alpha")
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            submissions = connection.execute("SELECT COUNT(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    finally:
        _stop(process)
    assert delivered == _structured(replay_response)
    assert delivered["confirmed"] is False and delivered["status"] == "answered"
    assert delivered["result_id"] == submitted["result_id"]
    assert submissions == 1
