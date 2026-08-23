"""Task lifecycle: dispatch, atomic claim, progress, completion, reclamation."""

from __future__ import annotations

import pytest

from yeschef.hub import Store
from yeschef.models import (
    HEARTBEAT_TTL_S,
    AgentKind,
    HubError,
    TaskState,
)


@pytest.fixture
def fleet(store: Store) -> Store:
    store.register_agent("alpha", tags=["tier:fast"])
    store.register_agent("beta", tags=["tier:fast"])
    store.register_agent("gamma", tags=["tier:reasoning"])
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    return store


def test_submit_returns_queued_task_immediately(fleet: Store) -> None:
    task = fleet.submit_task("summarize", "summarize the log", "claude:main", assignee="alpha")
    assert task.state is TaskState.QUEUED
    assert task.assignee == "alpha"
    assert task.id.startswith("task_")


def test_submit_without_target_routes_to_first_idle(store) -> None:
    """Omitting assignee AND selector means 'whoever is idle' — the wildcard selector
    routes to the first online worker instead of bouncing the submit."""
    store.register_agent("anyone", kind="worker", node="n", backend="b", tags=[])
    task = store.submit_task(title="t", spec="do it", created_by="c")
    assert task.selector == "*"
    assert store.next_task_for("anyone").id == task.id


def test_dedupe_key_returns_the_same_task(fleet: Store) -> None:
    first = fleet.submit_task("x", "spec", "claude:main", assignee="alpha", dedupe_key="k1")
    second = fleet.submit_task("x", "spec", "claude:main", assignee="alpha", dedupe_key="k1")
    assert first.id == second.id


def test_claim_is_atomic_across_agents(fleet: Store) -> None:
    """Selector tasks are announced to every match; exactly one claim can win."""
    task = fleet.submit_task("race", "spec", "claude:main", selector="tier:fast")
    claimed = fleet.claim_task(task.id, "alpha")
    assert claimed.state is TaskState.CLAIMED
    assert claimed.assignee == "alpha"

    with pytest.raises(HubError) as exc:
        fleet.claim_task(task.id, "beta")
    assert exc.value.code == "conflict"
    assert exc.value.http_status == 409


def test_next_task_matches_by_selector_and_name(fleet: Store) -> None:
    fleet.submit_task("fast one", "spec", "claude:main", selector="tier:fast")
    assert fleet.next_task_for("gamma") is None
    assert fleet.next_task_for("alpha") is not None

    fleet.submit_task("for gamma", "spec", "claude:main", assignee="gamma")
    picked = fleet.next_task_for("gamma")
    assert picked is not None and picked.title == "for gamma"


def test_priority_orders_the_queue(fleet: Store) -> None:
    fleet.submit_task("low", "spec", "claude:main", selector="tier:fast", priority=0)
    fleet.submit_task("high", "spec", "claude:main", selector="tier:fast", priority=10)
    picked = fleet.next_task_for("alpha")
    assert picked is not None and picked.title == "high"


def test_progress_moves_claimed_to_working(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")
    updated = fleet.update_progress(task.id, "alpha", pct=42.0, message="halfway")
    assert updated.state is TaskState.WORKING
    assert updated.progress_pct == 42.0
    assert updated.progress_msg == "halfway"


def test_only_the_holder_can_update(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")
    with pytest.raises(HubError) as exc:
        fleet.update_progress(task.id, "beta", pct=10.0)
    assert exc.value.code == "forbidden"


def test_complete_records_result_and_is_terminal(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")
    done = fleet.complete_task(task.id, "alpha", {"text": "answer"})
    assert done.state is TaskState.COMPLETED
    assert done.result == {"text": "answer"}
    assert done.progress_pct == 100.0

    with pytest.raises(HubError) as exc:
        fleet.update_progress(task.id, "alpha", pct=50.0)
    assert exc.value.code == "conflict"


def test_cancel_terminates_a_running_task(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")
    cancelled = fleet.cancel_task(task.id, "claude:main", reason="changed my mind")
    assert cancelled.state is TaskState.CANCELLED
    # provenance lives in result, not error — a deliberate cancel is not a failure
    assert cancelled.error is None
    assert cancelled.result["cancelled_by"] == "claude:main"
    assert cancelled.result["cancel_reason"] == "changed my mind"


def test_events_form_an_audit_trail(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")
    fleet.update_progress(task.id, "alpha", message="working")
    fleet.complete_task(task.id, "alpha", {"text": "done"})
    kinds = [event.kind for event in fleet.task_events(task.id)]
    assert kinds == ["submitted", "claimed", "progress", "completed"]


def test_input_required_round_trip(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")

    parked = fleet.request_input(task.id, "alpha", "which environment, staging or prod?")
    assert parked.state is TaskState.INPUT_REQUIRED
    assert parked.room_id is not None

    room_messages = fleet.fetch_messages(parked.room_id)
    assert room_messages[0].sender == "alpha"
    assert "staging" in room_messages[0].body

    resumed = fleet.provide_input(task.id, "claude:main", "staging")
    assert resumed.state is TaskState.WORKING
    assert fleet.fetch_messages(parked.room_id)[-1].body == "staging"


def test_provide_input_rejected_when_not_parked(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")
    with pytest.raises(HubError) as exc:
        fleet.provide_input(task.id, "claude:main", "unsolicited")
    assert exc.value.code == "conflict"


def test_task_room_is_created_on_demand(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    assert task.room_id is None
    room = fleet.ensure_task_room(task.id)
    assert fleet.require_task(task.id).room_id == room.id
    assert fleet.ensure_task_room(task.id).id == room.id


def test_sweep_requeues_a_task_whose_agent_vanished(fleet: Store, monkeypatch) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    fleet.claim_task(task.id, "alpha")

    stale = fleet.get_agent("alpha").last_seen - (HEARTBEAT_TTL_S + 60)
    with fleet._lock:
        fleet._db.execute("UPDATE agents SET last_seen = ? WHERE name = ?", (stale, "alpha"))
        fleet._db.commit()

    stats = fleet.sweep()
    assert stats["reclaimed"] == 1
    requeued = fleet.require_task(task.id)
    assert requeued.state is TaskState.QUEUED
    assert requeued.assignee is None


def test_sweep_fails_a_task_that_keeps_losing_its_agent(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha")
    for _ in range(2):
        fleet.claim_task(task.id, "alpha")
        with fleet._lock:
            fleet._db.execute("UPDATE agents SET last_seen = 0 WHERE name = ?", ("alpha",))
            fleet._db.commit()
        fleet.sweep()
    assert fleet.require_task(task.id).state is TaskState.FAILED
    assert fleet.require_task(task.id).error == "agent_lost"


def test_sweep_times_out_an_overrunning_task(fleet: Store) -> None:
    task = fleet.submit_task("t", "spec", "claude:main", assignee="alpha", timeout_s=0.0)
    fleet.claim_task(task.id, "alpha")
    stats = fleet.sweep()
    assert stats["timed_out"] == 1
    assert fleet.require_task(task.id).error == "timeout"


def test_agents_can_delegate_tasks_to_each_other(fleet: Store) -> None:
    """Agent-to-agent delegation: a worker submits work for another worker."""
    task = fleet.submit_task("subtask", "spec", created_by="alpha", assignee="gamma")
    assert task.created_by == "alpha"
    claimed = fleet.claim_task(task.id, "gamma")
    assert claimed.assignee == "gamma"
