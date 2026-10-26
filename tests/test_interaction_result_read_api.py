"""Red contracts for authorized, budgeted reads of persisted terminal results.

The tests use the central service's real TLS listener and SQLite archive.  They
intentionally cover reading only: terminal acceptance, upload, and submission
are fixture setup for a durable result, not new workflow coverage.
"""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _free_port, _post, _rpc, _start, _stop, _structured
from test_terminal_attachment_upload_api import _accept, _attachment_files, _attachment_rows, _upload
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


_MIN_RESULT_BUDGET = 16_384


def _tool_call(
    port: int,
    certificate: Path,
    token: str,
    name: str,
    arguments: dict[str, object],
) -> tuple[int, dict[str, Any]]:
    status, response, _ = _post(
        port,
        certificate,
        _rpc("tools/call", {"name": name, "arguments": arguments}, 71),
        token,
    )
    return status, response


def _read_result(
    port: int,
    certificate: Path,
    token: str,
    interaction_id: str,
    result_id: str,
    cursor: int | None,
    budget: int,
) -> dict[str, Any]:
    arguments: dict[str, object] = {
        "interaction_id": interaction_id,
        "result_id": result_id,
        "max_result_bytes": budget,
    }
    if cursor is not None:
        arguments["cursor"] = cursor
    status, response = _tool_call(
        port, certificate, token, "read_interaction_result", arguments
    )
    assert status == 200
    return _structured(response)


def _read_attachment(
    port: int,
    certificate: Path,
    token: str,
    interaction_id: str,
    attachment_id: str,
    offset: int | None,
    budget: int,
) -> dict[str, Any]:
    arguments: dict[str, object] = {
        "interaction_id": interaction_id,
        "attachment_id": attachment_id,
        "max_result_bytes": budget,
    }
    if offset is not None:
        arguments["offset_bytes"] = offset
    status, response = _tool_call(
        port, certificate, token, "read_interaction_attachment", arguments
    )
    assert status == 200
    return _structured(response)


def _persisted_snapshot(data_dir: Path, call_id: str) -> tuple[object, ...]:
    """Take an independent SQLite/filesystem snapshot before a read."""
    database = data_dir / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        call = connection.execute(
            "SELECT state, version, task_version FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()
        events = connection.execute(
            "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
        ).fetchall()
        operations = connection.execute(
            "SELECT submission_id, payload, result FROM submission_operations "
            "WHERE call_id = ? ORDER BY submission_id",
            (call_id,),
        ).fetchall()
    attachments = _attachment_rows(database, call_id)
    files = sorted(
        (path.relative_to(data_dir).as_posix(), path.read_bytes())
        for path in _attachment_files(data_dir)
        if path.is_file()
    )
    return call, events, operations, attachments, files


def _completed_fixture(
    tmp_path: Path,
    *,
    request_id: str,
    owner_session_id: str,
    body: str,
    attachment: bytes,
) -> tuple[int, Path, Any, dict[str, Any], dict[str, Any]]:
    """Start one service and persist a terminal answer plus one archive entry."""
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    created = _create_ringing_call(port, certificate, request_id)
    call_id = created["call_id"]
    _accept(port, certificate, call_id, owner_session_id)
    uploaded_status, uploaded = _upload(
        port, certificate, call_id, "desktop-token", owner_session_id, content=attachment
    )
    assert uploaded_status == 200
    attachment_id = uploaded["attachment"]["attachment_id"]
    submitted_status, submitted = _terminal_request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/submissions",
        "desktop-token",
        {
            "owner_session_id": owner_session_id,
            "submission_id": f"submit-{request_id}",
            "task_version": 0,
            "attachment_ids": [attachment_id],
            "user_input": body,
        },
    )
    assert submitted_status == 200
    assert submitted["status"] == "answered"
    return port, certificate, process, created, {"submitted": submitted, "uploaded": uploaded}


def test_tls_persisted_terminal_answer_fixture_control_is_real_and_durable(tmp_path: Path) -> None:
    """Passing control: the read contract starts from a real persisted result."""
    port, certificate, process, created, fixture = _completed_fixture(
        tmp_path,
        request_id="result-read-control",
        owner_session_id="reader-owner",
        body="control answer",
        attachment=b"control attachment",
    )
    try:
        database = tmp_path / "data" / "calls.sqlite3"
        assert fixture["submitted"]["user_input"] == "control answer"
        assert fixture["submitted"]["attachment_ids"] == [
            fixture["uploaded"]["attachment"]["attachment_id"]
        ]
        assert _persisted_snapshot(tmp_path / "data", created["call_id"])[0] == (
            "answered",
            3,
            0,
        )
        assert _attachment_rows(database, created["call_id"])[0]["byte_count"] == len(
            b"control attachment"
        )
    finally:
        _stop(process)


def test_tls_result_reader_pages_unicode_with_budget_and_keeps_archive_immutable(tmp_path: Path) -> None:
    body = "汉😀é" * 7_000
    attachment = (b"\x00\xffresult-attachment\n" * 1_300) + b"tail"
    port, certificate, process, created, fixture = _completed_fixture(
        tmp_path,
        request_id="result-read-pages",
        owner_session_id="reader-pages-owner",
        body=body,
        attachment=attachment,
    )
    try:
        submitted = fixture["submitted"]
        uploaded = fixture["uploaded"]
        # A final result must expose its stable ID so an authenticated caller can read it.
        result_id = submitted["result_id"]
        before = _persisted_snapshot(tmp_path / "data", created["call_id"])

        cursor: int | None = None
        parts: list[str] = []
        while True:
            page = _read_result(
                port,
                certificate,
                "test-token-alpha",
                created["interaction_id"],
                result_id,
                cursor,
                _MIN_RESULT_BUDGET,
            )
            assert page["success"] is True
            assert page["result_id"] == result_id
            assert page["full_length"] == len(body)
            assert isinstance(page["body"], str)
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= _MIN_RESULT_BUDGET
            parts.append(page["body"])
            if page["complete"]:
                assert page["next_cursor"] is None
                break
            assert isinstance(page["next_cursor"], int)
            assert page["next_cursor"] > (cursor or 0)
            cursor = page["next_cursor"]

        assert "".join(parts) == body  # Joined pages prove no Unicode character was split.
        assert page["attachments"] == [
            {
                "attachment_id": uploaded["attachment"]["attachment_id"],
                "name": "evidence.txt",
                "media_type": "text/plain",
                "byte_count": len(attachment),
                "sha256": hashlib.sha256(attachment).hexdigest(),
            }
        ]
        assert _persisted_snapshot(tmp_path / "data", created["call_id"]) == before
    finally:
        _stop(process)


def test_tls_attachment_reader_offsets_authorization_and_budget_are_nonleaking(tmp_path: Path) -> None:
    attachment = (b"\x00\x01\xffbinary\n" * 2_000) + b"end"
    port, certificate, process, created, fixture = _completed_fixture(
        tmp_path,
        request_id="attachment-read-pages",
        owner_session_id="attachment-reader-owner",
        body="attachment read answer",
        attachment=attachment,
    )
    try:
        submitted = fixture["submitted"]
        attachment_id = fixture["uploaded"]["attachment"]["attachment_id"]
        before = _persisted_snapshot(tmp_path / "data", created["call_id"])

        offset: int | None = None
        chunks: list[bytes] = []
        while True:
            page = _read_attachment(
                port,
                certificate,
                "test-token-alpha",
                created["interaction_id"],
                attachment_id,
                offset,
                _MIN_RESULT_BUDGET,
            )
            assert page["success"] is True
            assert page["attachment_id"] == attachment_id
            assert page["full_length"] == len(attachment)
            assert page["sha256"] == hashlib.sha256(attachment).hexdigest()
            assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) <= _MIN_RESULT_BUDGET
            chunks.append(base64.b64decode(page["data_base64"], validate=True))
            if page["complete"]:
                assert page["next_offset_bytes"] is None
                break
            assert isinstance(page["next_offset_bytes"], int)
            assert page["next_offset_bytes"] > (offset or 0)
            offset = page["next_offset_bytes"]

        assert b"".join(chunks) == attachment
        assert _persisted_snapshot(tmp_path / "data", created["call_id"]) == before

        small_result = _read_result(
            port, certificate, "test-token-alpha", created["interaction_id"], submitted["result_id"], None, _MIN_RESULT_BUDGET - 1
        )
        small_attachment = _read_attachment(
            port, certificate, "test-token-alpha", created["interaction_id"], attachment_id, None, _MIN_RESULT_BUDGET - 1
        )
        expected_budget_failure = {
            "success": False,
            "status": "invalid_result_budget",
            "needs_continuation": False,
        }
        assert small_result == expected_budget_failure
        assert small_attachment == expected_budget_failure

        foreign_status, foreign = _tool_call(
            port,
            certificate,
            "test-token-beta",
            "read_interaction_result",
            {"interaction_id": created["interaction_id"], "result_id": submitted["result_id"], "max_result_bytes": _MIN_RESULT_BUDGET},
        )
        unknown_status, unknown = _tool_call(
            port,
            certificate,
            "test-token-alpha",
            "read_interaction_result",
            {"interaction_id": "not-a-visible-interaction", "result_id": "not-a-visible-result", "max_result_bytes": _MIN_RESULT_BUDGET},
        )
        assert foreign_status == unknown_status == 200
        assert foreign == unknown
        assert _persisted_snapshot(tmp_path / "data", created["call_id"]) == before
    finally:
        _stop(process)
