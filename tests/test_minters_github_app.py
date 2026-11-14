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
    app.s.record("r1", "home", "worker", ["github-app"])
    out = app.mint(permissions={"contents": "read"})
    assert out["token"].startswith("ghs_") and out["expires_at"].endswith("Z")
    sent = app.fake.minted[-1]
    assert sent["body"] == {"repositories": ["ws"], "permissions": {"contents": "read"}}
    assert sent["claims"]["iss"] == "123"
    assert app.mint()["token"] != out["token"], "other permissions, another token"
    assert app.fake.minted[-1]["body"]["permissions"] == {"contents": "write", "pull_requests": "write"}


def test_a_token_is_reused_within_its_life_and_never_logged(app):
    app.s.record("r1", "home", "worker", ["github-app"])
    t1, t2 = app.mint()["token"], app.mint()["token"]
    assert t1 == t2 and len(app.fake.minted) == 1
    log = app.raw_log()
    assert t1 not in log and app.s.leaked(log) == []
    lines = [json.loads(x) for x in log.splitlines()]
    assert [x["status"] for x in lines if x["op"] == "mint"] == ["ok", "ok"]
    assert lines[-1]["purpose"] == "repos=ws contents:write,pull_requests:write"
    assert lines[-1]["host"] == "127.0.0.1" and lines[-1]["run"] == "r1"


def test_asks_beyond_the_entry_or_the_run_are_refused(app):
    app.s.record("r1", "home", "worker", ["github-app"])
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
    app.s.record("r3", "money", "worker", ["github-app"])
    assert app.refused(run="r3").code == "area_refused"
    app.s.record("r4", "home", "walled", ["github-app"])
    assert app.refused(run="r4").code == "tier_refused"
    assert app.fake.minted == [], "nothing refused reached GitHub"


def test_a_lease_request_for_a_minter_entry_is_refused(app):
    app.s.record("r1", "home", "worker", ["github-app"])
    with pytest.raises(broker.BrokerRefused) as e:
        broker.request("github-app", "run", "api.github.com", socket=app.sock, run="r1")
    assert e.value.code in ("host_not_allowed", "wrong_kind")


def test_github_refusing_is_a_refusal_with_no_token(app):
    app.s.record("r1", "home", "worker", ["github-app"])
    app.fake.status = 422
    e = app.refused()
    assert e.code == "upstream_refused" and "422" in e.reason


def test_elevate_off_revokes_tokens_minted_for_an_elevated_app(base, fake, pem):
    a = App(base, fake, pem, tier="lead-elevated")
    try:
        a.s.record("r1", "home", "lead-elevated", ["github-app"])
        token = a.mint()["token"]
        out = broker.elevate(False, a.sock)
        assert out["revoked"] == 1 and out["revoke_failed"] == 0
        assert fake.revoked == [token]
        assert a.refused().code == "elevation_off"
        assert token not in a.raw_log()
    finally:
        a.broker.stop()


def test_the_git_credential_helper_answers_github_only_and_prints_no_reason_text(app, monkeypatch, capsys):
    app.s.record("r1", "home", "worker", ["github-app"])
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
