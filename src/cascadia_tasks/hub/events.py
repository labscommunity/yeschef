"""In-process fan-out of hub events to connected agents.

Each connected agent holds one queue per open stream. The store is synchronous, so it
publishes through a thread-safe hand-off onto the event loop.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections import defaultdict
from collections.abc import AsyncIterator

from ..models import EventKind, now

MAX_QUEUE = 256
MAX_PENDING = 1024
"""Cap on events buffered before the bus is bound to a loop (CLI and embedded use)."""


class EventBus:
    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue]] = defaultdict(set)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._pending: list[tuple[str, dict]] = []

    def bind(self, loop: asyncio.AbstractEventLoop) -> None:
        """Attach to the serving loop; flush anything published before startup."""
        self._loop = loop
        pending, self._pending = self._pending, []
        for target, event in pending:
            self._deliver(target, event)

    def publish(self, target: str, kind: EventKind | str, payload: dict) -> None:
        """Publish to one agent. Safe from any thread."""
        event = {"kind": str(kind), "at": now(), **payload}
        loop = self._loop
        if loop is None:
            # Nobody is listening yet (CLI, embedding, tests). Keep a bounded tail so a
            # long-lived unbound Store cannot leak every event it ever published.
            self._pending.append((target, event))
            del self._pending[:-MAX_PENDING]
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            self._deliver(target, event)
        else:
            loop.call_soon_threadsafe(self._deliver, target, event)

    def publish_many(self, targets: list[str], kind: EventKind | str, payload: dict) -> None:
        for target in targets:
            self.publish(target, kind, payload)

    def _deliver(self, target: str, event: dict) -> None:
        for queue in list(self._subs.get(target, ())):
            if queue.full():
                # Slow consumer: drop the oldest so a stalled stream cannot wedge the hub.
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(event)

    def subscriber_count(self, agent: str) -> int:
        return len(self._subs.get(agent, ()))

    @contextlib.asynccontextmanager
    async def subscribe(self, agent: str) -> AsyncIterator[asyncio.Queue]:
        queue: asyncio.Queue = asyncio.Queue(maxsize=MAX_QUEUE)
        self._subs[agent].add(queue)
        try:
            yield queue
        finally:
            self._subs[agent].discard(queue)
            if not self._subs[agent]:
                self._subs.pop(agent, None)
