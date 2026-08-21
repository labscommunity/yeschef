# Renders docs/claude-demo.gif — farmteam driven from inside a Claude Code session.
#
# Unlike docs/demo.tape this one is live, not a replay: a real Claude Code session
# calling the farmteam MCP tools against a real hub with real local workers. Recording
# it again needs the hub reachable and the MCP server wired (`farmteam up` does both).
#
#   vhs docs/claude-demo.tape

Output docs/claude-demo.gif

Set FontSize 15
Set Width 1240
Set Height 720
Set Padding 20
Set Theme "Catppuccin Mocha"
Set TypingSpeed 28ms

# Launch and get past the workspace-trust prompt off camera, so the recording opens
# on a working session rather than on setup.
Hide
Type "cd /tmp/ft-demo && clear"
Enter
Sleep 1s
Type "claude --allowedTools 'mcp__farmteam__list_agents,mcp__farmteam__submit_task,mcp__farmteam__task_status,mcp__farmteam__task_result,Read'"
Enter
Sleep 5s
Enter
Sleep 14s
Show

# Dispatch the buildout and keep working.
Type "I specced the landing page in DESIGN.md. Send the buildout to my farm team and don't wait around — just give me the task id."
Enter
Sleep 40s

# Check on it from the same session, later.
Type "is it done yet?"
Enter
Sleep 30s
Sleep 3s
