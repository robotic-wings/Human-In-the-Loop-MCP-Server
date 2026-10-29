"""Red HTTPS contract for authenticated iPhone ringing-action failure UI.

This stays page-local: API/coordinator authority, route selection, and the
existing accept/decline settlement contracts remain covered by their own
focused suites.
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_ringing_countdown import _snapshot
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _action_handler(source: str) -> str:
    match = re.search(
        r"document\.querySelectorAll\('button\[data-action\]'\)\.forEach\(\(button\)=>"
        r"button\.addEventListener\('click',async\(\)=>\{(?P<body>.*?)\}\)\);",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def _ringing_page(tmp_path: Path, request_id: str):
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    created = _create_call(port, certificate, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    cookie, csrf = _login(port, certificate)
    return port, certificate, process, call_id, cookie, csrf


def test_tls_ringing_actions_render_ready_and_bound_non_ok_status(tmp_path: Path) -> None:
    port, certificate, process, call_id, cookie, _ = _ringing_page(tmp_path, "ring-action-status")
    try:
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'data-ringing-action-status="ready"' in source
    assert 'data-action="accept"' in source and 'data-action="decline"' in source
    handler = _action_handler(source)
    assert "action_id:crypto.randomUUID()" in handler
    assert "credentials:'same-origin'" in handler and "X-CSRF-Token" in handler
    assert "if(response.ok)location.reload();" in handler
    failed = re.search(r"if\(response\.ok\)\{.*?\}else\{(?P<body>.*?)\}", handler, re.DOTALL)
    assert failed is not None
    failure = failed.group("body")
    assert any(word in failure for word in ("not-submitted", "conflict", "unavailable"))
    assert "location.reload" not in failure and "remove" not in failure
    assert not any(word in failure for word in ("accepted", "declined", "answered", "result"))


def test_tls_ringing_non_ok_actions_are_nonmutating_until_valid_settlement(tmp_path: Path) -> None:
    port, certificate, process, call_id, cookie, csrf = _ringing_page(tmp_path, "ring-action-nonmutation")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _snapshot(database, call_id)
        bad_accept = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "bad-accept"},
            headers={"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}"},
        )
        bad_decline = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/decline", body={"action_id": "bad-decline"},
            headers={**_headers(cookie, csrf, port), "Origin": "https://evil.example"},
        )
        after_bad = _snapshot(database, call_id)
        accepted = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "good-accept"},
            headers=_headers(cookie, csrf, port),
        )
        replay = _request(
            port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "good-accept"},
            headers=_headers(cookie, csrf, port),
        )
        final = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert bad_accept[0] in {400, 403} and bad_decline[0] in {400, 403}
    assert before == after_bad and before[0] == "ringing"
    assert accepted[0] == replay[0] == 200 and accepted[2] == replay[2]
    assert final[0] == "accepted" and final[1] == before[1] + 1
    assert final[4:6] != (None, None)
    assert final[6:] == (before[6] + 1, before[7], before[8], before[9] + 1)


def test_tls_ringing_action_boundaries_settle_expiry_without_takeover_or_read_mutation(tmp_path: Path) -> None:
    port, certificate, process, call_id, cookie, csrf = _ringing_page(tmp_path, "ring-action-boundaries")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _snapshot(database, call_id)
        read = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        reread = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        anonymous = _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "anonymous"})
        bearer = _request(port, certificate, "POST", f"/api/calls/{call_id}/decline", body={"action_id": "bearer"}, headers={"Authorization": "Bearer terminal-secret"})
        after_reads = _snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET ring_deadline=? WHERE call_id=?", ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id))
            connection.commit()
        late_accept = _request(port, certificate, "POST", f"/api/calls/{call_id}/accept", body={"action_id": "late-accept"}, headers=_headers(cookie, csrf, port))
        late_decline = _request(port, certificate, "POST", f"/api/calls/{call_id}/decline", body={"action_id": "late-decline"}, headers=_headers(cookie, csrf, port))
        final = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert read[0] == reread[0] == 200 and before == after_reads
    assert anonymous[0] in {401, 403, 404} and bearer[0] in {401, 403, 404}
    assert late_accept[0] == late_decline[0] == 409
    assert final[0] == "not_accepted" and final[4:6] == (None, None)
