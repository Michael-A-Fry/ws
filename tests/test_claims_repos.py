"""The claims guard looks for a commit in the repositories in the policy's `repos` before it denies the message, and
trusts nothing else: not a folder a message names, not a clone, not a repository with no real remote."""

from __future__ import annotations

import re
import shutil

import pytest
from test_guard_claims import git, judge

from ws.guards import claims as cl

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
URL = "https://example.invalid/other.git"


def readable(sha: str) -> bool:
    return bool(re.search(r"\d", sha) and re.search(r"[a-f]", sha))


def repo_with_commit(base, name: str, branch: str = "main") -> tuple:
    """A repository `base/name…` with one commit whose short id the guard reads as a commit."""
    for attempt in range(40):
        d = base / f"{name}{attempt}"
        d.mkdir()
        git(d, "init", "-q", "-b", branch)
        (d / "f").write_text(f"{name}{attempt}")
        git(d, "add", "f")
        git(d, "commit", "-q", "-m", f"{name} {attempt}")
        sha = git(d, "rev-parse", "HEAD")[:10]
        if readable(sha):
            return d, sha
    raise AssertionError("no commit with a readable id")


def with_real_remote(d, branch: str = "main") -> None:
    """Make `d` look like a checkout of a hosted repository whose default branch is `branch`."""
    git(d, "remote", "add", "origin", URL)
    git(d, "update-ref", f"refs/remotes/origin/{branch}", "HEAD")
    git(d, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{branch}")


@pytest.fixture
def two(tmp_path, monkeypatch):
    """The session's repo (origin/main holds its commit) and another one, `other`, with its own commit `theirs`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    here, _ = repo_with_commit(tmp_path, "here")
    git(here, "update-ref", "refs/remotes/origin/main", "HEAD")
    other, theirs = repo_with_commit(tmp_path, "other")
    return here, other, theirs


def listed(other):
    return cl.config({"repos": [str(other)]}, "w")


@needs_git
def test_a_commit_on_a_listed_repos_default_branch_passes(two):
    here, other, theirs = two
    with_real_remote(other)
    assert judge(f"merged {theirs} into main", here, listed(other)) is None
    assert judge(f"{theirs} is on main", here, listed(other)) is None


@needs_git
def test_a_path_in_the_message_vouches_for_nothing(two):
    """The blocker: naming a repository in the message, even a perfectly good one, doesn't make it count."""
    here, other, theirs = two
    with_real_remote(other)
    assert judge(f"merged {theirs} into main in {other}", here).action == "deny"
    assert judge(f"merged {theirs} into main in {other}", here, cl.config({}, "w")).action == "deny"


@needs_git
def test_a_made_up_commit_is_still_refused(two):
    here, other, _ = two
    with_real_remote(other)
    assert judge("merged abc1234def into main", here, listed(other)).action == "deny"


@needs_git
def test_a_clone_vouches_for_nothing(two, tmp_path):
    """A plain clone of a local repository has a folder for an origin, and its origin/HEAD is whatever branch was out."""
    here, other, theirs = two
    git(other, "checkout", "-q", "-b", "feat")
    clone = tmp_path / "clone"
    git(tmp_path, "clone", "-q", str(other), str(clone))
    assert judge(f"merged {theirs} into main", here, listed(clone)).action == "deny"


@needs_git
def test_a_repo_with_no_remote_vouches_for_nothing(two):
    """A scratch repo whose local `main` holds the commit (or a hand-made remote-tracking ref) is not a record."""
    here, other, theirs = two
    git(other, "update-ref", "refs/remotes/origin/main", "HEAD")  # a ref with no remote behind it
    assert judge(f"merged {theirs} into main", here, listed(other)).action == "deny"


@needs_git
def test_a_repo_whose_default_branch_is_another_one_does_not_vouch_for_main(two):
    here, other, theirs = two
    git(other, "branch", "-m", "trunk")
    with_real_remote(other, "trunk")
    assert judge(f"merged {theirs} into main", here, listed(other)).action == "deny"


@needs_git
def test_a_listed_repo_with_the_commit_only_on_a_feature_branch_does_not_vouch(two):
    here, other, _ = two
    with_real_remote(other)
    git(other, "checkout", "-q", "-b", "work")
    (other / "f").write_text("later")
    git(other, "commit", "-q", "-am", "later")
    later = git(other, "rev-parse", "HEAD")[:10]
    if not readable(later):
        pytest.skip("this id isn't one the guard reads as a commit")
    assert judge(f"merged {later} into main", here, listed(other)).action == "deny"


@needs_git
def test_a_repo_git_cannot_answer_for_does_not_vouch(two, monkeypatch):
    here, other, theirs = two
    with_real_remote(other)
    real = cl._git_run

    def run(cwd, *args, budget=None):
        return None if str(cwd) == str(other) else real(cwd, *args, budget=budget)

    monkeypatch.setattr(cl, "_git_run", run)
    assert judge(f"merged {theirs} into main", here, listed(other)).action == "deny"


@needs_git
def test_a_listed_folder_that_is_not_a_repository_is_ignored(two, tmp_path):
    here, _, _ = two
    plain = tmp_path / "plain"
    plain.mkdir()
    assert judge("merged abc1234def into main", here, listed(plain)).action == "deny"


@needs_git
def test_the_six_repo_cap_counts_after_the_sessions_own_repo_and_repeats(two):
    here, other, theirs = two
    with_real_remote(other)
    cfg = cl.config({"repos": [str(here), str(here), *[str(other)] * 3, str(other)]}, "w")
    assert cl.other_repos(cfg, str(here), cl.Budget()) == [str(other.resolve())]
    assert judge(f"merged {theirs} into main", here, cfg) is None


def test_repos_must_be_absolute_paths():
    with pytest.raises(Exception, match="repos"):
        cl.config({"repos": ["relative/dir"]}, "w")
