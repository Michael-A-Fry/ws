"""`ws mail connect --web`: the "Connect mail" card as one local page, over ws.mailconnect, with no shell step after it opens.

The page lists the mailboxes (`mailconnect.known`) and has one form: mailbox name, address, provider and app password,
with a line on where to make an app password. Connect posts to `mailconnect.connect` and shows its answer in place:
connected with the folder count and where drafts go, or why not in ws's own words. The password field is emptied as the
post is sent and never filled back in.

It is a table of routes on the shared loopback server (ws.ui.server): bound to 127.0.0.1 on a random port, a random
token in the address that the page keeps in memory and sends back in `X-Ws-Token`, a matching Origin on every POST, a
per-response nonce for its one script and stylesheet, `Cache-Control: no-store` on every reply, and no request log. It
closes after ten idle minutes or on Close. The password is in no reply, log line or error text: the route hands it to
`connect` and drops it.
"""

from __future__ import annotations

import json

from ws import mailconnect
from ws.ui import components, css
from ws.ui.server import BadRequest, LoopbackServer

FIELDS = ("name", "address", "provider", "password")
MAX_FIELD = 300

_CSS = """
.wrap{max-width:640px;padding-top:28px;padding-bottom:40px}
h1{font-size:22px;margin:0 0 4px}
.lede{color:var(--muted);margin:0 0 22px}
.mb{display:flex;flex-wrap:wrap;justify-content:space-between;gap:2px 12px;padding:8px 18px;align-items:baseline}
.mb .who{min-width:0;overflow-wrap:anywhere}.mb .who small{display:block;color:var(--muted)}
form{padding:14px 18px 18px;display:flex;flex-direction:column;gap:14px}
label{display:flex;flex-direction:column;gap:6px;font-size:13px;color:var(--muted)}
input,select{font:inherit;color:var(--text);background:var(--bg);border:1px solid var(--line2);border-radius:8px;
padding:10px 12px;width:100%}
.help{margin:0;font-size:13px;color:var(--muted)}.help a{color:var(--accent)}
.go{align-self:flex-start;background:var(--accent);color:var(--bg);font-weight:600;border-radius:999px;padding:9px 20px}
.go[disabled]{opacity:.6}
.out{margin:0;min-height:1.5em}.out.st{display:block;white-space:normal}.out .hint{display:block;color:var(--muted);font-size:13px}
.close{color:var(--muted);text-decoration:underline;margin-top:18px}
"""

_SCRIPT = """
const q = new URLSearchParams(location.search), t = q.get("t") || "";
history.replaceState(null, "", location.pathname);
const help = JSON.parse(document.getElementById("help").textContent);
const f = document.getElementById("f"), out = document.getElementById("out"), go = document.getElementById("go");
const sel = f.elements.provider, tip = document.getElementById("tip"), link = document.getElementById("link");
function showHelp() { const h = help[sel.value] || help.gmail; tip.textContent = h[1] + " "; link.href = h[0]; }
sel.addEventListener("change", showHelp); showHelp();
function say(cls, text, hint) {
  out.className = "out st " + cls; out.textContent = text;
  if (hint) { const s = document.createElement("span"); s.className = "hint"; s.textContent = hint; out.appendChild(s); }
}
async function post(path, body) {
  const r = await fetch(path, {method: "POST", headers: {"Content-Type": "application/json", "X-Ws-Token": t},
                               body: JSON.stringify(body)});
  return r.ok ? r.json() : {ok: false, message: "ws refused the request (" + r.status + ").", hint: "Open the link ws printed again."};
}
f.addEventListener("submit", async (e) => {
  e.preventDefault();
  const body = {}; for (const k of ["name", "address", "provider", "password"]) body[k] = f.elements[k].value;
  f.elements.password.value = "";
  go.disabled = true; say("st-info", "Logging in once, read-only...");
  try {
    const r = await post("/connect", body);
    if (r.ok) say("st-ok", "Connected: " + r.folders + " folders; " +
      (r.drafts ? "drafts go to " + r.drafts + "." : "no drafts folder found, so drafts are refused until there is one."));
    else say("st-warn", r.message, r.hint);
  } catch (err) { say("st-error", "ws stopped answering.", "Run the command again."); }
  go.disabled = false;
});
document.getElementById("close").addEventListener("click", async (e) => {
  e.preventDefault(); await post("/close", {}); say("st-muted", "Closed. You can shut this tab.");
});
"""


def _e(text: object) -> str:
    import html

    return html.escape(str(text), quote=True)


def page_html(nonce: str, known: list[dict]) -> str:
    """The page. `known` is mailconnect.known(): names and addresses only, never a value."""
    rows = []
    for b in known:
        ok = b.get("checked_at") and b.get("in_profile")
        state = components.status("ok", "working") if ok else components.status("waiting", "needs its app password")
        rows.append(
            f'<div class="mb"><span class="who">{_e(b["name"])}<small>{_e(b["address"])}</small></span>{state}</div>'
        )
    listing = components.panel("Mailboxes", "".join(rows)) if rows else ""
    names = "".join(f'<option value="{_e(b["name"])}">' for b in known)
    opts = "".join(f'<option value="{_e(p.key)}">{_e(p.label)}</option>' for p in mailconnect.PROVIDERS.values())
    # raw JSON in a data block (its text is never entity-decoded); "<" escaped so it can't close the tag
    help_json = json.dumps({p.key: [p.app_passwords, p.help] for p in mailconnect.PROVIDERS.values()}).replace("<", "\\u003c")
    form = (
        '<form id="f" autocomplete="off">'
        '<label>Mailbox name<input name="name" list="names" required maxlength="40" pattern="[a-z0-9][a-z0-9_\\-]*" '
        'autocapitalize="none" spellcheck="false" placeholder="personal"></label>'
        f'<datalist id="names">{names}</datalist>'
        '<label>Address<input name="address" type="email" required autocapitalize="none" spellcheck="false"></label>'
        f'<label>Provider<select name="provider">{opts}</select></label>'
        '<label>App password<input name="password" type="password" required autocomplete="off" maxlength="200" '
        'autocapitalize="none" spellcheck="false"></label>'
        '<p class="help"><span id="tip"></span><a id="link" rel="noreferrer noopener" target="_blank">'
        "Make an app password</a></p>"
        '<button class="go" id="go" type="submit">Connect</button>'
        '<p class="out" id="out" role="status" aria-live="polite"></p></form>'
    )
    body = (
        '<main class="wrap"><h1>Connect mail</h1>'
        '<p class="lede">ws logs in once, read-only, to check the password before it keeps it. Drafts only: nothing '
        "is ever sent.</p>"
        f"{listing}{components.panel('Connect a mailbox', form)}"
        '<a href="#" class="close" id="close">Close</a>'
        f'<script type="application/json" id="help">{help_json}</script></main>'
    )
    return components.page("Connect mail - ws", body, nonce, css.PANEL + _CSS, script=_SCRIPT)


def _fields(data: dict) -> dict:
    out = {}
    for k in FIELDS:
        v = data.get(k, "")
        if not isinstance(v, str) or len(v) > MAX_FIELD:
            raise BadRequest(k)
        out[k] = v
    return out


def build_server(env: dict | None = None, **kw) -> LoopbackServer:
    """The server and its routes. `kw` goes to LoopbackServer (tests pass a token or a short idle time); a `cafile`
    in it goes to connect instead (a test CA)."""
    cafile = kw.pop("cafile", None)
    server: LoopbackServer

    def get_page(nonce: str) -> str:
        return page_html(nonce, mailconnect.known(env))

    def post_connect(data: dict):
        f = _fields(data)
        password = f.pop("password")
        try:
            r = mailconnect.connect(f["name"], f["address"], password, f["provider"] or None, env, cafile=cafile)
        except mailconnect.ConnectError as e:
            return 200, {"ok": False, "kind": e.kind, "message": e.message, "hint": e.hint}
        finally:
            del password
        return 200, {"ok": True, **r}

    def post_close(data: dict):
        return 200, {"ok": True}, True

    server = LoopbackServer(
        {"/": get_page},
        {"/connect": post_connect, "/close": post_close},
        server_version="ws-mail",
        denied="This page needs the link `ws mail connect --web` printed. Run it again.",
        **kw,
    )
    return server
