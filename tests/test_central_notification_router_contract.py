"""Red contract for central notification routing and desktop registration.

This deliberately bounded contract defines a GUI-free, SQLite-backed
``central_notification_router`` boundary.  It is not a desktop application,
phone web UI, Bark integration test, or deployment test.  ``TerminalRegistry``
holds management-authorized desktop endpoint/readiness data; ``NotificationRouter``
persists a call's configured route and one outbox record per enabled channel,
then invokes injected asynchronous desktop and Bark transport seams.

The proposed public boundary is:

* ``TerminalRegistry(database_path, clock)`` with
  ``register_desktop(actor_role, device_id, endpoint, callback_credential,
  gui_ready)`` and ``heartbeat(device_id, gui_ready)``;
* ``NotificationRouter(database_path, clock, registry, desktop_dispatch,
  bark_dispatch, route_mode)`` with ``validate_route()``, ``enqueue(event)``,
  ``dispatch_pending()``, ``delivery_states(call_id)`` and ``recover()``.

``route_mode`` is ``desktop``, ``iphone`` or ``both``.  Delivery state records
transport knowledge, never human acceptance: a successful Bark request and a
transport timeout are both potentially deliverable/uncertain, whereas a
definite transport failure is not.  A route is ``unreachable`` only when every
enabled channel is definitely unavailable; it otherwise remains ringing for
the coordinator's original deadline.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
from pathlib import Path

import pytest

from central_call_coordinator import CallCoordinator


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def _routing_module():
    """Lazy import leaves the integrated coordinator as a passing Red control."""

    return importlib.import_module("central_notification_router")


def _registry(tmp_path: Path, clock: FakeClock):
    return _routing_module().TerminalRegistry(
        database_path=tmp_path / "routing.sqlite3", clock=clock
    )


def _router(
    tmp_path: Path,
    clock: FakeClock,
    registry: object,
    *,
    desktop_dispatch,
    bark_dispatch,
    route_mode: str = "both",
    call_state=None,
    web_base_url: str | None = "https://central.example.test",
):
    return _routing_module().NotificationRouter(
        database_path=tmp_path / "routing.sqlite3",
        clock=clock,
        registry=registry,
        desktop_dispatch=desktop_dispatch,
        bark_dispatch=bark_dispatch,
        route_mode=route_mode,
        desktop_timeout_seconds=2.5,
        bark_timeout_seconds=2.5,
        call_state=call_state,
        web_base_url=web_base_url,
    )


async def _desktop_success(_: dict[str, object]) -> dict[str, object]:
    return {"accepted": True}


async def _bark_success(_: dict[str, object]) -> dict[str, object]:
    # HTTP success alone is insufficient: the Bark business result must agree.
    return {"http_status": 200, "business_success": True}


def _event(call_id: str = "call-1", deadline: float = 1_700_000_060.0) -> dict[str, object]:
    return {"call_id": call_id, "ring_deadline": deadline}


def test_existing_coordinator_control_is_gui_free_and_keeps_notification_seam(tmp_path: Path) -> None:
    """Passing control independent of the absent router module."""

    source = inspect.getsource(CallCoordinator)
    tree = ast.parse(source)
    assert "tkinter" not in source.lower()
    assert any(
        isinstance(node, ast.Attribute) and node.attr == "_notify"
        for node in ast.walk(tree)
    )


def test_management_only_registration_persists_endpoint_and_fresh_gui_readiness(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry = _registry(tmp_path, clock)

    with pytest.raises(PermissionError):
        registry.register_desktop(
            actor_role="agent",
            device_id="opaque-device-7",
            endpoint="https://10.0.0.7:9123/terminal/events",
            callback_credential="callback-secret-reference",
            gui_ready=True,
        )

    registered = registry.register_desktop(
        actor_role="operator",
        device_id="opaque-device-7",
        endpoint="https://10.0.0.7:9123/terminal/events",
        callback_credential="callback-secret-reference",
        gui_ready=True,
    )
    assert registered["device_id"] == "opaque-device-7"
    assert registered["gui_ready"] is True
    assert registered["endpoint"] == "https://10.0.0.7:9123/terminal/events"

    # Restart does not lose a registered endpoint/readiness record.
    reopened = _registry(tmp_path, clock)
    assert reopened.desktop_state("opaque-device-7")["gui_ready"] is True
    clock.advance(31)
    assert reopened.desktop_state("opaque-device-7")["fresh"] is False


def test_route_modes_and_no_enabled_channel_are_decided_before_call_notification(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry = _registry(tmp_path, clock)
    no_channel = _router(
        tmp_path,
        clock,
        registry,
        desktop_dispatch=_desktop_success,
        bark_dispatch=_bark_success,
        route_mode="none",
    )
    assert no_channel.validate_route() == {
        "success": False,
        "status": "no_enabled_channel",
        "needs_continuation": False,
    }

    for mode, expected in (("desktop", {"desktop"}), ("iphone", {"bark"}), ("both", {"desktop", "bark"})):
        router = _router(
            tmp_path / mode,
            clock,
            _registry(tmp_path / mode, clock),
            desktop_dispatch=_desktop_success,
            bark_dispatch=_bark_success,
            route_mode=mode,
        )
        assert router.validate_route()["success"] is True
        router.enqueue(_event(f"call-{mode}"))
        assert {item["channel"] for item in router.delivery_states(f"call-{mode}")} == expected


def test_enqueue_is_compatible_with_coordinator_notify_and_persists_route_before_dispatch(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry = _registry(tmp_path, clock)
    router = _router(
        tmp_path,
        clock,
        registry,
        desktop_dispatch=_desktop_success,
        bark_dispatch=_bark_success,
    )
    coordinator = CallCoordinator(
        database_path=tmp_path / "calls.sqlite3",
        clock=clock,
        notify=router.enqueue,
    )
    created = coordinator.create_call(
        caller_id="agent-a", request_id="request-1", payload={"tool": "text"}
    )
    assert {row["channel"] for row in router.delivery_states(created["call_id"])} == {
        "desktop",
        "bark",
    }
    assert {row["state"] for row in router.delivery_states(created["call_id"])} == {"pending"}


def test_desktop_and_bark_dispatch_start_independently_and_classify_outcomes(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry = _registry(tmp_path, clock)
    registry.register_desktop(
        actor_role="operator",
        device_id="desktop-opaque-id",
        endpoint="https://10.0.0.8:9123/terminal/events",
        callback_credential="desktop-callback-reference",
        gui_ready=True,
    )
    started: set[str] = set()
    release = asyncio.Event()

    async def desktop(_: dict[str, object]) -> dict[str, object]:
        started.add("desktop")
        await release.wait()
        return {"accepted": True}

    async def bark(_: dict[str, object]) -> dict[str, object]:
        started.add("bark")
        await release.wait()
        return {"http_status": 200, "business_success": True}

    router = _router(tmp_path, clock, registry, desktop_dispatch=desktop, bark_dispatch=bark)
    router.enqueue(_event())

    async def run() -> None:
        task = asyncio.create_task(router.dispatch_pending())
        for _ in range(20):
            if started == {"desktop", "bark"}:
                break
            await asyncio.sleep(0)
        assert started == {"desktop", "bark"}
        release.set()
        await task

    asyncio.run(run())
    states = {row["channel"]: row["state"] for row in router.delivery_states("call-1")}
    assert states == {"desktop": "delivered", "bark": "delivered"}


def test_uncertain_delivery_waits_for_ring_deadline_but_all_definite_failures_are_unreachable(
    tmp_path: Path,
) -> None:
    clock = FakeClock()

    async def timeout(_: dict[str, object]) -> dict[str, object]:
        raise TimeoutError("bounded transport timeout")

    async def failure(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 503, "business_success": False}

    uncertain = _router(
        tmp_path / "uncertain",
        clock,
        _registry(tmp_path / "uncertain", clock),
        desktop_dispatch=failure,
        bark_dispatch=timeout,
    )
    uncertain.enqueue(_event("uncertain"))
    result = asyncio.run(uncertain.dispatch_pending())
    assert result["call_decisions"]["uncertain"] == "ringing"
    assert {row["state"] for row in uncertain.delivery_states("uncertain")} == {"failed", "uncertain"}

    failed = _router(
        tmp_path / "failed",
        clock,
        _registry(tmp_path / "failed", clock),
        desktop_dispatch=failure,
        bark_dispatch=failure,
    )
    failed.enqueue(_event("failed"))
    result = asyncio.run(failed.dispatch_pending())
    assert result["call_decisions"]["failed"] == "unreachable"
    assert {row["state"] for row in failed.delivery_states("failed")} == {"failed"}


def test_stale_desktop_readiness_gets_one_bounded_attempt_and_send_rechecks_call_state_and_deadline(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    registry = _registry(tmp_path, clock)
    registry.register_desktop(
        actor_role="operator",
        device_id="desktop-opaque-id",
        endpoint="https://10.0.0.8:9123/terminal/events",
        callback_credential="desktop-callback-reference",
        gui_ready=True,
    )
    clock.advance(31)
    attempted: list[dict[str, object]] = []

    async def desktop(payload: dict[str, object]) -> dict[str, object]:
        attempted.append(payload)
        return {"accepted": True}

    router = _router(
        tmp_path, clock, registry, desktop_dispatch=desktop, bark_dispatch=_bark_success, route_mode="desktop"
    )
    router.enqueue(_event("stale", clock() + 60))
    asyncio.run(router.dispatch_pending())
    assert len(attempted) == 1

    router.enqueue(_event("expired", clock()))
    result = asyncio.run(router.dispatch_pending())
    assert len(attempted) == 1
    assert router.delivery_states("expired")[0]["state"] == "skipped"
    assert result["call_decisions"].get("expired") != "unreachable"


def test_restart_preserves_outbox_and_never_blindly_resends_uncertain_transport(
    tmp_path: Path,
) -> None:
    clock = FakeClock()
    calls: list[str] = []

    async def timeout(payload: dict[str, object]) -> dict[str, object]:
        calls.append(str(payload["call_id"]))
        raise TimeoutError("unknown after outbound request")

    router = _router(
        tmp_path,
        clock,
        _registry(tmp_path, clock),
        desktop_dispatch=_desktop_success,
        bark_dispatch=timeout,
        route_mode="iphone",
    )
    router.enqueue(_event("restart"))
    asyncio.run(router.dispatch_pending())
    assert calls == ["restart"]

    reopened = _router(
        tmp_path,
        clock,
        _registry(tmp_path, clock),
        desktop_dispatch=_desktop_success,
        bark_dispatch=timeout,
        route_mode="iphone",
    )
    assert reopened.recover() == []
    asyncio.run(reopened.dispatch_pending())
    assert calls == ["restart"]
    assert reopened.delivery_states("restart")[0]["state"] == "uncertain"


def test_route_snapshot_is_persisted_at_enqueue_and_later_settings_only_affect_new_calls(
    tmp_path: Path,
) -> None:
    """SPEC 10.2: routing changes are prospective, including after restart."""

    clock = FakeClock()
    sent: list[str] = []

    async def desktop(_: dict[str, object]) -> dict[str, object]:
        sent.append("desktop")
        return {"accepted": True}

    async def bark(_: dict[str, object]) -> dict[str, object]:
        sent.append("bark")
        return {"http_status": 200, "business_success": True}

    registry = _registry(tmp_path, clock)
    first_settings = _router(
        tmp_path,
        clock,
        registry,
        desktop_dispatch=desktop,
        bark_dispatch=bark,
        route_mode="desktop",
    )
    first_settings.enqueue(_event("old-call"))
    assert [row["channel"] for row in first_settings.delivery_states("old-call")] == ["desktop"]

    # A reopened service with changed settings must consume the durable original
    # route rather than recalculate it as iphone/Bark.
    changed_settings = _router(
        tmp_path,
        clock,
        _registry(tmp_path, clock),
        desktop_dispatch=desktop,
        bark_dispatch=bark,
        route_mode="iphone",
    )
    asyncio.run(changed_settings.dispatch_pending())
    assert sent == ["desktop"]
    assert [row["channel"] for row in changed_settings.delivery_states("old-call")] == ["desktop"]

    changed_settings.enqueue(_event("new-call"))
    assert [row["channel"] for row in changed_settings.delivery_states("new-call")] == ["bark"]


@pytest.mark.parametrize("ended_state", ["accepted", "declined", "not_accepted", "answered"])
def test_authoritative_call_state_prevents_late_dispatch_after_state_changes(
    tmp_path: Path, ended_state: str
) -> None:
    """SPEC §§3/5.1: router state is not authoritative; recheck central call state."""

    clock = FakeClock()
    calls: list[str] = []

    async def desktop(_: dict[str, object]) -> dict[str, object]:
        calls.append("desktop")
        return {"accepted": True}

    async def bark(_: dict[str, object]) -> dict[str, object]:
        calls.append("bark")
        return {"http_status": 200, "business_success": True}

    authoritative_state = {"call-after-transition": "ringing"}
    router = _router(
        tmp_path,
        clock,
        _registry(tmp_path, clock),
        desktop_dispatch=desktop,
        bark_dispatch=bark,
        call_state=lambda call_id: authoritative_state.get(call_id),
    )
    router.enqueue(_event("call-after-transition", clock() + 60))
    authoritative_state["call-after-transition"] = ended_state

    result = asyncio.run(router.dispatch_pending())
    assert calls == []
    assert {row["state"] for row in router.delivery_states("call-after-transition")} == {"skipped"}
    # A non-ringing central state means the notification is stale.  It is not
    # evidence that every delivery channel is unreachable.
    assert result["call_decisions"].get("call-after-transition") != "unreachable"
