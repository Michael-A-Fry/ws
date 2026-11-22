"""IMAP mailbox kind: the broker holds a mailbox password and runs a fixed table of IMAP operations, never send or delete.

Registry entry (the password, for Gmail an app password, is the stored value; paste it with `ws secret ask NAME`):

    [mail-work]
    kind = "imap"
    host = "imap.example.com"
    port = 993                              # optional; implicit TLS only, no STARTTLS
    user = "someone@example.com"
    allow = ["read", "label", "draft"]      # optional; which `needs` of OPS this name may use (default all three)
    drafts_folder = "Drafts"                # optional; used only when the server marks no folder \\Drafts
    purposes = ["search", "fetch"]          # optional; limits the name to these OPS names as well

Why the broker and not the mail tool: a mailbox password also sends mail over SMTP, so it must never reach a process an
agent can steer. Mail is untrusted input (indirect prompt injection), so which actions exist is fixed here, in the
process that holds the password, before any mail is read: the `imap` socket op names one entry of OPS and the broker
opens a TLS session to the registered host:port only, logs in, runs that operation and logs out. Nothing in OPS sends,
deletes, expunges, moves or copies, and there is no way to name another command.

Two locks, both a whitelist. OPS is the only table of operations; each says what it `needs` (an entry's `allow` must
list it) and which IMAP commands it may send. Under it, the session itself refuses any IMAP command that is not in the
current operation's set, and checks each command's arguments (STORE only adds one of LABELS; FETCH only BODY.PEEK of
the whole message or a few headers; APPEND only to the drafts folder found for this session, flagged \\Draft \\Seen).
A later operation (send, say) is one new row in OPS behind its own `needs`, a visible and reviewed change.

What a caller gets back is data only: folder names, uids, raw message bytes (base64, capped) and the folder's
UIDVALIDITY. Every uid is paired with its UIDVALIDITY and an operation on a uid refuses when the folder's has changed.
Reads use EXAMINE and BODY.PEEK, so reading never marks mail seen. A draft must be one plain-text part from the entry's
own user, carry `X-WS-Draft: 1`, start with DRAFT_MARKER and have no Bcc; recipients and threading are the mail tool's
job (ws.mail). The password never appears in a reply, the audit log, an error or an exception text: server text is
never passed on, only a known RFC 5530 response code.

Swap note: the same table works over OAuth (XOAUTH2) by changing `_login`; the OPS rows and their checks stay.
"""

from __future__ import annotations

import base64
import imaplib
import re
import ssl
import time
from collections.abc import Callable
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path

KIND = "imap"
OP = "imap"
EXTRA_KEYS = frozenset({"host", "port", "user", "allow", "drafts_folder"})
NEEDS = ("read", "label", "draft")  # what an entry may `allow`; a new op's need is added here and to OPS
LABELS = ("ws/needs-reply", "ws/fyi", "ws/waiting", "ws/low")  # the only labels or keywords STORE may add
DRAFT_MARKER = "[draft by ws: review before sending]"
DRAFT_FLAGS = "(\\Draft \\Seen)"
MAX_RESULTS = 50  # uids per search, newest first
MAX_FETCH = 1024 * 1024  # bytes of one message
MAX_DRAFT = 32 * 1024  # bytes of one draft: it travels in one request line
SOCKET_TIMEOUT_S = 20.0
CALL_DEADLINE_S = 60.0  # wall clock of one operation, login to logout
HEADER_FIELDS = "FROM TO CC REPLY-TO SUBJECT DATE MESSAGE-ID IN-REPLY-TO REFERENCES"
DRAFT_HEADERS = {
    "from", "to", "cc", "subject", "date", "message-id", "in-reply-to", "references", "mime-version",
    "content-type", "content-transfer-encoding", "x-ws-draft",
}  # fmt: skip
SESSION_COMMANDS = frozenset({"CAPABILITY", "LOGIN", "NOOP", "LOGOUT"})  # every operation may send these
USER_RE = re.compile(r"^[A-Za-z0-9._%+@-]{1,254}$")
FOLDER_RE = re.compile(r"^[^\x00-\x1f\x7f\"\\]{1,200}$")
QUERY_RE = re.compile(r"^[ !#-\[\]-~]{1,200}$")  # printable ASCII without \" and \\
DATE_RE = re.compile(r"^(\d{4})-(\d{2})-(\d{2})$")
SEARCH_RE = re.compile(
    r'^(ALL|SINCE \d{1,2}-[A-Z][a-z]{2}-\d{4})( UNSEEN)?( (?:X-GM-RAW|TEXT) "[ !#-\[\]-~]{1,200}")?$'
)
LOGIN_CODES = frozenset({
    "AUTHENTICATIONFAILED", "AUTHORIZATIONFAILED", "UNAVAILABLE", "ALERT", "CONTACTADMIN", "PRIVACYREQUIRED",
    "LIMIT", "EXPIRED",
})  # fmt: skip
GMAIL_HOSTS = frozenset({"imap.gmail.com", "imap.googlemail.com"})
APP_PASSWORD_RE = re.compile(r"^[A-Za-z]{16}$")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def _fetch_items(gmail: bool, part: str) -> str:
    body = "BODY.PEEK[]" if part == "full" else f"BODY.PEEK[HEADER.FIELDS ({HEADER_FIELDS})]"
    if part == "full":
        body += f"<0.{MAX_FETCH}>"
    extra = " X-GM-THRID X-GM-LABELS" if gmail else ""
    return f"(UID FLAGS RFC822.SIZE{extra} {body})"


FETCH_ITEMS = frozenset(_fetch_items(g, p) for g in (True, False) for p in ("full", "headers"))


class ImapError(Exception):
    """Never carries the password or server text. `code` is a broker refusal code."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Options:
    host: str
    user: str
    port: int = 993
    allow: tuple[str, ...] = NEEDS
    drafts_folder: str | None = None


def parse(path: Path, name: str, t: dict) -> Options:
    from ws.secrets import HOST_RE, SecretsError

    where = f"{path}: [{name}]"
    host = t.get("host")
    if not (isinstance(host, str) and HOST_RE.match(host.lower()) and ":" not in host):
        raise SecretsError(f'{where} host must be the IMAP server\'s name, e.g. "imap.example.com" (no port)')
    port = t.get("port", 993)
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        raise SecretsError(f"{where} port must be a whole number from 1 to 65535 (993 for IMAP over TLS)")
    user = t.get("user")
    if not (isinstance(user, str) and USER_RE.match(user)):
        raise SecretsError(f"{where} user must be the mailbox login, e.g. someone@example.com")
    allow = t.get("allow", list(NEEDS))
    if not (isinstance(allow, list) and allow and all(a in NEEDS for a in allow)):
        raise SecretsError(f"{where} allow must be a list from {', '.join(NEEDS)}")
    drafts = t.get("drafts_folder")
    if drafts is not None and not (isinstance(drafts, str) and FOLDER_RE.match(drafts)):
        raise SecretsError(f"{where} drafts_folder must be a folder name on one line")
    return Options(
        host=host.lower(), user=user, port=port, allow=tuple(n for n in NEEDS if n in allow), drafts_folder=drafts
    )


# ---------------------------------------------------------------- modified UTF-7 (RFC 3501 5.1.3)


def encode_mutf7(s: str) -> str:
    out, run = [], []

    def flush():
        if run:
            b = base64.b64encode("".join(run).encode("utf-16-be")).decode().rstrip("=").replace("/", ",")
            out.append(f"&{b}-")
            run.clear()

    for ch in s:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            run.append(ch)
    flush()
    return "".join(out)


def decode_mutf7(s: str) -> str:
    def one(m: re.Match) -> str:
        b = m.group(1)
        if not b:
            return "&"
        b = b.replace(",", "/")
        return base64.b64decode(b + "=" * (-len(b) % 4)).decode("utf-16-be")

    return re.sub(r"&([A-Za-z0-9+,]*)-", one, s)


def _quoted_folder(name: object) -> str:
    if not (isinstance(name, str) and FOLDER_RE.match(name)):
        raise ImapError("bad_request", "folder must be a folder name on one line")
    enc = encode_mutf7(name)
    if not FOLDER_RE.match(enc):
        raise ImapError("bad_request", "folder name is too long")
    return f'"{enc}"'


# ---------------------------------------------------------------- the session and its command whitelist


def _s(a: object) -> str:
    return a.decode("ascii", "replace") if isinstance(a, bytes) else str(a)


class _Session(imaplib.IMAP4_SSL):
    """IMAP over TLS that sends only the commands `allowed` names, each with arguments it checks."""

    def __init__(self, opts: Options, allowed: frozenset[str], ctx: ssl.SSLContext, deadline: float):
        self.ws_user = opts.user
        self.ws_allowed = allowed | SESSION_COMMANDS
        self.ws_deadline = deadline
        self.ws_drafts: str | None = None  # the quoted drafts folder, once LIST found it
        super().__init__(opts.host, opts.port, ssl_context=ctx, timeout=SOCKET_TIMEOUT_S)

    def _command(self, name, *args):
        _check(self, _s(name).upper(), [_s(a) for a in args if a is not None])
        if time.monotonic() > self.ws_deadline:
            raise ImapError("upstream_unreachable", "the mail server took too long")
        return super()._command(name, *args)


def _check(sess: _Session, name: str, args: list[str]) -> None:
    """Refuse (op_refused) any command or argument outside the current operation's whitelist."""

    def no(why: str) -> None:
        raise ImapError("op_refused", f"IMAP {name} refused: {why}")

    if name not in sess.ws_allowed and not (name == "UID" and args and f"UID {args[0].upper()}" in sess.ws_allowed):
        no("not in this operation's commands")
    if name in ("CAPABILITY", "NOOP", "LOGOUT"):
        if args:
            no("takes no arguments")
    elif name == "LOGIN":
        if len(args) != 2 or args[0].strip('"') != sess.ws_user:
            no("only the entry's own user")
    elif name == "LIST":
        if args != ['""', '"*"']:
            no('only LIST "" "*"')
    elif name in ("EXAMINE", "SELECT"):
        if len(args) != 1 or not (args[0].startswith('"') and args[0].endswith('"') and FOLDER_RE.match(args[0][1:-1])):
            no("one quoted folder")
    elif name == "APPEND":
        if sess.ws_drafts is None or args != [sess.ws_drafts, DRAFT_FLAGS]:
            no("only to the drafts folder, flagged \\Draft \\Seen")
    elif name == "UID":
        sub, rest = args[0].upper(), args[1:]
        if sub == "SEARCH":
            if not SEARCH_RE.match(" ".join(rest)):
                no("search criteria outside SINCE, UNSEEN, TEXT and X-GM-RAW")
        elif sub == "FETCH":
            if len(rest) != 2 or not rest[0].isdigit() or rest[1] not in FETCH_ITEMS:
                no("one uid, BODY.PEEK only")
        elif sub == "STORE":
            ok_value = {f'("{x}")' for x in LABELS} if rest[1:2] == ["+X-GM-LABELS"] else {f"({x})" for x in LABELS}
            if (
                len(rest) != 3
                or not rest[0].isdigit()
                or rest[1] not in ("+X-GM-LABELS", "+FLAGS")
                or (rest[2] not in ok_value)
            ):
                no("adds one ws label to one uid, nothing else")
        else:
            no("only UID SEARCH, FETCH and STORE")
    else:
        no("not a whitelisted command")


def _ok(typ: str, what: str) -> None:
    if typ != "OK":
        raise ImapError("upstream_refused", f"the mail server refused {what}")


def _is_gmail(sess: _Session) -> bool:
    return "X-GM-EXT-1" in getattr(sess, "capabilities", ())


def _open(sess: _Session, folder: object, readonly: bool, uidvalidity: object = None) -> int:
    typ, _ = sess.select(_quoted_folder(folder), readonly=readonly)
    _ok(typ, "opening that folder")
    _, dat = sess.response("UIDVALIDITY")
    try:
        uv = int(dat[0])
    except (TypeError, ValueError, IndexError):
        raise ImapError("upstream_refused", "the mail server sent no UIDVALIDITY") from None
    if uidvalidity is not None and uidvalidity != uv:
        raise ImapError("uidvalidity_changed", "the folder was rebuilt since that uid was read; search again")
    return uv


def _uid_arg(args: dict) -> str:
    uid, uv = args.get("uid"), args.get("uidvalidity")
    for v, k in ((uid, "uid"), (uv, "uidvalidity")):
        if isinstance(v, bool) or not isinstance(v, int) or not 0 < v < 2**32:
            raise ImapError("bad_request", f"{k} must be a whole number from a search")
    return str(uid)


LIST_RE = re.compile(rb'^\((?P<flags>[^)]*)\) (?:"(?:[^"\\]|\\.)*"|NIL) (?P<name>"(?:[^"\\]|\\.)*"|[^\s"]+)$')


def _folders(sess: _Session) -> list[dict]:
    typ, dat = sess.list('""', '"*"')
    _ok(typ, "LIST")
    out = []
    for line in dat:
        m = LIST_RE.match(line) if isinstance(line, bytes) else None
        if not m:
            continue  # a literal name: skipped, never guessed
        raw = m.group("name").decode("ascii", "replace")
        raw = raw[1:-1].replace('\\"', '"').replace("\\\\", "\\") if raw.startswith('"') else raw
        try:
            name = decode_mutf7(raw)
        except ValueError:
            continue
        out.append({"name": name, "flags": m.group("flags").decode("ascii", "replace").split()})
    return out


def _no_args(opts: Options, args: dict) -> dict:
    return {}


def _op_list(sess: _Session, opts: Options, a: dict) -> dict:
    folders = _folders(sess)
    drafts = [f["name"] for f in folders if "\\Drafts" in f["flags"]]
    return {"folders": folders, "gmail": _is_gmail(sess), "drafts": drafts[0] if drafts else opts.drafts_folder}


def _check_search(opts: Options, args: dict) -> dict:
    folder = args.get("folder", "INBOX")
    _quoted_folder(folder)
    since, query, unseen = args.get("since"), args.get("query"), args.get("unseen", False)
    crit = ["ALL"]
    if since is not None:
        m = DATE_RE.match(since) if isinstance(since, str) else None
        if not m or not 1 <= int(m.group(2)) <= 12:
            raise ImapError("bad_request", "since must be a date, YYYY-MM-DD")
        crit = [f"SINCE {int(m.group(3))}-{MONTHS[int(m.group(2)) - 1]}-{m.group(1)}"]
    if not isinstance(unseen, bool):
        raise ImapError("bad_request", "unseen must be true or false")
    if unseen:
        crit.append("UNSEEN")
    if query is not None and not (isinstance(query, str) and QUERY_RE.match(query)):
        raise ImapError("bad_request", 'query must be printable ASCII without " or \\, at most 200 characters')
    return {"folder": folder, "crit": crit, "query": query}


def _op_search(sess: _Session, opts: Options, a: dict) -> dict:
    crit = list(a["crit"])
    if a["query"] is not None:
        crit.append(f'{"X-GM-RAW" if _is_gmail(sess) else "TEXT"} "{a["query"]}"')
    uv = _open(sess, a["folder"], readonly=True)
    typ, dat = sess.uid("SEARCH", " ".join(crit))
    _ok(typ, "the search")
    uids = sorted((int(x) for x in (dat[0] or b"").split() if x.isdigit()), reverse=True)
    return {"folder": a["folder"], "uidvalidity": uv, "uids": uids[:MAX_RESULTS], "total": len(uids)}


META_RE = {
    "flags": re.compile(rb"FLAGS \(([^)]*)\)"),
    "size": re.compile(rb"RFC822\.SIZE (\d+)"),
    "thread": re.compile(rb"X-GM-THRID (\d+)"),
    "labels": re.compile(rb"X-GM-LABELS \(([^)]*)\)"),
}


def _check_uid(args: dict) -> dict:
    folder = args.get("folder", "INBOX")
    _quoted_folder(folder)
    return {"folder": folder, "uid": _uid_arg(args), "uidvalidity": args["uidvalidity"]}


def _check_fetch(opts: Options, args: dict) -> dict:
    part = args.get("part", "full")
    if part not in ("full", "headers"):
        raise ImapError("bad_request", "part must be full or headers")
    return {**_check_uid(args), "part": part}


def _op_fetch(sess: _Session, opts: Options, a: dict) -> dict:
    uid = a["uid"]
    uv = _open(sess, a["folder"], readonly=True, uidvalidity=a["uidvalidity"])
    typ, dat = sess.uid("FETCH", uid, _fetch_items(_is_gmail(sess), a["part"]))
    _ok(typ, "the fetch")
    meta, raw = b"", None
    for item in dat:
        if isinstance(item, tuple):
            meta += item[0]
            raw = item[1] if raw is None else raw
        elif isinstance(item, bytes):
            meta += item
    if raw is None:
        raise ImapError("upstream_refused", "no such message in that folder")
    found = {k: r.search(meta) for k, r in META_RE.items()}
    size = int(found["size"].group(1)) if found["size"] else len(raw)
    out = {
        "folder": a["folder"],
        "uidvalidity": uv,
        "uid": int(uid),
        "raw": base64.b64encode(raw[:MAX_FETCH]).decode(),
        "truncated": size > MAX_FETCH,
        "flags": found["flags"].group(1).decode("ascii", "replace").split() if found["flags"] else [],
    }
    if found["thread"]:
        out["thread"] = found["thread"].group(1).decode()
    if found["labels"]:
        out["labels"] = re.findall(r'"((?:[^"\\]|\\.)*)"|(\S+)', found["labels"].group(1).decode("ascii", "replace"))
        out["labels"] = [decode_mutf7(a or b) for a, b in out["labels"]]
    return out


def _check_label(opts: Options, args: dict) -> dict:
    label = args.get("label")
    if label not in LABELS:
        raise ImapError("bad_request", f"label must be one of {', '.join(LABELS)}")
    return {**_check_uid(args), "label": label}


def _op_label(sess: _Session, opts: Options, a: dict) -> dict:
    uid, label = a["uid"], a["label"]
    _open(sess, a["folder"], readonly=False, uidvalidity=a["uidvalidity"])
    if _is_gmail(sess):
        typ, _ = sess.uid("STORE", uid, "+X-GM-LABELS", f'("{encode_mutf7(label)}")')
    else:
        typ, _ = sess.uid("STORE", uid, "+FLAGS", f"({label})")
    _ok(typ, "the label")
    return {"folder": a["folder"], "uid": int(uid), "label": label}


def check_draft(opts: Options, data: bytes) -> None:
    """Refuse (bad_request) a draft that is not one plain-text part from the entry's user with the ws marker."""
    if len(data) > MAX_DRAFT:
        raise ImapError("bad_request", f"a draft is at most {MAX_DRAFT} bytes")
    try:
        msg = BytesParser(policy=policy.default).parsebytes(data)
        names = [k.lower() for k in msg]
        if set(names) - DRAFT_HEADERS:
            raise ImapError("bad_request", "a draft has only From, To, Cc, Subject, Date and threading headers")
        if len(names) != len(set(names)):
            raise ImapError("bad_request", "a draft has each header at most once")
        if msg.is_multipart() or msg.get_content_type() != "text/plain" or msg.get_filename():
            raise ImapError("bad_request", "a draft is one plain-text part, no attachments")
        if msg.get("X-WS-Draft") != "1":
            raise ImapError("bad_request", "a draft carries X-WS-Draft: 1")
        sender = msg["From"]
        addrs = getattr(sender, "addresses", ()) if sender is not None else ()
        if len(addrs) != 1 or addrs[0].addr_spec.lower() != opts.user.lower():
            raise ImapError("bad_request", "a draft is from the mailbox's own user, one address")
        body = msg.get_content()
    except ImapError:
        raise
    except Exception:  # noqa: BLE001 (a bad charset, encoding or header: the draft is refused, never a crash)
        raise ImapError("bad_request", "the draft could not be read as a plain-text message") from None
    if not body.lstrip("\ufeff").startswith(DRAFT_MARKER):
        raise ImapError("bad_request", "a draft starts with the ws marker line")


def _check_draft_args(opts: Options, args: dict) -> dict:
    message = args.get("message")
    if not isinstance(message, str) or not message:
        raise ImapError("bad_request", "message must be base64 of the draft")
    try:
        data = base64.b64decode(message, validate=True)
    except ValueError:
        raise ImapError("bad_request", "message must be base64 of the draft") from None
    check_draft(opts, data)
    return {"data": data}


def _op_draft(sess: _Session, opts: Options, a: dict) -> dict:
    folders = _folders(sess)
    marked = [f["name"] for f in folders if "\\Drafts" in f["flags"]]
    known = {f["name"] for f in folders}
    folder = marked[0] if marked else (opts.drafts_folder if opts.drafts_folder in known else None)
    if folder is None:
        raise ImapError("no_drafts_folder", "no folder is marked \\Drafts: set drafts_folder on the entry")
    sess.ws_drafts = _quoted_folder(folder)
    typ, dat = sess.append(sess.ws_drafts, DRAFT_FLAGS, None, a["data"])
    _ok(typ, "the draft")
    m = re.search(rb"\[APPENDUID (\d+) (\d+)\]", dat[0] if dat and isinstance(dat[0], bytes) else b"")
    out: dict = {"folder": folder}
    if m:
        out.update(uidvalidity=int(m.group(1)), uid=int(m.group(2)))
    return out


@dataclass(frozen=True)
class Op:
    needs: str  # one of NEEDS; the entry's `allow` must list it
    imap: frozenset[str]  # the IMAP commands it may send, besides SESSION_COMMANDS ("UID X" for a UID command)
    check: Callable[
        [Options, dict], dict
    ]  # checks the caller's args before anything is dialled; the handler gets its result
    handler: Callable[[_Session, Options, dict], dict]


# The only operations. A new one is a new row here (and its need in NEEDS); tests pin this table exactly.
OPS: dict[str, Op] = {
    "list": Op("read", frozenset({"LIST"}), _no_args, _op_list),
    "search": Op("read", frozenset({"EXAMINE", "UID SEARCH"}), _check_search, _op_search),
    "fetch": Op("read", frozenset({"EXAMINE", "UID FETCH"}), _check_fetch, _op_fetch),
    "label": Op("label", frozenset({"SELECT", "UID STORE"}), _check_label, _op_label),
    "draft": Op("draft", frozenset({"LIST", "APPEND"}), _check_draft_args, _op_draft),
}


def clean_value(opts: Options, value: str) -> str:
    """The password as the server wants it, or a SecretsError. Google shows a Gmail app password in four
    groups ("abcd efgh ijkl mnop") and its IMAP login wants the 16 letters alone, so for a Gmail host every whitespace
    character goes (a phone's copy may carry no-break spaces too) and what is left must be 16 letters. Any other host
    gets the value as typed: a password there may hold a space. The error never carries the value."""
    from ws.secrets import SecretsError

    if opts.host.lower() not in GMAIL_HOSTS:
        return value
    v = "".join(value.split())
    if not APP_PASSWORD_RE.match(v):
        raise SecretsError(
            "a Gmail app password is 16 letters (Google shows it as four groups of four); this is not one",
        )
    return v


def _login_code(e: BaseException) -> str:
    """A known RFC 5530 response code from the server's refusal, never its text."""
    m = re.search(r"\[([A-Z-]+)", str(e))
    return f" ({m.group(1)})" if m and m.group(1) in LOGIN_CODES else ""


def run(opts: Options, password: str, cmd: str, args: object, cafile: str | None = None) -> dict:
    """Run one OPS entry against the entry's server and log out. Raises ImapError, whose text never holds the
    password or server text. `cafile` is for tests (a test CA); the command line has no switch for it."""
    op = OPS.get(cmd)
    if op is None:
        raise ImapError("op_refused", f"no IMAP operation {cmd!r}; the operations are {', '.join(OPS)}")
    if op.needs not in opts.allow:
        raise ImapError("op_refused", f"this mailbox does not allow {op.needs}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise ImapError("bad_request", "args must be an object")
    checked = op.check(opts, args)  # before anything is dialled: a bad request never reaches the server
    ctx = ssl.create_default_context(cafile=cafile)
    deadline = time.monotonic() + CALL_DEADLINE_S
    sess = None
    try:
        try:
            sess = _Session(opts, op.imap, ctx, deadline)
        except ssl.SSLError:
            raise ImapError("tls_failed", f"the TLS check of {opts.host} failed; nothing was sent") from None
        except (OSError, imaplib.IMAP4.error):
            raise ImapError("upstream_unreachable", f"cannot reach {opts.host}:{opts.port}") from None
        try:
            sess.login(opts.user, password)
        except imaplib.IMAP4.error as e:
            raise ImapError("upstream_refused", f"the mail server refused the login{_login_code(e)}") from None
        out = op.handler(sess, opts, checked)
        if "raw" in out and password.encode() in base64.b64decode(out["raw"]):  # a server never should; still
            out["raw"] = base64.b64encode(
                base64.b64decode(out["raw"]).replace(password.encode(), b"[removed]")
            ).decode()
        return out
    except ImapError:
        raise
    except ssl.SSLError:
        raise ImapError("tls_failed", "the TLS session failed") from None
    except OSError:
        raise ImapError("upstream_unreachable", f"lost the connection to {opts.host}") from None
    except imaplib.IMAP4.error:
        raise ImapError("upstream_refused", "the mail server refused the command") from None
    except (ValueError, KeyError, IndexError, UnicodeError):
        raise ImapError("upstream_refused", "the mail server's answer could not be read") from None
    finally:
        if sess is not None:
            try:
                sess.logout()
            except Exception:  # noqa: BLE001, S110 (logging out is best effort; the result is already decided)
                pass
