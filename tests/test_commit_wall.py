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
def test_the_whole_commit_message_is_scanned_hash_lines_too(repo, box):
    configure()
    msg = box / "msg"
    msg.write_text(f"Fix it\n\nthanks {PLANT}\n")
    assert check(repo, "--stage", "commit-msg", str(msg)) == 1
    msg.write_text(f"Fix it\n# {PLANT} starts with a hash, and -m and --cleanup=verbatim keep such lines\n")
    assert check(repo, "--stage", "commit-msg", str(msg)) == 1
    msg.write_text("Fix it\n# a comment git adds\n")
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
def test_nothing_is_skipped_big_files_nul_bytes_and_no_diff_attributes_are_scanned(repo, capsys):
    configure()
    (repo / "big.txt").write_text("x" * 1_200_000 + "\n" + PLANT + "\n")  # padded past 1 MB
    git(repo, "add", "big.txt")
    assert check(repo) == 1
    git(repo, "reset", "-q")
    (repo / "nul.dat").write_bytes(b"\0\0" + PLANT.encode() + b"\n")  # a NUL byte makes git call it binary
    git(repo, "add", "nul.dat")
    assert check(repo) == 1
    git(repo, "reset", "-q")
    (repo / ".gitattributes").write_text("marked.txt -diff\n")
    (repo / "marked.txt").write_text(PLANT + "\n")
    git(repo, "add", ".gitattributes", "marked.txt")
    assert check(repo) == 1
    assert "wall skipped" not in capsys.readouterr().err


@needs_git
def test_an_added_line_that_starts_with_two_plus_signs_is_still_a_line(repo):
    configure()
    (repo / "a.txt").write_text(f"hello\n++ {PLANT}\n")  # the diff shows it as `+++ ...`, a header's shape
    git(repo, "add", "a.txt")
    assert check(repo) == 1


@needs_git
@pytest.mark.parametrize("sep", ["\x0c", "\r", "\u2028", "\x1c", "\x85"])
def test_lines_split_on_newlines_only(repo, sep):
    configure('[[pattern]]\nname = "span"\nregex = "ab.cd"\n')
    (repo / "a.txt").write_text(f"hello\nab{sep}cd\n", encoding="utf-8", newline="")
    git(repo, "add", "a.txt")
    assert check(repo) == 1


@needs_git
def test_invalid_utf8_is_read_as_it_is_and_does_not_crash(repo):
    configure()
    (repo / "a.txt").write_bytes(b"hello\n\xff\xfe" + PLANT.encode() + b"\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 1


@needs_git
@pytest.mark.parametrize("setting", ["diff.noprefix", "diff.mnemonicPrefix"])
def test_diff_prefix_settings_do_not_change_the_paths(repo, capsys, setting):
    configure()
    git(repo, "config", setting, "true")
    (repo / "a.txt").write_text(f"hello\n{PLANT}\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 1
    assert "a.txt:2" in capsys.readouterr().err


@needs_git
def test_file_names_and_normalised_text_are_scanned(repo):
    configure()
    (repo / f"{PLANT}.txt").write_text("fine\n")
    git(repo, "add", f"{PLANT}.txt")
    assert check(repo) == 1  # the name alone
    git(repo, "reset", "-q")
    (repo / f"{PLANT}.txt").unlink()
    wide = "".join(chr(ord(c) + 0xFEE0) if c.isascii() and c.isalpha() else c for c in PLANT)  # fullwidth letters
    assert wide != PLANT
    (repo / "a.txt").write_text(f"hello\n{wide}\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 1  # NFKC folds it back


@needs_git
def test_a_merge_leaves_out_a_line_only_when_the_other_side_added_it_to_the_same_file(repo):
    configure()
    git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text(f"{PLANT}\n")
    git(repo, "add", "side.txt")
    git(repo, "commit", "-q", "-m", "side", "--no-verify")
    git(repo, "checkout", "-q", "main")
    (repo / "main.txt").write_text("main work\n")
    git(repo, "add", "main.txt")
    git(repo, "commit", "-q", "-m", "main work")
    git(repo, "merge", "--no-commit", "--no-ff", "side")
    assert check(repo) == 0
    (repo / "elsewhere.txt").write_text(f"{PLANT}\n")  # the same text, in another file
    git(repo, "add", "elsewhere.txt")
    assert check(repo) == 1


@needs_git
def test_with_no_pattern_the_check_refuses(repo):
    configure("# nothing here\n")
    (repo / "a.txt").write_text("hello\nclean\n")
    git(repo, "add", "a.txt")
    assert check(repo) == 1
    (commit_wall.folder() / "patterns.toml").unlink()
    assert check(repo) == 1  # missing, too


@needs_git
def test_the_hooks_carry_their_config_folder_and_a_different_ws_home_cannot_empty_it(repo, box, monkeypatch):
    configure()
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 0
    hooks = Path(git(repo, "config", "--local", "core.hooksPath").strip())
    assert {"pre-commit", "pre-merge-commit", "commit-msg"} <= {f.name for f in hooks.iterdir()}
    assert "--wall-dir" in (hooks / "pre-commit").read_text()
    other = box / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("WS_HOME", str(other))  # the committing shell points ws somewhere with no patterns
    (repo / "a.txt").write_text(f"hello\n{PLANT}\n")
    git(repo, "add", "a.txt")
    assert git(repo, "commit", "-m", "x", check=False).startswith("1\n")


@needs_git
def test_a_hooks_path_with_a_tilde_is_expanded_and_restored_as_written(repo, box):
    configure()
    old = box / "home" / "oldhooks"
    old.mkdir()
    (old / "pre-commit").write_text("#!/bin/sh\necho OLD-HOOK >&2\nexit 0\n")
    (old / "pre-commit").chmod(0o755)
    git(repo, "config", "--local", "core.hooksPath", "~/oldhooks")
    assert cli.main(["commit-wall", "install", "--repo", str(repo)]) == 0
    (repo / "a.txt").write_text("hello\nclean\n")
    git(repo, "add", "a.txt")
    assert "OLD-HOOK" in git(repo, "commit", "-m", "fine", check=False)  # the ~ path was found
    assert cli.main(["commit-wall", "uninstall", "--repo", str(repo)]) == 0
    assert git(repo, "config", "--local", "--get", "core.hooksPath").strip() == "~/oldhooks"


@needs_git
@pytest.mark.parametrize("install_from, uninstall_from", [("repo", "wt"), ("wt", "repo")])
def test_a_repository_and_its_worktrees_share_one_record(repo, box, install_from, uninstall_from):
    configure()
    wt = box / "wt"
    git(repo, "worktree", "add", "-q", "-b", "other", str(wt))
    where = {"repo": repo, "wt": wt}
    assert cli.main(["commit-wall", "install", "--repo", str(where[install_from])]) == 0
    here = git(repo, "config", "--local", "core.hooksPath").strip()
    assert (
        cli.main(["commit-wall", "install", "--repo", str(where[uninstall_from])]) == 1
    )  # already on, seen from either
    assert cli.main(["commit-wall", "uninstall", "--repo", str(where[uninstall_from])]) == 0
    assert git(repo, "config", "--local", "--get", "core.hooksPath", check=False).startswith("1\n")
    assert not Path(here).exists()  # no hooksPath left pointing at a deleted folder


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


@needs_git
def test_the_first_commit_of_a_new_repository_is_scanned(box):
    configure()
    fresh = box / "fresh"
    fresh.mkdir()
    git(fresh, "init", "-q", "-b", "main")
    (fresh / "f.txt").write_text(f"{PLANT}\n")
    git(fresh, "add", "f.txt")
    assert check(fresh) == 1
    git(fresh, "reset", "-q")
    (fresh / "f.txt").write_text("fine\n")
    git(fresh, "add", "f.txt")
    assert check(fresh) == 0
