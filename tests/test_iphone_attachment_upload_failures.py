"""Red HTTPS contract for truthful iPhone attachment-upload failures.

This covers only the accepted-owner page presentation around an existing
single-file upload API.  It does not change attachment protocol or storage.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_iphone_browser_attachment_upload import (
    _accepted_owner,
    _attachment_rows,
    _last_seen,
    _snapshot,
    _stop,
    _upload_browser,
)
from test_iphone_web_acceptance import _request


def test_tls_accepted_iphone_attachment_control_has_ready_and_truthful_non_ok_presentation(tmp_path: Path) -> None:
    """A rejected upload must retain the same usable page controls and state."""
    port, _, cert, process, call_id, cookie, _owner, _writer = _accepted_owner(tmp_path, "attachment-upload-presentation")
    try:
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
    finally:
        _stop(process)

    assert page[0] == 200
    source = str(page[2])
    assert 'data-attachment-status="ready"' in source
    assert "No attachment selected." in source
    handler = re.search(
        r"button\[data-attachment-upload\].*?addEventListener\('click'.*?"
        r"response\.ok.*?pendingAttachmentIds\.push\(uploaded\.attachment\.attachment_id\).*?"
        r"else\{.*?data-attachment-status.*?not-uploaded.*?conflict or unavailable.*?\}",
        source,
        re.DOTALL,
    )
    assert handler is not None
    assert "outerHTML" not in handler.group(0)
    assert "location.reload" not in handler.group(0)


def test_tls_attachment_upload_failure_keeps_existing_archive_and_business_state(tmp_path: Path) -> None:
    port, _, cert, process, call_id, _cookie, _owner, writer = _accepted_owner(
        tmp_path, "attachment-upload-failure-state", file_limit=4, archive_limit=4
    )
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        first = _upload_browser(port, cert, call_id, writer, filename="prior.txt", content=b"old")
        before = _snapshot(database, call_id); before_seen = _last_seen(database, call_id)
        before_rows = _attachment_rows(database, call_id)
        rejected = _upload_browser(port, cert, call_id, writer, filename="too-large.txt", content=b"five!")
        after = _snapshot(database, call_id); after_seen = _last_seen(database, call_id)
        after_rows = _attachment_rows(database, call_id)
    finally:
        _stop(process)

    assert first[0] == 200 and isinstance(first[1], dict)
    assert isinstance(first[1].get("attachment"), dict)
    assert rejected[0] == 413
    assert before == after and before_seen == after_seen
    assert after_rows == before_rows and len(after_rows) == 1
    assert after_rows[0]["attachment_id"] == first[1]["attachment"]["attachment_id"]


def test_tls_attachment_upload_untrusted_requests_and_page_reads_do_not_take_over_or_mutate(tmp_path: Path) -> None:
    port, _, cert, process, call_id, cookie, owner, writer = _accepted_owner(tmp_path, "attachment-upload-read-only")
    database = tmp_path / "data" / "calls.sqlite3"
    try:
        before = _snapshot(database, call_id); before_seen = _last_seen(database, call_id)
        tokenless = _upload_browser(port, cert, call_id, owner)
        stale = _upload_browser(port, cert, call_id, {**writer, "X-Writer-Token": "stale"})
        page = _request(port, cert, "GET", f"/calls/{call_id}", headers={"Cookie": cookie})
        after = _snapshot(database, call_id); after_seen = _last_seen(database, call_id)
    finally:
        _stop(process)

    assert tokenless[0] in {400, 401, 403, 404, 409}
    assert stale[0] in {400, 401, 403, 404, 409}
    assert page[0] == 200
    assert before == after and before_seen == after_seen
    assert _attachment_rows(database, call_id) == []
