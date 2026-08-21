"""Model backends for the harness."""

from __future__ import annotations

from .anthropic_compat import AnthropicCompatBackend
from .base import Backend, ChatResult, ToolCall, ToolResult, Turn
from .openai_compat import OpenAICompatBackend


def build_backend(config: dict) -> Backend:
    """Construct a backend from the `[backend]` block of an agent config.

    `tahoma` is an OpenAI-compatible preset — Tahoma serves /v1/chat/completions, so it
    shares the adapter and only differs in defaults.
    """
    kind = (config.get("type") or "openai_compat").lower()
    common = {
        "base_url": config["base_url"],
        "model": config["model"],
        "api_key": config.get("api_key"),
        "timeout": float(config.get("timeout_s", 600.0)),
        "extra_body": config.get("extra_body") or {},
    }
    if kind == "anthropic_compat":
        return AnthropicCompatBackend(**common)
    if kind in ("openai_compat", "tahoma"):
        backend = OpenAICompatBackend(**common)
        if kind == "tahoma":
            backend.name = "tahoma"
        return backend
    raise ValueError(f"unknown backend type: {kind}")


__all__ = [
    "AnthropicCompatBackend",
    "Backend",
    "ChatResult",
    "OpenAICompatBackend",
    "ToolCall",
    "ToolResult",
    "Turn",
    "build_backend",
]
