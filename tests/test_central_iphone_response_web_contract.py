"""Red contract for the bounded central iPhone response-web adapter.

``central_iphone_response_web`` is an ASGI boundary around the durable
``TerminalCallService``.  In this slice an injected operator-browser session
verifier supplies the already-authenticated browser capability and an injected
renderer receives a safe view model.  A call link is an identifier only: it is
never authorization and a GET is inert.  This is deliberately not an HTML,
cookie, CSRF, TLS, Bark, or real-iPhone acceptance contract.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
import sqlite3
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from central_call_coordinator import CallCoordinator
from central_terminal_call_service import TerminalCallService


class FakeClock:
    def __init__(self, now: float = 1_700_000_000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@dataclass
class BrowserSessions:
    sessions: dict[str, dict[str, str]] = field(default_factory=dict)
    verifications: list[str | None] = field(default_factory=list)

    def verify(self, token: str | None) -> dict[str, str] | None:
        self.verifications.append(token)
        return self.sessions.get(token or "")


@dataclass
class Renderer:
    views: list[dict[str, object]] = field(default_factory=list)

    def render(self, view: dict[str, object]) -> dict[str, object]:
        self.views.append(dict(view))
        return dict(view)


def _adapter():
    """Lazy import keeps this file an intended Red contract."""

    return importlib.import_module("central_iphone_response_web")


def _service(tmp_path: Path, clock: FakeClock, updates: list[dict[str, object]]):
    coordinator = CallCoordinator(
        database_path=tmp_path / "calls.sqlite3", clock=clock, notify=lambda _: None
    )
    service = TerminalCallService(
        database_path=tmp_path / "terminal.sqlite3",
        coordinator=coordinator,
        clock=clock,
        registered_credentials={
            "iphone-terminal": {
                "device_id": "iphone-browser-device",
                "terminal_category": "iphone_web",
                "terminal_platform": "ios",
            },
            "desktop-terminal": {
                "device_id": "desktop-device",
                "terminal_category": "desktop",
                "terminal_platform": "linux",
            },
        },
        on_state_update=updates.append,
    )
    return coordinator, service


def _call(coordinator: CallCoordinator, service: TerminalCallService, channels: set[str]) -> str:
    created = coordinator.create_call(
        caller_id="agent-a", request_id=f"create-{len(channels)}-{uuid.uuid4()}", payload={"prompt": "Proceed?"}
    )
    call_id = str(created["call_id"])
    service.track_call(call_id=call_id, enabled_channels=channels)
    return call_id


def _terminal_session(service: TerminalCallService, credential: str) -> str:
    result = service.authenticate(credential=credential)
    assert result["success"] is True
    return str(result["session_id"])


def _app(service: TerminalCallService, sessions: BrowserSessions, renderer: Renderer):
    return _adapter().create_app(
        terminal_service=service,
        operator_session_verifier=sessions,
        renderer=renderer,
    )


def _request(
    app: Any, method: str, path: str, *, token: str | None = None, body: dict[str, object] | None = None
) -> httpx.Response:
    async def run() -> httpx.Response:
        headers = {} if token is None else {"X-Operator-Session": token}
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://central.invalid") as client:
            return await client.request(method, path, headers=headers, json=body)

    return asyncio.run(run())


def _hidden(response: httpx.Response) -> None:
    assert response.status_code in {401, 404}
    assert response.json() == {"success": False, "status": "session_not_found"}


def test_terminal_service_control_is_gui_free_and_has_no_browser_renderer() -> None:
    """Existing durable control, independent of the absent web adapter."""

    source = inspect.getsource(TerminalCallService)
    assert "tkinter" not in source.lower()
    assert "render(" not in ast.unparse(ast.parse(source))


def test_link_only_or_missing_browser_session_is_inert_and_non_disclosing(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    sessions = BrowserSessions()
    app = _app(service, sessions, renderer)

    link_only = _request(app, "GET", f"/calls/{call_id}")
    api = _request(app, "GET", f"/api/calls/{call_id}")

    _hidden(link_only)
    _hidden(api)
    assert coordinator.get_call(call_id)["call_state"] == "ringing"
    assert updates == []
    assert renderer.views == []


def test_authenticated_ringing_page_is_read_only_and_exposes_explicit_actions(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark", "desktop"})
    iphone_session = _terminal_session(service, "iphone-terminal")
    sessions = BrowserSessions({"operator-browser": {"operator_id": "only-operator", "terminal_session_id": iphone_session}})
    app = _app(service, sessions, renderer)

    page = _request(app, "GET", f"/calls/{call_id}", token="operator-browser")
    api = _request(app, "GET", f"/api/calls/{call_id}", token="operator-browser")

    assert page.status_code == api.status_code == 200
    assert page.json() == api.json()
    view = page.json()
    assert view["call_id"] == call_id
    assert view["call_state"] == "ringing"
    assert view["can_accept"] is view["can_decline"] is True
    assert "accept" in view["actions"] and "decline" in view["actions"]
    assert "session" not in page.text.lower()
    assert coordinator.get_call(call_id)["call_state"] == "ringing"
    assert updates == []
    assert renderer.views == [view, view]


def test_post_accept_is_browser_session_bound_replayable_and_updates_after_persistence(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    iphone_session = _terminal_session(service, "iphone-terminal")
    sessions = BrowserSessions({"owner": {"operator_id": "only-operator", "terminal_session_id": iphone_session}})
    app = _app(service, sessions, renderer)

    accepted = _request(app, "POST", f"/calls/{call_id}/accept", token="owner", body={"operation_id": "web-accept"})
    replay = _request(app, "POST", f"/calls/{call_id}/accept", token="owner", body={"operation_id": "web-accept"})

    assert accepted.status_code == replay.status_code == 200
    assert accepted.json() == replay.json()
    assert accepted.json()["call_state"] == "accepted"
    assert accepted.json()["handled_on"] == "iphone_web"
    assert coordinator.get_call(call_id)["call_state"] == "accepted"
    assert updates == [{"call_id": call_id, "call_state": "accepted"}]
    assert "terminal_session_id" not in accepted.text and "owner_session_id" not in accepted.text


def test_decline_is_explicit_whole_call_action_and_replays(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    iphone_session = _terminal_session(service, "iphone-terminal")
    sessions = BrowserSessions({"owner": {"operator_id": "only-operator", "terminal_session_id": iphone_session}})
    app = _app(service, sessions, renderer)

    declined = _request(app, "POST", f"/calls/{call_id}/decline", token="owner", body={"operation_id": "web-decline"})
    replay = _request(app, "POST", f"/calls/{call_id}/decline", token="owner", body={"operation_id": "web-decline"})

    assert declined.status_code == replay.status_code == 200
    assert declined.json() == replay.json()
    assert coordinator.get_call(call_id)["call_state"] == "declined"
    assert updates == [{"call_id": call_id, "call_state": "declined"}]


def test_route_foreign_unknown_and_late_states_are_uniformly_hidden(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    bark_call = _call(coordinator, service, {"bark"})
    # The coordinator intentionally permits only one active call.  Seed the
    # first call's terminal state before constructing a desktop-only call so
    # this test exercises route exclusion on a genuinely distinct call rather
    # than accidentally reusing the active Bark call.
    with sqlite3.connect(tmp_path / "calls.sqlite3") as conn:
        conn.execute("UPDATE calls SET call_state='answered' WHERE call_id=?", (bark_call,))
    desktop_call = _call(coordinator, service, {"desktop"})
    assert desktop_call != bark_call
    iphone_session = _terminal_session(service, "iphone-terminal")
    other_iphone = _terminal_session(service, "iphone-terminal")
    sessions = BrowserSessions({
        "owner": {"operator_id": "only-operator", "terminal_session_id": iphone_session},
        "other": {"operator_id": "only-operator", "terminal_session_id": other_iphone},
    })
    app = _app(service, sessions, renderer)
    for response in (
        _request(app, "GET", f"/calls/{desktop_call}", token="owner"),
        _request(app, "GET", "/calls/no-such-call", token="owner"),
        _request(app, "GET", f"/calls/{bark_call}", token="other"),
        _request(app, "POST", f"/calls/{bark_call}/decline", token="other", body={"operation_id": "late"}),
    ):
        _hidden(response)
    # The first call was deliberately settled before construction of the
    # distinct desktop-only call.  A late or foreign browser operation cannot
    # revive it or disclose a writable state.
    assert coordinator.get_call(bark_call)["call_state"] == "answered"
    assert updates == []


def test_desktop_processing_and_processed_views_are_truthful_not_editable(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark", "desktop"})
    desktop_session = _terminal_session(service, "desktop-terminal")
    iphone_session = _terminal_session(service, "iphone-terminal")
    sessions = BrowserSessions({"browser": {"operator_id": "only-operator", "terminal_session_id": iphone_session}})
    app = _app(service, sessions, renderer)
    accepted = service.accept(call_id=call_id, session_id=desktop_session, operation_id="desktop-take")
    assert accepted["success"] is True

    processing = _request(app, "GET", f"/calls/{call_id}", token="browser")
    assert processing.status_code == 200
    assert processing.json()["call_state"] == "accepted"
    assert processing.json()["handled_on"] == "desktop"
    assert processing.json()["can_accept"] is processing.json()["can_decline"] is False
    assert processing.json()["display_state"] == "handled_on_desktop_processing"

    # This bounded adapter does not yet own task submissions.  Seed the
    # authoritative completed state to verify that a late browser view is
    # truthful and remains non-editable rather than pretending the desktop is
    # still processing.
    with sqlite3.connect(tmp_path / "calls.sqlite3") as conn:
        conn.execute("UPDATE calls SET call_state='answered' WHERE call_id=?", (call_id,))
    processed = _request(app, "GET", f"/calls/{call_id}", token="browser")
    assert processed.status_code == 200
    assert processed.json()["display_state"] == "handled_on_desktop_processed"
    assert processed.json()["can_accept"] is processed.json()["can_decline"] is False


def test_invalid_method_or_body_has_no_state_or_update_side_effect(tmp_path: Path) -> None:
    clock, updates, renderer = FakeClock(), [], Renderer()
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    iphone_session = _terminal_session(service, "iphone-terminal")
    sessions = BrowserSessions({"owner": {"operator_id": "only-operator", "terminal_session_id": iphone_session}})
    app = _app(service, sessions, renderer)

    for response in (
        _request(app, "PUT", f"/calls/{call_id}/accept", token="owner", body={"operation_id": "bad"}),
        _request(app, "POST", f"/calls/{call_id}/accept", token="owner", body={}),
        _request(app, "POST", f"/calls/{call_id}/decline", token="owner", body={"other": "bad"}),
    ):
        assert response.status_code in {400, 404, 405}
    assert coordinator.get_call(call_id)["call_state"] == "ringing"
    assert updates == []
