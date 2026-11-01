"""Sessions: what is working, what is idle, and what finished today, as rows that open in place.

Each row says what the session is doing, where it runs (workspace, machine, tmux pane, runner) and how old that is.
Opening a row shows its folder, runner, tokens and cost (`not measured` until it is) and the ways to open it. Idle
sessions are folded into one line. An opaque session shows its folder label and tmux target, says to open the session to
see more, and its tokens and cost read `hidden`. A session that has an ask is drawn as the ask, under Needs you.
"""

from __future__ import annotations

from ws.home.areas import _fmt, _rows
from ws.home.areas._base import View
from ws.ui import components

ID = "sessions"
ORDER = 3
TITLE = "Sessions"
READS = "sessions: the records of every session, by what they are doing"
EMPTY = "No sessions found on this machine yet."
OPAQUE = "Name, folder, tokens and cost are hidden; the line says to open the session to see this."
ACTIONS = "Open the session where it lives (the opener the profile picks)."
BUILT = True

DONE_SHOWN = 8  # a day of finished jobs can run to hundreds; the rest is a count

WORDS = {
    "working": "Working",
    "idle": "Waiting for your next message",
    "ended": "Finished",
    "unknown": "State not known",
}


def name_of(s) -> str:
    if s.opaque:
        return "Hidden session"
    return s.name or s.id[:8]


def what_of(s) -> str:
    """What the session is doing, in a few words."""
    if s.opaque:
        return _fmt.OPAQUE_LINE
    if s.state == "waiting":
        return f"Waiting for: {s.waiting_for}" if s.waiting_for else "Waiting for you"
    return WORDS.get(s.state, WORDS["unknown"])


def _kv(label: str, value: str) -> str:
    return f"<dt>{_fmt.esc(label)}</dt><dd>{_fmt.esc(value)}</dd>"


def _body(view: View, s) -> str:
    opens = _fmt.open_cell(view, s, "session")
    if s.opaque:
        return f'<p class="dec">{_fmt.OPAQUE_LINE}</p>{opens}'
    started = _fmt.age(s.started_at, view.snap.ts)
    rows = [
        _kv("Folder", s.cwd) if s.cwd else "",
        _kv("Runner", s.runner) if s.runner else "",
        _kv("Started", started) if started else "",
        _kv("Tokens", _fmt.tokens(s)),
        _kv("Cost", _fmt.cost(s)),
    ]
    return f'<dl class="kv">{"".join(rows)}</dl>{opens}'


def session_row(view: View, s, bucket: str, *, pin: bool = False) -> str:
    """One session as a row of the list."""
    if s.opaque:
        meta = [_fmt.esc(s.repo), _fmt.esc(s.tmux)]
        tag = '<span class="tagc">terminal only</span>'
    else:
        meta = [
            _fmt.esc(s.repo),
            _fmt.esc(_rows.where_of(view, s.host)),
            _fmt.esc(s.tmux),
            _rows.runner_chip(s.runner),
        ]
        tag = ""
    return _rows.row(
        view,
        bucket=bucket,
        name=name_of(s),
        what=what_of(s),
        meta=meta,
        age=_fmt.age_short(s.last_seen, view.snap.ts),
        body=_body(view, s),
        ws=s.repo,
        host=s.host,
        runner="" if s.opaque else s.runner,
        cls=view.hub.classes.get(s.repo, ""),
        pin=pin,
        tag=tag,
    )


def render(view: View) -> str:
    snap = view.snap
    if not snap.sessions and snap.lead is None:
        return components.empty(EMPTY)
    b = _rows.buckets(snap)
    out = [
        _rows.group("work", [session_row(view, s, "work") for s in b.work], empty="Nothing is working right now."),
    ]
    if b.idle:
        out.append(_rows.group("idle", [session_row(view, s, "idle") for s in b.idle], fold=True))
    if b.done:
        shown = [session_row(view, s, "done") for s in b.done[:DONE_SHOWN]]
        more = len(b.done) - len(shown)
        out.append(_rows.group("done", shown, count=len(b.done), more=f"and {more} more" if more > 0 else ""))
    return "".join(out)
