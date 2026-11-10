"""Red contract for task-family status paths through the submission boundary.

The task tool family currently collapses every human submission into
``answered``.  A task report must instead surface its own top-level status
(``completed`` / ``failed``), a progress report must hold the round in
``in_progress`` with ``needs_continuation``, and an accepted round must be
cancellable by its human owner.  These tests stay red until the submission and
cancel routes in ``central_mcp_waiting_lifecycle`` honour those states.
"""

from __future__ import annotations

import asyncio
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


def components(tmp_path: Path, *, route_mode: str = "both") -> tuple[Any, Clock]:
    clock = Clock()
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
        }, device_state=lambda _device: [],
    )

    async def desktop_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"accepted": True}

    async def bark_dispatch(_: dict[str, object]) -> dict[str, object]:
        return {"http_status": 200, "business_success": True}

    def call_state(call_id: str) -> str | None:
        import sqlite3
        if not lifecycle_path.exists():
            return None
        with sqlite3.connect(lifecycle_path) as conn:
            row = conn.execute(
                "SELECT call_state FROM lifecycle_calls WHERE call_id=?", (call_id,)
            ).fetchone()
        return None if row is None else str(row[0])

    router = NotificationRouter(
        database_path=tmp_path / "router.sqlite3", clock=clock, registry=registry,
        desktop_dispatch=desktop_dispatch, bark_dispatch=bark_dispatch,
        route_mode=route_mode, call_state=call_state, web_base_url="https://central.test",
    )
    app = create_app(
        database_path=lifecycle_path, credentials={"agent-a": "agent-secret"},
        clock=clock, notify=router.enqueue, wait=no_wait, route_admission=router.validate_route,
        notification_router=router, desktop_registration_service=registration,
        operator_session_verifier=BrowserVerifier(),
    )
    return app, clock


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


def create_task(app: Any, request_id: str = "task-create") -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2,
        "payload": {"tool": "task", "task_description": "Deploy the service"},
    })
    assert response.status_code == 200
    return response.json()


def terminal_session(app: Any) -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    })
    if response.status_code != 201:
        async def register() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
                return await client.post("/api/terminals/register", headers={"authorization": "Terminal desktop-secret"}, json={
                    "endpoint": "https://desktop.test/events", "gui_ready": True,
                })
        response = asyncio.run(register())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session,
                       body={"operation_id": "desktop-accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def continuation(app: Any, call: dict[str, object], after: int) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": f"continue-{after}", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": after,
    })
    assert response.status_code == 200
    return response.json()


def test_task_report_completed_surfaces_top_level_status_and_report(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "task-final-1", "result": {"status": "completed", "report": "deployed the service"},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "completed"
    assert submitted.json()["call_state"] == "completed"
    assert submitted.json()["report"] == "deployed the service"
    assert submitted.json()["human_action"] is True
    assert submitted.json()["needs_continuation"] is False
    assert submitted.json()["resubmittable"] is True

    delivered = continuation(app, call, after=1)
    assert delivered["status"] == "completed"
    assert delivered["call_state"] == "completed"
    assert delivered["report"] == "deployed the service"


def test_task_report_failed_surfaces_top_level_status_and_report(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "task-final-2", "result": {"status": "failed", "report": "the deploy broke"},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "failed"
    assert submitted.json()["call_state"] == "failed"
    assert submitted.json()["report"] == "the deploy broke"
    assert submitted.json()["human_action"] is True
    assert submitted.json()["needs_continuation"] is False
    assert submitted.json()["resubmittable"] is True

    delivered = continuation(app, call, after=1)
    assert delivered["status"] == "failed"
    assert delivered["report"] == "the deploy broke"


def test_task_progress_holds_round_in_progress_with_needs_continuation(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    progress = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "task-progress-1", "result": {"status": "in_progress", "progress": "halfway done"},
    })
    assert progress.status_code == 200
    assert progress.json()["status"] == "in_progress"
    assert progress.json()["call_state"] == "in_progress"
    assert progress.json()["progress"] == "halfway done"
    assert progress.json()["human_action"] is True
    assert progress.json()["needs_continuation"] is True

    delivered = continuation(app, call, after=1)
    assert delivered["status"] == "in_progress"
    assert delivered["needs_continuation"] is True

    # The round is not released: a competing new call is still busy.
    assert create_task(app, "while-in-progress")["status"] == "busy"


def test_human_owner_cancels_accepted_task_releasing_busy(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    cancelled = request(app, "POST", f"/api/calls/{call['call_id']}/cancel", terminal=owner,
                        body={"operation_id": "desktop-cancel"})
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["call_state"] == "cancelled"
    assert cancelled.json()["reason"] == "human_cancelled"
    assert cancelled.json()["human_action"] is True
    assert cancelled.json()["needs_continuation"] is False

    delivered = continuation(app, call, after=1)
    assert delivered["status"] == "cancelled"
    assert delivered["reason"] == "human_cancelled"

    assert create_task(app, "after-cancel")["call_state"] == "ringing"


def test_task_submission_idempotency_replays_original_result(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    answer = {"submission_id": "task-final-3", "result": {"status": "completed", "report": "all good"}}
    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body=answer)
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "completed"

    replay = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body=answer)
    assert replay.status_code == 200
    assert replay.json() == submitted.json()
    assert replay.json()["status"] == "completed"

    conflict = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "task-final-3", "result": {"status": "completed", "report": "changed"},
    })
    assert conflict.status_code == 400 and conflict.json()["status"] == "request_conflict"


def test_browser_task_report_completed_surfaces_top_level_status_and_report(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, route_mode="iphone")
    call = create_task(app)
    accepted = request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator",
                       body={"operation_id": "web-accept"})
    assert accepted.status_code == 200

    submitted = request(app, "POST", f"/calls/{call['call_id']}/submissions", browser="operator", body={
        "submission_id": "web-task-final", "result": {"status": "completed", "report": "finished from phone"},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "completed"
    assert submitted.json()["call_state"] == "completed"
    assert submitted.json()["report"] == "finished from phone"
    assert submitted.json()["handled_on"] == "iphone_web"

    delivered = continuation(app, call, after=1)
    assert delivered["status"] == "completed"
    assert delivered["report"] == "finished from phone"
