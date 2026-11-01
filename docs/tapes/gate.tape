# The gate: replay what a session might try against the default policy, then one refusal the way a hook sees it.
Output "/tmp/ws-gate.gif"
Set Width 1240
Set Height 640
Source docs/tapes/_brand.tape

# Off camera: the demo HOME and its prompt.
Hide
Type "source ~/.bashrc && cd ~/projects/tide-tables && clear"
Enter
Sleep 300ms
Type "ws gate init >/dev/null && clear"
Enter
Sleep 1s
Show
Type "cat ~/calls.jsonl"
Sleep 300ms
Enter
Sleep 1.2s
Type "ws gate replay --runner claude-code ~/calls.jsonl"
Sleep 400ms
Enter
Sleep 2s
Type "ws gate check --runner claude-code < ~/one-call.json; echo exit $?"
Sleep 400ms
Enter
Sleep 2s
Screenshot docs/assets/ws-gate.png
Sleep 1s
