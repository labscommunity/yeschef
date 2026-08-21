"""`cascadia-tasks` command: run the hub, run an agent, and inspect state."""

from __future__ import annotations

import asyncio
import json
import logging
import os

import httpx
import typer
from rich.console import Console
from rich.table import Table

from .agent import AgentConfig, run_agent

app = typer.Typer(help="Task dispatch and multi-agent conversation hub.", no_args_is_help=True)
hub_app = typer.Typer(help="Run and inspect the hub.")
agent_app = typer.Typer(help="Run a local agent.")
app.add_typer(hub_app, name="hub")
app.add_typer(agent_app, name="agent")
console = Console()


def _hub_url(explicit: str | None) -> str:
    return (explicit or os.environ.get("CASCADIA_TASKS_HUB", "http://localhost:8787")).rstrip("/")


def _headers() -> dict[str, str]:
    token = os.environ.get("CASCADIA_TASKS_ADMIN_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def _get(path: str, hub: str | None = None, **params) -> dict:
    url = f"{_hub_url(hub)}{path}"
    response = httpx.get(url, params=params, headers=_headers(), timeout=30.0)
    if response.status_code >= 400:
        console.print(f"[red]{response.status_code}[/red] {response.text[:300]}")
        raise typer.Exit(1)
    return response.json()


def _post(path: str, hub: str | None = None, **payload) -> dict:
    url = f"{_hub_url(hub)}{path}"
    response = httpx.post(url, json=payload, headers=_headers(), timeout=30.0)
    if response.status_code >= 400:
        console.print(f"[red]{response.status_code}[/red] {response.text[:300]}")
        raise typer.Exit(1)
    return response.json()


# --------------------------------------------------------------------- hub


@hub_app.command("serve")
def hub_serve(
    host: str = typer.Option("0.0.0.0", help="Bind address. LAN/Tailscale only — never funnel."),
    port: int = typer.Option(8787),
    db: str = typer.Option("~/.cascadia-tasks/hub.db", help="SQLite path"),
    reload: bool = typer.Option(False, help="Reload on code changes (development)"),
) -> None:
    """Run the hub: /mcp for Claude Code, /api/v1 for agents."""
    import uvicorn

    os.environ["CASCADIA_TASKS_DB"] = db
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    console.print(f"[bold]cascadia-tasks hub[/bold] → http://{host}:{port}")
    console.print(f"  MCP     [cyan]http://{host}:{port}/mcp[/cyan]")
    console.print(f"  agents  [cyan]http://{host}:{port}/api/v1[/cyan]")
    console.print(f"  db      {db}")
    uvicorn.run(
        "cascadia_tasks.hub.app:app_factory",
        host=host,
        port=port,
        factory=True,
        reload=reload,
        log_level="info",
    )


@hub_app.command("health")
def hub_health(hub: str = typer.Option(None)) -> None:
    """Check that the hub is up."""
    console.print_json(data=_get("/healthz", hub))


# ------------------------------------------------------------------ agent


@agent_app.command("run")
def agent_run(
    config: str = typer.Option(..., "--config", "-c", help="Path to the agent TOML config"),
    verbose: bool = typer.Option(False, "--verbose", "-v"),
) -> None:
    """Run an agent daemon from a config file."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    cfg = AgentConfig.load(config)
    console.print(f"[bold]{cfg.name}[/bold] → {cfg.hub}  ({cfg.backend_label()})")
    try:
        asyncio.run(run_agent(cfg))
    except KeyboardInterrupt:
        console.print("stopped")


# ----------------------------------------------------------------- inspect


@app.command("agents")
def list_agents(hub: str = typer.Option(None)) -> None:
    """List registered agents."""
    data = _get("/api/v1/agents", hub)
    table = Table("name", "kind", "node", "backend", "tags", "status")
    for agent in data["agents"]:
        status = agent["status"]
        colour = {"online": "green", "busy": "yellow"}.get(status, "dim")
        table.add_row(
            agent["name"],
            agent["kind"],
            agent["node"] or "-",
            agent["backend"] or "-",
            ",".join(agent["tags"]) or "-",
            f"[{colour}]{status}[/{colour}]",
        )
    console.print(table)


@app.command("tasks")
def list_tasks(
    state: str = typer.Option(None, help="Filter by state"),
    hub: str = typer.Option(None),
) -> None:
    """List tasks."""
    params = {"state": state} if state else {}
    data = _get("/api/v1/tasks", hub, **params)
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
    """Show one task with its event history."""
    data = _get(f"/api/v1/tasks/{task_id}", hub)
    console.print_json(data=data["task"])
    table = Table("at", "kind", "payload")
    for event in data["events"]:
        table.add_row(
            f"{event['created_at']:.0f}", event["kind"], json.dumps(event["payload"])[:80]
        )
    console.print(table)


@app.command("rooms")
def list_rooms(include_archived: bool = typer.Option(False), hub: str = typer.Option(None)) -> None:
    """List rooms."""
    data = _get("/api/v1/rooms", hub, include_archived=include_archived)
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


@app.command("tail")
def tail_room(
    room_id: str,
    follow: bool = typer.Option(False, "--follow", "-f"),
    hub: str = typer.Option(None),
) -> None:
    """Print a room transcript, optionally following new messages."""
    after = 0
    while True:
        data = _get(f"/api/v1/rooms/{room_id}/messages", hub, after=after, limit=200)
        for message in data["messages"]:
            after = message["seq"]
            console.print(f"[bold cyan]{message['sender']}[/bold cyan]: {message['body']}")
        if not follow:
            return
        import time

        time.sleep(2.0)


@app.command("cancel")
def cancel_task(task_id: str, hub: str = typer.Option(None)) -> None:
    """Cancel a task."""
    data = _post(f"/api/v1/tasks/{task_id}/cancel", hub)
    console.print(f"{task_id} → [yellow]{data['task']['state']}[/yellow]")


def main() -> None:
    app()


if __name__ == "__main__":
    main()
