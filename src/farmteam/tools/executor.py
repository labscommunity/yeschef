"""Worker-side tool execution.

Off by default. An agent gets only the tools its config names, shell commands must match
an explicit allowlist, and file access is jailed to one root. Everything runs on the
worker node; the hub never executes anything.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import ipaddress
import os
import shlex
import socket
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

import httpx

from ..models import ToolCall, ToolResult

MAX_OUTPUT_CHARS = 20_000

_POSIX_METACHARACTERS = (";", "&&", "||", "|", "`", "$(", ">", "<", "\n", "&")
# cmd.exe adds its own chaining and expansion characters; `create_subprocess_shell`
# uses cmd on Windows, so a worker there must screen for these too.
_WINDOWS_METACHARACTERS = _POSIX_METACHARACTERS + ("%", "^", "\r")

SHELL_METACHARACTERS = _WINDOWS_METACHARACTERS if os.name == "nt" else _POSIX_METACHARACTERS
"""Anything that could chain or redirect a second command past the allowlist."""

MAX_REDIRECTS = 3


def _reject_internal_url(url: str) -> str | None:
    """Return a reason to refuse this URL, or None if it looks externally routable."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        return "url must be http(s)"
    if not parsed.hostname:
        return "url has no host"
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or 0, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        return f"could not resolve host: {exc}"
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        if (
            address.is_private
            or address.is_loopback
            or address.is_link_local
            or address.is_reserved
            or address.is_multicast
            or address.is_unspecified
        ):
            return (
                f"refusing to fetch an internal address ({address}); web_fetch reaches "
                "the public internet only"
            )
    return None


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
        # `shell` enforces its own deadline so it can kill the child; give the generic
        # backstop extra room rather than racing it.
        budget = self.config.timeout_s + (5.0 if call.name == "shell" else 0.0)
        try:
            return await asyncio.wait_for(handler(call), timeout=budget)
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
        try:
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=self.config.timeout_s)
        except TimeoutError:
            # Cancelling the wait leaves the process running; kill it or the node
            # accumulates an orphan per timed-out call.
            proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            return self._error(call, f"command timed out after {self.config.timeout_s}s (killed)")
        text = stdout.decode(errors="replace")[:MAX_OUTPUT_CHARS]
        return ToolResult(
            call_id=call.id,
            name=call.name,
            content=f"exit={proc.returncode}\n{text}",
            is_error=proc.returncode != 0,
        )

    def _shell_allowed(self, command: str) -> bool:
        """Match against the allowlist without letting a wildcard smuggle in a second command.

        A pattern like `rg *` is meant to allow ripgrep, not `rg x; rm -rf /`, and plain
        fnmatch matches both. So a command carrying shell metacharacters is only accepted
        by a pattern that carries one itself — checked **per matching pattern**, because a
        single deliberate pipeline elsewhere in the list must not re-open chaining for
        every other entry.
        """
        if not self.config.shell_allowlist:
            return False
        try:
            # Windows paths use backslashes that POSIX-mode shlex mangles; validate
            # parseability in the mode that matches the shell we'll actually invoke.
            shlex.split(command, posix=(os.name != "nt"))
        except ValueError:
            return False

        command_chains = any(token in command for token in SHELL_METACHARACTERS)
        for pattern in self.config.shell_allowlist:
            if not fnmatch.fnmatch(command, pattern):
                continue
            if command_chains and not any(token in pattern for token in SHELL_METACHARACTERS):
                continue  # this pattern authorized one command, not a chain
            return True
        return False

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
        """Fetch a public URL.

        Redirects are followed by hand so every hop is re-checked: otherwise a public URL
        could bounce the worker into the hub's own API, a node's model server, or a cloud
        metadata endpoint.
        """
        url = str(call.arguments.get("url", ""))
        async with httpx.AsyncClient(timeout=30.0, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                problem = _reject_internal_url(url)
                if problem:
                    return self._error(call, problem)
                response = await client.get(url)
                if response.is_redirect and response.headers.get("location"):
                    url = str(response.next_request.url) if response.next_request else ""
                    continue
                response.raise_for_status()
                body = b""
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) >= self.config.max_fetch_bytes:
                        break
                text = body[: self.config.max_fetch_bytes].decode(errors="replace")
                return ToolResult(call.id, call.name, text[:MAX_OUTPUT_CHARS])
        return self._error(call, "too many redirects")

    def _error(self, call: ToolCall, message: str) -> ToolResult:
        return ToolResult(call.id, call.name, f"error: {message}", is_error=True)
