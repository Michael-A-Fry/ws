"""Here-document bodies and the gate: a body is left unread only where the shell really treats it as text.

Every case in BYPASSES was found by a review as a command the gate allowed and a real shell ran. Each must be
denied. KEPT is the other side: quoted bodies given to a data-only program, which stay allowed.
"""

from __future__ import annotations

import re

import pytest

from ws import policy

D = "rm" + " -rf /"


@pytest.fixture
def pol():
    return policy.default_policy()


# The shell closes `<<` on the line `E` and nothing else; `<<-` also drops leading tabs, only tabs. Any other line
# is body, so the command that follows is a command, not text.
CLOSING = {
    "space_indent_is_not_a_tab": f"cat <<-'E'\n  E\ncat <<'F'\nE\n{D}\nF\n",
    "trailing_space": f"cat <<-'E'\nE \ncat <<'F'\nE\n{D}\nF\n",
    "trailing_space_plain": f"cat <<'E'\nE \ncat <<'F'\nE\n{D}\nF\n",
    "tab_does_not_close_plain": f"cat <<'E'\n\tE\ncat <<'F'\nE\n{D}\nF\n",
    "ifs_in_the_line": f"cat <<-'E'\nE$IFS\ncat <<'F'\nE\n{D}\nF\n",
    "ifs_braced": f"cat <<-'E'\nE${{IFS}}\ncat <<'F'\nE\n{D}\nF\n",
    "carriage_return": f"cat <<'E'\r\n{D}\r\nE\r\n{D}\r\n",
    "full_width_closer": f"cat <<'E'\nＥ\ncat <<'F'\nE\n{D}\nF\n",
    "full_width_word": f"cat <<'Ｅ'\nE\ncat <<'F'\nＥ\n{D}\nF\n",
    "word_inside_body": f"cat <<'E'\ncat <<'F'\nE\n{D}\nF\n",
    "never_closed_still_scanned": f"cat <<'E'\n{D}\n",
}

# `cat` or what follows the body is something else, or the text goes on to a program that runs it.
REDIRECTED = {
    "hash_p": f"hash -p /bin/sh cat; cat <<'E'\n{D}\nE\n",
    "hash_p_newline": f"hash -p /bin/sh cat\ncat <<'E'\n{D}\nE\n",
    "path_export": f"export PATH=.:$PATH\ncat <<'E'\n{D}\nE\n",
    "path_symlink": f"ln -s /bin/sh cat; PATH=.:$PATH; cat <<'E'\n{D}\nE\n",
    "enable_n": f"enable -n echo; echo <<'E'\n{D}\nE\n",
    "builtin_word": f"builtin cat <<'E'\n{D}\nE\n",
    "command_p": f"command -p cat <<'E'\n{D}\nE\n",
    "function_keyword_tab": f"function\tcat {{ sh; }}\ncat <<'E'\n{D}\nE\n",
    "function_parens": f"cat() {{ sh; }}\ncat <<'E'\n{D}\nE\n",
    "alias": f"alias cat=sh; cat <<'E'\n{D}\nE\n",
    "eval_defines": f"eval 'cat() {{ sh; }}'; cat <<'E'\n{D}\nE\n",
    "relative_sink": f"./cat <<'E'\n{D}\nE\n",
    "absolute_sink": f"/tmp/cat <<'E'\n{D}\nE\n",
    "group_pipe": f"{{\ncat <<'E'\n{D}\nE\n}} | sh\n",
    "group_pipe_to_another_program": f"{{\ncat <<'E'\n{D}\nE\n}} | ssh somewhere\n",
    "subshell_pipe": f"(\ncat <<'E'\n{D}\nE\n) | bash\n",
    "loop_pipe": f"while true; do\ncat <<'E'\n{D}\nE\ndone | sh\n",
    "quote_runs_over_the_line": f"cat <<'E' \"x\n\" | sh\n{D}\nE\n",
    "fifo": f"mkfifo p; sh < p &\ncat <<'E' > p\n{D}\nE\n",
    "exec_descriptor": f"exec 3> >(sh)\ncat <<'E' >&3\n{D}\nE\n",
    "coprocess": f"coproc sh\ncat <<'E' >&${{COPROC[1]}}\n{D}\nE\n",
    "descriptor_to_another_program": f"cat <<'E' >&3\n{D}\nE\n",
    "process_substitution_across_a_line": f"tee \\\n>(sh) <<'E'\n{D}\nE\n",
    "command_after_on_the_same_line": f"cat <<'E'; {D}\nx\nE\n",
    "substitution_across_a_line": f"cat <<'E'; echo $(\ntrue\n)\n{D}\nE\n",
    "backtick_across_a_line": f"cat <<'E' `\ntrue\n`\n{D}\nE\n",
    "shell_first": f"sh <<'A'; cat <<'B'\n{D}\nA\nx\nB\n",
    "here_string_is_not_a_body": f"cat <<<x\n{D}\n",
}

# An unquoted body is expanded: `$(`, a backtick or `${ ` anywhere in it and all of it is read as commands.
EXPANDING = {
    "parameter_default": f"cat <<E\n${{x:-$({D})}}\nE\n",
    "arithmetic": f"cat <<E\n$((`{D}`))\nE\n",
    "brackets": f"cat <<E\n$[$({D})]\nE\n",
    "brace_backtick": f"cat <<E\n${{x:-`{D}`}}\nE\n",
    "paren_in_a_string": f'cat <<E\n$(echo ")" ; {D})\nE\n',
    "paren_in_quotes": f"cat <<E\n$(echo ')' ; {D})\nE\n",
    "case_pattern_paren": f"cat <<E\n$(case x in x) {D};; esac)\nE\n",
    "comment_with_paren": f"cat <<E\n$(echo # )\n{D}\n)\nE\n",
    "escaped_backtick_inside": f"cat <<E\n`echo \\`true\\`; {D}`\nE\n",
    "nested_heredoc_with_paren": f"cat <<E\n$(cat <<'X'\n)\nX\n{D})\nE\n",
    "double_backslash": f"cat <<E\n\\\\$({D})\nE\n",
    "function_substitution": f"cat <<E\n${{ {D}; }}\nE\n",
    "unclosed_substitution": f"cat <<E\n$({D}\nE\n",
    # the first body expands, so the text after it is not trusted to close where the shell closes it
    "a_second_heredoc_starts_inside_the_body": f"cat <<E\n$(true)\ncat <<'F'\nE\n{D}\nF\n",
    "a_shell_body_hides_a_second": f"ssh somewhere <<'A'\ncat <<'F'\nA\n{D}\nF\n",
}

BYPASSES = {**CLOSING, **REDIRECTED, **EXPANDING}


@pytest.mark.parametrize("cmd", list(BYPASSES.values()), ids=list(BYPASSES))
def test_a_body_the_shell_does_not_treat_as_text_is_read_as_commands(pol, cmd):
    assert policy.is_denied_command(pol, cmd), cmd


@pytest.mark.parametrize(
    "cmd",
    [
        "echo `echo \\`true\\`; " + D + "`",
        'echo "`echo \\`true\\`; ' + D + '`"',
        "echo `echo \\\\`; " + D,  # an escaped backslash does not escape the closing backtick
    ],
)
def test_an_escaped_backtick_inside_backticks_belongs_to_the_inner_command(pol, cmd):
    assert policy.is_denied_command(pol, cmd), cmd


KEPT = {
    "the original: a script written by a quoted body": "cat > run.sh <<'EOF'\nCMD=x\nbash -c \"$CMD\"\nEOF\n",
    "a quoted body of a dangerous-looking line": f"cat <<'E'\n{D}\nE\n",
    "a tab closes <<-": f"cat <<-'E'\n{D}\n\tE\n",
    "tabs close <<-": f"cat <<-'E'\n{D}\n\t\tE\n",
    "to a file": f"cat > notes.md <<'E'\n{D}\nE\n",
    "appending": f'cat >> notes.md <<"E"\n{D}\nE\n',
    "the redirect after the opener": f"cat <<'E' > notes.md\n{D}\nE\n",
    "tee": f"tee notes.md <<'E'\n{D}\nE\n",
    "tee with a flag": f"tee -a notes.md <<'E'\n{D}\nE\n",
    "hash in a word": "cat <<'E'\nthe hash-object command\nE\n",
    "two steps joined by &&": f"cat > a <<'A' && cat > b <<'B'\n{D}\nA\n{D}\nB\n",
    "two steps joined by a new line": f"cat > a <<'A'\n{D}\nA\ntee b <<'B'\n{D}\nB\n",
    "two steps joined by a semicolon": f"cat <<'A'; cat <<'B'\n{D}\nA\n{D}\nB\n",
    "no new line after the last closing line": f"cat <<'E'\n{D}\nE",
}


@pytest.mark.parametrize("cmd", list(KEPT.values()), ids=list(KEPT))
def test_a_quoted_body_given_to_a_data_program_is_still_left_unread(pol, cmd):
    assert policy.is_denied_command(pol, cmd) is None, cmd


# --- the allowlist: any token outside the one plain shape and every body is read as commands ------------------------
#
# Each opener is a step that is not exactly `cat` or `tee`, plain words, one `>` or `>>` to a plain path and one
# quoted opener, or a command around such steps. The body holds a dangerous line, so a body read as text would pass.

OUTSIDE = {
    # the sink word
    "other_data_program": "grep -c x <<'E'",
    "wc": "wc -l <<'E'",
    "colon": ": <<'E'",
    "head": "head -n 1 <<'E'",
    "tr": "tr a b <<'E'",
    "a_shell": "bash <<'E'",
    "an_interpreter": "python3 - <<'E'",
    "a_path_to_cat": "/bin/cat <<'E'",
    "a_variable_for_the_word": "$c <<'E'",
    "a_quoted_sink_word": "'cat' <<'E'",
    "an_assignment_before": "X=1 cat <<'E'",
    "a_wrapper": "time cat <<'E'",
    "sudo": "sudo tee f <<'E'",
    "nothing_before_the_opener": "<<'E'",
    # the arguments
    "variable_argument": "cat $x <<'E'",
    "braced_variable": "cat ${x} <<'E'",
    "substitution_argument": "cat $(true) <<'E'",
    "backtick_argument": "cat `true` <<'E'",
    "glob_argument": "cat * <<'E'",
    "question_glob": "cat ? <<'E'",
    "bracket_glob": "cat [a] <<'E'",
    "tilde_argument": "cat ~/f <<'E'",
    "brace_expansion": "cat {a,b} <<'E'",
    "quoted_argument": "cat 'a b' <<'E'",
    "double_quoted_argument": "cat \"a\" <<'E'",
    "escaped_argument": "cat a\\ b <<'E'",
    "history_bang": "cat !x <<'E'",
    "process_substitution_argument": "tee >(sh) <<'E'",
    "input_process_substitution": "cat <(true) <<'E'",
    # the redirections
    "input_redirect": "cat < f <<'E'",
    "read_write_redirect": "cat <> f <<'E'",
    "fd_number": "cat 2> f <<'E'",
    "fd_to_fd": "cat <<'E' 2>&1",
    "fd_duplicate": "cat <<'E' >&3",
    "stdout_duplicate": "cat <<'E' >&2",
    "ampersand_redirect": "cat &> f <<'E'",
    "named_fd": "cat {fd}> f <<'E'",
    "clobber": "cat >| f <<'E'",
    "two_redirects": "cat > a > b <<'E'",
    "two_redirects_around_the_opener": "cat > a <<'E' > b",
    "append_then_write": "cat >> a > b <<'E'",
    "redirect_to_a_variable": "cat > $f <<'E'",
    "redirect_to_a_tilde": "cat > ~/f <<'E'",
    "redirect_to_a_quoted_path": "cat > \"f g\" <<'E'",
    "redirect_to_a_substitution": "cat > $(true) <<'E'",
    "redirect_to_a_process_substitution": "cat > >(sh) <<'E'",
    "redirect_with_no_path": "cat > <<'E'",
    "redirect_to_a_second_opener": "cat <<'E' <<'E'",
    # the opener
    "unquoted": "cat <<E",
    "unquoted_dash": "cat <<-E",
    "backslash_word": "cat <<\\E",
    "here_string": "cat <<<x",
    "here_string_quoted": "cat <<<'E'",
    "space_before_the_word": "cat << 'E'",
    "a_name_with_a_space": "cat <<'a b'",
    "a_name_with_a_dash": "cat <<'E-1'",
    "a_name_with_a_dot": "cat <<'E.1'",
    "a_name_starting_with_a_digit": "cat <<'1E'",
    "an_empty_name": "cat <<''",
    "a_name_of_two_quoted_parts": "cat <<'E'F",
    "a_mixed_quote": "cat <<'E\"",
    "a_dollar_quoted_name": "cat <<$'E'",
    "a_name_that_goes_on": "cat <<'E'x",
    "two_openers_in_a_step": "cat <<'E' <<'F'",
    # the joins and what is around them
    "pipe_after": "cat <<'E' | cat",
    "pipe_before": "echo x | cat <<'E'",
    "or_after": "cat <<'E' || true",
    "background": "cat <<'E' &",
    "a_command_before": "echo x; cat <<'E'",
    "a_command_after_on_the_line": "cat <<'E' && echo done",
    "a_semicolon_at_the_end": "cat <<'E';",
    "a_semicolon_at_the_start": "; cat <<'E'",
    "two_semicolons": "cat <<'E';; cat <<'E'",
    "three_ampersands": "cat <<'E' &&& cat <<'E'",
    "subshell": "(cat <<'E')",
    "subshell_open": "( cat <<'E'",
    "brace_group": "{ cat <<'E'; }",
    "negation": "! cat <<'E'",
    "if": "if true; then cat <<'E'",
    "loop": "while true; do cat <<'E'",
    "function": "f() { cat <<'E'; }",
    "function_keyword": "function f { cat <<'E'; }",
    "assignment_line_before": "x=1\ncat <<'E'",
    "a_comment_on_the_opener_line": "cat <<'E' # note",
    "a_comment_hiding_the_opener": "cat # <<'E'",
    "a_line_continuation": "cat \\\n> f <<'E'",
    "a_tab_in_the_name": "cat <<'E\t'",
    "a_carriage_return_at_the_end": "cat <<'E'\r",
    "an_expansion_in_a_later_step": "cat <<'E' && cat $x <<'F'",
}


def _closing(opener: str) -> str:
    """Each closing line the opener's here-documents name, one per line."""
    return "\n".join(re.findall(r"<<-?['\"]?([A-Za-z_]\w*)", opener) or ["E"])


@pytest.mark.parametrize("opener", list(OUTSIDE.values()), ids=list(OUTSIDE))
def test_anything_outside_the_plain_shape_reads_every_body_as_commands(pol, opener):
    assert policy.is_denied_command(pol, f"{opener}\n{D}\n{_closing(opener)}\n"), opener
    # a good step first does not make a later one safe
    assert policy.is_denied_command(pol, f"cat <<'G'\nok\nG\n{opener}\n{D}\n{_closing(opener)}\n"), opener


def test_a_good_step_beside_a_bad_one_does_not_make_the_bad_body_text(pol):
    assert policy.is_denied_command(pol, "cat > a <<'A' && cat $x <<'B'\nok\nA\n" + D + "\nB\n")
    assert policy.is_denied_command(pol, "cat > a <<'A' | cat\nok\nA\n" + D + "\n")
    assert policy.is_denied_command(pol, "cat > a <<'A'\nok\nA\nbash <<'B'\n" + D + "\nB\n")


def test_the_text_the_gate_reads_has_only_the_command_lines_of_an_allowed_shape():
    ok = "cat > a <<'A' && cat >> b <<-'B'\nx\nA\n\ty\n\tB\ntee c <<\"C\"\nz\nC\n"
    assert policy._without_bodies(ok) == "cat > a <<'A' && cat >> b <<-'B'\ntee c <<\"C\"\n"
    assert policy._without_bodies("cat <<'E'\nx\nE") == "cat <<'E'\n"  # the closing line needs no new line after it
    assert policy._without_bodies("cat <<'E'\nx\n") is None  # never closed
    assert policy._without_bodies("") is None
    assert policy._without_bodies("\n\n") is None
    assert policy._without_bodies("cat <<'E'\nx\nE \n") is None  # a trailing space does not close it
    assert policy._without_bodies("cat <<'E'\nx\n\tE\n") is None  # a tab closes only <<-
    assert policy._without_bodies("cat <<'Ｅ'\nx\nＥ\n") is None  # full-width letters are not a plain name


# --- the files a data-only step writes go through the same path checks -----------------------------------------------


@pytest.mark.parametrize("sink", ["tee", "tee -a", "cat >", "cat >>"])
@pytest.mark.parametrize("body", ["hello", D])
def test_a_write_to_a_protected_file_is_denied_whatever_the_body(tmp_path, monkeypatch, sink, body):
    home, ws = tmp_path / "home", tmp_path / "ws"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(ws))
    pol = policy.loads("")
    protected = (str(home / ".bashrc"), str(ws / "policy.toml"))
    for target in (f"{home}/.bashrc", "~/.bashrc", "$HOME/.bashrc", f"{ws}/policy.toml", "$WS_HOME/policy.toml"):
        assert policy.is_denied_command(pol, f"{sink} {target} <<'E'\n{body}\nE\n", protect=protected), target


def test_a_tee_into_ws_home_is_denied_by_the_protect_rule_with_a_plain_path_or_a_variable(tmp_path, monkeypatch):
    ws = tmp_path / "wshome"
    monkeypatch.setenv("WS_HOME", str(ws))
    pol = policy.loads("")
    protected = tuple(policy.control_paths(ws))
    for cmd in (
        f"tee {ws}/policy.toml <<'E'\nx\nE\n",
        f"tee -a {ws}/policy.toml <<'E'\nx\nE\n",
        "tee $WS_HOME/policy.toml <<'E'\nx\nE\n",
        "tee ${WS_HOME}/policy.toml <<'E'\nx\nE\n",
        f"cat >> {ws}/policy.toml <<'E'\nx\nE\n",
        f"cat <<'E' | tee {ws}/policy.toml\nx\nE\n",
        f"cat <<'A' && tee {ws}/policy.toml <<'B'\nx\nA\ny\nB\n",
    ):
        assert policy.is_denied_command(pol, cmd, protect=protected), cmd
    assert policy.is_denied_command(pol, f"tee {ws}/notes.txt <<'E'\nx\nE\n", protect=protected) is None


def test_a_step_that_names_a_denied_read_path_is_denied(tmp_path, monkeypatch):
    ws = tmp_path / "wshome"
    monkeypatch.setenv("WS_HOME", str(ws))
    pol = policy.loads("")
    for cmd in (f"cat {ws}/secrets/k <<'E'\nx\nE\n", f"tee {ws}/secrets/k <<'E'\nx\nE\n"):
        assert "deny_read" in (policy.is_denied_command(pol, cmd) or ""), cmd


def test_a_command_after_a_body_the_shell_closes_is_still_read(pol):
    assert policy.is_denied_command(pol, f"cat <<-'E'\n\tE\n{D}\n")  # the tab line closes it; the next line runs
    assert policy.is_denied_command(pol, f"cat <<'E'\ntext\nE\n{D}\n")


def test_the_guards_read_a_body_the_shell_does_not_close_as_commands():
    add = ["git", "add", "-A"]
    for text in (
        "cat <<-'E'\n  E\ncat <<'F'\nE\ngit add -A\nF\n",
        "cat <<'E'\nＥ\ncat <<'F'\nE\ngit add -A\nF\n",
        "cat <<'E'\nE \ncat <<'F'\nE\ngit add -A\nF\n",
    ):
        assert add in policy.simple_commands(text), text
    assert add not in policy.simple_commands("cat <<-'E'\ngit add -A\n\tE\n")
    assert add not in policy.simple_commands("cat <<'E'\ngit add -A\nE\n")


# --- the toolkit's secrets folder, however a command names it ------------------------------------------------------


def test_the_secrets_folder_is_denied_by_variable_and_by_both_spellings_of_a_linked_ws_home(tmp_path, monkeypatch):
    real = tmp_path / "real-ws"
    (real / "secrets").mkdir(parents=True)
    link = tmp_path / "link-ws"
    link.symlink_to(real)
    monkeypatch.setenv("WS_HOME", str(link))
    rules = policy.secret_read_rules()
    assert rules == (f"{link}/secrets/**", f"{real}/secrets/**")
    p = policy.loads("")
    for cmd in (
        f"cat {link}/secrets/k",
        f"cat {real}/secrets/k",
        "cat $WS_HOME/secrets/k",
        "cat ${WS_HOME}/secrets/k",
        'cat "$WS_HOME/secrets/k"',
        "cat $WS_HOME/secrets/*",
        f"cat {link}/../link-ws/secrets/k",
        "cd $WS_HOME && cat secrets/k",
        "grep -r x $WS_HOME",
    ):
        assert "deny_read" in (policy.is_denied_command(p, cmd) or ""), cmd
    assert policy.is_denied_command(p, "cat $WS_HOME/policy.toml") is None


def test_a_policy_file_in_a_linked_ws_home_covers_both_spellings(tmp_path):
    real = tmp_path / "real-ws"
    real.mkdir()
    link = tmp_path / "link-ws"
    link.symlink_to(real)
    (real / "policy.toml").write_text("")
    p = policy.load(link / "policy.toml")
    assert policy.is_denied_read(p, str(link / "secrets" / "k"))
    assert policy.is_denied_read(p, str(real / "secrets" / "k"))


# --- tests start in their own HOME and WS_HOME ---------------------------------------------------------------------


def test_every_test_starts_with_a_home_and_ws_home_under_tmp_path(tmp_path):
    import os

    assert os.environ["HOME"].startswith(str(tmp_path))
    assert os.environ["WS_HOME"].startswith(str(tmp_path))
