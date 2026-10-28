"""Red HTTPS contract for iPhone information-acknowledgement failure presentation."""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _wait_for_listener
from test_iphone_browser_drafts import _snapshot, _writer_headers
from test_iphone_info_submission import _accept, _call, _submit
from test_iphone_web_acceptance import _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_mcp_continuation import _continuation


def _ack_handler(source: str) -> str:
    match = re.search(
        r"button\.addEventListener\('click',\(\)=>\{(?P<body>.*?)\}\)\);"
        r"(?:const|let|var|document\.querySelectorAll|</script>)",
        source,
        re.DOTALL,
    )
    assert match is not None
    return match.group("body")


def test_tls_accepted_iphone_info_ack_failure_handler_renders_truthful_nonfinal_status(tmp_path: Path) -> None:
    """The acknowledgement handler itself must explain a rejected POST without finalizing it."""
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-failure-ui")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        cookie, csrf = _login(port, certificate)
        owner = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'data-acknowledged="true"' in source
    assert 'data-acknowledgement-status' in source
    handler = _ack_handler(source)
    assert "const acknowledged=button.dataset.acknowledged==='true';" in handler
    assert "if(response.ok)location.reload();" in handler
    failed = re.search(r"if\(response\.ok\)\{.*?\}else\{(?P<body>.*?)\}", handler, re.DOTALL)
    assert failed is not None
    assert any(word in failed.group("body") for word in ("not-submitted", "conflict", "unavailable"))


def test_tls_info_non_ok_replay_reads_and_disconnect_settlement_preserve_authoritative_state(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-failure-nonmutation")
        call_id, interaction_id = created["call_id"], created["interaction_id"]
        assert isinstance(call_id, str) and isinstance(interaction_id, str)
        cookie, csrf = _login(port, certificate)
        owner = _headers(cookie, csrf, port)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        writer = _writer_headers(port, certificate, call_id, owner, "info-failure-nonmutation")
        before = _snapshot(database, call_id)
        malformed = _submit(port, certificate, call_id, "bad", "true", writer)
        replay = _submit(port, certificate, call_id, "bad", "true", writer)
        page = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        api = _request(port, certificate, "GET", f"/api/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(database, call_id)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "UPDATE calls SET terminal_last_seen_at=?, terminal_disconnect_deadline=? WHERE call_id=?",
                ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
            )
            connection.commit()
        settled = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        late = _submit(port, certificate, call_id, "late", True, writer)
        _, event, _ = _post(port, certificate, _continuation("info-failure-event", interaction_id, call_id, 1), "test-token-alpha")
        final = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert malformed[0] == replay[0] == 400
    assert before == after
    assert page[0] == api[0] == settled[0] == 200
    assert late[0] == 409 and final[0] == "disconnected" and final[1] == before[1] + 1
    assert final[2:5] == before[2:5] and final[5] == before[5] + 1 and final[6:] == before[6:]
    assert event is not None


def test_tls_info_failure_page_boundaries_are_readonly_and_nonmutating(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_operator_config(tmp_path, port)
    _configure_phone_route(config)
    database = tmp_path / "data" / "calls.sqlite3"
    process = _start(config)
    _wait_for_listener(process, port)
    try:
        created = _call(port, certificate, "info-failure-boundary")
        call_id = created["call_id"]
        assert isinstance(call_id, str)
        owner_cookie, owner_csrf = _login(port, certificate)
        other_cookie, other_csrf = _login(port, certificate)
        owner = _headers(owner_cookie, owner_csrf, port)
        ringing_before = _snapshot(database, call_id)
        ringing = _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": owner_cookie})
        ringing_after = _snapshot(database, call_id)
        assert _accept(port, certificate, call_id, owner)[0] == 200
        accepted = _snapshot(database, call_id)
        denied = (
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Cookie": other_cookie}),
            _submit(port, certificate, call_id, "other", True, _headers(other_cookie, other_csrf, port)),
            _submit(port, certificate, call_id, "csrf", True, {**owner, "X-CSRF-Token": "bad"}),
            _submit(port, certificate, call_id, "origin", True, {**owner, "Origin": "https://evil.example"}),
            _submit(port, certificate, call_id, "host", True, {**owner, "Host": "evil.example"}),
            _request(port, certificate, "GET", f"/calls/{call_id}", headers={"Authorization": "Bearer terminal-secret"}),
        )
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert ringing_before == ringing_after and ringing_before[0] == "ringing"
    assert ringing[0] == 200 and "data-acknowledged" not in str(ringing[2])
    assert after == accepted
    assert all(item[0] in {200, 400, 401, 403, 404, 409} for item in denied)
    assert all("data-acknowledged" not in str(item[2]) for item in denied)
