"""Red contract for lifecycle-owned final owner submissions.

The only authority exercised here is ``central_mcp_waiting_lifecycle``.  In
particular, an accepted terminal/browser session must turn one final human
answer into an immutable event which the originating Agent can continue to
read; the old parallel coordinator is intentionally absent from this test.
"""

from __future__ import annotations

import asyncio
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_mcp_waiting_lifecycle import create_app
from central_notification_router import NotificationRouter, TerminalRegistry


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class BrowserVerifier:
    def __init__(self) -> None:
        self.sessions = {
            "operator": {"operator_id": "only-operator", "browser_session_id": "browser-owner"},
            "foreign": {"operator_id": "other", "browser_session_id": "browser-foreign"},
        }

    def verify(self, token: str | None) -> dict[str, str] | None:
        return self.sessions.get(token or "")


async def no_wait(_: float) -> None:
    return None


def components(tmp_path: Path, *, clock: Clock | None = None, route_mode: str = "both") -> tuple[Any, Clock]:
    clock = clock or Clock()
    lifecycle_path = tmp_path / "lifecycle.sqlite3"
    registry = TerminalRegistry(database_path=tmp_path / "registry.sqlite3", clock=clock)
    registry.register_desktop(
        actor_role="management", device_id="desktop-owner",
        endpoint="https://desktop.test/events", callback_credential="callback-secret", gui_ready=False,
    )
    registration = DesktopRegistrationStateService(
        registry=registry, clock=clock,
        registered_credentials={
            "desktop-secret": {"device_id": "desktop-owner", "terminal_platform": "windows"},
            "other-desktop-secret": {"device_id": "desktop-other", "terminal_platform": "linux"},
        }, device_state=lambda _device: [],
    )

    async def desktop_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    def call_state(call_id: str) -> str | None:
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute("SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
        route_mode=route_mode, call_state=call_state, web_base_url="https://central.test",
    )
    return create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret", "agent-b": "other-agent"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_session_verifier=BrowserVerifier(),
    ), clock


def request(app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
            agent: str | None = None, terminal: str | None = None, browser: str | None = None) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if agent: headers["authorization"] = f"Bearer {agent}"
        if terminal: headers["x-terminal-session"] = terminal
        if browser: headers["x-operator-session"] = browser
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.request(method, path, headers=headers, json=body)
    return asyncio.run(run())


def create(app: Any, request_id: str = "create") -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2, "payload": {"prompt": "Answer precisely"},
    })
    assert response.status_code == 200
    return response.json()


def terminal_session(app: Any, credential: str = "desktop-secret") -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    })
    if response.status_code != 201:
        async def register() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
                return await client.post("/api/terminals/register", headers={"authorization": f"Terminal {credential}"}, json={
                    "endpoint": "https://desktop.test/events", "gui_ready": True,
                })
        response = asyncio.run(register())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session, body={"operation_id": "desktop-accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def continuation(app: Any, call: dict[str, object], after: int) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": f"continue-{after}", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": after,
    })
    assert response.status_code == 200
    return response.json()


def hidden(response: httpx.Response) -> None:
    assert response.status_code in {401, 404}
    assert response.json()["success"] is False
    assert response.json()["status"] == "session_not_found"


def test_exact_desktop_owner_submission_is_durable_replayable_and_agent_readable(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create(app)
    accept_desktop(app, call, owner)
    answer = {"submission_id": "final-1", "result": {"user_input": "approved", "confirmed": True}}

    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body=answer)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == submitted.json()["call_state"] == "answered"
    assert submitted.json()["human_action"] is True
    assert submitted.json()["needs_continuation"] is False
    assert submitted.json()["result"] == answer["result"]
    assert submitted.json()["event_seq"] == 2

    replay = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body=answer)
    assert replay.status_code == 200 and replay.json() == submitted.json()
    conflict = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "final-1", "result": {"user_input": "changed"},
    })
    assert conflict.status_code == 400 and conflict.json()["status"] == "request_conflict"

    delivered = continuation(app, call, after=1)
    assert delivered["event_seq"] == submitted.json()["event_seq"]
    assert delivered["result"] == answer["result"]
    replacement = create(app, "after-answer")
    assert replacement["call_state"] == "ringing"

    # Rebuild the lifecycle application over the durable lifecycle/router
    # databases, while deliberately retaining the already-authenticated
    # registration service.  Its terminal-session map is process-local and
    # registration-session recovery is outside this submission contract.
    router = app._notification_router
    registration = app._desktop_registration_service
    assert router is not None and registration is not None
    restarted = create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "agent-secret", "agent-b": "other-agent"},
        clock=_clock,
        notify=router.enqueue,
        wait=no_wait,
        route_admission=router.validate_route,
        notification_router=router,
        desktop_registration_service=registration,
        operator_session_verifier=BrowserVerifier(),
    )
    replay_after_restart = request(restarted, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body=answer)
    assert replay_after_restart.status_code == 200 and replay_after_restart.json() == submitted.json()
    assert continuation(restarted, call, after=1)["result"] == answer["result"]


def test_browser_owner_submission_and_unauthorized_or_preaccept_attempts_are_inert(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, route_mode="iphone")
    call = create(app)
    blocked_body = {"submission_id": "blocked", "result": {"user_input": "no"}}
    for browser in (None, "foreign"):
        hidden(request(app, "POST", f"/calls/{call['call_id']}/submissions", browser=browser, body=blocked_body))
    hidden(request(app, "POST", f"/calls/{call['call_id']}/submissions", agent="agent-secret", body=blocked_body))
    assert continuation(app, call, after=0)["call_state"] == "ringing"

    accepted = request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator", body={"operation_id": "web-accept"})
    assert accepted.status_code == 200
    submitted = request(app, "POST", f"/calls/{call['call_id']}/submissions", browser="operator", body={
        "submission_id": "web-final", "result": {"user_input": "from iphone"},
    })
    assert submitted.status_code == 200
    assert submitted.json()["handled_on"] == "iphone_web"
    assert continuation(app, call, after=1)["result"] == {"user_input": "from iphone"}

    other = terminal_session(app, "other-desktop-secret")
    hidden(request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=other, body={
        "submission_id": "other", "result": {"user_input": "steal"},
    }))


def test_deadline_and_terminal_submission_are_authoritative_before_late_owner_submission(tmp_path: Path) -> None:
    app, clock = components(tmp_path, route_mode="desktop")
    owner = terminal_session(app)
    call = create(app)
    accept_desktop(app, call, owner)
    clock.advance(181)
    late = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "late", "result": {"user_input": "too late"},
    })
    # Request paths settle the persisted earliest deadline before accepting a
    # late human action.  The saved terminal outcome is immutable.
    assert late.status_code == 200
    assert late.json()["status"] == "disconnected"
    assert late.json()["reason"] == "terminal_disconnected"
    assert late.json()["human_action"] is False
    assert continuation(app, call, after=1)["status"] == "disconnected"
