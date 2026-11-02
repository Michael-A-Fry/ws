"""Sessions: what is working, what is idle, and what finished today, as rows that open in place.

Each row says what the session is doing, where it runs (workspace, machine, tmux pane, runner) and how old that is.
Opening a row shows its folder, pane, runner, tokens and cost (`not measured` until it is) and the ways to open it.
Idle sessions and Done today are folded. Headless runs are one row per job (`triage-replay · 158 runs today`), so one
busy job can't bury the rest; the API gives no pass or fail for a run, so the row counts runs and no more. An opaque session shows its folder label and tmux target, says to open the session to
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

DONE_SHOWN = 5  # a day of finished work can run long; the rest is a link to History

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
        _kv("Machine", _rows.where_of(view, s.host)),
        _kv("Pane", s.tmux) if s.tmux else "",
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
        key=s.id,
    )


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def job_row(view: View, job: str, runs: list) -> str:
    """One headless job as a row: how many runs today and when the newest finished."""
    newest = runs[0]
    cost = sum(r.cost_usd for r in runs if r.cost_usd is not None)
    known = sum(1 for r in runs if r.cost_usd is not None)
    rows = [
        _kv("Runs today", str(len(runs))),
        _kv("Newest", _fmt.age(newest.last_seen, view.snap.ts)),
        _kv("Folder", newest.cwd) if newest.cwd else "",
        _kv("Cost", f"US${cost:.2f} ({known} of {len(runs)} measured)" if known else "not measured"),
    ]
    body = (
        f'<dl class="kv">{"".join(rows)}</dl>'
        '<a class="act" href="#v-history" data-action="nav.history" data-expect="navigate:#v-history">See in History</a>'
    )
    return _rows.row(
        view,
        bucket="done",
        name=job,
        what=_plural(len(runs), "run") + " today",
        meta=[_fmt.esc(newest.repo), _fmt.esc(_rows.where_of(view, newest.host)), _rows.runner_chip(newest.runner)],
        age=_fmt.age_short(newest.last_seen, view.snap.ts),
        body=body,
        ws=newest.repo,
        host=newest.host,
        runner=newest.runner,
        cls=view.hub.classes.get(newest.repo, ""),
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
    if b.done_today:
        entries = [(s.last_seen or "", session_row(view, s, "done")) for s in b.done]
        entries += [(runs[0].last_seen or "", job_row(view, job, runs)) for job, runs in b.jobs]
        entries.sort(key=lambda e: e[0], reverse=True)
        shown = [html for _, html in entries[:DONE_SHOWN]]
        more = len(entries) - len(shown)
        out.append(
            _rows.group("done", shown, fold=True, count=len(entries), more=f"and {more} more" if more > 0 else "")
        )
    return "".join(out)
