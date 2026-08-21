"""Run a real hub on a loopback port.

httpx's ASGITransport buffers response bodies, so anything that depends on incremental
delivery (SSE) has to talk to an actual server.
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import uvicorn

from farmteam.hub import HubConfig, Store, create_app
from farmteam.hub.events import EventBus


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@dataclass(slots=True)
class LiveHub:
    url: str
    store: Store


@contextlib.asynccontextmanager
async def live_hub(with_mcp: bool = False, **config_kwargs) -> AsyncIterator[LiveHub]:
    store = Store(":memory:", EventBus())
    config = HubConfig(db_path=":memory:", sweep_interval_s=3600.0, **config_kwargs)
    mcp_app = None
    if with_mcp:
        from farmteam.hub.mcp_server import build_mcp

        mcp_app = build_mcp(store, config).http_app(path="/")
    app = create_app(store, config, mcp_app=mcp_app)

    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    )
    serving = asyncio.create_task(server.serve())
    url = f"http://127.0.0.1:{port}"

    async with httpx.AsyncClient(timeout=5.0) as probe:
        for _ in range(100):
            if server.started:
                with contextlib.suppress(httpx.HTTPError):
                    response = await probe.get(f"{url}/healthz")
                    if response.status_code == 200:
                        break
            await asyncio.sleep(0.05)
        else:  # pragma: no cover - only on a broken environment
            raise RuntimeError("hub did not start")

    try:
        yield LiveHub(url=url, store=store)
    finally:
        server.should_exit = True
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(serving, timeout=10.0)
        store.close()
