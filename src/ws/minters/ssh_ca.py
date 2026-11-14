"""SSH certificate authority minter: the broker signs a session's own public key for 15 minutes, principals per tier.

Registry entry (the CA private key is the stored value, as one line: `base64 -w0 ws_user_ca`):

    [ssh-ca]
    kind = "ssh-ca"
    principals = { lead = ["ws-audit"], "lead-elevated" = ["ws-admin"] }   # a tier with none gets no certificate
    validity_s = 900                        # optional, at most 3600; default 900 (15 minutes)
    tier = "lead"
    area = "home"

A run asks with the `sign` op, giving its own public key (never a private one; the broker never sees the session's
private key). The broker checks the run record as for any scoped name, then signs with `ssh-keygen -s`: the identity is
`ws:<run id>`, the principals are the entry's for the run's tier, the window starts a minute in the past (clock drift)
and ends `validity_s` later, and the certificate permits a terminal and nothing else (no agent, port or X11
forwarding). The CA key is written to a 0600 file in a fresh 0700 folder for the one `ssh-keygen` call and removed at
once. The certificate is public: it is returned to the caller and only its serial and principals reach the audit log.

Hosts trust the CA, not each key: `TrustedUserCAKeys` names the CA's public key and `AuthorizedPrincipalsFile` says
which principals may log in as which user. `host_steps` prints those steps. Install the CA only on hosts agents may
reach; a host that must stay out of every agent's reach never gets the CA line, so no certificate opens it.

There is no revocation: a certificate is short instead. `ws elevate off` stops new certificates for elevated names at
once; one already issued lasts out its window (15 minutes by default). Swap note: OpenBao's SSH secrets engine signs the
same certificates with its own audit; hosts keep the same `TrustedUserCAKeys` line.
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

KIND = "ssh-ca"
OP = "sign"
DEFAULT_VALIDITY_S = 900
MAX_VALIDITY_S = 3600
PRINCIPAL_RE = re.compile(r"^[a-z_][a-z0-9_.-]{0,31}$")
PUBKEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com"
    r"|sk-ecdsa-sha2-nistp256@openssh\.com) ([A-Za-z0-9+/]+={0,2})(?: [^\x00-\x1f\x7f]{0,200})?$"
)
MAX_PUBKEY = 16384
EXTRA_KEYS = frozenset({"principals", "validity_s"})


class MintError(Exception):
    """Never carries the CA key. `code` is a broker refusal code."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Options:
    principals: tuple[tuple[str, tuple[str, ...]], ...]
    validity_s: int = DEFAULT_VALIDITY_S

    def for_tier(self, tier: str) -> tuple[str, ...]:
        return dict(self.principals).get(tier, ())


def parse(path: Path, name: str, t: dict) -> Options:
    from ws.secrets import TIERS, SecretsError

    where = f"{path}: [{name}]"
    pr = t.get("principals")
    if not (isinstance(pr, dict) and pr):
        raise SecretsError(f'{where} principals must be a table of tier to names, e.g. {{ lead = ["ws-audit"] }}')
    out = []
    for tier, names in pr.items():
        if tier not in TIERS:
            raise SecretsError(f"{where} principals.{tier} is not a tier ({', '.join(TIERS)})")
        if not (isinstance(names, list) and all(isinstance(n, str) and PRINCIPAL_RE.match(n) for n in names)):
            raise SecretsError(f"{where} principals.{tier} must be a list of user-like names")
        out.append((tier, tuple(names)))
    v = t.get("validity_s", DEFAULT_VALIDITY_S)
    if isinstance(v, bool) or not isinstance(v, int) or not 60 <= v <= MAX_VALIDITY_S:
        raise SecretsError(f"{where} validity_s must be a whole number of seconds from 60 to {MAX_VALIDITY_S}")
    return Options(principals=tuple(sorted(out)), validity_s=v)


def check_public_key(text: object) -> str:
    """The key as `type base64` (the comment dropped). Raises MintError("bad_request") for anything else, a private
    key included."""
    if not isinstance(text, str) or len(text) > MAX_PUBKEY:
        raise MintError("bad_request", "public_key must be one OpenSSH public key line")
    m = PUBKEY_RE.match(text.strip())
    if not m:
        raise MintError("bad_request", "public_key must be one OpenSSH public key line (a private key is refused)")
    return f"{m.group(1)} {m.group(2)}"


def _ca_bytes(value: str) -> bytes:
    """The stored CA key: base64 of the OpenSSH private key file, or the file with line breaks written as \\n."""
    try:
        if value.startswith("-----BEGIN"):
            data = value.replace("\\n", "\n").encode()
        else:
            data = base64.b64decode(value.strip(), validate=True)
    except ValueError as e:
        raise MintError("store_unreadable", "the stored CA key is not base64 of a private key file") from e
    if b"PRIVATE KEY-----" not in data:
        raise MintError("store_unreadable", "the stored CA key is not a private key file")
    return data if data.endswith(b"\n") else data + b"\n"


@contextlib.contextmanager
def _key_file(value: str):
    d = Path(tempfile.mkdtemp(prefix="ws-ca-"))  # mkdtemp makes it 0700
    try:
        ca = d / "ca"
        fd = os.open(ca, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(_ca_bytes(value))
        yield d, ca
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _keygen() -> str:
    exe = shutil.which("ssh-keygen")
    if exe is None:
        raise MintError("store_unreadable", "ssh-keygen is not installed where the broker runs")
    return exe


def ca_public_key(value: str) -> str:
    """The CA's public key line, for the hosts to trust."""
    with _key_file(value) as (_d, ca):
        r = subprocess.run(
            [_keygen(), "-y", "-f", str(ca)],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
    if r.returncode != 0:
        raise MintError("store_unreadable", "ssh-keygen could not read the stored CA key")
    return " ".join(r.stdout.split()[:2])


def sign(opts: Options, ca_value: str, public_key: str, run_id: str, tier: str) -> dict:
    """{"certificate": text, "principals": [...], "serial": n, "valid_s": n} for a run's own public key."""
    principals = opts.for_tier(tier)
    if not principals:
        raise MintError("tier_refused", f"the CA gives a {tier} run no principals")
    pub = check_public_key(public_key)
    serial = int.from_bytes(os.urandom(8), "big") >> 1
    with _key_file(ca_value) as (d, ca):
        (d / "id.pub").write_text(pub + "\n", encoding="ascii")
        r = subprocess.run(
            [
                _keygen(),
                "-q",
                "-s",
                str(ca),
                "-I",
                f"ws:{run_id}",
                "-n",
                ",".join(principals),
                "-V",
                f"-1m:+{opts.validity_s}s",
                "-z",
                str(serial),
                "-O",
                "clear",
                "-O",
                "permit-pty",
                str(d / "id.pub"),
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
            stdin=subprocess.DEVNULL,
        )
        cert_file = d / "id-cert.pub"
        if r.returncode != 0 or not cert_file.is_file():
            raise MintError("store_unreadable", f"ssh-keygen could not sign (exit {r.returncode})")
        cert = cert_file.read_text(encoding="ascii").strip()
    return {"certificate": cert, "principals": list(principals), "serial": serial, "valid_s": opts.validity_s}


def host_steps(ca_public: str, user: str, principals: list[str]) -> str:
    """The steps for one host that should trust the CA. Generic: no host is named; the person runs them on each."""
    if not re.fullmatch(r"[A-Za-z0-9@.+/= -]{20,2000}", ca_public):
        raise MintError("bad_request", "that is not a public key line")
    if not PRINCIPAL_RE.match(user) or not all(PRINCIPAL_RE.match(p) for p in principals):
        raise MintError("bad_request", "user and principals must be user-like names")
    lines = "\n".join(principals)
    return f"""# Run these on each host that agents may reach with a certificate, and on no other.
# A host that must stay out of every agent's reach never gets the CA line, so no certificate can open it.
sudo tee /etc/ssh/ws_user_ca.pub >/dev/null <<'EOF'
{ca_public}
EOF
sudo install -d -m 0755 /etc/ssh/auth_principals
sudo tee /etc/ssh/auth_principals/{user} >/dev/null <<'EOF'
{lines}
EOF
sudo tee /etc/ssh/sshd_config.d/ws-ca.conf >/dev/null <<'EOF'
TrustedUserCAKeys /etc/ssh/ws_user_ca.pub
AuthorizedPrincipalsFile /etc/ssh/auth_principals/%u
EOF
sudo sshd -t && sudo systemctl reload ssh
# Your own keys and other ways in are untouched: this adds a way in, it removes none.
# Undo: sudo rm /etc/ssh/sshd_config.d/ws-ca.conf /etc/ssh/ws_user_ca.pub && sudo systemctl reload ssh
"""


# ---------------------------------------------------------------- the client side


def cmd_sign(args: argparse.Namespace) -> int:
    from ws import broker
    from ws import secrets as wsecrets

    key = Path(args.key).expanduser()
    if not key.name.endswith(".pub"):
        print("ws: --key must be the public key (the .pub file); the private key never leaves you", file=sys.stderr)
        return 2
    try:
        pub = key.read_text(encoding="ascii").strip()
    except (OSError, UnicodeDecodeError) as e:
        print(f"ws: cannot read {key}: {type(e).__name__}", file=sys.stderr)
        return 1
    try:
        reply = broker._client_call(
            {"op": OP, "name": args.name, "run": args.run, "public_key": pub},
            args.socket or str(wsecrets.socket_path()),
            timeout=60.0,
        )
    except broker.BrokerError as e:
        print(f"ws: no certificate from the broker: {e}", file=sys.stderr)
        return 1
    out = key.with_name(key.name.removesuffix(".pub") + "-cert.pub")
    out.write_text(reply["certificate"] + "\n", encoding="ascii")
    print(f"ws: certificate for {', '.join(reply['principals'])}, valid {reply['valid_s'] // 60} minutes: {out}")
    return 0


def cmd_host_steps(args: argparse.Namespace) -> int:
    from ws import broker
    from ws import secrets as wsecrets

    try:
        reply = broker._client_call(
            {"op": "ca_public", "name": args.name}, args.socket or str(wsecrets.socket_path()), timeout=30.0
        )
        print(host_steps(reply["public_key"], args.user, args.principal), end="")
    except (broker.BrokerError, MintError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m ws.minters.ssh_ca", description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sign", help="get a short certificate for your own public key")
    s.add_argument("--name", required=True, help="the registry name of the CA")
    s.add_argument("--run", required=True, help="the run id (its run record must declare the name)")
    s.add_argument("--key", required=True, help="your public key file, e.g. ~/.ssh/id_ed25519.pub")
    s.add_argument("--socket", metavar="PATH")
    s.set_defaults(func=cmd_sign)
    h = sub.add_parser("host-steps", help="print the steps for a host to trust the CA")
    h.add_argument("--name", required=True, help="the registry name of the CA")
    h.add_argument("--user", required=True, help="the login on that host the certificates are for")
    h.add_argument("--principal", action="append", required=True, help="a principal that may log in as it (repeat)")
    h.add_argument("--socket", metavar="PATH")
    h.set_defaults(func=cmd_host_steps)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
