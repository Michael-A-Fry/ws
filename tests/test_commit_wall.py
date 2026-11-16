"""The commit wall (ws.commit_wall): what it refuses, what it lets through, merges, big files, and the installer that
records and restores core.hooksPath. Repositories are real, made with git; a real `git commit` runs the hooks."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ws
from ws import cli, commit_wall, paths

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
# built at run time: the planted name is no one's
PLANT = "zz" + "planted" + "-name"


def git(cwd, *args, check=True) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com"}
    env |= {"GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}
    done = subprocess.run(
        ["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, env=env, check=False
    )
    if check and done.returncode:
        raise AssertionError(f"git {args}: {done.stderr}{done.stdout}")
    return done.stdout if check else f"{done.returncode}\n{done.stderr}{done.stdout}"


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("PYTHONPATH", str(Path(ws.__file__).resolve().parent.parent))
    monkeypatch.setattr(commit_wall, "ws_argv", lambda: [sys.executable, "-m", "ws"])
    return tmp_path


@pytest.fixture
def repo(box):
    d = box / "repo"
    d.mkdir()
    git(d, "init", "-q", "-b", "main")
    (d / "a.txt").write_text("hello\n")
    git(d, "add", "a.txt")
    git(d, "commit", "-q", "-m", "first")
    return d


def configure(patterns="", exceptions=""):
    f = commit_wall.folder()
    f.mkdir(parents=True, exist_ok=True)
    (f / "patterns.toml").write_text(patterns or f'[[pattern]]\nname = "plant"\nterms = ["{PLANT}"]\n')
    (f / "exceptions.toml").write_text(exceptions)


def check(repo, *extra) -> int:
    return cli.main(["commit-wall", "check", "--repo", str(repo), *extra])


@needs_git
def test_a_planted_name_in_a_staged_line_is_refused_and_the_hit_names_no_text(repo, capsys):
    configure()
    (repo / "a.txt").write_text(f"hello\nsee {PLANT.upper()} here\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 1
    err = capsys.readouterr().err
    assert "a.txt:2" in err and "plant" in err and PLANT not in err.lower()  # the file, the line, the pattern's name


@needs_git
def test_a_clean_staged_diff_and_an_unstaged_planted_line_pass(repo):
    configure()
    (repo / "a.txt").write_text(f"hello\n{PLANT}\n")  # not staged
    assert check(repo) == 0
    (repo / "b.txt").write_text("fine\n")
    git(repo, "add", "b.txt")
    assert check(repo) == 0


@needs_git
def test_only_the_lines_a_commit_adds_are_read(repo):
    (repo / "a.txt").write_text(f"hello\n{PLANT}\n")
    git(repo, "add", "a.txt")
    git(repo, "commit", "-q", "-m", "had it already")
    configure()
    (repo / "a.txt").write_text(f"hello\n{PLANT}\nmore\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 0  # the planted line was already there: this commit adds only `more`


@needs_git
def test_an_exception_lets_a_line_through_and_each_field_given_has_to_match(repo):
    (repo / "a.txt").write_text(f"hello\nuse {PLANT} in docs\n")
    git(repo, "add", "a.txt")
    configure()
    assert check(repo) == 1
    for allow, want in (
        ('pattern = "plant"', 0),
        ('path = "a.*"', 0),
        ('text = "in docs"', 0),
        ('pattern = "plant"\npath = "b.*"', 1),
        ('pattern = "other"', 1),
        ('text = "not there"', 1),
    ):
        configure(exceptions=f"[[allow]]\n{allow}\n")
        assert check(repo) == want, allow


@needs_git
def test_a_regex_pattern_and_a_bad_config(repo):
    configure('[[pattern]]\nname = "host"\nregex = "int[a-z]+[.]example"\n')
    (repo / "a.txt").write_text("hello\nvia internal.example\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 1
    configure("[[pattern]]\nname = 'x'\nregex = '('\n")
    assert check(repo) == 1  # a pattern that doesn't compile refuses the check, never skips it
    configure("[[pattern]]\nname = 'x'\n")
    assert check(repo) == 1
    configure("", "[[allow]]\n")
    assert check(repo) == 1


@needs_git
def test_the_commit_message_is_scanned_and_git_comments_are_not(repo, box):
    configure()
    msg = box / "msg"
    msg.write_text(f"Fix it\n\nthanks {PLANT}\n")
    assert check(repo, "--stage", "commit-msg", str(msg)) == 1
    msg.write_text(f"Fix it\n# {PLANT} is a comment git adds\n")
    assert check(repo, "--stage", "commit-msg", str(msg)) == 0


@needs_git
def test_a_merge_scans_only_the_lines_new_to_the_merge(repo):
    configure()
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text(f"from the other side {PLANT}\n")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-q", "-m", "side", "--no-verify")
    git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main work\n")
    git(repo, "add", "main.txt")
    git(repo, "commit", "-q", "-m", "main work")
    git(repo, "merge", "--no-commit", "--no-ff", "side")
    assert check(repo) == 0  # the planted line came in from the other side: not new to the merge
    (repo / "fix.txt").write_text(f"written in the merge {PLANT}\n")
    git(repo, "add", "fix.txt")
    assert check(repo) == 1


@needs_git
def test_a_file_over_1_mb_is_skipped_and_said_so(repo, capsys):
    configure()
    (repo / "big.txt").write_text((PLANT + "\n") * 80000)  # over 1 MB
    git(repo, "add", "big.txt")
    assert check(repo) == 0
    assert "skipped big.txt" in capsys.readouterr().err
    assert check(repo, "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["data"]["skipped"] == [{"path": "big.txt", "why": "over 1 MB"}]


@needs_git
def test_install_blocks_a_real_commit_and_uninstall_restores_the_prior_value(repo, box):
    configure()
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 0
    here = git(repo, "config", "--local", "core.hooksPath").strip()
    assert here.startswith(str(commit_wall.folder() / "hooks"))
    (repo / "a.txt").write_text(f"hello\n{PLANT}\n")
    git(repo, "add", "a.txt")
    out = git(repo, "commit", "-m", "x", check=False)
    assert out.startswith("1\n") and "commit wall" in out
    git(repo, "commit", "-q", "-m", f"the message has {PLANT}", "--no-verify", check=False)  # the bypass is git's own
    git(repo, "reset", "-q", "--hard")
    (repo / "a.txt").write_text("hello\nclean\n")
    git(repo, "add", "a.txt")
    assert git(repo, "commit", "-m", f"msg with {PLANT}", check=False).startswith("1\n")  # commit-msg hook
    git(repo, "commit", "-q", "-m", "clean one")
    assert cli.main(["commit-wall", "uninstall", "--repo", str(repo)]) == 0
    assert git(repo, "config", "--local", "--get", "core.hooksPath", check=False).startswith("1\n")  # unset again


@needs_git
def test_install_keeps_the_hooks_the_repository_had_and_restores_a_set_hooks_path(repo, box):
    configure()
    old = box / "oldhooks"
    old.mkdir()
    (old / "pre-commit").write_text("#!/bin/sh\necho OLD-PRE-COMMIT >&2\nexit 0\n")
    (old / "post-commit").write_text(f"#!/bin/sh\necho ran > {box / 'post-ran'}\n")
    for f in old.iterdir():
        f.chmod(0o755)
    git(repo, "config", "--local", "core.hooksPath", str(old))
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 0
    (repo / "a.txt").write_text("hello\nclean\n")
    git(repo, "add", "a.txt")
    out = git(repo, "commit", "-m", "fine", check=False)
    assert out.startswith("0\n") and "OLD-PRE-COMMIT" in out  # the wall passed, then the old pre-commit ran
    assert (box / "post-ran").read_text().strip() == "ran"  # a hook the wall doesn't own is forwarded
    prior = json.loads((commit_wall.folder() / "prior.json").read_text())
    assert [v["prior"] for v in prior.values()] == [str(old)]
    assert cli.main(["commit-wall", "uninstall", "--repo", str(repo)]) == 0
    assert git(repo, "config", "--local", "--get", "core.hooksPath").strip() == str(old)


@needs_git
def test_a_hook_that_cannot_run_ws_blocks_the_commit(repo, monkeypatch):
    configure()
    monkeypatch.setattr(commit_wall, "ws_argv", lambda: ["/nonexistent/ws"])
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 0
    (repo / "a.txt").write_text("hello\nclean\n")
    git(repo, "add", "a.txt")
    assert git(repo, "commit", "-m", "fine", check=False).startswith("1\n")  # fails closed


@needs_git
def test_installing_twice_is_refused_and_the_folder_is_a_control_path(repo, box):
    configure()
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 0
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 1
    assert str(commit_wall.folder()) in paths.control_paths()  # a session can't write the patterns or exceptions
