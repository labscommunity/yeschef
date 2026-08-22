"""Codex support: the stdio MCP proxy and idempotent config wiring."""

from __future__ import annotations

import asyncio

from fastmcp import Client

from farmteam.cli import codex_config_with_farmteam
from farmteam.hub import HubConfig
from farmteam.hub.mcp_server import build_mcp

from .live import live_hub


def test_codex_config_appends_the_server_block() -> None:
    updated = codex_config_with_farmteam('model = "gpt-5"\n', "http://mini:8787")
    assert "[mcp_servers.farmteam]" in updated
    assert 'command = "farmteam"' in updated
    assert '"mcp-proxy", "--hub", "http://mini:8787"' in updated
    assert updated.startswith('model = "gpt-5"\n')  # preserves existing content


def test_codex_config_is_idempotent() -> None:
    once = codex_config_with_farmteam("", "http://h:8787")
    assert codex_config_with_farmteam(once, "http://h:8787") is None


def test_codex_config_normalizes_a_missing_trailing_newline() -> None:
    # No newline before the appended block, or the TOML table merges into the last line.
    updated = codex_config_with_farmteam("model = 'x'", "http://h:8787")
    assert "\n[mcp_servers.farmteam]" in updated


async def test_the_stdio_proxy_exposes_every_hub_tool() -> None:
    """A stdio client (Codex's transport) sees the same tools an HTTP client does."""
    async with live_hub(with_mcp=True) as hub:
        # Tools the hub actually serves, straight from the source of truth.
        direct = build_mcp(hub.store, HubConfig(db_path=":memory:"))
        async with Client(direct) as c:
            expected = {t.name for t in await c.list_tools()}

        # The same set, reached over stdio through the proxy subprocess.
        proxy_client = Client(
            {
                "mcpServers": {
                    "farmteam": {
                        "command": ".venv/bin/farmteam",
                        "args": ["mcp-proxy", "--hub", hub.url],
                    }
                }
            }
        )
        async with asyncio.timeout(30):
            async with proxy_client as c:
                got = {t.name for t in await c.list_tools()}
                assert {"submit_task", "task_files", "wait_task"} <= got
                assert got == expected
