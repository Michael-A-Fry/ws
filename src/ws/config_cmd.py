"""`ws config`: every file ws reads, where its path came from, and the keys it sets.

Four kinds: the profile, the policy, the secrets registry and one file per job. Each is reported as `ok`, `missing`
(ws uses its defaults) or `invalid` (with the error). Nothing here loads a bad file for use: the policy and the secrets
registry fail closed everywhere else, and this only reads them to say what is wrong. A bad job is listed and the other
jobs are untouched. Values are never shown, only key names. A key in a file's DEPRECATED table (profile.py, policy.py)
is reported with its replacement. Exit 1 when a file is invalid.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path

from ws import jobs, output, policy, profile, secrets


def _origin(env: dict) -> str:
    return "$WS_HOME" if env.get("WS_HOME") else "default (~/.ws)"


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
    home = profile.ws_home(env)
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
    return {"home": str(home), "origin": origin, "files": files, "warnings": warnings}


def _raise_if(reason: str | None):
    def check() -> None:
        if reason:
            raise ValueError(reason)

    return check


def render(data: dict) -> str:
    out = [f"ws home: {data['home']} ({data['origin']})"]
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


def register(sub) -> None:
    c = sub.add_parser("config", help="every file ws reads: where it is, where its path came from, the keys it sets")
    c.set_defaults(func=cmd_config)
