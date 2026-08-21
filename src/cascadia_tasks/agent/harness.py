"""Reference agent runtime.

Turns a local model endpoint into a named agent that joins rooms, converses with Claude
Code and with other agents, and claims and works tasks.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

from ..models import EventKind, ReplyWhen, TurnPolicy
from ..sdk import AgentClient, HubClientError
from ..tools.executor import ToolExecutor
from .backends import Turn, build_backend
from .backends.base import Backend, ToolResult
from .config import AgentConfig

log = logging.getLogger("cascadia_tasks.agent")

HEARTBEAT_INTERVAL_S = 10.0
MAX_TRACKED_ROOMS = 256
TASK_ROOM_PREFIX = "room_task_"
"""A task's room is `room_` + the task id, and task ids start with `task_`."""


class Harness:
    def __init__(self, config: AgentConfig, backend: Backend | None = None) -> None:
        """`backend` may be supplied directly when embedding the harness or testing."""
        self.config = config
        self.backend: Backend = backend or build_backend(config.backend)
        self.tools = ToolExecutor(config.tools)
        self.client = AgentClient(config.hub, config.name, register_token=config.register_token)
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
            return  # replies are driven by floor grants
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
                result = await self._run_model(system, turns)
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
        try:
            await self.client.progress(task_id, pct=5.0, message="started")
            system = (
                f"{self.config.rendered_system_prompt()}\n\n"
                "You have been given a task. Work it to completion and finish with a clear "
                "summary of what you did and what the answer is. If you genuinely cannot "
                "proceed without a decision only the requester can make, say exactly "
                "NEED_INPUT: followed by your question."
            )
            turns = [Turn(role="user", content=f"Task: {task['title']}\n\n{task['spec']}")]

            for round_index in range(4):
                self._drain_task_context(task_id, turns)
                result = await self._run_model(system, turns, task_id=task_id)
                text = (result.text or "").strip()

                if text.startswith("NEED_INPUT:") or "\nNEED_INPUT:" in text:
                    question = text.split("NEED_INPUT:", 1)[1].strip()
                    answer = await self._await_input(task_id, question)
                    if answer is None:
                        await self.client.fail(task_id, "no input provided before timeout")
                        return
                    turns.append(Turn(role="assistant", content=text))
                    turns.append(Turn(role="user", content=answer))
                    continue

                await self.client.complete(
                    task_id,
                    {
                        "text": text,
                        "tokens": result.total_tokens,
                        "rounds": round_index + 1,
                        "model": self.backend.model,
                    },
                )
                return

            await self.client.fail(task_id, "exceeded input rounds without completing")
        except asyncio.CancelledError:
            log.info("task %s cancelled", task_id)
            raise
        except HubClientError as exc:
            log.warning("hub rejected update for %s: %s", task_id, exc)
        except Exception as exc:  # noqa: BLE001 - report failure rather than die silently
            log.exception("task %s failed", task_id)
            with contextlib.suppress(HubClientError):
                await self.client.fail(task_id, f"{type(exc).__name__}: {exc}")

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

    async def _run_model(self, system: str, turns: list[Turn], task_id: str | None = None):
        """One model call, plus the tool loop if this agent has tools enabled."""
        specs = self.tools.specs() if self.tools.enabled else None
        result = await self.backend.chat(
            system,
            turns,
            tools=specs,
            max_tokens=self.config.max_tokens,
            temperature=self.config.temperature,
        )
        iterations = 0
        while result.tool_calls and iterations < self.config.max_tool_iterations:
            iterations += 1
            if task_id:
                names = ", ".join(call.name for call in result.tool_calls)
                with contextlib.suppress(HubClientError):
                    await self.client.progress(
                        task_id,
                        pct=min(90.0, 10.0 + iterations * 15.0),
                        message=f"tools: {names}",
                    )
            results: list[ToolResult] = [await self.tools.run(call) for call in result.tool_calls]
            turns.append(Turn(role="assistant", content=result.text, tool_calls=result.tool_calls))
            turns.append(Turn(role="user", content="", tool_results=results))
            result = await self.backend.chat(
                system,
                turns,
                tools=specs,
                max_tokens=self.config.max_tokens,
                temperature=self.config.temperature,
            )
        return result


async def run_agent(config: AgentConfig) -> None:
    harness = Harness(config)
    try:
        await harness.run()
    except (KeyboardInterrupt, asyncio.CancelledError):
        await harness.aclose()
