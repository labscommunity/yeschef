"""Rooms: membership, N-party messaging, floor control, and policy bounds."""

from __future__ import annotations

import pytest

from yeschef.hub import Store
from yeschef.models import AgentKind, HubError, RoomPolicy, TurnPolicy


@pytest.fixture
def fleet(store: Store) -> Store:
    store.register_agent("alpha")
    store.register_agent("beta")
    store.register_agent("gamma")
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    return store


def test_dm_is_a_two_party_room_and_is_reused(fleet: Store) -> None:
    first = fleet.get_or_create_dm("claude:main", "alpha")
    second = fleet.get_or_create_dm("alpha", "claude:main")
    assert first.id == second.id
    assert set(first.members) == {"claude:main", "alpha"}


def test_n_party_room_delivers_to_everyone_but_the_sender(fleet: Store) -> None:
    room = fleet.create_room("standup", "claude:main", ["alpha", "beta", "gamma"])
    fleet.post_message(room.id, "alpha", "morning")

    for member in ("claude:main", "beta", "gamma"):
        messages, _ = fleet.fetch_inbox(member)
        assert [m["body"] for m in messages] == ["morning"]

    own, _ = fleet.fetch_inbox("alpha")
    assert own == []


def test_inbox_cursor_advances_and_does_not_repeat(fleet: Store) -> None:
    room = fleet.create_room("r", "claude:main", ["alpha"])
    fleet.post_message(room.id, "alpha", "one")
    first_batch, cursor = fleet.fetch_inbox("claude:main")
    assert len(first_batch) == 1

    empty, cursor2 = fleet.fetch_inbox("claude:main", cursor)
    assert empty == []

    fleet.post_message(room.id, "alpha", "two")
    second_batch, _ = fleet.fetch_inbox("claude:main", cursor2)
    assert [m["body"] for m in second_batch] == ["two"]


def test_inbox_spans_rooms_and_can_be_filtered(fleet: Store) -> None:
    room_a = fleet.create_room("a", "claude:main", ["alpha"])
    room_b = fleet.create_room("b", "claude:main", ["beta"])
    fleet.post_message(room_a.id, "alpha", "from a")
    fleet.post_message(room_b.id, "beta", "from b")

    everything, _ = fleet.fetch_inbox("claude:main")
    assert {m["body"] for m in everything} == {"from a", "from b"}

    just_a, _ = fleet.fetch_inbox("claude:main", room_id=room_a.id)
    assert [m["body"] for m in just_a] == ["from a"]


def test_non_members_cannot_post(fleet: Store) -> None:
    room = fleet.create_room("private", "claude:main", ["alpha"])
    with pytest.raises(HubError) as exc:
        fleet.post_message(room.id, "beta", "let me in")
    assert exc.value.code == "forbidden"


def test_open_rooms_auto_join_the_sender(fleet: Store) -> None:
    room = fleet.create_room("lobby", "claude:main", [], open_room=True)
    fleet.post_message(room.id, "gamma", "hello")
    assert "gamma" in fleet.require_room(room.id).members


def test_mentions_are_parsed_from_the_body(fleet: Store) -> None:
    room = fleet.create_room("r", "claude:main", ["alpha", "beta"])
    message = fleet.post_message(room.id, "claude:main", "@alpha please take this")
    assert message.mentions == ["alpha"]


def test_idempotent_post_by_client_message_id(fleet: Store) -> None:
    room = fleet.create_room("r", "claude:main", ["alpha"])
    first = fleet.post_message(room.id, "alpha", "once", client_msg_id="c1")
    second = fleet.post_message(room.id, "alpha", "once", client_msg_id="c1")
    assert first.id == second.id
    assert len(fleet.fetch_messages(room.id)) == 1


def test_seq_is_monotonic_per_room(fleet: Store) -> None:
    room = fleet.create_room("r", "claude:main", ["alpha"])
    seqs = [fleet.post_message(room.id, "alpha", f"m{i}").seq for i in range(5)]
    assert seqs == [1, 2, 3, 4, 5]


# ------------------------------------------------------------- floor control


def test_round_robin_grants_the_floor_to_the_first_worker(fleet: Store) -> None:
    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    assert fleet._floor_holder(room.id) == "alpha"


def test_round_robin_blocks_out_of_turn_workers(fleet: Store) -> None:
    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    with pytest.raises(HubError) as exc:
        fleet.post_message(room.id, "beta", "jumping in")
    assert exc.value.code == "conflict"


def test_floor_passes_around_the_ring(fleet: Store) -> None:
    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta", "gamma"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    fleet.post_message(room.id, "alpha", "a")
    assert fleet._floor_holder(room.id) == "beta"
    fleet.post_message(room.id, "beta", "b")
    assert fleet._floor_holder(room.id) == "gamma"
    fleet.post_message(room.id, "gamma", "c")
    assert fleet._floor_holder(room.id) == "alpha"


def test_claude_interjects_out_of_turn_and_re_anchors(fleet: Store) -> None:
    """A Claude session never waits for the floor; its post resets the ring."""
    room = fleet.create_room(
        "dialogue",
        "claude:main",
        ["alpha", "beta"],
        RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
    )
    fleet.post_message(room.id, "alpha", "a")
    assert fleet._floor_holder(room.id) == "beta"

    fleet.post_message(room.id, "claude:main", "actually, focus on X")
    assert fleet._floor_holder(room.id) == "alpha"


# ----------------------------------------------------------------- policies


def test_stop_phrase_archives_the_room(fleet: Store) -> None:
    room = fleet.create_room(
        "bounded", "claude:main", ["alpha"], RoomPolicy(stop_phrase="TASK COMPLETE")
    )
    fleet.post_message(room.id, "alpha", "still working")
    assert not fleet.require_room(room.id).archived

    fleet.post_message(room.id, "alpha", "finished — TASK COMPLETE")
    closed = fleet.require_room(room.id)
    assert closed.archived and closed.archived_reason == "stop_phrase"


def test_max_messages_archives_the_room(fleet: Store) -> None:
    room = fleet.create_room("bounded", "claude:main", ["alpha"], RoomPolicy(max_messages=3))
    for i in range(3):
        fleet.post_message(room.id, "alpha", f"m{i}")
    closed = fleet.require_room(room.id)
    assert closed.archived and closed.archived_reason == "max_messages"


def test_token_budget_archives_the_room(fleet: Store) -> None:
    room = fleet.create_room("bounded", "claude:main", ["alpha"], RoomPolicy(max_total_tokens=10))
    fleet.post_message(room.id, "alpha", "x" * 200, tokens=50)
    closed = fleet.require_room(room.id)
    assert closed.archived and closed.archived_reason == "max_total_tokens"


def test_archived_rooms_reject_further_messages(fleet: Store) -> None:
    room = fleet.create_room("r", "claude:main", ["alpha"])
    fleet.archive_room(room.id, "done")
    with pytest.raises(HubError) as exc:
        fleet.post_message(room.id, "alpha", "hello?")
    assert exc.value.code == "conflict"


def test_idle_rooms_are_archived_by_the_sweep(fleet: Store) -> None:
    room = fleet.create_room("idle", "claude:main", ["alpha"], RoomPolicy(idle_timeout_s=0.0))
    stats = fleet.sweep()
    assert stats["rooms_archived"] == 1
    assert fleet.require_room(room.id).archived_reason == "idle_timeout"


def test_identity_rename_carries_membership(fleet: Store) -> None:
    room = fleet.create_room("r", "claude:main", ["alpha"])
    fleet.rename_identity("claude:main", "claude:mac-mini")
    assert "claude:mac-mini" in fleet.require_room(room.id).members
    fleet.post_message(room.id, "alpha", "still reachable")
    messages, _ = fleet.fetch_inbox("claude:mac-mini")
    assert [m["body"] for m in messages] == ["still reachable"]
