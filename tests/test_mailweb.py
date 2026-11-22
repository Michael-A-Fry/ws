"""`ws mail connect --web` (ws.mailweb): the Connect mail page over ws.mailconnect, on the shared loopback server.

A POST without the page's token or with another Origin calls nothing; a connect answers in place and the password is in
no reply, header or line the server prints; every reply is no-store; the help data parses as JSON."""

from __future__ import annotations

import http.client
import json
import os
import re
import threading

import pytest
from helpers.fake_imap import _REAL_CONNECT, USER
from test_mail import base, cert  # noqa: F401 (fixtures)
from test_mailconnect import box  # noqa: F401 (fixture)

from ws import cli, mailconnect, mailweb


@pytest.fixture
def page(box):  # noqa: F811
    server = mailweb.build_server(token="tok", cafile=box.cafile)
    t = threading.Thread(target=server.serve, daemon=True)
    t.start()
    yield server
    server.close()
    t.join(5)


def call(server, method: str, path: str, body: dict | None = None, *, token: str = "tok", origin: str | None = None):
    c = http.client.HTTPConnection("127.0.0.1", server.port, timeout=30)
    c._create_connection = _REAL_CONNECT  # the fake IMAP server lets the process dial only the mail host
    host = f"127.0.0.1:{server.port}"
    headers = {"Host": host}
    if method == "POST":
        headers |= {"Content-Type": "application/json", "X-Ws-Token": token, "Origin": origin or f"http://{host}"}
    c.request(method, path, json.dumps(body or {}) if method == "POST" else None, headers)
    r = c.getresponse()
    data = r.read().decode()
    c.close()
    return r.status, dict(r.getheaders()), data


def test_the_page_lists_mailboxes_and_its_help_data_parses(page, box):  # noqa: F811
    status, headers, text = call(page, "GET", "/?t=tok")
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert "Connect mail" in text and USER in text and 'type="password"' in text
    raw = re.search(r'<script type="application/json" id="help">(.*?)</script>', text, re.DOTALL).group(1)
    assert json.loads(raw)["gmail"][0] == "https://myaccount.google.com/apppasswords"
    assert box.password not in text


def test_a_post_without_the_token_or_from_another_origin_calls_nothing(page, box, monkeypatch):  # noqa: F811
    calls = []
    monkeypatch.setattr(mailconnect, "connect", lambda *a, **k: calls.append(a) or {})
    body = {"name": "a", "address": USER, "provider": "", "password": box.password}
    assert call(page, "POST", "/connect", body, token="wrong")[0] == 403
    assert call(page, "POST", "/connect", body, origin="http://evil.example")[0] == 403
    assert call(page, "GET", "/")[0] == 403  # the page itself needs the token too
    assert calls == []


def test_a_refused_password_answers_why_and_never_carries_it(page, box, capsys):  # noqa: F811
    wrong = "wrong-" + os.urandom(8).hex()
    status, headers, text = call(
        page, "POST", "/connect", {"name": "a", "address": USER, "provider": "", "password": wrong}
    )
    r = json.loads(text)
    assert status == 200 and r["ok"] is False and r["kind"] == "bad_credentials" and "app password" in r["hint"]
    assert headers["Cache-Control"] == "no-store"
    out = capsys.readouterr()
    assert wrong not in text + str(headers) + out.out + out.err
    assert box.s.store.read("mail-a") == box.password  # the value that works is kept


def test_a_good_password_connects_and_the_reply_has_no_password(page, box, capsys):  # noqa: F811
    box.s.store.delete("mail-a")
    status, _, text = call(
        page, "POST", "/connect", {"name": "a", "address": USER, "provider": "", "password": box.password}
    )
    r = json.loads(text)
    assert status == 200 and r["ok"] is True and r["folders"] == 4 and r["drafts"] == "[Gmail]/Drafts"
    assert box.password not in text + "".join(capsys.readouterr())
    assert box.s.store.read("mail-a") == box.password


def test_a_field_that_is_not_text_is_a_400(page):
    assert call(page, "POST", "/connect", {"name": "a", "address": USER, "provider": "", "password": 5})[0] == 400


def test_connect_without_web_is_a_usage_error(capsys):
    assert cli.main(["mail", "connect"]) == 2
    assert "--web" in capsys.readouterr().err


def test_the_provider_defaults_to_the_address_so_an_outlook_address_never_tries_gmail(page, box):  # noqa: F811
    text = call(page, "GET", "/?t=tok")[2]
    assert '<option value="" selected>From the address</option>' in text
    body = {"name": "work", "address": "someone@outlook.com", "provider": "", "password": "x" * 16}
    r = json.loads(call(page, "POST", "/connect", body)[2])
    assert r["ok"] is False and r["kind"] == "unreachable"  # the test allows only the fake host: proof of where it went
    assert ("outlook.office365.com", 993) in box.fake.dialled and ("imap.gmail.com", 993) not in box.fake.dialled


def test_any_other_ws_error_comes_back_in_plain_words_not_a_500(page, monkeypatch):
    from ws.output import WsError

    def boom(*a, **k):
        raise WsError("io", "The profile can't be written.", "Check the disk.")

    monkeypatch.setattr(mailconnect, "connect", boom)
    status, _, text = call(page, "POST", "/connect", {"name": "a", "address": USER, "provider": "", "password": "x"})
    assert status == 200 and json.loads(text) == {
        "ok": False,
        "kind": "io",
        "message": "The profile can't be written.",
        "hint": "Check the disk.",
    }


def test_an_error_that_is_not_ws_own_is_still_plain_words_not_a_500(page, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("something internal " + "x" * 10)

    monkeypatch.setattr(mailconnect, "connect", boom)
    status, _, text = call(page, "POST", "/connect", {"name": "a", "address": USER, "provider": "", "password": "x"})
    r = json.loads(text)
    assert status == 200 and r["ok"] is False and r["kind"] == "failed" and "RuntimeError" in r["message"]
    assert "something internal" not in text  # the error's own text never reaches the page
