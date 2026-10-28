"""Red HTTPS contract for iPhone numeric submission failure presentation."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from test_central_mcp_gateway import _free_port, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _snapshot, _writer_headers
from test_iphone_numeric_input_submission import _accept, _numeric_call, _submit
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers


def _numeric_handler(source: str) -> str:
    match = re.search(
        r"button\.addEventListener\('click',\(\)=>\{(?P<body>.*?)\}\)\);"
        r"(?:const|let|var|document\.querySelectorAll|</script>)",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


@pytest.mark.parametrize(("input_type", "raw"), (("integer", " 12 <unsafe> "), ("float", " 3.50 <unsafe> ")))
def test_tls_accepted_iphone_numeric_failure_handler_keeps_raw_local_input_and_renders_truthful_status(
    tmp_path: Path, input_type: str, raw: str,
) -> None:
    """The exact numeric submit handler, not an unrelated page branch, owns failure UI."""
    port = _free_port()
    config, cert = _write_operator_config(tmp_path / input_type, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, cert, f"numeric-failure-ui-{input_type}", input_type)
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert)
        owner = _headers(cookie, csrf, port)
        assert _accept(port, cert, call_id, owner)[0] == 200
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert raw not in source  # typed raw input must remain browser-local, never interpolated into HTML.
    assert 'data-input-type="' + input_type + '"' in source
    assert 'data-numeric-status' in source
    handler = _numeric_handler(source)
    assert "const user_input=input.value;" in handler
    assert "if(response.ok)location.reload();" in handler
    failed = re.search(r"if\(response\.ok\)\{.*?\}else\{(?P<body>.*?)\}", handler, re.DOTALL)
    assert failed is not None
    body = failed.group("body")
    assert any(word in body for word in ("not-submitted", "conflict", "unavailable"))
    assert "input.value=" not in body


def test_tls_numeric_non_ok_submission_and_reads_leave_authoritative_state_unchanged(tmp_path: Path) -> None:
    port = _free_port()
    config, cert = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, cert, "numeric-failure-nonmutation", "integer")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert)
        owner = _headers(cookie, csrf, port)
        assert _accept(port, cert, call_id, owner)[0] == 200
        writer = _writer_headers(port, cert, call_id, owner, "numeric-failure-nonmutation")
        before = _snapshot(db, call_id)
        malformed = _submit(port, cert, call_id, "numeric-malformed", "not a number", writer)
        stale_or_replay = _submit(port, cert, call_id, "numeric-malformed", "not a number", writer)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        api = _request(port, cert, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(db, call_id)
        with sqlite3.connect(db) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
                ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        settled = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        final = _snapshot(db, call_id)
    finally:
        _stop(process)

    assert malformed[0] == stale_or_replay[0] == 400
    assert before == after
    assert page[0] == api[0] == 200
    assert final[0] == "disconnected"
    assert final[1] == before[1] + 1 and final[2:5] == before[2:5]
    assert final[5] == before[5] + 1 and final[6:] == before[6:]
    assert settled[0] == 200


def test_tls_numeric_page_failure_boundaries_are_nonwritable_and_nonmutating(tmp_path: Path) -> None:
    port = _free_port()
    config, cert = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    db = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _numeric_call(port, cert, "numeric-failure-boundary", "float")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, cert)
        other_cookie, other_csrf = _login(port, cert)
        owner = _headers(owner_cookie, owner_csrf, port)
        ringing_before = _snapshot(db, call_id)
        ringing = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie})
        ringing_after = _snapshot(db, call_id)
        assert _accept(port, cert, call_id, owner)[0] == 200
        writer = _writer_headers(port, cert, call_id, owner, "numeric-failure-boundary")
        accepted = _snapshot(db, call_id)
        denied = (
            _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie}),
            _submit(port, cert, call_id, "other", "2.5", _headers(other_cookie, other_csrf, port)),
            _submit(port, cert, call_id, "csrf", "2.5", {**writer, "X-CSRF-Token": "bad"}),
            _submit(port, cert, call_id, "origin", "2.5", {**writer, "Origin": "https://evil.example"}),
            _submit(port, cert, call_id, "host", "2.5", {**writer, "Host": "evil.example"}),
            _request(port, cert, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer terminal-secret"}),
        )
        after = _snapshot(db, call_id)
    finally:
        _stop(process)

    assert ringing_before[0] == "ringing" and ringing_after == ringing_before
    assert ringing[0] == 200 and "data-numeric-input" not in str(ringing[2])
    assert after == accepted
    assert all(item[0] in {200, 400, 401, 403, 404, 409} for item in denied)
    # Authenticated account reads can accurately render status, but never obtain the numeric writer form.
    assert all("data-numeric-input" not in str(item[2]) for item in denied)
