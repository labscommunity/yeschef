"""Backend adapters: request shape sent to the model server, and response parsing."""

from __future__ import annotations

import json

import httpx
import pytest

from yeschef.agent.backends import build_backend
from yeschef.agent.backends.anthropic_compat import AnthropicCompatBackend
from yeschef.agent.backends.base import ToolCall, ToolResult, Turn
from yeschef.agent.backends.openai_compat import OpenAICompatBackend

TOOL_SPECS = [
    {
        "name": "file_read",
        "description": "Read a file",
        "input_schema": {"type": "object", "properties": {"path": {"type": "string"}}},
    }
]


def stub(backend, payload: dict, capture: dict) -> None:
    """Point a backend's http client at an in-memory responder."""

    def handler(request: httpx.Request) -> httpx.Response:
        capture["url"] = str(request.url)
        capture["body"] = json.loads(request.content)
        capture["headers"] = dict(request.headers)
        return httpx.Response(200, json=payload)

    backend._http = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), headers=backend._http.headers
    )


# ------------------------------------------------------------- anthropic


async def test_anthropic_sends_system_and_messages_and_parses_text() -> None:
    backend = AnthropicCompatBackend("http://ollama:11434", "qwen3:8b")
    capture: dict = {}
    stub(
        backend,
        {
            "content": [{"type": "text", "text": "the answer"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 3},
        },
        capture,
    )

    result = await backend.chat("you are alpha", [Turn(role="user", content="question?")])

    assert capture["url"] == "http://ollama:11434/v1/messages"
    assert capture["body"]["system"] == "you are alpha"
    assert capture["body"]["messages"] == [{"role": "user", "content": "question?"}]
    assert result.text == "the answer"
    assert result.input_tokens == 11
    assert result.output_tokens == 3
    assert result.total_tokens == 14
    await backend.close()


async def test_anthropic_parses_tool_use_blocks() -> None:
    backend = AnthropicCompatBackend("http://ollama:11434", "qwen3:8b")
    capture: dict = {}
    stub(
        backend,
        {
            "content": [
                {"type": "text", "text": "let me look"},
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "file_read",
                    "input": {"path": "notes.txt"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 2},
        },
        capture,
    )

    result = await backend.chat("sys", [Turn(role="user", content="read it")], tools=TOOL_SPECS)

    assert capture["body"]["tools"][0]["name"] == "file_read"
    assert result.text == "let me look"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "file_read"
    assert result.tool_calls[0].arguments == {"path": "notes.txt"}
    await backend.close()


async def test_anthropic_round_trips_tool_results() -> None:
    """A tool result must go back as a user-role tool_result block."""
    backend = AnthropicCompatBackend("http://ollama:11434", "qwen3:8b")
    capture: dict = {}
    stub(backend, {"content": [{"type": "text", "text": "done"}], "usage": {}}, capture)

    turns = [
        Turn(role="user", content="read it"),
        Turn(
            role="assistant",
            content="looking",
            tool_calls=[ToolCall(id="toolu_1", name="file_read", arguments={"path": "n.txt"})],
        ),
        Turn(
            role="user",
            content="",
            tool_results=[ToolResult(call_id="toolu_1", name="file_read", content="42")],
        ),
    ]
    await backend.chat("sys", turns, tools=TOOL_SPECS)

    messages = capture["body"]["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["content"][1]["type"] == "tool_use"
    assert assistant["content"][1]["id"] == "toolu_1"

    tool_result = messages[-1]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "toolu_1"
    assert tool_result["content"] == "42"
    await backend.close()


async def test_anthropic_passes_extra_body_through() -> None:
    """Context-length overrides for Ollama ride along in extra_body."""
    backend = AnthropicCompatBackend(
        "http://ollama:11434", "qwen3:8b", extra_body={"options": {"num_ctx": 32768}}
    )
    capture: dict = {}
    stub(backend, {"content": [{"type": "text", "text": "ok"}], "usage": {}}, capture)
    await backend.chat("sys", [Turn(role="user", content="hi")])
    assert capture["body"]["options"] == {"num_ctx": 32768}
    await backend.close()


# ---------------------------------------------------------------- openai


async def test_openai_prepends_system_and_parses_choice() -> None:
    backend = OpenAICompatBackend("http://vllm:8000/v1", "qwen3-8b")
    capture: dict = {}
    stub(
        backend,
        {
            "choices": [{"message": {"content": "the answer"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 9, "completion_tokens": 4},
        },
        capture,
    )

    result = await backend.chat("you are alpha", [Turn(role="user", content="question?")])

    assert capture["url"] == "http://vllm:8000/v1/chat/completions"
    assert capture["body"]["messages"][0] == {"role": "system", "content": "you are alpha"}
    assert capture["body"]["messages"][1] == {"role": "user", "content": "question?"}
    assert result.text == "the answer"
    assert result.total_tokens == 13
    assert result.stop_reason == "stop"
    await backend.close()


async def test_openai_parses_tool_calls_with_json_arguments() -> None:
    backend = OpenAICompatBackend("http://vllm:8000/v1", "qwen3-8b")
    capture: dict = {}
    stub(
        backend,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {
                                    "name": "file_read",
                                    "arguments": '{"path": "notes.txt"}',
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {},
        },
        capture,
    )

    result = await backend.chat("sys", [Turn(role="user", content="read")], tools=TOOL_SPECS)

    assert capture["body"]["tools"][0]["function"]["name"] == "file_read"
    assert result.text == ""
    assert result.tool_calls[0].arguments == {"path": "notes.txt"}
    await backend.close()


async def test_openai_survives_malformed_tool_arguments() -> None:
    """A model that emits broken JSON must not crash the harness."""
    backend = OpenAICompatBackend("http://vllm:8000/v1", "qwen3-8b")
    capture: dict = {}
    stub(
        backend,
        {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "function": {"name": "file_read", "arguments": "{not json"},
                            }
                        ],
                    }
                }
            ],
            "usage": {},
        },
        capture,
    )

    result = await backend.chat("sys", [Turn(role="user", content="read")], tools=TOOL_SPECS)
    assert result.tool_calls[0].arguments == {"_raw": "{not json"}
    await backend.close()


async def test_openai_emits_tool_role_messages_for_results() -> None:
    backend = OpenAICompatBackend("http://vllm:8000/v1", "qwen3-8b")
    capture: dict = {}
    stub(backend, {"choices": [{"message": {"content": "done"}}], "usage": {}}, capture)

    turns = [
        Turn(role="user", content="read"),
        Turn(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="call_1", name="file_read", arguments={"path": "n.txt"})],
        ),
        Turn(
            role="user",
            content="",
            tool_results=[ToolResult(call_id="call_1", name="file_read", content="42")],
        ),
    ]
    await backend.chat("sys", turns, tools=TOOL_SPECS)

    messages = capture["body"]["messages"]
    assistant = next(m for m in messages if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["function"]["arguments"] == '{"path": "n.txt"}'
    assert messages[-1] == {"role": "tool", "tool_call_id": "call_1", "content": "42"}
    await backend.close()


async def test_api_key_is_sent_in_the_right_header() -> None:
    openai = OpenAICompatBackend("http://x/v1", "m", api_key="sk-test")
    assert openai._http.headers["authorization"] == "Bearer sk-test"
    await openai.close()

    anthropic = AnthropicCompatBackend("http://x", "m", api_key="sk-test")
    assert anthropic._http.headers["x-api-key"] == "sk-test"
    assert anthropic._http.headers["anthropic-version"] == "2023-06-01"
    await anthropic.close()


# ----------------------------------------------------------------- factory


async def test_build_backend_selects_the_adapter() -> None:
    anthropic = build_backend(
        {"type": "anthropic_compat", "base_url": "http://ollama:11434", "model": "qwen3:8b"}
    )
    assert isinstance(anthropic, AnthropicCompatBackend)
    await anthropic.close()

    openai = build_backend(
        {"type": "openai_compat", "base_url": "http://vllm:8000/v1", "model": "q"}
    )
    assert isinstance(openai, OpenAICompatBackend)
    await openai.close()

    # Tahoma serves an OpenAI-compatible API; it is the same adapter, relabelled.
    tahoma = build_backend({"type": "tahoma", "base_url": "http://mini:8080/v1", "model": "q"})
    assert isinstance(tahoma, OpenAICompatBackend)
    assert tahoma.name == "tahoma"
    await tahoma.close()


async def test_build_backend_rejects_an_unknown_type() -> None:
    with pytest.raises(ValueError, match="unknown backend type"):
        build_backend({"type": "telepathy", "base_url": "x", "model": "y"})


async def test_extra_headers_reach_the_provider() -> None:
    """OpenRouter-style attribution headers (and any custom header) must be sent."""
    backend = OpenAICompatBackend(
        "https://openrouter.ai/api/v1",
        "meta-llama/llama-3.3-70b-instruct",
        api_key="sk-or-test",
        extra_headers={"HTTP-Referer": "https://example.com", "X-Title": "yeschef"},
    )
    assert backend._http.headers["authorization"] == "Bearer sk-or-test"
    assert backend._http.headers["http-referer"] == "https://example.com"
    assert backend._http.headers["x-title"] == "yeschef"
    await backend.close()


async def test_build_backend_threads_extra_headers() -> None:
    backend = build_backend(
        {
            "type": "openai_compat",
            "base_url": "https://openrouter.ai/api/v1",
            "model": "m",
            "api_key": "k",
            "extra_headers": {"X-Title": "yeschef"},
        }
    )
    assert backend._http.headers["x-title"] == "yeschef"
    await backend.close()
