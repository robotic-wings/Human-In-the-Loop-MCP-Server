"""Red HTTPS contracts for one accepted iPhone browser attachment upload.

This deliberately covers archive-only upload.  It neither attaches an upload to
a final browser submission nor introduces a multi-file or retry workflow.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import sqlite3
import ssl
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from test_central_mcp_gateway import _free_port, _post, _start, _stop, _structured, _wait_for_listener
from test_iphone_browser_drafts import _accept, _writer_headers
from test_iphone_web_acceptance import _create_call, _login, _request, _write_operator_config
from test_iphone_web_decline import _configure_phone_route, _headers
from test_terminal_attachment_upload_api import _attachment_files, _attachment_rows


def _multipart(*, filename: str = "phone.txt", media_type: str = "text/plain", content: bytes = b"phone evidence\n", files: int = 1, malformed: bool = False) -> tuple[bytes, str]:
    boundary = "----iphone-attachment-boundary"
    part = b"".join((
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        f"Content-Type: {media_type}\r\n\r\n".encode(), content, b"\r\n",
    ))
    body = part * files + f"--{boundary}--\r\n".encode()
    if malformed:
        body = b"not-a-valid-multipart-body"
    return body, f"multipart/form-data; boundary={boundary}"


def _upload_browser(port: int, cert: Path, call_id: str, headers: dict[str, str], *, filename: str = "phone.txt", media_type: str = "text/plain", content: bytes = b"phone evidence\n", files: int = 1, malformed: bool = False) -> tuple[int, object]:
    body, content_type = _multipart(filename=filename, media_type=media_type, content=content, files=files, malformed=malformed)
    connection = http.client.HTTPSConnection("127.0.0.1", port, context=ssl.create_default_context(cafile=str(cert)), timeout=2)
    connection.request("POST", f"/api/calls/{call_id}/attachments", body=body, headers={**headers, "Accept": "application/json", "Content-Type": content_type})
    response = connection.getresponse(); raw = response.read(); status = response.status; connection.close()
    try:
        return status, json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return status, {"_non_json": raw.decode("utf-8", errors="replace")}


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    with sqlite3.connect(database) as connection:
        call = connection.execute("SELECT state, version, task_version, draft_version, draft_body FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        events = connection.execute("SELECT count(*) FROM events WHERE call_id = ?", (call_id,)).fetchone()[0]
        results = connection.execute("SELECT count(*) FROM results WHERE call_id = ?", (call_id,)).fetchone()[0]
        submissions = connection.execute("SELECT count(*) FROM submission_operations WHERE call_id = ?", (call_id,)).fetchone()[0]
    return (*call, events, results, submissions)


def _accepted_owner(tmp_path: Path, request_id: str, *, file_limit: int | None = None, submission_limit: int | None = None, archive_limit: int | None = None):
    port = _free_port(); config, cert = _write_operator_config(tmp_path, port); _configure_phone_route(config)
    if file_limit is not None or archive_limit is not None:
        raw = json.loads(config.read_text(encoding="utf-8"))
        if file_limit is not None:
            raw["attachment_max_file_bytes"] = file_limit
            raw["attachment_max_submission_bytes"] = max(file_limit, int(raw.get("attachment_max_submission_bytes", 50 * 1024 * 1024)))
        if submission_limit is not None:
            raw["attachment_max_submission_bytes"] = submission_limit
        if archive_limit is not None:
            raw["attachment_max_archive_bytes"] = archive_limit
        config.write_text(json.dumps(raw), encoding="utf-8")
    process = _start(config); _wait_for_listener(process, port)
    created = _create_call(port, cert, request_id); call_id = created["call_id"]; assert isinstance(call_id, str)
    cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
    _accept(port, cert, call_id, owner)
    writer = _writer_headers(port, cert, call_id, owner, f"attachment-{request_id}")
    return port, config, cert, process, call_id, cookie, owner, writer


def _last_seen(database: Path, call_id: str) -> object:
    with sqlite3.connect(database) as connection:
        return connection.execute("SELECT terminal_last_seen_at FROM calls WHERE call_id = ?", (call_id,)).fetchone()[0]


def test_tls_accepted_browser_upload_archives_generated_metadata_and_refreshes_only_owner_liveness(tmp_path: Path) -> None:
    port, config, cert, process, call_id, cookie, owner, writer = _accepted_owner(tmp_path, "browser-upload")
    database = tmp_path / "data" / "calls.sqlite3"; content = b"iPhone attachment bytes\x00\n"
    try:
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        uploaded = _upload_browser(port, cert, call_id, writer, filename="photo.txt", media_type="text/plain", content=content)
        _stop(process); process = _start(config); _wait_for_listener(process, port)
        rows = _attachment_rows(database, call_id)
        after = _snapshot(database, call_id)
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'type="file"' in source and f'/api/calls/{call_id}/attachments' in source
    assert "multipart/form-data" in source and "data-attachment-status" in source
    # The rendered control must actually transmit the selected file under the current
    # lease, and expose a truthful saved/failed state from the HTTP response.
    assert re.search(
        r"new FormData\(.*?file.*?fetch\(.*?attachments.*?X-Writer-Token.*?writerToken.*?"
        r"response\.ok.*?data-attachment-status.*?saved.*?data-attachment-status.*?failed",
        source,
        re.DOTALL,
    )
    assert uploaded[0] == 200 and isinstance(uploaded[1], dict)
    attachment = uploaded[1]["attachment"]
    assert attachment == {"attachment_id": attachment["attachment_id"], "name": "photo.txt", "media_type": "text/plain", "byte_count": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    assert len(rows) == 1 and rows[0]["attachment_id"] == attachment["attachment_id"]
    assert Path(rows[0]["storage_path"]).read_bytes() == content
    assert after[:5] == ("accepted", after[1], after[2], 0, None) and after[5:] == (1, 0, 0)
    assert _attachment_files(tmp_path / "data")


def test_tls_browser_attachment_upload_quota_rejections_preserve_prior_archive_and_show_failure(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, owner, writer = _accepted_owner(tmp_path, "browser-quota", file_limit=4, archive_limit=4)
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        first = _upload_browser(port, cert, call_id, writer, content=b"old")
        before_rows = _attachment_rows(database, call_id)
        rejected = _upload_browser(port, cert, call_id, writer, content=b"five!")
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after_rows = _attachment_rows(database, call_id)
    finally:
        _stop(process)

    assert first[0] == 200
    assert rejected[0] == 413 and isinstance(rejected[1], dict) and rejected[1].get("status") == "attachment_rejected"
    assert after_rows == before_rows and len(after_rows) == 1
    # A fresh authoritative page has no knowledge of a rejected browser upload;
    # its initial no-selection wording is independent of the client-side error.
    assert "no attachment selected." in str(page[2]).lower()


def test_tls_browser_attachment_submission_and_archive_quotas_are_distinct_and_preserve_prior_bytes(tmp_path: Path) -> None:
    """A smaller submission cap must not be conflated with file or archive capacity."""
    port, _, cert, process, call_id, _, _, writer = _accepted_owner(
        tmp_path, "browser-submission-quota", file_limit=64, submission_limit=4, archive_limit=64
    )
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        submission_rejected = _upload_browser(port, cert, call_id, writer, content=b"five!")
        assert submission_rejected[0] == 413 and isinstance(submission_rejected[1], dict)
        assert submission_rejected[1] == {"success": False, "status": "attachment_rejected", "reason": "submission_limit_exceeded"}
        assert _attachment_rows(database, call_id) == [] and _attachment_files(tmp_path / "data") == []
    finally:
        _stop(process)

    port, _, cert, process, call_id, _, _, writer = _accepted_owner(
        tmp_path / "archive", "browser-archive-quota", file_limit=64, submission_limit=64, archive_limit=4
    )
    database = tmp_path / "archive" / "data" / "calls.sqlite3"
    try:
        prior = _upload_browser(port, cert, call_id, writer, content=b"old")
        retained = _attachment_rows(database, call_id)
        rejected = _upload_browser(port, cert, call_id, writer, content=b"two")
        after = _attachment_rows(database, call_id)
    finally:
        _stop(process)

    assert prior[0] == 200 and len(retained) == 1
    assert rejected[0] == 413 and isinstance(rejected[1], dict)
    assert rejected[1] == {"success": False, "status": "attachment_rejected", "reason": "archive_capacity_exceeded"}
    assert after == retained and Path(retained[0]["storage_path"]).read_bytes() == b"old"


def test_tls_browser_attachment_upload_denies_untrusted_or_expired_contexts_without_archive_or_business_mutation(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, owner, writer = _accepted_owner(tmp_path, "browser-deny")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        other_cookie, other_csrf = _login(port, cert)
        before = _snapshot(database, call_id)
        before_denied_seen = _last_seen(database, call_id)
        denied = (
            _upload_browser(port, cert, call_id, owner),
            _upload_browser(port, cert, call_id, {"Cookie": cookie, "Origin": f"https://127.0.0.1:{port}", "X-Writer-Token": writer["X-Writer-Token"]}),
            _upload_browser(port, cert, call_id, {**writer, "X-Writer-Token": "stale"}),
            _upload_browser(port, cert, call_id, {"Cookie": other_cookie, "X-CSRF-Token": other_csrf, "Origin": f"https://127.0.0.1:{port}", "X-Writer-Token": writer["X-Writer-Token"]}),
            _upload_browser(port, cert, call_id, {"Authorization": "Bearer terminal-secret"}),
            _upload_browser(port, cert, call_id, {**writer, "Origin": "https://evil.example"}),
            _upload_browser(port, cert, call_id, {**writer, "Host": "evil.example"}),
            _upload_browser(port, cert, call_id, writer, malformed=True),
            _upload_browser(port, cert, call_id, writer, files=2),
        )
        denied_seen = _last_seen(database, call_id)
        assert denied_seen == before_denied_seen
        with sqlite3.connect(database) as connection:
            connection.execute("UPDATE calls SET terminal_last_seen_at = ?, terminal_disconnect_deadline = ? WHERE call_id = ?", ((datetime.now(UTC) - timedelta(seconds=181)).isoformat(), (datetime.now(UTC) - timedelta(seconds=1)).isoformat(), call_id)); connection.commit()
        expired = _upload_browser(port, cert, call_id, writer)
        rows = _attachment_rows(database, call_id); final = _snapshot(database, call_id)
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert all(item[0] in {400, 401, 403, 404, 409} for item in denied)
    assert all("attachment_id" not in repr(item[1]) for item in denied)
    assert expired[0] in {401, 403, 404, 409}
    assert rows == []
    assert final[:5] == ("disconnected", before[1] + 1, before[2], 0, None)
    assert final[5:] == (2, 0, 0)
    expired_page = str(page[2]).lower()
    assert 'data-state="disconnected"' in expired_page
    assert 'type="file"' not in expired_page and 'data-attachment-status' not in expired_page


def test_tls_browser_attachment_upload_rejects_ringing_and_route_excluded_without_liveness_or_storage_mutation(tmp_path: Path) -> None:
    port = _free_port(); config, cert = _write_operator_config(tmp_path / "ringing", port); _configure_phone_route(config)
    process = _start(config); _wait_for_listener(process, port)
    database = tmp_path / "ringing" / "data" / "calls.sqlite3"
    try:
        created = _create_call(port, cert, "browser-upload-ringing"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        before = _snapshot(database, call_id); before_seen = _last_seen(database, call_id)
        ringing = _upload_browser(port, cert, call_id, {**owner, "X-Writer-Token": "not-issued"})
        after = _snapshot(database, call_id); after_seen = _last_seen(database, call_id)
    finally:
        _stop(process)

    port = _free_port(); config, cert = _write_operator_config(tmp_path / "excluded", port)
    process = _start(config); _wait_for_listener(process, port)
    database = tmp_path / "excluded" / "data" / "calls.sqlite3"
    try:
        created = _create_call(port, cert, "browser-upload-excluded"); call_id = created["call_id"]; assert isinstance(call_id, str)
        cookie, csrf = _login(port, cert); owner = _headers(cookie, csrf, port)
        before_route = _snapshot(database, call_id); before_route_seen = _last_seen(database, call_id)
        excluded = _upload_browser(port, cert, call_id, {**owner, "X-Writer-Token": "not-issued"})
        after_route = _snapshot(database, call_id); after_route_seen = _last_seen(database, call_id)
    finally:
        _stop(process)

    assert ringing[0] in {400, 401, 403, 404, 409} and "attachment" not in repr(ringing[1]).lower()
    assert excluded[0] in {400, 401, 403, 404, 409} and "attachment" not in repr(excluded[1]).lower()
    assert before == after and before_seen == after_seen and _attachment_files(tmp_path / "ringing" / "data") == []
    assert before_route == after_route and before_route_seen == after_route_seen and _attachment_files(tmp_path / "excluded" / "data") == []


def test_tls_authorized_browser_upload_alone_refreshes_owner_liveness_and_uses_generated_storage_path(tmp_path: Path) -> None:
    port, _, cert, process, call_id, _, _, writer = _accepted_owner(tmp_path, "browser-upload-liveness")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before_seen = _last_seen(database, call_id)
        first = _upload_browser(port, cert, call_id, writer, filename="../../hostile-name.txt", content=b"safe path")
        after_seen = _last_seen(database, call_id)
        rows = _attachment_rows(database, call_id)
        get_seen = _last_seen(database, call_id)
        _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": writer["Cookie"]})
        after_get_seen = _last_seen(database, call_id)
    finally:
        _stop(process)

    assert first[0] == 200 and isinstance(first[1], dict) and len(rows) == 1
    stored = Path(rows[0]["storage_path"])
    assert stored.resolve().is_relative_to((tmp_path / "data" / "attachments").resolve())
    assert stored.name == rows[0]["attachment_id"] and stored.read_bytes() == b"safe path"
    assert rows[0]["original_name"] == "../../hostile-name.txt"
    assert before_seen != after_seen and get_seen == after_get_seen
