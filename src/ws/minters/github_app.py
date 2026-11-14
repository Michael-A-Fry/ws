"""GitHub App minter: the broker holds the App's private key and mints 1-hour installation tokens narrowed to a run's repos.

Registry entry (the private key is the stored value, as one line: `base64 -w0 app.pem`):

    [github-app]
    kind = "github-app"
    app_id = "123456"                       # the App ID, or its client ID
    installation_id = 78901234              # the installation on your account or organisation
    repos = ["ws", "notes"]                 # the most a run may ask for (repository names, no owner)
    permissions = { contents = "write", pull_requests = "write" }   # the most a run may ask for
    tier = "worker"
    area = "home"

A run asks with the `mint` op (its run record must declare the name). The request names repos and permissions; each
must be inside the entry's, and a level never above it (read < write < admin). The broker signs a 9-minute JWT with the
key (RS256, in pure Python: the standard library has no RSA), asks GitHub for an installation token narrowed to those
repos and permissions, and returns the token and its expiry to that one caller. The token is never logged. On
`ws elevate off` every token minted for an elevated name is revoked upstream (`DELETE /installation/token`).

Two ways to use it, both through the broker, neither printing the token:

    git config credential.https://github.com.helper \\
        '!python3 -m ws.minters.github_app credential --name github-app --run "$WS_RUN_ID" --repo ws'
    python3 -m ws.minters.github_app gh --name github-app --run "$WS_RUN_ID" --repo ws -- pr create --fill

The first is git's credential helper protocol (git reads the token from stdout, the agent never does); the second runs
`gh` with GH_TOKEN set in its own environment only. Swap note: a fine-grained personal access token with an expiry, kept
in the store under the same name with the plain header kind, does the same job with no App, and nothing else changes.

Signing: EMSA-PKCS1-v1_5 with SHA-256 (RFC 8017 section 9.2) over a PKCS#1 or PKCS#8 RSA key. It is not constant time;
the key never leaves the broker's process and the only input an attacker controls is not the signed message, which the
broker builds itself.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

KIND = "github-app"
OP = "mint"
API = "https://api.github.com"
LEVELS = {"read": 1, "write": 2, "admin": 3}
PERMISSION_RE = re.compile(r"^[a-z][a-z_]{0,63}$")
REPO_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
APP_ID_RE = re.compile(r"^[A-Za-z0-9.]{1,64}$")
JWT_LIFETIME_S = 540  # GitHub takes at most 10 minutes; a minute's slack each side for clock drift
SHA256_DIGEST_INFO = bytes.fromhex("3031300d060960864801650304020105000420")
TIMEOUT_S = 30


class MintError(Exception):
    """Never carries the key, a JWT or a token. `code` is a broker refusal code."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Options:
    app_id: str
    installation_id: int
    repos: tuple[str, ...]
    permissions: tuple[tuple[str, str], ...]
    api: str = API

    def permission_map(self) -> dict[str, str]:
        return dict(self.permissions)


def parse(path: Path, name: str, t: dict) -> Options:
    """The entry's minter keys, checked. Raises SecretsError (imported late: ws.secrets imports this module)."""
    from ws.secrets import SecretsError

    where = f"{path}: [{name}]"
    app_id = t.get("app_id")
    if isinstance(app_id, int) and not isinstance(app_id, bool):
        app_id = str(app_id)
    if not (isinstance(app_id, str) and APP_ID_RE.match(app_id)):
        raise SecretsError(f"{where} app_id must be the App ID (a number) or its client ID")
    inst = t.get("installation_id")
    if isinstance(inst, bool) or not isinstance(inst, int) or inst <= 0:
        raise SecretsError(f"{where} installation_id must be a number above 0")
    repos = t.get("repos")
    if not (isinstance(repos, list) and repos and all(isinstance(r, str) and REPO_RE.match(r) for r in repos)):
        raise SecretsError(f"{where} repos must be a list of repository names (no owner), at least one")
    perms = t.get("permissions")
    if not (isinstance(perms, dict) and perms):
        raise SecretsError(f'{where} permissions must be a table such as {{ contents = "write" }}')
    for k, v in perms.items():
        if not PERMISSION_RE.match(k) or v not in LEVELS:
            raise SecretsError(f"{where} permissions.{k} must be read, write or admin")
    api = t.get("api", API)
    if not isinstance(api, str) or urlsplit(api).scheme not in ("https", "http") or not urlsplit(api).hostname:
        raise SecretsError(f"{where} api must be a URL such as {API}")
    return Options(
        app_id=app_id,
        installation_id=inst,
        repos=tuple(repos),
        permissions=tuple(sorted(perms.items())),
        api=api.rstrip("/"),
    )


EXTRA_KEYS = frozenset({"app_id", "installation_id", "repos", "permissions", "api"})


def narrow(opts: Options, repos: object, permissions: object) -> tuple[tuple[str, ...], dict[str, str]]:
    """The repos and permissions a run asked for, checked against the entry's. No permissions asked: the entry's own.
    Raises MintError("bad_request") for a malformed ask and MintError("not_declared") for one above the entry."""
    if not (isinstance(repos, list) and repos and all(isinstance(r, str) and REPO_RE.match(r) for r in repos)):
        raise MintError("bad_request", "repos must be a list of repository names, at least one")
    extra = sorted(set(repos) - set(opts.repos))
    if extra:
        raise MintError("not_declared", f"the entry does not allow {', '.join(extra)}")
    cap = opts.permission_map()
    if permissions is None:
        return tuple(dict.fromkeys(repos)), cap
    if not (isinstance(permissions, dict) and permissions):
        raise MintError("bad_request", "permissions must be an object of name to read, write or admin")
    for k, v in permissions.items():
        if not (isinstance(k, str) and PERMISSION_RE.match(k)) or v not in LEVELS:
            raise MintError("bad_request", "permissions must be an object of name to read, write or admin")
        if k not in cap or LEVELS[v] > LEVELS[cap[k]]:
            raise MintError("not_declared", f"the entry does not allow {k}: {v}")
    return tuple(dict.fromkeys(repos)), dict(sorted(permissions.items()))


# ---------------------------------------------------------------- the key and the JWT


@dataclass(frozen=True)
class RsaKey:
    n: int
    d: int
    e: int = 65537

    def __repr__(self) -> str:  # never the numbers
        return f"RsaKey({self.n.bit_length()} bits)"

    @property
    def size(self) -> int:
        return (self.n.bit_length() + 7) // 8


def _der(data: bytes, i: int) -> tuple[int, bytes, int]:
    """(tag, value, next index) of the DER element at i. Raises ValueError."""
    if i + 2 > len(data):
        raise ValueError("short")
    tag, n = data[i], data[i + 1]
    i += 2
    if n & 0x80:
        k = n & 0x7F
        if k == 0 or k > 4 or i + k > len(data):
            raise ValueError("bad length")
        n = int.from_bytes(data[i : i + k], "big")
        i += k
    if i + n > len(data):
        raise ValueError("short")
    return tag, data[i : i + n], i + n


def _ints(seq: bytes) -> list[int]:
    out, i = [], 0
    while i < len(seq):
        tag, v, i = _der(seq, i)
        if tag != 0x02:
            raise ValueError("not an integer")
        out.append(int.from_bytes(v, "big"))
    return out


def _rsa_from_der(der: bytes) -> RsaKey:
    tag, body, _ = _der(der, 0)
    if tag != 0x30:
        raise ValueError("not a sequence")
    tag, _version, i = _der(body, 0)
    if tag != 0x02:
        raise ValueError("no version")
    tag2, _v, j = _der(body, i)
    if tag2 == 0x30:  # PKCS#8: version, algorithm, then the PKCS#1 key in an octet string
        tag3, inner, _ = _der(body, j)
        if tag3 != 0x04:
            raise ValueError("no key")
        return _rsa_from_der(inner)
    nums = _ints(body)
    if len(nums) < 9 or nums[0] != 0:
        raise ValueError("not an RSA private key")
    return RsaKey(n=nums[1], d=nums[3], e=nums[2])


def load_key(value: str) -> RsaKey:
    """The App key from the stored value: one line of base64 of the PEM file (`base64 -w0 app.pem`), base64 of the DER,
    or the PEM with its line breaks written as \\n. Raises MintError("store_unreadable"), never with the value."""
    try:
        text = value.replace("\\n", "\n") if value.startswith("-----BEGIN") else None
        if text is None:
            raw = base64.b64decode(value.strip(), validate=True)
            text = raw.decode("ascii") if raw.startswith(b"-----BEGIN") else None
            der = raw if text is None else b""
        if text is not None:
            m = re.search(r"-----BEGIN (RSA )?PRIVATE KEY-----(.+?)-----END (RSA )?PRIVATE KEY-----", text, re.DOTALL)
            if not m:
                raise ValueError("not a PEM private key")
            der = base64.b64decode("".join(m.group(2).split()), validate=True)
        key = _rsa_from_der(der)
    except (ValueError, UnicodeDecodeError) as e:
        raise MintError("store_unreadable", "the stored App key is not an RSA private key") from e
    if key.n.bit_length() < 2048:
        raise MintError("store_unreadable", "the stored App key is shorter than 2048 bits")
    return key


def sign_rs256(key: RsaKey, message: bytes) -> bytes:
    """RSASSA-PKCS1-v1_5 with SHA-256 (RFC 8017 8.2.1)."""
    t = SHA256_DIGEST_INFO + hashlib.sha256(message).digest()
    k = key.size
    if k < len(t) + 11:
        raise MintError("store_unreadable", "the App key is too short")
    em = b"\x00\x01" + b"\xff" * (k - len(t) - 3) + b"\x00" + t
    return pow(int.from_bytes(em, "big"), key.d, key.n).to_bytes(k, "big")


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def app_jwt(app_id: str, key: RsaKey, now: float) -> str:
    """The App's JWT: issued a minute in the past, over in 9 minutes (GitHub's limit is 10)."""
    head = _b64url(json.dumps({"alg": "RS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64url(
        json.dumps(
            {"iat": int(now) - 60, "exp": int(now) + JWT_LIFETIME_S, "iss": app_id}, separators=(",", ":")
        ).encode()
    )
    signing = f"{head}.{body}".encode()
    return f"{head}.{body}.{_b64url(sign_rs256(key, signing))}"


# ---------------------------------------------------------------- GitHub


def _connection(api: str, allow_plain_http: bool) -> tuple[http.client.HTTPConnection, str]:
    import ssl

    u = urlsplit(api)
    if u.scheme == "http":
        if not (allow_plain_http and u.hostname == "127.0.0.1"):
            raise MintError("host_not_allowed", "the App API must be https")
        return http.client.HTTPConnection(u.hostname, u.port or 80, timeout=TIMEOUT_S), u.path
    return (
        http.client.HTTPSConnection(u.hostname, u.port or 443, timeout=TIMEOUT_S, context=ssl.create_default_context()),
        u.path,
    )


def _call(api: str, method: str, path: str, auth: str, body: dict | None, allow_plain_http: bool) -> tuple[int, bytes]:
    conn, prefix = _connection(api, allow_plain_http)
    headers = {
        "Accept": "application/vnd.github+json",
        "Authorization": auth,
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "ws-broker",
    }
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        conn.request(method, prefix + path, body=data, headers=headers)
        r = conn.getresponse()
        return r.status, r.read(1 << 20)
    except (OSError, http.client.HTTPException) as e:
        raise MintError("upstream_unreachable", f"GitHub did not answer ({type(e).__name__})") from e
    finally:
        conn.close()


def mint(
    opts: Options,
    key_value: str,
    repos: tuple[str, ...],
    permissions: dict[str, str],
    now: float | None = None,
    allow_plain_http: bool = False,
) -> tuple[str, float]:
    """(token, expires at as a Unix time) for an installation token narrowed to repos and permissions."""
    now = time.time() if now is None else now
    jwt = app_jwt(opts.app_id, load_key(key_value), now)
    status, data = _call(
        opts.api,
        "POST",
        f"/app/installations/{opts.installation_id}/access_tokens",
        f"Bearer {jwt}",
        {"repositories": list(repos), "permissions": permissions},
        allow_plain_http,
    )
    if status != 201:
        raise MintError("upstream_refused", f"GitHub answered {status} to the token request")
    try:
        reply = json.loads(data)
        token, exp = reply["token"], reply["expires_at"]
        if not (isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9_]{20,255}", token)):
            raise ValueError("token")
        expires = datetime.fromisoformat(str(exp)).timestamp()
    except (ValueError, KeyError, TypeError) as e:
        raise MintError("upstream_refused", "GitHub's token reply could not be read") from e
    return token, expires


def revoke(opts: Options, token: str, allow_plain_http: bool = False) -> bool:
    """Revoke an installation token upstream. True when GitHub says it is gone (204), or was already (401)."""
    status, _ = _call(opts.api, "DELETE", "/installation/token", f"Bearer {token}", None, allow_plain_http)
    return status in (204, 401)


# ---------------------------------------------------------------- the client side: git and gh


def _ask(args: argparse.Namespace, repos: list[str]) -> dict:
    from ws import broker
    from ws import secrets as wsecrets

    perms = None
    if args.perm:
        perms = {}
        for p in args.perm:
            k, _, v = p.partition("=")
            perms[k] = v
    msg = {"op": OP, "name": args.name, "run": args.run, "repos": repos}
    if perms is not None:
        msg["permissions"] = perms
    sock = args.socket or str(wsecrets.socket_path())
    return broker._client_call(msg, sock, timeout=60.0)


def _git_fields(stream) -> dict[str, str]:
    out = {}
    for line in stream.read(65536).splitlines():
        if not line:
            break
        k, _, v = line.partition("=")
        out[k] = v
    return out


def cmd_credential(args: argparse.Namespace) -> int:
    """git credential helper: answers `get` for https://github.com, ignores `store` and `erase`."""
    if args.action != "get":
        return 0
    f = _git_fields(sys.stdin)
    if f.get("protocol") != "https" or f.get("host") != "github.com":
        return 0  # not ours: git asks the next helper
    repos = list(args.repo or ())
    path = f.get("path", "")
    if not repos and path.count("/") >= 1:
        repos = [path.split("/")[1].removesuffix(".git")]
    if not repos:
        print("ws: name the repository with --repo, or set credential.useHttpPath", file=sys.stderr)
        return 1
    try:
        reply = _ask(args, repos)
    except Exception as e:  # noqa: BLE001 (git gets no credential and says so; the reason goes to stderr)
        print(f"ws: no GitHub token from the broker: {e}", file=sys.stderr)
        return 1
    sys.stdout.write(f"username=x-access-token\npassword={reply['token']}\n")
    return 0


def cmd_gh(args: argparse.Namespace) -> int:
    """Run gh with a minted token in its environment only."""
    import shutil

    if not args.repo:
        print("ws: name the repository with --repo", file=sys.stderr)
        return 2
    try:
        reply = _ask(args, list(args.repo))
    except Exception as e:  # noqa: BLE001
        print(f"ws: no GitHub token from the broker: {e}", file=sys.stderr)
        return 1
    gh = shutil.which("gh")
    if gh is None:
        print("ws: gh is not installed", file=sys.stderr)
        return 1
    env = {k: v for k, v in os.environ.items() if k not in ("GH_TOKEN", "GITHUB_TOKEN")}
    env["GH_TOKEN"] = reply["token"]
    rest = args.gh_args[1:] if args.gh_args[:1] == ["--"] else args.gh_args
    os.execve(gh, [gh, *rest], env)
    return 1  # not reached


def setup_steps(name: str = "github-app") -> str:
    """The person's steps to make and install the App and hand its key to the broker. Nothing here runs anything."""
    return f"""Make a GitHub App for the broker (once, in a browser; ws runs nothing against your account):

1. GitHub > Settings > Developer settings > GitHub Apps > New GitHub App.
   - GitHub App name: any name that is free, e.g. "<you>-ws-broker".
   - Homepage URL: any URL you own (your profile page will do).
   - Webhook: untick Active. The broker needs no webhook.
   - Repository permissions: Contents: Read and write; Pull requests: Read and write; Metadata: Read-only (forced).
     Add others only if a job needs them. A token can be narrowed below these, never above.
   - Where can this GitHub App be installed: Only on this account.
   - Create GitHub App. Note the App ID at the top of the page.
2. On the same page: Private keys > Generate a private key. A .pem file downloads.
3. Install App (left menu) > Install > Only select repositories > pick the repos jobs may touch > Install.
   The address ends in /installations/<number>: that number is the installation_id.
4. Register it in secrets.toml (names only, no key):

   [{name}]
   kind = "github-app"
   app_id = "<App ID>"
   installation_id = <installation number>
   repos = ["<repo>", "<repo>"]
   permissions = {{ contents = "write", pull_requests = "write" }}
   tier = "worker"
   area = "<area>"

5. Give the broker the key, in your own terminal, then delete the download:

   base64 -w0 ~/Downloads/<app>.pem | ws secret set {name}
   rm ~/Downloads/<app>.pem

   (macOS: base64 -i ~/Downloads/<app>.pem | ws secret set {name})
6. Declare the name in the job's run record, then point git at the helper:

   git config --global credential.https://github.com.helper \\
     '!python3 -m ws.minters.github_app credential --name {name} --run "$WS_RUN_ID" --repo <repo>'

Undo: GitHub > Settings > Applications > Installed GitHub Apps > Configure > Uninstall; then
`ws secret rm {name}` and remove the [{name}] table. Tokens already minted end within the hour.
"""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="python3 -m ws.minters.github_app", description=__doc__.split("\n", 1)[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, text in (("credential", "a git credential helper"), ("gh", "run gh with a minted token")):
        c = sub.add_parser(name, help=text)
        c.add_argument("--name", required=True, help="the registry name of the App")
        c.add_argument("--run", required=True, help="the run id (its run record must declare the name)")
        c.add_argument("--repo", action="append", help="a repository the token is for (repeat)")
        c.add_argument("--perm", action="append", metavar="NAME=LEVEL", help="narrow further, e.g. contents=read")
        c.add_argument("--socket", metavar="PATH")
    sub.choices["credential"].add_argument("action", help="what git asks: get, store or erase")
    sub.choices["credential"].set_defaults(func=cmd_credential)
    sub.choices["gh"].add_argument("gh_args", nargs=argparse.REMAINDER, help="-- then gh's own arguments")
    sub.choices["gh"].set_defaults(func=cmd_gh)
    st = sub.add_parser("setup-steps", help="print the steps to make and install the App")
    st.add_argument("--name", default="github-app", help="the registry name to use")
    st.set_defaults(func=lambda a: print(setup_steps(a.name), end="") or 0)
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
