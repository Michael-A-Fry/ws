"""`ws ask`: a session leaves a question for its person, who answers it later; one queue across every repo and session.

The store is `asks.jsonl`, the file `ws home` already reads (`[home.sources] asks_jsonl`, else `.claude-usage/asks/`
below the home folder), so there is one asks store and not two. It is append-only events, folded into asks: `added`,
`answered`, `done` and `withdrawn`. `ts` is epoch milliseconds, as the OpenCode shim writes it, and `at` is the same
moment as RFC 3339 text, which `ws.jsonl` needs for its stamp. Every write goes through `ws.jsonl`'s lock.

A repo's data class decides what the store may hold: the strictest of the named repo's class and the working folder's
(from the policy), or any hit on the sensitive-terms file, stores the ask redacted (no text, no options, a hashed dedupe
key). The class is checked again when an ask is read, so a repo walled after it was asked shows as opaque. A token-shaped secret is refused outright. Notify is not built: `NOTIFY_HOOK` does nothing.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import secrets as _random
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ws import jsonl, msg, output, paths, policy
from ws.output import WsError

COMMAND = "ask"
SCHEMA = 1
OPAQUE = "Open the session to see this ask."
EVENTS = ("added", "answered", "withdrawn", "done")
CLOSED = ("withdrawn", "done")
LIMITS = {
    "question": 2000,
    "label": 80,
    "description": 300,
    "decision": 300,
    "dedupe_key": 300,
    "repo": 100,
    "session": 100,
}
MAX_OPTIONS = 6
LOCK_WAIT_S = 30.0  # how long a writer waits for `asks.lock` before it gives up
MAX_BYTES = 8 * 1024 * 1024  # a log turns over here, well under the 64 MiB `ws home` reads
DEFAULT_WAIT = 540  # seconds: under a 10-minute tool limit
RANK = {a: i for i, a in enumerate(policy.ACCESS)}  # a higher number is stricter
FLOWS = {"full": "full", "walled": "counts", "counts-only": "off"}  # the old program's `flows_back`, by policy access
NOTIFY_HOOK = None  # a function(ask) a later release may set; nothing is sent by default
_ID = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
_TARGET = re.compile(r"\A[A-Za-z0-9_.:@%=+][A-Za-z0-9_.:@%=+-]*\Z")  # a tmux target: no leading '-'


# --- where, and the class wall ---------------------------------------------------------------------------------------


def _env(env=None) -> dict:
    return dict(paths.environ() if env is None else env)


def store_path(env=None) -> Path:
    """The file `ws home` reads asks from: `[home.sources] asks_jsonl`, else `.claude-usage/asks/asks.jsonl` in HOME."""
    from ws.home import api

    e = _env(env)
    try:
        return api.Env.make(e).path("asks_jsonl")
    except WsError:
        return paths.home_dir(e) / ".claude-usage" / "asks" / "asks.jsonl"


def terms_path(env=None) -> Path:
    """The sensitive-terms file: `WS_ASK_TERMS`, else `ask-terms.txt` in the ws config folder."""
    e = _env(env)
    return Path(e["WS_ASK_TERMS"]) if e.get("WS_ASK_TERMS") else paths.config_dir(e) / "ask-terms.txt"


def terms_regex(env=None) -> re.Pattern | None:
    """The terms file as one regex. A plain line matches in any case as a substring; `=TERM` matches that exact case as a
    whole word; `#` starts a comment. A missing or unreadable file is None (the caller redacts). A readable file with no
    term is a regex that matches nothing: it says plainly that there are none."""
    try:
        text = terms_path(env).read_text(encoding="utf-8")
    except (OSError, ValueError):
        return None
    plain, exact = [], []
    for line in (ln.strip() for ln in text.splitlines()):
        if line.startswith("=") and line[1:].strip():
            exact.append(re.escape(line[1:].strip()))
        elif line and not line.startswith(("#", "=")):
            plain.append(re.escape(line))
    alts = ([r"(?i:" + "|".join(plain) + ")"] if plain else []) + (
        [r"(?<![A-Za-z0-9_])(?:" + "|".join(exact) + r")(?![A-Za-z0-9_])"] if exact else []
    )
    return re.compile("|".join(alts)) if alts else re.compile(r"(?!)")


def _policy(env):
    try:
        return policy.load(policy.policy_path(env))
    except (policy.PolicyError, OSError):
        return None  # an unreadable policy classifies nothing: the strictest answer


def _access(pol, path) -> str:
    """The access of the class holding `path`. No `[classes]` in the policy means nothing is classified, so full; a policy
    that did not load, or a path in no class, is the strictest."""
    if pol is None:
        return "counts-only"
    cls = policy.class_of(pol, path)
    return "full" if cls is None and not pol.classes else (cls.access if cls else "counts-only")


def _name_access(pol, repo: str, cwd: str) -> str:
    """The access of a repo named by label. A class whose repo glob ends in that name counts; else a name that is a folder
    of `cwd` is the class of `cwd` (the same repo); any other name is the strictest."""
    if pol is None:
        return "counts-only"
    if not pol.classes:
        return "full"
    hit = [c.access for c in pol.classes.values() for g in c.repos if g.rstrip("/*").rsplit("/", 1)[-1] == repo]
    if hit:
        return max(hit, key=RANK.__getitem__)
    return _access(pol, cwd) if repo in Path(cwd).parts else "counts-only"


def _repo_name(cwd: str) -> str:
    """The name of the repo `cwd` is in: the folder above the nearest `.git`, else the folder's own name."""
    here = Path(cwd)
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d.name
    return here.name or "unknown"


def _strictest(*access: str) -> str:
    return max(access, key=RANK.__getitem__)


# --- the store -------------------------------------------------------------------------------------------------------


def _now() -> tuple[int, str]:
    t = time.time()
    return int(t * 1000), jsonl.iso_utc(t)


@contextlib.contextmanager
def _store_lock(env=None):
    """Hold `asks.lock`, the lock beside the store that the older `ws-ask` program also takes around its read-then-append,
    so a check and the write that follows it are one step against every writer of this file. `ws.jsonl` still takes its
    own lock inside each append. Raises WsError `unavailable` if the lock stays held or the folder cannot be used."""
    path = store_path(env)
    try:
        import fcntl
    except ImportError:  # no flock here (native Windows): the append's own lock is all there is
        yield
        return
    fd = None
    try:
        fresh = not path.parent.exists()
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if fresh:
            os.chmod(path.parent, 0o700)
        fd = os.open(path.parent / "asks.lock", os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
        deadline = time.monotonic() + LOCK_WAIT_S
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise OSError("asks.lock stayed locked") from None
                time.sleep(0.02)
    except OSError as e:
        if fd is not None:
            os.close(fd)
        raise WsError(
            "unavailable", f"cannot use the asks store ({e})", f"check that {path.parent} is yours and writable", 1
        ) from e
    try:
        yield
    finally:
        os.close(fd)


def _write(env, event: dict, held: bool = False) -> None:
    """Append one event. Pass `held=True` when the caller already holds `_store_lock`."""
    if not held:
        with _store_lock(env):
            return _write(env, event, True)
    path = store_path(env)
    ms, at = _now()
    rec = {"schema": SCHEMA, "ts": ms, "at": at, **event}
    try:
        jsonl.append(path, rec, max_bytes=MAX_BYTES, time_keys=("at",))
    except (OSError, jsonl.LockTimeout) as e:
        raise WsError(
            "unavailable", f"cannot write the asks store ({e})", f"check that {path.parent} is yours and writable", 1
        ) from e
    with contextlib.suppress(OSError):
        if os.name == "posix":
            os.chmod(path.parent, 0o700)  # a folder made earlier by something else is narrowed too


def records(env=None) -> list[dict]:
    try:
        return jsonl.read(store_path(env))
    except (OSError, jsonl.LockTimeout):
        return []


def _ms(value) -> int | None:
    """A time in epoch milliseconds: a number as it is, or the older program's RFC 3339 text (`...Z`); None if neither."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, str):
        try:
            return int(datetime.fromisoformat(value).timestamp() * 1000)
        except (ValueError, OverflowError, OSError):
            return None
    return None


def fold(recs: list[dict]) -> dict[str, dict]:
    """Events folded into asks by id, in file order. Each ask is its `added` fields plus `status` (open, answered, done,
    withdrawn), `answer` and `closed_ts`. A line that does not apply (unknown id, bad event) is skipped, never raised."""
    asks: dict[str, dict] = {}
    for r in recs:
        ident, event = r.get("id"), r.get("event")
        if not isinstance(ident, str) or not ident or event not in EVENTS:
            continue
        if event == "added":
            if ident not in asks:
                asks[ident] = {
                    **{k: v for k, v in r.items() if k not in ("event", "v")},
                    "ts": _ms(r.get("ts")),
                    "status": "open",
                    "answer": None,
                    "closed_ts": None,
                }
            continue
        ask = asks.get(ident)
        if ask is None:
            continue
        if event == "answered" and ask["status"] == "open":
            a = r.get("answer")
            ask["status"] = "answered"
            ask["answer"] = (
                a
                if isinstance(a, dict)
                else {
                    "text": r.get("decision") if isinstance(r.get("decision"), str) else None,
                    "option_index": None,
                    "via": "runner",
                }
            )
        elif event in CLOSED and ask["status"] in ("open", "answered") and (event == "done" or ask["status"] == "open"):
            ask["status"], ask["closed_ts"] = event, _ms(r.get("ts"))
    return asks


def _ts(ask: dict) -> int:
    return _ms(ask.get("ts")) or 0


def load(env=None) -> dict[str, dict]:
    return fold(records(env))


def get(ident: str, env=None) -> dict:
    ask = load(env).get(ident) if isinstance(ident, str) and _ID.match(ident) else None
    if ask is None:
        raise WsError("unknown_ask", f"no such ask: {str(ident)[:40]}", "`ws ask list --all` shows them", 2)
    return ask


def _hash(key: str) -> str:
    return "sha256:" + hashlib.sha256(key.encode("utf-8")).hexdigest()


def view(ask: dict, env=None, pol=None) -> dict:
    """The ask as a reader may see it. It is opaque when it was stored redacted, its class when added was not full, or the
    class of its repo now is not full; an opaque view keeps the id, repo, status and times and drops the text, options,
    decision, session name, answer text and dedupe key."""
    pol = pol if pol is not None else _policy(_env(env))
    repo = ask.get("repo") if isinstance(ask.get("repo"), str) else "?"
    now = _name_access(pol, repo, str(ask.get("cwd") or ""))
    out = dict(ask)
    # only an explicit `redacted: false` is visible, as in `ws home`: a line that doesn't say is treated as redacted
    out["opaque"] = ask.get("redacted") is not False or ask.get("flows", "full") != "full" or now != "full"
    if out["opaque"]:
        out.update(question=OPAQUE, options=[], decision="", free_text_allowed=False, dedupe_key=None)
        if isinstance(ask.get("session"), dict):
            out["session"] = dict(ask["session"], name="")
        if isinstance(ask.get("answer"), dict):
            out["answer"] = dict(ask["answer"], text=None)
    return out


# --- add -------------------------------------------------------------------------------------------------------------


def _text(field: str, value, limit: int, required: bool = False) -> str:
    if value is None or value == "":
        if required:
            raise WsError("usage", f"{field} is required", "", 2)
        return ""
    if not isinstance(value, str):
        raise WsError("refused", f"{field} must be text", "", 1)
    value = value.strip()
    if required and not value:
        raise WsError("usage", f"{field} is required", "", 2)
    if len(value) > limit:
        raise WsError("refused", f"{field} is too long ({len(value)} characters, at most {limit})", "", 1)
    return value


def _options(raw) -> list[dict]:
    if raw in (None, ""):
        return []
    if not isinstance(raw, list) or len(raw) > MAX_OPTIONS:
        raise WsError("refused", f"options must be a list of at most {MAX_OPTIONS}", "", 1)
    out = []
    for n, o in enumerate(raw):
        o = {"label": o} if isinstance(o, str) else o
        if not isinstance(o, dict):
            raise WsError("refused", f"options[{n}] must be a label and description", "", 1)
        out.append(
            {
                "label": _text(f"options[{n}].label", o.get("label"), LIMITS["label"], True),
                "description": _text(f"options[{n}].description", o.get("description"), LIMITS["description"]),
            }
        )
    return out


def _refuse_secret(field: str, value) -> None:
    if isinstance(value, str) and value and msg.scan(value):
        raise WsError(
            "secret", f"a secret-shaped value in {field} (nothing stored)", "name the secret, never paste it", 1
        )


def _session(raw, env) -> dict:
    raw = {"name": raw} if isinstance(raw, str) else raw if isinstance(raw, dict) else {}
    name = raw.get("name") or _env(env).get("WS_SESSION_NAME") or ""
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)[: LIMITS["session"]] if isinstance(name, str) else ""
    tmux = raw.get("tmux")
    return {"name": name, "tmux": tmux if isinstance(tmux, str) and _TARGET.match(tmux) and len(tmux) <= 200 else None}


def add(fields: dict, cwd: str, env=None) -> tuple[str, bool, dict]:
    """Add an ask. Returns (id, created, the event written). An open ask with the same dedupe key is returned and nothing
    is written. Raises WsError: `secret` or `refused` (exit 1), `usage` (exit 2), `unavailable` (exit 1)."""
    e = _env(env)
    question = _text("question", fields.get("question"), LIMITS["question"], True)
    decision = _text("decision", fields.get("decision"), LIMITS["decision"])
    options = _options(fields.get("options"))
    key = _text("dedupe_key", fields.get("dedupe_key"), LIMITS["dedupe_key"]) or None
    pol = _policy(e)
    cwd = str(cwd)
    repo = _text("repo", fields.get("repo") or _repo_name(cwd), LIMITS["repo"], True)
    if re.search(r"[\x00-\x1f\x7f]", repo):
        raise WsError("refused", "repo has control characters", "", 1)
    for field, value in [("question", question), ("decision", decision), ("dedupe_key", key)] + [
        (f"options[{n}].{k}", o[k]) for n, o in enumerate(options) for k in ("label", "description")
    ]:
        _refuse_secret(field, value)

    access = _strictest(_name_access(pol, repo, cwd), _access(pol, cwd))
    terms = terms_regex(e)
    texts = [question, decision] + [t for o in options for t in (o["label"], o["description"])]
    reason = (
        f"its class is {access}"
        if access != "full"
        else "no sensitive-terms file"
        if terms is None
        else "a sensitive term"
        if any(terms.search(t) for t in texts)
        else ""
    )
    redacted = bool(reason)
    free_text = bool(fields.get("free_text_allowed", True))
    if redacted:
        question, options, decision, free_text = OPAQUE, [], "", False
    stored_key = _hash(key) if key and redacted else key

    with _store_lock(e):
        ids = load(e)
        if key:
            for a in ids.values():
                if a["status"] == "open" and a.get("dedupe_key") in (key, _hash(key)):
                    return a["id"], False, a
        ms, _ = _now()
        stamp = datetime.fromtimestamp(ms / 1000, UTC).strftime("%Y%m%dT%H%M%S")
        while True:
            ident = f"a{stamp}-" + "".join(_random.choice("abcdefghijklmnopqrstuvwxyz0123456789") for _ in range(4))
            if ident not in ids:
                break
        event = {
            "event": "added",
            "id": ident,
            "repo": repo,
            "cwd": cwd if not redacted else "",
            "session": _session(fields.get("session"), e),
            "question": question,
            "options": options,
            "free_text_allowed": free_text,
            "decision": decision,
            "source": "permission" if fields.get("source") == "permission" else "session",
            "dedupe_key": stored_key,
            "flows": FLOWS[access],
            "redacted": redacted,
        }
        if redacted:
            event["redacted_why"] = reason
        _write(e, event, held=True)
        return ident, True, event


# --- commands --------------------------------------------------------------------------------------------------------


def _line(text, n: int) -> str:
    t = " ".join(str(text).split())
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def age_text(ts) -> str:
    if not isinstance(ts, (int, float)) or isinstance(ts, bool):
        return "?"
    s = max(0, int(time.time() - ts / 1000))
    return (
        f"{s}s"
        if s < 90
        else f"{round(s / 60)}m"
        if s < 5400
        else f"{round(s / 3600)}h"
        if s < 172800
        else f"{round(s / 86400)}d"
    )


def _parse_option(raw: str) -> dict:
    label, sep, desc = raw.partition("|")
    return {"label": label.strip(), "description": desc.strip() if sep else ""}


def _stdin_fields() -> dict:
    try:
        data = json.loads(sys.stdin.read())
    except ValueError as e:
        raise WsError("usage", "stdin is not valid JSON", "pass one JSON object", 2) from e
    if not isinstance(data, dict):
        raise WsError("usage", "stdin must be one JSON object", "", 2)
    return data


def cmd_add(args) -> int:
    fields = _stdin_fields() if args.stdin else {}
    for name in ("question", "decision", "repo", "dedupe_key"):
        if getattr(args, name) is not None:
            fields[name] = getattr(args, name)
    if args.option:
        fields["options"] = [_parse_option(o) for o in args.option]
    if args.free_text is not None:
        fields["free_text_allowed"] = args.free_text
    sess = fields.get("session")
    sess = {"name": sess} if isinstance(sess, str) else dict(sess) if isinstance(sess, dict) else {}
    for k, v in (("name", args.session), ("tmux", args.tmux)):
        if v:
            sess[k] = v
    fields["session"] = sess
    ident, created, event = add(fields, args.cwd or os.getcwd())
    if args.json:
        output.result({"id": ident, "created": created, "redacted": bool(event.get("redacted"))})
    else:
        print(ident)
    if created and event.get("redacted"):
        print(f"ws: stored redacted ({event.get('redacted_why')})", file=sys.stderr)
    return 0


def cmd_list(args) -> int:
    pol = _policy(_env())
    asks = sorted(load().values(), key=_ts)
    rows = [view(a, pol=pol) for a in asks if args.all or a["status"] == "open"]
    if args.json:
        output.result({"asks": rows})
        return 0
    for x in rows:
        cols = (
            [x["id"]]
            + ([x["status"]] if args.all else [])
            + [age_text(x.get("ts")), str(x.get("repo")), _line(x["question"], 80)]
        )
        print("  ".join(cols))
    return 0


def cmd_get(args) -> int:
    x = view(get(args.id))
    if args.json:
        output.result(x)
        return 0
    print(f"{x['id']}  {x['status']}  {x.get('repo')}  {age_text(x.get('ts'))} old")
    print(f"question: {x['question']}")
    for n, o in enumerate(x.get("options") or [], 1):
        print(
            f"  {n}. {o['label']}"
            + (" (Recommended)" if n == 1 else "")
            + (f" - {o['description']}" if o.get("description") else "")
        )
    if x.get("answer"):
        a = x["answer"]
        how = f"option {a['option_index'] + 1}" if a.get("option_index") is not None else "free text"
        print(f"answer: {a.get('text') if a.get('text') is not None else '(hidden)'}  ({how}, via {a.get('via')})")
    return 0


def register(sub) -> None:
    p = sub.add_parser("ask", help="leave a question for a person and read back the answer (one queue, every session)")
    s = p.add_subparsers(dest="ask_command", required=True)

    a = s.add_parser("add", help="add an ask and print its id; the first --option is the recommendation")
    a.add_argument("--question", metavar="TEXT")
    a.add_argument("--option", action="append", metavar="LABEL|DESCRIPTION")
    a.add_argument("--decision", metavar="TEXT", help="what the answer decides")
    a.add_argument("--free-text", dest="free_text", action=argparse.BooleanOptionalAction, default=None)
    a.add_argument("--repo", metavar="NAME", help="default: the working folder's name")
    a.add_argument("--cwd", metavar="DIR", help="the folder the ask comes from (default: this one)")
    a.add_argument("--session", metavar="NAME", help="who asks; default $WS_SESSION_NAME")
    a.add_argument("--tmux", metavar="TARGET")
    a.add_argument(
        "--dedupe-key", dest="dedupe_key", metavar="KEY", help="an open ask with this key is returned, not added"
    )
    a.add_argument("--stdin", action="store_true", help="read the ask from standard input as one JSON object")
    a.set_defaults(func=cmd_add)

    ls = s.add_parser("list", help="open asks, oldest first")
    ls.add_argument("--all", action="store_true", help="every status")
    ls.set_defaults(func=cmd_list)

    g = s.add_parser("get", help="one ask: status and answer")
    g.add_argument("id")
    g.set_defaults(func=cmd_get)
