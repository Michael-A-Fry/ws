"""`ws term`: a terminal in your browser, on this machine's loopback, for reaching a session from a phone or laptop.

It starts your shell (or the command after `--`, or a tmux session with `--tmux NAME`) on a pseudo-terminal and serves
it at one address with a random token, printed once. It listens on 127.0.0.1; a wider address needs `--bind ADDR`
naming it. Remote reach is your own tunnel (see docs/web-terminal.md). The link stops working when `ws term` exits.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import shutil
import sys

from ws import output
from ws.output import WsError
from ws.term import tmux as tmux_mod
from ws.term.app import build_server
from ws.term.bridge import Session, child_env, open_pty

__all__ = ["SYNC_ENV", "bind_address", "bind_warning", "register"]

LOOPBACK = "127.0.0.1"
# The environment a full-screen program reads to turn on synchronized output (DECSET 2026), which the page's terminal
# honours. Left unset inside tmux, where programs never use it anyway. Other programs ignore it; `--no-sync-output`
# leaves it out.
SYNC_ENV = {"CLAUDE_CODE_FORCE_SYNC_OUTPUT": "1"}


def bind_address(text: str | None) -> str:
    """The address to listen on: loopback unless a specific IPv4 address is named. A wildcard is refused (exit 2)."""
    if text is None:
        return LOOPBACK
    hint = "name the address of one network interface, or leave --bind out to listen on loopback only"
    try:
        addr = ipaddress.ip_address(text.strip())
    except ValueError:
        raise WsError("usage", f"--bind {text!r} isn't an IPv4 address", hint, 2) from None
    if addr.version != 4:
        raise WsError("usage", "--bind takes an IPv4 address", hint, 2)
    if addr.is_unspecified:
        raise WsError("usage", "--bind won't listen on every interface", hint, 2)
    return str(addr)


def bind_warning(bind: str) -> str | None:
    """The line printed when the server listens beyond this machine; None on loopback."""
    if bind == LOOPBACK:
        return None
    return (
        f"listening on {bind}, not just this machine: anyone who can reach it and has the link can run commands as you"
    )


def _program(args: argparse.Namespace) -> list[str]:
    command = [a for a in (args.command or [])]
    if command and command[0] == "--":
        command = command[1:]
    if command:
        argv = command
    else:
        argv = [os.environ.get("SHELL") or "/bin/sh"]
    if shutil.which(argv[0]) is None:
        raise WsError("not_found", f"can't run {argv[0]!r}: not found", "give a program that is on your PATH", 2)
    return argv


def _env_pairs(items: list[str]) -> dict[str, str]:
    out = {}
    for item in items:
        name, eq, value = item.partition("=")
        if not eq or not name or not name.replace("_", "").isalnum():
            raise WsError("bad_env", f"--env {item!r} isn't NAME=VALUE", "write it as NAME=VALUE", 2)
        out[name] = value
    return out


def cmd_term(args: argparse.Namespace) -> int:
    if sys.platform == "win32":
        raise WsError("unsupported", "ws term needs a POSIX system", "run it inside WSL2", 1)
    bind = bind_address(args.bind)
    warnings = [w for w in (bind_warning(bind),) if w]
    if args.tmux and args.command:
        raise WsError("usage", "--tmux and a command after -- can't be combined", "pick one", 2)
    tm = None
    if args.tmux:
        if shutil.which("tmux") is None:
            raise WsError("missing_tool", "tmux isn't installed", "install tmux, or leave --tmux out", 1)
        try:
            tm = tmux_mod.Tmux(args.tmux, server_options=args.tmux_server_options)
        except ValueError as e:
            raise WsError("bad_name", str(e), "for example --tmux work", 2) from None
        argv = ["tmux"]
    else:
        argv = _program(args)
    extra = {} if (tm or args.no_sync_output) else dict(SYNC_ENV)
    extra.update(_env_pairs(args.env))
    env = child_env(os.environ, extra)

    def spawn(cols: int, rows: int):
        if tm is None:
            return open_pty(argv, env, cols, rows)
        attach = tm.prepare(cols, rows)
        if tm.adopted:
            print(
                f"ws term: attached to the tmux session {tm.name!r}, which was already there; "
                "its options are unchanged, so its own size policy applies",
                file=sys.stderr,
                flush=True,
            )
        return open_pty(attach, env, cols, rows)

    holder = {}
    session = Session(spawn, on_exit=lambda _code: holder["server"].stop(), on_resize=tm.resize if tm else None)
    idle = args.idle_minutes * 60 if args.idle_minutes > 0 else float("inf")
    try:
        server = build_server(session, bind=bind, port=args.port, allow_hosts=args.allow_host or (), idle_seconds=idle)
    except OSError as e:
        raise WsError(
            "unavailable", f"can't listen on {bind}:{args.port}: {e.strerror or e}", "try another --port"
        ) from e
    holder["server"] = server
    if args.json:
        output.result({"url": server.url, "bind": bind, "port": server.port, "warnings": warnings})
    else:
        for w in warnings:
            print(f"ws term: warning: {w}", file=sys.stderr)
        print(server.url, flush=True)
        print("Open this link in a browser. It works until you press Ctrl-C here.", file=sys.stderr, flush=True)
    try:
        server.serve()
    except KeyboardInterrupt:
        pass
    finally:
        session.close()
    if tm is not None and tm.skipped and not tm.adopted and not args.json:
        names = ", ".join(f"{o.name} {o.value}" for o in tm.skipped)
        print(
            f"ws term: left the tmux server options alone ({names}); add --tmux-server-options to set them",
            file=sys.stderr,
        )
    return 0


def register(sub) -> None:
    p = sub.add_parser("term", help="a terminal in your browser (loopback, one link), for a phone or another computer")
    p.add_argument("--port", type=int, default=0, metavar="N", help="listen on this port (default: any free one)")
    p.add_argument(
        "--bind", metavar="ADDR", help="listen on this IPv4 address instead of 127.0.0.1 (names one interface)"
    )
    p.add_argument(
        "--allow-host",
        action="append",
        metavar="HOST[:PORT]",
        help="also accept this Host header, for a proxy or a tunnel on another port (repeat for more)",
    )
    p.add_argument(
        "--tmux",
        metavar="NAME",
        help="attach this tmux session; one ws creates is sized by the browser, one that exists is left as it is",
    )
    p.add_argument(
        "--tmux-server-options",
        action="store_true",
        help=(
            "with --tmux: also set escape-time and extended-keys; these change the tmux server for every session "
            "on it, not just this one (off by default)"
        ),
    )
    p.add_argument(
        "--env", action="append", default=[], metavar="NAME=VALUE", help="add a variable to the session's environment"
    )
    p.add_argument(
        "--no-sync-output",
        action="store_true",
        help="don't set the variable that asks programs for synchronized output (other programs ignore it)",
    )
    p.add_argument(
        "--idle-minutes",
        type=float,
        default=60.0,
        metavar="N",
        help="stop after N minutes with no browser attached (default 60; 0 never)",
    )
    p.add_argument("command", nargs=argparse.REMAINDER, metavar="-- COMMAND", help="run this instead of your shell")
    p.set_defaults(func=cmd_term)
