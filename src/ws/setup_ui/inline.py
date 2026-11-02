"""The terminal walk of `ws setup`: plain ANSI and single-key input, no dependencies, and nothing written until you say so.

`run` is the entry point. `Wizard` is the walk itself and only talks to a text stream and a function that returns the
next key, so a test can drive it with a list of keys. `Terminal` puts the screen in single-key mode and hides the
cursor while ws asks things, and puts both back on every way out (Ctrl-C, SIGTERM, a hang-up, Ctrl-Z and `fg`). `Keys`
turns the bytes of a terminal into one key name at a time. The termios module is imported only inside the code that
needs it, so `ws --help` still loads on native Windows, where setup is refused by name.
"""

from __future__ import annotations

import os
import re
import select
import shutil
import signal
import sys
import threading
import time
from dataclasses import replace
from typing import Self

from ws import __version__
from ws.brand import (
    LOGO_WIDTH,
    SPINNER,
    TAGLINE,
    Ansi,
    can_redraw,
    color_mode,
    visible_len,
)
from ws.install import InstallError
from ws.output import WsError
from ws.setup_ui import flow

HIDE = "\x1b[?25l"
SHOW = "\x1b[?25h"
SMALL = 80  # under this many columns the mark folds to three rows
MAX_LIST = 8  # most file lines the review lists before "and N more"
ESC_WAIT = 0.05  # seconds to wait after an ESC for the rest of a key sequence
KEYS = {
    b"\x1b[A": "up",
    b"\x1b[B": "down",
    b"\x1bOA": "up",
    b"\x1bOB": "down",
}
CURSOR = "▏"
CANCELLED = "  Cancelled. Nothing was written."


class Cancelled(Exception):
    """The person backed out (q, Esc, a hang-up). Nothing has been written when this is raised before the install."""


# ---- keys and the terminal ---------------------------------------------------------------------------------------------


class Keys:
    """One key at a time from a terminal file descriptor, even when several arrive in one read (a held arrow, a paste)."""

    def __init__(self, fd: int) -> None:
        self.fd = fd
        self._buf = bytearray()

    def _fill(self, wait: float | None) -> bool:
        """Read more bytes. With `wait`, give up quietly after that long; without it, end of input is `Cancelled`."""
        if wait is not None and not select.select([self.fd], [], [], wait)[0]:
            return False
        data = os.read(self.fd, 1024)
        if not data:
            if wait is None:
                raise Cancelled
            return False
        self._buf += data
        return True

    def read(self) -> str:
        if not self._buf:
            self._fill(None)
        first = self._buf[0]
        if first == 0x1B:
            return self._escape()
        if first >= 0x80:
            return self._multibyte(first)
        del self._buf[0]
        if first == 0x03:
            raise KeyboardInterrupt
        if first in (0x0D, 0x0A):
            return "enter"
        if first == 0x20:
            return "space"
        if first in (0x7F, 0x08):
            return "backspace"
        return chr(first)

    def _escape(self) -> str:
        buf = self._buf
        if len(buf) < 2 and not self._fill(ESC_WAIT):
            del buf[0]
            return "esc"
        if buf[1] == ord("["):  # a CSI sequence runs to the first byte in 0x40..0x7E
            i = 2
            while True:
                while i < len(buf) and not 0x40 <= buf[i] <= 0x7E:
                    i += 1
                if i < len(buf):
                    break
                if not self._fill(ESC_WAIT):  # cut short: drop it
                    buf.clear()
                    return "other"
            seq = bytes(buf[: i + 1])
            del buf[: i + 1]
            return KEYS.get(seq, "other")
        if buf[1] == ord("O"):  # ESC O x
            if len(buf) < 3 and not self._fill(ESC_WAIT):
                buf.clear()
                return "other"
            seq = bytes(buf[:3])
            del buf[:3]
            return KEYS.get(seq, "other")
        del buf[0]  # an ESC that starts nothing is the Esc key
        return "esc"

    def _multibyte(self, lead: int) -> str:
        size = 2 if 0xC0 <= lead <= 0xDF else 3 if 0xE0 <= lead <= 0xEF else 4 if 0xF0 <= lead <= 0xF7 else 1
        while len(self._buf) < size and self._fill(ESC_WAIT):
            pass
        chunk = bytes(self._buf[:size])
        del self._buf[:size]
        return chunk.decode("utf-8", errors="ignore") or "other"


class Terminal:
    """Single-key input and a hidden cursor while ws asks, put back on every way out.

    While `hold` is set (the install is writing) SIGTERM and SIGHUP only set `pending`, so a write is never cut in half.
    """

    def __init__(self, fd: int, out, *, cursor: bool = True) -> None:
        import termios

        self.fd = fd
        self.out = out
        self.cursor = cursor  # False for TERM=dumb: no hide or show code at all
        self.saved = termios.tcgetattr(fd)
        self.hold = False
        self.pending = False
        self._old: dict[int, object] = {}

    def enter(self) -> None:
        import tty

        tty.setcbreak(self.fd)
        if self.cursor:
            self.out.write(HIDE)
            self.out.flush()

    def leave(self) -> None:
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)
        if self.cursor:
            self.out.write(SHOW)
            self.out.flush()

    def _on_end(self, signum, frame) -> None:
        if self.hold:
            self.pending = True
            return
        raise Cancelled

    def _on_stop(self, signum, frame) -> None:  # Ctrl-Z: give the screen back, then really stop
        self.leave()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def _on_continue(self, signum, frame) -> None:  # fg: take the screen again
        signal.signal(signal.SIGTSTP, self._on_stop)
        self.enter()

    def __enter__(self) -> Self:
        self.enter()
        if threading.current_thread() is threading.main_thread():
            for name, handler in (
                ("SIGTERM", self._on_end),
                ("SIGHUP", self._on_end),
                ("SIGTSTP", self._on_stop),
                ("SIGCONT", self._on_continue),
            ):
                sig = getattr(signal, name, None)
                if sig is not None:
                    self._old[sig] = signal.signal(sig, handler)
        return self

    def __exit__(self, *exc) -> bool:
        for sig, old in self._old.items():
            signal.signal(sig, old if old is not None else signal.SIG_DFL)
        self._old.clear()
        self.leave()
        return False


def is_tty(out) -> bool:
    try:
        return bool(out.isatty())
    except (AttributeError, ValueError, OSError):
        return False


# ---- looks: pure functions of text ----------------------------------------------------------------------------------------


def fit(text: str, room: int) -> str:
    """`text` if its visible length fits; else without colour, cut to one short of `room` and ended with an ellipsis."""
    if visible_len(text) <= room:
        return text
    plain = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    return plain[: max(room - 1, 0)] + "…"


def card(a: Ansi, title: str, right: str, rows: list[str], width: int) -> list[str]:
    """A rounded frame exactly `width` wide: the title on the left, a short caption on the right."""
    inner = width - 4
    if right:
        filler = max(width - 8 - visible_len(title) - len(right), 1)
        head = a.grey("╭─ ") + title + a.grey(" " + "─" * filler + " ") + a.dim(right) + a.grey(" ─╮")
    else:
        filler = max(width - 5 - visible_len(title), 1)
        head = a.grey("╭─ ") + title + a.grey(" " + "─" * filler + "╮")
    body = []
    for row in rows:
        row = fit(row, inner)
        body.append(a.grey("│ ") + row + " " * (inner - visible_len(row)) + a.grey(" │"))
    return [head, *body, a.grey("╰" + "─" * (width - 2) + "╯")]


def banner(a: Ansi, width: int, cols: int = 100) -> list[str]:
    """The mark with the name beside it, then the gradient rule."""
    small = cols < SMALL
    name = a.bold("ws-aitoolkit") + a.dim(f"  v{__version__}")
    beside = (
        {0: name, 1: a.mint("setup", bold=True)}
        if small
        else {1: name, 2: a.dim(TAGLINE), 4: a.mint("setup", bold=True)}
    )
    lines = [row + ("   " + beside[i] if i in beside else "") for i, row in enumerate(a.logo(small=small))]
    return [*lines, a.rule(width)]


def status(a: Ansi, facts: flow.Facts, width: int = 100) -> list[str]:
    lines = []
    for row in flow.status_rows(facts):
        value = row.value
        if row.tone == "ok":
            value = a.mint("✓") + " " + value
        elif row.tone == "warn":
            value = a.amber(value)
        if row.label == "Budget" and facts.cap and facts.spent > 0:
            value += "  " + a.track(min(facts.spent, facts.cap), facts.cap, 20)
        lines.append(fit("  " + a.dim(row.label.ljust(9)) + value, width))
    return lines


def hints(a: Ansi, *pairs: tuple[str, str]) -> str:
    return "  " + a.dim(" · ").join(a.violet(key, bold=True) + " " + a.dim(text) for key, text in pairs)


def command_line(a: Ansi, label: str, cmds: list[str]) -> str:
    return "  " + a.dim(f"{label}  ") + a.dim("  ·  ").join(a.violet(c, bold=True) for c in cmds)


def done_line(a: Ansi, headline: str) -> str:
    return "  " + a.mint("✓ Done", bold=True) + "  " + a.bold(headline)


def track_line(a: Ansi, done: int, total: int, n: int = 30) -> str:
    return "  " + a.track(done, total, n) + "  " + a.dim(f"{done} of {total} steps")


def core_row(a: Ansi, reached: int, room: int) -> str:
    cells = [(a.mint("✓") if i < reached else a.dim("·")) + " " + label for i, (_, label, _) in enumerate(flow.CORE)]
    wide = "  ".join(cells)
    return wide if visible_len(wide) <= room else " ".join(cells)


def plugin_rows(a: Ansi, facts: flow.Facts, picked: set[str], cursor: int, show_cursor: bool, room: int) -> list[str]:
    rows = []
    for i, p in enumerate(facts.plugins):
        under = show_cursor and i == cursor
        arrow = a.violet("❯", bold=True) if under else " "
        if not p.available:
            mark, label, text = a.dim("–"), a.dim(p.label.ljust(16)), "Not available in your region"
        else:
            mark = a.violet("◉", bold=True) if p.name in picked else a.grey("○")
            label = a.violet(p.label.ljust(16), bold=True) if under else a.bold(p.label.ljust(16))
            text = p.line
        tags = []
        if p.available and p.name in facts.base.modules:
            tags.append(a.dim("on now"))
        if p.available and p.tag:
            tags.append(a.grey(f"[{p.tag}]"))
        left = f"{arrow} {mark} {label} {a.dim(text)}"
        tag = " ".join(tags)
        rows.append(left + " " * max(room - visible_len(left) - visible_len(tag), 2) + tag if tag else left)
    return rows


def notes_line(a: Ansi, text: str, editing: bool, state: str) -> str:
    return (
        "  " + a.bold("Notes folder") + "  " + flow.clean(text) + (a.violet(CURSOR) if editing else "") + "   " + state
    )


def choices_rows(a: Ansi, facts: flow.Facts, c: flow.Choices) -> list[tuple[str, str]]:
    """The answers so far, as (label, text) pairs; the cap only when there is one."""
    names = [p.label for p in facts.plugins if p.name in c.plugins]
    rows = [("Core", " · ".join(label for _, label, _ in flow.CORE))]
    if facts.plugins:  # nothing built to choose, nothing to say
        rows.append(("Plugins", " · ".join(names) if names else "none yet (ws plugin add later)"))
    rows.append(("Notes", flow.tilde(c.notes, facts.env)))
    if c.cap:
        rows.append(("Cap", f"US${c.cap:g} a month"))
    return rows


def review_rows(
    a: Ansi, facts: flow.Facts, c: flow.Choices, pv: flow.Preview, *, details: bool, room: int, collapsed: bool
) -> list[str]:
    rows = []

    def add(label: str, text: str) -> None:
        rows.append(a.dim(label.ljust(9)) + text)

    if not collapsed:
        for label, text in choices_rows(a, facts, c):
            add(label, text)
    runner = next((r for r in facts.runners if r.name == c.runner), None)
    add("Runner", runner.label if runner else "none yet (the gate files wait for one)")
    add("Writes", a.mint(pv.headline(), bold=True))
    if details:
        listed = [r for r in pv.rows if r["kind"] == "file" and r["action"] in ("create", "change")]
        rows.append("")
        for r in listed[:room]:
            sign = a.mint("+", bold=True) if r["action"] == "create" else a.amber("~", bold=True)
            what = re.sub(r"\s*\([^()]*\)\s*$", "", r["what"])
            rows.append(f"{sign} {r['path']}  {a.dim(what)}")
        if len(listed) > room:
            rows.append(a.dim(f"… and {len(listed) - room} more"))
    return rows


def install_rows(a: Ansi, prog: flow.Progress, finished: set[str], frame: int) -> list[str]:
    current = next((n for n in prog.names if n not in finished), None)
    rows = []
    for name in prog.names:
        if name in finished:
            rows.append(a.mint("✓", bold=True) + " " + a.bold(name.ljust(20)) + " " + a.mint(prog.detail(name)))
        elif name == current:
            spin = a.violet(SPINNER[frame % len(SPINNER)], bold=True)
            rows.append(spin + " " + name.ljust(20) + " " + a.dim("writing…"))
        else:
            rows.append(a.dim("·") + " " + a.dim(name))
    return rows


# ---- the walk ---------------------------------------------------------------------------------------------------------------


class Wizard:
    """The walk. It writes to `out` and reads keys from `key()`; `live` redraws in place, `interactive` asks questions."""

    def __init__(
        self,
        facts: flow.Facts,
        choices: flow.Choices,
        out,
        key,
        *,
        sleep=time.sleep,
        cols=100,
        color: bool | str = True,
        live: bool = True,
        interactive: bool = True,
        terminal: Terminal | None = None,
        lines=None,
    ) -> None:
        self.facts = facts
        self.choices = choices
        self.out = out
        self.key = key
        self.sleep = sleep
        self._cols = cols
        self._lines = lines
        self.a = Ansi(color)
        self.live = live
        self.interactive = interactive
        self.terminal = terminal
        self._block = 0  # lines of the changing block now on screen
        self._perm = 0  # lines printed for good so far
        self._core_mark = 0  # `_perm` when the Core card began: what the review may fold
        self._fold = 0  # lines the review's fold took (0 = nothing folded)

    # sizes ----------------------------------------------------------------------------------------------------

    @property
    def cols(self) -> int:
        return int(self._cols() if callable(self._cols) else self._cols)

    @property
    def width(self) -> int:
        return max(56, min(self.cols - 4, 100))

    @property
    def small(self) -> bool:
        return self.cols < SMALL

    @property
    def lines(self) -> int:
        if self._lines is None:
            return shutil.get_terminal_size().lines
        return int(self._lines() if callable(self._lines) else self._lines)

    # drawing --------------------------------------------------------------------------------------------------

    def _write(self, text: str) -> None:
        self.out.write(text)
        self.out.flush()

    def print(self, lines: list[str]) -> None:
        """Lines that stay."""
        self._perm += self._block  # a block still showing becomes part of the log
        self._block = 0
        self._write("".join(line + "\n" for line in lines))
        self._perm += len(lines)

    def draw(self, lines: list[str], final: bool = False) -> None:
        """A block that changes: redrawn over itself while live, shown once (when `final`) otherwise."""
        if not self.live:
            if final:
                self.print(lines)
            return
        up = f"\x1b[{self._block}A\r" if self._block else ""
        self._write(up + "".join(f"\x1b[2K{line}\n" for line in lines) + "\x1b[0J")
        if final:
            self._perm += len(lines)
            self._block = 0
        else:
            self._block = len(lines)

    def erase(self) -> None:
        if self.live and self._block:
            self._write(f"\x1b[{self._block}A\r\x1b[0J")
        self._block = 0

    def erase_last(self, n: int) -> None:
        """Take back the last `n` lines that were printed for good (only while live)."""
        if self.live and n > 0:
            self._write(f"\x1b[{n}A\r\x1b[0J")
            self._perm -= n

    def pause(self, seconds: float) -> None:
        if self.live:
            self.sleep(seconds)

    def fail(self, message: str, hint: str) -> None:
        a, text = self.a, flow.clean(message)
        self.print(["", "  " + a.error("✗", bold=True) + " " + a.bold(text), "  " + a.dim(hint), ""])
        if not self.interactive:
            print(f"ws: {text}", file=sys.stderr)

    # the run --------------------------------------------------------------------------------------------------

    def run(self) -> int:
        if self.live:
            self._write(HIDE)
        try:
            return self._walk()
        except Cancelled:
            self.erase()
            self._write("\n" + self.a.dim(CANCELLED) + "\n")
            return 1
        except flow.ENGINE_ERRORS as e:
            self.erase()
            self.fail(str(e), "Nothing was written.")
            return 1
        finally:
            if self.live:
                self._write(SHOW)

    def _walk(self) -> int:
        done = self._welcome()
        if done is not None:
            return done
        self._core()
        while True:
            self.pick_plugins()
            self.ask_notes()
            self.ask_cap()
            try:
                pv = flow.preview(self.facts, self.choices)
            except flow.ENGINE_ERRORS as e:
                self.fail(str(e), "Pick the notes folder again.")
                if not self.interactive:
                    return 1
                continue
            if self.review(pv) == "install":
                break
        if not pv.changes:
            self.print(
                ["", done_line(self.a, "0 changes"), self.a.dim("  Nothing to do: everything already matches."), ""]
            )
            return 0
        return self.install(pv)

    # welcome ---------------------------------------------------------------------------------------------------

    def _welcome(self) -> int | None:
        a, facts = self.a, self.facts
        if self.live and not self.small:
            for cols in range(6, LOGO_WIDTH + 6, 6):
                self.draw(["", *a.logo(cols=min(cols, LOGO_WIDTH))])
                self.pause(0.035)
            self.erase()
        self.print(["", *banner(a, self.width, self.cols), ""])
        for line in status(a, facts, self.width):
            self.print([line])
            self.pause(0.06)
        if facts.set_up and not flow.preview(facts, self.choices).changes:
            return self._all_set()
        if not self.interactive:
            self.print([""])
            return None
        self.draw(["", hints(a, ("↵", "begin"), ("q", "quit"))])
        while True:
            k = self.key()
            if k in ("q", "esc"):
                raise Cancelled
            if k == "enter":
                break
        self.draw([""], final=True)
        return None

    def _all_set(self) -> int | None:
        """A machine that already matches: say so, and offer to change something. None means the person chose to."""
        a, facts, c = self.a, self.facts, self.choices
        names = [p.label for p in facts.plugins if p.name in c.plugins]
        on = f"core and {', '.join(names)} are on" if names else "core only are on"
        commands = flow.change_commands(facts, c)
        col = max(26, *(len(cmd) + 2 for cmd, _ in commands))
        rows = [a.mint("✓", bold=True) + " " + a.bold("0 changes") + "  " + a.dim(on), ""]
        rows += [a.violet(cmd.ljust(col), bold=True) + a.dim(what) for cmd, what in commands]
        frame = card(a, a.bold("Everything is set up"), "", rows, self.width)
        self.print([""])
        if not self.interactive:
            self.draw(frame, final=True)
            self.print([""])
            return 0
        hint = hints(a, ("↵", "finish"), ("c", "change something"), ("q", "quit"))
        while True:
            self.draw([*frame, "", hint])
            k = self.key()
            if k in ("enter", "q", "esc"):
                self.draw([*frame, ""], final=True)
                return 0
            if k == "c":
                self.erase()
                self.choices = flow.choices_from(
                    facts, plugins=c.plugins, notes=c.notes, runner=c.runner, cap=c.cap, region=c.region
                )
                return None

    # core -------------------------------------------------------------------------------------------------------

    def _core_card(self, reached: int) -> list[str]:
        a = self.a
        row = core_row(a, reached, self.width - 4)
        return card(a, a.bold("Core"), "always on", [row], self.width)

    def _core(self) -> None:
        self._core_mark = self._perm
        count = len(flow.CORE)
        if self.live:
            for reached in range(count + 1):
                self.draw(self._core_card(reached))
                self.pause(0.14)
        self.draw([*self._core_card(count), ""], final=True)

    # plugins ------------------------------------------------------------------------------------------------------

    def pick_plugins(self) -> None:
        a, facts = self.a, self.facts
        plugins = facts.plugins
        if not plugins:  # nothing built to choose: no card
            return
        picked = set(self.choices.plugins)
        cursor = next((i for i, p in enumerate(plugins) if p.available), 0)

        def frame(show_cursor: bool) -> list[str]:
            rows = plugin_rows(a, facts, picked, cursor, show_cursor, self.width - 4)
            if show_cursor and any(not p.built for p in plugins):
                rows.append(a.dim("  Planned: choices are only recorded for now."))
            return card(a, a.bold("Plugins"), f"{len(picked)} chosen", rows, self.width)

        if self.interactive and plugins:
            hint = hints(a, ("↑↓", "move"), ("space", "choose"), ("↵", "continue"), ("q", "cancel"))
            while True:
                self.draw([*frame(True), "", hint])
                k = self.key()
                if k in ("up", "k"):
                    cursor = (cursor - 1) % len(plugins)
                elif k in ("down", "j"):
                    cursor = (cursor + 1) % len(plugins)
                elif k == "space":
                    p = plugins[cursor]
                    if p.available:
                        picked ^= {p.name}
                elif k == "enter":
                    break
                elif k in ("q", "esc"):
                    raise Cancelled
        self.choices = replace(self.choices, plugins=frozenset(picked))
        self.draw([*frame(False), ""], final=True)

    # notes folder -----------------------------------------------------------------------------------------------

    def _notes_state(self, text: str) -> str:
        a = self.a
        app = flow.notes_app(self.facts, text) if text.strip() else "new folder"
        if app in ("Obsidian", "Logseq"):
            return a.mint("✓") + " " + a.dim(f"{app} vault found")
        return a.dim("new folder, will be made" if app == "new folder" else "your folder")

    def confirm(self, question: str) -> bool:
        a = self.a
        line = "  " + a.bold(question) + " [Y/n]"
        self.draw([line], final=not self.live)
        while True:
            k = self.key()
            if k in ("y", "enter"):
                answer = True
            elif k in ("n", "esc"):
                answer = False
            else:
                continue
            self.erase()
            return answer

    def ask_notes(self) -> None:
        a, facts = self.a, self.facts
        before = self.choices.notes
        text = before
        if self.interactive:
            hint = hints(a, ("type", "to change"), ("↑↓", "other vaults"), ("↵", "keep"))
            options: list[str] = []
            at = 0
            while True:
                self.draw([notes_line(a, text, True, self._notes_state(text)), "", hint])
                k = self.key()
                if k == "esc":
                    raise Cancelled
                if k == "enter":
                    text = text if text.strip() else before
                    if flow.notes_app(facts, text) == "new folder" and not self.confirm(
                        f"{flow.clean(text)} does not exist. Create it?"
                    ):
                        continue
                    break
                if k in ("up", "down"):
                    if not options:
                        seen: list[str] = []
                        for path in [text, *(flow.tilde(v.path, facts.env) for v in facts.vaults)]:
                            if path not in seen:
                                seen.append(path)
                        options, at = seen, 0
                    at = (at + (1 if k == "down" else -1)) % len(options)
                    text = options[at]
                    continue
                options = []
                if k == "backspace":
                    text = text[:-1]
                elif k == "space":
                    text += " "
                elif len(k) == 1 and k.isprintable():
                    text += k
        self.choices = replace(self.choices, notes=text)
        self.draw([notes_line(a, text, False, self._notes_state(text)), ""], final=True)

    # monthly cap ----------------------------------------------------------------------------------------------------

    def ask_cap(self) -> None:
        a = self.a
        if not (self.interactive and self.facts.cap is None and self.choices.cap is None):
            return
        label = a.bold("Monthly cap (US$)")
        text, error = "", ""
        while True:
            rows = [
                "  "
                + label
                + "  "
                + text
                + a.violet(CURSOR)
                + "  "
                + a.dim("optional: the most ws may spend in a month")
            ]
            if error:
                rows.append("  " + a.error(error))
            rows += ["", hints(a, ("type a number", ""), ("↵", "set" if text else "skip")).rstrip()]
            self.draw(rows)
            k = self.key()
            if k == "esc":
                raise Cancelled
            if k == "enter":
                try:
                    cap = flow.parse_cap(text)
                except InstallError as e:
                    error = str(e)
                    continue
                break
            error = ""
            if k == "backspace":
                text = text[:-1]
            elif len(k) == 1 and (k.isdecimal() and k.isascii() or k in ".$"):
                text += k
        self.choices = replace(self.choices, cap=cap)
        shown = f"US${cap:g} a month" if cap else "skipped"
        self.draw(["  " + label + "  " + shown, ""], final=True)

    # review ---------------------------------------------------------------------------------------------------------

    def _room(self) -> int:
        return max(3, min(MAX_LIST, self.lines - 18)) if self.live else MAX_LIST

    def _fold_cards(self, pv: flow.Preview) -> None:
        """When the screen is tall enough, shrink the cards so far to one line each, so the mark stays in view."""
        a, facts = self.a, self.facts
        used = self._perm - self._core_mark
        probe = review_rows(a, facts, self.choices, pv, details=True, room=self._room(), collapsed=True)
        if used + len(probe) + 5 > self.lines:
            return
        lines = [
            "  " + a.mint("✓") + " " + a.bold(label.ljust(8)) + a.dim(text)
            for label, text in choices_rows(a, facts, self.choices)
        ]
        self.erase_last(used)
        self.print([*lines, ""])
        self._fold = len(lines) + 1

    def _unfold(self) -> None:
        if self._fold:
            self.erase_last(self._fold)
            self._fold = 0
            self._core_mark = self._perm
            self.print([*self._core_card(len(flow.CORE)), ""])

    def review(self, pv: flow.Preview) -> str:
        """Show what will be written. Returns `install`, or `back` to ask the steps again."""
        a, facts = self.a, self.facts
        details = not self.interactive
        if self.live and self.interactive:
            self._fold_cards(pv)
        collapsed = self._fold > 0

        def frame() -> list[str]:
            rows = review_rows(a, facts, self.choices, pv, details=details, room=self._room(), collapsed=collapsed)
            return card(a, a.bold("Review"), "nothing is written until you press ↵", rows, self.width)

        if not self.interactive:
            self.draw([*frame(), ""], final=True)
            return "install"
        while True:
            pair = ("d", "hide list" if details else "details")
            self.draw([*frame(), "", hints(a, ("↵", "install"), pair, ("b", "back"), ("q", "cancel"))])
            k = self.key()
            if k == "enter":
                if self.live:
                    self.erase()
                else:
                    self.draw([*frame(), ""], final=True)
                return "install"
            if k == "d":
                details = not details
            elif k == "b":
                self.erase()
                self._unfold()
                return "back"
            elif k in ("q", "esc"):
                raise Cancelled

    # install ---------------------------------------------------------------------------------------------------------

    def install(self, pv: flow.Preview) -> int:
        a, facts = self.a, self.facts
        prog = flow.Progress(pv.steps)
        total = len(pv.steps)
        finished: set[str] = set()
        count = frame = 0

        def block(track: bool = True) -> list[str]:
            rows = install_rows(a, prog, finished, frame)
            lines = card(a, a.bold("Install"), "", rows, self.width)
            return [*lines, track_line(a, count, total)] if track else lines

        def on_step(step, action) -> None:
            nonlocal count, frame
            count += 1
            frame += 1
            if group := prog.step(step, action):
                for _ in range(2):  # the spinner turns twice before the tick lands
                    frame += 1
                    self.draw(block())
                    self.pause(0.09)
                finished.add(group)
            self.draw(block())

        self.draw(block())
        if self.terminal:
            self.terminal.hold = True
        try:
            outcome = flow.run_install(facts, self.choices, pv.steps, on_step)
        except (KeyboardInterrupt, Cancelled):
            self.draw(block(False), final=True)
            self.print(
                [
                    "",
                    "  " + a.amber("Stopped."),
                    a.dim("  What was written is recorded; run ws setup again to finish, or ws uninstall."),
                    "",
                ]
            )
            return 130
        except flow.ENGINE_ERRORS as e:
            self.draw(block(False), final=True)
            self.fail(str(e), "What was written is recorded; `ws uninstall` removes it.")
            return 1
        finally:
            if self.terminal:
                self.terminal.hold = False
        self.draw(block(False), final=True)
        result = outcome.result
        lines = ["", done_line(a, pv.headline() if result.changes else "0 changes")]
        lines.append(a.dim(f"  Saved in {flow.saved_in(facts.env)}. Run ws setup again any time to change it."))
        lines += [a.dim(f"  you changed it, so ws left it: {flow.tilde(p, facts.env)}") for p in result.edited]
        lines += [a.dim(f"  {note}") for note in outcome.notes]
        lines += ["", command_line(a, "Next", flow.next_commands(facts, self.choices)), ""]
        self.print(lines)
        return 130 if self.terminal and self.terminal.pending else 0


# ---- the entry point -----------------------------------------------------------------------------------------------------


def run(
    facts: flow.Facts, choices: flow.Choices, *, yes: bool = False, out=None, no_color: bool = False, env=None
) -> int:
    """Walk setup. With `yes`, take the defaults without asking; otherwise it needs a terminal to ask on.

    Colour follows `brand.color_mode`; a `TERM=dumb` terminal is asked the same questions but nothing is redrawn.
    """
    out = sys.stdout if out is None else out
    env = os.environ if env is None else env
    if not facts.osi.supported:
        raise WsError("unsupported", "native Windows is not supported", "install WSL2 and run ws inside it")
    tty = is_tty(out)
    mode = color_mode(env, tty, no_color=no_color)
    attended = sys.stdin is not None and sys.stdin.isatty() and tty
    if yes or not attended:
        if not yes:
            raise WsError("usage", "setup needs a terminal", "pass --yes to take the defaults, or use --web")
        wiz = Wizard(
            facts,
            choices,
            out,
            lambda: "enter",
            sleep=lambda _: None,
            cols=lambda: shutil.get_terminal_size().columns,
            color=mode,
            live=False,
            interactive=False,
        )
        return wiz.run()
    fd = sys.stdin.fileno()
    live = can_redraw(env, tty)
    try:
        with Terminal(fd, out, cursor=live) as terminal:
            wiz = Wizard(
                facts,
                choices,
                out,
                Keys(fd).read,
                cols=lambda: shutil.get_terminal_size().columns,
                color=mode,
                live=live,
                interactive=True,
                terminal=terminal,
            )
            return wiz.run()
    except KeyboardInterrupt:
        out.write(("\n" if not live else SHOW + "\n") + Ansi(mode).dim(CANCELLED) + "\n")
        out.flush()
        return 130
