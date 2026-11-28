"""A fake release for the upgrade proof's tests: a git repository with a really signed tag (a throwaway key), assets with
their hashes, bundles a stub `cosign` checks by digest and identity, and a stub `uv audit`. Nothing reaches the network.

`world(tmp_path, monkeypatch)` in a test makes the stub tools folder, a key, and the trust root that lists its public half;
`Release` is the `Source` the proof reads (`ws.upgradeproof.Source`).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from ws import upgradeproof

GIT, KEYGEN = shutil.which("git"), shutil.which("ssh-keygen")
NEEDS = "needs git and ssh-keygen"

TAG = "v0.1.11.8"
REPO = "owner/ws"
IDENTITY = f"https://github.com/{REPO}/.github/workflows/release.yml@refs/tags/{{tag}}"
ISSUER = "https://token.actions.githubusercontent.com"
STUB_INSTALLER = b"#!/bin/sh\nexit 0\n"

COSIGN = """#!{py}
import hashlib, json, os, sys
a = sys.argv[1:]
opt = lambda n: a[a.index(n) + 1]
open(os.path.join(os.path.dirname(__file__), "cosign.log"), "a").write(" ".join(a) + "\\n")
b = json.load(open(opt("--bundle")))
ok = (b["digest"] == hashlib.sha256(open(a[-1], "rb").read()).hexdigest()
      and b["identity"] == opt("--certificate-identity") and b["issuer"] == opt("--certificate-oidc-issuer"))
sys.exit(0 if ok else 1)
"""
UV = """#!{py}
import os, sys
sys.stdout.write(open(os.path.join(os.path.dirname(__file__), "audit.json")).read())
"""


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def git_env() -> dict[str, str]:
    """The environment a test's own git runs in: no user or system config, a fixed author."""
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env |= {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    return env


class Release:
    """A fake release of `tag`: a signed git tag, the installer and wheel assets with hashes and bundles, check runs."""

    def __init__(
        self, root: Path, key: Path, tag: str = TAG, signed: bool = True, installer: bytes = STUB_INSTALLER
    ) -> None:
        self.tag, self.root = tag, root
        self.runs = [{"name": "ci", "status": "completed", "conclusion": "success"}]
        self.wheel_name = f"ws_aitoolkit-{tag[1:]}-py3-none-any.whl"
        origin = root / "origin"
        origin.mkdir(parents=True)
        (origin / "uv.lock").write_text("version = 1\n", encoding="utf-8")
        sign = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"]
        tag_cmd = [*sign, "tag", "-s", "-m", tag, tag] if signed else ["tag", tag]
        for argv in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"], tag_cmd):
            subprocess.run([GIT, *argv], cwd=origin, env=git_env(), check=True, capture_output=True)
        self.origin = origin
        self.assets: dict[str, bytes] = {}
        self.add("install.sh", installer)
        self.add(self.wheel_name, b"wheel bytes")

    def add(self, name: str, data: bytes) -> None:
        self.assets[name] = data
        self.assets[f"{name}.sha256"] = f"{sha(data)}  {name}\n".encode()
        bundle = {"digest": sha(data), "identity": IDENTITY.format(tag=self.tag), "issuer": ISSUER}
        self.assets[f"{name}.sigstore.json"] = json.dumps(bundle).encode()

    def asset(self, tag: str, name: str) -> bytes:
        if name not in self.assets:
            raise upgradeproof.Refused(f"could not read {name} (HTTPError)")
        return self.assets[name]

    def checkout(self, tag: str, dest: Path) -> None:
        subprocess.run([GIT, "clone", "-q", str(self.origin), str(dest)], check=True, capture_output=True)

    def check_runs(self, sha_: str) -> list[dict]:
        return self.runs


def keygen(path: Path) -> str:
    """Make an ed25519 key at `path` and return the public half's key blob."""
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(path), "-C", ""], check=True)
    return path.with_suffix(".pub").read_text(encoding="utf-8").split()[1]


def stub_tools(folder: Path) -> Path:
    """A folder with a `cosign` that verifies a bundle by digest and identity and a `uv` that prints `audit.json`."""
    folder.mkdir(exist_ok=True)
    for name, text in (("cosign", COSIGN), ("uv", UV)):
        tool = folder / name
        tool.write_text(text.format(py=sys.executable), encoding="utf-8")
        tool.chmod(0o755)
    (folder / "audit.json").write_text(json.dumps({"vulnerabilities": []}), encoding="utf-8")
    return folder


def trust_root(blob: str) -> dict[str, str]:
    return {
        "repo": REPO,
        "identity": IDENTITY,
        "issuer": ISSUER,
        "tag_signers": f'ws-release namespaces="git" ssh-ed25519 {blob}\n',
    }
