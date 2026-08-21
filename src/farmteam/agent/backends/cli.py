"""Run a real coding-agent CLI as the worker's engine.

The chat backends give a worker one model call plus a bounded tool loop — fine for
drafts and triage, not for a buildout. This backend hands the whole task to an agent
CLI (``claude -p``, opencode, aider …) running in the task's workspace, where it brings
its own tool loop, its own depth, and its own judgment.

The flagship configuration points ``claude -p`` at a local model server, so the full
Claude Code harness runs against your own hardware:

    [backend]
    type = "cli"
    command = ["claude", "-p", "{prompt}", "--dangerously-skip-permissions"]
    [backend.env]
    ANTHROPIC_BASE_URL = "http://localhost:11434"   # Ollama v0.14+ speaks Anthropic
"""

from __future__ import annotations

import asyncio
import os

from .base import ChatResult, Turn

DEFAULT_TIMEOUT_S = 1800.0
MAX_OUTPUT_CHARS = 60_000
PROMPT_PLACEHOLDER = "{prompt}"


class CliBackend:
    name = "cli"
    uses_workspace = True

    def __init__(
        self,
        command: list[str],
        model: str = "cli-agent",
        timeout: float = DEFAULT_TIMEOUT_S,
        env: dict[str, str] | None = None,
        base_url: str | None = None,  # accepted for config symmetry; unused
        api_key: str | None = None,
        extra_body: dict | None = None,
    ) -> None:
        if not command:
            raise ValueError("cli backend needs a command")
        if not any(PROMPT_PLACEHOLDER in part for part in command):
            raise ValueError(f"cli backend command must contain {PROMPT_PLACEHOLDER}")
        self.command = list(command)
        self.model = model
        self.timeout = timeout
        self.env = env or {}
        # Set per task by the harness before each call; None runs in the process cwd.
        self.workspace: str | None = None

    async def close(self) -> None:
        return None

    async def chat(
        self,
        system: str,
        turns: list[Turn],
        tools: list[dict] | None = None,
        max_tokens: int = 2048,
        temperature: float = 0.7,
    ) -> ChatResult:
        del tools, max_tokens, temperature  # the CLI agent brings its own
        prompt = _flatten(system, turns)
        argv = [part.replace(PROMPT_PLACEHOLDER, prompt) for part in self.command]

        process = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self.workspace,
            env={**os.environ, **self.env},
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=self.timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            raise RuntimeError(f"cli agent timed out after {self.timeout:.0f}s (killed)") from None

        out = stdout.decode(errors="replace").strip()
        if process.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise RuntimeError(f"cli agent exited {process.returncode}: {(err or out)[:800]}")
        return ChatResult(text=out[-MAX_OUTPUT_CHARS:], stop_reason="cli_exit")


def _flatten(system: str, turns: list[Turn]) -> str:
    """One prompt for a one-shot CLI run: instructions first, then the conversation."""
    parts = [system.strip()] if system else []
    for turn in turns:
        content = (turn.content or "").strip()
        if not content:
            continue
        parts.append(content if turn.role == "user" else f"(your earlier reply) {content}")
    return "\n\n".join(parts)
