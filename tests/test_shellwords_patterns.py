"""Patterns the reader follows or refuses, case-change operators and `-c` wrappers. A pattern it does not fully
understand (`$`, a backquote, `[^`, `[!`, `[[:`, an extglob group, an anchored or globbed replacement) is a typed
refusal and never left as written; the six case-change operators match bash; `su - root -c TEXT` and `su -- root -c
TEXT` are read, and a `su` or `script` with no text to read is refused, not dropped. The protected file in these tests
is `~/.aws/credentials` (a rule that names one file), so a case only denies when the operator makes the value that."""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import sys

import pytest

from ws import cli, policy
from ws import shellwords as sw


def _bash_major(path: str | None) -> int:
    """The major version of the bash at `path` (0 when there's none): the oracle needs bash 4 or newer, which has the
    case operators, zero-padded sequences and the replacement forms; macOS ships bash 3.2 as /bin/bash."""
    if not path:
        return 0
    import subprocess

    out = subprocess.run([path, "-c", "echo ${BASH_VERSINFO[0]}"], capture_output=True, text=True, check=False).stdout
    return int(out.strip() or 0) if out.strip().isdigit() else 0


BASH = shutil.which("bash")
needs_bash = pytest.mark.skipif(_bash_major(BASH) < 4, reason="needs bash 4 or newer on PATH")
SECRET = "~/.aws/credentials"


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
    code = cli.main(["gate", "check", "--runner", "claude-code"])
    return code, capsys.readouterr().err


def refused(monkeypatch, capsys, cmd: str) -> bool:
    return hook(monkeypatch, capsys, cmd)[0] == 2


def bash_words(text: str, home, cwd, setup: str) -> list[str]:
    proc = subprocess.run(
        [BASH, "--norc", "--noprofile", "-c", f'set -f; {setup}; printf "%s\\0" {text}'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        cwd=str(cwd),
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert proc.returncode == 0, (text, proc.stderr)
    return proc.stdout.decode().split("\0")[:-1]


def value(text: str, **env: str) -> list[str]:
    return sw.lex(text, {"HOME": "/h", **env}).words


# --- R2: a pattern the reader does not fully understand is a refusal ---------------------------------------------

CUTS = [
    "${X%$Y}", "${X%%$Y}", "${X#$Y}", "${X%${Y}}", "${X%`echo .pub`}", "${X%$(echo .pub)}", "${X%.[^x]ub}",
    "${X%.[!x]ub}", "${X#[!x]*}", "${X%.[[:alpha:]]ub}", "${X%[[.period.]]pub}", "${X%[[=p=]]ub}", "${X%@(.pub)}",
    "${X%!(x)}", "${X%+(.pub)}", "${X%*(.pub)}", "${X%?(.pub)}", "${X%%@(.pub|.txt)}",
]  # fmt: skip
SWAPS = [
    "${X/$Y}", "${X/$Y/}", "${X/%pub}", "${X/#pub/x}", "${X//%pub}", "${X/p*}", "${X//p?b/x}", "${X/[p]ub}",
    "${X/.pub/$Y}", "${X/.pub/`echo`}", "${X/@(.pub)}", "${X/.pub/&}", "${X//.pub/&&}", "${X/[^x]}",
]  # fmt: skip
OFFSETS = ["${X:0:(-4)}", "${X:(0):4}", "${X:0:0x4}", "${X:0:1+1}", "${X:0:$Y}", "${X:${#X}-4}", "${X:0:-0x4}"]


@pytest.mark.parametrize("raw", [*CUTS, *SWAPS, *OFFSETS])
def test_a_pattern_the_reader_does_not_follow_is_a_typed_refusal(raw):
    with pytest.raises(sw.ShellError) as err:
        value(raw, X="/h/.aws/credentials.pub", Y=".pub")
    assert err.value.kind == "unsupported", raw


@pytest.mark.parametrize("raw", [*CUTS, *SWAPS, *OFFSETS])
def test_a_path_built_with_such_a_pattern_is_refused_through_the_hook(box, monkeypatch, capsys, raw):
    code, said = hook(monkeypatch, capsys, f"X=~/.aws/credentials.pub; Y=.pub; cat {raw}")
    assert code == 2 and ("does not follow" in said or "cannot be read" in said), (raw, said)


@pytest.mark.parametrize("raw", [*CUTS[:6], *SWAPS[:4], *OFFSETS[:3]])
def test_the_refusal_does_not_depend_on_the_path_being_protected(box, monkeypatch, capsys, raw):
    assert refused(monkeypatch, capsys, f"X=notes.txt; Y=.txt; echo {raw}")


def test_text_in_single_quotes_and_a_name_the_line_does_not_set_are_not_refused(box, monkeypatch, capsys):
    assert value("'${X%$Y}'", X="a") == ["${X%$Y}"]
    assert value("${NOPE%$Y} ${NOPE/%a/b} ${NOPE:0:4}") == ["${NOPE%$Y}", "${NOPE/%a/b}", "${NOPE:0:4}"]
    assert hook(monkeypatch, capsys, "echo '${X%$Y}' | wc -c")[0] == 0


ORDINARY = [
    ("${X%.*}", "dir/name.tar.gz"), ("${X%%.*}", "dir/name.tar.gz"), ("${X#*/}", "dir/name.tar.gz"),
    ("${X##*/}", "dir/name.tar.gz"), ("${X%/*}", "dir/name.tar.gz"), ("${X%.gz}.bak", "dir/name.tar.gz"),
    ("${X/a/b}", "banana"), ("${X//a/b}", "banana"), ("${X//a}", "banana"), ("${X/}", "banana"),
    ("${X//\\//-}", "a/b/c"), ('${X//"/"/-}', "a/b/c"), ("${X/'an'/AN}", "banana"), ("${X//a/ b }", "banana"),
    ("${X:1:3}", "banana"), ("${X: -3}", "banana"), ("${X:2}", "banana"), ("${X:0:-2}", "banana"),
    ("${X: -4:2}", "banana"), ("${X: -3:-1}", "banana"), ("${X:9}", "banana"), ("${X:9:-1}", "banana"),
    ("${X: -100}", "banana"), ("${X: -100:2}", "banana"), ("${X:2:99}", "banana"), ("${X/.pub/\"\"}", "a.pub"),
    ('${X/"".pub}', "a.pub"), ("${X//\\.pub/-}", "a.pub.pub"),
]  # fmt: skip


@needs_bash
@pytest.mark.parametrize(("raw", "x"), ORDINARY)
def test_the_ordinary_forms_are_read_as_bash_reads_them(box, raw, x):
    home, work = box
    got = sw.lex(raw, {"HOME": str(home), "X": x}).words
    want = bash_words(raw, home, work, f"X='{x}'")
    assert got == want or (got == [] and want == [""]), raw  # (printf shows no word as one empty one)


def test_an_offset_that_ends_before_it_starts_is_refused_as_bash_errors():
    for raw in ("${X:0:-9}", "${X: -3:-5}"):
        with pytest.raises(sw.ShellError) as err:
            value(raw, X="banana")
        assert err.value.kind == "unsupported"


def test_ordinary_commands_with_these_forms_are_allowed(box, monkeypatch, capsys):
    for cmd in (
        "f=a/b.tar.gz; echo ${f%.*} ${f##*/} ${f/a/b} ${f:1:3}",
        "for f in a.txt b.txt; do echo ${f%.txt}.bak; done",
        "f=$HOME/proj/x.py; cat ${f%.py}.txt",
    ):
        assert hook(monkeypatch, capsys, cmd)[0] == 0, cmd


# --- R3: the case-change operators are bash's ---------------------------------------------------------------------

OPS = [",", ",,", "^", "^^", "~", "~~"]


@needs_bash
@pytest.mark.parametrize("op", OPS)
@pytest.mark.parametrize("x", ["aBc/dEf", "ABC", "abc", "_ab", "1a", "a b"])
def test_the_case_change_operators_match_bash(box, op, x):
    home, work = box
    raw = '"${X' + op + '}"'
    assert sw.lex(raw, {"HOME": str(home), "X": x}).words == bash_words(raw, home, work, f"X='{x}'"), (op, x)


@pytest.mark.parametrize(
    ("op", "x", "want"),
    [
        (",", "ABC", "aBC"), (",,", "ABC", "abc"), ("^", "abc", "Abc"), ("^^", "abc", "ABC"), ("~", "aBc", "ABc"),
        ("~~", "aBc", "AbC"), ("^^", "déf", "DÉF"), (",,", "DÉF", "déf"), ("^^", "straße", "STRAßE"),
        ("~~", "straße", "STRAßE"), ("^", "ßa", "ßa"),
    ],
)  # fmt: skip
def test_the_case_change_operators_by_hand(op, x, want):
    assert value('"${X' + op + '}"', X=x) == [want]


@pytest.mark.parametrize("raw", ["${X,,A}", "${X,[A-Z]}", "${X^^[a-c]}", "${X~~a}", "${X,,*}", "${X^?}", "${X~a}"])
def test_a_case_operator_with_a_pattern_is_refused(raw):
    with pytest.raises(sw.ShellError) as err:
        value(raw, X="ABC")
    assert err.value.kind == "unsupported"


def test_a_case_change_that_makes_a_protected_path_is_refused(box, monkeypatch, capsys):
    assert refused(monkeypatch, capsys, "X=~/.AWS/CREDENTIALS; cat ${X,,}")
    assert refused(monkeypatch, capsys, 'X=~/.AWS/CREDENTIALS; cat "${X,,}"')
    assert refused(monkeypatch, capsys, "cd ~; X=.AWS/CREDENTIALS; cat ${X,,}")
    assert refused(monkeypatch, capsys, "cd ~; X=.aws/CREDENTIALS; cat ${X,,}")
    assert refused(monkeypatch, capsys, "cd ~; X=.AWS/credentials; cat ${X,,}")
    assert refused(monkeypatch, capsys, "cd ~; X=.AWS/CREDENTIALS; cat ${X~~}")


def test_a_case_change_that_does_not_make_a_protected_path_is_allowed(box, monkeypatch, capsys):
    assert not refused(monkeypatch, capsys, "cd ~; X=.AWS/credentials; cat ${X,}")  # only the first letter changes
    assert not refused(monkeypatch, capsys, "cd ~; X=.AWS/credentials; cat ${X^^}")  # upper case is another file
    assert not refused(monkeypatch, capsys, "cd ~; X=.AWS/credentials; cat ${X~~}")  # so is the swapped case


def test_the_at_operators():
    assert value('"${X@E}"', X="a\\x2fb\\n") == ["a/b\n"]
    assert value('"${X@E}"', X="it's") == ["it's"]
    for raw in ("${X@Q}", "${X@U}", "${X@L}", "${X@u}", "${X@l}", "${X@P}", "${X@A}", "${X@a}", "${X@K}", "${X@k}"):
        with pytest.raises(sw.ShellError) as err:
            value(raw, X="a")
        assert err.value.kind == "unsupported", raw


def test_an_escape_that_builds_a_protected_path_is_refused(box, monkeypatch, capsys):
    assert refused(monkeypatch, capsys, "cd ~; X='.aws/credent\\x69als'; cat ${X@E}")


# --- R4: su and the other -c wrappers -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        'su - root -c "{}"',
        'su -- root -c "{}"',
        'su -l root -c "{}"',
        'su - -c "{}"',
        'su -s /bin/sh root -c "{}"',
        'su root -c "{}"',
        'su -c "{}" root',
        'su - root --command "{}"',
        'su - root --command="{}"',
        'su -lc "{}" root',
        'su -- -c "{}"',
        'runuser - root -c "{}"',
        'runuser -- root -c "{}"',
        'runuser -l root -c "{}"',
        'flock -n /tmp/lock -c "{}"',
        'script -qec "{}" /dev/null',
        'sudo su - root -c "{}"',
        'timeout 5 su - root -c "{}"',
    ],
)
def test_the_text_of_a_c_wrapper_is_read_as_a_command(box, monkeypatch, capsys, cmd):
    assert refused(monkeypatch, capsys, cmd.format(f"cat {SECRET}")), cmd
    assert policy.simple_commands(cmd.format("cat x"))[-1] == ["cat", "x"], cmd


@pytest.mark.parametrize(
    "cmd", ["su", "su -", "su - root", "su root", "su -l", "script", "script -q out.txt", "su root extra -c x"]
)
def test_a_wrapper_that_runs_a_shell_with_no_text_to_read_is_refused_not_dropped(box, monkeypatch, capsys, cmd):
    with pytest.raises(sw.ShellError) as err:
        policy.simple_commands(cmd)
    assert err.value.kind == "unsupported"
    code, said = hook(monkeypatch, capsys, cmd)
    assert code == 2 and "cannot be read" in said, (cmd, said)


@pytest.mark.parametrize(
    "cmd",
    [
        "runuser -u bob -- ls -c",
        "flock /tmp/lock ls -c 1",
        "runuser -u bob -- bash -c 'echo hi'",
        "su - root -c 'echo hi'",
        "flock -n /tmp/lock -c 'echo hi'",
    ],
)
def test_a_wrapper_that_starts_a_program_is_still_read_as_that_program(box, monkeypatch, capsys, cmd):
    assert hook(monkeypatch, capsys, cmd)[0] == 0, cmd


def test_runuser_with_a_user_option_reads_the_program_not_a_text():
    assert policy.simple_commands("runuser -u bob -- cat x -c y") == [["cat", "x", "-c", "y"]]
