"""End-to-end acceptance tests for every interaction tool family over MCP.

These walk the real central HTTP boundary through the MCP JSON-RPC 2.0
``tools/call`` interface: the Agent creates a call with a tool name and
arguments, a human accepts and submits over the desktop terminal endpoints,
and the Agent then continues with ``interaction_id``/``call_id``/
``after_event_seq`` to read the durable result.  Each tool must surface its
correct top-level status and result fields, not a reduced ``answered``
fallback.
"""

from __future__ import annotations

import base64
import itertools
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import uvicorn

from central_http_service import CentralServiceConfig, create_lifecycle_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _serve(app: Any, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() >= deadline:
            raise AssertionError("HTTP server did not start")
        time.sleep(0.01)
    return server, thread


async def _no_wait(_seconds: float) -> None:
    return None


def _build_config(tmp_path: Path, port: int) -> CentralServiceConfig:
    return CentralServiceConfig.from_mapping({
        "listen_host": "127.0.0.1",
        "listen_port": port,
        "web_base_url": "https://central.test",
        "data_dir": str(tmp_path / "central-data"),
        "agent_credentials": {"agent-a": "agent-secret"},
        "tls_cert_file": "unused-in-test.crt",
        "tls_key_file": "unused-in-test.key",
        "route_mode": "desktop",
        "desktop_credentials": {
            "desktop-secret": {"device_id": "desktop-61", "terminal_platform": "windows"},
        },
        "desktop_endpoints": {
            "desktop-61": {"endpoint": "http://127.0.0.1:9100/terminal/events", "callback_credential": "callback-secret"},
        },
    })


@pytest.fixture
def central(tmp_path: Path) -> str:
    port = _free_port()
    config = _build_config(tmp_path, port)
    app = create_lifecycle_app(
        config,
        clock=time.time,
        wait=_no_wait,
        bark_dispatch=lambda _payload: {"http_status": 503, "business_success": False},
        desktop_dispatch=lambda _payload: {"accepted": True},
        operator_session_verifier=object(),
    )
    server, thread = _serve(app, port)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _register_terminal(base_url: str) -> str:
    with httpx.Client(base_url=base_url, timeout=8) as client:
        response = client.post(
            "/api/terminals/register",
            json={"callsign": "desktop-61", "password": "desktop-secret", "device_id": "desktop-61", "terminal_platform": "windows", "endpoint": "http://127.0.0.1:9100/terminal/events", "gui_ready": True},
        )
    assert response.status_code == 201, response.text
    return str(response.json()["session_id"])


def _accept(base_url: str, call_id: str, session: str) -> None:
    with httpx.Client(base_url=base_url, timeout=8) as client:
        response = client.post(
            f"/api/calls/{call_id}/accept",
            headers={"x-terminal-session": session},
            json={"operation_id": uuid.uuid4().hex},
        )
    assert response.status_code == 200, response.text


def _submit(base_url: str, call_id: str, session: str, result: dict[str, object]) -> None:
    with httpx.Client(base_url=base_url, timeout=8) as client:
        response = client.post(
            f"/api/calls/{call_id}/submissions",
            headers={"x-terminal-session": session},
            json={"submission_id": uuid.uuid4().hex, "result": result},
        )
    assert response.status_code == 200, response.text


def _upload(base_url: str, call_id: str, session: str, filename: str, content: bytes) -> dict[str, object]:
    with httpx.Client(base_url=base_url, timeout=8) as client:
        response = client.post(
            f"/api/calls/{call_id}/attachments",
            headers={"x-terminal-session": session},
            json={
                "filename": filename, "media_type": "application/octet-stream",
                "content": base64.b64encode(content).decode("ascii"),
            },
        )
    assert response.status_code == 200, response.text
    return response.json()


def _human_cancel(base_url: str, call_id: str, session: str) -> None:
    with httpx.Client(base_url=base_url, timeout=8) as client:
        response = client.post(
            f"/api/calls/{call_id}/cancel",
            headers={"x-terminal-session": session},
            json={"operation_id": uuid.uuid4().hex},
        )
    assert response.status_code == 200, response.text


_rid = itertools.count(1)


def _tool_call(base_url: str, name: str, arguments: dict[str, object]) -> dict[str, object]:
    rid = next(_rid)
    with httpx.Client(base_url=base_url, timeout=8) as client:
        response = client.post(
            "/mcp",
            headers={"authorization": "Bearer agent-secret"},
            json={"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                  "params": {"name": name, "arguments": arguments}},
        )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == rid
    result = body["result"]
    assert isinstance(result, dict)
    structured = result["structuredContent"]
    assert isinstance(structured, dict)
    return dict(structured)


def _answer(base_url: str, name: str, arguments: dict[str, object], result: dict[str, object]) -> dict[str, object]:
    session = _register_terminal(base_url)
    created = _tool_call(base_url, name, arguments)
    assert created["call_state"] == "ringing"
    _accept(base_url, str(created["call_id"]), session)
    _submit(base_url, str(created["call_id"]), session, result)
    return _tool_call(base_url, name, {
        "interaction_id": created["interaction_id"], "call_id": created["call_id"], "after_event_seq": 1,
    })


@pytest.mark.parametrize(
    ("name", "arguments", "result", "field", "expected"),
    [
        ("get_user_input", {"prompt": "say hi"}, {"user_input": "hello"}, "user_input", "hello"),
        ("get_user_input", {"prompt": "a number"}, {"user_input": "42"}, "user_input", "42"),
        ("get_multiline_input", {"prompt": "details"}, {"user_input": "line1\nline2"}, "user_input", "line1\nline2"),
        ("get_feedback_from_user", {"prompt": "feedback?"}, {"feedback": "great job"}, "feedback", "great job"),
    ],
)
def test_simple_answer_tools_surface_result(central: str, name: str, arguments: dict[str, object],
                                            result: dict[str, object], field: str, expected: object) -> None:
    delivered = _answer(central, name, arguments, result)
    assert delivered["status"] == "answered"
    assert delivered["call_state"] == "answered"
    assert delivered["result"][field] == expected


def test_show_info_message_surfaces_acknowledged(central: str) -> None:
    delivered = _answer(central, "show_info_message", {"message": "notice"}, {"acknowledged": True})
    assert delivered["status"] == "answered"
    assert delivered["result"]["acknowledged"] is True


def test_confirmation_false_is_answered_not_declined(central: str) -> None:
    delivered = _answer(central, "show_confirmation_dialog", {"prompt": "confirm?"}, {"confirmed": False})
    assert delivered["status"] == "answered"
    assert delivered["call_state"] == "answered"
    assert delivered["result"]["confirmed"] is False


def test_choice_single_and_multi_surface_selected_choices(central: str) -> None:
    single = _answer(
        central, "get_user_choice", {"choices": ["one", "two"], "prompt": "pick one"},
        {"selected_choices": ["two"]},
    )
    assert single["status"] == "answered"
    assert single["result"]["selected_choices"] == ["two"]

    multi = _answer(
        central, "get_user_choice",
        {"choices": ["one", "two", "three"], "multiple": True, "prompt": "pick many"},
        {"selected_choices": ["one", "three"]},
    )
    assert multi["status"] == "answered"
    assert multi["result"]["selected_choices"] == ["one", "three"]


def test_task_completed_and_failed_surface_report_and_resubmittable(central: str) -> None:
    completed = _answer(
        central, "assign_task_to_human", {"task_description": "deploy"},
        {"status": "completed", "report": "deployed cleanly"},
    )
    assert completed["status"] == "completed"
    assert completed["call_state"] == "completed"
    assert completed["report"] == "deployed cleanly"
    assert completed["resubmittable"] is True

    failed = _answer(
        central, "assign_task_to_human", {"task_description": "deploy"},
        {"status": "failed", "report": "deploy broke"},
    )
    assert failed["status"] == "failed"
    assert failed["call_state"] == "failed"
    assert failed["report"] == "deploy broke"
    assert failed["resubmittable"] is True


def test_task_progress_holds_in_progress_with_needs_continuation(central: str) -> None:
    session = _register_terminal(central)
    created = _tool_call(central, "assign_task_to_human", {"task_description": "deploy"})
    _accept(central, str(created["call_id"]), session)
    _submit(central, str(created["call_id"]), session, {"status": "in_progress", "progress": "halfway"})

    delivered = _tool_call(central, "assign_task_to_human", {
        "interaction_id": created["interaction_id"], "call_id": created["call_id"], "after_event_seq": 1,
    })
    assert delivered["status"] == "in_progress"
    assert delivered["call_state"] == "in_progress"
    assert delivered["progress"] == "halfway"
    assert delivered["needs_continuation"] is True


def test_task_human_cancel_surfaces_human_cancelled(central: str) -> None:
    session = _register_terminal(central)
    created = _tool_call(central, "assign_task_to_human", {"task_description": "deploy"})
    _accept(central, str(created["call_id"]), session)
    _human_cancel(central, str(created["call_id"]), session)

    delivered = _tool_call(central, "assign_task_to_human", {
        "interaction_id": created["interaction_id"], "call_id": created["call_id"], "after_event_seq": 1,
    })
    assert delivered["status"] == "cancelled"
    assert delivered["call_state"] == "cancelled"
    assert delivered["reason"] == "human_cancelled"


def test_attachment_upload_surfaces_metadata_with_final_result(central: str) -> None:
    content = b"\x00\x01\x02attachment-payload"
    session = _register_terminal(central)
    created = _tool_call(central, "assign_task_to_human", {"task_description": "deploy"})
    _accept(central, str(created["call_id"]), session)

    uploaded = _upload(central, str(created["call_id"]), session, "log.txt", content)
    attachment_id = uploaded["attachment_id"]

    _submit(central, str(created["call_id"]), session, {
        "status": "completed", "report": "done with log", "attachments": [attachment_id],
    })
    delivered = _tool_call(central, "assign_task_to_human", {
        "interaction_id": created["interaction_id"], "call_id": created["call_id"], "after_event_seq": 1,
    })

    assert delivered["status"] == "completed"
    attachments = delivered["attachments"]
    assert isinstance(attachments, list) and len(attachments) == 1
    assert attachments[0]["attachment_id"] == attachment_id
    assert attachments[0]["filename"] == "log.txt"
    assert attachments[0]["byte_count"] == len(content)


def test_agent_cancel_interaction_surfaces_agent_cancelled(central: str) -> None:
    _register_terminal(central)
    created = _tool_call(central, "get_user_input", {"prompt": "x"})
    assert created["call_state"] == "ringing"

    cancelled = _tool_call(central, "cancel_interaction", {
        "interaction_id": created["interaction_id"], "call_id": created["call_id"],
    })
    assert cancelled["status"] == "cancelled"
    assert cancelled["call_state"] == "cancelled"
    assert cancelled["reason"] == "agent_cancelled"
