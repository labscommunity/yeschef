# cascadia-tasks

Communication and task-orchestration fabric between Claude Code and local AI agents on
the Rainier fleet.

- **Tasks** — dispatch long-running work from Claude Code to a local agent and check its
  status at any time, from any session, days later. `submit_task` returns a task id
  immediately; `task_status(id)` answers whenever you ask.
- **Rooms** — N-party multi-turn conversation. Claude Code ↔ local agent, local agent ↔
  local agent, or any mix. Ergonomics mirror Claude Code's own cross-session messaging
  (`list_agents` / `send_message` / `fetch_messages`).

Claude Code stays on the Anthropic API. Local agents are separate processes running
against Ollama, vLLM, Tahoma, or any OpenAI-compatible endpoint.

```
   Claude Code ──MCP/HTTP──▶ ┌─────────┐ ◀──REST+SSE── harness ──▶ Ollama  (nuc-alpha)
   (any session,             │   hub   │ ◀──REST+SSE── harness ──▶ vLLM    (miner)
    any machine)             │ SQLite  │ ◀──REST+SSE── harness ──▶ Tahoma  (sharded)
                             └─────────┘
```

## Quickstart

```bash
uv venv && uv pip install -e ".[dev]"

# 1. Hub on the orchestrator (LAN/Tailscale only — never behind a funnel)
uv run cascadia-tasks hub serve --host 0.0.0.0 --port 8787

# 2. An agent on a worker node
uv run cascadia-tasks agent run -c examples/agents/nuc-alpha.toml

# 3. Point Claude Code at the hub, from any machine on the LAN
claude mcp add --transport http cascadia-tasks http://mini.local:8787/mcp
```

Then, in Claude Code:

```
> list_agents                                    # who is on the fleet
> submit_task("summarize today's hub log", assignee="nuc-alpha")
  → task_hx8k2m9qp4                              # returns immediately
> task_status("task_hx8k2m9qp4")                 # ask any time, any session
> send_message("nuc-alpha", "what did you find?")  # multi-turn, back and forth
> start_dialogue(["nuc-alpha", "miner-reasoner"], goal="pick a caching strategy")
  → room_a7c2k9                                  # agents converse on their own
> room_transcript("room_a7c2k9")                 # watch it happen
```

## The two primitives

**Tasks** carry a state machine that mirrors the MCP Tasks extension vocabulary, so the
tool surface can upgrade to spec-native tasks when Claude Code supports them:

```
queued → claimed → working → completed | failed | cancelled
                 ↘ input_required ↗
```

An agent that hits a genuine fork parks the task in `input_required` and asks its
question in the task's room; `provide_input` answers and work resumes. Tasks are durable
in SQLite: the hub can restart, the session can end, the status is still there.

**Rooms** are N-party conversations. Direct messages are just two-party rooms. Every
room can carry policy that the hub enforces, so no two agents can talk forever:

| guard | effect |
|---|---|
| `max_messages` | archives the room at N messages |
| `max_total_tokens` | archives it at a token budget |
| `idle_timeout_s` | archives it after silence |
| `stop_phrase` | archives it when an agent says the phrase |
| `turn_policy: round_robin` | hub grants the floor in ring order; out-of-turn posts are rejected |

Claude Code (and you) never wait for the floor — a post from a Claude session interjects
at any point and re-anchors the ring, which is how you steer a running dialogue.

## MCP tools

| | |
|---|---|
| `list_agents` `whoami` `set_identity` | who is on the fleet, and who this session is |
| `submit_task` `task_status` `task_result` `list_tasks` `cancel_task` | dispatch and check |
| `provide_input` `task_room` | turn a running task into a conversation |
| `send_message` `fetch_messages` | direct multi-turn with one agent |
| `create_room` `post` `room_transcript` `join_room` `leave_room` `list_rooms` `archive_room` | group conversation |
| `start_dialogue` | agents converse autonomously toward a goal, bounded |

Every tool returns in milliseconds. The only wait is the explicit `wait_s` on
`fetch_messages`, capped at 60 s — comfortably under Claude Code's ~2 minute MCP
auto-background threshold.

## Agents

An agent is a TOML file. See [`examples/agents/`](examples/agents/) for Ollama, vLLM,
and Tahoma configs.

```toml
name = "nuc-alpha"
hub = "http://mini.local:8787"
tags = ["tier:fast", "ctx:32k"]

[backend]
type = "anthropic_compat"          # Ollama v0.14+ serves the Anthropic Messages API
base_url = "http://localhost:11434"
model = "qwen3:8b"

[persona]
system_prompt = "You are nuc-alpha, a fast local agent…"
reply_when = "mentioned"           # mentioned | round_robin | always

[tools]                            # omit entirely for chat-only (the default)
allow = ["shell", "file_read"]
shell_allowlist = ["rg *", "python3 *"]
file_root = "~/agent-scratch"
```

Dispatch by name (`assignee="nuc-alpha"`) or by capability (`selector="tier:fast"`), in
which case every matching agent is offered the task and exactly one claim wins.

Tools are opt-in per agent, allowlisted by command pattern, jailed to one directory, and
executed on the worker node — never on the hub.

**Embedding instead of running the harness:** anything that uses
`cascadia_tasks.sdk.AgentClient` is a first-class agent. That is the seam for making
Tahoma itself join rooms and claim tasks.

## Operating it

```bash
cascadia-tasks agents               # fleet status
cascadia-tasks tasks --state working
cascadia-tasks task task_hx8k2m9qp4 # detail plus event history
cascadia-tasks rooms
cascadia-tasks tail room_a7c2k9 -f  # follow a conversation
cascadia-tasks cancel task_hx8k2m9qp4
```

A background sweep reclaims tasks whose agent disappeared (requeue, then fail after two
attempts), times out overruns, and archives idle rooms.

Deployment units for launchd (hub) and systemd (workers) are in
[`examples/deploy/`](examples/deploy/).

## Security posture

LAN and Tailscale only. Agents authenticate with a bearer token minted at registration;
set `CASCADIA_TASKS_REGISTER_TOKEN` to gate registration and
`CASCADIA_TASKS_ADMIN_TOKEN` to gate the MCP face and admin routes. There is no TLS
termination here — Tailscale provides transport encryption. Anything that reaches the
hub can dispatch work to every node on the fleet, so never expose it publicly.

## Development

```bash
uv run pytest              # 79 tests
uv run ruff check src tests
uv run ruff format src tests
```

[SPEC.md](SPEC.md) is the contract; [CLAUDE.md](CLAUDE.md) is the working agreement
(conventional commits, no AI co-authors).
