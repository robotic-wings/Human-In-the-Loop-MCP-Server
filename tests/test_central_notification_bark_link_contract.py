"""Red contract for a durable, call-specific Bark notification payload.

This is deliberately an injected-transport contract.  It does not contact
Bark, render a phone page, or assert that a gateway request reached a person.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path

from central_call_coordinator import CallCoordinator


class FakeClock:
    def __init__(self, now: float = 1_700_100_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _module():
    return importlib.import_module("central_notification_router")


def _registry(tmp_path: Path, clock: FakeClock):
    return _module().TerminalRegistry(database_path=tmp_path / "outbox.sqlite3", clock=clock)


def _router(tmp_path: Path, clock: FakeClock, *, desktop, bark, route_mode="both", call_state=None,
            configured_link: bool = True):
    """Proposed router boundary; web_base_url is central configuration, not request data."""
    arguments = dict(
        database_path=tmp_path / "outbox.sqlite3",
        clock=clock,
        registry=_registry(tmp_path, clock),
        desktop_dispatch=desktop,
        bark_dispatch=bark,
        route_mode=route_mode,
        call_state=call_state,
    )
    if configured_link:
        arguments["web_base_url"] = "https://10.0.0.1:8443/central/"
    return _module().NotificationRouter(**arguments)


async def _desktop_ok(_: dict[str, object]) -> dict[str, object]:
    return {"accepted": True}


def test_existing_router_control_remains_gui_free() -> None:
    # This control must stay green while the added payload boundary is Red.
    assert "tkinter" not in Path("central_notification_router.py").read_text(encoding="utf-8").lower()


def test_bark_outbox_persists_normalized_call_link_before_one_bounded_dispatch(tmp_path: Path) -> None:
    clock = FakeClock()
    bark_payloads: list[dict[str, object]] = []

    async def bark(payload: dict[str, object]) -> dict[str, object]:
        bark_payloads.append(payload)
        return {"http_status": 200, "business_success": True}

    router = _router(tmp_path, clock, desktop=_desktop_ok, bark=bark, route_mode="iphone")
    coordinator = CallCoordinator(
        database_path=tmp_path / "calls.sqlite3", clock=clock, notify=router.enqueue
    )
    call = coordinator.create_call(caller_id="agent-a", request_id="request-1", payload={"tool": "text"})
    call_id = str(call["call_id"])

    # The durable record must exist before a dispatcher is permitted to observe it.
    records = router.outbox_records(call_id)
    assert records == [{
        "channel": "bark", "state": "pending", "ring_deadline": call["ring_deadline"],
        "payload": {"call_id": call_id, "url": f"https://10.0.0.1:8443/central/calls/{call_id}"},
    }]
    asyncio.run(router.dispatch_pending())
    assert bark_payloads == [{"call_id": call_id, "url": f"https://10.0.0.1:8443/central/calls/{call_id}"}]


def test_bark_gateway_success_is_delivery_only_and_cannot_claim_human_acceptance(tmp_path: Path) -> None:
    clock = FakeClock()

    async def bark(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    states = {"call-opaque": "ringing"}
    router = _router(
        tmp_path,
        clock,
        desktop=_desktop_ok,
        bark=bark,
        route_mode="iphone",
        call_state=states.get,
    )
    router.enqueue({"call_id": "call-opaque", "ring_deadline": clock() + 60})
    result = asyncio.run(router.dispatch_pending())

    record = router.outbox_records("call-opaque")[0]
    assert record["state"] == "delivered"
    assert result["call_decisions"] == {"call-opaque": "ringing"}
    assert record["human_received"] is False
    assert record["human_accepted"] is False
    assert states["call-opaque"] == "ringing"
    assert set(record["payload"]) == {"call_id", "url"}
    assert "secret" not in repr(record["payload"]).lower()
    assert "request" not in repr(record["payload"]).lower()


def test_channels_are_independent_and_definite_failure_is_not_human_rejection(tmp_path: Path) -> None:
    clock = FakeClock()
    sent: list[str] = []

    async def desktop(_: dict[str, object]) -> dict[str, object]:
        sent.append("desktop")
        return {"accepted": True}

    async def bark(_: dict[str, object]) -> dict[str, object]:
        sent.append("bark")
        return {"http_status": 503, "business_success": False}

    router = _router(tmp_path, clock, desktop=desktop, bark=bark)
    router.enqueue({"call_id": "independent", "ring_deadline": clock() + 60})
    result = asyncio.run(router.dispatch_pending())
    assert set(sent) == {"desktop", "bark"}
    assert {row["channel"]: row["state"] for row in router.outbox_records("independent")} == {
        "bark": "failed", "desktop": "delivered"
    }
    assert result["call_decisions"]["independent"] == "ringing"


def test_uncertain_or_delivered_bark_is_not_blindly_resent_after_reopen(tmp_path: Path) -> None:
    clock = FakeClock()
    attempts: list[str] = []

    async def timeout(payload: dict[str, object]) -> dict[str, object]:
        attempts.append(str(payload["call_id"]))
        raise TimeoutError("outbound result unknown")

    first = _router(tmp_path, clock, desktop=_desktop_ok, bark=timeout, route_mode="iphone")
    first.enqueue({"call_id": "unknown", "ring_deadline": clock() + 60})
    asyncio.run(first.dispatch_pending())
    assert first.outbox_records("unknown")[0]["state"] == "uncertain"

    reopened = _router(tmp_path, clock, desktop=_desktop_ok, bark=timeout, route_mode="iphone")
    asyncio.run(reopened.dispatch_pending())
    assert attempts == ["unknown"]
    assert reopened.outbox_records("unknown")[0]["state"] == "uncertain"


def test_expired_or_terminal_call_skips_bark_without_deriving_link_from_host_or_caller(tmp_path: Path) -> None:
    clock = FakeClock()
    sent: list[dict[str, object]] = []

    async def bark(payload: dict[str, object]) -> dict[str, object]:
        sent.append(payload)
        return {"http_status": 200, "business_success": True}

    states = {"terminal": "answered", "expired": "ringing"}
    router = _router(tmp_path, clock, desktop=_desktop_ok, bark=bark, route_mode="iphone", call_state=states.get)
    router.enqueue({"call_id": "terminal", "ring_deadline": clock() + 60, "caller_url": "https://evil.invalid"})
    router.enqueue({"call_id": "expired", "ring_deadline": clock(), "host": "evil.invalid"})
    asyncio.run(router.dispatch_pending())
    assert sent == []
    assert [row["state"] for row in router.outbox_records("terminal")] == ["skipped"]
    assert [row["state"] for row in router.outbox_records("expired")] == ["skipped"]


def test_invalid_route_reports_no_enabled_channel_before_any_outbox_write(tmp_path: Path) -> None:
    clock = FakeClock()
    router = _router(tmp_path, clock, desktop=_desktop_ok, bark=_desktop_ok, route_mode="disabled",
                     configured_link=False)
    assert router.validate_route() == {
        "success": False, "status": "no_enabled_channel", "needs_continuation": False
    }
    router.enqueue({"call_id": "never", "ring_deadline": clock() + 60})
    assert router.delivery_states("never") == []
