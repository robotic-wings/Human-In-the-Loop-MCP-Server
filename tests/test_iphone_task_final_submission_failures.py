"""Red HTTPS contract for accepted-iPhone task-final failure presentation."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _snapshot, _writer_headers
from test_iphone_task_final_submission import _accept, _call, _submit
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation


def _handler(source: str, status: str) -> str:
    match = re.search(
        rf"const normal{status.title()}=document\.querySelector\('button\[data-task-final=\"{status}\"\]'\);"
        rf"normal{status.title()}\.addEventListener\('click',\(\)=>\{{(?P<body>.*?)\}}\);",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_tls_accepted_task_final_renders_ready_and_bound_non_ok_status(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-final-status"); call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally: _stop(process)
    source = str(page[2]); assert page[0] == 200
    assert 'data-task-final-status="ready"' in source
    for status in ("completed", "failed"):
        handler = _handler(source, status)
        assert "submission_id:crypto.randomUUID()" in handler and f"status:{status}" not in handler
        assert "task_version:Number(" in handler and "attachment_ids:pendingAttachmentIds" in handler
        assert "if(response.ok)location.reload();" in handler
        failed = re.search(r"if\(response\.ok\)\{.*?\}else\{(?P<body>.*?)\}", handler, re.DOTALL)
        assert failed is not None
        failure = failed.group("body")
        assert any(word in failure for word in ("not-submitted", "conflict", "unavailable"))
        assert "location.reload" not in failure and not any(word in failure for word in ("completed", "failed", "cancelled", "in_progress", "feedback_requested", "result"))


def test_tls_task_final_rejections_and_disconnect_settlement_preserve_authoritative_state(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-final-failure-nonmutation"); call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate); owner = _headers(cookie, csrf, port); assert _accept(port, certificate, call_id, owner)[0] == 200
        writer = _writer_headers(port, certificate, call_id, owner, "task-final-failure-nonmutation")
        before = _snapshot(database, call_id)
        malformed = _submit(port, certificate, call_id, "bad", "other", "report", writer)
        replay = _submit(port, certificate, call_id, "bad", "other", "report", writer)
        stale = _submit(port, certificate, call_id, "stale", "completed", "report", {**writer, "X-Writer-Token": "stale"})
        after = _snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        settled = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        late = _submit(port, certificate, call_id, "late", "completed", "report", writer)
        continuation = _continuation("task-final-failure-event", interaction_id, call_id, 1); continuation["params"]["arguments"]["task_id"] = continuation["params"]["arguments"].pop("interaction_id")
        _, event, _ = _post(port, certificate, continuation, "test-token-alpha")
        final = _snapshot(database, call_id)
    finally: _stop(process)
    assert malformed[0] == replay[0] in {400, 409} and stale[0] in {400, 401, 403, 409} and before == after
    assert settled[0] == 200 and late[0] == 409 and event is not None
    assert final[0] == "disconnected" and final[1] == before[1] + 1 and final[2:5] == before[2:5]
    assert final[5] == before[5] + 1 and final[6:] == before[6:]


def test_tls_task_final_failure_page_boundaries_are_readonly_and_nonmutating(tmp_path: Path) -> None:
    port = _free_port(); config, certificate = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"; process = _start(config); _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "task-final-failure-boundary"); call_id = created["call_id"]; assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate); other_cookie, other_csrf = _login(port, certificate); owner = _headers(owner_cookie, owner_csrf, port)
        ringing_before = _snapshot(database, call_id); ringing = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); ringing_after = _snapshot(database, call_id)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        writer = _writer_headers(port, certificate, call_id, owner, "task-final-failure-boundary"); accepted = _snapshot(database, call_id)
        denied = (
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie}),
            _submit(port, certificate, call_id, "other", "completed", "x", _headers(other_cookie, other_csrf, port)),
            _submit(port, certificate, call_id, "tokenless", "completed", "x", owner),
            _submit(port, certificate, call_id, "csrf", "completed", "x", {**writer, "X-CSRF-Token": "bad"}),
            _submit(port, certificate, call_id, "origin", "completed", "x", {**writer, "Origin": "https://evil.example"}),
            _submit(port, certificate, call_id, "host", "completed", "x", {**writer, "Host": "evil.example"}),
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer terminal-secret"}),
        )
        reloaded = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie}); after = _snapshot(database, call_id)
    finally: _stop(process)
    assert ringing_before == ringing_after and ringing_before[0] == "ringing" and ringing[0] == 200 and "data-task-final" not in str(ringing[2])
    assert accepted == after and reloaded[0] == 200 and 'data-task-final="completed"' in str(reloaded[2])
    assert all(item[0] in {200, 400, 401, 403, 404, 409} for item in denied) and all("data-task-final" not in str(item[2]) for item in denied)
