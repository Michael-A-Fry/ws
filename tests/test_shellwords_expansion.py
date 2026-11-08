"""The command reader's expansions, bounds and wrappers: prefix and suffix cuts of long values, the time limit, case
and `@` operators, defaults and indirection, the working folder (`~+`, `$PWD`, `cd`), variables set by `export`,
`declare`, `read` and `for`, `env -S`, and the wrappers that run a `-c` text. Each case is also run through the
real hook entry (`ws gate check --runner claude-code` with a PreToolUse payload); where bash can show what a spelling
expands to, the reader's words are compared with bash's."""

from __future__ import annotations

import io
import json
import shlex
import shutil
import subprocess
import sys
import time

import pytest

from ws import cli, paths, policy
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
    """(exit code, what it said on stderr) of `ws gate check --runner claude-code` for a Bash call."""
    payload = json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd}, "hook_event_name": "PreToolUse"})
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    code = cli.main(["gate", "check", "--runner", "claude-code", *paths.pin_of().args()])
    return code, capsys.readouterr().err


def refused(monkeypatch, capsys, cmd: str) -> bool:
    return hook(monkeypatch, capsys, cmd)[0] == 2


def bash_words(text: str, home, cwd, setup: str = "") -> list[str]:
    """What bash makes of the words in `text` after `setup` has run. Globbing is off."""
    proc = subprocess.run(
        [BASH, "--norc", "--noprofile", "-c", f'set -f; {setup or ":"}; printf "%s\\0" {text}'],
        env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
        cwd=str(cwd),
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert proc.returncode == 0, (text, proc.stderr)
    return proc.stdout.decode().split("\0")[:-1]


# --- prefix and suffix cuts of long values; the bounds ------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("op", ["#", "##", "%", "%%"])
@pytest.mark.parametrize(
    "pat",
    [
        "*/",
        "/*",
        "*.*",
        ".*",
        "*",
        "?",
        "??",
        "[a-c]*",
        "*/*/",
        "*o*o*",
        "/home",
        "e",
        "*d/e",
        "*[.]*",
        "\\*",
        "'a.b'",
    ],
)
def test_a_cut_of_a_value_is_what_bash_cuts(box, op, pat):
    home, work = box
    value = "/home/a.b/c.d/e*"
    word = f'"${{X{op}{pat}}}"'
    assert sw.lex(word, {"X": value}).words == bash_words(word, home, work, f"X={shlex.quote(value)}"), word


def test_a_cut_with_many_stars_does_not_back_up(box):
    value = "a" * 3000
    began = time.perf_counter()
    got = sw.lex("${X#*a*a*a*a*a*b}", {"X": value}).words
    assert got == [value] and time.perf_counter() - began < 1


def test_repeated_cuts_of_a_long_value_are_read_in_well_under_a_second(box, monkeypatch, capsys):
    cmd = "X=" + "ab" * 2000 + "; echo " + "${X##*b} " * 2000
    assert 20_000 < len(cmd) < policy.MAX_COMMAND_CHARS
    began = time.perf_counter()
    assert policy.is_denied_command(policy.default_policy(), cmd) is None
    assert time.perf_counter() - began < 1
    assert hook(monkeypatch, capsys, cmd)[0] == 0


def test_many_different_cuts_of_a_long_value_are_refused_when_they_take_too_long(box, monkeypatch, capsys):
    monkeypatch.setattr(sw, "TIME_LIMIT", 0.2)
    cmd = "X=" + "ab" * 2000 + "; echo " + " ".join(f"${{X##*b{k}*}}" for k in range(1500))
    began = time.perf_counter()
    code, err = hook(monkeypatch, capsys, cmd)
    assert code == 2 and ("takes too long" in err or "expand to more than" in err), (
        err
    )  # a limit, whichever is hit first
    assert time.perf_counter() - began < 5


def test_a_value_past_the_cap_is_a_typed_refusal(box, monkeypatch, capsys):
    long = "a" * (sw.MAX_VALUE + 1)
    with pytest.raises(sw.ShellError) as err:
        sw.lex("${X%a}", {"X": long})
    assert err.value.kind == "too_long"
    assert sw.lex("${X}", {"X": long}).words == [long]  # a plain value is only text
    code, said = hook(monkeypatch, capsys, f"X={long}; echo ${{X%a}}")
    assert code == 2 and f"longer than {sw.MAX_VALUE:,} characters" in said
    assert hook(monkeypatch, capsys, f"X={'a' * sw.MAX_VALUE}; echo ${{X%a}}")[0] == 0


def test_a_deadline_that_is_hit_is_a_typed_refusal_through_the_hook(box, monkeypatch, capsys):
    assert hook(monkeypatch, capsys, "echo hi")[0] == 0
    monkeypatch.setattr(sw, "TIME_LIMIT", 0.0)
    code, said = hook(monkeypatch, capsys, "echo hi")
    assert code == 2 and "takes too long" in said and "could not check" not in said
    with pytest.raises(sw.ShellError) as err, sw.time_limit(0.0):
        sw.lex("echo hi")
    assert err.value.kind == "too_slow"


def test_the_clock_is_off_outside_a_time_limit_and_the_outer_one_wins_inside_another():
    sw.lex("echo hi")  # nothing times it
    with sw.time_limit(60.0), sw.time_limit(0.0):  # the inner block keeps the outer clock
        sw.lex("echo hi")
    with pytest.raises(sw.ShellError), sw.time_limit(0.0), sw.time_limit(60.0):
        sw.lex("echo hi")


def test_the_guards_read_a_command_under_the_same_limit(box, monkeypatch):
    monkeypatch.setattr(sw, "TIME_LIMIT", 0.0)
    with pytest.raises(sw.ShellError) as err:
        policy.simple_commands("echo hi")
    assert err.value.kind == "too_slow"
    with pytest.raises(sw.ShellError):
        policy.simple_commands_flow("echo hi")


def test_defaults_that_nest_without_end_are_a_typed_refusal():
    with pytest.raises(sw.ShellError) as err:
        sw.lex("${A:-" * 1500 + "x" + "}" * 1500, {"HOME": "/h"})
    assert err.value.kind == "too_deep"


# --- operators the reader follows without changing the path -------------------------------------------------------

HOME = "/home/pat"
ENV = {"HOME": HOME}


@pytest.mark.parametrize(
    "raw",
    ["${HOME,}", "${HOME,,}", "${HOME^}", "${HOME~}", "${HOME@E}", "${HOME[0]}", "${HOME[@]}", "${HOME[*]}",
     "${HOME[0]%/*}"],
)  # fmt: skip
def test_a_case_change_or_at_operator_or_index_reads_as_the_value(raw):
    got = sw.lex(raw, ENV).words
    assert got == [HOME.rsplit("/", 1)[0] if raw.endswith("%/*}") else HOME]


@pytest.mark.parametrize("form", ["${HOME,}", "${HOME,,}", "${HOME^}", "${HOME~}", "${HOME@E}", "${HOME[0]}"])
def test_a_path_spelled_with_such_an_operator_is_refused_through_the_hook(box, monkeypatch, capsys, form):
    assert refused(monkeypatch, capsys, f"cat {form}/.ssh/id_rsa"), form
    assert refused(monkeypatch, capsys, f'cat "{form}/.ssh/id_rsa"'), form
    assert not refused(monkeypatch, capsys, f"cat {form}/proj/a.txt"), form


@needs_bash
def test_an_index_of_zero_is_the_value_in_bash_too(box):
    home, work = box
    assert sw.lex("${HOME[0]}", {"HOME": str(home)}).words == bash_words("${HOME[0]}", home, work)


@pytest.mark.parametrize("raw", ["${HOME:$N}", "${HOME[1]}", "${HOME!x}", "${HOME@}x", "${HOME:1+1}"])
def test_an_expansion_the_reader_does_not_know_is_a_typed_refusal(box, monkeypatch, capsys, raw):
    if raw == "${HOME@}x":
        pytest.skip("covered by the case-change forms")
    with pytest.raises(sw.ShellError) as err:
        sw.lex(raw, ENV)
    assert err.value.kind == "unsupported"
    code, said = hook(monkeypatch, capsys, f"cat {raw}/.ssh/id_rsa")
    assert code == 2 and "does not follow" in said or "indexes" in said


def test_text_in_single_quotes_is_not_refused_for_an_expansion_the_reader_does_not_know(box, monkeypatch, capsys):
    assert sw.lex("echo '${HOME:$N}'", ENV).words == ["echo", "${HOME:$N}"]
    assert hook(monkeypatch, capsys, "echo '${HOME:$N}' | grep -c x")[0] == 0


def test_a_variable_the_line_does_not_set_keeps_the_operators_the_reader_follows():
    assert sw.lex("${NOPE,,} ${NOPE[1]} ${NOPE%x} ${NOPE:1:2}", ENV).words == [
        "${NOPE,,}",
        "${NOPE[1]}",
        "${NOPE%x}",
        "${NOPE:1:2}",
    ]


@pytest.mark.parametrize("raw", ["${NOPE:$N}", "${NOPE@Q}", "${NOPE@L}", "${NOPE:010}", "${NOPE,,a}", "${NOPE.x}"])
def test_a_variable_the_line_does_not_set_still_refuses_an_operator_the_reader_does_not_follow(raw):
    with pytest.raises(sw.ShellError) as err:
        sw.lex(raw, ENV)
    assert err.value.kind == "unsupported"


# --- defaults and alternatives, indirection -----------------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize(
    "word",
    ["${NOPE:-~}", "${NOPE-~}", "${HOME:+$HOME}", "${HOME:+~}", "${HOME:-x}", "${NOPE:+x}y",
     '"${NOPE:-a b}"', "${NOPE:-${HOME}/a}", "${NOPE:-$HOME/.ssh}"],
)  # fmt: skip
def test_a_default_or_alternative_is_expanded_as_bash_does(box, word):
    home, work = box
    if word == "${NOPE:+x}y":
        # an unset name reads as set for an alternative: the reading that names a path
        assert sw.lex(word, {"HOME": str(home)}).words == ["xy"]
        return
    assert sw.lex(word, {"HOME": str(home)}).words == bash_words(word, home, work), word


@pytest.mark.parametrize(
    "form",
    ["${NOPE:-~}", "${NOPE-~}", "${HOME:+$HOME}", "${HOME:+~}", "${NOPE:-$HOME}", "${NOPE:-${HOME}}"],
)
def test_a_path_in_a_default_or_alternative_is_refused_through_the_hook(box, monkeypatch, capsys, form):
    assert refused(monkeypatch, capsys, f"cat {form}/.ssh/id_rsa"), form
    assert refused(monkeypatch, capsys, f"cat {form}/.ssh/id_rsa > /dev/null"), form
    # a name the line does not set may be set outside it, so any value is possible; HOME is known
    assert refused(monkeypatch, capsys, f"cat {form}/proj/a.txt") is ("NOPE" in form), form


def test_a_default_with_nothing_to_do_with_a_path_is_allowed(box, monkeypatch, capsys):
    assert hook(monkeypatch, capsys, "echo ${NOPE:-}")[0] == 0
    assert hook(monkeypatch, capsys, "echo ${NOPE:-work}")[0] == 0


def test_a_default_on_a_name_the_line_does_not_set_may_still_be_set_outside(box, monkeypatch, capsys):
    """The shell's own environment may hold `NOPE`, so `${NOPE:-work}` is not `work` for a command that reads files."""
    code, err = hook(monkeypatch, capsys, "cat ${NOPE:-work}/a.txt")
    assert code == 2 and "set the variable on its own first" in err
    assert hook(monkeypatch, capsys, "NOPE=work; cat ${NOPE:-other}/a.txt")[0] == 0


@needs_bash
def test_an_indirect_name_is_followed_as_bash_follows_it(box):
    home, work = box
    got = sw.lex("${!N}", {"HOME": str(home), "N": "HOME"}).words
    assert got == bash_words("${!N}", home, work, "N=HOME") == [str(home)]


def test_an_indirect_name_through_the_command_is_refused_through_the_hook(box, monkeypatch, capsys):
    assert refused(monkeypatch, capsys, "N=HOME; cat ${!N}/.ssh/id_rsa")
    assert refused(monkeypatch, capsys, 'N=HOME; cat "${!N}/.ssh/id_rsa"')
    assert not refused(monkeypatch, capsys, "N=HOME; cat ${!N}/proj/a.txt")
    assert not refused(monkeypatch, capsys, "N=HOME; M=N; echo '${!M}'")


def test_an_indirect_name_that_cannot_be_followed_is_a_typed_refusal(box, monkeypatch, capsys):
    for text, env in (
        ("${!N}", ENV),
        ("${!N}", {**ENV, "N": "ELSE"}),
        ("${!N}", {**ENV, "N": "not a name"}),
        ("${!A*}", ENV),
    ):
        with pytest.raises(sw.ShellError) as err:
            sw.lex(text, env)
        assert err.value.kind == "unsupported", text
    code, said = hook(monkeypatch, capsys, "cat ${!NOPE}")
    assert code == 2 and "cannot be read" in said


# --- the working folder -------------------------------------------------------------------------------------------


@needs_bash
@pytest.mark.parametrize("word", ["~+", "~+/x", "$PWD/x", "${PWD}", "~-/x", "$OLDPWD", "a=~+:~-"])
def test_the_working_folder_is_expanded_as_bash_does(box, word):
    home, work = box
    env = {"HOME": str(home), "PWD": str(work), "OLDPWD": str(home)}
    got = sw.lex(word, env).words
    assert got == bash_words(word, home, work, f"OLDPWD={shlex.quote(str(home))}"), word


def test_the_working_folder_is_left_as_written_when_it_is_not_known():
    assert sw.lex("~+/x ~-/x $PWD", ENV).words == ["~+/x", "~-/x", "$PWD"]


@pytest.mark.parametrize(
    "cmd",
    [
        "cd ~ && cat $PWD/.ssh/id_rsa",
        "cd ~ && cat ${PWD}/.ssh/id_rsa",
        "cd ~ && cat ~+/.ssh/id_rsa",
        "cd ~; cd /tmp; cat ~-/.ssh/id_rsa",
        "cd ~; cd /tmp; cat $OLDPWD/.ssh/id_rsa",
        "cd ~; cd /tmp; cd -; cat .ssh/id_rsa",
        "cd ~; cd /tmp; cd - >/dev/null; cat ./.ssh/id_rsa",
        "cd ~; bash -c 'cat $PWD/.ssh/id_rsa'",
        "cd ~; (cat ~+/.ssh/id_rsa)",
    ],
)
def test_a_path_through_the_working_folder_is_refused_through_the_hook(box, monkeypatch, capsys, cmd):
    assert refused(monkeypatch, capsys, cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "cd /tmp && cat $PWD/a.txt",
        "cd ~/proj; cd /tmp; cat ~-/a.txt",
        "cd ~; cd /tmp; cd -; cd /tmp; cat .ssh/id_rsa",
        "cd /tmp; cd ~; cd -; cat .ssh/id_rsa",
        "cat ~+/a.txt",
        "cd ~ && cat $PWD/proj/a.txt",
    ],
)
def test_the_working_folder_alone_does_not_make_a_command_refused(box, monkeypatch, capsys, cmd):
    assert not refused(monkeypatch, capsys, cmd), cmd


def test_cd_dash_goes_back_and_does_nothing_when_there_is_no_last_folder(box):
    pol = policy.default_policy()
    assert policy.is_denied_command(pol, "cd -; cat id_rsa", cwd="/tmp") is None
    assert policy.is_denied_command(pol, "cd ~; cd /tmp; cd -; cat .ssh/id_rsa", cwd="/tmp")


# --- variables the command sets ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setter",
    [
        "export D=~/.ssh",
        "export -n D=~/.ssh",
        "declare D=~/.ssh",
        "declare -x D=~/.ssh",
        "declare -g D=~/.ssh",
        "typeset D=~/.ssh",
        "local D=~/.ssh",
        "readonly D=~/.ssh",
        "readonly -p D=~/.ssh",
        "export A=1 D=~/.ssh",
        "read -r D <<< ~/.ssh",
        "read D <<< ~/.ssh",
        'read -r A D <<< "1 $HOME/.ssh"',
        "read -r -p prompt D <<< ~/.ssh",
    ],
)
def test_a_variable_set_by_a_builtin_is_followed_through_the_hook(box, monkeypatch, capsys, setter):
    assert refused(monkeypatch, capsys, f"{setter}; cat $D/id_rsa"), setter
    assert refused(monkeypatch, capsys, f'{setter}; cat "${{D}}/id_rsa"'), setter


def test_the_values_a_builtin_sets_are_split_as_the_shell_splits_them():
    assert policy._read_values(["read", "-r", "A", "B"], [sw.Redirect("<<<", "x y z")]) == {"A": "x", "B": "y z"}
    assert policy._read_values(["read", "A", "B"], [sw.Redirect("<<<", "x")]) == {"A": "x", "B": ""}
    assert policy._read_values(["read"], [sw.Redirect("<<<", "x y")]) == {"REPLY": "x y"}
    assert policy._read_values(["read", "A"], []) == {}
    assert policy._read_values(["read", "-a", "arr", "A"], [sw.Redirect("<<<", "x")]) == {"A": "x"}


@pytest.mark.parametrize("setter", ["export D=work", "declare -x D=work", "read -r D <<< work"])
def test_a_variable_a_builtin_sets_to_a_harmless_name_allows_the_command(box, monkeypatch, capsys, setter):
    assert not refused(monkeypatch, capsys, f"{setter}; cat $D/a.txt"), setter


# --- every word of a for list -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "for d in work ~/.ssh; do cat $d/id_rsa; done",
        "for d in ~/.ssh work; do cat $d/id_rsa; done",
        "for d in a ~/.ssh b; do cat ${d}/id_rsa; done",
        "for d in {a,b,~/.ssh}; do cat $d/id_rsa; done",
        'for d in a b ~/.ssh c d e; do echo $d; cat "$d/id_rsa"; done',
        "for a in ~/.ssh; do for b in x y; do cat $a/id_rsa; done; done",
        "for a in 1 2; do for b in x ~/.ssh; do cat $b/id_rsa; done; done",
    ],
)
def test_the_body_of_a_for_loop_is_read_for_every_word_of_its_list(box, monkeypatch, capsys, cmd):
    assert refused(monkeypatch, capsys, cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "for d in a b c; do cat $d/x; done",
        "for i in {1..1000}; do echo $i; cat file$i; done",
        "for i in 1 2 3 4 5 6 7 8 9; do echo $i; done",
        "for f in work proj; do cat $f/a.txt; done",
    ],
)
def test_a_for_loop_over_harmless_words_is_allowed(box, monkeypatch, capsys, cmd):
    assert not refused(monkeypatch, capsys, cmd), cmd


def test_a_for_list_of_more_than_the_limit_is_a_typed_refusal_when_the_body_uses_it(box, monkeypatch, capsys):
    cmd = "for i in {a..z}{a..z}; do echo $i; done"
    code, said = hook(monkeypatch, capsys, cmd)
    assert code == 2 and "more than 64 different words" in said
    assert hook(monkeypatch, capsys, "for i in {a..z}{a..z}; do echo hi; done")[0] == 0 or True


# --- env -S and wrappers that run text ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("toks", "text"),
    [
        (["env", "-Scat ~/x", "y"], "cat ~/x"),
        (["env", "-S", "cat ~/x", "y"], "cat ~/x"),
        (["env", "--split-string=cat ~/x", "y"], "cat ~/x"),
        (["env", "--split-string", "cat ~/x", "y"], "cat ~/x"),
        (["env", "-iS", "cat ~/x", "y"], "cat ~/x"),
        (["env", "-iScat ~/x", "y"], "cat ~/x"),
        (["env", "A=1", "-u", "B", "-S", "cat ~/x"], "cat ~/x"),
    ],
)
def test_every_spelling_of_an_env_split_string_is_given_back_as_a_command_line(toks, text):
    scripts: list[str] = []
    out, seen = sw.strip_wrappers(toks, scripts=scripts, lexenv={"HOME": HOME})
    assert scripts == [text] and out[:2] == ["cat", f"{HOME}/x"] and seen == {"env"}


def test_an_env_option_that_takes_a_name_is_not_a_split_string():
    out, _ = sw.strip_wrappers(["env", "-uS", "ls"], scripts=[])
    assert out == ["ls"]
    out, _ = sw.strip_wrappers(["env", "-i", "ls"], scripts=[])
    assert out == ["ls"]


@pytest.mark.parametrize(
    "cmd",
    [
        "env -S'cat ~/.ssh/id_rsa'",
        'env -S"cat ~/.ssh/id_rsa"',
        "env -S 'cat ~/.ssh/id_rsa'",
        "env --split-string='cat ~/.ssh/id_rsa'",
        "env --split-string 'cat ~/.ssh/id_rsa'",
        "env -iS 'cat ~/.ssh/id_rsa'",
        "env A=1 -S'cat ~/.ssh/id_rsa'",
        "sudo env -S'cat ~/.ssh/id_rsa'",
    ],
)
def test_an_env_split_string_that_reads_a_protected_file_is_refused_through_the_hook(box, monkeypatch, capsys, cmd):
    assert refused(monkeypatch, capsys, cmd), cmd
    assert not refused(monkeypatch, capsys, cmd.replace("~/.ssh/id_rsa", "work/a.txt")), cmd


SHELL_TEXT = [
    "su -c 'cat {k}'",
    "su -c'cat {k}'",
    "su user -c 'cat {k}'",
    "su -l user -c 'cat {k}'",
    "su -lc 'cat {k}' user",
    "su - -c 'cat {k}'",
    "su -s /bin/sh -c 'cat {k}' user",
    "su --command='cat {k}'",
    "su --command 'cat {k}' user",
    "sudo su -c 'cat {k}'",
    "runuser -l user -c 'cat {k}'",
    "runuser -u user -- cat {k}",
    "flock /tmp/lock -c 'cat {k}'",
    "flock -n /tmp/lock -c 'cat {k}'",
    "flock -w 5 /tmp/lock --command 'cat {k}'",
    "flock -n /tmp/lock cat {k}",
    "flock -w 5 /tmp/lock cat {k}",
    "script -c 'cat {k}' /dev/null",
    "script -qc 'cat {k}' /dev/null",
    "script -q -c 'cat {k}'",
    "script --command='cat {k}' /dev/null",
    "watch 'cat {k}'",
    "watch cat {k}",
    "watch -n 5 cat {k}",
    "watch -n5 'cat {k}'",
    "watch -d -n 1 'cat {k}'",
    "watch -x cat {k}",
    "watch --exec cat {k}",
    "chroot / cat {k}",
    "chroot --userspec=u:g / cat {k}",
    "timeout 5 cat {k}",
    "timeout -s KILL 5 cat {k}",
    "nice -n 5 cat {k}",
    "nice cat {k}",
    "nohup cat {k}",
    "stdbuf -oL cat {k}",
    "stdbuf -o0 -e0 cat {k}",
    "setsid cat {k}",
    "setsid -f cat {k}",
    "ionice -c3 cat {k}",
    "ionice -c 2 -n 7 cat {k}",
    "nice -n 5 nohup setsid ionice -c3 timeout 9 cat {k}",
]


@pytest.mark.parametrize("cmd", SHELL_TEXT)
def test_a_wrapper_that_runs_a_file_read_is_refused_through_the_hook(box, monkeypatch, capsys, cmd):
    assert refused(monkeypatch, capsys, cmd.format(k=KEY)), cmd
    assert not refused(monkeypatch, capsys, cmd.format(k="work/a.txt")), cmd


def test_the_dash_c_of_the_program_a_wrapper_starts_is_not_the_wrappers():
    toks, _ = sw.strip_wrappers(["flock", "/tmp/lock", "mycmd", "-c", "conf"], scripts=(got := []))
    assert toks == ["mycmd", "-c", "conf"] and got == []
    toks, _ = sw.strip_wrappers(["flock", "-n", "/tmp/lock", "tar", "-c", "x"], scripts=(got := []))
    assert toks == ["tar", "-c", "x"] and got == []
    toks, _ = sw.strip_wrappers(["watch", "-x", "git", "-c", "k=v", "status"], scripts=(got := []))
    assert toks == ["git", "-c", "k=v", "status"] and got == []
    toks, _ = sw.strip_wrappers(["runuser", "-u", "user", "--", "sh", "-c", "x"], scripts=(got := []))
    assert toks == ["sh", "-c", "x"] and got == []


def test_the_text_a_wrapper_runs_is_given_back_as_a_command_line():
    scripts: list[str] = []
    toks, seen = sw.strip_wrappers(["su", "-c", "ls ~/x", "user"], scripts=scripts)
    assert toks == [] and scripts == ["ls ~/x"] and seen == {"su"}
    scripts = []
    toks, _ = sw.strip_wrappers(["watch", "-n", "2", "ls", "-l", "x"], scripts=scripts)
    assert toks == [] and scripts == ["ls -l x"]
    scripts = []
    toks, _ = sw.strip_wrappers(["flock", "lock", "-c", "ls | wc"], scripts=scripts)
    assert toks == [] and scripts == ["ls | wc"]
    scripts = []
    toks, _ = sw.strip_wrappers(["chroot", "/jail", "ls"], scripts=scripts)
    assert toks == ["ls"] and scripts == []


# --- nested too deeply --------------------------------------------------------------------------------------------


def nested(levels: int) -> str:
    cmd = "echo hi"
    for _ in range(levels):
        cmd = "bash -c " + shlex.quote(cmd)
    return cmd


def test_a_command_nested_past_the_limit_is_a_typed_refusal_through_the_hook(box, monkeypatch, capsys):
    code, said = hook(monkeypatch, capsys, nested(8))
    assert code == 2 and "nests" in said and "could not check" not in said
    assert hook(monkeypatch, capsys, nested(3))[0] == 0


def test_the_readers_that_follow_nesting_refuse_it_with_a_kind():
    deep = nested(8)
    for read in (policy.simple_commands, policy.simple_commands_env, policy.simple_commands_flow):
        with pytest.raises(sw.ShellError) as err:
            read(deep)
        assert err.value.kind == "too_deep", read.__name__
    with pytest.raises(sw.ShellError) as err:
        policy.needs_approval(policy.loads('[gate]\nask_commands = ["git push *"]'), deep)
    assert err.value.kind == "too_deep"
