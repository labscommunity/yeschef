# Changelog

## 0.1.0 — 2026-08-21

Initial release.

- Hub (SQLite + REST/SSE + MCP over HTTP), Python SDK, and worker harness for
  dispatching tasks to local models (Ollama, vLLM, LM Studio, any OpenAI- or
  Anthropic-compatible endpoint) and holding bounded multi-agent conversations.
- Two-command onboarding (`farmteam up` / `farmteam join`), auto-detection of local
  model servers, background process management, `farmteam doctor`.
- Buildout support: per-task workspaces, produced files returned through the hub
  (`task_files` / `task_file`), and a `cli` backend that runs a real coding-agent CLI
  as the worker's engine.
- Claude Code plugin: `/farmteam:setup` install command, the farmteam skill, the
  `farmteam-watcher` subagent (dispatched tasks appear in the subagent panel and
  return on their own), and MCP wiring.
- Codex support: `farmteam mcp-proxy` bridges the hub's tools to Codex over stdio,
  `farmteam up` auto-wires Codex's config.toml, and a `codex exec` worker example.
- Security defaults: per-agent bearer tokens, registration gating, scoped reads,
  bounded rooms, jailed and allowlisted worker tools.
