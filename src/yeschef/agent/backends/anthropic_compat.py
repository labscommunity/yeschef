"""Anthropic Messages API shape — Ollama v0.14+ serves this natively at /v1/messages.

Ollama defaults to a 4,096-token context; raise it (num_ctx / OLLAMA_CONTEXT_LENGTH) or
long task prompts are silently truncated.
"""

from __future__ import annotations

from typing import Any

import httpx

from .base import ChatResult, ToolCall, Turn


class AnthropicCompatBackend:
    name = "anthropic_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 600.0,
        extra_body: dict | None = None,
        extra_headers: dict | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = extra_body or {}
        headers = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if api_key:
            headers["x-api-key"] = api_key
        headers.update(extra_headers or {})
        self._http = httpx.AsyncClient(headers=headers, timeout=timeout)

    async def close(self) -> None:
        await self._http.aclose()

    async def chat(
        self,
        system: str,
        turns: list[Turn],
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ChatResult:
        messages: list[dict[str, Any]] = []
        for turn in turns:
            if turn.role == "assistant" and turn.tool_calls:
                content: list[dict] = []
                if turn.content:
                    content.append({"type": "text", "text": turn.content})
                content.extend(
                    {
                        "type": "tool_use",
                        "id": call.id,
                        "name": call.name,
                        "input": call.arguments,
                    }
                    for call in turn.tool_calls
                )
                messages.append({"role": "assistant", "content": content})
            else:
                messages.append({"role": turn.role, "content": turn.content})
            if turn.tool_results:
                messages.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": result.call_id,
                                "content": result.content,
                                "is_error": result.is_error,
                            }
                            for result in turn.tool_results
                        ],
                    }
                )

        payload: dict[str, Any] = {
            "model": self.model,
            "system": system,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **self.extra_body,
        }
        if tools:
            payload["tools"] = [
                {
                    "name": tool["name"],
                    "description": tool.get("description", ""),
                    "input_schema": tool.get("input_schema", {"type": "object"}),
                }
                for tool in tools
            ]

        response = await self._http.post(f"{self.base_url}/v1/messages", json=payload)
        response.raise_for_status()
        data = response.json()

        text_parts, calls = [], []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                calls.append(
                    ToolCall(id=block["id"], name=block["name"], arguments=block.get("input") or {})
                )
        usage = data.get("usage") or {}
        return ChatResult(
            text="".join(text_parts),
            tool_calls=calls,
            input_tokens=usage.get("input_tokens", 0),
            output_tokens=usage.get("output_tokens", 0),
            stop_reason=data.get("stop_reason"),
        )
