"""`ws mail`: add, check and remove the mailboxes of the profile, and serve the mail MCP tools (ws.mail_mcp) over stdio.

`ws mail add NAME --host H --user U [--kind gmail]` writes `[mail.NAME]` (secret `mail-NAME`) to the profile, checks
the secrets registry has that name as an `imap` entry for the same host and user (if not, it prints the block to add
and stops: the registry is the person's file, ws never writes it), asks for the password with the existing `ws secret
ask` / `wait` flow when the store has none, then logs in read-only through the broker and prints the folders found and
the drafts folder. A missing drafts folder is said out loud: drafts are refused until there is one.

`ws mail check` logs in read-only (LIST) to every mailbox and says ok or why not; exit 1 when any fails. `ws mail rm
NAME` takes the profile table out; the registry entry and the stored password stay, and it prints how to remove them.
No command here prints or takes a password: the value goes from the person's prompt window to the store.
"""

from __future__ import annotations

import argparse
import contextlib
import sys

from ws import mail, mail_mcp, output, policy, profile, secretportal
from ws import secrets as wsecrets
from ws.minters import imap
from ws.output import WsError

COMMAND = "mail"


def secret_name(name: str) -> str:
    return f"mail-{name}"


def registry_block(name: str, host: str, user: str, port: int) -> str:
    lines = [f"[{secret_name(name)}]", 'kind = "imap"', f'host = "{host}"']
    if port != 993:
        lines.append(f"port = {port}")
    lines.append(f'user = "{user}"')
    return "\n".join(lines) + "\n"


def _checked(args: argparse.Namespace) -> tuple[str, str, str]:
    name, host, user = args.name, (args.host or "").lower(), args.user or ""
    if not mail.MAILBOX_RE.match(name):
        raise WsError("usage", f"{name!r} is not a mailbox name", "lower-case letters, digits, - and _", 2)
    if not wsecrets.HOST_RE.match(host) or ":" in host:
        raise WsError("usage", f"{args.host!r} is not an IMAP host name", "e.g. --host imap.gmail.com (no port)", 2)
    if not imap.USER_RE.match(user):
        raise WsError("usage", f"{user!r} is not a mailbox login", "e.g. --user someone@example.com", 2)
    if not 1 <= args.port <= 65535:
        raise WsError("usage", "--port must be from 1 to 65535", "993 is IMAP over TLS", 2)
    return name, host, user


def _registry() -> dict:
    try:
        return wsecrets.load_registry(wsecrets.registry_path())
    except wsecrets.SecretsError as e:
        raise WsError("bad_config", str(e), f"fix {wsecrets.registry_path()}") from None


def _write_profile(name: str, kind: str) -> bool:
    path = profile.profile_path()
    try:
        doc = profile.load(path) or {}
    except profile.ProfileError as e:
        raise WsError("bad_config", str(e), f"fix {path}") from None
    mail.parse_mailboxes(doc, str(path))  # the tables already there must be whole before one is added
    boxes = doc.setdefault("mail", {})
    boxes[name] = {"secret": secret_name(name), "kind": kind}
    return profile.write_if_changed(path, doc)


def _ask(secret: str, timeout: float) -> None:
    """The existing person-only flow: a no-echo prompt window, then a wait until it has stored the value. Its own lines
    go to stderr, so `--json` keeps one envelope on stdout."""
    with contextlib.redirect_stdout(sys.stderr):
        secretportal.cmd_ask(argparse.Namespace(name=secret, json=False))
        secretportal.cmd_wait(argparse.Namespace(name=secret, json=False, timeout=timeout))


def _connector_missing() -> bool:
    try:
        return mail_mcp.NAME not in policy.load().connectors
    except Exception:  # noqa: BLE001 (no policy, or one that does not load: the snippet is shown either way)
        return True


def cmd_add(args: argparse.Namespace) -> int:
    name, host, user = _checked(args)
    secret = secret_name(name)
    entry = _registry().get(secret)
    if entry is not None:
        o = entry.options
        if entry.kind != imap.KIND or not isinstance(o, imap.Options):
            raise WsError("conflict", f"{secret} is in the registry as kind {entry.kind}, not imap", "pick another NAME", 2)
        if (o.host, o.user, o.port) != (host, user, args.port):
            raise WsError(
                "conflict",
                f"{secret} in the registry is {o.user} at {o.host}:{o.port}, not {user} at {host}:{args.port}",
                f"edit {wsecrets.registry_path()}, or pick another NAME",
                2,
            )
    written = _write_profile(name, args.kind)
    data: dict = {"mailbox": name, "secret": secret, "kind": args.kind, "profile_written": written}
    if entry is None:
        block = registry_block(name, host, user, args.port)
        data.update(registered=False, registry=str(wsecrets.registry_path()), add=block)
        if args.json:
            output.result(data, status=output.ATTENTION)
        else:
            print(f"ws: [mail.{name}] is in the profile. Add this to {wsecrets.registry_path()}, then run this again:")
            print(block, end="")
        return 1
    client = mail.Mail.from_profile()
    try:
        found = client.folders(name)
    except mail.MailError as e:
        if e.kind != "unset":
            raise
        _ask(secret, args.timeout)
        found = client.folders(name)
    data.update(registered=True, **{k: found[k] for k in ("folders", "gmail", "drafts")})
    data["connector"] = mail_mcp.CONNECTOR_TOML if _connector_missing() else None
    if args.json:
        output.result(data, status=output.OK if found["drafts"] else output.ATTENTION)
    else:
        print(f"ws: {name} logged in: {len(found['folders'])} folders: {', '.join(found['folders'])}")
        if found["drafts"]:
            print(f"drafts go to {found['drafts']}")
        else:
            print(f"no drafts folder found: drafts are refused until the server marks one, or {secret} sets drafts_folder")
        if data["connector"]:
            print("To give a session the mail tools, add this to your policy:\n" + data["connector"], end="")
    return 0 if found["drafts"] else 1


def cmd_check(args: argparse.Namespace) -> int:
    client = mail.Mail.from_profile()
    rows = []
    for b in client.mailboxes():
        try:
            f = client.folders(b["name"])
            rows.append({"mailbox": b["name"], "ok": True, "folders": len(f["folders"]), "drafts": f["drafts"]})
        except mail.MailError as e:
            rows.append({"mailbox": b["name"], "ok": False, "error_kind": e.kind, "message": e.message})
    bad = not rows or not all(r["ok"] for r in rows)
    if args.json:
        output.result({"mailboxes": rows}, status=output.ATTENTION if bad else output.OK)
    elif not rows:
        print("ws: no mailboxes in the profile (`ws mail add NAME --host H --user U`)")
    else:
        for r in rows:
            if r["ok"]:
                print(f"{r['mailbox']}: ok, {r['folders']} folders, drafts {r['drafts'] or 'none found'}")
            else:
                print(f"{r['mailbox']}: fail ({r['error_kind']}): {r['message']}")
    return 1 if bad else 0


def cmd_rm(args: argparse.Namespace) -> int:
    path = profile.profile_path()
    try:
        doc = profile.load(path) or {}
    except profile.ProfileError as e:
        raise WsError("bad_config", str(e), f"fix {path}") from None
    boxes = doc.get("mail")
    if not isinstance(boxes, dict) or args.name not in boxes:
        raise WsError("not_found", f"no mailbox {args.name!r} in {path}", "`ws mail check` lists them", 2)
    secret = boxes.pop(args.name).get("secret") if isinstance(boxes[args.name], dict) else None
    if not boxes:
        doc.pop("mail")
    profile.write_if_changed(path, doc)
    left = f"the registry entry [{secret}] and its stored value are kept: `ws secret rm {secret}` and edit the registry"
    if args.json:
        output.result({"mailbox": args.name, "removed": True, "secret": secret})
    else:
        print(f"ws: removed [mail.{args.name}]; {left}" if secret else f"ws: removed [mail.{args.name}]")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    try:
        return mail_mcp.serve()
    except (BrokenPipeError, KeyboardInterrupt):
        return 0


def register(sub) -> None:
    m = sub.add_parser("mail", help="mailboxes for triage ([mail.NAME]): add, check, rm, and the mail MCP tools")
    ms = m.add_subparsers(dest="mail_command", required=True)
    a = ms.add_parser("add", help="add a mailbox: profile table, password prompt, a read-only test login")
    a.add_argument("name", help="the mailbox name tools use (lower-case)")
    a.add_argument("--host", required=True, help="the IMAP server, e.g. imap.gmail.com")
    a.add_argument("--user", required=True, help="the mailbox login")
    a.add_argument("--kind", choices=mail.KINDS, default="imap", help="gmail uses Gmail search syntax and labels")
    a.add_argument("--port", type=int, default=993, help="IMAP over TLS (default 993)")
    a.add_argument("--timeout", type=float, default=secretportal.DEFAULT_WAIT_S, help="seconds to wait for the paste")
    a.set_defaults(func=cmd_add)
    c = ms.add_parser("check", help="log in read-only to every mailbox: ok or why not")
    c.set_defaults(func=cmd_check)
    r = ms.add_parser("rm", help="take a mailbox out of the profile (its registry entry and password stay)")
    r.add_argument("name")
    r.set_defaults(func=cmd_rm)
    s = ms.add_parser("serve", help="serve mail_list_mailboxes, mail_search, mail_get, mail_label, mail_draft on stdio")
    s.set_defaults(func=cmd_serve, raw=True)  # stdout is the MCP protocol
