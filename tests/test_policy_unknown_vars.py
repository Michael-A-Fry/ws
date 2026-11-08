"""A variable the line sets more than once, appends to, or sets inside a block, a loop body or ahead of a command has a
value the reader can't know (which assignment ran depends on a subshell, a branch or a loop), so a word that uses it is
refused. A variable set once, plainly, is still followed."""

from __future__ import annotations

import pytest

from ws import policy, shellwords


@pytest.fixture
def pol(monkeypatch):
    monkeypatch.setenv("HOME", "/home/pat")
    monkeypatch.setenv("WS_HOME", "/home/pat/.ws")
    return policy.default_policy()


@pytest.mark.parametrize(
    "cmd",
    [
        "X=$HOME/.aws/credentials; (X=/tmp/a); cat $X",
        "X=$HOME/.aws/credentials; false && X=/tmp/a; cat $X",
        "X=$HOME/.aws/credentials; true || X=/tmp/a; cat $X",
        "X=$HOME/.aws/credentials; X=/tmp/a | true; cat $X",
        "X=$HOME/.aws/credentials; X=/tmp/a & wait; cat ${X}",
        "X=$HOME/.aws/cred; X+=entials; cat $X",
        "X=$HOME/.aw; X+=s; cat $X/credentials",
        "for d in $HOME/.aw; do X=$d; done; cat ${X}s/credentials",
        "if true; then X=$HOME/.aws; fi; cat $X/credentials",
        "{ X=$HOME/.aws; }; cat $X/credentials",
        "X=$HOME/.aws env true; X=/tmp; X=/tmp; cat $X/credentials",
        "export X=/tmp; export X=$HOME/.aws; cat $X/credentials",
    ],
)
def test_a_word_using_a_variable_of_unknown_value_is_refused(pol, cmd):
    assert policy.is_denied_command(pol, cmd, cwd="/tmp"), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "X=/tmp/a; cat $X",
        "X=/tmp/a; Y=/tmp/b; cat $X $Y",
        "for f in a b; do echo $f; done",
        "make CFLAGS=-O2 all",
        "LANG=C sort notes.txt",
        "X=/tmp/a; X=/tmp/b; echo done",  # set twice but never read
    ],
)
def test_ordinary_lines_are_left_alone(pol, cmd):
    assert not policy.is_denied_command(pol, cmd, cwd="/tmp"), cmd


def test_reading_an_unknown_name_is_a_typed_refusal_with_a_hint():
    env = shellwords.Unknown({"HOME": "/h", "X": "/tmp"}, {"X"})
    assert "X" in env and env.get("HOME") == "/h"
    with pytest.raises(shellwords.ShellError) as err:
        shellwords.lex("cat $X", env)
    assert err.value.kind == "unsupported" and "write" in err.value.message


@pytest.mark.parametrize(
    "cmd",
    [
        "for X in a; do :; done; X=$HOME/.aws; cat $X/credentials",
        "for X in a; do X=$HOME/.aws; cat $X/credentials; done",
        "env X=$HOME/.aws sh -c 'cat $X/credentials'",
        "sudo X=$HOME/.aws bash -c 'cat $X/credentials'",
        "eval 'X=$HOME/.aws'; cat $X/credentials",
        "eval X=$HOME/.aws; cat $X/credentials",
        "source /dev/stdin <<< 'X=$HOME/.aws'; cat $X/credentials",
        "X=/tmp; . ./set.sh; cat $X/credentials",
        'read IFS <<< /; cat "$HOME${IFS}.aws${IFS}credentials"',
        'printf -v IFS %s /; cat "$HOME${IFS}.aws${IFS}credentials"',
        "Y=$HOME/.aws; declare -n X=Y; cat $X/credentials",
        "X=/tmp; printf -v X %s $HOME/.aws; cat $X/credentials",
        "X=/tmp; ((X=1)); cat $X/credentials",
        "eval 'HOME=/x'; cat ~/.aws/credentials",
        "X=$HOME/.aws; read X <<< /tmp; cat $X/credentials",
        "X=/tmp; let X=1; cat $X/credentials",
        "X=/tmp; read -r X; cat $X/credentials",
        "X=/tmp; mapfile X < f; cat $X/credentials",
        'declare IFS=/; cat "$HOME${IFS}.aws${IFS}credentials"',
    ],
)
def test_a_command_that_can_set_any_variable_makes_later_values_unknown(pol, cmd):
    assert policy.is_denied_command(pol, cmd, cwd="/tmp"), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "source .venv/bin/activate && pytest -q",
        "source ~/.venv/bin/activate",
        "for f in a b; do echo $f; done; echo ok",
        "read -r name; echo hi",
        "echo $IFS",
        'while read -r x; do echo "$x"; done < list.txt',
        "read -r x <<< hi; echo $x",
        "let x=1; echo $x",
        "grep -r IFS src",
        "git log -S IFS",
        "unset IFS",
    ],
)
def test_common_lines_with_those_commands_are_still_allowed(pol, cmd):
    assert not policy.is_denied_command(pol, cmd, cwd="/tmp"), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "source ~/.bashrc; ls $HOME",
        ". venv/bin/activate; cd $HOME/proj",
        "source .env && cd ~ && ls",
        "source ./f; cat ~/notes.txt",
    ],
)
def test_after_a_sourced_file_the_home_and_working_folders_are_not_trusted(pol, cmd):
    # the file can set HOME and PWD as eval text can, and in a project it is whatever the project holds: the recovery is
    # to write the path out in full (`ls /home/pat`) or to put the lines that use it before the `source`
    why = policy.is_denied_command(pol, cmd, cwd="/tmp")
    assert why and "isn't known" in why, cmd


def test_a_loop_over_the_paths_a_command_prints_is_refused_with_the_way_out(pol):
    # data-derived path, refused on purpose: each `$f` comes out of `git ls-files`, which the reader cannot see, so it
    # could be any file. Recovery: write the path out (`wc -l src/a.py`).
    why = policy.is_denied_command(pol, 'git ls-files | while read -r f; do wc -l "$f"; done', cwd="/tmp")
    assert why and "set the variable on its own first" in why
    assert policy.is_denied_command(pol, "wc -l src/a.py", cwd="/tmp") is None


def test_on_a_case_insensitive_platform_path_rules_match_without_regard_to_case(pol, monkeypatch):
    for caseless in (True, False):
        monkeypatch.setattr(policy, "CASELESS_PATHS", caseless)
        policy._rules.cache_clear()
        denied = policy.is_denied_command(policy.default_policy(), "cat ~/.SSH/ID_RSA", cwd="/tmp")
        assert bool(denied) is caseless, caseless
    policy._rules.cache_clear()
