"""Contract tests for the postman bridge's delivery decisions.

The postman's two pure decisions decide whether a human answer is preserved or
dropped:

* ``select_target`` — route only chidori-detached entries to their agent.
* ``classify_http_error`` — a definitive status (404/409) marks undeliverable;
  anything else retries, so a Chidori restart never costs a human answer.
"""

from __future__ import annotations

import importlib

import pytest


def _module():
    return importlib.import_module("hitl_postman")


def test_select_target_routes_only_chidori_detached() -> None:
    m = _module()
    assert m.select_target({"delivery": {"kind": "chidori-detached", "agent": "probe"}}) == "probe"
    assert m.select_target({"delivery": {"kind": "other", "agent": "x"}}) is None
    assert m.select_target({"delivery": {"kind": "chidori-detached", "agent": ""}}) is None
    assert m.select_target({"delivery": None}) is None
    assert m.select_target({}) is None


def test_classify_http_error_preserves_answers_across_restarts() -> None:
    m = _module()
    # A definitive status means the agent can never receive this again.
    assert m.classify_http_error(404) == "definitive"
    assert m.classify_http_error(409) == "definitive"
    # Anything else — including the transport errors that carry no status —
    # must retry, or a Chidori restart drops the human's answer.
    for status in (400, 403, 500, 502, 503, 0):
        assert m.classify_http_error(status) == "retryable", status


def test_cursor_is_atomic_on_disk(tmp_path, monkeypatch) -> None:
    m = _module()
    monkeypatch.setattr(m, "CURSOR_FILE", str(tmp_path / "postman.cursor"))
    assert m.load_cursor() == 0
    m.save_cursor(42)
    assert m.load_cursor() == 42
    # no .tmp litter after a successful save
    assert not (tmp_path / "postman.cursor.tmp").exists()
    # corrupt content degrades to 0 rather than crashing the loop
    (tmp_path / "postman.cursor").write_text("not-a-number")
    assert m.load_cursor() == 0
