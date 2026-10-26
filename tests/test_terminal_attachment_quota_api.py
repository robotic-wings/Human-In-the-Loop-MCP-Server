"""Red TLS contracts for single-attachment capacity limits.

Only the one-file multipart route is exercised here.  The tests do not add a
multi-file workflow, retry semantics, result reads, or other attachment APIs.
"""

from __future__ import annotations

import json
import ssl
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from test_central_mcp_gateway import _free_port, _start, _stop
from test_terminal_attachment_upload_api import (
    _accept,
    _attachment_files,
    _attachment_rows,
    _event_states,
    _upload,
)
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


MIB = 1024 * 1024


def _quota_gateway_config(
    directory: Path,
    port: int,
    *,
    file_limit: int | None = None,
    submission_limit: int | None = None,
    archive_limit: int | None = None,
) -> tuple[Path, Path]:
    config, certificate = _write_terminal_gateway_config(directory, port)
    raw = json.loads(config.read_text(encoding="utf-8"))
    if file_limit is not None:
        raw["attachment_max_file_bytes"] = file_limit
    if submission_limit is not None:
        raw["attachment_max_submission_bytes"] = submission_limit
    if archive_limit is not None:
        raw["attachment_max_archive_bytes"] = archive_limit
    config.write_text(json.dumps(raw), encoding="utf-8")
    return config, certificate


def _accepted_call(port: int, certificate: Path, request_id: str, owner_session_id: str) -> str:
    created = _create_ringing_call(port, certificate, request_id)
    _accept(port, certificate, created["call_id"], owner_session_id)
    return created["call_id"]


def _assert_unmodified_accepted_call(directory: Path, call_id: str) -> None:
    database = directory / "data" / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
    assert state == ("accepted",)
    assert _attachment_rows(database, call_id) == []
    assert _attachment_files(directory / "data") == []
    assert _event_states(database, call_id) == [(1, "opened")]


def _upload_observing_current_parser_cutoff(*args: object, **kwargs: object) -> object:
    """Keep the required HTTP assertion meaningful while documenting old parser behavior."""
    try:
        return _upload(*args, **kwargs)
    except ssl.SSLEOFError as error:
        return ("connection_eof", type(error).__name__)


def test_default_file_limit_rejects_twenty_mib_plus_one_before_any_mutation(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _quota_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        call_id = _accepted_call(port, certificate, "quota-default-create", "default-owner")
        oversized = _upload_observing_current_parser_cutoff(
            port,
            certificate,
            call_id,
            "desktop-token",
            "default-owner",
            content=b"x" * (20 * MIB + 1),
        )
        assert oversized == (
            413,
            {"success": False, "status": "attachment_rejected", "reason": "file_limit_exceeded"},
        )
        _assert_unmodified_accepted_call(tmp_path, call_id)
    finally:
        _stop(process)


def test_configured_small_file_limit_is_honored_before_any_attachment_write(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _quota_gateway_config(tmp_path, port, file_limit=4, submission_limit=64)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        call_id = _accepted_call(port, certificate, "quota-file-create", "file-owner")
        oversized = _upload(
            port, certificate, call_id, "desktop-token", "file-owner", content=b"five!"
        )
        assert oversized == (
            413,
            {"success": False, "status": "attachment_rejected", "reason": "file_limit_exceeded"},
        )
        _assert_unmodified_accepted_call(tmp_path, call_id)
    finally:
        _stop(process)


def test_configured_submission_limit_is_independent_of_file_limit(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _quota_gateway_config(tmp_path, port, file_limit=64, submission_limit=4)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        call_id = _accepted_call(port, certificate, "quota-submission-create", "submission-owner")
        oversized = _upload(
            port, certificate, call_id, "desktop-token", "submission-owner", content=b"five!"
        )
        assert oversized == (
            413,
            {
                "success": False,
                "status": "attachment_rejected",
                "reason": "submission_limit_exceeded",
            },
        )
        _assert_unmodified_accepted_call(tmp_path, call_id)
    finally:
        _stop(process)


def test_configured_archive_limit_preserves_existing_archive_and_rejects_new_file(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _quota_gateway_config(
        tmp_path, port, file_limit=64, submission_limit=64, archive_limit=8
    )
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    existing_bytes = b"six!!!"
    try:
        call_id = _accepted_call(port, certificate, "quota-archive-create", "archive-owner")
        database = tmp_path / "data" / "calls.sqlite3"
        attachment_id = str(uuid.uuid4())
        archive_path = tmp_path / "data" / "attachments" / attachment_id
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        archive_path.write_bytes(existing_bytes)
        with sqlite3.connect(database) as connection:
            connection.execute(
                "INSERT INTO attachments "
                "(attachment_id, call_id, original_name, media_type, byte_count, sha256, storage_path) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (attachment_id, call_id, "prior.txt", "text/plain", len(existing_bytes), "prior", str(archive_path)),
            )

        rejected = _upload(
            port, certificate, call_id, "desktop-token", "archive-owner", content=b"three"
        )
        assert rejected == (
            413,
            {
                "success": False,
                "status": "attachment_rejected",
                "reason": "archive_capacity_exceeded",
            },
        )
        rows = _attachment_rows(database, call_id)
        assert len(rows) == 1
        assert rows[0]["attachment_id"] == attachment_id
        assert archive_path.read_bytes() == existing_bytes
        assert _attachment_files(tmp_path / "data") == [archive_path]
        with sqlite3.connect(database) as connection:
            state = connection.execute("SELECT state FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        assert state == ("accepted",)
        assert _event_states(database, call_id) == [(1, "opened")]
    finally:
        _stop(process)


def test_authorization_and_expiry_precede_quota_rejection_and_preserve_draft(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _quota_gateway_config(tmp_path, port, file_limit=4, submission_limit=4)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    payload = b"oversized"
    try:
        call_id = _accepted_call(port, certificate, "quota-priority-create", "priority-owner")
        wrong_session = _upload(
            port, certificate, call_id, "desktop-token", "wrong-owner", content=payload
        )
        wrong_device = _upload(
            port, certificate, call_id, "other-desktop-token", "priority-owner", content=payload
        )
        wrong_route = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/attachment-typo",
            "desktop-token",
        )
        assert wrong_session == wrong_device == wrong_route == (404, {"error": "not found"})
        _assert_unmodified_accepted_call(tmp_path, call_id)

        ended = _terminal_request(
            port,
            certificate,
            "POST",
            f"/api/calls/{call_id}/cancel",
            "desktop-token",
            {"action_id": "quota-priority-end", "owner_session_id": "priority-owner"},
        )
        assert ended[0] == 200

        expired_call_id = _accepted_call(port, certificate, "quota-expiry-create", "expiry-owner")
        saved = _terminal_request(
            port,
            certificate,
            "PUT",
            f"/api/calls/{expired_call_id}/draft",
            "desktop-token",
            {
                "owner_session_id": "expiry-owner",
                "body": "keep this draft",
                "expected_draft_version": 0,
            },
        )
        assert saved[0] == 200
    finally:
        _stop(process)

    database = tmp_path / "data" / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE calls SET terminal_disconnect_deadline = ? WHERE call_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), expired_call_id),
        )

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        expired = _upload(
            port, certificate, expired_call_id, "desktop-token", "expiry-owner", content=payload
        )
        assert expired == (
            409,
            {"success": False, "status": "disconnected", "reason": "terminal_disconnected"},
        )
        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT state, draft_body, draft_version FROM calls WHERE call_id = ?", (expired_call_id,)
            ).fetchone()
        assert state == ("disconnected", "keep this draft", 1)
        assert _attachment_rows(database, expired_call_id) == []
        assert _attachment_files(tmp_path / "data") == []
        assert _event_states(database, expired_call_id) == [(1, "opened"), (2, "disconnected")]
    finally:
        _stop(process)
