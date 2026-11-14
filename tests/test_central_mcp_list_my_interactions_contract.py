"""Contract: an agent can rediscover its own open rounds after context loss.

The continuation protocol requires the agent to carry ``interaction_id`` /
``call_id`` / ``after_event_seq`` in its own context.  When that context is
compacted or reset, the round is otherwise orphaned — durable on the server,
unreachable by the agent.  ``list_my_interactions`` closes that gap: it returns
the caller's active rounds, scoped to the caller, with enough to resume.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from central_mcp_waiting_lifecycle import create_app


class Clock:
    def __init__(self) -> None:
        self.now = 1_700_000_000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


async def no_wait(_: float) -> None:
    return None


def make_app(tmp_path: Path, clock: Clock) -> Any:
    return create_app(
        database_path=tmp_path / "lifecycle.sqlite3",
        credentials={"agent-a": "token-a", "agent-b": "token-b"},
        clock=clock, notify=lambda _e: None, wait=no_wait,
    )


def rpc(app: Any, token: str, name: str, arguments: dict[str, object], rid: int = 1) -> dict[str, object]:
    async def run() -> httpx.Response:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://central.test") as client:
            return await client.post("/mcp", headers={"authorization": f"Bearer {token}"}, json={
                "jsonrpc": "2.0", "id": rid, "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            })
    response = asyncio.run(run())
    assert response.status_code == 200, response.text
    return dict(response.json()["result"]["structuredContent"])


def _ids(result: dict[str, object]) -> set[tuple[str, str]]:
    return {(str(e["interaction_id"]), str(e["call_id"])) for e in result["result"]["interactions"]}


def test_list_my_interactions_returns_my_open_round_and_is_scoped(tmp_path: Path) -> None:
    clock = Clock()
    app = make_app(tmp_path, clock)
    mine = rpc(app, "token-a", "get_user_input", {"prompt": "q"})

    a_view = rpc(app, "token-a", "list_my_interactions", {})
    assert a_view["success"] is True
    assert (str(mine["interaction_id"]), str(mine["call_id"])) in _ids(a_view)

    # The entry carries enough to resume, and names the originating tool.
    (entry,) = a_view["result"]["interactions"]
    assert entry["tool"] == "get_user_input"
    assert entry["call_state"] == "ringing"

    # Another caller sees none of mine: the listing is scoped to the caller.
    b_view = rpc(app, "token-b", "list_my_interactions", {})
    assert _ids(b_view) == set()


def test_list_my_interactions_omits_settled_rounds(tmp_path: Path) -> None:
    clock = Clock()
    app = make_app(tmp_path, clock)
    call = rpc(app, "token-a", "get_user_input", {"prompt": "q"})

    rpc(app, "token-a", "cancel_interaction",
        {"interaction_id": call["interaction_id"], "call_id": call["call_id"]})

    view = rpc(app, "token-a", "list_my_interactions", {})
    assert (str(call["interaction_id"]), str(call["call_id"])) not in _ids(view)
