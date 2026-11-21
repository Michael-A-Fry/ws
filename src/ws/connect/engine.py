"""The connect engine: keeps a flow's state, walks its steps for a person, shows them to an agent, and undoes them.

State is one JSON file per flow, `<state>/connect/<id>.json` (schema `ws.connect-state/1`, 0600 in a 0700 folder): which
steps are done, the non-secret facts a verify learned (the identity label, the stable unique id, a bound chat id) and the
current nonce. A secret is never in it: a captured secret is held in memory from the prompt until its verify passes, then
goes to the secrets store (`ws.secrets`) and nowhere else. A verify that fails stores nothing, and a secret already in the
store stays as it was. Errors and output never carry the value or a URL that holds it.

Who runs what. `walk` and `revoke` run only in the person's own terminal (the command checks that: ws.connectcmd);
`dry_run`, `diagnose` and the list read state and the registry and change nothing, so an agent may run them. `dry_run` is
the agent contract (`SCHEMA_VERSION`, frozen): each step is `done` or `needs_human` with an `action`; the process exit is 0
when every step is done and 3 when a person has something to do. Not built in this phase: device-code polling (RFC 8628),
a server-side callback, re-authorising an existing connection, and QR codes.
"""

from __future__ import annotations

import json
import os
import re
import secrets as _secrets
import string
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

from ws.connect.spec import Flow
from ws.secrets import SecretsError

STATE_SCHEMA = "ws.connect-state/1"
SCHEMA_VERSION = 1  # the agent contract: frozen. A change to a key or its meaning takes a new number.
CONTRACT_KIND = "ws.connect/1"
EXIT_DONE, EXIT_ERROR, EXIT_NEEDS_HUMAN = 0, 1, 3
HTTP_TIMEOUT_S = 15
REDACTED = "<redacted>"


class ConnectError(Exception):
    """A failure with a kind the command turns into a typed error. Never carries a secret or a URL with one."""

    def __init__(self, kind: str, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.kind, self.message, self.hint = kind, message, hint


class Io(Protocol):
    def say(self, text: str) -> None: ...
    def ask(self, label: str) -> str: ...
    def ask_secret(self, label: str) -> str: ...
    def pause(self, text: str) -> str: ...


class Transport(Protocol):
    def get(self, url: str) -> tuple[int, Any]:
        """(HTTP status, decoded JSON body or None). Raises ConnectError('network', ...) without the url."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):  # a token in a URL must not follow a redirect to another host
        return None


class UrlTransport:
    def get(self, url: str) -> tuple[int, Any]:
        if urlsplit(url).scheme != "https":
            raise ConnectError("network", "refused a request that is not https")
        opener = urllib.request.build_opener(NoRedirect)
        req = urllib.request.Request(url, headers={"User-Agent": "ws-connect"})
        try:
            with opener.open(req, timeout=HTTP_TIMEOUT_S) as r:
                status, body = r.status, r.read(1 << 20)
        except urllib.error.HTTPError as e:
            status, body = e.code, e.read(1 << 20)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise ConnectError("network", f"could not reach {urlsplit(url).hostname} ({type(e).__name__})") from None
        try:
            return status, json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return status, None


class StoreLike(Protocol):
    def read(self, name: str) -> str: ...
    def write(self, name: str, value: str) -> None: ...
    def delete(self, name: str) -> bool: ...
    def names(self) -> list[str]: ...


@dataclass
class Context:
    state_dir: Path
    store: StoreLike | None = None
    registered: frozenset[str] = frozenset()
    registry_path: str = ""
    transport: Transport = field(default_factory=UrlTransport)
    io: Io | None = None
    now: Callable[[], float] = time.time
    open_url: Callable[[str], object] = webbrowser.open


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---- state -----------------------------------------------------------------------------------------------------------


def state_path(ctx: Context, flow_id: str) -> Path:
    return ctx.state_dir / "connect" / f"{flow_id}.json"


def load_state(ctx: Context, flow: Flow) -> dict:
    try:
        d = json.loads(state_path(ctx, flow.id).read_text(encoding="utf-8"))
    except FileNotFoundError:
        d = None
    except (OSError, ValueError) as e:
        raise ConnectError(
            "state",
            f"the state of {flow.id} cannot be read ({type(e).__name__})",
            f"`ws connect {flow.id} --revoke` clears it",
        ) from None
    if not isinstance(d, dict) or d.get("schema") != STATE_SCHEMA or d.get("flow") != flow.id:
        d = {"schema": STATE_SCHEMA, "flow": flow.id, "status": "pending", "steps": {}, "config": {}, "nonce": None}
    d.setdefault("steps", {})
    d.setdefault("config", {})
    return d


def save_state(ctx: Context, flow: Flow, st: dict) -> None:
    f = state_path(ctx, flow.id)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    st["ts"] = _iso(ctx.now())
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(st, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, f)


def _mark(ctx: Context, st: dict, sid: str) -> None:
    st["steps"][sid] = {"status": "done", "at": _iso(ctx.now())}


# ---- small helpers ---------------------------------------------------------------------------------------------------


def dig(obj: Any, path: str) -> Any:
    """The value at a dotted path in decoded JSON (a name, or a list index for a number), or None."""
    for part in path.split("."):
        if isinstance(obj, dict):
            obj = obj.get(part)
        elif isinstance(obj, list) and part.isdigit() and int(part) < len(obj):
            obj = obj[int(part)]
        else:
            return None
    return obj


def fill(template: str, values: dict[str, str], *, secret: str | None = None) -> str:
    """Put values into `{name}` places, URL-quoted. `{secret}` is filled only when a secret is given."""
    out = []
    for lit, field_name, _, _ in string.Formatter().parse(template):
        out.append(lit)
        if field_name:
            v = secret if field_name == "secret" else values.get(field_name)
            if v is None:
                raise KeyError(field_name)
            out.append(quote(str(v), safe=":" if field_name == "secret" else ""))
    return "".join(out)


def _values(flow: Flow, st: dict) -> dict[str, str]:
    v = {k: str(x) for k, x in st["config"].items()}
    n = st.get("nonce")
    if isinstance(n, dict) and n.get("value"):
        v["nonce"] = n["value"]
    return v


def _nonce_live(st: dict, now: float) -> bool:
    n = st.get("nonce")
    return bool(isinstance(n, dict) and n.get("value") and float(n.get("expires_ts") or 0) > now)


def _secret_kept(ctx: Context, flow: Flow, s: dict) -> bool:
    """A step that stored the secret counts as done only while the secret is still in the store (unknown store: yes)."""
    holds = (s["type"] == "capture" and any(f.get("secret") for f in s["fields"])) or (
        s["type"] == "verify" and "identity" in s
    )
    if not holds or ctx.store is None or flow.secret_name is None:
        return True
    try:
        return flow.secret_name in ctx.store.names()
    except OSError:
        return True


def _step_done(ctx: Context, flow: Flow, st: dict, i: int, now: float) -> bool:
    s = flow.steps[i]
    if st["steps"].get(s["id"], {}).get("status") != "done":
        return False
    if not _secret_kept(ctx, flow, s):
        return False
    if s["type"] == "link" and s.get("nonce") and not _nonce_live(st, now):
        # an expired nonce only matters while the verify that uses it is still to do
        for later in flow.steps[i + 1 :]:
            if (
                later["type"] == "verify"
                and "find" in later
                and st["steps"].get(later["id"], {}).get("status") != "done"
            ):
                return False
    return True


def _registered(ctx: Context, flow: Flow) -> bool:
    return flow.secret_name is None or flow.secret_name in ctx.registered


def registry_step(ctx: Context, flow: Flow) -> str:
    hosts = ", ".join(f'"{h}"' for h in flow.secret_hosts)
    return f"add this to {ctx.registry_path or 'your secrets registry'}:\n[{flow.secret_name}]\nhosts = [{hosts}]"


# ---- the agent contract ----------------------------------------------------------------------------------------------

_ACTION = {"info": "read", "link": "open_link", "capture": "paste_secret", "verify": "run_verify"}


def dry_run(ctx: Context, flow: Flow) -> dict:
    """Every step as `done` or `needs_human`, for an agent. Reads state and the registry; writes nothing, runs nothing,
    and holds no secret. `status` is `done` when every step is, else `needs_human`."""
    st = load_state(ctx, flow)
    now = ctx.now()
    vals = _values(flow, st)
    steps, blockers = [], []
    if not _registered(ctx, flow):
        blockers.append({"kind": "secret_not_registered", "name": flow.secret_name, "action": registry_step(ctx, flow)})
    for i, s in enumerate(flow.steps):
        done = _step_done(ctx, flow, st, i, now)
        row: dict[str, Any] = {"id": s["id"], "type": s["type"], "status": "done" if done else "needs_human"}
        if not done:
            row["action"] = _ACTION[s["type"]]
            if s["type"] == "capture":
                row["action"] = "paste_secret" if any(f.get("secret") for f in s["fields"]) else "enter_value"
        if s.get("title"):
            row["title"] = s["title"]
        if s["type"] == "info":
            row["text"] = s["text"]
        if s["type"] == "link":
            if s.get("text"):
                row["text"] = s["text"]
            if s.get("nonce"):
                row["url_template"] = s["url"]  # never the live nonce: an agent must not be able to bind a chat
            else:
                try:
                    row["url"] = fill(s["url"], vals)
                except KeyError:
                    row["url_template"] = s["url"]
            if s.get("nonce"):
                row["expires_in_s"] = s["expires_s"]
                if _nonce_live(st, now):
                    row["expires_at"] = _iso(float(st["nonce"]["expires_ts"]))
        if s["type"] == "capture":
            row["fields"] = [
                {"name": f["name"], "label": f["label"], "secret": bool(f.get("secret"))} for f in s["fields"]
            ]
        steps.append(row)
    done_all = all(r["status"] == "done" for r in steps) and not blockers
    nxt = next((r["id"] for r in steps if r["status"] != "done"), None)
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "contract": CONTRACT_KIND,
        "flow": flow.id,
        "title": flow.title,
        "status": "done" if done_all else "needs_human",
        "exit_code": EXIT_DONE if done_all else EXIT_NEEDS_HUMAN,
        "next_step": nxt,
        "run": f"ws connect {flow.id}",
        "steps": steps,
    }
    if blockers:
        out["blockers"] = blockers
    return out


def diagnose(ctx: Context, flow: Flow) -> dict:
    """Step state and the facts learned, with the nonce redacted, and whether the secret is registered and stored."""
    st = load_state(ctx, flow)
    cfg = {k: v for k, v in st["config"].items()}
    out: dict[str, Any] = {
        "flow": flow.id,
        "status": st.get("status", "pending"),
        "state_file": str(state_path(ctx, flow.id)),
        "steps": {s["id"]: st["steps"].get(s["id"], {}).get("status", "todo") for s in flow.steps},
        "config": cfg,
        "nonce": REDACTED if st.get("nonce") else None,
    }
    if flow.secret_name:
        stored = None
        if ctx.store is not None:
            try:
                stored = flow.secret_name in ctx.store.names()
            except OSError:
                stored = None
        out["secret"] = {"name": flow.secret_name, "registered": _registered(ctx, flow), "stored": stored}
    return out


# ---- the walk --------------------------------------------------------------------------------------------------------


def _check_http(status: int, body: Any, host: str, *, conflict: bool = False) -> None:
    if status == 409 and conflict:
        raise ConnectError("conflict", f"{host} says another consumer holds this (HTTP 409)")
    if status != 200 or (isinstance(body, dict) and body.get("ok") is False):
        why = ""
        if isinstance(body, dict) and isinstance(body.get("description"), str):
            why = f": {body['description'][:120]}"
        raise ConnectError("verify_failed", f"{host} did not accept it (HTTP {status}{why})")


def _run_identity(ctx: Context, flow: Flow, s: dict, st: dict, value: str) -> tuple[str, str]:
    url = fill(s["request"]["url"], _values(flow, st), secret=value)
    status, body = ctx.transport.get(url)
    _check_http(status, body, urlsplit(url).hostname or "the service")
    ident, uid = dig(body, s["identity"]), dig(body, s["unique_id"])
    if ident in (None, "") or uid in (None, ""):
        raise ConnectError("verify_failed", "the answer had no identity or no unique id")
    return str(ident), str(uid)


def _run_find(ctx: Context, flow: Flow, s: dict, st: dict, value: str) -> dict | None:
    vals = _values(flow, st)
    url = fill(s["request"]["url"], vals, secret=value)
    status, body = ctx.transport.get(url)
    _check_http(status, body, urlsplit(url).hostname or "the service", conflict="conflict" in s)
    f = s["find"]
    items = dig(body, f["list"])
    want = {p: _plain(t, vals) for p, t in f["where"].items()}
    for item in items if isinstance(items, list) else []:
        if all(str(dig(item, p)) == w for p, w in want.items()):
            got = {k: dig(item, p) for k, p in f["take"].items()}
            if all(v is not None for v in got.values()):
                return {k: str(v) for k, v in got.items()}
    return None


def _plain(template: str, vals: dict[str, str]) -> str:
    """Fill a `where` text with the raw values (no URL quoting: it is compared, not sent)."""
    return string.Formatter().vformat(template, (), vals)


def _read_secret(ctx: Context, flow: Flow) -> str:
    assert ctx.store is not None and flow.secret_name
    try:
        return ctx.store.read(flow.secret_name)
    except (OSError, SecretsError) as e:  # neither carries a value
        raise ConnectError(
            "secret_missing",
            f"{flow.secret_name} is not in the secrets store",
            f"walk the capture step again: `ws connect {flow.id} --revoke`, then `ws connect {flow.id}`",
        ) from e


def walk(ctx: Context, flow: Flow) -> dict:
    """Walk the steps that are not done, in a person's terminal. Returns {"status": "done"|"stopped", ...}.
    Raises ConnectError for a secret that is not registered, a verify that fails (nothing stored) or a bad value."""
    io = ctx.io
    assert io is not None
    if not _registered(ctx, flow):
        raise ConnectError(
            "not_registered",
            f"{flow.secret_name} is not in the secrets registry",
            registry_step(ctx, flow) + "\nthen run this again (ws never edits the registry for you)",
        )
    if flow.secret_name and ctx.store is None:
        raise ConnectError(
            "store",
            "the secrets store can't be opened for writing here",
            "this profile keeps secrets elsewhere (OpenBao): write the value there; ws connect needs the local store",
        )
    st = load_state(ctx, flow)
    io.say(flow.title)
    i = 0
    while i < len(flow.steps):
        s = flow.steps[i]
        now = ctx.now()
        if _step_done(ctx, flow, st, i, now):
            i += 1
            continue
        if s["type"] == "info":
            io.say(f"\n{s.get('title', s['id'])}\n{s['text']}")
            io.pause("Press Enter when you have read it")
            _mark(ctx, st, s["id"])
        elif s["type"] == "link":
            vals = _values(flow, st)
            if s.get("nonce") and not _nonce_live(st, now):
                st["nonce"] = {"value": _secrets.token_urlsafe(32)[:64], "expires_ts": now + s["expires_s"]}
                vals["nonce"] = st["nonce"]["value"]
            io.say(f"\n{s.get('title', s['id'])}\n{s.get('text', '')}".rstrip())
            url = fill(s["url"], vals)
            try:
                opened = bool(ctx.open_url(url))
            except (OSError, webbrowser.Error):
                opened = False
            io.say(f"  {'Opened in your browser. If it did not open, use' if opened else 'Open'}: {url}")
            if s.get("nonce"):
                io.say(f"  This link works for {s['expires_s'] // 60} minutes.")
            io.pause("Press Enter when you have done it")
            _mark(ctx, st, s["id"])
        elif s["type"] == "capture":
            i = _capture(ctx, flow, st, i)
        else:
            _verify_find(ctx, flow, st, s)
            if st["steps"].get(s["id"], {}).get("status") != "done":
                save_state(ctx, flow, st)
                return {"status": "stopped", "flow": flow.id, "at": s["id"]}
        save_state(ctx, flow, st)
        i += 1
    st["status"] = "done"
    save_state(ctx, flow, st)
    io.say(f"\nConnected: {st['config'].get('identity', flow.id)}")
    return {"status": "done", "flow": flow.id, "identity": st["config"].get("identity")}


def _capture(ctx: Context, flow: Flow, st: dict, i: int) -> int:
    """Ask for the fields. A secret field is held in memory; the next step (its verify) runs on it, and only a pass
    stores it. Returns the index of the last step this handled."""
    io = ctx.io
    assert io is not None
    s = flow.steps[i]
    io.say(f"\n{s.get('title', s['id'])}")
    secret_value, plain = None, {}
    for f in s["fields"]:
        raw = io.ask_secret(f["label"]) if f.get("secret") else io.ask(f["label"])
        raw = raw.strip()
        if not raw or (f.get("pattern") and not re.fullmatch(f["pattern"], raw)):
            raise ConnectError("bad_value", f"that is not a valid {f['name']}", "nothing was stored; run it again")
        if f.get("secret"):
            secret_value = raw
        else:
            plain[f["name"]] = raw
    if secret_value is None:
        st["config"].update(plain)
        _mark(ctx, st, s["id"])
        return i
    v = flow.steps[i + 1]  # spec guarantees an identity verify follows a secret capture
    assert ctx.store is not None and flow.secret_name
    ident, uid = _run_identity(ctx, flow, v, st, secret_value)  # raises ConnectError; nothing has been stored
    ctx.store.write(flow.secret_name, secret_value)
    st["secret_stored"] = True  # ws connect put it there: only then may --revoke delete it
    old = st["config"].get(flow.unique_key)
    if old is not None and old != uid:  # a different account: what was bound to the old one no longer holds
        for later in flow.steps[i + 2 :]:
            st["steps"].pop(later["id"], None)
            for key in (later.get("find") or {}).get("take", {}):
                st["config"].pop(key, None)
        st["nonce"] = None
        io.say(f"  This is a different {flow.unique_key} from before: the later steps run again.")
    st["config"].update(plain)
    st["config"]["identity"] = ident
    st["config"][flow.unique_key] = uid
    _mark(ctx, st, s["id"])
    _mark(ctx, st, v["id"])
    io.say(f"  Verified: {ident} ({flow.unique_key} {uid})")
    return i + 1


def _verify_find(ctx: Context, flow: Flow, st: dict, s: dict) -> None:
    io = ctx.io
    assert io is not None
    value = _read_secret(ctx, flow)
    while True:
        try:
            got = _run_find(ctx, flow, s, st, value)
        except ConnectError as e:
            if e.kind != "conflict":
                raise
            got = _resolve_conflict(ctx, flow, st, s, value, e)
        if got is not None:
            st["config"].update(got)
            st["nonce"] = None
            _mark(ctx, st, s["id"])
            io.say(f"  {s.get('title', s['id'])}: done")
            return
        if not _nonce_live(st, ctx.now()):
            raise ConnectError(
                "expired", "the link ran out before it was used", f"run `ws connect {flow.id}` again for a new one"
            )
        if io.pause("Not seen yet. Press Enter to check again, or q then Enter to stop").strip().lower() == "q":
            return


def _resolve_conflict(ctx: Context, flow: Flow, st: dict, s: dict, value: str, e: ConnectError) -> dict | None:
    """The service refused the check because something else is set up on the same bot (Telegram: a webhook). Say what
    and offer the step that clears it, run only on a yes; then look again."""
    io = ctx.io
    assert io is not None
    c = s["conflict"]
    io.say(f"  {c['text']}")
    if io.ask("Run the fix now? (yes/no)").strip().lower() not in ("y", "yes"):
        raise ConnectError("conflict", e.message, c["text"])
    url = fill(c["fix_url"], _values(flow, st), secret=value)
    status, body = ctx.transport.get(url)
    _check_http(status, body, urlsplit(url).hostname or "the service")
    io.say("  Done. Checking again.")
    return _run_find(ctx, flow, s, st, value)


# ---- revoke ----------------------------------------------------------------------------------------------------------


def revoke(ctx: Context, flow: Flow, assume_yes: bool = False) -> dict:
    """Undo each step that declares a revoke, last step first: delete the secret, tell the person what to do elsewhere,
    forget what was learned. Then clear the state, so the flow can be walked again. Asks first (default no) unless
    `assume_yes`; deletes the secret only when this flow's own state says ws connect stored it."""
    st = load_state(ctx, flow)
    mine = bool(st.get("secret_stored"))
    if not assume_yes:
        if ctx.io is None:
            raise ConnectError("confirm", "revoke needs a yes", "run it at a terminal, or add --yes")
        what = f"delete {flow.secret_name} and " if mine and flow.secret_name else ""
        if ctx.io.ask(f"Revoke {flow.id}: {what}clear its state? (y/N)").strip().lower() not in ("y", "yes"):
            return {"flow": flow.id, "did": [], "tell": [], "status": "cancelled"}
    did, told = [], []
    for s in reversed(flow.steps):
        rv = s.get("revoke")
        if not rv:
            continue
        if rv["action"] == "delete_secret" and ctx.store is not None and flow.secret_name:
            if mine and ctx.store.delete(flow.secret_name):
                did.append(f"deleted {flow.secret_name} from the secrets store")
            elif not mine and flow.secret_name in ctx.store.names():
                did.append(f"left {flow.secret_name} in the store: ws connect did not put it there")
        elif rv["action"] == "tell":
            told.append(rv["text"])
        elif rv["action"] == "forget":
            st["config"].clear()
            did.append("forgot what the flow had learned")
    try:
        state_path(ctx, flow.id).unlink()
        did.append("cleared the flow's state")
    except FileNotFoundError:
        pass
    return {"flow": flow.id, "did": did, "tell": told, "status": "revoked"}
