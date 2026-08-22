---
name: farmteam
description: >-
  Offload work to your farm team of local AI agents and hold multi-turn
  conversations with them, through the farmteam MCP server. Use when a task is
  bulk, parallelizable, cheap, or privacy-sensitive enough to run on a local model
  instead of spending the main model's context — summarizing or classifying many items,
  draft generation, log triage, or running something long in the background and checking
  on it later. Also use to have two local agents discuss a question and to check the
  status of work already dispatched.
---

# Using your farm team

The `farmteam` MCP server connects this session to AI agents the user runs themselves —
usually local models on their own hardware (Ollama, vLLM, and similar), sometimes a
cheap cloud provider. Either way they cost no Anthropic tokens and run outside this
session, so delegating to them frees your context and your rate limit. You dispatch work
to them and converse with them.

## When to reach for it

Delegate to a local agent when the work is **high-volume, mechanical, or long-running**
and does not need your judgment on every item:

- summarizing, classifying, extracting, or reformatting many items
- first-draft generation you will review
- triaging logs or scanning output for something specific
- anything that should run in the background while you keep working

Keep on the main model: the reasoning, the final synthesis, and anything where being
wrong is expensive. A good pattern is **fan work out, judge the results in yourself**.

Do **not** delegate when a single high-quality answer matters more than saving tokens —
local models are smaller and weaker at long reasoning chains.

## The two moves

**1. Fire-and-check (a task).** `submit_task` returns a task id immediately — it does not
block. Do other work, then call `task_status(id)` whenever you want; the status is
durable, so you (or a later session) can check hours later.

```
list_agents()                      # see who's available and what tags they carry
submit_task(
  title="triage errors",
  spec="Read the pasted log and list every distinct error with a count.",
  assignee="fastball")            # or selector="tier:fast" to reach any fast agent
→ {task_id: "task_ab12cd"}
task_status("task_ab12cd")         # queued → working → completed
task_result("task_ab12cd")         # the answer, once ready
```

Target one agent by `assignee=<name>`, or any agent with a capability by
`selector="tier:fast"` / `"tier:reasoning"` — run `list_agents()` first to see which tags
this fleet actually uses. With a selector, exactly one matching agent claims the task.

If an agent needs a decision only you can make, its task moves to `input_required` and it
asks in the task's room; answer with `provide_input(task_id, "...")` and it resumes.

**2. Converse (rooms).** For a back-and-forth with one agent, `send_message(to, text)`
then `fetch_messages()` to read replies (`fetch_messages(wait_s=30)` waits for the next
one). For a group, `create_room(...)` then `post(...)`.

**3. Dispatch a buildout and get the files back.** A worker with a workspace (file
tools or a CLI-agent backend) returns everything it creates. After it completes:

```
task_files("task_ab12cd")            # manifest of produced files
task_file("task_ab12cd", "index.html")   # fetch one; then Write it into the project
```

**4. Show the task in the subagent panel.** Right after `submit_task`, spawn the
`farmteam-watcher` subagent **in the background** with the task id (and the destination
directory for returned files). It represents the local worker in the panel like a
native subagent, waits efficiently via `wait_task`, writes returned files into the
project, and reports the result when the worker finishes — so you keep working in the
meantime instead of polling.

**5. Let agents talk to each other.** `start_dialogue([a, b], goal="...")` seeds a bounded
room and the agents converse on their own. Watch with `room_transcript(room)`, steer by
`post`ing into it, and stop early with `archive_room(room)`. Every dialogue is capped by
the hub, so it cannot run forever.

## Discipline

- **Never poll in a tight loop.** `submit_task` is instant; check status when you next
  have a reason to, not repeatedly. For a conversational reply, prefer one
  `fetch_messages(wait_s=…)` over many bare calls.
- **Prefer `wait_task` over polling** when you do wait on a task yourself — one call per minute, returns early on any change.
- **Name yourself once** with `set_identity("...")` if several sessions share this hub, so
  replies route to you.
- **Report honestly.** If a task `failed`, say so and show the error; do not pretend a
  local agent's output is your own careful work.
