"""The web terminal in a real browser (headless Chromium through Playwright): a desktop and a phone, every control.

The server is the real one (`ws.term.app`) with a fake program behind it (`ws.term.testing.FakeChild`), so a test
types in the page and reads the bytes the program received, and pushes bytes the way a full-screen program would. A
missing Playwright or browser skips the tests, unless WS_E2E_REQUIRE=1 (CI), when it fails.

To run them on a machine without Playwright in the project: `uv run --locked --with playwright pytest -q <this file>`
(and `playwright install chromium` once, or point PLAYWRIGHT_BROWSERS_PATH at an existing install).
"""

from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path

import pytest

from ws.term import app, bridge, keys
from ws.term.testing import FakeChild

REQUIRE = os.environ.get("WS_E2E_REQUIRE") == "1"
sync_api = pytest.importorskip("playwright.sync_api") if not REQUIRE else __import__("playwright.sync_api").sync_api

# Browsers: PLAYWRIGHT_BROWSERS_PATH if set, else Playwright's own default folder. Nothing here looks in a home folder.

ESC = "\x1b"
PROFILES = {
    "desktop": {"viewport": {"width": 1440, "height": 900}},
    "phone": {
        "viewport": {"width": 390, "height": 844},
        "is_mobile": True,
        "has_touch": True,
        "device_scale_factor": 2,
    },
}

# Every control the page draws. A test below names each one; `test_every_control_has_a_test` fails on any it doesn't.
BYTE_ACTIONS = [r["action"] for r in keys.KEYBAR if r["bytes"] is not None]
OTHER_ACTIONS = ["ctrl", "paste", "paste-send", "paste-cancel", "reconnect"]
TESTED_ACTIONS = set(BYTE_ACTIONS) | set(OTHER_ACTIONS)

# Runs in every page before its own script: spies on what the page sends, stubs the clipboard and notifications, and
# collects policy violations.
INIT = r"""
(() => {
  window.__sent = []; window.__csp = []; window.__clip = []; window.__notes = [];
  window.__cliptext = ''; window.__clipdeny = false; window.__clipreads = 0;
  const Real = window.WebSocket;
  window.WebSocket = new Proxy(Real, { construct(target, args) {
    const ws = new target(...args);
    const send = ws.send.bind(ws);
    ws.send = (d) => {
      if (typeof d === 'string') window.__sent.push({ t: 'text', v: d });
      else window.__sent.push({ t: 'bin', v: Array.from(d, (b) => b.toString(16).padStart(2, '0')).join('') });
      return send(d);
    };
    return ws;
  } });
  document.addEventListener('securitypolicyviolation', (e) => window.__csp.push(e.violatedDirective + ' ' + e.blockedURI));
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: {
    writeText: async (t) => { window.__clip.push(t); },
    readText: async () => { window.__clipreads += 1; if (window.__clipdeny) throw new Error('denied'); return window.__cliptext; },
  } });
  window.Notification = class { static permission = 'granted'; constructor(t, o) { window.__notes.push([t, o ? o.body : null]); } };
})();
"""


def wait_for(cond, seconds: float = 5.0, what: str = "the condition"):
    end = time.monotonic() + seconds
    last = None
    while time.monotonic() < end:
        try:
            last = cond()
        except Exception as e:  # noqa: BLE001 - a page still loading raises; keep waiting
            last = e
        if last and not isinstance(last, Exception):
            return last
        time.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what} (last: {last!r})")


class Term:
    """The server under test, with its one fake program."""

    def __init__(self):
        self.children: list[FakeChild] = []
        self.session = bridge.Session(self._spawn)
        self.server = app.build_server(self.session, idle_seconds=600)
        self.thread = threading.Thread(target=self.server.serve, daemon=True)
        self.thread.start()
        self.url = self.server.url

    def _spawn(self, cols: int, rows: int) -> FakeChild:
        self.children.append(FakeChild())
        return self.children[-1]

    @property
    def child(self) -> FakeChild:
        return wait_for(lambda: self.children[0] if self.children else None, what="the program to start")

    def stop(self) -> None:
        self.session.close()
        self.server.stop()
        self.thread.join(5)


@pytest.fixture(scope="module")
def browser():
    try:
        pw = sync_api.sync_playwright().start()
        b = pw.chromium.launch()
    except Exception as e:
        if REQUIRE:
            raise
        pytest.skip(f"no Chromium for Playwright: {e}")
    yield b
    b.close()
    pw.stop()


@pytest.fixture(params=sorted(PROFILES))
def profile(request):
    return request.param


@pytest.fixture
def term():
    t = Term()
    yield t
    t.stop()


@pytest.fixture
def ctx(browser, profile):
    c = browser.new_context(**PROFILES[profile])
    c.add_init_script(INIT)
    yield c
    c.close()


class Open:
    """A page connected to the server, with helpers for what a person does and what the program saw."""

    def __init__(self, term: Term, ctx, profile: str):
        self.term, self.profile = term, profile
        self.page = ctx.new_page()
        self.errors: list[str] = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))
        self.page.on("console", lambda m: self.errors.append(m.text) if m.type == "error" else None)
        self.page.goto(term.url)
        wait_for(lambda: self.state() == "open", what="the page to connect")

    @property
    def phone(self) -> bool:
        return self.profile == "phone"

    def state(self) -> str:
        return self.page.evaluate("document.body.dataset.state")

    def press(self, action: str) -> None:
        sel = f'[data-action="{action}"]'
        if self.phone:
            self.page.tap(sel)
        else:
            self.page.click(sel)

    def grid(self) -> tuple[int, int]:
        return tuple(self.page.evaluate("[wsTerm.term.cols, wsTerm.term.rows]"))

    def sent(self) -> list[dict]:
        return self.page.evaluate("window.__sent")

    def resizes(self) -> list[dict]:
        return [json.loads(m["v"]) for m in self.sent() if m["t"] == "text" and '"resize"' in m["v"]]

    def screen(self) -> str:
        return self.page.inner_text(".xterm-rows")

    def typed(self, n: int | None = None) -> bytes:
        child = self.term.child
        if n is not None:
            wait_for(lambda: len(child.typed) >= n, what=f"{n} bytes to arrive")
        return child.typed

    def wait_typed(self, want: bytes) -> bytes:
        wait_for(lambda: self.term.child.typed == want, what=f"the program to receive {want!r}")
        return self.term.child.typed


@pytest.fixture
def web(term, ctx, profile):
    return Open(term, ctx, profile)


def settle(seconds: float = 0.7) -> None:
    time.sleep(seconds)  # longer than the 150 ms debounce, so a pending resize would have gone


# -- loading


def test_the_page_loads_connects_and_is_laid_out_for_the_screen(web, term, profile):
    assert web.page.evaluate("innerWidth") == PROFILES[profile]["viewport"]["width"], "not laid out at 980 px"
    assert web.page.title() == "ws term"
    assert web.page.evaluate("document.querySelector('#banner').hidden") is True
    assert web.page.evaluate("window.__csp") == [], "the policy blocked nothing"
    assert web.errors == []
    cols, rows = web.grid()
    assert term.child and term.session.size == (cols, rows), "the program started at the page's own size"
    assert web.page.get_attribute('meta[name="viewport"]', "content").endswith("interactive-widget=resizes-visual")


def test_the_link_token_is_taken_out_of_the_address_bar_and_a_reconnect_still_works(web, term, ctx):
    assert web.page.evaluate("location.search") == "" and "t=" not in web.page.evaluate("location.href")
    other = Open(term, ctx, web.profile)
    wait_for(lambda: web.state() == "replaced", what="the first window to be told")
    web.press("reconnect")
    web.page.bring_to_front()
    wait_for(lambda: web.state() == "open", what="the reconnect with the remembered token")
    assert web.page.evaluate("location.search") == ""
    wait_for(lambda: other.state() == "replaced", what="the second window to be told")
    web.page.keyboard.type("q")
    assert web.wait_typed(b"q")
    other.page.close()


def test_the_page_scrolls_and_zooms_nothing(web):
    assert web.page.evaluate("getComputedStyle(document.documentElement).overscrollBehaviorY") == "none"
    assert web.page.evaluate("document.documentElement.scrollHeight <= innerHeight + 1")
    assert web.page.evaluate("getComputedStyle(document.body).overflow") == "hidden"


def test_coloured_and_styled_output_is_drawn_with_its_colours_and_the_policy_blocks_nothing(web, term):
    term.child.emit(
        f"{ESC}[38;2;255;0;0mTRUECOLOUR{ESC}[0m {ESC}[38;5;34mPALETTE{ESC}[0m {ESC}[1;4mBOLDLINE{ESC}[0m "
        f"{ESC}[48;2;0;0;255m BG {ESC}[0m {ESC}[7mREVERSE{ESC}[0m".encode()
    )
    wait_for(lambda: "TRUECOLOUR" in web.screen() and "REVERSE" in web.screen(), what="the text")
    colour = web.page.evaluate(
        """() => { const s = [...document.querySelectorAll('.xterm-rows span')].find((e) => e.textContent.includes('TRUECOLOUR'));
          return getComputedStyle(s).color; }"""
    )
    assert colour == "rgb(255, 0, 0)", "the true-colour cell kept its style attribute"
    assert web.page.evaluate("window.__csp") == [] and web.errors == []


def test_output_from_the_program_is_drawn(web, term):
    term.child.emit(b"hello from the program\r\n$ ")
    wait_for(lambda: "hello from the program" in web.screen(), what="the text to be drawn")


def test_typing_reaches_the_program(web):
    web.page.keyboard.type("echo hi")
    assert web.wait_typed(b"echo hi") == b"echo hi"


def test_the_title_follows_the_program(web, term):
    term.child.emit(f"{ESC}]0;Agent session\x07".encode())
    wait_for(lambda: web.page.title() == "Agent session - ws term", what="the title")


# -- Enter, Shift+Enter, Ctrl+Enter


def test_shift_enter_and_ctrl_enter_send_escape_carriage_return_and_plain_enter_submits(web):
    web.page.keyboard.press("Shift+Enter")
    assert web.wait_typed(b"\x1b\r") == b"\x1b\r"
    web.page.keyboard.press("Control+Enter")
    assert web.wait_typed(b"\x1b\r\x1b\r")
    web.page.keyboard.press("Enter")
    assert web.wait_typed(b"\x1b\r\x1b\r\r")
    web.page.keyboard.press("Alt+Enter")
    assert web.wait_typed(b"\x1b\r\x1b\r\r\x1b\r")


# -- the key bar: every control, by its data-action


@pytest.mark.parametrize("action", BYTE_ACTIONS)
def test_each_key_bar_control_sends_its_bytes(web, action):
    expected = next(r["bytes"] for r in keys.KEYBAR if r["action"] == action).encode("latin-1")
    web.press(action)
    assert web.wait_typed(expected) == expected


def test_the_key_bar_keeps_focus_in_the_terminal_so_the_keyboard_stays_up(web):
    web.page.keyboard.press("a")
    web.press("tab")
    assert web.page.evaluate("document.activeElement.classList.contains('xterm-helper-textarea')")


def test_ctrl_is_sticky_for_one_key_and_toggles_off(web):
    pressed = lambda: web.page.get_attribute('[data-action="ctrl"]', "aria-pressed")
    assert pressed() == "false"
    web.press("ctrl")
    assert pressed() == "true"
    web.page.keyboard.type("c")
    assert web.wait_typed(b"\x03") == b"\x03"
    assert pressed() == "false", "it applied to one key"
    web.page.keyboard.type("c")
    assert web.wait_typed(b"\x03c")
    web.press("ctrl")
    web.press("ctrl")
    web.page.keyboard.type("d")
    assert web.wait_typed(b"\x03cd"), "pressed twice is off"
    web.press("ctrl")
    web.page.keyboard.type("[")
    assert web.wait_typed(b"\x03cd\x1b"), "Ctrl+[ is Escape"


def test_ctrl_then_a_key_bar_key_sends_the_key_and_disarms(web):
    web.press("ctrl")
    web.press("up")
    assert web.wait_typed(b"\x1b[A")
    assert web.page.get_attribute('[data-action="ctrl"]', "aria-pressed") == "false"
    web.page.keyboard.type("c")
    assert web.wait_typed(b"\x1b[Ac"), "the key from the bar used up Ctrl"


# -- paste


def paste_text(web, text: str) -> None:
    web.page.evaluate("(t) => { window.__cliptext = t; }", text)


def test_the_paste_button_pastes_the_clipboard_with_markers_when_the_program_asked_for_them(web, term):
    term.child.emit(f"{ESC}[?2004h".encode())
    wait_for(lambda: web.page.evaluate("wsTerm.term.modes.bracketedPasteMode"), what="bracketed paste on")
    paste_text(web, "line one\nline two")
    web.press("paste")
    want = f"{ESC}[200~line one\rline two{ESC}[201~".encode()
    assert web.wait_typed(want) == want


def test_the_paste_button_pastes_plain_text_when_bracketed_paste_is_off(web):
    paste_text(web, "echo plain")
    web.press("paste")
    assert web.wait_typed(b"echo plain")


def test_an_empty_clipboard_pastes_nothing(web):
    paste_text(web, "")
    web.press("paste")
    settle(0.3)
    assert web.typed() == b"" and web.page.evaluate("document.querySelector('#sheet').hidden") is True


def test_when_the_browser_refuses_the_clipboard_a_box_takes_the_paste(web):
    web.page.evaluate("window.__clipdeny = true")
    web.press("paste")
    wait_for(lambda: web.page.evaluate("!document.querySelector('#sheet').hidden"), what="the paste box")
    web.page.fill('[data-field="paste-text"]', "from the box")
    web.press("paste-send")
    assert web.wait_typed(b"from the box")
    assert web.page.evaluate("document.querySelector('#sheet').hidden") is True
    assert web.page.input_value('[data-field="paste-text"]') == ""


def test_the_paste_box_can_be_cancelled(web):
    web.page.evaluate("window.__clipdeny = true")
    web.press("paste")
    wait_for(lambda: web.page.evaluate("!document.querySelector('#sheet').hidden"), what="the paste box")
    web.page.fill('[data-field="paste-text"]', "never sent")
    web.press("paste-cancel")
    assert web.page.evaluate("document.querySelector('#sheet').hidden") is True
    assert web.page.input_value('[data-field="paste-text"]') == ""
    settle(0.3)
    assert web.typed() == b""


def test_a_ten_kilobyte_paste_arrives_bracketed_and_intact(web, term):
    term.child.emit(f"{ESC}[?2004h".encode())
    wait_for(lambda: web.page.evaluate("wsTerm.term.modes.bracketedPasteMode"), what="bracketed paste on")
    text = "\n".join(f"line {n}: the quick brown fox jumps over the lazy dog" for n in range(200))
    assert len(text) > 10_000
    web.page.evaluate(
        """(t) => {
          const dt = new DataTransfer(); dt.setData('text/plain', t);
          const el = document.querySelector('.xterm-helper-textarea');
          el.dispatchEvent(new ClipboardEvent('paste', { clipboardData: dt, bubbles: true, cancelable: true }));
        }""",
        text,
    )
    want = (f"{ESC}[200~" + text.replace("\n", "\r") + f"{ESC}[201~").encode()
    wait_for(lambda: len(term.child.typed) >= len(want), what="the whole paste")
    assert term.child.typed == want


# -- size: one owner, one debounced size, no label


def test_the_first_size_is_the_final_one_and_nothing_follows_it(web, term):
    settle(1.0)
    cols, rows = web.grid()
    assert web.resizes() == [{"type": "resize", "cols": cols, "rows": rows}], "one size sent, at the start"
    assert term.session.size == (cols, rows) and term.child.sizes == []
    # the grid is what fitting the container gives: nothing is left to adjust after the first resize
    assert web.page.evaluate("(() => { const d = wsTerm.fit.proposeDimensions(); return [d.cols, d.rows]; })()") == [
        cols,
        rows,
    ]


def test_no_element_ever_shows_a_size_label(web):
    def labels():
        return web.page.evaluate(
            """() => [...document.querySelectorAll('body *')]
              .filter((e) => /^\\s*\\d+\\s*[x\\u00d7]\\s*\\d+\\s*$/.test(e.textContent) && e.children.length === 0)
              .filter((e) => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
                return r.width > 0 && r.height > 0 && s.visibility !== 'hidden' && s.display !== 'none'; })
              .map((e) => e.textContent)"""
        )

    assert labels() == []
    for size in ((1000, 600), (700, 500), (1100, 700)):
        web.page.set_viewport_size({"width": size[0], "height": size[1]})
        time.sleep(0.12)  # inside the debounce window, where a label would show
        assert labels() == []
    settle()
    assert labels() == []


def test_a_window_resize_sends_exactly_one_size_after_the_debounce(web, term):
    if web.phone:
        pytest.skip("a phone's height changes are the keyboard; its width changes are covered below")
    settle()
    before = len(web.resizes())
    web.page.set_viewport_size({"width": 1000, "height": 620})
    settle()
    sizes = web.resizes()
    assert len(sizes) == before + 1
    assert (sizes[-1]["cols"], sizes[-1]["rows"]) == web.grid() and term.child.sizes[-1] == web.grid()


def test_a_burst_of_resizes_sends_only_the_final_size(web, term):
    if web.phone:
        pytest.skip("a phone's height changes are the keyboard")
    settle()
    before = len(web.resizes())
    for h in (880, 840, 800, 760, 720, 680):
        web.page.set_viewport_size({"width": 1300, "height": h})
        time.sleep(0.02)
    settle()
    assert len(web.resizes()) == before + 1
    assert web.resizes()[-1]["rows"] == web.grid()[1] == term.child.sizes[-1][1]


def test_a_resize_that_doesnt_change_the_grid_sends_nothing(web):
    if web.phone:
        pytest.skip("covered on the desktop profile")
    settle()
    before = len(web.resizes())
    w, h = web.page.evaluate("[innerWidth, innerHeight]")
    web.page.set_viewport_size({"width": w, "height": h - 1})
    settle()
    assert len(web.resizes()) == before


def test_a_phones_keyboard_opening_and_closing_sends_no_size(web, term):
    if not web.phone:
        pytest.skip("a phone behaviour")
    settle()
    cols_rows = web.grid()
    before = len(web.resizes())
    # iOS keeps the layout viewport and shrinks only the visual one: model that, then undo it
    web.page.evaluate(
        """() => {
          const vv = window.visualViewport;
          Object.defineProperty(vv, 'height', { configurable: true, get: () => 420 });
          vv.dispatchEvent(new Event('resize'));
        }"""
    )
    settle(0.5)
    app_box = web.page.evaluate(
        "(() => { const r = document.querySelector('#app').getBoundingClientRect(); return [r.top, r.height]; })()"
    )
    bar = web.page.evaluate("document.querySelector('#keybar').getBoundingClientRect().bottom")
    assert app_box[1] == 420 and bar <= 421, "the key bar rides above the keyboard"
    assert web.grid() == cols_rows and len(web.resizes()) == before and term.child.sizes == []
    web.page.evaluate(
        """() => { const vv = window.visualViewport; delete vv.height; vv.dispatchEvent(new Event('resize')); }"""
    )
    settle(0.5)
    assert (
        web.page.evaluate("getComputedStyle(document.querySelector('#app')).height")
        == f"{PROFILES['phone']['viewport']['height']}px"
    )
    assert web.grid() == cols_rows and len(web.resizes()) == before


def test_on_a_phone_a_shorter_layout_viewport_is_a_keyboard_and_sends_no_size(web):
    if not web.phone:
        pytest.skip("a phone behaviour")
    settle()
    before = len(web.resizes())
    for h in (540, 300, 844):
        web.page.set_viewport_size({"width": 390, "height": h})
        settle(0.4)
    assert len(web.resizes()) == before


def test_on_a_phone_turning_it_sideways_sends_one_new_size(web):
    if not web.phone:
        pytest.skip("a phone behaviour")
    settle()
    before = len(web.resizes())
    web.page.set_viewport_size({"width": 844, "height": 390})
    settle()
    sizes = web.resizes()
    assert len(sizes) == before + 1 and sizes[-1]["cols"] > sizes[0]["cols"]
    assert (sizes[-1]["cols"], sizes[-1]["rows"]) == web.grid()


# -- scroll: wheel and touch drag as SGR mouse events


MOUSE_ON = f"{ESC}[?1000h{ESC}[?1006h".encode()


def test_the_wheel_is_sent_as_sgr_mouse_events_when_the_program_has_the_mouse(web, term):
    if web.phone:
        pytest.skip("a mouse wheel is a desktop input")
    term.child.emit(MOUSE_ON)
    wait_for(lambda: web.page.evaluate("wsTerm.term.modes.mouseTrackingMode") != "none", what="mouse mode")
    web.page.mouse.move(400, 300)
    web.page.mouse.wheel(0, -120)
    wait_for(lambda: b"\x1b[<64;" in term.child.typed, what="a wheel-up report")
    web.page.mouse.wheel(0, 120)
    wait_for(lambda: b"\x1b[<65;" in term.child.typed, what="a wheel-down report")


def drag(web, x: int, y0: int, y1: int, steps: int = 6) -> None:
    """A one-finger vertical drag, through the browser's own touch input."""
    cdp = web.page.context.new_cdp_session(web.page)
    cdp.send("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [{"x": x, "y": y0}]})
    for i in range(1, steps + 1):
        y = y0 + (y1 - y0) * i / steps
        cdp.send("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [{"x": x, "y": y}]})
    cdp.send("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def test_a_touch_drag_is_wheel_events_when_the_program_has_the_mouse(web, term):
    if not web.phone:
        pytest.skip("a touch input")
    term.child.emit(MOUSE_ON)
    wait_for(lambda: web.page.get_attribute("#term", "data-mouse") == "on", what="the page to notice mouse mode")
    drag(web, 200, 300, 520)  # a finger moving down: the content scrolls up
    wait_for(lambda: b"\x1b[<64;" in term.child.typed, what="wheel-up reports")
    assert b"\x1b[<65;" not in term.child.typed
    reports = re.findall(rb"\x1b\[<64;(\d+);(\d+)M", term.child.typed)
    cols, rows = web.grid()
    assert len(reports) >= 5 and all(1 <= int(c) <= cols and 1 <= int(r) <= rows for c, r in reports)
    drag(web, 200, 520, 300)
    wait_for(lambda: b"\x1b[<65;" in term.child.typed, what="wheel-down reports")


def test_a_touch_drag_sends_nothing_when_the_program_has_no_mouse(web, term):
    if not web.phone:
        pytest.skip("a touch input")
    term.child.emit(b"plain screen\r\n")
    drag(web, 200, 300, 520)
    settle(0.4)
    assert term.child.typed == b""


# -- escapes from the program: notifications, clipboard, synchronized output


def test_a_notification_escape_becomes_a_browser_notification(web, term):
    term.child.emit(f"{ESC}]9;build finished\x07".encode())
    term.child.emit(f"{ESC}]777;notify;Agent;Needs your answer\x07".encode())
    wait_for(lambda: len(web.page.evaluate("window.__notes")) == 2, what="two notifications")
    assert web.page.evaluate("window.__notes") == [["build finished", None], ["Agent", "Needs your answer"]]


def test_a_clipboard_escape_sets_the_browsers_clipboard(web, term):
    payload = base64.b64encode(b"copied by the program").decode()
    term.child.emit(f"{ESC}]52;c;{payload}\x07".encode())
    wait_for(lambda: web.page.evaluate("window.__clip") == ["copied by the program"], what="the clipboard write")


def test_a_clipboard_read_escape_never_reaches_the_clipboard_or_the_program(web, term):
    planted = "planted-" + base64.b32encode(os.urandom(9)).decode().lower()
    web.page.evaluate("(v) => { window.__cliptext = v; }", planted)
    term.child.emit(f"{ESC}]52;c;?\x07{ESC}]52;p;?\x07{ESC}]52;;?\x07".encode())
    settle()
    assert web.page.evaluate("window.__clipreads") == 0, "the page never read the clipboard for the program"
    typed = term.child.typed
    assert planted.encode() not in typed and base64.b64encode(planted.encode()) not in typed
    web.page.keyboard.type("ok")  # the page still works, and what it sends is what the person typed
    assert web.wait_typed(typed + b"ok")


def test_the_bundled_terminal_honours_synchronized_output(web, term):
    term.child.emit(f"{ESC}[?2026h".encode())
    wait_for(lambda: web.page.evaluate("wsTerm.term.modes.synchronizedOutputMode") is True, what="mode 2026 on")
    term.child.emit(f"{ESC}[?2026l".encode())
    wait_for(lambda: web.page.evaluate("wsTerm.term.modes.synchronizedOutputMode") is False, what="mode 2026 off")


# -- the connection: another window, the program ending, a drop


def test_a_second_window_takes_over_and_the_first_can_take_it_back_with_reconnect(web, term, ctx):
    other = Open(term, ctx, web.profile)
    wait_for(lambda: web.state() == "replaced", what="the first window to be told")
    assert "Another window took this session over" in web.page.inner_text("#banner")
    assert web.page.evaluate("document.querySelector('#banner').hidden") is False
    other.page.keyboard.type("x")
    assert other.wait_typed(b"x")
    web.press("reconnect")
    web.page.bring_to_front()
    wait_for(lambda: web.state() == "open", what="the first window to reconnect")
    wait_for(lambda: other.state() == "replaced", what="the second window to be told")
    web.page.keyboard.type("y")
    assert web.wait_typed(b"xy")


def test_a_dropped_connection_comes_back_by_itself_and_the_program_redraws(web, term):
    wait_for(lambda: term.session.client is not None)
    term.session.client.ws.close(1012, "restarting")
    wait_for(lambda: web.state() in ("closed", "open"), what="the page to notice")
    wait_for(lambda: web.state() == "open" and term.child.winches >= 1, seconds=8, what="the page to reconnect")
    assert web.page.evaluate("document.querySelector('#banner').hidden") is True
    web.page.keyboard.type("z")
    assert web.wait_typed(b"z")


def test_when_the_program_exits_the_page_says_so_and_leaves_no_mode_behind(web, term):
    term.child.emit(f"{ESC}[?1049h{ESC}[?1000h{ESC}[?1006h{ESC}[?2004h{ESC}[?25l".encode())
    wait_for(lambda: web.page.evaluate("wsTerm.term.modes.mouseTrackingMode") != "none", what="mouse mode")
    term.child.finish(3)
    wait_for(lambda: web.state() == "ended", what="the page to show the end")
    assert "exit code 3" in web.page.inner_text("#banner")
    assert web.page.evaluate(
        "[wsTerm.term.modes.mouseTrackingMode, wsTerm.term.modes.bracketedPasteMode, wsTerm.term.buffer.active.type]"
    ) == ["none", False, "normal"]
    assert web.page.evaluate("document.querySelector('[data-action=reconnect]').hidden") is True


def test_a_program_that_could_not_start_is_explained_on_the_page(browser, profile):
    def broken(cols, rows):
        raise OSError("no such program")

    session = bridge.Session(broken)
    server = app.build_server(session)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    c = browser.new_context(**PROFILES[profile])
    try:
        page = c.new_page()
        page.goto(server.url)
        wait_for(lambda: page.evaluate("document.body.dataset.state") == "ended", what="the page to show the failure")
        assert "no such program" in page.inner_text("#banner")
    finally:
        c.close()
        server.stop()
        t.join(5)


def test_a_wrong_link_shows_the_refusal_not_a_page(browser, term, profile):
    c = browser.new_context(**PROFILES[profile])
    try:
        page = c.new_page()
        resp = page.goto(term.url.split("?")[0] + "?t=wrong")
        assert resp.status == 403 and "Open that link again" in page.content()
    finally:
        c.close()


# -- every control has a test


def test_every_control_has_a_test(web):
    in_page = set(web.page.evaluate("[...document.querySelectorAll('[data-action]')].map((e) => e.dataset.action)"))
    assert in_page == TESTED_ACTIONS, (
        f"untested: {sorted(in_page - TESTED_ACTIONS)}, gone: {sorted(TESTED_ACTIONS - in_page)}"
    )
    source = Path(__file__).read_text()
    for action in OTHER_ACTIONS:
        assert f'press("{action}")' in source, f"no test presses {action}"
