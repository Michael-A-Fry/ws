"""The tmux side of `ws term --tmux NAME`: the options ws manages, gated by the tmux version, and the commands.

The browser is the one size owner, so a session ws creates gets `window-size manual` and each resize from the page is
passed on with `resize-window`; two clients (a phone and a laptop) then never repaint each other's grid. One more option
helps a full-screen app on that session's window: `allow-passthrough on` (notifications and clipboard escapes reach the
browser). Both are set on the session and window ws just created, by name, and nowhere else: no global (`-g`) option is
ever changed, `aggressive-resize` is never set (iTerm2's tmux integration refuses it), and `tmux.conf` is never read or
written. A session that already exists is attached to as it is, with none of its options changed. `escape-time` and
`extended-keys` belong to the whole tmux server, so ws sets them only when the person asks (`server_options=True`).
Options a tmux version doesn't have are left out.

Every command goes through `run(argv) -> (returncode, stdout)`, so tests inject a fake; `runner(prefix)` makes the real
one, and a prefix such as `["-L", "name"]` points it at a private tmux server.
"""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = [
    "NAME_RE",
    "Opt",
    "Tmux",
    "TmuxError",
    "managed_options",
    "parse_version",
    "run_tmux",
    "runner",
]

NAME_RE = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_-]{0,63}")
Run = Callable[[list[str]], "tuple[int, str]"]


class TmuxError(RuntimeError):
    """A tmux command ws needed failed; `str(e)` is tmux's own message."""


@dataclass(frozen=True)
class Opt:
    name: str
    value: str
    scope: str  # "session", "window" or "server"
    since: tuple[int, int]  # the first tmux version that has it


OPTIONS = (
    Opt("window-size", "manual", "session", (2, 9)),
    Opt("allow-passthrough", "on", "window", (3, 3)),
    Opt("escape-time", "10", "server", (2, 6)),
    Opt("extended-keys", "on", "server", (3, 2)),
)


def parse_version(text: str) -> tuple[int, int]:
    """`tmux 3.3a` -> (3, 3). A development build (`tmux next-3.5`, `tmux master`) counts as the newest."""
    m = re.search(r"(\d+)\.(\d+)", text)
    if m:
        return int(m.group(1)), int(m.group(2))
    return (999, 0) if re.search(r"master|next", text) else (0, 0)


def managed_options(version: tuple[int, int], *, server_options: bool) -> list[Opt]:
    """The options to set on this tmux: those it has, and server-wide ones only when `server_options`."""
    return [o for o in OPTIONS if version >= o.since and (server_options or o.scope != "server")]


def runner(prefix: list[str] | None = None, env: Mapping[str, str] | None = None) -> Run:
    """The real `run`: tmux with `prefix` (for example `-L name -f /dev/null` for a private server) before each command,
    in `env` when it's given (a tmux server started here keeps that environment for every pane it makes)."""
    pre = list(prefix or [])
    environ = dict(env) if env is not None else None

    def run(argv: list[str]) -> tuple[int, str]:
        try:
            p = subprocess.run(
                ["tmux", *pre, *argv], capture_output=True, text=True, timeout=10, check=False, env=environ
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return 127, str(e)
        return p.returncode, (p.stdout if p.returncode == 0 else p.stderr or p.stdout)

    return run


run_tmux = runner()


class Tmux:
    """Prepare a named session and keep its window at the browser's size."""

    def __init__(
        self, name: str, *, run: Run = run_tmux, server_options: bool = False, prefix: list[str] | None = None
    ):
        if not NAME_RE.fullmatch(name):
            raise ValueError(
                "a tmux session name is 1 to 64 letters, digits, dashes or underscores, and doesn't start with a dash"
            )
        self.name, self.run, self.server_options = name, run, server_options
        self.prefix = list(prefix or [])
        self.skipped: list[Opt] = []  # server options left alone, for the notice the person sees
        self.adopted = False  # the session was there already: attached to, its options untouched
        self._window = ""
        self._status_rows = 1

    def _ok(self, argv: list[str]) -> str:
        code, out = self.run(argv)
        if code != 0:
            raise TmuxError(out.strip() or f"tmux {argv[0]} failed")
        return out

    def version(self) -> tuple[int, int]:
        return parse_version(self._ok(["-V"]))

    def prepare(self, cols: int, rows: int) -> list[str]:
        """Create the session if it's missing and set the managed options on it, or leave an existing one alone.
        Returns the argv that attaches a client."""
        version = self.version()
        attach = ["tmux", *self.prefix, "attach-session", "-t", f"={self.name}"]
        if self.run(["has-session", "-t", f"={self.name}"])[0] == 0:
            self.adopted = True
            return attach
        out = self._ok(
            ["new-session", "-d", "-P", "-F", "#{window_id}", "-s", self.name, "-x", str(cols), "-y", str(rows)]
        )
        self._window = out.strip()
        target = {"session": ["-t", f"={self.name}:"], "window": ["-w", "-t", self._window], "server": ["-s"]}
        for opt in OPTIONS:
            if version < opt.since:
                continue
            if opt.scope == "server" and not self.server_options:
                self.skipped.append(opt)
                continue
            self._ok(["set-option", *target[opt.scope], opt.name, opt.value])
        self._status_rows = self._status_lines()
        self.resize(cols, rows)
        return attach

    def _status_lines(self) -> int:
        code, out = self.run(["show-options", "-A", "-v", "-t", f"={self.name}:", "status"])
        value = out.strip() if code == 0 else "on"
        if value == "off":
            return 0
        return int(value) if value.isdigit() else 1

    def resize(self, cols: int, rows: int) -> None:
        """Keep the window at the browser's grid: the client's rows less the status line(s). A session ws didn't
        create keeps its own size policy."""
        if self.adopted or not self._window:
            return
        height = max(1, rows - self._status_rows)
        self.run(["resize-window", "-t", self._window, "-x", str(cols), "-y", str(height)])
