"""``farmteam`` — set up a hub, bring a worker online, and drive the fleet.

Onboarding is meant to be two commands total:

    # on the hub machine (where Claude Code runs)
    farmteam up

    # on each worker (auto-detects the local model)
    farmteam join --hub http://<hub> --token <printed by up>

Everything else (submit, ask, watch, agents, tasks) talks to the hub over the same MCP
endpoint Claude Code uses, so the user's CLI needs no token of its own.
"""

from __future__ import annotations

import asyncio
import getpass
import logging
import os
import secrets
import shutil
import socket
import subprocess
import time
from pathlib import Path

import httpx
import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from . import procs, settings
from .agent import AgentConfig, run_agent
from .agent.detect import autodetect, preflight, probe

app = typer.Typer(
    help="Task dispatch and multi-agent conversation hub for Claude Code + local AI.",
    no_args_is_help=True,
    add_completion=False,
)
hub_app = typer.Typer(help="Run and inspect the hub (advanced; `up` is the easy path).")
agent_app = typer.Typer(help="Run an agent from a TOML config (advanced; `join` is easier).")
app.add_typer(hub_app, name="hub")
app.add_typer(agent_app, name="agent")
console = Console()

OPERATOR_IDENTITY = f"operator:{getpass.getuser()}"


# --------------------------------------------------------------- shared plumbing


def _hub_url(explicit: str | None = None) -> str:
    return (explicit or settings.load().hub_url).rstrip("/")


def _mcp_url(explicit: str | None = None) -> str:
    return f"{_hub_url(explicit)}/mcp"


async def _call(tool: str, arguments: dict, *, identity: str | None = None, hub: str | None = None):
    """Call one MCP tool and return its data, surfacing hub errors as a clean exit."""
    from fastmcp import Client

    async with Client(_mcp_url(hub)) as client:
        if identity:
            await client.call_tool("set_identity", {"label": identity})
        result = await client.call_tool(tool, arguments)
    data = result.data
    if isinstance(data, dict) and "error" in data and isinstance(data["error"], dict):
        console.print(
            f"[red]{data['error'].get('code', 'error')}[/red]: {data['error']['message']}"
        )
        raise typer.Exit(1)
    return data


def _mcp(tool: str, arguments: dict, *, identity: str | None = None, hub: str | None = None):
    try:
        return asyncio.run(_call(tool, arguments, identity=identity, hub=hub))
    except typer.Exit:
        raise
    except Exception as exc:  # noqa: BLE001 - a CLI should not stacktrace on a dead hub
        console.print(
            f"[red]could not reach the hub[/red] at {_hub_url(hub)} — {type(exc).__name__}: {exc}"
        )
        console.print("Is it running? Start one with [cyan]farmteam up[/cyan].")
        raise typer.Exit(1) from exc


# =============================================================== onboarding: up


def _wire_claude_code(mcp_url: str) -> None:
    if shutil.which("claude") is None:
        console.print(
            "[yellow]note:[/yellow] the `claude` CLI is not on PATH, so I did not wire it up. "
            f"Add it yourself with:\n  [cyan]claude mcp add --transport http farmteam "
            f"{mcp_url}[/cyan]"
        )
        return
    result = subprocess.run(
        [
            "claude",
            "mcp",
            "add",
            "--scope",
            "user",
            "--transport",
            "http",
            "farmteam",
            mcp_url,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        console.print(
            "[green]✓[/green] wired into Claude Code (user scope) as [bold]farmteam[/bold]"
        )
    elif "already exists" in (result.stderr + result.stdout).lower():
        console.print("[green]✓[/green] already wired into Claude Code as [bold]farmteam[/bold]")
    else:
        console.print(
            "[yellow]note:[/yellow] could not wire Claude Code automatically. Run:\n"
            f"  [cyan]claude mcp add --transport http farmteam {mcp_url}[/cyan]"
        )


def _serve_hub(host: str, port: int, db: str, cfg: settings.Settings) -> None:
    import uvicorn

    os.environ["FARMTEAM_DB"] = db
    if cfg.admin_token:
        os.environ.setdefault("FARMTEAM_ADMIN_TOKEN", cfg.admin_token)
    if cfg.register_token:
        os.environ.setdefault("FARMTEAM_REGISTER_TOKEN", cfg.register_token)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    uvicorn.run(
        "farmteam.hub.app:app_factory",
        host=host,
        port=port,
        factory=True,
        log_level="info",
    )


@app.command("up")
def up(
    port: int = typer.Option(8787, help="Port to serve on."),
    host: str = typer.Option("0.0.0.0", help="Bind address. LAN/Tailscale only — never funnel."),
    advertise: str = typer.Option(
        None, help="URL workers should use to reach this hub (default: best-guess LAN address)."
    ),
    detach: bool = typer.Option(False, "--detach", "-d", help="Run in the background and return."),
    open_mode: bool = typer.Option(
        False, "--open", help="Skip token generation — anyone on the network can drive the hub."
    ),
    no_wire: bool = typer.Option(False, "--no-wire", help="Do not touch the Claude Code config."),
    db: str = typer.Option(None, help="SQLite path (default: <farmteam home>/hub.db)."),
) -> None:
    """Start the hub, wire it into Claude Code, and print the worker join command."""
    db = db or str(settings.home() / "hub.db")
    cfg = settings.load()
    if not open_mode:
        cfg.admin_token = cfg.admin_token or secrets.token_urlsafe(24)
        cfg.register_token = cfg.register_token or secrets.token_urlsafe(24)
    else:
        cfg.admin_token = None
        cfg.register_token = None
    cfg.hub_url = f"http://localhost:{port}"
    cfg.advertise_url = advertise or settings.best_lan_url(port)
    settings.save(cfg)

    mcp_url = f"{cfg.hub_url}/mcp"
    if not no_wire:
        _wire_claude_code(mcp_url)

    join = f"farmteam join --hub {cfg.advertise_url}"
    if cfg.register_token:
        join += f" --token {cfg.register_token}"
    console.print(
        Panel(
            f"[bold]Run this on each worker node[/bold] (auto-detects the local model):\n\n"
            f"  [cyan]{join}[/cyan]\n\n"
            f"On a Tailnet, swap the host for the machine's MagicDNS name.\n"
            f"Talk to the fleet from here with [cyan]farmteam ask[/cyan] / "
            f"[cyan]submit[/cyan] / [cyan]agents[/cyan].",
            title="farmteam is up",
            border_style="green",
        )
    )
    if open_mode:
        console.print(
            "[yellow]open mode:[/yellow] no tokens — any process that can reach this port can "
            "drive the hub. Fine on a trusted LAN; never expose it publicly.\n"
        )

    if detach:
        command = procs.cli_executable() + [
            "hub",
            "serve",
            "--host",
            host,
            "--port",
            str(port),
            "--db",
            db,
        ]
        proc = procs.spawn("hub", "hub", command)
        _wait_healthy(cfg.hub_url)
        console.print(
            f"[green]✓[/green] hub running in the background (pid {proc.pid}), logging to "
            f"{proc.log}\n  stop it with [cyan]farmteam down[/cyan]."
        )
        return

    console.print(f"[dim]serving on http://{host}:{port} — Ctrl-C to stop[/dim]\n")
    try:
        _serve_hub(host, port, db, cfg)
    except KeyboardInterrupt:
        console.print("\nstopped")


@app.command("down")
def down() -> None:
    """Stop the hub and any agents started with --detach on this machine."""
    stopped = procs.stop_all()
    if not stopped:
        console.print("nothing running in the background here.")
        return
    for label in stopped:
        console.print(f"[green]stopped[/green] {label}")


@app.command("ps")
def ps() -> None:
    """Show background hub/agent processes started on this machine."""
    running = procs.list_all()
    if not running:
        console.print("no background processes here. (foreground ones aren't tracked.)")
        return
    table = Table("label", "kind", "pid", "state", "log")
    for proc in running:
        alive = procs.is_alive(proc.pid)
        table.add_row(
            proc.label,
            proc.kind,
            str(proc.pid),
            "[green]alive[/green]" if alive else "[red]dead[/red]",
            proc.log,
        )
    console.print(table)


# =============================================================== onboarding: join


@app.command("join")
def join(
    hub: str = typer.Option(None, help="Hub URL (default: the one saved by a prior join/up)."),
    token: str = typer.Option(None, help="Register token from `up` (omit in open mode)."),
    name: str = typer.Option(None, help="Agent name (default: this machine's hostname)."),
    model: str = typer.Option(None, help="Model to serve (default: auto-detected)."),
    base_url: str = typer.Option(None, help="Model API base (default: auto-detected)."),
    backend: str = typer.Option(
        "openai_compat", help="Backend adapter: openai_compat | anthropic_compat | tahoma."
    ),
    tier: str = typer.Option(None, help="Capability tier tag, e.g. fast or reasoning."),
    tag: list[str] = typer.Option(None, "--tag", help="Extra capability tag (repeatable)."),
    detach: bool = typer.Option(False, "--detach", "-d", help="Run in the background and return."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Bring a worker online: detect the local model, verify it, and register."""
    cfg = settings.load()
    hub = hub or cfg.hub_url
    token = token or cfg.register_token
    name = name or socket.gethostname().split(".")[0]

    runtime: str | None = None
    if not base_url:
        console.print("probing for a local model server…")
        detected = autodetect()
        if detected is None:
            console.print(
                "[red]no local model server found[/red] on the usual ports (Ollama 11434, "
                "vLLM 8000, LM Studio 1234).\nStart one, or pass [cyan]--base-url[/cyan] and "
                "[cyan]--model[/cyan] to point at it."
            )
            raise typer.Exit(1)
        base_url = detected.base_url
        runtime = detected.runtime
        model = detected.pick_model(model)
        console.print(
            f"[green]✓[/green] found [bold]{detected.runtime}[/bold] at {base_url}"
            + (f" → model [bold]{model}[/bold]" if model else "")
        )

    if not model:
        console.print("[red]no model selected[/red]; pass --model.")
        raise typer.Exit(1)

    backend_cfg = {"type": backend, "base_url": base_url, "model": model}
    if runtime and runtime != "openai_compat":
        backend_cfg["runtime"] = runtime
    console.print("verifying the model answers…")
    ok, message = asyncio.run(preflight(backend_cfg))
    if not ok:
        console.print(f"[red]the model endpoint did not answer[/red]: {message}")
        console.print(f"Check that {base_url} is serving [bold]{model}[/bold].")
        raise typer.Exit(1)
    console.print(f"[green]✓[/green] model reachable (replied: {message[:40]!r})")

    tags = list(tag or [])
    if tier:
        tags.append(f"tier:{tier}")
    tags.append(f"node:{name}")

    # Remember hub + token so a plain `farmteam join` works next time, and so
    # `ask`/`agents`/etc. on this box point at the right hub.
    cfg.hub_url = hub
    if token:
        cfg.register_token = token
    settings.save(cfg)

    agent_config = AgentConfig(
        name=name,
        hub=hub,
        tags=tags,
        register_token=token,
        backend=backend_cfg,
    )

    if detach:
        command = procs.cli_executable() + [
            "agent",
            "run-detached-worker",
            "--name",
            name,
            "--hub",
            hub,
            "--base-url",
            base_url,
            "--model",
            model,
            "--backend",
            backend,
        ]
        if runtime:
            command += ["--runtime", runtime]
        for extra in tags:
            command += ["--tag", extra]
        if token:
            command += ["--token", token]
        proc = procs.spawn(f"agent-{name}", "agent", command)
        console.print(
            f"[green]✓[/green] [bold]{name}[/bold] running in the background (pid {proc.pid}), "
            f"logging to {proc.log}\n  stop it with [cyan]farmteam down[/cyan]."
        )
        return

    _run_worker(agent_config, verbose)


def _run_worker(agent_config: AgentConfig, verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    console.print(
        f"[bold]{agent_config.name}[/bold] → {agent_config.hub}  "
        f"({agent_config.backend_label()}, tags: {', '.join(agent_config.tags)})\n"
        f"[dim]listening for tasks and messages — Ctrl-C to stop[/dim]"
    )
    try:
        asyncio.run(run_agent(agent_config))
    except KeyboardInterrupt:
        console.print("\nstopped")


# =============================================================== doctor


@app.command("doctor")
def doctor() -> None:
    """Diagnose a setup: config, hub reachability, and local model servers."""
    cfg = settings.load()
    console.print("[bold]farmteam doctor[/bold]\n")

    ok = "[green]✓[/green]"
    bad = "[red]✗[/red]"

    if settings.config_path().exists():
        console.print(f"{ok} config at {settings.config_path()}")
    else:
        console.print(
            f"{bad} no config yet — run [cyan]farmteam up[/cyan] (hub) or "
            f"[cyan]join[/cyan] (worker)"
        )
    console.print(f"    hub_url: {cfg.hub_url}")
    console.print(f"    tokens:  {'set' if cfg.admin_token or cfg.register_token else 'OPEN MODE'}")

    try:
        health = httpx.get(f"{cfg.hub_url}/healthz", timeout=5.0).json()
        console.print(
            f"{ok} hub reachable — {health['agents']} agents "
            f"({health['online']} online), {health['queued_tasks']} queued tasks"
        )
    except Exception as exc:  # noqa: BLE001
        console.print(f"{bad} hub not reachable at {cfg.hub_url} ({type(exc).__name__})")

    detected = probe()
    if detected:
        for backend in detected:
            models = ", ".join(backend.models[:4]) or "(no models loaded)"
            console.print(
                f"{ok} local model server: {backend.runtime} at {backend.base_url} — {models}"
            )
    else:
        console.print(
            "[dim]•[/dim] no local model server detected on this machine (fine for a hub-only box)"
        )

    if shutil.which("claude"):
        listed = subprocess.run(["claude", "mcp", "list"], capture_output=True, text=True)
        if "farmteam" in (listed.stdout + listed.stderr):
            console.print(f"{ok} Claude Code is wired to farmteam")
        else:
            console.print(
                "[dim]•[/dim] Claude Code present but not wired — run "
                "[cyan]farmteam up[/cyan] or add it manually"
            )
    else:
        console.print(
            "[dim]•[/dim] `claude` CLI not on PATH (only needed on the box you drive from)"
        )


# =============================================================== interaction


@app.command("agents")
def list_agents(hub: str = typer.Option(None)) -> None:
    """List agents on the fleet."""
    data = _mcp("list_agents", {}, hub=hub)
    table = Table("name", "kind", "node", "backend", "tags", "status")
    for agent in data["agents"]:
        colour = {"online": "green", "busy": "yellow"}.get(agent["status"], "dim")
        table.add_row(
            agent["name"],
            agent["kind"],
            agent["node"] or "-",
            agent["backend"] or "-",
            ",".join(agent["tags"]) or "-",
            f"[{colour}]{agent['status']}[/{colour}]",
        )
    console.print(table)


@app.command("submit")
def submit(
    spec: str = typer.Argument(..., help="What the agent should do."),
    to: str = typer.Option(None, "--to", help="Agent name to assign it to."),
    tier: str = typer.Option(None, "--tier", help="Assign to any agent with tier:<x>."),
    title: str = typer.Option(None, help="Short title (default: first line of the spec)."),
    hub: str = typer.Option(None),
) -> None:
    """Dispatch a background task and print its id (check it with `task`)."""
    if not to and not tier:
        console.print("[red]pick a target[/red]: --to <agent> or --tier <tier>")
        raise typer.Exit(1)
    args = {"title": title or spec.splitlines()[0][:60], "spec": spec}
    if to:
        args["assignee"] = to
    if tier:
        args["selector"] = f"tier:{tier}"
    data = _mcp("submit_task", args, identity=OPERATOR_IDENTITY, hub=hub)
    console.print(
        f"[green]dispatched[/green] [bold]{data['task_id']}[/bold] → "
        f"{data['task']['assignee'] or data['task']['selector']}"
    )
    console.print(f"  check it: [cyan]farmteam task {data['task_id']}[/cyan]")


@app.command("result")
def result(task_id: str, hub: str = typer.Option(None)) -> None:
    """Print a task's result (or why it isn't ready)."""
    data = _mcp("task_result", {"task_id": task_id}, hub=hub)
    if not data["ready"]:
        console.print(f"[yellow]{data['state']}[/yellow] — not finished yet")
        raise typer.Exit(0)
    if data.get("error"):
        console.print(f"[red]failed[/red]: {data['error']}")
        raise typer.Exit(1)
    payload = data["result"] or {}
    console.print(payload.get("text") if isinstance(payload, dict) else payload)


@app.command("send")
def send(agent: str, message: str, hub: str = typer.Option(None)) -> None:
    """Send a message to an agent without waiting for the reply."""
    data = _mcp(
        "send_message", {"to": agent, "message": message}, identity=OPERATOR_IDENTITY, hub=hub
    )
    console.print(f"[green]sent[/green] to {agent} (room {data['room_id']})")
    console.print(f"  read the reply: [cyan]farmteam watch {data['room_id']}[/cyan]")


@app.command("ask")
def ask(
    agent: str,
    message: str,
    wait: float = typer.Option(45.0, help="Seconds to wait for the reply."),
    hub: str = typer.Option(None),
) -> None:
    """Send a message to an agent and wait for its reply."""

    async def _ask():
        from fastmcp import Client

        async with Client(_mcp_url(hub)) as client:
            await client.call_tool("set_identity", {"label": OPERATOR_IDENTITY})
            sent = await client.call_tool("send_message", {"to": agent, "message": message})
            room_id = sent.data["room_id"]
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                got = await client.call_tool(
                    "fetch_messages", {"room": room_id, "after": 0, "wait_s": min(30.0, wait)}
                )
                replies = [m for m in got.data["messages"] if m["sender"] == agent]
                if replies:
                    return replies[-1]["body"]
            return None

    console.print(f"[dim]asking {agent}…[/dim]")
    reply = _mcp_run(_ask, hub)
    if reply is None:
        console.print(
            f"[yellow]no reply within {wait:.0f}s[/yellow] — the agent may be offline or slow."
        )
        raise typer.Exit(1)
    console.print(Panel(reply, title=agent, border_style="cyan"))


def _mcp_run(coro_fn, hub):
    try:
        return asyncio.run(coro_fn())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]could not reach the hub[/red] at {_hub_url(hub)} — {exc}")
        raise typer.Exit(1) from exc


@app.command("dialogue")
def dialogue(
    agents: list[str] = typer.Argument(..., help="Two or more agent names."),
    goal: str = typer.Option(..., "--goal", "-g", help="What they should work toward."),
    max_messages: int = typer.Option(20, help="Cap before the room auto-closes."),
    stop_phrase: str = typer.Option(None, help="Archive the room when an agent says this."),
    watch: bool = typer.Option(True, help="Follow the conversation live after starting it."),
    record: str = typer.Option(
        None, "--record", help="Save the finished transcript to this JSON file (for `replay`)."
    ),
    hub: str = typer.Option(None),
) -> None:
    """Have two or more local agents converse autonomously toward a goal."""
    args = {"participants": agents, "goal": goal, "max_messages": max_messages}
    if stop_phrase:
        args["stop_phrase"] = stop_phrase
    data = _mcp("start_dialogue", args, identity=OPERATOR_IDENTITY, hub=hub)
    room_id = data["room_id"]
    console.print(f"[green]started[/green] dialogue in room [bold]{room_id}[/bold]")
    if watch or record:
        _follow_room(room_id, hub, record=record)
    else:
        console.print(f"  watch it: [cyan]farmteam watch {room_id}[/cyan]")


@app.command("watch")
def watch(
    room_id: str,
    record: str = typer.Option(
        None, "--record", help="Save the transcript to this JSON file when the room closes."
    ),
    hub: str = typer.Option(None),
) -> None:
    """Follow a room's conversation live."""
    _follow_room(room_id, hub, record=record)


@app.command("tail", hidden=True)
def tail(room_id: str, hub: str = typer.Option(None)) -> None:
    """Alias for `watch`."""
    _follow_room(room_id, hub)


def _follow_room(room_id: str, hub: str | None, record: str | None = None) -> None:
    seen = 0
    console.print(f"[dim]following {room_id} — Ctrl-C to stop[/dim]")
    closed = False
    try:
        while True:
            data = _mcp("room_transcript", {"room": room_id, "from_seq": seen}, hub=hub)
            for message in data["messages"]:
                seen = message["seq"]
                console.print(f"[bold cyan]{message['sender']}[/bold cyan]: {message['body']}")
            if data["room"]["archived"]:
                console.print(f"[dim]— room closed ({data['room']['archived_reason']})[/dim]")
                closed = True
                return
            time.sleep(2.0)
    except KeyboardInterrupt:
        console.print("\nstopped following")
    finally:
        if record:
            _save_transcript(room_id, record, hub, complete=closed)


def _save_transcript(room_id: str, path: str, hub: str | None, complete: bool) -> None:
    from .replay import save_transcript, transcript_payload

    transcript = _mcp("room_transcript", {"room": room_id, "from_seq": 0, "limit": 1000}, hub=hub)
    fleet = _mcp("list_agents", {}, hub=hub)
    payload = transcript_payload(transcript["room"], transcript["messages"], fleet["agents"])
    target = save_transcript(path, payload)
    note = "" if complete else " (room still open — partial transcript)"
    console.print(f"[green]recorded[/green] {len(payload['messages'])} turns → {target}{note}")
    console.print(f"  replay it: [cyan]farmteam replay {target}[/cyan]")


@app.command("replay")
def replay_cmd(
    transcript: str = typer.Argument(..., help="A JSON transcript saved with --record."),
    speed: float = typer.Option(1.0, help="Multiplier on the real inter-message gaps."),
    tokens_per_sec: float = typer.Option(40.0, help="Streaming rate within a message."),
    no_delay: bool = typer.Option(False, "--no-delay", help="Render instantly (sanity checks)."),
) -> None:
    """Re-stream a recorded conversation with realistic pacing.

    Live model output is nondeterministic; a recorded transcript replays identically
    every time, which is what screen-recorder pipelines (VHS, asciinema) need to render
    a reproducible demo GIF from a real conversation.
    """
    import json as _json

    from .replay import ReplayOptions, replay

    payload = _json.loads(Path(transcript).expanduser().read_text())
    replay(
        payload,
        console,
        ReplayOptions(speed=speed, tokens_per_sec=tokens_per_sec, no_delay=no_delay),
    )


@app.command("stats")
def stats(hub: str = typer.Option(None)) -> None:
    """Show what the farm team has done for you, lifetime."""
    try:
        health = httpx.get(f"{_hub_url(hub)}/healthz", timeout=5.0).json()
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]hub unreachable[/red]: {exc}")
        raise typer.Exit(1) from exc
    lifetime = health.get("lifetime", {})
    hours = lifetime.get("work_seconds", 0) / 3600.0
    table = Table(show_header=False, box=None)
    table.add_row("tasks completed", f"[bold]{lifetime.get('tasks_completed', 0)}[/bold]")
    table.add_row("tokens kept local", f"[bold]{lifetime.get('local_tokens', 0):,}[/bold]")
    table.add_row("messages exchanged", f"[bold]{lifetime.get('messages', 0):,}[/bold]")
    table.add_row("work run on your hardware", f"[bold]{hours:.1f}h[/bold]")
    table.add_row("fleet", f"{health.get('online', 0)}/{health.get('agents', 0)} agents online")
    console.print(table)


@app.command("tasks")
def list_tasks(state: str = typer.Option(None), hub: str = typer.Option(None)) -> None:
    """List tasks."""
    args = {"state": state} if state else {}
    data = _mcp("list_tasks", args, hub=hub)
    table = Table("id", "title", "state", "assignee", "progress")
    for task in data["tasks"]:
        pct = task["progress"]["pct"]
        table.add_row(
            task["id"],
            task["title"][:48],
            task["state"],
            task["assignee"] or "-",
            f"{pct:.0f}%" if pct is not None else "-",
        )
    console.print(table)


@app.command("task")
def show_task(task_id: str, hub: str = typer.Option(None)) -> None:
    """Show one task with its progress and event history."""
    data = _mcp("task_status", {"task_id": task_id}, hub=hub)
    task = data["task"]
    console.print(
        f"[bold]{task['title']}[/bold]  [{task['state']}]  → {task['assignee'] or task['selector']}"
    )
    if task["progress"]["message"]:
        console.print(f"  progress: {task['progress']['message']}")
    if task["result"]:
        text = task["result"].get("text") if isinstance(task["result"], dict) else task["result"]
        console.print(f"  result: {str(text)[:200]}")
    if task["error"]:
        console.print(f"  [red]error[/red]: {task['error']}")
    table = Table("event", "detail")
    for event in data["events"]:
        detail = event["payload"].get("message") or event["payload"].get("error") or ""
        table.add_row(event["kind"], str(detail)[:60])
    console.print(table)


@app.command("rooms")
def list_rooms(
    active_only: bool = typer.Option(False, "--active", help="Hide closed conversations."),
    hub: str = typer.Option(None),
) -> None:
    """List conversations, including recently closed ones."""
    data = _mcp("list_rooms", {"mine_only": False, "include_archived": not active_only}, hub=hub)
    table = Table("id", "topic", "members", "turns", "state")
    for room in data["rooms"]:
        state = f"archived ({room['archived_reason']})" if room["archived"] else "open"
        table.add_row(
            room["id"],
            room["topic"][:44],
            ",".join(room["members"]),
            room["policy"]["turn_policy"],
            state,
        )
    console.print(table)


@app.command("cancel")
def cancel_task(task_id: str, hub: str = typer.Option(None)) -> None:
    """Cancel a task."""
    data = _mcp("cancel_task", {"task_id": task_id}, identity=OPERATOR_IDENTITY, hub=hub)
    console.print(f"{task_id} → [yellow]{data['task']['state']}[/yellow]")


@app.command("install-skill")
def install_skill(
    project: str = typer.Option(
        ".", help="Project directory to install the skill into (its .claude/skills/)."
    ),
    user: bool = typer.Option(
        False, "--user", help="Install for all projects (~/.claude/skills/)."
    ),
) -> None:
    """Install the Claude Code skill that teaches agents how and when to use the fleet."""
    from importlib import resources
    from pathlib import Path

    source = resources.files("farmteam.resources.skill")
    target_root = (
        Path("~/.claude/skills").expanduser() if user else Path(project) / ".claude" / "skills"
    )
    target = target_root / "farmteam"
    target.mkdir(parents=True, exist_ok=True)
    for entry in source.iterdir():
        if entry.is_file() and not entry.name.startswith("__"):
            (target / entry.name).write_text(entry.read_text())
    console.print(f"[green]✓[/green] installed the farmteam skill into {target}")
    console.print("  Claude Code will pick it up in that scope on its next run.")


# =============================================================== advanced (hub/agent)


@hub_app.command("serve")
def hub_serve(
    host: str = typer.Option("0.0.0.0"),
    port: int = typer.Option(8787),
    db: str = typer.Option(None, help="SQLite path (default: <farmteam home>/hub.db)."),
) -> None:
    """Run the hub in the foreground (advanced; `up` wraps this with setup)."""
    cfg = settings.load()
    db = db or str(settings.home() / "hub.db")
    console.print(f"[bold]farmteam hub[/bold] → http://{host}:{port}  (MCP at /mcp)")
    if not cfg.admin_token and not cfg.register_token:
        console.print("[yellow]open mode — no tokens set.[/yellow]")
    _serve_hub(host, port, db, cfg)


@hub_app.command("health")
def hub_health(hub: str = typer.Option(None)) -> None:
    """Check that the hub is up."""
    try:
        console.print_json(data=httpx.get(f"{_hub_url(hub)}/healthz", timeout=5.0).json())
    except Exception as exc:  # noqa: BLE001
        console.print(f"[red]unreachable[/red]: {exc}")
        raise typer.Exit(1) from exc


@agent_app.command("run")
def agent_run(
    config: str = typer.Option(..., "--config", "-c", help="Path to the agent TOML config."),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run an agent from a TOML config (advanced; `join` is the easy path)."""
    _run_worker(AgentConfig.load(config), verbose)


@agent_app.command("run-detached-worker", hidden=True)
def _run_detached_worker(
    name: str = typer.Option(...),
    hub: str = typer.Option(...),
    base_url: str = typer.Option(...),
    model: str = typer.Option(...),
    backend: str = typer.Option("openai_compat"),
    runtime: str = typer.Option(None),
    token: str = typer.Option(None),
    tag: list[str] = typer.Option(None, "--tag"),
) -> None:
    """Internal entry point used by `join --detach`."""
    backend_cfg = {"type": backend, "base_url": base_url, "model": model}
    if runtime:
        backend_cfg["runtime"] = runtime
    _run_worker(
        AgentConfig(
            name=name,
            hub=hub,
            tags=list(tag or []),
            register_token=token,
            backend=backend_cfg,
        ),
        verbose=False,
    )


def _wait_healthy(hub_url: str, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{hub_url}/healthz", timeout=2.0).status_code == 200:
                return True
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    return False


def main() -> None:
    app()


def legacy_main() -> None:
    """Entry point for the deprecated `cascadia-tasks` command name."""
    console.print(
        "[yellow]note:[/yellow] `cascadia-tasks` is now [bold]farmteam[/bold] — "
        "same tool, new name. This alias keeps working for now.\n"
    )
    app()


if __name__ == "__main__":
    main()
