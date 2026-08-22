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


# ------------------------------------------------------------- cycle 2 locks


def test_selector_union_matches_any_part() -> None:
    from farmteam.models import Agent, AgentKind

    a = Agent(name="scout", kind=AgentKind.WORKER, tags=["tier:fast"])
    assert a.matches("tier:fast|tier:build")
    assert a.matches("tier:build|scout")
    assert not a.matches("tier:build|tier:reasoning")


def test_summary_carries_age_ran_and_files() -> None:
    from farmteam.models import Task, TaskState, now

    t = Task(
        id="task_x",
        title="t",
        spec="s",
        created_by="c",
        state=TaskState.COMPLETED,
        created_at=now() - 100,
        claimed_at=now() - 90,
        finished_at=now() - 10,
        result={"files": [{"path": "a"}, {"path": "b"}]},
    )
    d = t.to_summary()
    assert 95 < d["age_s"] < 110
    assert 75 < d["ran_s"] < 85
    assert d["files"] == 2


async def test_need_input_detected_mid_text(tmp_path) -> None:
    """The sentinel buried mid-sentence must still park the task, not complete it."""

    def respond(system, turns):
        return "I need more details. NEED_INPUT: which database should I use?"

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="asker",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("asker"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task", {"title": "t", "spec": "build storage", "assignee": "asker"}
                    )
                ).data
                task_id = ack["task_id"]
                await wait_for(lambda: hub.store.require_task(task_id).state == "input_required")
                waited = (
                    await claude.call_tool("wait_task", {"task_id": task_id, "wait_s": 1})
                ).data
                # The question rides in the summary's progress message.
                assert "which database" in waited["task"]["progress"]["message"]
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


async def test_lifetime_tokens_include_task_tokens() -> None:
    async with live_hub() as hub:
        hub.store.register_agent("earner", kind="worker", node="n", backend="b", tags=[])
        t = hub.store.submit_task(title="t", spec="s", created_by="c", assignee="earner")
        hub.store.claim_task(t.id, "earner")
        hub.store.complete_task(t.id, "earner", {"text": "ok", "tokens": 395})
        stats = hub.store.lifetime_stats()
        assert stats["local_tokens"] >= 395


async def test_cancel_ack_is_a_proof_receipt() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("stopper", kind="worker", node="n", backend="b", tags=[])
            ack = (
                await claude.call_tool(
                    "submit_task", {"title": "t", "spec": "some work here", "assignee": "stopper"}
                )
            ).data
            receipt = (await claude.call_tool("cancel_task", {"task_id": ack["task_id"]})).data
            assert receipt["task"]["state"] == "cancelled"
            assert "spec" not in receipt["task"]
            assert receipt["files_produced"] == 0
            assert receipt["assignee_status"] in ("online", "offline")
            # terminal progress message no longer reads 'started'
            assert receipt["task"]["progress"]["message"] != "started"


async def test_tiny_spec_gets_a_warning_note() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("w1", kind="worker", node="n", backend="b", tags=[])
            ack = (
                await claude.call_tool(
                    "submit_task", {"title": "t", "spec": "handle it", "assignee": "w1"}
                )
            ).data
            assert "underspecified" in ack["note"]


async def test_roster_carries_active_task_counts() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("busybee", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="work work", created_by="c", assignee="busybee"
            )
            hub.store.claim_task(t.id, "busybee")
            agents = (await claude.call_tool("list_agents", {})).data["agents"]
            bee = next(a for a in agents if a["name"] == "busybee")
            assert bee["active_tasks"] == 1


async def test_code_in_text_only_is_flagged(tmp_path) -> None:
    """Tools granted, no files written, fenced code in prose → loud flag."""

    def respond(system, turns):
        return "Here is the file:\n```python\nprint('hi')\n```\nDone."

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="proser",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("proser"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {"title": "t", "spec": "write hello.py", "assignee": "proser"},
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state == "completed")
                result = (await claude.call_tool("task_result", {"task_id": ack["task_id"]})).data
                assert result["result"]["code_in_text_only"] is True
                assert result["result"]["tool_log"] == []
                # The lone block is auto-materialized rather than lost — flagged as such.
                assert result["result"]["files"][0]["auto_extracted"] is True
                assert "harness extracted" in result["result"]["text"]
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


# ------------------------------------------------------------- cycle 3 locks


async def test_project_affinity_round_trip() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("wproj", kind="worker", node="n", backend="b", tags=[])
            await claude.call_tool(
                "submit_task",
                {
                    "title": "t",
                    "spec": "long enough spec here",
                    "assignee": "wproj",
                    "project": "acme-site",
                },
            )
            await claude.call_tool(
                "submit_task",
                {"title": "other", "spec": "long enough spec here", "assignee": "wproj"},
            )
            mine = (await claude.call_tool("list_tasks", {"project": "acme-site"})).data["tasks"]
            assert len(mine) == 1 and mine[0]["project"] == "acme-site"


async def test_task_files_include_content_lands_everything_in_one_call() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("filer", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="build stuff please", created_by="c", assignee="filer"
            )
            hub.store.claim_task(t.id, "filer")
            a1 = hub.store.save_artifact("index.html", "text/html", b"<h1>hi</h1>", "filer")
            a2 = hub.store.save_artifact("css/site.css", "text/css", b"body{}", "filer")
            hub.store.complete_task(
                t.id,
                "filer",
                {
                    "text": "done",
                    "files": [
                        {"path": "index.html", "artifact_id": a1["id"], "bytes": 11},
                        {"path": "css/site.css", "artifact_id": a2["id"], "bytes": 6},
                    ],
                },
            )
            got = (
                await claude.call_tool("task_files", {"task_id": t.id, "include_content": True})
            ).data
            by_path = {f["path"]: f for f in got["files"]}
            assert by_path["index.html"]["content"] == "<h1>hi</h1>"
            assert by_path["css/site.css"]["content"] == "body{}"


def test_lone_code_block_extraction_names_from_context() -> None:
    from farmteam.agent.harness import _extract_lone_code_block

    got = _extract_lone_code_block(
        "Here is the corrected `parser.py`:\n```python\nx = 1\n```\nDone."
    )
    assert got == ("parser.py", "x = 1\n")
    # language fallback when no filename appears
    got2 = _extract_lone_code_block("Result:\n```python\ny = 2\n```")
    assert got2 == ("extracted.py", "y = 2\n")
    # two blocks = ambiguous, leave alone
    assert _extract_lone_code_block("```py\na\n```\n```py\nb\n```") is None


async def test_lone_code_block_is_materialized_as_artifact(tmp_path) -> None:
    def respond(system, turns):
        return "Here is `fix.py`:\n```python\nprint('fixed')\n```"

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="fencer",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("fencer"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {"title": "t", "spec": "fix the parser file", "assignee": "fencer"},
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state == "completed")
                got = (
                    await claude.call_tool(
                        "task_files", {"task_id": ack["task_id"], "include_content": True}
                    )
                ).data
                assert len(got["files"]) == 1
                assert got["files"][0]["path"] == "fix.py"
                assert got["files"][0]["auto_extracted"] is True
                assert got["files"][0]["content"] == "print('fixed')\n"
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


async def test_wait_more_hint_on_capped_done_wait() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("slowpoke", kind="worker", node="n", backend="b", tags=[])
            ack = (
                await claude.call_tool(
                    "submit_task",
                    {"title": "t", "spec": "something long running", "assignee": "slowpoke"},
                )
            ).data
            waited = (
                await claude.call_tool(
                    "wait_task", {"task_id": ack["task_id"], "wait_s": 1, "until": "done"}
                )
            ).data
            assert waited["done"] is False
            assert waited["wait_more"] is True


def test_worker_advertises_tool_roster_tag() -> None:
    """Registration tags carry tools:<granted> so specs never demand the impossible."""
    from farmteam.agent.config import ToolsConfig as TC

    cfg = AgentConfig(
        name="x",
        hub="http://h",
        backend={"type": "cli", "command": ["true"], "model": "m"},
        tools=TC(allow=["file_write", "shell"], file_root="/tmp/x"),
    )
    h = Harness(cfg, backend=MockBackend(lambda s, t: "ok"))
    granted = "+".join(cfg.tools.allow)
    assert h.tools.enabled
    assert granted == "file_write+shell"


async def test_dedupe_releases_after_failure_and_flags_duplicates() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("dedup1", kind="worker", node="n", backend="b", tags=[])
            hub.store.register_agent("dedup2", kind="worker", node="n", backend="b", tags=[])
            first = (
                await claude.call_tool(
                    "submit_task",
                    {
                        "title": "verse",
                        "spec": "write a limerick please",
                        "assignee": "dedup1",
                        "dedupe_key": "verse-1",
                    },
                )
            ).data
            # duplicate while live → same id, flagged
            dup = (
                await claude.call_tool(
                    "submit_task",
                    {
                        "title": "verse",
                        "spec": "write a limerick please",
                        "assignee": "dedup1",
                        "dedupe_key": "verse-1",
                    },
                )
            ).data
            assert dup["task_id"] == first["task_id"]
            assert dup["deduped"] is True and "no new task" in dup["note"]
            # fail it → key releases → resubmit creates a NEW task honoring new assignee
            hub.store.claim_task(first["task_id"], "dedup1")
            hub.store.fail_task(first["task_id"], "dedup1", "model exploded")
            fresh = (
                await claude.call_tool(
                    "submit_task",
                    {
                        "title": "verse",
                        "spec": "AABBA this time",
                        "assignee": "dedup2",
                        "dedupe_key": "verse-1",
                    },
                )
            ).data
            assert fresh["task_id"] != first["task_id"]
            assert "deduped" not in fresh
            assert fresh["task"]["assignee"] == "dedup2"


async def test_revise_task_carries_history_and_feedback() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("reviser", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="build parser",
                spec="parse durations like 2h30m",
                created_by="c",
                assignee="reviser",
                project="acme",
            )
            hub.store.claim_task(t.id, "reviser")
            hub.store.complete_task(t.id, "reviser", {"text": "def parse(s): return 0"})
            revised = (
                await claude.call_tool(
                    "revise_task",
                    {"task_id": t.id, "feedback": "test_mixed fails: expected 150 got 0"},
                )
            ).data
            assert revised["revises"] == t.id
            new = hub.store.require_task(revised["task_id"])
            assert "PRIOR ATTEMPT" in new.spec and "def parse" in new.spec
            assert "expected 150 got 0" in new.spec
            assert new.assignee == "reviser" and new.project == "acme"
            # revising a live task is refused
            live = hub.store.submit_task(
                title="x", spec="another live task", created_by="c", assignee="reviser"
            )
            err = (
                await claude.call_tool("revise_task", {"task_id": live.id, "feedback": "f"})
            ).data
            assert err["error"]["code"] == "conflict"


async def test_bare_done_claim_is_flagged_no_output(tmp_path) -> None:
    def respond(system, turns):
        return "WROTE .gitignore"  # no fence, no tools, nothing

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="claimer",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("claimer"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {"title": "t", "spec": "write a gitignore file", "assignee": "claimer"},
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state == "completed")
                res = (await claude.call_tool("task_result", {"task_id": ack["task_id"]})).data
                assert res["result"]["no_output"] is True
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


async def test_malformed_tool_text_gets_one_corrective_retry(tmp_path) -> None:
    state = {"round": 0}

    def respond(system, turns):
        state["round"] += 1
        if state["round"] == 1:
            # unparseable tool-shaped text (unquoted keys)
            return 'I will call:\n```json\n{"name": "file_write", "arguments": {path: x}}\n```'
        return ChatResult(
            text="done",
            tool_calls=[],
            input_tokens=1,
            output_tokens=1,
        )

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="retryer",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("retryer"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {"title": "t", "spec": "write the x file now", "assignee": "retryer"},
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state.terminal)
                task = hub.store.require_task(ack["task_id"])
                # retry happened (2 model rounds) and the task did not hard-fail
                assert state["round"] == 2
                assert str(task.state) == "completed"
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


# ------------------------------------------------------------- cycle 4 locks


async def test_wait_room_returns_new_messages_and_archive() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("talker", kind="worker", node="n", backend="b", tags=[])
            room = hub.store.create_room("debate", "claude:test", participants=["talker"])
            seed = hub.store.post_message(room.id, "claude:test", "the seed")

            async def speak():
                await asyncio.sleep(0.3)
                hub.store.post_message(room.id, "talker", "worker turn one")

            mover = asyncio.create_task(speak())
            got = (
                await claude.call_tool(
                    "wait_room", {"room": room.id, "from_seq": seed.seq, "wait_s": 10}
                )
            ).data
            await mover
            assert [m["body"] for m in got["messages"]] == ["worker turn one"]
            assert got["archived"] is False

            hub.store.archive_room(room.id, "cap reached", by="hub", privileged=True)
            done = (
                await claude.call_tool(
                    "wait_room",
                    {"room": room.id, "from_seq": got["next_seq"], "wait_s": 5},
                )
            ).data
            assert done["archived"] is True


async def test_double_unparseable_with_code_block_salvages(tmp_path) -> None:
    """Two garbage tool emissions but a real code block → completed with flags, not lost."""

    def respond(system, turns):
        return (
            'Calling: {"name": "file_write", "arguments": {bad json here}}\n'
            "the file `util.py`:\n```python\nVALUE = 42\n```"
        )

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="salvager",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("salvager"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {"title": "t", "spec": "write util.py with VALUE", "assignee": "salvager"},
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state.terminal)
                task = hub.store.require_task(ack["task_id"])
                assert str(task.state) == "completed"
                assert task.result["tool_text_unparsed"] is True
                assert task.result["files"][0]["path"] == "util.py"
                assert task.result["files"][0]["auto_extracted"] is True
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


async def test_all_tool_failures_flag_blocked(tmp_path) -> None:
    def respond(system, turns):
        # native tool call to an escaping path, then a polite report
        if len(turns) == 1:
            from farmteam.agent.backends.base import ToolCall as TC

            return ChatResult(
                text="",
                tool_calls=[TC(id="1", name="file_read", arguments={"path": "/etc/passwd"})],
                input_tokens=1,
                output_tokens=1,
            )
        return ChatResult(
            text="Could not access the path; stopping.", input_tokens=1, output_tokens=1
        )

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="blocked",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("blocked"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {"title": "t", "spec": "read the passwd file", "assignee": "blocked"},
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state.terminal)
                task = hub.store.require_task(ack["task_id"])
                assert task.result.get("all_tools_failed") is True
                assert "blocked" in task.result["text"]
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


# ------------------------------------------------------------- cycle 5 locks


async def test_wait_task_returns_on_input_required() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("qworker", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="needs input soon", created_by="c", assignee="qworker"
            )
            hub.store.claim_task(t.id, "qworker")

            async def park():
                await asyncio.sleep(0.3)
                hub.store.request_input(t.id, "qworker", "which db?")

            mover = asyncio.create_task(park())
            waited = (
                await claude.call_tool(
                    "wait_task", {"task_id": t.id, "wait_s": 30, "until": "done"}
                )
            ).data
            await mover
            assert waited["task"]["state"] == "input_required"
            assert "which db" in waited["task"]["progress"]["message"]


async def test_uncollected_counter_is_live_and_dismissable() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("maker", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="make a file", created_by="c", assignee="maker", project="p1"
            )
            hub.store.claim_task(t.id, "maker")
            art = hub.store.save_artifact("out.txt", "text/plain", b"data", "maker")
            hub.store.complete_task(
                t.id,
                "maker",
                {"text": "done", "files": [{"path": "out.txt", "artifact_id": art["id"]}]},
            )
            who = (await claude.call_tool("whoami", {})).data
            assert who["uncollected_results"] == 1
            assert who["uncollected_sample"][0]["project"] == "p1"
            # scoped: another project sees zero
            scoped = (await claude.call_tool("whoami", {"project": "other"})).data
            assert "uncollected_results" not in scoped
            # collecting the file clears the counter
            await claude.call_tool("task_files", {"task_id": t.id, "include_content": True})
            after = (await claude.call_tool("whoami", {})).data
            assert "uncollected_results" not in after


async def test_dismiss_results_sweeps_without_fetching() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("maker2", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="make a file", created_by="c", assignee="maker2"
            )
            hub.store.claim_task(t.id, "maker2")
            art = hub.store.save_artifact("x.txt", "text/plain", b"d", "maker2")
            hub.store.complete_task(
                t.id,
                "maker2",
                {"text": "done", "files": [{"path": "x.txt", "artifact_id": art["id"]}]},
            )
            swept = (await claude.call_tool("dismiss_results", {"dismiss_all": True})).data
            assert swept["dismissed_tasks"] == 1
            who = (await claude.call_tool("whoami", {})).data
            assert "uncollected_results" not in who


async def test_list_tasks_running_alias_and_state_enum_error() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            ok = (await claude.call_tool("list_tasks", {"state": "running"})).data
            assert "tasks" in ok
            err = (await claude.call_tool("list_tasks", {"state": "sprinting"})).data
            assert err["error"]["code"] == "invalid"
            assert "working" in err["error"]["message"]


def test_extraction_prefers_spec_named_file() -> None:
    from farmteam.agent.harness import _extract_lone_code_block

    got = _extract_lone_code_block(
        "Result:\n```python\nx = 1\n```",
        spec_hint="Write validate.py with two functions and doctests.",
    )
    assert got == ("validate.py", "x = 1\n")


# ------------------------------------------------------------- cycle 7 locks


async def test_flags_ride_in_summaries() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("flagger", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="do the thing please", created_by="c", assignee="flagger"
            )
            hub.store.claim_task(t.id, "flagger")
            hub.store.complete_task(
                t.id, "flagger", {"text": "done", "truncated": True, "no_output": True}
            )
            waited = (await claude.call_tool("wait_task", {"task_id": t.id, "wait_s": 1})).data
            assert set(waited["task"]["flags"]) == {"truncated", "no_output"}


async def test_input_required_ttl_fails_with_the_question() -> None:
    from farmteam.models import INPUT_REQUIRED_TTL_S

    async with live_hub() as hub:
        hub.store.register_agent("stuck", kind="worker", node="n", backend="b", tags=[])
        t = hub.store.submit_task(
            title="t", spec="build the widget", created_by="c", assignee="stuck"
        )
        hub.store.claim_task(t.id, "stuck")
        hub.store.request_input(t.id, "stuck", "may I proceed?")
        # age the park beyond the TTL
        hub.store._db.execute(
            "UPDATE tasks SET claimed_at = claimed_at - ? WHERE id = ?",
            (INPUT_REQUIRED_TTL_S + 60, t.id),
        )
        hub.store._db.commit()
        hub.store.sweep()
        task = hub.store.require_task(t.id)
        assert str(task.state) == "failed"
        assert "may I proceed?" in task.error


async def test_output_mode_text_disables_tools_and_extracts(tmp_path) -> None:
    def respond(system, turns):
        assert "Do not attempt tool calls" in system
        return "Here is `thing.py`:\n```python\nDONE = 1\n```"

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="texty2",
                hub=hub.url,
                backend={"type": "cli", "command": ["true"], "model": "m"},
                tools=ToolsConfig(
                    allow=["file_write", "file_read"], file_root=str(tmp_path / "scratch")
                ),
            )
            harness = Harness(config, backend=MockBackend(respond))
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("texty2"))
            try:
                ack = (
                    await claude.call_tool(
                        "submit_task",
                        {
                            "title": "t",
                            "spec": "write thing.py",
                            "assignee": "texty2",
                            "output_mode": "text",
                        },
                    )
                ).data
                await wait_for(lambda: hub.store.require_task(ack["task_id"]).state.terminal)
                task = hub.store.require_task(ack["task_id"])
                assert str(task.state) == "completed"
                assert task.result["files"][0]["path"] == "thing.py"
                assert task.result["files"][0]["auto_extracted"] is True
            finally:
                runner.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


async def test_revise_inherits_output_mode() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("modal", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t",
                spec="write it in text mode",
                created_by="c",
                assignee="modal",
                output_mode="text",
            )
            hub.store.claim_task(t.id, "modal")
            hub.store.complete_task(t.id, "modal", {"text": "v1"})
            revised = (
                await claude.call_tool("revise_task", {"task_id": t.id, "feedback": "v2 please"})
            ).data
            assert hub.store.require_task(revised["task_id"]).output_mode == "text"


# ------------------------------------------------------------- cycle 8 locks


async def test_wait_room_never_withholds_messages() -> None:
    """until='archived' hitting the cap still delivers accumulated messages."""
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("talky", kind="worker", node="n", backend="b", tags=[])
            room = hub.store.create_room("d", "claude:test", participants=["talky"])
            hub.store.post_message(room.id, "talky", "turn one")
            got = (
                await claude.call_tool(
                    "wait_room",
                    {"room": room.id, "from_seq": 0, "wait_s": 1, "until": "archived"},
                )
            ).data
            assert [m["body"] for m in got["messages"]] == ["turn one"]
            assert got["wait_more"] is True  # room still open


async def test_stop_phrase_needs_every_worker_to_have_spoken() -> None:
    from farmteam.models import RoomPolicy

    async with live_hub() as hub:
        hub.store.register_agent("w1", kind="worker", node="n", backend="b", tags=[])
        hub.store.register_agent("w2", kind="worker", node="n", backend="b", tags=[])
        room = hub.store.create_room(
            "debate",
            "claude:test",
            participants=["w1", "w2"],
            policy=RoomPolicy(stop_phrase="VERDICT:"),
        )
        # w1 fires the terminator on turn ONE — w2 has not spoken; room must survive
        hub.store.post_message(room.id, "w1", "VERDICT: use httpx")
        assert hub.store.require_room(room.id).archived is False
        hub.store.post_message(room.id, "w2", "I disagree, requests is fine")
        hub.store.post_message(room.id, "w1", "VERDICT: httpx, final")
        assert hub.store.require_room(room.id).archived is True


async def test_operator_posts_do_not_burn_room_budget() -> None:
    from farmteam.models import RoomPolicy

    async with live_hub() as hub:
        hub.store.register_agent("w3", kind="worker", node="n", backend="b", tags=[])
        room = hub.store.create_room(
            "d",
            "claude:test",
            participants=["w3"],
            policy=RoomPolicy(max_messages=2),
        )
        hub.store.post_message(room.id, "claude:test", "seed")
        hub.store.post_message(room.id, "claude:test", "steering interjection")
        assert hub.store.require_room(room.id).archived is False  # 0 worker messages
        hub.store.post_message(room.id, "w3", "turn 1")
        hub.store.post_message(room.id, "w3", "turn 2")
        assert hub.store.require_room(room.id).archived is True  # 2 worker messages


async def test_start_dialogue_refuses_dead_participants() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("alive", kind="worker", node="n", backend="b", tags=[])
            hub.store.register_agent("corpse", kind="worker", node="n", backend="b", tags=[])
            hub.store._db.execute(
                "UPDATE agents SET last_seen = last_seen - 3600 WHERE name = 'corpse'"
            )
            hub.store._db.commit()
            err = (
                await claude.call_tool(
                    "start_dialogue",
                    {"participants": ["alive", "corpse"], "goal": "debate something"},
                )
            ).data
            assert err["error"]["code"] == "conflict"
            assert "corpse" in err["error"]["message"]


async def test_reassign_preserves_identity_and_lineage() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("wa", kind="worker", node="n", backend="b", tags=[])
            hub.store.register_agent("wb", kind="worker", node="n", backend="b", tags=[])
            t = hub.store.submit_task(
                title="t", spec="glossary please", created_by="c", assignee="wa"
            )
            moved = (
                await claude.call_tool("reassign_task", {"task_id": t.id, "assignee": "wb"})
            ).data
            assert moved["task"]["id"] == t.id
            assert moved["task"]["assignee"] == "wb"
            assert moved["moved_from"] == "wa"
            # working task without force → guarded
            hub.store.claim_task(t.id, "wb")
            hub.store._db.execute("UPDATE tasks SET state='working' WHERE id = ?", (t.id,))
            hub.store._db.commit()
            err = (
                await claude.call_tool("reassign_task", {"task_id": t.id, "assignee": "wa"})
            ).data
            assert err["error"]["code"] == "conflict"


async def test_cancel_all_returns_receipt_table() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            hub.store.register_agent("wf", kind="worker", node="n", backend="b", tags=[])
            for n in range(3):
                hub.store.submit_task(
                    title=f"t{n}",
                    spec="fan out work",
                    created_by="c",
                    assignee="wf",
                    project="stopme",
                )
            got = (await claude.call_tool("cancel_all", {"project": "stopme"})).data
            assert got["cancelled"] == 3
            assert all(t["state"] == "cancelled" for t in got["tasks"])


async def test_priority_visible_and_ordering_real() -> None:
    async with live_hub() as hub:
        hub.store.register_agent("wp", kind="worker", node="n", backend="b", tags=[])
        low = hub.store.submit_task(title="low", spec="later please", created_by="c", assignee="wp")
        high = hub.store.submit_task(
            title="high", spec="urgent thing", created_by="c", assignee="wp", priority=10
        )
        assert high.to_summary()["priority"] == 10
        nxt = hub.store.next_task_for("wp")
        assert nxt.id == high.id  # priority actually orders the claim path
        assert low.to_summary()["priority"] is None
