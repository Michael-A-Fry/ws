"""A push with a link a tap opens (ntfy's Click header), the first line of a summary file as the message, and the
reveal link pushed to the phone (`ws secret reveal --notify`)."""

from __future__ import annotations

import http.client
import os
import threading
import time

import pytest

from ws import agentsession, cli, reveal
from ws import secrets as wsecrets
from ws.roles import commands, ntfy

URL = "https://ntfy.example.org/my-topic"
CFG = {"adapter": "ntfy", "url": URL}
DRAFTS = "https://mail.example.org/#drafts"


class Sender:
    def __init__(self):
        self.calls = []

    def __call__(self, url, body, headers):
        self.calls.append((url, body, headers))
        return 200


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "ftp://x.example.org/a",
        "javascript:alert(1)",
        "https://",
        "https://a.example.org/x y",
        "https://a.example.org/\nx",
        "https://a.example.org/" + "x" * 2100,
    ],
)
def test_click_must_be_one_http_link(bad):
    with pytest.raises(ntfy.NotifyError):
        ntfy.click_url(bad)


def test_deliver_sends_the_click_header(env):
    s = Sender()
    ntfy.deliver(CFG, env, "ws", "3 drafts", send=s, click=DRAFTS)
    assert s.calls[0][2]["Click"] == DRAFTS
    ntfy.deliver(CFG, env, "ws", "x", send=s)
    assert "Click" not in s.calls[1][2]


def test_a_bad_click_is_refused_before_anything_is_counted(env):
    with pytest.raises(ntfy.NotifyError):
        ntfy.send(CFG, env, "x", click="nope", sender=Sender())
    assert ntfy.read_counts(env, ntfy.today())["push"] == 0


def test_first_line_skips_blanks_trims_and_caps(tmp_path):
    f = tmp_path / "summary.md"
    f.write_text("\n\n   Inbox:  4 drafts,\t2 labelled   \nmore\n", encoding="utf-8")
    assert commands.first_line(str(f)) == "Inbox: 4 drafts, 2 labelled"
    f.write_text("x" * 1000, encoding="utf-8")
    assert len(commands.first_line(str(f))) == commands.LINE_MAX
    f.write_text("\n \n", encoding="utf-8")
    with pytest.raises(ValueError, match="no line with text"):
        commands.first_line(str(f))
    with pytest.raises(ValueError, match="cannot be read"):
        commands.first_line(str(tmp_path / "missing.md"))


def test_first_line_with_fresh_skips_an_old_file(tmp_path):
    f = tmp_path / "summary.md"
    f.write_text("old news\n", encoding="utf-8")
    hour_ago = time.time() - 3600
    os.utime(f, (hour_ago, hour_ago))
    assert commands.first_line(str(f), fresh_min=30) is None
    assert commands.first_line(str(f), fresh_min=90) == "old news"


def test_cli_sends_a_summary_line_with_a_link(box, write_profile, capsys, monkeypatch, tmp_path):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    f = tmp_path / "summary.md"
    f.write_text("Inbox: 4 drafts ready\n", encoding="utf-8")
    code = cli.main(["notify", "send", "--from-file", str(f), "--fresh", "30", "--click", DRAFTS, "--title", "inbox"])
    assert code == 0, capsys.readouterr().err
    _, body, headers = s.calls[0]
    assert body == b"Inbox: 4 drafts ready" and headers["Click"] == DRAFTS and headers["Title"] == "inbox"


def test_cli_does_not_resend_an_old_summary(box, write_profile, capsys, monkeypatch, tmp_path):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    f = tmp_path / "summary.md"
    f.write_text("yesterday\n", encoding="utf-8")
    old = time.time() - 86400
    os.utime(f, (old, old))
    assert cli.main(["notify", "send", "--from-file", str(f), "--fresh", "120"]) == 0
    assert s.calls == [] and "0 sent" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [["notify", "send"], ["notify", "send", "hi", "--from-file", "x"], ["notify", "send", "hi", "--fresh", "5"]],
)
def test_cli_usage(box, write_profile, capsys, argv):
    write_profile({"roles": {"notify": CFG}})
    assert cli.main(argv) == 2


def test_cli_refuses_a_bad_click(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    s = Sender()
    monkeypatch.setattr(ntfy, "post", s)
    assert cli.main(["notify", "send", "hi", "--click", "javascript:alert(1)"]) == 1
    assert s.calls == []


# ---- the reveal link, pushed ------------------------------------------------------------------------------------------


def test_reveal_notify_pushes_the_link_once_and_still_serves(box, write_profile, capsys, monkeypatch):
    write_profile({"roles": {"notify": CFG}})
    wsecrets.registry_path().parent.mkdir(parents=True, exist_ok=True)
    wsecrets.registry_path().write_text('[backup-password]\nsource = "generate"\n', encoding="utf-8")
    assert cli.main(["secret", "generate", "backup-password"]) == 0
    value = wsecrets.Store(wsecrets.store_dir()).read("backup-password")
    s = Sender()
    pushed = threading.Event()  # the push comes after the page is made: once it is set, both are there

    def post(*a):
        try:
            return s(*a)
        finally:
            pushed.set()

    monkeypatch.setattr(ntfy, "post", post)
    made = []
    real = reveal.make_page
    monkeypatch.setattr(reveal, "make_page", lambda *a, **k: made.append(real(*a, **k)) or made[-1])
    box_ = {}
    t = threading.Thread(
        target=lambda: box_.update(
            code=cli.main(["secret", "reveal", "backup-password", "--allow-loopback", "--notify"])
        ),
        daemon=True,
    )
    t.start()
    # wait on the push itself, not a fixed number of polls: a loaded macOS runner took over 5 s to get there (16 Nov).
    # The cap only stops a hang; a reveal that ends without pushing fails at once with its exit code.
    give_up = time.monotonic() + 120
    while not pushed.wait(0.05):
        assert t.is_alive() or pushed.is_set(), f"the reveal ended without pushing the link (exit {box_.get('code')})"
        assert time.monotonic() < give_up, "the reveal neither pushed the link nor ended"
    page = made[0]
    (_, body, headers) = s.calls[0]
    assert headers["Click"].endswith(f"/{page.token}") and value.encode() not in body
    c = http.client.HTTPConnection("127.0.0.1", page.server.server_address[1], timeout=10)
    c.request("POST", f"/{page.token}", body=b"")
    assert value in c.getresponse().read().decode()
    t.join(10)
    assert box_["code"] == 0 and len(s.calls) == 1


def test_reveal_notify_with_no_notify_role_still_serves(box, write_profile, capsys, monkeypatch):
    wsecrets.registry_path().parent.mkdir(parents=True, exist_ok=True)
    wsecrets.registry_path().write_text('[backup-password]\nsource = "generate"\n', encoding="utf-8")
    assert cli.main(["secret", "generate", "backup-password"]) == 0
    monkeypatch.setattr(reveal.Page, "serve", lambda self, lifetime: "expired")
    assert cli.main(["secret", "reveal", "backup-password", "--allow-loopback", "--notify", "--minutes", "1"]) == 1
    assert "was not pushed" in capsys.readouterr().err


def test_notify_is_allowed_in_the_session_form():
    assert agentsession.session_secret_words(["secret", "reveal", "x", "--notify"], {})
    assert agentsession.session_secret_words(["secret", "reveal", "x", "--notify", "--again", "--minutes", "5"], {})
    assert not agentsession.session_secret_words(["secret", "reveal", "x", "--notify", "--notify"], {})


# ---- the review's cases -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["http://exaĀmple.com/", "https://a.example.org/ ", "https://a.example.org/\xa0"])
def test_click_must_be_ascii(bad):
    with pytest.raises(ntfy.NotifyError):
        ntfy.click_url(bad)


def test_first_line_refuses_what_is_not_a_file_and_bounds_a_long_line(tmp_path):
    with pytest.raises(ValueError, match="not a file"):
        commands.first_line(str(tmp_path))
    f = tmp_path / "one-long-line"
    f.write_text("y" * (commands.LINE_MAX * 50), encoding="utf-8")
    assert commands.first_line(str(f)) == "y" * commands.LINE_MAX


@pytest.mark.parametrize("fresh", ["-1", "0", "nan", "inf"])
def test_fresh_must_be_minutes_above_zero(box, write_profile, tmp_path, fresh):
    write_profile({"roles": {"notify": CFG}})
    f = tmp_path / "s.md"
    f.write_text("x\n", encoding="utf-8")
    assert cli.main(["notify", "send", "--from-file", str(f), "--fresh", fresh]) == 2
