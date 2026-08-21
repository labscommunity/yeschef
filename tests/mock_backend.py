"""A scripted stand-in for a local model, so integration tests need no GPU."""

from __future__ import annotations

from collections.abc import Callable

from cascadia_tasks.agent.backends.base import ChatResult, ToolCall, Turn


class MockBackend:
    """Replies by running `responder` over the conversation so far.

    `responder` receives (system, turns) and returns either a string or a ChatResult,
    which lets a test drive tool calls as well as plain replies.
    """

    name = "mock"

    def __init__(
        self,
        responder: Callable[[str, list[Turn]], str | ChatResult],
        model: str = "mock-model",
    ) -> None:
        self.responder = responder
        self.model = model
        self.calls: list[list[Turn]] = []
        self.closed = False

    async def chat(
        self,
        system: str,
        turns: list[Turn],
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ChatResult:
        self.calls.append(list(turns))
        reply = self.responder(system, turns)
        if isinstance(reply, ChatResult):
            return reply
        return ChatResult(text=reply, input_tokens=10, output_tokens=5)

    async def close(self) -> None:
        self.closed = True


def echo_responder(prefix: str) -> Callable[[str, list[Turn]], str]:
    def respond(system: str, turns: list[Turn]) -> str:
        last = turns[-1].content if turns else ""
        return f"{prefix}: {last[-80:]}"

    return respond


def tool_then_answer(tool_name: str, arguments: dict, final: str):
    """First call asks for a tool, second call answers."""
    state = {"used": False}

    def respond(system: str, turns: list[Turn]) -> str | ChatResult:
        if not state["used"]:
            state["used"] = True
            return ChatResult(
                text="", tool_calls=[ToolCall(id="call_1", name=tool_name, arguments=arguments)]
            )
        return final

    return respond
