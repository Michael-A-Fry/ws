"""Text given on standard input to any shell is code: the gate doesn't keep a closed list of shell names.

A here-document or here-string handed to a command whose name ends in `sh` (and `ksh93`) is refused, in any path
spelling, because that command runs the text. Quoted bodies given to a program that only keeps text stay allowed.
"""

from __future__ import annotations

import pytest

from ws import policy

SHELLS = (
    "sh", "bash", "zsh", "dash", "ksh", "ash", "fish", "csh", "tcsh", "rbash", "mksh", "lksh", "yash", "posh",
    "oksh", "loksh", "pdksh", "hush", "ksh93", "tclsh", "wish", "rc.sh", "/bin/oksh", "/usr/local/bin/hush",
)  # fmt: skip


@pytest.fixture
def pol(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / ".ws"))
    return policy.default_policy()


@pytest.mark.parametrize("shell", SHELLS)
def test_a_here_document_or_string_given_to_a_shell_is_refused(pol, shell):
    for line in (f"{shell} <<'EOF'\necho hi\nEOF", f"{shell} <<< 'echo hi'", f"{shell} <<EOF\necho hi\nEOF"):
        assert policy.is_denied_command(pol, line, cwd="/tmp"), line


@pytest.mark.parametrize(
    "line",
    [
        "cat <<'EOF'\nhello\nEOF",
        "cat <<< 'hello'",
        "git commit -F - <<'EOF'\nsubject\n\nbody\nEOF",
        "wc -l <<< 'a b'",
        "grep -c x <<< 'xyz'",
    ],
)
def test_text_given_to_a_program_that_keeps_it_stays_allowed(pol, line):
    assert policy.is_denied_command(pol, line, cwd="/tmp") is None
