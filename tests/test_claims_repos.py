"""The claims guard looks for a commit in the other repositories a message names before it denies the message."""

from __future__ import annotations

import re
import shutil

import pytest
from test_guard_claims import git, judge

from ws.guards import claims as cl

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def two(tmp_path, monkeypatch):
    """The session's repo (origin/main holds `mine`) and another (origin/main holds `theirs`)."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    out = []
    for name in ("here", "other"):
        for attempt in range(40):  # a short id needs a digit and a letter a to f to be read as a commit
            d = tmp_path / f"{name}{attempt}"
            d.mkdir()
            git(d, "init", "-q", "-b", "main")
            (d / "f").write_text(f"{name}{attempt}")
            git(d, "add", "f")
            git(d, "commit", "-q", "-m", f"{name} {attempt}")
            sha = git(d, "rev-parse", "HEAD")[:10]
            if re.search(r"\d", sha) and re.search(r"[a-f]", sha):
                git(d, "update-ref", "refs/remotes/origin/main", "HEAD")
                out.append((d, sha))
                break
    assert len(out) == 2
    return out


@needs_git
def test_a_commit_on_another_repos_default_branch_named_by_path_passes(two):
    (here, _), (other, theirs) = two
    assert judge(f"merged {theirs} into main in {other}", here) is None
    assert judge(f"{theirs} is on main ({other}/src/x.py)", here) is None


@needs_git
def test_a_commit_on_a_listed_repo_passes_without_the_path(two):
    (here, _), (other, theirs) = two
    cfg = cl.config({"repos": [str(other)]}, "w")
    assert judge(f"merged {theirs} into main", here, cfg) is None
    assert judge(f"merged {theirs} into main", here).action == "deny"  # not listed, not named: still refused


@needs_git
def test_a_made_up_commit_is_still_refused_when_other_repos_are_named(two):
    (here, _), (other, _) = two
    fake = "abc1234def"
    cfg = cl.config({"repos": [str(other)]}, "w")
    assert judge(f"merged {fake} into main in {other}", here, cfg).action == "deny"


@needs_git
def test_a_commit_not_on_the_other_repos_default_branch_is_refused(two):
    (here, _), (other, _) = two
    git(other, "checkout", "-q", "-b", "work")
    (other / "f").write_text("later")
    git(other, "commit", "-q", "-am", "later")
    later = git(other, "rev-parse", "HEAD")[:10]
    if not (re.search(r"\d", later) and re.search(r"[a-f]", later)):
        pytest.skip("this id isn't one the guard reads as a commit")
    assert judge(f"merged {later} into main in {other}", here).action == "deny"


@needs_git
def test_a_path_that_is_not_a_repository_is_ignored(two, tmp_path):
    (here, mine), _ = two
    plain = tmp_path / "plain"
    plain.mkdir()
    assert judge(f"merged {mine} into main, notes in {plain}/x", here) is None
    assert judge(f"merged abc1234def into main, notes in {plain}/x", here).action == "deny"


def test_repos_must_be_absolute_paths():
    with pytest.raises(Exception, match="repos"):
        cl.config({"repos": ["relative/dir"]}, "w")
