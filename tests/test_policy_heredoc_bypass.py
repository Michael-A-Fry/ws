"""Here-document bodies and the gate: a body is left unread only where the shell really treats it as text.

Every case in BYPASSES was found by a review as a command the gate allowed and a real shell ran. Each must be
denied. KEPT is the other side: quoted bodies given to a data-only program, which stay allowed.
"""

from __future__ import annotations

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
    "after a semicolon": f"echo x; cat <<'B'\n{D}\nB\n",
    "after a pipe": f"echo x | cat <<'B'\n{D}\nB\n",
    "two on one line": f"cat <<'E' <<'E'\n{D}\nE\nx\nE\n",
    "a tab closes <<-": f"cat <<-'E'\n{D}\n\tE\n",
    "tabs close <<-": f"cat <<-'E'\n{D}\n\t\tE\n",
    "grep reading it": f"grep -f /dev/stdin <<'E'\n{D}\nE\n",
    "colon": f": <<'E'\n{D}\nE\n",
    "to a file": f"cat > notes.md <<'E' && echo done\n{D}\nE\n",
    "hash in a word": "cat <<'E'\nthe hash-object command\nE\n",
    "escaped substitution in an unquoted body": 'cat > f.sh <<EOF\nprice is \\$(not run) \\`no\\`\nbash -c "$CMD"\nEOF\n',
}


@pytest.mark.parametrize("cmd", list(KEPT.values()), ids=list(KEPT))
def test_a_quoted_body_given_to_a_data_program_is_still_left_unread(pol, cmd):
    assert policy.is_denied_command(pol, cmd) is None, cmd


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
