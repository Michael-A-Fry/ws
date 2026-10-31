"""One output contract for every `ws` command: a typed error, one JSON envelope, and the global `--json` / `-C`.

Shapes (schema `ws.cli/1`, the same for every command; a change to either bumps the number):

    result  {"schema", "kind", "status", "data"}
    error   {"schema", "kind", "status": "error", "error_kind", "message", "hint", "exit_code"}

`kind` is the command (`doctor`, `gate.generate`); `status` is `ok`, `attention` (a valid result, exit code 1: doctor
found something to do) or `error`. `error_kind` is raised at the source as a `WsError` kind and never worked out from
the message text. Kinds in use: usage, invalid_cwd, no_profile, bad_config, bad_input, not_found, refused, denied,
unavailable, io, unsupported, failed.

`--json` (or `WS_OUTPUT=json`) is global: it works before or after the command, on every command. A command with
structured data calls `result(data)`; one that only prints prose gets its text wrapped as `{"output": ...}`; one that
speaks a protocol on stdout (the MCP server, the gate hook) sets `raw` and keeps its own stdout, but its errors are still
the envelope. Text mode prints `ws: <message>` then `hint: <hint>` to stderr. The shape follows claw-code's error
envelope (MIT; https://github.com/ultraworkers/claw-code, rusty-claude-cli main.rs), except that the kind is typed.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

SCHEMA = "ws.cli/1"
OK, ATTENTION, ERROR = "ok", "attention", "error"


class WsError(Exception):
    """A failure the person can act on. `str(e)` is the message; `hint` says what to try next."""

    def __init__(self, kind: str, message: str, hint: str = "", exit_code: int = 1, details: dict | None = None):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.details = details or {}


def did_you_mean(word: str, choices) -> str:
    """A "did you mean" hint for the closest of `choices`, or an empty string."""
    close = difflib.get_close_matches(word, [str(c) for c in choices], n=1, cutoff=0.6)
    return f"did you mean `{close[0]}`?" if close else ""


def error_envelope(e: WsError, kind: str = "ws") -> dict:
    env = {
        "schema": SCHEMA,
        "kind": kind,
        "status": ERROR,
        "error_kind": e.kind,
        "message": e.message,
        "hint": e.hint,
        "exit_code": e.exit_code,
    }
    if e.details:
        env["details"] = e.details
    return env


def error_text(e: WsError) -> str:
    text = f"ws: {e.message}"
    if e.hint:
        text += f"\nhint: {e.hint}"
    return text


# ---- the global flags ---------------------------------------------------------------------------------------------------


@dataclass
class Globals:
    json: bool = False
    cwd: str | None = None


def wants_json(argv: list[str], env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    before_dashes = argv[: argv.index("--")] if "--" in argv else argv
    return env.get("WS_OUTPUT", "").strip().lower() == "json" or "--json" in before_dashes


def split_global(argv: list[str], env: dict | None = None) -> tuple[Globals, list[str]]:
    """Take `--json` and `-C PATH` out of argv wherever they are (not after a bare `--`), so they work on any command."""
    env = os.environ if env is None else env
    g = Globals(json=wants_json([], env))
    rest: list[str] = []
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "--":
            rest.extend(argv[i:])
            break
        if a == "--json":
            g.json = True
        elif a in ("-C", "--cwd"):
            if i + 1 >= len(argv):
                raise WsError("usage", f"{a} needs a folder", "e.g. `ws -C ~/code/app doctor`", 2)
            g.cwd = argv[i + 1]
            i += 1
        elif a.startswith("--cwd="):
            g.cwd = a[len("--cwd=") :]
        elif a.startswith("-C") and len(a) > 2:
            g.cwd = a[2:]
        else:
            rest.append(a)
        i += 1
    return g, rest


def enter_cwd(path: str) -> None:
    """Change into `path` before anything runs; a bad one is `invalid_cwd`, with the reason."""
    p = Path(path).expanduser()
    if not p.exists():
        reason = "does not exist"
    elif not p.is_dir():
        reason = "is not a folder"
    elif not os.access(p, os.X_OK):
        reason = "is not readable"
    else:
        os.chdir(p)
        return
    raise WsError(
        "invalid_cwd",
        f"-C {path}: {reason}",
        "give a folder that exists, e.g. `ws -C ~/code doctor`",
        2,
        {"path": str(path), "reason": reason},
    )


# ---- results ------------------------------------------------------------------------------------------------------------

_sink: io.TextIOBase | None = None  # the real stdout while dispatch has captured the command's own prints
_kind = "ws"
_emitted = False


def result(data, *, status: str = OK, kind: str | None = None) -> None:
    """Print the result envelope. A command with `--json` calls this instead of printing its own JSON."""
    global _emitted
    _emitted = True
    env = {"schema": SCHEMA, "kind": kind or _kind, "status": status, "data": data}
    print(json.dumps(env, indent=2, default=str), file=_sink or sys.stdout)


def _raw(args) -> bool:
    raw = getattr(args, "raw", False)
    return bool(raw(args)) if callable(raw) else bool(raw)


def dispatch(args, kind: str, want_json: bool) -> int:
    """Run `args.func(args)`. In JSON mode the command's prints are captured so that stdout is exactly one envelope."""
    global _sink, _kind, _emitted
    args.json = want_json or bool(getattr(args, "json", False))
    if not args.json or _raw(args):
        return args.func(args)
    _kind, _emitted = kind, False
    real = sys.stdout
    out, err = io.StringIO(), io.StringIO()
    _sink = real
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = args.func(args)
    except WsError:
        sys.stderr.write(err.getvalue())  # warnings the command printed before it failed
        raise
    finally:
        _sink = None
    if err.getvalue():
        if rc and not _emitted:
            msg = err.getvalue().strip()
            first, _, rest = msg.partition("\n")
            first = first.removeprefix("ws: ")
            env = error_envelope(WsError("failed", first, rest.strip(), rc), kind)
            print(json.dumps(env, indent=2), file=real)
            return rc
        sys.stderr.write(err.getvalue())
    if not _emitted:
        result({"output": out.getvalue().rstrip("\n")}, status=OK if not rc else ATTENTION, kind=kind)
    return rc
