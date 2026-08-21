"""Invariants that must hold no matter what a caller asks for.

Every test here corresponds to a bug that was live at some point. The spec mandate is
blunt: no conversation is unbounded, and no agent can act as another.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from fastmcp import Client

from farmteam.hub import HubConfig, Store
from farmteam.hub.mcp_server import build_mcp
from farmteam.models import (
    DEFAULT_MAX_ROOM_MESSAGES,
    AgentKind,
    HubError,
    ReplyWhen,
    RoomPolicy,
)

from .live import live_hub
from .test_integration import start_agent, stop_agent, wait_for


@pytest.fixture
def fleet(store: Store) -> Store:
    store.register_agent("alpha")
    store.register_agent("beta")
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    return store


# ------------------------------------------------- no unbounded conversation


def test_every_room_gets_a_message_backstop(fleet: Store) -> None:
    room = fleet.create_room("unbounded?", "claude:main", ["alpha"])
    assert room.policy.max_messages == DEFAULT_MAX_ROOM_MESSAGES
    assert room.policy.idle_timeout_s is not None


def test_a_caller_can_raise_but_not_remove_the_backstop(fleet: Store) -> None:
    room = fleet.create_room("long", "claude:main", ["alpha"], RoomPolicy(max_messages=1000))
    assert room.policy.max_messages == 1000


def test_the_backstop_actually_archives(fleet: Store) -> None:
    room = fleet.create_room("chatty", "claude:main", ["alpha"], RoomPolicy(max_messages=3))
    for i in range(5):
        try:
            fleet.post_message(room.id, "alpha", f"m{i}")
        except HubError as exc:
            assert exc.code == "conflict"
            break
    assert fleet.require_room(room.id).archived


async def test_two_always_reply_agents_cannot_talk_forever() -> None:
    """The runaway case: both agents reply to everything, in a free-turn room."""
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:

            def chatty(label: str):
                def respond(system, turns):
                    return f"{label}: go on"

                return respond

            first = await start_agent(hub, "alpha", chatty("alpha"), reply_when=ReplyWhen.ALWAYS)
            second = await start_agent(hub, "beta", chatty("beta"), reply_when=ReplyWhen.ALWAYS)
            try:
                created = await claude.call_tool(
                    "create_room",
                    {"topic": "runaway", "participants": ["alpha", "beta"], "max_messages": 12},
                )
                room_id = created.data["room"]["id"]
                await claude.call_tool("post", {"room": room_id, "message": "hello both"})

                await wait_for(lambda: hub.store.require_room(room_id).archived, timeout=25.0)
                room = hub.store.require_room(room_id)
                assert room.archived_reason == "max_messages"

                # And it stays stopped: no straggler can reopen the conversation.
                await asyncio.sleep(1.0)
                assert len(hub.store.fetch_messages(room_id, limit=1000)) <= 13
            finally:
                await stop_agent(*first)
                await stop_agent(*second)


def test_an_archived_dm_rotates_instead_of_wedging(fleet: Store) -> None:
    """A DM that hits a backstop must not lock the pair out of talking again."""
    first = fleet.get_or_create_dm("claude:main", "alpha")
    fleet.archive_room(first.id, "max_messages")

    second = fleet.get_or_create_dm("claude:main", "alpha")
    assert second.id != first.id
    assert not second.archived
    fleet.post_message(second.id, "alpha", "still reachable")


# --------------------------------------------------------------- identity


def test_re_registering_a_name_requires_its_token(fleet: Store) -> None:
    _, token = fleet.register_agent("worker-1")

    with pytest.raises(HubError) as exc:
        fleet.register_agent("worker-1")
    assert exc.value.code == "forbidden"

    with pytest.raises(HubError):
        fleet.register_agent("worker-1", presented_token="not-the-token")

    agent, new_token = fleet.register_agent("worker-1", presented_token=token)
    assert agent.name == "worker-1"
    assert new_token != token


def test_a_privileged_caller_can_reclaim_a_name(fleet: Store) -> None:
    fleet.register_agent("worker-1")
    _, token = fleet.register_agent("worker-1", privileged=True)
    assert fleet.verify_token("worker-1", token)


async def test_hijack_is_rejected_over_http() -> None:
    async with live_hub() as hub:
        async with httpx.AsyncClient(base_url=hub.url, timeout=5.0) as client:
            first = await client.post("/api/v1/agents/register", json={"name": "miner-qwen"})
            token = first.json()["token"]

            hijack = await client.post("/api/v1/agents/register", json={"name": "miner-qwen"})
            assert hijack.status_code == 403
            assert hijack.json()["error"]["code"] == "forbidden"

            # The real agent still works.
            beat = await client.post(
                "/api/v1/agents/miner-qwen/heartbeat",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert beat.status_code == 200

            # And it can rotate its own token by presenting the current one.
            again = await client.post(
                "/api/v1/agents/register",
                json={"name": "miner-qwen"},
                headers={"Authorization": f"Bearer {token}"},
            )
            assert again.status_code == 200


# ---------------------------------------------------------- authorization


def test_invite_only_rooms_reject_uninvited_agents(fleet: Store) -> None:
    room = fleet.create_room("private", "claude:main", ["alpha"])
    assert room.open is False

    with pytest.raises(HubError) as exc:
        fleet.join_room(room.id, "beta")
    assert exc.value.code == "forbidden"

    # The operator (Claude session) may still pull someone in.
    joined = fleet.join_room(room.id, "beta", privileged=True)
    assert "beta" in joined.members


def test_open_rooms_still_accept_anyone(fleet: Store) -> None:
    room = fleet.create_room("lobby", "claude:main", [], open_room=True)
    assert "beta" in fleet.join_room(room.id, "beta").members


def test_only_members_can_archive_a_room(fleet: Store) -> None:
    room = fleet.create_room("private", "claude:main", ["alpha"])
    with pytest.raises(HubError) as exc:
        fleet.archive_room(room.id, "rude", by="beta")
    assert exc.value.code == "forbidden"
    assert not fleet.require_room(room.id).archived

    fleet.archive_room(room.id, "done", by="alpha")
    assert fleet.require_room(room.id).archived


def test_only_the_requester_or_assignee_can_cancel(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")

    with pytest.raises(HubError) as exc:
        fleet.cancel_task(task.id, "beta")
    assert exc.value.code == "forbidden"
    assert not fleet.require_task(task.id).state.terminal

    fleet.cancel_task(task.id, "alpha")
    assert fleet.require_task(task.id).state == "cancelled"


async def test_reads_require_a_token_once_auth_is_configured() -> None:
    async with live_hub(admin_token="ADMIN") as hub:
        admin = {"Authorization": "Bearer ADMIN"}
        async with httpx.AsyncClient(base_url=hub.url, timeout=5.0) as client:
            registered = await client.post(
                "/api/v1/agents/register", json={"name": "worker"}, headers=admin
            )
            worker = {"Authorization": f"Bearer {registered.json()['token']}"}
            await client.post(
                "/api/v1/tasks",
                headers=worker,
                json={
                    "as_agent": "worker",
                    "title": "t",
                    "spec": "SENSITIVE",
                    "assignee": "worker",
                },
            )

            for path in ("/api/v1/tasks", "/api/v1/agents"):
                anonymous = await client.get(path)
                assert anonymous.status_code == 401, path

            allowed = await client.get("/api/v1/tasks", headers=worker)
            assert allowed.status_code == 200
            assert allowed.json()["tasks"][0]["spec"] == "SENSITIVE"


async def test_open_mode_still_works_with_no_tokens_configured() -> None:
    """Zero-config on a trusted LAN stays usable."""
    async with live_hub() as hub:
        async with httpx.AsyncClient(base_url=hub.url, timeout=5.0) as client:
            assert (await client.get("/api/v1/agents")).status_code == 200


# ------------------------------------------------- dialogues cannot deadlock


def test_the_sweep_hands_on_a_floor_its_holder_cannot_use(fleet: Store) -> None:
    """A dropped grant or a dead agent must not freeze a dialogue until idle timeout."""
    from farmteam.models import TurnPolicy

    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    assert fleet._floor_holder(room.id) == "alpha"

    with fleet._lock:
        fleet._db.execute("UPDATE agents SET last_seen = 0 WHERE name = ?", ("alpha",))
        fleet._db.commit()

    stats = fleet.sweep()
    assert stats["floors_recovered"] == 1
    assert fleet._floor_holder(room.id) == "beta"


def test_an_unseeded_ring_still_enforces_turns(fleet: Store) -> None:
    """A room built before its agents registered must not skip floor control."""
    from farmteam.models import TurnPolicy

    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    with fleet._lock:
        fleet._db.execute("UPDATE rooms SET floor_holder = NULL WHERE id = ?", (room.id,))
        fleet._db.commit()

    with pytest.raises(HubError) as exc:
        fleet.post_message(room.id, "beta", "jumping the queue")
    assert exc.value.code == "conflict"

    assert fleet.post_message(room.id, "alpha", "my turn").seq == 1


def test_yield_floor_passes_the_turn_without_speaking(fleet: Store) -> None:
    from farmteam.models import TurnPolicy

    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    fleet.yield_floor(room.id, "alpha")
    assert fleet._floor_holder(room.id) == "beta"
    assert fleet.fetch_messages(room.id) == []

    with pytest.raises(HubError):
        fleet.yield_floor(room.id, "alpha")  # no longer holds it


# ------------------------------------------------------- context windows


def test_agents_see_the_end_of_a_long_conversation_not_the_start(fleet: Store) -> None:
    room = fleet.create_room("long", "claude:main", ["alpha"], RoomPolicy(max_messages=10_000))
    for i in range(250):
        fleet.post_message(room.id, "claude:main", f"m{i}")

    head = fleet.fetch_messages(room.id, limit=30)
    assert head[0].body == "m0"

    tail = fleet.fetch_messages(room.id, limit=30, tail=True)
    assert tail[-1].body == "m249"
    assert tail[0].body == "m220"
    assert [m.seq for m in tail] == sorted(m.seq for m in tail)
