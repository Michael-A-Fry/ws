"""`ws gcal`: connect a Google Calendar to the broker, and the switches for its writes.

- `ws gcal ask NAME` (a session may run it): opens a tmux window running `ws gcal connect NAME` for the person;
  `ws secret wait NAME` then blocks until it has stored the value, as for any secret.
- `ws gcal connect NAME` (a person's terminal only): asks for the OAuth client's id and secret, prints Google's consent
  address, takes back the address the browser ends on, swaps the code for a refresh token (PKCE), makes or finds the
  "ws" calendar and stores all of it as NAME's value. The token is never printed.
- `ws gcal switch [insert|patch on|off]`: shows the switches, or moves one. Off is anyone's; on is a person's, at their
  own terminal (ws.agentsession), and the broker checks again that the caller holds a terminal. Each flip is a line in
  the broker's audit log.

The redirect is http://127.0.0.1:<port>/ on the person's own machine (Google's loopback flow for a Desktop client).
Nothing listens there: the browser shows "can't connect", and the address bar holds the code, which is what is pasted
back. That works from any machine, with no tunnel and no port open on this one.
"""

from __future__ import annotations

import getpass
import os
import secrets as pysecrets
import shlex
import shutil
import subprocess
import sys
import time

from ws import broker, output, paths, secretportal, selfcmd
from ws import secrets as wsecrets
from ws.minters import gcal
from ws.output import WsError

REDIRECT_PORT = 8765


def _entry(name: str) -> wsecrets.Entry:
    reg = wsecrets.load_registry(wsecrets.registry_path())
    e = reg.get(name)
    if e is None or e.kind != gcal.KIND:
        raise WsError("not_found", f"{name!r} is not a gcal entry in {wsecrets.registry_path()}",
                      f'add it first:\n    [{name}]\n    kind = "gcal"', 2)  # fmt: skip
    return e


def cmd_ask(args) -> int:
    _entry(args.name)
    tmux = shutil.which("tmux")
    if not tmux or not os.environ.get("TMUX"):
        raise WsError("unsupported", "no tmux session to open the window in", f"run `ws gcal connect {args.name}`")
    why = selfcmd.unstable()
    if why:
        raise WsError("refused", f"ws runs from {why}", "ask from the installed ws")
    env = [f"HOME={shlex.quote(secretportal.trusted_env()['HOME'])}", f"PATH={shlex.quote(paths.tool_path())}",
           "PYTHONUTF8=1"]  # fmt: skip
    cmd = "/usr/bin/env -i " + " ".join(env) + " " + shlex.join([*selfcmd.argv(), "gcal", "connect", args.name])
    secretportal._write_marker(args.name, "asked")
    r = subprocess.run([tmux, "new-window", "-d", "-n", f"secret:{args.name}", cmd],
                       capture_output=True, text=True, timeout=10, check=False)  # fmt: skip
    if r.returncode != 0:
        raise WsError("failed", "tmux refused to open the window", "check `tmux ls`")
    if args.json:
        output.result({"name": args.name, "asked": True})
    else:
        print(
            f"ws: the window 'secret:{args.name}' walks through the Google consent; then `ws secret wait {args.name}`"
        )
    return 0


def _ask_line(prompt: str, secret: bool = False) -> str:
    v = getpass.getpass(prompt) if secret else input(prompt)
    return v.strip()


def cmd_connect(args) -> int:
    name = args.name
    try:
        entry = _entry(name)
        store = wsecrets.local_store()
        old_id = None
        try:
            old_id = gcal.load_held(store.read(name)).calendar_id
        except (FileNotFoundError, gcal.GcalError, wsecrets.SecretsError):
            pass
        print(f"Connecting Google Calendar as {name} (stored in {wsecrets.store_dir()}).\n")
        client_id = _ask_line("1. OAuth client ID (ends .apps.googleusercontent.com): ")
        if not gcal.CLIENT_ID_RE.match(client_id):
            raise gcal.GcalError("bad_request", "that is not a Google OAuth client ID")
        client_secret = _ask_line("2. Client secret (not shown): ", secret=True)
        if not 10 <= len(client_secret) <= 200 or any(c.isspace() for c in client_secret):
            raise gcal.GcalError("bad_request", "that is not a client secret")
        verifier, challenge = gcal.pkce()
        state = pysecrets.token_urlsafe(24)
        redirect = f"http://127.0.0.1:{REDIRECT_PORT}/"
        print("\n3. Open this address, choose your Google account, click Advanced > Go to ws (unsafe) if Google")
        print("   warns that the app is unverified, tick every calendar box and press Continue:\n")
        print(gcal.auth_url(client_id, redirect, state, challenge))
        print("\n   The browser then says it can't connect to 127.0.0.1. That is expected.")
        pasted = _ask_line("4. Copy the whole address from the browser's address bar and paste it here (not shown): ",
                           secret=True)  # fmt: skip
        code = gcal.code_from(pasted, state)
        refresh = gcal.exchange(client_id, client_secret, code, verifier, redirect)
        assert isinstance(entry.options, gcal.Options)
        cal_id = gcal.ensure_calendar(client_id, client_secret, refresh, entry.options.timezone, old_id)
        value = gcal.dump_held(gcal.Held(client_id, client_secret, refresh, cal_id))
        store.write(name, wsecrets.check_value(value))
    except (EOFError, KeyboardInterrupt):
        secretportal._write_marker(name, "cancelled")
        print("\ncancelled")
        return 1
    except (gcal.GcalError, wsecrets.SecretsError, OSError) as e:
        secretportal._write_marker(name, "failed", why=str(e)[:200])
        print(f"\nnot stored: {e}", file=sys.stderr)
        time.sleep(5)
        return 1
    secretportal._write_marker(name, "stored")
    print(
        f"\nstored {name}: ws can read your calendars and write to its own 'ws' calendar ({'kept' if cal_id == old_id else 'made'})."
    )
    time.sleep(2)
    return 0


def cmd_switch(args) -> int:
    from ws import agentsession

    if args.write is None:
        out = broker.gcal_switch(None, None, args.socket)
        if args.json:
            output.result(out)
        else:
            for w, on in out["switches"].items():
                print(f"calendar {w}: {'on' if on else 'off'}")
        return 0
    if args.state is None:
        raise WsError("bad_input", "say on or off", f"`ws gcal switch {args.write} off`", 2)
    if args.state == "on":
        why = agentsession.refusal(os.environ, sys.stdin.isatty(), sys.stdout.isatty())
        if why:
            raise WsError("refused", f"ws gcal switch {args.write} on: {why}", "run it in your own terminal", 3)
        word = pysecrets.choice(["kauri", "tui", "kea", "rata", "nikau", "weka"])
        typed = input(f"Turn calendar {args.write} ON: ws may then {args.write} events on its 'ws' calendar by itself.\n"
                      f"Type {word} to confirm: ").strip()  # fmt: skip
        if typed != word:
            raise WsError("refused", "not confirmed; the switch did not move", "", 1)
    try:
        out = broker.gcal_switch(args.write, args.state, args.socket)
    except broker.BrokerRefused as e:
        raise WsError("refused", str(e)) from e
    except broker.BrokerUnavailable as e:
        raise WsError("unavailable", str(e), "start the broker; a switch moves only through it") from e
    if args.json:
        output.result(out)
    else:
        print(f"ws: calendar {args.write} is {args.state}" + ("" if out.get("changed") else " (it already was)"))
    return 0


def register(sub) -> None:
    g = sub.add_parser("gcal", help="Google Calendar through the broker: connect it, and its write switches")
    gsub = g.add_subparsers(dest="gcal_command", required=True)
    p = gsub.add_parser("ask", help="open a tmux window that walks a person through connecting NAME")
    p.add_argument("name")
    p.set_defaults(func=cmd_ask)
    p = gsub.add_parser("connect", help="connect NAME: Google consent, then store the token in the broker's store")
    p.add_argument("name")
    p.set_defaults(func=cmd_connect, raw=lambda a: True)
    p = gsub.add_parser("switch", help="show the write switches, or turn one on (a person only) or off")
    p.add_argument("write", nargs="?", choices=gcal.WRITES)
    p.add_argument("state", nargs="?", choices=("on", "off"))
    p.add_argument("--socket", metavar="PATH")
    p.set_defaults(func=cmd_switch, raw=lambda a: a.state == "on")
