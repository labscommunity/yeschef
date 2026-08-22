"""Regression locks for the UX-lab fix cycle 1 (branch lab/ux-improvements).

Findings these pin down: spec echo in acks/waits/listings, wait_task until="done",
roster noise, text-form tool-call recovery, truncated-output honesty, unknown
config keys, and submit-time assignee validation.
"""

from __future__ import annotations

import asyncio
import contextlib

from fastmcp import Client

from farmteam.agent import AgentConfig
from farmteam.agent.backends.base import ChatResult
from farmteam.agent.config import ToolsConfig
from farmteam.agent.harness import Harness, _parse_text_tool_calls
from farmteam.hub import HubConfig
from farmteam.hub.mcp_server import build_mcp

from .live import live_hub
from .mock_backend import MockBackend
from .test_integration import wait_for

ALLOWED = {"file_write", "file_read", "shell"}


# ------------------------------------------------------- text tool-call recovery


def test_parses_qwen_tool_call_tags() -> None:
    text = (
        'ok\n<tool_call>\n{"name": "file_write", '
        '"arguments": {"path": "a.txt", "content": "hi"}}\n</tool_call>'
    )
    calls = _parse_text_tool_calls(text, ALLOWED)
    assert len(calls) == 1
    assert calls[0].name == "file_write"
    assert calls[0].arguments == {"path": "a.txt", "content": "hi"}


def test_parses_fenced_json_and_openai_nesting() -> None:
    text = '```json\n{"function": {"name": "shell", "arguments": "{\\"command\\": \\"ls\\"}"}}\n```'
    calls = _parse_text_tool_calls(text, ALLOWED)
    assert len(calls) == 1
    assert calls[0].name == "shell"
    assert calls[0].arguments == {"command": "ls"}


def test_rejects_unknown_tools_and_prose() -> None:
    assert _parse_text_tool_calls('{"name": "rm_rf", "arguments": {}}', ALLOWED) == []
    assert _parse_text_tool_calls("just a normal reply about tools", ALLOWED) == []
    assert _parse_text_tool_calls("", ALLOWED) == []


async def test_text_form_tool_calls_are_executed(tmp_path) -> None:
    """A model that writes tool calls as text still gets its file created."""
    state = {"round": 0}

    def respond(system, turns):
        state["round"] += 1
        if state["round"] == 1:
            return ChatResult(
                text='<tool_call>{"name": "file_write", "arguments": '
                '{"path": "out.txt", "content": "built by text-form call"}}</tool_call>',
                input_tokens=5,
                output_tokens=5,
            )
        return ChatResult(text="done: wrote out.txt", input_tokens=5, output_tokens=5)

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="texty",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "mock"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read", "file_list"],
                    file_root=str(tmp_path / "scratch"),
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("texty"))
            try:
                submitted = await claude.call_tool(
                    "submit_task",
                    {"title": "write file", "spec": "write out.txt", "assignee": "texty"},
                )
                task_id = submitted.data["task_id"]
                await wait_for(lambda: hub.store.require_task(task_id).state == "completed")
                listing = await claude.call_tool("task_files", {"task_id": task_id})
                assert {f["path"] for f in listing.data["files"]} == {"out.txt"}
                result = await claude.call_tool("task_result", {"task_id": task_id})
                assert result.data["result"]["tool_rounds"] >= 1
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


# ------------------------------------------------------------ truncation honesty


async def test_truncated_output_is_flagged(tmp_path) -> None:
    def respond(system, turns):
        return ChatResult(
            text="half a fil", input_tokens=5, output_tokens=2048, stop_reason="length"
        )

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="chopper",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "mock"},
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("chopper"))
            try:
                submitted = await claude.call_tool(
                    "submit_task", {"title": "long", "spec": "write a lot", "assignee": "chopper"}
                )
                task_id = submitted.data["task_id"]
                await wait_for(lambda: hub.store.require_task(task_id).state == "completed")
                result = (await claude.call_tool("task_result", {"task_id": task_id})).data
                assert result["result"]["truncated"] is True
                assert result["result"]["max_tokens_ceiling"] == config.max_tokens
                assert "truncated" in result["result"]["text"]
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


# ---------------------------------------------------------------- slim responses


async def test_acks_waits_and_listings_do_not_echo_the_spec() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("ghostless", kind="worker", node="n", backend="b", tags=[])
            spec = "SPEC-SENTINEL " * 50
            ack = (
                await claude.call_tool(
                    "submit_task", {"title": "t", "spec": spec, "assignee": "ghostless"}
                )
            ).data
            assert "spec" not in ack["task"]
            task_id = ack["task_id"]

            waited = (await claude.call_tool("wait_task", {"task_id": task_id, "wait_s": 1})).data
            assert "spec" not in waited["task"]

            listed = (await claude.call_tool("list_tasks", {})).data
            assert all("spec" not in t and "result" not in t for t in listed["tasks"])

            status = (await claude.call_tool("task_status", {"task_id": task_id})).data
            assert "spec" not in status["task"]
            verbose = (
                await claude.call_tool("task_status", {"task_id": task_id, "verbose": True})
            ).data
            assert verbose["task"]["spec"] == spec


async def test_submit_ack_flags_offline_assignee_and_empty_selector() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("sleeper", kind="worker", node="n", backend="b", tags=[])
            with hub.store._lock if hasattr(hub.store, "_lock") else contextlib.nullcontext():
                hub.store._db.execute(
                    "UPDATE agents SET last_seen = last_seen - 3600 WHERE name = 'sleeper'"
                )
                hub.store._db.commit()
            ack = (
                await claude.call_tool(
                    "submit_task", {"title": "t", "spec": "s", "assignee": "sleeper"}
                )
            ).data
            assert ack["assignee_status"] == "offline"
            assert "offline" in ack["note"]

            ack2 = (
                await claude.call_tool(
                    "submit_task", {"title": "t2", "spec": "s", "selector": "tier:nonexistent"}
                )
            ).data
            assert ack2["selector_online_matches"] == 0
            assert "queued" in ack2["note"]

            bogus = (
                await claude.call_tool(
                    "submit_task", {"title": "t3", "spec": "s", "assignee": "atlas"}
                )
            ).data
            assert bogus["error"]["code"] == "not_found"


async def test_wait_until_done_skips_intermediate_states() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("stepper", kind="worker", node="n", backend="b", tags=[])
            ack = (
                await claude.call_tool(
                    "submit_task", {"title": "t", "spec": "s", "assignee": "stepper"}
                )
            ).data
            task_id = ack["task_id"]

            async def advance():
                await asyncio.sleep(0.3)
                hub.store.claim_task(task_id, "stepper")
                await asyncio.sleep(0.3)
                hub.store.complete_task(task_id, "stepper", {"text": "done"})

            mover = asyncio.create_task(advance())
            waited = (
                await claude.call_tool(
                    "wait_task", {"task_id": task_id, "wait_s": 30, "until": "done"}
                )
            ).data
            await mover
            assert waited["done"] is True
            assert waited["task"]["state"] == "completed"


async def test_list_agents_kind_filter_and_worker_first_order() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("zeb", kind="worker", node="n", backend="b", tags=[])
            all_agents = (await claude.call_tool("list_agents", {})).data["agents"]
            assert all_agents[0]["kind"] == "worker"  # workers sort first
            workers = (await claude.call_tool("list_agents", {"kind": "worker"})).data["agents"]
            assert workers and all(a["kind"] == "worker" for a in workers)


# -------------------------------------------------------------- config hygiene


def test_unknown_config_keys_warn(capsys) -> None:
    AgentConfig.from_dict({"name": "x", "backend": {}, "regster_token": "typo"})
    err = capsys.readouterr().err
    assert "unknown config key 'regster_token'" in err
