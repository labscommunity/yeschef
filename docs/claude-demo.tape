# Renders docs/claude-demo.gif — the full buildout loop from inside Claude Code:
# dispatch to a local worker, then pull the files it built into the project.
#
# Live session, not a replay. Playback runs at 2x so the GIF stays short; typing speed
# is set slow here so it reads naturally after the speed-up. The worker's build happens
# during the hidden gap between the two prompts (a cut, like any screencast).
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
Type "claude --allowedTools 'mcp__farmteam__list_agents,mcp__farmteam__submit_task,mcp__farmteam__task_status,mcp__farmteam__wait_task,mcp__farmteam__task_result,mcp__farmteam__task_files,mcp__farmteam__task_file,Read,Write'"
Enter
Sleep 5s
Enter
Sleep 14s
Show

# 1. Dispatch and keep the session free.
Type "Read DESIGN.md and dispatch the buildout to my farm team's builder. Don't wait on it — just give me the task id."
Enter
Sleep 32s

# The local model builds during this cut.
Hide
Sleep 80s
Show

# 2. Bring the build home.
Type "should be done by now — pull whatever it built into ./site and show me what came back."
Enter
Sleep 50s

Sleep 3s
