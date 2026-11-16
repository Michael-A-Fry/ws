"""The claims guard and a commit that sits on another remote branch the message names: it is accepted when the wording
names the branch (`on origin/wt/x`, `pushed to origin/wt/x`), and `merged` and main claims stay main-only."""

from __future__ import annotations

import re
import shutil

import pytest
from test_guard_claims import git, judge

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def readable(sha: str) -> bool:
    return bool(re.search(r"\d", sha) and re.search(r"[a-f]", sha))


def repo_with_commit(base, name: str):
    for n in range(40):
        d = base / f"{name}{n}"
        d.mkdir()
        git(d, "init", "-q", "-b", "main")
        (d / "f").write_text(f"{name}{n}")
        git(d, "add", "f")
        git(d, "commit", "-q", "-m", f"{name} {n}")
        sha = git(d, "rev-parse", "HEAD")[:10]
        if readable(sha):
            return d, sha
    raise AssertionError("no readable id")


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """origin/main holds `base`; origin/wt/x holds `base` and `topic`; the local branch also has `local`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    d, base = repo_with_commit(tmp_path, "r")
    git(d, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(d, "checkout", "-q", "-b", "wt/x")
    for n in range(40):
        (d / "t").write_text(f"topic{n}")
        git(d, "add", "t")
        git(d, "commit", "-q", "-m", f"topic {n}")
        topic = git(d, "rev-parse", "HEAD")[:10]
        if readable(topic):
            break
        git(d, "reset", "-q", "--hard", "HEAD~1")
    git(d, "update-ref", "refs/remotes/origin/wt/x", "HEAD")
    for n in range(40):
        (d / "u").write_text(f"local{n}")
        git(d, "add", "u")
        git(d, "commit", "-q", "-m", f"local {n}")
        local = git(d, "rev-parse", "HEAD")[:10]
        if readable(local):
            break
        git(d, "reset", "-q", "--hard", "HEAD~1")
    return d, base, topic, local


@needs_git
def test_a_commit_on_an_origin_branch_the_message_names_is_accepted(repo):
    d, _, topic, _ = repo
    assert judge(f"{topic} is on origin/wt/x", d) is None
    assert judge(f"pushed {topic} to origin/wt/x", d) is None


@needs_git
def test_merged_and_main_claims_stay_main_only(repo):
    d, base, topic, _ = repo
    assert judge(f"merged {topic} into main", d).action == "deny"
    assert judge(f"{topic} is on main", d).action == "deny"
    assert judge(f"merged {base} into main", d) is None
