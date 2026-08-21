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

## Quickstart — two commands

```bash
uv tool install cascadia-tasks          # or: uv pip install -e ".[dev]"
```

**On the hub machine** (where you run Claude Code) — one command generates tokens,
wires Claude Code, and prints the worker command:

```bash
cascadia-tasks up
```

**On each worker node** — one command auto-detects the local model, verifies it, and
registers. Paste the line `up` printed:

```bash
cascadia-tasks join --hub http://mini.local:8787 --token <printed-by-up>
```

That's it. `up` already connected Claude Code, so a session can immediately
`list_agents`, `submit_task`, and `start_dialogue`. Single-box demo? Run both in two
terminals — on the same machine `join` needs no flags at all.

Prefer the background? `cascadia-tasks up --detach` and `join --detach` return to the
prompt; `cascadia-tasks down` stops them, `ps` lists them. Runs the same on macOS,
Linux, and Windows (see [`examples/deploy/`](examples/deploy/) for persistent services).

**Check it's healthy anytime:**

```bash
cascadia-tasks doctor        # config, hub reachability, detected model servers
```

### Drive it yourself, from the terminal

```bash
cascadia-tasks agents                              # who is on the fleet
cascadia-tasks submit "summarize today's log" --to nuc-alpha
cascadia-tasks task <task-id>                       # status + result, anytime
cascadia-tasks ask nuc-alpha "what did you find?"   # send and wait for the reply
cascadia-tasks dialogue nuc-alpha miner-reasoner --goal "pick a caching strategy"
```

### From Claude Code

`up` wires the MCP server in automatically. To teach a session *when* to delegate,
install the bundled skill:

```bash
cascadia-tasks install-skill          # into this project's .claude/skills/
cascadia-tasks install-skill --user   # for every project
```

Then, in a session:

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

See [docs/AGENTS.md](docs/AGENTS.md) for the full agent-facing guide.

## Runs anywhere

Pure Python (3.11+), SQLite, and HTTP — no platform-specific code. The hub and workers
run on macOS, Linux, and Windows. Two roles, different needs:

- **The hub** does no inference — it's a bookkeeper. Happy on a Mac Mini, a Linux box, or
  a Raspberry Pi. Moving it is a one-line change (`--advertise` / the workers' `--hub`).
- **A worker** calls a model server over HTTP, so its requirements are whatever
  Ollama / vLLM / Tahoma needs. A worker can even run on a different machine from the
  model it drives.

Windows workers: everything works; if you grant the `shell` tool, write allowlist
patterns in `cmd.exe` syntax (see [`examples/deploy/windows.md`](examples/deploy/windows.md)).

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

**Every room is bounded whether you ask for it or not.** A room created without limits
gets a 200-message cap and a 24-hour idle timeout; you can raise them, not remove them.
Two agents set to reply to everything will therefore stop, rather than talking until the
hardware gives out. The reverse is also handled: an agent that holds the floor and has
nothing to say passes the turn, and the hub hands the floor on if its holder goes
offline, so a dialogue cannot deadlock either.

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

`cascadia-tasks join` is the easy path — it auto-detects the model and needs no file.
Reach for a **TOML config** only when you want to pin things `join` doesn't set: a custom
persona, a specific backend adapter, or worker-side tools. Run one with
`cascadia-tasks agent run -c <file>`. See [`examples/agents/`](examples/agents/) for
Ollama, vLLM, and Tahoma configs.

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
shell_allowlist = ["rg *", "ls *"]  # an interpreter here would mean arbitrary code
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
cascadia-tasks result task_hx8k2m9qp4
cascadia-tasks rooms                # conversations, including recently closed
cascadia-tasks watch room_a7c2k9    # follow a conversation live
cascadia-tasks cancel task_hx8k2m9qp4
cascadia-tasks ps                   # background processes started here
```

A background sweep reclaims tasks whose agent disappeared (requeue, then fail after two
attempts), times out overruns, archives idle rooms, and hands on a stuck dialogue floor.

Persistent-service units are in [`examples/deploy/`](examples/deploy/): launchd (hub),
systemd (workers), and [Windows](examples/deploy/windows.md) (NSSM / Task Scheduler).

## Security posture

LAN and Tailscale only, and **set the two tokens**:

```bash
export CASCADIA_TASKS_ADMIN_TOKEN=$(openssl rand -hex 24)     # admin + operator routes
export CASCADIA_TASKS_REGISTER_TOKEN=$(openssl rand -hex 24)  # who may join the fleet
```

With no tokens the hub runs in **open mode** — any process that can reach the port may
read every transcript and dispatch work to every node. It says so loudly at startup.
Once either token is set, authentication is enforced everywhere and reads are scoped:
an agent sees rooms it belongs to and tasks it created or was assigned, and only the
admin token sees the whole fleet.

Other guarantees worth knowing:

- Agents authenticate with a bearer token minted at registration. Re-registering an
  existing name requires that token (or the admin token), so a name cannot be hijacked.
- `kind` is not self-declared — only workers may self-register.
- Worker tools are opt-in, allowlisted per command pattern, jailed to one directory, and
  refuse shell metacharacters unless the matching pattern contains one. `web_fetch`
  refuses private, loopback, and link-local addresses, re-checking every redirect hop.
- An interpreter in a shell allowlist (`python3 *`) is arbitrary code execution by
  definition. List narrower commands unless you mean to grant it.
- There is no TLS termination here — Tailscale provides transport encryption. Never
  expose the hub through a Tailscale funnel.

## Development

```bash
uv run pytest              # 159 tests
uv run ruff check src tests
uv run ruff format src tests
```

[SPEC.md](SPEC.md) is the contract; [CLAUDE.md](CLAUDE.md) is the working agreement
(conventional commits, no AI co-authors).
