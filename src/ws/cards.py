"""`ws cards`: a walled class's account leaves a fixed-kind card (`done`, `counts`...) for the lead's account, across the
OS-account wall, in a spool folder the host's admin makes once; nothing else crosses that way.

Why. A walled class runs as its own OS account (`[classes.NAME] os_user`), and the lead's message store lives under the
lead's home, which is closed (0700) to that account, so `ws msg send` from inside the class has nowhere to write. A card
is the one thing a walled session may send anyway (`msg.FIXED_KINDS`: no body, no key, no thread), so it needs no store,
only a folder both accounts can reach.

The folder. `SPOOL` (`/var/spool/ws-cards`), made by the admin: owned by the lead's account, group `ws-cards` (each
class account is in it, and so is the lead's account), mode 1730 or 3730. So a class account can create a file in it
but can't list it, read another account's card by name it can't guess, or delete one (sticky). `drop` and `take` refuse a
folder that is a link, isn't a folder, has no sticky bit or that other accounts outside the group can reach (fails
closed).

What it promises.
  * `drop(kind)`: the caller is the registered session this process is under (`session.resolve_caller`, as `ws msg`);
    its class, found by its working folder in its own policy, must be walled with an `os_user` that is this process's
    own account, and `kind` one of `msg.FIXED_KINDS`. It writes one card, `<ns>-<16 hex>.card` (mode 0640), under a
    dot name first and renamed, so a half-written card is never taken. A card holds `v`, `kind`, `class`, `session`
    and `at`, nothing else.
  * `take()`: the folder must be owned by this account. Each card is read without following links, must be a regular
    file with one link, at most `CARD_MAX` bytes, with exactly those keys, a fixed kind and a session id; and its class
    is the class whose `os_user` owns the file, by the OS, never by what the card says: a card whose `class` differs
    from its owner's class is rejected. Taken and rejected cards are removed (`keep` leaves them); a dot file older
    than `STALE_S` is removed too. It returns the cards and the rejections (name and reason), never a card's raw text.
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
CARD_MAX = 512
STALE_S = 300
KEYS = {"v", "kind", "class", "session", "at"}
_NAME = re.compile(r"\d{1,20}-[0-9a-f]{16}\.card")


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


def _check_folder(folder: Path, *, owner: bool) -> None:
    """Refuse a spool that a stranger could read or plant in. `owner`: this account must own it (the lead's side)."""
    try:
        st = os.lstat(folder)
    except OSError as e:
        raise _refused(f"there is no card spool at {folder}", "the host's admin makes it once (see `ws cards`)") from e
    mode = stat.S_IMODE(st.st_mode)
    if not stat.S_ISDIR(st.st_mode):
        raise _refused(f"the card spool {folder} is not a folder (or is a link)", "the admin remakes it as a folder")
    if not mode & stat.S_ISVTX or mode & 0o007:
        raise _refused(
            f"the card spool {folder} is mode {oct(mode)}; it needs the sticky bit and nothing for other accounts",
            f"chmod 1730 {folder}",
        )
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
    _check_folder(folder, owner=False)
    at = datetime.fromtimestamp(time.time() if now is None else now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    card = {"v": 1, "kind": kind, "class": cls.name, "session": me.session_id, "at": at}
    name = f"{time.time_ns()}-{secrets.token_hex(8)}.card"
    data = json.dumps(card, sort_keys=True).encode()
    tmp = folder / f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o640)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.rename(tmp, folder / name)
    return {"card": name, "kind": kind, "class": cls.name}


def _read(path: Path, by_user: dict[str, str]) -> tuple[dict | None, str | None]:
    """(the card, None) or (None, why it is rejected)."""
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None, "a link, or not readable"
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
            return None, "not a plain file with one link"
        if st.st_size > CARD_MAX:
            return None, f"bigger than {CARD_MAX} bytes"
        raw = os.read(fd, CARD_MAX + 1)
    finally:
        os.close(fd)
    owner_class = by_user.get(_account(st.st_uid) or "")
    if owner_class is None:
        return None, "its owner is no walled class's account"
    try:
        card = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "not JSON"
    if not isinstance(card, dict) or set(card) != KEYS or card.get("v") != 1:
        return None, "not a card"
    if card["kind"] not in msg.FIXED_KINDS:
        return None, "not a fixed kind"
    if not isinstance(card["session"], str) or not msg._SESSION_ID.fullmatch(card["session"]):
        return None, "no session id"
    if not isinstance(card["at"], str) or len(card["at"]) > 32:
        return None, "no time"
    if card["class"] != owner_class:
        return None, f"says class {str(card['class'])[:40]!r}, but its owner is class {owner_class!r}'s account"
    return card, None


def take(*, folder: Path = SPOOL, keep: bool = False, now: float | None = None) -> dict:
    """The cards in the spool, oldest first, and the rejected ones. See the module docstring."""
    _check_folder(folder, owner=True)
    pol = msg._policy()
    by_user = {c.os_user: c.name for c in pol.classes.values() if c.access == "walled" and c.os_user}
    clock = time.time() if now is None else now
    cards: list[dict] = []
    rejected: list[dict] = []
    for name in sorted(os.listdir(folder)):
        path = folder / name
        if name.startswith("."):
            try:
                if clock - os.lstat(path).st_mtime > STALE_S:
                    path.unlink()
            except OSError:
                pass
            continue
        card, why = (None, "not a card name") if not _NAME.fullmatch(name) else _read(path, by_user)
        if card is not None:
            cards.append({**card, "card": name})
        else:
            rejected.append({"card": name[:80], "reason": why})
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
        for c in result["cards"]:
            print(f"{c['at']}  {c['class']}  {c['kind']}  {c['session']}")
        for r in result["rejected"]:
            print(f"ws: rejected {r['card']}: {r['reason']}", file=sys.stderr)
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
