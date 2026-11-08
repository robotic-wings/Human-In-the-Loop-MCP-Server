"""Red contract for durable, sanitized Bark delivery diagnostics.

This is deliberately a notification-router contract.  It requires each Bark
attempt to persist only a small machine-readable outcome category which can
separate an HTTP failure, a bounded timeout, and a Bark business-result
failure.  The persisted diagnostic is not a copy of a Bark URL, device token,
request body, response body, or human message.

The contract does not implement a transport, retry policy, phone UI, or
central lifecycle state transitions.  It only makes a completed delivery
attempt inspectable after a process reconstruction, as required for the
physical retry diagnosis in SPEC 5.3 and 9.2.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path

import pytest


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _router(tmp_path: Path, clock: Clock, bark_dispatch):
    module = importlib.import_module("central_notification_router")
    registry = module.TerminalRegistry(database_path=tmp_path / "router.sqlite3", clock=clock)

    async def desktop_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"accepted": True}

    return module.NotificationRouter(
        database_path=tmp_path / "router.sqlite3",
        clock=clock,
        registry=registry,
        desktop_dispatch=desktop_dispatch,
        bark_dispatch=bark_dispatch,
        route_mode="iphone",
        bark_timeout_seconds=2.5,
        web_base_url="https://central.example.test",
    )


def _event(call_id: str) -> dict[str, object]:
    return {"call_id": call_id, "ring_deadline": 1_700_000_060.0}


@pytest.mark.parametrize(
    ("call_id", "dispatch_kind", "expected_state", "expected_outcome"),
    (
        ("http-failure", "http-failure", "failed", "http_failure"),
        ("business-failure", "business-failure", "failed", "business_failure"),
        ("timeout", "timeout", "uncertain", "timeout"),
        ("delivered", "delivered", "delivered", "delivered"),
    ),
)
def test_bark_delivery_persists_sanitized_machine_outcome_and_survives_reconstruction(
    tmp_path: Path,
    call_id: str,
    dispatch_kind: str,
    expected_state: str,
    expected_outcome: str,
) -> None:
    """A completed Bark attempt has a durable diagnostic without secret content.

    HTTP 200 without Bark's affirmative business result is deliberately not
    delivery.  Conversely a positive business body cannot make a non-200 HTTP
    response successful.  The diagnostic's category is enough for a safe
    operator retry decision, while arbitrary transport body/URL/token data is
    never persisted as the outcome.
    """

    secret_marker = "bark-token-and-response-body-must-not-persist"

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        if dispatch_kind == "timeout":
            raise TimeoutError(secret_marker)
        if dispatch_kind == "http-failure":
            return {
                "http_status": 503,
                "business_success": True,
                "response_body": secret_marker,
            }
        if dispatch_kind == "business-failure":
            return {
                "http_status": 200,
                "business_success": False,
                "response_body": secret_marker,
            }
        return {
            "http_status": 200,
            "business_success": True,
            "response_body": secret_marker,
        }

    clock = Clock()
    first = _router(tmp_path, clock, bark_dispatch)
    first.enqueue(_event(call_id))
    asyncio.run(first.dispatch_pending())

    # Reconstruct over the same SQLite outbox: no subsequent in-memory result
    # is allowed to be necessary to explain the already completed attempt.
    reopened = _router(tmp_path, clock, bark_dispatch)
    records = reopened.outbox_records(call_id)
    assert len(records) == 1
    record = records[0]
    assert record["channel"] == "bark"
    assert record["state"] == expected_state
    assert record["delivery_outcome"] == expected_outcome

    diagnostic = json.dumps(record["delivery_outcome"], sort_keys=True)
    assert secret_marker not in diagnostic
    assert "url" not in diagnostic.lower()
    assert "token" not in diagnostic.lower()
    assert "message" not in diagnostic.lower()
