"""The record/replay demo pipeline: capture a real room, re-stream it faithfully."""

from __future__ import annotations

import json

from rich.console import Console

from farmteam.hub import Store
from farmteam.hub.events import EventBus
from farmteam.models import AgentKind
from farmteam.replay import ReplayOptions, replay, save_transcript, transcript_payload


def build_room(store: Store) -> tuple[dict, list[dict], list[dict]]:
    store.register_agent("fastball", node="office-mac", backend="ollama/qwen3:8b")
    store.register_agent("closer", node="gpu-box", backend="vllm/qwen3-32b")
    store.ensure_identity("operator:me", AgentKind.CLAUDE)
    room = store.create_room("pick a cache", "operator:me", ["fastball", "closer"])
    store.post_message(room.id, "operator:me", "settle this: which cache?")
    store.post_message(room.id, "fastball", "write-through is simpler")
    store.post_message(room.id, "closer", "agreed, write-through")
    fresh = store.require_room(room.id)
    messages = [m.to_dict() for m in store.fetch_messages(room.id)]
    agents = [a.to_dict() for a in store.list_agents()]
    return fresh.to_dict(), messages, agents


def test_transcript_payload_captures_everything_replay_needs() -> None:
    store = Store(":memory:", EventBus())
    try:
        room, messages, agents = build_room(store)
        payload = transcript_payload(room, messages, agents)

        assert payload["version"] == 1
        assert payload["room"]["topic"] == "pick a cache"
        assert payload["participants"]["fastball"]["backend"] == "ollama/qwen3:8b"
        assert payload["participants"]["fastball"]["node"] == "office-mac"
        assert payload["participants"]["operator:me"]["kind"] == "claude"
        assert [m["sender"] for m in payload["messages"]] == [
            "operator:me",
            "fastball",
            "closer",
        ]
        # Timestamps ride along so replay can reproduce the real pacing.
        assert all("created_at" in m for m in payload["messages"])
    finally:
        store.close()


def test_save_and_replay_round_trip(tmp_path) -> None:
    store = Store(":memory:", EventBus())
    try:
        room, messages, agents = build_room(store)
        payload = transcript_payload(room, messages, agents)
    finally:
        store.close()

    target = save_transcript(tmp_path / "demo.json", payload)
    loaded = json.loads(target.read_text())

    console = Console(record=True, width=100, force_terminal=False)
    replay(loaded, console, ReplayOptions(no_delay=True))
    output = console.export_text()

    # The roster names the machines and models — the "this is real and local" proof.
    assert "ollama/qwen3:8b" in output
    assert "office-mac" in output
    assert "vllm/qwen3-32b" in output
    assert "gpu-box" in output
    # Every turn is attributed, and the operator's turn is badged distinctly.
    assert "fastball" in output and "closer" in output
    assert "settle this: which cache?" in output
    assert "write-through is simpler" in output
    assert "dialogue complete · 3 turns" in output


def test_replay_marks_the_bounded_ending(tmp_path) -> None:
    store = Store(":memory:", EventBus())
    try:
        room, messages, agents = build_room(store)
        room["archived_reason"] = "stop_phrase"
        payload = transcript_payload(room, messages, agents)
    finally:
        store.close()

    console = Console(record=True, width=100)
    replay(payload, console, ReplayOptions(no_delay=True))
    assert "ended by stop phrase" in console.export_text()


def test_lifetime_stats_come_from_the_durable_record() -> None:
    store = Store(":memory:", EventBus())
    try:
        store.register_agent("fastball")
        store.ensure_identity("operator:me", AgentKind.CLAUDE)
        task = store.submit_task("t", "spec", "operator:me", assignee="fastball")
        store.claim_task(task.id, "fastball")
        store.complete_task(task.id, "fastball", {"text": "done", "tokens": 500})

        stats = store.lifetime_stats()
        assert stats["tasks_completed"] == 1
        assert stats["work_seconds"] >= 0
        line = store.format_stats(stats)
        assert "1 tasks completed" in line and "local compute" in line
        assert stats["task_tokens"] == 500  # task tokens tracked distinctly from rooms
    finally:
        store.close()


def test_operator_turns_are_visually_distinct_from_model_turns(tmp_path) -> None:
    """A viewer must be able to tell a human interjection from a model reply."""
    store = Store(":memory:", EventBus())
    try:
        room, messages, agents = build_room(store)
        payload = transcript_payload(room, messages, agents)
    finally:
        store.close()

    console = Console(record=True, width=100, force_terminal=True, color_system="truecolor")
    replay(payload, console, ReplayOptions(no_delay=True))
    ansi = console.export_text(styles=True, clear=False)
    plain = console.export_text()

    # The operator gets a reverse-video badge (bold black on bright white); models never do.
    badge = next((line for line in ansi.splitlines() if "\x1b[1;30;107m" in line), None)
    assert badge is not None, f"no operator badge found in:\n{ansi}"
    assert "me" in badge

    # Agent turns carry their model on the label line, so identity is never ambiguous.
    assert "ollama/qwen3:8b" in plain
    assert "vllm/qwen3-32b" in plain


def test_the_header_shows_the_command_that_produced_the_dialogue() -> None:
    store = Store(":memory:", EventBus())
    try:
        store.register_agent("ace", node="gpu-box", backend="ollama/qwen2.5:7b")
        store.register_agent("scout", node="gpu-box", backend="ollama/llama3.2:3b")
        store.ensure_identity("operator:me", AgentKind.CLAUDE)
        room = store.create_room(
            "dialogue: pick a cache eviction policy", "operator:me", ["ace", "scout"]
        )
        payload = transcript_payload(
            store.require_room(room.id).to_dict(), [], [a.to_dict() for a in store.list_agents()]
        )
    finally:
        store.close()

    console = Console(record=True, width=100)
    replay(payload, console, ReplayOptions(no_delay=True))
    output = console.export_text()
    assert "farmteam dialogue ace scout" in output
    assert "pick a cache eviction policy" in output
