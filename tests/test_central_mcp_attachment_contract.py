"""Red contract for attachment upload and attachment-carrying results.

The central service has no attachment capability today.  A single-file upload
must exist on both human endpoints (``POST /api/calls/<id>/attachments`` for
the owning desktop terminal and ``POST /calls/<id>/attachments`` for the
owning iPhone browser session), store the bytes centrally, and return a
server-generated ``attachment_id`` together with the original name, media
type, byte count and a content checksum.  A later submission that references
an uploaded attachment must surface that attachment's metadata in the result
the Agent reads back.  These tests stay red until both routes and the
submission resolution exist in ``central_mcp_waiting_lifecycle``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
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


CONTENT = b"\x00\x01\x02\x03hello-attachment"


def upload_body(filename: str = "photo.png", media_type: str = "image/png") -> dict[str, object]:
    return {
        "filename": filename,
        "media_type": media_type,
        "content": base64.b64encode(CONTENT).decode("ascii"),
    }


def assert_attachment_metadata(meta: dict[str, object], filename: str = "photo.png") -> None:
    assert isinstance(meta["attachment_id"], str) and meta["attachment_id"]
    assert meta["filename"] == filename
    assert meta["media_type"] == "image/png"
    assert meta["byte_count"] == len(CONTENT)
    assert meta["checksum"] == hashlib.sha256(CONTENT).hexdigest()


def test_desktop_attachment_upload_returns_metadata_and_checksum(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    response = request(app, "POST", f"/api/calls/{call['call_id']}/attachments",
                       terminal=owner, body=upload_body())
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert_attachment_metadata(response.json())


def test_mobile_attachment_upload_returns_metadata_and_checksum(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, route_mode="iphone")
    call = create_task(app)
    accepted = request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator",
                       body={"operation_id": "web-accept"})
    assert accepted.status_code == 200

    response = request(app, "POST", f"/calls/{call['call_id']}/attachments",
                       browser="operator", body=upload_body())
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert_attachment_metadata(response.json())


def test_uploaded_attachment_metadata_is_returned_with_final_result_to_agent(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    uploaded = request(app, "POST", f"/api/calls/{call['call_id']}/attachments",
                       terminal=owner, body=upload_body())
    assert uploaded.status_code == 200
    attachment_id = uploaded.json()["attachment_id"]

    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "final-with-attachment",
        "result": {"status": "completed", "report": "deployed, see screenshot",
                   "attachments": [attachment_id]},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "completed"
    attachments = submitted.json()["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 1
    assert attachments[0]["attachment_id"] == attachment_id
    assert_attachment_metadata(attachments[0])

    delivered = continuation(app, call, after=1)
    assert delivered["status"] == "completed"
    delivered_attachments = delivered["attachments"]
    assert isinstance(delivered_attachments, list) and len(delivered_attachments) == 1
    assert delivered_attachments[0]["attachment_id"] == attachment_id
    assert_attachment_metadata(delivered_attachments[0])


def test_mobile_uploaded_attachment_metadata_is_returned_with_final_result_to_agent(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, route_mode="iphone")
    call = create_task(app)
    accepted = request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator",
                       body={"operation_id": "web-accept"})
    assert accepted.status_code == 200

    uploaded = request(app, "POST", f"/calls/{call['call_id']}/attachments",
                       browser="operator", body=upload_body())
    assert uploaded.status_code == 200
    attachment_id = uploaded.json()["attachment_id"]

    submitted = request(app, "POST", f"/calls/{call['call_id']}/submissions", browser="operator", body={
        "submission_id": "web-final-with-attachment",
        "result": {"status": "completed", "report": "finished from phone", "attachments": [attachment_id]},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "completed"

    delivered = continuation(app, call, after=1)
    delivered_attachments = delivered["attachments"]
    assert isinstance(delivered_attachments, list) and len(delivered_attachments) == 1
    assert delivered_attachments[0]["attachment_id"] == attachment_id
    assert_attachment_metadata(delivered_attachments[0])


def test_upload_rejects_local_path_without_content(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_task(app)
    accept_desktop(app, call, owner)

    response = request(app, "POST", f"/api/calls/{call['call_id']}/attachments", terminal=owner, body={
        "filename": "secret.png", "path": "C:\\Users\\operator\\secret.png",
    })
    assert response.status_code == 400
    assert response.json()["success"] is False
    assert response.json()["status"] == "local_path_not_accepted"
    assert "attachment_id" not in response.json()
