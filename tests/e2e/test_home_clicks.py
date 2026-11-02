"""The click-walk for the home page: every control, in every state it can be reached, is clicked in a real browser.

`ws home` is served on loopback from a fixture snapshot made at run time (sessions in every state, answerable and opaque
asks, an approval, a permission, a job with runs, eval arms and a pinned lead). The profile's `[home.start]`,
`[home.answer]` and `[home.open]` name a recorder script that appends its argv to a file, so a POST is checked by what
the program was given, and nothing real is dispatched or answered.

The walker finds every element with `data-action` in each reachable state (the page with its rows open, the View menu,
Settings, the phone drawer, the phone sheet, the answer form, a saved view and each place), at 1440x900 and 390x844, in
light and dark. It does what the control's `data-expect` says and checks that effect. The test fails if a control's
`data-expect` has no handler, if a handler's check fails, or if a `data-action` seen in the page was never exercised.

Needs Playwright and a Chromium (neither is a ws dependency; the CI job installs them in a venv of their own). Without
them the test skips, unless WS_E2E_REQUIRE=1 (CI), when it fails. Run it with `-s` to see the coverage table.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import stat
import sys
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field, replace
from pathlib import Path

import pytest

from ws.home import api, hub, page
from ws.home import open as opener
from ws.home.areas import _fmt, _rows
from ws.home.records import Ask, EvalArm, Plugin, Session, Snapshot, SourceRow
from ws.home.server import HomeServer
from ws.ui.server import LoopbackServer

REQUIRE = os.environ.get("WS_E2E_REQUIRE") == "1"

NOW = 1793620800.0  # 2026-11-02T12:00:00Z
TS = "2026-11-02T12:00:00.000Z"
UNDO_TEST_MS = 500  # the page waits 6 s before it sends an answer; the browser is told to wait this long instead
VIEWPORTS = {"1440x900": (1440, 900), "390x844": (390, 844)}
SCHEMES = ("light", "dark")
WORDS = "hold; rm -rf / && $(x) `y` 'q' \"z\""  # shell words: they must arrive as one argument
PLACES = ("evals", "sources", "search", "scheduled", "history", "skills", "plugins", "connections")
# the effects the walker has a handler for; a `data-expect` outside this list fails the test
KINDS = (
    "navigate", "toggle", "set", "filter", "focus", "type", "select", "add", "post", "cancel", "copy", "none",
    "close-drawer", "open-sheet",
)  # fmt: skip

# --- the page's test hooks: a shorter undo window, reloads counted instead of done, no timed refresh ------------------

HOOKS = """
(() => {
  const st = window.setTimeout.bind(window), si = window.setInterval.bind(window);
  window.__reloads = 0;
  try {  // the first load of a state starts from no saved choices (saved views stay); a reload inside the state keeps them
    if (!sessionStorage.getItem('__walk')) {
      sessionStorage.setItem('__walk', '1');
      ['ws-home-look', 'ws-home-view', 'ws-home-active', 'ws-home-picks'].forEach((k) => localStorage.removeItem(k));
    }
  } catch (_) {}
  window.setTimeout = (fn, ms, ...a) => {
    if (typeof fn === 'function' && fn.toString().replace(/\\s/g, '') === '()=>location.reload()') { window.__reloads++; return 0; }
    return st(fn, ms === 6000 ? __UNDO__ : ms, ...a);
  };
  window.setInterval = (fn, ms, ...a) => (ms === 45000 ? 0 : si(fn, ms, ...a));
})();
""".replace("__UNDO__", str(UNDO_TEST_MS))

SCAN = """
([scope, exclude]) => {
  window.__wk = window.__wk || 0;
  const count = {}, out = [], all = [];
  document.querySelectorAll('[data-action]').forEach((el) => {
    const a = el.dataset.action, n = (count[a] = (count[a] === undefined ? 0 : count[a] + 1));
    all.push([a, el.dataset.expect || '']);
    if (scope && !el.closest(scope)) return;
    if (exclude && el.closest(exclude)) return;
    if (!el.dataset.wk) el.dataset.wk = String(++window.__wk);
    const r = el.getBoundingClientRect();
    const vis = el.checkVisibility({ checkVisibilityCSS: true }) && r.width > 0 && r.height > 0 && r.right > 0 && r.left < innerWidth;
    out.push({ wk: el.dataset.wk, key: a + '#' + n, action: a, expect: el.dataset.expect || '', tag: el.tagName.toLowerCase(), vis });
  });
  return { out, all };
}
"""

ON_SCREEN = """
(sel) => {
  const el = document.querySelector(sel); if (!el) return false;
  const r = el.getBoundingClientRect();
  return el.checkVisibility({ checkVisibilityCSS: true }) && r.width > 0 && r.height > 0 && r.right > 0 && r.left < innerWidth;
}
"""

FREE_POINT = """
(wk) => {
  const el = document.querySelector(`[data-wk="${wk}"]`), r = el.getBoundingClientRect();
  const pts = [[r.left + 5, r.top + 5], [r.right - 5, r.top + 5], [r.left + 5, r.bottom - 5], [r.right - 5, r.bottom - 5],
               [r.left + r.width / 2, r.top + r.height / 2]];
  for (const [x, y] of pts) if (document.elementFromPoint(x, y) === el) return [x, y];
  return null;
}
"""

LUMA = """() => {
  const m = getComputedStyle(document.body).backgroundColor.match(/[\\d.]+/g).map(Number);
  return (0.2126 * m[0] + 0.7152 * m[1] + 0.0722 * m[2]) / 255;
}"""


def unavailable(why: str) -> None:
    if REQUIRE:
        pytest.fail(f"{why} and WS_E2E_REQUIRE=1")
    pytest.skip(why)


# --- the fixture: a snapshot, a profile that names a recorder, and the real server on loopback ------------------------


def fixture_snapshot() -> Snapshot:
    def s(ident, name, state, repo, tmux="", cwd="", **kw):
        kw.setdefault("source", "claude_code")
        kw.setdefault("runner", "claude-code")
        return Session(id=ident, name=name, state=state, repo=repo, tmux=tmux, cwd=cwd or (f"/x/{repo}" if tmux else ""),
                       started_at="2026-11-02T10:00:00.000Z", last_seen=TS, **kw)  # fmt: skip

    runs = [
        Session(id=f"run{i}", source="ws_runs", runner="claude-code", name="nightly", repo="jobs", cwd="/x/jobs",
                state="ended", started_at=TS, last_seen=TS, cost_usd=0.1 * (i + 1), cost_basis="reported")
        for i in range(3)
    ]  # fmt: skip
    lead = s("lead", "Home lead", "working", "code", "ws:@5", lead=True)
    sessions = (
        lead,
        s("w1", "builder", "working", "app", "ws:@1", tokens={"input": 1200, "output": 300, "cache_creation": 0, "cache_read": 0},
          cost_usd=1.25, cost_basis="reported"),
        s("w2", "remote builder", "working", "app", "ws:@2", host="far"),
        s("i1", "idle one", "idle", "app", "ws:@3"),
        s("i2", "idle two", "idle", "code", "ws:@4"),
        s("n1", "needs one", "waiting", "app", "ws:@6", waiting_for="approve the plan"),
        s("d1", "done one", "ended", "app", "ws:@7"),
        # an opaque session keeps only what the opaque rule lists
        Session(id="x1", source="claude_code", repo="private", tmux="ws:@9", state="working", opaque=True,
                started_at="2026-11-02T10:00:00.000Z"),
        *runs,
    )  # fmt: skip
    opts = (
        {"label": "Tidy", "description": "small"},
        {"label": "Rewrite", "description": "big"},
        {"label": "Leave it", "description": ""},
    )
    asks = (
        Ask(id="q1", source="asks_jsonl", kind="question", title="Pick one", session_name="builder", repo="app",
            tmux="ws:@11", options=opts, decision="the plan", created=TS, rc_url="https://claude.ai/code/walk", answer_in="app"),
        Ask(id="q2", source="asks_jsonl", kind="question", title="Say it in your own words", repo="app", tmux="ws:@12", created=TS),
        Ask(id="p1", source="asks_jsonl", kind="permission", title="Run the build?", repo="app", tmux="ws:@13", created=TS),
        Ask(id="a1", source="ws_approvals", kind="approval", title="command: terraform apply", repo="app", tmux="ws:@14",
            decision="ws approve a1", created=TS),
        Ask(id="o1", source="asks_jsonl", kind="question", repo="private", tmux="ws:@19", created=TS, opaque=True),
    )  # fmt: skip
    arm = {
        "job": "triage", "trials": 20, "solved": 15, "pass_rate": 0.75, "pass_hat_k": {"k": 3, "value": 0.5},
        "time_per_solved": {"p50_s": 12.0, "p90_s": 30.0}, "tokens_per_solved": 1200.0, "cost_per_solved_usd": 0.42,
        "noise": {"points": 5, "wins_needed": 6},
    }  # fmt: skip
    trend = tuple({"batch": i, "ts": TS, "n": 10, "pass_rate": 0.6 + 0.05 * i} for i in range(4))
    evals = (
        EvalArm(arm="baseline", trend=trend, **arm),
        EvalArm(arm="variant", trend=trend[:2], **{**arm, "pass_rate": 0.6}),
    )
    sources = (
        SourceRow(name="claude_code", ok=True, n=8), SourceRow(name="asks_jsonl", ok=True, n=5),
        SourceRow(name="ws_eval", ok=False, kind="unreadable", why="can't read the eval results"),
        SourceRow(name="collie", ok=False, kind="unavailable", why="not installed"),
    )  # fmt: skip
    return Snapshot(ts=TS, host="here", lead=lead, sessions=sessions, asks=asks, plugins=(Plugin(name="memory", on=True, built=True),),
                    evals=evals, sources=sources)  # fmt: skip


class Rig:
    """The fixture and the real server. `calls()` is what the recorder was given, in order."""

    def __init__(self, tmp: Path, monkeypatch: pytest.MonkeyPatch, *, start: bool = True, folder: bool = True) -> None:
        self.snap = fixture_snapshot()
        self.log = tmp / "calls.jsonl"
        self.log.write_text("", encoding="utf-8")
        self.rec = tmp / "recorder"
        self.rec.write_text(
            f"#!{sys.executable}\nimport json, sys\n"
            f"with open({str(self.log)!r}, 'a', encoding='utf-8') as f:\n    f.write(json.dumps(sys.argv[1:]) + '\\n')\n",
            encoding="utf-8",
        )
        self.rec.chmod(self.rec.stat().st_mode | stat.S_IXUSR)
        home = tmp / "home"
        (home / ".ws").mkdir(parents=True)
        r = json.dumps(str(self.rec))
        profile = ["[home.answer]", f'command = [{r}, "answer", "{{id}}", "{{choice}}", "{{text}}"]']
        if start:
            where = (
                '"{workspace}", "{folder}"' if folder else '"{workspace}"'
            )  # {folder}: the full path, not a short name
            profile += [
                "[home.start]",
                f'command = [{r}, "start", "{{what}}", {where}, "{{where}}", "{{runner}}", "{{model}}", "{{request}}"]',
            ]
        profile += [
            "[home.open]", f"session = {json.dumps(f'command: {self.rec} open {{tmux}}')}",
            "[home.workspaces]", 'app = "financial"',
            "[home.models]", 'claude-code = ["opus", "sonnet"]',
            "[home.deny]", '"claude-code/sonnet" = ["financial"]',
        ]  # fmt: skip
        profile_text = "[home]\n" + "\n".join(profile).replace("[home.", "\n[home.") + "\n"
        (home / ".ws" / "profile.toml").write_text(profile_text.replace("[home]\n\n", "[home]\n"), encoding="utf-8")
        self.environ = {"HOME": str(home), "WS_HOME": str(home / ".ws"), "TMUX": "/tmp/tmux-1/default,1,0"}
        monkeypatch.setattr(api, "snapshot", lambda env=None: self.snap)
        monkeypatch.setattr(api, "asks", lambda env=None: list(self.snap.asks))
        self.srv = HomeServer(self.env, idle_seconds=600)
        self.thread = threading.Thread(target=self.srv.serve, daemon=True)
        self.thread.start()
        self.url = self.srv.url

    def env(self, environ: dict | None = None) -> api.Env:
        def run(argv, timeout_s):
            if argv[0] == "tmux":  # a tmux call is recorded, never made
                argv = [str(self.rec), *argv]
            return api.default_run(argv, timeout_s)

        return api.Env.make(environ or self.environ, run=run, now=NOW, host="here")

    def calls(self) -> list:
        return [json.loads(line) for line in self.log.read_text(encoding="utf-8").splitlines()]

    def reset(self) -> None:
        self.log.write_text("", encoding="utf-8")
        with self.srv._sent_lock:
            self.srv._sent.clear()

    def record_for(self, row_id: str):
        for rec in (*self.snap.sessions, *self.snap.asks):
            if row_id == "r-" + _rows.row_key(rec.id):
                return rec
        raise AssertionError(f"no record for row {row_id}")

    def post(self, path: str, body: dict) -> tuple[int, dict]:
        host = f"127.0.0.1:{self.srv.port}"
        headers = {
            "Content-Type": "application/json",
            "X-Ws-Token": self.srv.token,
            "Origin": f"http://{host}",
            "Host": host,
        }
        conn = http.client.HTTPConnection("127.0.0.1", self.srv.port, timeout=5)
        try:
            conn.request("POST", path, json.dumps(body), headers)
            res = conn.getresponse()
            return res.status, json.loads(res.read() or b"{}")
        finally:
            conn.close()

    def close(self) -> None:
        self.srv.close()
        self.thread.join(5)


def expected_open(rec) -> list[str]:
    """What an Open button for `rec` runs: the terminal opener for an ask or an opaque session, else the profile's command."""
    if isinstance(rec, Ask) or rec.opaque:
        return ["tmux", "switch-client", "-t", rec.tmux]
    return ["open", rec.tmux]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path, monkeypatch)
    yield r
    r.close()


@pytest.fixture(scope="module")
def browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        unavailable("Playwright is not installed")
    with sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception as e:  # noqa: BLE001 - a browser that won't start is a skip or, in CI, a fail
            unavailable(f"Chromium cannot start here ({type(e).__name__})")
        yield b
        b.close()


# --- the walker -------------------------------------------------------------------------------------------------------


@dataclass
class Ctl:
    wk: str
    key: str
    action: str
    expect: str
    tag: str
    vis: bool


@dataclass
class Out:
    ok: bool = True
    note: str = ""
    dirty: bool = False  # the page is not as the state left it: load it again
    credit: list = field(default_factory=list)  # (action, ok, note) for controls this one's flow also exercised


@dataclass
class State:
    name: str
    scope: str = "body"
    exclude: str = ""
    anchor: str = ""  # the place hash the state sits on
    enter: str = ""  # a walker method that gets the page into the state
    holds: str = ""  # JS that is true while the page is in the state
    needs: str = ""  # an action that must be on screen for the state to apply here


STATES = (
    State("page", exclude=".ask[data-ask]", enter="enter_page"),
    State("view menu", scope="#viewpop", enter="enter_view_menu", holds="!document.getElementById('viewpop').hidden", needs="view.open"),
    State("saved view", scope="#viewlist", enter="enter_page"),
    State("settings", scope="#setpop", enter="enter_settings", holds="!document.getElementById('setpop').hidden"),
    State("drawer", scope="#side, .scrim:not(.sscrim)", enter="enter_drawer", holds="document.body.classList.contains('drawer')", needs="drawer.toggle"),
    State("sheet", scope="#sheet, .sscrim", enter="enter_sheet", holds="document.body.classList.contains('sheet')", needs="composer.sheet"),
    State("answer form", scope=".ask[data-ask]", enter="enter_page"),
    *(State(f"place {p}", scope=f"#v-{p}", anchor=f"#v-{p}", enter="enter_page", holds=f"location.hash === '#v-{p}'") for p in PLACES),
)  # fmt: skip


class Walker:
    """One browser context (a viewport and a colour scheme) walked through every state."""

    def __init__(self, browser, rig: Rig, vp: str, scheme: str, url: str | None = None) -> None:
        from playwright.sync_api import expect

        self.expect = expect
        self.rig, self.vp, self.scheme = rig, vp, scheme
        self.url = url or rig.url
        w, h = VIEWPORTS[vp]
        self.ctx = browser.new_context(viewport={"width": w, "height": h}, color_scheme=scheme)
        self.ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        self.ctx.route(
            re.compile(r"^https://"), lambda route: route.fulfill(status=200, content_type="text/html", body="external")
        )
        self.ctx.add_init_script(HOOKS)
        self.page = self.ctx.new_page()
        self.page.set_default_timeout(4000)
        self.expect.set_options(timeout=4000)
        self.results: list[tuple[str, str, str, bool, str]] = []  # action, viewport, state, ok, note
        self.seen: dict[str, str] = {}  # every data-action in the page -> its data-expect
        self.state = STATES[0]
        self.saved_view: dict = {}
        self.reloaded: set = set()
        self.timings: dict[str, float] = {}
        self.undo_done = False
        self.handlers = {
            "navigate": self.h_navigate, "toggle": self.h_toggle, "set": self.h_set, "filter": self.h_filter,
            "focus": self.h_focus, "type": self.h_type, "select": self.h_select, "add": self.h_add, "post": self.h_post,
            "cancel": self.h_cancel, "copy": self.h_copy, "none": self.h_none, "close-drawer": self.h_close_drawer,
            "open-sheet": self.h_open_sheet,
        }  # fmt: skip

    # -- plumbing --

    def close(self) -> None:
        self.ctx.close()

    def js(self, expr: str, arg=None):
        return self.page.evaluate(expr, arg)

    def on(self, sel: str) -> bool:
        return self.js(ON_SCREEN, sel)

    def loc(self, c: Ctl):
        return self.page.locator(f'[data-wk="{c.wk}"]')

    def act(self, name: str):
        return self.page.locator(f'[data-action="{name}"]')

    def visible_act(self, *names: str):
        """The first control of these actions that is on screen."""
        for n in names:
            for i in range(self.act(n).count()):
                el = self.act(n).nth(i)
                if self.js(
                    "(e) => e.checkVisibility({checkVisibilityCSS:true}) && e.getBoundingClientRect().right > 0",
                    el.element_handle(),
                ):
                    return el
        raise AssertionError(f"none of {names} is on screen")

    def scan(self, st: State) -> list[Ctl]:
        got = self.js(SCAN, [st.scope if st.scope != "body" else "", st.exclude])
        for action, expect in got["all"]:
            self.seen[action] = expect
        return [Ctl(**c) for c in got["out"]]

    def load(self, st: State) -> None:
        self.state = st
        self.rig.reset()
        if self.page.url.startswith("http"):
            self.js("sessionStorage.removeItem('__walk')")
        self.page.goto("about:blank")  # so the next address is a fresh load even when only the #place differs
        self.page.goto(self.url + st.anchor)
        getattr(self, st.enter)()

    def record(self, action: str, ok: bool, note: str = "") -> None:
        self.results.append((action, self.vp, self.state.name, ok, note))

    def clickable(self, c: Ctl) -> None:
        """Click a control; a div (a scrim) is clicked at a point nothing else covers."""
        if c.tag == "div":
            pt = self.js(FREE_POINT, c.wk)
            assert pt, "no free point on the scrim"
            self.page.mouse.click(*pt)
        else:
            self.loc(c).click()

    def restore_hash(self) -> None:
        self.js("(h) => { location.hash = h; }", self.state.anchor or "#v-home")

    # -- entering a state --

    def enter_page(self) -> None:
        self.js("document.querySelectorAll('details').forEach((d) => { d.open = true; })")

    def enter_view_menu(self) -> None:
        self.visible_act("view.open").click()
        assert self.on("#viewpop")

    def enter_settings(self) -> None:
        try:
            button = self.visible_act("settings.avatar", "settings.open")
        except AssertionError:  # on a phone the settings button sits in the drawer
            self.visible_act("drawer.toggle").click()
            button = self.visible_act("settings.avatar", "settings.open")
        button.click()
        assert self.on("#setpop")

    def enter_drawer(self) -> None:
        self.visible_act("drawer.toggle").click()
        assert self.js("document.body.classList.contains('drawer')")

    def enter_sheet(self) -> None:
        self.visible_act("composer.sheet").click()
        assert self.js("document.body.classList.contains('sheet')")

    def applies(self, st: State) -> bool:
        if not st.needs:
            return True
        self.load(STATES[0])
        return any(c.vis for c in self.scan(State("probe")) if c.action == st.needs)

    # -- the walk --

    def walk(self) -> None:
        for st in STATES:
            if not self.applies(st):
                continue
            started = time.monotonic()
            self.load(st)
            done: set[str] = set()
            while True:
                todo = [c for c in self.scan(st) if c.vis and c.key not in done]
                if not todo:
                    break
                c = todo[0]
                done.add(c.key)
                reload_needed = False
                try:
                    out = self.exercise(c)
                    self.record(c.action, out.ok, out.note)
                    for action, ok, note in out.credit:
                        self.record(action, ok, note)
                    reload_needed = out.dirty
                except Exception as e:  # noqa: BLE001 - a failed check is a row in the table, and the walk goes on
                    self.record(c.action, False, f"{type(e).__name__}: {str(e).splitlines()[0][:200]}")
                    reload_needed = True
                if reload_needed or (st.holds and not self.js(f"() => {st.holds}")):
                    self.load(st)
            self.extras(st)
            self.timings[st.name] = time.monotonic() - started
        self.looks()

    def exercise(self, c: Ctl) -> Out:
        kind, _, arg = c.expect.partition(":")
        handler = self.handlers.get(kind)
        if handler is None:
            return Out(False, f"no handler for data-expect={c.expect!r}")
        return handler(c, self.loc(c), arg)

    def extras(self, st: State) -> None:
        """What the page does around its controls: Escape closes a menu, a drawer or a sheet."""
        if st.name in ("view menu", "settings", "drawer", "sheet"):
            self.load(st)
            try:
                self.page.keyboard.press("Escape")
                assert not self.js(f"() => {st.holds}"), "still open"
                self.record(f"key.escape ({st.name})", True)
            except Exception as e:  # noqa: BLE001
                self.record(f"key.escape ({st.name})", False, str(e).splitlines()[0][:200])
        if st.name in ("view menu", "settings"):
            self.load(st)
            try:
                self.page.mouse.click(5, self.page.viewport_size["height"] - 5)
                assert not self.js(f"() => {st.holds}"), "still open"
                self.record(f"click away ({st.name})", True)
            except Exception as e:  # noqa: BLE001
                self.record(f"click away ({st.name})", False, str(e).splitlines()[0][:200])

    def looks(self) -> None:
        """Light and dark are real: the page's background follows the colour scheme."""
        self.load(STATES[0])
        luma = self.js(LUMA)
        ok = luma < 0.4 if self.scheme == "dark" else luma > 0.6
        self.record(f"colour scheme ({self.scheme})", ok, f"background brightness {luma:.2f}")

    # -- handlers: navigate --

    def heading(self) -> str:
        return self.js(
            "() => { const h = document.querySelector('.pg:target h2.vh2'); if (h) return h.textContent;"
            " return ['', '#v-home'].includes(location.hash) ? 'Home' : ''; }"
        )

    def h_navigate(self, c: Ctl, loc, arg: str) -> Out:
        if arg == "external":
            href = loc.get_attribute("href")
            assert loc.get_attribute("target") == "_blank" and "noopener" in (loc.get_attribute("rel") or ""), (
                "not a safe new-tab link"
            )
            with self.ctx.expect_page() as popup:
                loc.click()
            new = popup.value
            new.wait_for_load_state()
            assert new.url.rstrip("/") == href.rstrip("/"), f"opened {new.url}, wanted {href}"
            new.close()
            return Out()
        assert arg.startswith("#"), arg
        drawer_was_open = self.js("document.body.classList.contains('drawer')")
        before_hash, before = self.js("location.hash"), self.heading()
        if before_hash == arg:  # move away first, when the control can still be seen, so a change is visible
            self.js("(h) => { location.hash = h; }", "#v-evals" if arg != "#v-evals" else "#v-home")
            if not loc.is_visible():
                self.js("(h) => { location.hash = h; }", arg)
        before_hash, before = self.js("location.hash"), self.heading()
        in_drawer = loc.evaluate("(e) => !!e.closest('.side')")
        loc.click()
        self.expect(self.page).to_have_url(re.compile(re.escape(arg) + "$"))
        if arg.startswith("#r-"):
            row = self.page.locator(arg)
            self.expect(row).to_have_js_property("open", True)
            self.expect(row).to_be_in_viewport()
        elif arg == "#v-home":
            self.expect(self.page.locator("#composer")).to_be_visible()
            assert self.heading() == "Home"
        else:
            title = self.page.locator(f"{arg} h2.vh2")
            self.expect(title).to_be_visible()
            assert self.heading() == title.inner_text() != before or before_hash == arg, (
                "the page heading did not change"
            )
            assert not self.page.locator("#v-home").is_visible(), "Home is still showing"
        if drawer_was_open and in_drawer:  # a link in the drawer takes you there and puts the drawer away
            self.expect(self.page.locator("body")).not_to_have_class(re.compile(r"\bdrawer\b"))
        self.restore_hash()
        return Out()

    # -- handlers: toggle --

    def h_toggle(self, c: Ctl, loc, arg: str) -> Out:
        if c.tag == "summary":
            return self.toggle_fold(loc)
        if arg == "row":  # Not now: the row folds
            row = loc.locator("xpath=ancestor::details[contains(@class,'r')][1]")
            assert row.evaluate("(e) => e.open"), "the row was not open"
            loc.click()
            assert not row.evaluate("(e) => e.open") and not row.locator(".rb").is_visible(), "the row did not fold"
            row.evaluate("(e) => { e.open = true; }")
            return Out()
        if arg.startswith("show."):
            return self.toggle_show(loc, arg.removeprefix("show."))
        was = self.on(arg)
        self.clickable(c)
        now = self.settled(arg, not was)
        assert now != was, f"{arg} did not {'close' if was else 'open'}"
        aria = loc.get_attribute("aria-expanded")
        if aria is not None:
            assert aria == str(now).lower(), f"aria-expanded is {aria} but {arg} is {'shown' if now else 'hidden'}"
        return Out(dirty=True)

    def settled(self, sel: str, want: bool) -> bool:
        """Whether `sel` is on screen once its transition is over (a drawer slides)."""
        for _ in range(30):
            if self.on(sel) == want:
                break
            self.page.wait_for_timeout(50)
        return self.on(sel)

    def toggle_fold(self, loc) -> Out:
        """Click a fold shut and open (or open and leave it open: a fold the person needs to reach what is inside)."""
        det = loc.locator("xpath=..")
        was = det.evaluate("(e) => e.open")
        body = det.locator("xpath=./*[not(self::summary)]").first
        for want in [True] if not was else [False, True]:
            loc.click()
            assert det.evaluate("(e) => e.open") == want, f"the fold did not {'open' if want else 'close'}"
            if body.count():
                assert body.is_visible() == want, "the fold's content did not follow it"
        return Out()

    def toggle_show(self, loc, key: str) -> Out:
        attr = {
            "pinned": ("#list", "showPinned"),
            "done": ("#list", "showDone"),
            "quiet": ("#main", "showQuiet"),
            "model": ("#list", "showModel"),
        }[key]
        target = {"pinned": "[data-grp=pin]", "done": "[data-grp=done]", "quiet": "#quiet", "model": ".mdl"}[key]
        for _ in range(2):
            was = loc.get_attribute("aria-checked") == "true"
            loc.click()
            now = loc.get_attribute("aria-checked") == "true"
            assert now != was, "aria-checked did not flip"
            assert self.js("([s, k]) => document.querySelector(s).dataset[k]", list(attr)) == ("1" if now else "0")
            first = self.page.locator(target).first
            assert first.count(), f"nothing in the page for {key}"
            if key == "model" and now and self.page.viewport_size["width"] <= 600:
                continue  # a phone keeps only the first two parts of a row's line, and the runner is never one of them
            assert first.is_visible() == now, f"{key} is {'hidden' if now else 'shown'} but the box says {now}"
        return Out()

    def h_open_sheet(self, c: Ctl, loc, arg: str) -> Out:
        self.clickable(c)
        assert self.js("document.body.classList.contains('sheet')") and self.on("#sheet")
        return Out(dirty=True)

    def h_close_drawer(self, c: Ctl, loc, arg: str) -> Out:
        assert self.js("document.body.classList.contains('drawer')"), "the drawer was not open"
        self.clickable(c)
        assert not self.js("document.body.classList.contains('drawer')") and not self.settled("#side", False), (
            "the drawer stayed open"
        )
        return Out(dirty=True)

    # -- handlers: set, filter --

    def h_set(self, c: Ctl, loc, arg: str) -> Out:
        lhs, _, val = arg.partition("=")
        if lhs == "view":
            return self.apply_view(c, loc, val)
        store, key = lhs.split(".")
        loc.click()
        group = loc.locator("xpath=..")
        assert "on" in (loc.get_attribute("class") or "").split(), "the chosen button isn't marked on"
        assert group.locator(".on").count() == 1, "more than one button is marked on"
        if store == "look":
            self.check_look(key, val)
        elif key == "group":
            assert self.js("document.getElementById('list').dataset.group") == val
            assert self.js("document.getElementById('viewlbl').textContent") == f"View: by {val}"
            heads = self.js(
                "Array.from(document.querySelectorAll('#list > .grp > h5')).map((h) => h.textContent.trim())"
            )
            assert heads and all(heads), f"the groups have no names: {heads}"
        elif key == "density":
            assert self.js("document.getElementById('list').dataset.density") == val
            l2 = self.page.locator("#list .r .l2").first
            assert l2.is_visible() == (val == "comfy"), "the second line of a row did not follow the density"
        saved = self.js(
            "([s, k]) => JSON.parse(localStorage.getItem(s === 'look' ? 'ws-home-look' : 'ws-home-view'))[k]",
            [store, key],
        )
        assert saved == val, f"the choice was not kept ({saved!r})"
        if (store, key) not in self.reloaded and val not in ("system", "violet", "sans", "m", "need", "comfy"):
            self.reloaded.add((store, key))  # and a new load brings it back (once for each kind of choice)
            self.page.reload()
            if store == "look":
                self.check_look(key, val)
            else:
                assert self.js("(k) => document.getElementById('list').dataset[k]", key) == val, (
                    "a new load lost the view"
                )
        return Out(dirty=True)

    def check_look(self, key: str, val: str) -> None:
        root = "(k) => document.documentElement.dataset[k]"
        if key == "theme":
            assert self.js(root, "theme") == (None if val == "system" else val)
            luma = self.js(LUMA)
            dark = (val == "dark") or (val == "system" and self.scheme == "dark")
            assert (luma < 0.4) if dark else (luma > 0.6), f"the {val} theme has brightness {luma:.2f}"
        elif key == "accent":
            assert self.js(root, "accent") == (None if val == "violet" else val)
        elif key == "font":
            assert self.js(root, "font") == (None if val == "sans" else val)
        elif key == "size":
            assert self.js(root, "size") == (None if val == "m" else val)

    def apply_view(self, c: Ctl, loc, val: str) -> Out:
        """A saved view, or the default one: from some other view, it brings the list back to what was saved."""
        self.js("document.getElementById('viewpop').hidden = false")
        self.page.locator('[data-action="view.group-machine"]').click()
        self.page.locator('[data-action="view.density-comfy"]').click()
        self.js("document.getElementById('viewpop').hidden = true")
        loc.click()
        want = self.saved_view if val != "default" else {"group": "need", "density": "comfy"}
        got = self.js("() => ({group: list.dataset.group, density: list.dataset.density})")
        assert got == want, f"the list is {got}, wanted {want}"
        assert "on" in (loc.get_attribute("class") or "").split() and self.page.locator("#viewlist .on").count() == 1
        assert self.js("location.hash") == "#v-home"
        assert self.js("document.getElementById('viewlbl').textContent") == f"View: by {want['group']}"
        return Out(dirty=True)

    def h_filter(self, c: Ctl, loc, arg: str) -> Out:
        if arg == "workspace":
            ws = loc.get_attribute("data-ws")
            drawer_was_open = self.js("document.body.classList.contains('drawer')")
            loc.click()
            hidden = self.js("Array.from(document.querySelectorAll('#list .r')).map((r) => [r.dataset.ws, r.hidden])")
            assert hidden and all(h == (w != ws) for w, h in hidden), f"rows not filtered to {ws}"
            assert "on" in (loc.get_attribute("class") or "").split()
            if drawer_was_open:
                assert not self.js("document.body.classList.contains('drawer')"), "the drawer stayed open"
            self.js("document.body.classList.add('drawer')") if drawer_was_open else None
            loc.click()  # the same button again clears it
            assert not self.js("document.querySelectorAll('#list .r[hidden]').length"), "the filter did not clear"
            return Out(dirty=True)
        assert arg == "machine", arg
        values = loc.evaluate("(s) => Array.from(s.options).map((o) => o.value)")
        assert "" in values and len(values) > 1
        for v in values:
            loc.select_option(v)
            rows = self.js(
                "Array.from(document.querySelectorAll('#list .r')).map((r) => [r.dataset.machine, r.hidden])"
            )
            assert rows and all(h == (bool(v) and m != v) for m, h in rows), f"rows not filtered to {v!r}"
        return Out(dirty=True)

    def h_focus(self, c: Ctl, loc, arg: str) -> Out:
        loc.click()
        self.expect(self.page.locator(arg)).to_be_focused()
        assert not self.js("document.body.classList.contains('drawer')"), "the drawer stayed open"
        self.restore_hash()
        return Out(dirty=True)

    # -- handlers: type, select, add --

    def h_type(self, c: Ctl, loc, arg: str) -> Out:
        loc.fill("a few words")
        assert loc.input_value() == "a few words"
        if c.action == "ask.text":
            self.rig.reset()
            loc.press("Enter")
            ask = loc.locator("xpath=ancestor::*[@data-ask][1]")
            self.expect(ask.locator(".stamp .msg")).to_have_text(re.compile(r"^Answered · a few words"))
            ask.locator('[data-action="ask.undo"]').click()
            assert ask.locator(".stamp").is_hidden() and loc.is_visible(), "undo did not bring the box back"
            self.page.wait_for_timeout(UNDO_TEST_MS + 400)
            assert self.rig.calls() == [], f"an undone answer was sent: {self.rig.calls()}"
            loc.fill("")
            return Out(credit=[("ask.undo", True, "after Enter")])
        loc.fill("")
        return Out()

    def h_select(self, c: Ctl, loc, arg: str) -> Out:
        opts = loc.evaluate("(s) => Array.from(s.options).map((o) => [o.value, o.disabled])")
        usable = [v for v, off in opts if not off]
        assert usable, "nothing to choose"
        for v in usable:
            loc.select_option(v)
            assert loc.input_value() == v
            if arg == "#c-ws":
                self.check_model_rules()
        if arg == "#c-ws":  # what was picked for a workspace comes back when the workspace does
            self.set_select("#c-ws", usable[0])
            self.set_select("#c-what", "job")
            self.set_select("#c-ws", usable[-1])
            self.set_select("#c-ws", usable[0])
            assert self.page.locator("#c-what").input_value() == "job", "the picks for a workspace were not kept"
        else:
            self.set_select(arg, usable[-1])
            self.page.reload()  # and a new load brings the last choice back
            got = self.page.locator(arg).input_value()
            assert got == usable[-1], f"the choice was not kept ({got!r})"
        return Out(dirty=True)

    def check_model_rules(self) -> None:
        bad = self.js(
            """() => {
              const ws = document.getElementById('c-ws'), cls = ws.selectedOptions[0].dataset.cls || '', bad = [];
              document.querySelectorAll('#c-runner option[data-deny]').forEach((o) => {
                const off = cls === 'terminal only' || (!!cls && o.dataset.deny.split(',').includes(cls));
                if (o.disabled !== off) bad.push(o.dataset.label + ' disabled=' + o.disabled);
                if (off && !/terminal only|not for/.test(o.textContent)) bad.push(o.dataset.label + ' gives no reason');
              });
              return bad;
            }"""
        )
        assert not bad, f"the model list does not follow the workspace's class: {bad}"

    def h_add(self, c: Ctl, loc, arg: str) -> Out:
        assert arg == "#viewlist", arg
        before = self.page.locator("#viewlist [data-view]").count()
        loc.click()  # no name: nothing is saved
        assert self.page.locator("#viewlist [data-view]").count() == before, "a view with no name was saved"
        self.page.locator('[data-action="view.group-model"]').click()
        self.page.locator('[data-action="view.density-compact"]').click()
        self.page.locator("#viewname").fill("walk")
        loc.click()
        new = self.page.locator('#viewlist [data-view="walk"]')
        self.expect(new).to_have_count(1)
        assert new.get_attribute("data-action") == "view.apply" and new.get_attribute("data-expect") == "set:view=walk"
        assert "on" in (new.get_attribute("class") or "").split()
        assert self.page.locator("#viewname").input_value() == "" and not self.on("#viewpop"), (
            "the name stayed or the menu stayed open"
        )
        self.saved_view = {"group": "model", "density": "compact"}
        return Out(dirty=True)

    # -- handlers: copy, none --

    def h_copy(self, c: Ctl, loc, arg: str) -> Out:
        text = loc.get_attribute("data-copy")
        assert text, "nothing to copy"
        was = loc.inner_text()
        loc.click()
        self.expect(loc).to_have_text("Copied")
        assert self.js("navigator.clipboard.readText()") == text, "the clipboard does not hold the text"
        assert was != "Copied"
        return Out()

    def h_none(self, c: Ctl, loc, arg: str) -> Out:
        assert loc.is_disabled(), "a control that does nothing should be disabled"
        assert loc.get_attribute("title") == arg, f"its reason is {loc.get_attribute('title')!r}"
        assert arg in loc.locator("xpath=following-sibling::*[1]").inner_text(), "the reason is not shown beside it"
        self.rig.reset()
        loc.click(force=True)
        self.page.wait_for_timeout(200)
        assert self.rig.calls() == [], "a disabled button ran something"
        return Out()

    # -- handlers: post --

    def h_post(self, c: Ctl, loc, arg: str) -> Out:
        flows = {"/api/answer": self.answer_flow, "/api/start": self.start_flow, "/api/open": self.open_flow}
        if arg not in flows:
            return Out(False, f"no handler for post to {arg}")
        self.rig.reset()
        if loc.is_disabled():  # a page written to a file has no server to post to: the button is off and says why
            return self.off_flow(c, loc)
        return flows[arg](c, loc)

    def off_flow(self, c: Ctl, loc) -> Out:
        assert c.action == "composer.send", f"{c.action} is off"
        why = self.page.locator("#c-note").inner_text().strip()
        assert why, "the button is off and says nothing about why"
        loc.click(force=True)
        self.page.wait_for_timeout(200)
        assert self.rig.calls() == [] and self.page.locator("#c-note").inner_text().strip() == why
        return Out()

    def h_cancel(self, c: Ctl, loc, arg: str) -> Out:
        return Out(False, "Undo is only on screen after a choice; it is exercised by the choice")

    def stamp(self, ask) -> str:
        return ask.locator(".stamp .msg")

    def answer_flow(self, c: Ctl, loc) -> Out:
        ask = loc.locator("xpath=ancestor::*[@data-ask][1]")
        ident = ask.get_attribute("data-ask")
        credit = []
        if c.action == "ask.choose":
            label = loc.get_attribute("data-choice")
            want, said = ["answer", ident, label, ""], label
            go = loc.click
        else:  # ask.send
            box = ask.locator(".else input")
            go = ask.locator('[data-action="ask.send"]').click
            go()  # nothing typed: nothing is staged
            assert ask.locator(".stamp").is_hidden(), "an empty answer was staged"
            box.fill(WORDS)
            want, said = ["answer", ident, "", WORDS], WORDS
        if not self.undo_done:  # choose, then undo inside the window: nothing is sent
            go()
            self.expect(self.stamp(ask)).to_have_text(re.compile(rf"^Answered · {re.escape(said)} · \d\d:\d\d$"))
            assert ask.locator(".opts2").is_hidden() and ask.locator(".else").is_hidden(), (
                "the choices stayed on screen"
            )
            assert self.rig.calls() == [], "sent before the undo window ended"
            ask.locator('[data-action="ask.undo"]').click()
            assert ask.locator(".stamp").is_hidden() and ask.locator(".opts2, .else").first.is_visible(), (
                "undo did not bring the form back"
            )
            self.page.wait_for_timeout(UNDO_TEST_MS + 400)
            assert self.rig.calls() == [], f"an undone answer was sent: {self.rig.calls()}"
            credit.append(("ask.undo", True, "nothing sent"))
            self.undo_done = True
            if c.action == "ask.send":
                box.fill(WORDS)
        go()
        self.expect(self.stamp(ask)).to_have_text(re.compile(r"^Sent · "))
        assert self.rig.calls() == [want], f"the program was given {self.rig.calls()}"
        assert self.js("window.__reloads") == 1, "the page does not reload after a send"
        return Out(dirty=True, credit=credit)

    def start_flow(self, c: Ctl, loc) -> Out:
        note = self.page.locator("#c-note")
        text = self.page.locator("#c-text")
        text.fill("")
        loc.click()
        self.expect(note).to_have_text("Write what you want done first.")
        assert self.rig.calls() == []
        # a terminal-only folder is refused on the page, before anything is sent
        self.set_select("#c-ws", "opaque:private")
        text.fill("do the thing")
        loc.click()
        self.expect(note).to_contain_text("terminal only")
        assert self.rig.calls() == [], "a terminal-only workspace was started"
        self.set_select("#c-ws", "/x/code")
        self.set_select("#c-what", "chat")
        self.set_select("#c-runner", "claude-code|opus")
        text.fill(WORDS)
        loc.click()
        self.expect(note).to_have_text("Started. It shows up in the list in a moment.")
        want = ["start", "chat", "code", "/x/code", "here", "claude-code", "opus", WORDS]
        assert self.rig.calls() == [want], f"the program was given {self.rig.calls()}"
        assert text.input_value() == "" and self.js("window.__reloads") == 1
        return Out(dirty=True)

    def set_select(self, sel: str, value: str) -> None:
        """Choose in a picker; on a phone the pickers are in a sheet that is closed, so this sets them as the page's own
        script would see a change."""
        self.js(
            "([s, v]) => { const e = document.querySelector(s); e.value = v; e.dispatchEvent(new Event('change', {bubbles: true})); }",
            [sel, value],
        )
        assert self.page.locator(sel).input_value() == value

    def open_flow(self, c: Ctl, loc) -> Out:
        rec = self.rig.record_for(loc.locator("xpath=ancestor::details[@id][1]").get_attribute("id"))
        loc.click()
        self.expect(loc).to_have_text("Done")
        want = expected_open(rec)
        assert self.rig.calls() == [want], f"the program was given {self.rig.calls()}, wanted {want}"
        return Out()


# --- the tests --------------------------------------------------------------------------------------------------------


def table(results, seen: dict[str, str]) -> str:
    """Action by viewport: how many times it was exercised ok, how many failed, or - when it was never on screen there."""
    cells: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0, 0])
    for action, vp, _state, ok, _note in results:
        cells[(action, vp)][0 if ok else 1] += 1
    vps = sorted({vp for _, vp in cells}, key=lambda v: (v == "static", v))
    names = sorted({a for a, _ in cells} | set(seen))
    width = max(len(n) for n in names)
    lines = [f"{'action':<{width}}  " + "  ".join(f"{v:>10}" for v in vps) + "   expect"]
    for n in names:
        row = []
        for v in vps:
            ok, bad = cells.get((n, v), (0, 0))
            row.append(f"{'FAIL ' + str(bad) + '/' + str(ok + bad) if bad else ('ok ' + str(ok) if ok else '-'):>10}")
        lines.append(f"{n:<{width}}  " + "  ".join(row) + f"   {seen.get(n, '')}")
    return "\n".join(lines)


def test_every_control_does_what_it_says(browser, rig, tmp_path):
    t0 = time.monotonic()
    walkers: list[Walker] = []
    static = LoopbackServer({"/": lambda nonce: _static_page(rig, nonce)}, idle_seconds=600)
    threading.Thread(target=static.serve, daemon=True).start()
    try:
        for vp in VIEWPORTS:
            for scheme in SCHEMES:
                w = Walker(browser, rig, vp, scheme)
                walkers.append(w)
                w.walk()
                w.close()
        # the page written to a file shows the same controls, with a copy button where the live page has a call
        w = Walker(browser, rig, "1440x900", "light", url=static.url)
        w.vp = "static"
        walkers.append(w)
        w.walk()
        w.close()
    finally:
        static.stop()
    results = [r for w in walkers for r in w.results]
    seen: dict[str, str] = {}
    for w in walkers:
        seen.update(w.seen)
    print("\n" + table(results, seen))
    per_vp = {
        vp: sorted({a for a, v, _, ok, _ in results if v == vp and ok and a in seen}) for vp in (*VIEWPORTS, "static")
    }
    print(
        "\n".join(
            f"{vp}: {len(acts)} of {len(seen)} actions exercised, {sum(1 for r in results if r[1] == vp and r[3])} clicks"
            for vp, acts in per_vp.items()
        )
    )
    print(f"walked in {time.monotonic() - t0:.0f} s")
    failed = [r for r in results if not r[3]]
    assert not failed, "a control did not do what it says:\n" + "\n".join(
        f"  {a} @{v} [{s}]: {n}" for a, v, s, _, n in failed
    )
    kinds = {e.partition(":")[0] for e in seen.values()}
    assert kinds <= set(KINDS), f"data-expect kinds with no handler: {sorted(kinds - set(KINDS))}"
    never = sorted(set(seen) - {a for a, *_, ok, _ in results if ok})
    assert not never, f"controls in the page that were never exercised: {never}"
    assert len(seen) > 60


def _static_page(rig: Rig, nonce: str) -> str:
    """The page as `ws home --render` writes it: no server behind it, and no tmux, so the terminal opener is a line to copy."""
    env = rig.env({**rig.environ, "TMUX": ""})
    snap = opener.attach(rig.snap, env)
    return page.render(snap, nonce, live=False, hub=hub.build(snap, env))


def test_an_opaque_ask_and_the_asks_a_page_cannot_answer_have_no_answer_controls(browser, rig):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    p = ctx.new_page()
    p.set_default_timeout(4000)
    try:
        p.goto(rig.url)
        for ident in ("o1", "p1", "a1"):
            row = p.locator(f"#r-{ident}")
            assert row.count() == 1, ident
            row.evaluate("(e) => { e.open = true; }")
            assert row.locator('.opt, [data-ask], [data-action^="ask."], .else, input, textarea').count() == 0, (
                f"{ident} has answer controls"
            )
        assert _fmt.OPAQUE_LINE in p.locator("#r-o1").inner_text()
        assert (
            p.locator("#r-q1 .opt").count() == 3 and p.locator("#r-q1 [data-ask]").count() == 1
        )  # the one that can be answered
        for ident in ("o1", "p1", "a1"):  # and the server agrees
            status, body = rig.post("/api/answer", {"id": ident, "choice": "Yes"})
            assert status == 409 and body["error"], (ident, status, body)
        assert rig.calls() == []
    finally:
        ctx.close()


def test_a_terminal_only_workspace_cannot_be_started(browser, rig):
    ctx = browser.new_context(viewport={"width": 1440, "height": 900})
    p = ctx.new_page()
    p.set_default_timeout(4000)
    try:
        p.goto(rig.url)
        p.locator("#c-ws").select_option("opaque:private")
        runners = p.evaluate(
            "Array.from(document.querySelectorAll('#c-runner option[data-deny]')).map((o) => o.disabled)"
        )
        assert runners and all(runners), "a runner is on offer for a terminal-only folder"
        assert "terminal only" in p.locator("#c-runner option[data-deny]").first.inner_text()
        p.locator("#c-text").fill("do the thing")
        p.locator("#c-send").click()
        assert "terminal only" in p.locator("#c-note").inner_text()
        good = {"what": "session", "where": "here", "runner": "claude-code", "model": "opus", "request": "do it"}
        for ws in ("opaque:private", "private"):  # by its key and by its folder name
            status, body = rig.post("/api/start", {**good, "workspace": ws})
            assert status == 409 and body["kind"] == "refused" and "terminal only" in body["error"], (ws, status, body)
        assert rig.calls() == [], "a terminal-only workspace was started"
        status, _ = rig.post("/api/start", {**good, "workspace": "/x/code"})
        assert status == 200 and rig.calls() == [
            ["start", "session", "code", "/x/code", "here", "claude-code", "opus", "do it"]
        ]
    finally:
        ctx.close()


def _with_opaque_code(rig: Rig) -> None:
    """A terminal-only folder that is also called `code`, beside the open /x/code."""
    hidden = Session(id="x2", source="claude_code", repo="code", tmux="ws:@20", state="working", opaque=True)
    rig.snap = replace(rig.snap, sessions=(*rig.snap.sessions, hidden))


def test_a_name_a_terminal_only_folder_shares_needs_the_full_path_in_the_start_command(browser, tmp_path, monkeypatch):
    from playwright.sync_api import expect

    good = {
        "what": "session",
        "where": "here",
        "runner": "claude-code",
        "model": "opus",
        "request": "do it",
        "workspace": "/x/code",
    }
    (tmp_path / "short").mkdir()
    short = Rig(tmp_path / "short", monkeypatch, folder=False)
    try:
        _with_opaque_code(short)
        status, body = short.post("/api/start", good)
        assert status == 409 and body["kind"] == "ambiguous" and "{folder}" in body["error"], (status, body)
        assert short.calls() == [], "a start that could pick either folder ran"
        ctx = browser.new_context(viewport={"width": 1440, "height": 900})  # the page tells the person the same
        p = ctx.new_page()
        p.set_default_timeout(4000)
        p.goto(short.url)
        p.locator("#c-ws").select_option("/x/code")
        p.locator("#c-text").fill("do the thing")
        p.locator("#c-send").click()
        expect(p.locator("#c-note")).to_contain_text("{folder}")
        assert short.calls() == []
        ctx.close()
    finally:
        short.close()
    (tmp_path / "full").mkdir()
    full = Rig(tmp_path / "full", monkeypatch)
    try:
        _with_opaque_code(full)
        status, _ = full.post("/api/start", good)
        assert status == 200 and full.calls() == [
            ["start", "session", "code", "/x/code", "here", "claude-code", "opus", "do it"]
        ]
    finally:
        full.close()
