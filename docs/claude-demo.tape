# Renders docs/claude-demo.gif — the full yeschef loop from one prompt:
# the line (local models, named machines) → fire the ticket → the yeschef-expediter
# subagent appears in the panel → the plate and its files come back on their own.
# No polling.
#
# Live session, not a replay. Playback runs at 2x (typing slowed to compensate); the
# hidden cut skips part of the local cook's build time. Re-recording needs a reachable
# hub with a `line-cook` cook online and `yeschef install-skill` run in the project.
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
Type "cd /tmp/yeschef-demo && clear"
Enter
Sleep 1s
Type "claude --model opus --allowedTools 'mcp__yeschef__list_agents,mcp__yeschef__fleet_stats,mcp__yeschef__submit_task,mcp__yeschef__task_status,mcp__yeschef__wait_task,mcp__yeschef__task_result,mcp__yeschef__task_files,mcp__yeschef__task_file,mcp__yeschef__wait_room,mcp__yeschef__room_transcript,Read,Write,Task'"
Enter
Sleep 5s
Enter
Sleep 14s
Show

# One prompt: show the line, fire the ticket, spawn the expediter, hand the session back.
Type "Read DESIGN.md, show me the line, then fire the buildout to line-cook and spawn the yeschef-expediter in the background to plate the files into ./site. When it comes back, give it a quick taste — if it's close, tidy any small drift locally instead of re-firing — then hand me the finished result. Keep it tight."
Enter
Sleep 46s

# The local cook builds during this cut; the expediter is waiting on it.
Hide
Sleep 35s
Show

# Nothing is typed here — the expediter calls the plate back on its own, with the files.
Sleep 50s
Sleep 3s
