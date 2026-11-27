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


def render(sock: Path, argv: list[str] | None = None, path_env: str | None = None) -> str:
    cmd = " ".join(_quote(a) for a in [*(argv or selfcmd.argv()), "broker", "serve", "--socket", str(sock)])
    path_env = path_env if path_env is not None else os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin")
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
        f"Environment={_quote('PATH=' + path_env)}\n"
        "Restart=always\n"
        f"RestartSec={RESTART_S}\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def _quote(a: str) -> str:
    """systemd's own quoting: double quotes around a word with a space, a quote or a backslash in it."""
    if a and not any(c in a for c in " \t\"\\'"):
        return a
    return '"' + a.replace("\\", "\\\\").replace('"', '\\"') + '"'


Runner = Callable[..., subprocess.CompletedProcess]


def systemctl(*args: str, run: Runner = subprocess.run) -> int:
    try:
        return run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False).returncode
    except (OSError, subprocess.SubprocessError):
        return 1


def start(run: Runner = subprocess.run) -> bool:
    """Ask systemd to start the service (a no-op when it runs); the caller waits for the socket to answer."""
    return systemctl("start", "--no-block", UNIT, run=run) == 0


def restart(run: Runner = subprocess.run) -> bool:
    return systemctl("restart", "--no-block", UNIT, run=run) == 0


def cmd_unit(args: argparse.Namespace, run: Runner = subprocess.run) -> int:
    path = unit_path()
    if args.remove:
        systemctl("disable", "--now", UNIT, run=run)
        path.unlink(missing_ok=True)
        systemctl("daemon-reload", run=run)
        print(f"ws broker unit: removed {path}; the next `ws run` starts a bare broker again")
        return 0
    text = render(socket_of())
    if not args.apply:
        sys.stdout.write(text)
        return 0
    if not shutil.which("systemctl"):
        raise WsError(
            "no_systemd", "systemctl is not on PATH", "run the broker by hand, or under your own supervisor", 1
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    for step in (("daemon-reload",), ("enable", "--now", UNIT)):
        if systemctl(*step, run=run) != 0:
            raise WsError(
                "systemd", f"systemctl --user {' '.join(step)} failed", f"look at systemctl --user status {UNIT}", 1
            )
    print(f"ws broker unit: {path} written and started; a broker already running keeps the socket until it stops")
    return 0
