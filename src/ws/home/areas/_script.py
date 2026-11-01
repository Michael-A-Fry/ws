"""The home page's script, in two parts. `SHARED` is on every page, a rendered file too: the look (theme, accent, list
font, text size), the view (group, density, what shows, saved views, a workspace or machine filter), the menus, the phone
drawer, the pickers' rules and the copy buttons. `LIVE` is added only when a server stands behind the page: the open
buttons, answering an ask (sent after a 6 second undo window), starting a session from the box, and the page's own
refresh. Choices are kept in localStorage for now; keeping them in the profile comes later.
"""

from __future__ import annotations

__all__ = ["LIVE", "SHARED"]

SHARED = r"""
const $ = (s, r = document) => r.querySelector(s), $$ = (s, r = document) => Array.from(r.querySelectorAll(s));
const root = document.documentElement;
const store = {
  get(k, d) { try { const v = localStorage.getItem(k); return v ? JSON.parse(v) : d; } catch (_) { return d; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (_) {} },
};

/* the look: theme, accent, list font, text size */
const look = Object.assign({ theme: 'system', accent: 'violet', font: 'sans', size: 'm' }, store.get('ws-home-look', {}));
function applyLook() {
  if (look.theme === 'system') delete root.dataset.theme; else root.dataset.theme = look.theme;
  if (look.accent === 'violet') delete root.dataset.accent; else root.dataset.accent = look.accent;
  if (look.font === 'mono') root.dataset.font = 'mono'; else delete root.dataset.font;
  if (look.size === 'm') delete root.dataset.size; else root.dataset.size = look.size;
  $$('[data-store=look]').forEach((g) => $$('[data-v]', g).forEach((b) => b.classList.toggle('on', b.dataset.v === look[g.dataset.k])));
}

/* the view: how the list is grouped, how dense, what shows */
const list = $('#list'), col = $('#main');
const DEFAULT_VIEW = { group: 'need', density: 'comfy', show: { pinned: true, done: true, quiet: true, model: true }, ws: '', machine: '' };
let view = Object.assign({}, DEFAULT_VIEW, store.get('ws-home-view', {}));
view.show = Object.assign({}, DEFAULT_VIEW.show, view.show || {});
let views = store.get('ws-home-views', []);
let active = store.get('ws-home-active', '');
const GROUP_WORD = { need: 'need', workspace: 'workspace', model: 'model', machine: 'machine' };

let rows = [], needSections = [];
if (list) {
  rows = $$('.r', list);
  rows.forEach((r, i) => { r._home = r.parentNode; r._i = i; });
  needSections = $$(':scope > .grp', list);
}

function regroup(g) {
  if (!list) return;
  if (g === 'need') {
    list.replaceChildren(...needSections);
    rows.forEach((r) => r._home.appendChild(r));
    return;
  }
  const key = { workspace: 'ws', model: 'model', machine: 'machine' }[g];
  const none = { workspace: 'No workspace', model: 'No runner', machine: 'Unknown machine' }[g];
  const groups = new Map();
  rows.forEach((r) => {
    const k = r.dataset[key] || none;
    if (!groups.has(k)) groups.set(k, []);
    groups.get(k).push(r);
  });
  const secs = [];
  groups.forEach((members, name) => {
    const sec = document.createElement('section'); sec.className = 'grp';
    const h = document.createElement('h5'); h.textContent = name + ' ';
    const cls = g === 'workspace' ? members[0].dataset.cls : '';
    if (cls) { const c = document.createElement('em'); c.textContent = cls + ' '; h.appendChild(c); }
    const em = document.createElement('em'); em.className = 'num'; h.appendChild(em);
    sec.appendChild(h); members.forEach((m) => sec.appendChild(m)); secs.push(sec);
  });
  list.replaceChildren(...secs);
}

let shownGroup = 'need';
function applyView() {
  if (list) {
    if (view.group !== shownGroup) { regroup(view.group); shownGroup = view.group; }
    list.dataset.group = view.group; list.dataset.density = view.density;
    list.dataset.showPinned = view.show.pinned ? '1' : '0';
    list.dataset.showDone = view.show.done ? '1' : '0';
    list.dataset.showModel = view.show.model ? '1' : '0';
    rows.forEach((r) => { r.hidden = !!((view.ws && r.dataset.ws !== view.ws) || (view.machine && r.dataset.machine !== view.machine)); });
    const filtering = !!(view.ws || view.machine);
    $$('.grp', list).forEach((g) => {
      const n = $$('.r:not([hidden])', g).length, em = $('h5 em.num, summary em.num', g);
      if (em) { if (!em.dataset.t) em.dataset.t = em.textContent; em.textContent = filtering || !em.dataset.t ? n : em.dataset.t; }
      g.hidden = filtering && n === 0;
    });
  }
  if (col) col.dataset.showQuiet = view.show.quiet ? '1' : '0';
  const lbl = $('#viewlbl'); if (lbl) lbl.textContent = 'View: by ' + GROUP_WORD[view.group];
  const mach = $('#machine'); if (mach) mach.value = view.machine;
  $$('[data-store=view]').forEach((g) => $$('[data-v]', g).forEach((b) => b.classList.toggle('on', b.dataset.v === view[g.dataset.k])));
  $$('#viewpop [data-s]').forEach((c) => c.setAttribute('aria-checked', String(!!view.show[c.dataset.s])));
  $$('#wslist [data-ws]').forEach((b) => b.classList.toggle('on', b.dataset.ws === view.ws));
  $$('#viewlist [data-view]').forEach((b) => b.classList.toggle('on', b.dataset.view === active));
}
function saveView() { store.set('ws-home-view', view); store.set('ws-home-active', active); }
function drawViews() {
  const box = $('#viewlist'); if (!box) return;
  $$('[data-view]:not([data-view=""])', box).forEach((b) => b.remove());
  views.forEach((v) => {
    const b = document.createElement('button'); b.type = 'button'; b.className = 'nav'; b.dataset.view = v.name;
    b.textContent = v.name; box.appendChild(b);
  });
}

/* menus: one open at a time, closed by Escape or a click away */
function closePops(except) {
  $$('.pop').forEach((p) => { if (p !== except) p.hidden = true; });
  $$('[data-pop]').forEach((b) => b.setAttribute('aria-expanded', String(!!except && b.dataset.pop === except.id && !except.hidden)));
}
document.addEventListener('keydown', (e) => { if (e.key === 'Escape') { closePops(null); document.body.classList.remove('drawer'); } });

document.addEventListener('click', (e) => {
  const t = e.target;
  const pop = t.closest('[data-pop]');
  if (pop) {
    const p = document.getElementById(pop.dataset.pop); p.hidden = !p.hidden; closePops(p.hidden ? null : p); return;
  }
  if (!t.closest('.pop')) closePops(null);
  const seg = t.closest('[data-store] [data-v]');
  if (seg) {
    const g = seg.parentNode.closest('[data-store]');
    if (g.dataset.store === 'look') { look[g.dataset.k] = seg.dataset.v; store.set('ws-home-look', look); applyLook(); }
    else { view[g.dataset.k] = seg.dataset.v; active = ''; saveView(); applyView(); }
    return;
  }
  const chk = t.closest('#viewpop [data-s]');
  if (chk) { view.show[chk.dataset.s] = !view.show[chk.dataset.s]; active = ''; saveView(); applyView(); return; }
  const act = t.closest('[data-act]');
  if (act) {
    const a = act.dataset.act;
    if (a === 'drawer') document.body.classList.toggle('drawer');
    else if (a === 'new') { document.body.classList.remove('drawer'); const ta = $('#c-text'); if (ta) setTimeout(() => ta.focus(), 0); }
    else if (a === 'save-view') {
      const n = ($('#viewname').value || '').trim(); if (!n) return;
      views = views.filter((v) => v.name !== n).concat([{ name: n, group: view.group, density: view.density, show: view.show, ws: view.ws, machine: view.machine }]);
      store.set('ws-home-views', views); active = n; saveView(); drawViews(); applyView(); $('#viewname').value = ''; closePops(null);
    }
    else if (a === 'not-now') { const r = act.closest('.r'); if (r) r.open = false; }
    return;
  }
  const w = t.closest('#wslist [data-ws]');
  if (w) { view.ws = view.ws === w.dataset.ws ? '' : w.dataset.ws; saveView(); applyView(); document.body.classList.remove('drawer'); location.hash = '#v-home'; return; }
  const sv = t.closest('#viewlist [data-view]');
  if (sv) {
    const found = views.find((v) => v.name === sv.dataset.view);
    view = Object.assign({}, DEFAULT_VIEW, found || {}); view.show = Object.assign({}, DEFAULT_VIEW.show, view.show || {});
    active = found ? found.name : ''; saveView(); applyView(); document.body.classList.remove('drawer'); location.hash = '#v-home'; return;
  }
  const nav = t.closest('.side a.nav'); if (nav) document.body.classList.remove('drawer');
  const cp = t.closest('button[data-copy]');
  if (cp) {
    const was = cp.textContent, text = cp.dataset.copy;
    const done = () => { cp.textContent = 'Copied'; setTimeout(() => { cp.textContent = was; }, 1500); };
    if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, done);
    else { const a = document.createElement('textarea'); a.value = text; document.body.appendChild(a); a.select(); try { document.execCommand('copy'); } catch (_) {} a.remove(); done(); }
  }
});
const mach = $('#machine'); if (mach) mach.addEventListener('change', () => { view.machine = mach.value; saveView(); applyView(); });

/* the place you are in: the nav link that matches the address */
function here() {
  const h = location.hash || '#v-home';
  $$('.side a.nav[href^="#v-"]').forEach((a) => { if (a.dataset.act !== 'new') { if (a.getAttribute('href') === h) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); } });
}
window.addEventListener('hashchange', here);

/* the box: a workspace's class decides which models stay greyed out; the last choice is kept per workspace */
const cws = $('#c-ws'), crun = $('#c-runner'), cwhat = $('#c-what'), cwhere = $('#c-where');
const picks = store.get('ws-home-picks', {});
function modelRules() {
  if (!cws || !crun) return;
  const opt = cws.selectedOptions[0], cls = opt ? (opt.dataset.cls || '') : '';
  $$('option[data-deny]', crun).forEach((o) => {
    const bad = !!cls && (o.dataset.deny || '').split(',').includes(cls);
    o.disabled = bad; o.textContent = o.dataset.label + (bad ? ' · not for ' + cls + ' data (your policy)' : '');
  });
  if (crun.selectedOptions[0] && crun.selectedOptions[0].disabled) {
    const ok = $$('option:not([disabled])', crun)[0]; if (ok) crun.value = ok.value;
  }
}
function loadPicks() {
  const p = picks[cws && cws.value]; if (!p) return;
  if (p.what) cwhat.value = p.what;
  if (p.where && $$('option', cwhere).some((o) => o.value === p.where)) cwhere.value = p.where;
  if (p.runner && $$('option', crun).some((o) => o.value === p.runner && !o.disabled)) crun.value = p.runner;
}
function savePicks() { if (cws && cws.value) { picks[cws.value] = { what: cwhat.value, where: cwhere.value, runner: crun.value }; store.set('ws-home-picks', picks); } }
if (cws) {
  cws.addEventListener('change', () => { modelRules(); loadPicks(); modelRules(); });
  [cwhat, cwhere, crun].forEach((s) => s.addEventListener('change', savePicks));
  modelRules(); loadPicks(); modelRules();
}

/* the greeting follows this computer's clock */
const hello = $('#hello');
const ta0 = $('#c-text');
if (ta0 && matchMedia('(max-width:700px)').matches) ta0.placeholder = 'Describe a task\u2026';
if (hello) { const h = new Date().getHours(); hello.textContent = (h < 12 ? 'Morning' : h < 18 ? 'Afternoon' : 'Evening') + '. What’s next?'; }

applyLook(); drawViews(); applyView(); here();
"""

LIVE = r"""
const token = new URLSearchParams(location.search).get('t') || '';
async function post(path, body) {
  try {
    const r = await fetch(path, { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Ws-Token': token }, body: JSON.stringify(body) });
    let j = {}; try { j = await r.json(); } catch (_) {}
    return { ok: r.ok, error: j.error || '' };
  } catch (_) { return { ok: false, error: 'ws home did not answer' }; }
}
let busy = false, lastTouch = 0;
const pending = new Map();
const UNDO_MS = 6000;
const clock = () => { const d = new Date(); return String(d.getHours()).padStart(2, '0') + ':' + String(d.getMinutes()).padStart(2, '0'); };

/* open a session or an ask where it lives */
document.addEventListener('click', async (e) => {
  const b = e.target.closest('button[data-opener]');
  if (!b) return;
  const was = b.textContent; b.disabled = true;
  const r = await post('/api/open', { opener: b.dataset.opener, kind: b.dataset.kind, id: b.dataset.id });
  b.textContent = r.ok ? 'Done' : (r.error || 'It did not work');
  setTimeout(() => { b.textContent = was; b.disabled = false; }, 2500);
});

/* answer on the row: choose, see a stamp, and the answer is sent after the undo window */
function stage(row, label, payload) {
  const ask = $('.ask[data-ask]', row); if (!ask || pending.has(row)) return;
  const stamp = $('.stamp', ask), msg = $('.msg', stamp), undo = $('[data-act=undo]', stamp);
  const choices = $$('.opts2, .else', ask);
  choices.forEach((c) => { c.hidden = true; });
  stamp.hidden = false; undo.hidden = false; msg.textContent = 'Answered · ' + label + ' · ' + clock();
  const timer = setTimeout(async () => {
    pending.delete(row); undo.hidden = true; busy = true;
    const r = await post('/api/answer', Object.assign({ id: ask.dataset.ask }, payload));
    busy = false;
    if (r.ok) { msg.textContent = 'Sent · ' + label + ' · ' + clock(); setTimeout(() => location.reload(), 1500); }
    else { msg.textContent = r.error || 'It did not work'; stamp.hidden = false; choices.forEach((c) => { c.hidden = false; }); }
  }, UNDO_MS);
  pending.set(row, { timer, choices, stamp });
}
document.addEventListener('click', (e) => {
  const t = e.target;
  if (t.closest('.r')) lastTouch = Date.now();
  const undo = t.closest('[data-act=undo]');
  if (undo) {
    const row = undo.closest('.r'), p = pending.get(row); if (!p) return;
    clearTimeout(p.timer); pending.delete(row); p.stamp.hidden = true; p.choices.forEach((c) => { c.hidden = false; }); return;
  }
  const opt = t.closest('.opt[data-choice]');
  if (opt) { stage(opt.closest('.r'), opt.dataset.choice, { choice: opt.dataset.choice }); return; }
  const send = t.closest('[data-act=send-text]');
  if (send) { const i = $('input', send.parentNode), v = i.value.trim(); if (v) stage(send.closest('.r'), v, { text: v }); }
});
document.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && e.target.matches('.else input')) { const v = e.target.value.trim(); if (v) stage(e.target.closest('.r'), v, { text: v }); }
});

/* start a session from the box */
const send = $('#c-send'), text = $('#c-text'), note = $('#c-note');
async function start() {
  if (!send || send.disabled || !text.value.trim()) { if (note && text && !text.value.trim()) { note.textContent = 'Write what you want done first.'; note.className = 'cnote bad'; } return; }
  const [runner, model] = $('#c-runner').value.split('|');
  send.disabled = true; busy = true; note.className = 'cnote'; note.textContent = 'Starting…';
  const r = await post('/api/start', { what: $('#c-what').value, workspace: $('#c-ws').value, where: $('#c-where').value, runner: runner || '', model: model || '', request: text.value });
  busy = false; send.disabled = false;
  if (r.ok) { text.value = ''; note.className = 'cnote good'; note.textContent = 'Started. It shows up in the list in a moment.'; setTimeout(() => location.reload(), 4000); }
  else { note.className = 'cnote bad'; note.textContent = r.error || 'It did not work'; }
}
if (send) { send.addEventListener('click', start); text.addEventListener('keydown', (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) start(); }); }

/* refresh while the page is in view, unless you are in the middle of something */
setInterval(() => {
  if (document.hidden || busy || pending.size || Date.now() - lastTouch < 120000) return;
  const a = document.activeElement;
  if (a && /^(INPUT|TEXTAREA|SELECT)$/.test(a.tagName)) return;
  if ((text && text.value) || $$('.else input').some((i) => i.value) || $$('.pop').some((p) => !p.hidden)) return;
  location.reload();
}, 45000);
"""
