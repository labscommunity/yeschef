---
description: Install the yeschef CLI, start a hub on this machine, and wire everything up
---

Set up yeschef on this machine, end to end. Work through these steps, reporting each
result briefly. Stop and show the error if a step fails.

1. **Check for the CLI.** Run `yeschef --version`. If it exists, skip to step 3.

2. **Install it.** Try, in order, stopping at the first that works:
   - `uv tool install yeschef`
   - `pipx install yeschef`
   - `uv tool install git+https://github.com/labscommunity/yeschef` (if the PyPI
     package is not available yet)
   Confirm with `yeschef --version`. If none work, report what's missing (Python 3.11+
   and uv or pipx) and stop.

3. **Start the hub.** Run `yeschef up --detach`. This generates auth tokens, starts the
   hub in the background, and wires the `yeschef` MCP server into both Claude Code (HTTP MCP)
   and Codex (a stdio bridge), whichever are installed. Capture the `yeschef join ...` command it prints — that is what each machine
   machine runs.

4. **Verify.** Run `yeschef doctor` and confirm the hub is reachable. If a local model
   server (Ollama/vLLM/LM Studio) was detected on this machine, offer to also run
   `yeschef join --detach` here so this box doubles as a cook.

5. **Report.** Tell the user:
   - the hub is up and this session is wired to it (note: the MCP connection loads on
     the next Claude Code start),
   - the exact join command to run on each machine machine,
   - that `yeschef cooks` shows the line, and the `yeschef-expediter` subagent from
     this plugin puts fired tickets on the rail in the subagent panel.

Do not expose the hub through any public tunnel or port-forward — LAN/Tailscale only.
