"""Red HTTPS contract for an accepted iPhone owner's heartbeat failure UI.

The heartbeat remains a server-authorized liveness write.  These checks only
specify what an already-rendered page must show if that write cannot be
confirmed; they do not alter heartbeat, retry, or deadline semantics.
"""

from __future__ import annotations

from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _writer_headers
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_iphone_web_heartbeat import _business_evidence, _call_row, _heartbeat


def _accepted_owner(tmp_path: Path, request_id: str):
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    created = _create_call(port, certificate, request_id)
    call_id = created["call_id"]
    assert isinstance(call_id, str)
    cookie, csrf = _login(port, certificate)
    headers = _headers(cookie, csrf, port)
    accepted = _request(
        port, certificate, "POST", f"/api/calls/{call_id}/accept",
        body={"action_id": f"{request_id}-accept"}, headers=headers,
    )
    assert accepted[0] == 200
    return port, config, certificate, process, call_id, cookie, headers


def test_tls_owner_heartbeat_page_has_independent_ready_and_failure_status(tmp_path: Path) -> None:
    """A failed liveness write must not overwrite the authoritative call state."""
    port, _, certificate, process, call_id, cookie, _ = _accepted_owner(tmp_path, "heartbeat-failure-ui")
    try:
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'data-heartbeat-status="ready"' in source
    assert "const heartbeatStatus=document.querySelector('[data-heartbeat-status]');" in source
    assert "const showHeartbeatUnavailable=()=>" in source
    assert "heartbeatStatus.setAttribute('data-heartbeat-status','unavailable')" in source
    assert "heartbeatStatus.textContent='unavailable: reload needed'" in source
    assert "try{const response=await fetch(heartbeatEndpoint" in source
    assert "catch(_error){showHeartbeatUnavailable();}" in source
    heartbeat_source = source.split("const heartbeatEndpoint=", 1)[1].split("const authoritativeStateEndpoint=", 1)[0]
    assert "location.reload()" not in heartbeat_source
    assert "document.querySelector('[data-state]').textContent" not in heartbeat_source
    assert "Connection lost." not in heartbeat_source


def test_tls_owner_heartbeat_keeps_server_authority_and_denials_nonmutating(tmp_path: Path) -> None:
    port, _, certificate, process, call_id, cookie, headers = _accepted_owner(tmp_path, "heartbeat-failure-auth")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        writer = _writer_headers(port, certificate, call_id, headers, "heartbeat-current")
        before_valid = _call_row(database, call_id)
        valid = _heartbeat(port, certificate, call_id, writer)
        after_valid = _call_row(database, call_id)
        evidence_after_valid = _business_evidence(database, call_id)

        other_cookie, other_csrf = _login(port, certificate)
        before_denials = _call_row(database, call_id)
        denied = (
            _heartbeat(port, certificate, call_id, {}),
            _heartbeat(port, certificate, call_id, headers),
            _heartbeat(port, certificate, call_id, {**writer, "Origin": "https://evil.invalid"}),
            _heartbeat(port, certificate, call_id, {**writer, "Host": "evil.invalid"}),
            _heartbeat(port, certificate, call_id, _headers(other_cookie, other_csrf, port)),
        )
        after_denials = _call_row(database, call_id)
        evidence_after_denials = _business_evidence(database, call_id)
    finally:
        _stop(process)

    assert valid[0] == 200 and isinstance(valid[2], dict)
    assert valid[2]["status"] == valid[2]["call_state"] == "accepted"
    assert after_valid[:3] == before_valid[:3]
    assert after_valid[3] != before_valid[3] and after_valid[4] != before_valid[4]
    assert all(status in {400, 401, 403, 404, 409} for status, _, _ in denied)
    assert after_denials == before_denials
    assert evidence_after_denials == evidence_after_valid


def test_tls_heartbeat_page_reads_reload_and_restart_do_not_take_over_or_mutate(tmp_path: Path) -> None:
    port, config, certificate, process, call_id, cookie, headers = _accepted_owner(tmp_path, "heartbeat-failure-reads")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        _writer_headers(port, certificate, call_id, headers, "heartbeat-read-owner")
        before = _call_row(database, call_id)
        reads = (
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie}),
            _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie}),
            _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie, "Host": "forged.invalid"}),
        )
        _stop(process)
        process = _start(config)
        _wait_for_listener(process, port)
        reloaded = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _call_row(database, call_id)
    finally:
        _stop(process)

    assert reads[0][0] == reads[1][0] == 200
    assert reads[2][0] in {400, 401, 403}
    assert reloaded[0] == 200
    assert before == after
