"""Red contracts for non-disclosing attachment archive write failures.

These coordinator-boundary cases use accepted, routed owners so an injected
filesystem failure cannot be mistaken for an authorization denial.  They do
not prescribe archive repair, schema changes, or upload retries.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from call_coordinator import AcceptCall, CallCoordinator, CreateCall


@dataclass
class _Clock:
    current: datetime = datetime(2026, 10, 30, tzinfo=UTC)

    def now_utc(self) -> datetime:
        return self.current


class _WriteFailurePath:
    def write_bytes(self, content: bytes) -> int:
        raise OSError("injected archive write failure")


class _WriteFailureDirectory:
    def exists(self) -> bool:
        return False

    def mkdir(self, *, parents: bool, exist_ok: bool) -> None:
        return None

    def __truediv__(self, attachment_id: str) -> _WriteFailurePath:
        return _WriteFailurePath()


class _MkdirFailureDirectory:
    def exists(self) -> bool:
        return False

    def mkdir(self, *, parents: bool, exist_ok: bool) -> None:
        raise OSError("injected archive mkdir failure")


def _snapshot(database: Path, call_id: str) -> tuple[object, ...]:
    """Record durable state independently of the coordinator response."""
    with sqlite3.connect(database) as connection:
        call = connection.execute("SELECT * FROM calls WHERE call_id = ?", (call_id,)).fetchone()
        attachments = connection.execute(
            "SELECT * FROM attachments WHERE call_id = ? ORDER BY attachment_id", (call_id,)
        ).fetchall()
        events = connection.execute(
            "SELECT * FROM events WHERE call_id = ? ORDER BY event_seq", (call_id,)
        ).fetchall()
        submissions = connection.execute(
            "SELECT * FROM submission_operations WHERE call_id = ? ORDER BY submission_id", (call_id,)
        ).fetchall()
    return call, attachments, events, submissions


def _accepted_owner(
    tmp_path: Path, *, owner_device_id: str, session_id: str, bark_enabled: bool,
) -> tuple[CallCoordinator, str, Path]:
    database = tmp_path / "calls.sqlite3"
    coordinator = CallCoordinator(database, clock=_Clock())
    created = coordinator.create_call(
        CreateCall(
            "operator-default", "agent-alpha", f"archive-{owner_device_id}",
            f"create-{owner_device_id}", "archive failure", 60,
            desktop_route_device_id=None if bark_enabled else owner_device_id,
            bark_enabled=bark_enabled,
        )
    )
    call_id = created["call_id"]
    coordinator.accept_call(
        AcceptCall(
            call_id, f"accept-{owner_device_id}", owner_device_id, session_id,
            "iphone_web" if bark_enabled else "desktop", "ios" if bark_enabled else "windows",
        )
    )
    return coordinator, call_id, database


def test_accepted_desktop_archive_mkdir_failure_is_non_disclosing_and_atomic(tmp_path: Path) -> None:
    coordinator, call_id, database = _accepted_owner(
        tmp_path, owner_device_id="desktop-registered", session_id="desktop-owner", bark_enabled=False
    )
    try:
        before = _snapshot(database, call_id)
        coordinator._attachment_directory = _MkdirFailureDirectory()  # type: ignore[assignment]
        failed = coordinator.upload_terminal_attachment(
            call_id, "desktop-registered", "desktop-owner", "evidence.txt", "text/plain", b"bytes"
        )
        assert failed == {
            "success": False,
            "status": "attachment_rejected",
            "reason": "archive_unavailable",
        }
        assert _snapshot(database, call_id) == before
    finally:
        coordinator.close()


def test_accepted_iphone_archive_write_failure_is_non_disclosing_atomic_and_recoverable(tmp_path: Path) -> None:
    coordinator, call_id, database = _accepted_owner(
        tmp_path, owner_device_id="iphone_web", session_id="iphone-owner", bark_enabled=True
    )
    archive_directory = coordinator._attachment_directory
    try:
        prior = coordinator.upload_iphone_attachment(
            call_id, "iphone-owner", "already-archived.txt", "text/plain", b"prior bytes"
        )
        assert prior is not None and prior["status"] == "uploaded"
        prior_attachment_id = prior["attachment"]["attachment_id"]
        before = _snapshot(database, call_id)
        coordinator._attachment_directory = _WriteFailureDirectory()  # type: ignore[assignment]
        failed = coordinator.upload_iphone_attachment(
            call_id, "iphone-owner", "photo.txt", "text/plain", b"phone bytes"
        )
        assert failed == {
            "success": False,
            "status": "attachment_rejected",
            "reason": "archive_unavailable",
        }
        assert _snapshot(database, call_id) == before

        # The failure cannot make an earlier durable attachment unreadable or
        # prevent the still-accepted owner from completing the interaction.
        assert coordinator.read_iphone_pending_attachments(call_id, "iphone-owner") == [
            prior["attachment"]
        ]

        completed = coordinator.submit_terminal_text(
            call_id, "iphone_web", "iphone-owner", "submit-after-archive-failure",
            "continue after archive failure", attachment_ids=[prior_attachment_id], operator_owner=True,
        )
        assert completed is not None and completed["status"] == "answered"
    finally:
        coordinator._attachment_directory = archive_directory
        coordinator.close()
