"""Agent-facing REST + SSE API.

Authentication: agents present the bearer token minted at registration. Admin token (if
configured) unlocks registration and the cross-identity `/watch` stream.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, FastAPI, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ..models import (
    DEFAULT_TASK_TIMEOUT_S,
    MAX_LONG_POLL_S,
    AgentKind,
    ErrorCode,
    HubError,
    RoomPolicy,
    TaskState,
)
from .store import Store

SSE_KEEPALIVE_S = 15.0

# Must live at module scope: `from __future__ import annotations` turns every annotation
# into a string, and FastAPI resolves those against module globals only.
AuthHeader = Annotated[str | None, Header(alias="Authorization")]


@dataclass(slots=True)
class HubConfig:
    db_path: str = "~/.cascadia-tasks/hub.db"
    admin_token: str | None = None
    register_token: str | None = None
    sweep_interval_s: float = 10.0
    default_identity: str = "claude:local"
    cors_origins: list[str] = field(default_factory=list)


def _bearer(header: str | None) -> str | None:
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip()


class Auth:
    """Resolves the calling identity for agent-facing routes."""

    def __init__(self, store: Store, config: HubConfig) -> None:
        self.store = store
        self.config = config

    def admin(self, header: str | None) -> None:
        if self.config.admin_token is None:
            return
        if _bearer(header) != self.config.admin_token:
            raise HubError(ErrorCode.UNAUTHORIZED, "admin token required", 401)

    def agent(self, name: str, header: str | None) -> str:
        token = _bearer(header)
        if token is None:
            raise HubError(ErrorCode.UNAUTHORIZED, "bearer token required", 401)
        if self.config.admin_token is not None and token == self.config.admin_token:
            return name
        if not self.store.verify_token(name, token):
            raise HubError(ErrorCode.UNAUTHORIZED, "invalid token for agent", 401)
        self.store.heartbeat(name)
        return name


def build_router(store: Store, config: HubConfig) -> APIRouter:
    router = APIRouter(prefix="/api/v1")
    auth = Auth(store, config)

    def as_agent(name: str, authorization: AuthHeader = None) -> str:
        return auth.agent(name, authorization)

    # ------------------------------------------------------------- agents

    @router.post("/agents/register")
    def register(payload: dict = Body(...), authorization: AuthHeader = None) -> dict:
        if config.register_token is not None:
            accepted = {t for t in (config.register_token, config.admin_token) if t}
            if _bearer(authorization) not in accepted:
                raise HubError(ErrorCode.UNAUTHORIZED, "registration token required", 401)
        agent, token = store.register_agent(
            name=payload["name"],
            kind=AgentKind(payload.get("kind", AgentKind.WORKER)),
            node=payload.get("node"),
            backend=payload.get("backend"),
            tags=payload.get("tags") or [],
        )
        return {"agent": agent.to_dict(), "token": token}

    @router.post("/agents/{name}/heartbeat")
    def heartbeat(name: str, agent: str = Depends(as_agent)) -> dict:
        return {"ok": True, "agent": name}

    @router.get("/agents")
    def list_agents(kind: str | None = None) -> dict:
        agents = store.list_agents(AgentKind(kind) if kind else None)
        return {"agents": [a.to_dict() for a in agents]}

    @router.get("/agents/{name}/events")
    async def agent_events(
        name: str, request: Request, agent: str = Depends(as_agent)
    ) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(store, name, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    # -------------------------------------------------------------- rooms

    @router.post("/rooms")
    def create_room(payload: dict = Body(...), authorization: AuthHeader = None) -> dict:
        creator = payload["as_agent"]
        auth.agent(creator, authorization)
        room = store.create_room(
            topic=payload.get("topic", "room"),
            created_by=creator,
            participants=payload.get("participants") or [],
            policy=RoomPolicy.from_dict(payload.get("policy")),
            open_room=bool(payload.get("open")),
        )
        return {"room": room.to_dict()}

    @router.get("/rooms")
    def list_rooms(
        as_agent: str | None = Query(None),
        include_archived: bool = False,
        authorization: AuthHeader = None,
    ) -> dict:
        """Rooms for one agent, or every room when called with the admin token."""
        if as_agent:
            auth.agent(as_agent, authorization)
        else:
            auth.admin(authorization)
        rooms = store.list_rooms(agent=as_agent, include_archived=include_archived)
        return {"rooms": [r.to_dict() for r in rooms]}

    @router.get("/rooms/{room_id}")
    def get_room(room_id: str) -> dict:
        return {"room": store.require_room(room_id).to_dict()}

    @router.post("/rooms/{room_id}/join")
    def join_room(
        room_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        return {"room": store.join_room(room_id, who).to_dict()}

    @router.post("/rooms/{room_id}/leave")
    def leave_room(
        room_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        store.leave_room(room_id, who)
        return {"ok": True}

    @router.post("/rooms/{room_id}/archive")
    def archive_room(
        room_id: str, payload: dict = Body(default={}), authorization: AuthHeader = None
    ) -> dict:
        who = payload.get("as_agent")
        if who:
            auth.agent(who, authorization)
        else:
            auth.admin(authorization)
        reason = payload.get("reason") or f"archived by {who or 'admin'}"
        return {"room": store.archive_room(room_id, reason).to_dict()}

    @router.post("/rooms/{room_id}/messages")
    def post_message(
        room_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        sender = payload["as_agent"]
        auth.agent(sender, authorization)
        message = store.post_message(
            room_id=room_id,
            sender=sender,
            body=payload["body"],
            data=payload.get("data"),
            reply_to=payload.get("reply_to"),
            mentions=payload.get("mentions"),
            client_msg_id=payload.get("client_msg_id"),
            tokens=payload.get("tokens"),
        )
        return {"message": message.to_dict()}

    @router.get("/rooms/{room_id}/messages")
    def get_messages(room_id: str, after: int = 0, limit: int = 100) -> dict:
        store.require_room(room_id)
        messages = store.fetch_messages(room_id, after_seq=after, limit=min(limit, 500))
        return {"messages": [m.to_dict() for m in messages]}

    @router.get("/inbox")
    def inbox(
        as_agent: str = Query(...),
        after: int = 0,
        limit: int = 100,
        room_id: str | None = None,
        authorization: AuthHeader = None,
    ) -> dict:
        auth.agent(as_agent, authorization)
        messages, cursor = store.fetch_inbox(as_agent, after, min(limit, 500), room_id)
        return {"messages": messages, "cursor": cursor}

    # -------------------------------------------------------------- tasks

    @router.post("/tasks")
    def submit_task(payload: dict = Body(...), authorization: AuthHeader = None) -> dict:
        creator = payload["as_agent"]
        auth.agent(creator, authorization)
        task = store.submit_task(
            title=payload["title"],
            spec=payload["spec"],
            created_by=creator,
            assignee=payload.get("assignee"),
            selector=payload.get("selector"),
            priority=int(payload.get("priority", 0)),
            timeout_s=float(payload.get("timeout_s", DEFAULT_TASK_TIMEOUT_S)),
            dedupe_key=payload.get("dedupe_key"),
        )
        return {"task": task.to_dict()}

    @router.get("/tasks")
    def list_tasks(
        state: str | None = None,
        assignee: str | None = None,
        created_by: str | None = None,
        limit: int = 100,
    ) -> dict:
        tasks = store.list_tasks(
            state=TaskState(state) if state else None,
            assignee=assignee,
            created_by=created_by,
            limit=limit,
        )
        return {"tasks": [t.to_dict() for t in tasks]}

    @router.get("/tasks/next")
    def next_task(as_agent: str = Query(...), authorization: AuthHeader = None) -> dict:
        auth.agent(as_agent, authorization)
        task = store.next_task_for(as_agent)
        return {"task": task.to_dict() if task else None}

    @router.get("/tasks/{task_id}")
    def get_task(task_id: str) -> dict:
        task = store.require_task(task_id)
        return {"task": task.to_dict(), "events": [e.to_dict() for e in store.task_events(task_id)]}

    @router.post("/tasks/{task_id}/claim")
    def claim_task(
        task_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        return {"task": store.claim_task(task_id, who).to_dict()}

    @router.post("/tasks/{task_id}/progress")
    def task_progress(
        task_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        task = store.update_progress(task_id, who, payload.get("pct"), payload.get("message"))
        return {"task": task.to_dict()}

    @router.post("/tasks/{task_id}/result")
    def task_result(
        task_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        return {"task": store.complete_task(task_id, who, payload.get("result")).to_dict()}

    @router.post("/tasks/{task_id}/fail")
    def task_fail(
        task_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        return {"task": store.fail_task(task_id, who, payload.get("error", "unknown")).to_dict()}

    @router.post("/tasks/{task_id}/input_required")
    def task_input_required(
        task_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        return {"task": store.request_input(task_id, who, payload["question"]).to_dict()}

    @router.post("/tasks/{task_id}/input")
    def task_input(
        task_id: str, payload: dict = Body(...), authorization: AuthHeader = None
    ) -> dict:
        who = payload["as_agent"]
        auth.agent(who, authorization)
        return {"task": store.provide_input(task_id, who, payload["message"]).to_dict()}

    @router.post("/tasks/{task_id}/cancel")
    def task_cancel(
        task_id: str, payload: dict = Body(default={}), authorization: AuthHeader = None
    ) -> dict:
        who = payload.get("as_agent")
        if who:
            auth.agent(who, authorization)
        else:
            auth.admin(authorization)
        return {"task": store.cancel_task(task_id, who or "admin").to_dict()}

    # -------------------------------------------------------------- watch

    @router.get("/watch")
    async def watch(
        request: Request, identity: str = Query(...), authorization: AuthHeader = None
    ) -> StreamingResponse:
        auth.admin(authorization)
        store.ensure_identity(identity)
        return StreamingResponse(
            _event_stream(store, identity, request),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router


async def _event_stream(store: Store, agent: str, request: Request) -> AsyncIterator[str]:
    """SSE stream. The open connection doubles as the agent's heartbeat."""
    async with store.bus.subscribe(agent) as queue:
        store.heartbeat(agent)
        yield _sse({"kind": "connected", "agent": agent})
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.wait_for(queue.get(), timeout=SSE_KEEPALIVE_S)
            except TimeoutError:
                store.heartbeat(agent)
                yield ": keepalive\n\n"
                continue
            store.heartbeat(agent)
            yield _sse(event)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def create_app(store: Store, config: HubConfig, mcp_app: Any | None = None) -> FastAPI:
    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        store.bus.bind(asyncio.get_running_loop())
        sweeper = asyncio.create_task(_sweep_loop(store, config.sweep_interval_s))
        try:
            if mcp_app is not None and hasattr(mcp_app, "lifespan"):
                async with mcp_app.lifespan(app):
                    yield
            else:
                yield
        finally:
            sweeper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await sweeper

    app = FastAPI(title="cascadia-tasks hub", version="0.1.0", lifespan=lifespan)

    @app.exception_handler(HubError)
    async def hub_error_handler(request: Request, exc: HubError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content=exc.to_dict())

    @app.get("/healthz")
    def healthz() -> dict:
        agents = store.list_agents()
        return {
            "ok": True,
            "agents": len(agents),
            "online": sum(1 for a in agents if str(a.status()) == "online"),
            "queued_tasks": len(store.list_tasks(state=TaskState.QUEUED)),
        }

    app.include_router(build_router(store, config))
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
    return app


async def _sweep_loop(store: Store, interval: float) -> None:
    while True:
        await asyncio.sleep(interval)
        with contextlib.suppress(Exception):
            await asyncio.to_thread(store.sweep)


__all__ = ["HubConfig", "create_app", "build_router", "MAX_LONG_POLL_S"]
