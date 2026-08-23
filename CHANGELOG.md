# Changelog

## 0.1.0 — 2026-08-21

Initial release.

- Hub (SQLite + REST/SSE + MCP over HTTP), Python SDK, and worker harness for
  dispatching tasks to local models (Ollama, vLLM, LM Studio, any OpenAI- or
  Anthropic-compatible endpoint) and holding bounded multi-agent conversations.
- Two-command onboarding (`yeschef up` / `yeschef join`), auto-detection of local
  model servers, background process management, `yeschef doctor`.
- Buildout support: per-task workspaces, produced files returned through the hub
  (`task_files` / `task_file`), and a `cli` backend that runs a real coding-agent CLI
  as the worker's engine.
- Claude Code plugin: `/yeschef:setup` install command, the yeschef skill, the
  `yeschef-expediter` subagent (dispatched tasks appear in the subagent panel and
  return on their own), and MCP wiring.
- Codex support: `yeschef mcp-proxy` bridges the hub's tools to Codex over stdio,
  `yeschef up` auto-wires Codex's config.toml, and a `codex exec` worker example.
- Cloud-provider workers: `yeschef join --provider openrouter|openai|groq|together|
  deepseek|fireworks` (or `--base-url` + `--api-key-env` for any endpoint); keys read
  from the environment. Mix cloud and local workers on one hub.
- Security defaults: per-agent bearer tokens, registration gating, scoped reads,
  bounded rooms, jailed and allowlisted worker tools.
