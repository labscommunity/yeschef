# farmteam — working agreement

## Commits (mandatory)

- **Never add a `Co-Authored-By` trailer.** Claude does not co-author commits, PRs, or
  issues in this repo. No "Generated with Claude Code" footers either.
- **Conventional Commits are mandatory** for every commit:
  `<type>(<optional scope>): <description>`
  - Types: `feat`, `fix`, `docs`, `refactor`, `test`, `chore`, `perf`, `build`, `ci`.
  - Scopes used here: `hub`, `sdk`, `agent`, `tools`, `cli`, `spec`.
  - Description in imperative mood, lower-case, no trailing period.
  - Breaking changes: `!` after the scope (`feat(hub)!: …`) plus a `BREAKING CHANGE:`
    footer.
  - Examples: `feat(hub): add round-robin floor control`,
    `fix(sdk): retry SSE reconnect on 502`, `test(agent): cover claim race`.

## Project shape

Read [SPEC.md](SPEC.md) before changing behavior — it is the contract. Three parts of
one package:

- `src/farmteam/hub/` — the hub: SQLite store, REST + SSE agent API, FastMCP tool
  surface for Claude Code. Single process, runs on the hub box.
- `src/farmteam/sdk/` — client library. **This is the protocol contract**; anything
  embedding it becomes a first-class agent. Keep it dependency-light (httpx only).
- `src/farmteam/agent/` — reference harness daemon + model backends.

## Conventions

- Python 3.11+, `async` throughout the hub and SDK. Type hints on public functions.
- Formatting and linting: `ruff format` / `ruff check` (line length 100).
- Tests: `pytest` + `pytest-asyncio`, run with `uv run pytest`. Every hub state
  transition needs a test; concurrency-sensitive paths (task claim, room floor control)
  need a race test.
- Store: SQLite in WAL mode, single writer. All timestamps are hub-assigned UTC epoch
  seconds (`float`). Agents never write times.
- Errors cross the wire as JSON `{"error": {"code": "...", "message": "..."}}` with a
  stable machine-readable `code`.
- No secrets in the repo. Tokens come from the environment or `~/.farmteam/`.

## Guardrails

- MCP tools must return in milliseconds. Any wait is explicit and capped at 60 s to stay
  clear of Claude Code's ~2-minute auto-background threshold.
- Every autonomous room must be bounded by policy (max messages, token budget, idle
  timeout). Never ship a code path that lets two agents loop unbounded.
- Worker-side tools are opt-in per agent and execute only on the worker node, never on
  the hub.
- The hub binds to the LAN/Tailscale only. Never expose it through a Tailscale funnel.
