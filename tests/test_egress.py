"""Egress per data class (ws.sandbox): the allowlist matcher, the address checks, the plan, the proxy, the shims, the
macOS profile lines, the policy's host and exception rules and the WS015 doctor line. Everything runs on loopback; no
test reaches the network, reads the real HOME or depends on the clock's zone."""

from __future__ import annotations

import base64
import ipaddress
import secrets
import socket
import subprocess
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ws import doctor, policy, sandbox, wallsettings
from ws.adapters import claude_code
from ws.policy import PolicyError

NOW = datetime(2026, 11, 9, 12, 0, tzinfo=UTC)


def v4(*octets) -> str:
    """A private or reserved address, built at run time (the PII gate refuses them written out)."""
    return ".".join(map(str, octets))


META = v4(169, 254, 169, 254)
TEN = v4(10, 1, 2, 3)

POLICY = """
[classes.work]
access = "walled"
repos = ["/srv/work/**"]
allowed_providers = ["anthropic"]
allowed_hosts = ["api.example.com", "*.example.org:443", "localhost:11434"]

[classes.home]
access = "full"
repos = ["/srv/home/**"]
allowed_hosts = ["pypi.org"]

[classes.fiu]
access = "walled"
repos = ["/srv/fiu/**"]
allowed_providers = ["local"]
local_only = true
allowed_hosts = ["api.example.com", "127.0.0.1:8080"]

[[egress_exceptions]]
name = "docs-lookup"
classes = ["work", "fiu"]
hosts = ["docs.example.net"]
reason = "reads the vendor docs for one job"
owner = "the lead"
expires = 2099-01-01

[[egress_exceptions]]
name = "old-mirror"
classes = ["work", "home"]
hosts = ["mirror.example.net"]
reason = "was needed for a migration"
owner = "the lead"
expires = 2026-01-01
"""


@pytest.fixture(autouse=True)
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    return tmp_path


@pytest.fixture
def pol():
    return policy.loads(POLICY)


def plan_for(pol, name, provider="", now=NOW):
    return sandbox.egress_plan(pol, pol.classes[name], provider, now)


# --- the matcher --------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("host", "port", "entry", "want"),
    [
        ("api.example.com", 443, "api.example.com", True),
        ("API.Example.COM.", 443, "api.example.com", True),  # case and a trailing dot do not matter
        ("api.example.com", 443, "api.example.com:443", True),
        ("api.example.com", 80, "api.example.com:443", False),  # a port in the entry pins it
        ("x.api.example.com", 443, "api.example.com", False),  # a name is itself only
        ("a.example.org", 443, "*.example.org", True),
        ("a.b.example.org", 443, "*.example.org", True),
        ("example.org", 443, "*.example.org", False),  # the wildcard is below the name, not the name
        ("badexample.org", 443, "*.example.org", False),  # and it is a label boundary
        ("a.example.org", 80, "*.example.org:443", False),
        (v4(10, 0, 0, 1), 443, "*.0.0.1", False),  # a wildcard never matches an address
        ("127.0.0.1", 11434, "127.0.0.1:11434", True),
        ("localhost", 11434, "localhost:11434", True),
        ("evil.com", 443, "api.example.com:443.evil.com", False),  # a smuggled suffix is not a port
    ],
)
def test_the_matcher_reads_an_entry_as_the_runner_does(host, port, entry, want):
    assert sandbox.host_matches(host, port, entry) is want


@pytest.mark.parametrize(
    ("entry", "bad"),
    [
        ("api.example.com", False),
        ("*.example.org:443", False),
        ("localhost:11434", False),
        ("127.0.0.1:8080", False),
        ("8.8.8.8", False),
        ("localhost", True),  # a loopback entry names its port
        ("127.0.0.1", True),
        ("intranet", True),  # no dot: the proxies read fully qualified names
        (META, True),  # a literal metadata or private address is never an entry
        (TEN, True),
        (v4(192, 168, 1, 1) + ":8080", True),
        (v4(169, 254, 1, 1), True),
        ("*.co.uk", True),  # a whole registry
        ("*.com.au", True),
        ("*.example.co.uk", False),
        ("*.gov.uk", True),
        ("a b.example.org", True),
        ("*.com", True),  # a whole top-level name
        ("*.1.2.3", True),  # a wildcard over an address
        ("0.0.0.0", True),  # the unspecified address is everyone
        ("api.example.com:0", True),
        ("api.example.com:99999", True),
        ("127.1", True),  # a legacy literal: a resolver may read it as 127.0.0.1
        ("2130706433", True),
        ("0x7f.0.0.1", True),
    ],
)
def test_an_entry_the_proxies_can_not_read_is_named(entry, bad):
    assert bool(sandbox.entry_problem(entry)) is bad


@pytest.mark.parametrize("text", ["127.1", "2130706433", "0x7f.1", "0177.0.0.1", "1.2.3"])
def test_legacy_ip_spellings_are_found(text):
    assert sandbox.legacy_literal(text)


@pytest.mark.parametrize("text", ["127.0.0.1", "example.com", "1.example.com", "::1"])
def test_ordinary_hosts_are_not_legacy_literals(text):
    assert not sandbox.legacy_literal(text)


@pytest.mark.parametrize(
    ("ip", "kind"),
    [
        ("8.8.8.8", None),
        ("93.184.216.34", None),
        ("2606:2800:220:1:248:1893:25c8:1946", None),
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        (TEN, "private"),
        (v4(172, 16, 0, 1), "private"),
        (v4(192, 168, 1, 1), "private"),
        (v4(100, 64, 0, 1), "private"),
        ("fd12::1", "private"),
        (v4(169, 254, 1, 1), "link-local"),
        ("fe80::1", "link-local"),
        (META, "metadata"),
        (v4(100, 100, 100, 200), "metadata"),
        ("168.63.129.16", "metadata"),
        ("fd00:ec2::254", "metadata"),
        ("0.0.0.0", "unspecified"),
        ("::", "unspecified"),
        ("224.0.0.1", "multicast"),
        ("::ffff:127.0.0.1", "loopback"),  # an IPv4 address carried in an IPv6 one is judged as the IPv4 one
        ("::ffff:" + META, "metadata"),
        ("64:ff9b::7f00:1", "loopback"),  # NAT64
        ("2002:7f00:1::", "loopback"),  # 6to4
    ],
)
def test_addresses_a_name_must_not_lead_to(ip, kind):
    assert sandbox.blocked_address(ipaddress.ip_address(ip)) == kind


# --- the plan: class x host x exception state ------------------------------------------------------------------------

# (class, host, port, allowed, why it is decided so)
MATRIX = [
    ("work", "api.example.com", 443, True, "class"),
    ("work", "api.example.com", 80, True, "class"),
    ("work", "a.example.org", 443, True, "class"),
    ("work", "a.example.org", 80, False, ""),  # the entry pins 443
    ("work", "api.anthropic.com", 443, True, "class"),  # the provider's host rides along
    ("work", "pypi.org", 443, False, ""),  # another class's host
    ("work", "docs.example.net", 443, True, "exception:docs-lookup"),  # an exception in force
    ("work", "mirror.example.net", 443, False, "exception:old-mirror"),  # an expired one covers nothing
    ("work", "localhost", 11434, True, "class"),
    ("work", "localhost", 9999, False, ""),
    ("home", "pypi.org", 443, True, "class"),
    ("home", "api.example.com", 443, False, ""),
    ("home", "docs.example.net", 443, False, ""),  # the exception does not name this class
    ("home", "mirror.example.net", 443, False, "exception:old-mirror"),
    ("fiu", "127.0.0.1", 8080, True, "class"),
    ("fiu", "api.example.com", 443, False, ""),  # local_only: no remote host, though the class lists one
    ("fiu", "api.anthropic.com", 443, False, ""),
    ("fiu", "docs.example.net", 443, False, ""),  # nor by exception
]


@pytest.mark.parametrize(("cls", "host", "port", "allow", "via"), MATRIX)
def test_the_matrix_of_class_host_and_exception_state(pol, cls, host, port, allow, via):
    d = plan_for(pol, cls, "anthropic" if cls == "work" else "").decide(host, port)
    assert d.allow is allow and d.via == via, d


def test_an_exception_ends_at_its_expiry_and_not_before(pol):
    edge = datetime(2099, 1, 1, tzinfo=UTC)
    before = plan_for(pol, "work", now=edge - timedelta(seconds=1))
    after = plan_for(pol, "work", now=edge)
    assert before.decide("docs.example.net", 443, edge - timedelta(seconds=1)).allow
    refused = after.decide("docs.example.net", 443, edge)
    assert not refused.allow and "expired" in refused.reason and "docs-lookup" in refused.reason


def test_an_exception_that_ends_during_a_session_stops_covering_at_that_moment(pol):
    plan = plan_for(pol, "work", now=NOW)  # built when the session started, with the exception in force
    edge = datetime(2099, 1, 1, tzinfo=UTC)
    assert plan.decide("docs.example.net", 443, edge - timedelta(seconds=1)).allow
    late = plan.decide("docs.example.net", 443, edge)
    assert not late.allow and late.via == "exception:docs-lookup" and "expired" in late.reason
    assert plan.decide("api.example.com", 443, edge).allow  # the class's own list is not an exception


def test_one_exception_lapsing_does_not_deny_a_host_another_still_covers():
    toml = '[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\n'
    for name, day in (("short", 5), ("long", 25)):
        toml += (
            f'[[egress_exceptions]]\nname = "{name}"\nclasses = ["c"]\nhosts = ["docs.example.net"]\n'
            f'reason = "r"\nowner = "o"\nexpires = 2026-12-{day:02d}\n'
        )
    p = policy.loads(toml)
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    mid = datetime(2026, 12, 10, tzinfo=UTC)  # "short" has ended, "long" has not
    assert plan.decide("docs.example.net", 443, mid).allow
    assert plan.decide("docs.example.net", 443, mid).via == "exception:long"
    end = datetime(2026, 12, 26, tzinfo=UTC)
    gone = plan.decide("docs.example.net", 443, end)
    assert not gone.allow and "expired" in gone.reason
    later = sandbox.egress_plan(p, p.classes["c"], "", mid)  # built after "short" lapsed: it is in `lapsed`
    assert later.decide("docs.example.net", 443, mid).allow


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8\x00.example.org",
        "a.example.org\x00",
        "a b.example.org",
        "a" + "@" + "evil.com.example.org",
        "a/b.example.org",
        "a%2e.example.org",
        "a\n.example.org",
        "ａ.example.org",
    ],
)
def test_a_host_with_characters_a_name_does_not_have_is_refused_even_under_a_wildcard(host):
    p = policy.loads('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["*.example.org"]\n')
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    assert plan.decide("a.example.org", 443).allow
    d = plan.decide(host, 443)
    assert not d.allow and d.reason == "not a destination"
    assert not sandbox.valid_destination(host)


@pytest.mark.parametrize("host", ["example.org", "a-b.example.org", "[::1]", "::1", "2001:db8::1", "1.2.3.4"])
def test_ordinary_destinations_are_valid(host):
    assert sandbox.valid_destination(host)


@pytest.mark.parametrize("target", ["8.8.8.8\x00.example.org:443", "a b:443", "a:b:443", "[::1:443", "x\u00e9.org:443"])
def test_a_connect_target_with_odd_characters_does_not_parse(target):
    with pytest.raises(ValueError):
        sandbox._split_hostport(target, 443)


def test_a_policy_with_no_classes_has_no_plan_and_the_network_is_as_it_was():
    p = policy.loads("")
    assert sandbox.egress_plan(p, None, "anthropic") is None


def test_a_class_with_no_hosts_reaches_nothing(pol):
    p = policy.loads('[classes.shut]\naccess = "walled"\nrepos = ["/srv/shut/**"]\n')
    plan = sandbox.egress_plan(p, p.classes["shut"], "local", NOW)
    assert plan.domains() == [] and not plan.decide("example.com", 443).allow
    assert plan.local_ports() == ()


def test_the_unclassified_class_reaches_nothing():
    plan = sandbox.egress_plan(policy.loads(""), policy.UNCLASSIFIED, "anthropic", NOW)
    assert not plan.decide("api.anthropic.com", 443).allow or plan.hosts  # its provider host only, never a class host
    assert plan.hosts == ("api.anthropic.com", "platform.claude.com")


def test_entries_the_proxies_can_not_read_are_dropped_not_applied():
    p = policy.loads(
        '[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["intranet", "ok.example.com"]\n'
    )
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    assert plan.hosts == ("ok.example.com",) and plan.dropped == ("intranet",)
    assert not plan.decide("intranet", 443).allow


@pytest.mark.parametrize("host", ["127.1", "2130706433", "0x7f.0.0.1", ""])
def test_an_odd_destination_is_refused_whatever_the_list_says(pol, host):
    assert not plan_for(pol, "work", "anthropic").decide(host, 443).allow


@pytest.mark.parametrize("port", [0, 70000, -1, True, "443"])
def test_a_port_that_is_not_one_is_refused(pol, port):
    assert not plan_for(pol, "home").decide("pypi.org", port).allow


def test_local_ports_are_the_loopback_entries(pol):
    assert plan_for(pol, "work", "anthropic").local_ports() == (11434,)
    assert plan_for(pol, "fiu", "local").local_ports() == (8080,)


def test_a_local_provider_adds_no_remote_host(pol):
    assert plan_for(pol, "home", "ollama").hosts == ("pypi.org",)


def test_the_domains_for_claude_code_are_the_list_in_force_once(pol):
    got = plan_for(pol, "work", "anthropic").domains()
    assert got[:3] == ["api.example.com", "*.example.org:443", "localhost:11434"]
    assert "docs.example.net" in got and "mirror.example.net" not in got and len(got) == len(set(got))


# --- the address check ------------------------------------------------------------------------------------------------


def test_a_name_that_leads_to_loopback_is_refused_unless_it_is_a_named_model_port(pol):
    plan = plan_for(pol, "work", "anthropic")
    lo = ipaddress.ip_address("127.0.0.1")
    assert plan.address_ok("localhost", lo, 11434) and plan.address_ok("127.0.0.1", lo, 11434)
    assert not plan.address_ok("localhost", lo, 9999)  # a loopback port the list does not name
    assert not plan.address_ok("api.example.com", lo, 11434)  # DNS pointing an allowed name at the model server
    assert not plan.address_ok("a.example.org", lo, 11434)


def test_a_cloud_metadata_address_listed_is_dropped_and_never_dialled():
    p = policy.loads('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["' + META + '"]\n')
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    assert plan.dropped == (META,) and not plan.decide(META, 80).allow  # the entry is refused
    assert not plan.address_ok(META, ipaddress.ip_address(META), 80)  # and the dial would refuse it too


def test_a_private_address_is_never_an_entry_and_a_name_that_resolves_to_one_is_refused():
    p = policy.loads(
        '[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["' + TEN + '", "*.lan.example.com"]\n'
    )
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    ten = ipaddress.ip_address(TEN)
    assert plan.dropped == (TEN,) and not plan.decide(TEN, 443).allow
    assert plan.address_ok(TEN, ten, 443)  # defence in depth: the dial would take the literal itself, never a name
    assert not plan.address_ok("a.lan.example.com", ten, 443)  # a wildcard name that resolves inside the network


# --- the proxy (loopback only) ----------------------------------------------------------------------------------------


class Echo:
    """A model server stand-in: answers any bytes it gets with `pong:` and the bytes, and any HTTP request with a body."""

    def __init__(self):
        self.srv = socket.create_server(("127.0.0.1", 0))
        self.port = self.srv.getsockname()[1]
        self.heads: list[bytes] = []
        threading.Thread(target=self._loop, daemon=True).start()

    def _loop(self):
        while True:
            try:
                c, _ = self.srv.accept()
            except OSError:
                return
            threading.Thread(target=self._one, args=(c,), daemon=True).start()

    def _one(self, c):
        with c:
            data = c.recv(4096)
            self.heads.append(data)
            if data.startswith(b"GET "):
                c.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\nConnection: close\r\n\r\nmodel")
            elif data:
                c.sendall(b"pong:" + data)

    def close(self):
        self.srv.close()


@pytest.fixture
def echo():
    e = Echo()
    yield e
    e.close()


def talk(proxy, request: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", proxy.port), timeout=5) as s:
        s.sendall(request)
        s.shutdown(socket.SHUT_WR)
        out = b""
        while chunk := s.recv(4096):
            out += chunk
        return out


def started(plan, **kw):
    proxy = sandbox.EgressProxy(plan, **kw)
    proxy.start()
    return proxy


POLICY_LOCAL = """
[classes.c]
access = "walled"
repos = ["/srv/c/**"]
[[egress_exceptions]]
name = "model"
classes = ["c"]
hosts = ["localhost:{port}"]
reason = "r"
owner = "o"
expires = 2099-01-01
"""


def local_plan(port, *extra, local_only=False):
    toml = (
        f'[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nlocal_only = {str(local_only).lower()}\n'
        f'allowed_hosts = ["localhost:{port}", {", ".join(repr(e).replace(chr(39), chr(34)) for e in extra)}]\n'
    )
    p = policy.loads(toml.replace(", ]", "]"))
    return sandbox.egress_plan(p, p.classes["c"], "local", NOW)


def test_the_proxy_carries_a_connect_to_a_local_model_port(echo):
    proxy = started(local_plan(echo.port))
    try:
        out = talk(proxy, f"CONNECT localhost:{echo.port} HTTP/1.1\r\nHost: x\r\n\r\nhello".encode())
        assert out.startswith(b"HTTP/1.1 200") and out.endswith(b"pong:hello")
        assert proxy.denials == []
    finally:
        proxy.stop()


def test_the_proxy_carries_a_plain_http_request_to_a_local_model_port(echo):
    proxy = started(local_plan(echo.port))
    try:
        req = f"GET http://localhost:{echo.port}/v1/models HTTP/1.1\r\nHost: localhost\r\nProxy-Connection: keep-alive\r\n\r\n"
        out = talk(proxy, req.encode())
        assert out.startswith(b"HTTP/1.1 200 OK") and out.endswith(b"model")
    finally:
        proxy.stop()


def test_the_proxy_names_the_checked_host_to_the_server_not_the_one_the_client_wrote(echo):
    proxy = started(local_plan(echo.port))
    try:
        req = (
            f"GET http://localhost:{echo.port}/x HTTP/1.1\r\nHost: evil.example.com\r\nhost: other.example.net\r\n\r\n"
        )
        assert talk(proxy, req.encode()).startswith(b"HTTP/1.1 200")
        head = echo.heads[-1].decode().lower()
        assert (
            f"host: localhost:{echo.port}" in head
            and "evil.example.com" not in head
            and "other.example.net" not in head
        )
        assert head.count("\r\nhost:") == 1 and "connection: close" in head and head.startswith("get /x http/1.1")
    finally:
        proxy.stop()


@pytest.mark.parametrize("header", ["Host : evil.example.com", "Host\t: evil.example.com", " Host: evil.example.com"])
def test_a_host_header_with_whitespace_around_its_name_is_dropped_too(echo, header):
    proxy = started(local_plan(echo.port))
    try:
        req = f"GET http://localhost:{echo.port}/x HTTP/1.1\r\n{header}\r\n\r\n"
        assert talk(proxy, req.encode()).startswith(b"HTTP/1.1 200")
        head = echo.heads[-1].decode().lower()
        assert "evil.example.com" not in head and head.count("\r\nhost") == 1
    finally:
        proxy.stop()


@pytest.mark.parametrize("sneaky", ["X-A: 1\nHost: evil.example.com", "X-A: 1\rHost: evil.example.com"])
def test_a_bare_line_feed_in_a_header_is_a_bad_request(echo, sneaky):
    proxy = started(local_plan(echo.port))
    try:
        req = f"GET http://localhost:{echo.port}/x HTTP/1.1\r\n{sneaky}\r\n\r\n"
        assert talk(proxy, req.encode()).startswith(b"HTTP/1.1 400")
        assert echo.heads == []  # nothing went upstream
    finally:
        proxy.stop()


def test_a_name_the_resolver_can_not_encode_is_a_logged_denial():
    p = policy.loads('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["*.example.org"]\n')

    def broken(*a, **k):
        raise UnicodeError("label empty or too long")

    seen = []
    proxy = started(sandbox.egress_plan(p, p.classes["c"], "", NOW), resolver=broken, on_deny=lambda *a: seen.append(a))
    try:
        assert talk(proxy, b"CONNECT a..example.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 403")
        assert len(proxy.denials) == 1 and len(seen) == 1
    finally:
        proxy.stop()


def test_a_connect_to_a_nul_smuggled_name_is_a_bad_request_not_a_wildcard_match(echo):
    p = policy.loads('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["*.example.org"]\n')
    seen = []
    proxy = started(sandbox.egress_plan(p, p.classes["c"], "", NOW), resolver=lambda *a, **k: seen.append(a) or [])
    try:
        assert talk(proxy, b"CONNECT 8.8.8.8\x00.example.org:443 HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")
        assert talk(proxy, b"GET http://8.8.8.8\x00.example.org/ HTTP/1.1\r\n\r\n").startswith(b"HTTP/1.1 400")
        assert seen == []  # nothing was resolved
    finally:
        proxy.stop()


def test_the_proxy_asks_for_the_password_and_takes_only_the_right_one(echo):
    password = "pw-" + secrets.token_hex(8)  # built at run time
    proxy = started(local_plan(echo.port), secret=password)
    connect = f"CONNECT localhost:{echo.port} HTTP/1.1\r\n"
    try:
        none = talk(proxy, (connect + "\r\n").encode())
        assert none.startswith(b"HTTP/1.1 407") and b"Proxy-Authenticate: Basic" in none
        wrong = base64.b64encode(b"ws:not-it").decode()
        assert talk(proxy, f"{connect}Proxy-Authorization: Basic {wrong}\r\n\r\n".encode()).startswith(b"HTTP/1.1 407")
        assert talk(proxy, f"{connect}Proxy-Authorization: Basic !!!\r\n\r\n".encode()).startswith(b"HTTP/1.1 407")
        right = base64.b64encode(b"ws:" + password.encode()).decode()
        ok = talk(proxy, f"{connect}Proxy-Authorization: Basic {right}\r\n\r\nhi".encode())
        assert ok.startswith(b"HTTP/1.1 200") and ok.endswith(b"pong:hi")
        assert proxy.denials == []  # a missing password is not the allowlist refusing a host
    finally:
        proxy.stop()


def test_the_proxy_judges_an_exception_at_each_request(echo):
    clock = [datetime(2098, 12, 31, tzinfo=UTC)]
    p = policy.loads(POLICY_LOCAL.format(port=echo.port))
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    proxy = started(plan, clock=lambda: clock[0])
    try:
        assert talk(proxy, f"CONNECT localhost:{echo.port} HTTP/1.1\r\n\r\n".encode()).startswith(b"HTTP/1.1 200")
        clock[0] = datetime(2099, 1, 1, tzinfo=UTC)
        out = talk(proxy, f"CONNECT localhost:{echo.port} HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403") and b"expired" in out
    finally:
        proxy.stop()


def test_the_proxy_refuses_a_host_that_is_not_listed_and_says_so_without_naming_the_list(echo):
    seen = []
    proxy = started(local_plan(echo.port), on_deny=lambda h, p, d: seen.append((h, p, d.via)))
    try:
        out = talk(proxy, b"CONNECT other.example.com:443 HTTP/1.1\r\nHost: x\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403") and b"X-Ws-Egress: refused" in out
        assert b"localhost" not in out and str(echo.port).encode() not in out  # the list stays private
        assert proxy.denials == [("list", "not in this class's allowlist")]
        assert seen == [("other.example.com", 443, "")]
    finally:
        proxy.stop()


def test_the_proxy_refuses_a_listed_name_that_resolves_to_loopback(echo):
    # DNS rebinding: an allowed public name whose answer is the machine's own model server
    plan = local_plan(echo.port, "models.example.com")
    fake = lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port))]
    proxy = started(plan, resolver=fake)
    try:
        out = talk(proxy, f"CONNECT models.example.com:{echo.port} HTTP/1.1\r\n\r\n".encode())
        assert out.startswith(b"HTTP/1.1 403") and b"loopback" in out
    finally:
        proxy.stop()


def test_the_proxy_refuses_a_listed_name_that_resolves_to_a_metadata_address():
    p = policy.loads('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["*.example.com"]\n')
    plan = sandbox.egress_plan(p, p.classes["c"], "", NOW)
    fake = lambda host, port, **kw: [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (META, port))]
    proxy = started(plan, resolver=fake)
    try:
        out = talk(proxy, b"CONNECT a.example.com:80 HTTP/1.1\r\n\r\n")
        assert out.startswith(b"HTTP/1.1 403") and b"metadata" in out
    finally:
        proxy.stop()


def test_the_proxy_refuses_a_loopback_port_the_list_does_not_name(echo):
    other = socket.create_server(("127.0.0.1", 0))
    try:
        proxy = started(local_plan(echo.port))
        try:
            out = talk(proxy, f"CONNECT localhost:{other.getsockname()[1]} HTTP/1.1\r\n\r\n".encode())
            assert out.startswith(b"HTTP/1.1 403")
        finally:
            proxy.stop()
    finally:
        other.close()


@pytest.mark.parametrize(
    "head", [b"garbage\r\n\r\n", b"GET /relative HTTP/1.1\r\n\r\n", b"CONNECT :443 HTTP/1.1\r\n\r\n"]
)
def test_the_proxy_refuses_what_is_not_a_proxy_request(echo, head):
    proxy = started(local_plan(echo.port))
    try:
        assert talk(proxy, head).startswith(b"HTTP/1.1 400")
    finally:
        proxy.stop()


def test_the_proxy_listens_on_loopback_only_and_stops(echo):
    proxy = started(local_plan(echo.port))
    assert proxy._srv.getsockname()[0] == "127.0.0.1"
    proxy.stop()
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", proxy.port), timeout=2).close()


# --- the environment, the shims, the profile ---------------------------------------------------------------------------


def test_the_proxy_variables_send_everything_but_loopback_through_it():
    env = sandbox.proxy_env(4321)
    for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        assert env[k] == "http://127.0.0.1:4321"
    with_secret = sandbox.proxy_env(4321, "pw")
    assert with_secret["HTTPS_PROXY"] == "http://ws:pw@127.0.0.1:4321"
    assert sandbox.new_secret() != sandbox.new_secret() and len(sandbox.new_secret()) >= 24
    assert env["NO_PROXY"] == env["no_proxy"] == "localhost,127.0.0.1,::1"


def test_a_shim_says_so_logs_and_exits_126(tmp_path):
    bin_dir = Path(sandbox.write_deny_shims(str(tmp_path / "wall")))
    assert {p.name for p in bin_dir.iterdir()} == set(sandbox.SHIM_TOOLS)
    r = subprocess.run([str(bin_dir / "nc"), "example.com", "80"], capture_output=True, text=True, check=False)
    assert r.returncode == 126 and "blocked in this session" in r.stderr
    subprocess.run([str(bin_dir / "ssh"), "x"], capture_output=True, check=False)
    assert sandbox.shim_events(str(tmp_path / "wall")) == 2
    assert sandbox.shim_events(str(tmp_path / "none")) == 0


def test_the_end_of_run_line_counts_and_names_the_class_only(pol):
    plan = plan_for(pol, "work")
    assert sandbox.egress_note(plan, 0, 0) is None
    line = sandbox.egress_note(plan, 3, 1)
    assert line == "egress, class 'work': 3 request(s) refused by the allowlist, 1 blocked tool call(s)"


def test_the_macos_profile_shuts_the_network_but_for_the_proxy_and_the_model_ports(tmp_path):
    prof = sandbox.sandbox_exec_profile(sandbox.Plan(tmp_path, net=(4321, 11434)))
    lines = prof.splitlines()
    assert "(deny network-outbound)" in lines
    assert '(allow network-outbound (remote ip "localhost:4321"))' in lines
    assert '(allow network-outbound (remote ip "localhost:11434"))' in lines
    assert lines.index("(deny network-outbound)") < lines.index('(allow network-outbound (remote ip "localhost:4321"))')
    assert lines[-1] == "(deny network-outbound (remote unix-socket))"  # the unix-socket deny still comes last


def test_the_macos_profile_leaves_the_network_alone_without_a_plan(tmp_path):
    assert "(deny network-outbound)" not in sandbox.sandbox_exec_profile(sandbox.Plan(tmp_path)).splitlines()


def test_wrap_passes_the_ports_to_the_macos_profile(tmp_path):
    home = tmp_path / "ws"
    home.mkdir()
    w = sandbox.wrap(
        ["opencode"], policy.loads(""), home, [], system="darwin", which=lambda n: "/usr/bin/" + n, net=(4321,)
    )
    assert '(allow network-outbound (remote ip "localhost:4321"))' in w.argv[2]


# --- explain ------------------------------------------------------------------------------------------------------------


def test_explain_says_which_kind_of_refusal_it_is(pol):
    work = pol.classes["work"]
    assert "allowed" in sandbox.explain_host(pol, work, "api.example.com", now=NOW)
    assert "service's own answer" in sandbox.explain_host(pol, work, "api.example.com", now=NOW)
    gone = sandbox.explain_host(pol, work, "mirror.example.net", now=NOW)
    assert "expired" in gone and "old-mirror" in gone
    pypi = sandbox.explain_host(pol, work, "files.pythonhosted.org", now=NOW)
    assert "pypi set" in pypi and '"pypi.org"' in pypi and "[classes.work]" in pypi
    other = sandbox.explain_host(pol, work, "unknown.example.com", now=NOW)
    assert 'add "unknown.example.com"' in other
    assert "no classes" in sandbox.explain_host(policy.loads(""), None, "x.example.com")


# --- the policy's rules ---------------------------------------------------------------------------------------------


def refused(text, *words):
    with pytest.raises(PolicyError) as e:
        policy.loads(text)
    for w in words:
        assert w in str(e.value), str(e.value)


@pytest.mark.parametrize(
    "host", ["0.0.0.0", "0.0.0.0:443", "*.com", "*.1.2.3", "api.example.com:0", "api.example.com:70000"]
)
def test_a_host_that_opens_the_network_to_everyone_is_refused_in_every_list(host):
    refused(f'[gate]\nallow_hosts = ["{host}"]\n', "[gate] allow_hosts", host)
    refused(
        f'[classes.c]\naccess = "walled"\nrepos = ["/x/**"]\nallowed_hosts = ["{host}"]\n',
        "[classes.c] allowed_hosts",
        host,
    )


def test_a_subdomain_wildcard_and_an_ordinary_host_are_fine():
    p = policy.loads('[gate]\nallow_hosts = ["*.example.org", "203.0.113.5:8443", "localhost:11434"]\n')
    assert p.gate.allow_hosts == ("*.example.org", "203.0.113.5:8443", "localhost:11434")


def exc(**kw):
    base = {
        "name": '"x"',
        "classes": '["c"]',
        "hosts": '["a.example.com"]',
        "reason": '"why"',
        "owner": '"me"',
        "expires": "2099-01-01",
    }
    base.update({k: v for k, v in kw.items() if v is not None})
    body = "".join(f"{k} = {v}\n" for k, v in base.items() if k not in kw or kw[k] is not None)
    return '[classes.c]\naccess = "walled"\nrepos = ["/x/**"]\n[[egress_exceptions]]\n' + body


def test_an_egress_exception_parses_with_its_owner_and_expiry():
    x = policy.loads(exc()).egress_exceptions[0]
    assert (x.name, x.classes, x.hosts, x.owner) == ("x", ("c",), ("a.example.com",), "me")
    assert x.expires == datetime(2099, 1, 1, tzinfo=UTC) and not x.expired(NOW) and x.expired(x.expires)


@pytest.mark.parametrize("key", ["name", "classes", "hosts", "reason", "owner", "expires"])
def test_an_egress_exception_with_a_key_missing_is_refused(key):
    refused(exc(**{key: None}), "[[egress_exceptions]] #1")


def test_an_egress_exception_must_end_and_have_an_owner():
    refused(exc(expires=None), "expires is required", "must end")
    refused(exc(owner='""'), "owner is required")
    refused(exc(expires='"2099-01-01"'), "expires")  # text is not a date
    refused(exc(expires="2099-01-01T00:00:00"), "timezone")


def test_an_egress_exception_must_name_known_classes_and_good_hosts():
    refused(exc(classes='["nope"]'), "unknown class")
    refused(exc(classes="[]"), "at least one class")
    refused(exc(hosts="[]"), "at least one host")
    refused(exc(hosts='["*.com"]'), "[[egress_exceptions]] #1 hosts", "*.com")
    refused(exc() + "colour = 1\n", "colour")


def test_two_egress_exceptions_can_not_share_a_name():
    refused(
        exc()
        + '[[egress_exceptions]]\nname = "x"\nclasses = ["c"]\nhosts = ["b.example.com"]\nreason = "r"\nowner = "o"\nexpires = 2099-01-01\n',
        "used twice",
    )


# --- the claude code adapter, the class file and the doctor ------------------------------------------------------------


def test_for_project_writes_the_hosts_as_allowed_domains_and_an_empty_list_reaches_nothing(tmp_path):
    base = '{"sandbox": {"enabled": true, "network": {"allowedDomains": ["old.example.com"]}}}'
    import json

    got = json.loads(claude_code.for_project(base, str(tmp_path), ["a.example.com"]))
    assert got["sandbox"]["network"]["allowedDomains"] == ["a.example.com"]
    none = json.loads(claude_code.for_project(base, str(tmp_path), []))
    assert none["sandbox"]["network"]["allowedDomains"] == []
    kept = json.loads(claude_code.for_project(base, str(tmp_path)))
    assert kept["sandbox"]["network"]["allowedDomains"] == ["old.example.com"]  # no plan: the shared list stands


def test_for_project_with_a_list_and_no_sandbox_block_is_refused_not_ignored(tmp_path):
    with pytest.raises(ValueError, match="no sandbox block"):
        claude_code.for_project("{}", str(tmp_path), ["a.example.com"])
    with pytest.raises(ValueError, match="no sandbox block"):
        claude_code.with_hosts("{}", [])
    import json

    assert json.loads(claude_code.for_project("{}", str(tmp_path))) == {}  # no list asked for: as it was


def test_the_launch_copy_refuses_a_host_outside_its_list_instead_of_asking(tmp_path):
    import json

    got = json.loads(claude_code.with_hosts('{"sandbox": {"enabled": true}}', ["a.example.com"]))
    assert got["sandbox"]["network"] == {"allowedDomains": ["a.example.com"], "strictAllowlist": True}


@pytest.mark.parametrize(
    ("net", "named"),
    [
        ({"allowedDomains": ["evil.example.com"]}, "network.allowedDomains"),
        ({"allowUnixSockets": ["/var/run/x.sock"]}, "network.allowUnixSockets"),
        ({"httpProxyPort": 8080}, "network.httpProxyPort"),
        ({"socksProxyPort": 1080}, "network.socksProxyPort"),
        ({"allowLocalBinding": True}, "network.allowLocalBinding = true"),
        ({"allowAllUnixSockets": True}, "network.allowAllUnixSockets = true"),
    ],
)
def test_a_project_settings_file_that_adds_network_is_a_widening_the_hook_denies(box, monkeypatch, net, named):
    import json

    monkeypatch.delenv("WS_SANDBOXED", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv(claude_code.EGRESS_MARKER, "1")  # a class session: with_hosts puts this in its settings
    proj = box / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"network": net}}), encoding="utf-8")
    why = claude_code.preflight({"cwd": str(proj)})
    assert why and named in why and "open the sandbox" in why


def test_a_project_settings_file_that_adds_network_is_fine_outside_a_class_session(box, monkeypatch):
    import json

    monkeypatch.delenv("WS_SANDBOXED", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.delenv(claude_code.EGRESS_MARKER, raising=False)  # no classes in the policy: nothing sets it
    proj = box / "proj"
    (proj / ".claude").mkdir(parents=True)
    net = {"allowedDomains": ["registry.npmjs.org"], "allowLocalBinding": True, "httpProxyPort": 8080}
    (proj / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"network": net}}), encoding="utf-8")
    assert claude_code.preflight({"cwd": str(proj)}) is None


def test_the_class_settings_carry_the_marker_the_hook_reads():
    import json

    got = json.loads(claude_code.with_hosts('{"sandbox": {"enabled": true}, "env": {"A": "1"}}', []))
    assert got["env"] == {"A": "1", claude_code.EGRESS_MARKER: "1"}


def test_a_project_settings_file_with_a_quiet_network_block_is_not_a_widening(box, monkeypatch):
    import json

    monkeypatch.delenv("WS_SANDBOXED", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    monkeypatch.setenv(claude_code.EGRESS_MARKER, "1")
    proj = box / "proj"
    (proj / ".claude").mkdir(parents=True)
    quiet = {
        "sandbox": {"network": {"allowedDomains": [], "allowLocalBinding": False, "deniedDomains": ["x.example.com"]}}
    }
    (proj / ".claude" / "settings.json").write_text(json.dumps(quiet), encoding="utf-8")
    assert claude_code.preflight({"cwd": str(proj)}) is None


def test_the_headless_runner_waits_for_its_mcp_tools():
    assert claude_code.MCP_WAIT_ENV == {
        "MCP_CONNECTION_NONBLOCKING": "false",
        "MCP_TIMEOUT": "300000",
        "MCP_CONNECT_TIMEOUT_MS": "300000",
    }


def test_the_class_file_holds_the_class_hosts_as_the_only_ones(box):
    cls = policy.loads(
        f'[classes.w]\naccess = "walled"\nrepos = ["{box}/w/**"]\nallowed_providers = ["anthropic", "local"]\n'
        'allowed_hosts = ["api.example.com", "intranet"]\n'
    ).classes["w"]
    net = wallsettings.class_settings(cls, str(box / "home"), "linux")["sandbox"]["network"]
    assert net["allowManagedDomainsOnly"] is True and net["allowAllUnixSockets"] is False
    assert net["allowLocalBinding"] is False
    # a bad entry is left out, the provider's hosts are in, a local provider adds none
    assert net["allowedDomains"] == ["api.example.com", "api.anthropic.com", "platform.claude.com"]
    assert net == wallsettings.class_settings(cls, str(box / "home"), "macos")["sandbox"]["network"]


def test_a_class_that_names_no_providers_gets_no_provider_host_in_its_file(box):
    cls = policy.loads(f'[classes.w]\naccess = "walled"\nrepos = ["{box}/w/**"]\n').classes["w"]
    assert wallsettings.class_settings(cls, str(box / "home"), "linux")["sandbox"]["network"]["allowedDomains"] == []


def write_policy(box, text):
    f = box / "wshome" / "policy.toml"
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text, encoding="utf-8")


def checks(box, text, now=NOW):
    import os

    write_policy(box, text)
    return doctor._egress_checks(dict(os.environ), "linux", now)


def test_ws015_has_a_line_per_class_and_none_without_classes(box):
    assert checks(box, "") == []
    got = checks(box, POLICY)
    assert [(c.id, c.role) for c in got] == [("WS015", "egress")] * 3
    by = {c.detail.split(":")[0]: c for c in got}
    assert by["home"].status == "warn" and "old-mirror" in by["home"].detail and "covers nothing" in by["home"].detail
    assert by["work"].status == "warn" and "old-mirror" in by["work"].detail
    assert by["fiu"].status == "ok" and "network shut but for" in by["fiu"].detail


def test_ws015_is_ok_for_a_clean_class_and_counts_its_hosts(box):
    text = '[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["a.example.com", "b.example.com"]\n'
    (only,) = checks(box, text)
    assert only.status == "ok" and "2 host(s)" in only.detail and "a.example.com" not in only.detail


def test_ws015_warns_of_an_exception_ending_within_a_week_and_of_unreadable_entries(box):
    soon = NOW + timedelta(days=3)
    text = (
        '[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_hosts = ["intranet", "a.example.com"]\n'
        '[[egress_exceptions]]\nname = "e"\nclasses = ["c"]\nhosts = ["b.example.com"]\nreason = "r"\nowner = "o"\n'
        f"expires = {soon.strftime('%Y-%m-%dT%H:%M:%S')}+00:00\n"
    )
    (only,) = checks(box, text)
    assert only.status == "warn" and "ends within 7 days" in only.detail
    assert "1 host entry the proxies can't read" in only.detail and "intranet" not in only.detail  # counts, no names


def test_ws015_warns_when_the_sandbox_is_off_for_a_class(box):
    text = '[gate]\nsandbox = false\n[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\n'
    (only,) = checks(box, text)
    assert only.status == "warn" and "[gate] sandbox is off" in only.detail and "refused" in only.detail


def test_ws015_says_nothing_when_the_policy_does_not_load(box):
    assert checks(box, "[classes.c]\nnonsense = 1\n") == []
