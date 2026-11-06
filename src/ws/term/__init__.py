"""`ws term`: a browser terminal that behaves like a native one, for reaching a ws machine from a phone or another computer.

A full-screen program (a coding agent's interface, an editor) repaints everything on every size change, so the page has
one size owner and sends one debounced size; the on-screen keyboard never resizes the grid; there is a key bar for the
keys a phone lacks, a visible Paste button, Shift+Enter, and touch-drag scrolling. `bridge` runs the program on a pty and
holds the one client that owns it, `websocket` is the small RFC 6455 server end, `page` and `assets` are the page and
the vendored xterm.js, `tmux` prepares a session whose window the browser sizes, and `app` and `cli` serve it on the
loopback address with a random token. Stdlib only.
"""
