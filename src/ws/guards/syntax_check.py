"""syntax-check: after a session writes a file, check that it parses and tell the model the line that does not. Python
(`compile`), JSON and TOML are read in process; `.sh` and `.bash` go through `sh -n` or `bash -n` (a note when the
machine has no such shell). `extra` maps any other extension to a checker, with the file path added as its last
argument and 10 seconds to answer: `extra = { yaml = ["yamllint", "-d", "relaxed"] }`.

It runs after the write, so a finding is a message to the model: the runner shows it the text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field

from ws.guards import Finding, known_keys
from ws.policy import PolicyError

NAME = "syntax-check"
EVENTS = ("post",)
KINDS = ("write",)

TIMEOUT_S = 10
_EXT = re.compile(r"^[a-z0-9]+$")
_MAX_FILE = 4 * 1024 * 1024
_MAX_REPORT = 600


@dataclass(frozen=True)
class Config:
    extra: dict[str, tuple[str, ...]] = field(default_factory=dict)  # extension -> checker argv


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("extra",))
    raw = table.get("extra", {})
    if not isinstance(raw, dict):
        raise PolicyError(f"{where}: extra must be a table of extension = checker command")
    extra = {}
    for ext, argv in raw.items():
        if not _EXT.match(ext):
            raise PolicyError(f"{where}: extra: {ext!r} is not an extension (lower-case letters and digits, no dot)")
        if not isinstance(argv, list) or not argv or not all(isinstance(a, str) and a for a in argv):
            raise PolicyError(f"{where}: extra.{ext} must be a list of text, the checker and its arguments")
        extra[ext] = tuple(argv)
    return Config(extra)


def _brief(text: str) -> str:
    lines = [ln.rstrip() for ln in text.strip().splitlines() if ln.strip()]
    return "\n".join(lines[:6])[:_MAX_REPORT]


def _python(path: str, data: bytes) -> str | None:
    try:
        compile(data, path, "exec", dont_inherit=True)
    except SyntaxError as e:
        where = f"line {e.lineno}" + (f", column {e.offset}" if e.offset else "") if e.lineno else "unknown line"
        return f"{path}: Python syntax error at {where}: {e.msg}"
    except ValueError as e:  # a null byte
        return f"{path}: Python cannot read this file: {e}"
    return None


def _json(path: str, data: bytes) -> str | None:
    try:
        json.loads(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as e:  # JSONDecodeError is a ValueError and says "line N column M"
        return f"{path}: not valid JSON: {e}"
    return None


def _toml(path: str, data: bytes) -> str | None:
    try:
        tomllib.loads(data.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return f"{path}: not valid TOML: {e}"
    return None


def _run(argv: list[str], path: str) -> tuple[str | None, str | None]:
    """(problem, note) from running a checker on the file: a problem when it exits non-zero, a note when it cannot run."""
    exe = shutil.which(argv[0])
    if exe is None:
        return None, f"syntax-check skipped {path}: {argv[0]} is not installed"
    try:
        done = subprocess.run(
            [exe, *argv[1:], path],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return None, f"syntax-check skipped {path}: {argv[0]} took more than {TIMEOUT_S} seconds"
    except OSError as e:
        return None, f"syntax-check skipped {path}: {argv[0]} could not run ({type(e).__name__})"
    if done.returncode == 0:
        return None, None
    return f"{path}: {argv[0]} found a problem:\n{_brief(done.stdout + done.stderr)}", None


def judge(call, cfg: Config, ctx) -> Finding | None:
    path = os.path.abspath(os.path.join(ctx.cwd or "", call.value))
    ext = path.rsplit(".", 1)[-1].lower() if "." in os.path.basename(path) else ""
    if not os.path.isfile(path):
        return None
    if ext in cfg.extra:
        problem, note = _run(list(cfg.extra[ext]), path)
    elif ext in ("py", "json", "toml"):
        if os.path.getsize(path) > _MAX_FILE:
            return None
        with open(path, "rb") as f:
            data = f.read()
        problem, note = {"py": _python, "json": _json, "toml": _toml}[ext](path, data), None
    elif ext in ("sh", "bash"):
        shell = "bash" if ext == "bash" else "sh"
        if shutil.which(shell) is None and shutil.which("bash") is not None:
            shell = "bash"
        problem, note = _run([shell, "-n"], path)
    else:
        return None
    if problem:
        return Finding("deny", problem)
    return Finding("note", note) if note else None
