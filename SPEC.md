# yeschef — v1 Design Spec

**Status:** draft for review · 2026-08-20
**Owner:** t8
**Repo:** `labscommunity/yeschef` (private)

A communication and task-orchestration layer between Claude Code and local AI agents
running on your own machines. Claude Code stays on the Anthropic API; local agents are
independent processes running against local model backends (Ollama, Tahoma, vLLM, any
OpenAI-compatible endpoint). The hub gives both sides a shared fabric for:

- **Tasks** — dispatch long-running work to a local agent, check status at any time,
  from any Claude Code session, on any machine on the LAN.
- **Rooms** — N-party multi-turn conversation: Claude Code ↔ local agent, local agent ↔
  local agent, or any mix, with ergonomics modeled on Claude Code's cross-session
  messaging (`ListAgents` / `SendMessage`).

## 1. Design constraints (from research, 2026-08-20)

1. Claude Code does not implement the MCP Tasks extension (SEP-2663) as a client
   (anthropics/claude-code#18617 closed not-planned). All tools must therefore return
   fast; long-running semantics live at the application level: `submit_task` returns an
   ID immediately, `task_status(id)` answers anytime.
2. Claude Code v2.1.212+ auto-backgrounds MCP calls that exceed ~2 minutes
   (`CLAUDE_CODE_MCP_AUTO_BACKGROUND_MS`). Long-poll tools must default well under that.
3. No local inference runtime has job semantics — the lifecycle lives here, above the
   runtime.
4. Push into a live Claude Code session is not generally available (channels are
   research-preview). v1 delivery to Claude Code is cursor-based fetch, plus an SSE
   `/watch` endpoint consumable via the Monitor tool for push-like interjection.
5. Build with FastMCP v4 so tools can later be marked `task=True` and become spec-native
   MCP Tasks with no interface change, once Claude Code declares the capability.

## 2. Components (one repo, one Python package)

```
yeschef/
├── src/yeschef/
│   ├── hub/          # FastMCP server + agent-facing HTTP/SSE API + SQLite store
│   ├── sdk/          # Python client library — THE protocol contract
│   ├── agent/        # reference harness daemon (uses sdk/)
│   │   └── backends/ # anthropic_compat, openai_compat (local or cloud), tahoma, cli
│   ├── tools/        # opt-in worker-side tool executor (shell, file, web_fetch)
│   └── cli.py        # `yeschef` command
├── SPEC.md
└── README.md
```

- **Hub** — single process on the orchestrator box. One ASGI app serving two faces:
  - `/mcp` — FastMCP HTTP transport, for Claude Code sessions.
  - `/api/v1` — REST + SSE, for agents (harness daemons or anything embedding the SDK).
  - State in SQLite (WAL mode). No other infrastructure.
- **SDK** — `yeschef.sdk.AgentClient`: register, stream events, send messages,
  claim/update tasks. This is the contract; Tahoma (or anything else) can embed it to
  become a first-class agent without running the harness.
- **Harness** — reference agent runtime: config file in, named room-participating,
  task-working agent out. One process per agent; multiple agents per node allowed.
- **CLI** — `hub serve`, `agent run -c <config>`, plus human debugging: `agents`,
  `tasks`, `rooms`, `tail <room>`, `kill-room <room>`.

## 3. Primitives

Tasks and rooms are **separate primitives** (per design review). A task may link to a
room for discussion; a room may exist with no task. DMs are 2-party rooms, so messaging
has one implementation underneath.

### 3.1 Agents

Registered identities in the hub.

| field | notes |
|---|---|
| `name` | stable slug, unique — `miner-qwen`, `alpha-scout` |
| `kind` | `worker` \| `claude` |
| `node` | hostname |
| `backend` | backend + model id (informational) |
| `tags` | capability selectors — `tier:reasoning`, `ctx:32k`, `tools:shell` |
| `status` | `online` / `busy` / `offline` — derived from heartbeat TTL (30 s) |

Claude Code sessions become addressable agents lazily: the first MCP call from a session
auto-registers an identity (`claude:<label>`, label settable via `set_identity`). Local
agents can then address replies **to** a Claude session, which fetches them by cursor.

### 3.2 Rooms and messages

N-party from day one.

- **Room:** `id`, `topic`, `created_by`, participant list (invite-only by default;
  `open: true` allows any agent to join), `policy`, `archived`.
- **Policy** (anti-runaway guards, hub-enforced). Backstops are applied to *every*
  room at creation — `max_messages` defaults to 200 and `idle_timeout_s` to 24 h when
  the caller omits them, so an unbounded conversation cannot be created by omission:
  - `turn_policy`: `free` (default) or `round_robin` (hub grants the floor in order —
    the right default for 2-agent dialogues so they don't talk over each other)
  - `max_messages`, `max_total_tokens` (budget), `idle_timeout_s`
  - `stop_phrase` — a literal string (e.g. `TASK COMPLETE`) that archives the room
- **Message:** `id`, `room_id`, `seq` (per-room monotonic — the cursor), `sender`,
  `body` (text), `data` (optional JSON payload), `reply_to`, `mentions`, `created_at`.

Delivery: workers hold an SSE stream and receive room events push-style. Claude Code
fetches with `fetch_messages(after_seq)` or long-polls briefly; `@name` mentions set a
flag so an agent's harness knows a reply is expected from it specifically.

### 3.3 Tasks

State machine mirrors the MCP Tasks extension vocabulary for future compatibility:

```
queued → claimed → working → completed | failed | cancelled
                 ↘ input_required ↗ (via provide_input)
```

| field | notes |
|---|---|
| `id`, `title`, `spec` | spec = full prompt/instructions for the worker |
| `created_by` | requesting identity (Claude session or agent — agents can submit tasks too) |
| `assignee` | explicit agent name **or** tag selector (`tier:reasoning`) — first matching idle agent claims atomically |
| `priority`, `timeout_s`, `deadline` | hub enforces timeout → `failed(timeout)` |
| `progress` | latest percent/message; full history in `task_events` |
| `result` | text and/or JSON, plus artifact refs |
| `room_id` | nullable. Created on demand (`task-<id>`) the first time anyone needs to discuss the task — this is how a task becomes multi-turn: `input_required` posts the agent's question into the room; `provide_input` answers there and resumes the task |

`task_events` is an append-only log (state changes, progress, errors) — the audit trail
`task_status` summarizes.

### 3.4 Artifacts

Small blob store on the hub (`artifacts/` dir + DB row: id, mime, size, sha256).
Messages and results reference artifacts by id; 32 MB cap per artifact in v1. Room
transcripts are exportable as artifacts.

## 4. MCP tool surface (Claude Code side)

Modeled on cross-session messaging, extended with tasks. All tools return in
milliseconds except where a `wait` parameter is passed explicitly.

**Messaging** (SendMessage/ListAgents ergonomics)
- `list_agents()` — names, node, model, status, tags
- `send_message(to, message, data?)` — DM; auto-creates/reuses the 2-party room
- `post(room, message, data?, reply_to?)` — post into an N-party room
- `fetch_messages(after?, room?, wait_s? ≤ 60)` — cursor fetch for this session's
  identity (all rooms it participates in, or one room)
- `create_room(topic, participants, policy?)` / `join_room(room)` / `leave_room(room)`
- `list_rooms(mine_only?)` / `room_transcript(room, from_seq?, limit?)`
- `set_identity(label)` — name this session (`claude:mac-mini-main`)

**Autonomous dialogue** (fire-and-observe)
- `start_dialogue(participants, goal, max_messages=40, max_total_tokens?, stop_phrase?,
  turn_policy="round_robin")` — creates a room, seeds it with the goal, agents converse
  without further input; returns `room_id` immediately. Observe with
  `room_transcript`, steer by `post`ing (a human/Claude message interrupts round-robin
  and re-anchors the conversation), stop with `archive_room`.

**Tasks**
- `submit_task(title, spec, assignee_or_selector, priority?, timeout_s?)` → `task_id`
- `task_status(task_id)` — state + progress + recent events
- `task_result(task_id)`
- `list_tasks(state?, assignee?, mine_only?)`
- `cancel_task(task_id)`
- `provide_input(task_id, message)` — answer `input_required`

**Watching (push-like UX today).** `GET /api/v1/watch?identity=<me>&token=…` streams
one JSON line per event. In an interactive session, Claude runs the Monitor tool on
`curl -N <watch-url>` and gets interjected when a task completes or a message arrives —
no polling loop in the conversation. Headless/cross-session flows just poll
`task_status` / `fetch_messages`, which work from any session at any time (SQLite is
the durable store; nothing is session-scoped).

## 5. Agent API (worker side, REST + SSE)

- `POST /agents/register` `{name, kind, node, backend, tags}` → agent token
- `GET  /agents/{name}/events` — SSE: `message`, `task_assigned`, `task_cancelled`,
  `room_invite`, `shutdown`. The open stream is the heartbeat; `POST /agents/{name}/heartbeat` is the fallback for long-poll mode.
- `POST /rooms/{id}/messages` · `GET /rooms/{id}/messages?after=`
- `POST /tasks/{id}/claim` (atomic; 409 if lost) · `/progress` · `/result` · `/fail` ·
  `/input_required`
- `POST /tasks` — agents may submit tasks (enables agent→agent delegation)

Auth: per-agent bearer token issued at registration + a hub admin token for the MCP
face and CLI. LAN/Tailscale only — **the hub must never be exposed via a public
funnel or port-forward**: anything that reaches it can dispatch work to every node.

Enforcement rules settled during implementation:

- With no tokens configured the hub runs open (and warns at startup). Configuring either
  token turns on enforcement everywhere, including reads.
- Reads are scoped: an authenticated agent sees rooms it belongs to and tasks it created
  or was assigned; the admin token sees everything.
- Re-registering an existing agent name requires that agent's current token or the admin
  token, so an identity cannot be taken over.
- `kind` is never self-declared — `POST /agents/register` accepts workers only. Claude
  identities are created by the MCP layer, because claude-kind agents bypass floor
  control by design.

## 6. Harness daemon

One TOML config per agent:

```toml
name = "miner-qwen"
hub = "http://mini.local:8787"
[backend]
type = "openai_compat"            # anthropic_compat | openai_compat | tahoma | cli
base_url = "http://localhost:8000/v1"
model = "qwen3-8b"
max_context_tokens = 32768
[persona]
system_prompt = "You are miner-qwen, a fast reasoning cook in this kitchen…"
reply_when = "mentioned"          # mentioned | round_robin | always
[tools]                           # omit section entirely for chat-only (default)
allow = ["shell", "file_read"]
shell_allowlist = ["rg *", "python3 *"]
file_root = "/home/tate/agent-scratch"
```

Behavior:

- **Tasks:** on `task_assigned` matching self/selector → claim → build context (task
  spec + linked-room transcript if any) → run backend loop → stream `progress` →
  `result` / `fail` / `input_required`. Honors `task_cancelled` mid-run.
- **Rooms:** on `message` in a joined room → reply per `reply_when` policy.
  `round_robin` rooms reply only when the hub grants the floor; `mentioned` replies only
  to `@name`. Context = pinned room goal + recent transcript windowed to fit context.
- **Tools:** opt-in per agent. Where the backend supports native tool-calling
  (Ollama/vLLM/OpenAI-compat do), tools are passed as schemas; otherwise a ReAct-style
  text protocol. Executor enforces the allowlist, a cwd jail for file tools, command
  pattern matching for shell, and per-call timeouts. Everything executes on the worker
  node, never the hub.
- **Backends:** `anthropic_compat` (Ollama v0.14+ — remember to raise Ollama's 4,096
  default context or long tasks silently truncate), `openai_compat` (vLLM on the miner,
  LM Studio, anything `/v1/chat/completions`), `tahoma` (thin adapter to whatever
  Tahoma serves; if Tahoma exposes OpenAI-compat, this collapses to a config preset).

## 7. Failure and lifecycle semantics

- Hub restart: SQLite is the source of truth; agents reconnect and resume; `claimed`/
  `working` tasks whose agent misses heartbeat TTL revert to `queued` (retry once, then
  `failed(agent_lost)`).
- Idempotency: `submit_task` accepts an optional client `dedupe_key`; message posts are
  idempotent on `(sender, client_msg_id)`.
- Every autonomous room is bounded by policy (messages, tokens, idle timeout) — there is
  no unbounded agent↔agent loop by construction.
- Clocks: hub timestamps everything; agents never write times.

## 8. Reference deployment (a 3-node example)

- **Hub:** the always-on box (a Mac mini works well), launchd/systemd service, port 8787.
- **Workers:** three mini-PCs — harness + Ollama; one GPU box — harness + vLLM
  (existing models only; root disk at 98%); Tahoma nodes via SDK or `tahoma` backend.
- **Claude Code (any machine):**
  `claude mcp add --transport http yeschef http://mini.local:8787/mcp`

## 9. Milestones

1. **M1 — dispatch + DM (end-to-end proof):** hub (tasks + 2-party rooms, SQLite),
   SDK, MCP tools, harness with `anthropic_compat`, one Ollama agent on a NUC.
   Exit test: submit task from Claude Code on the Mac, check status from a *different*
   session, hold a multi-turn DM with the agent.
2. **M2 — N-party + autonomous dialogue:** rooms with policies, `start_dialogue`,
   round-robin floor control, `openai_compat` backend (vLLM on miner), artifacts.
   Exit test: two local agents converse to a stop-phrase while Claude observes and
   interjects once.
3. **M3 — tools + watch + Tahoma:** opt-in tool executor, `/watch` + Monitor recipe,
   `tahoma` backend, CLI polish, `input_required` flow.
4. **M4 — forward compatibility (as ecosystem lands):** FastMCP `task=True` (MCP Tasks
   extension), Claude Code channel for push when channels GA, optional A2A v1.0 face
   over the same store if non-Claude agents ever need to submit work.

## 10. Non-goals (v1)

- Not a model router or load balancer (Tahoma's job) — the hub schedules *agents*, not
  GPU shards.
- No web dashboard (CLI + SQLite queries; revisit after M3).
- No WAN federation, no TLS termination (Tailscale provides transport encryption), no
  multi-tenant auth.
- No moderated-turn mode — messaging is direct-delivery like cross-session messaging;
  oversight = observe/interject/kill, not approval gates.

## Appendix A — schema sketch

```sql
agents(name PK, kind, node, backend, tags, token_hash, last_seen, created_at)
rooms(id PK, topic, created_by, open, policy_json, archived, created_at)
room_members(room_id, agent, joined_at, last_read_seq, PRIMARY KEY(room_id, agent))
messages(id PK, room_id, seq, sender, body, data_json, reply_to, mentions_json,
         client_msg_id, created_at, UNIQUE(room_id, seq))
tasks(id PK, title, spec, created_by, assignee, selector, state, priority, timeout_s,
      dedupe_key, room_id, progress_pct, progress_msg, result_json, created_at,
      claimed_by, claimed_at, finished_at)
task_events(id PK, task_id, kind, payload_json, created_at)
artifacts(id PK, mime, bytes, sha256, created_by, created_at)
```

## Appendix B — decisions taken without asking (flag if wrong)

- DMs are 2-party rooms (one messaging implementation).
- Claude sessions auto-register as addressable identities on first MCP call.
- Agent→agent task delegation allowed (agents can call `POST /tasks`).
- Round-robin floor control is the default for `start_dialogue`, free-form for ad-hoc
  rooms.
- SQLite over Redis: single-writer hub on the Mini is well within SQLite/WAL territory;
  Redis only becomes interesting if the hub ever needs horizontal workers.
- Hub port 8787; heartbeat TTL 30 s; artifact cap 32 MB; long-poll cap 60 s (stays
  clear of Claude Code's 2-minute auto-background threshold).
