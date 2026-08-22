# ⚾ farmteam

**The farm team for Claude Code.** Local models on your own hardware that take the
grunt work, talk to each other in bounded rooms, and never hit a rate limit.

You pay per token and wait out rate limits while the GPUs you already own sit idle.
farmteam turns them into a roster Claude Code can actually manage: dispatch a task, keep
working, check on it whenever — from any session, days later. Your ace does the
thinking; the farm team does the reps.

![One prompt in Claude Code: dispatch to a local worker, the watcher subagent appears in the panel, and the finished files return on their own](docs/claude-demo.gif)

*One prompt, live session: Claude shows the roster — every worker's model and machine
(`builder · ollama/qwen2.5:7b-instruct @ miner`) — dispatches the buildout, and spawns
the `farmteam-watcher` subagent, visible in the native panel like any subagent. The
session comes straight back to you; when the local worker finishes, the watcher returns
on its own, lands the files in `./site`, and Claude reviews the local model's work
against the spec. No polling, no "is it done yet."*

```
   Claude Code ──MCP/HTTP──▶ ┌─────────┐ ◀──REST+SSE── worker ──▶ Ollama  (office-mac)
   (any session,             │   hub   │ ◀──REST+SSE── worker ──▶ vLLM    (gpu-box)
    any machine)             │ SQLite  │ ◀──REST+SSE── worker ──▶ any /v1 (spare-pc)
                             └─────────┘
```

|   | Without a farm team | With one |
|---|---|---|
| Grunt work (summarize, classify, extract, triage) | burns your Claude tokens | runs on your hardware, $0 marginal |
| Rate-limited or throttled | you wait | your fleet keeps working |
| Long-running work | blocks the session, dies with it | dispatched in the background; status durable in SQLite, checkable from any session |
| A second opinion | another API call | two of your machines argue it out in a bounded room while you watch — and interject |

## Two commands

```bash
uv tool install farmteam        # or: pipx install farmteam

# 1. On the hub machine (where you run Claude Code) — generates tokens,
#    wires Claude Code, prints the worker command:
farmteam up

# 2. On each worker — auto-detects Ollama / vLLM / LM Studio, verifies the
#    model answers, registers. Paste the line `up` printed:
farmteam join --hub http://hub-host:8787 --token <printed-by-up>
```

That's the whole setup. Single-box demo: run both on one machine — `join` needs no
flags at all. `farmteam doctor` diagnoses anything that's off; `--detach`/`down`/`ps`
run it all in the background from one terminal. macOS, Linux, and Windows.

**Or let Claude Code install it.** This repo is also a Claude Code plugin:

```
/plugin marketplace add labscommunity/farmteam
/plugin install farmteam@farmteam
```

Then say "set up farmteam" (or run `/farmteam:setup`) and Claude installs the CLI,
starts the hub, wires the MCP connection, and hands you the worker join command. The
plugin also ships the skill and the `farmteam-watcher` subagent automatically.

## What it feels like

From Claude Code (wired automatically by `up`):

```
> list_agents                                     # your roster, live
> submit_task("label these 400 log lines", selector="tier:fast")
  → task_9f3k2p                                   # returns immediately
> task_status("task_9f3k2p")                      # any time, any session
> task_result("task_9f3k2p")
  → … lifetime: 62 tasks · ~1.2M tokens kept local · 9.4h of work on your hardware
> start_dialogue(["fastball", "closer"], goal="agree on a cache eviction policy")
> room_transcript("room_x7c2")                    # watch them work it out
```

From your terminal:

```bash
farmteam ask fastball "what's in today's error log?"    # send, wait, print the reply
farmteam submit "draft release notes from this diff" --to closer
farmteam dialogue fastball closer --goal "pick a caching strategy"   # follows it live
farmteam watch room_x7c2 --record demo.json             # save a transcript
farmteam replay demo.json                               # re-stream it, real pacing
farmteam stats                                          # what the team has done for you
```

The dialogue is the part you have to see: two of your machines, in different colors,
working a problem — and you can type into the middle of it. Every room is **bounded by
construction** (message caps, token budgets, idle timeouts, stop phrases — enforced by
the hub, not by hoping), so nothing loops forever.

![Two local models plan this exact demo while the human interjects mid-conversation](docs/demo.gif)

*Real conversation, not a script. Two local models on one GPU box argue about what this
demo should show — until the operator cuts in and overrules them both. The transcript
ships in this repo; after install, watch the identical conversation yourself with
`farmteam replay docs/demo.json`.*

## Dispatch a buildout, get the files back

Every task with a workspace returns what it builds. A worker with file tools writes
into a per-task jail; a worker with the **`cli` backend** hands the whole task to a real
coding agent — the flagship config runs the full Claude Code harness against your own
local model:

```toml
[backend]
type = "cli"
command = ["claude", "-p", "{prompt}", "--dangerously-skip-permissions"]
[backend.env]
ANTHROPIC_BASE_URL = "http://localhost:11434"   # Ollama v0.14+ speaks Anthropic
```

Deep agentic loop, zero API tokens, and everything it creates ships back through the
hub:

```
> submit_task("build the landing page from DESIGN.md", assignee="carpenter")
> wait_task("task_ab12cd")            # one call/min, returns early when done
> task_files("task_ab12cd")           # index.html · css/styles.css
> task_file("task_ab12cd", "index.html")   # pull it, write it into the repo
```

**See it in the subagent panel.** `farmteam install-skill` also installs the
`farmteam-watcher` subagent: spawn it in the background after dispatching and the local
task shows up in Claude Code's native panel like any subagent — it waits efficiently,
pulls returned files into the project, and reports the result when the worker finishes.

## Isn't this what Agent Teams does?

Complementary, not competing. Native subagents and teams are Claude-only, same billing,
same cloud — excellent at parallel *thinking*. A farm team is the other half:
**heterogeneous workers that are free after hardware**, that keep going when you're
throttled, and whose task status outlives any session. Claude Code stays the brain and
delegates the verifiable grunt work.

## What a worker is

`farmteam join` auto-configures one from whatever model server it finds. A TOML config
(see [`examples/agents/`](examples/agents/)) is for pinning a persona, capability tags
(`tier:fast`, `tier:reasoning` — dispatch by tag and the first idle match claims it),
or **opt-in tools**: shell (pattern-allowlisted, metacharacter-screened), file access
(jailed to one directory), web fetch (refuses internal addresses) — always executed on
the worker, never the hub. Anything embedding the Python SDK (`farmteam.sdk.AgentClient`)
is a first-class agent too.

## Honesty ledger

What this is, and isn't:

- **Local models are slower and weaker than Claude** — often 3–30× slower per token.
  Delegation wins on bounded, verifiable work (bulk transforms, drafts, triage,
  second opinions), not on deep reasoning. That's why the design keeps Claude in charge.
- **Total cost isn't $0.** It's your electricity and your hardware. What it isn't is
  metered, throttled, or revocable.
- **No TLS termination** — the hub is designed for LAN/Tailscale (which encrypts
  transport). Never expose it through a public funnel or port-forward.
- **Dialogues are real model output, not magic.** Small models sometimes say dull
  things. The `--record`/`replay` pipeline exists so demos are replays of real
  conversations, not scripts.
- Windows support is tested in CI logic-paths but has had less real-world mileage than
  macOS/Linux. Reports welcome.

## Security posture

Auth is on by default (`up` generates tokens; `--open` exists for trusted LANs and says
so loudly). Agents hold per-agent bearer tokens; a registered name can't be hijacked
without its token; reads are scoped to the caller once auth is on; agents can't
self-register as privileged kinds; every autonomous room is bounded; worker tools are
opt-in, allowlisted, and jailed. Full details in [SPEC.md](SPEC.md).

## Operating it

```bash
farmteam agents / tasks / task <id> / result <id> / rooms / watch <room> / cancel <id>
farmteam stats            # lifetime: tasks completed, tokens kept local, hours worked
farmteam ps / down        # background processes on this machine
```

A background sweep requeues tasks from lost agents, times out overruns, archives idle
rooms, and un-sticks stalled dialogue turns. Persistent-service units for launchd,
systemd, and Windows are in [`examples/deploy/`](examples/deploy/).

## For agents

Claude Code sessions get tool docs automatically over MCP. To teach a session *when* to
delegate (not just how), install the bundled skill: `farmteam install-skill` (or
`--user` for all projects). The full agent-facing guide is
[docs/AGENTS.md](docs/AGENTS.md).

## Standing on

farmteam is a client of the local-inference ecosystem, not a fork of it: it talks to
[Ollama](https://github.com/ollama/ollama) (and the
[llama.cpp](https://github.com/ggml-org/llama.cpp) engine underneath it),
[vLLM](https://github.com/vllm-project/vllm), LM Studio, and any OpenAI- or
Anthropic-compatible endpoint. The MCP server is built on
[FastMCP](https://github.com/jlowin/fastmcp). Those projects do the hard part.

## Development

```bash
uv venv && uv pip install -e ".[dev]"
uv run pytest              # 176 tests, including thread-level race regressions
uv run ruff check src tests
```

MIT. Formerly `cascadia-tasks` — the old CLI name still works as an alias.
