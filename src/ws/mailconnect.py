"""Connect a mailbox from a page: one call that checks an app password, then stores it and adds the mailbox, or says why not.

`connect(name, address, password, provider)` is what a "Connect mail" card calls (`ws mail connect --web`, or any
page of the person's own that imports it). In order:

1. The name, address and provider are checked.
2. The registry entry `mail-<name>` is read. One that is there must be for the same host and login (`conflict`). One
   that is missing is added only for a host in PROVIDERS (Gmail, Outlook, iCloud); for any other host nothing is
   written and the block to add is returned (`needs_registry`). ws only ever appends a new entry; it never edits or
   removes one.
3. The password is tidied for its host (`ws.secrets.clean_value`: a Gmail app password loses Google's spaces and must
   be 16 letters).
4. The limit: an account (host and login, whatever the mailbox is named) with MAX_FAILS tries in the last WINDOW_S
   seconds is refused before anything is dialled (`rate_limited`), because a provider locks an account out after a few
   bad tries. In one locked section the count is checked and this try is written; a refusal keeps it, a login that
   worked clears the account, and no answer takes it out. The tries are times in one state file, pruned by age, so
   the count can't go stale.
5. One read-only login and LIST, made here with the typed value, before anything is stored. A refusal is a typed
   ConnectError (`bad_credentials`, `app_password_required`, `imap_off`, `web_login_required`, `unreachable`, ...)
   whose words say what to fix; it counts towards the limit, and nothing is written, so a stored value that works is
   never replaced by one that doesn't.
6. Only then, all or nothing: the registry entry (if new), the value in the local store, and `[mail.<name>]` in the
   profile (`save_failed` puts back what was there). A last LIST through the broker proves the broker sees it
   (`broker_ok`).

`set_on(name, on)` switches a mailbox off (its profile table out, its password kept) or back on; `disconnect(name)`
takes the table and the stored password out. Neither touches the registry entry.

The value lives only in this call's memory and the store: it is never in a log, a state file, a result or an error
text, and no error carries the server's own words. Drafts only: nothing here changes what the mail connector may do.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

from ws import mail, mailcmd, paths
from ws import secrets as wsecrets
from ws.minters import imap
from ws.output import WsError

MAX_FAILS = 3
WINDOW_S = 15 * 60
FUTURE_S = 5.0  # how far past `now` a try's time may be and still count
ADDRESS_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,189}\.[A-Za-z]{2,24}$")


@dataclass(frozen=True)
class Provider:
    key: str
    label: str
    host: str
    kind: str  # the mailbox kind ws.mail uses: "gmail" searches with Gmail syntax
    app_passwords: str  # where the person makes an app password
    help: str  # one line for the card
    domains: tuple[str, ...]  # address domains that mean this provider


PROVIDERS = {
    p.key: p
    for p in (
        Provider(
            "gmail", "Gmail or Google Workspace", "imap.gmail.com", "gmail",
            "https://myaccount.google.com/apppasswords",
            "2-Step Verification must be on. Paste the 16 letters as Google shows them; spaces are fine.",
            ("gmail.com", "googlemail.com"),
        ),
        Provider(
            "outlook", "Outlook or Microsoft 365", "outlook.office365.com", "imap",
            "https://account.live.com/proofs/AppPassword",
            "Two-step verification must be on to make an app password.",
            ("outlook.com", "hotmail.com", "live.com", "msn.com"),
        ),
        Provider(
            "icloud", "iCloud Mail", "imap.mail.me.com", "imap",
            "https://account.apple.com",
            "Sign-In and Security, App-Specific Passwords. Two-factor authentication must be on.",
            ("icloud.com", "me.com", "mac.com"),
        ),
    )
}  # fmt: skip
HOSTS = {p.host: p for p in PROVIDERS.values()}

# What to say for each refusal. The words are ours: a server's own text never reaches a page.
SAY = {
    "bad_credentials": (
        "The mail server refused the password.",
        (
            "Use an app password, not your account password. 2-Step Verification must be on; for a work account, "
            "the admin must allow app passwords."
        ),
    ),
    "app_password_required": (
        "That is your account password; this needs an app password.",
        "Make an app password and paste that instead.",
    ),
    "imap_off": ("IMAP is turned off for this mailbox.", "Turn IMAP on in the mailbox's settings, then try again."),
    "web_login_required": (
        "The provider wants you to sign in on the web first.",
        "Sign in to the mailbox in a browser, then try again.",
    ),
    "refused": ("The mail server refused the login.", "Check the address and the app password."),
    "unreachable": ("The mail server could not be reached.", "Check this machine's network, then try again."),
    "tls_failed": ("The mail server's certificate did not check out.", "Nothing was sent. Try again later."),
}


class ConnectError(WsError):
    """A connect that did not happen. `kind` is one of SAY's keys or this module's own; the text is safe to show."""


def provider_for(address: str, provider: str | None = None) -> Provider | None:
    """The provider named, else the one the address's domain means, else None (a custom domain: the person picks)."""
    if provider:
        return PROVIDERS.get(provider)
    domain = address.rpartition("@")[2].lower()
    return next((p for p in PROVIDERS.values() if domain in p.domains), None)


def known(env: dict | None = None) -> list[dict]:
    """The mailboxes a card can offer: every `mail-*` imap registry entry, with its login, host, whether a value is
    stored and a profile table points at it, and when a connect last logged in with the stored value (`checked_at`,
    None when it never has: a value set another way is unproven). Never a value."""
    reg = wsecrets.load_registry(wsecrets.registry_path(env))
    try:
        stored = set(wsecrets.local_store(env).names())
    except (PermissionError, wsecrets.SecretsError):
        stored = set()
    try:
        boxes = mail.load_mailboxes(env)
    except mail.MailError:
        boxes = {}
    checked = _checked(env)
    out = []
    for e in reg.values():
        if e.kind != imap.KIND or not e.name.startswith("mail-") or not isinstance(e.options, imap.Options):
            continue
        name = e.name[len("mail-") :]
        if not mail.MAILBOX_RE.match(name):
            continue
        p = HOSTS.get(e.options.host)
        out.append(
            {
                "name": name,
                "address": e.options.user,
                "host": e.options.host,
                "provider": p.key if p else None,
                "stored": e.name in stored,
                "in_profile": name in boxes,
                "checked_at": checked.get(name) if e.name in stored else None,
            }
        )
    return sorted(out, key=lambda r: r["name"])


# ---------------------------------------------------------------- the failure count


def _limit_file(env: dict | None) -> Path:
    return paths.state_dir(env) / "mail-connect" / "failures.json"


@contextlib.contextmanager
def _locked(path: Path):
    """An exclusive lock on `path`'s lock file for the read-modify-write of the count (no lock where there is no flock)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path.with_suffix(".lock"), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX)
        except ImportError:
            pass
        yield
    finally:
        os.close(fd)  # closing releases the lock


def _account(host: str, address: str) -> str:
    """What the limit counts by: the provider's account (host and login), never the mailbox name, which is free to change."""
    return f"{host.lower()}|{address.lower()}"


def _tries(path: Path, now: float) -> dict[str, list[dict]]:
    """The tries still inside the window, per account: {"t": time, "id": token}. A try is written before its login and
    taken out again only when the login could not count (no answer from the server, or a success)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    out: dict[str, list[dict]] = {}
    for k, v in raw.items() if isinstance(raw, dict) else ():
        if isinstance(k, str) and isinstance(v, list):
            keep = [
                {"t": float(x["t"]), "id": str(x.get("id", ""))}
                for x in v
                if isinstance(x, dict)
                and isinstance(x.get("t"), (int, float))
                and now - WINDOW_S
                < x["t"]
                <= now + FUTURE_S  # another thread read its clock a moment later: its try still counts
            ]
            if keep:
                out[k] = keep
    return out


def _write_recent(path: Path, doc: dict) -> None:
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    tmp.write_text(json.dumps(doc), encoding="utf-8")
    os.replace(tmp, path)


def _reserve(account: str, name: str, env: dict | None, now: float) -> str:
    """In one locked section: refuse when the account already has MAX_FAILS tries in the window (a try still logging
    in counts), else write this try and return its id. Parallel posts can't all pass, and a try whose process dies
    stays counted until it ages out."""
    path = _limit_file(env)
    with _locked(path):
        doc = _tries(path, now)
        held = doc.get(account, [])
        if len(held) >= MAX_FAILS:
            wait = max(1, int((min(x["t"] for x in held) + WINDOW_S - now + 59) // 60))
            raise ConnectError(
                "rate_limited",
                f"{MAX_FAILS} logins for {name} failed in the last {WINDOW_S // 60} minutes; ws stops here so the "
                "provider does not lock the account.",
                f"Try again in {wait} minute{'s' if wait != 1 else ''}.",
                details={"retry_minutes": wait},
            )
        token = secrets.token_hex(8)
        doc.setdefault(account, []).append({"t": now, "id": token})
        _write_recent(path, doc)
    return token


def _release(account: str, token: str, env: dict | None, now: float, *, all_of_them: bool = False) -> None:
    """Take this try out (the server never answered), or every try of the account (a login that worked)."""
    path = _limit_file(env)
    with _locked(path):
        doc = _tries(path, now)
        left = [] if all_of_them else [x for x in doc.get(account, []) if x["id"] != token]
        if left:
            doc[account] = left
        else:
            doc.pop(account, None)
        _write_recent(path, doc)


def _checked_file(env: dict | None) -> Path:
    return paths.state_dir(env) / "mail-connect" / "checked.json"


def _checked(env: dict | None) -> dict[str, float]:
    """When each mailbox's stored password last logged in through a connect (a time, never a value)."""
    try:
        raw = json.loads(_checked_file(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return (
        {k: float(v) for k, v in raw.items() if isinstance(k, str) and isinstance(v, (int, float))}
        if isinstance(raw, dict)
        else {}
    )


def _mark_checked(name: str, env: dict | None, now: float) -> None:
    path = _checked_file(env)
    with _locked(path):
        doc = _checked(env)
        doc[name] = now
        _write_recent(path, doc)  # type: ignore[arg-type]


# ---------------------------------------------------------------- the registry


# ws.secrets.append_entry and remove_block are the one registry writer (ADR 0034).


# ---------------------------------------------------------------- connect


def _save(name: str, kind: str, secret: str, value: str, reg_path: Path, block: str | None, env: dict | None) -> bool:
    """Write the registry entry (if new), the stored value and the profile table, or put back what was there and raise
    ConnectError: a good login never leaves a stored value with no table, or a table with no value. Saves run one at a
    time (a lock), and a restore takes out only what this save wrote."""
    with _locked(paths.state_dir(env) / "mail-connect" / "save"):  # one save at a time: a restore can't undo another's
        return _save_locked(name, kind, secret, value, reg_path, block, env)


def _save_locked(name, kind, secret, value, reg_path: Path, block: str | None, env: dict | None) -> bool:
    store = wsecrets.local_store(env)
    try:
        old_value = store.read(secret) if secret in store.names() else None
    except (OSError, PermissionError, wsecrets.SecretsError) as e:
        raise ConnectError("store_refused", "The password checked out, but ws can't read its store.", str(e)) from None
    profile_path = mailcmd.profile.profile_path(env)
    old_profile = profile_path.read_bytes() if profile_path.exists() else None
    step, appended = "registry", False
    try:
        if block is not None:
            wsecrets.append_entry(reg_path, block)
            appended = True
        step = "store"
        store.write(secret, value)
        step = "profile"
        return mailcmd.write_profile(name, kind, env)
    except (OSError, PermissionError, WsError, wsecrets.SecretsError) as e:
        # Each restore stands alone: one that fails never stops the next.
        if appended:
            with contextlib.suppress(OSError, wsecrets.SecretsError):
                wsecrets.remove_block(reg_path, block or "")
        with contextlib.suppress(OSError, wsecrets.SecretsError):
            if old_value is None:
                store.delete(secret)
            else:
                store.write(secret, old_value)
        with contextlib.suppress(OSError):
            if old_profile is None:
                profile_path.unlink(missing_ok=True)
            else:
                profile_path.write_bytes(old_profile)
        where = {"registry": "the secrets registry", "store": "the password store", "profile": "the profile"}[step]
        why = e.message if isinstance(e, WsError) else str(e)
        raise ConnectError(
            "save_failed", f"The password checked out, but ws couldn't write {where}, so nothing was kept.", why
        ) from None


def _fail(kind: str) -> ConnectError:
    message, hint = SAY[kind]
    return ConnectError(kind, message, hint)


def connect(
    name: str,
    address: str,
    password: str,
    provider: str | None = None,
    env: dict | None = None,
    *,
    host: str | None = None,
    cafile: str | None = None,
    now: float | None = None,
) -> dict:
    """Check the app password with one read-only login, then store it and add the mailbox. Returns
    {"mailbox", "address", "host", "folders", "drafts", "registry_added", "profile_written", "broker_ok"}; raises
    ConnectError. `host` names a server outside PROVIDERS (its registry entry must be there already). `cafile` is for
    tests (a test CA); `now` is for tests of the limit."""
    now = time.time() if now is None else now
    name, address = (name or "").strip().lower(), (address or "").strip()
    if not mail.MAILBOX_RE.match(name):
        raise ConnectError("bad_name", f"{name!r} can't be a mailbox name.", "Use lower-case letters, digits, - or _.")
    if not ADDRESS_RE.match(address):
        raise ConnectError("bad_address", "That isn't an e-mail address.", "Type the whole address.")
    if provider and provider not in PROVIDERS:
        raise ConnectError("bad_provider", "ws doesn't know that provider.", f"Pick one of: {', '.join(PROVIDERS)}.")
    if host and not (wsecrets.HOST_RE.match(host.lower()) and ":" not in host):
        raise ConnectError("bad_host", "That isn't a mail server name.", "e.g. imap.example.com, with no port.")
    if not password:
        raise ConnectError("no_password", "No app password was given.", "Paste the app password.")

    secret = mailcmd.secret_name(name)
    reg_path = wsecrets.registry_path(env)
    try:
        entry = wsecrets.load_registry(reg_path).get(secret)
    except wsecrets.SecretsError as e:
        raise ConnectError("bad_registry", f"The secrets registry doesn't load: {e}", f"Fix {reg_path}.") from None
    block = None
    if entry is not None:
        o = entry.options
        if entry.kind != imap.KIND or not isinstance(o, imap.Options):
            raise ConnectError("conflict", f"{secret} is already a secret of another kind.", "Pick another name.")
        if o.user.lower() != address.lower():
            raise ConnectError(
                "conflict", f"{name} is already the mailbox {o.user}.", "Pick that address, or another name."
            )
        opts, p = o, HOSTS.get(o.host)
        if provider and p and p.key != provider:
            raise ConnectError("conflict", f"{name} is already set up for {p.label}.", "Pick that provider.")
    else:
        if host and host.lower() not in HOSTS:  # a server ws doesn't know: the person adds the entry, ws never does
            block = mailcmd.registry_block(name, host.lower(), address, 993)
            raise ConnectError(
                "needs_registry", f"Add this to {reg_path}, then try again.", block, details={"add": block}
            )
        p = HOSTS[host.lower()] if host else provider_for(address, provider)
        if p is None:
            raise ConnectError("pick_provider", "ws can't tell who runs this mailbox.", "Pick the provider.")
        opts = imap.Options(host=p.host, user=address)
        block = mailcmd.registry_block(name, p.host, address, 993)
    kind = p.kind if p else "imap"

    try:
        value = wsecrets.clean_value(
            entry or wsecrets.Entry(name=secret, kind=imap.KIND, options=opts), wsecrets.check_value(password)
        )
    except wsecrets.SecretsError as e:
        raise ConnectError("not_app_password", str(e), "Copy the app password again and paste it.") from None

    account = _account(opts.host, address)
    token = _reserve(account, name, env, now)
    try:
        found = imap.run(opts, value, "list", {}, cafile=cafile)
    except imap.ImapError as e:
        if e.code == "login_refused":  # the server said no: the try stays counted
            raise _fail(e.login if e.login in SAY else "refused") from None
        _release(account, token, env, now)  # no answer to count
        if e.code in ("upstream_unreachable", "tls_failed"):
            raise _fail("unreachable" if e.code == "upstream_unreachable" else "tls_failed") from None
        raise ConnectError("failed", "The test login did not finish.", "Try again.") from None
    except BaseException:
        _release(account, token, env, now)
        raise
    _release(account, token, env, now, all_of_them=True)

    # The login worked: now, and only now, write; all of it or none of it.
    written = _save(name, kind, secret, value, reg_path, block, env)
    try:
        _mark_checked(name, env, now)
        checked = True
    except OSError:  # saved and working; only the "last checked" mark couldn't be written
        checked = False
    broker_ok, broker_reason = True, ""
    try:
        mail.Mail.from_profile(env).folders(name)
    except mail.MailError as e:  # saved, but the broker can't use it yet: say why (its words never hold the value)
        broker_ok, broker_reason = False, e.message
    folders = [f["name"] for f in found.get("folders", [])]
    return {
        "mailbox": name,
        "address": address,
        "host": opts.host,
        "folders": len(folders),
        "drafts": found.get("drafts"),
        "registry_added": block is not None,
        "profile_written": written,
        "checked": checked,
        "broker_ok": broker_ok,
        "broker_reason": broker_reason,
    }


# ---------------------------------------------------------------- on, off and disconnect


def _entry_for(name: str, env: dict | None) -> wsecrets.Entry:
    if not mail.MAILBOX_RE.match(name or ""):
        raise ConnectError("bad_name", f"{name!r} can't be a mailbox name.", "Use lower-case letters, digits, - or _.")
    entry = wsecrets.load_registry(wsecrets.registry_path(env)).get(mailcmd.secret_name(name))
    if entry is None or entry.kind != imap.KIND or not isinstance(entry.options, imap.Options):
        raise ConnectError("not_found", f"There is no mailbox {name}.", "Connect it first.")
    return entry


def set_on(name: str, on: bool, env: dict | None = None) -> dict:
    """Switch a mailbox on (its `[mail.NAME]` table back in the profile) or off (the table out; its stored password and
    registry entry stay, so on again needs no password). On needs a password that a connect has proven."""
    entry = _entry_for(name, env)
    if on:
        if name not in _checked(env) or entry.name not in wsecrets.local_store(env).names():
            raise ConnectError("needs_login", f"{name} has no password that has logged in yet.", "Fix its login first.")
        p = HOSTS.get(entry.options.host)
        mailcmd.write_profile(name, p.kind if p else "imap", env)
    else:
        mailcmd.remove_profile(name, env)
    return {"mailbox": name, "on": on}


def disconnect(name: str, env: dict | None = None) -> dict:
    """Take the mailbox out of ws: its profile table and its checked mark go, and its stored password too unless the
    table named another secret or another `[mail.*]` table still uses it (a mailbox switched off first, with no
    table left, is forgotten too). It runs under the save lock, so it never interleaves with a connect. The registry entry stays (ws
    never removes one); connecting again re-uses it."""
    entry = _entry_for(name, env)
    with _locked(paths.state_dir(env) / "mail-connect" / "save"):  # never in the middle of a connect's save
        removed = mailcmd.remove_profile(name, env)
        # Forget the value unless the table that went named another secret, or another table still uses this one. No
        # table at all (switched off first) still forgets it: the mailbox is being taken out.
        others = {b.secret for b in mail.load_mailboxes(env).values()}
        mine = removed in (None, entry.name)
        forgot = mine and entry.name not in others and wsecrets.local_store(env).delete(entry.name)
        path = _checked_file(env)
        with _locked(path):
            doc = _checked(env)
            if doc.pop(name, None) is not None:
                _write_recent(path, doc)  # type: ignore[arg-type]
    return {"mailbox": name, "disconnected": True, "password_forgotten": forgot, "registry_entry_kept": entry.name}
