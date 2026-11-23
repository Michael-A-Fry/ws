"""Load and check a connect flow file: the closed set of step types, with every rule that can be checked before a step runs.

A flow is TOML text (`flows/<id>.py` holds it in `TOML`; outside the package it is `<id>.toml`; schema `ws.connect-flow/1`; docs/reference/connect-flow.schema.json says the same in
JSON Schema). `load` raises `FlowError` for the first fault, with the file and the step named, never a value. What it
promises: every URL is https; a `{secret}` is only ever sent to a host the flow's `[secret]` block names; a secret
capture is followed at once by the verify that proves it (so nothing is kept until a real call passes); every
`{placeholder}` in a URL or a `where` is one the engine will fill; ids are plain and unique.
"""

from __future__ import annotations

import importlib
import os
import re
import string
import tomllib
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from ws import secrets as wsecrets

SCHEMA = "ws.connect-flow/1"
ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,31}$")
RESERVED_IDS = frozenset({"list"})
RESERVED_STEP_IDS = frozenset({"register-secret"})  # the engine's own first row when the secret is not registered
STEP_TYPES = ("info", "link", "capture", "verify")
REVOKE_ACTIONS = ("delete_secret", "tell", "forget")
NONCE_MAX_S = 86400
NONCE_MIN_S = 60


class FlowError(Exception):
    """A flow file that is not valid. The message names the file and the step; it never holds a value."""


@dataclass(frozen=True)
class Flow:
    id: str
    title: str
    unique_key: str
    secret_name: str | None
    secret_hosts: tuple[str, ...]
    steps: tuple[dict, ...]
    path: Path | None = None
    secret_entry: tuple[
        tuple[str, object], ...
    ] = ()  # the registry keys of [secret] (hosts, base_path, purposes, paths)

    def registry_block(self) -> str:
        """The registry entry `ws connect` adds for this flow, on the person's yes (ADR 0034)."""
        assert self.secret_name
        return wsecrets.entry_block(
            self.secret_name, dict(self.secret_entry), comment=f"added by ws connect {self.id}, on your yes (ADR 0034)"
        )


def flows_dir() -> Path:
    return Path(__file__).resolve().parent / "flows"


def _fields(text: str) -> list[str]:
    try:
        return [f for _, f, _, _ in string.Formatter().parse(text) if f]
    except ValueError as e:
        raise FlowError(f"bad placeholder in {text!r}: {e}") from e


def _need(cond: bool, where: str, why: str) -> None:
    if not cond:
        raise FlowError(f"{where}: {why}")


def _text(d: dict, key: str, where: str, required: bool = True) -> str | None:
    v = d.get(key)
    if v is None:
        _need(not required, where, f"{key} is needed")
        return None
    _need(isinstance(v, str) and bool(v.strip()) and "\x00" not in v, where, f"{key} must be a non-empty string")
    return v


def _https(url: str, where: str) -> str:
    probe = re.sub(r"\{[a-z_][a-z0-9_]*\}", "x", url)
    p = urlsplit(probe)
    _need(p.scheme == "https" and bool(p.hostname), where, "url must be https with a host")
    _need(not p.username and not p.password, where, "url must not carry a user or password")
    host = urlsplit(url).hostname
    _need("{" not in (host or "{"), where, "the host part of a url is fixed text, never a placeholder")
    return (host or "").lower()


def _only(d: dict, allowed: set[str], where: str) -> None:
    extra = set(d) - allowed
    _need(not extra, where, f"unknown keys: {', '.join(sorted(extra))}")


def parse(raw: dict, path: Path | None = None) -> Flow:
    """Check a decoded flow and return it. Raises FlowError."""
    name = path.name if path else "flow"
    _only(raw, {"schema", "id", "title", "unique_key", "secret", "steps"}, name)
    _need(raw.get("schema") == SCHEMA, name, f"schema must be {SCHEMA!r}")
    fid = _text(raw, "id", name) or ""
    _need(bool(ID_RE.match(fid)) and fid not in RESERVED_IDS, name, "id must be lower-case letters, digits and -")
    if path and path.stem != fid and not path.stem.startswith("_"):
        raise FlowError(f"{name}: the file is named for the id {fid!r}")
    title = _text(raw, "title", name) or ""
    ukey = _text(raw, "unique_key", name) or ""
    _need(bool(re.match(r"^[a-z][a-z0-9_]{0,31}$", ukey)), name, "unique_key must be a short snake_case name")
    _need(ukey not in {"identity", "secret", "nonce"}, name, "unique_key may not be identity, secret or nonce")
    sec = raw.get("secret")
    sname, hosts, secret_entry = None, (), ()
    if sec is not None:
        _need(isinstance(sec, dict), name, "[secret] must be a table")
        _only(sec, {"name", "hosts", "purposes", "base_path", "paths"}, f"{name} [secret]")
        sname = _text(sec, "name", f"{name} [secret]")
        _need(bool(wsecrets.NAME_RE.match(sname or "")), name, "[secret] name is not a valid name")
        h = sec.get("hosts")
        _need(isinstance(h, list) and bool(h) and all(isinstance(x, str) for x in h), name, "[secret] hosts is a list")
        hosts = tuple(x.lower() for x in h)
        _need(all(wsecrets.HOST_RE.match(x) for x in hosts), name, "[secret] hosts are plain host names")
        entry: dict[str, object] = {"hosts": list(hosts)}
        for key in ("base_path", "purposes", "paths"):
            if key in sec:
                entry[key] = sec[key]
        try:
            wsecrets.entry_block(sname or "", entry)  # the broker's own rules for an entry
        except wsecrets.SecretsError as e:
            raise FlowError(f"{name} [secret]: {e}") from None
        secret_entry = tuple((k, tuple(v) if isinstance(v, list) else v) for k, v in entry.items())
    steps = raw.get("steps")
    _need(isinstance(steps, list) and bool(steps), name, "steps must be a non-empty list")
    seen: set[str] = set()
    known = {"identity", "nonce", ukey}
    secret_pending = False
    for i, s in enumerate(steps):
        _need(isinstance(s, dict), name, f"step {i + 1} must be a table")
        sid = _text(s, "id", f"{name} step {i + 1}") or ""
        where = f"{name} step {sid!r}"
        _need(bool(ID_RE.match(sid)) and sid not in seen, where, "id must be unique, lower-case letters, digits and -")
        _need(sid not in RESERVED_STEP_IDS, where, f"{sid!r} is the engine's own step id")
        seen.add(sid)
        typ = s.get("type")
        _need(typ in STEP_TYPES, where, f"type must be one of {', '.join(STEP_TYPES)}")
        base = {"id", "type", "title", "revoke"}
        _need(not secret_pending or typ == "verify", where, "a secret capture must be followed by its verify step")
        if typ == "info":
            _only(s, base | {"text"}, where)
            _text(s, "text", where)
        elif typ == "link":
            _only(s, base | {"url", "text", "nonce", "expires_s"}, where)
            url = _text(s, "url", where) or ""
            _https(url, where)
            for f in _fields(url):
                _need(f in known, where, f"{{{f}}} is not known here (known: {', '.join(sorted(known))})")
            if s.get("nonce"):
                e = s.get("expires_s")
                _need(
                    isinstance(e, int) and not isinstance(e, bool) and NONCE_MIN_S <= e <= NONCE_MAX_S,
                    where,
                    f"a nonce link needs expires_s between {NONCE_MIN_S} and {NONCE_MAX_S}",
                )
                _need("{nonce}" in url, where, "a nonce link carries {nonce} in its url")
            else:
                _need("expires_s" not in s and "{nonce}" not in url, where, "expires_s and {nonce} need nonce = true")
        elif typ == "capture":
            _only(s, base | {"fields"}, where)
            fs = s.get("fields")
            _need(isinstance(fs, list) and bool(fs), where, "fields must be a non-empty list")
            secrets_n = 0
            for f in fs:
                _need(isinstance(f, dict), where, "a field must be a table")
                _only(f, {"name", "label", "secret", "pattern"}, where)
                fname = _text(f, "name", where) or ""
                _need(
                    bool(re.match(r"^[a-z][a-z0-9_]{0,31}$", fname)), where, f"field name {fname!r} is not snake_case"
                )
                _need(fname not in {"identity", "secret", "nonce"}, where, f"field name {fname!r} is reserved")
                _text(f, "label", where)
                pat = f.get("pattern")
                if pat is not None:
                    try:
                        re.compile(pat)
                    except (re.error, TypeError) as e:
                        raise FlowError(f"{where}: pattern is not a regular expression") from e
                if f.get("secret"):
                    secrets_n += 1
                else:
                    known.add(fname)
            _need(secrets_n <= 1, where, "at most one secret field")
            if secrets_n:
                _need(sname is not None, where, "a secret field needs a [secret] block")
                secret_pending = True
        else:  # verify
            _only(s, base | {"request", "identity", "unique_id", "find", "conflict"}, where)
            req = s.get("request")
            _need(isinstance(req, dict), where, "request must be a table with a url")
            _only(req, {"url"}, f"{where} request")
            url = _text(req, "url", f"{where} request") or ""
            host = _https(url, where)
            fl = _fields(url)
            if sname:
                _need(host in hosts, where, f"host {host!r} is not in [secret] hosts")
            else:
                _need("secret" not in fl, where, "{secret} needs a [secret] block")
            _need("conflict" not in s or "find" in s, where, "conflict belongs to a find verify")
            has_id = "identity" in s or "unique_id" in s
            has_find = "find" in s
            _need(has_id != has_find, where, "a verify step has identity and unique_id, or find, not both or neither")
            if has_id:
                _text(s, "identity", where)
                _text(s, "unique_id", where)
                secret_pending = False
            else:
                _need(not secret_pending, where, "the verify after a secret capture must have identity and unique_id")
                f = s["find"]
                _need(isinstance(f, dict), where, "find must be a table")
                _only(f, {"list", "where", "take"}, f"{where} find")
                _text(f, "list", f"{where} find")
                w, t = f.get("where"), f.get("take")
                _need(
                    isinstance(w, dict) and bool(w) and all(isinstance(v, str) for v in w.values()),
                    where,
                    "find.where maps a path to the text it must equal",
                )
                _need(
                    isinstance(t, dict) and bool(t) and all(isinstance(v, str) for v in t.values()),
                    where,
                    "find.take maps a name to the path to keep",
                )
                for v in w.values():
                    for ff in _fields(v):
                        _need(ff in known, where, f"{{{ff}}} is not known here")
                for k in t:
                    _need(
                        bool(re.match(r"^[a-z][a-z0-9_]{0,31}$", k)) and k not in {"identity", "secret", "nonce"},
                        where,
                        f"take name {k!r} is not allowed",
                    )
                known.update(t)
                cf = s.get("conflict")
                if cf is not None:
                    _need(isinstance(cf, dict), where, "conflict must be a table")
                    _only(cf, {"text", "fix_url"}, f"{where} conflict")
                    _text(cf, "text", f"{where} conflict")
                    fu = _text(cf, "fix_url", f"{where} conflict") or ""
                    _need(_https(fu, where) in hosts, where, "conflict fix_url host is not in [secret] hosts")
                    for ff in _fields(fu):
                        _need(ff == "secret" or ff in known, where, f"{{{ff}}} is not known here")
            for ff in fl:
                _need(ff == "secret" or ff in known, where, f"{{{ff}}} is not known here")
        rv = s.get("revoke")
        if rv is not None:
            _need(isinstance(rv, dict), where, "revoke must be a table")
            _only(rv, {"action", "text"}, f"{where} revoke")
            act = rv.get("action")
            _need(act in REVOKE_ACTIONS, where, f"revoke action must be one of {', '.join(REVOKE_ACTIONS)}")
            if act == "tell":
                _text(rv, "text", f"{where} revoke")
            else:
                _need("text" not in rv, where, "text belongs to the tell action")
            if act == "delete_secret":
                _need(sname is not None, where, "delete_secret needs a [secret] block")
        if typ == "verify" and "identity" in s:
            known.add("identity")
    _need(not secret_pending, name, "a secret capture must be followed by its verify step")
    return Flow(fid, title, ukey, sname, hosts, tuple(steps), path, secret_entry)


def load_file(path: Path) -> Flow:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise FlowError(f"{path.name}: cannot be read ({type(e).__name__})") from e
    return _from_text(text, path)


def _from_text(text: str, path: Path | None) -> Flow:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise FlowError(f"{path.name if path else 'flow'}: not valid TOML ({e})") from e
    return parse(raw, path)


def shipped_text(name: str) -> str:
    """The TOML text of a flow that ships in the package (`telegram`, or `_template`). FlowError when there is none."""
    mod = name.replace("-", "_")
    if not re.match(r"^_?[a-z][a-z0-9_]*$", mod):
        raise FlowError(f"{name!r} is not a flow id")
    try:
        return importlib.import_module(f"ws.connect.flows.{mod}").TOML
    except (ImportError, AttributeError) as e:
        raise FlowError(f"no flow named {name!r}") from e


def load(flow_id: str, folder: Path | None = None) -> Flow:
    """The flow with this id: from the package, or from `<folder>/<id>.toml` when a folder is given. FlowError when
    there is none or it is not valid."""
    if not ID_RE.match(flow_id or "") or flow_id in RESERVED_IDS:
        raise FlowError(f"{flow_id!r} is not a flow id")
    if folder is not None:
        f = folder / f"{flow_id}.toml"
        if not f.is_file():
            raise FlowError(f"no flow named {flow_id!r}")
        return load_file(f)
    flow = _from_text(shipped_text(flow_id), None)
    if flow.id != flow_id:
        raise FlowError(f"the flow {flow_id!r} holds the id {flow.id!r}")
    return flow


def ids(folder: Path | None = None) -> list[str]:
    """The ids of the flows, `_template` and any `_` file left out: the package's, or those of a folder of `.toml`."""
    if folder is not None:
        try:
            return sorted(p.stem for p in folder.glob("*.toml") if not p.name.startswith("_"))
        except OSError:
            return []
    try:
        names = [e.name[:-3] for e in os.scandir(flows_dir()) if e.name.endswith(".py") and not e.name.startswith("_")]
    except OSError:
        return []
    return sorted(n.replace("_", "-") for n in names)
