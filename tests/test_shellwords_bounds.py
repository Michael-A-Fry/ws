"""Paths are resolved against the file system with a bound: a path of more than 4,096 characters or 256 parts, and a
command line that names more than 8,192 paths, are typed refusals before any of the work is done, so no command line
outlasts the hook's time. The long lines here came from a re-check of the reader: a variable of a hundred parts, used
a few thousand times in one word, then a protected read."""

from __future__ import annotations

import io
import json
import sys
import time

import pytest

from ws import cli, paths, policy
from ws import shellwords as sw

KEY = "~/.ssh/id_rsa"


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(work)
    return home, work


def hook(monkeypatch, capsys, cmd: str) -> tuple[int, str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "hook_event_name": "PreToolUse"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    code = cli.main(["gate", "check", "--runner", "claude-code", *paths.pin_of().args()])
    return code, capsys.readouterr().err


LONG_LINES = {
    "9.8 kB, a variable used 4,800 times": "X=" + "/a" * 100 + '; cat "' + "$X" * 4800 + f'"; cat {KEY}',
    "64 kB, the same": ("X=" + "/a" * 100 + '; cat "' + "$X" * 4800 + '"; ' + f"cat {KEY}; ").ljust(64 * 1024, " "),
    "a literal path of 30,000 parts": "cat " + "/a" * 30000,
    "the same word many times": "X=" + "/a" * 2000 + "; cat " + "$X " * 20000,
}


@pytest.mark.parametrize("name", list(LONG_LINES))
def test_a_long_path_is_refused_in_well_under_the_deadline(box, monkeypatch, capsys, name):
    cmd = LONG_LINES[name][: policy.MAX_COMMAND_CHARS]
    began = time.perf_counter()
    reason = policy.is_denied_command(policy.default_policy(), cmd, cwd=str(box[1]))
    assert time.perf_counter() - began < 1, name
    assert reason, name
    began = time.perf_counter()
    code, _ = hook(monkeypatch, capsys, cmd)
    assert code == 2 and time.perf_counter() - began < 1.5, name


def test_a_long_path_that_is_short_once_written_out_is_read_quickly(box):
    began = time.perf_counter()
    assert policy.is_denied_command(policy.default_policy(), "cat " + "/." * 30000, cwd=str(box[1])) is None
    assert policy.is_denied_command(policy.default_policy(), "cat ~/" + "x/.." * 15000 + KEY[1:], cwd=str(box[1]))
    assert time.perf_counter() - began < 1


def test_a_path_of_too_many_parts_is_a_typed_refusal_and_one_at_the_cap_is_read(box):
    with pytest.raises(sw.ShellError) as err:
        sw.check_path("/a" * (sw.MAX_PATH_PARTS + 1))
    assert err.value.kind == "too_long"
    assert sw.check_path("/a" * sw.MAX_PATH_PARTS) is True
    pol = policy.default_policy()
    with pytest.raises(sw.ShellError) as err:
        policy.is_denied_read(pol, "/a" * 300)
    assert err.value.kind == "too_long"
    assert policy.is_denied_read(pol, "/" + "a" * 100) is None


def test_a_word_longer_than_the_kernel_opens_is_matched_as_text_and_not_resolved(box, monkeypatch):
    seen: list[str] = []
    real = policy.os.path.realpath
    monkeypatch.setattr(
        policy.os.path, "realpath", lambda p: seen.append(p) or real(p) if str(p).startswith("/aaa") else real(p)
    )
    assert sw.check_path("/" + "a" * (sw.MAX_PATH_CHARS - 1)) is True
    assert sw.check_path("/" + "a" * sw.MAX_PATH_CHARS) is False
    pol = policy.default_policy()
    assert policy.is_denied_read(pol, "/" + "a" * 5000) is None and seen == []
    assert policy.is_denied_command(pol, "echo " + "x" * 60000) is None and seen == []
    assert policy.is_denied_read(pol, "/" + "a" * 100) is None and len(seen) == 1


def test_the_number_of_paths_resolved_in_one_command_is_capped(box, monkeypatch):
    monkeypatch.setattr(sw, "MAX_PATHS", 5)
    pol = policy.default_policy()
    assert policy.is_denied_command(pol, "cat a b c") is None
    reason = policy.is_denied_command(pol, "cat a b c d e f g")
    assert reason and "more than 5 paths" in reason


def test_a_command_of_ordinary_paths_is_not_limited(box, monkeypatch, capsys):
    cmd = "cat " + " ".join(f"proj/dir{k}/file{k}.txt" for k in range(2000))
    assert len(cmd) < policy.MAX_COMMAND_CHARS
    assert hook(monkeypatch, capsys, cmd)[0] == 0


def test_each_path_resolved_keeps_the_deadline(box, monkeypatch):
    monkeypatch.setattr(sw, "TIME_LIMIT", 0.0)
    with pytest.raises(sw.ShellError) as err, sw.time_limit():
        sw.check_path("/a")
    assert err.value.kind == "too_slow"


def test_a_glob_of_many_parts_is_refused_not_walked(box, monkeypatch, capsys):
    began = time.perf_counter()
    code, _ = hook(monkeypatch, capsys, "cat ~/" + "**/" * 20000 + "x")
    assert code == 2 and time.perf_counter() - began < 1.5


def test_variables_that_add_more_than_a_megabyte_are_refused_not_built(box, monkeypatch, capsys):
    cmd = "X=" + "a" * 30000 + '; echo "' + "$X" * 9000 + '"'
    began = time.perf_counter()
    reason = policy.is_denied_command(policy.default_policy(), cmd[: policy.MAX_COMMAND_CHARS])
    assert reason and "characters" in reason and time.perf_counter() - began < 1
    with pytest.raises(sw.ShellError) as err, sw.time_limit():
        sw.lex("echo " + "$X" * 100, {"X": "a" * (sw.MAX_EXPANDED // 50)})
    assert err.value.kind == "too_long"
    with sw.time_limit():  # a line that stays under the cap is read
        assert sw.lex("echo " + "$X" * 10, {"X": "a" * 1000}).words[0] == "echo"
    assert sw.lex("echo " + "$X" * 100, {"X": "a" * (sw.MAX_EXPANDED // 50)}).words  # untimed: not counted
