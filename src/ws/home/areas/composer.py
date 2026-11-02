"""The box: describe a task, ask a question or paste a link, then four pickers say what it is, where it runs and with what.

The pickers are filled from real data: what (Session, Chat or Job), the workspace (the folders sessions were seen in and
any the profile names, each with its data class), where (this machine and every host seen) and the runner and model
(the installed runners and the models the profile offers, grouped by runner). A model the chosen workspace's class rules
out is shown greyed with the reason, so the policy is never a surprise. Sending posts to the page's server, which runs the
profile's `[home.start] command`; with none set, or with no server behind the page, the button is off and says why.
"""

from __future__ import annotations

from ws.home.areas import _fmt
from ws.home.areas._base import Hub, View
from ws.ui import components

OPAQUE_CLASS = (
    "terminal only"  # as `ws.home.hub.OPAQUE_CLASS`; areas read nothing but records and the hub they're given
)
ID = "composer"
ORDER = 0
TITLE = "Start"
READS = "the hub: workspaces, machines, runners and models from the profile and the records"
EMPTY = "No workspace to start in yet: run a session once, or list one under [home.workspaces]."
OPAQUE = "A workspace that only opaque sessions were seen in is listed by its folder label and marked terminal only."
ACTIONS = "Start a session, a chat or a job from the box; the profile's [home.start] command does the starting."
BUILT = True

SET_START = "Set [home.start] command to start sessions from here"
NOT_LIVE = "This copy of the page is read-only: run ws home to start sessions"
WHAT = (("session", "Session", "term"), ("chat", "Chat", "new"), ("job", "Job", "clock"))
PLACEHOLDER = "Describe a task, ask a question, or paste a link…"
REASON = "not for {cls} data (your policy)"
REASON_OPAQUE = "terminal only: start it from a terminal"


def _pick(icon: str, select_id: str, label: str, options: str, solo: bool = False) -> str:
    """One picker: an icon and a native `<select>`. `solo` marks one with a single choice. A phone shows each picker as
    a labelled line in the sheet the `+` opens."""
    return (
        f'<label class="pick{" solo" if solo else ""}" title="{_fmt.esc(label)}">{components.icon(icon)}'
        f'<span class="pl">{_fmt.esc(label)}</span>'
        f'<select id="{select_id}" aria-label="{_fmt.esc(label)}" data-action="composer.pick-{select_id[2:]}" '
        f'data-expect="select:#{select_id}">{options}</select>{components.icon("chev", "chev")}</label>'
    )


def _what() -> str:
    return "".join(f'<option value="{v}">{t}</option>' for v, t, _ in WHAT)


def _workspaces(hub: Hub) -> str:
    if not hub.workspaces:
        return '<option value="" disabled selected>No workspace yet</option>'
    out = []
    for w in hub.workspaces:
        label = f"{w['name']} · {w['cls']}" if w["cls"] else w["name"]
        out.append(f'<option value="{_fmt.esc(w["key"])}" data-cls="{_fmt.esc(w["cls"])}">{_fmt.esc(label)}</option>')
    return "".join(out)


def _where(view: View) -> str:
    hosts = view.hub.machines or (view.snap.host or "this machine",)
    out = []
    for i, h in enumerate(hosts):
        label = "This machine" if i == 0 else h
        out.append(f'<option value="{_fmt.esc(h)}">{_fmt.esc(label)}</option>')
    return "".join(out)


def _models(hub: Hub) -> str:
    """Runner and model, one optgroup per runner. The first workspace's class decides what starts greyed out; the page's
    script redoes this when the workspace changes."""
    cls = hub.workspaces[0]["cls"] if hub.workspaces else ""
    groups = []
    for r in hub.runners:
        opts = []
        for m in r["models"]:
            label = m["name"] or "default model"
            full = f"{r['name']} · {label}"
            closed = cls == OPAQUE_CLASS  # a terminal-only folder takes no runner from here
            denied = closed or (bool(cls) and cls in m["deny"])
            shown = f"{full} · {REASON_OPAQUE if closed else REASON.format(cls=cls)}" if denied else full
            opts.append(
                f'<option value="{_fmt.esc(r["name"])}|{_fmt.esc(m["name"])}" data-label="{_fmt.esc(full)}" '
                f'data-deny="{_fmt.esc(",".join(m["deny"]))}"{" disabled" if denied else ""}>{_fmt.esc(shown)}</option>'
            )
        groups.append(f'<optgroup label="{_fmt.esc(r["name"])}">{"".join(opts)}</optgroup>')
    return "".join(groups) or '<option value="" disabled selected>No runner installed</option>'


def _note(view: View) -> tuple[str, bool]:
    """The line under the box when sending is off, and whether sending is on. With a server behind the page and
    `[home.start]` set the line is empty."""
    if not view.hub.start:
        return SET_START, False
    if not view.hub.workspaces:
        return EMPTY, False
    return ("", True) if view.live else (NOT_LIVE, False)


def render(view: View) -> str:
    note, on = _note(view)
    return (
        '<p class="hello" id="hello">What\'s next?</p>'
        '<div class="composer" id="composer">'
        '<button type="button" class="plus" aria-label="Choose what, where and with which model" aria-expanded="false" '
        f'data-act="sheet" data-action="composer.sheet" data-expect="toggle:#sheet">{components.icon("plus")}</button>'
        f'<textarea id="c-text" rows="2" maxlength="4000" placeholder="{PLACEHOLDER}" aria-label="Your request" '
        'data-action="composer.text" data-expect="type:#c-text"></textarea>'
        '<div class="crow">'
        '<div class="picks" id="sheet" role="dialog" aria-label="Start with"><div class="shead">'
        '<button type="button" class="xbtn" aria-label="Close" data-act="sheet" data-action="composer.close" '
        f'data-expect="toggle:#sheet">{components.icon("x")}</button><b>Start with</b></div>'
        + _pick("term", "c-what", "What", _what())
        + _pick("folder", "c-ws", "Workspace", _workspaces(view.hub))
        + _pick("mon", "c-where", "Where", _where(view), solo=len(view.hub.machines) < 2)
        + "</div>"
        + _pick("chip", "c-runner", "Runner and model", _models(view.hub))
        + f'<button type="button" class="send" id="c-send" aria-label="Start"{"" if on else " disabled"} '
        f'data-action="composer.send" data-expect="post:/api/start">{components.icon("up")}</button>'
        "</div>"
        f'<p class="cnote" id="c-note" role="status">{_fmt.esc(note)}</p>'
        '<div class="scrim sscrim" data-act="sheet" data-action="composer.close" data-expect="toggle:#sheet"></div></div>'
    )
