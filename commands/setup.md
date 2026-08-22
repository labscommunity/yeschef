---
description: Install the farmteam CLI, start a hub on this machine, and wire everything up
---

Set up farmteam on this machine, end to end. Work through these steps, reporting each
result briefly. Stop and show the error if a step fails.

1. **Check for the CLI.** Run `farmteam --version`. If it exists, skip to step 3.

2. **Install it.** Try, in order, stopping at the first that works:
   - `uv tool install farmteam`
   - `pipx install farmteam`
   - `uv tool install git+https://github.com/labscommunity/farmteam` (if the PyPI
     package is not available yet)
   Confirm with `farmteam --version`. If none work, report what's missing (Python 3.11+
   and uv or pipx) and stop.

3. **Start the hub.** Run `farmteam up --detach`. This generates auth tokens, starts the
   hub in the background, and wires the `farmteam` MCP server into Claude Code at user
   scope. Capture the `farmteam join ...` command it prints — that is what each worker
   machine runs.

4. **Verify.** Run `farmteam doctor` and confirm the hub is reachable. If a local model
   server (Ollama/vLLM/LM Studio) was detected on this machine, offer to also run
   `farmteam join --detach` here so this box doubles as a worker.

5. **Report.** Tell the user:
   - the hub is up and this session is wired to it (note: the MCP connection loads on
     the next Claude Code start),
   - the exact join command to run on each worker machine,
   - that `farmteam agents` shows the roster, and the `farmteam-watcher` subagent from
     this plugin makes dispatched tasks appear in the subagent panel.

Do not expose the hub through any public tunnel or port-forward — LAN/Tailscale only.
