# The look every tape shares: the ws visual kit (src/ws/setup_ui/brand.py). Violet is chosen, mint is done, amber is a
# warning or a refusal, one grey for everything quiet. ws draws in 256-colour numbers, so the palette below only sets
# the frame, the cursor and the 16 base colours. Only settings here; each tape sets its own size. VHS ignores a Set that comes after any command,
# Source included, so each tape sets its height before it sources this file.
Set Shell "bash"
Set FontFamily "JetBrains Mono"
Set FontSize 16
Set LineHeight 1.15
Set Padding 28
Set Margin 0
Set WindowBar Colorful
Set WindowBarSize 36
Set BorderRadius 10
Set CursorBlink false
Set TypingSpeed 45ms
Set Theme { "name": "ws", "background": "#14121c", "foreground": "#e4e1ee", "cursor": "#875fff", "selection": "#2f2a45", "black": "#14121c", "red": "#ff5f87", "green": "#00ffaf", "yellow": "#ffaf00", "blue": "#5f5fff", "magenta": "#875fff", "cyan": "#00d7af", "white": "#e4e1ee", "brightBlack": "#8a8a8a", "brightRed": "#ff87af", "brightGreen": "#5fffd7", "brightYellow": "#ffd75f", "brightBlue": "#8787ff", "brightMagenta": "#af87ff", "brightCyan": "#5fffd7", "brightWhite": "#ffffff" }
Env HOME "/home/demo"
Env TERM "xterm-256color"
Env COLORTERM "truecolor"
Env LANG "C.UTF-8"
