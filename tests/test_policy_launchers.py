"""A command the reader has no entry for, given a shell, is refused: the shell it starts runs text no one reads.

`taskset 1 sh -c TEXT` and its kin start a shell, so the rule is the allowlist's and not a list of launchers: the first
word of a simple command is not one the reader knows, and a later word's file name (with a version or `-static` taken
off) is a shell name. A version in the shell's own name is the same shell (`bash5.2`, `zsh5`, `sh5`, `bash-static`).
Commands the reader knows keep their words as files and patterns, and the names of shells among them stay fine.
"""

from __future__ import annotations

import pytest

from ws import policy

SSH = ".ss" + "h"  # spelled in two parts so that this file is not itself a path word
UNREAD = (  # launchers the reader has no entry for
    "taskset 1",
    "fakeroot",
    "unshare -r",
    "ssh-agent",
    "chrt 1",
    "toybox",
    "/usr/bin/taskset 1",
    "some-new-launcher --level 3",
)
WRAPPERS = ("ionice", "setsid", "stdbuf -o0", "flock /tmp/l", "busybox")  # launchers it already reads through
LAUNCHERS = (*UNREAD, *WRAPPERS)
VERSIONED = ("sh5", "bash5.2", "bash-static", "zsh5", "mksh-static", "dash0.5", "/usr/bin/bash5.2", "ksh93")


@pytest.fixture
def pol(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / ".ws"))
    return policy.default_policy()


def denied(pol, line: str) -> str | None:
    return policy.is_denied_command(pol, line, cwd="/tmp")


@pytest.mark.parametrize("launcher", LAUNCHERS)
def test_a_launcher_given_a_shell_and_a_command_text_that_reads_a_protected_file_is_refused(pol, launcher):
    for shell in ("sh", "bash", "zsh5", "bash-static"):
        assert denied(pol, f"{launcher} {shell} -c 'cat ~/{SSH}/id_rsa'"), shell
        assert denied(pol, f"{launcher} {shell} <<< 'echo hi'"), shell
        assert denied(pol, f"echo hi | {launcher} {shell}"), shell


@pytest.mark.parametrize("launcher", UNREAD)
def test_a_launcher_the_reader_has_no_entry_for_is_refused_whatever_the_shell_is_given(pol, launcher):
    for shell in ("sh", "bash", "zsh5", "bash-static", "/bin/dash", "'sh'"):
        assert denied(pol, f"{launcher} {shell} -c 'echo hi'"), shell
        assert denied(pol, f"{launcher} {shell} script.sh"), shell
        assert denied(pol, f"{launcher} -- {shell}"), shell


@pytest.mark.parametrize("shell", VERSIONED)
def test_a_shell_with_a_version_or_build_in_its_name_reads_standard_input_as_code(pol, shell):
    for line in (f"{shell} <<< 'echo hi'", f"{shell} <<'EOF'\necho hi\nEOF", f"{shell} <<EOF\necho hi\nEOF"):
        assert denied(pol, line), line
    assert denied(pol, f"{shell} -c 'cat ~/{SSH}/id_rsa'"), "the -c text is read as a script of its own"


@pytest.mark.parametrize("shell", VERSIONED)
def test_a_launcher_given_a_versioned_shell_is_refused(pol, shell):
    assert denied(pol, f"taskset 1 {shell} -c 'echo hi'")
    assert denied(pol, f"busybox {shell} -c 'cat ~/{SSH}/id_rsa'")


def test_the_message_names_the_launcher_and_the_shell(pol):
    reason = denied(pol, "taskset 1 bash5.2 -c 'echo hi'")
    assert reason and "`taskset`" in reason and "`bash5.2`" in reason


@pytest.mark.parametrize(
    "line",
    [
        "grep bash file",
        "grep -r sh src",
        "git log -- foo.sh",
        "git log --oneline -- scripts/bash",
        "ls /bin/sh",
        "ls -l /usr/bin/bash5.2",
        "which python3",
        "which bash",
        "man bash",
        "cat /bin/sh",
        "file /bin/bash",
        "echo bash",
        "echo sh -c hi",
        "printf '%s\\n' zsh",
        "shellcheck -s bash build.sh",
        "stat /bin/sh",
        "wc -c /bin/dash",
        "head -c 4 /bin/sh",
        "ln -s /bin/bash /tmp/mybash",
        "cp /bin/sh /tmp/copy-of-sh",
        "type bash",
        "bash build.sh",
        "bash -c 'echo hi'",
        "sh ./script.sh",
        "sudo -u build bash -c 'echo hi'",
        "nice -n 5 sh -c 'echo hi'",
        "timeout 5 bash -c 'echo hi'",
        "env FOO=1 sh -c 'echo hi'",
        "taskset 1 make all",
        "chrt 1 ./run-tests.sh",
        "docker push registry/app:1",
        "npm publish",
        "git push origin main",
        "gh pr merge 5 --squash",
        "python3 tool.py bash",
    ],
)
def test_calls_that_only_name_a_shell_or_start_none_stay_allowed(pol, line):
    assert denied(pol, line) is None, line


def test_a_here_document_body_that_mentions_a_launcher_and_a_shell_is_text(pol):
    line = "cat > notes.txt <<'EOF'\nrun taskset 1 sh -c hi to pin it\nEOF"
    assert denied(pol, line) is None
    line = "git commit -q -F - <<'EOF'\nfakeroot sh is why the build was slow\nEOF"
    assert denied(pol, line) is None


def test_a_launcher_inside_a_command_substitution_or_a_pipeline_is_still_refused(pol):
    for line in (
        "echo $(taskset 1 sh -c 'echo hi')",
        "ls && taskset 1 sh -c 'echo hi'",
        "cd /tmp; fakeroot bash -c 'echo hi'",
        "true || chrt 1 bash -c 'echo hi'",
        "x=$(unshare -r sh -c 'echo hi')",
    ):
        assert denied(pol, line), line
