# Web terminal

`ws term` puts a terminal in a browser tab, so you can reach a session on this machine from a phone or another computer.
It runs a full-screen program, such as a coding agent's interface, and keeps the screen still: one size owner, one
debounced size, no size label, and a keyboard that doesn't resize the grid.

## Start it

```sh
ws term
```

ws prints one link, like `http://127.0.0.1:41873/?t=...`. Open it in a browser on this machine. The link carries a
random token that's new every launch, and it stops working when `ws term` exits (press Ctrl-C).

| You want | Run |
|---|---|
| Your shell | `ws term` |
| One command | `ws term -- claude` |
| A tmux session | `ws term --tmux work` |
| A fixed port | `ws term --port 8123` |
| Stop after 15 minutes with no browser attached | `ws term --idle-minutes 15` (the default is 60; 0 never stops) |
| An extra environment variable for the session | `ws term --env NAME=VALUE` |
| Scripts to read the link | `ws term --json` (one JSON document with `url`, `bind` and `port`, printed at start) |

The session runs as you, with your environment, and `TERM=xterm-256color`. A `TMUX` variable from the terminal that
launched `ws term` is removed, because the session isn't inside that tmux. So are the variables a coding agent sets to
mark its own session (`CLAUDECODE`, `CLAUDE_CODE_*`, `OPENCODE` and the Codex sandbox flags), so a program started
from inside an agent doesn't think it's nested in it; `--env NAME=VALUE` still sets one on purpose.

## What the page does

- **One size owner.** The page fits the grid once the fonts have loaded, then opens the connection, so the first size
  it sends is the final one. A later change waits 150 ms and sends only the last size. Repeating the current size sends
  nothing. A full-screen program clears and redraws on every size change, so fewer changes means less flicker.
- **It fills the screen.** The grid has as many rows as fit in the visible height, less the key bar, so on a phone
  the terminal starts at the top of the screen with no blank band, and the page itself never scrolls. The page is
  fitted on load, when the visible height changes (a browser toolbar sliding away) and when you turn the phone.
- **The on-screen keyboard.** The page sets `interactive-widget=resizes-visual`, and a keyboard opening or closing
  doesn't change the grid. The key bar rides above the keyboard and the grid stays anchored to the bottom, so the prompt
  line stays in view. On a touch device the grid is sized to the tallest the visible height has been, so a shorter view
  is a keyboard and a taller one refits. A change in width, such as turning the phone sideways, fits again. On a
  device with a mouse every change fits again.
- **A key bar** with the keys a phone lacks: Esc, Tab, Shift+Tab, Ctrl (applies to the next key), the arrows, `/`,
  a new line (Shift+Enter), Ctrl+C and Paste. Every control carries a `data-action` and has a test.
- **Shift+Enter and Ctrl+Enter** send ESC then carriage return. Browsers send a plain carriage return for both, which
  submits the line; ESC CR works without extended-key support and through tmux.
- **Paste.** The Paste button reads the clipboard and pastes through the terminal, so bracketed-paste markers are added
  when the program asked for them. A 10 KB paste arrives intact. A browser that won't hand over the clipboard (for
  example over plain HTTP on another host) gets a box to paste into instead.
- **Scrolling.** A mouse wheel, and a vertical touch drag, become wheel events for the program when it has mouse mode
  on. Without mouse mode the terminal's own scrollback applies.
- **Notifications and the clipboard.** A program's notification escapes (OSC 9 and OSC 777) become browser
  notifications once the browser allows them, and its clipboard escape (OSC 52) sets your clipboard. Clipboard reads
  are off: a program that asks for your clipboard gets an empty answer.
- **When the connection drops** the page reconnects by itself, and the program redraws. The page also takes the link's
  token out of the address bar once it loads, so it isn't left in the address bar or the history; to open the page
  again use the link `ws term` printed. When the program exits, the
  page says so and puts the terminal back to its normal screen.
- **Synchronized output.** The bundled terminal supports it (DECSET 2026), and ws sets the environment variable a
  program reads to turn it on (`CLAUDE_CODE_FORCE_SYNC_OUTPUT=1`) unless the session is in tmux, where programs don't
  use it. Outside tmux, `ws term` sets that variable in the session's environment; other programs ignore it. Turn it
  off with `--no-sync-output`. Nothing depends on it.

## One window at a time

The session has one owner, so two windows never fight over the size. A second window that connects takes the session
over: the first shows "Another window took this session over" and a Reconnect button, which takes it back. The program
keeps running between windows, and the new window triggers a redraw.

ws term pings the open window every 20 seconds and drops it once it has been silent for more than 40 (checked at
each ping). A window that has vanished
(a closed laptop, a lost network) therefore doesn't block a new window from taking over, doesn't make Ctrl-C wait, and
doesn't keep the idle stop from running.

## tmux

With `--tmux NAME`, ws starts the tmux session `NAME` if it's missing, sets two options on that session and its window
only, and attaches a client:

- `window-size manual` on the session, so the browser's size is the window's size and `resize-window` follows each
  resize. A phone and a laptop on the same session then never repaint each other's grid.
- `allow-passthrough on` on the window, so notification and clipboard escapes reach the browser.

ws never changes a global (`-g`) option, never sets `aggressive-resize` (iTerm2's tmux integration refuses to attach
when it's on), and never reads or writes `tmux.conf`. Options an older tmux doesn't have are left out.

If the session already exists, ws attaches to it without changing any of its options, and says so in one line. Its own
size policy then applies: by default tmux follows the client that typed last.

`escape-time 10` (so Esc arrives at once) and `extended-keys on` belong to the whole tmux server, so ws sets them only
when you add `--tmux-server-options`. That changes the tmux server for every session on it, not just this one, and
stays changed after `ws term` exits. Leave it out if you share the server with other work.

## Reach it from another device

`ws term` listens on 127.0.0.1, this machine only. It never listens on a wider address unless you name one with
`--bind`. Reach it from elsewhere through a tunnel you already trust.

**SSH tunnel.** Forward the same port on your other device, then open the link there:

```sh
ssh -N -L 8123:127.0.0.1:8123 you@your-machine   # on your other device
ws term --port 8123                              # on your machine
```

**A private network (a tailnet or a VPN).** Bind the address your machine has on that network, and open the link
with that address:

```sh
ws term --bind 192.0.2.10 --port 8123
```

`--bind` takes one IPv4 address and prints a warning, because anyone who can reach that address and has the link can
run commands as you. The wildcard addresses `0.0.0.0` and `::` are refused. If a proxy or tunnel gives the page another
host name or port, name it with `--allow-host HOST[:PORT]` (repeat for more).

A request must carry the right `Host` and, for the socket, a matching `Origin`. This closes DNS-rebinding and
cross-site connections. ws sends no CORS headers.

## What's in the page

The page is plain HTML, CSS and JavaScript that ws ships, and it holds no environment values or secrets. Its policy
runs one vendored script by its sha256 and the page's own script and stylesheet by a per-response nonce. Nothing loads
from outside. A test plants a fake value in the server's environment and checks it's absent from everything served.

The terminal is xterm.js, vendored as a pinned copy with its add-ons. The licence is MIT; the texts ship in
`src/ws/term/assets.py`, and a test checks the sha256 of each file.

| Package | Version |
|---|---|
| `@xterm/xterm` | 6.0.0 |
| `@xterm/addon-fit` | 0.11.0 |
| `@xterm/addon-clipboard` | 0.2.0 |
| `@xterm/addon-web-links` | 0.12.0 |

To update, `npm pack` the new versions, replace the blobs and hashes in `assets.py`, and run the term tests.

## How it's tested

- Unit tests cover the WebSocket framing and every refusal, the pty bridge, the key table, the tmux option builder per
  version, the loopback bind, and the asset hashes.
- Replay tests push synthetic byte streams (start-up modes, a repaint on every resize, a 10 KB bracketed paste, the
  reset on exit) through the bridge and check the bytes out.
- A real tmux on a private socket checks that the global options are byte-identical before and after a session runs.
- Browser tests (`tests/e2e/test_term_browser.py`, Playwright with Chromium) drive a desktop and a phone, press every
  key-bar control, paste 10 KB, open and close the keyboard, resize the window, scroll by wheel and touch, and check
  that no size label ever shows. They also check the grid fills the visible space above the key bar (with the
  visible height shorter than the layout one, as on iOS) and that the page never scrolls. They skip when Playwright
  isn't installed.

## Limits

- Not measured: iOS Safari's keyboard behaviour (the page models it), the long-press paste gesture, and a live model
  streaming a long answer through the page.
- Windows needs WSL2.
