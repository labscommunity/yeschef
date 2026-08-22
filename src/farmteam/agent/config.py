"""Agent configuration: one TOML file per agent."""

from __future__ import annotations

import os
import socket
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ..models import ReplyWhen
from ..settings import env as _settings_env
from ..tools.executor import ToolsConfig

DEFAULT_SYSTEM_PROMPT = (
    "You are {name}, an agent on the farm team running on node {node}. You collaborate "
    "with Claude Code and with other local agents through a shared hub. Be direct and "
    "concrete. When you are given a task, do the work and report the result; when you are in "
    "a conversation, reply with substance and stop when the goal is met."
)


@dataclass(slots=True)
class AgentConfig:
    name: str
    hub: str = "http://localhost:8787"
    node: str = field(default_factory=socket.gethostname)
    tags: list[str] = field(default_factory=list)
    register_token: str | None = None

    backend: dict = field(default_factory=dict)
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    reply_when: ReplyWhen = ReplyWhen.MENTIONED
    max_tokens: int = 2048
    temperature: float = 0.7
    max_context_messages: int = 30
    max_tool_iterations: int = 8
    max_concurrent_tasks: int = 1
    tools: ToolsConfig = field(default_factory=ToolsConfig)

    @classmethod
    def load(cls, path: str | Path) -> AgentConfig:
        raw = tomllib.loads(Path(path).expanduser().read_text())
        return cls.from_dict(raw)

    @classmethod
    def from_dict(cls, raw: dict) -> AgentConfig:
        known = {
            "name",
            "hub",
            "node",
            "tags",
            "register_token",
            "backend",
            "persona",
            "runtime",
            "tools",
        }
        for key in raw:
            if key not in known:
                # A misplaced key (register_token under [tools], a typo) otherwise
                # fails much later with an error that never mentions it.
                print(
                    f"warning: unknown config key {key!r} ignored "
                    f"(known: {', '.join(sorted(known))})",
                    file=sys.stderr,
                )
        persona = raw.get("persona") or {}
        runtime = raw.get("runtime") or {}
        backend = dict(raw.get("backend") or {})
        if "api_key_env" in backend:
            backend["api_key"] = os.environ.get(backend.pop("api_key_env"))
        node = raw.get("node") or socket.gethostname()
        return cls(
            name=raw["name"],
            hub=raw.get("hub", "http://localhost:8787"),
            node=node,
            tags=list(raw.get("tags") or []),
            register_token=raw.get("register_token") or _settings_env("REGISTER_TOKEN"),
            backend=backend,
            system_prompt=persona.get("system_prompt") or DEFAULT_SYSTEM_PROMPT,
            reply_when=ReplyWhen(persona.get("reply_when", ReplyWhen.MENTIONED)),
            max_tokens=int(runtime.get("max_tokens", 2048)),
            temperature=float(runtime.get("temperature", 0.7)),
            max_context_messages=int(runtime.get("max_context_messages", 30)),
            max_tool_iterations=int(runtime.get("max_tool_iterations", 8)),
            max_concurrent_tasks=int(runtime.get("max_concurrent_tasks", 1)),
            tools=ToolsConfig.from_dict(raw.get("tools")),
        )

    def rendered_system_prompt(self) -> str:
        return self.system_prompt.format(name=self.name, node=self.node)

    def backend_label(self) -> str:
        """Prefer the detected runtime name (ollama, vllm) — it is what a human means."""
        kind = self.backend.get("runtime") or self.backend.get("type", "openai_compat")
        return f"{kind}/{self.backend.get('model', 'unknown')}"
