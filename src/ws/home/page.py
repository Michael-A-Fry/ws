"""The home page: the areas in the order the person chose, in the shared ws look, from a snapshot and nothing else.

`render(snap, nonce, live=...)` returns the whole page as one string: Lead pinned and a rule under it, then Needs you,
Sessions and Evaluation, then a closed list of the sources that were read. It reads only the records it is given (and
`ws.ui`), so a page drawn from a file shows what a page served on loopback shows. Rendering changes nothing.
"""

from __future__ import annotations

from ws.home import areas
from ws.home.areas import _fmt
from ws.home.records import Snapshot, SourceRow
from ws.ui import components, css

__all__ = ["CSS", "SCRIPT", "SCRIPT_LIVE", "render"]

# The page's own rules, after the shared ones. Colours are `var(--role)` from `ws.brand`; no `style=` anywhere.
CSS = (
    css.PANEL
    + """
.top{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:18px;padding-bottom:6px}
.brand{display:flex;align-items:center;gap:14px;min-width:0}
.logo{margin:0;font:700 9px/9px ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace;
white-space:pre;letter-spacing:0}
.logo i{font-style:normal}
.top .logo{font-size:5px;line-height:5px}
.brand b{font-size:15px;font-weight:650}
.ghost{color:var(--muted);padding:5px 10px;border-radius:7px}.ghost:hover{background:var(--raise);color:var(--text)}
.head{padding-top:14px;padding-bottom:22px}
h1{font-size:30px;line-height:1.2;font-weight:650;letter-spacing:-.015em;margin:0 0 6px}
.sub{color:var(--muted);margin:0;overflow-wrap:anywhere}
main.wrap{padding-bottom:64px}
.lead{margin:0}
.lead-h{margin:0 0 8px;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
.lead .list{background:var(--tint);border:1px solid var(--accent);border-radius:14px;overflow:hidden}
.sep{border:0;border-top:1px solid var(--line);margin:24px 0}
.list{list-style:none;margin:0;padding:0}
.list>li{display:grid;grid-template-columns:132px minmax(0,1fr) auto auto;gap:6px 18px;align-items:start;
padding:12px 18px;border-top:1px solid var(--line)}
.list>li:first-child{border-top:0}
.list>li.ask{grid-template-columns:132px minmax(0,1fr) auto}
.c-who{min-width:0;display:flex;flex-direction:column;gap:3px}
.nm{font-weight:600;overflow-wrap:anywhere}
.where{color:var(--muted);font-size:13px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
overflow-wrap:anywhere}
.note{color:var(--muted);font-size:13px}
.c-num{display:flex;flex-direction:column;align-items:flex-end;gap:1px;min-width:116px;color:var(--muted);
font-size:13px;font-variant-numeric:tabular-nums;white-space:nowrap}
.c-num span:first-child{color:var(--text)}
.c-open{display:flex;justify-content:flex-end;min-width:0}
.acts{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end}
.act{border:1px solid var(--line2);border-radius:999px;padding:3px 12px;font-size:13px;color:var(--text);
text-decoration:none;white-space:nowrap;background:none}
.act:hover{border-color:var(--accent);background:var(--raise)}
.act:disabled{opacity:.6;cursor:default}
.act.off{color:var(--muted)}
code.cmd{font-size:12.5px;background:var(--raise);color:var(--text);padding:3px 8px;border-radius:5px;
overflow-wrap:anywhere}
.meta{display:flex;flex-wrap:wrap;gap:6px}
.chip{display:inline-block;border:1px solid var(--line);border-radius:999px;padding:0 9px;font-size:12px;
line-height:20px;color:var(--muted);overflow-wrap:anywhere}
.opts{list-style:none;margin:6px 0 0;padding:0;display:grid;gap:3px;font-size:13.5px}
.opts .d{display:block;color:var(--muted);font-size:13px}
.rec{color:var(--ok);font-size:12px;margin-left:8px;font-weight:550}
.empty{margin:0;padding:16px 18px;color:var(--muted)}
.host{margin:0;padding:12px 18px 0;font-size:12px;font-weight:600;letter-spacing:.06em;text-transform:uppercase;
color:var(--muted)}
.fold>summary{cursor:pointer;padding:11px 18px;color:var(--muted);border-top:1px solid var(--line)}
.fold>summary:hover{color:var(--text)}
.arms{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(300px,100%),1fr));gap:14px;padding:16px 18px}
.arm{background:var(--bg);border:1px solid var(--line);border-radius:12px;padding:16px;min-width:0}
.arm header{display:flex;align-items:baseline;justify-content:space-between;gap:10px}
.arm h3{margin:0;font-size:16px;font-weight:650}
.job{color:var(--muted);font-size:12.5px;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.rate{display:flex;align-items:baseline;gap:10px;flex-wrap:wrap;margin:8px 0 2px}
.big{font-size:30px;line-height:1.2;font-weight:650;letter-spacing:-.015em;font-variant-numeric:tabular-nums}
.big.off{font-size:18px;color:var(--muted)}
.of{color:var(--muted);font-size:13px}
.stats{display:grid;grid-template-columns:1fr 1fr;gap:12px 16px;margin:14px 0}
.stat{display:flex;flex-direction:column;min-width:0}
.stat .k{color:var(--muted);font-size:12.5px}
.stat .v{font-weight:600;font-variant-numeric:tabular-nums;overflow-wrap:anywhere}
.stat .n{color:var(--muted);font-size:12px;overflow-wrap:anywhere}
.trend{display:flex;align-items:center;gap:12px}
.arm .spark{width:160px;height:32px}
.cap{color:var(--muted);font-size:12.5px}
.band{margin:10px 0 0;color:var(--muted);font-size:12.5px}
.gap{margin:0;padding:0 18px 16px}
.srcs{padding:4px 0 8px}
.srcs>li{display:grid;grid-template-columns:132px 150px minmax(0,1fr);gap:4px 18px;padding:5px 18px;font-size:13px}
.srcs .nm{font-weight:500;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
.srcs .why{color:var(--muted);overflow-wrap:anywhere}
.foot{margin:0;padding:6px 18px 14px;color:var(--muted);font-size:13px}
.panel>details>summary{cursor:pointer;display:flex;justify-content:space-between;padding:12px 18px;font-weight:600;
font-size:14px;list-style:none}
.panel>details>summary::-webkit-details-marker{display:none}
.panel>details>summary span{color:var(--muted);font-weight:400;font-size:13px}
@media (max-width:700px){
.wrap{padding-left:16px;padding-right:16px}
h1{font-size:24px}
.list>li,.list>li.ask{grid-template-columns:minmax(0,1fr);padding:12px 14px}
.c-num{flex-direction:row;flex-wrap:wrap;align-items:baseline;gap:4px 14px;min-width:0}
.c-open,.acts{justify-content:flex-start}
.srcs>li{grid-template-columns:minmax(0,1fr);padding:6px 14px}
.stats{grid-template-columns:1fr 1fr}
.arms{padding:14px}
.panel h2,.panel>details>summary,.empty,.host,.fold>summary,.gap,.foot{padding-left:14px;padding-right:14px}
}
"""
)

_THEME = """
const root = document.documentElement, tb = document.getElementById('theme');
const dark = () => (root.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')) === 'dark';
const label = () => { tb.textContent = dark() ? 'Light' : 'Dark'; };
try { const t = localStorage.getItem('ws-theme'); if (t) root.dataset.theme = t; } catch (_) {}
tb.addEventListener('click', () => {
  root.dataset.theme = dark() ? 'light' : 'dark'; label();
  try { localStorage.setItem('ws-theme', root.dataset.theme); } catch (_) {}
});
label();
"""

# What a page served on loopback adds: the open buttons post to the server with the token the address carries, and the
# page reloads itself while it is in view.
_LIVE = """
const token = new URLSearchParams(location.search).get('t') || '';
document.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-opener]');
  if (!b) return;
  const was = b.textContent; b.disabled = true;
  try {
    const r = await fetch('/api/open', { method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-Ws-Token': token },
      body: JSON.stringify({ opener: b.dataset.opener, kind: b.dataset.kind, id: b.dataset.id }) });
    const j = await r.json();
    b.textContent = r.ok ? 'Done' : (j.error || 'It did not work');
  } catch (_) { b.textContent = 'It did not work'; }
  setTimeout(() => { b.textContent = was; b.disabled = false; }, 2500);
});
setInterval(() => { if (!document.hidden) location.reload(); }, 45000);
"""

SCRIPT = _THEME
SCRIPT_LIVE = _THEME + _LIVE

_SOURCE_STATUS = {"unavailable": ("idle", "not here")}


def _source(row: SourceRow) -> str:
    if row.ok:
        mark = components.status("ok", "read")
        why = f"{row.n} record{'s' if row.n != 1 else ''}"
    else:
        state, word = _SOURCE_STATUS.get(row.kind, ("warn", row.kind.replace("_", " ")))
        mark, why = components.status(state, word), row.why
    return f'<li><span class="nm">{_fmt.esc(row.name)}</span>{mark}<span class="why">{_fmt.esc(why)}</span></li>'


def _sources(snap: Snapshot) -> str:
    """The sources that were read, folded: a person looks here only when a number on the page looks wrong."""
    rows = snap.sources
    bad = sum(1 for r in rows if not r.ok and r.kind != "unavailable")
    aside = f"{len(rows)} read" if not bad else f"{bad} need a look"
    body = f'<ul class="list srcs">{"".join(_source(r) for r in rows)}</ul>'
    return (
        '<section class="panel"><details><summary>Sources'
        f"<span>{_fmt.esc(aside)}</span></summary>{body}</details></section>"
    )


def _summary(snap: Snapshot) -> str:
    live = sum(1 for s in snap.sessions if s.state in ("working", "waiting", "idle"))
    parts = [
        f"{live} live session{'s' if live != 1 else ''}",
        f"{len(snap.asks)} waiting for you",
        f"{len(snap.evals)} eval arm{'s' if len(snap.evals) != 1 else ''}",
    ]
    when = f"{snap.ts[:10]} {snap.ts[11:16]} UTC" if len(snap.ts) >= 16 else snap.ts
    return f"{snap.host or 'this machine'} · " + " · ".join(parts) + f" · as of {when}"


def render(snap: Snapshot, nonce: str = "", *, live: bool = False) -> str:
    """The whole page. `live` is True when a server stands behind it (open buttons and reload); `nonce` is the one in
    that response's Content-Security-Policy."""
    view = areas.View(snap, live)
    body = "".join(areas.MODULES[s.id].render(view) for s in areas.built())
    head = (
        f'<header class="wrap top"><div class="brand">{components.mark()}<b>home</b></div>'
        '<button type="button" class="ghost" id="theme">Light</button></header>'
        f'<div class="wrap head"><h1>ws home</h1><p class="sub">{_fmt.esc(_summary(snap))}</p></div>'
    )
    main = f'<main class="wrap" id="main">{body}{_sources(snap)}</main>'
    return components.page("ws home", head + main, nonce, CSS, script=SCRIPT_LIVE if live else SCRIPT)
