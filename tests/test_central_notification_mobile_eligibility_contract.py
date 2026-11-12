"""Contract: Bark push honors the call's mobile eligibility.

A call round records ``mobile_eligible`` at creation from the TARGET
operator's ``mobile`` flag (multi-operator support). The notification router
must not enqueue a Bark push for a round the target operator cannot take on
a phone — otherwise a temporary or desktop-only operator's calls ring the
primary operator's iPhone. Found live: a call addressed to a desktop-only
``probe-op`` operator pushed to the owner's phone under ``route_mode: both``.

The lifecycle create notification carries ``mobile_eligible`` explicitly;
the router skips the ``bark`` channel when it is ``False``. An absent flag
(legacy hand-built events, existing contracts) keeps the historical
enqueue-always behavior.
"""

from __future__ import annotations

import asyncio
import importlib
from pathlib import Path


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now


def _router(tmp_path: Path, clock: Clock, bark_dispatch, route_mode: str = "both"):
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
        route_mode=route_mode,
        bark_timeout_seconds=2.5,
        web_base_url="https://central.example.test",
    )


def _event(call_id: str, **extra: object) -> dict[str, object]:
    return {"call_id": call_id, "ring_deadline": 1_700_000_060.0, **extra}


def _bark_recorder():
    pushed: list[str] = []

    async def bark_dispatch(event: dict[str, object]) -> dict[str, object]:
        pushed.append(str(event.get("call_id")))
        return {"accepted": True}

    return bark_dispatch, pushed


def _run_pending(router) -> None:
    asyncio.run(router.dispatch_pending())


def test_mobile_ineligible_call_never_pushes_bark(tmp_path: Path) -> None:
    bark, pushed = _bark_recorder()
    router = _router(tmp_path, Clock(), bark)
    router.enqueue(_event("to-desktop-only", mobile_eligible=False))
    _run_pending(router)
    assert pushed == [], "a mobile-ineligible call must never push to the (primary operator's) phone"
    states = router.delivery_states("to-desktop-only")
    assert all(row["channel"] != "bark" for row in states), states


def test_mobile_eligible_call_still_pushes_bark(tmp_path: Path) -> None:
    bark, pushed = _bark_recorder()
    router = _router(tmp_path, Clock(), bark)
    router.enqueue(_event("to-mobile-op", mobile_eligible=True))
    _run_pending(router)
    assert pushed == ["to-mobile-op"]


def test_legacy_event_without_flag_keeps_enqueue_behavior(tmp_path: Path) -> None:
    bark, pushed = _bark_recorder()
    router = _router(tmp_path, Clock(), bark)
    router.enqueue(_event("legacy-no-flag"))
    _run_pending(router)
    assert pushed == ["legacy-no-flag"]


def test_create_notification_carries_mobile_eligibility(tmp_path: Path) -> None:
    """The lifecycle create path must stamp the flag onto the notification."""
    from tests.test_central_progress_outcome_feed_contract import FakeClock, make_app  # noqa: PLC0415

    seen: list[dict[str, object]] = []
    module = importlib.import_module("central_mcp_waiting_lifecycle")
    app = module.create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "token-a"},
        clock=FakeClock(),
        notify=seen.append,
        wait=_NoWait(),
        ring_timeout_seconds=600,
        agent_reconnect_seconds=30,
    )
    import httpx

    async def request() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
            return await client.post(
                "/mcp",
                json={"request_id": "mobile-flag", "payload": {"tool": "get_user_input", "prompt": "ping"},
                      "client_timeout_seconds": 5},
                headers={"Authorization": "Bearer token-a"},
            )

    response = asyncio.run(request())
    assert response.status_code == 200
    assert seen, "create must emit a ringing notification"
    event = seen[-1]
    assert isinstance(event.get("mobile_eligible"), bool), \
        "create must stamp the target operator's mobile eligibility onto the notification"


class _NoWait:
    async def __call__(self, seconds: float) -> None:
        return None


def test_mobile_false_operator_never_pushes_bark_end_to_end(tmp_path: Path) -> None:
    """The real composition: operator_directory → operator_mobile → create
    notification → router. A call addressed to a ``mobile:false`` operator
    must not push to the single Bark device; the mobile operator's call must.
    This pins the live incident: a desktop-only temp operator's call rang
    the primary operator's iPhone under ``route_mode: both``."""
    import httpx

    bark, pushed = _bark_recorder()
    router = _router(tmp_path, Clock(), bark, route_mode="both")
    module = importlib.import_module("central_mcp_waiting_lifecycle")
    app = module.create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "token-a"},
        clock=Clock(),
        notify=router.enqueue,
        wait=_NoWait(),
        ring_timeout_seconds=600,
        agent_reconnect_seconds=30,
        notification_router=router,
        operator_directory={
            "MOBILE-OP": {"password": "pw", "mobile": True, "primary": True},
            "DESK-ONLY": {"password": "pw", "mobile": False},
        },
    )

    def create(request_id: str, callsign: str) -> dict[str, object]:
        async def request() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://central.test") as client:
                return await client.post(
                    "/mcp",
                    json={"request_id": request_id, "payload": {"tool": "get_user_input", "prompt": "ping"},
                          "callsign": callsign, "client_timeout_seconds": 5},
                    headers={"Authorization": "Bearer token-a"},
                )

        response = asyncio.run(request())
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "heartbeat", body
        return body

    desk_call = create("desk-1", "DESK-ONLY")
    mobile_call = create("mobile-1", "MOBILE-OP")
    asyncio.run(router.dispatch_pending())

    assert pushed == [str(mobile_call["call_id"])], (
        "only the mobile operator's call may push to the phone; "
        f"got pushes for {[c[:8] for c in pushed]}, desk call was {str(desk_call['call_id'])[:8]}"
    )
    desk_states = router.delivery_states(str(desk_call["call_id"]))
    assert [row["channel"] for row in desk_states] == ["desktop"], desk_states
