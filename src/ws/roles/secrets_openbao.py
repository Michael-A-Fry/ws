"""Secrets role, `openbao`: the broker reads values from an OpenBao (or Vault) KV v2 mount instead of ws's own store.

Optional, and never required: the default `ws-secret` role keeps one 0600 file per name and needs no vault. Choose this
one with `[roles.secrets] adapter = "openbao"` and these keys:

    addr = "https://bao.example.org:8200"   # http is accepted for a loopback address only
    mount = "secret"                        # the KV v2 mount (default "secret")
    prefix = "ws"                           # names live at <mount>/data/<prefix>/<name>, field `value` (default "ws")
    token_file = "~/.config/ws/bao-token"   # mode 0600, yours; the token is read per request, never kept in the profile

The registry, tiers, areas, run records, leases, audit and the proxy are unchanged: only where a value is read from
differs. Values are written in OpenBao (its own CLI or UI), not with `ws secret set`. Nothing here installs OpenBao.

Swap note (DEC-017): the contract is the `read(name)`, `names()`, `loose()` and `problems()` of a store, so any other
backend is one module like this one. Switching back is one line in the profile; values do not move by themselves.
"""

from __future__ import annotations

import json
import os
import stat
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ws import secrets as wsecrets
from ws.roles import FAIL, OK

ROLE, NAME = "secrets", "openbao"
TIMEOUT_S = 5.0
_LOOPBACK = ("127.0.0.1", "localhost", "::1", "[::1]")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the token to another host."""

    def redirect_request(self, *a, **k):
        return None


class OpenBaoStore:
    """The store interface the broker uses, over an OpenBao KV v2 mount. `opener` is a test seam."""

    remote = True

    def __init__(self, addr: str, mount: str, prefix: str, token_file: Path, opener=None):
        self.addr, self.mount, self.prefix, self.token_file = (
            addr.rstrip("/"),
            mount.strip("/"),
            prefix.strip("/"),
            token_file,
        )
        self.path = token_file  # what `health` stats: the token file, so a loose one is reported
        # no proxy: an environment proxy would see the token (urllib does not bypass loopback); no redirect: it would carry it on
        self._open = opener or urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open

    def _token(self) -> str:
        try:
            fd = os.open(self.token_file, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(fd, "rb") as fh:  # O_NONBLOCK: a FIFO in its place cannot hang the read
                st = os.fstat(fh.fileno())
                if not stat.S_ISREG(st.st_mode) or st.st_mode & 0o077:
                    raise wsecrets.SecretsError("the OpenBao token file is open to other users")
                if st.st_uid != os.geteuid():
                    raise wsecrets.SecretsError("the OpenBao token file is not yours")
                tok = fh.read(4097).decode("utf-8").strip()
        except FileNotFoundError as e:
            raise wsecrets.SecretsError("the OpenBao token file does not exist") from e
        except (OSError, UnicodeDecodeError) as e:
            raise wsecrets.SecretsError("the OpenBao token file cannot be read") from e
        if not tok or len(tok) > 4096 or any(c.isspace() or ord(c) < 32 for c in tok):
            raise wsecrets.SecretsError("the OpenBao token file does not hold a usable token")
        return tok

    def _call(self, url: str, method: str = "GET") -> dict | None:
        req = urllib.request.Request(url, method=method, headers={"X-Vault-Token": self._token()})
        try:
            with self._open(req, timeout=TIMEOUT_S) as r:
                return json.loads(r.read(1 << 20) or b"{}")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            raise wsecrets.SecretsError(f"OpenBao answered {e.code}") from e
        except (urllib.error.URLError, OSError, ValueError, TimeoutError) as e:
            raise wsecrets.SecretsError("OpenBao could not be reached") from e

    def _url(self, kind: str, name: str = "") -> str:
        tail = "/".join(p for p in (self.prefix, name) if p)
        return f"{self.addr}/v1/{self.mount}/{kind}/{urllib.parse.quote(tail, safe='/')}"

    def read(self, name: str) -> str:
        """The value. FileNotFoundError if unset, SecretsError if the backend or the token cannot be trusted."""
        if not wsecrets.NAME_RE.match(name):
            raise wsecrets.SecretsError("not a valid secret name")
        d = self._call(self._url("data", name))
        if d is None:
            raise FileNotFoundError(name)
        try:
            v = d["data"]["data"]["value"]
        except (KeyError, TypeError) as e:
            raise wsecrets.SecretsError("an OpenBao entry has no usable `value` field") from e
        if not isinstance(v, str):
            raise wsecrets.SecretsError("an OpenBao entry has no usable `value` field")
        try:
            return wsecrets.check_value(v)
        except wsecrets.SecretsError as e:
            raise wsecrets.SecretsError("an OpenBao entry does not hold a usable value") from e

    def names(self) -> list[str]:
        d = self._call(self._url("metadata") + "?list=true")
        keys = (d or {}).get("data", {}).get("keys", []) if isinstance(d, dict) else []
        return sorted(k for k in keys if isinstance(k, str) and wsecrets.NAME_RE.match(k))

    def loose(self) -> list[str]:
        try:
            self._token()
        except wsecrets.SecretsError as e:
            return [str(e)]
        return []

    def problems(self) -> list[str]:
        return self.loose()


def store_from_cfg(cfg: dict, env=None, opener=None) -> OpenBaoStore:
    addr, mount, prefix, tf = (
        cfg.get("addr"),
        cfg.get("mount", "secret"),
        cfg.get("prefix", "ws"),
        cfg.get("token_file"),
    )
    for k, v in (("addr", addr), ("mount", mount), ("prefix", prefix), ("token_file", tf)):
        if not isinstance(v, str) or not v:
            raise wsecrets.SecretsError(f"[roles.secrets] {k} is needed for the openbao adapter")
    u = urllib.parse.urlsplit(addr)
    if not (u.scheme == "https" or (u.scheme == "http" and (u.hostname or "") in _LOOPBACK)):
        raise wsecrets.SecretsError("[roles.secrets] addr must be https (http only for a loopback address)")
    if not u.hostname or u.username or u.password:
        raise wsecrets.SecretsError("[roles.secrets] addr must be a plain host address")
    return OpenBaoStore(addr, mount, prefix, Path(tf).expanduser(), opener)


def check(cfg, osi, env, network):
    try:
        store = store_from_cfg(cfg, env)
    except wsecrets.SecretsError as e:
        return FAIL, str(e)
    if bad := store.loose():
        return FAIL, "; ".join(bad)
    if network:
        try:
            store.names()
        except wsecrets.SecretsError as e:
            return FAIL, str(e)
    return OK, f"openbao: {store.addr}/{store.mount}/{store.prefix}"


def steps(cfg, answers, env):
    """Nothing: OpenBao, its token and its values are yours to set up."""
    return []
