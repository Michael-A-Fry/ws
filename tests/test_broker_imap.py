"""The broker's `imap` kind: the mailbox password stays in the broker, which runs only the fixed OPS table over TLS to the
registered host:port. Against a real TLS fake IMAP server in-process (a test certificate made at run time), no network.
The planted password never reaches a reply, the audit log, stderr or an exception text."""

from __future__ import annotations

import base64
import json
import shutil
import tempfile
from pathlib import Path

import pytest
from helpers.fake_imap import HOST, USER, FakeImap, make_cert
from helpers.stub_secrets import StubSecrets

from ws import broker
from ws import secrets as wsecrets
from ws.minters import imap

ORIGINAL = (
    b"From: Sender <sender@example.com>\r\nTo: someone@example.com\r\nSubject: Lunch?\r\n"
    b"Date: Mon, 2 Nov 2026 10:00:00 +1300\r\nMessage-ID: <m1@example.com>\r\n\r\nAre you free on Friday?\r\n"
)


def draft(user: str = USER, extra: str = "", body: str = imap.DRAFT_MARKER + "\r\n\r\nYes, Friday works.\r\n") -> str:
    raw = (
        f"From: {user}\r\nTo: sender@example.com\r\nSubject: Re: Lunch?\r\nIn-Reply-To: <m1@example.com>\r\n"
        f"References: <m1@example.com>\r\nX-WS-Draft: 1\r\nMIME-Version: 1.0\r\n"
        f'Content-Type: text/plain; charset="utf-8"\r\n{extra}\r\n{body}'
    )
    return base64.b64encode(raw.encode()).decode()


@pytest.fixture(scope="module")
def certs():
    d = Path(tempfile.mkdtemp(prefix="wsi"))
    good = make_cert(d)
    other = make_cert(d, stem="other")  # the same host name, a CA the client does not trust
    yield good, other
    shutil.rmtree(d, ignore_errors=True)


class Box:
    """A broker with one `kind = "imap"` name, pointed at a fake server through the dial patch."""

    def __init__(self, base: Path, cert, monkeypatch, gmail=True, drafts=True, served=None, entry_extra=""):
        self.s = StubSecrets(base)
        self.password = wsecrets_value()
        self.s.registry.write_text(
            f'[mail-a]\nkind = "imap"\nhost = "{HOST}"\nuser = "{USER}"\n{entry_extra}', encoding="utf-8"
        )
        self.s.store.write("mail-a", self.password)
        self.s.values["mail-a"] = self.password
        self.fake = FakeImap(*(served or cert), self.password, gmail=gmail, drafts=drafts)
        self.fake.dial(monkeypatch)
        self.log = base / "logs" / "broker.jsonl"
        self.sock = base / "run" / "b.sock"
        self.broker = broker.Broker(
            self.sock, self.s.registry, self.s.store, log_path=self.log, records=self.s.records,
            elevate_file=self.s.elevate_file,
        )  # fmt: skip
        self.broker.imap_cafile = str(cert[0])
        self.broker.start()

    def call(self, cmd: str, **args) -> dict:
        return broker.imap_call("mail-a", cmd, args, socket=self.sock)

    def refused(self, cmd: str, **args) -> broker.BrokerRefused:
        with pytest.raises(broker.BrokerRefused) as e:
            self.call(cmd, **args)
        return e.value

    def raw_log(self) -> str:
        return self.log.read_text() if self.log.exists() else ""

    def close(self):
        self.broker.stop()
        self.fake.close()


def wsecrets_value() -> str:
    from helpers.stub_secrets import planted_value

    return planted_value()


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="wsi"))  # short: Unix socket paths top out near 100 characters
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def box(base, certs, monkeypatch):
    b = Box(base, certs[0], monkeypatch)
    yield b
    b.close()


def test_the_ops_table_is_exactly_this():
    """Adding an operation (send, say) must change this test: a visible, reviewed change."""
    got = {k: (v.needs, sorted(v.imap)) for k, v in imap.OPS.items()}
    assert got == {
        "list": ("read", ["LIST"]),
        "search": ("read", ["EXAMINE", "UID SEARCH"]),
        "fetch": ("read", ["EXAMINE", "UID FETCH"]),
        "label": ("label", ["SELECT", "UID STORE"]),
        "draft": ("draft", ["APPEND", "LIST"]),
    }
    assert imap.NEEDS == ("read", "label", "draft")
    assert imap.SESSION_COMMANDS == {"CAPABILITY", "LOGIN", "NOOP", "LOGOUT"}
    assert imap.LABELS == ("ws/needs-reply", "ws/fyi", "ws/waiting", "ws/low")


class _Probe:
    """Enough of a session for the command whitelist, with every operation's commands allowed."""

    ws_user = USER
    ws_drafts = '"Drafts"'
    ws_allowed = frozenset().union(*(o.imap for o in imap.OPS.values())) | imap.SESSION_COMMANDS


@pytest.mark.parametrize(
    "name,args",
    [
        ("UID", ["STORE", "4", "+FLAGS", "(\\Deleted)"]),
        ("UID", ["STORE", "4", "-X-GM-LABELS", '("\\\\Inbox")']),
        ("UID", ["STORE", "4", "+X-GM-LABELS", '("\\\\Trash")']),
        ("UID", ["STORE", "4", "FLAGS", "(ws/fyi)"]),
        ("UID", ["STORE", "1:*", "+FLAGS", "(ws/fyi)"]),
        ("UID", ["COPY", "4", '"Trash"']),
        ("UID", ["MOVE", "4", '"Trash"']),
        ("UID", ["EXPUNGE", "4"]),
        ("UID", ["FETCH", "4", "(BODY[])"]),
        ("UID", ["FETCH", "1:*", "(UID FLAGS RFC822.SIZE BODY.PEEK[]<0.1048576>)"]),
        ("UID", ["SEARCH", 'OR ALL TEXT "x"']),
        ("EXPUNGE", []),
        ("CLOSE", []),
        ("DELETE", ['"INBOX"']),
        ("RENAME", ['"INBOX"', '"Old"']),
        ("CREATE", ['"x"']),
        ("COPY", ["1", '"Trash"']),
        ("MOVE", ["1", '"Trash"']),
        ("STORE", ["1", "+FLAGS", "(\\Deleted)"]),
        ("AUTHENTICATE", ["PLAIN"]),
        ("STARTTLS", []),
        ("SETACL", ['"INBOX"', "anyone", "lrswipkxtecda"]),
        ("APPEND", ['"INBOX"', imap.DRAFT_FLAGS]),
        ("APPEND", ['"Drafts"', "(\\Seen)"]),
        ("LIST", ['""', '"%"']),
        ("LOGIN", ["other@example.com", '"x"']),
        ("MAIL FROM", []),
        ("RCPT", []),
        ("DATA", []),
        ("SEND", []),
    ],
)
def test_the_whitelist_refuses_everything_else(name, args):
    with pytest.raises(imap.ImapError) as e:
        imap._check(_Probe(), name, args)
    assert e.value.code == "op_refused"


def test_the_whitelist_takes_what_the_ops_send():
    for name, args in [
        ("UID", ["STORE", "4", "+X-GM-LABELS", '("ws/needs-reply")']),
        ("UID", ["STORE", "4", "+FLAGS", "(ws/low)"]),
        ("UID", ["FETCH", "4", imap._fetch_items(True, "full")]),
        ("UID", ["SEARCH", 'SINCE 1-Nov-2026 UNSEEN X-GM-RAW "from:sender"']),
        ("UID", ["SEARCH", 'SINCE 1-Nov-2026 UID 5:* X-GM-RAW "from:sender"']),
        ("APPEND", ['"Drafts"', imap.DRAFT_FLAGS]),
        ("EXAMINE", ['"INBOX"']),
        ("LIST", ['""', '"*"']),
    ]:
        imap._check(_Probe(), name, args)


def test_an_op_may_send_only_its_own_commands():
    p = _Probe()
    p.ws_allowed = imap.OPS["search"].imap | imap.SESSION_COMMANDS
    with pytest.raises(imap.ImapError):
        imap._check(p, "UID", ["STORE", "4", "+FLAGS", "(ws/low)"])
    with pytest.raises(imap.ImapError):
        imap._check(p, "SELECT", ['"INBOX"'])


def test_search_fetch_and_list_read_without_marking_seen(box):
    m = box.fake.add(ORIGINAL)
    found = box.call("search", since="2026-11-01", query="from:sender")
    assert found["uids"] == [m.uid] and found["uidvalidity"] == 7
    got = box.call("fetch", uid=m.uid, uidvalidity=7)
    assert base64.b64decode(got["raw"]) == ORIGINAL and got["thread"] == "99" and not got["truncated"]
    assert "\\Seen" not in m.flags
    listed = box.call("list")
    assert listed["gmail"] is True and listed["drafts"] == "[Gmail]/Drafts"
    verbs = set(box.fake.verbs())
    assert verbs <= {"CAPABILITY", "LOGIN", "LIST", "EXAMINE", "UID SEARCH", "UID FETCH", "LOGOUT"}
    assert ("UID", 'SEARCH SINCE 1-Nov-2026 X-GM-RAW "from:sender"') in box.fake.log


def test_a_label_is_added_from_the_allowlist_only(box):
    m = box.fake.add(ORIGINAL)
    assert box.call("label", uid=m.uid, uidvalidity=7, label="ws/needs-reply")["label"] == "ws/needs-reply"
    assert m.labels == {"ws/needs-reply"}
    for bad in ("\\Trash", "\\Inbox", "Spam", "ws/other"):
        assert box.refused("label", uid=m.uid, uidvalidity=7, label=bad).code == "bad_request"
    assert m.labels == {"ws/needs-reply"} and len(box.fake.folders["INBOX"].messages) == 1


def test_plain_imap_labels_by_keyword_flag_not_copy(base, certs, monkeypatch):
    b = Box(base, certs[0], monkeypatch, gmail=False)
    try:
        m = b.fake.add(ORIGINAL)
        b.call("label", uid=m.uid, uidvalidity=7, label="ws/fyi")
        assert m.flags == {"ws/fyi"} and "UID COPY" not in b.fake.verbs()
    finally:
        b.close()


def test_a_changed_uidvalidity_is_refused(box):
    m = box.fake.add(ORIGINAL)
    box.fake.folders["INBOX"].uidvalidity = 8
    assert box.refused("fetch", uid=m.uid, uidvalidity=7).code == "uidvalidity_changed"
    assert box.refused("label", uid=m.uid, uidvalidity=7, label="ws/fyi").code == "uidvalidity_changed"
    assert m.labels == set()


def test_a_draft_lands_in_drafts_flagged_draft_and_nothing_else_moves(box):
    m = box.fake.add(ORIGINAL)
    out = box.call("draft", message=draft())
    assert out["folder"] == "[Gmail]/Drafts" and out["uid"] == 1
    d = box.fake.folders["[Gmail]/Drafts"].messages
    assert len(d) == 1 and d[0].flags == {"\\Draft", "\\Seen"} and b"X-WS-Draft: 1" in d[0].raw
    assert box.fake.folders["INBOX"].messages == [m] and m.flags == set()
    assert box.fake.folders["Sent"].messages == [] and box.fake.folders["Trash"].messages == []
    assert set(box.fake.verbs()) <= {"CAPABILITY", "LOGIN", "LIST", "APPEND", "LOGOUT"}


@pytest.mark.parametrize(
    "msg",
    [
        draft(extra="Bcc: evil@example.com\r\n"),
        draft(user="other@example.com"),
        draft(body="no marker\r\n"),
        base64.b64encode(
            b"From: someone@example.com\r\nContent-Type: multipart/mixed; boundary=x\r\n\r\n--x--\r\n"
        ).decode(),
        base64.b64encode(b"x" * (imap.MAX_DRAFT + 1)).decode(),
        "not base64!",
        draft(extra="Content-Type: text/html\r\n"),  # a second Content-Type after text/plain
        draft(extra="To: evil@example.com\r\n"),  # any header twice
        draft(user=f'"{USER}" <other@example.com>'),  # the user only as a display name
        draft(user=f"{USER}, other@example.com"),  # two senders
        base64.b64encode(
            f"From: {USER}\r\nX-WS-Draft: 1\r\nContent-Type: text/plain; charset=bogus\r\n\r\n"
            f"{imap.DRAFT_MARKER}\r\n".encode()
        ).decode(),  # an unknown charset: a refusal, not a crash
    ],
)
def test_a_draft_that_is_not_plain_ours_and_marked_is_refused_before_dialling(box, msg):
    e = box.refused("draft", message=msg)
    assert e.code == "bad_request"
    assert box.fake.folders["[Gmail]/Drafts"].messages == []
    assert box.fake.log == [] and box.fake.dialled == []  # checked before the server was dialled
    statuses = [json.loads(x)["status"] for x in box.raw_log().splitlines()]
    assert statuses == ["begin", "bad_request"]  # the begin line always gets its outcome


def test_a_search_after_a_uid_is_the_oldest_page_above_it(box):
    ms = [box.fake.add(ORIGINAL) for _ in range(imap.MAX_RESULTS + 5)]
    got = box.call("search", since="2026-11-01", after_uid=ms[2].uid)
    want = [m.uid for m in ms[3:]]
    assert got["uids"] == want[: imap.MAX_RESULTS] and got["total"] == len(want)
    assert ("UID", f"SEARCH SINCE 1-Nov-2026 UID {ms[2].uid + 1}:*") in box.fake.log
    assert box.call("search", after_uid=ms[-1].uid)["uids"] == []  # "n:*" names the top uid too: it is left out


@pytest.mark.parametrize(
    "cmd,args",
    [
        ("search", {"since": "22/11/2026"}),
        ("search", {"query": 'a" OR ALL'}),
        ("search", {"folder": 'IN"BOX'}),
        ("search", {"after_uid": "3"}),
        ("search", {"after_uid": -1}),
        ("search", {"after_uid": True}),
        ("fetch", {"uid": "1", "uidvalidity": 7}),
        ("fetch", {"uid": 1, "uidvalidity": 7, "part": "BODY[]"}),
        ("label", {"uid": 1, "uidvalidity": 7, "label": "\\Trash"}),
        ("draft", {}),
    ],
)
def test_bad_arguments_are_refused_before_dialling(box, cmd, args):
    assert box.refused(cmd, **args).code == "bad_request"
    assert box.fake.dialled == [] and box.fake.log == []


def test_no_drafts_folder_is_refused_never_guessed(base, certs, monkeypatch):
    b = Box(base, certs[0], monkeypatch, gmail=False, drafts=False)
    try:
        assert b.refused("draft", message=draft()).code == "no_drafts_folder"
        assert "APPEND" not in b.fake.verbs()
    finally:
        b.close()
    b = Box(base / "2", certs[0], monkeypatch, gmail=False, drafts=False, entry_extra='drafts_folder = "Drafts"\n')
    try:
        assert b.call("draft", message=draft())["folder"] == "Drafts"
    finally:
        b.close()


def test_unknown_ops_and_ops_the_entry_does_not_allow_are_refused(base, certs, monkeypatch):
    b = Box(base, certs[0], monkeypatch, entry_extra='allow = ["read"]\n')
    try:
        for cmd in ("send", "delete", "expunge", "move", "copy", "smtp", "LIST"):
            assert b.refused(cmd).code == "op_refused"
        assert b.refused("draft", message=draft()).code == "op_refused"
        assert b.refused("label", uid=1, uidvalidity=7, label="ws/fyi").code == "op_refused"
        assert b.fake.log == []  # refused before the server was dialled
    finally:
        b.close()


def test_only_the_registered_host_and_port_is_dialled(box):
    box.fake.add(ORIGINAL)
    box.call("search")
    box.call("list")
    assert set(box.fake.dialled) == {(HOST, 993)}


def test_a_tls_failure_refuses_and_sends_nothing(base, certs, monkeypatch):
    b = Box(base, certs[0], monkeypatch, served=certs[1])
    try:
        assert b.refused("list").code == "tls_failed"
        assert b.fake.log == [] and b.password not in b.raw_log()
    finally:
        b.close()


def test_the_password_is_in_no_reply_log_or_error(base, certs, monkeypatch, capsys):
    b = Box(base, certs[0], monkeypatch)
    try:
        b.fake.add(ORIGINAL.replace(b"Friday?", b"Friday? " + b.password.encode()))
        got = b.call("fetch", uid=1, uidvalidity=7)
        assert b.password.encode() not in base64.b64decode(got["raw"])
        b.fake.password = "something-else"  # the server now refuses and echoes the password it was sent
        e = b.refused("list")
        assert e.code == "login_refused" and "AUTHENTICATIONFAILED" in e.reason
        text = json.dumps(got) + str(e) + e.reason + b.raw_log() + "".join(capsys.readouterr())
        assert b.password not in text
        lines = [json.loads(x) for x in b.raw_log().splitlines()]
        assert {x["op"] for x in lines} == {"imap"} and all(x["host"] == HOST for x in lines)
        assert [x["status"] for x in lines] == ["begin", "ok", "begin", "login_refused"]
    finally:
        b.close()


def test_the_registry_entry_is_checked_whole(tmp_path):
    reg = tmp_path / "secrets.toml"
    for bad in (
        'host = "imap.example.com:993"\nuser = "a@example.com"',
        'host = "imap.example.com"\nuser = "a b"',
        'host = "imap.example.com"\nuser = "a@example.com"\nallow = ["send"]',
        'host = "imap.example.com"\nuser = "a@example.com"\nport = 0',
        'host = "imap.example.com"\nuser = "a@example.com"\nhosts = ["x.example.com"]',
    ):
        reg.write_text(f'[m]\nkind = "imap"\n{bad}\n', encoding="utf-8")
        with pytest.raises(wsecrets.SecretsError):
            wsecrets.load_registry(reg)
    reg.write_text('[m]\nkind = "imap"\nhost = "imap.example.com"\nuser = "a@example.com"\n', encoding="utf-8")
    opts = wsecrets.load_registry(reg)["m"].options
    assert opts == imap.Options(host="imap.example.com", user="a@example.com")


def test_a_lease_request_for_a_mailbox_is_refused(box):
    with pytest.raises(broker.BrokerRefused) as e:
        broker.request("mail-a", "", HOST, socket=box.sock)
    assert e.value.code in ("host_not_allowed", "wrong_kind")


def test_modified_utf7_round_trips():
    for s in ("INBOX", "Entwürfe", "Brouillons & co", "下書き", "&"):
        assert imap.decode_mutf7(imap.encode_mutf7(s)) == s
    assert imap.encode_mutf7("Entwürfe") == "Entw&APw-rfe"


@pytest.mark.parametrize(
    "said, token",
    [
        ("[AUTHENTICATIONFAILED] Invalid credentials (Failure)", "bad_credentials"),
        (
            "[ALERT] Application-specific password required: https://support.google.com/x (Failure)",
            "app_password_required",
        ),
        ("[ALERT] Your account is not enabled for IMAP use.", "imap_off"),
        ("[ALERT] Please log in via your web browser: https://support.google.com/x (Failure)", "web_login_required"),
        ("[UNAVAILABLE] try later", "refused"),
    ],
)
def test_a_refused_login_names_why_as_a_fixed_token(said, token):
    assert imap.login_reason(Exception(said)) == token
