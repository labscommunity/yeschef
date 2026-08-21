"""Backend protocol: one normalized chat call over any local model server."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(slots=True)
class ToolResult:
    call_id: str
    name: str
    content: str
    is_error: bool = False


@dataclass(slots=True)
class ChatResult:
    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(slots=True)
class Turn:
    """One entry of conversation history in backend-neutral form."""

    role: str  # "user" | "assistant"
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_results: list[ToolResult] = field(default_factory=list)


class Backend(Protocol):
    name: str
    model: str

    async def chat(
        self,
        system: str,
        turns: list[Turn],
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ChatResult: ...

    async def close(self) -> None: ...
