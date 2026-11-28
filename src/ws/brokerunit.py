"""The broker as a systemd user service (C848): `ws broker unit [--apply | --remove]`.

On 24 Nov the broker was a bare process that `ws run` or `ws upgrade` started. Once it was stopped, nothing could start
it again: sessions may not start it, `ws run` was refused at the cap, and triage does not start brokers. Telegram and
inbox-v2 went down until Michael restarted it by hand. As a service, `Restart=always` brings it back whatever stopped
it. It always runs the installed ws, because ExecStart is this ws's own isolated `python -I -m ws`. `ws upgrade`
restarts the service instead of signalling a process, and `ws run` starts the service instead of spawning a broker.

Taking over from a bare broker needs no step of its own. While another broker answers on the socket, the service's
`broker serve` exits ("a broker is already running"), and systemd tries again every RestartSec with no start limit.
Whatever stops the bare broker, the service holds the socket within seconds.

`--apply` writes the unit, reloads systemd and enables and starts it. `--remove` stops, disables and deletes it. With
no flag the unit text is printed and nothing changes. Every systemctl call goes through `run`, so tests never touch
systemd. Swap note: any supervisor that restarts on exit (launchd KeepAlive, runit, s6) fits the same ExecStart.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

from ws import paths, selfcmd
from ws.output import WsError

Runner = Callable[..., subprocess.CompletedProcess]

UNIT = "ws-broker.service"
RESTART_S = 3


def unit_path(env: Mapping[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "systemd" / "user" / UNIT


def installed(env: Mapping[str, str] | None = None) -> bool:
    """True when the service file exists: ws then starts and restarts the broker through systemd only."""
    return unit_path(env).is_file()


def socket_of(env: Mapping[str, str] | None = None) -> Path:
    return paths.state_dir(dict(os.environ if env is None else env)).absolute() / "run" / "broker.sock"


def render(sock: Path, argv: list[str] | None = None, env: Mapping[str, str] | None = None) -> str:
    """The unit. Its environment is the one a timer unit of ws gets (`roles.systemd._env_lines`: the layout pinned, a
    scrubbed PATH), so the service's broker reads the same folders as one `ws run` starts."""
    from ws.roles import systemd as _units  # late: the roles package is not needed to serve

    cmd = " ".join(_quote(a) for a in [*(argv or selfcmd.argv()), "broker", "serve", "--socket", str(sock)])
    env_lines = "".join(line + "\n" for line in _units._env_lines(os.environ if env is None else env))
    return (
        "# Written by `ws broker unit --apply` (C848). Remove with `ws broker unit --remove`.\n"
        "[Unit]\n"
        "Description=ws secrets broker (C848)\n"
        "StartLimitIntervalSec=0\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={cmd}\n"
        f"WorkingDirectory={sock.parent.parent}\n"
        f"{env_lines}"
        "Restart=always\n"
        f"RestartSec={RESTART_S}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def refresh(python: str, run: Runner | None = None) -> bool:
    """On an upgrade: point ExecStart at the ws now installed (`python -I -m ws`) and reload, when it changed. True
    when the file is current afterwards."""
    path = unit_path()
    text = render(socket_of(), [python, "-I", "-m", "ws"])
    try:
        if path.is_file() and path.read_text(encoding="utf-8") == text:
            return True
        _write(path, text)
    except OSError:
        return False
    return systemctl("daemon-reload", run=run or subprocess.run) == 0


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def _quote(a: str) -> str:
    """systemd's own quoting: double quotes around a word with a space, a quote or a backslash in it."""
    if a and not any(c in a for c in " \t\"\\'"):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'


def systemctl(*args: str, run: Runner = subprocess.run) -> int:
    try:
        return run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def start(run: Runner = subprocess.run) -> bool:
    """Ask systemd to start the service (a no-op when it runs); the caller waits for the socket to answer."""
    return systemctl("start", "--no-block", UNIT, run=run) == 0


HINT = f"look at `systemctl --user status {UNIT}` and `journalctl --user -u {UNIT} -n 50`"


def main_pid(run: Runner = subprocess.run) -> int | None:
    """The pid systemd runs the service's broker as, or None (not running, or systemctl cannot say)."""
    try:
        r = run(["systemctl", "--user", "show", "-p", "MainPID", "--value", UNIT], capture_output=True, text=True,
                timeout=30, check=False)  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return None
    v = (r.stdout or "").strip()
    return int(v) if r.returncode == 0 and v.isdigit() and int(v) > 0 else None


def restart(run: Runner = subprocess.run) -> bool:
    return systemctl("restart", "--no-block", UNIT, run=run) == 0


def cmd_unit(args: argparse.Namespace, run: Runner = subprocess.run, start_bare=None) -> int:
    path = unit_path()
    if args.apply or args.remove:
        from ws import agentsession

        why = agentsession.refusal(os.environ, sys.stdin.isatty(), sys.stdout.isatty())
        if why:
            raise WsError("agent_session", f"ws broker unit changes a systemd service: {why}", "run it yourself", 1)
    if args.remove:
        if systemctl("disable", "--now", UNIT, run=run) != 0 and installed():
            raise WsError("systemd", f"systemctl --user disable --now {UNIT} failed", HINT, 1)
        path.unlink(missing_ok=True)
        systemctl("daemon-reload", run=run)
        from ws import run as _run  # late: run imports this module

        ok = (start_bare or _run._start_broker)(socket_of())  # never leave the person with no broker at all
        print(f"ws broker unit: removed {path}; " + ("a bare broker answers" if ok else "the next `ws run` starts one"))
        return 0
    text = render(socket_of())
    if not args.apply:
        sys.stdout.write(text)
        return 0
    if why := selfcmd.unstable():
        raise WsError("unstable", f"this ws would not last as a service's ExecStart: {why}", "run the installed ws", 1)
    if not shutil.which("systemctl"):
        raise WsError(
            "no_systemd", "systemctl is not on PATH", "run the broker by hand, or under your own supervisor", 1
        )
    _write(path, text)
    for step in (("daemon-reload",), ("enable", "--now", UNIT)):
        if systemctl(*step, run=run) != 0:
            raise WsError("systemd", f"systemctl --user {' '.join(step)} failed", HINT, 1)
    print(f"ws broker unit: {path} written and started; a broker already running keeps the socket until it stops")
    return 0
