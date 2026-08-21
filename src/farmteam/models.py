"""Shared types for hub, SDK, and harness.

Task states deliberately mirror the MCP Tasks extension vocabulary (SEP-2663) so the
tool surface can be upgraded to spec-native tasks without renaming anything.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import StrEnum

_ALPHABET = "23456789abcdefghjkmnpqrstuvwxyz"


def now() -> float:
    """Hub-assigned UTC epoch seconds. Agents never write times."""
    return time.time()


def new_id(prefix: str, length: int = 10) -> str:
    body = "".join(secrets.choice(_ALPHABET) for _ in range(length))
    return f"{prefix}_{body}"


class TaskState(StrEnum):
    QUEUED = "queued"
    CLAIMED = "claimed"
    WORKING = "working"
    INPUT_REQUIRED = "input_required"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def terminal(self) -> bool:
        return self in (TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED)

    @property
    def active(self) -> bool:
        """Held by an agent — subject to heartbeat reclamation."""
        return self in (TaskState.CLAIMED, TaskState.WORKING, TaskState.INPUT_REQUIRED)


class AgentKind(StrEnum):
    WORKER = "worker"
    CLAUDE = "claude"


class AgentStatus(StrEnum):
    ONLINE = "online"
    BUSY = "busy"
    OFFLINE = "offline"


class TurnPolicy(StrEnum):
    FREE = "free"
    ROUND_ROBIN = "round_robin"


class ReplyWhen(StrEnum):
    MENTIONED = "mentioned"
    ROUND_ROBIN = "round_robin"
    ALWAYS = "always"


class EventKind(StrEnum):
    MESSAGE = "message"
    TASK_ASSIGNED = "task_assigned"
    TASK_CANCELLED = "task_cancelled"
    TASK_UPDATED = "task_updated"
    ROOM_INVITE = "room_invite"
    FLOOR_GRANTED = "floor_granted"
    ROOM_ARCHIVED = "room_archived"
    SHUTDOWN = "shutdown"


class ErrorCode(StrEnum):
    NOT_FOUND = "not_found"
    CONFLICT = "conflict"
    FORBIDDEN = "forbidden"
    UNAUTHORIZED = "unauthorized"
    INVALID = "invalid"
    POLICY_EXCEEDED = "policy_exceeded"


class HubError(Exception):
    """Error that crosses the wire as {"error": {"code", "message"}}."""

    def __init__(self, code: ErrorCode, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status

    def to_dict(self) -> dict:
        return {"error": {"code": str(self.code), "message": self.message}}


def not_found(what: str) -> HubError:
    return HubError(ErrorCode.NOT_FOUND, f"{what} not found", 404)


HEARTBEAT_TTL_S = 30.0
"""Agent considered offline after this long without a heartbeat or open event stream."""

MAX_LONG_POLL_S = 60.0
"""Cap on any explicit wait, staying clear of Claude Code's ~2 min auto-background."""

MAX_ARTIFACT_BYTES = 32 * 1024 * 1024

DEFAULT_TASK_TIMEOUT_S = 3600.0

DEFAULT_MAX_ROOM_MESSAGES = 200
"""Backstop applied to any room created without a message cap.

The point is not the number — it is that no room is ever unbounded. Two agents set to
reply on every message will otherwise talk to each other until the hardware gives out.
"""

DEFAULT_ROOM_IDLE_TIMEOUT_S = 24 * 3600.0
"""Backstop applied to any room created without an idle timeout."""


@dataclass(slots=True)
class ToolCall:
    """A model's request to run a worker-side tool.

    Lives here rather than in `agent.backends` so `tools` and `agent` can both use it
    without importing each other.
    """

    id: str
    name: str
    arguments: dict


@dataclass(slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class RoomPolicy:
    """Hub-enforced guards. Every autonomous room is bounded by construction."""

    turn_policy: TurnPolicy = TurnPolicy.FREE
    max_messages: int | None = None
    max_total_tokens: int | None = None
    idle_timeout_s: float | None = None
    stop_phrase: str | None = None

    def to_dict(self) -> dict:
        return {
            "turn_policy": str(self.turn_policy),
            "max_messages": self.max_messages,
            "max_total_tokens": self.max_total_tokens,
            "idle_timeout_s": self.idle_timeout_s,
            "stop_phrase": self.stop_phrase,
        }

    @classmethod
    def from_dict(cls, raw: dict | None) -> RoomPolicy:
        raw = raw or {}
        return cls(
            turn_policy=TurnPolicy(raw.get("turn_policy") or TurnPolicy.FREE),
            max_messages=raw.get("max_messages"),
            max_total_tokens=raw.get("max_total_tokens"),
            idle_timeout_s=raw.get("idle_timeout_s"),
            stop_phrase=raw.get("stop_phrase"),
        )

    def bounded(
        self,
        max_messages: int = DEFAULT_MAX_ROOM_MESSAGES,
        idle_timeout_s: float = DEFAULT_ROOM_IDLE_TIMEOUT_S,
    ) -> RoomPolicy:
        """Return this policy with backstops filled in where the caller left gaps.

        Every room goes through here, so an unbounded conversation cannot be created
        by omission — only a caller who names a larger explicit limit gets one.
        """
        return RoomPolicy(
            turn_policy=self.turn_policy,
            max_messages=self.max_messages if self.max_messages is not None else max_messages,
            max_total_tokens=self.max_total_tokens,
            idle_timeout_s=(
                self.idle_timeout_s if self.idle_timeout_s is not None else idle_timeout_s
            ),
            stop_phrase=self.stop_phrase,
        )


@dataclass(slots=True)
class Agent:
    name: str
    kind: AgentKind
    node: str | None = None
    backend: str | None = None
    tags: list[str] = field(default_factory=list)
    last_seen: float = 0.0
    created_at: float = 0.0

    def status(self, ref: float | None = None) -> AgentStatus:
        ref = ref if ref is not None else now()
        if ref - self.last_seen > HEARTBEAT_TTL_S:
            return AgentStatus.OFFLINE
        return AgentStatus.ONLINE

    def matches(self, selector: str) -> bool:
        """`name` match, or `tag:value` / bare tag selector."""
        if selector == self.name:
            return True
        return selector in self.tags

    def to_dict(self, ref: float | None = None) -> dict:
        return {
            "name": self.name,
            "kind": str(self.kind),
            "node": self.node,
            "backend": self.backend,
            "tags": list(self.tags),
            "status": str(self.status(ref)),
            "last_seen": self.last_seen,
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class Room:
    id: str
    topic: str
    created_by: str
    open: bool = False
    policy: RoomPolicy = field(default_factory=RoomPolicy)
    archived: bool = False
    archived_reason: str | None = None
    dm_key: str | None = None
    floor_holder: str | None = None
    created_at: float = 0.0
    members: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic": self.topic,
            "created_by": self.created_by,
            "open": self.open,
            "is_dm": self.dm_key is not None,
            "floor_holder": self.floor_holder,
            "policy": self.policy.to_dict(),
            "archived": self.archived,
            "archived_reason": self.archived_reason,
            "members": list(self.members),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class Message:
    id: str
    room_id: str
    seq: int
    sender: str
    body: str
    data: dict | None = None
    reply_to: str | None = None
    mentions: list[str] = field(default_factory=list)
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "room_id": self.room_id,
            "seq": self.seq,
            "sender": self.sender,
            "body": self.body,
            "data": self.data,
            "reply_to": self.reply_to,
            "mentions": list(self.mentions),
            "created_at": self.created_at,
        }


@dataclass(slots=True)
class Task:
    id: str
    title: str
    spec: str
    created_by: str
    state: TaskState = TaskState.QUEUED
    assignee: str | None = None
    selector: str | None = None
    priority: int = 0
    timeout_s: float = DEFAULT_TASK_TIMEOUT_S
    dedupe_key: str | None = None
    room_id: str | None = None
    progress_pct: float | None = None
    progress_msg: str | None = None
    result: dict | None = None
    error: str | None = None
    attempts: int = 0
    created_at: float = 0.0
    claimed_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "spec": self.spec,
            "created_by": self.created_by,
            "state": str(self.state),
            "assignee": self.assignee,
            "selector": self.selector,
            "priority": self.priority,
            "timeout_s": self.timeout_s,
            "room_id": self.room_id,
            "progress": {"pct": self.progress_pct, "message": self.progress_msg},
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "finished_at": self.finished_at,
        }


@dataclass(slots=True)
class TaskEvent:
    id: str
    task_id: str
    kind: str
    payload: dict
    created_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "task_id": self.task_id,
            "kind": self.kind,
            "payload": self.payload,
            "created_at": self.created_at,
        }
