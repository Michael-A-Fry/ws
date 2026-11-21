"""`ws config`: every file ws reads, where its path came from, and the keys it sets.

Four kinds: the profile, the policy, the secrets registry and one file per job. Each is reported as `ok`, `missing`
(ws uses its defaults) or `invalid` (with the error). Nothing here loads a bad file for use: the policy and the secrets
registry fail closed everywhere else, and this only reads them to say what is wrong. A bad job is listed and the other
jobs are untouched. Values are never shown, only key names. A key in a file's DEPRECATED table (profile.py, policy.py)
is reported with its replacement. Exit 1 when a file is invalid.

The setters change one profile key each, and nothing else ws reads:

    ws config model                 each job, its purpose, its model and where the model came from
    ws config model ID              [models].default: the model of a job that names none and has no purpose entry
    ws config model PURPOSE ID      [models].PURPOSE: the model of a job with that `purpose` that names none
    ws config budget USD            [roles.budget] monthly_cap_usd (a positive number)

A setter prints the one-line change and asks unless `--yes`; `--dry-run` prints it and writes nothing. Before a write
the profile is copied to `profile.toml.bak-<UTC stamp>`, because the rewrite drops its comments. Setting the value the
key already has writes nothing. The `fallback` topic is extcmd's; this module only parses it.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tomllib
from pathlib import Path

from ws import jobs, output, paths, policy, profile, secrets
from ws.output import WsError

TOPICS = ("fallback", "model", "budget")


def _origin(env: dict) -> str:
    return paths.describe(env)


def _file(name: str, path: Path, origin: str, table: dict[str, str], check) -> dict:
    """One entry. `check(doc)` raises ValueError-like with the reason when the content is not acceptable."""
    entry = {"name": name, "path": str(path), "origin": origin, "status": "missing", "keys": [], "deprecated": []}
    if not path.is_file():
        return entry
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        entry.update(status="invalid", error=f"{path}: {e}")
        return entry
    entry["keys"] = profile.flat_keys(doc)
    entry["deprecated"] = [{"key": k, "replacement": r} for k, r in profile.deprecated_in(doc, table)]
    try:
        check()
    except Exception as e:  # noqa: BLE001 (each loader has its own error type; all of them mean "invalid")
        entry.update(status="invalid", error=str(e))
    else:
        entry["status"] = "ok"
    return entry


def collect(env: dict | None = None) -> dict:
    env = dict(os.environ) if env is None else env
    lay = paths.layout(env)
    origin = _origin(env)
    files = [
        _file(
            "profile",
            profile.profile_path(env),
            origin,
            profile.DEPRECATED,
            lambda: profile.load(profile.profile_path(env)),
        ),
        _file(
            "policy", policy.policy_path(env), origin, policy.DEPRECATED, lambda: policy.load(policy.policy_path(env))
        ),
        _file(
            "secrets",
            secrets.registry_path(env),
            origin,
            {},
            lambda: secrets.load_registry(secrets.registry_path(env)),
        ),
    ]
    folder = jobs.jobs_dir(env)
    bad = jobs.problems(env)
    for path in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        if jobs.NAME.match(path.stem):
            files.append(
                _file(f"job:{path.stem}", path, origin, {}, _raise_if(bad.get(path.stem) or bad.get("(folder)")))
            )
    warnings = [
        f"{f['name']}: `{d['key']}` is deprecated; use `{d['replacement']}`" for f in files for d in f["deprecated"]
    ]
    warnings += [
        f"job:{n}: still runs, but lacks the run contract: {why}" for n, why in jobs.contract_gaps(env).items()
    ]
    folders = {k: str(lay.dir(k)) for k in paths.KINDS}
    return {"folders": folders, "origin": origin, "files": files, "warnings": warnings}


def _raise_if(reason: str | None):
    def check() -> None:
        if reason:
            raise ValueError(reason)

    return check


def render(data: dict) -> str:
    shown = data["folders"]
    if len(set(shown.values())) == 1:
        out = [f"ws folder: {shown['config']} ({data['origin']})"]
    else:
        out = [f"ws folders ({data['origin']}):"] + [f"  {k:<7} {v}" for k, v in shown.items()]
    for f in data["files"]:
        keys = f["keys"]
        shown = ", ".join(keys[:8]) + (f", +{len(keys) - 8} more" if len(keys) > 8 else "")
        out.append(f"{f['status']:<8} {f['name']:<16} {f['path']}")
        if f["status"] == "invalid":
            out.append(f"         error: {f['error']}")
        elif keys:
            out.append(f"         sets: {shown}")
    out += [f"warn: {w}" for w in data["warnings"]]
    return "\n".join(out)


def cmd_config(args: argparse.Namespace) -> int:
    data = collect()
    bad = any(f["status"] == "invalid" for f in data["files"])
    if args.json:
        output.result(data, status=output.ATTENTION if bad else output.OK)
    else:
        print(render(data))
    return 1 if bad else 0


# ---- the setters ---------------------------------------------------------------------------------------------------


def _usage(message: str, hint: str) -> WsError:
    return WsError("usage", message, hint, 2)


def _read_profile(path: Path) -> dict:
    try:
        return profile.load(path) or {}
    except profile.ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {path}") from e


def _table(doc: dict, dotted: str, path: Path) -> dict:
    """The table at `dotted` (made if missing). A key there that is not a table is a bad profile, never overwritten."""
    here = doc
    for part in dotted.split("."):
        nxt = here.setdefault(part, {})
        if not isinstance(nxt, dict):
            raise WsError("bad_config", f"{path}: [{dotted}] must be a table", f"fix {path}")
        here = nxt
    return here


def _shown(v) -> str:
    if v is None:
        return "(unset)"
    return f'"{v}"' if isinstance(v, str) else repr(v)


def _confirm(args: argparse.Namespace) -> bool:
    if args.yes:
        return True
    if not sys.stdin.isatty():
        return False
    try:
        return input("write it? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def set_key(args: argparse.Namespace, dotted: str, key: str, value) -> int:
    """Set `[dotted] key = value` in the profile: show the change, ask, back up, write. Exit 0 written or unchanged,
    1 declined (or no terminal and no --yes)."""
    path = profile.profile_path()
    doc = _read_profile(path)
    table = _table(doc, dotted, path)
    old = table.get(key)
    line = f"[{dotted}] {key}: {_shown(old)} -> {_shown(value)}"
    data = {"path": str(path), "key": f"{dotted}.{key}", "old": old, "new": value, "changed": False, "backup": None}
    if old == value and type(old) is type(value):
        return _say(args, data, f"{line} (no change)")
    if args.dry_run:
        return _say(args, data | {"dry_run": True}, f"{line} (dry run: nothing written)")
    if not args.json:
        print(line)
    if not _confirm(args):
        if args.json:
            output.result(data | {"declined": True}, status=output.ATTENTION)
        else:
            print("ws: not written (pass --yes to write without asking)")
        return 1
    table[key] = value
    try:
        saved = profile.backup(path)
        profile.write_if_changed(path, doc)
    except (OSError, profile.ProfileError) as e:
        raise WsError("io", str(e), f"check that {path.parent} is writable") from e
    data.update(changed=True, backup=str(saved) if saved else None)
    if args.json:
        output.result(data)
    else:
        print(f"ws: wrote {path}" + (f"; the old file is {saved} (the rewrite drops comments)" if saved else ""))
        print(f"undo: {_undo(dotted, key, old)}")
    return 0


def _undo(dotted: str, key: str, old) -> str:
    if old is None:
        return f"remove `{key}` from [{dotted}] in the profile"
    if dotted == "models":
        return f"ws config model {old}" if key == "default" else f"ws config model {key} {old}"
    return f"ws config budget {old}"


def _say(args: argparse.Namespace, data: dict, text: str) -> int:
    if args.json:
        output.result(data)
    else:
        print(text)
    return 0


def model_rows(env: dict | None = None) -> list[dict]:
    """One row per job file that loads: name, purpose, model, and where the model came from (job, purpose, default,
    none). The profile's [models] table is read once; a bad one raises WsError."""
    env = dict(os.environ) if env is None else env
    path = profile.profile_path(env)
    prof = _read_profile(path)
    folder = jobs.jobs_dir(env)
    bad = jobs.problems(env)
    rows = []
    for f in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
        if not jobs.NAME.match(f.stem) or f.stem in bad or "(folder)" in bad:
            continue
        try:
            job = jobs.loads(f.stem, f.read_text(encoding="utf-8"), folder, str(f))
        except (jobs.JobError, OSError, UnicodeDecodeError):
            continue  # a schedule-only job (a ws command) has no model
        try:
            model, source = jobs.resolve_model(job, prof)
        except jobs.JobError as e:
            raise WsError("bad_config", f"{path}: {e}", f"fix [models] in {path}") from e
        rows.append({"job": job.name, "purpose": job.purpose, "model": model, "from": source})
    return rows


def cmd_model(args: argparse.Namespace) -> int:
    words = args.words
    if len(words) > 2:
        raise _usage("ws config model takes at most a purpose and a model id", "e.g. `ws config model jobs ID`")
    if not words:
        if args.dry_run or args.yes:
            raise _usage("--yes and --dry-run go with a model id", "e.g. `ws config model ID --dry-run`")
        prof = _read_profile(profile.profile_path())
        try:
            table = jobs.models_table(prof)
        except jobs.JobError as e:
            raise WsError("bad_config", str(e), "fix [models] in the profile") from e
        rows = model_rows()
        if args.json:
            output.result({"models": table, "jobs": rows})
            return 0
        print(f"default: {table.get('default', '(unset)')}")
        for k, v in sorted(table.items()):
            if k != "default":
                print(f"purpose {k}: {v}")
        if not rows:
            print("no jobs")
        for r in rows:
            print(f"{r['job']:<20} {r['purpose'] or '-':<12} {r['model'] or '(none)':<32} {r['from']}")
        return 0
    *purpose, model_id = words
    key = purpose[0] if purpose else "default"
    if not jobs.NAME.match(key):
        raise _usage(
            f"purpose {key!r} is not a plain name (letters, digits, - and _)", "e.g. `ws config model jobs ID`"
        )
    if not model_id.strip():
        raise _usage("a model id is a non-empty string", "e.g. `ws config model claude-haiku-4-5`")
    return set_key(args, "models", key, model_id)


def parse_usd(text: str) -> int | float:
    """A positive, finite US$ amount; a whole number is kept as an int. Anything else is a usage error."""
    try:
        v = float(text)
    except ValueError:
        v = math.nan
    if not (math.isfinite(v) and v > 0):
        raise _usage(f"budget must be a positive number of US dollars, not {text!r}", "e.g. `ws config budget 30`")
    return int(v) if v.is_integer() else v


def cmd_budget(args: argparse.Namespace) -> int:
    if len(args.words) != 1:
        raise _usage("ws config budget takes one amount in US$", "e.g. `ws config budget 30`")
    return set_key(args, "roles.budget", "monthly_cap_usd", parse_usd(args.words[0]))


def cmd_topic(args: argparse.Namespace) -> int:
    """`ws config [TOPIC ...]`: no topic is the status view; `fallback` goes to extcmd; `model` and `budget` here."""
    setter_flags = args.yes or args.dry_run
    if args.topic in (None, "fallback"):
        if setter_flags:
            raise _usage("--yes and --dry-run go with `model` and `budget`", "e.g. `ws config budget 30 --dry-run`")
        from ws import extcmd  # here, not at the top: extcmd imports this module

        if len(args.words) > 1:
            raise _usage("give one PATH", "e.g. a single program path after `fallback`")
        args.value = args.words[0] if args.words else None
        return extcmd.cmd_config(args)
    if args.clear:
        raise _usage("--clear goes with `fallback`", "--clear removes the fallback program")
    return cmd_model(args) if args.topic == "model" else cmd_budget(args)


def register(sub) -> None:
    c = sub.add_parser("config", help="every file ws reads: where it is, where its path came from, the keys it sets")
    c.add_argument(
        "topic",
        nargs="?",
        choices=TOPICS,
        help="`fallback`: the program that gets words ws lacks; `model`: which model each job runs on; "
        "`budget`: the monthly spend cap",
    )
    c.add_argument("words", nargs="*", metavar="VALUE", help="fallback PATH | model [[PURPOSE] ID] | budget USD")
    c.add_argument("--clear", action="store_true", help="with `fallback`: remove it")
    c.add_argument("--yes", action="store_true", help="with `model` or `budget`: write without asking")
    c.add_argument("--dry-run", action="store_true", help="with `model` or `budget`: show the change, write nothing")
    c.set_defaults(func=cmd_topic)
