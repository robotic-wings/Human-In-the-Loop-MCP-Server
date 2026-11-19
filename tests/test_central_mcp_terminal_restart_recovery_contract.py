"""Red contract for terminal restart recovery against the central lifecycle.

A desktop terminal that accepted a round and then died (crash, SIGKILL,
host reboot) restarts with a fresh registration session while its device
keeps owning the round.  These tests pin the central-side guarantees that
make layered recovery work:

* the state poll carries rings invited to the polling device, so a ring
  whose push was acknowledged by the dead process is still presented;
* the poll rebinds the device's in-progress rounds to the polling session
  (owner identity — device and channel — stays immutable per I2; only the
  per-process session handle moves);
* rounds whose owner lease already expired settle first and are never
  resurrected;
* another device's rounds are never stolen by the rebind;
* the call view exposes the saved draft to the owning surface only, and
  stops exposing it once the round settles.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_notification_router import NotificationRouter, TerminalRegistry
from central_mcp_waiting_lifecycle import create_app


class FakeClock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def _no_wait(_: float) -> None:
    return None


def _components(tmp_path: Path) -> tuple[Any, TerminalRegistry, FakeClock]:
    clock = FakeClock()
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-61",
        endpoint="https://10.10.0.61:9100/terminal/events",
        callback_credential="central-callback-secret",
        gui_ready=False,
    )
    registry.register_desktop(
        actor_role="management",
        device_id="desktop-62",
        endpoint="https://10.10.0.62:9100/terminal/events",
        callback_credential="central-callback-secret-62",
        gui_ready=False,
    )
    service = DesktopRegistrationStateService(
        registry=registry,
        clock=clock,
        operator_directory={
            "desktop-61": {"password": "desktop-secret", "devices": ["desktop-61"], "primary": True},
            "desktop-62": {"password": "desktop-62-secret", "devices": ["desktop-62"]},
        },
        device_state=lambda _device_id: [],
    )
    lifecycle_path = tmp_path / "lifecycle.sqlite3"

    async def desktop_dispatch(payload: dict[str, object]) -> dict[str, object]:
        del payload
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute(
                "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "routing.sqlite3",
        clock=clock,
        registry=registry,
        desktop_dispatch=desktop_dispatch,
        bark_dispatch=bark_dispatch,
        route_mode="desktop",
        call_state=call_state,
        web_base_url="https://central.example.test",
    )
    app = create_app(
        database_path=lifecycle_path,
        credentials={"agent-a": "agent-secret"},
        clock=clock,
        notify=router.enqueue,
        wait=_no_wait,
        route_admission=router.validate_route,
        notification_router=router,
        desktop_registration_service=service,
    )
    return app, registry, clock


def _request(
    app: Any,
    method: str,
    path: str,
    *,
    body: dict[str, object] | None = None,
    authorization: str | None = None,
    session: str | None = None,
) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if authorization is not None:
            headers["authorization"] = authorization
        if session is not None:
            headers["x-terminal-session"] = session
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="https://central.test"
        ) as client:
            return await client.request(method, path, json=body, headers=headers)

    return asyncio.run(run())


def _register(app: Any, *, credential: str = "desktop-secret") -> str:
    device_suffix = "61" if credential == "desktop-secret" else "62"
    response = _request(
        app,
        "POST",
        "/api/terminals/register",
        body={"callsign": f"desktop-{device_suffix}", "password": credential,
              "device_id": f"desktop-{device_suffix}", "terminal_platform": "macos",
              "endpoint": f"https://10.10.0.{device_suffix}:9100/terminal/events", "gui_ready": True},
    )
    assert response.status_code == 201
    return str(response.json()["session_id"])


def _ringing_call(app: Any, request_id: str) -> dict[str, object]:
    response = _request(
        app,
        "POST",
        "/mcp",
        authorization="Bearer agent-secret",
        body={"request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "recover"}},
    )
    assert response.status_code == 200
    call = response.json()
    assert call["call_state"] == "ringing"
    return call


def _accept(app: Any, call_id: str, session: str) -> None:
    response = _request(
        app, "POST", f"/api/calls/{call_id}/accept", session=session,
        body={"operation_id": f"accept-{call_id}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "opened"


def _poll_states(app: Any, session: str) -> list[dict[str, object]]:
    response = _request(app, "GET", "/api/terminals/state", session=session)
    assert response.status_code == 200
    assert response.json()["success"] is True
    states = response.json()["states"]
    assert isinstance(states, list)
    return [dict(item) for item in states]


def test_state_poll_carries_rings_invited_to_the_polling_device(tmp_path: Path) -> None:
    """A restarted terminal must learn about a ring whose push acknowledgement
    died with the previous process — the poll, not only the push, presents it."""
    app, _registry, _clock = _components(tmp_path)
    session = _register(app)
    call = _ringing_call(app, "restart-ring-poll")

    states = _poll_states(app, session)
    matching = [item for item in states if item["call_id"] == call["call_id"]]
    assert matching and matching[0]["call_state"] == "ringing" and matching[0]["version"] == 1


def test_state_poll_rebinds_in_progress_rounds_to_the_restarted_session(tmp_path: Path) -> None:
    """Scenario B: the session file is gone (or central restarted), so the
    terminal registers a fresh session; the poll rebinds the device's
    in-progress round to it and a submit under the new session succeeds."""
    app, _registry, _clock = _components(tmp_path)
    first = _register(app)
    call = _ringing_call(app, "restart-rebind")
    _accept(app, call["call_id"], first)

    # The terminal process dies and comes back with a brand-new session.
    second = _register(app)
    assert second != first

    states = _poll_states(app, second)
    matching = [item for item in states if item["call_id"] == call["call_id"]]
    assert matching and matching[0]["call_state"] == "accepted"

    submitted = _request(
        app, "POST", f"/api/calls/{call['call_id']}/submissions", session=second,
        body={"submission_id": "restarted-submit", "result": {"user_input": "typed after restart"}},
    )
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "answered"

    with sqlite3.connect(tmp_path / "lifecycle.sqlite3") as conn:
        row = conn.execute(
            "SELECT owner_device_id, owner_session_id, handled_on FROM lifecycle_calls WHERE call_id=?",
            (call["call_id"],),
        ).fetchone()
    assert row == ("desktop-61", second, "desktop"), "owner identity stays the device; only the handle moves"


def test_expired_owner_lease_settles_instead_of_resurrecting(tmp_path: Path) -> None:
    """A restart that outlives the disconnect deadline must find the round
    settled (terminal_disconnected), never resurrected by the rebind."""
    app, _registry, clock = _components(tmp_path)
    first = _register(app)
    call = _ringing_call(app, "restart-expired")
    _accept(app, call["call_id"], first)

    clock.advance(181)  # past terminal_disconnect_seconds (180)
    second = _register(app)
    states = _poll_states(app, second)
    matching = [item for item in states if item["call_id"] == call["call_id"]]
    assert matching and matching[0]["call_state"] == "disconnected"

    submitted = _request(
        app, "POST", f"/api/calls/{call['call_id']}/submissions", session=second,
        body={"submission_id": "late-submit", "result": {"user_input": "too late"}},
    )
    assert submitted.status_code in {404, 409}
    assert submitted.json()["success"] is False


def test_rebind_never_steals_another_devices_round(tmp_path: Path) -> None:
    """Desktop-62 owns its round; desktop-61's poll must not rebind it and
    desktop-61's session must not be able to submit to it."""
    app, _registry, _clock = _components(tmp_path)
    owner_session = _register(app, credential="desktop-62-secret")
    call = _ringing_call(app, "restart-foreign")
    _accept(app, call["call_id"], owner_session)

    other_session = _register(app, credential="desktop-secret")
    states = _poll_states(app, other_session)
    assert all(item["call_id"] != call["call_id"] for item in states), \
        "a round owned by another device must not appear in this device's feed"

    submitted = _request(
        app, "POST", f"/api/calls/{call['call_id']}/submissions", session=other_session,
        body={"submission_id": "foreign-submit", "result": {"user_input": "not mine"}},
    )
    assert submitted.status_code in {401, 404}
    assert submitted.json()["success"] is False


def test_call_view_exposes_draft_to_owner_and_hides_it_after_settlement(tmp_path: Path) -> None:
    """Draft continuity: partial input belongs to the round; the owning
    surface reads it back after a restart, and nobody reads it after
    settlement."""
    app, _registry, clock = _components(tmp_path)
    first = _register(app)
    call = _ringing_call(app, "restart-draft")
    _accept(app, call["call_id"], first)

    saved = _request(
        app, "PUT", f"/api/calls/{call['call_id']}/draft", session=first,
        body={"draft_version": 1, "content": "half-typed answer"},
    )
    assert saved.status_code == 200
    assert saved.json()["draft_version"] == 1

    # Restart with a fresh session; the poll rebinds, the view returns the draft.
    second = _register(app)
    _poll_states(app, second)
    view = _request(app, "GET", f"/api/calls/{call['call_id']}", session=second)
    assert view.status_code == 200
    assert view.json()["draft_content"] == "half-typed answer"
    assert view.json()["draft_version"] == 1

    clock.advance(181)
    settled = _request(app, "GET", f"/api/calls/{call['call_id']}", session=second)
    assert settled.status_code == 200
    assert "draft_content" not in settled.json(), "settled rounds leak no drafts"
