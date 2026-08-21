"""Record and replay room transcripts for demos.

Live model dialogue is nondeterministic, which breaks scripted screen recorders (VHS
tapes wait on exact output). `record` captures a real conversation once; `replay`
re-streams it with realistic token pacing — real content, reproducible render, so the
README GIF can be regenerated in CI forever.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

PALETTE = ["cyan", "magenta", "yellow", "green", "blue", "red"]
HUMAN_COLOR = "bright_white"

FORMAT_VERSION = 1


def transcript_payload(room: dict, messages: list[dict], agents: list[dict]) -> dict:
    """The recording format: everything replay needs, nothing environment-specific."""
    by_name = {a["name"]: a for a in agents}
    participants = {}
    for member in room.get("members", []):
        agent = by_name.get(member, {})
        participants[member] = {
            "kind": agent.get("kind", "worker"),
            "backend": agent.get("backend"),
            "node": agent.get("node"),
        }
    return {
        "version": FORMAT_VERSION,
        "room": {
            "topic": room.get("topic", ""),
            "policy": room.get("policy", {}),
            "archived_reason": room.get("archived_reason"),
        },
        "participants": participants,
        "messages": [
            {
                "sender": m["sender"],
                "body": m["body"],
                "created_at": m["created_at"],
            }
            for m in messages
        ],
    }


def save_transcript(path: str | Path, payload: dict) -> Path:
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2) + "\n")
    return target


@dataclass(slots=True)
class ReplayOptions:
    speed: float = 1.0  # multiplier on inter-message gaps
    tokens_per_sec: float = 40.0  # streaming rate within a message
    max_gap_s: float = 2.5  # cap long real-world pauses so the demo stays tight
    no_delay: bool = False  # instant render (tests, sanity checks)


def replay(payload: dict, console, options: ReplayOptions | None = None) -> None:
    """Stream a recorded conversation to a rich console with realistic pacing."""
    options = options or ReplayOptions()
    participants = payload.get("participants", {})
    room = payload.get("room", {})

    colors: dict[str, str] = {}
    for name, meta in participants.items():
        if meta.get("kind") == "claude" or name.startswith(("claude:", "operator:")):
            colors[name] = HUMAN_COLOR
        else:
            colors[name] = PALETTE[
                len([c for c in colors.values() if c != HUMAN_COLOR]) % len(PALETTE)
            ]

    header = " ⇄ ".join(
        f"{name} · {meta['backend']}" + (f" @ {meta['node']}" if meta.get("node") else "")
        for name, meta in participants.items()
        if colors.get(name) != HUMAN_COLOR and meta.get("backend")
    )
    if header:
        console.print(f"[bold]{header}[/bold]")
    topic = room.get("topic") or ""
    if topic and not topic.startswith("dialogue: "):
        # Dialogue rooms open with the goal as the human's first message — printing the
        # (truncated) topic above it would just say the same thing twice.
        console.print(f"[dim]goal: {topic}[/dim]")
    console.print()

    previous_at: float | None = None
    for message in payload.get("messages", []):
        sender = message["sender"]
        color = colors.get(sender, PALETTE[0])
        display = _display_name(sender) if color == HUMAN_COLOR else sender
        if not options.no_delay and previous_at is not None:
            gap = max(0.0, (message["created_at"] - previous_at)) / max(options.speed, 0.01)
            time.sleep(min(gap, options.max_gap_s))
        previous_at = message["created_at"]

        console.print(f"[bold {color}]{display}[/bold {color}]: ", end="")
        _stream_body(console, message["body"], options)
        console.print()

    reason = room.get("archived_reason")
    footer = f"dialogue complete · {len(payload.get('messages', []))} turns"
    if reason:
        footer += f" · ended by {reason}"
    console.print(f"\n[dim]{footer}[/dim]")


def _display_name(identity: str) -> str:
    """Show the human as their bare name; `claude:operator:tate` is transport detail."""
    name = identity
    for prefix in ("claude:", "operator:"):
        name = name.removeprefix(prefix)
    return name or identity


def _stream_body(console, body: str, options: ReplayOptions) -> None:
    """Word-chunked streaming that reads like live token output.

    Goes through the console (not raw stdout) so capture/record pipelines see it too.
    """
    if options.no_delay:
        console.print(body, end="", markup=False, highlight=False)
        return
    delay = 1.0 / max(options.tokens_per_sec, 1.0)
    for i, word in enumerate(body.split(" ")):
        console.print(("" if i == 0 else " ") + word, end="", markup=False, highlight=False)
        time.sleep(delay)
