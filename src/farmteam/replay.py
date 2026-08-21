"""Record and replay room transcripts for demos.

Live model dialogue is nondeterministic, which breaks scripted screen recorders (VHS
tapes wait on exact output). `record` captures a real conversation once; `replay`
re-streams it with realistic pacing — real content, reproducible render, so the README
GIF can be regenerated in CI forever.

The rendering is deliberately explicit about *who is speaking*: every agent turn is
labelled with the model and machine behind it, and anything the operator types gets a
badge and a slower, typed cadence, so a viewer can tell a human interjection from a
model reply at a glance.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

AGENT_COLORS = ["cyan", "magenta", "green", "yellow", "blue", "red"]
HUMAN_STYLE = "bold black on bright_white"
HUMAN_TEXT = "bold bright_white"
GUTTER = "▏"

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
    """Pacing. Defaults are tuned for a readable screen recording, not for speed."""

    speed: float = 1.0  # multiplier on inter-message gaps
    words_per_sec: float = 13.0  # model output cadence
    human_words_per_sec: float = 6.0  # the operator types slower than a GPU generates
    max_gap_s: float = 1.2  # cap long real-world pauses so the demo stays tight
    no_delay: bool = False  # instant render (tests, sanity checks)


def is_human(name: str, meta: dict) -> bool:
    return meta.get("kind") == "claude" or name.startswith(("claude:", "operator:"))


def replay(payload: dict, console, options: ReplayOptions | None = None) -> None:
    """Stream a recorded conversation to a rich console with realistic pacing."""
    options = options or ReplayOptions()
    participants = payload.get("participants", {})
    room = payload.get("room", {})

    workers = [n for n, meta in participants.items() if not is_human(n, meta)]
    colors = {name: AGENT_COLORS[i % len(AGENT_COLORS)] for i, name in enumerate(workers)}

    _render_header(console, payload, workers, colors, options)

    previous_at: float | None = None
    for message in payload.get("messages", []):
        sender = message["sender"]
        meta = participants.get(sender, {})
        if not options.no_delay and previous_at is not None:
            gap = max(0.0, message["created_at"] - previous_at) / max(options.speed, 0.01)
            time.sleep(min(gap, options.max_gap_s))
        previous_at = message["created_at"]

        if is_human(sender, meta):
            _render_human_turn(console, _display_name(sender), message["body"], options)
        else:
            _render_agent_turn(console, sender, meta, colors.get(sender, "cyan"), message, options)

    _render_footer(console, payload, room)


def _render_header(console, payload: dict, workers, colors, options: ReplayOptions) -> None:
    """Open with the command that produced this conversation, then the roster.

    Without it the recording starts mid-air; with it a viewer knows in one frame what
    was run, which models answered, and what stops the conversation.
    """
    room = payload.get("room", {})
    topic = room.get("topic") or ""
    goal = topic.removeprefix("dialogue: ") if topic.startswith("dialogue: ") else ""

    if goal and workers:
        flags = f' [dim]--goal "{_truncate(goal, 28)}"[/dim]'
        console.print(f"[dim]$[/dim] [bold]farmteam dialogue {' '.join(workers)}[/bold]{flags}")
    console.print()

    participants = payload.get("participants", {})
    for name in workers:
        meta = participants.get(name, {})
        model = meta.get("backend") or "local model"
        node = meta.get("node")
        where = f" [dim]@ {node}[/dim]" if node else ""
        colour = colors.get(name, "cyan")
        console.print(f"  [bold {colour}]{name:<7}[/bold {colour}] [dim]{model}[/dim]{where}")

    policy = room.get("policy") or {}
    bounds = []
    if policy.get("max_messages"):
        bounds.append(f"{policy['max_messages']} turns max")
    if policy.get("stop_phrase"):
        bounds.append(f'stops on "{policy["stop_phrase"]}"')
    if bounds:
        console.print(f"  [dim]{'bounded':<7} {' · '.join(bounds)}[/dim]")
    console.print()


def _render_agent_turn(console, name, meta, colour, message, options: ReplayOptions) -> None:
    model = meta.get("backend") or ""
    node = meta.get("node")
    suffix = f" [dim]@ {node}[/dim]" if node else ""
    console.print(f"[bold {colour}]{name}[/bold {colour}]  [dim]{model}[/dim]{suffix}")
    _stream_body(
        console, message["body"], options.words_per_sec, options, style=None, colour=colour
    )
    console.print()


def _render_human_turn(console, name: str, body: str, options: ReplayOptions) -> None:
    """The operator's turn: badged, and typed at human speed so it reads as an interruption."""
    console.print(f"[{HUMAN_STYLE}] {name} [/{HUMAN_STYLE}]")
    _stream_body(
        console, body, options.human_words_per_sec, options, style=HUMAN_TEXT, colour="bright_white"
    )
    console.print()


def _stream_body(console, body: str, rate: float, options: ReplayOptions, style, colour) -> None:
    """Word-chunked streaming behind a coloured gutter, so turns read as blocks.

    Wrapping is done by hand rather than left to the console: a continuation line that
    starts at column zero breaks the visual block and makes a long turn look like two.
    """
    gutter = f"[{colour}]{GUTTER}[/{colour}] "
    indent = "  "
    opener, closer = (f"[{style}]", f"[/{style}]") if style else ("", "")
    limit = max(40, (console.width or 100) - len(indent) - 1)

    if options.no_delay:
        console.print(f"{gutter}{opener}{_escape(body)}{closer}")
        return

    delay = 1.0 / max(rate, 0.5)
    console.print(gutter, end="")
    column = len(indent)
    for index, raw in enumerate(body.split(" ")):
        word = raw.strip()
        if not word:
            continue
        lead = "" if index == 0 or column == len(indent) else " "
        if column + len(lead) + len(word) > limit:
            console.print()
            console.print(indent, end="")
            column, lead = len(indent), ""
        console.print(
            f"{opener}{_escape(lead + word)}{closer}", end="", highlight=False, markup=True
        )
        column += len(lead) + len(word)
        time.sleep(delay)
    console.print()


def _render_footer(console, payload: dict, room: dict) -> None:
    reason = room.get("archived_reason")
    turns = len(payload.get("messages", []))
    footer = f"dialogue complete · {turns} turns"
    if reason:
        footer += f" · ended by {reason.replace('_', ' ')}"
    console.print(f"[dim]{'─' * 60}[/dim]")
    console.print(f"[dim]{footer}[/dim]")


def _display_name(identity: str) -> str:
    """Show the human as their bare name; `claude:operator:tate` is transport detail."""
    name = identity
    for prefix in ("claude:", "operator:"):
        name = name.removeprefix(prefix)
    return name or identity


def _truncate(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _escape(text: str) -> str:
    """Model output is data — never let a stray bracket read as rich markup."""
    return text.replace("[", "\\[")
