"""`ws version`: which build is this, where did it come from, and where is it installed.

    ws version [--json]

Fields (the JSON `data`, schema `ws.cli/1`): `version`, `channel`, `git_sha`, `dirty`, `build_date`, `python`,
`install_path` and `install_source`. `ws --version` stays the one line `ws X.Y.Z`.

Where the build facts come from. The release workflow writes `ws/_build.py` (a Python string module, git-ignored) just
before `uv build`, so a release wheel carries the commit it was built from, whether the tree was clean and when it was
built. A checkout has no such file: there `ws version` asks git, at run time, about the checkout the package sits in
(and only that one, never a repo that merely holds the virtualenv). Anything it cannot find is `unknown` (`git_sha`,
`build_date`) or `null` (`dirty`); it never guesses.

`install_source` is the first rule that matches:

1. `checkout`: the package sits in a source tree (`pyproject.toml` and `.git` two folders up), or pip's `direct_url.json`
   says it came from a folder or a git URL (`install.sh --from PATH`, `uv run`, an editable install).
2. `release`: `direct_url.json` says it came from a wheel file and the wheel carries the release stamp (what
   `install.sh` installs after checking it). A wheel file without the stamp falls through to the rules below.
3. `pypi`: no `direct_url.json` and a `_build.py`, so an index handed over the release's own wheel.
4. `uv-tool` or `pipx`: neither of the above, and the interpreter lives in that tool's environment folder.
5. `unknown`.

The same stamp carries the trust root for `ws upgrade --unattended` (`trust_root()`): the repo, the release workflow's
cosign identity and the allowed signers for a release tag. It is not part of `ws version`'s output.

`channel` is the release channel the install came from, the first rule that matches: `edge` when the install is a
checkout or a git install (`install_source` is `checkout`) or the version has a dev or local part (`0.2.0.dev3`,
`0.0.0+src`); `rc` when the version is a pre-release (`0.2.0rc1`); else `stable`.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path

from ws import __version__, output

UNKNOWN = "unknown"
PACKAGE = "ws-aitoolkit"
_DEV_OR_LOCAL = re.compile(r"\.dev\d*|\+")
_PRE_RELEASE = re.compile(r"\d(?:a|b|rc)\d+")


def package_dir() -> Path:
    return Path(__file__).resolve().parent


def source_tree(pkg: Path) -> Path | None:
    """The checkout root when `pkg` is `<root>/src/ws` of a git checkout, else None."""
    root = pkg.parent.parent
    if pkg.parent.name == "src" and (root / "pyproject.toml").is_file() and (root / ".git").exists():
        return root
    return None


def build_info() -> dict | None:
    """The facts the release build stamped in, or None in a checkout (where there is no `_build.py`)."""
    try:
        from ws import _build  # type: ignore[attr-defined]  # written by the release workflow only
    except ImportError:
        return None
    return {
        "git_sha": str(getattr(_build, "git_sha", UNKNOWN)),
        "dirty": _build.dirty if isinstance(getattr(_build, "dirty", None), bool) else None,
        "build_date": str(getattr(_build, "build_date", UNKNOWN)),
    }


def trust_root() -> dict[str, str] | None:
    """What this build trusts to prove the next release, as the release workflow stamped it in, or None (a checkout, a
    build with no stamp, or a stamp with a field missing or the wrong shape: never a half-filled answer).

    Keys: `repo` (where releases come from), `identity` (the cosign certificate identity of the release workflow, with
    `{tag}` where the tag goes; fill it with `identity.format(tag=...)`), `issuer` (the OIDC issuer the certificate
    names) and `tag_signers` (an allowed-signers file for `git -c gpg.ssh.allowedSignersFile=<file> tag -v`). It is
    read from the installed wheel and nowhere else: a request, an environment variable or a newer release never
    supplies it, because a newer release can only be trusted through the one already installed."""
    try:
        from ws import _build  # type: ignore[attr-defined]  # written by the release workflow only
    except ImportError:
        return None
    if getattr(_build, "source", None) != "release" or getattr(_build, "dirty", None) is not False:
        return None  # only a clean release build vouches for the next one
    ident, signers = getattr(_build, "release_identity", None), getattr(_build, "tag_signers", None)
    if not isinstance(ident, dict) or not isinstance(signers, str) or not signers.strip():
        return None
    root = {key: ident.get(key) for key in ("repo", "identity", "issuer")}
    if not all(isinstance(v, str) and v for v in root.values()) or "{tag}" not in str(root["identity"]):
        return None
    if "{" in str(root["identity"]).replace("{tag}", "") or "}" in str(root["identity"]).replace("{tag}", ""):
        return None  # `identity.format(tag=...)` must not meet any other brace field
    return {**{k: str(v) for k, v in root.items()}, "tag_signers": signers}


def _git(root: Path, *args: str) -> str | None:
    try:
        # Only this install's own repo: a GIT_DIR or GIT_WORK_TREE from a hook or `rebase -x` would point git elsewhere,
        # and --no-optional-locks keeps `git status` from taking the index lock under the person's own git commands.
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        p = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(root), *args],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return p.stdout if p.returncode == 0 else None


def git_info(root: Path) -> dict:
    sha = (_git(root, "rev-parse", "HEAD") or "").strip()
    status = _git(root, "status", "--porcelain")
    return {
        "git_sha": sha or UNKNOWN,
        "dirty": None if status is None else bool(status.strip()),
        "build_date": UNKNOWN,
    }


def direct_url() -> dict | None:
    """pip's record (PEP 610) of what the install came from, or None when it came from an index."""
    try:
        text = metadata.distribution(PACKAGE).read_text("direct_url.json")
    except metadata.PackageNotFoundError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def tool_manager(prefix: str) -> str | None:
    p = prefix.replace("\\", "/")
    if "/uv/tools/" in p:
        return "uv-tool"
    if "/pipx/venvs/" in p:
        return "pipx"
    return None


def install_source(pkg: Path, has_build: bool, durl: dict | None, prefix: str) -> str:
    if source_tree(pkg) is not None:
        return "checkout"
    if durl is not None:
        if "dir_info" in durl or "vcs_info" in durl:
            return "checkout"
        if "archive_info" in durl and has_build:
            return "release"
    elif has_build:
        return "pypi"
    return tool_manager(prefix) or UNKNOWN


def channel(version: str, source: str) -> str:
    """`edge`, `rc` or `stable`: from the install source and the PEP 440 shape of the version."""
    if source == "checkout" or _DEV_OR_LOCAL.search(version):
        return "edge"
    if _PRE_RELEASE.search(version):
        return "rc"
    return "stable"


def collect() -> dict:
    pkg = package_dir()
    built = build_info()
    root = source_tree(pkg)
    if built is not None:
        facts = built
    elif root is not None:
        facts = git_info(root)
    else:
        facts = {"git_sha": UNKNOWN, "dirty": None, "build_date": UNKNOWN}
    source = install_source(pkg, built is not None, direct_url(), sys.prefix)
    return {
        "version": __version__,
        "channel": channel(__version__, source),
        "git_sha": facts["git_sha"],
        "dirty": facts["dirty"],
        "build_date": facts["build_date"],
        "python": platform.python_version(),
        "install_path": str(pkg),
        "install_source": source,
    }


def cmd_version(args: argparse.Namespace) -> int:
    data = collect()
    if args.json:
        output.result(data)
        return 0
    width = max(len(k) for k in data)
    for key, value in data.items():
        shown = UNKNOWN if value is None else str(value).lower() if isinstance(value, bool) else value
        print(f"{key:<{width}}  {shown}")
    return 0


def register(sub) -> None:
    v = sub.add_parser("version", help="which build this is: version, commit, install path and where it came from")
    v.set_defaults(func=cmd_version)
