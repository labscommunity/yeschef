# Using farmteam from an agent

This is the reference for an AI agent (a Claude Code or Codex session, or any MCP
client) that wants to use the fleet. For the short, always-loaded version, install the skill:

```bash
farmteam install-skill            # into this project's .claude/skills/
farmteam install-skill --user     # into ~/.claude/skills/ for every project
```

## What the fleet is

A `farmteam` hub connects your session to **local AI agents** running on the user's
own machines against local models. You dispatch work to them over MCP; they cost no
Anthropic tokens. The hub is durable — a task you submit is checkable from any session at
any later time.

## The tools

| tool | use it to |
|---|---|
| `list_agents()` | see who is online and what capability tags they carry |
| `submit_task(title, spec, assignee=… \| selector=…)` | dispatch background work; returns a `task_id` immediately |
| `task_status(task_id)` | check state (`queued`/`working`/`completed`/`failed`) and progress |
| `task_result(task_id)` | get the finished output |
| `list_tasks(state?)` / `cancel_task(task_id)` | survey and stop work |
| `provide_input(task_id, message)` | answer an agent that parked a task on `input_required` |
| `wait_task(task_id, wait_s≤60)` | wait efficiently for a state change (one call/min, early return) |
| `task_files(task_id)` / `task_file(task_id, path)` | list and pull the files a task produced on its worker |
| `send_message(to, message)` / `fetch_messages(wait_s?)` | 1:1 multi-turn |
| `create_room(topic, participants, …)` / `post(room, message)` | group conversation |
| `start_dialogue(participants, goal, …)` | agents converse autonomously; you observe |
| `room_transcript(room)` / `archive_room(room)` | read or stop a conversation |
| `set_identity(label)` | name your session so replies route to you |

## Deciding what to delegate

Delegate high-volume, mechanical, or long-running work; keep judgment and final synthesis
on the main model. The strong pattern is **fan out to local agents, verify the results
yourself**. Don't delegate when a single high-quality answer matters more than saving
tokens — local models are smaller.

## Targeting

- `assignee="closer"` — a specific agent.
- `selector="tier:reasoning"` — any agent carrying that tag; exactly one claims it.

Tags are per-fleet. Run `list_agents()` to see what this deployment actually offers
(commonly `tier:fast`, `tier:reasoning`, `node:<host>`). The operator can document their
fleet's tiers in a project `CLAUDE.md`.

## Async discipline

`submit_task` never blocks — it returns a `task_id` and the agent works in the background.
Check `task_status` when you next have a reason to, not in a tight loop. For a
conversational reply, use a single `fetch_messages(wait_s=30)` rather than many bare
polls.

## The buildout pattern

For "spec it here, build it there": dispatch to a worker with a workspace (file tools
or a `cli` coding-agent backend), then bring the results home:

```
submit_task(title="landing page", spec=<the design spec>, assignee="carpenter")
# spawn farmteam-watcher in the background with the task id — it appears in the
# subagent panel, waits via wait_task, and writes returned files into the project.
# Or do it inline:
wait_task("task_ab12cd")             # repeat while not done
task_files("task_ab12cd")            # → index.html, css/styles.css
task_file("task_ab12cd", "index.html")   # fetch content, Write it into the repo
```

## Worked example

```
list_agents()
# → fastball (tier:fast, online), closer (tier:reasoning, online)

submit_task(
  title="classify feedback",
  spec="For each line of the pasted feedback, label it bug | feature | praise | other.",
  selector="tier:fast")
# → {task_id: "task_9f3k2p"}

# ... do other work ...

task_status("task_9f3k2p")   # → working, 60%
task_result("task_9f3k2p")   # → the labeled lines

# Have two agents debate an approach, then read the outcome:
start_dialogue(
  ["fastball", "closer"],
  goal="Propose and agree on a cache eviction policy for the gateway.",
  stop_phrase="AGREED")
# → {room_id: "room_x7c2"}
room_transcript("room_x7c2")
```
