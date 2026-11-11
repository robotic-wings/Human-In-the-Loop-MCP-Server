"""Contract tests locking the ``get_user_choice`` ``selected_choices`` behaviour.

Every other interaction family (``confirmed``, ``acknowledged``, ``feedback``,
``user_input``) has contract coverage; ``selected_choices`` had none.  These
tests lock the existing, correct behaviour: a single-select submission carries
a one-element list, a multi-select submission carries several values, and both
the mobile web form and the desktop terminal renderer collect ``selected_choices``
into the same shape that reaches the Agent through the submission boundary.
"""

from __future__ import annotations

import asyncio
import html as html_module
import sqlite3
from pathlib import Path
from typing import Any

import httpx

from central_desktop_registration_sync import DesktopRegistrationStateService
from central_desktop_terminal import TkPresentation
from central_http_service import _mobile_response_fields
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
        operator_directory={
            "desktop-owner": {"password": "desktop-secret", "devices": ["desktop-owner"], "primary": True},
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


def create_choice(app: Any, *, request_id: str = "choice-create",
                  multiple: bool = False) -> dict[str, object]:
    payload: dict[str, object] = {
        "tool": "get_user_choice", "prompt": "Pick an option", "choices": ["one", "two", "three"],
    }
    if multiple:
        payload["multiple"] = True
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": request_id, "client_timeout_seconds": 2, "payload": payload,
    })
    assert response.status_code == 200
    return response.json()


def terminal_session(app: Any) -> str:
    response = request(app, "POST", "/api/terminals/register", body={
        "callsign": "desktop-owner", "password": "desktop-secret", "device_id": "desktop-owner", "terminal_platform": "windows",
        "endpoint": "https://desktop.test/events", "gui_ready": True,
    })
    if response.status_code != 201:
        async def register() -> httpx.Response:
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
                return await client.post("/api/terminals/register", json={
                    "callsign": "desktop-owner", "password": "desktop-secret", "device_id": "desktop-owner", "terminal_platform": "windows",
                    "endpoint": "https://desktop.test/events", "gui_ready": True,
                })
        response = asyncio.run(register())
    assert response.status_code == 201
    return str(response.json()["session_id"])


def accept_desktop(app: Any, call: dict[str, object], session: str) -> None:
    response = request(app, "POST", f"/api/calls/{call['call_id']}/accept", terminal=session,
                       body={"operation_id": "desktop-accept"})
    assert response.status_code == 200 and response.json()["call_state"] == "accepted"


def accept_browser(app: Any, call: dict[str, object], operation_id: str = "web-accept") -> None:
    response = request(app, "POST", f"/calls/{call['call_id']}/accept", browser="operator",
                       body={"operation_id": operation_id})
    assert response.status_code == 200


def continuation(app: Any, call: dict[str, object], after: int) -> dict[str, object]:
    response = request(app, "POST", "/mcp", agent="agent-secret", body={
        "request_id": f"continue-{call['call_id']}-{after}", "client_timeout_seconds": 2,
        "interaction_id": call["interaction_id"], "call_id": call["call_id"], "after_event_seq": after,
    })
    assert response.status_code == 200
    return response.json()


def test_desktop_single_select_reaches_agent_as_one_value_list(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_choice(app)
    accept_desktop(app, call, owner)

    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "choice-single-1", "result": {"selected_choices": ["two"]},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "answered"
    assert submitted.json()["result"]["selected_choices"] == ["two"]

    delivered = continuation(app, call, after=1)
    assert delivered["result"]["selected_choices"] == ["two"]
    assert isinstance(delivered["result"]["selected_choices"], list)
    assert len(delivered["result"]["selected_choices"]) == 1


def test_desktop_multi_select_reaches_agent_as_multiple_value_list(tmp_path: Path) -> None:
    app, _clock = components(tmp_path)
    owner = terminal_session(app)
    call = create_choice(app, multiple=True)
    accept_desktop(app, call, owner)

    submitted = request(app, "POST", f"/api/calls/{call['call_id']}/submissions", terminal=owner, body={
        "submission_id": "choice-multi-1", "result": {"selected_choices": ["one", "three"]},
    })
    assert submitted.status_code == 200
    assert submitted.json()["status"] == "answered"
    assert submitted.json()["result"]["selected_choices"] == ["one", "three"]

    delivered = continuation(app, call, after=1)
    assert delivered["result"]["selected_choices"] == ["one", "three"]
    assert len(delivered["result"]["selected_choices"]) == 2


def test_mobile_selected_choices_match_desktop_shape(tmp_path: Path) -> None:
    app, _clock = components(tmp_path, route_mode="iphone")
    call = create_choice(app)
    accept_browser(app, call)

    submitted = request(app, "POST", f"/calls/{call['call_id']}/submissions", browser="operator", body={
        "submission_id": "choice-web-1", "result": {"selected_choices": ["one"]},
    })
    assert submitted.status_code == 200
    assert submitted.json()["handled_on"] == "iphone_web"
    assert submitted.json()["result"]["selected_choices"] == ["one"]

    delivered = continuation(app, call, after=1)
    assert delivered["result"]["selected_choices"] == ["one"]

    multi_call = create_choice(app, request_id="choice-multi-web", multiple=True)
    accept_browser(app, multi_call, operation_id="web-accept-multi")
    multi = request(app, "POST", f"/calls/{multi_call['call_id']}/submissions", browser="operator", body={
        "submission_id": "choice-web-multi", "result": {"selected_choices": ["one", "two"]},
    })
    assert multi.status_code == 200
    assert multi.json()["result"]["selected_choices"] == ["one", "two"]
    assert continuation(app, multi_call, after=1)["result"]["selected_choices"] == ["one", "two"]


def test_mobile_choice_form_renders_radio_for_single_and_checkbox_for_multiple() -> None:
    def esc(value: object) -> str:
        return html_module.escape(str(value), quote=True)

    single = _mobile_response_fields("get_user_choice", {"choices": ["one", "two"]}, esc)
    assert 'data-result-key="selected_choices"' in single
    assert 'type="radio"' in single
    assert 'type="checkbox"' not in single
    assert 'value="one"' in single and 'value="two"' in single

    multi = _mobile_response_fields("get_user_choice", {"choices": ["one", "two"], "multiple": True}, esc)
    assert 'data-result-key="selected_choices"' in multi
    assert 'type="checkbox"' in multi
    assert 'type="radio"' not in multi


class FakeVar:
    def __init__(self, value: object = None) -> None:
        self._value = value

    def get(self) -> object:
        return self._value

    def set(self, value: object) -> None:
        self._value = value


class FakeWidget:
    def pack(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self
    def grid(self, *_args: object, **_kwargs: object) -> "FakeWidget":
        return self

    def columnconfigure(self, *_args: object, **_kwargs: object) -> None:
        return None



class FakeTk:
    def __init__(self) -> None:
        self.string_vars: list[FakeVar] = []
        self.boolean_vars: list[FakeVar] = []
    def LabelFrame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()

    def Frame(self, _root: object, **kwargs: object) -> object:
        return FakeWidget()


    def StringVar(self, value: str = "") -> FakeVar:
        var = FakeVar(value)
        self.string_vars.append(var)
        return var

    def BooleanVar(self, value: bool = False) -> FakeVar:
        var = FakeVar(value)
        self.boolean_vars.append(var)
        return var

    def Label(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Radiobutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()

    def Checkbutton(self, *_args: object, **_kwargs: object) -> FakeWidget:
        return FakeWidget()


def test_desktop_choice_controls_collect_single_and_multiple_selections() -> None:
    single_tk = FakeTk()
    build_single = TkPresentation._response_controls(
        None, "get_user_choice", {"choices": ["one", "two"]}, single_tk
    )
    assert build_single() is None
    single_tk.string_vars[0].set("two")
    assert build_single() == {"selected_choices": ["two"]}

    multi_tk = FakeTk()
    build_multi = TkPresentation._response_controls(
        None, "get_user_choice", {"choices": ["one", "two"], "multiple": True}, multi_tk
    )
    assert build_multi() is None
    multi_tk.boolean_vars[0].set(True)
    multi_tk.boolean_vars[1].set(True)
    assert build_multi() == {"selected_choices": ["one", "two"]}
