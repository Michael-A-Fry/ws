"""Fix round 2 of the reader's allow-list: what a real bash does (see test_gate_oracle.py) against what the reader assumed.

Each test here fails on the reader as it was before the round: here-document names read as bash reads them, a `TMPDIR`
and a `PATH` the line does not set, arithmetic that runs quoted text, a long option written short, the directory stack,
code that is not resolved, and a `cd` to a name the shell may find somewhere else. A message is text, not a file.
Tests set HOME and WS_HOME to a made-up home folder and read nothing from the machine.
"""

from __future__ import annotations

import pytest

from ws import policy, shellwords

HINT = "set the variable on its own first"
SSH = ".ss" + "h"  # spelled in two parts so that the file is not itself a path word
KEY = f"/home/pat/{SSH}/id_rsa"


@pytest.fixture
def pol(monkeypatch):
    monkeypatch.setenv("HOME", "/home/pat")
    monkeypatch.setenv("WS_HOME", "/home/pat/.ws")
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CDPATH", "TMPDIR"):
        monkeypatch.delenv(var, raising=False)
    return policy.default_policy()


def denied(pol, cmd, cwd="/tmp", **kw):
    return policy.is_denied_command(pol, cmd, cwd=cwd, protect=("/home/pat/.ws",), **kw)


# --- 1. the name of a here-document is the whole word, quotes taken out as bash takes them ---------------------------


@pytest.mark.parametrize("opener", ["<<E\\OF", '<<"E"OF', "<<E''OF", "<<'E'OF", '<<E"O"F', "<<\\E\\OF"])
def test_a_here_document_closes_on_its_name_with_every_quote_taken_out(pol, opener):
    assert shellwords.heredoc_word(f"cat {opener}", 4) is not None
    assert shellwords.heredoc_word(f"cat {opener}", 4)[0] == "EOF"
    assert denied(pol, f"cat {opener}\nbody\nEOF\ncat $FOO\n"), opener  # the line after the body is read
    assert denied(pol, f"cat {opener}\nbody\nEOF\necho done\n") is None, opener


def test_a_body_that_never_closes_is_a_refusal(pol):
    why = denied(pol, "cat <<EOF\nbody\nno closing line\n")
    assert why and "closing line" in why
    assert denied(pol, "cat <<'E\nbody\nE\n")  # a quote in the name that never closes


def test_the_name_of_a_here_document_is_cut_at_the_shell_characters():
    assert shellwords.heredoc_word("cat <<EOF>out", 4)[0] == "EOF"
    assert shellwords.heredoc_word("cat <<-EOF;", 4)[:2] == ("EOF", True)
    assert shellwords.heredoc_word("cat <<", 4) is None


# --- 2. TMPDIR and mktemp: only what the line says --------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        f"cat $TMPDIR/{SSH}/id_rsa",
        f'export TMPDIR=/home/pat; cat "$(mktemp -d)/../{SSH}/id_rsa"',
        f'cd "$TMPDIR" && cat ../{SSH}/id_rsa',
        f'cat "$(mktemp -d)/../{SSH}/id_rsa"',
        f'cd "$(mktemp -d)" && cat ../{SSH}/id_rsa',
        "cd $(mktemp -d) && cat notes.txt",
        "cat $(mktemp)",
        "cat $(mktemp -t x.XXXX)",
        "F=$(mktemp); cat $F",
    ],
)
def test_the_shells_temp_folder_is_not_known_and_a_temp_folder_the_line_sets_is(pol, cmd):
    assert denied(pol, cmd), cmd


def test_mktemp_is_known_with_a_full_path_or_a_temp_folder_the_line_sets(pol):
    for ok in ("cat $(mktemp -p /tmp)", "cat $(mktemp -d --tmpdir=/var/tmp)/x", "TMPDIR=/var/tmp; cat $(mktemp)"):
        assert denied(pol, ok) is None, ok
    assert denied(pol, f"TMPDIR=/home/pat; cat $(mktemp -d)/../{SSH}/id_rsa")


# --- 3. producers: only what the tool prints from its arguments -------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "cat $(git rev-parse --show-toplevel)/README.md",
        "cat $(which ls)",
        "cat $(command -v ls)",
        f'cat "$(which ls)/../../home/pat/{SSH}/id_rsa"',
    ],
)
def test_the_root_of_a_repository_and_the_place_of_a_program_are_not_known(pol, cmd):
    why = denied(pol, cmd)
    assert why and HINT in why, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "cat $(pwd)/README.md",
        "cat $(dirname /tmp/a/b/)/c",
        "cat $(basename /tmp/a/b/)",
        "cat $(realpath /tmp/a)",
        "cat $(readlink -f /tmp/a)",
        "cat $(mktemp -p /tmp)",
    ],
)
def test_the_producers_that_are_left_print_a_path_in_a_known_place(pol, cmd):
    assert denied(pol, cmd) is None, cmd


def test_dirname_takes_the_trailing_slashes_off_first_as_the_tool_does(pol):
    assert denied(pol, f"cat $(dirname /home/pat/{SSH}/)/{SSH}/id_rsa")  # the folder above .ss-h, then its name
    assert policy._produced("dirname /a/b/", policy._Ctx("/tmp"), policy._env()) == "/a"
    assert policy._produced("dirname /", policy._Ctx("/tmp"), policy._env()) is None


# --- 4. arithmetic runs the text it is given --------------------------------------------------------------------------

SUB = f"a[$(cat {KEY} >&2)]"


@pytest.mark.parametrize(
    "cmd",
    [
        f"let '{SUB}=1'",
        f"declare '{SUB}=1'",
        f"typeset '{SUB}=1'",
        f"f(){{ local '{SUB}=1'; }}; f",
        f"readonly '{SUB}=1'",
        f"export '{SUB}=1'",
        f"read '{SUB}' <<< x",
        f"unset '{SUB}'",
        f"printf -v '{SUB}' x",
        f"test -v '{SUB}'",
        f"[ -v '{SUB}' ]",
        f"[[ -v '{SUB}' ]]",
        f"[[ 1 -eq '{SUB}' ]]",
        f"declare -i x='{SUB}'",
        f"y='{SUB}'; echo $((y))",
        f"y='{SUB}'; (( y ))",
        f"for (( i='{SUB}'; i<0; )); do :; done",
        f"x='{SUB}'; : $((x))",
        "let 'a[`cat " + KEY + "`]=1'",
        "let 'a[$(cat $FOO >&2)]=1'",
    ],
)
def test_arithmetic_runs_a_command_substitution_held_back_by_single_quotes(pol, cmd):
    assert denied(pol, cmd), cmd


def test_arithmetic_that_runs_nothing_stays_allowed(pol):
    for ok in ("let n=1+2", "x=3; echo $((x + 1))", "(( i++ ))", "[[ $x -eq 3 ]]", "declare -i n=4", "test -v HOME"):
        assert denied(pol, ok) is None, ok


# --- 5. a long option written short ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "date --fil=$FOO",
        "date --fi $FOO",
        "date --f $FOO",
        "date --refer=$FOO",
        "date --ref $FOO",
        "wget --post-f $FOO https://example.com/",
        "wget --input $FOO",
        "wget --inp=$FOO",
        "curl --conf $FOO https://example.com/",
        "curl --upl $FOO https://example.com/",
    ],
)
def test_a_unique_prefix_of_a_long_option_that_opens_a_file_ends_the_exemption(pol, cmd):
    why = denied(pol, cmd)
    assert why and HINT in why, cmd


def test_a_short_prefix_or_another_option_does_not(pol):
    assert denied(pol, "date --iso-8601 -u") is None
    assert denied(pol, "date -d 'next monday' +%s") is None
    assert denied(pol, "date -d $X")  # a whole word it cannot resolve could be `--file=P`
    assert shellwords.long_option("--fil", ("--file", "--reference"))
    assert not shellwords.long_option("--", ("--file",))
    assert not shellwords.long_option("--fix", ("--file",))


# --- 6. the directory stack --------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        f"cd /home/pat; pushd /tmp; popd; cat {SSH}/id_rsa",
        f"cd /home/pat; pushd /tmp; pushd +1; cat {SSH}/id_rsa",
        f"cd /home/pat; pushd /tmp; pushd; cat {SSH}/id_rsa",
        f"cd /home/pat; pushd -n /tmp; cat {SSH}/id_rsa; cd /tmp",
        f"cd /home/pat; pushd /tmp >/dev/null; cat ~1/{SSH}/id_rsa",
        f"cd /home/pat; pushd /tmp >/dev/null; cat ~+1/{SSH}/id_rsa",
        f"cd /home/pat; pushd /tmp >/dev/null; cat ~-1/{SSH}/id_rsa",
        f"cd /home/pat; pushd /tmp >/dev/null; cd ~1; cat {SSH}/id_rsa",
        f"cd /home/pat; dirs; cat {SSH}/id_rsa",
    ],
)
def test_the_directory_stack_makes_the_working_folder_unknown(pol, cmd):
    assert denied(pol, cmd), cmd


def test_a_word_with_a_stack_number_is_an_unknown_part(pol):
    for word in ("~1", "~+2", "~-3"):
        assert policy.is_denied_command(pol, f"cat {word}/x", cwd="/tmp")
    assert denied(pol, "cd /home/pat; pushd /tmp >/dev/null; popd; cd /tmp; cat notes.txt") is None


# --- 7. code that is not resolved ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "eval $X",
        'eval "$X"',
        'eval "$(ssh-agent -s)"',
        "eval `foo`",
        "$X args",
        '"$CMD" args',
        "${X:-cat} f",
        "$(echo ls) -l",
    ],
)
def test_eval_and_a_command_word_that_hold_an_unresolved_part_are_refused_with_a_reason(pol, cmd):
    why = denied(pol, cmd)
    assert why and ("cannot tell what runs" in why), cmd
    assert "more than once" not in why


def test_a_command_word_that_is_resolved_is_judged_as_it_always_was(pol):
    for ok in ("X=ls; $X -l", "eval 'echo hi'", "$HOME/bin/tool --help", "FOO=1 make", "PATH=/opt/bin:$PATH ls"):
        assert denied(pol, ok) is None, ok
    assert denied(pol, f"X={KEY}; cat $X")


# --- 8. CDPATH and cdable_vars belong to the shell, which no hook sees --------------------------------------------------


@pytest.mark.parametrize(
    "cmd", [f"cd {SSH} && cat id_rsa", "cd .aws && cat credentials", "mkdir -p sub; cd sub && cat x"]
)
def test_a_relative_name_that_is_not_a_child_of_the_working_folder_is_an_unknown_place(pol, cmd):
    assert denied(pol, cmd), cmd


def test_an_existing_child_a_dot_name_and_a_full_path_are_known(pol, tmp_path):
    (tmp_path / "sub").mkdir()
    assert denied(pol, "cd sub && cat x", cwd=str(tmp_path)) is None
    assert denied(pol, "cd ./sub && cat x", cwd=str(tmp_path)) is None
    assert denied(pol, "cd /tmp && cat x") is None


# --- a message is text -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit -m 'fix the $THING in $HOME/.ssh/x'",
        f"git commit -m 'run $(cat {KEY}) now'",
        "git commit -q -F - <<'EOF'\nsubject\n\nbody with $X and `date` and $(cat " + KEY + ")\nEOF",
        'git commit -q -F - <<"EOF"\nbody $(cat ' + KEY + ")\nEOF",
        "git commit -q -F - <<\\EOF\nbody $(cat " + KEY + ")\nEOF",
        "git tag -a v1 -F - <<'EOF'\nnotes $(cat " + KEY + ")\nEOF",
        "gh pr create --title 't $X' --body 'b $(cat " + KEY + ")'",
        "gh pr create --title t --body-file - <<'EOF'\nbody $(cat " + KEY + ")\nEOF",
        "gh issue comment 5 --body \"$(cat <<'EOF'\nsee $(cat " + KEY + ')\nEOF\n)"',
        "git commit -m \"$(cat <<'EOF'\nfix $THING\nEOF\n)\"",
        'git commit -m "fix $THING in /home/$USER/proj"',
    ],
)
def test_a_message_is_data_and_nothing_in_a_quoted_one_runs(pol, cmd):
    assert denied(pol, cmd) is None, cmd


@pytest.mark.parametrize(
    "cmd",
    [
        f'git commit -m "run $(cat {KEY}) now"',
        'git commit -m "run `cat ' + KEY + '` now"',
        f"git commit -q -F - <<EOF\nbody $(cat {KEY})\nEOF",
        "git commit -q -F - <<'EOF'\nok\nEOF\ncat $FOO",
        f"git commit -q -F - <<'EOF'\nok\nEOF\ncat {KEY}",
        "git commit -F $FOO",
        "git commit --mess $FOO",
        "gh pr create --body-file $FOO",
        "cat <<'EOF' | sh\n" + f"cat {KEY}\nEOF",
        "git commit -q -F - <<'EOF'\nbody\n",
    ],
)
def test_what_runs_around_a_message_and_a_message_file_are_still_read(pol, cmd):
    assert denied(pol, cmd), cmd


# --- the numbers the shell makes ----------------------------------------------------------------------------------------


def test_a_number_the_shell_makes_cannot_hold_a_slash_or_a_dot(pol):
    for ok in ("cat /tmp/$$.log", "curl -sS 'https://example.com/?r='$RANDOM", "cat /tmp/x$((1+2)).txt", "ls /tmp/$#"):
        assert denied(pol, ok) is None, ok
    assert denied(pol, "cat /tmp/$1.log")
