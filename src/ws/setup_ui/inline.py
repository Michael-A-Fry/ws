"""`ws setup` inline: the mark, a status block of real facts, then the steps one after another in your own terminal.

Plain ANSI and termios, no dependencies, like the Claude Code start-up: it stays in your scrollback instead of taking
over the screen. The layout (a block-letter mark over a dim-label status block, then box-drawn cards with ticks and a
spinner) follows the shape of claw-code's start-up banner and tool cards (MIT licence,
github.com/ultraworkers/claw-code). The colours, the mark and the words are ws's own. Every look rule is in the Visual
kit in `ws.setup_ui`; every file written goes through `ws.setup_ui.flow` and so `ws.install`.
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
from collections.abc import Callable
from dataclasses import replace
from typing import IO, Self

from ws import __version__, install
from ws.install import InstallError
from ws.setup_ui import flow
from ws.setup_ui.brand import (
    AMBER,
    ANSI_RE,
    LOGO_WIDTH,
    SPINNER,
    TAGLINE,
    Ansi,
    visible_len,
)

KEYS = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1bOA": "up", b"\x1bOB": "down"}
HIDE, SHOW = "\x1b[?25l", "\x1b[?25h"
SMALL = 80  # under this many columns the mark folds to three rows and the tagline goes
MAX_LIST = 8  # rows of the file list shown in the review before "and N more"


class Cancelled(Exception):
    """The person pressed q, Esc or Ctrl-C before anything was written."""


# ---- drawing: pure functions from facts to lines -------------------------------------------------------------------


def fit(text: str, room: int) -> str:
    """Cut to `room` columns. Colour is dropped from a cut line, which only happens on a very narrow terminal."""
    if visible_len(text) <= room:
        return text
    return ANSI_RE.sub("", text)[: max(0, room - 1)] + "…"


def card(a: Ansi, title: str, right: str, rows: list[str], width: int) -> list[str]:
    """`╭─ Title ───── right ─╮`, the rows, then the foot."""
    inner = width - 4
    plain_right = f" {right} " if right else ""
    fill = max(1, width - 5 - visible_len(title) - len(plain_right) - 1)
    head = a.grey("╭─ ") + title + a.grey(" " + "─" * fill) + (a.dim(plain_right) if right else "") + a.grey("─╮")
    out = [head]
    for r in rows:
        r = fit(r, inner)
        out.append(a.grey("│ ") + r + " " * (inner - visible_len(r)) + a.grey(" │"))
    out.append(a.grey("╰" + "─" * (width - 2) + "╯"))
    return out


def banner(a: Ansi, width: int, cols: int = 100) -> list[str]:
    """The mark with its name beside it and the gradient rule under it. Under SMALL columns: the small mark, no tagline."""
    if cols < SMALL:
        logo = a.logo(small=True)
        logo[0] += "   " + a.bold("ws-aitoolkit") + a.dim(f"  v{__version__}")
        logo[1] += "   " + a.mint("setup", bold=True)
        return [*logo, a.rule(width)]
    logo = a.logo()
    logo[1] += "   " + a.bold("ws-aitoolkit") + a.dim(f"  v{__version__}")
    logo[2] += "   " + a.dim(TAGLINE)
    logo[4] += "   " + a.mint("setup", bold=True)
    return [*logo, a.rule(width)]


def status(a: Ansi, facts: flow.Facts) -> list[str]:
    """The dim-label, bright-value block, from the real machine."""
    out = []
    for row in flow.status_rows(facts):
        value = row.value
        if row.tone == "ok":
            value = f"{a.mint('✓')} {value}"
        elif row.tone == "warn":
            value = a.fg(AMBER, value)
        if row.label == "Budget" and facts.cap and facts.spent:
            value += "  " + a.track(round(facts.spent), round(facts.cap), 20)
        out.append(f"  {a.dim(f'{row.label:<9}')}{value}")
    return out


def hints(a: Ansi, *pairs: tuple[str, str]) -> str:
    """`↵ begin · q quit`: the keys bold in violet, what they do quiet."""
    return "  " + a.dim(" · ").join(f"{a.violet(k, bold=True)} {a.dim(v)}" for k, v in pairs)


def core_row(a: Ansi, ticked: int) -> str:
    items = [
        f"{a.mint('✓', bold=True) if i < ticked else a.dim('·')} {a.bold(name)}"
        for i, (_, name, _) in enumerate(flow.CORE)
    ]
    return "   ".join(items)


def plugin_rows(
    a: Ansi, facts: flow.Facts, cursor: int | None, picked: set[str], inner: int, note: bool = True
) -> list[str]:
    rows = []
    for i, p in enumerate(facts.plugins):
        here = cursor == i
        arrow = a.violet("❯", bold=True) if here else " "
        if p.available:
            mark = a.violet("◉", bold=True) if p.name in picked else a.grey("○")
            name = a.violet(f"{p.label:<16}", bold=True) if here else a.bold(f"{p.label:<16}")
            line = p.line
        else:
            mark, name, line = a.dim("–"), a.dim(f"{p.label:<16}"), "Not available in your region"
        left = f"{arrow} {mark} {name} {a.dim(line)}"
        right = []
        if p.available and p.name in facts.base.modules:
            right.append(a.dim("on now"))
        if p.tag and p.available:
            right.append(a.grey(f"[{p.tag}]"))
        if right:
            tag = " ".join(right)
            left += " " * max(1, inner - visible_len(left) - visible_len(tag)) + tag
        rows.append(left)
    if note and any(not p.built for p in facts.plugins):
        rows.append(a.dim("  Planned: a choice is recorded for now and changes nothing else."))
    return rows


def notes_line(a: Ansi, facts: flow.Facts, text: str, editing: bool) -> str:
    cursor = a.violet("▏") if editing else ""
    app = flow.notes_app(facts, text)
    found = (
        f"{a.mint('✓')} {a.dim(app + ' vault found')}"
        if app in ("Obsidian", "Logseq")
        else a.dim("new folder, will be made" if app == "new folder" else "your folder")
    )
    return f"  {a.violet('Notes folder', bold=True)}  {a.bold(text)}{cursor}   {found}"


def choices_rows(facts: flow.Facts, c: flow.Choices) -> list[tuple[str, str]]:
    """What was chosen so far, one (label, value) per step."""
    names = [p.label for p in facts.plugins if p.name in c.plugins]
    return [
        ("Core", " · ".join(name for _, name, _ in flow.CORE)),
        ("Plugins", " · ".join(names) if names else "none yet (ws plugin add later)"),
        ("Notes", flow.tilde(install.expand(c.notes, facts.env), facts.env)),
        *([("Cap", f"US${c.cap:g} a month")] if c.cap else []),
    ]


def review_rows(
    a: Ansi, facts: flow.Facts, c: flow.Choices, pv: flow.Preview, details: bool, room: int, collapsed: bool = False
) -> list[str]:
    runner = next((r for r in facts.runners if r.name == c.runner), None)
    rows = [] if collapsed else choices_rows(facts, c)  # once the steps are folded to one line each, they are above
    rows += [
        ("Runner", runner.label if runner else "none yet (the gate files wait for one)"),
        ("Writes", pv.headline()),
    ]
    out = [f"{a.dim(f'{k:<9}')}{v}" for k, v in rows[:-1]]
    out.append(f"{a.dim(f'{rows[-1][0]:<9}')}{a.mint(rows[-1][1], bold=True)}")
    if details:
        out.append("")
        shown = [r for r in pv.rows if r["action"] != "keep" and r["kind"] == "file"]  # the folders are in the count
        for r in shown[:room]:
            sign = a.mint("+") if r["action"] == "create" else a.fg(AMBER, "~")
            what = re.sub(r"\s*\(.*\)\s*$", "", r["what"] or r["kind"])  # a label, without its own brackets
            out.append(f"{sign} {r['path']}  {a.dim(what)}")
        if len(shown) > room:
            out.append(a.dim(f"… and {len(shown) - room} more"))
    return out


def install_rows(a: Ansi, names: list[str], detail: dict[str, str], done: int, tick: int) -> list[str]:
    rows = []
    for i, name in enumerate(names):
        if i < done:
            rows.append(
                f"{a.mint('✓', bold=True)} {a.bold(f'{name:<20}')} {a.mint(detail.get(name, 'kept as it was'))}"
            )
        elif i == done:
            rows.append(
                f"{a.violet(SPINNER[tick % len(SPINNER)], bold=True)} {a.bold(f'{name:<20}')} {a.dim('writing…')}"
            )
        else:
            rows.append(f"{a.dim('·')} {a.dim(f'{name:<20}')}")
    return rows


def track_line(a: Ansi, done: int, total: int, n: int = 30) -> str:
    done = min(done, total)
    return "  " + a.track(done, total, n) + a.dim(f"  {done} of {total} steps")


def command_line(a: Ansi, label: str, cmds: list[str]) -> str:
    return "  " + a.dim(f"{label}  ") + a.dim("  ·  ").join(a.violet(c, bold=True) for c in cmds)


def done_line(a: Ansi, headline: str) -> str:
    return f"  {a.mint('✓ Done', bold=True)}  {a.bold(headline)}"


# ---- the walk -------------------------------------------------------------------------------------------------------


class Wizard:
    def __init__(
        self,
        facts: flow.Facts,
        choices: flow.Choices,
        out: IO[str],
        key: Callable[[], str],
        *,
        sleep: Callable[[float], None] = time.sleep,
        cols: int | Callable[[], int] = 100,
        color: bool = True,
        live: bool = True,
        interactive: bool = True,
        terminal: Terminal | None = None,
    ) -> None:
        self.facts, self.c, self.out, self.key, self.sleep = facts, choices, out, key, sleep
        self._cols, self.live, self.interactive = cols, live, interactive
        self.terminal = terminal
        self.a = Ansi(color)
        self.shown = 0  # lines of the live block currently on screen
        self.out_lines = 0  # lines printed so far that stay on the screen

    @property
    def cols(self) -> int:
        """The terminal's width now: read again each time, so a resize is picked up by the next thing drawn."""
        return self._cols() if callable(self._cols) else self._cols

    @property
    def width(self) -> int:
        return max(56, min(self.cols - 4, 100))

    # -- output
    def w(self, text: str = "") -> None:
        self.out.write(text)
        self.out.flush()

    def print(self, lines: list[str]) -> None:
        self.w("".join(f"{ln}\n" for ln in lines))
        self.shown = 0
        self.out_lines += len(lines)

    def draw(self, lines: list[str], final: bool = False) -> None:
        """Show a block that changes: in place on a live terminal, once at the end otherwise."""
        if not self.live:
            if final:
                self.print(lines)
            return
        up = f"\x1b[{self.shown}A\r" if self.shown else ""
        self.w(up + "".join(f"\x1b[2K{ln}\n" for ln in lines) + "\x1b[0J")  # 0J clears what a longer block left
        self.shown = len(lines) if not final else 0
        if final:
            self.out_lines += len(lines)

    def erase(self) -> None:
        """Remove the live block from the screen, so the next thing prints where it was."""
        if self.live and self.shown:
            self.w(f"\x1b[{self.shown}A\r\x1b[0J")
        self.shown = 0

    def erase_last(self, n: int) -> None:
        if self.live:
            self.w(f"\x1b[{n}A\r\x1b[0J")
            self.out_lines -= n

    def pause(self, seconds: float) -> None:
        if self.live:
            self.sleep(seconds)

    # -- the whole thing
    def run(self) -> int:
        a = self.a
        self.w(HIDE if self.live else "")
        try:
            return self._walk()
        except Cancelled:
            self.w("\n" + a.dim("  Cancelled. Nothing was written.") + "\n")
            return 1
        except flow.ENGINE_ERRORS as e:
            self.fail(str(e), "Nothing was written.")
            return 1
        finally:
            self.w(SHOW if self.live else "")

    def _walk(self) -> int:
        a, facts = self.a, self.facts
        again = self.welcome()
        if again == "finish":
            return 0
        first = self.out_lines
        self.core()
        while True:
            self.pick_plugins()
            self.ask_notes()
            self.ask_cap()
            used = self.out_lines - first
            try:
                pv = flow.preview(facts, self.c)
            except flow.ENGINE_ERRORS as e:
                self.fail(str(e), "Pick the notes folder again.")
                if not self.interactive:
                    return 1
                continue
            go = self.review(pv, used)
            if go == "back":
                first = self.out_lines
                continue
            break
        if not pv.changes:
            self.print(["", done_line(a, "0 changes"), a.dim("  Nothing to do: everything already matches."), ""])
            return 0
        return self.install(pv)

    # -- steps
    def welcome(self) -> str:
        a, facts = self.a, self.facts
        small = self.cols < SMALL
        if self.live and not small:  # the mark sweeps in from the left
            for cols in range(6, LOGO_WIDTH + 6, 6):
                self.draw(a.logo(cols), final=cols >= LOGO_WIDTH)
                self.pause(0.035)
            self.erase_last(len(a.logo()))
        self.print(banner(a, self.width, self.cols))
        for row in status(a, facts):
            self.print([row])
            self.pause(0.06)
        if facts.set_up:
            pv = flow.preview(facts, self.c)
            if not pv.changes:
                return self.all_set(pv)
        begin = hints(a, ("↵", "begin"), ("q", "quit"))
        self.draw(["", begin, ""])
        if self.interactive:
            self.wait_enter()
        self.erase()
        self.print([""])
        return "go"

    def all_set(self, pv: flow.Preview) -> str:
        """Setup is already done: say so, list what to change, and offer to change it."""
        a, facts, c = self.a, self.facts, self.c
        names = [p.label for p in facts.plugins if p.name in c.plugins]
        on = "core" + (f" and {', '.join(names)}" if names else " only")
        rows = [f"{a.mint('✓', bold=True)} {a.bold('0 changes')}  {a.dim(on + ' are on')}", ""]
        rows += [f"{a.violet(f'{cmd:<26}', bold=True)}{a.dim(what)}" for cmd, what in flow.change_commands(facts, c)]
        shown = card(a, a.violet("Everything is set up", bold=True), "", rows, self.width)
        self.print(["", *shown])
        if not self.interactive:
            self.print([""])
            return "finish"
        self.draw(["", hints(a, ("↵", "finish"), ("c", "change something"), ("q", "quit")), ""])
        while True:
            k = self.key()
            if k == "enter" or k in ("q", "esc"):
                self.erase()
                return "finish"
            if k == "c":
                self.erase()
                self.erase_last(len(shown) + 1)  # the card goes too, so two cards do not compete
                self.print([""])
                self.c = flow.choices_from(facts, plugins=c.plugins, notes=c.notes, runner=c.runner, cap=c.cap)
                return "go"

    def wait_enter(self) -> None:
        while True:
            k = self.key()
            if k == "enter":
                return
            if k in ("q", "esc"):
                raise Cancelled

    def core(self) -> None:
        a = self.a
        title = a.violet("Core", bold=True)
        for n in range(len(flow.CORE) + 1):
            self.draw(card(a, title, "always on", [core_row(a, n)], self.width), final=n == len(flow.CORE))
            self.pause(0.14)

    def pick_plugins(self) -> None:
        a, facts = self.a, self.facts
        picked = set(self.c.plugins)
        usable = [i for i, p in enumerate(facts.plugins) if p.available]
        cursor = usable[0] if usable else 0
        title = a.violet("Plugins", bold=True)
        keys = hints(a, ("↑↓", "move"), ("space", "choose"), ("↵", "continue"), ("q", "cancel"))
        inner = self.width - 4
        while self.interactive:
            rows = plugin_rows(a, facts, cursor, picked, inner)
            self.draw([*card(a, title, f"{len(picked)} chosen", rows, self.width), keys])
            k = self.key()
            if k in ("up", "k"):
                cursor = (cursor - 1) % len(facts.plugins)
            elif k in ("down", "j"):
                cursor = (cursor + 1) % len(facts.plugins)
            elif k == "space":
                p = facts.plugins[cursor]
                if p.available:
                    picked ^= {p.name}
            elif k == "enter":
                break
            elif k in ("q", "esc"):
                raise Cancelled
        self.c = replace(self.c, plugins=frozenset(picked))
        self.draw(
            card(a, title, f"{len(picked)} chosen", plugin_rows(a, facts, None, picked, inner, note=False), self.width),
            final=True,
        )

    def ask_notes(self) -> None:
        a, facts = self.a, self.facts
        text = self.c.notes
        options = [
            text,
            *dict.fromkeys(
                flow.tilde(v.path, facts.env) for v in facts.vaults if flow.tilde(v.path, facts.env) != text
            ),
        ]
        pos = 0
        while self.interactive:
            self.draw(
                [
                    notes_line(a, facts, text, True),
                    hints(a, ("type", "to change"), ("↑↓", "other vaults"), ("↵", "keep")),
                ]
            )
            k = self.key()
            if k == "enter":
                if flow.notes_app(facts, text) == "new folder" and not self.confirm(
                    f"{text} does not exist. Create it?"
                ):
                    continue
                break
            if k == "esc":
                raise Cancelled
            if k in ("up", "down"):
                pos = (pos + (1 if k == "down" else -1)) % len(options)
                text = options[pos]
            elif k == "backspace":
                text = text[:-1]
            elif len(k) == 1 and k.isprintable():
                text += k
        text = text or self.c.notes
        self.c = replace(self.c, notes=text)
        self.draw([notes_line(a, facts, text, False)], final=True)
        self.print([""])

    def ask_cap(self) -> None:
        """An optional monthly cap, asked only when the profile has none and none was given. Enter skips it."""
        a = self.a
        if not self.interactive or self.facts.cap is not None or self.c.cap is not None:
            return
        text, err, value = "", "", None
        label = a.violet("Monthly cap (US$)", bold=True)
        while True:
            line = f"  {label}  {a.bold(text)}{a.violet('▏')}   {a.dim('optional: the most ws may spend in a month')}"
            rows = [line, hints(a, ("type", "a number"), ("↵", "set" if text else "skip"))]
            self.draw(rows + ([f"  {a.fg(AMBER, err)}"] if err else []))
            k = self.key()
            if k == "enter":
                try:
                    value = flow.parse_cap(text)
                except InstallError as e:
                    err = str(e)
                    continue
                break
            if k == "esc":
                raise Cancelled
            err = ""
            if k == "backspace":
                text = text[:-1]
            elif len(k) == 1 and (k.isdigit() or k in ".$"):
                text += k
        self.c = replace(self.c, cap=value)
        done = f"US${value:g} a month" if value else "skipped"
        self.draw([f"  {label}  {a.bold(done)}"], final=True)
        self.print([""])

    def confirm(self, question: str) -> bool:
        a = self.a
        self.draw([f"  {a.bold(question)} {a.dim('[Y/n]')}"])
        while True:
            k = self.key()
            if k in ("y", "enter"):
                return True
            if k in ("n", "esc"):
                return False

    def collapse(self, used: int) -> list[str]:
        """Fold the Core, Plugins and Notes cards into one line each, so the mark and the status block stay on screen
        while the review and the install run. Only on a live terminal tall enough to move back up over them."""
        a = self.a
        if not self.live or used <= 0 or self.out_lines + 6 > shutil.get_terminal_size().lines:
            return []
        self.erase_last(used)
        rows = [
            f"  {a.mint('✓', bold=True)} {a.violet(f'{k:<8}', bold=True)}{v}"
            for k, v in choices_rows(self.facts, self.c)
        ]
        rows.append("")
        self.print(rows)
        return rows

    def review(self, pv: flow.Preview, used: int = 0) -> str:
        a, facts = self.a, self.facts
        title = a.violet("Review", bold=True)
        folded = self.collapse(used)
        details = not self.interactive  # a log keeps the list; a terminal shows it when asked
        while True:
            room = max(3, min(MAX_LIST, (shutil.get_terminal_size().lines - 18) if self.live else MAX_LIST))
            rows = review_rows(a, facts, self.c, pv, details, room, collapsed=bool(folded))
            right = "nothing is written until you press ↵"
            keys = hints(
                a, ("↵", "install"), ("d", "hide list" if details else "details"), ("b", "back"), ("q", "cancel")
            )
            block = [*card(a, title, right, rows, self.width), *([keys] if self.interactive else [])]
            self.draw(block)
            if not self.interactive:
                self.print(block)
                return "go"
            k = self.key()
            if k == "enter":
                self.erase()  # the review makes way for the install, which draws where it was
                if not self.live:  # a log has no "where it was": keep the review
                    self.print(block[: -1 if self.interactive else None])
                return "go"
            if k == "d":
                details = not details
            elif k == "b":
                self.erase()
                if folded:
                    self.erase_last(len(folded))
                return "back"
            elif k in ("q", "esc"):
                raise Cancelled

    def install(self, pv: flow.Preview) -> int:
        a, facts = self.a, self.facts
        prog = flow.Progress(pv.steps)
        names = prog.names
        state = {"done": 0, "tick": 0}
        title = a.violet("Install", bold=True)

        def block() -> list[str]:
            detail = {n: prog.detail(n) for n in names}
            rows = install_rows(a, names, detail, state["done"], state["tick"])
            return [*card(a, title, "", rows, self.width), track_line(a, state["done"], len(names))]

        def on_step(step: install.Step, action: str) -> None:
            state["tick"] += 1
            if prog.step(step, action):  # a group is finished: let the spinner turn a little, then the tick lands
                for _ in range(2):
                    self.draw(block())
                    state["tick"] += 1
                    self.pause(0.09)
                state["done"] += 1
            self.draw(block())

        self.draw(block())
        if self.terminal:
            self.terminal.hold = True  # from here a SIGTERM is only noted: the summary below says what was written
        try:
            outcome = flow.run_install(facts, self.c, pv.steps, on_step)
        except (KeyboardInterrupt, Cancelled):
            self.draw(block(), final=True)
            self.print(
                [
                    "",
                    a.fg(AMBER, "  Stopped."),
                    a.dim("  What was written is recorded; run ws setup again to finish, or ws uninstall."),
                    "",
                ]
            )
            return 130
        except flow.ENGINE_ERRORS as e:
            self.draw(block(), final=True)
            self.fail(str(e), "What was written is recorded; `ws uninstall` removes it.")
            return 1
        state["done"] = len(names)
        self.draw(block()[:-1], final=True)  # the track has done its job
        res = outcome.result
        headline = pv.headline() if res.changes else "0 changes"
        lines = ["", done_line(a, headline)]
        lines += [a.dim(f"  Saved in {flow.saved_in(facts.env)}. Run ws setup again any time to change it.")]
        lines += [a.dim(f"  you changed it, so ws left it: {flow.tilde(p, facts.env)}") for p in res.edited]
        lines += [a.dim(f"  {n}") for n in outcome.notes]
        lines += ["", command_line(a, "Next", flow.next_commands(facts, self.c)), ""]
        self.print(lines)
        if self.terminal and self.terminal.pending:  # asked to stop while it was writing: it finished, and said so
            return 130
        return 0

    def fail(self, message: str, hint: str) -> None:
        a, message = self.a, flow.clean(message)
        self.print(["", f"  {a.fg(AMBER, '✗', bold=True)} {a.bold(message)}", a.dim(f"  {hint}"), ""])
        if not self.interactive:  # a script reads stderr
            print(f"ws: {message}", file=sys.stderr)


# ---- the terminal -------------------------------------------------------------------------------------------------


class Terminal:
    """Single-key input and a hidden cursor while ws asks things, put back on every way out: a normal finish, Ctrl-C,
    SIGTERM and SIGHUP (both raise Cancelled, which writes nothing before Install), and Ctrl-Z (the screen is restored and
    the shell gets control; when ws is continued it takes the terminal again). termios is imported only here, so native
    Windows, which has none, still loads `ws --help`."""

    def __init__(self, fd: int, out: IO[str]) -> None:
        import termios

        self.fd, self.out = fd, out
        self.old = termios.tcgetattr(fd)
        self._saved: dict[int, object] = {}
        self.hold = False  # set while the install writes: a SIGTERM is then recorded, not raised
        self.pending = False

    def enter(self) -> None:
        import tty

        tty.setcbreak(self.fd)
        self.out.write(HIDE)
        self.out.flush()

    def leave(self) -> None:
        import termios

        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.old)
        self.out.write(SHOW)
        self.out.flush()

    def _on_stop(self, signum, frame) -> None:  # Ctrl-Z
        self.leave()
        signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        os.kill(os.getpid(), signal.SIGTSTP)

    def _on_cont(self, signum, frame) -> None:  # fg: take the terminal again
        signal.signal(signal.SIGTSTP, self._on_stop)
        self.enter()

    def _on_end(self, signum, frame) -> None:  # SIGTERM, SIGHUP
        if self.hold:  # a handler must not re-enter a half-finished write to the screen, nor stop the install half way
            self.pending = True
            return
        raise Cancelled

    def __enter__(self) -> Self:
        self.enter()
        if threading.current_thread() is threading.main_thread():
            for name, handler in (
                ("SIGTERM", self._on_end),
                ("SIGHUP", self._on_end),
                ("SIGTSTP", self._on_stop),
                ("SIGCONT", self._on_cont),
            ):
                sig = getattr(signal, name, None)
                if sig is not None:
                    self._saved[sig] = signal.signal(sig, handler)
        return self

    def __exit__(self, *exc) -> None:
        for sig, old in self._saved.items():
            signal.signal(sig, old)
        self.leave()


class Keys:
    """Reads one key at a time from a terminal, even when several arrive together (a held arrow, a paste). Arrows and
    other escape sequences are read whole; one that ws has no use for (a function key, the mouse) is `other`, which
    no step acts on, so it never cancels anything."""

    def __init__(self, fd: int) -> None:
        self.fd, self.buf = fd, b""

    def _fill(self, wait: float | None = None) -> bool:
        if wait is not None and not select.select([self.fd], [], [], wait)[0]:
            return False
        data = os.read(self.fd, 1024)
        if not data:
            raise Cancelled
        self.buf += data
        return True

    def read(self) -> str:
        if not self.buf:
            self._fill()
        c = self.buf[0]
        if c == 0x1B:
            if len(self.buf) == 1:
                self._fill(0.05)  # a lone Esc, or the start of an arrow: give the rest a moment to arrive
            if len(self.buf) >= 2 and self.buf[1] in (ord("["), ord("O")):
                end = (
                    3
                    if self.buf[1] == ord("O")
                    else next((i + 1 for i in range(2, len(self.buf)) if 0x40 <= self.buf[i] <= 0x7E), 0)
                )
                if not end or end > len(self.buf):
                    self.buf = b""  # a sequence cut short: drop it
                    return "other"
                seq, self.buf = self.buf[:end], self.buf[end:]
                return KEYS.get(seq, "other")
            self.buf = self.buf[1:]
            return "esc"
        if c in (0x0D, 0x0A):
            self.buf = self.buf[1:]
            return "enter"
        self.buf = self.buf[1:]
        if c == 0x20:
            return "space"
        if c in (0x7F, 0x08):
            return "backspace"
        if c == 0x03:
            raise KeyboardInterrupt
        need = 0 if c < 0xC0 else 1 if c < 0xE0 else 2 if c < 0xF0 else 3
        while len(self.buf) < need:
            self._fill()
        char, self.buf = bytes([c]) + self.buf[:need], self.buf[need:]
        return char.decode("utf-8", errors="ignore") or "other"


def wants_color(out: IO[str], env=None) -> bool:
    env = os.environ if env is None else env
    return out.isatty() and "NO_COLOR" not in env and env.get("TERM") != "dumb"


def run(facts: flow.Facts, choices: flow.Choices, *, yes: bool = False, out: IO[str] | None = None) -> int:
    """Walk the steps. Without a terminal (or with --yes) the defaults and the flags are taken and nothing is asked."""
    out = out or sys.stdout
    color = wants_color(out, facts.env)
    if not facts.osi.supported:
        print("ws: native Windows is not supported; install WSL2 and run ws inside it", file=sys.stderr)
        return 1
    if yes or not (sys.stdin.isatty() and out.isatty()):
        if not yes:
            print("ws: setup needs a terminal; pass --yes to take the defaults, or use --web", file=sys.stderr)
            return 1
        wiz = Wizard(
            facts, choices, out, lambda: "enter", sleep=lambda _: None, cols=shutil.get_terminal_size().columns,
            color=color, live=False, interactive=False,
        )  # fmt: skip
        return wiz.run()
    fd = sys.stdin.fileno()
    with Terminal(fd, out) as term:
        wiz = Wizard(
            facts,
            choices,
            out,
            Keys(fd).read,
            cols=lambda: shutil.get_terminal_size().columns,
            color=color,
            terminal=term,
        )
        try:
            return wiz.run()
        except KeyboardInterrupt:
            out.write(SHOW + "\n" + Ansi(color).dim("  Cancelled. Nothing was written.") + "\n")
            return 130
