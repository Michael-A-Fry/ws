"""The shell around the list: the sidebar of places, the bar above the column, the View and Settings menus, and the pages
that say what is coming.

Places are plain `#v-name` links; the page shows the one that is targeted (a rule in `_style`), so they work from a file
and on a phone with no script. Home and Evals are drawn. The others each say in a line what will be there.
"""

from __future__ import annotations

from ws.home.areas import _fmt, _rows
from ws.home.areas._base import View
from ws.ui import components

# (id, label, icon, what it will hold). The places that are not drawn yet.
COMING = (
    ("search", "Search", "search", "Search every live and ended session by name, folder or question."),
    ("scheduled", "Scheduled", "clock", "Your jobs with their next run and last result, and a way to add one."),
    ("history", "History", "hist", "Ended sessions with their transcript and output, and what's left to tidy."),
    ("skills", "Skills", "book", "The skills ws installs, each on or off, with Add."),
    ("plugins", "Plugins", "blocks", "The plugins ws can run, on or off, and Add from the ws list."),
    ("connections", "Connections", "plug", "Models, tools (MCP) and machines in one list, with one Add button."),
)
WORKSPACES_SHOWN = 8  # the sidebar lists the busiest; the box lists them all
GROUPS = (("need", "Need"), ("workspace", "Workspace"), ("model", "Model"), ("machine", "Machine"))
SHOW = (
    ("pinned", "Pinned"),
    ("done", "Done today"),
    ("quiet", "Scheduled, evals and tidy line"),
    ("model", "Runner on each row"),
)


def _nav(icon: str, label: str, href: str, extra: str = "", attrs: str = "") -> str:
    return f'<a class="nav" href="{href}"{attrs}>{components.icon(icon)}{_fmt.esc(label)}{extra}</a>'


def _seg(store: str, key: str, options: tuple, on: str) -> str:
    buttons = "".join(
        f'<button type="button" data-v="{v}"{" class=on" if v == on else ""}>{_fmt.esc(t)}</button>' for v, t in options
    )
    return f'<div class="seg" data-store="{store}" data-k="{key}">{buttons}</div>'


def sidebar(view: View) -> str:
    snap, hub = view.snap, view.hub
    need = _rows.buckets(snap).need
    plugins_on = sum(1 for p in snap.plugins if p.on)
    count = f'<span class="n num" aria-label="{need} need you">{need}</span>' if need else ""
    sub = {"plugins": f'<span class="sub num">{plugins_on}</span>' if plugins_on else ""}
    coming = {c[0]: c for c in COMING}

    def place(key: str) -> str:
        _, label, icon, _ = coming[key]
        return _nav(icon, label, f"#v-{key}", sub.get(key, ""))

    workspaces = "".join(
        f'<button type="button" class="nav" data-ws="{_fmt.esc(w["name"])}">{components.icon("lock" if w["cls"] == "terminal only" else "folder")}'
        f'{_fmt.esc(w["name"])}<span class="sub">{_fmt.esc(w["cls"])}</span></button>'
        for w in hub.workspaces[:WORKSPACES_SHOWN]
    )
    sources_ok = sum(1 for r in snap.sources if r.ok)
    return (
        '<aside class="side" id="side">'
        f'<div class="sbrand">{components.mark()}<b>home</b></div>'
        f'<a class="nav new" href="#v-home" data-act="new">{components.icon("new")}New session</a>'
        + place("search")
        + _nav("home", "Home", "#v-home", count, ' aria-current="page"')
        + place("scheduled")
        + place("history")
        + "<h6>Customise</h6>"
        + place("skills")
        + place("plugins")
        + place("connections")
        + _nav("gauge", "Evals", "#v-evals")
        + '<h6>Views</h6><div id="viewlist"><button type="button" class="nav on" data-view="">'
        f"{components.icon('layers')}Everything, by need</button></div>"
        + (f'<h6>Workspaces</h6><div id="wslist">{workspaces}</div>' if workspaces else "")
        + '<div class="foot"><span class="avatar" aria-hidden="true">ws</span>'
        f'<span class="who">{_fmt.esc(snap.host or _rows.THIS_MACHINE)}<span>{sources_ok} of {len(snap.sources)} sources read</span></span>'
        f'<button type="button" class="nav" data-pop="setpop" aria-label="Settings" aria-expanded="false">{components.icon("gear")}</button></div>'
        "</aside>"
    )


def topbar(view: View) -> str:
    hosts = view.hub.machines
    machines = ""
    if len(hosts) > 1:
        opts = '<option value="">All machines</option>' + "".join(
            f'<option value="{_fmt.esc(_rows.where_of(view, h))}">{_fmt.esc(_rows.where_of(view, h))}</option>'
            for h in hosts
        )
        machines = (
            f'<label class="pill only-home">{components.icon("mon")}<select id="machine" aria-label="Machine">{opts}</select>'
            f"{components.icon('chev')}</label>"
        )
    return (
        '<div class="topbar">'
        f'<button type="button" class="pill menu" data-act="drawer" aria-label="Menu">{components.icon("menu")}</button>'
        f"{machines}"
        '<button type="button" class="pill only-home" data-pop="viewpop" aria-expanded="false">'
        f'{components.icon("sliders")}<span id="viewlbl">View: by need</span>{components.icon("chev")}</button>'
        + view_menu()
        + "</div>"
    )


def view_menu() -> str:
    checks = "".join(
        f'<button type="button" class="chk" role="checkbox" aria-checked="true" data-s="{k}">'
        f'<span class="box">{components.icon("check")}</span>{_fmt.esc(t)}</button>'
        for k, t in SHOW
    )
    return (
        '<div class="pop" id="viewpop" hidden>'
        f"<div><h6>Group by</h6>{_seg('view', 'group', GROUPS, 'need')}</div>"
        f"<div><h6>Density</h6>{_seg('view', 'density', (('comfy', 'Comfortable'), ('compact', 'Compact')), 'comfy')}</div>"
        f"<div><h6>Show on home</h6>{checks}</div>"
        '<div class="popfoot"><input type="text" id="viewname" maxlength="40" placeholder="Name this view" '
        'aria-label="Name this view"><button type="button" class="btn" data-act="save-view">Save as view</button></div>'
        '<p class="more">Saved on this browser for now.</p></div>'
    )


def settings_menu(view: View) -> str:
    swatches = "".join(
        f'<button type="button" class="c-{k[0]}" data-v="{k}" aria-label="{k.capitalize()}"></button>'
        for k in ("violet", "indigo", "teal")
    )
    return (
        '<div class="pop set" id="setpop" hidden>'
        f"<div><h6>Theme</h6>{_seg('look', 'theme', (('system', 'System'), ('light', 'Light'), ('dark', 'Dark')), 'system')}</div>"
        f'<div><h6>Accent</h6><div class="sw" data-store="look" data-k="accent">{swatches}</div></div>'
        f"<div><h6>List font</h6>{_seg('look', 'font', (('sans', 'Sans'), ('mono', 'Mono')), 'sans')}</div>"
        f"<div><h6>Text size</h6>{_seg('look', 'size', (('s', 'S'), ('m', 'M'), ('l', 'L')), 'm')}</div>"
        '<p class="more">Mint (done) and amber (needs you) stay fixed, so their meaning never moves. '
        "Saved on this browser for now.</p>"
        f'<a href="#v-sources">What ws read: {sum(1 for r in view.snap.sources if r.ok)} of {len(view.snap.sources)} sources ›</a>'
        "</div>"
    )


def coming_pages() -> str:
    return "".join(
        f'<section class="pg" id="v-{key}"><h2 class="vh2">{_fmt.esc(label)}</h2>'
        f'<div class="soon"><b>Coming next</b><span>{_fmt.esc(what)}</span>'
        '<a class="back" href="#v-home">Back to Home</a></div></section>'
        for key, label, _, what in COMING
    )
