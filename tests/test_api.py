"""Agent-facing HTTP API: auth, task lifecycle over the wire, and SSE delivery."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator

import httpx
import pytest
from asgi_lifespan import LifespanManager

from farmteam.hub import HubConfig, Store, create_app
from farmteam.hub.events import EventBus
from farmteam.models import AgentKind, TaskState
from farmteam.sdk import AgentClient

from .live import live_hub


@pytest.fixture
async def hub() -> AsyncIterator[tuple[httpx.AsyncClient, Store]]:
    store = Store(":memory:", EventBus())
    app = create_app(store, HubConfig(db_path=":memory:", sweep_interval_s=3600.0))
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as client:
            yield client, store
    store.close()


async def _register(client: httpx.AsyncClient, name: str, tags: list[str] | None = None) -> str:
    response = await client.post(
        "/api/v1/agents/register", json={"name": name, "tags": tags or [], "node": "test"}
    )
    assert response.status_code == 200
    return response.json()["token"]


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_healthz(hub) -> None:
    client, _ = hub
    response = await client.get("/healthz")
    assert response.status_code == 200
    assert response.json()["ok"] is True


async def test_register_returns_a_usable_token(hub) -> None:
    client, store = hub
    token = await _register(client, "alpha")
    assert store.verify_token("alpha", token)
    assert not store.verify_token("alpha", "wrong-token")


async def test_calls_without_a_token_are_rejected(hub) -> None:
    client, _ = hub
    await _register(client, "alpha")
    response = await client.post("/api/v1/agents/alpha/heartbeat")
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "unauthorized"


async def test_one_agent_cannot_act_as_another(hub) -> None:
    client, _ = hub
    alpha_token = await _register(client, "alpha")
    await _register(client, "beta")
    response = await client.post("/api/v1/agents/beta/heartbeat", headers=_auth(alpha_token))
    assert response.status_code == 401


async def test_task_round_trip_over_http(hub) -> None:
    client, store = hub
    token = await _register(client, "alpha", tags=["tier:fast"])
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    claude_token = await _register(client, "claude:main")

    submitted = await client.post(
        "/api/v1/tasks",
        headers=_auth(claude_token),
        json={
            "as_agent": "claude:main",
            "title": "count",
            "spec": "count to three",
            "selector": "tier:fast",
        },
    )
    task_id = submitted.json()["task"]["id"]
    assert submitted.json()["task"]["state"] == "queued"

    available = await client.get(
        "/api/v1/tasks/next", headers=_auth(token), params={"as_agent": "alpha"}
    )
    assert available.json()["task"]["id"] == task_id

    claimed = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_auth(token), json={"as_agent": "alpha"}
    )
    assert claimed.json()["task"]["state"] == "claimed"

    await client.post(
        f"/api/v1/tasks/{task_id}/progress",
        headers=_auth(token),
        json={"as_agent": "alpha", "pct": 50.0, "message": "halfway"},
    )
    await client.post(
        f"/api/v1/tasks/{task_id}/result",
        headers=_auth(token),
        json={"as_agent": "alpha", "result": {"text": "1 2 3"}},
    )

    # Status is durable and readable by anyone, from any session, at any time.
    status = await client.get(f"/api/v1/tasks/{task_id}")
    assert status.json()["task"]["state"] == "completed"
    assert status.json()["task"]["result"] == {"text": "1 2 3"}
    assert [event["kind"] for event in status.json()["events"]][-1] == "completed"


async def test_second_claim_is_a_conflict_over_http(hub) -> None:
    client, store = hub
    alpha = await _register(client, "alpha", tags=["t"])
    beta = await _register(client, "beta", tags=["t"])
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    claude = await _register(client, "claude:main")

    submitted = await client.post(
        "/api/v1/tasks",
        headers=_auth(claude),
        json={"as_agent": "claude:main", "title": "t", "spec": "s", "selector": "t"},
    )
    task_id = submitted.json()["task"]["id"]

    first = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_auth(alpha), json={"as_agent": "alpha"}
    )
    second = await client.post(
        f"/api/v1/tasks/{task_id}/claim", headers=_auth(beta), json={"as_agent": "beta"}
    )
    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "conflict"


async def test_missing_task_is_a_404(hub) -> None:
    client, _ = hub
    response = await client.get("/api/v1/tasks/task_nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


async def test_room_messaging_over_http(hub) -> None:
    client, store = hub
    alpha = await _register(client, "alpha")
    store.ensure_identity("claude:main", AgentKind.CLAUDE)
    claude = await _register(client, "claude:main")

    created = await client.post(
        "/api/v1/rooms",
        headers=_auth(claude),
        json={"as_agent": "claude:main", "topic": "chat", "participants": ["alpha"]},
    )
    room_id = created.json()["room"]["id"]

    await client.post(
        f"/api/v1/rooms/{room_id}/messages",
        headers=_auth(claude),
        json={"as_agent": "claude:main", "body": "hello @alpha"},
    )
    await client.post(
        f"/api/v1/rooms/{room_id}/messages",
        headers=_auth(alpha),
        json={"as_agent": "alpha", "body": "hello back"},
    )

    transcript = await client.get(f"/api/v1/rooms/{room_id}/messages")
    assert [m["body"] for m in transcript.json()["messages"]] == ["hello @alpha", "hello back"]

    inbox = await client.get(
        "/api/v1/inbox", headers=_auth(claude), params={"as_agent": "claude:main"}
    )
    assert [m["body"] for m in inbox.json()["messages"]] == ["hello back"]


async def test_sse_stream_delivers_task_and_message_events() -> None:
    """The event stream is how an idle agent learns about work and conversation."""
    async with live_hub() as hub:
        async with AgentClient(hub.url, "alpha") as agent:
            await agent.register(tags=["tier:fast"])
            hub.store.ensure_identity("claude:main", AgentKind.CLAUDE)

            received: asyncio.Queue = asyncio.Queue()

            async def reader() -> None:
                async for event in agent.events(reconnect=False):
                    await received.put(event)

            pump = asyncio.create_task(reader())
            try:
                first = await asyncio.wait_for(received.get(), timeout=10.0)
                assert first["kind"] == "connected"

                hub.store.submit_task("stream me", "spec", "claude:main", selector="tier:fast")
                assigned = await asyncio.wait_for(received.get(), timeout=10.0)
                assert assigned["kind"] == "task_assigned"
                assert assigned["task"]["title"] == "stream me"

                room = hub.store.create_room("chat", "claude:main", ["alpha"])
                invite = await asyncio.wait_for(received.get(), timeout=10.0)
                assert invite["kind"] == "room_invite"

                hub.store.post_message(room.id, "claude:main", "ping @alpha")
                message = await asyncio.wait_for(received.get(), timeout=10.0)
                assert message["kind"] == "message"
                assert message["message"]["body"] == "ping @alpha"
                assert message["message"]["mentions"] == ["alpha"]
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump


async def test_open_event_stream_counts_as_a_heartbeat() -> None:
    """An agent holding a stream must not be reaped as lost."""
    async with live_hub() as hub:
        async with AgentClient(hub.url, "alpha") as agent:
            await agent.register()
            hub.store.ensure_identity("claude:main", AgentKind.CLAUDE)
            task = hub.store.submit_task("t", "spec", "claude:main", assignee="alpha")

            received: asyncio.Queue = asyncio.Queue()

            async def reader() -> None:
                async for event in agent.events(reconnect=False):
                    await received.put(event)

            pump = asyncio.create_task(reader())
            try:
                await asyncio.wait_for(received.get(), timeout=10.0)
                hub.store.claim_task(task.id, "alpha")
                assert hub.store.sweep()["reclaimed"] == 0
                assert hub.store.require_task(task.id).state is TaskState.CLAIMED
            finally:
                pump.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await pump


async def test_admin_token_gates_registration_when_configured() -> None:
    store = Store(":memory:", EventBus())
    app = create_app(
        store,
        HubConfig(db_path=":memory:", register_token="s3cret", sweep_interval_s=3600.0),
    )
    async with LifespanManager(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://hub") as client:
            denied = await client.post("/api/v1/agents/register", json={"name": "alpha"})
            assert denied.status_code == 401

            allowed = await client.post(
                "/api/v1/agents/register",
                headers=_auth("s3cret"),
                json={"name": "alpha"},
            )
            assert allowed.status_code == 200
    store.close()
