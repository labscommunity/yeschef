"""The Claude Code face: MCP tools for dispatch, status, and conversation."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from fastmcp import Client

from cascadia_tasks.hub import HubConfig, Store
from cascadia_tasks.hub.events import EventBus
from cascadia_tasks.hub.mcp_server import build_mcp
from cascadia_tasks.models import TaskState

EXPECTED_TOOLS = {
    "set_identity",
    "whoami",
    "list_agents",
    "send_message",
    "post",
    "fetch_messages",
    "create_room",
    "join_room",
    "leave_room",
    "list_rooms",
    "room_transcript",
    "archive_room",
    "start_dialogue",
    "submit_task",
    "task_status",
    "task_result",
    "list_tasks",
    "cancel_task",
    "provide_input",
    "task_room",
}


@pytest.fixture
async def mcp_client() -> AsyncIterator[tuple[Client, Store]]:
    store = Store(":memory:", EventBus())
    store.register_agent("alpha", node="nuc-alpha", backend="ollama/qwen3-8b", tags=["tier:fast"])
    store.register_agent("beta", node="miner", backend="vllm/qwen3-8b", tags=["tier:reasoning"])
    mcp = build_mcp(store, HubConfig(db_path=":memory:", default_identity="claude:test"))
    async with Client(mcp) as client:
        yield client, store
    store.close()


async def test_every_documented_tool_is_exposed(mcp_client) -> None:
    client, _ = mcp_client
    names = {tool.name for tool in await client.list_tools()}
    assert EXPECTED_TOOLS <= names


async def test_list_agents_shows_the_fleet(mcp_client) -> None:
    client, _ = mcp_client
    result = await client.call_tool("list_agents", {})
    names = {agent["name"] for agent in result.data["agents"]}
    assert {"alpha", "beta"} <= names
    assert result.data["me"] == "claude:test"


async def test_submit_task_returns_immediately_with_an_id(mcp_client) -> None:
    """Use case A: dispatch without blocking."""
    client, store = mcp_client
    result = await client.call_tool(
        "submit_task", {"title": "summarize", "spec": "summarize the log", "assignee": "alpha"}
    )
    task_id = result.data["task_id"]
    assert result.data["task"]["state"] == "queued"
    assert store.require_task(task_id).created_by == "claude:test"


async def test_task_status_reports_progress_at_any_time(mcp_client) -> None:
    """Use case B: check on a running task whenever."""
    client, store = mcp_client
    submitted = await client.call_tool(
        "submit_task", {"title": "t", "spec": "s", "assignee": "alpha"}
    )
    task_id = submitted.data["task_id"]

    store.claim_task(task_id, "alpha")
    store.update_progress(task_id, "alpha", pct=60.0, message="two of three done")

    status = await client.call_tool("task_status", {"task_id": task_id})
    assert status.data["task"]["state"] == "working"
    assert status.data["task"]["progress"] == {"pct": 60.0, "message": "two of three done"}
    assert [event["kind"] for event in status.data["events"]] == [
        "submitted",
        "claimed",
        "progress",
    ]


async def test_task_result_waits_for_terminal_state(mcp_client) -> None:
    client, store = mcp_client
    submitted = await client.call_tool(
        "submit_task", {"title": "t", "spec": "s", "assignee": "alpha"}
    )
    task_id = submitted.data["task_id"]

    pending = await client.call_tool("task_result", {"task_id": task_id})
    assert pending.data["ready"] is False

    store.claim_task(task_id, "alpha")
    store.complete_task(task_id, "alpha", {"text": "done"})

    ready = await client.call_tool("task_result", {"task_id": task_id})
    assert ready.data["ready"] is True
    assert ready.data["result"] == {"text": "done"}


async def test_unknown_agent_is_a_structured_error_not_a_crash(mcp_client) -> None:
    client, _ = mcp_client
    result = await client.call_tool("submit_task", {"title": "t", "spec": "s", "assignee": "nope"})
    assert result.data["error"]["code"] == "not_found"


async def test_cancel_task_from_mcp(mcp_client) -> None:
    client, store = mcp_client
    submitted = await client.call_tool(
        "submit_task", {"title": "t", "spec": "s", "assignee": "alpha"}
    )
    task_id = submitted.data["task_id"]
    result = await client.call_tool("cancel_task", {"task_id": task_id})
    assert result.data["task"]["state"] == "cancelled"
    assert store.require_task(task_id).state is TaskState.CANCELLED


async def test_send_message_creates_a_dm_and_fetch_reads_the_reply(mcp_client) -> None:
    """Multi-turn Claude Code <-> local agent."""
    client, store = mcp_client
    sent = await client.call_tool("send_message", {"to": "alpha", "message": "what is 2+2?"})
    room_id = sent.data["room_id"]

    store.post_message(room_id, "alpha", "4")

    inbox = await client.call_tool("fetch_messages", {})
    assert [m["body"] for m in inbox.data["messages"]] == ["4"]

    # The cursor is remembered, so a second call does not repeat the message.
    again = await client.call_tool("fetch_messages", {})
    assert again.data["messages"] == []

    store.post_message(room_id, "alpha", "anything else?")
    follow_up = await client.call_tool("fetch_messages", {})
    assert [m["body"] for m in follow_up.data["messages"]] == ["anything else?"]


async def test_fetch_messages_can_wait_briefly(mcp_client) -> None:
    client, _ = mcp_client
    result = await client.call_tool("fetch_messages", {"wait_s": 0.2})
    assert result.data["messages"] == []


async def test_create_room_and_post_multi_party(mcp_client) -> None:
    client, store = mcp_client
    created = await client.call_tool(
        "create_room", {"topic": "design review", "participants": ["alpha", "beta"]}
    )
    room_id = created.data["room"]["id"]
    assert set(created.data["room"]["members"]) == {"claude:test", "alpha", "beta"}

    await client.call_tool("post", {"room": room_id, "message": "@alpha start us off"})
    store.post_message(room_id, "alpha", "here is my take")
    store.post_message(room_id, "beta", "and mine")

    transcript = await client.call_tool("room_transcript", {"room": room_id})
    assert [m["sender"] for m in transcript.data["messages"]] == ["claude:test", "alpha", "beta"]


async def test_start_dialogue_creates_a_bounded_round_robin_room(mcp_client) -> None:
    """Agents converse autonomously; the hub bounds the conversation."""
    client, store = mcp_client
    result = await client.call_tool(
        "start_dialogue",
        {
            "participants": ["alpha", "beta"],
            "goal": "agree on a caching strategy",
            "max_messages": 6,
            "stop_phrase": "AGREED",
        },
    )
    room_id = result.data["room_id"]
    room = store.require_room(room_id)

    assert room.policy.turn_policy == "round_robin"
    assert room.policy.max_messages == 6
    assert room.policy.stop_phrase == "AGREED"
    assert result.data["seed_message"]["body"] == "agree on a caching strategy"
    assert store._floor_holder(room_id) == "alpha"


async def test_dialogue_stops_itself_on_the_stop_phrase(mcp_client) -> None:
    client, store = mcp_client
    result = await client.call_tool(
        "start_dialogue",
        {"participants": ["alpha", "beta"], "goal": "settle it", "stop_phrase": "AGREED"},
    )
    room_id = result.data["room_id"]

    store.post_message(room_id, "alpha", "I propose write-through")
    store.post_message(room_id, "beta", "fine by me — AGREED")

    room = store.require_room(room_id)
    assert room.archived and room.archived_reason == "stop_phrase"

    blocked = await client.call_tool("post", {"room": room_id, "message": "one more thing"})
    assert blocked.data["error"]["code"] == "conflict"


async def test_observer_can_interject_in_a_dialogue(mcp_client) -> None:
    client, store = mcp_client
    result = await client.call_tool(
        "start_dialogue", {"participants": ["alpha", "beta"], "goal": "discuss"}
    )
    room_id = result.data["room_id"]
    store.post_message(room_id, "alpha", "point one")
    assert store._floor_holder(room_id) == "beta"

    await client.call_tool("post", {"room": room_id, "message": "hold on, consider X"})
    assert store._floor_holder(room_id) == "alpha"


async def test_set_identity_renames_the_session(mcp_client) -> None:
    client, store = mcp_client
    await client.call_tool("send_message", {"to": "alpha", "message": "hi"})
    renamed = await client.call_tool("set_identity", {"label": "mac-mini-main"})
    assert renamed.data["identity"] == "claude:mac-mini-main"

    who = await client.call_tool("whoami", {})
    assert who.data["identity"] == "claude:mac-mini-main"

    rooms = await client.call_tool("list_rooms", {})
    assert len(rooms.data["rooms"]) == 1
    assert store.get_agent("claude:mac-mini-main") is not None


async def test_task_room_opens_a_conversation_on_a_running_task(mcp_client) -> None:
    """A dispatched task becomes multi-turn by growing a room."""
    client, store = mcp_client
    submitted = await client.call_tool(
        "submit_task", {"title": "build", "spec": "build the thing", "assignee": "alpha"}
    )
    task_id = submitted.data["task_id"]
    store.claim_task(task_id, "alpha")

    opened = await client.call_tool("task_room", {"task_id": task_id})
    room_id = opened.data["room"]["id"]
    assert set(opened.data["room"]["members"]) >= {"claude:test", "alpha"}

    await client.call_tool("post", {"room": room_id, "message": "also handle the edge case"})
    assert store.fetch_messages(room_id)[-1].body == "also handle the edge case"


async def test_input_required_flow_from_mcp(mcp_client) -> None:
    client, store = mcp_client
    submitted = await client.call_tool(
        "submit_task", {"title": "deploy", "spec": "deploy it", "assignee": "alpha"}
    )
    task_id = submitted.data["task_id"]
    store.claim_task(task_id, "alpha")
    store.request_input(task_id, "alpha", "staging or prod?")

    status = await client.call_tool("task_status", {"task_id": task_id})
    assert status.data["task"]["state"] == "input_required"

    answered = await client.call_tool("provide_input", {"task_id": task_id, "message": "staging"})
    assert answered.data["task"]["state"] == "working"


async def test_list_tasks_filters_by_state(mcp_client) -> None:
    client, store = mcp_client
    first = await client.call_tool("submit_task", {"title": "a", "spec": "s", "assignee": "alpha"})
    await client.call_tool("submit_task", {"title": "b", "spec": "s", "assignee": "beta"})
    store.claim_task(first.data["task_id"], "alpha")
    store.complete_task(first.data["task_id"], "alpha", {"text": "ok"})

    completed = await client.call_tool("list_tasks", {"state": "completed"})
    assert [t["title"] for t in completed.data["tasks"]] == ["a"]

    queued = await client.call_tool("list_tasks", {"state": "queued"})
    assert [t["title"] for t in queued.data["tasks"]] == ["b"]
