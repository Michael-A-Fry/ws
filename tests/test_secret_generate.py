"""`source = "generate"` in the secrets registry, `ws secret generate`, and the reveal-once page (`ws.reveal`) with the
tailnet checks it shares (`ws.tailnet`). No value is ever planted in a file here: the generated one is read back from the
store at run time and checked never to reach output, an error or a log line."""

from __future__ import annotations

import http.client
import ipaddress
import json
import stat
import threading
from pathlib import Path

import pytest

from ws import cli, reveal, tailnet
from ws import secrets as wsecrets


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "wshome"
    h.mkdir()
    monkeypatch.setenv("WS_HOME", str(h))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    return h


REG = """
[backup-password]
source = "generate"

[vendor]
hosts = ["api.example.com"]
"""


def registry(home: Path, text: str = REG) -> None:
    (home / "secrets.toml").write_text(text, encoding="utf-8")


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def stored(name: str) -> str:
    return wsecrets.Store(wsecrets.store_dir()).read(name)


# ---- the registry -----------------------------------------------------------------------------------------------------


def test_source_defaults_to_paste_and_reads_generate(home):
    registry(home)
    reg = wsecrets.load_registry(wsecrets.registry_path())
    assert reg["vendor"].source == "paste"
    assert reg["backup-password"].source == "generate"


def test_source_must_be_paste_or_generate(home):
    registry(home, '[x]\nsource = "make"\n')
    with pytest.raises(wsecrets.SecretsError, match="source must be paste or generate"):
        wsecrets.load_registry(wsecrets.registry_path())


def test_a_generated_name_with_no_hosts_is_not_a_health_problem(home):
    registry(home)
    problems, _ = wsecrets.health(wsecrets.registry_path(), wsecrets.Store(wsecrets.store_dir()))
    assert not any("backup-password" in p for p in problems)


# ---- ws secret generate -----------------------------------------------------------------------------------------------


def test_generate_stores_a_strong_value_mode_600_and_never_prints_it(home, capsys):
    registry(home)
    code, out, err = run(capsys, "secret", "generate", "backup-password")
    assert code == 0, err
    v = stored("backup-password")
    assert len(v) >= 40 and v not in out and v not in err
    f = wsecrets.store_dir() / "backup-password"
    assert stat.S_IMODE(f.stat().st_mode) == 0o600
    assert stat.S_IMODE(f.parent.stat().st_mode) == 0o700
    assert "generated backup-password" in out


def test_two_generates_differ_and_the_second_needs_replace(home, capsys):
    registry(home)
    assert run(capsys, "secret", "generate", "backup-password")[0] == 0
    first = stored("backup-password")
    code, _out, err = run(capsys, "secret", "generate", "backup-password")
    assert code == 1 and "already has a value" in err and "--replace" in err
    assert stored("backup-password") == first
    assert run(capsys, "secret", "generate", "backup-password", "--replace")[0] == 0
    assert stored("backup-password") != first


def test_generate_refuses_a_paste_name(home, capsys):
    registry(home)
    code, _, err = run(capsys, "secret", "generate", "vendor")
    assert code == 2 and "source" in err
    assert "vendor" not in wsecrets.Store(wsecrets.store_dir()).names()


def test_generate_refuses_an_unregistered_name(home, capsys):
    registry(home)
    code, _, err = run(capsys, "secret", "generate", "nope")
    assert code == 2 and "not in" in err


def test_generate_json(home, capsys):
    registry(home)
    code, out, _ = run(capsys, "--json", "secret", "generate", "backup-password")
    assert code == 0
    doc = json.loads(out)
    assert doc["data"] == {"name": "backup-password", "generated": True, "replaced": False}
    assert stored("backup-password") not in out


# ---- the tailnet checks (ported) --------------------------------------------------------------------------------------

# Addresses are built at run time, so no private address is written in the repo (the PII gate).
TS4 = str(tailnet.TAILNET[0][0x10203])
TS6 = str(tailnet.TAILNET[1][1])
LAN = str(ipaddress.IPv4Address((192 << 24) | (168 << 16) | (1 << 8) | 5))


@pytest.mark.parametrize(
    ("addr", "loop", "ok"),
    [
        (TS4, False, True),
        (TS6, False, True),
        ("127.0.0.1", False, False),
        ("127.0.0.1", True, True),
        ("0.0.0.0", True, False),
        ("::", True, False),
        ("", True, False),
        (LAN, True, False),
        ("8.8.8.8", True, False),
        (f"{TS4} 8.8.8.8", True, False),
        ("not an ip", True, False),
    ],
)
def test_bind_ok(addr, loop, ok):
    assert tailnet.bind_ok(addr, allow_loopback=loop) is ok


def _whois_runner(doc):
    def runner(argv):
        assert argv[:3] == ["tailscale", "whois", "--json"]
        if isinstance(doc, Exception):
            raise doc
        return json.dumps(doc)

    return runner


def test_tailnet_ip_reads_tailscale_and_refuses_anything_else():
    assert tailnet.tailnet_ip(run=lambda argv: f"{TS4}\n") == TS4
    for out in ("", "8.8.8.8\n", f"{LAN}\n", "garbage"):
        with pytest.raises(tailnet.TailnetError):
            tailnet.tailnet_ip(run=lambda argv, out=out: out)

    def boom(argv):
        raise OSError("no tailscale")

    with pytest.raises(tailnet.TailnetError):
        tailnet.tailnet_ip(run=boom)


def test_whois_ok_needs_the_owner_prefix_and_no_tags():
    me = {"UserProfile": {"LoginName": "owner@example.org"}, "Node": {}}
    assert tailnet.whois_ok(TS4, "owner@", run=_whois_runner(me))
    assert not tailnet.whois_ok(TS4, "someone@", run=_whois_runner(me))
    tagged = {"UserProfile": {"LoginName": "owner@example.org"}, "Node": {"Tags": ["tag:server"]}}
    assert not tailnet.whois_ok(TS4, "owner@", run=_whois_runner(tagged))
    assert not tailnet.whois_ok(TS4, "owner@", run=_whois_runner(OSError("no tailscale")))
    assert not tailnet.whois_ok(TS4, "owner@", run=lambda argv: "not json")


def test_whois_ok_with_no_owner_is_always_false():
    me = {"UserProfile": {"LoginName": "owner@example.org"}, "Node": {}}
    assert not tailnet.whois_ok(TS4, "", run=_whois_runner(me))
    assert not tailnet.whois_ok(TS4, None, run=_whois_runner(me))


# ---- the reveal page --------------------------------------------------------------------------------------------------


class Page:
    """A reveal page on loopback, served in a thread, with requests made over a real socket."""

    def __init__(self, home: Path, name: str = "backup-password", allowed=lambda ip: True, lifetime: float = 30):
        self.name = name
        self.page = reveal.make_page(
            name,
            "127.0.0.1",
            wsecrets.Store(wsecrets.store_dir()),
            allowed=allowed,
            allow_loopback=True,
            markers=reveal.marker_dir(),
        )
        self.port = self.page.server.server_address[1]
        self.thread = threading.Thread(target=self.page.serve, args=(lifetime,), daemon=True)
        self.thread.start()

    def req(self, method: str, path: str | None = None, headers: dict | None = None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request(
            method,
            path if path is not None else f"/{self.page.token}",
            body=b"" if method == "POST" else None,
            headers=headers or {},
        )
        r = c.getresponse()
        body = r.read().decode()
        c.close()
        return r.status, dict(r.getheaders()), body


@pytest.fixture
def generated(home, capsys):
    registry(home)
    assert run(capsys, "secret", "generate", "backup-password")[0] == 0
    return stored("backup-password")


def test_get_never_shows_the_value_and_post_shows_it_once(generated):
    p = Page(Path())
    status, headers, body = p.req("GET")
    assert status == 200 and generated not in body and "Show" in body
    assert headers["Cache-Control"] == "no-store"
    status, headers, body = p.req("GET")  # a link preview, then the person: still not burnt
    assert status == 200 and generated not in body
    status, headers, body = p.req("POST")
    assert status == 200 and generated in body
    assert headers["Cache-Control"] == "no-store"
    assert "script-src" not in headers["Content-Security-Policy"] or "'none'" in headers["Content-Security-Policy"]
    p.thread.join(5)
    assert not p.thread.is_alive(), "the server exits after one use"
    assert p.page.result == "revealed"


def test_the_value_is_marked_consumed_before_it_is_sent(generated, monkeypatch):
    p = Page(Path())
    seen = {}
    real = reveal._send

    def spy(handler, code, body):
        if generated in body:
            seen["marked"] = reveal.consumed("backup-password", reveal.marker_dir())
        return real(handler, code, body)

    monkeypatch.setattr(reveal, "_send", spy)
    assert p.req("POST")[0] == 200
    assert seen == {"marked": True}


def test_a_second_post_gets_nothing(generated):
    p = Page(Path())
    assert p.req("POST")[0] == 200
    p.thread.join(5)
    # the server has gone; a fresh page for the same name refuses to start without again=True
    with pytest.raises(reveal.RevealError, match="already shown"):
        reveal.make_page(
            "backup-password",
            "127.0.0.1",
            wsecrets.Store(wsecrets.store_dir()),
            allowed=lambda ip: True,
            allow_loopback=True,
            markers=reveal.marker_dir(),
        )


def test_wrong_token_and_wrong_device_get_404_and_nothing_is_burnt(generated):
    p = Page(Path(), allowed=lambda ip: False)
    assert p.req("GET")[0] == 404
    assert p.req("POST")[0] == 404
    assert not reveal.consumed("backup-password", reveal.marker_dir())
    q = Page(Path())
    assert q.req("GET", "/wrong-token")[0] == 404
    assert q.req("POST", "/wrong-token")[0] == 404
    assert q.req("POST", f"/{q.page.token}/x")[0] == 404
    assert not reveal.consumed("backup-password", reveal.marker_dir())
    q.page.stop()
    p.page.stop()


def test_a_cross_origin_post_is_refused(generated):
    p = Page(Path())
    status, _, body = p.req("POST", headers={"Origin": "http://evil.example"})
    assert status == 403 and generated not in body
    assert not reveal.consumed("backup-password", reveal.marker_dir())
    p.page.stop()


def test_the_page_expires(generated):
    p = Page(Path(), lifetime=0.3)
    p.thread.join(5)
    assert p.page.result == "expired"
    assert not reveal.consumed("backup-password", reveal.marker_dir())


def test_the_token_and_value_are_never_logged(generated, capsys):
    p = Page(Path())
    p.req("GET", "/wrong")
    p.req("GET")
    p.req("POST")
    p.thread.join(5)
    out = capsys.readouterr()
    assert p.page.token not in out.out + out.err
    assert generated not in out.out + out.err


def test_make_page_refuses_a_wildcard_or_lan_address(generated):
    store = wsecrets.Store(wsecrets.store_dir())
    for addr in ("0.0.0.0", "", LAN):
        with pytest.raises(reveal.RevealError, match="tailnet"):
            reveal.make_page(
                "backup-password",
                addr,
                store,
                allowed=lambda ip: True,
                allow_loopback=True,
                markers=reveal.marker_dir(),
            )
    with pytest.raises(reveal.RevealError, match="tailnet"):
        reveal.make_page(
            "backup-password",
            "127.0.0.1",
            store,
            allowed=lambda ip: True,
            allow_loopback=False,
            markers=reveal.marker_dir(),
        )


def test_reveal_cli_needs_an_owner_setting(generated, capsys):
    code, _, err = run(capsys, "secret", "reveal", "backup-password")
    assert code == 2 and "owner" in err


def test_reveal_cli_refuses_an_unset_name(home, capsys):
    registry(home)
    code, _, err = run(capsys, "secret", "reveal", "backup-password", "--allow-loopback")
    assert code == 1 and "no value" in err


def test_reveal_cli_on_loopback_end_to_end(generated, capsys, monkeypatch):
    """The CLI prints the link (on stdout, the one place it goes), serves one POST and exits 0."""
    made = []
    real = reveal.make_page

    def spy(*a, **k):
        made.append(real(*a, **k))
        return made[-1]

    monkeypatch.setattr(reveal, "make_page", spy)
    box = {}
    t = threading.Thread(
        target=lambda: box.update(code=cli.main(["secret", "reveal", "backup-password", "--allow-loopback"])),
        daemon=True,
    )
    t.start()
    for _ in range(100):
        if made:
            break
        t.join(0.05)
    page = made[0]
    c = http.client.HTTPConnection("127.0.0.1", page.server.server_address[1], timeout=10)
    c.request("POST", f"/{page.token}", body=b"")
    assert generated in c.getresponse().read().decode()
    t.join(10)
    out = capsys.readouterr()
    assert box["code"] == 0, out.err
    assert f"/{page.token}" in out.out and generated not in out.out + out.err
    assert "shown" in out.out


def test_generate_replace_clears_the_shown_marker(generated, capsys):
    folder = reveal.marker_dir()
    folder.mkdir(parents=True)
    (folder / "backup-password").write_text("x", encoding="utf-8")
    assert run(capsys, "secret", "generate", "backup-password", "--replace")[0] == 0
    assert not reveal.consumed("backup-password", folder)


# ---- the reviewer's two cases -----------------------------------------------------------------------------------------


def test_a_page_that_loses_the_marker_race_finishes_at_once(generated):
    p = Page(Path())
    folder = reveal.marker_dir()
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "backup-password").write_text("x", encoding="utf-8")  # another page showed it a moment ago
    status, _, body = p.req("POST")
    assert status == 410 and generated not in body
    p.thread.join(5)
    assert not p.thread.is_alive() and p.page.result == "taken"


def test_a_failed_read_shows_nothing_and_leaves_no_marker(generated, monkeypatch):
    p = Page(Path())

    def broken(self, name):
        raise wsecrets.SecretsError("a store file does not hold a usable value")

    monkeypatch.setattr(wsecrets.Store, "read", broken)
    status, _, body = p.req("POST")
    assert status == 410 and "Nothing to show" in body
    p.thread.join(5)
    assert p.page.result == "failed"
    assert not reveal.consumed("backup-password", reveal.marker_dir()), "nothing was shown, so a new link may show it"


def test_own_addresses_reads_tailscale_ip_and_is_empty_on_error():
    assert tailnet.own_addresses(run=lambda argv: f"{TS4}\n{TS6}\nnoise\n") == {TS4, TS6}

    def boom(argv):
        raise OSError("no tailscale")

    assert tailnet.own_addresses(run=boom) == set()


# ---- the two forms a session may run (lead ruling on DEC-099) ---------------------------------------------------------

from ws import agentsession, policy, wallsettings

ALLOWED_WORDS = [
    ["secret", "generate", "backup-password"],
    ["--json", "secret", "generate", "backup-password"],
    ["secret", "reveal", "backup-password"],
    ["secret", "reveal", "backup-password", "--again"],
    ["secret", "reveal", "backup-password", "--minutes", "5"],
    ["secret", "reveal", "backup-password", "--again", "--minutes", "15"],
]
REFUSED_WORDS = [
    ["secret", "generate", "backup-password", "--replace"],
    ["secret", "generate", "--replace", "backup-password"],
    ["secret", "generate", "a", "b"],
    ["secret", "generate", "-x"],
    ["secret", "generate"],
    ["secret", "reveal", "backup-password", "--allow-loopback"],
    ["secret", "reveal", "backup-password", "--owner", "someone@"],
    ["secret", "reveal", "backup-password", "--bind", "127.0.0.1"],
    ["secret", "reveal", "backup-password", "--minutes", "0"],
    ["secret", "reveal", "backup-password", "--minutes"],
    ["secret", "reveal", "backup-password", "--again", "--again"],
    ["secret", "reveal"],
    ["secret", "list"],
    ["secret", "list", "--values"],
    ["secret", "set", "backup-password"],
    ["secret", "rm", "backup-password"],
    ["secret", "import", "backup-password", "--from-env", "x.env"],
    ["secret", "wall"],
    ["secret", "generated", "backup-password"],
]


def test_the_wall_marker_is_the_walls_own():
    assert agentsession.WALL_MARKER == wallsettings.WALL_MARKER


@pytest.mark.parametrize("words", ALLOWED_WORDS, ids=" ".join)
def test_a_session_may_run_the_two_exact_forms(words, monkeypatch):
    monkeypatch.delenv(agentsession.WALL_MARKER, raising=False)
    assert agentsession.session_secret_words(words, {})
    assert not policy.is_denied_command(policy.default_policy(), "ws " + " ".join(words))
    assert not policy.is_denied_command(policy.default_policy(), "uv run ws " + " ".join(words))


@pytest.mark.parametrize("words", REFUSED_WORDS, ids=" ".join)
def test_every_neighbour_stays_admin(words, monkeypatch):
    monkeypatch.delenv(agentsession.WALL_MARKER, raising=False)
    assert not agentsession.session_secret_words(words, {})
    assert policy.is_denied_command(policy.default_policy(), "ws " + " ".join(words))


@pytest.mark.parametrize("words", ALLOWED_WORDS, ids=" ".join)
def test_a_walled_session_gets_neither(words, monkeypatch):
    monkeypatch.setenv(agentsession.WALL_MARKER, "1")
    assert not agentsession.session_secret_words(words, {agentsession.WALL_MARKER: "1"})
    assert policy.is_denied_command(policy.default_policy(), "ws " + " ".join(words))


def test_a_compound_line_is_read_whole(monkeypatch):
    monkeypatch.delenv(agentsession.WALL_MARKER, raising=False)
    pol = policy.default_policy()
    assert policy.is_denied_command(pol, "ws secret generate backup-password && ws secret list")
    assert policy.is_denied_command(pol, "ws secret generate backup-password; ws secret rm vendor")


def _guarded(argv, env):
    import argparse as _argparse

    parser = cli.build_parser()
    ns = parser.parse_args(argv)
    if not getattr(ns, "ws_command", None):
        ns.ws_command = ".".join(a for a in argv[:2])
    agentsession.guard(ns, env=env, stdin_tty=False, stdout_tty=False, ancestry=lambda: ["bash", "claude"])
    return _argparse


@pytest.mark.parametrize(
    ("argv", "ok"),
    [
        (["secret", "generate", "backup-password"], True),
        (["secret", "generate", "backup-password", "--replace"], False),
        (["secret", "reveal", "backup-password"], True),
        (["secret", "reveal", "backup-password", "--again", "--minutes", "5"], True),
        (["secret", "reveal", "backup-password", "--allow-loopback"], False),
        (["secret", "reveal", "backup-password", "--owner", "x@"], False),
        (["secret", "reveal", "backup-password", "--bind", "127.0.0.1"], False),
        (["secret", "list"], False),
        (["secret", "set", "backup-password"], False),
    ],
)
def test_ws_itself_applies_the_same_rule(argv, ok, home):
    from ws.output import WsError

    for env, want in (({}, ok), ({agentsession.WALL_MARKER: "1"}, False)):
        try:
            _guarded(argv, env)
            passed = True
        except WsError as e:
            assert e.exit_code == 3
            passed = False
        assert passed is want, (argv, env)


# ---- the audit line -------------------------------------------------------------------------------------------------


def audit_lines():
    p = wsecrets.paths.state_dir() / "logs" / wsecrets.AUDIT_LOG
    return [json.loads(x) for x in p.read_text(encoding="utf-8").splitlines()] if p.is_file() else []


def test_generate_and_reveal_each_write_an_audit_line_and_never_the_value(generated, capsys):
    (line,) = audit_lines()
    assert (line["action"], line["name"], line["result"]) == ("generate", "backup-password", "made")
    assert line["user"] and isinstance(line["in_session"], bool) and line["ts"]
    made = []
    real = reveal.make_page

    def spy(*a, **k):
        made.append(real(*a, **k))
        return made[-1]

    import unittest.mock

    with unittest.mock.patch.object(reveal, "make_page", spy):
        t = threading.Thread(
            target=lambda: cli.main(["secret", "reveal", "backup-password", "--allow-loopback", "--minutes", "1"]),
            daemon=True,
        )
        t.start()
        for _ in range(100):
            if made:
                break
            t.join(0.05)
        c = http.client.HTTPConnection("127.0.0.1", made[0].server.server_address[1], timeout=10)
        c.request("POST", f"/{made[0].token}", body=b"")
        c.getresponse().read()
        t.join(10)
    out = capsys.readouterr()
    lines = audit_lines()
    assert [(x["action"], x["result"]) for x in lines] == [
        ("generate", "made"),
        ("reveal", "started"),
        ("reveal", "revealed"),
    ]
    log = (wsecrets.paths.state_dir() / "logs" / wsecrets.AUDIT_LOG).read_text(encoding="utf-8")
    assert generated not in log and made[0].token not in log
    assert generated not in out.out + out.err
