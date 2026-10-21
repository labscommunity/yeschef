"""Apple's on-device Foundation Model, served by the `fm` CLI (macOS 26+).

`fm serve` exposes the model as an OpenAI-compatible /v1/chat/completions on
127.0.0.1:1976, so this is the same adapter with three local quirks pinned down:

- `fm serve` streams unless told otherwise, so every request sends `stream: false`.
- The on-device model has a 4,096-token context shared by prompt and reply; replies are
  capped so a long prompt still has room. Overflows come back as a 500 whose message
  says so, and that message is surfaced instead of a bare status code.
- Only the `system` model runs on this machine. `pcc` (Private Cloud Compute) and custom
  `--model-provider` models leave it, so this preset refuses them.

    [backend]
    type = "apple_fm"   # base_url and model default to the local `fm serve`
"""

from __future__ import annotations

import httpx

from .base import ChatResult, Turn
from .openai_compat import OpenAICompatBackend

DEFAULT_BASE_URL = "http://127.0.0.1:1976/v1"
ON_DEVICE_MODEL = "system"
CONTEXT_TOKENS = 4096
MAX_REPLY_TOKENS = 1024


class AppleFMBackend(OpenAICompatBackend):
    name = "apple_fm"

    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        model: str = ON_DEVICE_MODEL,
        api_key: str | None = None,
        timeout: float = 600.0,
        extra_body: dict | None = None,
        extra_headers: dict | None = None,
    ) -> None:
        # extra_body is merged after `model` in the payload, so it could swap the model too.
        for requested in (model, (extra_body or {}).get("model", ON_DEVICE_MODEL)):
            if requested != ON_DEVICE_MODEL:
                raise ValueError(
                    f"apple_fm runs the on-device model only (model = {ON_DEVICE_MODEL!r}); "
                    f"{requested!r} would leave this machine — use openai_compat for that"
                )
        super().__init__(
            base_url,
            model,
            api_key=api_key,
            timeout=timeout,
            extra_body={**(extra_body or {}), "stream": False},
            extra_headers=extra_headers,
        )

    async def chat(
        self,
        system: str,
        turns: list[Turn],
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ChatResult:
        try:
            return await super().chat(
                system,
                turns,
                tools=tools,
                max_tokens=min(max_tokens, MAX_REPLY_TOKENS),
                temperature=temperature,
            )
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"fm serve: {_error_message(exc.response)}") from exc


def _error_message(response: httpx.Response) -> str:
    """`fm serve` explains failures (context overflow, guardrails) in an OpenAI error body."""
    try:
        error = response.json().get("error") or {}
        message = error.get("message")
    except (ValueError, AttributeError):
        message = None
    return message or f"HTTP {response.status_code}"
