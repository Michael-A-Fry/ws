"""`ws secret ask` and `ws secret wait`: a person pastes a secret into its own terminal window, and it goes straight to the store.

`ask NAME` opens a new tmux window whose only job is a no-echo prompt for NAME, on that window's own tty. The value is
read there and written to the store; it is never in an argument list, an environment variable, a log, an audit line or
any reply. `wait NAME` blocks until the prompt has stored it (or was cancelled, or the time ran out). Both are admin
commands that show no value, so a gated session may run them (ws.agentsession.SESSION_OK); only a person's terminal
runs `prompt`, which is where the value is typed, and the window says which store it will write to.

The window command carries the name only. A small marker file in the state folder says when a prompt was opened and how it
ended; it holds no value. Without tmux, `ws secret set NAME` does the same on the terminal you are in.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

from ws import output, paths, selfcmd
from ws import secrets as wsecrets
from ws.output import WsError

POLL_S = 0.5
DEFAULT_WAIT_S = 900.0
STALE_AFTER_S = 3.0  # a window opened this recently may not be listed yet


def asks_dir(env: dict | None = None) -> Path:
    return paths.state_dir(env) / "secret-asks"


def _marker(name: str, env: dict | None = None) -> Path:
    if not wsecrets.NAME_RE.match(name):
        raise WsError("bad_input", "not a valid secret name", "names start with a letter: letters, digits, - and _", 2)
    return asks_dir(env) / f"{name}.json"


def _write_marker(name: str, state: str, env: dict | None = None, **extra: object) -> None:
    f = _marker(name, env)
    f.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = f.with_name(f".{f.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps({"name": name, "state": state, "at": time.time(), **extra}))
    os.replace(tmp, f)


def _read_marker(name: str, env: dict | None = None) -> dict:
    try:
        d = json.loads(_marker(name, env).read_text())
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _window_gone(name: str) -> bool:
    """True when tmux answers and no window of that name is left: the person closed the prompt without answering."""
    tmux = shutil.which("tmux")
    if not tmux:
        return False
    try:
        r = subprocess.run(
            [tmux, "list-windows", "-a", "-F", "#{window_name}"], capture_output=True, text=True, timeout=5, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return r.returncode == 0 and f"secret:{name}" not in r.stdout.split("\n")


def trusted_env() -> dict[str, str]:
    """The environment the person's own ws folders come from: the account's home folder from the system's account
    database, and no layout variable. A session can set `WS_HOME`, `HOME`, an `XDG_*` variable or a tmux environment, so
    nothing here, in the window or in the prompt reads them."""
    home = paths.account_home()
    if home is None:
        raise WsError("refused", "the account's home folder cannot be told", "run it in a terminal of your own")
    return {"HOME": str(home)}


def _real_dirs(env) -> tuple[str, ...]:
    lay = paths.layout(env)
    return tuple(os.path.realpath(lay.dir(k)) for k in paths.KINDS)


@contextlib.contextmanager
def _trusted():
    """Inside the block every ws folder is the account's own, whatever this process's environment says."""
    env = trusted_env()
    try:
        pin = paths.pin_of(env)
    except WsError as e:
        raise WsError("refused", f"the account's ws folders cannot be told ({e.message})", e.hint or "") from e
    with paths.pinned(pin):
        yield env


def window_command(name: str) -> str:
    """The shell text the new window runs: `env -i` with the account's home and nothing else from the session, the ws
    this one is (refused when it runs from a folder that comes and goes), the name, never a value."""
    why = selfcmd.unstable()
    if why:
        raise WsError("refused", f"ws runs from {why}", "ask from the installed ws, in your own terminal")
    env = [f"HOME={shlex.quote(trusted_env()['HOME'])}", f"PATH={shlex.quote(paths.tool_path())}", "PYTHONUTF8=1"]
    # `/usr/bin/env` by path: a session can set tmux's global PATH
    return "/usr/bin/env -i " + " ".join(env) + " " + shlex.join([*selfcmd.argv(), "secret", "prompt", name])


def cmd_ask(args) -> int:
    with _trusted():
        try:
            same = _real_dirs(os.environ) == _real_dirs(trusted_env())
        except WsError:  # the session's variables name a layout that cannot be told: not the account's own
            same = False
        if not same:
            raise WsError(
                "refused",
                "this session's ws folders are not the account's own (WS_HOME, HOME or an XDG variable points elsewhere)",
                "ask from your own terminal, or take the variable out of the session",
            )
        return _ask(args)


def cmd_prompt(args) -> int:
    with _trusted():
        return _prompt(args)


def cmd_wait(args) -> int:
    with _trusted():
        return _wait(args)


def _ask(args) -> int:
    reg = wsecrets.load_registry(wsecrets.registry_path())
    name = args.name
    _marker(name)
    if name not in reg:
        raise WsError(
            "not_found", f"{name!r} is not in {wsecrets.registry_path()}", "register it with its hosts first", 2
        )
    tmux = shutil.which("tmux")
    if not tmux or not os.environ.get("TMUX"):
        raise WsError(
            "unsupported",
            "no tmux session to open a prompt window in",
            f"run `ws secret set {name}` in a terminal you are in",
        )
    _write_marker(name, "asked")
    try:
        r = subprocess.run(
            [tmux, "new-window", "-d", "-n", f"secret:{name}", window_command(name)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        raise WsError("failed", "tmux did not open the window", "check `tmux ls`") from e
    if r.returncode != 0:
        raise WsError("failed", "tmux refused to open the window", "check `tmux ls`")
    if args.json:
        output.result({"name": name, "asked": True})
        return 0
    print(f"ws: asked for {name}: paste it in the window 'secret:{name}', then `ws secret wait {name}`")
    return 0


def _prompt(args) -> int:
    """Runs inside the new window: read the value with no echo, store it, record how it ended."""
    name = args.name
    _marker(name)
    try:
        if name not in wsecrets.load_registry(wsecrets.registry_path()):
            raise wsecrets.SecretsError(f"{name!r} is not registered")
        # the store is the one the account's own profile names (`[roles.secrets] store`, in the gate-protected config
        # folder: the person's choice) or the default one; never a session's variable, which `_trusted` ignores
        print(f"Storing {name} in {wsecrets.store_dir()}")
        try:
            value = getpass.getpass(f"Value for {name} (not shown; Enter stores it, Ctrl-C cancels): ")
        except (EOFError, KeyboardInterrupt):
            _write_marker(name, "cancelled")
            print("cancelled")
            return 1
        wsecrets.local_store().write(name, wsecrets.check_value(value))
    except (wsecrets.SecretsError, OSError) as e:
        _write_marker(name, "failed", why=str(e)[:200])
        print(f"not stored: {e}", file=sys.stderr)
        time.sleep(3)
        return 1
    _write_marker(name, "stored")
    print(f"stored {name}")
    time.sleep(1)
    return 0


def _wait(args) -> int:
    name = args.name
    _marker(name)
    deadline = time.monotonic() + args.timeout
    while True:
        marker = _read_marker(name)
        state = marker.get("state")
        if state == "asked" and time.time() - float(marker.get("at") or 0) > STALE_AFTER_S and _window_gone(name):
            raise WsError("failed", f"the prompt window for {name} was closed", f"ask again: `ws secret ask {name}`")
        if state == "stored":
            if args.json:
                output.result({"name": name, "stored": True})
            else:
                print(f"ws: {name} is stored")
            return 0
        if state in ("cancelled", "failed"):
            raise WsError("failed", f"the prompt for {name} ended: {state}", f"ask again: `ws secret ask {name}`")
        if state is None:
            raise WsError("not_found", f"no prompt is open for {name}", f"run `ws secret ask {name}` first", 2)
        if time.monotonic() >= deadline:
            raise WsError("timeout", f"{name} was not stored in {args.timeout:g}s", f"`ws secret wait {name}` again")
        time.sleep(POLL_S)


def shim() -> None:
    """The `ws-secret` command: the older name for the secret commands, so existing briefs and scripts keep working.

    `ws-secret ask NAME` is `ws secret ask NAME`. It is kept for 30 days after the AI_Workspace tools are switched off,
    then removed; use `ws secret` directly."""
    from ws import cli

    sys.exit(cli.main(["secret", *sys.argv[1:]]))


def register(ssub) -> None:
    p = ssub.add_parser("ask", help="open a tmux window with a no-echo prompt for a registered name")
    p.add_argument("name")
    p.set_defaults(func=cmd_ask)
    p = ssub.add_parser("wait", help="block until `ask` has stored the value")
    p.add_argument("name")
    p.add_argument("--timeout", type=float, default=DEFAULT_WAIT_S, metavar="SECONDS")
    p.set_defaults(func=cmd_wait)
    p = ssub.add_parser("prompt", help=argparse.SUPPRESS)
    p.add_argument("name")
    p.set_defaults(func=cmd_prompt)
