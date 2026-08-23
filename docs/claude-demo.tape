# Renders docs/claude-demo.gif — the full farm-team loop from one prompt:
# roster (local models, named machines) → dispatch → the yeschef-expediter subagent
# appears in the panel → the result and files come back on their own. No polling.
#
# Live session, not a replay. Playback runs at 2x (typing slowed to compensate); the
# hidden cut skips part of the local model's build time. Re-recording needs a reachable
# hub with a `builder` worker and `yeschef install-skill` run in the project.
#
#   vhs docs/claude-demo.tape

Output docs/claude-demo.gif

Set FontSize 15
Set Width 1240
Set Height 720
Set Padding 20
Set TypingSpeed 60ms
Set PlaybackSpeed 2.0
Set Theme "Catppuccin Mocha"

# Launch and clear the workspace-trust prompt off camera.
Hide
Type "cd /tmp/ft-demo && clear"
Enter
Sleep 1s
Type "claude --allowedTools 'mcp__yeschef__list_agents,mcp__yeschef__submit_task,mcp__yeschef__task_status,mcp__yeschef__wait_task,mcp__yeschef__task_result,mcp__yeschef__task_files,mcp__yeschef__task_file,Read,Write,Task'"
Enter
Sleep 5s
Enter
Sleep 14s
Show

# One prompt: dispatch, spawn the watcher, and get the session back.
Type "Read DESIGN.md, show me the farm team roster, then dispatch the buildout to the builder and spawn the yeschef-expediter in the background to bring the files into ./site when it's done. Then hand the session back to me."
Enter
Sleep 38s

# The local model builds during this cut; the watcher is waiting on it.
Hide
Sleep 45s
Show

# Nothing is typed here — the watcher returns on its own with the report and files.
Sleep 70s
Sleep 3s
