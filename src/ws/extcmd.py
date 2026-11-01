"""The `ws` name: your own `ws-<word>` programs, and one fallback program for every other word.

Precedence, first match wins: a built-in command; `ws-<word>` on PATH (a word is `[a-z][a-z0-9-]{0,40}`); the
profile's `[cli] fallback`; the usual usage error with "did you mean". An external gets the arguments after its word,
`WS_OUTPUT=json` when `--json` was given, and `-C PATH` as its working folder (the way git runs `git-<word>`). The
fallback gets the whole original argument list, leading flags and the empty list (a bare `ws`) included, and runs with
`WS_FALLBACK=1`; a run that already has it set is not handed on again. The fallback is an absolute path (or `~/...`) to a
file that is yours, executable and closed to other writers, the same test a job file passes. PATH entries that are not
absolute are skipped, so a program in the current folder is never picked up by name. `ws config fallback` reads, sets
and clears it.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Collection, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from ws import config_cmd, jobs, output
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path, write_if_changed

WORD = re.compile(r"^[a-z][a-z0-9-]{0,40}$")
PREFIX = "ws-"
FALLBACK_ENV = "WS_FALLBACK"
_FALLBACK_HINT = "set one with `ws config fallback PATH`, or remove it with `ws config fallback --clear`"


@dataclass
class Launch:
    """A program to start in place of ws: its path, the arguments after its name, its environment and its folder."""

    path: str
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)
    keep_cwd: bool = True  # an external runs in the `-C` folder; the fallback is told `-C` itself


def _is_exec(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _path_dirs(env: Mapping[str, str]) -> list[Path]:
    return [Path(d) for d in env.get("PATH", "").split(os.pathsep) if d and os.path.isabs(d)]


def find_external(word: str, env: Mapping[str, str] | None = None) -> str | None:
    """The path of `ws-<word>` in the first absolute PATH entry that holds an executable of that name."""
    env = os.environ if env is None else env
    if not WORD.match(word):
        return None
    for d in _path_dirs(env):
        candidate = d / f"{PREFIX}{word}"
        if _is_exec(candidate):
            return str(candidate)
    return None


def list_externals(builtins: Collection[str], env: Mapping[str, str] | None = None) -> list[str]:
    """Every `ws-<word>` on PATH that `ws <word>` would run, as `ws-<word>`, sorted (a built-in's name is taken)."""
    env = os.environ if env is None else env
    found: set[str] = set()
    for d in _path_dirs(env):
        try:
            names = [p.name for p in d.iterdir() if p.name.startswith(PREFIX)]
        except OSError:
            continue
        for name in names:
            word = name[len(PREFIX) :]
            if word not in builtins and WORD.match(word) and _is_exec(d / name):
                found.add(name)
    return sorted(found)


def configured_fallback(path: Path | None = None) -> str | None:
    """`[cli] fallback` from the profile, or None when it is not set. A profile that cannot be read is an error."""
    path = path or profile_path()
    try:
        prof = load(path) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {path}") from e
    table = prof.get("cli")
    if table is None:
        return None
    if not isinstance(table, dict):
        raise WsError("bad_config", f"{path}: [cli] must be a table", f"fix {path}")
    value = table.get("fallback")
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise WsError("bad_config", f"{path}: [cli] fallback must be a path", _FALLBACK_HINT)
    return value


def resolve_fallback(value: str) -> Path:
    """The fallback as a path ws will start, or a typed error saying which rule it breaks."""
    p = Path(value).expanduser()
    if not p.is_absolute():
        raise WsError("bad_config", f"fallback {value!r} must be an absolute path or start with ~/", _FALLBACK_HINT)
    if not p.is_file():
        raise WsError("not_found", f"the fallback program {p} is missing", _FALLBACK_HINT)
    if not os.access(p, os.X_OK):
        raise WsError("bad_config", f"the fallback program {p} is not executable", f"run `chmod u+x {p}`")
    try:
        jobs.check_private(p)
    except jobs.JobError as e:
        raise WsError("refused", str(e), _FALLBACK_HINT) from e
    return p


def plan(
    raw: list[str],
    g: output.Globals,
    rest: list[str],
    builtins: Collection[str],
    flags: Collection[str],
    env: Mapping[str, str] | None = None,
) -> Launch | None:
    """What to start instead of a built-in command, or None when ws runs the command itself (or reports the mistake)."""
    env = os.environ if env is None else env
    first = rest[0] if rest else None
    if first is not None and not first.startswith("-"):
        if first in builtins:
            return None
        found = find_external(first, env)
        if found:
            child = dict(env)
            if g.json:
                child["WS_OUTPUT"] = "json"
            return Launch(found, rest[1:], child)
    elif first is not None and first.split("=")[0] in flags:
        return None
    value = configured_fallback()
    if value is None:
        return None
    what = f"`{first}`" if first is not None else "ws with no command"
    if env.get(FALLBACK_ENV):
        hint = output.did_you_mean(first or "", builtins) if first and not first.startswith("-") else ""
        raise WsError(
            "usage",
            f"{what} is not a ws command, and the fallback program started this run",
            hint or "run `ws help`",
            2,
        )
    path = resolve_fallback(value)
    return Launch(str(path), list(raw), {**env, FALLBACK_ENV: "1"}, keep_cwd=False)


def launch(spec: Launch) -> int:
    """Start the program in place of ws (exec on POSIX); where that is not available, run it and return its code."""
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        if os.name == "posix":
            os.execve(spec.path, [spec.path, *spec.argv], spec.env)
        return subprocess.run([spec.path, *spec.argv], env=spec.env, check=False).returncode
    except OSError as e:
        raise WsError(
            "io",
            f"cannot start {spec.path}: {e.strerror or type(e).__name__}",
            "the file needs to be executable and start with a `#!` line",
        ) from e


def help_lines(builtins: Collection[str], env: Mapping[str, str] | None = None) -> list[str]:
    """The lines `ws help` adds after the built-ins: the externals on PATH, and where other words go."""
    found = list_externals(builtins, env)
    lines = [f"Also on PATH: {', '.join(found)}"] if found else []
    target = _fallback_for_help()
    if target:
        lines.append(f"Other words go to {target}")
    return lines


def _fallback_for_help() -> str | None:
    try:
        return configured_fallback()
    except WsError:
        return None  # help never fails over a profile; `ws doctor` and `ws config` say what is wrong


def cmd_config(args: argparse.Namespace) -> int:
    """`ws config`, plus `ws config fallback [PATH | --clear]`."""
    if args.topic is None:
        if args.value is not None or args.clear:
            raise WsError("usage", "PATH and --clear go after `fallback`", "e.g. `ws config fallback ~/bin/ws-old`", 2)
        return config_cmd.cmd_config(args)
    if args.clear and args.value is not None:
        raise WsError("usage", "give a PATH or --clear, not both", "e.g. `ws config fallback --clear`", 2)
    path = profile_path()
    try:
        prof = load(path) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {path}") from e
    table = prof.get("cli")
    if table is not None and not isinstance(table, dict):
        raise WsError("bad_config", f"{path}: [cli] must be a table", f"fix {path}")
    current = configured_fallback(path)
    changed = False
    if args.clear:
        if current is not None:
            table = dict(table)
            table.pop("fallback", None)
            if table:
                prof["cli"] = table
            else:
                prof.pop("cli", None)
            changed = True
        current = None
    elif args.value is not None:
        wanted = args.value if args.value.startswith("~") else os.path.abspath(args.value)
        resolve_fallback(wanted)
        prof["cli"] = {**(table or {}), "fallback": wanted}
        changed = wanted != current
        current = wanted
    if changed:
        try:
            write_if_changed(path, prof)
        except ProfileError as e:
            raise WsError("io", str(e), f"check that {path.parent} is writable") from e
    if args.json:
        output.result({"path": str(path), "fallback": current, "changed": changed})
    elif changed:
        print(f"ws: fallback is {current}" if current else "ws: fallback cleared")
    else:
        print(f"fallback: {current}" if current else "fallback: none")
    return 0


def register(sub) -> None:
    """Adds `fallback` to `ws config`: the command takes an optional topic, so `ws config` alone is unchanged."""
    c = sub.choices["config"]
    c.add_argument("topic", nargs="?", choices=["fallback"], help="`fallback`: the program that gets words ws lacks")
    c.add_argument("value", nargs="?", metavar="PATH", help="with `fallback`: the program (absolute, or ~/...)")
    c.add_argument("--clear", action="store_true", help="with `fallback`: remove it")
    c.set_defaults(func=cmd_config)
