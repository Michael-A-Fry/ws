"""`ws device add`: the one line that links a second machine to this one.

More than one machine needs Tailscale, so the new machine can reach this one by name without opening a port. This
module only prints the line; it changes nothing on either machine.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from collections.abc import Callable

INSTALL_URL = "https://tailscale.com/download"
INSTALL_WS = "uv tool install ws-aitoolkit"

Runner = Callable[[list[str]], str]  # argv -> stdout; raises OSError or SubprocessError when it cannot run


class DeviceError(Exception):
    """Something the person can fix; the message is safe to print."""


def _run(argv: list[str]) -> str:
    return subprocess.run(argv, capture_output=True, text=True, check=True, timeout=10).stdout


def tailscale_name(run: Runner = _run, which: Callable[[str], str | None] | None = None) -> str:
    """This machine's tailnet name from `tailscale status --json`. Raises DeviceError with what to do instead."""
    if not (which or shutil.which)("tailscale"):
        raise DeviceError(
            f"Tailscale is needed to link more than one machine and was not found. Install it from {INSTALL_URL}, "
            "sign in on both machines, then run `ws device add` again."
        )
    try:
        status = json.loads(run(["tailscale", "status", "--json"]))
    except (OSError, subprocess.SubprocessError, ValueError) as e:
        raise DeviceError(
            f"`tailscale status --json` did not answer ({type(e).__name__}): is Tailscale running?"
        ) from e
    me = status.get("Self") if isinstance(status, dict) else None
    if not isinstance(me, dict) or status.get("BackendState") not in (None, "Running"):
        raise DeviceError("Tailscale is installed but not signed in and running: start it, then run `ws device add`")
    name = str(me.get("DNSName") or me.get("HostName") or "").rstrip(".")
    if not name:
        raise DeviceError("Tailscale did not report a name for this machine")
    return name


def join_line(server: str) -> str:
    """The command for the new machine: install ws, then point it at this one."""
    return f"{INSTALL_WS} && ws init --server {server}"


def cmd_add(args: argparse.Namespace, run: Runner = _run, which: Callable[[str], str | None] | None = None) -> int:
    try:
        server = tailscale_name(run, which)
    except DeviceError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    label = f" {args.name}" if args.name else " the new machine"
    print(f"On{label}, run:")
    print(f"  {join_line(server)}")
    return 0


def register(sub) -> None:
    d = sub.add_parser("device", help="link another machine to this one")
    ds = d.add_subparsers(dest="device_command", required=True)
    a = ds.add_parser("add", help="print the one line to run on the new machine")
    a.add_argument("--name", metavar="NAME", help="what you call the new machine (only used in the message)")
    a.set_defaults(func=cmd_add)
