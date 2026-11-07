"""The `${...}` reader is an allowlist: a body, operator, offset or pattern character it does not fully follow is a typed
refusal (kind `unsupported`, with how to write the command plainly) and never text left as written or a value passed
through. The forms the reader does follow are listed in `ws.shellwords.OPERATORS`; a test below fails when one is added
without a sample here. Each refused form is also pinned through the real hook with a stand-in protected folder in a
scratch policy and with the default `~/.ssh/**`."""

from __future__ import annotations

import io
import itertools
import json
import sys

import pytest

from ws import cli, paths
from ws import shellwords as sw


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def hook(monkeypatch, capsys, cmd: str, *extra: str) -> tuple[int, str]:
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "hook_event_name": "PreToolUse"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    code = cli.main(["gate", "check", "--runner", "claude-code", *paths.pin_of().args(), *extra])
    return code, capsys.readouterr().err


def value(text: str, **env: str) -> list[str]:
    return sw.lex(text, {"HOME": "/h", **env}).words


# --- every operator the reader follows has a sample ---------------------------------------------------------------

# (raw word, variables, the one word it makes) for each entry of OPERATORS. A shell makes the same word of each.
SAMPLES = {
    "-": ('"${X-d}"', {"X": "v"}, "v"),
    ":-": ('"${E:-d}"', {"E": ""}, "d"),
    "+": ('"${X+d}"', {"X": "v"}, "d"),
    ":+": ('"${E:+d}"', {"E": ""}, ""),
    "?": ('"${X?m}"', {"X": "v"}, "v"),
    ":?": ('"${X:?m}"', {"X": "v"}, "v"),
    "#": ('"${X#a/}"', {"X": "a/b/c"}, "b/c"),
    "##": ('"${X##*/}"', {"X": "a/b/c"}, "c"),
    "%": ('"${X%/*}"', {"X": "a/b/c"}, "a/b"),
    "%%": ('"${X%%/*}"', {"X": "a/b/c"}, "a"),
    "/": ('"${X/a/z}"', {"X": "aa"}, "za"),
    "//": ('"${X//a/z}"', {"X": "aa"}, "zz"),
    ":off": ('"${X:1}"', {"X": "abc"}, "bc"),
    ":off:len": ('"${X:1:1}"', {"X": "abc"}, "b"),
    ",": ('"${X,}"', {"X": "AB"}, "aB"),
    ",,": ('"${X,,}"', {"X": "AB"}, "ab"),
    "^": ('"${X^}"', {"X": "ab"}, "Ab"),
    "^^": ('"${X^^}"', {"X": "ab"}, "AB"),
    "~": ('"${X~}"', {"X": "aB"}, "AB"),
    "~~": ('"${X~~}"', {"X": "aB"}, "Ab"),
    "@E": ('"${X@E}"', {"X": "a\\x2fb"}, "a/b"),
    "[0]": ('"${X[0]}"', {"X": "v"}, "v"),
    "[@]": ('"${X[@]}"', {"X": "v"}, "v"),
    "[*]": ('"${X[*]}"', {"X": "v"}, "v"),
    "!NAME": ('"${!P}"', {"P": "X", "X": "v"}, "v"),
}


def test_every_operator_the_reader_follows_has_a_sample():
    """Add an operator to OPERATORS and this fails until SAMPLES shows it working (and the refusals below still hold)."""
    assert set(sw.OPERATORS) == set(SAMPLES)
    assert len(sw.OPERATORS) == len(set(sw.OPERATORS))


@pytest.mark.parametrize("op", sw.OPERATORS)
def test_each_operator_makes_the_word_a_shell_makes(op):
    raw, env, want = SAMPLES[op]
    assert value(raw, **env) == [want]


def test_the_shape_reader_only_ever_answers_with_a_listed_operator():
    """Every `rest` of up to three characters from the operator alphabet is one of OPERATORS or a refusal."""
    for n in range(4):
        for chars in itertools.product(":-=+?#%/,^~@E0 1.$", repeat=n):
            rest = "".join(chars)
            try:
                op = sw._shape("X" + rest, rest)
            except sw.ShellError as err:
                assert err.kind == "unsupported", rest
            else:
                assert op == "" or op in sw.OPERATORS or op in ("-", ":-", "=", ":=", "+", ":+", "?", ":?"), rest


# --- every form it does not follow is refused ---------------------------------------------------------------------

REFUSED = {
    "an at operator": [
        "${X@L}",
        "${X@U}",
        "${X@u}",
        "${X@l}",
        "${X@Q}",
        "${X@P}",
        "${X@A}",
        "${X@a}",
        "${X@K}",
        "${X@k}",
    ],
    "a tilde in a pattern": [
        "${X#~}",
        "${X##~}",
        "${X%~}",
        "${X%%~}",
        "${X/~}",
        "${X//~}",
        "${X#a~}",
        "${X/a/~}",
        "${X//a/~b}",
        "${X/~/a}",
    ],
    "an offset or length that is not a plain decimal": [
        "${X:010}",
        "${X:08}",
        "${X: 010}",
        "${X:00}",
        "${X:0x8}",
        "${X:1+1}",
        "${X:$Y}",
        "${X:(1)}",
        "${X:1:010}",
        "${X:1:0x2}",
        "${X:1:$Y}",
        "${X:1.5}",
        "${X:1:}",
        "${X::1}",
        "${X:1 }",
        "${X:12345678901234567890}",
    ],
    "a command group": ["${ cmd; }", "${\tcmd; }", "${\ncmd; }", "${| cmd; }", "${ cmd; ", "${X-}${"],
    "a body that is not a name or a parameter": ["${%}", "${;}", "${.}", "${/}", "${:}", "${=}", "${~}", "${X.y}"],
    "a length": ["${#X}", "${#X[@]}", "${#1}"],
    "a pattern that is not followed": ["${X%$Y}", "${X#[!a]}", "${X/a*}", "${X/%a/b}", "${X/a/&}", "${X%@(a)}"],
    "an index": ["${X[1]}", "${X[a]}"],
    "an assignment": ["${X=d}", "${E:=d}", "${U:=d}"],
    "the last word of the previous command": ["${_}", "${_:-a}"],
    "a case change by a pattern": ["${X,,a}", "${X^^[a-c]}", "${X~a}"],
    "an indirection that is not a plain name": ["${!X[@]}", "${!X*}", "${!Q}"],
}
FORMS = [raw for forms in REFUSED.values() for raw in forms]
SET = {"X": "/h/.aws/credentials", "Y": ".pub", "P": "X"}


@pytest.mark.parametrize("raw", FORMS)
def test_a_form_the_reader_does_not_follow_is_a_typed_refusal_with_a_hint(raw):
    # a variable the line sets, and (below) one it does not: the same shape is refused either way
    with pytest.raises(sw.ShellError) as err:
        sw.lex(f'"{raw}"' if " " in raw or "\n" in raw or "\t" in raw else raw, {"HOME": "/h", **SET})
    assert err.value.kind == "unsupported", raw
    assert "write" in err.value.message, raw  # a hint that says how to write it plainly


UNSET_REFUSED = [
    raw
    for raw in FORMS
    if raw
    not in (
        "${X%$Y}", "${X#[!a]}", "${X/a*}", "${X/%a/b}", "${X/a/&}", "${X%@(a)}", "${X#a~}", "${X/a/~}", "${X//a/~b}",
        "${X/~/a}", "${X#~}", "${X##~}", "${X%~}", "${X%%~}", "${X/~}", "${X//~}", "${X[1]}", "${X[a]}", "${!X[@]}",
        "${!X*}", "${!Q}",
    )
]  # fmt: skip


@pytest.mark.parametrize("raw", UNSET_REFUSED)
def test_the_shape_is_refused_for_a_variable_the_line_does_not_set_too(raw):
    with pytest.raises(sw.ShellError) as err:
        sw.lex(f'"{raw}"' if " " in raw or "\n" in raw or "\t" in raw else raw, {"HOME": "/h"})
    assert err.value.kind == "unsupported", raw


@pytest.mark.parametrize("raw", ["${X:1:2}", "${X: -1}", "${X:0}", "${X:-1}", "${X:0:-0}", "${X: -1: 2}"])
def test_a_plain_decimal_offset_is_followed(raw):
    sw.lex(raw, {"HOME": "/h", "X": "abcd"})  # no refusal


@pytest.mark.parametrize("raw", ["${1}", "${10}", "${0}", "${@}", "${*}", "${#}", "${?}", "${$}", "${!}", "${-}"])
def test_a_positional_or_special_parameter_is_unknown_and_left_as_written(raw):
    assert sw.lex(raw, {"HOME": "/h"}).words == [raw]


def test_a_command_group_inside_single_quotes_is_text_and_not_refused():
    assert value("'${ cmd; }'") == ["${ cmd; }"]


# --- the four forms, through the real hook ------------------------------------------------------------------------

DIRS = [".ssh", "vault"]  # the default policy protects ~/.ssh/**; a scratch policy protects ~/vault/**


@pytest.fixture(params=DIRS)
def folder(request, box):
    if request.param == "vault":
        (box / ".ws").mkdir()
        (box / ".ws" / "policy.toml").write_text('[gate]\ndeny_read = ["~/vault/**"]\n')
    return request.param


def forms(d: str) -> list[str]:
    big = d.upper()
    return [
        f"X={big}; cat ~/${{X@L}}/id_rsa",
        f"X={d}; cat ~/${{X@U}}/id_rsa",
        f"X={big}; cat ~/${{X@u}}/id_rsa",
        f"X={big}; cat ~/${{X@l}}/id_rsa",
        f"X={d}; cat ~/${{X@Q}}/id_rsa",
        f"X=$HOME$HOME/{d}/id_rsa; cat ${{X#~}}",
        f"X=$HOME$HOME/{d}/id_rsa; cat ${{X##~}}",
        f"X=$HOME$HOME/{d}/id_rsa; cat ${{X%~}}",
        f"X=$HOME$HOME/{d}/id_rsa; cat ${{X/~}}",
        f"X=$HOME$HOME/{d}/id_rsa; cat ${{X//a/~}}",
        f"X=12345678{d}; cat ~/${{X:010}}/id_rsa",
        f"X=12345678{d}; cat ~/${{X:0x8}}/id_rsa",
        f"X=12345678{d}; cat ~/${{X:8:010}}/id_rsa",
        f'echo "${{ cat ~/{d}/id_rsa; }}"',
        f'echo "${{| cat ~/{d}/id_rsa; }}"',
        f'echo "${{\tcat ~/{d}/id_rsa; }}"',
    ]


@pytest.mark.parametrize("n", range(16))
def test_the_four_forms_are_refused_through_the_hook(box, monkeypatch, capsys, folder, n):
    cmd = forms(folder)[n]
    code, said = hook(monkeypatch, capsys, cmd)
    assert code == 2, (cmd, said)
    assert "write" in said or "deny_read" in said, (cmd, said)  # the hint reaches the person (or the path rule did)


def test_the_plain_writing_of_each_is_still_allowed(box, monkeypatch, capsys, folder):
    for cmd in (
        "X=notes; cat ~/${X}/a.txt",
        "X=a.txt; cat ${X:0:1}.md",
        "f=a/b.txt; echo ${f%.txt} ${f##*/}",
        "echo ${1} ${@}",
    ):
        assert hook(monkeypatch, capsys, cmd)[0] == 0, cmd
    assert hook(monkeypatch, capsys, f"cat ~/{folder}/id_rsa")[0] == 2  # and the plain protected path is not


def test_the_worst_case_of_a_large_input_is_under_two_seconds(box, monkeypatch, capsys):
    import time

    for body in (
        "X=" + "a" * 60000 + "; echo ${X:010}",
        "echo " + "${ " * 20000,
        "echo " + "${X@L}" * 10000,
        "X=a; echo " + "${X#~}" * 10000,
    ):
        started = time.monotonic()
        code, _ = hook(monkeypatch, capsys, body[:65536])
        assert code == 2 and time.monotonic() - started < 2, body[:20]


@pytest.mark.parametrize("cmd", ["IFS=/; cat x", "IFS+=/ cat x", "export IFS=/"])
def test_a_line_that_sets_ifs_is_refused(cmd):
    with pytest.raises(sw.ShellError) as err:
        sw.prep(cmd)
    assert err.value.kind == "unsupported", cmd
    assert "write" in err.value.message, cmd


@pytest.mark.parametrize("cmd", ["echo a $_", 'echo "$_"b'])
def test_reading_the_last_word_of_the_previous_command_is_refused(cmd):
    with pytest.raises(sw.ShellError) as err:
        sw.lex(cmd, {"HOME": "/h"})
    assert err.value.kind == "unsupported", cmd
    assert "write" in err.value.message, cmd


@pytest.mark.parametrize("cmd", ["echo $IFS_DIR", "echo MYIFS=1", "cat a_b", "echo $_x"])
def test_names_that_only_look_like_ifs_or_the_last_word_are_read_as_usual(cmd):
    sw.lex(sw.prep(cmd), {"HOME": "/h"})  # no refusal
