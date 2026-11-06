"""The web terminal's page: HTML, the stylesheet and the script, as strings, and the policy that lets it run.

The page is one terminal filling the screen, a key bar under it, and a banner that only appears when the connection
changes. What it keeps still, and how:

- One size owner. The grid is fitted once after the fonts load, before the socket opens, so the first size is the
  final one. Later changes are debounced (150 ms) and the final size alone is sent; a repeat of the last size is not
  sent. There is no size label anywhere.
- The grid fills what is visible. Its rows are the visual viewport's height (`innerHeight` where there is none) less
  the key bar, over the measured cell height, so there is no blank band above it; the page is that tall, fixed, and
  never scrolls as a document. The key bar sits under the grid, clear of the home-indicator inset.
- The on-screen keyboard doesn't resize the grid. The viewport meta says `interactive-widget=resizes-visual`; the page
  follows `visualViewport` to keep the key bar above the keyboard, and the grid stays anchored to the bottom so the
  prompt line stays in view. On a touch device the grid is sized to the tallest the visible height has been at this
  width: it grows when the first layout was short or a browser toolbar slides away, never when a keyboard shrinks the
  view. A new width (orientation) starts again, and on a mouse-and-keyboard device every change refits.
- Input is bytes. Typing, the key bar and paste go out as binary frames; Shift+Enter and Ctrl+Enter send `ESC CR`;
  the Paste button reads the clipboard (or opens a box when the browser won't) and pastes through the terminal, so
  bracketed paste markers are added when the program asked for them.
- The link's token is read once and removed from the address bar (`history.replaceState`), so it isn't left in the
  address bar or the history; reconnecting reuses it. A program's clipboard escape can set the clipboard but not read it.
- A vertical touch drag becomes SGR wheel events when the program has mouse mode on (otherwise the terminal's own
  scrollback scrolls), and the wheel does the same through the terminal itself.

The policy is exact: the vendored terminal script runs by its sha256 (the tag carries the same hash as `integrity`),
the page's own script and stylesheet by a per-response nonce, and nothing else loads. The one loosening is
`style-src-attr 'unsafe-inline'`: the terminal colours a cell with a `style` attribute (a true-colour cell can't be a
class), and without it a coloured screen draws plain. It can't run script or load anything, since the rest of the policy
allows neither. The page holds no environment
values: its only data is the key table, the colours and fixed settings.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
from typing import Any

from ws import brand
from ws.term import keys
from ws.ui.css import FONT_MONO, FONT_SANS

__all__ = ["ASSET_PATH", "DEBOUNCE_MS", "RESET", "SOCKET_PATH", "page_html", "policy", "script_integrity"]

SOCKET_PATH = "/socket"
ASSET_PATH = "/assets/terminal.js"
DEBOUNCE_MS = 150
# Written to the terminal when the program exits, so a program that died with a mode on can't leave the page in it:
# mouse reporting, focus reporting, bracketed paste and synchronized output off, the main screen, the cursor back.
RESET = "\x1b[?1000l\x1b[?1002l\x1b[?1003l\x1b[?1006l\x1b[?1004l\x1b[?2004l\x1b[?2026l\x1b[?1049l\x1b[?25h"


def script_integrity(script: bytes) -> str:
    """The `sha256-...` value for a script, as `integrity` and the policy write it."""
    return "sha256-" + base64.b64encode(hashlib.sha256(script).digest()).decode("ascii")


def policy(nonce: str, script_hash: str) -> str:
    return (
        f"default-src 'none'; script-src '{script_hash}' 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
        "style-src-attr 'unsafe-inline'; connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def _theme(name: str) -> dict[str, str]:
    c = brand.colour
    return {
        "background": c("bg", name),
        "foreground": c("text", name),
        "cursor": c("accent-text", name),
        "cursorAccent": c("bg", name),
        "selectionBackground": c("tint", name),
    }


def _css(xterm_css: str) -> str:
    return f"""{brand.css()}
{xterm_css}
[hidden]{{display:none!important}}
html,body{{height:100%;margin:0;overflow:hidden;overscroll-behavior:none;background:var(--bg);color:var(--text);
font:14px/1.4 {FONT_SANS};-webkit-text-size-adjust:100%}}
@supports (height:100dvh){{html,body{{height:100dvh}}}}
#app{{position:fixed;left:0;right:0;top:var(--vv-top,0px);height:var(--vv-h,100%);display:flex;flex-direction:column}}
#stage{{position:relative;flex:1 1 0;min-height:0;overflow:hidden}}
#term{{position:absolute;left:0;right:0;bottom:0;height:100%;padding:0 4px;box-sizing:border-box}}
#term[data-mouse=on]{{touch-action:none}}
.xterm{{font-family:{FONT_MONO}}}
#keybar{{display:grid;grid-template-columns:repeat(auto-fit,minmax(56px,1fr));gap:6px;
padding:6px calc(6px + env(safe-area-inset-right)) calc(6px + env(safe-area-inset-bottom))
calc(6px + env(safe-area-inset-left));background:var(--panel);border-top:1px solid var(--line);flex:none}}
button{{font:600 14px {FONT_SANS};color:var(--text);cursor:pointer;touch-action:manipulation;user-select:none;
-webkit-user-select:none}}
#keybar button{{min-width:0;min-height:44px;padding:0 6px;border:1px solid var(--line2);
border-radius:8px;background:var(--raise)}}
#keybar button[aria-pressed=true]{{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}}
#banner,#sheet{{position:absolute;z-index:5;left:8px;right:8px;display:flex;gap:8px;align-items:center;padding:10px 12px;
background:var(--panel);border:1px solid var(--line2);border-radius:10px}}
#banner{{top:8px;justify-content:space-between}}
#sheet{{bottom:8px;flex-wrap:wrap}}
#sheet textarea{{flex:1 1 100%;min-height:96px;background:var(--bg);color:var(--text);border:1px solid var(--line2);
border-radius:8px;padding:8px;font:14px {FONT_MONO}}}
#banner button,#sheet button{{min-height:40px;padding:0 14px;border:1px solid var(--line2);border-radius:8px;
background:var(--raise)}}
"""


_SCRIPT = r"""
(() => {
'use strict';
const cfg = JSON.parse(document.getElementById('cfg').textContent);
const nonce = document.currentScript.nonce;
// xterm.js makes <style> elements as it runs; the policy lets in only those that carry this page's nonce.
const makeElement = document.createElement.bind(document);
document.createElement = (tag, opts) => {
  const el = makeElement(tag, opts);
  if (String(tag).toLowerCase() === 'style') el.setAttribute('nonce', nonce);
  return el;
};
const $ = (sel) => document.querySelector(sel);
const root = document.documentElement;
const coarse = matchMedia('(pointer: coarse)').matches;
const light = matchMedia('(prefers-color-scheme: light)').matches;
const rows = Object.fromEntries(cfg.keybar.map((r) => [r.action, r]));
const termEl = $('#term');
const keybar = $('#keybar');
const banner = $('#banner');
const sheet = $('#sheet');
const encoder = new TextEncoder();

const term = new Terminal({
  cursorBlink: true,
  fontSize: coarse ? 13 : 14,
  fontFamily: cfg.font,
  scrollback: 5000,
  allowProposedApi: true,
  theme: cfg.themes[light ? 'light' : 'dark'],
  linkHandler: { activate: (_e, uri) => { if (/^https?:/i.test(uri)) window.open(uri, '_blank', 'noopener'); } },
});
const fit = new FitAddon.FitAddon();
term.loadAddon(fit);
// A program may set the clipboard (OSC 52 write) but never read it: a read answers with nothing.
term.loadAddon(new ClipboardAddon.ClipboardAddon(undefined, {
  readText: () => '',
  writeText: (selection, text) => (selection === 'c' ? navigator.clipboard.writeText(text) : Promise.resolve()),
}));
term.loadAddon(new WebLinksAddon.WebLinksAddon((_e, uri) => window.open(uri, '_blank', 'noopener')));
term.open(termEl);

// ---- the socket
// The link's token goes into a variable once and out of the address bar and history; a reconnect reuses it.
const token = new URLSearchParams(location.search).get('t') || '';
if (location.search) history.replaceState(null, '', location.pathname);
let sock = null;
let sent = { cols: 0, rows: 0 };
let state = 'connecting';
let tries = 0;
let retry = null;

function setState(next, message) {
  state = next;
  document.body.dataset.state = next;
  banner.hidden = !message;
  $('#banner-text').textContent = message || '';
  $('[data-action=reconnect]').hidden = next === 'ended';
}
function send(bytes) { if (sock && sock.readyState === 1) sock.send(bytes); }
function sendText(text) { send(encoder.encode(text)); }
function sendResize(force) {
  const { cols, rows: r } = term;
  if (!force && cols === sent.cols && r === sent.rows) return;
  sent = { cols, rows: r };
  if (sock && sock.readyState === 1) sock.send(JSON.stringify({ type: 'resize', cols, rows: r }));
}
function connect() {
  clearTimeout(retry);
  if (sock && sock.readyState < 2) return;
  const scheme = location.protocol === 'https:' ? 'wss:' : 'ws:';
  const s = new WebSocket(`${scheme}//${location.host}${cfg.socket}?t=${encodeURIComponent(token)}`);
  s.binaryType = 'arraybuffer';
  sock = s;
  s.onopen = () => {
    tries = 0;
    setState('open', '');
    sendResize(true);
  };
  s.onmessage = (ev) => {
    if (typeof ev.data !== 'string') { term.write(new Uint8Array(ev.data)); return; }
    let msg = null;
    try { msg = JSON.parse(ev.data); } catch (_) { return; }
    if (msg.type === 'exit') {
      term.write(cfg.reset);
      setState('ended', `The session ended (exit code ${msg.code}).`);
    } else if (msg.type === 'error') {
      setState('ended', String(msg.message || 'The session could not start.'));
    }
  };
  s.onclose = (ev) => {
    if (sock !== s) return;
    if (state === 'ended') return;
    if (ev.code === 4000) { setState('replaced', 'Another window took this session over.'); return; }
    tries += 1;
    if (tries > cfg.max_tries) { setState('closed', "Can't reach ws term. Open the link it printed again."); return; }
    setState('closed', 'Reconnecting…');
    retry = setTimeout(connect, Math.min(500 * 2 ** (tries - 1), 5000));
  };
}

// ---- size: one owner, one debounced resize, no label
// The grid is as many rows as fit in what is really visible: the visual viewport's height (the layout one, where the
// browser has no visual one) less the key bar, over the measured cell height. The page is that tall and never scrolls.
const vv = window.visualViewport;
const viewHeight = () => Math.round(vv ? vv.height : innerHeight);
let fitTimer = null;
let lastWidth = innerWidth;
// On a touch device a height that only shrinks is the keyboard, so the grid is sized to the tallest the visible height
// has been at this width: a first layout that was short, or a toolbar that slides away, grows it; a keyboard doesn't.
let tallest = 0;
function followViewport() {
  root.style.setProperty('--vv-h', `${viewHeight()}px`);
  root.style.setProperty('--vv-top', `${vv ? vv.offsetTop : 0}px`);
}
function doFit() {
  fitTimer = null;
  followViewport();
  tallest = coarse ? Math.max(tallest, viewHeight()) : viewHeight();
  const dims = fit.proposeDimensions();
  const screen = termEl.querySelector('.xterm-screen');
  const cell = screen && term.rows ? screen.getBoundingClientRect().height / term.rows : 0;
  if (!dims || !cell) {
    fit.fit();
  } else {
    const rows = Math.max(1, Math.floor((tallest - keybar.offsetHeight) / cell));
    if (dims.cols !== term.cols || rows !== term.rows) term.resize(dims.cols, rows);
  }
  if (screen) termEl.style.height = `${screen.getBoundingClientRect().height}px`; // bottom-anchored: the prompt stays in view
  sendResize(false);
}
function scheduleFit() { clearTimeout(fitTimer); fitTimer = setTimeout(doFit, cfg.debounce_ms); }
function viewportChanged() {
  followViewport(); // the key bar rides above the on-screen keyboard
  if (innerWidth !== lastWidth) { // turning the phone: a new width, so the height starts again
    lastWidth = innerWidth;
    tallest = 0;
    scheduleFit();
  } else if (!coarse || viewHeight() > tallest) {
    scheduleFit();
  }
}
addEventListener('resize', viewportChanged);
addEventListener('orientationchange', viewportChanged);
if (vv) {
  vv.addEventListener('resize', viewportChanged);
  vv.addEventListener('scroll', followViewport);
}

// ---- keys
let ctrl = false;
function setCtrl(on) {
  ctrl = on;
  $('[data-action=ctrl]').setAttribute('aria-pressed', String(on));
}
function ctrlByte(ch) {
  const low = ch.toLowerCase();
  if (low >= 'a' && low <= 'z') return String.fromCharCode(low.charCodeAt(0) & 31);
  if (Object.prototype.hasOwnProperty.call(cfg.ctrl_punct, ch)) return String.fromCharCode(cfg.ctrl_punct[ch]);
  return ch;
}
function sendInput(text) {
  if (text.length === 1 && ctrl) { text = ctrlByte(text); setCtrl(false); }
  sendText(text);
}
term.onData(sendInput);
term.onBinary((text) => send(Uint8Array.from(text, (c) => c.charCodeAt(0) & 255)));
term.attachCustomKeyEventHandler((ev) => {
  // Browsers send a plain Enter for Shift+Enter and Ctrl+Enter; a newline in a prompt is ESC CR.
  if (ev.key === 'Enter' && (ev.shiftKey || ev.ctrlKey) && !ev.altKey && !ev.metaKey) {
    if (ev.type === 'keydown') { ev.preventDefault(); sendText(cfg.shift_enter); }
    return false;
  }
  return true;
});
term.onTitleChange((t) => { document.title = t ? `${t} - ws term` : 'ws term'; });

// ---- the key bar and the banner
async function pasteFromClipboard() {
  try {
    const text = await navigator.clipboard.readText();
    if (text) term.paste(text);
  } catch (_) {
    sheet.hidden = false; // the browser won't hand over the clipboard here: paste into a box instead
    $('[data-field=paste-text]').focus();
  }
}
function act(action) {
  if (action === 'ctrl') { setCtrl(!ctrl); return; }
  if (action === 'paste') { pasteFromClipboard(); return; }
  if (action === 'paste-send') {
    const box = $('[data-field=paste-text]');
    if (box.value) term.paste(box.value);
    box.value = '';
    sheet.hidden = true;
    return;
  }
  if (action === 'paste-cancel') { $('[data-field=paste-text]').value = ''; sheet.hidden = true; return; }
  if (action === 'reconnect') { tries = 0; setState('connecting', ''); connect(); return; }
  const row = rows[action];
  if (row && row.bytes !== null) {
    sendInput(row.bytes);
    if (ctrl) setCtrl(false); // a key from the bar uses up Ctrl, whatever its length
  }
}
document.addEventListener('mousedown', (e) => { if (e.target.closest('#keybar,#banner')) e.preventDefault(); });
document.addEventListener('click', (e) => {
  const el = e.target.closest('[data-action]');
  if (el) act(el.dataset.action);
});

// ---- scroll: a touch drag is wheel events when the program has the mouse
const SGR = (button, col, row) => `\x1b[<${button};${col};${row}M`;
let touchY = null;
let carried = 0;
function syncMouse() { termEl.dataset.mouse = term.modes.mouseTrackingMode === 'none' ? 'off' : 'on'; }
term.onWriteParsed(syncMouse);
termEl.addEventListener('touchstart', (e) => {
  touchY = e.touches.length === 1 ? e.touches[0].clientY : null;
  carried = 0;
}, { passive: true });
termEl.addEventListener('touchmove', (e) => {
  if (touchY === null || e.touches.length !== 1 || term.modes.mouseTrackingMode === 'none') return;
  e.preventDefault();
  const t = e.touches[0];
  const screen = termEl.querySelector('.xterm-screen').getBoundingClientRect();
  const cellH = screen.height / term.rows;
  const cellW = screen.width / term.cols;
  carried += t.clientY - touchY;
  touchY = t.clientY;
  const col = Math.min(term.cols, Math.max(1, Math.floor((t.clientX - screen.left) / cellW) + 1));
  const row = Math.min(term.rows, Math.max(1, Math.floor((t.clientY - screen.top) / cellH) + 1));
  while (Math.abs(carried) >= cellH) {
    sendText(SGR(carried > 0 ? 64 : 65, col, row)); // a finger moving down scrolls the content up
    carried -= Math.sign(carried) * cellH;
  }
}, { passive: false });
termEl.addEventListener('touchend', () => { touchY = null; }, { passive: true });

// ---- notifications: OSC 9 and OSC 777 become a browser notification when the person allowed it
function notify(title, body) {
  if (typeof Notification === 'undefined' || Notification.permission !== 'granted') return;
  new Notification(title, body ? { body } : undefined);
}
term.parser.registerOscHandler(9, (data) => { notify(data); return true; });
term.parser.registerOscHandler(777, (data) => {
  const [kind, title, ...rest] = data.split(';');
  if (kind === 'notify') notify(title || 'ws term', rest.join(';'));
  return true;
});
addEventListener('pointerdown', () => {
  if (typeof Notification !== 'undefined' && Notification.permission === 'default') Notification.requestPermission();
}, { once: true });

addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && state === 'closed') { tries = 0; connect(); }
});
window.wsTerm = { term, fit };

followViewport();
(document.fonts ? document.fonts.ready : Promise.resolve()).then(() => requestAnimationFrame(() => {
  doFit();
  connect();
  term.focus();
}));
})();
"""


def _keybar_html() -> str:
    parts = []
    for row in keys.KEYBAR:
        pressed = ' aria-pressed="false"' if row["action"] == "ctrl" else ""
        parts.append(
            f'<button type="button" data-action="{html.escape(row["action"] or "")}"{pressed} '
            f'title="{html.escape(row["title"] or "")}" aria-label="{html.escape(row["title"] or "")}">'
            f"{html.escape(row['label'] or '')}</button>"
        )
    return "".join(parts)


def _config() -> dict[str, Any]:
    return {
        "keybar": keys.keybar_json(),
        "ctrl_punct": keys.CTRL_PUNCT,
        "shift_enter": keys.SHIFT_ENTER,
        "socket": SOCKET_PATH,
        "debounce_ms": DEBOUNCE_MS,
        "max_tries": 40,
        "reset": RESET,
        "font": FONT_MONO,
        "themes": {"dark": _theme("dark"), "light": _theme("light")},
    }


def page_html(nonce: str, *, token: str, xterm_css: str, integrity: str) -> str:
    """The page for one response. `integrity` is the `sha256-...` of the vendored script the policy allows."""
    cfg = json.dumps(_config()).replace("<", "\\u003c")
    return (
        '<!doctype html>\n<html lang="en"><head><meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, interactive-widget=resizes-visual">\n'
        '<meta name="referrer" content="no-referrer"><meta name="robots" content="noindex">\n'
        f'<title>ws term</title><style nonce="{html.escape(nonce)}">{_css(xterm_css)}</style></head>\n'
        '<body data-state="connecting">\n'
        '<div id="app"><div id="stage"><div id="term"></div>\n'
        '<div id="banner" hidden role="status"><span id="banner-text"></span>'
        '<button type="button" data-action="reconnect">Reconnect</button></div>\n'
        '<div id="sheet" hidden><textarea data-field="paste-text" aria-label="Text to paste" '
        'placeholder="Paste here"></textarea>'
        '<button type="button" data-action="paste-send">Paste</button>'
        '<button type="button" data-action="paste-cancel">Cancel</button></div></div>\n'
        f'<div id="keybar" role="toolbar" aria-label="Keys">{_keybar_html()}</div></div>\n'
        f'<script type="application/json" id="cfg">{cfg}</script>\n'
        f'<script src="{ASSET_PATH}?t={html.escape(token)}" integrity="{html.escape(integrity)}"></script>\n'
        f'<script nonce="{html.escape(nonce)}">{_SCRIPT}</script>\n'
        "</body></html>"
    )
