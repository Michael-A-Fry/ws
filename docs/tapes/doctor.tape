# `ws doctor`: one line per check, each with a stable ID, on the demo machine after setup.
Output "/tmp/ws-doctor.gif"
Set Width 1660
Set Height 900
Source docs/tapes/_brand.tape

# Off camera: the demo HOME and its prompt.
Hide
Type "source ~/.bashrc && cd ~/projects/tide-tables && clear"
Enter
Sleep 300ms
Type "ws setup --yes --region NZ --cap 20 --runner claude-code --notes ~/Documents/Garden >/dev/null && clear"
Enter
Sleep 3s
Show
Type "ws doctor --offline"
Sleep 400ms
Enter
Sleep 2.5s
Screenshot docs/assets/ws-doctor.png
Sleep 1s
