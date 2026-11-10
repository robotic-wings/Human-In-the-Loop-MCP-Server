"""Red contract for a non-blocking desktop-terminal heartbeat loop.

``central_desktop_terminal.main``'s ``tick`` runs three blocking HTTP calls on
the Tk main thread every ``heartbeat_seconds``: ``central.heartbeat()``,
``central.synchronize()`` and ``client.sync()`` (each bounded by a 5s timeout).
That freezes the GUI on every tick.  The blocking network group must move off
the UI thread, and UI state updates must reach the Tk loop through ``root.after``
rather than being applied inline.

The fix needs a testable seam.  This file pins that seam as a module-level
``run_heartbeat_tick`` in ``central_desktop_terminal``:

    run_heartbeat_tick(central, client, *, callback_credential, heartbeat_seconds,
                       run_worker, schedule_ui)

* ``run_worker(fn)`` executes ``fn`` on a background thread (injected).
* ``schedule_ui(delay_ms, fn)`` schedules ``fn`` on the UI thread after
  ``delay_ms`` (injected; production supplies ``root.after``).

Contract: the heartbeat/synchronize/sync network group runs only inside the
``run_worker`` callback (never on the calling thread); the next tick is re-armed
through ``schedule_ui(heartbeat_seconds * 1000, ...)``; and a ``ConnectionError``
is applied to the client through ``schedule_ui(0, ...)``, not inline.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any


def _module() -> Any:
    return importlib.import_module("central_desktop_terminal")


class FakeCentral:
    def __init__(self) -> None:
        self.heartbeat_tids: list[int] = []
        self.synchronize_tids: list[int] = []
        self.fail = False

    def heartbeat(self) -> dict[str, object]:
        self.heartbeat_tids.append(threading.get_ident())
        if self.fail:
            raise ConnectionError("central offline")
        return {"success": True}

    def synchronize(self) -> list[dict[str, object]]:
        self.synchronize_tids.append(threading.get_ident())
        if self.fail:
            raise ConnectionError("central offline")
        return []

    def read_call(self, *_args: object, **_kwargs: object) -> dict[str, object]:
        return {"success": False, "status": "session_not_found"}


class FakeClient:
    def __init__(self) -> None:
        self.sync_tids: list[int] = []
        self.lost_tids: list[int] = []

    def sync(self) -> dict[str, object]:
        self.sync_tids.append(threading.get_ident())
        return {"success": True, "status": "synchronized", "call_ids": []}

    def receive_notification(self, _state: object, *, callback_credential: str | None) -> dict[str, object]:
        del callback_credential
        return {"success": True, "status": "presented"}

    def _central_lost(self) -> dict[str, object]:
        self.lost_tids.append(threading.get_ident())
        return {"success": False, "status": "central_unavailable"}


class FakeWorker:
    def __init__(self) -> None:
        self.fns: list[Any] = []
        self.done = threading.Event()

    def __call__(self, fn: Any) -> None:
        self.fns.append(fn)

        def target() -> None:
            try:
                fn()
            finally:
                self.done.set()

        threading.Thread(target=target, daemon=True).start()


class FakeScheduler:
    def __init__(self) -> None:
        self.calls: list[tuple[int, Any]] = []

    def __call__(self, delay_ms: int, fn: Any) -> None:
        self.calls.append((delay_ms, fn))


def test_heartbeat_tick_offloads_blocking_network_calls_from_the_calling_thread() -> None:
    central, client = FakeCentral(), FakeClient()
    worker, scheduler = FakeWorker(), FakeScheduler()
    caller_tid = threading.get_ident()

    _module().run_heartbeat_tick(
        central, client,
        callback_credential="central-callback",
        heartbeat_seconds=10,
        run_worker=worker,
        schedule_ui=scheduler,
    )

    assert worker.fns, "the blocking network group must run inside run_worker"
    assert worker.done.wait(timeout=5)
    assert central.heartbeat_tids and all(t != caller_tid for t in central.heartbeat_tids)
    assert central.synchronize_tids and all(t != caller_tid for t in central.synchronize_tids)
    assert client.sync_tids and all(t != caller_tid for t in client.sync_tids)


def test_heartbeat_tick_rearms_the_next_tick_on_the_ui_thread() -> None:
    central, client = FakeCentral(), FakeClient()
    worker, scheduler = FakeWorker(), FakeScheduler()

    _module().run_heartbeat_tick(
        central, client,
        callback_credential="central-callback",
        heartbeat_seconds=10,
        run_worker=worker,
        schedule_ui=scheduler,
    )

    rearm = [delay for delay, _fn in scheduler.calls if delay == 10_000]
    assert rearm, "the next tick must be scheduled on the UI thread via schedule_ui"


def test_connection_loss_is_applied_via_ui_scheduler_not_inline() -> None:
    central, client = FakeCentral(), FakeClient()
    central.fail = True
    worker, scheduler = FakeWorker(), FakeScheduler()
    caller_tid = threading.get_ident()

    _module().run_heartbeat_tick(
        central, client,
        callback_credential="central-callback",
        heartbeat_seconds=10,
        run_worker=worker,
        schedule_ui=scheduler,
    )

    assert worker.done.wait(timeout=5)
    assert client.lost_tids == [], "central loss must not mutate the client on the worker thread"
    loss = [fn for delay, fn in scheduler.calls if delay == 0]
    assert loss, "central loss must be scheduled on the UI thread via schedule_ui(0, ...)"
    loss[0]()
    assert client.lost_tids == [caller_tid]
