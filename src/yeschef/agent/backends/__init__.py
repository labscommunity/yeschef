"""Model backends for the harness."""

from __future__ import annotations

from . import apple_fm
from .anthropic_compat import AnthropicCompatBackend
from .apple_fm import AppleFMBackend
from .base import Backend, ChatResult, ToolCall, ToolResult, Turn
from .cli import CliBackend
from .openai_compat import OpenAICompatBackend


def build_backend(config: dict) -> Backend:
    """Construct a backend from the `[backend]` block of an agent config.

    `cascadia` and `exo` are OpenAI-compatible presets — each serves
    /v1/chat/completions, so they ride the same adapter and only relabel the roster.
    `tahoma` is kept as a back-compat alias for `cascadia` (the project's former name).
    `apple_fm` is Apple's on-device model via `fm serve`; base_url and model default.
    """
    kind = (config.get("type") or "openai_compat").lower()
    if kind == "cli":
        return CliBackend(
            command=list(config.get("command") or []),
            model=config.get("model", "cli-agent"),
            timeout=float(config.get("timeout_s", 1800.0)),
            env=dict(config.get("env") or {}),
        )
    if kind == "apple_fm":
        return AppleFMBackend(
            base_url=config.get("base_url") or apple_fm.DEFAULT_BASE_URL,
            model=config.get("model") or apple_fm.ON_DEVICE_MODEL,
            api_key=config.get("api_key"),
            timeout=float(config.get("timeout_s", 600.0)),
            extra_body=config.get("extra_body") or {},
            extra_headers=config.get("extra_headers") or {},
        )
    common = {
        "base_url": config["base_url"],
        "model": config["model"],
        "api_key": config.get("api_key"),
        "timeout": float(config.get("timeout_s", 600.0)),
        "extra_body": config.get("extra_body") or {},
        "extra_headers": config.get("extra_headers") or {},
    }
    if kind == "anthropic_compat":
        return AnthropicCompatBackend(**common)
    if kind in ("openai_compat", "cascadia", "tahoma", "exo"):
        backend = OpenAICompatBackend(**common)
        # Named presets over the same adapter — they only relabel the roster "stove".
        if kind in ("cascadia", "tahoma"):
            backend.name = "cascadia"
        elif kind == "exo":
            backend.name = "exo"
        return backend
    raise ValueError(f"unknown backend type: {kind}")


__all__ = [
    "AnthropicCompatBackend",
    "AppleFMBackend",
    "Backend",
    "CliBackend",
    "ChatResult",
    "OpenAICompatBackend",
    "ToolCall",
    "ToolResult",
    "Turn",
    "build_backend",
]
