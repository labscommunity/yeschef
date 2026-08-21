"""Concurrency regressions, driven with real threads.

Each of these reproduced a data-losing race before the guards went in. They hammer the
store directly because that is where the interleaving happens.
"""

from __future__ import annotations

import threading

import pytest

from cascadia_tasks.hub import Store
from cascadia_tasks.hub.events import EventBus
from cascadia_tasks.models import AgentKind, HubError, TaskState

ROUNDS = 40


def fresh_store() -> Store:
    store = Store(":memory:", EventBus())
    store.register_agent("alpha")
    store.register_agent("beta")
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    return store


def run_together(*fns) -> list[BaseException | None]:
    """Start every callable at once and collect whatever each raised."""
    barrier = threading.Barrier(len(fns))
    errors: list[BaseException | None] = [None] * len(fns)

    def wrap(index: int, fn):
        def inner() -> None:
            barrier.wait()
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - recorded and asserted on
                errors[index] = exc

        return inner

    threads = [threading.Thread(target=wrap(i, fn)) for i, fn in enumerate(fns)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    return errors


def test_a_timeout_sweep_cannot_erase_a_delivered_result() -> None:
    """The sweep and a finishing agent race; the result must survive either ordering."""
    for _ in range(ROUNDS):
        store = fresh_store()
        try:
            task = store.submit_task("t", "spec", "claude:main", assignee="alpha", timeout_s=0.0)
            store.claim_task(task.id, "alpha")

            run_together(
                lambda: store.sweep(),
                lambda: store.complete_task(task.id, "alpha", {"text": "real work"}),
            )

            final = store.require_task(task.id)
            assert final.state.terminal
            if final.state is TaskState.COMPLETED:
                assert final.result == {"text": "real work"}
            else:
                # The timeout won the race, which is allowed — but it must not have
                # left a half-written task claiming success with no result.
                assert final.error == "timeout"
                assert final.result is None
        finally:
            store.close()


def test_progress_cannot_resurrect_a_finished_task() -> None:
    for _ in range(ROUNDS):
        store = fresh_store()
        try:
            task = store.submit_task("t", "spec", "claude:main", assignee="alpha")
            store.claim_task(task.id, "alpha")

            def progress() -> None:
                try:
                    store.update_progress(task.id, "alpha", pct=50.0, message="working")
                except HubError:
                    pass  # losing to the completion is the correct outcome

            run_together(progress, lambda: store.complete_task(task.id, "alpha", {"text": "x"}))

            final = store.require_task(task.id)
            assert final.state.terminal, f"task resurrected to {final.state}"
            assert final.result == {"text": "x"}
            assert final.finished_at is not None
        finally:
            store.close()


def test_a_lapsed_heartbeat_cannot_requeue_finished_work() -> None:
    """An agent whose heartbeat lapsed but whose result lands must keep the result."""
    for _ in range(ROUNDS):
        store = fresh_store()
        try:
            task = store.submit_task("t", "spec", "claude:main", assignee="alpha")
            store.claim_task(task.id, "alpha")
            with store._lock:
                store._db.execute("UPDATE agents SET last_seen = 0 WHERE name = ?", ("alpha",))
                store._db.commit()

            run_together(
                lambda: store.sweep(),
                lambda: store.complete_task(task.id, "alpha", {"text": "delivered"}),
            )

            final = store.require_task(task.id)
            if final.state is TaskState.COMPLETED:
                assert final.result == {"text": "delivered"}
            else:
                # Requeued instead: acceptable, but then it must be genuinely re-runnable
                # rather than a completed task masquerading as queued.
                assert final.state is TaskState.QUEUED
                assert final.assignee is None
                assert final.result is None
        finally:
            store.close()


def test_two_agents_cannot_both_claim_one_task() -> None:
    for _ in range(ROUNDS):
        store = fresh_store()
        try:
            task = store.submit_task("t", "spec", "claude:main", selector="pool")
            outcomes: list[str] = []
            lock = threading.Lock()

            def claim(name: str):
                def inner() -> None:
                    try:
                        store.claim_task(task.id, name)
                        with lock:
                            outcomes.append(name)
                    except HubError:
                        pass

                return inner

            run_together(claim("alpha"), claim("beta"))
            assert len(outcomes) == 1, f"both agents claimed: {outcomes}"
            assert store.require_task(task.id).attempts == 1
        finally:
            store.close()


def test_concurrent_posts_cannot_reuse_a_sequence_number() -> None:
    store = fresh_store()
    try:
        room = store.create_room("busy", "claude:main", ["alpha", "beta"])
        posters = [
            (lambda i=i: store.post_message(room.id, "alpha" if i % 2 else "beta", f"m{i}"))
            for i in range(16)
        ]
        run_together(*posters)

        messages = store.fetch_messages(room.id, limit=100)
        seqs = [m.seq for m in messages]
        assert len(seqs) == 16
        assert sorted(seqs) == list(range(1, 17)), seqs
    finally:
        store.close()


def test_only_one_room_is_created_for_a_task_under_concurrency() -> None:
    for _ in range(ROUNDS):
        store = fresh_store()
        try:
            task = store.submit_task("t", "spec", "claude:main", assignee="alpha")
            store.claim_task(task.id, "alpha")

            rooms: list[str] = []
            lock = threading.Lock()

            def ensure() -> None:
                room = store.ensure_task_room(task.id)
                with lock:
                    rooms.append(room.id)

            errors = run_together(ensure, ensure)
            assert not any(errors), errors
            assert len(set(rooms)) == 1
        finally:
            store.close()


@pytest.mark.parametrize("_", range(5))
def test_round_robin_never_lets_one_agent_take_two_turns(_) -> None:
    """Floor control must hold under concurrent posts from the same holder."""
    from cascadia_tasks.models import RoomPolicy, TurnPolicy

    store = fresh_store()
    try:
        room = store.create_room(
            "dialogue",
            "claude:main",
            ["alpha", "beta"],
            RoomPolicy(turn_policy=TurnPolicy.ROUND_ROBIN),
        )
        assert store._floor_holder(room.id) == "alpha"

        accepted: list[int] = []
        lock = threading.Lock()

        def post(body: str):
            def inner() -> None:
                try:
                    message = store.post_message(room.id, "alpha", body)
                    with lock:
                        accepted.append(message.seq)
                except HubError:
                    pass

            return inner

        run_together(post("first"), post("second"))
        assert len(accepted) == 1, "the floor holder took two turns in one round"
        assert store._floor_holder(room.id) == "beta"
    finally:
        store.close()
