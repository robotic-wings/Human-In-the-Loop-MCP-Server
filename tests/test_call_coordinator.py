"""Red contracts for the SQLite call-coordination boundary.

This intentionally exercises only persistent ringing, admission, expiration,
acceptance, decline, and idempotency.  HTTP, MCP, notifications, and waits
belong to later vertical slices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import Barrier, Thread
from typing import Any

from call_coordinator import AcceptCall, CallCoordinator, CreateCall, DeclineCall


UNSET = object()


@dataclass
class FakeClock:
    current: datetime

    def now_utc(self) -> datetime:
        return self.current

    def advance(self, seconds: int) -> None:
        self.current += timedelta(seconds=seconds)


def make_clock() -> FakeClock:
    return FakeClock(datetime(2026, 10, 26, 0, 0, tzinfo=UTC))


def create_request(
    *,
    caller_id: str = "agent-a",
    interaction_id: str = "interaction-a",
    request_id: str = "request-a",
    message: str = "Need confirmation",
) -> CreateCall:
    return CreateCall(
        operator_id="operator-1",
        caller_id=caller_id,
        interaction_id=interaction_id,
        request_id=request_id,
        message=message,
        ring_timeout_seconds=60,
    )


def coordinator(tmp_path: Path, clock: FakeClock) -> CallCoordinator:
    return CallCoordinator(tmp_path / "calls.sqlite3", clock=clock)


def assert_envelope(
    result: dict[str, Any], *, status: str, call_state: str | None | object = UNSET
) -> None:
    assert result["success"] is True
    assert result["status"] == status
    expected_state = status if call_state is UNSET else call_state
    assert result["call_state"] == expected_state
    for field in {
        "reason",
        "human_action",
        "needs_continuation",
        "interaction_id",
        "call_id",
        "event_seq",
        "retryable",
        "retry_after_seconds",
        "handled_on",
        "terminal_platform",
        "message",
    }:
        assert field in result


def accept_request(call_id: str, *, action_id: str = "accept-a") -> AcceptCall:
    return AcceptCall(
        call_id=call_id,
        action_id=action_id,
        device_id="iphone-1",
        session_id="browser-session-1",
        handled_on="iphone_web",
        terminal_platform="ios",
    )


def decline_request(call_id: str, *, action_id: str = "decline-a") -> DeclineCall:
    return DeclineCall(call_id=call_id, action_id=action_id, actor_id="operator-1")


def test_ringing_call_persists_across_reopen_and_remains_busy(tmp_path: Path) -> None:
    clock = make_clock()
    first = coordinator(tmp_path, clock)
    created = first.create_call(create_request())
    assert_envelope(created, status="ringing")
    call_id = created["call_id"]
    deadline = first.get_call(call_id)["ring_deadline"]
    first.close()

    reopened = coordinator(tmp_path, clock)
    recovered = reopened.recover_and_settle()
    assert recovered == []
    assert reopened.get_call(call_id)["ring_deadline"] == deadline
    busy = reopened.create_call(
        create_request(caller_id="agent-b", request_id="request-b")
    )
    assert_envelope(busy, status="busy", call_state=None)
    assert busy["call_state"] is None
    assert busy["call_id"] is None
    assert busy["handled_on"] is None
    assert busy["terminal_platform"] is None
    assert busy["event_seq"] is None


def test_accepted_call_persists_across_reopen_and_remains_busy(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    first = coordinator(tmp_path, clock)
    created = first.create_call(create_request())
    call_id = created["call_id"]
    accepted = first.accept_call(accept_request(call_id))
    assert_envelope(accepted, status="opened", call_state="accepted")
    first.close()

    reopened = coordinator(tmp_path, clock)
    recovered = reopened.get_call(call_id)
    assert_envelope(recovered, status="accepted")
    assert recovered["call_id"] == call_id
    assert recovered["owner_device_id"] == "iphone-1"
    assert recovered["owner_session_id"] == "browser-session-1"
    busy = reopened.create_call(
        create_request(caller_id="agent-b", request_id="request-b")
    )
    assert_envelope(busy, status="busy", call_state=None)
    assert busy["call_state"] is None
    assert busy["call_id"] is None
    assert busy["handled_on"] is None
    assert busy["terminal_platform"] is None
    assert busy["event_seq"] is None


def test_expiration_settles_on_request_path_and_cannot_be_revived(
    tmp_path: Path,
) -> None:
    clock = make_clock()
    service = coordinator(tmp_path, clock)
    created = service.create_call(create_request())
    call_id = created["call_id"]
    clock.advance(60)

    settled = service.get_call(call_id)
    assert_envelope(settled, status="not_accepted")
    assert settled["reason"] == "ring_timeout"
    assert settled["human_action"] is False
    assert settled["event_seq"] == 1
    assert service.events_for(call_id) == [settled]

    late_accept = service.accept_call(accept_request(call_id))
    late_decline = service.decline_call(decline_request(call_id))
    assert late_accept == settled
    assert late_decline == settled
    assert service.events_for(call_id) == [settled]


def test_second_active_call_is_busy_and_terminal_actions_release_occupancy(
    tmp_path: Path,
) -> None:
    service = coordinator(tmp_path, make_clock())
    first = service.create_call(create_request())
    call_id = first["call_id"]
    busy = service.create_call(
        create_request(caller_id="agent-b", request_id="request-b")
    )
    assert_envelope(busy, status="busy", call_state=None)
    assert service.events_for(call_id) == []

    accepted = service.accept_call(accept_request(call_id))
    assert_envelope(accepted, status="opened", call_state="accepted")
    assert accepted["event_seq"] == 1
    assert accepted["owner_device_id"] == "iphone-1"
    assert accepted["owner_session_id"] == "browser-session-1"
    still_busy = service.create_call(
        create_request(caller_id="agent-c", request_id="request-c")
    )
    assert_envelope(still_busy, status="busy", call_state=None)


def test_decline_releases_occupancy_and_is_not_a_negative_confirmation(
    tmp_path: Path,
) -> None:
    service = coordinator(tmp_path, make_clock())
    created = service.create_call(create_request())
    declined = service.decline_call(decline_request(created["call_id"]))
    assert_envelope(declined, status="declined")
    assert declined["human_action"] is True
    assert declined["event_seq"] == 1
    assert "confirmed" not in declined
    next_call = service.create_call(
        create_request(caller_id="agent-b", request_id="request-b")
    )
    assert_envelope(next_call, status="ringing")


def test_create_request_id_replays_exactly_and_rejects_changed_contents(
    tmp_path: Path,
) -> None:
    service = coordinator(tmp_path, make_clock())
    request = create_request()
    original = service.create_call(request)
    replay = service.create_call(request)
    conflict = service.create_call(create_request(message="Changed request"))

    assert replay == original
    assert service.events_for(original["call_id"]) == []
    assert conflict == {
        "success": False,
        "status": "request_conflict",
        "needs_continuation": False,
    }


def test_accept_and_decline_action_ids_replay_without_duplicate_events(
    tmp_path: Path,
) -> None:
    service = coordinator(tmp_path, make_clock())
    created = service.create_call(create_request())
    call_id = created["call_id"]
    accepted = service.accept_call(accept_request(call_id))
    assert service.accept_call(accept_request(call_id)) == accepted
    conflict = service.accept_call(
        AcceptCall(
            call_id=call_id,
            action_id="accept-a",
            device_id="desktop-1",
            session_id="browser-session-1",
            handled_on="desktop",
            terminal_platform="windows",
        )
    )
    assert conflict["success"] is False
    assert conflict["status"] == "request_conflict"
    assert service.events_for(call_id) == [accepted]

    declined = service.create_call(
        create_request(caller_id="agent-b", request_id="request-b")
    )
    declined_id = declined["call_id"]
    result = service.decline_call(decline_request(declined_id))
    assert service.decline_call(decline_request(declined_id)) == result
    assert service.events_for(declined_id) == [result]


def test_reopen_settles_expired_before_new_create_releases_busy(tmp_path: Path) -> None:
    clock = make_clock()
    first = coordinator(tmp_path, clock)
    created = first.create_call(create_request())
    first.close()
    clock.advance(60)

    reopened = coordinator(tmp_path, clock)
    settled = reopened.recover_and_settle()
    assert settled == [reopened.get_call(created["call_id"])]
    assert settled[0]["status"] == "not_accepted"
    new_call = reopened.create_call(
        create_request(caller_id="agent-b", request_id="request-b")
    )
    assert_envelope(new_call, status="ringing")


def test_accept_and_decline_races_have_exactly_one_legal_winner(tmp_path: Path) -> None:
    service = coordinator(tmp_path, make_clock())
    created = service.create_call(create_request())
    call_id = created["call_id"]
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def accept() -> None:
        barrier.wait()
        outcomes.append(service.accept_call(accept_request(call_id)))

    def decline() -> None:
        barrier.wait()
        outcomes.append(service.decline_call(decline_request(call_id)))

    first, second = Thread(target=accept), Thread(target=decline)
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()

    current = service.get_call(call_id)
    assert current["call_state"] in {"accepted", "declined"}
    assert len(service.events_for(call_id)) == 1
    if current["call_state"] == "accepted":
        assert current["owner_device_id"] == "iphone-1"
        assert all(item["status"] != "declined" for item in outcomes)
    else:
        assert current["owner_device_id"] is None
        assert all(item["status"] != "opened" for item in outcomes)


def test_two_accepts_have_one_owner_and_one_opened_event(tmp_path: Path) -> None:
    service = coordinator(tmp_path, make_clock())
    created = service.create_call(create_request())
    call_id = created["call_id"]
    barrier = Barrier(3)
    outcomes: list[dict[str, Any]] = []

    def accept(device_id: str, session_id: str, action_id: str) -> None:
        barrier.wait()
        outcomes.append(
            service.accept_call(
                AcceptCall(
                    call_id=call_id,
                    action_id=action_id,
                    device_id=device_id,
                    session_id=session_id,
                    handled_on="desktop",
                    terminal_platform="windows",
                )
            )
        )

    first = Thread(target=accept, args=("device-a", "session-a", "accept-a"))
    second = Thread(target=accept, args=("device-b", "session-b", "accept-b"))
    first.start()
    second.start()
    barrier.wait()
    first.join()
    second.join()

    current = service.get_call(call_id)
    assert current["call_state"] == "accepted"
    assert (current["owner_device_id"], current["owner_session_id"]) in {
        ("device-a", "session-a"),
        ("device-b", "session-b"),
    }
    assert len(service.events_for(call_id)) == 1
    assert service.events_for(call_id)[0]["status"] == "opened"
    assert len({item["owner_device_id"] for item in outcomes if item["success"]}) == 1
