"""Red TLS contracts for one accepted terminal attachment upload.

This slice intentionally specifies one small multipart entity only.  It does
not add upload retries, quotas, task reports, result reads, browser UI, or any
other attachment workflow.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import sqlite3
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _call, _free_port, _post, _start, _stop, _structured
from test_terminal_control_api import (
    _create_ringing_call,
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


def _accept(port: int, certificate: Path, call_id: str, owner_session_id: str) -> None:
    status, response = _terminal_request(
        port,
        certificate,
        "POST",
        f"/api/calls/{call_id}/accept",
        "desktop-token",
        {"action_id": f"accept-{owner_session_id}", "owner_session_id": owner_session_id},
    )
    assert status == 200
    assert response["status"] == "opened"


def _multipart_attachment(
    owner_session_id: str,
    *,
    filename: str = "evidence.txt",
    media_type: str = "text/plain",
    content: bytes = b"attachment evidence\n",
) -> tuple[bytes, str]:
    """Build the one-file wire representation required by this Red slice."""
    boundary = "----hitl-attachment-boundary"
    body = b"".join(
        (
            f"--{boundary}\r\n".encode(),
            b'Content-Disposition: form-data; name="owner_session_id"\r\n\r\n',
            owner_session_id.encode(),
            b"\r\n",
            f"--{boundary}\r\n".encode(),
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
            f"Content-Type: {media_type}\r\n\r\n".encode(),
            content,
            b"\r\n",
            f"--{boundary}--\r\n".encode(),
        )
    )
    return body, f"multipart/form-data; boundary={boundary}"


def _upload(
    port: int,
    certificate: Path,
    call_id: str,
    token: str,
    owner_session_id: str,
    *,
    filename: str = "evidence.txt",
    media_type: str = "text/plain",
    content: bytes = b"attachment evidence\n",
) -> tuple[int, dict[str, Any]]:
    body, content_type = _multipart_attachment(
        owner_session_id, filename=filename, media_type=media_type, content=content
    )
    context = ssl.create_default_context(cafile=str(certificate))
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=context, timeout=2)
    connection.request(
        "POST",
        f"/api/calls/{call_id}/attachments",
        body=body,
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
            "Content-Type": content_type,
        },
    )
    response = connection.getresponse()
    raw = response.read()
    status = response.status
    connection.close()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {"_non_json": raw.decode("utf-8", errors="replace")}


def _attachment_rows(database: Path, call_id: str) -> list[dict[str, Any]]:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'attachments'"
        ).fetchone()
        if table is None:
            # Before the upload feature exists, lack of an archive table is
            # equivalent to no possible archive mutation for denial controls.
            return []
        rows = connection.execute(
            "SELECT * FROM attachments WHERE call_id = ? ORDER BY attachment_id", (call_id,)
        ).fetchall()
    return [dict(row) for row in rows]


def _event_states(database: Path, call_id: str) -> list[tuple[int, str]]:
    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT event_seq, result FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
        ).fetchall()
    return [(event_seq, json.loads(result)["status"]) for event_seq, result in rows]


def _attachment_files(data_dir: Path) -> list[Path]:
    attachment_dir = data_dir / "attachments"
    return list(attachment_dir.rglob("*")) if attachment_dir.exists() else []


def test_tls_owner_attachment_upload_persists_metadata_bytes_and_keeps_call_busy(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    content = b"attachment evidence\n"
    digest = hashlib.sha256(content).hexdigest()
    try:
        created = _create_ringing_call(port, certificate, "attachment-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "attachment-owner")
        status, uploaded = _upload(
            port, certificate, call_id, "desktop-token", "attachment-owner", content=content
        )
        assert status == 200
        attachment = uploaded["attachment"]
        assert uploaded["success"] is True
        assert uploaded["status"] == "uploaded"
        assert attachment == {
            "attachment_id": attachment["attachment_id"],
            "name": "evidence.txt",
            "media_type": "text/plain",
            "byte_count": len(content),
            "sha256": digest,
        }
        assert isinstance(attachment["attachment_id"], str) and attachment["attachment_id"]

        database = tmp_path / "data" / "calls.sqlite3"
        rows = _attachment_rows(database, call_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["attachment_id"] == attachment["attachment_id"]
        assert row["original_name"] == "evidence.txt"
        assert row["media_type"] == "text/plain"
        assert row["byte_count"] == len(content)
        assert row["sha256"] == digest
        stored_path = Path(row["storage_path"])
        assert stored_path.resolve().is_relative_to((tmp_path / "data").resolve())
        assert stored_path.read_bytes() == content
        assert hashlib.sha256(stored_path.read_bytes()).hexdigest() == digest

        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        assert state == ("accepted",)
        assert _event_states(database, call_id) == [(1, "opened")]

        busy_status, busy_response, _ = _post(
            port, certificate, _call("attachment-busy"), "test-token-alpha"
        )
        assert busy_status == 200
        assert _structured(busy_response)["status"] == "busy"
    finally:
        _stop(process)


def test_tls_attachment_metadata_and_generated_bytes_survive_service_restart(tmp_path: Path) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    content = b"restart persistence\n"
    try:
        created = _create_ringing_call(port, certificate, "attachment-restart-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "restart-owner")
        status, uploaded = _upload(
            port,
            certificate,
            call_id,
            "desktop-token",
            "restart-owner",
            filename="restart.txt",
            content=content,
        )
        assert status == 200
    finally:
        _stop(process)

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        database = tmp_path / "data" / "calls.sqlite3"
        rows = _attachment_rows(database, call_id)
        assert len(rows) == 1
        row = rows[0]
        assert row["attachment_id"] == uploaded["attachment"]["attachment_id"]
        assert row["original_name"] == "restart.txt"
        assert Path(row["storage_path"]).read_bytes() == content
        assert row["sha256"] == hashlib.sha256(content).hexdigest()
    finally:
        _stop(process)


def test_tls_attachment_denials_are_uniform_and_leave_no_archive_mutation(tmp_path: Path) -> None:
    port = _free_port()
    # Both known desktop credentials are eligible here.  The other credential
    # is therefore a routed terminal but still not this call's accepted owner.
    config, certificate = _write_terminal_gateway_config(
        tmp_path, port, eligible_desktop_device_ids=["desktop-registered", "desktop-other"]
    )
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "attachment-denial-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "denial-owner")
        wrong_session = _upload(
            port, certificate, call_id, "desktop-token", "other-owner"
        )
        wrong_device = _upload(
            port, certificate, call_id, "other-desktop-token", "denial-owner"
        )
        unknown_call = _upload(
            port, certificate, "missing-call", "desktop-token", "denial-owner"
        )

        # The default route excludes desktop-other, exercising a non-routed
        # device independently of the wrong-owner check above.
        nonrouted_config, nonrouted_certificate = _write_terminal_gateway_config(
            tmp_path / "nonrouted", _free_port()
        )
        nonrouted_process = _start(nonrouted_config)
        _wait_for_tls_gateway(
            nonrouted_process,
            int(json.loads(nonrouted_config.read_text(encoding="utf-8"))["listen_port"]),
            nonrouted_certificate,
        )
        try:
            nonrouted_created = _create_ringing_call(
                int(json.loads(nonrouted_config.read_text(encoding="utf-8"))["listen_port"]),
                nonrouted_certificate,
                "attachment-nonrouted-create",
            )
            nonrouted_call_id = nonrouted_created["call_id"]
            _accept(
                int(json.loads(nonrouted_config.read_text(encoding="utf-8"))["listen_port"]),
                nonrouted_certificate,
                nonrouted_call_id,
                "nonrouted-owner",
            )
            nonrouted = _upload(
                int(json.loads(nonrouted_config.read_text(encoding="utf-8"))["listen_port"]),
                nonrouted_certificate,
                nonrouted_call_id,
                "other-desktop-token",
                "nonrouted-owner",
            )
            nonrouted_database = tmp_path / "nonrouted" / "data" / "calls.sqlite3"
            assert _attachment_rows(nonrouted_database, nonrouted_call_id) == []
            assert _attachment_files(tmp_path / "nonrouted" / "data") == []
        finally:
            _stop(nonrouted_process)

        database = tmp_path / "data" / "calls.sqlite3"
        assert _attachment_rows(database, call_id) == []
        assert _attachment_files(tmp_path / "data") == []
        with sqlite3.connect(database) as connection:
            state = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        assert state == ("accepted",)
        assert _event_states(database, call_id) == [(1, "opened")]
    finally:
        _stop(process)

    # While the endpoint is absent, these are endpoint-absence controls only;
    # they become authorization/non-leakage evidence once upload exists.
    assert wrong_session == wrong_device == unknown_call == nonrouted == (404, {"error": "not found"})


def test_expired_accepted_attachment_settles_before_upload_and_preserves_draft(
    tmp_path: Path,
) -> None:
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        created = _create_ringing_call(port, certificate, "attachment-expiry-create")
        call_id = created["call_id"]
        _accept(port, certificate, call_id, "expiry-owner")
        saved = _terminal_request(
            port,
            certificate,
            "PUT",
            f"/api/calls/{call_id}/draft",
            "desktop-token",
            {
                "owner_session_id": "expiry-owner",
                "body": "keep this draft",
                "expected_draft_version": 0,
            },
        )
        assert saved == (
            200,
            {
                "success": True,
                "status": "saved",
                "draft": {"body": "keep this draft", "draft_version": 1},
            },
        )
    finally:
        _stop(process)

    database = tmp_path / "data" / "calls.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute(
            "UPDATE calls SET terminal_disconnect_deadline = ? WHERE call_id = ?",
            ((datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id),
        )

    process = _start(config)
    _wait_for_tls_gateway(process, port, certificate)
    try:
        late = _upload(port, certificate, call_id, "desktop-token", "expiry-owner")
        assert late == (
            409,
            {"success": False, "status": "disconnected", "reason": "terminal_disconnected"},
        )
        with sqlite3.connect(database) as connection:
            call = connection.execute(
                "SELECT state, draft_body, draft_version FROM calls WHERE call_id = ?", (call_id,)
            ).fetchone()
        assert _attachment_rows(database, call_id) == []
        assert _attachment_files(tmp_path / "data") == []
        assert _event_states(database, call_id) == [(1, "opened"), (2, "disconnected")]
    finally:
        _stop(process)

    assert call == ("disconnected", "keep this draft", 1)
