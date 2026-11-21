"""`ws cards`: a walled class's account leaves a fixed-kind card (`done`, `counts`...) for the lead's account, across the
OS-account wall, in a spool folder the host's admin makes once; nothing else crosses that way, and no free text at all.

Why. A walled class runs as its own OS account (`[classes.NAME] os_user`), and the lead's message store lives under the
lead's home, which is closed (0700) to that account, so `ws msg send` from inside the class has nowhere to write. A card
is the one thing a walled session may send anyway (`msg.FIXED_KINDS`: no body, no key, no thread), so it needs no store,
only a folder both accounts can reach.

The folder. `SPOOL` (`/var/spool/ws-cards`), made by the admin: owned by the lead's account, group `ws-cards` (each
class account is in it, and so is the lead's account), mode exactly 3730. Setgid gives every card the group `ws-cards`,
so the lead can read it; the group has write and search but not read, so a class account can't list the folder or read
another class's card; sticky stops it deleting one. Any other mode, a link or a non-folder is refused on both sides.

What it promises.
  * `drop(kind)`: the caller is the registered session this process is under (`session.resolve_caller`, as `ws msg`),
    and its id has the UUID shape; its class, found by its working folder in its own policy, must be walled with an
    `os_user` that is this process's own account, and `kind` one of `msg.FIXED_KINDS`. It writes one card,
    `<ns>-<16 hex>.card`, under a dot name first, `fchmod` 0640 whatever the umask, checks the file's group is
    `ws-cards`, and renames it, so a half-written card is never taken. A card holds `v`, `kind`, `class`, `session` (a
    UUID) and `at` (`%Y-%m-%dT%H:%M:%SZ`), nothing else.
  * `take()`: the folder must be owned by this account. Each card is read without following links, must be a regular
    file with one link, group `ws-cards`, at most `CARD_MAX` bytes, with exactly those keys in exactly those shapes; and
    its class is the class whose `os_user` owns the file, by the OS, never by what the card says. Taken and rejected
    cards are removed (`keep` leaves them); a dot file older than `STALE_S` is removed too. It returns the cards and,
    for the rest, a count per reason code (`REASONS`): never a rejected card's name, class or any of its text.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import stat
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from ws import msg, output, policy
from ws.output import WsError

COMMAND = "cards"
SPOOL = Path("/var/spool/ws-cards")
GROUP = "ws-cards"
SPOOL_MODE = 0o3730
CARD_MODE = 0o640
CARD_MAX = 512
STALE_S = 300
KEYS = {"v", "kind", "class", "session", "at"}
AT_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
REASONS = ("name", "link", "file", "group", "size", "owner", "json", "shape", "kind", "session", "at", "class")
_NAME = re.compile(r"\d{1,20}-[0-9a-f]{16}\.card", re.ASCII)  # ASCII: \d would take any script's digits
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_AT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", re.ASCII)


def _refused(message: str, hint: str) -> WsError:
    return WsError("refused", message, hint)


def _account(uid: int | None = None) -> str | None:
    """The account name of `uid` (default: this process's), from the system's user list; None where there is none."""
    try:
        import pwd
    except ImportError:  # not POSIX: there are no class accounts
        return None
    getuid = getattr(os, "getuid", None)
    if uid is None and getuid is None:
        return None
    try:
        return pwd.getpwuid(getuid() if uid is None else uid).pw_name
    except KeyError:
        return None


def _group_gid() -> int | None:
    """The id of the `ws-cards` group from the system's group list; None where there is none."""
    try:
        import grp
    except ImportError:
        return None
    try:
        return grp.getgrnam(GROUP).gr_gid
    except KeyError:
        return None


def _check_folder(folder: Path, *, owner: bool) -> None:
    """Refuse a spool in any mode but 3730. `owner`: this account must own it (the lead's side)."""
    try:
        st = os.lstat(folder)
    except OSError as e:
        raise _refused(f"there is no card spool at {folder}", "the host's admin makes it once (see `ws cards`)") from e
    if not stat.S_ISDIR(st.st_mode):
        raise _refused(f"the card spool {folder} is not a folder (or is a link)", "the admin remakes it as a folder")
    mode = stat.S_IMODE(st.st_mode)
    if mode != SPOOL_MODE:
        raise _refused(
            f"the card spool {folder} is mode {oct(mode)}, not {oct(SPOOL_MODE)} (setgid, sticky, group write only)",
            f"chmod 3730 {folder}",
        )
    gid = _group_gid()
    if gid is None or st.st_gid != gid:
        raise _refused(f"the card spool {folder} isn't in group {GROUP}", f"chgrp {GROUP} {folder}")
    getuid = getattr(os, "getuid", None)
    if owner and (getuid is None or st.st_uid != getuid()):
        raise _refused(f"the card spool {folder} isn't owned by this account", "take cards as the lead's account")


def drop(kind: str, *, folder: Path = SPOOL, now: float | None = None) -> dict:
    """Leave one card of `kind` for the lead. See the module docstring for what is checked."""
    if kind not in msg.FIXED_KINDS:
        raise WsError("bad_input", f"a card is one of {', '.join(msg.FIXED_KINDS)}, not {kind!r}", "", 2)
    me = msg.caller()
    pol = msg._policy()
    cls = policy.class_of(pol, me.cwd)
    account = _account()
    if cls is None or cls.access != "walled" or not cls.os_user:
        raise _refused("only a session of a walled class with an os_user drops cards", "use `ws msg send`")
    if account != cls.os_user:
        raise _refused(
            f"class {cls.name!r} runs as account {cls.os_user!r}, not this one",
            f"start the session as {cls.os_user} with `ws gate launch`",
        )
    if not _UUID.fullmatch(me.session_id):
        raise _refused(
            "this session's id isn't a UUID, and a card carries only a UUID", "drop it from a Claude Code session"
        )
    _check_folder(folder, owner=False)
    at = datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime(AT_FORMAT)
    card = {"v": 1, "kind": kind, "class": cls.name, "session": me.session_id, "at": at}
    name = f"{time.time_ns()}-{secrets.token_hex(8)}.card"
    data = json.dumps(card, sort_keys=True).encode()
    tmp = folder / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), CARD_MODE)
    try:
        os.fchmod(fd, CARD_MODE)  # the umask may have taken the group's read
        if os.fstat(fd).st_gid != _group_gid():
            raise _refused(f"a card in {folder} doesn't take group {GROUP}", f"chmod 3730 {folder} (setgid)")
        os.write(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    os.close(fd)
    os.rename(tmp, folder / name)
    return {"card": name, "kind": kind, "class": cls.name}


def _at_ok(value: object) -> bool:
    if not isinstance(value, str) or not _AT.fullmatch(value):
        return False
    try:
        datetime.strptime(value, AT_FORMAT)
    except ValueError:
        return False
    return True


def _read(path: Path, by_user: dict[str, str], gid: int | None) -> tuple[dict | None, str | None]:
    """(the card, None) or (None, a code from REASONS)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None, "link"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None, "file"
        if gid is None or st.st_gid != gid:
            return None, "group"
        if st.st_size > CARD_MAX:
            return None, "size"
        raw = os.read(fd, CARD_MAX + 1)
    finally:
        os.close(fd)
    owner_class = by_user.get(_account(st.st_uid) or "")
    if owner_class is None:
        return None, "owner"
    try:
        card = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "json"
    if not isinstance(card, dict) or set(card) != KEYS or card.get("v") != 1:
        return None, "shape"
    if card["kind"] not in msg.FIXED_KINDS:
        return None, "kind"
    if not isinstance(card["session"], str) or not _UUID.fullmatch(card["session"]):
        return None, "session"
    if not _at_ok(card["at"]):
        return None, "at"
    if card["class"] != owner_class:
        return None, "class"
    return card, None


def take(*, folder: Path = SPOOL, keep: bool = False, now: float | None = None) -> dict:
    """The cards in the spool, oldest first, and a count per reason code of the rejected ones. See the module docstring."""
    _check_folder(folder, owner=True)
    pol = msg._policy()
    by_user = {c.os_user: c.name for c in pol.classes.values() if c.access == "walled" and c.os_user}
    gid = _group_gid()
    clock = time.time() if now is None else now
    cards: list[dict] = []
    rejected: dict[str, int] = {}
    for name in sorted(os.listdir(folder)):
        path = folder / name
        if name.startswith("."):
            try:
                if clock - os.lstat(path).st_mtime > STALE_S:
                    path.unlink()
            except OSError:
                pass
            continue
        card, why = (None, "name") if not _NAME.fullmatch(name) else _read(path, by_user, gid)
        if card is not None:
            cards.append({**card, "card": name})
        else:
            rejected[why or "file"] = rejected.get(why or "file", 0) + 1
        if not keep:
            try:
                path.unlink()
            except OSError:
                pass
    return {"cards": cards, "rejected": rejected}


def cmd_drop(args: argparse.Namespace) -> int:
    result = drop(args.kind)
    if args.json:
        output.result(result)
    else:
        print(f"left a {result['kind']} card for the lead ({result['class']})")
    return 0


def cmd_take(args: argparse.Namespace) -> int:
    result = take(keep=args.keep)
    if args.json:
        output.result(result)
    else:
        for c in result["cards"]:  # every field here passed its exact shape in `_read`
            print(f"{c['at']}  {c['class']}  {c['kind']}  {c['session']}")
        for code, n in sorted(result["rejected"].items()):
            print(f"ws: rejected {n} card(s): {code}", file=sys.stderr)
        if not result["cards"]:
            print("no cards")
    return 0


def register(sub) -> None:
    p = sub.add_parser("cards", help="fixed-kind cards from a walled class's account to the lead's, across the OS wall")
    s = p.add_subparsers(dest="cards_command", required=True)
    d = s.add_parser("drop", help="leave a card for the lead (a walled class's session, as its own account)")
    d.add_argument("kind", choices=msg.FIXED_KINDS)
    d.set_defaults(func=cmd_drop)
    t = s.add_parser("take", help="read and remove the cards left for this account (the lead's)")
    t.add_argument("--keep", action="store_true", help="leave them in the spool")
    t.set_defaults(func=cmd_take)
