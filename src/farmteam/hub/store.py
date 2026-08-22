"""SQLite-backed hub state.

Single-writer, WAL mode, guarded by one re-entrant lock. Every method is synchronous and
short; async handlers call straight in. All timestamps are assigned here.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import secrets
import sqlite3
import threading
from pathlib import Path

from ..models import (
    DEFAULT_TASK_TIMEOUT_S,
    HEARTBEAT_TTL_S,
    INPUT_REQUIRED_TTL_S,
    MAX_ARTIFACT_BYTES,
    Agent,
    AgentKind,
    ErrorCode,
    EventKind,
    HubError,
    Message,
    Room,
    RoomPolicy,
    Task,
    TaskEvent,
    TaskState,
    TurnPolicy,
    new_id,
    not_found,
    now,
)
from .events import EventBus

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
MAX_TASK_ATTEMPTS = 2
FLOOR_NUDGE_S = 20.0
"""Re-send a floor grant when its online holder has said nothing for this long."""


def estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _dm_key(a: str, b: str) -> str:
    return "\x1f".join(sorted((a, b)))


class Store:
    def __init__(self, path: str | Path, bus: EventBus | None = None) -> None:
        self.path = str(path)
        self.bus = bus or EventBus()
        self._lock = threading.RLock()
        self._artifact_blobs: dict[str, bytes] = {}  # in-memory stores only (tests)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(self.path, check_same_thread=False)
        self._db.row_factory = sqlite3.Row
        self._db.executescript(SCHEMA_PATH.read_text())
        # Idempotent migrations for columns added after a database was created.
        import contextlib as _ctx
        import sqlite3 as _sqlite3

        with _ctx.suppress(_sqlite3.OperationalError):
            self._db.execute("ALTER TABLE tasks ADD COLUMN project TEXT")
            self._db.commit()
        with _ctx.suppress(_sqlite3.OperationalError):
            self._db.execute("ALTER TABLE artifacts ADD COLUMN fetched_at REAL")
            self._db.commit()
        with _ctx.suppress(_sqlite3.OperationalError):
            self._db.execute("ALTER TABLE tasks ADD COLUMN output_mode TEXT")
            self._db.commit()
        self._db.commit()

    def close(self) -> None:
        with self._lock:
            self._db.close()

    # ---------------------------------------------------------------- agents

    def register_agent(
        self,
        name: str,
        kind: AgentKind = AgentKind.WORKER,
        node: str | None = None,
        backend: str | None = None,
        tags: list[str] | None = None,
        presented_token: str | None = None,
        privileged: bool = False,
    ) -> tuple[Agent, str]:
        """Register, or re-register with proof of ownership.

        Claiming a name that already has a token requires either that token or a
        privileged caller — otherwise anyone reaching the hub could take over an
        agent's identity and lock the real one out.
        """
        if not name or "\x1f" in name:
            raise HubError(ErrorCode.INVALID, "invalid agent name")
        token = secrets.token_urlsafe(24)
        ts = now()
        with self._lock:
            existing = self._db.execute(
                "SELECT created_at, token_hash FROM agents WHERE name = ?", (name,)
            ).fetchone()
            if existing and existing["token_hash"] and not privileged:
                matches = presented_token is not None and secrets.compare_digest(
                    existing["token_hash"], _hash_token(presented_token)
                )
                if not matches:
                    raise HubError(
                        ErrorCode.FORBIDDEN,
                        f"agent '{name}' is already registered; present its token to re-register",
                        403,
                    )
            created_at = existing["created_at"] if existing else ts
            self._db.execute(
                """INSERT INTO agents (name, kind, node, backend, tags, token_hash, last_seen,
                                       created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(name) DO UPDATE SET
                       kind = excluded.kind, node = excluded.node, backend = excluded.backend,
                       tags = excluded.tags, token_hash = excluded.token_hash,
                       last_seen = excluded.last_seen""",
                (
                    name,
                    str(kind),
                    node,
                    backend,
                    json.dumps(tags or []),
                    _hash_token(token),
                    ts,
                    created_at,
                ),
            )
            self._db.commit()
        agent = self.get_agent(name)
        assert agent is not None
        return agent, token

    def ensure_identity(self, name: str, kind: AgentKind = AgentKind.CLAUDE) -> Agent:
        """Lazily create an addressable identity (used by Claude Code sessions)."""
        with self._lock:
            row = self._db.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
            ts = now()
            if row is None:
                self._db.execute(
                    """INSERT INTO agents (name, kind, tags, last_seen, created_at)
                       VALUES (?, ?, '[]', ?, ?)""",
                    (name, str(kind), ts, ts),
                )
            else:
                self._db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (ts, name))
            self._db.commit()
        agent = self.get_agent(name)
        assert agent is not None
        return agent

    def rename_identity(self, old: str, new: str, kind: AgentKind = AgentKind.CLAUDE) -> Agent:
        """Re-label a session identity, carrying room membership across."""
        if old == new:
            return self.ensure_identity(new, kind)
        self.ensure_identity(new, kind)
        with self._lock:
            self._db.execute(
                """UPDATE OR IGNORE room_members SET agent = ? WHERE agent = ?""", (new, old)
            )
            self._db.execute("DELETE FROM room_members WHERE agent = ?", (old,))
            self._db.execute("UPDATE rooms SET floor_holder = ? WHERE floor_holder = ?", (new, old))
            self._db.execute("UPDATE rooms SET created_by = ? WHERE created_by = ?", (new, old))
            self._db.execute("UPDATE messages SET sender = ? WHERE sender = ?", (new, old))
            self._db.execute("UPDATE tasks SET created_by = ? WHERE created_by = ?", (new, old))
            self._db.execute("UPDATE tasks SET assignee = ? WHERE assignee = ?", (new, old))
            self._db.execute("DELETE FROM agents WHERE name = ? AND kind = 'claude'", (old,))
            self._db.commit()
        agent = self.get_agent(new)
        assert agent is not None
        return agent

    def verify_token(self, name: str, token: str) -> bool:
        with self._lock:
            row = self._db.execute(
                "SELECT token_hash FROM agents WHERE name = ?", (name,)
            ).fetchone()
        if row is None or not row["token_hash"]:
            return False
        return secrets.compare_digest(row["token_hash"], _hash_token(token))

    def identify_token(self, token: str) -> str | None:
        """Resolve a bearer token to an agent name, for routes that only need 'someone valid'."""
        digest = _hash_token(token)
        with self._lock:
            row = self._db.execute(
                "SELECT name FROM agents WHERE token_hash = ?", (digest,)
            ).fetchone()
        return row["name"] if row else None

    def heartbeat(self, name: str) -> None:
        with self._lock:
            self._db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (now(), name))
            self._db.commit()

    def get_agent(self, name: str) -> Agent | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM agents WHERE name = ?", (name,)).fetchone()
        return self._row_to_agent(row) if row else None

    def require_agent(self, name: str) -> Agent:
        agent = self.get_agent(name)
        if agent is None:
            raise not_found(f"agent '{name}'")
        return agent

    def list_agents(self, kind: AgentKind | None = None) -> list[Agent]:
        sql = "SELECT * FROM agents"
        args: tuple = ()
        if kind is not None:
            sql += " WHERE kind = ?"
            args = (str(kind),)
        sql += " ORDER BY name"
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row_to_agent(r) for r in rows]

    def _row_to_agent(self, row: sqlite3.Row) -> Agent:
        return Agent(
            name=row["name"],
            kind=AgentKind(row["kind"]),
            node=row["node"],
            backend=row["backend"],
            tags=json.loads(row["tags"]),
            last_seen=row["last_seen"],
            created_at=row["created_at"],
        )

    # ----------------------------------------------------------------- rooms

    def create_room(
        self,
        topic: str,
        created_by: str,
        participants: list[str] | None = None,
        policy: RoomPolicy | None = None,
        open_room: bool = False,
        room_id: str | None = None,
        dm_key: str | None = None,
    ) -> Room:
        # Backstops are applied here so no code path can create an unbounded room.
        policy = (policy or RoomPolicy()).bounded()
        rid = room_id or new_id("room")
        ts = now()
        members = list(dict.fromkeys([created_by, *(participants or [])]))
        with self._lock:
            self._db.execute(
                """INSERT INTO rooms (id, topic, created_by, open, policy_json, dm_key,
                                      last_activity, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    rid,
                    topic,
                    created_by,
                    int(open_room),
                    json.dumps(policy.to_dict()),
                    dm_key,
                    ts,
                    ts,
                ),
            )
            for pos, member in enumerate(members):
                self._db.execute(
                    """INSERT OR IGNORE INTO room_members (room_id, agent, ring_pos, joined_at)
                       VALUES (?, ?, ?, ?)""",
                    (rid, member, pos, ts),
                )
            self._db.commit()
        room = self.get_room(rid)
        assert room is not None
        if policy.turn_policy is TurnPolicy.ROUND_ROBIN:
            self._grant_floor(room, self._first_worker(room))
            room = self.get_room(rid) or room
        invitees = [m for m in members if m != created_by]
        self.bus.publish_many(
            invitees, EventKind.ROOM_INVITE, {"room": room.to_dict(), "by": created_by}
        )
        return room

    def get_or_create_dm(self, a: str, b: str) -> Room:
        key = _dm_key(a, b)
        with self._lock:
            row = self._db.execute("SELECT id FROM rooms WHERE dm_key = ?", (key,)).fetchone()
        if row:
            room = self.get_room(row["id"])
            if room is not None and not room.archived:
                return room
            if room is not None:
                # The old thread hit a policy backstop. Release the key so the pair can
                # keep talking in a fresh room instead of being wedged forever.
                with self._lock:
                    self._db.execute("UPDATE rooms SET dm_key = NULL WHERE id = ?", (room.id,))
                    self._db.commit()
        return self.create_room(
            topic=f"{a} ↔ {b}", created_by=a, participants=[b], dm_key=key, room_id=new_id("dm")
        )

    def get_room(self, room_id: str) -> Room | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM rooms WHERE id = ?", (room_id,)).fetchone()
            if row is None:
                return None
            members = [
                r["agent"]
                for r in self._db.execute(
                    "SELECT agent FROM room_members WHERE room_id = ? ORDER BY ring_pos, joined_at",
                    (room_id,),
                ).fetchall()
            ]
        return Room(
            id=row["id"],
            topic=row["topic"],
            created_by=row["created_by"],
            open=bool(row["open"]),
            policy=RoomPolicy.from_dict(json.loads(row["policy_json"])),
            archived=bool(row["archived"]),
            archived_reason=row["archived_reason"],
            dm_key=row["dm_key"],
            floor_holder=row["floor_holder"],
            created_at=row["created_at"],
            members=members,
        )

    def require_room(self, room_id: str) -> Room:
        room = self.get_room(room_id)
        if room is None:
            raise not_found(f"room '{room_id}'")
        return room

    def list_rooms(self, agent: str | None = None, include_archived: bool = False) -> list[Room]:
        sql = "SELECT r.id FROM rooms r"
        args: list = []
        clauses = []
        if agent:
            sql += " JOIN room_members m ON m.room_id = r.id"
            clauses.append("m.agent = ?")
            args.append(agent)
        if not include_archived:
            clauses.append("r.archived = 0")
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY r.last_activity DESC"
        with self._lock:
            ids = [r["id"] for r in self._db.execute(sql, args).fetchall()]
        return [room for room in (self.get_room(i) for i in ids) if room is not None]

    def join_room(self, room_id: str, agent: str, privileged: bool = False) -> Room:
        room = self.require_room(room_id)
        if room.archived:
            raise HubError(ErrorCode.CONFLICT, "room is archived", 409)
        if agent in room.members:
            return room
        if not room.open and not privileged:
            raise HubError(ErrorCode.FORBIDDEN, "room is invite-only", 403)
        with self._lock:
            pos = self._db.execute(
                "SELECT COALESCE(MAX(ring_pos), -1) + 1 AS p FROM room_members WHERE room_id = ?",
                (room_id,),
            ).fetchone()["p"]
            self._db.execute(
                "INSERT OR IGNORE INTO room_members (room_id, agent, ring_pos, joined_at) "
                "VALUES (?, ?, ?, ?)",
                (room_id, agent, pos, now()),
            )
            self._db.commit()
        room = self.require_room(room_id)
        self.bus.publish_many(
            [m for m in room.members if m != agent],
            EventKind.ROOM_INVITE,
            {"room": room.to_dict(), "joined": agent},
        )
        return room

    def leave_room(self, room_id: str, agent: str) -> None:
        room = self.require_room(room_id)
        with self._lock:
            self._db.execute(
                "DELETE FROM room_members WHERE room_id = ? AND agent = ?", (room_id, agent)
            )
            self._db.commit()
        if room.policy.turn_policy is TurnPolicy.ROUND_ROBIN:
            fresh = self.get_room(room_id)
            if fresh and self._floor_holder(room_id) == agent:
                self._grant_floor(fresh, self._first_worker(fresh))

    def archive_room(
        self, room_id: str, reason: str, by: str | None = None, privileged: bool = False
    ) -> Room:
        """`by` is checked for membership unless the caller is privileged."""
        if by is not None and not privileged:
            room = self.require_room(room_id)
            if by not in room.members:
                raise HubError(ErrorCode.FORBIDDEN, "not a member of this room", 403)
        with self._lock:
            self._db.execute(
                "UPDATE rooms SET archived = 1, archived_reason = ?, floor_holder = NULL "
                "WHERE id = ? AND archived = 0",
                (reason, room_id),
            )
            self._db.commit()
        room = self.require_room(room_id)
        self.bus.publish_many(
            room.members, EventKind.ROOM_ARCHIVED, {"room_id": room_id, "reason": reason}
        )
        return room

    # ------------------------------------------------------------ floor control

    def _floor_holder(self, room_id: str) -> str | None:
        with self._lock:
            row = self._db.execute(
                "SELECT floor_holder FROM rooms WHERE id = ?", (room_id,)
            ).fetchone()
        return row["floor_holder"] if row else None

    def _worker_ring(self, room: Room) -> list[str]:
        """Members that take scheduled turns. Claude identities interject, unscheduled."""
        ring = []
        for name in room.members:
            agent = self.get_agent(name)
            if agent is not None and agent.kind is AgentKind.WORKER:
                ring.append(name)
        return ring

    def _first_worker(self, room: Room) -> str | None:
        ring = self._worker_ring(room)
        return ring[0] if ring else None

    def _grant_floor(self, room: Room, holder: str | None) -> None:
        with self._lock:
            self._db.execute("UPDATE rooms SET floor_holder = ? WHERE id = ?", (holder, room.id))
            self._db.commit()
        if holder:
            self.bus.publish(
                holder, EventKind.FLOOR_GRANTED, {"room_id": room.id, "topic": room.topic}
            )

    def yield_floor(self, room_id: str, agent: str) -> Room:
        """Pass the turn without speaking.

        Without this a round-robin dialogue stalls for good whenever the agent holding
        the floor has nothing to say or its model call fails.
        """
        room = self.require_room(room_id)
        if room.policy.turn_policy is not TurnPolicy.ROUND_ROBIN or room.archived:
            return room
        if self._floor_holder(room_id) != agent:
            raise HubError(ErrorCode.CONFLICT, "you do not hold the floor", 409)
        self._advance_floor(room, agent)
        return self.require_room(room_id)

    def _advance_floor(self, room: Room, sender: str) -> None:
        """Worker turn passes to the next in ring; a Claude interjection re-anchors."""
        ring = self._worker_ring(room)
        if not ring:
            self._grant_floor(room, None)
            return
        if sender in ring:
            nxt = ring[(ring.index(sender) + 1) % len(ring)]
        else:
            nxt = ring[0]
        self._grant_floor(room, nxt)

    # -------------------------------------------------------------- messages

    def post_message(
        self,
        room_id: str,
        sender: str,
        body: str,
        data: dict | None = None,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
        client_msg_id: str | None = None,
        tokens: int | None = None,
    ) -> Message:
        room = self.require_room(room_id)
        if room.archived:
            raise HubError(ErrorCode.CONFLICT, f"room is archived ({room.archived_reason})", 409)

        if sender not in room.members:
            if room.open:
                room = self.join_room(room_id, sender)
            else:
                raise HubError(ErrorCode.FORBIDDEN, "not a member of this room", 403)

        agent = self.get_agent(sender)
        is_claude = agent is not None and agent.kind is AgentKind.CLAUDE
        round_robin = room.policy.turn_policy is TurnPolicy.ROUND_ROBIN

        mentions = mentions if mentions is not None else _parse_mentions(body, room.members)
        cost = tokens if tokens is not None else estimate_tokens(body)
        ts = now()
        mid = new_id("msg")

        # Floor check, idempotency, sequence assignment, insert, and floor hand-off all
        # happen under one lock. Split across transactions, two concurrent posts from the
        # floor holder both passed the turn check and it took two turns in one round.
        with self._lock:
            if round_robin and not is_claude:
                holder = self._floor_holder(room_id)
                if holder is None:
                    # An unseeded ring would otherwise mean no turn enforcement at all.
                    holder = self._first_worker(room)
                    if holder is not None:
                        self._grant_floor(room, holder)
                if holder is not None and holder != sender:
                    raise HubError(
                        ErrorCode.CONFLICT, f"not your turn (floor held by '{holder}')", 409
                    )

            if client_msg_id:
                dupe = self._db.execute(
                    "SELECT * FROM messages WHERE sender = ? AND client_msg_id = ?",
                    (sender, client_msg_id),
                ).fetchone()
                if dupe:
                    return self._row_to_message(dupe)

            seq = self._db.execute(
                "SELECT next_seq FROM rooms WHERE id = ?", (room_id,)
            ).fetchone()["next_seq"]
            self._db.execute(
                """INSERT INTO messages (id, room_id, seq, sender, body, data_json, reply_to,
                                         mentions_json, client_msg_id, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    mid,
                    room_id,
                    seq,
                    sender,
                    body,
                    json.dumps(data) if data is not None else None,
                    reply_to,
                    json.dumps(mentions),
                    client_msg_id,
                    ts,
                ),
            )
            # max_messages bounds AGENT chatter; the operator steering a room is the
            # safety valve and must not burn its budget by speaking.
            counted = 0 if sender.startswith(("claude:", "operator:")) else 1
            self._db.execute(
                """UPDATE rooms SET next_seq = next_seq + 1,
                                    message_count = message_count + ?,
                                    total_tokens = total_tokens + ?, last_activity = ?
                   WHERE id = ?""",
                (counted, cost, ts, room_id),
            )
            self._db.commit()
            row = self._db.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            message = self._row_to_message(row)
            if round_robin:
                self._advance_floor(room, sender)

        self.bus.publish_many(
            [m for m in room.members if m != sender],
            EventKind.MESSAGE,
            {"message": message.to_dict(), "room_topic": room.topic},
        )
        self._enforce_room_policy(room_id, body, data, is_claude)
        return message

    def _enforce_room_policy(
        self, room_id: str, body: str, data: dict | None = None, from_operator: bool = False
    ) -> None:
        """Bound every room by construction: stop phrase, message cap, token budget."""
        room = self.get_room(room_id)
        if room is None or room.archived:
            return
        policy = room.policy
        with self._lock:
            row = self._db.execute(
                "SELECT message_count, total_tokens FROM rooms WHERE id = ?", (room_id,)
            ).fetchone()
        # The stop phrase is how *agents* signal they have converged. An operator who
        # writes it — in the seed goal ("say X when you agree") or mid-conversation — is
        # setting the rule, not ending the room; archive_room is the deliberate exit.
        if policy.stop_phrase and policy.stop_phrase in body and not from_operator:
            # Floor: small models emit the terminator on turn ONE, archiving the room
            # before anyone else has spoken and locking the operator out. Honor the
            # phrase only once every worker participant has taken a turn.
            worker_members = [m for m in room.members if not m.startswith(("claude:", "operator:"))]
            with self._lock:
                spoken = {
                    r["sender"]
                    for r in self._db.execute(
                        "SELECT DISTINCT sender FROM messages WHERE room_id = ?",
                        (room_id,),
                    ).fetchall()
                }
            if all(m in spoken for m in worker_members):
                self.archive_room(room_id, "stop_phrase")
            return
        if policy.max_messages is not None and row["message_count"] >= policy.max_messages:
            self.archive_room(room_id, "max_messages")
        elif policy.max_total_tokens is not None and row["total_tokens"] >= policy.max_total_tokens:
            self.archive_room(room_id, "max_total_tokens")

    def fetch_messages(
        self, room_id: str, after_seq: int = 0, limit: int = 100, tail: bool = False
    ) -> list[Message]:
        """Messages in seq order.

        `tail=True` returns the most recent `limit` instead of the oldest — what an agent
        needs when building its context window, since a long room would otherwise leave it
        replying to the start of a conversation that has moved on.
        """
        with self._lock:
            if tail:
                rows = self._db.execute(
                    """SELECT * FROM messages WHERE room_id = ? AND seq > ?
                       ORDER BY seq DESC LIMIT ?""",
                    (room_id, after_seq, limit),
                ).fetchall()
                rows = list(reversed(rows))
            else:
                rows = self._db.execute(
                    """SELECT * FROM messages WHERE room_id = ? AND seq > ?
                       ORDER BY seq LIMIT ?""",
                    (room_id, after_seq, limit),
                ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def fetch_inbox(
        self, agent: str, after_cursor: int = 0, limit: int = 100, room_id: str | None = None
    ) -> tuple[list[dict], int]:
        """Cross-room cursor fetch for one identity. Cursor is the global message rowid."""
        sql = """SELECT m.*, m.rowid AS cursor FROM messages m
                 JOIN room_members rm ON rm.room_id = m.room_id AND rm.agent = ?
                 WHERE m.rowid > ? AND m.sender != ?"""
        args: list = [agent, after_cursor, agent]
        if room_id:
            sql += " AND m.room_id = ?"
            args.append(room_id)
        sql += " ORDER BY m.rowid LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
            top = self._db.execute("SELECT COALESCE(MAX(rowid), 0) AS c FROM messages").fetchone()
        out = []
        for row in rows:
            item = self._row_to_message(row).to_dict()
            item["cursor"] = row["cursor"]
            out.append(item)
        if out:
            cursor = out[-1]["cursor"]
        elif room_id is not None:
            # A filtered read saw nothing; jumping to the global max here would skip
            # unread messages sitting in the caller's other rooms.
            cursor = after_cursor
        else:
            cursor = max(after_cursor, top["c"])
        return out, cursor

    def _row_to_message(self, row: sqlite3.Row) -> Message:
        return Message(
            id=row["id"],
            room_id=row["room_id"],
            seq=row["seq"],
            sender=row["sender"],
            body=row["body"],
            data=json.loads(row["data_json"]) if row["data_json"] else None,
            reply_to=row["reply_to"],
            mentions=json.loads(row["mentions_json"]),
            created_at=row["created_at"],
        )

    # ----------------------------------------------------------------- tasks

    def submit_task(
        self,
        title: str,
        spec: str,
        created_by: str,
        assignee: str | None = None,
        selector: str | None = None,
        priority: int = 0,
        timeout_s: float = DEFAULT_TASK_TIMEOUT_S,
        dedupe_key: str | None = None,
        project: str | None = None,
        output_mode: str | None = None,
    ) -> Task:
        if not assignee and not selector:
            raise HubError(ErrorCode.INVALID, "assignee or selector required")
        if dedupe_key:
            with self._lock:
                row = self._db.execute(
                    "SELECT id FROM tasks WHERE dedupe_key = ?", (dedupe_key,)
                ).fetchone()
            if row:
                existing = self.get_task(row["id"])
                if existing is not None and existing.state in (
                    TaskState.FAILED,
                    TaskState.CANCELLED,
                ):
                    # A dead task must not swallow a fresh attempt: release the key
                    # so the resubmit (possibly with a new spec/assignee) proceeds.
                    with self._lock:
                        self._db.execute(
                            "UPDATE tasks SET dedupe_key = NULL WHERE id = ?",
                            (existing.id,),
                        )
                        self._db.commit()
                elif existing is not None:
                    return existing
        tid = new_id("task")
        ts = now()
        with self._lock:
            self._db.execute(
                """INSERT INTO tasks (id, title, spec, created_by, state, assignee, selector,
                                      priority, timeout_s, dedupe_key, project,
                                      output_mode, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tid,
                    title,
                    spec,
                    created_by,
                    str(TaskState.QUEUED),
                    assignee,
                    selector,
                    priority,
                    timeout_s,
                    dedupe_key,
                    project,
                    output_mode,
                    ts,
                ),
            )
            self._db.commit()
        task = self.get_task(tid)
        assert task is not None
        self._log_task(tid, "submitted", {"by": created_by, "target": assignee or selector})
        self._announce_task(task)
        return task

    def _candidates(self, task: Task) -> list[str]:
        if task.assignee:
            return [task.assignee]
        if not task.selector:
            return []
        return [a.name for a in self.list_agents(kind=AgentKind.WORKER) if a.matches(task.selector)]

    def _announce_task(self, task: Task) -> None:
        self.bus.publish_many(
            self._candidates(task), EventKind.TASK_ASSIGNED, {"task": task.to_dict()}
        )

    def claim_task(self, task_id: str, agent: str) -> Task:
        """Atomic: first caller wins, everyone else gets 409."""
        ts = now()
        with self._lock:
            cur = self._db.execute(
                """UPDATE tasks SET state = ?, assignee = ?, claimed_at = ?, attempts = attempts + 1
                   WHERE id = ? AND state = ?""",
                (str(TaskState.CLAIMED), agent, ts, task_id, str(TaskState.QUEUED)),
            )
            self._db.commit()
            claimed = cur.rowcount == 1
        task = self.get_task(task_id)
        if task is None:
            raise not_found(f"task '{task_id}'")
        if not claimed:
            raise HubError(
                ErrorCode.CONFLICT, f"task already {task.state} (held by {task.assignee})", 409
            )
        self._log_task(task_id, "claimed", {"by": agent})
        self._notify_task_watchers(task)
        return task

    def next_task_for(self, agent_name: str) -> Task | None:
        """Highest-priority queued task this agent could claim."""
        agent = self.get_agent(agent_name)
        if agent is None:
            return None
        with self._lock:
            rows = self._db.execute(
                """SELECT * FROM tasks WHERE state = ? ORDER BY priority DESC, created_at""",
                (str(TaskState.QUEUED),),
            ).fetchall()
        for row in rows:
            task = self._row_to_task(row)
            if task.assignee == agent_name:
                return task
            if task.selector and agent.matches(task.selector):
                return task
        return None

    def update_progress(
        self, task_id: str, agent: str, pct: float | None = None, message: str | None = None
    ) -> Task:
        """Record progress and promote claimed → working, atomically.

        Checking the state and then writing in a second transaction let an in-flight
        progress call resurrect a task that had just completed, leaving it permanently
        non-terminal.
        """
        with self._lock:
            self._require_holder(task_id, agent)
            self._db.execute(
                """UPDATE tasks
                      SET state = CASE WHEN state = ? THEN ? ELSE state END,
                          progress_pct = COALESCE(?, progress_pct),
                          progress_msg = COALESCE(?, progress_msg)
                    WHERE id = ? AND state IN (?, ?, ?)""",
                (
                    str(TaskState.CLAIMED),
                    str(TaskState.WORKING),
                    pct,
                    message,
                    task_id,
                    str(TaskState.CLAIMED),
                    str(TaskState.WORKING),
                    str(TaskState.INPUT_REQUIRED),
                ),
            )
            self._db.commit()
        self._log_task(task_id, "progress", {"pct": pct, "message": message})
        fresh = self.get_task(task_id)
        assert fresh is not None
        self._notify_task_watchers(fresh)
        return fresh

    def complete_task(self, task_id: str, agent: str, result: dict | None) -> Task:
        self._require_holder(task_id, agent)
        return self._finish(task_id, TaskState.COMPLETED, result=result)

    def fail_task(self, task_id: str, agent: str, error: str, result: dict | None = None) -> Task:
        """`result` may carry partial output/usage — a failed run's tokens still cost
        electricity, and the accounting should not have holes."""
        self._require_holder(task_id, agent)
        return self._finish(task_id, TaskState.FAILED, result=result, error=error)

    def cancel_task(self, task_id: str, by: str, privileged: bool = False) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise not_found(f"task '{task_id}'")
        if not privileged and by not in {task.created_by, task.assignee}:
            raise HubError(
                ErrorCode.FORBIDDEN, "only the requester or the assignee can cancel a task", 403
            )
        if task.state.terminal:
            raise HubError(ErrorCode.CONFLICT, f"task already {task.state}", 409)
        holder = task.assignee
        finished = self._finish(task_id, TaskState.CANCELLED, error=f"cancelled by {by}")  # noqa: E501
        if holder:
            self.bus.publish(holder, EventKind.TASK_CANCELLED, {"task_id": task_id, "by": by})
        return finished

    def reassign_task(self, task_id: str, assignee: str, by: str) -> Task:
        """Requeue a live task onto a different worker, preserving id and history."""
        task = self.require_task(task_id)
        holder = task.assignee
        with self._lock:
            self._db.execute(
                """UPDATE tasks SET assignee = ?, state = ?, claimed_at = NULL,
                                    progress_pct = NULL, progress_msg = ?
                   WHERE id = ? AND state NOT IN (?, ?, ?)""",
                (
                    assignee,
                    str(TaskState.QUEUED),
                    f"reassigned from {holder} by {by}",
                    task_id,
                    str(TaskState.COMPLETED),
                    str(TaskState.FAILED),
                    str(TaskState.CANCELLED),
                ),
            )
            self._db.commit()
        if holder and holder != assignee:
            self.bus.publish(holder, EventKind.TASK_CANCELLED, {"task_id": task_id, "by": by})
        self._log_task(task_id, "reassigned", {"from": holder, "to": assignee, "by": by})
        fresh = self.require_task(task_id)
        self._announce_task(fresh)
        return fresh

    def request_input(self, task_id: str, agent: str, question: str) -> Task:
        """Agent needs a human/Claude answer: opens the task room and parks the task."""
        self._require_holder(task_id, agent)
        room = self.ensure_task_room(task_id)
        self._set_state(task_id, TaskState.INPUT_REQUIRED)
        with self._lock:
            # Summaries carry progress, so the question rides along — a wait_task
            # return alone tells the caller what the worker is asking.
            self._db.execute(
                "UPDATE tasks SET progress_msg = ? WHERE id = ?",
                (f"awaiting input: {question[:300]}", task_id),
            )
            self._db.commit()
        self.post_message(room.id, agent, question)
        self._log_task(task_id, "input_required", {"question": question})
        task = self.get_task(task_id)
        assert task is not None
        self._notify_task_watchers(task)
        return task

    def provide_input(self, task_id: str, by: str, message: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise not_found(f"task '{task_id}'")
        with self._lock:
            # The parked question is stale the moment an answer lands.
            self._db.execute(
                "UPDATE tasks SET progress_msg = ? WHERE id = ? AND state = ?",
                ("input received, resuming", task_id, str(TaskState.INPUT_REQUIRED)),
            )
            self._db.commit()
        if task.state is not TaskState.INPUT_REQUIRED:
            raise HubError(ErrorCode.CONFLICT, f"task is {task.state}, not input_required", 409)
        room = self.ensure_task_room(task_id)
        if by not in room.members:
            self.join_room(room.id, by)
        self.post_message(room.id, by, message)
        self._set_state(task_id, TaskState.WORKING)
        self._log_task(task_id, "input_provided", {"by": by})
        fresh = self.get_task(task_id)
        assert fresh is not None
        if fresh.assignee:
            self.bus.publish(
                fresh.assignee,
                EventKind.TASK_UPDATED,
                {"task": fresh.to_dict(), "input": message, "by": by},
            )
        return fresh

    def ensure_task_room(self, task_id: str) -> Room:
        """A task grows a room the moment anyone needs to discuss it."""
        task = self.get_task(task_id)
        if task is None:
            raise not_found(f"task '{task_id}'")
        if task.room_id:
            room = self.get_room(task.room_id)
            if room is not None:
                return room
        members = [m for m in (task.created_by, task.assignee) if m]
        room_id = f"room_{task.id}"
        with self._lock:
            # Re-check under the lock: request_input and task_room can arrive together.
            existing = self._db.execute("SELECT id FROM rooms WHERE id = ?", (room_id,)).fetchone()
        if existing:
            room = self.get_room(room_id)
            if room is not None:
                return room
        try:
            room = self.create_room(
                topic=f"task: {task.title}",
                created_by=task.created_by,
                participants=members[1:],
                room_id=room_id,
            )
        except sqlite3.IntegrityError:
            # Another caller created it between the check and the insert.
            room = self.get_room(room_id)
            if room is None:
                raise
            return room
        with self._lock:
            self._db.execute("UPDATE tasks SET room_id = ? WHERE id = ?", (room.id, task_id))
            self._db.commit()
        return room

    def get_task(self, task_id: str) -> Task | None:
        with self._lock:
            row = self._db.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        return self._row_to_task(row) if row else None

    def require_task(self, task_id: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            begins = self.history_begins_at()
            if begins:
                import datetime as _dt

                iso = _dt.datetime.fromtimestamp(begins, _dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
                raise HubError(
                    ErrorCode.NOT_FOUND,
                    f"task '{task_id}' not found — this hub's task history begins "
                    f"{iso}; older ids are gone. list_tasks shows what exists.",
                    404,
                )
        if task is None:
            raise not_found(f"task '{task_id}'")
        return task

    def list_tasks(
        self,
        state: TaskState | None = None,
        assignee: str | None = None,
        created_by: str | None = None,
        project: str | None = None,
        limit: int = 100,
    ) -> list[Task]:
        sql = "SELECT * FROM tasks"
        clauses, args = [], []
        if project:
            clauses.append("project = ?")
            args.append(project)
        if state is not None:
            clauses.append("state = ?")
            args.append(str(state))
        if assignee:
            clauses.append("assignee = ?")
            args.append(assignee)
        if created_by:
            clauses.append("created_by = ?")
            args.append(created_by)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [self._row_to_task(r) for r in rows]

    def task_events(self, task_id: str, limit: int = 50) -> list[TaskEvent]:
        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM task_events WHERE task_id = ? ORDER BY created_at DESC LIMIT ?",
                (task_id, limit),
            ).fetchall()
        events = [
            TaskEvent(
                id=r["id"],
                task_id=r["task_id"],
                kind=r["kind"],
                payload=json.loads(r["payload_json"]),
                created_at=r["created_at"],
            )
            for r in rows
        ]
        events.reverse()
        return events

    def _require_holder(self, task_id: str, agent: str) -> Task:
        task = self.get_task(task_id)
        if task is None:
            raise not_found(f"task '{task_id}'")
        if task.assignee != agent:
            raise HubError(ErrorCode.FORBIDDEN, f"task is held by '{task.assignee}'", 403)
        if task.state.terminal:
            raise HubError(ErrorCode.CONFLICT, f"task already {task.state}", 409)
        return task

    def _set_state(self, task_id: str, state: TaskState) -> None:
        with self._lock:
            self._db.execute("UPDATE tasks SET state = ? WHERE id = ?", (str(state), task_id))
            self._db.commit()

    def _finish(
        self,
        task_id: str,
        state: TaskState,
        result: dict | None = None,
        error: str | None = None,
    ) -> Task:
        """Move a task to a terminal state, once.

        The guard matters: the sweep and a finishing agent can race, and an unguarded
        write would let a timeout overwrite a result the agent had already delivered.
        First writer wins; the loser sees a conflict instead of silently erasing work.
        """
        with self._lock:
            cursor = self._db.execute(
                """UPDATE tasks SET state = ?, result_json = ?, error = ?, finished_at = ?,
                                    progress_pct = CASE WHEN ? = 'completed' THEN 100.0
                                                        ELSE progress_pct END,
                                    progress_msg = ?
                   WHERE id = ? AND state NOT IN (?, ?, ?)""",
                (
                    str(state),
                    json.dumps(result) if result is not None else None,
                    error,
                    now(),
                    str(state),
                    str(state),
                    task_id,
                    str(TaskState.COMPLETED),
                    str(TaskState.FAILED),
                    str(TaskState.CANCELLED),
                ),
            )
            self._db.commit()
            applied = cursor.rowcount == 1
        task = self.get_task(task_id)
        if task is None:
            raise not_found(f"task '{task_id}'")
        if not applied:
            raise HubError(ErrorCode.CONFLICT, f"task already {task.state}", 409)
        self._log_task(task_id, str(state), {"error": error} if error else {})
        self._notify_task_watchers(task)
        return task

    def _log_task(self, task_id: str, kind: str, payload: dict) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO task_events (id, task_id, kind, payload_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (new_id("ev"), task_id, kind, json.dumps(payload), now()),
            )
            self._db.commit()

    def _notify_task_watchers(self, task: Task) -> None:
        watchers = {task.created_by}
        if task.assignee:
            watchers.add(task.assignee)
        self.bus.publish_many(sorted(watchers), EventKind.TASK_UPDATED, {"task": task.to_dict()})

    def _row_to_task(self, row: sqlite3.Row) -> Task:
        return Task(
            id=row["id"],
            title=row["title"],
            spec=row["spec"],
            created_by=row["created_by"],
            state=TaskState(row["state"]),
            assignee=row["assignee"],
            selector=row["selector"],
            priority=row["priority"],
            timeout_s=row["timeout_s"],
            dedupe_key=row["dedupe_key"],
            project=row["project"] if "project" in row.keys() else None,
            output_mode=row["output_mode"] if "output_mode" in row.keys() else None,
            room_id=row["room_id"],
            progress_pct=row["progress_pct"],
            progress_msg=row["progress_msg"],
            result=json.loads(row["result_json"]) if row["result_json"] else None,
            error=row["error"],
            attempts=row["attempts"],
            created_at=row["created_at"],
            claimed_at=row["claimed_at"],
            finished_at=row["finished_at"],
        )

    # ----------------------------------------------------------------- stats

    def history_begins_at(self) -> float | None:
        with self._lock:
            row = self._db.execute("SELECT MIN(created_at) AS t FROM tasks").fetchone()
        return row["t"]

    def unretrieved_result_entries(self, limit: int = 3, project: str | None = None) -> list[dict]:
        """A few identifying rows for the uncollected counter, so a session can judge
        relevance (same project or not) without extra calls."""
        rows = self._unretrieved_rows(project)
        return [{"id": r.id, "title": r.title, "project": r.project} for r in rows[:limit]]

    def _unretrieved_rows(self, project: str | None = None):
        """Completed tasks with files none of which anyone ever fetched."""

        out = []
        for t in self.list_tasks(state=TaskState.COMPLETED, project=project, limit=500):
            files = (t.result or {}).get("files") or []
            ids = [f["artifact_id"] for f in files if "artifact_id" in f]
            if not ids:
                continue
            with self._lock:
                fetched = self._db.execute(
                    f"SELECT COUNT(*) AS n FROM artifacts WHERE id IN "
                    f"({','.join('?' * len(ids))}) AND fetched_at IS NOT NULL",
                    ids,
                ).fetchone()["n"]
            if fetched == 0:
                out.append(t)
        return out

    def unretrieved_results(self, project: str | None = None) -> int:
        return len(self._unretrieved_rows(project))

    def mark_collected(self, task) -> None:
        """Fetching a result counts as collecting it — the requester has seen the
        work; its files stay fetchable but stop reading as orphaned."""
        ids = [
            f["artifact_id"] for f in (task.result or {}).get("files") or [] if "artifact_id" in f
        ]
        if not ids:
            return
        with self._lock:
            self._db.execute(
                f"UPDATE artifacts SET fetched_at = COALESCE(fetched_at, ?) "
                f"WHERE id IN ({','.join('?' * len(ids))})",
                [now(), *ids],
            )
            self._db.commit()

    def dismiss_results(self, task_ids: list[str] | None = None, dismiss_all: bool = False) -> int:
        """Mark uncollected results' artifacts fetched without pulling them."""
        targets = (
            self._unretrieved_rows()
            if dismiss_all
            else [self.require_task(t) for t in task_ids or []]
        )
        n = 0
        for t in targets:
            ids = [
                f["artifact_id"] for f in (t.result or {}).get("files") or [] if "artifact_id" in f
            ]
            if not ids:
                continue
            with self._lock:
                self._db.execute(
                    f"UPDATE artifacts SET fetched_at = COALESCE(fetched_at, ?) "
                    f"WHERE id IN ({','.join('?' * len(ids))})",
                    [now(), *ids],
                )
                self._db.commit()
            n += 1
        return n

    def active_task_counts(self) -> dict[str, int]:
        """Live tasks per assignee, so the roster can show busy/idle directly."""
        with self._lock:
            rows = self._db.execute(
                """SELECT assignee, COUNT(*) AS n FROM tasks
                    WHERE assignee IS NOT NULL AND state IN (?, ?, ?)
                    GROUP BY assignee""",
                (
                    str(TaskState.CLAIMED),
                    str(TaskState.WORKING),
                    str(TaskState.INPUT_REQUIRED),
                ),
            ).fetchall()
        return {row["assignee"]: row["n"] for row in rows}

    def lifetime_stats(self) -> dict:
        """What the farm team has done for you, computed from the durable record.

        Derived on demand from tasks/rooms rather than kept as a counter, so it can
        never drift from the truth and needs no migration.
        """
        with self._lock:
            tasks_row = self._db.execute(
                """SELECT COUNT(*) AS done,
                          COALESCE(SUM(finished_at - claimed_at), 0) AS work_s
                     FROM tasks WHERE state = ? AND claimed_at IS NOT NULL""",
                (str(TaskState.COMPLETED),),
            ).fetchone()
            token_row = self._db.execute(
                "SELECT COALESCE(SUM(total_tokens), 0) AS tokens, "
                "COALESCE(SUM(message_count), 0) AS messages FROM rooms"
            ).fetchone()
            task_tokens = self._db.execute(
                """SELECT COALESCE(SUM(COALESCE(json_extract(result_json, '$.tokens'), 0)), 0)
                          AS tokens
                     FROM tasks WHERE state = ?""",
                (str(TaskState.COMPLETED),),
            ).fetchone()
        return {
            "tasks_completed": tasks_row["done"],
            "work_seconds": round(tasks_row["work_s"], 1),
            "local_tokens": token_row["tokens"] + task_tokens["tokens"],
            "messages": token_row["messages"],
        }

    @staticmethod
    def format_stats(stats: dict) -> str:
        """One compact human line, e.g. for tool-response footers."""
        hours = stats["work_seconds"] / 3600.0
        clock = f"{hours:.1f}h" if hours >= 1 else f"{stats['work_seconds'] / 60.0:.0f}m"
        return (
            f"farm team lifetime: {stats['tasks_completed']} tasks · "
            f"~{stats['local_tokens']:,} tokens kept local · {clock} of work on your hardware"
        )

    # -------------------------------------------------------------- artifacts

    def _artifact_dir(self) -> Path | None:
        if self.path == ":memory:":
            return None
        return Path(self.path).parent / "artifacts"

    def save_artifact(self, name: str, mime: str, content: bytes, created_by: str) -> dict:
        """Store a file a worker produced, so a requester on another machine can pull it."""
        if len(content) > MAX_ARTIFACT_BYTES:
            raise HubError(
                ErrorCode.INVALID,
                f"artifact exceeds {MAX_ARTIFACT_BYTES // (1024 * 1024)}MB cap",
            )
        artifact_id = new_id("art")
        digest = hashlib.sha256(content).hexdigest()
        directory = self._artifact_dir()
        if directory is None:
            self._artifact_blobs[artifact_id] = content
        else:
            directory.mkdir(parents=True, exist_ok=True)
            (directory / artifact_id).write_bytes(content)
        with self._lock:
            self._db.execute(
                "INSERT INTO artifacts (id, name, mime, bytes, sha256, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (artifact_id, name, mime, len(content), digest, created_by, now()),
            )
            self._db.commit()
        return {
            "id": artifact_id,
            "name": name,
            "mime": mime,
            "bytes": len(content),
            "sha256": digest,
        }

    def get_artifact(self, artifact_id: str) -> tuple[dict, bytes]:
        with self._lock:
            row = self._db.execute(
                "SELECT * FROM artifacts WHERE id = ?", (artifact_id,)
            ).fetchone()
            if row:
                self._db.execute(
                    "UPDATE artifacts SET fetched_at = COALESCE(fetched_at, ?) WHERE id = ?",
                    (now(), artifact_id),
                )
                self._db.commit()
        if row is None:
            raise not_found(f"artifact '{artifact_id}'")
        directory = self._artifact_dir()
        if directory is None:
            content = self._artifact_blobs.get(artifact_id)
        else:
            path = directory / artifact_id
            content = path.read_bytes() if path.exists() else None
        if content is None:
            raise not_found(f"artifact '{artifact_id}' content")
        meta = {
            "id": row["id"],
            "name": row["name"],
            "mime": row["mime"],
            "bytes": row["bytes"],
            "sha256": row["sha256"],
            "created_by": row["created_by"],
        }
        return meta, content

    # --------------------------------------------------------------- janitor

    def sweep(self) -> dict[str, int]:
        """Reclaim tasks from lost agents, time out overruns, archive idle rooms."""
        ref = now()
        stats = {"reclaimed": 0, "timed_out": 0, "rooms_archived": 0, "floors_recovered": 0}

        with self._lock:
            rows = self._db.execute(
                "SELECT * FROM tasks WHERE state IN (?, ?, ?, ?)",
                (
                    str(TaskState.QUEUED),
                    str(TaskState.CLAIMED),
                    str(TaskState.WORKING),
                    str(TaskState.INPUT_REQUIRED),
                ),
            ).fetchall()
        for row in rows:
            task = self._row_to_task(row)
            # A task nobody ever claimed still has a deadline — otherwise work dispatched
            # to an offline agent sits in `queued` forever and the requester is never told.
            started = task.claimed_at if task.claimed_at is not None else task.created_at
            if ref - started > task.timeout_s:
                with contextlib.suppress(HubError):
                    self._finish(task.id, TaskState.FAILED, error="timeout")
                    stats["timed_out"] += 1
                continue
            if task.state in (TaskState.QUEUED, TaskState.INPUT_REQUIRED):
                # Queued waits on an agent to appear; input_required waits on a human —
                # but not forever: a stranded question failed one task for 41 minutes
                # before anyone noticed. Fail with the question in the error so the
                # requester sees WHAT was asked, not just that time passed.
                if (
                    task.state is TaskState.INPUT_REQUIRED
                    and ref - (task.claimed_at or task.created_at) > INPUT_REQUIRED_TTL_S
                ):
                    question = (task.progress_msg or "").removeprefix("awaiting input: ")
                    with contextlib.suppress(HubError):
                        self._finish(
                            task.id,
                            TaskState.FAILED,
                            error=(
                                "no input arrived within "
                                f"{int(INPUT_REQUIRED_TTL_S / 60)}m; the worker was "
                                f"asking: {question[:300]}"
                            ),
                        )
                        stats["timed_out"] += 1
                    continue
                if task.state is TaskState.QUEUED:
                    self._announce_task(task)
                continue
            holder = self.get_agent(task.assignee) if task.assignee else None
            if holder is None or ref - holder.last_seen > HEARTBEAT_TTL_S:
                if task.attempts >= MAX_TASK_ATTEMPTS:
                    with contextlib.suppress(HubError):
                        self._finish(task.id, TaskState.FAILED, error="agent_lost")
                else:
                    with self._lock:
                        cursor = self._db.execute(
                            """UPDATE tasks SET state = ?, assignee = NULL, claimed_at = NULL
                               WHERE id = ? AND state IN (?, ?)""",
                            (
                                str(TaskState.QUEUED),
                                task.id,
                                str(TaskState.CLAIMED),
                                str(TaskState.WORKING),
                            ),
                        )
                        self._db.commit()
                        requeued_ok = cursor.rowcount == 1
                    if not requeued_ok:
                        continue  # the agent finished after all; leave its result alone
                    self._log_task(task.id, "requeued", {"reason": "agent_lost"})
                    requeued = self.get_task(task.id)
                    if requeued is not None:
                        self._announce_task(requeued)
                stats["reclaimed"] += 1

        with self._lock:
            room_rows = self._db.execute(
                "SELECT id, policy_json, last_activity, floor_holder FROM rooms WHERE archived = 0"
            ).fetchall()
        for row in room_rows:
            policy = RoomPolicy.from_dict(json.loads(row["policy_json"]))
            if (
                policy.idle_timeout_s is not None
                and ref - row["last_activity"] >= policy.idle_timeout_s
            ):
                self.archive_room(row["id"], "idle_timeout")
                stats["rooms_archived"] += 1
                continue
            if policy.turn_policy is TurnPolicy.ROUND_ROBIN:
                stats["floors_recovered"] += self._recover_floor(
                    row["id"], row["floor_holder"], ref, row["last_activity"]
                )
        return stats

    def _recover_floor(
        self, room_id: str, holder: str | None, ref: float, last_activity: float = 0.0
    ) -> int:
        """Hand the floor on — or nudge it — when a dialogue has gone quiet.

        Three stall shapes: no holder was ever seeded; the holder went offline; or the
        holder is online but its FLOOR_GRANTED event was lost (e.g. delivered before the
        seed message existed). The last one gets a re-grant, which the harness treats
        idempotently.
        """
        room = self.get_room(room_id)
        if room is None or room.archived:
            return 0
        if holder is None:
            candidate = self._first_worker(room)
            if candidate is None:
                return 0
            self._grant_floor(room, candidate)
            return 1
        agent = self.get_agent(holder)
        if agent is None or ref - agent.last_seen > HEARTBEAT_TTL_S:
            self._advance_floor(room, holder)
            return 1
        if last_activity and ref - last_activity > FLOOR_NUDGE_S:
            self._grant_floor(room, holder)  # online holder, silent room: nudge again
            return 1
        return 0


def _parse_mentions(body: str, members: list[str]) -> list[str]:
    return [m for m in members if f"@{m}" in body]
