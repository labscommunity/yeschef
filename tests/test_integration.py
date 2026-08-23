"""End-to-end: Claude Code (MCP) → hub → live harness → local model → back.

These are the acceptance tests for the two use cases the project exists for:
dispatch work to a background local agent, and check on it at any time.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import pytest
from fastmcp import Client

from farmteam.agent import AgentConfig, Harness
from farmteam.agent.backends.base import Turn
from farmteam.hub import HubConfig
from farmteam.hub.mcp_server import build_mcp
from farmteam.models import ReplyWhen
from farmteam.tools.executor import ToolsConfig

from .live import LiveHub, live_hub
from .mock_backend import MockBackend, tool_then_answer

SETTLE_TIMEOUT_S = 15.0


async def start_agent(
    hub: LiveHub,
    name: str,
    responder,
    *,
    tags: list[str] | None = None,
    reply_when: ReplyWhen = ReplyWhen.MENTIONED,
    tools: ToolsConfig | None = None,
) -> tuple[Harness, asyncio.Task]:
    config = AgentConfig(
        name=name,
        hub=hub.url,
        node="test-node",
        tags=tags or [],
        backend={"type": "mock", "model": "mock-model", "base_url": "http://unused"},
        reply_when=reply_when,
        tools=tools or ToolsConfig(),
    )
    harness = Harness(config, backend=MockBackend(responder))
    runner = asyncio.create_task(harness.run())
    for _ in range(100):
        agent = hub.store.get_agent(name)
        if agent is not None and hub.store.bus.subscriber_count(name):
            break
        await asyncio.sleep(0.05)
    else:  # pragma: no cover
        raise RuntimeError(f"agent {name} did not come online")
    return harness, runner


async def stop_agent(harness: Harness, runner: asyncio.Task) -> None:
    runner.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await runner
    await harness.aclose()


async def wait_for(predicate, timeout: float = SETTLE_TIMEOUT_S):
    """Poll until predicate returns a truthy value, like a human checking status."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        value = predicate()
        if value:
            return value
        await asyncio.sleep(0.05)
    raise AssertionError("condition not reached before timeout")


@pytest.fixture
async def fleet() -> AsyncIterator[tuple[LiveHub, Client]]:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            yield hub, claude


# --------------------------------------------------------------- use case A + B


async def test_dispatch_a_task_and_check_status_until_it_completes(fleet) -> None:
    hub, claude = fleet
    harness, runner = await start_agent(
        hub, "alpha", lambda system, turns: "The log shows three OOM kills.", tags=["tier:fast"]
    )
    try:
        submitted = await claude.call_tool(
            "submit_task",
            {"title": "read the log", "spec": "summarize /var/log/app.log", "assignee": "alpha"},
        )
        task_id = submitted.data["task_id"]
        assert submitted.data["task"]["state"] == "queued"

        async def completed() -> dict | None:
            status = await claude.call_tool("task_status", {"task_id": task_id})
            return status.data if status.data["task"]["state"] == "completed" else None

        deadline = asyncio.get_running_loop().time() + SETTLE_TIMEOUT_S
        final = None
        while asyncio.get_running_loop().time() < deadline:
            final = await completed()
            if final:
                break
            await asyncio.sleep(0.1)
        assert final is not None, "task never completed"

        result = await claude.call_tool("task_result", {"task_id": task_id})
        assert result.data["ready"] is True
        assert "OOM" in result.data["result"]["text"]
        assert result.data["result"]["model"] == "mock-model"

        kinds = [event["kind"] for event in final["events"]]
        assert kinds[0] == "submitted"
        assert "claimed" in kinds and kinds[-1] == "completed"
    finally:
        await stop_agent(harness, runner)


async def test_selector_dispatch_reaches_a_tagged_agent(fleet) -> None:
    hub, claude = fleet
    harness, runner = await start_agent(
        hub, "reasoner", lambda system, turns: "42", tags=["tier:reasoning"]
    )
    try:
        submitted = await claude.call_tool(
            "submit_task",
            {"title": "think", "spec": "what is the answer?", "selector": "tier:reasoning"},
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "completed")
        assert hub.store.require_task(task_id).assignee == "reasoner"
    finally:
        await stop_agent(harness, runner)


async def test_only_one_agent_wins_a_selector_task(fleet) -> None:
    """Both agents are told; the hub guarantees exactly one does the work."""
    hub, claude = fleet
    first = await start_agent(hub, "alpha", lambda s, t: "alpha did it", tags=["pool"])
    second = await start_agent(hub, "beta", lambda s, t: "beta did it", tags=["pool"])
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "race", "spec": "do it once", "selector": "pool"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "completed")

        task = hub.store.require_task(task_id)
        assert task.assignee in {"alpha", "beta"}
        assert task.attempts == 1
        winner = task.result["text"]
        assert winner in {"alpha did it", "beta did it"}
    finally:
        await stop_agent(*first)
        await stop_agent(*second)


async def test_task_failure_is_reported_not_swallowed(fleet) -> None:
    hub, claude = fleet

    def explode(system: str, turns: list[Turn]) -> str:
        raise RuntimeError("model server unreachable")

    harness, runner = await start_agent(hub, "alpha", explode)
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "doomed", "spec": "spec", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "failed")

        status = await claude.call_tool("task_status", {"task_id": task_id})
        assert "model server unreachable" in status.data["task"]["error"]
    finally:
        await stop_agent(harness, runner)


async def test_cancelling_a_task_stops_the_agent(fleet) -> None:
    hub, claude = fleet
    release = asyncio.Event()

    def slow(system: str, turns: list[Turn]) -> str:
        raise AssertionError("should not be reached")

    class BlockingBackend(MockBackend):
        async def chat(self, *args, **kwargs):
            await release.wait()
            return await super().chat(*args, **kwargs)

    config = AgentConfig(
        name="alpha",
        hub=hub.url,
        backend={"type": "mock", "model": "mock", "base_url": "x"},
    )
    harness = Harness(config, backend=BlockingBackend(slow))
    runner = asyncio.create_task(harness.run())
    await wait_for(lambda: hub.store.bus.subscriber_count("alpha"))
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "long", "spec": "spec", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state in ("claimed", "working"))

        cancelled = await claude.call_tool("cancel_task", {"task_id": task_id})
        assert cancelled.data["task"]["state"] == "cancelled"
        await wait_for(lambda: not harness._running_tasks)
    finally:
        release.set()
        await stop_agent(harness, runner)


async def test_agent_asks_for_input_and_resumes_when_answered(fleet) -> None:
    """Multi-turn inside a task: the agent parks, Claude answers, work continues."""
    hub, claude = fleet

    def needs_input(system: str, turns: list[Turn]) -> str:
        # Only a USER-provided answer counts: the harness's self-answer bounce echoes
        # the model's own NEED_INPUT text back as an assistant turn, which must not
        # read as an answer.
        answered = any(
            "staging" in turn.content and "NEED_INPUT" not in turn.content
            for turn in turns
            if turn.role == "user"
        )
        if answered:
            return "Deployed to staging."
        return "NEED_INPUT: staging or prod?"

    harness, runner = await start_agent(hub, "alpha", needs_input)
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "deploy", "spec": "deploy the service", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]

        await wait_for(lambda: hub.store.require_task(task_id).state == "input_required")
        status = await claude.call_tool("task_status", {"task_id": task_id})
        room_id = status.data["task"]["room_id"]
        assert room_id is not None

        transcript = await claude.call_tool("room_transcript", {"room": room_id})
        assert "staging or prod?" in transcript.data["messages"][0]["body"]

        await claude.call_tool("provide_input", {"task_id": task_id, "message": "staging"})
        await wait_for(lambda: hub.store.require_task(task_id).state == "completed")
        assert "Deployed to staging" in hub.store.require_task(task_id).result["text"]
    finally:
        await stop_agent(harness, runner)


async def test_agent_uses_an_allowlisted_tool_on_its_node(fleet, tmp_path) -> None:
    hub, claude = fleet
    (tmp_path / "notes.txt").write_text("the answer is 42")

    harness, runner = await start_agent(
        hub,
        "alpha",
        tool_then_answer("file_read", {"path": "notes.txt"}, "The file says the answer is 42."),
        tools=ToolsConfig(allow=["file_read"], file_root=str(tmp_path)),
    )
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "read", "spec": "read notes.txt", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "completed")
        assert "42" in hub.store.require_task(task_id).result["text"]

        kinds = [event.kind for event in hub.store.task_events(task_id)]
        assert "progress" in kinds
    finally:
        await stop_agent(harness, runner)


# ------------------------------------------------ Claude Code <-> local agent


async def test_multi_turn_conversation_with_a_local_agent(fleet) -> None:
    hub, claude = fleet

    def responder(system: str, turns: list[Turn]) -> str:
        user_turns = [t for t in turns if t.role == "user"]
        return f"reply {len(user_turns)}"

    harness, runner = await start_agent(hub, "alpha", responder)
    try:
        sent = await claude.call_tool(
            "send_message", {"to": "alpha", "message": "@alpha first question"}
        )
        room_id = sent.data["room_id"]
        await wait_for(lambda: len(hub.store.fetch_messages(room_id)) == 2)

        inbox = await claude.call_tool("fetch_messages", {})
        assert inbox.data["messages"][0]["body"] == "reply 1"

        await claude.call_tool("post", {"room": room_id, "message": "@alpha second question"})
        await wait_for(lambda: len(hub.store.fetch_messages(room_id)) == 4)

        follow_up = await claude.call_tool("fetch_messages", {})
        assert follow_up.data["messages"][0]["body"] == "reply 2"

        transcript = await claude.call_tool("room_transcript", {"room": room_id})
        senders = [m["sender"] for m in transcript.data["messages"]]
        assert senders == ["claude:test", "alpha", "claude:test", "alpha"]
    finally:
        await stop_agent(harness, runner)


async def test_agent_replies_to_a_direct_message_without_a_mention(fleet) -> None:
    """A DM is addressed to the agent by definition, so `reply_when=mentioned` must
    not suppress it — this is what the `ask`/`send` CLI relies on."""
    hub, claude = fleet
    harness, runner = await start_agent(hub, "alpha", lambda s, t: "the answer is 4")
    try:
        # send_message creates a real DM (dm_key set); no @mention in the body.
        sent = await claude.call_tool("send_message", {"to": "alpha", "message": "what is 2+2?"})
        room_id = sent.data["room_id"]
        await wait_for(lambda: len(hub.store.fetch_messages(room_id)) == 2)

        reply = hub.store.fetch_messages(room_id)[-1]
        assert reply.sender == "alpha"
        assert "4" in reply.body
    finally:
        await stop_agent(harness, runner)


async def test_agent_ignores_messages_it_was_not_mentioned_in(fleet) -> None:
    hub, claude = fleet
    harness, runner = await start_agent(hub, "alpha", lambda s, t: "should not fire")
    try:
        created = await claude.call_tool(
            "create_room", {"topic": "quiet", "participants": ["alpha"]}
        )
        room_id = created.data["room"]["id"]
        await claude.call_tool("post", {"room": room_id, "message": "thinking out loud"})
        await asyncio.sleep(1.0)
        assert len(hub.store.fetch_messages(room_id)) == 1
    finally:
        await stop_agent(harness, runner)


# ----------------------------------------------- local agent <-> local agent


async def test_two_local_agents_converse_autonomously(fleet) -> None:
    """The headline capability: agent <-> agent multi-turn, bounded, observable."""
    hub, claude = fleet

    def make_responder(label: str):
        def respond(system: str, turns: list[Turn]) -> str:
            return f"{label} turn {len([t for t in turns if t.role == 'assistant']) + 1}"

        return respond

    first = await start_agent(hub, "alpha", make_responder("alpha"))
    second = await start_agent(hub, "beta", make_responder("beta"))
    try:
        started = await claude.call_tool(
            "start_dialogue",
            {
                "participants": ["alpha", "beta"],
                "goal": "decide on a caching strategy",
                "max_messages": 6,  # agent turns only — the seed no longer counts
            },
        )
        room_id = started.data["room_id"]

        # The room bounds itself: 1 seed + 6 agent turns, then archived.
        await wait_for(lambda: hub.store.require_room(room_id).archived, timeout=25.0)

        room = hub.store.require_room(room_id)
        assert room.archived_reason == "max_messages"

        transcript = await claude.call_tool("room_transcript", {"room": room_id})
        senders = [m["sender"] for m in transcript.data["messages"]]
        assert senders[0] == "claude:test"
        # Strict alternation proves floor control worked.
        assert senders[1:] == ["alpha", "beta", "alpha", "beta", "alpha", "beta"]
    finally:
        await stop_agent(*first)
        await stop_agent(*second)


async def test_dialogue_ends_early_on_the_stop_phrase(fleet) -> None:
    hub, claude = fleet

    def alpha_responder(system: str, turns: list[Turn]) -> str:
        return "I propose write-through caching."

    def beta_responder(system: str, turns: list[Turn]) -> str:
        return "That works for me. AGREED"

    first = await start_agent(hub, "alpha", alpha_responder)
    second = await start_agent(hub, "beta", beta_responder)
    try:
        started = await claude.call_tool(
            "start_dialogue",
            {
                "participants": ["alpha", "beta"],
                "goal": "settle the caching question",
                "max_messages": 40,
                "stop_phrase": "AGREED",
            },
        )
        room_id = started.data["room_id"]
        await wait_for(lambda: hub.store.require_room(room_id).archived, timeout=25.0)

        room = hub.store.require_room(room_id)
        assert room.archived_reason == "stop_phrase"
        assert len(hub.store.fetch_messages(room_id)) == 3
    finally:
        await stop_agent(*first)
        await stop_agent(*second)


async def test_observer_can_steer_a_running_dialogue(fleet) -> None:
    hub, claude = fleet

    def responder(system: str, turns: list[Turn]) -> str:
        # Like a real model, react to the whole visible window: the operator's
        # constraint may arrive while another agent is mid-reply.
        window = " ".join(turn.content for turn in turns).lower()
        if "must use redis" in window:
            return "Switching to redis then."
        return "Considering in-memory caching."

    first = await start_agent(hub, "alpha", responder)
    second = await start_agent(hub, "beta", responder)
    try:
        started = await claude.call_tool(
            "start_dialogue",
            {"participants": ["alpha", "beta"], "goal": "pick a cache", "max_messages": 40},
        )
        room_id = started.data["room_id"]
        await wait_for(lambda: len(hub.store.fetch_messages(room_id)) >= 3)

        await claude.call_tool("post", {"room": room_id, "message": "constraint: must use redis"})
        await wait_for(
            lambda: any("redis then" in m.body for m in hub.store.fetch_messages(room_id)),
            timeout=25.0,
        )

        await claude.call_tool("archive_room", {"room": room_id, "reason": "decided"})
        assert hub.store.require_room(room_id).archived_reason == "decided"
    finally:
        await stop_agent(*first)
        await stop_agent(*second)


async def test_agent_delegates_a_subtask_to_another_agent(fleet) -> None:
    """Agent-to-agent task delegation, not just conversation."""
    hub, claude = fleet
    worker = await start_agent(hub, "worker", lambda s, t: "subtask done", tags=["tier:fast"])
    try:
        from farmteam.sdk import AgentClient

        async with AgentClient(hub.url, "coordinator") as coordinator:
            await coordinator.register(tags=["tier:coordinator"])
            delegated = await coordinator.submit_task(
                title="delegated", spec="do the sub-thing", assignee="worker"
            )
            await wait_for(lambda: hub.store.require_task(delegated["id"]).state == "completed")

            task = hub.store.require_task(delegated["id"])
            assert task.created_by == "coordinator"
            assert task.assignee == "worker"
            assert task.result["text"] == "subtask done"

        # Claude can see and audit work it did not dispatch.
        listing = await claude.call_tool("list_tasks", {})
        assert delegated["id"] in [t["id"] for t in listing.data["tasks"]]
    finally:
        await stop_agent(*worker)


async def test_context_posted_into_a_task_room_reaches_the_working_agent(fleet) -> None:
    """`task_room` promises mid-flight context; the agent must actually read it."""
    hub, claude = fleet
    gate = asyncio.Event()
    seen: list[str] = []

    def responder(system, turns):
        window = " ".join(turn.content for turn in turns)
        seen.append(window)
        if "edge case" in window:
            return "Handled the edge case."
        return "NEED_INPUT: anything else to consider?"

    harness, runner = await start_agent(hub, "alpha", responder)
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "build", "spec": "build the thing", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "input_required")

        opened = await claude.call_tool("task_room", {"task_id": task_id})
        await claude.call_tool(
            "post", {"room": opened.data["room"]["id"], "message": "also handle the edge case"}
        )
        await claude.call_tool(
            "provide_input", {"task_id": task_id, "message": "see my note in the room"}
        )

        await wait_for(lambda: hub.store.require_task(task_id).state == "completed", timeout=25.0)
        assert "edge case" in hub.store.require_task(task_id).result["text"]
    finally:
        gate.set()
        await stop_agent(harness, runner)


async def test_an_agent_picks_up_work_queued_while_it_was_busy(fleet) -> None:
    """Nothing re-announces an already-queued task, so the agent must re-check."""
    hub, claude = fleet
    harness, runner = await start_agent(hub, "alpha", lambda s, t: "done")
    try:
        ids = []
        for i in range(3):
            submitted = await claude.call_tool(
                "submit_task", {"title": f"t{i}", "spec": "s", "assignee": "alpha"}
            )
            ids.append(submitted.data["task_id"])

        for task_id in ids:
            await wait_for(
                lambda tid=task_id: hub.store.require_task(tid).state == "completed",
                timeout=25.0,
            )
        assert all(hub.store.require_task(i).state == "completed" for i in ids)
    finally:
        await stop_agent(harness, runner)


async def test_status_is_durable_across_sessions(fleet) -> None:
    """A second Claude session checks on work the first one dispatched."""
    hub, claude = fleet
    harness, runner = await start_agent(hub, "alpha", lambda s, t: "finished the job")
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "t", "spec": "s", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "completed")

        other_mcp = build_mcp(
            hub.store, HubConfig(db_path=":memory:", default_identity="claude:other-session")
        )
        async with Client(other_mcp) as other_session:
            status = await other_session.call_tool("task_status", {"task_id": task_id})
            assert status.data["task"]["state"] == "completed"
            assert status.data["task"]["created_by"] == "claude:test"

            result = await other_session.call_tool("task_result", {"task_id": task_id})
            assert result.data["result"]["text"] == "finished the job"
    finally:
        await stop_agent(harness, runner)


async def test_a_model_that_only_describes_tool_calls_fails_loudly(fleet) -> None:
    """Parseable text-form tool calls are recovered and executed (see test_ux_fixes);
    this guards the remaining case — tool-call-shaped text the parser CANNOT recover. Completing
    that as a task is worse than failing: the requester believes work happened."""
    hub, claude = fleet
    pretend = (
        'I will create the files.\n```json\n{"name": "file_write", '
        '"arguments": {path: index.html, content: <html></html>}}\n```'
    )
    harness, runner = await start_agent(
        hub,
        "alpha",
        lambda s, t: pretend,
        tools=ToolsConfig(allow=["file_write"], file_root="/tmp"),
    )
    try:
        submitted = await claude.call_tool(
            "submit_task", {"title": "build", "spec": "build a page", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: hub.store.require_task(task_id).state == "failed")
        assert "tool-call-like text" in hub.store.require_task(task_id).error
    finally:
        await stop_agent(harness, runner)


async def test_a_summary_that_mentions_tool_calls_is_not_flagged(fleet) -> None:
    """The unexecuted-tool-call guard must not fire when tools genuinely ran: models
    often describe what they did, and failing that work would be worse than the bug."""
    from farmteam.agent.backends.base import ChatResult, ToolCall

    state = {"used": False}

    def respond(system, turns):
        if not state["used"]:
            state["used"] = True
            return ChatResult(
                text="",
                tool_calls=[
                    ToolCall(
                        id="c1", name="file_write", arguments={"path": "a.txt", "content": "x"}
                    )
                ],
            )
        # The summary quotes the call it made — shape the guard used to reject.
        return '{"name": "file_write", "arguments": {"path": "a.txt"}} — wrote the file.'

    harness, runner = await start_agent(
        hub_and_tools := fleet[0],
        "alpha",
        respond,
        tools=ToolsConfig(allow=["file_write"], file_root="/tmp/ft-guard-test"),
    )
    del hub_and_tools
    try:
        submitted = await fleet[1].call_tool(
            "submit_task", {"title": "write", "spec": "write a.txt", "assignee": "alpha"}
        )
        task_id = submitted.data["task_id"]
        await wait_for(lambda: fleet[0].store.require_task(task_id).state == "completed")
        assert fleet[0].store.require_task(task_id).result["tool_rounds"] >= 1
    finally:
        await stop_agent(harness, runner)
