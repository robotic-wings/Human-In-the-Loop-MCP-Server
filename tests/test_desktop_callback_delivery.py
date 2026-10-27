"""Red contracts for persisted, authenticated desktop callback delivery.

The central HTTPS listener is exercised with its real SQLite database and a
separate, TLS-authenticated local terminal receiver.  This slice deliberately
does not prescribe GUI behaviour, polling, phone delivery, or unreachable
classification.
"""

from __future__ import annotations

import json
import http.client
import sqlite3
import ssl
import threading
import time
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from call_coordinator import AcceptCall, CallCoordinator, CreateCall, DeclineCall
from test_central_mcp_gateway import _call, _free_port, _post, _start, _stop, _structured, _write_certificate
from test_terminal_control_api import (
    _terminal_request,
    _wait_for_tls_gateway,
    _write_terminal_gateway_config,
)


class _CallbackReceiver(BaseHTTPRequestHandler):
    deliveries: list[dict[str, object]] = []
    rejected_authorizations: list[str | None] = []
    started = threading.Event()
    response_status = 204
    response_body = b""
    release = threading.Event()
    required_authorization = "Bearer desktop-callback-sender-secret"

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            decoded: object = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            decoded = raw.decode("utf-8", errors="replace")
        if self.headers.get("Authorization") != type(self).required_authorization:
            type(self).rejected_authorizations.append(self.headers.get("Authorization"))
            self.send_response(401)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        type(self).deliveries.append({"headers": dict(self.headers), "body": decoded})
        type(self).started.set()
        type(self).release.wait(timeout=4)
        self.send_response(type(self).response_status)
        self.send_header("Content-Length", str(len(type(self).response_body)))
        self.end_headers()
        self.wfile.write(type(self).response_body)

    def log_message(self, _format: str, *args: object) -> None:
        return


def _start_callback_receiver(tmp_path: Path) -> tuple[ThreadingHTTPServer, threading.Thread, str, Path]:
    certificate, key = _write_certificate(tmp_path / "callback-cert")
    _CallbackReceiver.deliveries = []
    _CallbackReceiver.rejected_authorizations = []
    _CallbackReceiver.started = threading.Event()
    _CallbackReceiver.response_status = 204
    _CallbackReceiver.response_body = b""
    _CallbackReceiver.release = threading.Event()
    _CallbackReceiver.release.set()
    _CallbackReceiver.required_authorization = "Bearer desktop-callback-sender-secret"
    receiver = ThreadingHTTPServer(("127.0.0.1", 0), _CallbackReceiver)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(certfile=certificate, keyfile=key)
    receiver.socket = context.wrap_socket(receiver.socket, server_side=True)
    thread = threading.Thread(target=receiver.serve_forever, daemon=True)
    thread.start()
    return receiver, thread, f"https://127.0.0.1:{receiver.server_port}/terminal/events", certificate


def _stop_callback_receiver(receiver: ThreadingHTTPServer, thread: threading.Thread) -> None:
    _CallbackReceiver.release.set()
    receiver.shutdown()
    receiver.server_close()
    thread.join(timeout=3)


def _direct_receiver_post(
    callback_url: str, certificate: Path, authorization: str | None
) -> int:
    parsed = urlsplit(callback_url)
    context = ssl.create_default_context(cafile=str(certificate))
    connection = http.client.HTTPSConnection(parsed.hostname, parsed.port, context=context, timeout=2)
    headers = {"Content-Type": "application/json"}
    if authorization is not None:
        headers["Authorization"] = authorization
    connection.request("POST", parsed.path, body=b"{}", headers=headers)
    response = connection.getresponse()
    response.read()
    connection.close()
    return response.status


def _configure_managed_callback(
    config_path: Path, callback_url: str, callback_ca_file: Path
) -> None:
    """Set callback values only in managed terminal credentials/service config."""
    config = json.loads(config_path.read_text(encoding="utf-8"))
    credentials_path = Path(config["terminal_credentials_file"])
    credentials = json.loads(credentials_path.read_text(encoding="utf-8"))
    credentials["desktop-token"] |= {
        "callback_url": callback_url,
        "callback_auth_token": "desktop-callback-sender-secret",
    }
    credentials_path.write_text(json.dumps(credentials), encoding="utf-8")
    config["terminal_callback_ca_file"] = str(callback_ca_file)
    config_path.write_text(json.dumps(config), encoding="utf-8")


def _register_desktop(port: int, certificate: Path) -> tuple[int, dict[str, Any]]:
    return _terminal_request(
        port,
        certificate,
        "POST",
        "/api/terminals/register",
        "desktop-token",
        {
            "instance_id": "callback-instance",
            "gui_ready": True,
            # These must remain untrusted input even when they look plausible.
            "callback_url": "https://attacker.invalid/terminal/events",
            "callback_auth_token": "attacker-secret",
            "device_id": "attacker-device",
            "route_device_id": "attacker-route",
            "terminal_platform": "ios",
        },
    )


def _hostile_heartbeat(port: int, certificate: Path) -> tuple[int, dict[str, Any]]:
    return _terminal_request(
        port,
        certificate,
        "POST",
        "/api/terminals/heartbeat",
        "desktop-token",
        {
            "instance_id": "callback-instance",
            "callback_url": "https://heartbeat-attacker.invalid/events",
            "callback_auth_token": "heartbeat-attacker-secret",
        },
    )


def _wait_for_callback(expected: int = 1) -> None:
    assert _CallbackReceiver.started.wait(timeout=3), "callback was not sent"
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        if len(_CallbackReceiver.deliveries) >= expected:
            return
        time.sleep(0.02)
    raise AssertionError(f"expected {expected} callbacks, got {_CallbackReceiver.deliveries!r}")


def _delivery_row(database: Path, call_id: str) -> tuple[str, int] | None:
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT state, attempt_count FROM terminal_callback_deliveries WHERE call_id = ?",
            (call_id,),
        ).fetchone()
    return None if row is None else (str(row[0]), int(row[1]))


def _assert_no_terminal_result_or_unreachable(database: Path, call_id: str) -> None:
    """Delivery trouble remains delivery trouble, never a human/business outcome."""
    with sqlite3.connect(database) as connection:
        call_state = connection.execute(
            "SELECT state FROM calls WHERE call_id = ?", (call_id,)
        ).fetchone()[0]
        result_count = connection.execute(
            "SELECT COUNT(*) FROM results WHERE call_id = ?", (call_id,)
        ).fetchone()[0]
        unreachable_events = connection.execute(
            "SELECT COUNT(*) FROM events WHERE call_id = ? AND result LIKE '%unreachable%'",
            (call_id,),
        ).fetchone()[0]
    assert call_state == "ringing"
    assert result_count == 0
    assert unreachable_events == 0


def test_managed_callback_configuration_survives_restart_and_ignores_registration_body(
    tmp_path: Path,
) -> None:
    """Only pre-registered configuration selects persisted callback routing."""
    receiver, receiver_thread, callback_url, callback_certificate = _start_callback_receiver(tmp_path)
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    _configure_managed_callback(config, callback_url, callback_certificate)
    process = _start(config)
    try:
        _wait_for_tls_gateway(process, port, certificate)
        registered = _register_desktop(port, certificate)
        assert registered[0] == 200
        assert registered[1]["presence"]["callback_url"] == callback_url
        heartbeat = _hostile_heartbeat(port, certificate)
        assert heartbeat[0] == 200
        assert heartbeat[1]["presence"]["callback_url"] == callback_url
        # The receiver itself rejects clients which do not possess the managed
        # sender credential, over the same trusted HTTPS transport as delivery.
        assert _direct_receiver_post(callback_url, callback_certificate, None) == 401
        assert _direct_receiver_post(
            callback_url, callback_certificate, "Bearer forged-sender-secret"
        ) == 401
        assert _CallbackReceiver.rejected_authorizations == [None, "Bearer forged-sender-secret"]
        hostile_agent = _call("hostile-callback-arguments")
        hostile_agent["params"]["arguments"].update({
            "callback_url": "https://agent-attacker.invalid/events",
            "callback_auth_token": "agent-attacker-secret",
        })
        _, hostile_response, _ = _post(port, certificate, hostile_agent, "test-token-alpha")
        assert hostile_response["error"]["code"] == -32602
        _stop(process)
        process = _start(config)
        _wait_for_tls_gateway(process, port, certificate)
        status, response, _ = _post(port, certificate, _call("callback-after-restart"), "test-token-alpha")
        assert status == 200
        call = _structured(response)
        assert call["status"] == "heartbeat"
        _wait_for_callback()
    finally:
        _stop(process)
        _stop_callback_receiver(receiver, receiver_thread)

    delivered = _CallbackReceiver.deliveries[0]
    assert delivered["headers"].get("Authorization") == "Bearer desktop-callback-sender-secret"
    assert delivered["body"] == {
        "call_id": call["call_id"],
        "version": delivered["body"]["version"],
        "ring_deadline": delivered["body"]["ring_deadline"],
        "event_type": "ringing",
    }


def test_desktop_callback_is_one_shot_durable_and_does_not_delay_mcp_wait(
    tmp_path: Path,
) -> None:
    """One admitted desktop ringing call creates one small, asynchronous callback."""
    receiver, receiver_thread, callback_url, callback_certificate = _start_callback_receiver(tmp_path)
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    _configure_managed_callback(config, callback_url, callback_certificate)
    process = _start(config)
    try:
        _wait_for_tls_gateway(process, port, certificate)
        assert _register_desktop(port, certificate)[0] == 200
        status, response, elapsed = _post(port, certificate, _call("callback-one-shot"), "test-token-alpha")
        call = _structured(response)
        assert status == 200
        assert elapsed < 1
        _wait_for_callback()
        _, continuation, _ = _post(
            port, certificate,
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_user_input", "arguments": {
                "interaction_id": call["interaction_id"], "call_id": call["call_id"],
                "request_id": "callback-one-shot-continuation", "after_event_seq": 0,
                "client_timeout_seconds": 2,
            }}},
            "test-token-alpha",
        )
        assert _structured(continuation)["status"] in {"heartbeat", "opened"}
        time.sleep(0.2)
        row = _delivery_row(tmp_path / "data" / "calls.sqlite3", call["call_id"])
    finally:
        _stop(process)
        _stop_callback_receiver(receiver, receiver_thread)

    assert len(_CallbackReceiver.deliveries) == 1
    payload = _CallbackReceiver.deliveries[0]["body"]
    assert isinstance(payload, dict)
    assert set(payload) == {"call_id", "version", "ring_deadline", "event_type"}
    assert call["call_id"] == payload["call_id"]
    encoded = json.dumps(payload).lower()
    assert "prompt" not in encoded and "token" not in encoded and "secret" not in encoded
    assert row == ("success", 1)


def test_callback_failure_and_pre_send_suppression_are_durable_and_never_blindly_resent(
    tmp_path: Path,
) -> None:
    """Failure state is distinct, while an already-ended call never crosses HTTP."""
    receiver, receiver_thread, callback_url, callback_certificate = _start_callback_receiver(tmp_path)
    _CallbackReceiver.response_status = 503
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    _configure_managed_callback(config, callback_url, callback_certificate)
    process = _start(config)
    try:
        _wait_for_tls_gateway(process, port, certificate)
        assert _register_desktop(port, certificate)[0] == 200
        _, response, _ = _post(port, certificate, _call("callback-failed"), "test-token-alpha")
        failed_call = _structured(response)
        _wait_for_callback()
        deadline = time.monotonic() + 3
        row = None
        while time.monotonic() < deadline:
            row = _delivery_row(tmp_path / "data" / "calls.sqlite3", failed_call["call_id"])
            if row is not None and row[0] == "failed":
                break
            time.sleep(0.03)
        assert row == ("failed", 1)
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            failed_call_state = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (failed_call["call_id"],)
            ).fetchone()[0]
            failed_events = connection.execute(
                "SELECT COUNT(*) FROM events WHERE call_id = ?", (failed_call["call_id"],)
            ).fetchone()[0]
        _stop(process)
        process = _start(config)
        _wait_for_tls_gateway(process, port, certificate)
        time.sleep(0.2)
    finally:
        _stop(process)
        _stop_callback_receiver(receiver, receiver_thread)

    assert len(_CallbackReceiver.deliveries) == 1
    assert failed_call_state == "ringing"
    assert failed_events == 0
    _assert_no_terminal_result_or_unreachable(tmp_path / "data" / "calls.sqlite3", failed_call["call_id"])


def test_callback_sending_restart_is_uncertain_not_resent_and_cancelled_call_is_suppressed(
    tmp_path: Path,
) -> None:
    """Unknown sends are retained once; an authoritative early end suppresses another send."""
    receiver, receiver_thread, callback_url, callback_certificate = _start_callback_receiver(tmp_path)
    _CallbackReceiver.release.clear()
    port = _free_port()
    config, certificate = _write_terminal_gateway_config(tmp_path, port)
    _configure_managed_callback(config, callback_url, callback_certificate)
    process = _start(config)
    try:
        _wait_for_tls_gateway(process, port, certificate)
        assert _register_desktop(port, certificate)[0] == 200
        _, response, _ = _post(port, certificate, _call("callback-timeout"), "test-token-alpha")
        sending_call = _structured(response)
        _wait_for_callback()
        assert _delivery_row(tmp_path / "data" / "calls.sqlite3", sending_call["call_id"]) == ("sending", 1)
        # Terminate while the callback receiver keeps the TLS request open.
        # Startup recovery must record the unknowable outcome as uncertain.
        _stop(process)
        _CallbackReceiver.release.set()
        process = _start(config)
        _wait_for_tls_gateway(process, port, certificate)
        time.sleep(0.25)
        recovered_row = _delivery_row(tmp_path / "data" / "calls.sqlite3", sending_call["call_id"])
        with sqlite3.connect(tmp_path / "data" / "calls.sqlite3") as connection:
            timeout_call_state = connection.execute(
                "SELECT state FROM calls WHERE call_id = ?", (sending_call["call_id"],)
            ).fetchone()[0]
            timeout_events = connection.execute(
                "SELECT COUNT(*) FROM events WHERE call_id = ?", (sending_call["call_id"],)
            ).fetchone()[0]

        # This second call is ended through the authoritative terminal route
        # before the asynchronous callback worker may cross its final send
        # boundary.  It must be recorded as suppression, not a second HTTP POST.
        _, response, _ = _post(port, certificate, _call("callback-cancelled-before-send"), "test-token-alpha")
        cancelled_call = _structured(response)
        declined = _terminal_request(
            port, certificate, "POST", f"/api/calls/{cancelled_call['call_id']}/decline", "desktop-token",
            {"action_id": "decline-before-callback"},
        )
        assert declined[0] == 200
        time.sleep(0.3)
        suppressed_row = _delivery_row(tmp_path / "data" / "calls.sqlite3", cancelled_call["call_id"])
    finally:
        _stop(process)
        _stop_callback_receiver(receiver, receiver_thread)

    assert len(_CallbackReceiver.deliveries) == 1
    assert recovered_row == ("uncertain", 1)
    assert timeout_call_state == "ringing"
    assert timeout_events == 0
    _assert_no_terminal_result_or_unreachable(tmp_path / "data" / "calls.sqlite3", sending_call["call_id"])
    assert suppressed_row == ("suppressed", 1)


class _WallClock:
    def now_utc(self) -> datetime:
        return datetime.now(UTC)


class _MutableClock:
    def __init__(self) -> None:
        self.now = datetime.now(UTC)

    def now_utc(self) -> datetime:
        return self.now

    def advance(self, seconds: int) -> None:
        self.now = self.now.replace(microsecond=0) + timedelta(seconds=seconds)


class _AcceptAfterCallbackClaimCoordinator(CallCoordinator):
    """Force the post-claim/pre-send race without allowing external I/O first."""

    def claim_next_terminal_callback_delivery(self) -> dict[str, str] | None:
        delivery = super().claim_next_terminal_callback_delivery()
        if delivery is not None:
            self.accept_call(AcceptCall(
                call_id=delivery["call_id"], action_id="accept-after-callback-claim",
                device_id="desktop-registered", session_id="accepted-session",
                handled_on="desktop", terminal_platform="windows",
            ))
        return delivery


class _EndAfterCallbackClaimCoordinator(CallCoordinator):
    """Settle a selected terminal state between durable claim and transport I/O."""

    def __init__(self, *args: object, end_kind: str, clock: _MutableClock | _WallClock, **kwargs: object) -> None:
        super().__init__(*args, clock=clock, **kwargs)
        self._end_kind = end_kind
        self._test_clock = clock

    def claim_next_terminal_callback_delivery(self) -> dict[str, str] | None:
        delivery = super().claim_next_terminal_callback_delivery()
        if delivery is None:
            return None
        if self._end_kind == "decline":
            self.decline_call(DeclineCall(
                call_id=delivery["call_id"], action_id="decline-after-callback-claim",
                actor_id="operator-default",
            ))
        elif self._end_kind == "expiry":
            assert isinstance(self._test_clock, _MutableClock)
            self._test_clock.advance(61)
            self.recover_and_settle()
        else:
            raise AssertionError(f"unknown test end kind {self._end_kind}")
        return delivery


def test_accepted_after_callback_claim_is_suppressed_before_external_https_send(tmp_path: Path) -> None:
    """The final authority check must observe acceptance after an outbox claim."""
    # The callback scheduler is intentionally imported in this test, rather
    # than at module import, so the preceding HTTP contracts retain their
    # specific Red failures before the production scheduler exists.
    from central_service import DesktopCallbackDeliveryScheduler

    receiver, receiver_thread, callback_url, callback_certificate = _start_callback_receiver(tmp_path)
    coordinator = _AcceptAfterCallbackClaimCoordinator(tmp_path / "calls.sqlite3", clock=_WallClock())
    coordinator.register_terminal_presence(
        device_id="desktop-registered", terminal_platform="windows",
        route_device_id="desktop-registered", callback_url=callback_url,
        instance_id="callback-instance", gui_ready=True,
    )
    created = coordinator.create_call(CreateCall(
        "operator-default", "agent-alpha", "accepted-after-claim", "accepted-after-claim-create",
        json.dumps({"arguments": {"title": "No external callback"}}), 60,
        desktop_route_device_id="desktop-registered",
    ))
    scheduler = DesktopCallbackDeliveryScheduler(coordinator, callback_ca_file=callback_certificate)
    scheduler.start()
    try:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            row = _delivery_row(tmp_path / "calls.sqlite3", created["call_id"])
            if row == ("suppressed", 1):
                break
            time.sleep(0.02)
        assert row == ("suppressed", 1)
        assert not _CallbackReceiver.started.wait(timeout=0.3)
    finally:
        scheduler.stop()
        coordinator.close()
        _stop_callback_receiver(receiver, receiver_thread)


def _assert_end_after_claim_is_suppressed(tmp_path: Path, end_kind: str) -> None:
    """Run a real TLS receiver but deterministically forbid post-claim external I/O."""
    from central_service import DesktopCallbackDeliveryScheduler

    receiver, receiver_thread, callback_url, callback_certificate = _start_callback_receiver(tmp_path)
    clock: _MutableClock | _WallClock = _MutableClock() if end_kind == "expiry" else _WallClock()
    coordinator = _EndAfterCallbackClaimCoordinator(
        tmp_path / "calls.sqlite3", clock=clock, end_kind=end_kind
    )
    coordinator.register_terminal_presence(
        device_id="desktop-registered", terminal_platform="windows",
        route_device_id="desktop-registered", callback_url=callback_url,
        instance_id="callback-instance", gui_ready=True,
    )
    created = coordinator.create_call(CreateCall(
        "operator-default", "agent-alpha", f"{end_kind}-after-claim", f"{end_kind}-after-claim-create",
        json.dumps({"arguments": {"title": "No external callback"}}), 60,
        desktop_route_device_id="desktop-registered",
    ))
    scheduler = DesktopCallbackDeliveryScheduler(coordinator, callback_ca_file=callback_certificate)
    scheduler.start()
    try:
        deadline = time.monotonic() + 2
        row = None
        while time.monotonic() < deadline:
            row = _delivery_row(tmp_path / "calls.sqlite3", created["call_id"])
            if row == ("suppressed", 1):
                break
            time.sleep(0.02)
        assert row == ("suppressed", 1)
        assert not _CallbackReceiver.started.wait(timeout=0.3)
    finally:
        scheduler.stop()
        coordinator.close()
        _stop_callback_receiver(receiver, receiver_thread)


def test_declined_after_callback_claim_is_suppressed_before_external_https_send(tmp_path: Path) -> None:
    _assert_end_after_claim_is_suppressed(tmp_path, "decline")


def test_expired_after_callback_claim_is_suppressed_before_external_https_send(tmp_path: Path) -> None:
    _assert_end_after_claim_is_suppressed(tmp_path, "expiry")
