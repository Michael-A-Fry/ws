"""The rows of the home list: which bucket each record falls in, and one row drawn the same way whatever it is.

A row is a `<details>`: the summary is what you scan (a status glyph, a name and what it is doing, then where it runs and
how old it is) and the body is what opens in place (an answer for an ask, the ways to open a session). The summary
carries `data-` attributes for the workspace, machine and runner so the page's script can regroup the list without asking
the server. Everything that came from a record goes through `html.escape`.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ws.home.areas import _fmt
from ws.home.areas._base import View
from ws.home.records import Snapshot
from ws.ui import components

DONE_WINDOW_S = 24 * 3600  # "done today": ended within the last 24 hours
THIS_MACHINE = "this machine"

# bucket -> (components status state, the word that goes with the glyph)
BUCKETS = {
    "need": ("waiting", "needs you"),
    "work": ("working", "working"),
    "idle": ("idle", "idle"),
    "done": ("ok", "done"),
}
TITLES = {"pin": "Pinned", "need": "Needs you", "work": "Working", "idle": "Idle", "done": "Done today"}


@dataclass(frozen=True)
class Buckets:
    """The snapshot sorted into the groups of the list. `covered` is the sessions that already show as an ask."""

    pin: list = field(default_factory=list)
    need_asks: list = field(default_factory=list)
    need_sessions: list = field(default_factory=list)
    work: list = field(default_factory=list)
    idle: list = field(default_factory=list)
    done: list = field(default_factory=list)
    jobs: list = field(default_factory=list)  # (job, [runs, newest first]) for each headless job that ran today
    ended: int = 0  # every ended session with a window, done today or not

    @property
    def need(self) -> int:
        return len(self.need_asks) + len(self.need_sessions)

    @property
    def done_today(self) -> int:
        """The count line's "done today": sessions, and each job as one."""
        return len(self.done) + len(self.jobs)


def _asked(snap: Snapshot):
    ids = {a.session_id for a in snap.asks if a.session_id}
    panes = {a.tmux for a in snap.asks if a.tmux}
    return lambda s: s.id in ids or bool(s.tmux and s.tmux in panes)


def is_run(s) -> bool:
    """A headless run: a session that ended and never had a terminal pane. Runs are folded by job, not listed one by one.
    An opaque session is never folded, because the name that would group it is one it hides."""
    return s.state == "ended" and not s.tmux and not s.opaque and not s.lead


def job_of(s) -> str:
    """The job a run belongs to: its name when the record has one, else the folder it ran in."""
    return s.name or s.repo or "unnamed job"


def buckets(snap: Snapshot) -> Buckets:
    """Sort a snapshot. The lead is pinned. A session that has an ask shows as the ask. Waiting with no ask is still
    Needs you. Ended sessions are Done today when they ended in the last 24 hours; the headless runs among them are
    gathered into one entry per job."""
    asked = _asked(snap)
    pin, need, work, idle, done, ended = [], [], [], [], [], 0
    runs: dict[str, list] = {}
    for s in snap.sessions:
        if s.state == "ended" and s.tmux:
            ended += 1
        if s.lead:
            pin.append(s)
        elif asked(s):
            continue
        elif s.state == "waiting":
            need.append(s)
        elif s.state == "working":
            work.append(s)
        elif s.state == "ended":
            if _fmt.within(s.last_seen, snap.ts, DONE_WINDOW_S):
                (runs.setdefault(job_of(s), []) if is_run(s) else done).append(s)
        else:
            idle.append(s)
    jobs = [(j, sorted(r, key=lambda x: x.last_seen or "", reverse=True)) for j, r in runs.items()]
    jobs.sort(key=lambda j: j[1][0].last_seen or "", reverse=True)
    return Buckets(pin, list(snap.asks), need, work, idle, done, jobs, ended)


def row_key(key: str) -> str:
    """A record id as an element id: anything that isn't a letter, digit, dash or underscore becomes a dash."""
    return "".join(c if c.isascii() and (c.isalnum() or c in "-_") else "-" for c in key)


def row_id(key: str) -> str:
    """The ` id="r-..."` attribute of a row, or nothing without a key."""
    return f' id="r-{row_key(key)}"' if key else ""


def where_of(view: View, host: str) -> str:
    """The machine as a row says it: `this machine` for the one the snapshot came from."""
    return THIS_MACHINE if not host or host == view.snap.host else host


def row(
    view: View,
    *,
    bucket: str,
    name: str,
    what: str,
    meta: list[str],
    age: str,
    body: str,
    ws: str = "",
    host: str = "",
    runner: str = "",
    cls: str = "",
    pin: bool = False,
    open_: bool = False,
    tag: str = "",
    key: str = "",
) -> str:
    """One row. `meta` is the muted line as HTML (escape each piece: `_fmt.esc`, or `runner_chip`); `body` is the HTML
    that opens in place. `name`, `what`, `ws`, `host`, `runner`, `cls` and `age` are text and are escaped here. `key`
    (a session or ask id) makes the row addressable as `#r-<key>`, which the phone drawer's session lines link to."""
    state, word = BUCKETS[bucket]
    attrs = (
        f' data-need="{bucket}" data-ws="{_fmt.esc(ws)}" data-machine="{_fmt.esc(where_of(view, host))}"'
        f' data-model="{_fmt.esc(runner)}" data-cls="{_fmt.esc(cls)}"' + (' data-pin="1"' if pin else "")
    )
    sep = '<span class="sep">·</span>'
    state_word = f'<span class="sw">{_fmt.esc(word)}{sep}</span>'  # shown only when the list isn't grouped by need
    pieces = [m for m in meta if m]  # a phone keeps the first two (workspace, machine) and drops the rest
    line = state_word + "".join(
        f'<span class="mp{" x" if i >= 2 else ""}">{sep if i else ""}{m}</span>' for i, m in enumerate(pieces)
    )
    if tag:
        line += f'<span class="mp">{sep if pieces else ""}{tag}</span>'
    return (
        f'<details class="r {bucket}"{row_id(key)}{attrs}{" open" if open_ else ""}>'
        '<summary data-action="row.toggle" data-expect="toggle:row">'
        f'<span class="rg">{components.status(state, word)}</span>'
        f'<span class="rt"><span class="l1"><b class="nm">{_fmt.esc(name)}</b><span class="wh">{_fmt.esc(what)}</span></span>'
        f'<span class="l2">{line}</span></span><span class="age num">{_fmt.esc(age)}</span></summary>'
        f'<div class="rb">{body}</div></details>'
    )


def group(
    key: str, rows: list[str], *, fold: bool = False, empty: str = "", count: int | None = None, more: str = ""
) -> str:
    """A titled group of rows. `fold` makes it a closed `<details>`; `empty` is what shows when there are no rows;
    `count` is the group's size when `rows` holds only the first few, and `more` the line that says so, a link to
    History."""
    title = TITLES[key]
    size = f'<em class="num">{len(rows) if count is None else count}</em>'
    body = "".join(rows) or (f'<p class="more">{_fmt.esc(empty)}</p>' if empty else "")
    if more:
        body += (
            '<p class="more"><a href="#v-history" data-action="nav.history" data-expect="navigate:#v-history">'
            f"{_fmt.esc(more)} ›</a></p>"
        )
    if fold:
        return (
            f'<details class="grp" data-grp="{key}" id="g-{key}">'
            f'<summary data-action="group.toggle" data-expect="toggle:#g-{key}">{title} {size}</summary>{body}</details>'
        )
    return f'<section class="grp" data-grp="{key}"><h5>{title} {size}</h5>{body}</section>'


def runner_chip(runner: str) -> str:
    """The runner on a row's muted line; hidden when the person turns `model on each row` off."""
    return f'<span class="mdl">{_fmt.esc(runner)}</span>' if runner else ""
