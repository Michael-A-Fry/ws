"""Every program on a stock machine reads its words as files, unless `policy._NO_FILE_CMDS` says it does not.

The reader's rules for a word with a part it cannot resolve (an unset variable, a command substitution, a default) once
applied to a LIST of file-reading commands, and a program the list did not name (dd, openssl, gpg, sqlite3 ...) passed
through. This test closes that class for good rather than one name at a time. It collects what is on this machine
(every executable in /usr/bin, /bin and /usr/local/bin, python3, perl, ruby, node, bash and sh when they are there,
`source` and `.`, and each git subcommand `git help -a` names), gives each two words, and asserts a refusal for every one
that is not on the exemption list:

    NAME ${NOPE:-$HOME/<a protected folder>/x}     a default that names a protected path
    NAME /tmp/$X                                   an unresolved part, which may hold `..`

The list of names is made when the test runs and the exemptions live in one place (`policy._NO_FILE_CMDS`), so a new
program, or a gap in the reader, fails here. A name that is not installed is not tested.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess

import pytest

from ws import paths, policy, shellwords

STOCK_DIRS = ("/usr/bin", "/bin", "/usr/local/bin")
ALSO = ("python3", "perl", "ruby", "node", "bash", "sh")
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]*\Z")
PROTECTED = ".ss" + "h"  # spelled in two parts so that the file is not itself a path word
EXEMPT = frozenset(policy._NO_FILE_CMDS)  # the one list


@pytest.fixture(autouse=True)
def pol_env(monkeypatch):
    monkeypatch.setenv("HOME", "/home/pat")
    monkeypatch.setenv("WS_HOME", "/home/pat/.ws")
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CDPATH", "TMPDIR"):
        monkeypatch.delenv(var, raising=False)


def executables() -> list[str]:
    """The names of the programs on this machine's stock PATH, and the extras when they are installed."""
    names: set[str] = set()
    for folder in STOCK_DIRS:
        try:
            entries = os.listdir(folder)
        except OSError:
            continue
        for entry in entries:
            found = os.path.join(folder, entry)
            if NAME.match(entry) and os.path.isfile(found) and os.access(found, os.X_OK):
                names.add(entry)
    names |= {n for n in ALSO if shutil.which(n, path=paths.tool_path())}
    names |= {"source", "."}
    return sorted(names)


def git_subcommands(tmp_path) -> list[str]:
    """The subcommands `git help -a` lists, or none when git is not installed."""
    git = shutil.which("git", path=paths.tool_path())
    if not git:
        return []
    env = {"PATH": paths.tool_path(), "HOME": str(tmp_path), "GIT_CONFIG_NOSYSTEM": "1", "LC_ALL": "C"}
    done = subprocess.run([git, "help", "-a"], capture_output=True, text=True, env=env, timeout=60, check=False)
    return sorted(
        {m.group(1) for line in done.stdout.splitlines() if (m := re.match(r"^ {3}([a-z][a-z0-9-]+)\s{2,}\S", line))}
    )


def gaps(commands: dict[str, list[str]]) -> list[str]:
    pol = policy.default_policy()
    missed = []
    for name, lines in commands.items():
        if name in EXEMPT:
            continue
        for text in lines:
            try:
                why = policy.is_denied_command(pol, text, cwd="/tmp")
            except Exception as e:  # noqa: BLE001 - a command the reader cannot judge is a refusal, which is what is wanted
                why = type(e).__name__
            if not why:
                missed.append(text)
    return missed


def lines_for(name: str, prefix: str = "") -> list[str]:
    """The two lines for one program, in the shape that gives its words to the program itself. A program that runs
    another (`timeout`, `env`, `nice`, `xargs`) is given `cat` to run, and its words go to that; one that takes its search
    pattern or script first (grep, sed, awk ...) is given one before them."""
    words = [f"${{NOPE:-$HOME/{PROTECTED}/x}}", "/tmp/$X"]
    head = f"{prefix}{name}"
    if not prefix and name in shellwords._SHELL_TEXT:
        return [f"{head} {'x ' if name == 'flock' else ''}-c 'cat {w}'" for w in words]
    if not prefix and name in shellwords._WRAPPERS:
        head += " x" * shellwords._POSITIONAL.get(name, 0) + " cat"
    elif not prefix and name in policy._PATTERN_FIRST:
        head += " x"
    return [f"{head} {w}" for w in words]


def test_the_machine_has_something_to_test():
    names = executables()
    assert "cat" in names and "ls" in names and len(names) > 20


def test_every_executable_refuses_a_default_naming_a_protected_path_and_a_part_that_may_climb():
    missed = gaps({name: lines_for(name) for name in executables()})
    assert missed == [], f"{len(missed)} programs read their words without the unknown-part rules:\n" + "\n".join(
        missed
    )


def test_every_git_subcommand_does_too(tmp_path):
    subs = git_subcommands(tmp_path)
    if not subs:
        pytest.skip("git is not installed here")
    missed = gaps({f"git {sub}": lines_for(sub, "git ") for sub in subs})
    assert missed == [], f"{len(missed)} git subcommands read their words without the rules:\n" + "\n".join(missed)
