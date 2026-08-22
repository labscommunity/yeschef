"""Reference agent runtime.

Turns a local model endpoint into a named agent that joins rooms, converses with Claude
Code and with other agents, and claims and works tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import re

from ..models import EventKind, ReplyWhen, ToolCall, TurnPolicy
from ..sdk import AgentClient, HubClientError
from ..tools.executor import ToolExecutor
from .backends import Turn, build_backend
from .backends.base import Backend, ToolResult
from .config import AgentConfig

log = logging.getLogger("farmteam.agent")


def agent_token_path(name: str):
    """Where this agent remembers its own registration token across restarts."""
    from ..settings import home

    safe = name.replace("/", "_").replace(":", "_")
    return home() / "tokens" / f"{safe}.token"


HEARTBEAT_INTERVAL_S = 10.0
MAX_TRACKED_ROOMS = 256
MAX_RETURN_FILES = 40
MAX_RETURN_FILE_BYTES = 512 * 1024
TASK_ROOM_PREFIX = "room_task_"
"""A task's room is `room_` + the task id, and task ids start with `task_`."""


class Harness:
    def __init__(self, config: AgentConfig, backend: Backend | None = None) -> None:
        """`backend` may be supplied directly when embedding the harness or testing."""
        self.config = config
        self.backend: Backend = backend or build_backend(config.backend)
        self.tools = ToolExecutor(config.tools)
        self.client = AgentClient(
            config.hub,
            config.name,
            register_token=config.register_token,
            token_path=agent_token_path(config.name),
        )
        self._running_tasks: dict[str, asyncio.Task] = {}
        self._input_waiters: dict[str, asyncio.Queue] = {}
        self._room_locks: dict[str, asyncio.Lock] = {}
        self._claim_lock = asyncio.Lock()
        self._event_tasks: set[asyncio.Task] = set()
        self._task_context: dict[str, list[str]] = {}
        self._stopping = asyncio.Event()

    # ------------------------------------------------------------- lifecycle

    async def run(self) -> None:
        await self.client.register(
            kind="worker",
            node=self.config.node,
            backend=self.config.backend_label(),
            tags=self.config.tags,
        )
        log.info(
            "registered %s on %s (%s), tools=%s",
            self.config.name,
            self.config.node,
            self.config.backend_label(),
            self.config.tools.allow or "none",
        )
        heartbeat = asyncio.create_task(self._heartbeat_loop())
        try:
            await self._drain_queued_tasks()
            async for event in self.client.events():
                if self._stopping.is_set():
                    break
                if event.get("kind") == "connected":
                    # A reconnect means we may have missed announcements while away.
                    self._spawn(self._drain_queued_tasks())
                    continue
                self._spawn(self._safe_handle(event))
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await heartbeat
            await self.aclose()

    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._event_tasks.add(task)
        task.add_done_callback(self._event_tasks.discard)
        return task

    async def aclose(self) -> None:
        self._stopping.set()
        for task in list(self._running_tasks.values()):
            task.cancel()
        for task in list(self._event_tasks):
            task.cancel()
        await self.backend.close()
        await self.client.close()

    async def _heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(HEARTBEAT_INTERVAL_S)
            with contextlib.suppress(Exception):
                await self.client.heartbeat()

    async def _drain_queued_tasks(self) -> None:
        """Claim queued work up to capacity.

        Run at startup (for tasks queued while this agent was down) and after each task
        finishes, since nothing re-announces a task that is already queued.
        """
        with contextlib.suppress(HubClientError):
            while len(self._running_tasks) < self.config.max_concurrent_tasks:
                task = await self.client.next_task()
                if not task or not await self._try_claim(task):
                    return

    async def _safe_handle(self, event: dict) -> None:
        try:
            await self._handle_event(event)
        except Exception:
            log.exception("error handling event %s", event.get("kind"))

    async def _handle_event(self, event: dict) -> None:
        kind = event.get("kind")
        if kind == EventKind.MESSAGE:
            await self._on_message(event)
        elif kind == EventKind.FLOOR_GRANTED:
            await self._on_floor(event)
        elif kind == EventKind.TASK_ASSIGNED:
            await self._try_claim(event["task"])
        elif kind == EventKind.TASK_CANCELLED:
            running = self._running_tasks.get(event["task_id"])
            if running:
                running.cancel()
        elif kind == EventKind.TASK_UPDATED and "input" in event:
            queue = self._input_waiters.get(event["task"]["id"])
            if queue:
                queue.put_nowait(f"{event.get('by', 'operator')}: {event['input']}")

    # -------------------------------------------------------------- messaging

    def _room_lock(self, room_id: str) -> asyncio.Lock:
        if len(self._room_locks) > MAX_TRACKED_ROOMS:
            for stale in [k for k, v in self._room_locks.items() if not v.locked()][
                : len(self._room_locks) // 2
            ]:
                self._room_locks.pop(stale, None)
        return self._room_locks.setdefault(room_id, asyncio.Lock())

    async def _on_message(self, event: dict) -> None:
        message = event["message"]
        room_id = message["room_id"]
        room = await self.client.get_room(room_id)
        if room["archived"]:
            return
        if room["id"].startswith(TASK_ROOM_PREFIX):
            # Not a conversation to reply to: it is extra context for the task in flight,
            # which is what the hub's `task_room` tool advertises.
            task_id = room["id"].removeprefix("room_")
            if task_id in self._running_tasks:
                self._task_context.setdefault(task_id, []).append(
                    f"{message['sender']}: {message['body']}"
                )
            return
        policy = room.get("policy") or {}
        if policy.get("turn_policy") == TurnPolicy.ROUND_ROBIN:
            # Replies are driven by floor grants — but a grant can arrive before the
            # room's first message exists and be unusable. If the hub still shows us
            # holding the floor when a message lands, that message is our cue.
            if room.get("floor_holder") == self.config.name:
                await self._reply_in_room(room_id)
            return
        # A direct message is addressed to me by definition — always answer it, whatever
        # the group-room reply policy is. `reply_when` governs multi-party rooms only.
        if not room.get("is_dm"):
            if self.config.reply_when is ReplyWhen.MENTIONED:
                if self.config.name not in (message.get("mentions") or []):
                    return
            elif self.config.reply_when is ReplyWhen.ROUND_ROBIN:
                return
        await self._reply_in_room(room_id)

    async def _on_floor(self, event: dict) -> None:
        room_id = event["room_id"]
        room = await self.client.get_room(room_id)
        if room["archived"]:
            return
        history = await self.client.messages(room_id, limit=1)
        if not history:
            return  # floor granted before the room was seeded; wait for the opener
        await self._reply_in_room(room_id)

    async def _reply_in_room(self, room_id: str) -> None:
        async with self._room_lock(room_id):
            window = await self.client.messages(
                room_id, limit=self.config.max_context_messages, tail=True
            )
            if not window:
                return
            if window[-1]["sender"] == self.config.name:
                return  # nothing new since our last turn

            room = await self.client.get_room(room_id)
            turns = self._turns_from_messages(window)
            system = (
                f"{self.config.rendered_system_prompt()}\n\n"
                f"You are in a conversation titled '{room['topic']}' with: "
                f"{', '.join(m for m in room['members'] if m != self.config.name)}. "
                "Messages from others are prefixed with their name. Reply as yourself only — "
                "never write another participant's turn."
            )
            try:
                result, _ = await self._run_model(system, turns)
                body = (result.text or "").strip()
            except Exception:
                log.exception("model call failed in room %s", room_id)
                body = ""

            if not body:
                # Holding the floor and saying nothing would stall the room for everyone.
                with contextlib.suppress(HubClientError):
                    await self.client.yield_floor(room_id)
                return
            with contextlib.suppress(HubClientError):
                await self.client.post(room_id, body, tokens=result.total_tokens or None)

    def _turns_from_messages(self, messages: list[dict]) -> list[Turn]:
        turns: list[Turn] = []
        for message in messages:
            if message["sender"] == self.config.name:
                turns.append(Turn(role="assistant", content=message["body"]))
            else:
                turns.append(Turn(role="user", content=f"{message['sender']}: {message['body']}"))
        if turns and turns[0].role == "assistant":
            turns.insert(0, Turn(role="user", content="(conversation continues)"))
        return turns

    # ------------------------------------------------------------------ tasks

    async def _try_claim(self, task: dict) -> bool:
        """Claim a task if there is capacity. Serialized so concurrent events cannot
        both slip past the capacity check and claim more work than this agent can run."""
        task_id = task["id"]
        async with self._claim_lock:
            if task_id in self._running_tasks:
                return False
            if len(self._running_tasks) >= self.config.max_concurrent_tasks:
                return False
            try:
                claimed = await self.client.claim(task_id)
            except HubClientError as exc:
                if exc.code != "conflict":
                    log.warning("claim failed for %s: %s", task_id, exc)
                return False
            runner = asyncio.create_task(self._work_task(claimed))
            self._running_tasks[task_id] = runner
            runner.add_done_callback(self._release_slot(task_id))
            return True

    def _release_slot(self, task_id: str):
        def done(_: asyncio.Task) -> None:
            self._running_tasks.pop(task_id, None)
            self._task_context.pop(task_id, None)
            if not self._stopping.is_set():
                # Free capacity: look for work that was queued while we were busy.
                asyncio.create_task(self._drain_queued_tasks())

        return done

    async def _work_task(self, task: dict) -> None:
        task_id = task["id"]
        workspace = self._task_workspace(task_id)
        try:
            await self.client.progress(task_id, pct=5.0, message="started")
            system = (
                f"{self.config.rendered_system_prompt()}\n\n"
                "You have been given a task. Work it to completion and finish with a clear "
                "summary of what you did and what the answer is. If you genuinely cannot "
                "proceed without a decision only the requester can make, say exactly "
                "NEED_INPUT: followed by your question."
            )
            if workspace is not None:
                system += (
                    "\n\nWork inside your current workspace directory. Files you create "
                    "there are returned to the requester when you finish."
                )
            turns = [Turn(role="user", content=f"Task: {task['title']}\n\n{task['spec']}")]

            for round_index in range(4):
                self._drain_task_context(task_id, turns)
                result, tool_rounds = await self._run_model(system, turns, task_id=task_id)
                text = (result.text or "").strip()

                if (
                    self.tools.enabled
                    and tool_rounds == 0
                    and _looks_like_unexecuted_tool_calls(text)
                ):
                    # Some models describe a tool call in prose or JSON instead of
                    # emitting one. Reporting that as a finished task is worse than
                    # failing: the requester believes work happened that never did.
                    await self.client.fail(
                        task_id,
                        "model wrote tool-call-like text that could not be parsed into any "
                        f"granted tool — nothing was executed. Model {self.backend.model} "
                        "may not support tool calling; try one that does.",
                    )
                    return

                if text.startswith("NEED_INPUT:") or "\nNEED_INPUT:" in text:
                    question = text.split("NEED_INPUT:", 1)[1].strip()
                    answer = await self._await_input(task_id, question)
                    if answer is None:
                        await self.client.fail(task_id, "no input provided before timeout")
                        return
                    turns.append(Turn(role="assistant", content=text))
                    turns.append(Turn(role="user", content=answer))
                    continue

                payload = {
                    "text": text,
                    "tokens": result.total_tokens,
                    "rounds": round_index + 1,
                    "tool_rounds": tool_rounds,
                    "model": self.backend.model,
                    "spec_chars": len(task.get("spec") or ""),
                }
                if result.stop_reason in ("length", "max_tokens"):
                    # The model hit its output ceiling — a half-finished payload must
                    # never masquerade as a clean completion.
                    payload["truncated"] = True
                    payload["stop_reason"] = result.stop_reason
                    payload["max_tokens_ceiling"] = self.config.max_tokens
                    payload["text"] = text + (
                        "\n\n[worker warning: output hit the max_tokens ceiling "
                        f"({self.config.max_tokens}) — this result is likely truncated; "
                        "re-dispatch in smaller pieces]"
                    )
                files = await self._collect_workspace(task_id, workspace)
                if files is not None:
                    payload["files"] = files
                if (
                    getattr(self.backend, "uses_workspace", False)
                    and not files
                    and _looks_like_unexecuted_tool_calls(text)
                ):
                    # A CLI agent whose model prints tool calls as text builds nothing.
                    # An empty workspace plus tool-call-shaped prose is that signature.
                    await self.client.fail(
                        task_id,
                        "the CLI agent produced no files and its output reads like "
                        "unexecuted tool calls — the underlying model likely cannot "
                        "drive this harness's tools. Try a stronger or tool-capable "
                        "model.",
                    )
                    return
                await self.client.complete(task_id, payload)
                return

            await self.client.fail(task_id, "exceeded input rounds without completing")
        except asyncio.CancelledError:
            log.info("task %s cancelled", task_id)
            raise
        except HubClientError as exc:
            log.warning("hub rejected update for %s: %s", task_id, exc)
        except Exception as exc:  # noqa: BLE001 - report failure rather than die silently
            log.exception("task %s failed", task_id)
            where = (
                f"{self.config.name}@{self.config.node} "
                f"({self.backend.name}/{self.backend.model} at "
                f"{getattr(self.backend, 'base_url', 'n/a')} on that node)"
            )
            with contextlib.suppress(HubClientError):
                await self.client.fail(task_id, f"{where}: {type(exc).__name__}: {exc}")

    def _task_workspace(self, task_id: str):
        """A fresh directory per task, jailing its tools and collecting its output.

        Exists when the agent can produce files at all — file tools or a CLI agent.
        Without it, a task's files land somewhere on the worker with no way back to
        the requester (the exact failure the first live buildout hit).
        """
        from pathlib import Path

        wants = getattr(self.backend, "uses_workspace", False) or any(
            tool.startswith("file") for tool in self.config.tools.allow
        )
        if not wants:
            return None
        root = Path(self.config.tools.file_root or "~/agent-scratch").expanduser()
        workspace = root / task_id
        workspace.mkdir(parents=True, exist_ok=True)
        if getattr(self.backend, "uses_workspace", False):
            self.backend.workspace = str(workspace)
        return workspace

    async def _collect_workspace(self, task_id: str, workspace) -> list[dict] | None:
        """Ship every file the task produced to the hub, so the requester can pull it."""
        if workspace is None:
            return None
        skip_dirs = {".git", "node_modules", "__pycache__", ".venv"}
        manifest: list[dict] = []
        files = [
            f
            for f in sorted(workspace.rglob("*"))
            if f.is_file() and not (set(f.relative_to(workspace).parts) & skip_dirs)
        ]
        for path in files[:MAX_RETURN_FILES]:
            rel = str(path.relative_to(workspace))
            content = path.read_bytes()
            if len(content) > MAX_RETURN_FILE_BYTES:
                manifest.append({"path": rel, "bytes": len(content), "skipped": "too large"})
                continue
            try:
                artifact = await self.client.upload_artifact(rel, content)
            except HubClientError as exc:
                manifest.append({"path": rel, "bytes": len(content), "skipped": str(exc)})
                continue
            manifest.append({"path": rel, "bytes": len(content), "artifact_id": artifact["id"]})
        if len(files) > MAX_RETURN_FILES:
            manifest.append(
                {"path": f"(+{len(files) - MAX_RETURN_FILES} more)", "skipped": "file cap"}
            )
        return manifest

    def _drain_task_context(self, task_id: str, turns: list[Turn]) -> None:
        """Fold messages posted into the task's room into the working context."""
        pending = self._task_context.pop(task_id, None)
        if pending:
            turns.append(Turn(role="user", content="Additional context:\n" + "\n".join(pending)))

    async def _await_input(self, task_id: str, question: str, timeout_s: float = 3600.0):
        queue: asyncio.Queue = asyncio.Queue()
        self._input_waiters[task_id] = queue
        try:
            await self.client.request_input(task_id, question)
            return await asyncio.wait_for(queue.get(), timeout=timeout_s)
        except TimeoutError:
            return None
        finally:
            self._input_waiters.pop(task_id, None)

    # ------------------------------------------------------------ model loop

    def _task_tools(self, task_id: str | None):
        """File tools jailed to the task's own workspace, not the shared root."""
        if task_id is None or not self.tools.enabled:
            return self.tools
        from dataclasses import replace as dc_replace

        from ..tools.executor import ToolExecutor

        root = str(self._task_workspace(task_id) or self.tools.root or "~/agent-scratch")
        return ToolExecutor(dc_replace(self.config.tools, file_root=root))

    async def _run_model(self, system: str, turns: list[Turn], task_id: str | None = None):
        """One model call, plus the tool loop if this agent has tools enabled.

        Returns (result, tool_rounds) — the count matters because a reply that merely
        *describes* tool calls is only suspicious when no tool actually ran.
        """
        executor = self._task_tools(task_id)
        specs = executor.specs() if executor.enabled else None
        allowed = {s["name"] for s in specs} if specs else set()
        result = await self.backend.chat(
            system,
            turns,
            tools=specs,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )
        iterations = 0
        while iterations < self.config.max_tool_iterations:
            if not result.tool_calls and specs:
                # Many local models (qwen2.5-coder among them) emit tool calls as
                # text instead of using the native protocol. Recover them: a parsed,
                # validated text-form call executes exactly like a native one.
                result.tool_calls = _parse_text_tool_calls(result.text or "", allowed)
            if not result.tool_calls:
                break
            iterations += 1
            if task_id:
                names = ", ".join(call.name for call in result.tool_calls)
                with contextlib.suppress(HubClientError):
                    await self.client.progress(
                        task_id,
                        pct=min(90.0, 10.0 + iterations * 15.0),
                        message=f"tools: {names}",
                    )
            results: list[ToolResult] = [await executor.run(call) for call in result.tool_calls]
            turns.append(Turn(role="assistant", content=result.text, tool_calls=result.tool_calls))
            turns.append(Turn(role="user", content="", tool_results=results))
            result = await self.backend.chat(
                system,
                turns,
                tools=specs,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
        return result, iterations


def _parse_text_tool_calls(text: str, allowed: set[str]) -> list[ToolCall]:
    """Recover tool calls a model wrote as text instead of emitting natively.

    Handles the shapes local models actually produce: qwen's <tool_call>{...}</tool_call>
    tags, fenced ```json blocks, OpenAI-style {"function": {"name", "arguments"}}
    nesting, arguments as a JSON-encoded string, and bare one-object lines. Only calls
    naming an allowed tool are returned — everything else stays plain text.
    """
    candidates: list[str] = []
    for m in re.finditer(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", text, re.DOTALL):
        candidates.append(m.group(1))
    for m in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(m.group(1))
    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        candidates.append(stripped)
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("{") and line.endswith("}") and '"name"' in line:
            candidates.append(line)

    calls: list[ToolCall] = []
    seen: set[str] = set()
    for raw in candidates:
        if raw in seen:
            continue
        seen.add(raw)
        try:
            obj = json.loads(raw)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        if isinstance(obj.get("function"), dict):
            obj = obj["function"]
        name = obj.get("name")
        args = obj.get("arguments", obj.get("parameters", {}))
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except ValueError:
                continue
        if name in allowed and isinstance(args, dict):
            calls.append(ToolCall(id=f"text_{len(calls)}", name=name, arguments=args))
    return calls


def _looks_like_unexecuted_tool_calls(text: str) -> bool:
    """Detect a reply that *describes* tool calls rather than making them."""
    if not text:
        return False
    lowered = text.lower()
    signals = (
        '"name": "file_write"',
        '"name": "file_read"',
        '"name": "shell"',
        '"arguments":',
        '"tool_call"',
        '"function":',
    )
    hits = sum(1 for token in signals if token in lowered)
    return hits >= 2


async def run_agent(config: AgentConfig) -> None:
    harness = Harness(config)
    try:
        await harness.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await harness.aclose()
