"""The output contract for every `ws` command: one typed error, one JSON envelope, and the global flags.

A command signals failure by raising `WsError(kind, message, hint, exit_code)`; the kind is chosen where it is raised
and is never worked out from message text. With `--json` (or `WS_OUTPUT=json`) stdout carries exactly one JSON
document in the envelope shape `ws.cli/1`, whatever the command prints along the way. `-C PATH` / `--cwd PATH` run any
command from another folder. `--no-color` turns colour off for any command (a non-empty `NO_COLOR` does the same, see
`style.py`). Commands that speak a protocol, are a hook or ask a person a question are "raw": they keep the terminal
and report errors as text on stderr. Changing either envelope shape means bumping `SCHEMA`.
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from ws.style import Style

SCHEMA = "ws.cli/1"
OK = "ok"
ATTENTION = "attention"  # a valid result, but with exit code 1: something is left to do
ERROR = "error"

_CWD_HINT = "give a folder that exists, e.g. `ws -C ~/code doctor`"

# Per-dispatch state, reset around every JSON-mode call: the command being run, whether it has printed its envelope,
# and the stream the envelope goes to (the real stdout, while the command's own prints are being captured).
_kind = "ws"
_emitted = False
_sink: Any = None


class WsError(Exception):
    """A failure the person can act on. `kind` says what sort; `hint` says what to try; `exit_code` is the exit status."""

    def __init__(
        self, kind: str, message: str, hint: str = "", exit_code: int = 1, details: dict | None = None
    ) -> None:
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.hint = hint
        self.exit_code = exit_code
        self.details = dict(details) if details else {}


def did_you_mean(word: str, choices: Sequence[Any]) -> str:
    """A "did you mean `x`?" sentence for the closest choice, or an empty string when nothing is close."""
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


def error_text(e: WsError, style: Style | None = None) -> str:
    """The error as the person reads it: `ws: message`, then `hint: ...`. Colour only reinforces (red, then quiet)."""
    st = style or Style()
    return f"ws: {st.error(e.message)}" + (f"\nhint: {st.muted(e.hint)}" if e.hint else "")


@dataclass
class Globals:
    json: bool = False
    cwd: str | None = None
    no_color: bool = False


def wants_json(argv: Sequence[str], env: Any = None) -> bool:
    """True if `WS_OUTPUT=json` or `--json` comes before the first bare `--` (after it, tokens are the command's own)."""
    env = os.environ if env is None else env
    if str(env.get("WS_OUTPUT", "")).strip().lower() == "json":
        return True
    for tok in argv:
        if tok == "--":
            return False
        if tok == "--json":
            return True
    return False


def split_global(argv: Sequence[str], env: Any = None) -> tuple[Globals, list[str]]:
    """Take `--json`, `--no-color` and `-C PATH` out of `argv` wherever they sit, up to the first bare `--`. Returns them
    and the rest."""
    g = Globals(json=wants_json([], env))
    rest: list[str] = []
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok == "--":
            rest.extend(argv[i:])
            break
        if tok == "--json":
            g.json = True
        elif tok == "--no-color":
            g.no_color = True
        elif tok in ("-C", "--cwd"):
            if i + 1 >= len(argv):
                raise WsError("usage", f"{tok} needs a folder", "e.g. `ws -C ~/code/app doctor`", 2)
            g.cwd = argv[i + 1]
            i += 1
        elif tok.startswith("--cwd="):
            g.cwd = tok[len("--cwd=") :]
        elif tok.startswith("-C") and len(tok) > 2:
            g.cwd = tok[2:]
        else:
            rest.append(tok)
        i += 1
    return g, rest


def enter_cwd(path: str) -> None:
    """Change into `path` (`~` expanded), or raise an `invalid_cwd` error saying why not."""
    target = os.path.expanduser(path)
    if not os.path.exists(target):
        reason = "does not exist"
    elif not os.path.isdir(target):
        reason = "is not a folder"
    elif not os.access(target, os.X_OK):
        reason = "is not readable"
    else:
        try:
            os.chdir(target)
        except OSError:
            reason = "is not readable"
        else:
            return
    raise WsError("invalid_cwd", f"-C {path}: {reason}", _CWD_HINT, 2, {"path": str(path), "reason": reason})


def result(data: Any, *, status: str = OK, kind: str | None = None) -> None:
    """Print the result envelope to the real stdout and note that one has been sent."""
    global _emitted
    env = {"schema": SCHEMA, "kind": kind or _kind, "status": status, "data": data}
    out = _sink if _sink is not None else sys.stdout
    print(json.dumps(env, indent=2, default=str), file=out)
    out.flush()
    _emitted = True


def _raw(args: Any) -> bool:
    """Does this command keep the real terminal under `--json`? Its `raw` is a flag, or a function of the arguments."""
    flag = getattr(args, "raw", False)
    return bool(flag(args)) if callable(flag) else bool(flag)


def _no_color(args: Any) -> bool:
    return bool(getattr(args, "no_color", False))


def dispatch(args: Any, kind: str, want_json: bool) -> int:
    """Run `args.func(args)` and return its exit code, making stdout one envelope when JSON is wanted."""
    global _kind, _emitted, _sink
    args.json = want_json or bool(getattr(args, "json", False))
    if not args.json:
        return args.func(args)
    if _raw(args):
        try:
            return args.func(args)
        except WsError as e:
            st = Style.detect(sys.stderr, no_color=_no_color(args))
            st.write(error_text(e, st), sys.stderr)
            return e.exit_code

    real_out, real_err = sys.stdout, sys.stderr
    out, err = io.StringIO(), io.StringIO()
    _kind, _emitted, _sink = kind, False, real_out
    try:
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = args.func(args) or 0
        except WsError:
            if err.getvalue():
                real_err.write(err.getvalue())
            raise
        if err.getvalue():
            if code and not _emitted:
                first, _, rest = err.getvalue().strip().partition("\n")
                failure = WsError("failed", first.removeprefix("ws: "), rest.strip(), code)
                print(json.dumps(error_envelope(failure, kind), indent=2), file=real_out)
                return code
            real_err.write(err.getvalue())
        if not _emitted:
            result({"output": out.getvalue().rstrip("\n")}, status=OK if not code else ATTENTION, kind=kind)
        return code
    finally:
        _kind, _emitted, _sink = "ws", False, None
