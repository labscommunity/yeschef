# cascadia-tasks

Communication and task-orchestration fabric between Claude Code and local AI agents on
the Rainier fleet.

- **Tasks:** dispatch long-running work from Claude Code to local agents
  (Ollama / Tahoma / vLLM backed), with durable status checkable from any session.
- **Rooms:** N-party multi-turn conversations — Claude Code ↔ local agent and local
  agent ↔ local agent — with ergonomics modeled on Claude Code's cross-session
  messaging (`ListAgents` / `SendMessage`).

One Python package, three parts: a **hub** (FastMCP + SQLite, runs on the Mac Mini), a
client **SDK** (the protocol contract), and a reference **harness** daemon that turns
any local model endpoint into a named agent.

See [SPEC.md](SPEC.md) for the v1 design. Status: spec under review, pre-implementation.
