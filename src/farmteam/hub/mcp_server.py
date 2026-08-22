"""FastMCP tool surface — the Claude Code face of the hub.

Ergonomics mirror Claude Code's cross-session messaging (`ListAgents` / `SendMessage`),
extended with task dispatch. Every tool returns in milliseconds; the only wait is the
explicit `wait_s` on `fetch_messages`, capped well under Claude Code's ~2 minute
auto-background threshold.

Tools are written so they can later be marked `task=True` for the MCP Tasks extension
(SEP-2663) without changing their signatures.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools

from fastmcp import FastMCP

from ..models import (
    MAX_LONG_POLL_S,
    AgentKind,
    ErrorCode,
    HubError,
    RoomPolicy,
    TaskState,
    TurnPolicy,
)
from .api import HubConfig
from .store import Store

MAX_FILE_FETCH_BYTES = 200 * 1024
"""task_file responses land in Claude's context; larger files go over HTTP instead."""


class IdentityResolver:
    """Maps an MCP session to a hub identity, defaulting to one shared local label."""

    def __init__(self, store: Store, default_identity: str) -> None:
        self.store = store
        from ..settings import env

        self.default = env("IDENTITY", default_identity) or default_identity
        self._by_session: dict[str, str] = {}
        self._cursors: dict[str, int] = {}

    def _session_id(self) -> str:
        try:
            from fastmcp.server.dependencies import get_context

            ctx = get_context()
        except Exception:
            return "default"
        for attr in ("session_id", "client_id", "request_id"):
            value = getattr(ctx, attr, None)
            if isinstance(value, str) and value:
                return value
        return "default"

    def current(self) -> str:
        name = self._by_session.get(self._session_id(), self.default)
        self.store.ensure_identity(name, AgentKind.CLAUDE)
        return name

    def rename(self, label: str) -> str:
        old = self.current()
        new = label if label.startswith("claude:") else f"claude:{label}"
        self.store.rename_identity(old, new, AgentKind.CLAUDE)
        self._by_session[self._session_id()] = new
        if old in self._cursors:
            self._cursors[new] = self._cursors.pop(old)
        return new

    def cursor(self, identity: str) -> int:
        return self._cursors.get(identity, 0)

    def set_cursor(self, identity: str, value: int) -> None:
        self._cursors[identity] = value


def build_mcp(store: Store, config: HubConfig) -> FastMCP:
    mcp = FastMCP(
        name="farmteam",
        instructions=(
            "Dispatch tasks to your farm team — AI agents you run yourself, usually local "
            "models on your own hardware — and "
            "hold multi-turn conversations with them. submit_task returns immediately with a "
            "task id; call "
            "task_status(id) at any time, from any session, to check on it. Use send_message "
            "for a direct back-and-forth with one agent, create_room/post for group "
            "conversations, and start_dialogue to have two or more local agents converse "
            "autonomously while you observe with room_transcript."
        ),
    )
    ident = IdentityResolver(store, config.default_identity)

    def _err(exc: HubError) -> dict:
        return exc.to_dict()

    def _guard(fn):
        """Surface HubError as structured data instead of a stack trace.

        `functools.wraps` keeps `__wrapped__` intact so FastMCP still derives the tool
        schema from the real signature.
        """

        if asyncio.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def wrapper(*args, **kwargs):
                try:
                    return await fn(*args, **kwargs)
                except HubError as exc:
                    return _err(exc)

        else:

            @functools.wraps(fn)
            def wrapper(*args, **kwargs):
                try:
                    return fn(*args, **kwargs)
                except HubError as exc:
                    return _err(exc)

        return wrapper

    # ------------------------------------------------------------- identity

    @mcp.tool
    @_guard
    def set_identity(label: str) -> dict:
        """Name this Claude Code session so agents can address it (e.g. "mac-mini-main").

        Room membership and pending messages follow the rename.
        """
        return {"identity": ident.rename(label)}

    @mcp.tool
    @_guard
    def whoami() -> dict:
        """Show this session's hub identity and unread cursor."""
        me = ident.current()
        return {"identity": me, "cursor": ident.cursor(me)}

    @mcp.tool
    @_guard
    def list_agents(include_offline: bool = True) -> dict:
        """List agents on the fleet: name, node, model backend, tags, online status."""
        ref_agents = [a.to_dict() for a in store.list_agents()]
        if not include_offline:
            ref_agents = [a for a in ref_agents if a["status"] != "offline"]
        return {"agents": ref_agents, "me": ident.current()}

    # ------------------------------------------------------------ messaging

    @mcp.tool
    @_guard
    def send_message(to: str, message: str, data: dict | None = None) -> dict:
        """Send a direct message to one agent, creating or reusing the 1:1 room.

        Returns immediately. The agent's reply arrives via fetch_messages.
        """
        me = ident.current()
        store.require_agent(to)
        room = store.get_or_create_dm(me, to)
        posted = store.post_message(room.id, me, message, data=data)
        return {"room_id": room.id, "message": posted.to_dict()}

    @mcp.tool
    @_guard
    def post(
        room: str, message: str, data: dict | None = None, reply_to: str | None = None
    ) -> dict:
        """Post into a room. Use @name to address a specific participant.

        In a round-robin room this interjects out of turn and re-anchors the conversation
        on the next agent in the ring.
        """
        me = ident.current()
        posted = store.post_message(room, me, message, data=data, reply_to=reply_to)
        return {"message": posted.to_dict()}

    @mcp.tool
    @_guard
    async def fetch_messages(
        after: int | None = None,
        room: str | None = None,
        wait_s: float = 0.0,
        limit: int = 50,
    ) -> dict:
        """Fetch messages addressed to this session since the last call.

        Pass `wait_s` (max 60) to wait for the next message instead of returning empty.
        The returned `cursor` is remembered, so calling with no arguments picks up where
        you left off.
        """
        me = ident.current()
        start = ident.cursor(me) if after is None else after
        messages, cursor = store.fetch_inbox(me, start, limit, room)

        if not messages and wait_s > 0:
            budget = min(wait_s, MAX_LONG_POLL_S)
            async with store.bus.subscribe(me) as queue:
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(queue.get(), timeout=budget)
            messages, cursor = store.fetch_inbox(me, start, limit, room)

        if room is None:
            # A room-scoped read is a peek, not a receipt: advancing the shared cursor
            # here would silently drop unread messages from every other room.
            ident.set_cursor(me, cursor)
        return {"messages": messages, "cursor": cursor, "identity": me}

    @mcp.tool
    @_guard
    def create_room(
        topic: str,
        participants: list[str],
        turn_policy: str = "free",
        max_messages: int | None = None,
        max_total_tokens: int | None = None,
        idle_timeout_s: float | None = None,
        stop_phrase: str | None = None,
        open_room: bool = False,
    ) -> dict:
        """Create an N-party room with this session and the named agents in it.

        Policy guards are enforced by the hub: the room archives itself when it hits
        max_messages, max_total_tokens, idle_timeout_s, or sees stop_phrase.
        """
        me = ident.current()
        for name in participants:
            store.require_agent(name)
        policy = RoomPolicy(
            turn_policy=TurnPolicy(turn_policy),
            max_messages=max_messages,
            max_total_tokens=max_total_tokens,
            idle_timeout_s=idle_timeout_s,
            stop_phrase=stop_phrase,
        )
        room = store.create_room(topic, me, participants, policy, open_room=open_room)
        return {"room": room.to_dict()}

    @mcp.tool
    @_guard
    def join_room(room: str) -> dict:
        """Join an existing room as this session, invited or not."""
        return {"room": store.join_room(room, ident.current(), privileged=True).to_dict()}

    @mcp.tool
    @_guard
    def leave_room(room: str) -> dict:
        """Leave a room; the conversation continues without this session."""
        store.leave_room(room, ident.current())
        return {"ok": True, "room_id": room}

    @mcp.tool
    @_guard
    def list_rooms(mine_only: bool = True, include_archived: bool = False) -> dict:
        """List rooms, by default only those this session participates in."""
        me = ident.current()
        rooms = store.list_rooms(agent=me if mine_only else None, include_archived=include_archived)
        return {"rooms": [r.to_dict() for r in rooms]}

    @mcp.tool
    @_guard
    def room_transcript(room: str, from_seq: int = 0, limit: int = 100) -> dict:
        """Read a room's transcript — how you observe an autonomous agent dialogue."""
        target = store.require_room(room)
        messages = store.fetch_messages(room, after_seq=from_seq, limit=limit)
        return {
            "room": target.to_dict(),
            "messages": [m.to_dict() for m in messages],
            "next_seq": messages[-1].seq if messages else from_seq,
        }

    @mcp.tool
    @_guard
    def archive_room(room: str, reason: str = "closed by operator") -> dict:
        """Stop a conversation. Archived rooms reject further messages."""
        archived = store.archive_room(room, reason, by=ident.current(), privileged=True)
        return {"room": archived.to_dict()}

    @mcp.tool
    @_guard
    def start_dialogue(
        participants: list[str],
        goal: str,
        max_messages: int = 40,
        max_total_tokens: int | None = None,
        stop_phrase: str | None = None,
        turn_policy: str = "round_robin",
        topic: str | None = None,
    ) -> dict:
        """Have local agents converse with each other autonomously toward a goal.

        Creates a bounded room, seeds it with the goal, and hands the floor to the first
        agent. Returns immediately — observe with room_transcript, steer by posting into
        the room, stop with archive_room.
        """
        me = ident.current()
        if not participants:
            raise HubError(ErrorCode.INVALID, "need at least one participant")
        for name in participants:
            store.require_agent(name)
        policy = RoomPolicy(
            turn_policy=TurnPolicy(turn_policy),
            max_messages=max_messages,
            max_total_tokens=max_total_tokens,
            stop_phrase=stop_phrase,
        )
        room = store.create_room(
            topic=topic or f"dialogue: {goal[:60]}",
            created_by=me,
            participants=participants,
            policy=policy,
        )
        seeded = store.post_message(room.id, me, goal, data={"role": "goal"})
        return {
            "room_id": room.id,
            "room": store.require_room(room.id).to_dict(),
            "seed_message": seeded.to_dict(),
        }

    # ---------------------------------------------------------------- tasks

    @mcp.tool
    @_guard
    def submit_task(
        title: str,
        spec: str,
        assignee: str | None = None,
        selector: str | None = None,
        priority: int = 0,
        timeout_s: float = 3600.0,
        dedupe_key: str | None = None,
    ) -> dict:
        """Dispatch a background task to a local agent and return its id immediately.

        Target one agent by name with `assignee`, or any agent carrying a tag with
        `selector` (e.g. "tier:reasoning") — the first idle match claims it. Check on it
        later with task_status(task_id) from any session.
        """
        me = ident.current()
        if assignee:
            store.require_agent(assignee)
        task = store.submit_task(
            title=title,
            spec=spec,
            created_by=me,
            assignee=assignee,
            selector=selector,
            priority=priority,
            timeout_s=timeout_s,
            dedupe_key=dedupe_key,
        )
        return {"task_id": task.id, "task": task.to_dict()}

    @mcp.tool
    @_guard
    def task_status(task_id: str, event_limit: int = 10) -> dict:
        """Check a task's state, progress, and recent events. Safe to call at any time."""
        task = store.require_task(task_id)
        return {
            "task": task.to_dict(),
            "events": [e.to_dict() for e in store.task_events(task_id, event_limit)],
        }

    @mcp.tool
    @_guard
    def task_result(task_id: str) -> dict:
        """Fetch a finished task's result (or the reason it is not finished)."""
        task = store.require_task(task_id)
        payload = {
            "task_id": task.id,
            "state": str(task.state),
            "result": task.result,
            "error": task.error,
            "ready": task.state.terminal,
        }
        if task.state.terminal:
            # The payoff moment gets the running total — the houtini-style counter
            # that keeps the value of the fleet visible without bloating every call.
            payload["lifetime"] = store.format_stats(store.lifetime_stats())
        return payload

    @mcp.tool
    @_guard
    def list_tasks(
        state: str | None = None,
        assignee: str | None = None,
        mine_only: bool = False,
        limit: int = 50,
    ) -> dict:
        """List tasks, optionally filtered by state, agent, or this session's own."""
        tasks = store.list_tasks(
            state=TaskState(state) if state else None,
            assignee=assignee,
            created_by=ident.current() if mine_only else None,
            limit=limit,
        )
        return {"tasks": [t.to_dict() for t in tasks]}

    @mcp.tool
    @_guard
    def cancel_task(task_id: str) -> dict:
        """Cancel a queued or running task. The agent is told to stop."""
        cancelled = store.cancel_task(task_id, ident.current(), privileged=True)
        return {"task": cancelled.to_dict()}

    @mcp.tool
    @_guard
    def provide_input(task_id: str, message: str) -> dict:
        """Answer an agent that parked a task in input_required, resuming it."""
        return {"task": store.provide_input(task_id, ident.current(), message).to_dict()}

    @mcp.tool
    @_guard
    async def wait_task(task_id: str, wait_s: float = 60.0) -> dict:
        """Wait (up to 60s) for a task to change state or finish, then report it.

        The efficient way to watch a task: one call per minute instead of a tight
        polling loop. Returns immediately once the task reaches a terminal state.
        """
        task = store.require_task(task_id)
        initial = str(task.state)
        deadline = asyncio.get_running_loop().time() + min(wait_s, MAX_LONG_POLL_S)
        while asyncio.get_running_loop().time() < deadline:
            if task.state.terminal or str(task.state) != initial:
                break
            await asyncio.sleep(2.0)
            task = store.require_task(task_id)
        return {
            "task": task.to_dict(),
            "changed": str(task.state) != initial,
            "done": task.state.terminal,
        }

    @mcp.tool
    @_guard
    def task_files(task_id: str) -> dict:
        """List the files a completed task produced on its worker.

        Follow up with task_file(task_id, path) to pull each one and write it into
        the project — this is how a buildout dispatched to another machine comes home.
        """
        task = store.require_task(task_id)
        files = (task.result or {}).get("files") or []
        return {"task_id": task.id, "state": str(task.state), "files": files}

    @mcp.tool
    @_guard
    def task_file(task_id: str, path: str) -> dict:
        """Fetch one file a task produced. Text comes back as `content`; binary as base64."""
        import base64

        task = store.require_task(task_id)
        manifest = (task.result or {}).get("files") or []
        entry = next((f for f in manifest if f.get("path") == path), None)
        if entry is None or "artifact_id" not in entry:
            raise HubError(
                ErrorCode.NOT_FOUND,
                f"task has no returned file '{path}' — task_files lists what came back",
                404,
            )
        meta, content = store.get_artifact(entry["artifact_id"])
        if len(content) > MAX_FILE_FETCH_BYTES:
            raise HubError(
                ErrorCode.INVALID,
                f"file is {len(content)} bytes; fetch it over HTTP: "
                f"/api/v1/artifacts/{entry['artifact_id']}",
            )
        try:
            return {"path": path, "content": content.decode("utf-8"), "bytes": meta["bytes"]}
        except UnicodeDecodeError:
            return {
                "path": path,
                "content_b64": base64.b64encode(content).decode(),
                "bytes": meta["bytes"],
            }

    @mcp.tool
    @_guard
    def task_room(task_id: str) -> dict:
        """Open (or fetch) the discussion room attached to a task.

        This is how a dispatched task becomes a multi-turn conversation: post into the
        room to give the working agent more context mid-flight.
        """
        task = store.require_task(task_id)
        room = store.ensure_task_room(task_id)
        me = ident.current()
        if me not in room.members:
            room = store.join_room(room.id, me, privileged=True)
        return {"room": room.to_dict(), "task_state": str(task.state)}

    return mcp


__all__ = ["build_mcp", "IdentityResolver"]
