# Launch checklist

The distribution steps, in dependency order. Research behind the priorities: for
MCP/Claude-ecosystem tools, one strong r/ClaudeAI post + an X thread + registry
placement drove the big repos; Show HN is the day-2 amplifier, not the engine.

## 0. Hard prerequisites (everything below needs these)

- [ ] **Make the repo public**: `gh repo edit labscommunity/yeschef --visibility public --accept-visibility-change-consequences`
- [ ] **Publish to PyPI** as `yeschef` (`uv build && uv publish`) so `uv tool install
      yeschef` and `/yeschef:setup` work as documented. (The name was
      collision-checked clean.)
- [ ] Tag `v0.1.0` matching `pyproject.toml`, `.claude-plugin/plugin.json`, and
      `CHANGELOG.md` (a parity test enforces the last two).

## 1. Claude Code plugin directory (official)

- [ ] `claude plugin validate .` passes (it does — keep it that way).
- [ ] README documents each component the plugin ships (commands, skill, agent, MCP) —
      the setup section covers this; reviewers reject thin docs.
- [ ] Submit at **clau.de/plugin-directory-submission**. Human review takes days —
      submit early in launch week. Accepted plugins land in
      `anthropics/claude-plugins-official` under `external_plugins/`.
- [ ] Optional wider net: PR to the community marketplace
      `anthropics/claude-plugins-community` (automated safety screening).
- [ ] Until accepted, users can install directly from the repo (already wired):
      `/plugin marketplace add labscommunity/yeschef` → `/plugin install yeschef@yeschef`.

## 2. MCP ecosystem listings

yeschef's MCP server is self-hosted HTTP (it lives inside the user's hub), so
registries that expect an installable stdio package are a mismatch. Do these:

- [ ] **awesome-mcp-servers** (punkpeye) PR — name, link, one-liner, correct category,
      alphabetical order. Adding 🤖🤖🤖 to the PR title opts into the agent-authored
      fast-track.
- [ ] **Glama** — claim the auto-crawled listing once public.
- [ ] **mcp.so** — submit via its form/issue.
- [ ] Skip the official MCP Registry for now (it models packaged/hosted servers, not a
      self-hosted hub); revisit if a stdio-proxy mode ever ships.

## 3. Announcement wave (per the research brief)

- [ ] Day 1: one workflow-story post on r/ClaudeAI + founder X thread with the
      claude-demo GIF, same morning. Answer everything for 12 hours.
- [ ] Day 2–3: Show HN at 12:00–17:00 UTC. Title carries a falsifiable, audit-proof
      number and the local-underdog frame. First comment: backstory + limitations +
      the security posture, pre-empting the standard attacks.
- [ ] Day 4–7: technical deep-dive post; convert objections into docs; <24h issue
      responses.

## Submission-reviewer facts (copy-paste answers)

- License: MIT. No telemetry. No network calls except to the user's own hub and model
  servers; `web_fetch` tool refuses internal addresses.
- The plugin's MCP entry points at `http://localhost:8787/mcp` (the default `yeschef
  up` hub); `/yeschef:setup` re-wires user scope for custom hosts.
- Auth: on by default (per-agent bearer tokens, gated registration, scoped reads);
  `--open` exists for trusted LANs and warns loudly.
