"""Red HTTPS contract for accepted-iPhone ordinary task-cancel failure presentation."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _snapshot, _writer_headers
from test_iphone_task_cancellation import _accept, _call, _cancel
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation


def _handler(source: str) -> str:
    match = re.search(
        r"const cancel=document\.querySelector\('button\[data-task-cancel\]'\);"
        r"cancel\.addEventListener\('click',\(\)=>\{(?P<body>.*?)\}\);",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_tls_accepted_task_cancel_renders_ready_and_bound_non_ok_status(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-cancel-status"); call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally: _stop(process)
    source = str(page[2]); assert page[0] == 200
    assert 'data-task-cancel-status="ready"' in source
    assert 'data-task-cancel="true"' in source and 'data-task-report' in source
    handler = _handler(source)
    assert "action_id:crypto.randomUUID()" in handler and "cancelled:true" in handler
    assert "if(response.ok)location.reload();" in handler
    failed = re.search(r"if\\(response\\.ok\\)\\{.*?\\}else\\{(?P<body>.*?)\\}", handler, re.DOTALL)
    assert failed is not None
    failure = failed.group("body")
    assert any(word in failure for word in ("not-submitted", "conflict", "unavailable"))
    assert "location.reload" not in failure and "cancelled:" not in failure and "feedback_requested" not in failure


def test_tls_task_cancel_rejections_and_disconnect_settlement_preserve_authoritative_state(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-cancel-failure-nonmutation")
        call_id, interaction_id = created["call_id"], created["interaction_id"]; assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200
        writer = _writer_headers(port, certificate, call_id, owner, "task-cancel-failure-nonmutation")
        before = _snapshot(database, call_id)
        malformed = _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": "bad", "cancelled": False}, headers=writer)
        replay = _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": "bad", "cancelled": False}, headers=writer)
        stale = _request(port, certificate, "POST", f"/api/calls/{call_id}/cancel", body={"action_id": "stale", "cancelled": True}, headers={**writer, "X-Writer-Token": "stale"})
        after = _snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        settled = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        late = _cancel(port, certificate, call_id, "late", writer)
        continuation = _continuation("task-cancel-failure-event", interaction_id, call_id, 1)
        continuation["params"]["arguments"]["task_id"] = continuation["params"]["arguments"].pop("interaction_id")
        _, event, _ = _post(port, certificate, continuation, "test-token-alpha")
        final = _snapshot(database, call_id)
    finally: _stop(process)
    assert malformed[0] == replay[0] in {400, 409} and stale[0] in {400, 401, 403, 409} and before == after
    assert settled[0] == 200 and late[0] == 409 and event is not None
    assert final[0] == "disconnected" and final[1] == before[1] + 1
    assert final[2:5] == before[2:5] and final[5] == before[5] + 1 and final[6:] == before[6:]


def test_tls_task_cancel_failure_page_boundaries_are_readonly_and_nonmutating(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-cancel-failure-boundary"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port)
        ringing_before = _snapshot(database, call_id); ringing = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); ringing_after = _snapshot(database, call_id)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        writer = _writer_headers(port, certificate, call_id, owner, "task-cancel-failure-boundary"); accepted = _snapshot(database, call_id)
        denied = (
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie}),
            _cancel(port, certificate, call_id, "other", _headers(other_cookie, other_csrf, port)),
            _cancel(port, certificate, call_id, "tokenless", owner),
            _cancel(port, certificate, call_id, "csrf", {**writer, "X-CSRF-Token": "bad"}),
            _cancel(port, certificate, call_id, "origin", {**writer, "Origin": "https://evil.example"}),
            _cancel(port, certificate, call_id, "host", {**writer, "Host": "evil.example"}),
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer terminal-secret"}),
        )
        reloaded = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); after = _snapshot(database, call_id)
    finally: _stop(process)
    assert ringing_before == ringing_after and ringing_before[0] == "ringing" and ringing[0] == 200 and "data-task-cancel" not in str(ringing[2])
    assert accepted == after and reloaded[0] == 200 and 'data-task-cancel="true"' in str(reloaded[2])
    assert all(item[0] in {200, 400, 401, 403, 404, 409} for item in denied) and all("data-task-cancel" not in str(item[2]) for item in denied)
