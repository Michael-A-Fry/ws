"""The SSH CA minter, with a throwaway CA made in a temporary folder. No host is touched."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from helpers.stub_secrets import StubSecrets

from ws import broker
from ws import secrets as wsecrets
from ws.minters import ssh_ca

pytestmark = pytest.mark.skipif(shutil.which("ssh-keygen") is None, reason="needs ssh-keygen")


def _keypair(folder: Path, name: str) -> tuple[Path, Path]:
    key = folder / name
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(key)], check=True)
    return key, key.with_name(name + ".pub")


@pytest.fixture
def base():
    d = Path(tempfile.mkdtemp(prefix="wsca"))  # short: Unix socket paths top out near 100 characters
    yield d
    shutil.rmtree(d, ignore_errors=True)


class Ca:
    def __init__(self, base: Path, tier: str = "lead"):
        self.base = base
        keys = base / "keys"
        keys.mkdir(mode=0o700)
        self.ca_key, self.ca_pub = _keypair(keys, "ca")
        self.user_key, self.user_pub = _keypair(keys, "id_ed25519")
        self.s = StubSecrets(base / "b")
        self.s.registry.write_text(
            '[ssh-ca]\nkind = "ssh-ca"\nprincipals = { lead = ["ws-audit"], "lead-elevated" = ["ws-admin", "ws-audit"] }\n'
            f'tier = "{tier}"\narea = "home"\n',
            encoding="utf-8",
        )
        value = base64.b64encode(self.ca_key.read_bytes()).decode()
        self.s.store.write("ssh-ca", value)
        self.s.values["ssh-ca"] = value
        self.log = base / "b" / "logs" / "broker.jsonl"
        self.sock = base / "b" / "run" / "s.sock"
        self.broker = broker.Broker(
            self.sock,
            self.s.registry,
            self.s.store,
            log_path=self.log,
            records=self.s.records,
            elevate_file=self.s.elevate_file,
        ).start()

    def sign(self, run="r1", public_key=None) -> dict:
        pub = self.user_pub.read_text().strip() if public_key is None else public_key
        return broker._client_call({"op": "sign", "name": "ssh-ca", "run": run, "public_key": pub}, self.sock)

    def refused(self, **kw) -> broker.BrokerRefused:
        with pytest.raises(broker.BrokerRefused) as e:
            self.sign(**kw)
        return e.value


@pytest.fixture
def ca(base):
    c = Ca(base)
    yield c
    c.broker.stop()


def _cert_info(base: Path, cert: str) -> str:
    f = base / "c-cert.pub"
    f.write_text(cert + "\n")
    return subprocess.run(["ssh-keygen", "-L", "-f", str(f)], check=True, capture_output=True, text=True).stdout


def test_a_run_gets_a_15_minute_certificate_with_its_tiers_principals(ca):
    ca.s.record("r1", "home", "lead", ["ssh-ca"])
    out = ca.sign()
    assert out["principals"] == ["ws-audit"] and out["valid_s"] == 900
    info = _cert_info(ca.base, out["certificate"])
    assert 'Key ID: "ws:r1"' in info
    assert "ws-audit" in info and "ws-admin" not in info
    assert "permit-pty" in info and "permit-agent-forwarding" not in info and "permit-port-forwarding" not in info
    fp = subprocess.run(["ssh-keygen", "-l", "-f", str(ca.ca_pub)], check=True, capture_output=True, text=True)
    assert fp.stdout.split()[1] in info, "signed by the throwaway CA"
    valid = next(x for x in info.splitlines() if "Valid:" in x)
    assert "from" in valid and "to" in valid


def test_the_validity_window_is_the_entries(ca):
    from datetime import datetime

    ca.s.record("r1", "home", "lead", ["ssh-ca"])
    info = _cert_info(ca.base, ca.sign()["certificate"])
    valid = next(x for x in info.splitlines() if "Valid:" in x).split()
    start, end = datetime.fromisoformat(valid[2]), datetime.fromisoformat(valid[4])
    assert (end - start).total_seconds() == 960, "a minute back for clock drift, then 15 minutes"


def test_elevated_tier_gets_its_own_principals_and_elevate_off_stops_it(base):
    c = Ca(base, tier="lead-elevated")
    try:
        c.s.record("r1", "home", "lead-elevated", ["ssh-ca"])
        assert c.sign()["principals"] == ["ws-admin", "ws-audit"]
        broker.elevate(False, c.sock)
        assert c.refused().code == "elevation_off"
    finally:
        c.broker.stop()


def test_refusals_never_sign(ca):
    assert ca.refused(run="nosuch").code == "run_refused"
    ca.s.record("r-worker", "home", "worker", ["ssh-ca"])
    assert ca.refused(run="r-worker").code == "tier_refused"
    ca.s.record("r-other", "home", "lead", ["other"])
    assert ca.refused(run="r-other").code == "not_declared"
    ca.s.record("r1", "home", "lead", ["ssh-ca"])
    private = ca.user_key.read_text()
    e = ca.refused(public_key=private)
    assert e.code == "bad_request" and "private key is refused" in e.reason
    assert ca.refused(public_key="ssh-ed25519 AAAA\nssh-ed25519 BBBB").code == "bad_request"
    log = ca.log.read_text()
    assert ca.s.leaked(log) == [] and "PRIVATE KEY" not in log
    assert [json.loads(x)["status"] for x in log.splitlines() if json.loads(x)["op"] == "sign"] == [
        "run_refused",
        "tier_refused",
        "not_declared",
        "bad_request",
        "bad_request",
    ]


def test_a_tier_with_no_principals_gets_no_certificate():
    opts = ssh_ca.Options(principals=(("lead", ("ws-audit",)),))
    with pytest.raises(ssh_ca.MintError) as e:
        ssh_ca.sign(opts, "unused", "ssh-ed25519 AAAA", "r1", "worker")
    assert e.value.code == "tier_refused"


def test_the_ca_key_file_is_gone_after_signing(ca, monkeypatch):
    made = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(Path(d))
        return d

    monkeypatch.setattr(ssh_ca.tempfile, "mkdtemp", spy)
    ca.s.record("r1", "home", "lead", ["ssh-ca"])
    ca.sign()
    assert made and not any(d.exists() for d in made)


def test_the_client_writes_the_certificate_beside_the_public_key(ca, capsys):
    ca.s.record("r1", "home", "lead", ["ssh-ca"])
    argv = ["sign", "--name", "ssh-ca", "--run", "r1", "--key", str(ca.user_pub), "--socket", str(ca.sock)]
    assert ssh_ca.main(argv) == 0
    cert = ca.user_pub.with_name("id_ed25519-cert.pub")
    assert cert.read_text().startswith("ssh-ed25519-cert-v01")
    assert "valid 15 minutes" in capsys.readouterr().out
    argv[argv.index("--key") + 1] = str(ca.user_key)
    assert ssh_ca.main(argv) == 2, "the private key file is never sent"


def test_host_steps_trust_the_ca_by_principal_and_name_no_host(ca, capsys):
    argv = ["host-steps", "--name", "ssh-ca", "--user", "ops", "--principal", "ws-audit", "--socket", str(ca.sock)]
    assert ssh_ca.main(argv) == 0
    text = capsys.readouterr().out
    assert " ".join(ca.ca_pub.read_text().split()[:2]) in text
    assert "TrustedUserCAKeys /etc/ssh/ws_user_ca.pub" in text
    assert "AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u" in text
    assert "sudo sshd -t" in text and "Undo:" in text
    assert "on no other" in text and "never gets the CA line" in text
    assert "prod" not in text.lower()


def test_the_registry_checks_the_ca_entry(tmp_path):
    p = tmp_path / "secrets.toml"
    for bad, why in (
        ('principals = { root = ["x"] }', "not a tier"),
        ('principals = { lead = ["Bad Name"] }', "user-like"),
        ('principals = { lead = ["x"] }\nvalidity_s = 86400', "validity_s"),
        ('principals = { lead = ["x"] }\nhosts = ["a.example.com"]', "unknown keys: hosts"),
    ):
        p.write_text(f'[c]\nkind = "ssh-ca"\n{bad}\n')
        with pytest.raises(wsecrets.SecretsError, match=why):
            wsecrets.load_registry(p)
    p.write_text('[c]\nkind = "ssh-ca"\nprincipals = { lead = ["x"] }\n')
    e = wsecrets.load_registry(p)["c"]
    assert e.options.for_tier("lead") == ("x",) and e.options.validity_s == 900
    problems, _ = wsecrets.health(p, wsecrets.Store(tmp_path / "s"))
    assert not any("no hosts" in x for x in problems), "a minter entry needs no hosts"
