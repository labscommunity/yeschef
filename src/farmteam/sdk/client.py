"""Async hub client. This module is the protocol contract for agents."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import httpx


class HubClientError(RuntimeError):
    def __init__(self, code: str, message: str, status: int) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message
        self.status = status


class AgentClient:
    """One agent identity's view of the hub.

    Usage:
        async with AgentClient("http://mini.local:8787", "miner-qwen") as client:
            await client.register(node="miner", backend="vllm/qwen3-8b", tags=["tier:fast"])
            async for event in client.events():
                ...
    """

    def __init__(
        self,
        hub_url: str,
        name: str,
        token: str | None = None,
        register_token: str | None = None,
        timeout: float = 30.0,
        token_path: str | Path | None = None,
    ) -> None:
        self.hub_url = hub_url.rstrip("/")
        self.name = name
        self.register_token = register_token
        # A restarting agent must be able to reclaim its own name. Its token is the
        # proof, so persist it: without this, a worker that restarts is locked out of
        # its own identity until an operator intervenes.
        self.token_path = Path(token_path).expanduser() if token_path else None
        self.token = token or self._load_token()
        self._http = httpx.AsyncClient(base_url=f"{self.hub_url}/api/v1", timeout=timeout)

    def _load_token(self) -> str | None:
        if self.token_path and self.token_path.exists():
            return self.token_path.read_text().strip() or None
        return None

    def _save_token(self, token: str) -> None:
        if not self.token_path:
            return
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.token_path.write_text(token)
        with contextlib.suppress(OSError):
            self.token_path.chmod(0o600)

    async def __aenter__(self) -> AgentClient:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------ plumbing

    def _headers(self, token: str | None = None) -> dict[str, str]:
        tok = token or self.token
        return {"Authorization": f"Bearer {tok}"} if tok else {}

    async def _request(
        self, method: str, path: str, *, token: str | None = None, **kwargs: Any
    ) -> dict:
        response = await self._http.request(method, path, headers=self._headers(token), **kwargs)
        if response.status_code >= 400:
            try:
                payload = response.json()["error"]
            except Exception:
                payload = {"code": "http_error", "message": response.text[:500]}
            raise HubClientError(payload["code"], payload["message"], response.status_code)
        return response.json()

    def _as_me(self, payload: dict) -> dict:
        return {"as_agent": self.name, **payload}

    # -------------------------------------------------------------- agents

    async def register(
        self,
        kind: str = "worker",
        node: str | None = None,
        backend: str | None = None,
        tags: list[str] | None = None,
    ) -> str:
        data = await self._request(
            "POST",
            "/agents/register",
            token=self.register_token or self.token,  # own token proves ownership
            json={
                "name": self.name,
                "kind": kind,
                "node": node,
                "backend": backend,
                "tags": tags or [],
            },
        )
        self.token = data["token"]
        self._save_token(self.token)
        return self.token

    async def heartbeat(self) -> None:
        await self._request("POST", f"/agents/{self.name}/heartbeat")

    async def list_agents(self) -> list[dict]:
        data = await self._request("GET", "/agents")
        return data["agents"]

    async def health(self) -> dict:
        response = await self._http.get("/healthz", headers=self._headers())
        response.raise_for_status()
        return response.json()

    async def events(self, reconnect: bool = True) -> AsyncIterator[dict]:
        """Stream hub events for this agent. The open stream is also the heartbeat."""
        backoff = 1.0
        while True:
            try:
                async with httpx.AsyncClient(timeout=None) as stream_client:
                    async with stream_client.stream(
                        "GET",
                        f"{self.hub_url}/api/v1/agents/{self.name}/events",
                        headers=self._headers(),
                    ) as response:
                        if response.status_code >= 400:
                            body = await response.aread()
                            raise HubClientError(
                                "stream_error", body.decode()[:200], response.status_code
                            )
                        backoff = 1.0
                        async for line in response.aiter_lines():
                            if line.startswith("data: "):
                                yield json.loads(line[6:])
            except (httpx.HTTPError, HubClientError):
                if not reconnect:
                    raise
            if not reconnect:
                return
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    # --------------------------------------------------------------- rooms

    async def create_room(
        self,
        topic: str,
        participants: list[str] | None = None,
        policy: dict | None = None,
        open_room: bool = False,
    ) -> dict:
        data = await self._request(
            "POST",
            "/rooms",
            json=self._as_me(
                {
                    "topic": topic,
                    "participants": participants or [],
                    "policy": policy or {},
                    "open": open_room,
                }
            ),
        )
        return data["room"]

    async def get_room(self, room_id: str) -> dict:
        return (await self._request("GET", f"/rooms/{room_id}"))["room"]

    async def list_rooms(self, include_archived: bool = False) -> list[dict]:
        data = await self._request(
            "GET",
            "/rooms",
            params={"as_agent": self.name, "include_archived": include_archived},
        )
        return data["rooms"]

    async def join_room(self, room_id: str) -> dict:
        return (await self._request("POST", f"/rooms/{room_id}/join", json=self._as_me({})))["room"]

    async def leave_room(self, room_id: str) -> None:
        await self._request("POST", f"/rooms/{room_id}/leave", json=self._as_me({}))

    async def archive_room(self, room_id: str, reason: str = "closed") -> dict:
        data = await self._request(
            "POST", f"/rooms/{room_id}/archive", json=self._as_me({"reason": reason})
        )
        return data["room"]

    async def post(
        self,
        room_id: str,
        body: str,
        data: dict | None = None,
        reply_to: str | None = None,
        mentions: list[str] | None = None,
        client_msg_id: str | None = None,
        tokens: int | None = None,
    ) -> dict:
        payload = self._as_me(
            {
                "body": body,
                "data": data,
                "reply_to": reply_to,
                "mentions": mentions,
                "client_msg_id": client_msg_id,
                "tokens": tokens,
            }
        )
        result = await self._request("POST", f"/rooms/{room_id}/messages", json=payload)
        return result["message"]

    async def yield_floor(self, room_id: str) -> dict:
        """Pass the turn in a round-robin room without posting."""
        data = await self._request("POST", f"/rooms/{room_id}/yield", json=self._as_me({}))
        return data["room"]

    async def messages(
        self, room_id: str, after: int = 0, limit: int = 100, tail: bool = False
    ) -> list[dict]:
        """`tail=True` returns the most recent `limit` messages, for context windows."""
        data = await self._request(
            "GET",
            f"/rooms/{room_id}/messages",
            params={"after": after, "limit": limit, "tail": tail},
        )
        return data["messages"]

    async def inbox(self, after: int = 0, limit: int = 100) -> tuple[list[dict], int]:
        data = await self._request(
            "GET", "/inbox", params={"as_agent": self.name, "after": after, "limit": limit}
        )
        return data["messages"], data["cursor"]

    # --------------------------------------------------------------- tasks

    async def submit_task(
        self,
        title: str,
        spec: str,
        assignee: str | None = None,
        selector: str | None = None,
        priority: int = 0,
        timeout_s: float = 3600.0,
        dedupe_key: str | None = None,
    ) -> dict:
        data = await self._request(
            "POST",
            "/tasks",
            json=self._as_me(
                {
                    "title": title,
                    "spec": spec,
                    "assignee": assignee,
                    "selector": selector,
                    "priority": priority,
                    "timeout_s": timeout_s,
                    "dedupe_key": dedupe_key,
                }
            ),
        )
        return data["task"]

    async def next_task(self) -> dict | None:
        data = await self._request("GET", "/tasks/next", params={"as_agent": self.name})
        return data["task"]

    async def claim(self, task_id: str) -> dict:
        return (await self._request("POST", f"/tasks/{task_id}/claim", json=self._as_me({})))[
            "task"
        ]

    async def progress(
        self, task_id: str, pct: float | None = None, message: str | None = None
    ) -> dict:
        data = await self._request(
            "POST", f"/tasks/{task_id}/progress", json=self._as_me({"pct": pct, "message": message})
        )
        return data["task"]

    async def complete(self, task_id: str, result: dict | None = None) -> dict:
        data = await self._request(
            "POST", f"/tasks/{task_id}/result", json=self._as_me({"result": result})
        )
        return data["task"]

    async def fail(self, task_id: str, error: str) -> dict:
        data = await self._request(
            "POST", f"/tasks/{task_id}/fail", json=self._as_me({"error": error})
        )
        return data["task"]

    async def request_input(self, task_id: str, question: str) -> dict:
        data = await self._request(
            "POST", f"/tasks/{task_id}/input_required", json=self._as_me({"question": question})
        )
        return data["task"]

    async def provide_input(self, task_id: str, message: str) -> dict:
        data = await self._request(
            "POST", f"/tasks/{task_id}/input", json=self._as_me({"message": message})
        )
        return data["task"]

    async def get_task(self, task_id: str) -> dict:
        return await self._request("GET", f"/tasks/{task_id}")

    async def list_tasks(self, state: str | None = None) -> list[dict]:
        params = {"state": state} if state else {}
        return (await self._request("GET", "/tasks", params=params))["tasks"]

    async def cancel_task(self, task_id: str) -> dict:
        data = await self._request("POST", f"/tasks/{task_id}/cancel", json=self._as_me({}))
        return data["task"]
