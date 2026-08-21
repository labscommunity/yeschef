"""The buildout path: files produced on a worker come home to the requester.

This is the use case the whole feature exists for — spec in Claude Code, dispatch to a
local agent on another machine, pull the built files back into the project.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastmcp import Client

from farmteam.agent import AgentConfig, Harness
from farmteam.agent.backends import build_backend
from farmteam.agent.backends.cli import CliBackend
from farmteam.hub import HubConfig, Store
from farmteam.hub.events import EventBus
from farmteam.hub.mcp_server import build_mcp
from farmteam.models import HubError
from farmteam.tools.executor import ToolsConfig

from .live import live_hub
from .test_integration import start_agent, stop_agent, wait_for

# ------------------------------------------------------------------- artifacts


def test_artifacts_round_trip_in_the_store() -> None:
    store = Store(":memory:", EventBus())
    try:
        store.register_agent("builder")
        meta = store.save_artifact("index.html", "text/html", b"<html></html>", "builder")
        got_meta, content = store.get_artifact(meta["id"])
        assert content == b"<html></html>"
        assert got_meta["name"] == "index.html"
        assert got_meta["sha256"] == meta["sha256"]

        with pytest.raises(HubError):
            store.get_artifact("art_nope")
    finally:
        store.close()


def test_artifacts_enforce_the_size_cap() -> None:
    store = Store(":memory:", EventBus())
    try:
        store.register_agent("builder")
        with pytest.raises(HubError) as exc:
            store.save_artifact(
                "huge.bin", "application/octet-stream", b"x" * (33 * 1024 * 1024), "builder"
            )
        assert "cap" in exc.value.message
    finally:
        store.close()


# ------------------------------------------------------------------ cli backend


def _fake_agent_script(tmp_path: Path, body: str) -> list[str]:
    script = tmp_path / "fake-agent.py"
    script.write_text(body)
    return [sys.executable, str(script), "{prompt}"]


async def test_cli_backend_runs_in_the_workspace(tmp_path) -> None:
    """The CLI agent works in the task's directory — that is its whole contract."""
    command = _fake_agent_script(
        tmp_path,
        "import sys, pathlib\n"
        "pathlib.Path('made-here.txt').write_text('from the cli agent')\n"
        "print('built one file in', pathlib.Path.cwd().name)\n",
    )
    backend = CliBackend(command=command, model="fake/agent")
    workspace = tmp_path / "task_x"
    workspace.mkdir()
    backend.workspace = str(workspace)

    result = await backend.chat("system", [])
    assert "built one file in task_x" in result.text
    assert (workspace / "made-here.txt").read_text() == "from the cli agent"


async def test_cli_backend_surfaces_failure_and_timeout(tmp_path) -> None:
    failing = _fake_agent_script(tmp_path, "import sys; sys.exit(3)")
    backend = CliBackend(command=failing, model="fake/agent")
    with pytest.raises(RuntimeError, match="exited 3"):
        await backend.chat("s", [])

    sleepy = _fake_agent_script(tmp_path, "import time; time.sleep(30)")
    slow = CliBackend(command=sleepy, model="fake/agent", timeout=0.5)
    with pytest.raises(RuntimeError, match="timed out"):
        await slow.chat("s", [])


def test_build_backend_constructs_cli(tmp_path) -> None:
    backend = build_backend(
        {"type": "cli", "command": ["echo", "{prompt}"], "model": "m", "timeout_s": 5}
    )
    assert isinstance(backend, CliBackend)
    with pytest.raises(ValueError, match="must contain"):
        build_backend({"type": "cli", "command": ["echo", "hi"]})


# --------------------------------------------------------- end-to-end buildout


async def test_buildout_files_come_home(tmp_path) -> None:
    """Dispatch → worker builds in its workspace → files land on the hub → the
    requester pulls them with task_files/task_file. The full journey."""
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            command = _fake_agent_script(
                tmp_path,
                "import pathlib\n"
                "pathlib.Path('index.html').write_text('<h1>Farmhand Coffee</h1>')\n"
                "sub = pathlib.Path('css'); sub.mkdir(exist_ok=True)\n"
                "(sub / 'styles.css').write_text('body { background: #111 }')\n"
                "print('wrote index.html and css/styles.css')\n",
            )
            config = AgentConfig(
                name="carpenter",
                hub=hub.url,
                backend={"type": "cli", "command": command, "model": "fake/agent"},
                tools=ToolsConfig(file_root=str(tmp_path / "scratch")),
            )
            harness = Harness(config)
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("carpenter"))
            try:
                submitted = await claude.call_tool(
                    "submit_task",
                    {"title": "build the page", "spec": "build it", "assignee": "carpenter"},
                )
                task_id = submitted.data["task_id"]
                await wait_for(lambda: hub.store.require_task(task_id).state == "completed")

                listing = await claude.call_tool("task_files", {"task_id": task_id})
                paths = {f["path"] for f in listing.data["files"]}
                assert paths == {"index.html", "css/styles.css"}
                assert all("artifact_id" in f for f in listing.data["files"])

                page = await claude.call_tool(
                    "task_file", {"task_id": task_id, "path": "index.html"}
                )
                assert page.data["content"] == "<h1>Farmhand Coffee</h1>"

                css = await claude.call_tool(
                    "task_file", {"task_id": task_id, "path": "css/styles.css"}
                )
                assert "background" in css.data["content"]

                missing = await claude.call_tool(
                    "task_file", {"task_id": task_id, "path": "nope.txt"}
                )
                assert missing.data["error"]["code"] == "not_found"
            finally:
                runner.cancel()
                import contextlib

                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()


async def test_file_tool_tasks_are_jailed_per_task_and_returned(tmp_path) -> None:
    """Chat-backend workers with file tools get the same journey: their writes go to a
    per-task workspace and come back in the manifest."""
    from .mock_backend import tool_then_answer

    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            harness, runner = await start_agent(
                hub,
                "scribe",
                tool_then_answer(
                    "file_write", {"path": "notes.md", "content": "# notes"}, "Wrote notes.md."
                ),
                tools=ToolsConfig(allow=["file_write"], file_root=str(tmp_path)),
            )
            try:
                submitted = await claude.call_tool(
                    "submit_task", {"title": "notes", "spec": "write notes", "assignee": "scribe"}
                )
                task_id = submitted.data["task_id"]
                await wait_for(lambda: hub.store.require_task(task_id).state == "completed")

                # The file physically lives in the task's own workspace…
                assert (tmp_path / task_id / "notes.md").read_text() == "# notes"
                # …and is retrievable through the hub.
                fetched = await claude.call_tool(
                    "task_file", {"task_id": task_id, "path": "notes.md"}
                )
                assert fetched.data["content"] == "# notes"
            finally:
                await stop_agent(harness, runner)


# -------------------------------------------------------------------- wait_task


async def test_wait_task_returns_early_on_completion() -> None:
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            harness, runner = await start_agent(hub, "quick", lambda s, t: "done fast")
            try:
                submitted = await claude.call_tool(
                    "submit_task", {"title": "t", "spec": "s", "assignee": "quick"}
                )
                task_id = submitted.data["task_id"]

                waited = await claude.call_tool("wait_task", {"task_id": task_id, "wait_s": 30})
                assert waited.data["done"] is True
                assert waited.data["task"]["state"] == "completed"
            finally:
                await stop_agent(harness, runner)


async def test_a_cli_agent_that_builds_nothing_and_talks_tool_json_fails(tmp_path) -> None:
    """A CLI worker whose model prints tool calls as text produces an empty workspace
    and tool-shaped prose. That must fail, not complete — the requester would otherwise
    believe a buildout happened."""
    command = _fake_agent_script(
        tmp_path,
        'print(\'{"name": "file_write", "arguments": {"path": "index.html"}}\')\n'
        'print(\'{"name": "shell", "arguments": {"command": "ls"}}\')\n',
    )
    async with live_hub() as hub:
        mcp = build_mcp(hub.store, HubConfig(db_path=":memory:", default_identity="claude:test"))
        async with Client(mcp) as claude:
            config = AgentConfig(
                name="pretender",
                hub=hub.url,
                backend={"type": "cli", "command": command, "model": "fake/agent"},
                tools=ToolsConfig(file_root=str(tmp_path / "scratch")),
            )
            harness = Harness(config)
            runner = asyncio.create_task(harness.run())
            await wait_for(lambda: hub.store.bus.subscriber_count("pretender"))
            try:
                submitted = await claude.call_tool(
                    "submit_task", {"title": "build", "spec": "build it", "assignee": "pretender"}
                )
                task_id = submitted.data["task_id"]
                await wait_for(lambda: hub.store.require_task(task_id).state == "failed")
                assert "cannot" in hub.store.require_task(task_id).error
            finally:
                runner.cancel()
                import contextlib

                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await runner
                await harness.aclose()
