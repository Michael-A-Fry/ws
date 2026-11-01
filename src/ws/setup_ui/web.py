"""`ws setup --web`: the same steps and the same engine as the terminal, as one page served from this process.

Bound to 127.0.0.1 on a random port, no CDN, no external fonts. The address carries a token that the page keeps in
memory and sends back in an `X-Ws-Token` header on every call, so a page from another site cannot drive it; every
request also needs the right Host, and a POST needs a matching Origin. Nothing is written until Install, which calls
`ws.install.apply` here on the server. The server closes after Install, after "Close", or after ten idle minutes.
The colours come from `ws.brand`, and the page's one script and one stylesheet carry a nonce made for each response.
"""

from __future__ import annotations

import html
import json
import os
import secrets
import shutil
import tempfile
import threading
import time
import webbrowser
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ws import __version__, brand, install
from ws.setup_ui import flow

IDLE_SECONDS = 600
MAX_BODY = 64 * 1024
MAX_ROWS = 400  # rows of the file list sent to the page

# The colours come from `ws.brand`; the rules below name them as `var(--role)` and hold no colour of their own.
STYLE = """
*{box-sizing:border-box}
body{margin:0;background:radial-gradient(900px 280px at 6% -60px,var(--glow),transparent 70%) no-repeat,
var(--bg);color:var(--text);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;
min-height:100vh}
code,.mono,input[type=text]{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
button{font:inherit;color:inherit;background:none;border:0;cursor:pointer}
svg{display:block;flex:none}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.wrap{max-width:1200px;margin:0 auto;padding:0 28px}
.top{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;padding-top:18px;padding-bottom:14px}
.logo{margin:0;font:700 9px/9px ui-monospace,SFMono-Regular,Menlo,Consolas,"DejaVu Sans Mono",monospace;
white-space:pre;letter-spacing:0}
.logo i{font-style:normal}
.top .ghost{justify-self:end}
.ghost{color:var(--muted);padding:5px 10px;border-radius:7px}.ghost:hover{background:var(--raise);color:var(--text)}
.steps{display:flex;align-items:center;list-style:none;margin:0;padding:0;font-size:13px;color:var(--muted)}
.steps li{display:flex;align-items:center;gap:8px}.steps .mob{display:none;color:var(--muted)}
.steps li+li::before{content:"";width:30px;height:1px;background:var(--line2);margin:0 10px}
.steps .n{width:22px;height:22px;border-radius:50%;border:1px solid var(--line2);display:grid;place-content:center;
font-size:11px;font-variant-numeric:tabular-nums}
.steps .done .n{border-color:transparent;background:var(--raise);color:var(--muted)}
.steps .now{color:var(--text);font-weight:550}
.steps .now .n{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.layout{display:grid;grid-template-columns:minmax(0,1fr) 340px;gap:56px;align-items:start;padding-top:14px;
padding-bottom:120px}
main:focus{outline:none}
h1{font-size:30px;line-height:1.2;font-weight:650;letter-spacing:-.015em;margin:0 0 8px}
.sub{color:var(--muted);margin:0 0 22px}
.sub code,.cmd{color:var(--text);font-size:13px;background:var(--raise);padding:1px 6px;border-radius:5px}
.label{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:2px 12px;
margin:0 0 10px;font-size:13px;color:var(--muted);font-weight:500}
.label b{color:var(--text);font-weight:600;font-size:14px}
.hero{display:flex;flex-direction:column;gap:6px;margin:10px 0 26px}
.hero .logo{font-size:15px;line-height:15px}
.hero b{font-size:18px;font-weight:650;margin-top:14px}.hero small{color:var(--muted);font-size:14px}
.panel{background:var(--panel);border:1px solid var(--line);border-radius:14px;margin:0 0 24px;overflow:hidden}
.panel h2{display:flex;justify-content:space-between;align-items:center;margin:0;padding:12px 18px;font-size:14px;
font-weight:600;border-bottom:1px solid var(--line)}
.panel h2 span{color:var(--muted);font-weight:400;font-size:13px}
.panel h2 span.mint{color:var(--ok);font-size:15px;font-weight:650}
.panel ul{list-style:none;margin:0;padding:4px 0 6px}
.core li{display:grid;grid-template-columns:20px 150px 1fr;align-items:center;gap:0 12px;padding:6px 18px 6px 20px}
.core li svg{color:var(--ok)}.core li b{font-weight:550}.core li span{color:var(--muted);font-size:14px}
.rowsr{display:grid;grid-template-columns:90px 1fr;gap:6px 14px;padding:14px 18px;margin:0}
.rowsr dt{color:var(--muted)}.rowsr dd{margin:0;word-break:break-word}
.tick{color:var(--ok)}.amber{color:var(--warn)}.mint{color:var(--ok);font-weight:550}
.cmds{display:grid;grid-template-columns:auto 1fr;gap:6px 18px;padding:14px 18px;margin:0}
.cmds dt{color:var(--accent-text);font-weight:550;font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
font-size:13px}.cmds dd{margin:0;color:var(--muted)}
input[type=text]{width:100%;background:var(--bg);color:var(--text);border:1px solid var(--line2);border-radius:8px;
padding:11px 14px;font-size:14px}
input[type=text]:hover{border-color:var(--muted)}
input[type=text]:focus-visible{outline-offset:0;border-color:var(--accent)}
.hint{display:inline-flex;align-items:center;gap:8px;font-weight:400;font-size:13px;color:var(--muted)}
.dot{width:7px;height:7px;border-radius:50%;background:var(--ok);flex:none}
.chips{display:flex;flex-wrap:wrap;gap:8px;margin:12px 0 0}
.chip{border:1px solid var(--line2);border-radius:999px;padding:3px 12px;font-size:13px;color:var(--muted)}
.chip:hover{border-color:var(--accent);color:var(--text)}
.plugs{display:grid;grid-template-columns:1fr 1fr;gap:12px}
.plug{position:relative;display:grid;grid-template-columns:40px 1fr;gap:3px 14px;padding:20px 44px 20px 18px;
background:var(--panel);border:1px solid var(--line);border-radius:14px;cursor:pointer;
transition:border-color .12s,background .12s}
.plug:hover{border-color:var(--line2);background:var(--raise)}
.plug input{position:absolute;opacity:0;inset:0;margin:0;cursor:inherit}
.ico{grid-row:1/3;width:40px;height:40px;border-radius:10px;background:var(--raise);color:var(--muted);
display:grid;place-content:center}
.plug b{font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.plug small{color:var(--muted);font-size:13.5px;line-height:1.4}
.tag{font-weight:450;font-size:11.5px;color:var(--muted);border:1px solid var(--line2);border-radius:999px;
padding:0 8px;line-height:18px}
.chk{position:absolute;top:16px;right:16px;width:20px;height:20px;border-radius:50%;border:1.5px solid var(--line2);
display:grid;place-content:center;color:transparent}
.plug:has(input:checked){border-color:var(--accent);background:var(--tint)}
.plug:has(input:checked) .ico{background:var(--accent);color:var(--on-accent)}
.plug:has(input:checked) .chk{background:var(--accent);border-color:var(--accent);color:var(--on-accent)}
.plug:has(input:focus-visible){outline:2px solid var(--accent);outline-offset:2px}
.plug.na{opacity:.55;cursor:not-allowed}.plug.na:hover{background:var(--panel);border-color:var(--line)}
.rail{display:flex;flex-direction:column;gap:10px;position:sticky;top:20px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:12px}
.card summary{display:flex;align-items:center;gap:8px;padding:11px 14px;cursor:pointer;list-style:none;font-weight:550;
border-radius:12px}
.card summary:hover{background:var(--raise)}
.card summary::-webkit-details-marker{display:none}
.card summary::before{content:"";width:6px;height:6px;border:solid var(--muted);border-width:0 1.5px 1.5px 0;
transform:rotate(-45deg);transition:transform .12s;margin-right:2px}
.card[open] summary::before{transform:rotate(45deg) translate(-1px,-1px)}
.card summary span{margin-left:auto;color:var(--muted);font-weight:400;font-variant-numeric:tabular-nums}
.card summary span.add{color:var(--ok)}
.body{padding:0 14px 13px 28px}
.rows{margin:0;padding:0;list-style:none;color:var(--muted)}
.rows li{display:grid;grid-template-columns:62px 1fr;gap:10px;padding:2px 0}
.rows li b{font-weight:400;color:var(--text)}.rows li b.amber{color:var(--warn)}
.files{max-height:15em;overflow:auto;font-size:12.5px;margin:0;padding:0;list-style:none}
.files li{display:flex;gap:8px;padding:1px 0;color:var(--muted)}
.files li::before{content:"+";color:var(--ok)}.files li.chg::before{content:"~";color:var(--warn)}
.meter{display:grid;grid-template-columns:repeat(20,1fr);gap:2px;margin:6px 0 4px}
.meter i{height:5px;border-radius:2px;background:var(--line2)}.meter i.on{background:var(--accent)}
.bar{position:fixed;left:0;right:0;bottom:0;background:var(--bg);border-top:1px solid var(--line)}
.bar .wrap{display:flex;align-items:center;justify-content:space-between;padding-top:14px;padding-bottom:14px}
.bar #count{color:var(--ok);font-variant-numeric:tabular-nums;font-weight:500;min-width:0;overflow:hidden;
text-overflow:ellipsis;white-space:nowrap;margin-right:12px}
.acts{display:flex;gap:10px;align-items:center;flex:none}
.welcome .top .logo{visibility:hidden}
.go{background:var(--accent);color:var(--on-accent);font-weight:600;padding:10px 30px;border-radius:9px;
transition:filter .12s}
.go:hover{filter:brightness(1.1)}.go:active{filter:brightness(.95)}.go:disabled{opacity:.4;cursor:default}
.prog{list-style:none;margin:0;padding:6px 0}
.prog li{display:grid;grid-template-columns:22px 190px 1fr;align-items:center;padding:6px 18px;gap:0 8px}
.prog .st{color:var(--muted)}.prog li.ok .st{color:var(--ok)}.prog li.ok span{color:var(--ok)}
.prog li.wait{color:var(--muted)}.prog li span{color:var(--muted)}
.spin{width:14px;height:14px;border:2px solid var(--line2);border-top-color:var(--accent);border-radius:50%;
animation:turn .7s linear infinite}
@keyframes turn{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){*,*::before,*::after{animation:none!important;transition:none!important}}
.err{border-color:var(--error)}.err h2{color:var(--error)}
.err p{padding:14px 18px;margin:0}
.gap{margin-top:22px}
[hidden]{display:none!important}
@media(max-width:860px){
.wrap{padding:0 16px}.top{grid-template-columns:auto 1fr auto;gap:12px;padding-top:14px;padding-bottom:12px}
.steps{justify-self:center}.steps li:not(.now){display:none}.steps li+li::before{display:none}
.steps .n{display:none}.steps .mob{display:inline}
.layout{grid-template-columns:minmax(0,1fr);gap:8px;padding-bottom:96px}h1{font-size:26px}
.core li{grid-template-columns:20px 1fr;gap:0 12px}.core li span{grid-column:2}
.plugs{grid-template-columns:1fr}.rail{position:static;margin-top:20px}
.bar .wrap{padding-top:10px;padding-bottom:10px}.logo{font-size:6px;line-height:6px}.hero .logo{font-size:9px;line-height:9px}
.prog li{grid-template-columns:22px 1fr}.prog li span{grid-column:2}}
"""


def _mark_css() -> str:
    """One class per row of the mark, each naming the role it takes its colour from (a darker ramp in light mode)."""
    return "\n".join(f".logo .r{i}{{color:var(--{role})}}" for i, role in enumerate(brand.GRADIENT_ROLES))


CSS = brand.css() + "\n" + STYLE.strip("\n") + "\n" + _mark_css()

JS = """
const B = JSON.parse(document.getElementById('boot').textContent);
const T = new URLSearchParams(location.search).get('t');
history.replaceState(null, '', location.pathname);  // the token stays in memory, not in the address bar
const STEPS = ['Welcome', 'Core', 'Plugins', 'Notes', 'Review'];
const ICON = {mail: 'M3 5h18v14H3zM4 7.5l8 5.5 8-5.5', calendar: 'M3.5 5h17v15h-17zM3.5 10h17M8 3v4M16 3v4',
  finance: 'M4 19V5M4 19h16M8 15l3.5-4 3 2.5L20 7', graph: 'M4 7a2 2 0 1 0 4 0a2 2 0 1 0-4 0M16 6a2 2 0 1 0 4 0a2 2 0 1 0-4 0M10 18a2 2 0 1 0 4 0a2 2 0 1 0-4 0M8 8l8-1.5M7 9l4 7M17 8l-4 8'};
const h = (tag, props, ...kids) => {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(props || {})) {
    if (k === 'class') e.className = v;
    else if (k === 'text') e.textContent = v;
    else if (k.startsWith('on')) e.addEventListener(k.slice(2), v);
    else e.setAttribute(k, v);
  }
  for (const c of kids.flat()) if (c != null) e.append(c);
  return e;
};
const svg = (d, size) => { const s = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  s.setAttribute('viewBox', '0 0 24 24'); s.setAttribute('width', size); s.setAttribute('height', size);
  s.setAttribute('fill', 'none'); s.setAttribute('stroke', 'currentColor'); s.setAttribute('stroke-width', '1.7');
  s.setAttribute('stroke-linecap', 'round'); s.setAttribute('stroke-linejoin', 'round');
  const p = document.createElementNS('http://www.w3.org/2000/svg', 'path'); p.setAttribute('d', d); s.append(p); return s; };
const tickIcon = () => svg('m5 12.5 4.5 4.5L19 7.5', 18);
const rich = text => text.split('`').map((t, i) => i % 2 ? h('code', {class: 'cmd', text: t}) : t);  // `x` becomes a code chip
const main = document.getElementById('main'), rail = document.getElementById('rail');
const back = document.getElementById('back'), next = document.getElementById('next'), count = document.getElementById('count');
let step = 0, plan = null, busy = false, done = false;
const picked = new Set(B.plugins.filter(p => p.on).map(p => p.name));
let notes = B.notes.value, capText = '';

async function api(path, body) {
  const r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Ws-Token': T},
    body: JSON.stringify(body || {})});
  let j = {}; try { j = await r.json(); } catch (e) { j = {error: 'The server did not answer.'}; }
  return {ok: r.ok, j};
}
const choice = () => ({plugins: [...picked], notes, cap: capText});

function steps() {
  document.getElementById('steps').replaceChildren(...STEPS.map((n, i) => h('li',
    {class: done || i < step ? 'done' : i === step ? 'now' : '', ...(i === step ? {'aria-current': 'step'} : {})},
    h('span', {class: 'n', text: done || i < step ? '✓' : String(i + 1)}),
    i === step ? h('span', {class: 'mob', text: 'Step ' + (i + 1) + ' of ' + STEPS.length + ' ·'}) : null,
    h('span', {class: 't', text: n}))));
}

function railCards() {
  const status = h('details', {class: 'card', open: ''}, h('summary', {text: 'Status'}),
    h('div', {class: 'body'}, h('ul', {class: 'rows'}, B.status.map(r => h('li', {},
      r.label, h('b', {class: r.tone === 'warn' ? 'amber' : ''}, r.tone === 'ok' ? h('span', {class: 'tick', text: '✓ '}) : null, rich(r.value)))))));
  const cards = [status];
  if (B.spent && B.cap) {
    const on = Math.min(20, Math.round(B.spent / B.cap * 20));
    status.querySelector('.body').append(h('div', {class: 'meter'}, Array.from({length: 20}, (_, i) => h('i', {class: i < on ? 'on' : ''}))));
  }
  if (plan && step !== 4 && !done) {
    const list = plan.rows.filter(r => r.action !== 'keep' && r.kind === 'file');
    cards.push(h('details', {class: 'card', open: ''}, h('summary', {text: 'Changes'}),
      h('div', {class: 'body'}, h('ul', {class: 'files mono'}, list.slice(0, 8).map(r =>
        h('li', {class: r.action === 'change' ? 'chg' : ''}, r.path)), list.length > 8 ? h('li', {text: '… and ' + (list.length - 8) + ' more'}) : null))));
  }
  rail.replaceChildren(...cards);
}

function welcome() {
  const mark = document.getElementById('mark').content.firstElementChild.cloneNode(true);
  const hero = h('div', {class: 'hero'}, mark, h('b', {text: 'ws'}), h('small', {text: B.tagline}));
  const out = [hero];
  if (B.setUp) {
    out.push(h('section', {class: 'panel'}, h('h2', {}, 'Everything is set up', h('span', {text: '0 changes'})),
      h('dl', {class: 'cmds'}, B.change.flatMap(([c, w]) => [h('dt', {text: c}), h('dd', {text: w})]))));
    out.push(h('p', {class: 'sub', text: 'Nothing is written unless you change something.'}));
  } else {
    out.push(h('h1', {text: 'Set up ws'}));
    out.push(h('p', {class: 'sub', text: 'About a minute. Nothing is written until you press Install.'}));
    out.push(h('section', {class: 'panel'}, h('h2', {}, 'What happens', h('span', {text: '5 steps'})),
      h('dl', {class: 'cmds'}, [['Core', 'is on already: memory, notes, skills and your rules.'], ['Plugins', 'you choose which to add.'],
        ['Notes', 'you confirm the folder ws files notes in.'], ['Review', 'you see exactly what would change, then install.']].flatMap(([k, v]) => [h('dt', {text: k}), h('dd', {text: v})]))));
  }
  return out;
}
function core() {
  return [h('h1', {text: 'Core is on'}), h('p', {class: 'sub', text: 'Always on, and nothing here to switch.'}),
    h('section', {class: 'panel core'}, h('h2', {}, 'Core', h('span', {text: 'Always on'})),
      h('ul', {}, B.core.map(c => h('li', {}, tickIcon(), h('b', {text: c.name}), h('span', {text: c.line})))))];
}
function plugins() {
  return [h('h1', {text: 'Choose your plugins'}),
    h('p', {class: 'sub'}, 'Each plugin adds one job. Add them now, or later with ', h('code', {text: 'ws plugin add'}), '.'),
    h('div', {class: 'label'}, h('b', {text: 'Plugins'}), h('span', {text: 'Planned: a choice is recorded for now'})),
    h('div', {class: 'plugs'}, B.plugins.map(p => {
      const input = h('input', {type: 'checkbox', value: p.name, ...(p.available ? {} : {disabled: ''})});
      input.checked = p.available && picked.has(p.name);
      input.addEventListener('change', () => { input.checked ? picked.add(p.name) : picked.delete(p.name); loadPlan(); });
      return h('label', {class: 'plug' + (p.available ? '' : ' na')}, input,
        h('span', {class: 'ico'}, svg(ICON[p.icon], 22)),
        h('b', {}, p.label, p.tag ? h('span', {class: 'tag', text: p.tag}) : null, p.current ? h('span', {class: 'tag', text: 'on now'}) : null),
        h('small', {text: p.available ? p.line : 'Not available in your region' + (p.why ? ' (' + p.why + ')' : '')}),
        h('span', {class: 'chk'}, svg('m5 12.5 4.5 4.5L19 7.5', 12)));
    }))];
}
function capField() {  // optional, and only when the profile has no cap yet
  const cap = h('input', {type: 'text', id: 'cap', inputmode: 'decimal', placeholder: 'skip', spellcheck: 'false', 'aria-label': 'Monthly cap (US$)'});
  cap.value = capText;
  let t = null;
  cap.addEventListener('input', () => { capText = cap.value; clearTimeout(t); t = setTimeout(loadPlan, 300); });
  return h('div', {class: 'gap'}, h('div', {class: 'label'}, h('b', {text: 'Monthly cap (US$)'}), h('span', {text: 'Optional: the most ws may spend in a month'})), cap);
}
function notesStep() {
  const input = h('input', {type: 'text', id: 'notes', spellcheck: 'false', 'aria-label': 'Notes folder'});
  input.value = notes;
  const hint = h('span', {class: 'hint'});
  const show = () => { const o = B.notes.options.find(x => x.path === input.value);
    hint.replaceChildren(h('i', {class: 'dot'}), o ? o.app + ' vault found' : 'Checked at the review: a folder that is not there is made');
  };
  let timer = null;
  input.addEventListener('input', () => { notes = input.value; show(); clearTimeout(timer); timer = setTimeout(loadPlan, 300); });
  show();
  return [h('h1', {text: 'Notes folder'}),
    h('p', {class: 'sub', text: 'ws files new notes in an _inbox there. It never edits your notes.'}),
    h('div', {class: 'label'}, h('b', {text: 'Notes folder'}), hint), input,
    B.cap ? null : capField(),
    B.notes.options.length ? h('div', {class: 'chips'}, B.notes.options.map(o => h('button', {type: 'button', class: 'chip',
      text: o.path, onclick: () => { input.value = o.path; input.dispatchEvent(new Event('input')); }}))) : null];
}
function review(err) {
  const p = plan;
  if (err) return [h('h1', {text: 'Review'}), h('section', {class: 'panel err'}, h('h2', {}, 'This cannot go ahead'), h('p', {class: 'sub', text: err}))];
  if (!p) return [h('h1', {text: 'Review'}), h('p', {class: 'sub', text: 'Working out what would change…'})];
  const names = B.plugins.filter(x => picked.has(x.name)).map(x => x.label);
  const rows = [['Core', B.core.map(c => c.name).join(' · ')], ['Plugins', names.join(' · ') || 'none yet (ws plugin add later)'],
    ['Notes', p.notes], ...(p.cap ? [['Cap', 'US$' + p.cap + ' a month']] : []), ['Runner', B.runner || 'none yet (the gate files wait for one)'], ['Writes', p.headline]];
  const list = p.rows.filter(r => r.action !== 'keep' && r.kind === 'file');
  return [h('h1', {text: p.changes ? 'Ready to install' : 'Nothing to change'}),
    h('p', {class: 'sub', text: p.changes ? 'Check it, then press Install.' : 'Everything already matches.'}),
    h('section', {class: 'panel'}, h('h2', {}, 'Review', h('span', {text: 'nothing is written yet'})),
      h('dl', {class: 'rowsr'}, rows.flatMap(([k, v]) => [h('dt', {text: k}), h('dd', {class: k === 'Writes' ? 'mint' : '', text: v})]))),
    list.length ? h('details', {class: 'card'}, h('summary', {text: 'Show the files (' + list.length + ')'}),
      h('div', {class: 'body'}, h('ul', {class: 'files mono'}, list.map(r => h('li', {class: r.action === 'change' ? 'chg' : ''}, r.path + ' (' + (r.what || r.kind) + ')'))))) : null];
}
function installing(groups, upto) {
  return [h('h1', {text: upto >= groups.length ? 'Done' : 'Installing'}),
    h('section', {class: 'panel'}, h('h2', {}, 'Install'),
      h('ul', {class: 'prog'}, groups.map((g, i) => h('li', {class: i < upto ? 'ok' : i === upto ? '' : 'wait'},
        i < upto ? h('b', {class: 'st', text: '✓'}) : i === upto ? h('i', {class: 'spin'}) : h('b', {class: 'st', text: '·'}),
        h('b', {text: g.name}), h('span', {text: i < upto ? g.detail : i === upto ? 'writing…' : ''})))))];
}
function finished(res) {
  done = true; steps();
  const out = [h('section', {class: 'panel'}, h('h2', {}, h('span', {class: 'mint', text: '✓ Done'}), h('span', {text: res.headline})),
    h('dl', {class: 'cmds'}, res.next.flatMap(([c, w]) => [h('dt', {text: c}), h('dd', {text: w})])))];
  if (res.notes.length) out.push(h('p', {class: 'sub', text: res.notes.join(' ')}));
  out.push(h('p', {class: 'sub', text: 'Saved in ' + res.saved + '. Run ws setup again any time to change it.'}));
  out.push(h('p', {class: 'sub', text: 'You can close this tab.'}));
  return out;
}

function refreshBar() {
  back.hidden = step === 0 || done; next.hidden = done;
  next.disabled = busy;
  next.textContent = busy ? 'Working…' : step === 0 ? (B.setUp ? 'Change something' : 'Begin') : step === 4 ? (plan && !plan.changes ? 'Close' : 'Install') : 'Continue';
  if (step === 0 && B.setUp) { back.hidden = true; }
  count.textContent = plan ? plan.headline : '';
}
let loading = 0;
async function loadPlan() {  // keeps the bar and the Changes card live while you choose
  const mine = ++loading;
  const r = await api('/api/preview', choice());
  if (mine !== loading) return;
  plan = r.ok ? r.j : null;
  railCards(); refreshBar();
}
function draw(extra) {
  document.body.classList.toggle('welcome', step === 0 && !done);
  steps(); railCards();
  const parts = step === 0 ? welcome() : step === 1 ? core() : step === 2 ? plugins() : step === 3 ? notesStep() : review(extra);
  main.replaceChildren(...parts); refreshBar(); main.focus({preventScroll: true});
}
async function go(to) {
  step = to; draw();
  if (to === 2 || to === 3) loadPlan();
  if (to === 4) {
    busy = true; refreshBar();
    const r = await api('/api/preview', choice());
    busy = false;
    if (r.ok) { plan = r.j; draw(); } else { plan = null; draw(r.j.error || 'Something went wrong.'); }
  }
}
back.addEventListener('click', () => go(Math.max(0, step - 1)));
next.addEventListener('click', async () => {
  if (done || busy) return;
  if (step === 0 && B.setUp) return go(2);
  if (step < 4) return go(step + 1);
  if (plan && !plan.changes) { await api('/api/finish'); done = true; main.replaceChildren(h('h1', {text: 'Everything is set up'}), h('p', {class: 'sub', text: 'You can close this tab.'})); refreshBar(); steps(); return; }
  if (!plan) return;
  busy = true; refreshBar();
  main.replaceChildren(...installing(plan.groups, 0));
  const r = await api('/api/install', choice());
  busy = false;
  if (!r.ok) { plan = null; step = 4; draw(r.j.error || 'Something went wrong.'); return; }
  for (let i = 1; i <= r.j.groups.length; i++) { main.replaceChildren(...installing(r.j.groups, i)); await new Promise(f => setTimeout(f, 170)); }
  plan = {...plan, headline: r.j.headline}; main.replaceChildren(...finished(r.j)); railCards(); refreshBar();
});
const root = document.documentElement, tb = document.getElementById('theme');
const dark = () => (root.dataset.theme || (matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark')) === 'dark';
const label = () => { tb.textContent = dark() ? 'Light' : 'Dark'; };
tb.addEventListener('click', () => { root.dataset.theme = dark() ? 'light' : 'dark'; label(); });
label(); draw();
"""


def _mark() -> str:
    """The pixel mark as text: one class per row, coloured by the stylesheet."""
    rows = "\n".join(f'<i class="r{i}">{row}</i>' for i, row in enumerate(brand.LOGO))
    return f'<pre class="logo" role="img" aria-label="ws">{rows}</pre>'


def _boot(facts: flow.Facts, c: flow.Choices) -> str:
    """Everything the page needs, from the real machine, as JSON (escaped so it cannot close the script tag)."""
    env = facts.env
    on = set(c.plugins)
    icons = {"mail-drafts": "mail", "calendar": "calendar", "finance": "finance", "knowledge-graph": "graph"}
    runner = next((r for r in facts.runners if r.name == c.runner), None)
    options = [
        {"path": flow.tilde(v.path, env), "app": v.app}
        for v in facts.vaults
        if flow.tilde(v.path, env) != flow.tilde(c.notes, env)
    ]
    if flow.notes_app(facts, c.notes) in ("Obsidian", "Logseq"):
        options.insert(0, {"path": flow.tilde(c.notes, env), "app": flow.notes_app(facts, c.notes)})
    data = {
        "version": __version__,
        "tagline": brand.TAGLINE,
        "status": [{"label": r.label, "value": r.value, "tone": r.tone} for r in flow.status_rows(facts)],
        "spent": facts.spent,
        "cap": facts.cap,
        "core": [{"name": n, "line": line} for _, n, line in flow.CORE],
        "plugins": [
            {
                "name": p.name,
                "label": p.label,
                "line": p.line,
                "tag": p.tag,
                "built": p.built,
                "available": p.available,
                "why": p.why,
                "on": p.name in on,
                "current": p.name in facts.base.modules,
                "icon": icons.get(p.name, "graph"),
            }
            for p in facts.plugins
        ],
        "notes": {"value": flow.tilde(c.notes, env), "options": options},
        "runner": runner.label if runner else "",
        "setUp": facts.set_up,
        "change": [list(x) for x in flow.change_commands(facts, c)],
    }
    return json.dumps(data).replace("<", "\\u003c")


def csp(nonce: str | None = None) -> str:
    """The Content-Security-Policy for a response. The page gets a per-response nonce for its one script and one
    stylesheet (no `unsafe-inline`); everything else (JSON, text) is allowed nothing."""
    if nonce is None:
        return "default-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    return (
        f"default-src 'none'; style-src 'nonce-{nonce}'; script-src 'nonce-{nonce}'; connect-src 'self'; "
        "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    )


def render_page(facts: flow.Facts, c: flow.Choices, nonce: str = "") -> str:
    """The whole page as one string. The page builds its steps from the JSON with `textContent`, never with markup.
    `nonce` goes on the script and the stylesheet; it must be the one in this response's `csp`."""
    attr = f' nonce="{nonce}"' if nonce else ""
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer"><title>ws setup</title><style{attr}>{CSS}</style></head>
<body>
<header class="wrap top">{_mark()}
<ol class="steps" id="steps" aria-label="Setup steps"></ol>
<button type="button" class="ghost" id="theme">Light</button></header>
<div class="wrap layout"><main id="main" tabindex="-1"></main><aside class="rail" id="rail" aria-label="Status"></aside></div>
<div class="bar"><div class="wrap"><span id="count"></span>
<span class="acts"><button type="button" class="ghost" id="back" hidden>Back</button>
<button type="button" class="go" id="next">Begin</button></span></div></div>
<template id="mark">{_mark()}</template>
<script type="application/json" id="boot">{_boot(facts, c)}</script>
<script{attr}>{JS}</script>
</body></html>"""


class SetupServer:
    """A one-shot local server. `serve()` returns the install result, or None if it closed idle or on Close."""

    def __init__(
        self, facts: flow.Facts, choices: flow.Choices, *, token: str | None = None, idle_seconds: float = IDLE_SECONDS
    ):
        self.facts, self.choices = facts, choices
        self.token = token or secrets.token_urlsafe(16)
        self.idle_seconds = idle_seconds
        self.result: dict | None = None
        self._last = time.monotonic()
        self._done = threading.Event()
        self._lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "ws-setup"

            def log_message(self, *args):  # quiet: the terminal belongs to the person
                pass

            # -- checks
            def _host_ok(self) -> bool:
                host = (self.headers.get("Host") or "").lower()
                return host in (f"127.0.0.1:{owner.port}", f"localhost:{owner.port}")

            def _origin_ok(self) -> bool:
                origin = (self.headers.get("Origin") or "").lower()
                return origin in (f"http://127.0.0.1:{owner.port}", f"http://localhost:{owner.port}")

            def _send(self, code: int, body: bytes, kind: str, nonce: str | None = None) -> None:
                self.send_response(code)
                self.send_header("Content-Type", f"{kind}; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Referrer-Policy", "no-referrer")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Content-Security-Policy", csp(nonce))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, code: int, doc: dict) -> None:
                self._send(code, json.dumps(doc).encode(), "application/json")

            def _same(self, a: str, b: str) -> bool:
                """Constant-time compare on bytes: a header with a non-ASCII character is a no, never a traceback."""
                try:
                    return secrets.compare_digest(a.encode("utf-8"), b.encode("utf-8"))
                except (UnicodeError, TypeError):
                    return False

            def _fail_500(self) -> None:
                try:
                    self._json(
                        500,
                        {"error": "Something went wrong inside ws setup. Run `ws doctor` to see where things stand."},
                    )
                except OSError:  # the connection is already gone: nothing more to do, and no traceback on the terminal
                    return

            # -- routes
            def do_GET(self):
                try:
                    self._get()
                except Exception:  # noqa: BLE001 - a handler never raises: the person gets a plain 500, not a traceback
                    self._fail_500()

            def do_POST(self):
                try:
                    self._post()
                except Exception:  # noqa: BLE001 - a handler never raises: the person gets a plain 500, not a traceback
                    self._fail_500()

            def _get(self):
                q = parse_qs(urlsplit(self.path).query)
                if not self._host_ok() or not self._same((q.get("t") or [""])[0], owner.token):
                    return self._send(
                        403, b"This page needs the link that ws setup printed. Open that link again.", "text/plain"
                    )
                owner._last = time.monotonic()  # only a request that proves it has the token keeps the page alive
                if urlsplit(self.path).path != "/":
                    return self._send(404, b"not found", "text/plain")
                nonce = secrets.token_urlsafe(16)  # new for every response
                page = render_page(owner.facts, owner.choices, nonce).encode()
                owner.forget_launcher()  # the token has been used once: the file that carried it goes
                self._send(200, page, "text/html", nonce)

            def _post(self):
                header = self.headers.get("X-Ws-Token") or ""
                if not (self._host_ok() and self._origin_ok() and self._same(header, owner.token)):
                    return self._send(403, b"forbidden", "text/plain")
                owner._last = time.monotonic()
                if not (self.headers.get("Content-Type") or "").startswith("application/json"):
                    return self._send(415, b"send JSON", "text/plain")
                path = urlsplit(self.path).path
                if path not in ("/api/preview", "/api/install", "/api/finish"):
                    return self._send(404, b"not found", "text/plain")
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if not 0 <= n <= MAX_BODY:
                        raise ValueError
                    data = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(data, dict):
                        raise TypeError
                    picks, notes, cap = data.get("plugins", []), data.get("notes"), data.get("cap")
                    if not isinstance(picks, list) or not all(isinstance(m, str) for m in picks) or len(picks) > 50:
                        raise ValueError
                    if notes is not None and not isinstance(notes, str):
                        raise ValueError
                    if cap is not None and (isinstance(cap, bool) or not isinstance(cap, (str, int, float))):
                        raise ValueError
                except (ValueError, TypeError):
                    return self._send(400, b"bad request", "text/plain")
                if path == "/api/finish":
                    owner.result = {"headline": "0 changes", "groups": [], "notes": [], "next": []}
                    self._json(200, {"ok": True})
                    owner._done.set()
                    return
                self._json(*owner.handle(path, picks, notes, cap))

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/?t={self.token}"

    launcher: Path | None = None  # the local file that redirects the browser here; gone after the first load

    def forget_launcher(self) -> None:
        if self.launcher is not None:
            shutil.rmtree(self.launcher.parent, ignore_errors=True)
            self.launcher = None

    def _choices(self, picks: list[str], notes: str | None, cap) -> flow.Choices:
        """What the page sent, on top of the starting choices. Nothing is trimmed but spaces; a control character or a
        cap that is not a number is refused by `flow.check_choices` / `flow.parse_cap` with a plain message."""
        c = replace(self.choices, plugins=frozenset(picks))
        if notes is not None and notes.strip(" "):
            c = replace(c, notes=notes.strip(" "))
        if isinstance(cap, str):
            c = replace(c, cap=flow.parse_cap(cap) if cap.strip() else self.choices.cap)
        elif cap is not None:
            c = replace(c, cap=float(cap))
        return c

    def handle(self, path: str, picks: list[str], notes: str | None, cap=None) -> tuple[int, dict]:
        """Run a preview or the install for what the page sent. Input ws cannot use comes back as a 400 with a plain
        message, before anything is written; anything unexpected is a 500 that says so. It never raises."""
        facts = self.facts
        started = False
        with self._lock:
            if self._done.is_set():
                return 409, {"error": "Setup has already finished."}
            try:
                c = self._choices(picks, notes, cap)
                pv = flow.preview(facts, c)  # checks everything, and does a dry run, before the first write
                headline = pv.headline()
                if path == "/api/preview":
                    return 200, {
                        "headline": headline,
                        "changes": pv.changes,
                        "notes": flow.tilde(install.expand(c.notes, facts.env), facts.env),
                        "cap": c.cap,
                        "rows": pv.rows[:MAX_ROWS],
                        "groups": [{"name": n, "detail": ""} for n, _ in flow.groups(pv.steps)],
                    }
                prog = flow.Progress(pv.steps)
                started = True
                outcome = flow.run_install(facts, c, pv.steps, prog.step)
            except flow.ENGINE_ERRORS as e:
                tail = " What was written is recorded; `ws uninstall` removes it." if started else ""
                return 400, {"error": flow.clean(str(e)) + tail}
            except Exception:  # noqa: BLE001 - whatever it was, answer with JSON and say what state things are in
                tail = (
                    " What was written is recorded; `ws uninstall` removes it." if started else " Nothing was written."
                )
                return 500, {"error": "Something went wrong inside ws setup." + tail}
            res = outcome.result
            doc = {
                "headline": pv.headline() if res.changes else "0 changes",
                "groups": [{"name": n, "detail": prog.detail(n)} for n in prog.names],
                "notes": outcome.notes
                + [f"you changed it, so ws left it: {flow.tilde(p, facts.env)}" for p in res.edited],
                "next": [list(x) for x in flow.next_steps(facts, c)],
                "saved": flow.saved_in(facts.env),
            }
            self.result = doc
            self._done.set()
            return 200, doc

    def serve(self) -> dict | None:
        self.httpd.timeout = 0.2
        try:
            while not self._done.is_set():
                self.httpd.handle_request()
                if time.monotonic() - self._last > self.idle_seconds:
                    break
            if self._done.is_set():
                time.sleep(0.4)  # let the reply reach the page before the socket goes
        finally:
            self.httpd.server_close()
        return self.result

    def close(self) -> None:
        self._done.set()


def _launcher(url: str) -> Path:
    """A local page that sends the browser to `url`. The browser is started on this file, so the one-time token is
    never on a command line (where `ps` would show it); the file is private to you and goes after the first load."""
    folder = Path(tempfile.mkdtemp(prefix="ws-setup-"))  # created 0700
    path = folder / "open.html"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    safe = html.escape(url, quote=True)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(
            f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" content="0;url={safe}">'
            f'<title>ws setup</title><p>Opening ws setup. If nothing happens, <a href="{safe}">open it</a>.</p>'
        )
    return path


def run(facts: flow.Facts, choices: flow.Choices, open_browser: bool = True) -> int:
    server = SetupServer(facts, choices)
    print(f"ws setup: {server.url}")
    print("Open it in a browser on this machine. It closes when you finish, or after 10 idle minutes.")
    print("If you reload the page, open this link again: the address is good for this run only.")
    try:
        if open_browser:
            server.launcher = _launcher(server.url)
            webbrowser.open(server.launcher.as_uri())
        result = server.serve()
    except KeyboardInterrupt:
        print("ws setup: cancelled; nothing was written")
        return 130
    finally:
        server.forget_launcher()
    if result is None:
        print("ws setup: closed without installing; nothing was written")
        return 1
    print(f"ws setup: done: {result['headline']}")
    return 0
