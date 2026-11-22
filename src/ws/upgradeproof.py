"""What `ws upgrade --unattended` proves about a release before it installs it, and how it reads a request.

The request is a wish, never trust: one line, a release tag or the word `rollback`, read as data (`parse_request`), and
anything else is dropped. The proof is the service's own and starts from the trust root of the ws that is installed
(`ws.version.trust_root`: the release workflow's identity and the allowed signers of a release tag), never from the
request, an environment variable or the release it is about to install, because a newer release can only be trusted
through the one already there. `verify_release` refuses, with a reason, unless all of these hold for the tag:

1. the tag is signed by one of the stamp's signers (`git tag -v` against an allowed-signers file made from the stamp);
2. the installer and the wheel each match their `.sha256`, and each `.sigstore.json` bundle verifies (`cosign verify-blob`)
   as made by the release workflow's identity for this tag, so the installer that runs is the one the workflow built;
3. every check run on the tag's commit has finished and none failed (the public API, no token);
4. the tag's lock has no vulnerability of high or critical severity (`uv audit`; a vulnerability with no severity, or
   output that can't be read, counts as high, because a gate that can't tell fails closed).

Every network read goes through one `Source` (GitHub over https; a test hands over a fake tree), and every tool is found on
the PATH given, never from the request. `no downgrade`: a tag at or below the installed version is refused (`version_key`
reads four-part versions such as 0.1.11.8 and release candidates, which sort below their release).

Swap note: cosign and `uv audit` are the two tools this leans on, each in one function (`_cosign`, `_audit`); a different
signer or scanner replaces that function and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Protocol

ROLLBACK = "rollback"
_TAG = re.compile(r"v[0-9]+(?:\.[0-9]+){1,3}(?:rc[0-9]+)?")
_VERSION = re.compile(r"v?([0-9]+(?:\.[0-9]+)*)(?:(rc)([0-9]+))?")
ASSET_LIMIT = 100 << 20  # bytes of one release file
TOOL_TIMEOUT_S = 120
BAD_SEVERITIES = frozenset({"high", "critical"})
SAFE_RUNS = frozenset({"success", "skipped", "neutral"})  # a check run that ended without failing


class Refused(Exception):
    """A release or a request that is not taken, with the reason line the log and the notice carry."""


def parse_request(text: str) -> str | None:
    """The tag (`v0.1.11.8`, `v0.2.0rc1`) or `rollback` a request file holds, else None: one line, nothing else."""
    word = text.strip()
    if "\n" in word or len(word) > 40:
        return None
    return word if word == ROLLBACK or _TAG.fullmatch(word) else None


def version_key(version: str) -> tuple[int, ...] | None:
    """A sort key for `0.1.11.8`, `v0.2.0` or `0.2.0rc1` (padded to four parts; a candidate sorts below its release), or
    None when the text isn't a release version (a dev or local version is one: it can't be told from a release)."""
    m = _VERSION.fullmatch(version.strip())
    if not m:
        return None
    parts = [int(n) for n in m.group(1).split(".")][:4]
    parts += [0] * (4 - len(parts))
    return (*parts, 0 if m.group(2) else 1, int(m.group(3) or 0))


def refuse_old(tag: str, installed: str) -> None:
    """Raise `Refused` unless `tag` is newer than the installed version (no downgrade, no re-install)."""
    new, have = version_key(tag), version_key(installed)
    if new is None or have is None:
        raise Refused(f"cannot compare {tag} with the installed ws {installed}, so it is refused")
    if new <= have:
        raise Refused(f"{tag} is not newer than the installed ws {installed}: an upgrade never goes down or stays")


class Source(Protocol):
    """Where a release is read from. `GitHub` is the real one; a test hands over a tree it made."""

    def asset(self, tag: str, name: str) -> bytes: ...
    def checkout(self, tag: str, dest: Path) -> None:
        """Put a git repository with `tag` (the tag object itself) and its files in `dest`."""

    def check_runs(self, sha: str) -> list[dict]: ...


class GitHub:
    """The public GitHub of one repository: releases and the tag over https, the check runs from the unauthenticated
    API. No token is read or sent, so nothing in the unit's environment can leak."""

    def __init__(self, repo: str, web: str = "https://github.com", api: str = "https://api.github.com") -> None:
        if not re.fullmatch(r"[A-Za-z0-9._-]+/[A-Za-z0-9._-]+", repo) or not web.startswith("https://"):
            raise Refused("the trust root names a repository ws cannot read over https")
        self.repo, self.web, self.api = repo, web, api

    def _get(self, url: str, limit: int) -> bytes:
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:  # https, checked in __init__
                data = resp.read(limit + 1)
        except OSError as e:
            raise Refused(f"could not read {url.rsplit('/', 1)[-1]} ({type(e).__name__})") from e
        if len(data) > limit:
            raise Refused(f"{url.rsplit('/', 1)[-1]} is larger than {limit} bytes")
        return data

    def asset(self, tag: str, name: str) -> bytes:
        return self._get(f"{self.web}/{self.repo}/releases/download/{tag}/{name}", ASSET_LIMIT)

    def checkout(self, tag: str, dest: Path) -> None:
        url = f"{self.web}/{self.repo}.git"
        steps = [
            ["init", "-q", str(dest)],
            ["-C", str(dest), "fetch", "-q", "--depth", "1", url, f"refs/tags/{tag}:refs/tags/{tag}"],
            ["-C", str(dest), "checkout", "-q", "--detach", tag],
        ]
        for argv in steps:
            _run(["git", *argv], clean_env(os.environ))

    def check_runs(self, sha: str) -> list[dict]:
        body = json.loads(self._get(f"{self.api}/repos/{self.repo}/commits/{sha}/check-runs?per_page=100", 4 << 20))
        runs = body.get("check_runs") if isinstance(body, dict) else None
        if not isinstance(runs, list) or body.get("total_count") != len(runs):
            raise Refused("the check runs of the release commit could not be read in full")
        return runs


def clean_env(env: Mapping[str, str]) -> dict[str, str]:
    """What a tool the service starts may see: the PATH, the home and the locale, and nothing else. A mirror address, a
    skip-the-checks switch or a token set for some other purpose never reaches the installer or a verifier."""
    return {
        k: env[k] for k in ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "XDG_STATE_HOME", "XDG_DATA_HOME") if k in env
    }


def _run(argv: list[str], env: Mapping[str, str], cwd: Path | None = None) -> str:
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=TOOL_TIMEOUT_S,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise Refused(f"{argv[0]} could not run ({type(e).__name__})") from e
    if p.returncode != 0:
        last = (p.stderr or p.stdout).strip().splitlines()[-1:] or [""]
        raise Refused(f"{' '.join(argv[:3])[:60]} failed (exit {p.returncode}): {last[0][:200]}")
    return p.stdout


def _tool(name: str, env: Mapping[str, str]) -> str:
    found = shutil.which(name, path=env.get("PATH"))
    if not found:
        raise Refused(f"{name} is not installed on this machine's PATH, so a release can't be proved; install it")
    return found


def _tag_signature(repo: Path, tag: str, root: Mapping[str, str], scratch: Path, env: Mapping[str, str]) -> str:
    """Check the tag is signed by an allowed signer and return the commit it names."""
    signers = scratch / "allowed_signers"
    signers.write_text(root["tag_signers"], encoding="utf-8")
    git = _tool("git", env)
    kind = _run([git, "-C", str(repo), "cat-file", "-t", f"refs/tags/{tag}"], env).strip()
    if kind != "tag":
        raise Refused(f"{tag} is not a signed (annotated) tag")
    cfg = ["-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={signers}"]
    _run([git, "-C", str(repo), *cfg, "tag", "-v", tag], env)
    return _run([git, "-C", str(repo), "rev-parse", f"{tag}^{{commit}}"], env).strip()


def _cosign(blob: Path, bundle: Path, identity: str, issuer: str, env: Mapping[str, str]) -> None:
    cmd = [_tool("cosign", env), "verify-blob", "--bundle", str(bundle)]
    _run([*cmd, "--certificate-identity", identity, "--certificate-oidc-issuer", issuer, str(blob)], env)


def _asset(src: Source, tag: str, name: str, scratch: Path, identity: str, issuer: str, env: Mapping[str, str]) -> Path:
    """Fetch one release file with its hash and bundle; return it once the hash matches and the bundle verifies."""
    path = scratch / name
    path.write_bytes(src.asset(tag, name))
    listed = src.asset(tag, f"{name}.sha256").decode("utf-8", "replace").split()
    if not listed or listed[0] != hashlib.sha256(path.read_bytes()).hexdigest():
        raise Refused(f"{name} does not match its sha256 in the {tag} release")
    bundle = scratch / f"{name}.sigstore.json"
    bundle.write_bytes(src.asset(tag, f"{name}.sigstore.json"))
    _cosign(path, bundle, identity, issuer, env)
    return path


def _audit(repo: Path, env: Mapping[str, str]) -> None:
    """Refuse when the tag's lock has a vulnerability of high or critical severity, or none can be told apart."""
    uv = _tool("uv", env)
    out = _run([uv, "audit", "--frozen", "--output-format", "json"], env, cwd=repo)
    try:
        report = json.loads(out)
        found = report["vulnerabilities"]
        severities = [_severity(v) for v in found]
    except (ValueError, KeyError, TypeError) as e:
        raise Refused("the audit of the release's lock could not be read") from e
    if any(s is None or s in BAD_SEVERITIES for s in severities):
        raise Refused(
            f"the release's lock has {len(found)} known vulnerabilit(y/ies), and at least one is high, critical or unrated"
        )


def _severity(vuln: object) -> str | None:
    """The first `severity` word anywhere in one vulnerability record, lower case, else None."""
    stack = [vuln]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for k, v in item.items():
                if k.lower() == "severity" and isinstance(v, str):
                    return v.lower()
                stack.append(v)
        elif isinstance(item, list):
            stack += item
    return None


def verify_release(tag: str, root: Mapping[str, str], src: Source, scratch: Path, env: Mapping[str, str]) -> Path:
    """Prove `tag` (see the module docstring) and return the installer file that was proved, in `scratch`. Raises `Refused`
    with the reason at the first thing that doesn't hold; nothing is installed by this function."""
    if not _TAG.fullmatch(tag):
        raise Refused(f"{tag!r} is not a release tag")
    env = clean_env(env)
    identity, issuer = root["identity"].format(tag=tag), root["issuer"]
    repo = scratch / "tag"
    src.checkout(tag, repo)
    sha = _tag_signature(repo, tag, root, scratch, env)
    wheel = f"ws_aitoolkit-{tag[1:]}-py3-none-any.whl"
    installer = _asset(src, tag, "install.sh", scratch, identity, issuer, env)
    _asset(src, tag, wheel, scratch, identity, issuer, env)
    runs = src.check_runs(sha)
    bad = [r.get("name", "?") for r in runs if r.get("status") != "completed" or r.get("conclusion") not in SAFE_RUNS]
    if not runs or bad:
        raise Refused(f"the checks on {tag}'s commit are not all green ({', '.join(map(str, bad[:3])) or 'none ran'})")
    _audit(repo, env)
    return installer
