"""ws.mailconnect: one call that checks an app password with a read-only login, then stores it and adds the mailbox.

Against the in-process TLS fake IMAP server and a real broker (tests/helpers/fake_imap.py, test_mail.Box). A refused
login stores nothing and keeps the value that worked; the refusal is typed and in plain words; three refusals in 15
minutes stop the next try before it dials; a registry entry is added only for a known provider's host; the password
is in no result, error, state file or log."""

from __future__ import annotations

import os
import socket
from pathlib import Path

import pytest
from helpers.fake_imap import HOST, USER
from test_mail import Box, base, cert  # noqa: F401 (base and cert are fixtures)

from ws import mail, mailconnect, profile
from ws import secrets as wsecrets


@pytest.fixture
def box(base, cert, monkeypatch, tmp_path):  # noqa: F811
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    b = Box(base, cert, monkeypatch)
    b.cafile = str(cert[0])
    monkeypatch.setattr(wsecrets, "registry_path", lambda env=None: b.s.registry)
    monkeypatch.setattr(wsecrets, "local_store", lambda env=None: b.s.store)
    monkeypatch.setattr(
        mail.Mail, "from_profile",
        classmethod(lambda cls, env=None, run=None: cls(mail.load_mailboxes(), socket=b.sock, registry=b.s.registry)),
    )  # fmt: skip
    yield b
    b.close()


def boxes() -> dict:
    return (profile.load(profile.profile_path()) or {}).get("mail", {})


def refused(box, *args, **kw) -> mailconnect.ConnectError:
    with pytest.raises(mailconnect.ConnectError) as e:
        mailconnect.connect(*args, cafile=box.cafile, **kw)
    return e.value


def everything_written(box, tmp_path) -> str:
    out = []
    for root in (tmp_path, box.s.base):
        for dirpath, _, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                if "secrets" in p.split(os.sep):  # the store itself: the one place the value belongs
                    continue
                try:
                    out.append(Path(p).read_bytes().decode("utf-8", "replace"))
                except OSError:
                    pass
    return "\n".join(out)


def test_a_good_password_is_stored_and_the_mailbox_added(box):
    box.s.store.delete("mail-a")
    r = mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    assert r == {
        "mailbox": "a", "address": USER, "host": HOST, "folders": 4, "drafts": "[Gmail]/Drafts",
        "registry_added": False, "profile_written": True, "checked": True, "broker_ok": True, "broker_reason": "",
    }  # fmt: skip
    assert box.s.store.read("mail-a") == box.password
    assert boxes() == {"a": {"secret": "mail-a", "kind": "imap"}}
    assert set(box.fake.verbs()) <= {"CAPABILITY", "LOGIN", "LIST", "LOGOUT"}  # read-only


def test_a_refused_password_stores_nothing_and_keeps_the_one_that_works(box, tmp_path):
    wrong = "wrong-" + os.urandom(8).hex()
    e = refused(box, "a", USER, wrong)
    assert e.kind == "bad_credentials" and "app password" in e.hint and "2-Step Verification" in e.hint
    assert box.s.store.read("mail-a") == box.password and boxes() == {}
    assert wrong not in e.message + e.hint + str(e.details)
    assert wrong not in everything_written(box, tmp_path)  # the fake server echoes it; nothing of ours keeps it


@pytest.mark.parametrize(
    "said, kind",
    [
        ("[ALERT] Your account is not enabled for IMAP use.", "imap_off"),
        (
            "[ALERT] Application-specific password required: https://support.google.com/x (Failure)",
            "app_password_required",
        ),
        ("[ALERT] Please log in via your web browser: https://support.google.com/x (Failure)", "web_login_required"),
    ],
)
def test_each_refusal_says_what_to_fix_in_ws_own_words(box, said, kind):
    box.fake.refusal = said
    e = refused(box, "a", USER, box.password)
    assert e.kind == kind and e.message == mailconnect.SAY[kind][0]
    assert "support.google.com" not in e.message + e.hint  # the server's text never reaches the page


def test_three_refusals_in_15_minutes_stop_the_next_try_before_it_dials(box):
    t0 = 1_800_000_000.0
    for i in range(3):
        assert refused(box, "a", USER, "wrong", now=t0 + i).kind == "bad_credentials"
    dialled = len(box.fake.dialled)
    e = refused(box, "a", USER, box.password, now=t0 + 60)
    assert e.kind == "rate_limited" and e.details["retry_minutes"] == 14
    assert len(box.fake.dialled) == dialled  # nothing sent: the provider sees no fourth try
    r = mailconnect.connect("a", USER, box.password, cafile=box.cafile, now=t0 + mailconnect.WINDOW_S + 1)
    assert r["folders"] == 4  # the window passed: the old failures fell out of it


def test_an_unreachable_server_does_not_count_and_stores_nothing(box, monkeypatch):
    box.s.store.delete("mail-a")

    def nowhere(*a, **k):
        raise ConnectionRefusedError

    monkeypatch.setattr(socket, "create_connection", nowhere)
    for _ in range(mailconnect.MAX_FAILS + 1):  # never rate_limited: only a refused login counts
        e = refused(box, "a", USER, box.password)
        assert e.kind == "unreachable"
    assert "mail-a" not in box.s.store.names()


def test_a_known_providers_entry_is_added_and_any_other_host_gets_the_block(box, monkeypatch):
    test = mailconnect.Provider("test", "Test mail", HOST, "imap", "https://example.com", "", ("example.com",))
    monkeypatch.setitem(mailconnect.PROVIDERS, "test", test)
    monkeypatch.setitem(mailconnect.HOSTS, HOST, test)
    before = box.s.registry.read_text()
    e = refused(box, "b", USER, box.password, host="imap.example.org")
    assert e.kind == "needs_registry" and '[mail-b]\nkind = "imap"\nhost = "imap.example.org"' in e.details["add"]
    assert box.s.registry.read_text() == before and boxes() == {}
    r = mailconnect.connect("b", USER, box.password, "test", cafile=box.cafile)
    assert r["registry_added"] is True and box.s.registry.read_text().startswith(before)
    entry = wsecrets.load_registry(box.s.registry)["mail-b"]
    assert (entry.kind, entry.options.host, entry.options.user) == ("imap", HOST, USER)
    assert box.s.store.read("mail-b") == box.password and "b" in boxes()


def test_a_refused_login_for_a_new_mailbox_adds_no_registry_entry(box, monkeypatch):
    test = mailconnect.Provider("test", "Test mail", HOST, "imap", "https://example.com", "", ("example.com",))
    monkeypatch.setitem(mailconnect.PROVIDERS, "test", test)
    monkeypatch.setitem(mailconnect.HOSTS, HOST, test)
    before = box.s.registry.read_text()
    assert refused(box, "b", USER, "wrong", "test").kind == "bad_credentials"
    assert box.s.registry.read_text() == before and "mail-b" not in box.s.store.names()


def test_a_gmail_value_that_is_not_an_app_password_is_refused_before_anything(box):
    before = box.s.registry.read_text()
    e = refused(box, "g", "someone@example.net", "not an app password at all", "gmail")
    assert e.kind == "not_app_password" and "16 letters" in e.message
    assert box.s.registry.read_text() == before and box.fake.dialled == []


@pytest.mark.parametrize(
    "args, kind",
    [
        (("Bad Name", USER, "x"), "bad_name"),
        (("a", "not-an-address", "x"), "bad_address"),
        (("a", USER, ""), "no_password"),
        (("c", "me@example.net", "x"), "pick_provider"),
        (("a", "other@example.com", "x"), "conflict"),
    ],
)
def test_bad_input_is_refused_in_plain_words_with_nothing_dialled(box, args, kind):
    assert refused(box, *args).kind == kind and box.fake.dialled == []


def test_known_lists_the_mail_entries_without_values(box):
    rows = mailconnect.known()
    row = {"name": "a", "address": USER, "host": HOST, "provider": None, "stored": True, "in_profile": False}
    assert mailconnect.known() == [{**row, "checked_at": None}]  # stored another way: unproven
    mailconnect.connect("a", USER, box.password, cafile=box.cafile, now=1_800_000_000.0)
    rows = mailconnect.known()
    assert rows == [{**row, "in_profile": True, "checked_at": 1_800_000_000.0}]
    assert box.password not in str(rows)


def test_off_keeps_the_password_and_on_needs_one_that_logged_in(box):
    with pytest.raises(mailconnect.ConnectError) as no:
        mailconnect.set_on("a", True)
    assert no.value.kind == "needs_login"  # stored another way: never proven
    mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    assert mailconnect.set_on("a", False) == {"mailbox": "a", "on": False}
    assert boxes() == {} and box.s.store.read("mail-a") == box.password
    assert mailconnect.set_on("a", True) == {"mailbox": "a", "on": True}
    assert boxes() == {"a": {"secret": "mail-a", "kind": "imap"}}


def test_disconnect_forgets_the_password_and_keeps_the_registry_entry(box):
    mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    before = box.s.registry.read_text()
    r = mailconnect.disconnect("a")
    assert r == {"mailbox": "a", "disconnected": True, "password_forgotten": True, "registry_entry_kept": "mail-a"}
    assert boxes() == {} and "mail-a" not in box.s.store.names() and box.s.registry.read_text() == before
    assert mailconnect.known()[0]["checked_at"] is None
    with pytest.raises(mailconnect.ConnectError) as e:
        mailconnect.disconnect("nope")
    assert e.value.kind == "not_found"


def _test_provider(monkeypatch):
    test = mailconnect.Provider("test", "Test mail", HOST, "imap", "https://example.com", "", ("example.com",))
    monkeypatch.setitem(mailconnect.PROVIDERS, "test", test)
    monkeypatch.setitem(mailconnect.HOSTS, HOST, test)


def test_a_fresh_name_for_the_same_address_is_still_rate_limited(box, monkeypatch):
    _test_provider(monkeypatch)
    t0 = 1_800_000_000.0
    for i in range(mailconnect.MAX_FAILS):
        assert refused(box, "a", USER, "wrong", now=t0 + i).kind == "bad_credentials"
    dialled = len(box.fake.dialled)
    assert refused(box, "b", USER.upper(), box.password, "test", now=t0 + 10).kind == "rate_limited"
    assert len(box.fake.dialled) == dialled


def test_parallel_posts_cannot_all_pass_the_limit(box, monkeypatch):
    import threading
    import time as _time

    real = mailconnect.imap.run

    def slow(*a, **k):
        _time.sleep(0.3)  # every try is in flight at once
        return real(*a, **k)

    monkeypatch.setattr(mailconnect.imap, "run", slow)
    kinds, lock = [], threading.Lock()

    def one():
        try:
            mailconnect.connect("a", USER, "wrong", cafile=box.cafile)
        except mailconnect.ConnectError as e:
            with lock:
                kinds.append(e.kind)

    threads = [threading.Thread(target=one) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    logins = [v for v in box.fake.verbs() if v == "LOGIN"]
    assert len(logins) <= mailconnect.MAX_FAILS
    assert kinds.count("rate_limited") >= 6 - mailconnect.MAX_FAILS


def test_a_write_that_fails_after_a_good_login_keeps_nothing_half_done(box, monkeypatch):
    _test_provider(monkeypatch)
    before = box.s.registry.read_text()

    def broken(*a, **k):
        raise mailconnect.WsError("io", "the profile can't be written", "check the disk")

    monkeypatch.setattr(mailconnect.mailcmd, "write_profile", broken)
    e = refused(box, "b", USER, box.password, "test")
    assert e.kind == "save_failed" and "nothing was kept" in e.message and "profile" in e.message
    assert box.s.registry.read_text() == before and "mail-b" not in box.s.store.names() and boxes() == {}
    assert box.s.store.read("mail-a") == box.password  # another mailbox's value is untouched


def test_disconnect_keeps_a_password_another_table_still_uses_or_that_the_table_did_not_name(box):
    mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    path = profile.profile_path()
    path.write_text(path.read_text() + '\n[mail.twin]\nsecret = "mail-a"\n', encoding="utf-8")
    assert mailconnect.disconnect("a")["password_forgotten"] is False  # [mail.twin] still reads it
    assert box.s.store.read("mail-a") == box.password
    path.write_text('[mail.a]\nsecret = "mail-other"\n', encoding="utf-8")  # a table for "a" that names another secret
    assert mailconnect.disconnect("a")["password_forgotten"] is False
    assert box.s.store.read("mail-a") == box.password


def test_a_failed_save_puts_back_the_value_that_was_stored_before(box, monkeypatch):
    old = "old-" + os.urandom(8).hex()
    box.s.store.write("mail-a", old)

    def broken(*a, **k):
        raise mailconnect.WsError("io", "the profile can't be written", "")

    monkeypatch.setattr(mailconnect.mailcmd, "write_profile", broken)
    assert refused(box, "a", USER, box.password).kind == "save_failed"
    assert box.s.store.read("mail-a") == old


def test_a_registry_restore_takes_out_only_its_own_block(tmp_path):
    reg = tmp_path / "secrets.toml"
    mine = '[mail-b]\nkind = "imap"\nhost = "imap.gmail.com"\nuser = "b@example.com"\n'
    other = '[mail-c]\nkind = "imap"\nhost = "imap.gmail.com"\nuser = "c@example.com"\n'
    reg.write_text('[mail-a]\nkind = "imap"\nhost = "imap.gmail.com"\nuser = "a@example.com"\n\n' + mine + "\n" + other)
    mailconnect._unappend(reg, mine)
    text = reg.read_text()
    assert "[mail-b]" not in text and "[mail-a]" in text and "[mail-c]" in text


def test_disconnect_after_switching_off_still_forgets_the_password(box):
    mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    mailconnect.set_on("a", False)  # the profile table is gone now
    assert mailconnect.disconnect("a")["password_forgotten"] is True
    assert "mail-a" not in box.s.store.names()


def test_a_broker_that_cant_use_it_yet_is_said_with_its_reason(box, monkeypatch):
    def no_broker(self, name):
        raise mail.MailError("broker_unavailable", "the secrets broker is not reachable")

    monkeypatch.setattr(mail.Mail, "folders", no_broker)
    r = mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    assert r["broker_ok"] is False and r["broker_reason"] == "the secrets broker is not reachable"
    assert box.s.store.read("mail-a") == box.password  # saved: the password checked out


def test_a_check_mark_that_cant_be_written_is_reported_as_saved(box, monkeypatch):
    def full_disk(*a, **k):
        raise OSError("no space")

    monkeypatch.setattr(mailconnect, "_mark_checked", full_disk)
    r = mailconnect.connect("a", USER, box.password, cafile=box.cafile)
    assert r["checked"] is False and r["folders"] == 4 and box.s.store.read("mail-a") == box.password
