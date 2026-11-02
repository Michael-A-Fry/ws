"""Every control on the home page says what it does: `data-action="<area>.<verb>"` and `data-expect` naming its effect, so
a walk of the page can click each one and check the result. The page is drawn live, with every kind of row, so a control
that only shows in one state is covered too."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from ws.home import areas, page
from ws.home.records import Ask, Session, Snapshot, SourceRow

TS = "2026-11-02T12:00:00.000Z"
TERM = {"opener": "terminal", "label": "Switch to it", "post": True, "text": "tmux switch-client -t ws:1"}
APP = {"opener": "claude_app", "label": "Open in Claude", "href": "https://claude.ai/code/x"}
FORMS = {"a", "button", "select", "input", "textarea", "summary"}


class Controls(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.found: list[tuple[str, dict]] = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in FORMS or a.get("role") == "button":
            if tag == "a" and "href" not in a:
                return
            self.found.append((tag, a))


def _page(**kw) -> str:
    sessions = (
        Session(id="w", source="s", name="working one", state="working", tmux="w:@1", repo="app", opens=(TERM, APP), last_seen=TS),
        Session(id="i", source="s", name="idle one", state="idle", tmux="w:@2", repo="app", last_seen=TS),
        Session(id="n", source="s", name="needs one", state="waiting", tmux="w:@3", repo="app", last_seen=TS),
        Session(id="d", source="s", name="done one", state="ended", tmux="w:@4", repo="app", last_seen=TS),
        *(Session(id=f"r{i}", source="s", name="nightly", state="ended", repo="jobs", last_seen=TS) for i in range(3)),
        Session(id="L", source="s", name="Home lead", state="idle", tmux="w:@5", repo="code", lead=True, last_seen=TS),
    )  # fmt: skip
    asks = (
        Ask(id="q1", source="asks_jsonl", kind="question", title="Pick one", session_name="app", repo="app",
            options=({"label": "Tidy", "description": "small"}, {"label": "Rewrite", "description": ""}),
            created=TS, opens=(TERM,)),
        Ask(id="p1", source="s", kind="permission", title="Run it?", repo="app", created=TS),
    )  # fmt: skip
    snap = Snapshot(
        ts=TS, host="here", sessions=sessions, asks=asks, lead=sessions[-1],
        sources=(SourceRow(name="ws_runs", ok=True, n=3),),
    )  # fmt: skip
    hub = areas.Hub(
        start=True, answer_set=True, answerable=frozenset({"q1"}),
        workspaces=({"key": "/x/app", "label": "app", "name": "app", "cls": "financial"},
                    {"key": "/x/code", "label": "code", "name": "code", "cls": ""}), machines=("here", "far"),
        runners=({"name": "claude-code", "models": ({"name": "opus", "deny": ()},)},), classes={"app": "financial"},
    )  # fmt: skip
    return page.render(snap, "abc123xyz", live=kw.get("live", True), hub=hub)


def test_every_control_on_the_page_has_an_action_and_an_expected_effect():
    for live in (True, False):
        parser = Controls()
        parser.feed(_page(live=live))
        assert len(parser.found) > 60  # the sidebar, the menus, the box and every kind of row
        bare = [(t, a) for t, a in parser.found if not a.get("data-action") or not a.get("data-expect")]
        assert bare == []
        for _, a in parser.found:
            assert re.fullmatch(r"[a-z]+\.[a-z0-9-]+", a["data-action"]), a["data-action"]


def test_a_phone_control_that_a_desktop_hides_still_says_what_it_does():
    parser = Controls()
    parser.feed(_page())
    actions = {a["data-action"] for _, a in parser.found}
    assert {"composer.sheet", "composer.close", "drawer.toggle", "drawer.session", "nav.new-phone"} <= actions
    assert {"ask.choose", "ask.send", "ask.undo", "ask.dismiss", "open.run", "open.copy", "open.link"} <= actions
    assert {"row.toggle", "group.toggle", "view.open", "settings.open", "composer.send", "nav.history"} <= actions


def test_the_drawer_lists_sessions_in_need_order_with_the_phone_only_controls_marked():
    out = _page()
    side = out.split('<aside class="side"')[1].split("</aside>")[0]
    names = re.findall(r'<span class="sn">([^<]*)</span>', side)
    assert names.index("app") < names.index("working one") < names.index("idle one")  # needs you, working, idle
    assert 'href="#r-q1"' in side and 'id="r-q1"' in out  # a line points at its row
    assert 'class="rbtn ph"' in side and "nav new ph" in side  # Search and New session for the phone
    assert 'class="nav new dk"' in side  # and the desktop's own New session
