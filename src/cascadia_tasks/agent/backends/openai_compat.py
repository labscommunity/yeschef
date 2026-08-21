"""Any OpenAI-compatible /chat/completions endpoint: vLLM, LM Studio, Ollama, Tahoma."""

from __future__ import annotations

import json
from typing import Any

import httpx

from .base import ChatResult, ToolCall, Turn


class OpenAICompatBackend:
    name = "openai_compat"

    def __init__(
        self,
        base_url: str,
        model: str,
        api_key: str | None = None,
        timeout: float = 600.0,
        extra_body: dict | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.extra_body = extra_body or {}
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
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
        messages: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for turn in turns:
            if turn.role == "assistant" and turn.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content or None,
                        "tool_calls": [
                            {
                                "id": call.id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
            else:
                messages.append({"role": turn.role, "content": turn.content})
            for result in turn.tool_results:
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": result.call_id,
                        "content": result.content,
                    }
                )

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
            **self.extra_body,
        }
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool["name"],
                        "description": tool.get("description", ""),
                        "parameters": tool.get("input_schema", {"type": "object"}),
                    },
                }
                for tool in tools
            ]

        response = await self._http.post(f"{self.base_url}/chat/completions", json=payload)
        response.raise_for_status()
        data = response.json()
        choice = data["choices"][0]
        message = choice.get("message", {})
        calls = []
        for raw in message.get("tool_calls") or []:
            fn = raw.get("function", {})
            try:
                args = json.loads(fn.get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {"_raw": fn.get("arguments")}
            calls.append(ToolCall(id=raw.get("id", fn.get("name", "call")), name=fn["name"], arguments=args))
        usage = data.get("usage") or {}
        return ChatResult(
            text=message.get("content") or "",
            tool_calls=calls,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            stop_reason=choice.get("finish_reason"),
        )
