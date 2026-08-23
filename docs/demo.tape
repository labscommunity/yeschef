# Renders docs/demo.gif from the recorded dialogue transcript.
#
# The conversation is real — two local models on one GPU box, with a human interjecting
# — captured once with `yeschef dialogue --record`. Replaying it makes the render
# deterministic, so this GIF can be regenerated any time:
#
#   vhs docs/demo.tape

Output docs/demo.gif

Set FontSize 16
Set Width 1180
Set Height 620
Set Padding 20
Set Theme "Catppuccin Mocha"

# The replay invocation itself is scaffolding — hide it so the recording opens on the
# command that actually produced the conversation.
Hide
Type "clear && yeschef replay docs/demo.json"
Enter
Sleep 1s
Show

Sleep 25s
