# Renders docs/demo.gif from the recorded dialogue transcript.
# The conversation is real (two local models on one of our GPU boxes); the replay
# re-streams it with live pacing so this render is reproducible. Requires `vhs`.
#
#   vhs docs/demo.tape

Output docs/demo.gif

Set FontSize 17
Set Width 1040
Set Height 640
Set Padding 18
Set Theme "Catppuccin Mocha"
Set TypingSpeed 45ms

Type "farmteam replay docs/demo.json --max-gap 1.5 --tokens-per-sec 60"
Enter

Sleep 31s
