"""Worker-side tool execution.

Off by default. An agent gets only the tools its config names, shell commands must match
an explicit allowlist, and file access is jailed to one root. Everything runs on the
worker node; the hub never executes anything.
"""

from __future__ import annotations

import asyncio
import fnmatch
import shlex
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from ..models import ToolCall, ToolResult

MAX_OUTPUT_CHARS = 20_000

SHELL_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n", "&")
"""Anything that could chain or redirect a second command past the allowlist."""


@dataclass(slots=True)
class ToolsConfig:
    allow: list[str] = field(default_factory=list)
    shell_allowlist: list[str] = field(default_factory=list)
    file_root: str | None = None
    timeout_s: float = 60.0
    max_fetch_bytes: int = 2_000_000

    @classmethod
    def from_dict(cls, raw: dict | None) -> ToolsConfig:
        raw = raw or {}
        return cls(
            allow=list(raw.get("allow") or []),
            shell_allowlist=list(raw.get("shell_allowlist") or []),
            file_root=raw.get("file_root"),
            timeout_s=float(raw.get("timeout_s", 60.0)),
            max_fetch_bytes=int(raw.get("max_fetch_bytes", 2_000_000)),
        )


SPECS: dict[str, dict] = {
    "shell": {
        "name": "shell",
        "description": "Run an allowlisted shell command on this node and return its output.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "Command to run"}},
            "required": ["command"],
        },
    },
    "file_read": {
        "name": "file_read",
        "description": "Read a UTF-8 text file from this node's agent workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    "file_write": {
        "name": "file_write",
        "description": "Write a UTF-8 text file into this node's agent workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
            "required": ["path", "content"],
        },
    },
    "file_list": {
        "name": "file_list",
        "description": "List files under a directory in this node's agent workspace.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "default": "."}},
        },
    },
    "web_fetch": {
        "name": "web_fetch",
        "description": "Fetch a URL and return its body as text.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
}


class ToolExecutor:
    def __init__(self, config: ToolsConfig) -> None:
        self.config = config
        self.root = Path(config.file_root).expanduser().resolve() if config.file_root else None

    @property
    def enabled(self) -> bool:
        return bool(self.config.allow)

    def specs(self) -> list[dict]:
        """Tool schemas for the model, limited to what this agent may use."""
        return [SPECS[name] for name in self.config.allow if name in SPECS]

    async def run(self, call: ToolCall) -> ToolResult:
        if call.name not in self.config.allow:
            return self._error(call, f"tool '{call.name}' is not enabled for this agent")
        handler = {
            "shell": self._shell,
            "file_read": self._file_read,
            "file_write": self._file_write,
            "file_list": self._file_list,
            "web_fetch": self._web_fetch,
        }.get(call.name)
        if handler is None:
            return self._error(call, f"unknown tool '{call.name}'")
        try:
            return await asyncio.wait_for(handler(call), timeout=self.config.timeout_s)
        except TimeoutError:
            return self._error(call, f"tool timed out after {self.config.timeout_s}s")
        except Exception as exc:  # noqa: BLE001 - surfaced to the model, not swallowed
            return self._error(call, f"{type(exc).__name__}: {exc}")

    # ------------------------------------------------------------- handlers

    async def _shell(self, call: ToolCall) -> ToolResult:
        command = str(call.arguments.get("command", "")).strip()
        if not command:
            return self._error(call, "command is required")
        if not self._shell_allowed(command):
            return self._error(
                call,
                f"command not allowed. Allowlist: {self.config.shell_allowlist or '(empty)'}",
            )
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(self.root) if self.root else None,
        )
        stdout, _ = await proc.communicate()
        text = stdout.decode(errors="replace")[:MAX_OUTPUT_CHARS]
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=f"exit={proc.returncode}\n{text}",
            is_error=proc.returncode != 0,
        )

    def _shell_allowed(self, command: str) -> bool:
        """Match against the allowlist, but never let a wildcard smuggle in a second command.

        A pattern like `rg *` is meant to allow ripgrep, not `rg x; rm -rf /` — and plain
        fnmatch would happily match both. Commands carrying shell metacharacters are
        therefore refused unless the operator put a metacharacter in a pattern themselves.
        """
        if not self.config.shell_allowlist:
            return False
        if any(token in command for token in SHELL_METACHARACTERS):
            deliberate = any(
                token in pattern
                for pattern in self.config.shell_allowlist
                for token in SHELL_METACHARACTERS
            )
            if not deliberate:
                return False
        try:
            shlex.split(command)
        except ValueError:
            return False
        return any(fnmatch.fnmatch(command, pattern) for pattern in self.config.shell_allowlist)

    def _resolve(self, raw: str) -> Path:
        if self.root is None:
            raise ValueError("file tools need [tools] file_root to be set")
        target = (self.root / raw).resolve() if not Path(raw).is_absolute() else Path(raw).resolve()
        if target != self.root and self.root not in target.parents:
            raise ValueError(f"path escapes the agent workspace ({self.root})")
        return target

    async def _file_read(self, call: ToolCall) -> ToolResult:
        target = self._resolve(str(call.arguments.get("path", "")))
        text = await asyncio.to_thread(target.read_text, "utf-8")
        return ToolResult(call.id, call.name, text[:MAX_OUTPUT_CHARS])

    async def _file_write(self, call: ToolCall) -> ToolResult:
        target = self._resolve(str(call.arguments.get("path", "")))
        content = str(call.arguments.get("content", ""))

        def _write() -> None:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, "utf-8")

        await asyncio.to_thread(_write)
        return ToolResult(call.id, call.name, f"wrote {len(content)} bytes to {target}")

    async def _file_list(self, call: ToolCall) -> ToolResult:
        target = self._resolve(str(call.arguments.get("path", ".")))
        entries = sorted(
            f"{'d' if p.is_dir() else '-'} {p.relative_to(self.root)}" for p in target.iterdir()
        )
        return ToolResult(call.id, call.name, "\n".join(entries)[:MAX_OUTPUT_CHARS] or "(empty)")

    async def _web_fetch(self, call: ToolCall) -> ToolResult:
        url = str(call.arguments.get("url", ""))
        if not url.startswith(("http://", "https://")):
            return self._error(call, "url must be http(s)")
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.text[: self.config.max_fetch_bytes]
        return ToolResult(call.id, call.name, body[:MAX_OUTPUT_CHARS])

    def _error(self, call: ToolCall, message: str) -> ToolResult:
        return ToolResult(call.id, call.name, f"error: {message}", is_error=True)
