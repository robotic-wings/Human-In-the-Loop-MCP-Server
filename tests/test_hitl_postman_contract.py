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


# ── delivery loop: at-least-once + cursor/ack ordering ────────────

def _entry(seq: int = 1) -> dict:
    return {
        "seq": seq, "call_id": f"call-{seq}", "operator": "NIGHTHAWK",
        "delivery": {"kind": "chidori-detached", "agent": "probe"},
        "outcome": {"call_state": "completed", "needs_continuation": False},
    }


def _probe(max_attempts=60):
    """Injected callbacks that record every send/ack/save/sleep."""
    calls = {"deliver": [], "ack": [], "save": [], "sleep": []}

    def deliver_fn(entry):
        calls["deliver"].append(entry["seq"])
        return calls["deliver_result"]

    def ack_fn(seq, status):
        calls["ack"].append((seq, status))

    def save_fn(seq):
        calls["save"].append(seq)

    def sleep_fn(seconds):
        calls["sleep"].append(seconds)

    return calls, deliver_fn, ack_fn, save_fn, sleep_fn


def test_delivery_success_acks_then_saves_cursor():
    m = _module()
    calls, deliver_fn, ack_fn, save_fn, sleep_fn = _probe()
    calls["deliver_result"] = True
    status = m.process_entry(_entry(7), deliver_fn=deliver_fn, ack_fn=ack_fn,
                             save_fn=save_fn, sleep_fn=sleep_fn)
    assert status == "delivered"
    assert calls["ack"] == [(7, "delivered")]
    assert calls["save"] == [7]
    assert calls["sleep"] == []


def test_definitive_404_is_undeliverable_without_retry():
    m = _module()
    calls, deliver_fn, ack_fn, save_fn, sleep_fn = _probe()
    def deliver_404(entry):
        calls["deliver"].append(entry["seq"])
        raise _http_error(404)
    status = m.process_entry(_entry(3), deliver_fn=deliver_404, ack_fn=ack_fn,
                             save_fn=save_fn, sleep_fn=sleep_fn)
    assert status == "undeliverable"
    assert calls["ack"] == [(3, "undeliverable")]
    assert calls["save"] == [3]
    assert calls["sleep"] == [], "a definitive status must not retry"


def test_connection_error_retries_bounded_then_undeliverable():
    m = _module()
    calls, deliver_fn, ack_fn, save_fn, sleep_fn = _probe()
    def deliver_down(entry):
        calls["deliver"].append(entry["seq"])
        raise ConnectionError("chidori restarting")
    status = m.process_entry(_entry(1), deliver_fn=deliver_down, ack_fn=ack_fn,
                             save_fn=save_fn, sleep_fn=sleep_fn, max_attempts=3)
    assert status == "undeliverable"
    assert len(calls["deliver"]) == 4, "1 initial + 3 retries"
    assert calls["ack"] == [(1, "undeliverable")]
    assert calls["save"] == [1]


def test_non_detached_entry_is_skipped_without_send():
    m = _module()
    calls, deliver_fn, ack_fn, save_fn, sleep_fn = _probe()
    entry = {"seq": 5, "call_id": "c", "delivery": None, "outcome": {}}
    status = m.process_entry(entry, deliver_fn=deliver_fn, ack_fn=ack_fn,
                             save_fn=save_fn, sleep_fn=sleep_fn)
    assert status == "skipped"
    assert calls["deliver"] == [] and calls["ack"] == []
    assert calls["save"] == [5]


def test_ack_failure_does_not_drop_the_outcome():
    m = _module()
    calls, deliver_fn, ack_fn, save_fn, sleep_fn = _probe()
    calls["deliver_result"] = True
    def ack_fails(seq, status):
        calls["ack"].append((seq, status))
        raise ConnectionError("central briefly down")
    status = m.process_entry(_entry(9), deliver_fn=deliver_fn, ack_fn=ack_fails,
                             save_fn=save_fn, sleep_fn=sleep_fn)
    assert status == "delivered", "delivery succeeded even though the ack failed"
    assert calls["save"] == [9], "cursor still advances so the outcome is not re-sent"


def _http_error(code):
    import urllib.error
    return urllib.error.HTTPError("http://x", code, "reason", None, None)
