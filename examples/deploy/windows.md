# Running on Windows

yeschef is pure Python and runs on Windows unchanged. The hub and workers behave
the same; only the "run it as a service" mechanics differ from launchd/systemd.

For a quick start you don't need a service at all — `yeschef up --detach` (hub) and
`yeschef join --detach` (worker) run in the background and are stopped with
`yeschef down`. Use a real service only for machines that must come back after a
reboot.

## Option A — NSSM (simplest persistent service)

[NSSM](https://nssm.cc) wraps any command as a Windows service.

```powershell
# hub, on the orchestrator
nssm install yeschef-hub "C:\path\to\.venv\Scripts\yeschef.exe" hub serve --port 8787
nssm set yeschef-hub AppEnvironmentExtra `
  CASCADIA_TASKS_ADMIN_TOKEN=<token> CASCADIA_TASKS_REGISTER_TOKEN=<token>
nssm start yeschef-hub

# worker, on each node (Ollama/vLLM already running locally)
nssm install yeschef-agent "C:\path\to\.venv\Scripts\yeschef.exe" `
  join --hub http://<hub-host>:8787 --token <register-token>
nssm start yeschef-agent
```

## Option B — Task Scheduler (no extra software)

Create a task that runs at startup:

```powershell
$action  = New-ScheduledTaskAction -Execute "C:\path\to\.venv\Scripts\yeschef.exe" `
                                    -Argument "join --hub http://<hub-host>:8787 --token <token>"
$trigger = New-ScheduledTaskTrigger -AtStartup
Register-ScheduledTask -TaskName "yeschef-agent" -Action $action -Trigger $trigger `
                       -RunLevel Highest -User "SYSTEM"
```

## Notes specific to Windows workers

- **Shell tool.** If you grant an agent the `shell` tool, commands run through `cmd.exe`.
  The allowlist screens for cmd's chaining and expansion characters (`&`, `|`, `>`, `%`,
  `^`) as well as the POSIX ones, but write allowlist patterns for cmd syntax
  (`dir *`, not `ls *`). Chat-only agents — the default — are unaffected.
- **Model backend.** Point `join` at your local runtime as usual; auto-detection probes
  the same ports (Ollama 11434, vLLM 8000, LM Studio 1234).
- **Firewall.** Allow inbound on the hub's port for the LAN/Tailscale interface only.
