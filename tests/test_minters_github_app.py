"""The GitHub App minter, against a fake GitHub token endpoint on 127.0.0.1. Nothing reaches GitHub."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from helpers.stub_secrets import StubSecrets

from ws import broker
from ws import secrets as wsecrets
from ws.minters import github_app as gh

INSTALLATION = 4242
# what a run record gives a run to mint for: both repos, and the entry's own permissions
GRANT = {"repos": ("ws", "notes"), "permissions": (("contents", "write"), ("pull_requests", "write"))}


def _make_pem(folder: Path) -> bytes:
    """A throwaway 2048-bit RSA key, made at run time (PKCS#8 from openssl, else PKCS#1 from ssh-keygen)."""
    out = folder / "app.pem"
    if shutil.which("openssl"):
        subprocess.run(
            ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048", "-out", str(out)],
            check=True,
            capture_output=True,
        )
    elif shutil.which("ssh-keygen"):
        subprocess.run(
            ["ssh-keygen", "-q", "-t", "rsa", "-b", "2048", "-m", "PEM", "-N", "", "-f", str(out)],
            check=True,
            capture_output=True,
        )
    else:
        pytest.skip("needs openssl or ssh-keygen to make a throwaway key")
    return out.read_bytes()


@pytest.fixture(scope="module")
def pem():
    d = Path(tempfile.mkdtemp(prefix="wsgh"))
    try:
        yield _make_pem(d)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _verify(key: gh.RsaKey, jwt: str) -> dict:
    head, body, sig = jwt.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    s = int.from_bytes(base64.urlsafe_b64decode(pad(sig)), "big")
    em = pow(s, key.e, key.n).to_bytes(key.size, "big")
    want = gh.SHA256_DIGEST_INFO + hashlib.sha256(f"{head}.{body}".encode()).digest()
    assert em.startswith(b"\x00\x01\xff") and em.endswith(b"\x00" + want), "a valid PKCS#1 v1.5 SHA-256 signature"
    assert json.loads(base64.urlsafe_b64decode(pad(head))) == {"alg": "RS256", "typ": "JWT"}
    return json.loads(base64.urlsafe_b64decode(pad(body)))


class FakeGitHub(ThreadingHTTPServer):
    def __init__(self, key: gh.RsaKey):
        super().__init__(("127.0.0.1", 0), FakeHandler)
        self.key = key
        self.minted: list[dict] = []
        self.revoked: list[str] = []
        self.status = 201

    @property
    def api(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class FakeHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _reply(self, status: int, obj: dict | None = None):
        data = json.dumps(obj).encode() if obj is not None else b""
        self.send_response(status)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        auth = self.headers["Authorization"]
        assert auth.startswith("Bearer ")
        claims = _verify(self.server.key, auth.removeprefix("Bearer "))
        assert self.path == f"/app/installations/{INSTALLATION}/access_tokens"
        assert claims["exp"] - claims["iat"] <= 600
        token = "ghs_" + os.urandom(18).hex()
        self.server.minted.append({"claims": claims, "body": body, "token": token})
        if self.server.status != 201:
            return self._reply(self.server.status, {"message": "nope"})
        exp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 3600))
        self._reply(201, {"token": token, "expires_at": exp})

    def do_DELETE(self):
        assert self.path == "/installation/token"
        self.server.revoked.append(self.headers["Authorization"].removeprefix("Bearer "))
        self._reply(204)


@pytest.fixture
def fake(pem):
    f = FakeGitHub(gh.load_key(base64.b64encode(pem).decode()))
    threading.Thread(target=f.serve_forever, args=(0.05,), daemon=True).start()
    yield f
    f.shutdown()
    f.server_close()


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="wsm"))  # short: Unix socket paths top out near 100 characters
    yield d
    shutil.rmtree(d, ignore_errors=True)


class App:
    def __init__(self, base: Path, fake: FakeGitHub, pem: bytes, tier: str = "worker"):
        self.s = StubSecrets(base)
        self.fake = fake
        self.s.registry.write_text(
            f'[github-app]\nkind = "github-app"\napp_id = "123"\ninstallation_id = {INSTALLATION}\n'
            f'repos = ["ws", "notes"]\npermissions = {{ contents = "write", pull_requests = "write" }}\n'
            f'tier = "{tier}"\narea = "home"\napi = "{fake.api}"\n',
            encoding="utf-8",
        )
        self.s.store.write("github-app", base64.b64encode(pem).decode())
        self.s.values["github-app"] = base64.b64encode(pem).decode()
        self.log = base / "logs" / "broker.jsonl"
        self.sock = base / "run" / "b.sock"
        self.broker = broker.Broker(
            self.sock,
            self.s.registry,
            self.s.store,
            log_path=self.log,
            allow_plain_http=True,
            records=self.s.records,
            elevate_file=self.s.elevate_file,
        ).start()

    def mint(self, run="r1", repos=("ws",), **extra) -> dict:
        return broker._client_call(
            {"op": "mint", "name": "github-app", "run": run, "repos": list(repos), **extra}, self.sock
        )

    def refused(self, **kw) -> broker.BrokerRefused:
        with pytest.raises(broker.BrokerRefused) as e:
            self.mint(**kw)
        return e.value

    def raw_log(self) -> str:
        return self.log.read_text() if self.log.exists() else ""


@pytest.fixture
def app(base, fake, pem):
    a = App(base, fake, pem)
    yield a
    a.broker.stop()


def test_the_key_loads_from_base64_of_the_pem_or_escaped_pem_and_hides_itself(pem):
    k1 = gh.load_key(base64.b64encode(pem).decode())
    k2 = gh.load_key(pem.decode().replace("\n", "\\n"))
    assert k1 == k2 and k1.n.bit_length() == 2048
    assert str(k1.n) not in repr(k1) and str(k1.d) not in repr(k1)
    with pytest.raises(gh.MintError) as e:
        gh.load_key(base64.b64encode(b"-----BEGIN PRIVATE KEY-----\nAAAA\n-----END PRIVATE KEY-----\n").decode())
    assert e.value.code == "store_unreadable" and "AAAA" not in str(e.value)


def test_the_jwt_is_rs256_signed_with_a_short_life(pem):
    k = gh.load_key(base64.b64encode(pem).decode())
    claims = _verify(k, gh.app_jwt("123", k, 1_000_000))
    assert claims == {"iat": 999_940, "exp": 1_000_540, "iss": "123"}


def test_the_signature_matches_openssl(pem, tmp_path):
    if not shutil.which("openssl"):
        pytest.skip("openssl is not installed")
    k = gh.load_key(base64.b64encode(pem).decode())
    (tmp_path / "k.pem").write_bytes(pem)
    (tmp_path / "msg").write_bytes(b"hello broker")
    want = subprocess.run(
        ["openssl", "dgst", "-sha256", "-sign", str(tmp_path / "k.pem"), str(tmp_path / "msg")],
        check=True,
        capture_output=True,
    ).stdout
    assert gh.sign_rs256(k, b"hello broker") == want


def test_a_run_gets_a_token_narrowed_to_its_repos_and_permissions(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    out = app.mint(permissions={"contents": "read"})
    assert out["token"].startswith("ghs_") and out["expires_at"].endswith("Z")
    sent = app.fake.minted[-1]
    assert sent["body"] == {"repositories": ["ws"], "permissions": {"contents": "read"}}
    assert sent["claims"]["iss"] == "123"
    assert app.mint()["token"] != out["token"], "other permissions, another token"
    assert app.fake.minted[-1]["body"]["permissions"] == {"contents": "write", "pull_requests": "write"}


def test_a_token_is_reused_within_its_life_and_never_logged(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    t1, t2 = app.mint()["token"], app.mint()["token"]
    assert t1 == t2 and len(app.fake.minted) == 1
    log = app.raw_log()
    assert t1 not in log and app.s.leaked(log) == []
    lines = [json.loads(x) for x in log.splitlines()]
    assert [x["status"] for x in lines if x["op"] == "mint"] == ["ok", "ok"]
    assert lines[-1]["purpose"] == "repos=ws contents:write,pull_requests:write"
    assert lines[-1]["host"] == "127.0.0.1" and lines[-1]["run"] == "r1"


def test_asks_beyond_the_entry_or_the_run_are_refused(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    assert app.refused(repos=("secret-repo",)).code == "not_declared"
    assert app.refused(permissions={"administration": "write"}).code == "not_declared"
    assert app.refused(permissions={"contents": "admin"}).code == "not_declared"
    assert app.refused(repos=()).code == "bad_request"
    assert app.refused(run="nosuch").code == "run_refused"
    with pytest.raises(broker.BrokerRefused) as e:
        broker._client_call({"op": "mint", "name": "github-app", "repos": ["ws"]}, app.sock)
    assert e.value.code == "run_required"
    app.s.record("r2", "home", "worker", ["other"])
    assert app.refused(run="r2").code == "not_declared"
    app.s.record("r3", "money", "worker", ["github-app"], **GRANT)
    assert app.refused(run="r3").code == "area_refused"
    app.s.record("r4", "home", "walled", ["github-app"], **GRANT)
    assert app.refused(run="r4").code == "tier_refused"
    assert app.fake.minted == [], "nothing refused reached GitHub"


def test_a_lease_request_for_a_minter_entry_is_refused(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    with pytest.raises(broker.BrokerRefused) as e:
        broker.request("github-app", "run", "api.github.com", socket=app.sock, run="r1")
    assert e.value.code in ("host_not_allowed", "wrong_kind")


def test_github_refusing_is_a_refusal_with_no_token(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    app.fake.status = 422
    e = app.refused()
    assert e.code == "upstream_refused" and "422" in e.reason


def test_elevate_off_revokes_tokens_minted_for_an_elevated_app(base, fake, pem):
    a = App(base, fake, pem, tier="lead-elevated")
    try:
        a.s.record("r1", "home", "lead-elevated", ["github-app"], **GRANT)
        token = a.mint()["token"]
        out = broker.elevate(False, a.sock)
        assert out["revoked"] == 1 and out["revoke_failed"] == 0
        assert fake.revoked == [token]
        assert a.refused().code == "elevation_off"
        assert token not in a.raw_log()
    finally:
        a.broker.stop()


def test_the_git_credential_helper_answers_github_only_and_prints_no_reason_text(app, monkeypatch, capsys):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    args = ["credential", "--name", "github-app", "--run", "r1", "--socket", str(app.sock)]
    monkeypatch.setattr("sys.stdin", io.StringIO("protocol=https\nhost=github.com\npath=me/ws.git\n\n"))
    assert gh.main([*args, "get"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[0] == "username=x-access-token" and out[1].startswith("password=ghs_")
    monkeypatch.setattr("sys.stdin", io.StringIO("protocol=https\nhost=gitlab.example\n\n"))
    assert gh.main([*args, "get"]) == 0
    assert capsys.readouterr().out == "", "another host: git asks its next helper"
    assert gh.main([*args, "store"]) == 0 and gh.main([*args, "erase"]) == 0


def test_the_registry_checks_the_app_entry(tmp_path):
    p = tmp_path / "secrets.toml"
    good = 'kind = "github-app"\napp_id = 1\ninstallation_id = 2\nrepos = ["ws"]\npermissions = { contents = "read" }\n'
    p.write_text("[a]\n" + good)
    e = wsecrets.load_registry(p)["a"]
    assert e.kind == "github-app" and e.options.app_id == "1" and e.options.repos == ("ws",)
    for bad, why in (
        (good.replace('"read"', '"owner"'), "read, write or admin"),
        (good.replace("installation_id = 2", "installation_id = 0"), "installation_id"),
        (good + 'hosts = ["x.example.com"]\n', "unknown keys: hosts"),
        (good.replace('repos = ["ws"]', "repos = []"), "repos"),
        (good.replace('"github-app"', '"vault"'), "kind must be"),
    ):
        p.write_text("[a]\n" + bad)
        with pytest.raises(wsecrets.SecretsError, match=why):
            wsecrets.load_registry(p)


def test_the_setup_steps_name_every_field_and_run_nothing():
    text = gh.setup_steps("github-app")
    for must in (
        "New GitHub App",
        "untick Active",
        "Contents: Read and write",
        "Pull requests: Read and write",
        "Only on this account",
        "Generate a private key",
        "Only select repositories",
        "installation_id",
        "base64 -w0",
        "| ws secret set github-app",
        "credential.https://github.com.helper",
        "Undo:",
    ):
        assert must in text, must
    assert "\\\n" in text, "the git config line continues with one backslash"


# ---------------------------------------------------------------- cold review: the run, not the caller, bounds a token


def test_must2_the_run_record_not_the_caller_bounds_repos_and_permissions(app):
    app.s.record("r1", "home", "worker", ["github-app"], repos=("ws",), permissions=(("contents", "read"),))
    assert app.refused(repos=("notes",)).code == "not_declared", "the entry allows notes, the run does not"
    assert app.refused(permissions={"contents": "write"}).code == "not_declared", "the run gives read only"
    assert app.refused(permissions={"pull_requests": "write"}).code == "not_declared"
    assert app.mint()["token"].startswith("ghs_")
    assert app.fake.minted[-1]["body"] == {"repositories": ["ws"], "permissions": {"contents": "read"}}
    app.s.record("r2", "home", "worker", ["github-app"])
    assert app.refused(run="r2").code == "not_declared", "a record with no repos gives no token"
    assert len(app.fake.minted) == 1


def test_must1_a_token_minted_while_elevate_off_lands_is_revoked_and_refused(base, fake, pem, monkeypatch):
    a = App(base, fake, pem, tier="lead-elevated")
    try:
        a.s.record("r1", "home", "lead-elevated", ["github-app"], **GRANT)
        real = gh.mint

        def slow_mint(*args, **kw):
            out = real(*args, **kw)
            broker.write_elevation(a.s.elevate_file, False)  # the switch is pulled while GitHub answers
            return out

        monkeypatch.setattr(gh, "mint", slow_mint)
        assert a.refused().code == "elevation_off"
        assert fake.revoked == [fake.minted[-1]["token"]], "the late token was revoked upstream"
        assert a.broker._minted == [] and a.broker._mint_cache == {}
        assert fake.minted[-1]["token"] not in a.raw_log()
    finally:
        a.broker.stop()


def test_must3_a_runs_tokens_are_revoked_when_its_record_goes(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    token = app.mint()["token"]
    assert broker.remove_run_record(app.s.records, "r1")
    assert app.broker.reap_minted() == (1, 0)
    assert app.fake.revoked == [token]
    assert app.broker._minted == [] and app.broker._mint_cache == {}
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    assert app.mint()["token"] != token, "a new run with the same id gets a fresh token, never the dead one"
    assert [json.loads(x)["status"] for x in app.raw_log().splitlines() if json.loads(x)["op"] == "revoke"] == ["ok"]


def test_must3_a_record_that_drops_the_name_ends_its_tokens_too(app):
    app.s.record("r1", "home", "worker", ["github-app"], **GRANT)
    token = app.mint()["token"]
    app.s.record("r1", "home", "worker", ["other"], **GRANT)
    assert app.broker.reap_minted() == (1, 0) and app.fake.revoked == [token]


def test_signing_is_blinded_and_still_deterministic(pem, monkeypatch):
    k = gh.load_key(base64.b64encode(pem).decode())
    seen = []
    real = os.urandom
    monkeypatch.setattr(gh.os, "urandom", lambda n: seen.append(n) or real(n))
    assert gh.sign_rs256(k, b"x") == gh.sign_rs256(k, b"x"), "PKCS#1 v1.5 is deterministic, blinding or not"
    assert seen and all(n == k.size for n in seen), "a fresh blinding factor per signature"


def test_run_record_cli_takes_repos_and_permissions(base, monkeypatch):
    from ws import cli

    monkeypatch.setenv("WS_HOME", str(base))
    monkeypatch.setenv("HOME", str(base))
    argv = ["broker", "run-record", "write", "r1", "--area", "home", "--tier", "worker", "--name", "github-app"]
    ns = cli.build_parser().parse_args([*argv, "--repo", "ws", "--perm", "contents=read"])
    assert broker.cmd_run_record(ns) == 0
    rec = broker.read_run_record(broker.records_dir(), "r1", os.geteuid())
    assert rec.repos == ("ws",) and rec.permissions == (("contents", "read"),)
    bad = cli.build_parser().parse_args([*argv, "--perm", "contents=owner"])
    with pytest.raises(broker.WsError):
        broker.cmd_run_record(bad)


def test_a_name_retiered_to_elevated_after_minting_loses_its_token_on_elevate_off(app):
    app.s.record("r1", "home", "lead-elevated", ["github-app"], **GRANT)
    token = app.mint()["token"]  # minted as a worker name: not elevated then
    text = app.s.registry.read_text()
    assert 'tier = "worker"' in text
    app.s.registry.write_text(text.replace('tier = "worker"', 'tier = "lead-elevated"'))
    out = broker.elevate(False, app.sock)
    assert out["revoked"] == 1 and app.fake.revoked == [token]
    assert app.broker._minted == [] and app.broker._mint_cache == {}
    assert app.refused().code == "elevation_off"
