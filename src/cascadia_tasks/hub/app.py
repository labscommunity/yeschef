"""Assemble the hub: one ASGI app serving /mcp for Claude Code and /api/v1 for agents."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi import FastAPI

from .api import HubConfig, create_app
from .events import EventBus
from .store import Store


def config_from_env() -> HubConfig:
    return HubConfig(
        db_path=os.environ.get("CASCADIA_TASKS_DB", "~/.cascadia-tasks/hub.db"),
        admin_token=os.environ.get("CASCADIA_TASKS_ADMIN_TOKEN"),
        register_token=os.environ.get("CASCADIA_TASKS_REGISTER_TOKEN"),
        default_identity=os.environ.get("CASCADIA_TASKS_IDENTITY", "claude:local"),
    )


def build_hub(config: HubConfig | None = None) -> tuple[FastAPI, Store]:
    config = config or config_from_env()
    db_path = (
        config.db_path
        if config.db_path == ":memory:"
        else str(Path(config.db_path).expanduser())
    )
    store = Store(db_path, EventBus())

    from .mcp_server import build_mcp

    mcp = build_mcp(store, config)
    mcp_app = mcp.http_app(path="/")
    app = create_app(store, config, mcp_app=mcp_app)
    return app, store


def app_factory() -> FastAPI:
    """Entry point for `uvicorn cascadia_tasks.hub.app:app_factory --factory`."""
    app, _ = build_hub()
    return app
