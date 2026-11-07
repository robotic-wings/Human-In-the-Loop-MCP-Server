"""Red contract for the GUI-free central terminal HTTP adapter.

``central_terminal_http_service`` is deliberately a thin ASGI boundary around
the already durable ``TerminalCallService``.  It does not render a page or
implement browser login, cookies, CSRF, TLS, forms, submission, attachments,
Bark, or device integration.  A terminal credential creates an opaque terminal
session; that credential is distinct from the Agent Bearer credential used by
``/mcp``.  The resulting session is the only capability for terminal reads and
writes in this bounded slice.
"""

from __future__ import annotations

import asyncio
import ast
import importlib
import inspect
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


def _adapter():
    """Lazy import retains the terminal-service source control during Red."""

    return importlib.import_module("central_terminal_http_service")


def _service(tmp_path: Path, clock: FakeClock, updates: list[dict[str, object]]):
    coordinator = CallCoordinator(
        database_path=tmp_path / "calls.sqlite3", clock=clock, notify=lambda _: None
    )
    service = TerminalCallService(
        database_path=tmp_path / "terminal.sqlite3",
        coordinator=coordinator,
        clock=clock,
        registered_credentials={
            "terminal-mobile-secret": {
                "device_id": "opaque-mobile",
                "terminal_category": "iphone_web",
                "terminal_platform": "ios",
            },
            "terminal-other-secret": {
                "device_id": "opaque-other-mobile",
                "terminal_category": "iphone_web",
                "terminal_platform": "ios",
            },
            "terminal-desktop-secret": {
                "device_id": "opaque-desktop",
                "terminal_category": "desktop",
                "terminal_platform": "linux",
            },
        },
        on_state_update=updates.append,
    )
    return coordinator, service


def _call(coordinator: CallCoordinator, service: TerminalCallService, channels: set[str]) -> str:
    created = coordinator.create_call(
        caller_id="agent-a", request_id="create-1", payload={"prompt": "Proceed?"}
    )
    call_id = str(created["call_id"])
    service.track_call(call_id=call_id, enabled_channels=channels)
    return call_id


def _app(service: TerminalCallService):
    return _adapter().create_app(terminal_service=service)


def _request(
    app: Any, method: str, path: str, *, body: dict[str, object] | None = None,
    credential: str | None = None, session: str | None = None,
) -> httpx.Response:
    async def run() -> httpx.Response:
        headers: dict[str, str] = {}
        if credential is not None:
            headers["Authorization"] = f"Terminal {credential}"
        if session is not None:
            headers["X-Terminal-Session"] = session
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://central") as client:
            return await client.request(method, path, json=body, headers=headers)

    return asyncio.run(run())


def _session(app: Any, credential: str = "terminal-mobile-secret") -> str:
    response = _request(app, "POST", "/api/terminal/sessions", credential=credential)
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    return str(body["session_id"])


def _hidden(response: httpx.Response) -> None:
    assert response.status_code in {401, 404}
    assert response.json() == {"success": False, "status": "session_not_found"}


def test_terminal_service_control_remains_gui_free() -> None:
    """Passing control independent of the absent ASGI adapter."""

    source = inspect.getsource(TerminalCallService)
    assert "tkinter" not in source.lower()
    assert "owner_session_id" in ast.unparse(ast.parse(source))


def test_terminal_credential_endpoint_is_distinct_from_agent_mcp_bearer(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    _, service = _service(tmp_path, clock, updates)
    app = _app(service)

    denied = _request(app, "POST", "/api/terminal/sessions", credential="agent-a")
    wrong_scheme = _request(app, "POST", "/api/terminal/sessions")
    session = _session(app)

    assert denied.status_code == wrong_scheme.status_code == 401
    assert denied.json()["status"] == wrong_scheme.json()["status"] == "terminal_not_authenticated"
    assert session
    assert updates == []


def test_mobile_get_views_are_read_only_and_route_eligible(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark", "desktop"})
    app = _app(service)
    session = _session(app)

    page = _request(app, "GET", f"/calls/{call_id}", session=session)
    api = _request(app, "GET", f"/api/calls/{call_id}", session=session)

    assert page.status_code == api.status_code == 200
    assert page.json() == api.json()
    assert api.json()["call_state"] == "ringing"
    assert coordinator.get_call(call_id)["call_state"] == "ringing"
    assert updates == []

    desktop_session = _session(app, "terminal-desktop-secret")
    # An arbitrary ID and an otherwise valid call excluded from this terminal's
    # route must be indistinguishable and inert.
    _hidden(_request(app, "GET", f"/api/calls/{call_id}", session=desktop_session))
    _hidden(_request(app, "GET", "/api/calls/no-such-call", session=desktop_session))
    assert updates == []


def test_session_bound_accept_replays_then_updates_only_after_persistence(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    app = _app(service)
    session = _session(app)
    request = {"operation_id": "accept-1"}

    accepted = _request(app, "POST", f"/api/calls/{call_id}/accept", body=request, session=session)
    replay = _request(app, "POST", f"/api/calls/{call_id}/accept", body=request, session=session)

    assert accepted.status_code == replay.status_code == 200
    assert replay.json() == accepted.json()
    assert accepted.json()["call_state"] == "accepted"
    assert updates == [{"call_id": call_id, "call_state": "accepted"}]
    assert "session_id" not in accepted.text and "owner_session_id" not in accepted.text

    reopened = _app(TerminalCallService(
        database_path=tmp_path / "terminal.sqlite3", coordinator=coordinator, clock=clock,
        registered_credentials={"terminal-mobile-secret": {"device_id": "opaque-mobile", "terminal_category": "iphone_web", "terminal_platform": "ios"}},
        on_state_update=lambda _: pytest.fail("read/replay must not update"),
    ))
    owner_view = _request(reopened, "GET", f"/api/calls/{call_id}", session=session)
    assert owner_view.status_code == 200
    assert owner_view.json()["can_write"] is True


def test_foreign_unknown_and_ineligible_terminal_calls_are_non_disclosing(tmp_path: Path) -> None:
    clock, updates = FakeClock(), []
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    app = _app(service)
    owner, other, desktop = _session(app), _session(app, "terminal-other-secret"), _session(app, "terminal-desktop-secret")
    assert _request(app, "POST", f"/api/calls/{call_id}/accept", body={"operation_id": "owner"}, session=owner).status_code == 200

    foreign = _request(app, "GET", f"/api/calls/{call_id}", session=other)
    unknown = _request(app, "GET", "/api/calls/no-such-call", session=other)
    ineligible = _request(app, "POST", f"/api/calls/{call_id}/decline", body={"operation_id": "nope"}, session=desktop)
    for response in (foreign, unknown, ineligible):
        _hidden(response)
    assert len(updates) == 1


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("PUT", "/api/calls/nope/accept", {"operation_id": "x"}),
        ("POST", "/api/calls/nope/accept", {}),
        ("POST", "/api/calls/nope/decline", {"unexpected": True}),
    ],
)
def test_invalid_requests_cause_no_transition_or_update(
    tmp_path: Path, method: str, path: str, body: dict[str, object]
) -> None:
    clock, updates = FakeClock(), []
    coordinator, service = _service(tmp_path, clock, updates)
    call_id = _call(coordinator, service, {"bark"})
    app = _app(service)
    session = _session(app)
    response = _request(app, method, path, body=body, session=session)
    assert response.status_code in {400, 404, 405}
    assert coordinator.get_call(call_id)["call_state"] == "ringing"
    assert updates == []
