"""Secrets role, `sops-age`: the secrets live in one sops-encrypted file, opened with an age key the agent user cannot read.

    [roles.secrets]
    adapter = "sops-age"
    file = "/var/lib/ws-broker/secrets.sops.yaml"     a sops file: {name: value, ...}
    key_file = "/var/lib/ws-broker/age-key.txt"       the age key; owned by another user, mode 0600 or 0400

It stands in for ws's own store (ws.secrets.Store): the registry still says which names go to which hosts, and agents
still reach a value only through the broker. `SopsStore.read` has the same shape as `Store.read`, so the broker can
use it in place of the store. The check looks at the key file's owner and mode and never at its content.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

from ws import secrets as wsecrets
from ws.roles import FAIL, OK, WARN

ROLE, NAME = "secrets", "sops-age"
TIMEOUT_S = 20.0


class SopsStore:
    """`read(name)` as ws.secrets.Store does it: FileNotFoundError if unset, SecretsError if it cannot be trusted."""

    def __init__(self, file: Path, key_file: Path, run=subprocess.run, sops: str = "sops"):
        self.file, self.key_file, self.run, self.sops = file, key_file, run, sops

    def read(self, name: str) -> str:
        if not wsecrets.NAME_RE.match(name):
            raise wsecrets.SecretsError("not a valid secret name")
        env = {"PATH": os.environ.get("PATH", ""), "SOPS_AGE_KEY_FILE": str(self.key_file)}
        try:
            p = self.run(
                [self.sops, "decrypt", "--extract", f'["{name}"]', str(self.file)],
                env=env, capture_output=True, text=True, timeout=TIMEOUT_S, check=False, stdin=subprocess.DEVNULL,
            )  # fmt: skip
        except (OSError, subprocess.SubprocessError) as e:
            raise wsecrets.SecretsError("sops did not run") from e
        if p.returncode:
            # sops exits 128+ for a missing key in the file; its message could name the file but never a value
            if "not found" in p.stderr.lower():
                raise FileNotFoundError(name)
            raise wsecrets.SecretsError("sops could not decrypt the file")
        return wsecrets.check_value(p.stdout.rstrip("\r\n"))


def _path(cfg: dict, key: str) -> Path | None:
    v = cfg.get(key)
    return Path(v).expanduser() if isinstance(v, str) and v else None


def key_problem(key_file: Path) -> tuple[str, str] | None:
    """(status, why) if the age key is not safely kept, else None. Looks at the owner and mode only."""
    try:
        st = key_file.stat()
    except FileNotFoundError:
        return FAIL, f"the age key {key_file} does not exist"
    except PermissionError:
        return None  # a folder this user cannot enter: the key is kept from us, which is the point
    except OSError as e:
        return FAIL, f"the age key {key_file} cannot be checked ({type(e).__name__})"
    if not stat.S_ISREG(st.st_mode):
        return FAIL, f"{key_file} is not a file"
    if st.st_mode & 0o077:
        return FAIL, f"the age key {key_file} is open to other users (mode {st.st_mode & 0o777:04o}, want 0600)"
    if st.st_uid == os.geteuid():
        return WARN, f"the age key {key_file} belongs to the same user the agent runs as, so the agent can read it"
    return None


def check(cfg, osi, env, network):
    file, key_file = _path(cfg, "file"), _path(cfg, "key_file")
    if (
        file is None and key_file is None
    ):  # nothing named yet: say what a full check needs, do not fail an unset-up role
        return OK, "sops-age (named; add file and key_file to have them checked)"
    if file is None or key_file is None:
        return FAIL, f"set both file and key_file ({'file' if file is None else 'key_file'} is missing)"
    missing = [t for t in ("sops", "age") if not shutil.which(t, path=env.get("PATH"))]
    if missing:
        return FAIL, f"{' and '.join(missing)} not on PATH"
    try:
        if not file.is_file():
            return FAIL, f"the sops file {file} does not exist"
    except PermissionError:
        pass  # kept from this user, like the key
    problem = key_problem(key_file)
    if problem:
        return problem
    return OK, f"sops-age: {file.name} opened with a key the agent user cannot read"


def steps(cfg, answers, env):
    """None: the file and the age key are made by a person with sops and age (ws never holds a key)."""
    return []
