"""release.yml: which tags it accepts, how it writes the notes for a release candidate, and that a candidate is marked.

The workflow is YAML and ws is stdlib only, so these tests read the file as text, pull out the shell of the two steps
that decide things (the tag check and the release notes) and run that shell on a temporary CHANGELOG. They do not
reach GitHub.
"""

import os
import re
import shutil
import subprocess
import sys
import textwrap
import types
from pathlib import Path

import pytest

from ws import version

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
BASH = shutil.which("bash")

pytestmark = pytest.mark.skipif(BASH is None or shutil.which("awk") is None, reason="needs bash and awk")


def step_script(name_start: str) -> str:
    """The `run: |` text of the step whose name starts with `name_start`, with the YAML indent removed."""
    lines = WORKFLOW.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip().startswith(f"- name: {name_start}"))
    run = next(i for i in range(start, len(lines)) if lines[i].strip() == "run: |")
    body: list[str] = []
    indent = len(lines[run]) - len(lines[run].lstrip()) + 2
    for line in lines[run + 1 :]:
        if line.strip() and len(line) - len(line.lstrip()) < indent:
            break
        body.append(line)
    return textwrap.dedent("\n".join(body))


def run_bash(script: str, cwd: Path, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    full = {"PATH": os.environ["PATH"], "HOME": str(cwd), **env}
    return subprocess.run(
        [BASH, "-e", "-c", script], cwd=cwd, env=full, capture_output=True, text=True, check=False, timeout=60
    )


def tag_check(tag: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    script = step_script("the tag is vX.Y.Z, vX.Y.Z.N or vX.Y.ZrcN")
    return run_bash(script, tmp_path, {"TAG": tag, "GITHUB_EVENT_NAME": "push"})


@pytest.mark.parametrize("tag", ["v0.1.8", "v10.20.30", "v0.2.0rc1", "v0.2.0rc12", "v0.1.11.1", "v0.1.11.12"])
def test_stable_and_release_candidate_tags_are_accepted(tag: str, tmp_path: Path):
    assert tag_check(tag, tmp_path).returncode == 0


@pytest.mark.parametrize(
    "tag",
    [
        "0.2.0",
        "v0.2",
        "v0.2.0rc",
        "v0.2.0-rc1",
        "v0.2.0b1",
        "v0.2.0a1",
        "v0.2.0.dev1",
        "v0.2.0rc1.post1",
        "v0.2.0+local",
        "v0.1.11.1.1",
        "v0.1.11.",
        "",
    ],
)
def test_other_tags_are_refused(tag: str, tmp_path: Path):
    r = tag_check(tag, tmp_path)
    assert r.returncode == 1
    assert "is not vX.Y.Z, vX.Y.Z.N or vX.Y.ZrcN" in r.stdout


CHANGELOG = """# Changelog

## [Unreleased]

Work in progress.

## [0.2.0] - unreleased

The stable notes.

## [0.1.9] - 2026-01-01

Older.

[Unreleased]: https://example.invalid/compare
"""


def notes(version: str, prerelease: str, tmp_path: Path, changelog: str = CHANGELOG, event: str = "push"):
    (tmp_path / "CHANGELOG.md").write_text(changelog, encoding="utf-8")
    script = step_script("release notes from the CHANGELOG section for this version")
    r = run_bash(script, tmp_path, {"VERSION": version, "PRERELEASE": prerelease, "GITHUB_EVENT_NAME": event})
    return r, tmp_path / "release-notes.md"


def test_a_candidate_uses_the_section_of_the_release_it_leads_to(tmp_path: Path):
    r, out = notes("0.2.0rc1", "true", tmp_path)
    assert r.returncode == 0, r.stdout
    text = out.read_text(encoding="utf-8")
    assert "release candidate for testing" in text
    assert "--prerelease allow" in text
    assert "The stable notes." in text
    assert "Older." not in text


def test_a_candidate_with_its_own_section_uses_it(tmp_path: Path):
    changelog = CHANGELOG.replace(
        "## [0.2.0] - unreleased", "## [0.2.0rc1] - 2026-02-02\n\nThe candidate notes.\n\n## [0.2.0] - unreleased"
    )
    r, out = notes("0.2.0rc1", "true", tmp_path, changelog)
    assert r.returncode == 0, r.stdout
    text = out.read_text(encoding="utf-8")
    assert "The candidate notes." in text and "The stable notes." not in text


def test_a_candidate_falls_back_to_unreleased_and_may_still_say_unreleased(tmp_path: Path):
    changelog = CHANGELOG.replace("## [0.2.0] - unreleased\n\nThe stable notes.\n\n", "")
    r, out = notes("0.2.0rc1", "true", tmp_path, changelog)
    assert r.returncode == 0, r.stdout
    assert "Work in progress." in out.read_text(encoding="utf-8")


def test_a_candidate_with_nothing_to_say_is_refused(tmp_path: Path):
    r, _ = notes("0.3.0rc1", "true", tmp_path, "# Changelog\n\n## [Unreleased]\n\n## [0.1.9] - 2026-01-01\n\nOlder.\n")
    assert r.returncode == 1
    assert "no '## [0.3.0rc1]' section" in r.stdout


def test_a_stable_release_still_needs_its_own_dated_section(tmp_path: Path):
    r, _ = notes("0.2.0", "false", tmp_path)
    assert r.returncode == 1  # the section still says "unreleased"
    assert "put the date in" in r.stdout
    dated = CHANGELOG.replace("## [0.2.0] - unreleased", "## [0.2.0] - 2026-02-03")
    r, out = notes("0.2.0", "false", tmp_path, dated)
    assert r.returncode == 0, r.stdout
    text = out.read_text(encoding="utf-8")
    assert "The stable notes." in text and "release candidate" not in text


def test_a_stable_release_does_not_borrow_unreleased(tmp_path: Path):
    r, _ = notes("0.9.0", "false", tmp_path)
    assert r.returncode == 1
    assert "no '## [0.9.0]' section" in r.stdout


def test_the_prerelease_flag_follows_the_version_and_reaches_the_release_and_the_pass_test():
    assert re.search(r'case "\$version" in \*rc\*\) prerelease=true ;; \*\) prerelease=false ;; esac', WORKFLOW)
    assert "prerelease: ${{ needs.build.outputs.prerelease }}" in WORKFLOW  # the GitHub release is marked
    assert 'if [ "$PRERELEASE" = true ]; then latest=false; else latest=true; fi' in WORKFLOW  # pass test by tag
    assert 'data["channel"] == ("rc" if sys.argv[4] == "true" else "stable")' in WORKFLOW


def test_the_pypi_job_uploads_a_candidate_like_any_release():
    pypi = WORKFLOW[WORKFLOW.index("  pypi:") : WORKFLOW.index("  # The pass test")]
    assert "if: github.event_name == 'push'" in pypi
    assert "prerelease" not in pypi  # PyPI reads the pre-release from the rcN version itself
    assert "environment: pypi" in pypi


# ---- the stamp's trust root and the installer as a release asset (what `ws upgrade --unattended` verifies) ------------------

SIGNERS = ROOT / ".github" / "release_signers"
GIT = shutil.which("git")
KEYGEN = shutil.which("ssh-keygen")
PYTHON3 = shutil.which("python3")


def stamped_repo(tmp_path: Path, signers: str | None = None) -> Path:
    """A throwaway git repo holding the signers file, with the workflow's stamp step run in it as `owner/ws`."""
    repo = tmp_path / "repo"
    (repo / ".github").mkdir(parents=True)
    (repo / "src" / "ws").mkdir(parents=True)
    (repo / ".github" / "release_signers").write_text(
        SIGNERS.read_text(encoding="utf-8") if signers is None else signers, encoding="utf-8"
    )
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t"}
    env |= {"GIT_COMMITTER_EMAIL": "t@example.com", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    for argv in (["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"]):
        subprocess.run([GIT, *argv], cwd=repo, env={**os.environ, **env}, check=True, capture_output=True)
    return repo


def run_stamp(repo: Path) -> subprocess.CompletedProcess[str]:
    env = {"GITHUB_REPOSITORY": "owner/ws", "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    return run_bash(step_script("stamp the build"), repo, env)


needs_stamp_tools = pytest.mark.skipif(GIT is None or PYTHON3 is None, reason="needs git and python3")


@needs_stamp_tools
def test_the_stamp_carries_the_trust_root_and_the_version_module_reads_it(tmp_path: Path, monkeypatch):
    repo = stamped_repo(tmp_path)
    r = run_stamp(repo)
    assert r.returncode == 0, r.stdout + r.stderr
    stamp = types.ModuleType("ws._build")
    exec((repo / "src" / "ws" / "_build.py").read_text(encoding="utf-8"), stamp.__dict__)  # noqa: S102  # our own output
    assert stamp.source == "release" and stamp.dirty is False and len(stamp.git_sha) == 40
    monkeypatch.setitem(sys.modules, "ws._build", stamp)
    root = version.trust_root()
    assert root is not None
    assert root["repo"] == "owner/ws"
    assert root["issuer"] == "https://token.actions.githubusercontent.com"
    assert root["identity"].format(tag="v1.2.3") == (
        "https://github.com/owner/ws/.github/workflows/release.yml@refs/tags/v1.2.3"
    )
    assert root["tag_signers"] == SIGNERS.read_text(encoding="utf-8")


@needs_stamp_tools
def test_a_release_with_no_signer_key_is_not_stamped(tmp_path: Path):
    repo = stamped_repo(tmp_path, signers="# the file with no key line\n")
    r = run_stamp(repo)
    assert r.returncode != 0
    assert "no key line" in r.stdout + r.stderr
    assert not (repo / "src" / "ws" / "_build.py").exists()


def test_the_committed_signers_file_names_a_public_key_and_nothing_private():
    text = SIGNERS.read_text(encoding="utf-8")
    keys = [ln for ln in text.splitlines() if ln and not ln.startswith("#")]
    assert len(keys) >= 1
    for line in keys:
        _principal, namespaces, kind, blob, *comment = line.split()
        assert namespaces == 'namespaces="git"' and kind == "ssh-ed25519" and re.fullmatch(r"[A-Za-z0-9+/=]+", blob)
        assert not comment, "a key comment is usually an e-mail address"
    assert "PRIVATE KEY" not in text


@pytest.mark.skipif(GIT is None or KEYGEN is None, reason="needs git and ssh-keygen")
def test_the_signers_line_format_verifies_a_signed_tag_with_git(tmp_path: Path):
    """The file's own format (principal, namespaces, key) is what `git tag -v` accepts; a throwaway key stands in."""
    key = tmp_path / "k"
    subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(key), "-C", ""], check=True)
    blob = key.with_suffix(".pub").read_text(encoding="utf-8").split()[1]
    allowed = tmp_path / "allowed"
    allowed.write_text(f'ws-release namespaces="git" ssh-ed25519 {blob}\n', encoding="utf-8")
    repo = tmp_path / "r"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env |= {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t"}
    env["GIT_COMMITTER_EMAIL"] = "t@example.com"
    sign = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={key}"]
    for argv in (["init", "-q"], ["commit", "-q", "--allow-empty", "-m", "x"], [*sign, "tag", "-s", "-m", "v1", "v1"]):
        subprocess.run([GIT, *argv], cwd=repo, env=env, check=True, capture_output=True)
    verify = [GIT, "-c", "gpg.format=ssh", "-c", f"gpg.ssh.allowedSignersFile={allowed}", "tag", "-v", "v1"]
    assert subprocess.run(verify, cwd=repo, env=env, capture_output=True, check=False).returncode == 0
    allowed.write_text(allowed.read_text(encoding="utf-8").replace(blob[-8:-4], "AAAA"), encoding="utf-8")
    assert subprocess.run(verify, cwd=repo, env=env, capture_output=True, check=False).returncode != 0


def test_the_installer_is_a_signed_release_asset_with_its_hash_and_attestation():
    """install.sh is copied into dist, hashed, attested, signed (each bundle verified) and published like the wheel."""
    assert "cp ../install.sh install.sh" in WORKFLOW
    assert "for f in *.whl *.tar.gz *.cdx.json install.sh; do sha256sum" in WORKFLOW
    attest = WORKFLOW[WORKFLOW.index("attest-build-provenance@") : WORKFLOW.index("sigstore/cosign-installer@")]
    assert "dist/install.sh" in attest
    sign = WORKFLOW[WORKFLOW.index("cosign sign-blob, keyless") : WORKFLOW.index("  release:")]
    assert "for f in *.whl *.tar.gz *.cdx.json install.sh; do" in sign and "cosign verify-blob" in sign
    files = WORKFLOW[WORKFLOW.index("fail_on_unmatched_files") : WORKFLOW.index("  pypi:")]
    assert "dist/install.sh\n" in files and "dist/*.sha256" in files and "dist/*.sigstore.json" in files


def test_the_wheel_is_checked_for_its_trust_root_before_it_is_published():
    assert "assert version.trust_root()" in WORKFLOW


def meta_repo(tmp_path: Path, signed_by: str | None) -> tuple[Path, dict[str, str]]:
    """A repo at a v0.0.1 tag (signed with a throwaway key, or unsigned) with a signers file listing one key."""
    assert GIT and KEYGEN
    blobs: dict[str, str] = {}
    for name in ("listed", "other"):
        subprocess.run([KEYGEN, "-q", "-t", "ed25519", "-N", "", "-f", str(tmp_path / name), "-C", ""], check=True)
        blobs[name] = (tmp_path / f"{name}.pub").read_text(encoding="utf-8").split()[1]
    repo = tmp_path / "meta"
    (repo / ".github").mkdir(parents=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "x"\nversion = "0.0.1"\n', encoding="utf-8")
    (repo / ".github" / "release_signers").write_text(
        f'ws-release namespaces="git" ssh-ed25519 {blobs["listed"]}\n', encoding="utf-8"
    )
    quiet = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull}
    env = {**os.environ, **quiet, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com"}
    env |= {"GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    steps = [["init", "-q"], ["add", "."], ["commit", "-q", "-m", "x"], ["update-ref", "refs/remotes/origin/main", "HEAD"]]
    if signed_by:
        sign = ["-c", "gpg.format=ssh", "-c", f"user.signingkey={tmp_path / signed_by}"]
        steps.append([*sign, "tag", "-s", "-m", "v", "v0.0.1"])
    else:
        steps.append(["tag", "-a", "-m", "v", "v0.0.1"])
    for argv in steps:
        subprocess.run([GIT, *argv], cwd=repo, env=env, check=True, capture_output=True)
    sha = subprocess.run([GIT, "rev-parse", "HEAD"], cwd=repo, env=env, capture_output=True, text=True, check=True)
    out = tmp_path / "out"
    out.write_text("", encoding="utf-8")
    run_env = {"TAG": "v0.0.1", "GITHUB_EVENT_NAME": "push", "GITHUB_SHA": sha.stdout.strip()}
    return repo, {**run_env, **quiet, "GITHUB_OUTPUT": str(out), "GITHUB_ENV": str(out)}


@pytest.mark.skipif(GIT is None or KEYGEN is None, reason="needs git and ssh-keygen")
@pytest.mark.parametrize(
    ("signed_by", "ok"), [("listed", True), ("other", False), (None, False)], ids=["listed-key", "unlisted-key", "unsigned"]
)
def test_a_pushed_tag_must_be_signed_by_a_key_in_the_signers_file(tmp_path: Path, signed_by, ok):
    repo, env = meta_repo(tmp_path, signed_by)
    r = run_bash(step_script("the tag equals v + the version"), repo, env)
    assert (r.returncode == 0) is ok, r.stdout + r.stderr
    if not ok:
        assert "is not signed by a key listed in .github/release_signers" in r.stdout + r.stderr


@pytest.mark.skipif(GIT is None or KEYGEN is None, reason="needs git and ssh-keygen")
def test_a_dry_run_is_not_asked_for_a_tag_signature(tmp_path: Path):
    repo, env = meta_repo(tmp_path, None)
    r = run_bash(step_script("the tag equals v + the version"), repo, {**env, "GITHUB_EVENT_NAME": "workflow_dispatch"})
    assert r.returncode == 0, r.stdout + r.stderr
