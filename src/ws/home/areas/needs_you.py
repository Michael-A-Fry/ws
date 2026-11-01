"""Needs you: everything waiting for a person, oldest first, with the question on the row.

A row opens in place. A question the page can answer shows what it decides, the recommended option first and the only
one filled, a box for anything else and `Not now`; choosing leaves a stamp with a few seconds to undo before anything is
sent. Everything else (a permission, an approval, a waiting session, any question the page isn't set up to answer) shows
what it asks and where it is answered, and offers `Open`. An opaque item shows only its kind, folder label and tmux
target, says to open the session to see it, and is offered only an opener that is safe for that.
"""

from __future__ import annotations

from ws.home.areas import _fmt, _rows
from ws.home.areas._base import View
from ws.home.areas.sessions import session_row

ID = "needs_you"
ORDER = 2
TITLE = "Needs you"
READS = "asks: what is waiting for the person, oldest first"
EMPTY = "Nothing is waiting for you."
OPAQUE = "Only the kind, folder label and tmux target show; the line says to open the session to see this."
ACTIONS = "Answer a question on its row; open anything else where it is answered."
BUILT = True

KINDS = {
    "question": "Question",
    "permission": "Permission",
    "approval": "Approval",
    "waiting": "Waiting",
    "draft": "Draft",
    "plugin": "Plugin",
}
OPTIONS_SHOWN = 4
SET_ANSWER = "Set [home.answer] command to answer here"


def _options_list(ask) -> str:
    """The options as plain text, the recommended one first (a question that can't be answered on the page)."""
    items = []
    for i, o in enumerate(ask.options[:OPTIONS_SHOWN]):
        tag = '<span class="rec-tag">recommended</span>' if i == 0 and len(ask.options) > 1 else ""
        desc = f'<span class="d">{_fmt.esc(o.get("description", ""))}</span>' if o.get("description") else ""
        items.append(f"<li><b>{_fmt.esc(o.get('label', ''))}</b>{tag}{desc}</li>")
    return f'<ul class="opts">{"".join(items)}</ul>' if items else ""


def _form(ask) -> str:
    """The answer, drawn: option buttons (the first filled and tagged), a box for anything else, Not now."""
    buttons = []
    for i, o in enumerate(ask.options[:OPTIONS_SHOWN]):
        rec = i == 0 and len(ask.options) > 1
        tag = "<small>Recommended</small>" if rec else ""
        desc = f"<span>{_fmt.esc(o.get('description', ''))}</span>" if o.get("description") else ""
        buttons.append(
            f'<button type="button" class="opt{" rec" if rec else ""}" data-choice="{_fmt.esc(o.get("label", ""))}">'
            f"<b>{_fmt.esc(o.get('label', ''))}{tag}</b>{desc}</button>"
        )
    return (
        f'<div class="opts2">{"".join(buttons)}</div>'
        '<div class="else"><input type="text" maxlength="2000" placeholder="Something else…" '
        'aria-label="Something else"><button type="button" data-act="send-text">Send</button></div>'
    )


def _body(view: View, ask, can_answer: bool) -> str:
    opens = _fmt.open_cell(view, ask, "ask")
    if ask.opaque:
        return f'<p class="dec">{_fmt.OPAQUE_LINE}</p>{opens}'
    decides = f'<p class="dec">Decides: {_fmt.esc(ask.decision)}</p>' if ask.decision else ""
    title = f'<p class="q">{_fmt.esc(ask.title or "No title given")}</p>'
    if can_answer:
        return (
            f'<div class="ask" data-ask="{_fmt.esc(ask.id)}">{title}{decides}{_form(ask)}'
            '<div class="stamp" hidden><span class="ok" aria-hidden="true">✓</span><span class="msg"></span>'
            '<button type="button" data-act="undo">Undo</button></div>'
            f'<div class="links"><button type="button" data-act="not-now">Not now</button>{opens}</div></div>'
        )
    where_in = "Answer in the app" if ask.answer_in == "app" else "Answer in a terminal"
    hint = ""
    if view.live and ask.kind == "question" and not view.hub.answer_set:
        hint = f'<p class="dec">{SET_ANSWER}</p>'
    return (
        f'<div class="ask">{title}{decides}{_options_list(ask)}'
        f'<p class="dec">{where_in} · from {_fmt.esc(ask.source)}</p>{hint}{opens}</div>'
    )


def _ask_row(view: View, ask, sessions, *, open_: bool) -> str:
    sess = sessions.get(ask.session_id) or sessions.get(ask.tmux)
    can_answer = view.live and ask.id in view.hub.answerable
    kind = KINDS.get(ask.kind, ask.kind.capitalize())
    if ask.opaque:
        name, what, meta = ask.repo or "Hidden session", _fmt.OPAQUE_LINE, [_fmt.esc(ask.tmux)]
        tag = '<span class="tagc">terminal only</span>'
        ws, host, runner = ask.repo, "", ""
    else:
        name = ask.session_name or (sess.name if sess else "") or ask.repo or kind
        what = ask.title or f"{kind}: no title given"
        host = sess.host if sess else ""
        runner = sess.runner if sess else ""
        ws = ask.repo or (sess.repo if sess else "")
        meta = [_fmt.esc(ws), _fmt.esc(_rows.where_of(view, host)), _fmt.esc(ask.tmux), _rows.runner_chip(runner)]
        tag = f'<span class="chip">{_fmt.esc(kind)}</span>' if ask.kind != "question" else ""
    return _rows.row(
        view,
        bucket="need",
        name=name,
        what=what,
        meta=meta,
        age=_fmt.age_short(ask.created, view.snap.ts),
        body=_body(view, ask, can_answer),
        ws=ws,
        host=host,
        runner=runner,
        cls=view.hub.classes.get(ws, ""),
        open_=open_,
        tag=tag,
    )


def render(view: View) -> str:
    b = _rows.buckets(view.snap)
    by = {}
    for s in view.snap.sessions:
        by[s.id] = s
        if s.tmux:
            by.setdefault(s.tmux, s)
    first_open = next((a.id for a in b.need_asks if view.live and a.id in view.hub.answerable), "")
    rows = [_ask_row(view, a, by, open_=a.id == first_open) for a in b.need_asks]
    rows += [session_row(view, s, "need") for s in b.need_sessions]
    return _rows.group("need", rows, empty=EMPTY)
