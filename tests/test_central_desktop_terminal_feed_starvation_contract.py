"""Contract for keeping the terminal's poll bounded and its closes prompt.

Central's ``/api/terminals/state`` reports every round this device ever owned,
including settled ones.  The terminal used to issue one ``read_call`` round
trip per reported row before it ran ``sync()``; once the device had a few
hundred historical rounds a single poll took about a minute, so a cancelled
call's window stayed on screen long after the 10-second poll should have
retired it (the user saw a minute or more).

Pinned here:

* a settled row the terminal never presented is retired locally without any
  authority read;
* a settled row that *is* presented closes its window without a read;
* the poll retires settled windows through ``sync()`` before walking the feed;
* a second heartbeat group never starts while one is still on the wire;
* the HTTP adapter reuses one pooled client instead of opening a fresh TLS
  connection per request.
"""

from __future__ import annotations

import importlib
import threading
from typing import Any

import httpx

from central_desktop_terminal import CentralHttpDesktopClient, DesktopTerminalConfig


def _module() -> Any:
    return importlib.import_module("central_desktop_terminal")


def _config() -> DesktopTerminalConfig:
    return DesktopTerminalConfig(
        central_url="https://central.test",
        callsign="desktop-61",
        operator_password="desktop-secret",
        device_id="desktop-61",
        terminal_platform="macos",
        callback_credential="callback-secret",
        listener_host="127.0.0.1",
        listener_port=9100,
        verify_tls=True,
    )


class FakeCentral:
    """Authority stand-in that counts how often it is read."""

    def __init__(self, states: dict[str, dict[str, object]] | None = None) -> None:
        self.reads: list[str] = []
        self.states = states or {}
        self.permissions = {"macos": True, "attach_files": True}

    def read_call(self, call_id: str, session_id: str) -> dict[str, object]:
        del session_id
        self.reads.append(call_id)
        if call_id not in self.states:
            return {"success": False, "status": "session_not_found"}
        return {"success": True, **self.states[call_id]}


class FakeListener:
    def __init__(self) -> None:
        self.acknowledged: list[dict[str, object]] = []

    def acknowledge(self, event: dict[str, object]) -> None:
        self.acknowledged.append(event)


class FakeRing:
    def __init__(self) -> None:
        self.started: list[str] = []
        self.stopped: list[str] = []

    def start(self, call_id: str, mode: str = "long") -> None:
        del mode
        self.started.append(call_id)

    def stop(self, call_id: str) -> None:
        self.stopped.append(call_id)


class FakePresenter:
    def __init__(self) -> None:
        self.shown: list[str] = []
        self.closed: list[str] = []

    def show(self, state: dict[str, object]) -> None:
        self.shown.append(str(state["call_id"]))

    def close(self, call_id: str) -> None:
        self.closed.append(call_id)


def _client(central: FakeCentral) -> tuple[Any, FakeRing, FakePresenter, FakeListener]:
    ring, presenter, listener = FakeRing(), FakePresenter(), FakeListener()
    client = _module().DesktopTerminalClient(
        config=_config().__dict__ if hasattr(_config(), "__dict__") else {},
        central=central,
        listener=listener,
        gui_capability=lambda: True,
        ring=ring,
        presenter=presenter,
    )
    client._online = True
    client._session_id = "session-1"
    client._gui_ready = True
    return client, ring, presenter, listener


def test_settled_feed_rows_cost_no_authority_read() -> None:
    central = FakeCentral()
    client, _ring, _presenter, listener = _client(central)

    for index in range(50):
        result = client.receive_notification(
            {"call_id": f"settled-{index}", "call_state": "declined", "version": 2},
            callback_credential="callback-secret",
        )
        assert result["success"] is True
        assert result["status"] == "ignored"

    assert central.reads == [], "a settled row nothing local tracks needs no round trip"
    assert len(listener.acknowledged) == 50


def test_tracked_settled_row_closes_its_window_without_a_read() -> None:
    central = FakeCentral({"call-1": {"call_id": "call-1", "call_state": "ringing", "version": 1}})
    client, ring, presenter, _listener = _client(central)

    presented = client.receive_notification(
        {"call_id": "call-1", "call_state": "ringing", "version": 1},
        callback_credential="callback-secret",
    )
    assert presented["status"] == "presented"
    assert presenter.shown == ["call-1"] and ring.started == ["call-1"]
    reads_before = list(central.reads)

    result = client.receive_notification(
        {"call_id": "call-1", "call_state": "cancelled", "version": 2},
        callback_credential="callback-secret",
    )

    assert result["status"] == "closed"
    assert presenter.closed == ["call-1"]
    assert ring.stopped == ["call-1"]
    assert central.reads == reads_before, "the feed row is authoritative; no extra read"


def test_tick_retires_settled_windows_before_walking_the_feed() -> None:
    order: list[str] = []

    class OrderingCentral(FakeCentral):
        def heartbeat(self) -> dict[str, object]:
            order.append("heartbeat")
            return {"success": True}

        def synchronize(self) -> list[dict[str, object]]:
            order.append("synchronize")
            return []

    class OrderingClient:
        def __init__(self) -> None:
            self.lost = False

        def sync(self) -> dict[str, object]:
            order.append("sync")
            return {"success": True, "status": "synchronized", "call_ids": []}

        def receive_notification(self, _state: object, *, callback_credential: str | None) -> dict[str, object]:
            del callback_credential
            order.append("receive")
            return {"success": True, "status": "ignored"}

        def begin_tick(self) -> bool:
            return True

        def end_tick(self) -> None:
            pass

        def _central_lost(self) -> dict[str, object]:
            self.lost = True
            return {"success": False, "status": "central_unavailable"}

    client = OrderingClient()
    done = threading.Event()

    def run_worker(fn: Any) -> None:
        def target() -> None:
            try:
                fn()
            finally:
                done.set()

        threading.Thread(target=target, daemon=True).start()

    _module().run_heartbeat_tick(
        OrderingCentral(), client,
        callback_credential="callback-secret",
        heartbeat_seconds=10,
        run_worker=run_worker,
        schedule_ui=lambda _delay, _fn: None,
    )

    assert done.wait(timeout=5)
    assert order[:2] == ["heartbeat", "sync"], order
    assert "synchronize" in order
    assert order.index("sync") < order.index("synchronize")


def test_a_second_group_does_not_start_while_one_is_on_the_wire() -> None:
    client, _ring, _presenter, _listener = _client(FakeCentral())

    assert client.begin_tick() is True
    assert client.begin_tick() is False, "overlapping polls must be refused"
    client.end_tick()
    assert client.begin_tick() is True
    client.end_tick()


def test_requests_reuse_one_pooled_client(monkeypatch: Any) -> None:
    created: list[object] = []

    class FakeResponse:
        def json(self) -> dict[str, object]:
            return {"success": True}

    class CountingClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            created.append(self)
            self.is_closed = False
            self.requests = 0

        def request(self, *args: object, **kwargs: object) -> FakeResponse:
            self.requests += 1
            return FakeResponse()

        def close(self) -> None:
            self.is_closed = True

    monkeypatch.setattr(httpx, "Client", CountingClient)
    adapter = CentralHttpDesktopClient(_config())

    for _ in range(5):
        adapter.heartbeat()

    assert len(created) == 1, "one pooled client serves the whole process"
    assert created[0].requests == 5
