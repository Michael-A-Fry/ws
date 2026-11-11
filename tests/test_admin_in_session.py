"""Admin commands refuse to run inside an agent session, in ws itself and not only in the gate's text matcher.

Each spelling of an admin command is refused twice: by the matcher (ws.policy), which reads the shell line, and by `ws`
(ws.agentsession), which is handed the same command already parsed and checks where it runs. The process tree is
planted; nothing here reads the real /proc, the real terminal or the real HOME.
"""

import argparse
import json
import os
import posixpath
import shlex
import subprocess
import sys

import pytest

from ws import agentsession, approvals, cli, output, policy
from ws.output import WsError

READ_ANCESTRY = agentsession.read_ancestry  # conftest swaps the module's for an empty tree
UNDER_CLAUDE = ["bash", "claude", "zsh", "tmux"]
UNDER_NOTHING = ["bash", "tmux", "systemd"]

# (what a shell line says, what `ws` itself receives). {tmp} is a folder the test makes.
SPELLINGS = [
    ("ws secret list", ["secret", "list"]),
    ("ws -C . secret list", ["-C", ".", "secret", "list"]),
    ("ws -C{tmp} secret list", ["-C{tmp}", "secret", "list"]),
    ("ws --cwd . secret list", ["--cwd", ".", "secret", "list"]),
    ("ws --cw . secret list", ["--cw", ".", "secret", "list"]),
    ("ws secret -C . list", ["secret", "-C", ".", "list"]),
    ("uv run --with pkg ws secret list", ["secret", "list"]),
    ("uv run --no-project --with a --with b ws secret list", ["secret", "list"]),
    ("uvx --from x ws secret list", ["secret", "list"]),
    ("uvx --from x --python 3.12 ws secret list", ["secret", "list"]),
    ("uvx ws secret list", ["secret", "list"]),
    ("uv tool run ws secret list", ["secret", "list"]),
    ("/abs/path/bin/ws secret list", ["secret", "list"]),
    ("./bin/ws secret list", ["secret", "list"]),
    ("env -u X ws secret list", ["secret", "list"]),
    ("env A=1 B=2 ws secret list", ["secret", "list"]),
    ("env -u X A=1 /opt/ws/bin/ws secret list", ["secret", "list"]),
    ("python -m ws secret list", ["secret", "list"]),
    ("python3.12 -m ws secret list", ["secret", "list"]),
    ("python -I -m ws -C . secret list", ["-C", ".", "secret", "list"]),
    ("uv run --project /x ws secret list", ["secret", "list"]),
    ("uvx --from pkg ws secret list", ["secret", "list"]),
    ("taskset 1 ws secret list", ["secret", "list"]),
]

# spellings where the words after `ws` are not on the line as plain words, for the matcher alone
MATCHER_ONLY = [
    "python3 -mws secret list",
    "python -Imws secret list",
    "python3 -m ws.__main__ secret list",
    "echo secret list | xargs ws",
    "xargs -I X ws X",
    "echo secret list | xargs -I cmd ws cmd",
    "echo generate | xargs ws gate",
    "echo x | xargs ws status",
    "tmux new -d -s x ws secret list",
    "tmux respawnp 'ws secret list'",
    "tmux new-ses -d 'A=1 nohup ws secret list'",
    "tmux new -d \"bash -c 'ws secret list'\"",
    "tmux new -d 'python3 -mws secret list'",
    "screen -dmS x ws secret list",
    "python3 -X utf8 -mws secret list",
    "python -W ignore -mws secret list",
    "tmux new -d 'nohup ws secret list'",
    "tmux new -d 'nohup python3 -mws secret list'",
    "tmux -c 'ws secret list'",
    "tmux new -d -n -t ws secret list",
    "tmux new -d 'ws se\"cr\"et list'",
    "tmux new -d 'w\"\"s secret list'",
    "tmux rename-window x ';' new-window 'ws secret list'",
    "tmux display x ';' run-shell 'ws secret list'",
    "tmux set-option -g default-command 'ws secret list'",
    "tmux set-hook -g session-created 'run-shell \"ws secret list\"'",
    "tmux display -p '#(ws secret list)'",
    "tmux -L rename new -d 'ws secret list'",
    "tmux new -d 'ws \"secret\" list'",
    "tmux new -d '\"ws\" secret list'",
    "tmux new -d python3 -mws secret list",
    "tmux rename-window 'ws secret'",
    "tmux send-keys -t x 'the ws secret is rotated' Enter",
    "tmux new -d 'ws secret list'",
    "tmux new-session -d 'cd /x; env -u A ws secret list'",
    "tmux split-window 'ws approve --list'",
    "tmux send-keys 'ws secret list' Enter",
    "script -qc 'ws secret list' /dev/null",
    "env -u A script -qc 'ws secret list' /dev/null",
]
MATCHER_ALLOWS = [
    "ws status",
    "tmux new -d 'ws status'",
    "tmux list-panes",
    "script -qc 'ws status' /dev/null",
    "tmux send-keys -t ws approve Enter",
    "tmux send-keys -t x su Enter",
    "tmux send-keys -t x 'script is done' Enter",
    "ls | xargs grep -l ws",
    "ls | xargs -I{} cp {} ~/code/ws",
    "python3 tool.py -mws secret list",
    "python3 -mws status",
    "grep -mws secret notes",
]

# every admin command once, as the argv `ws` receives ({tmp} is a folder the test makes)
ADMIN_ARGV = {
    "secret": ["secret", "list"],
    "broker": ["broker", "status", "--socket", "{tmp}/none.sock"],
    "approve": ["approve", "--list"],
    "uninstall": ["uninstall", "--dry-run"],
    "gate init": ["gate", "init"],
    "gate generate": ["gate", "generate", "--runner", "claude-code", "--out", "{tmp}/gate"],
    "skills adopt": ["skills", "adopt", "align"],
    "skills restore": ["skills", "restore", "align"],
    "config fallback": ["config", "fallback", "--clear"],
    "session prepare": ["session", "prepare", "--role", "worker"],
    "install --gate": ["install", "--gate", "launch", "--dry-run", "--notes", "{tmp}/Notes"],
    "node join": ["node", "join"],
    "node remove": ["node", "remove"],
    "queue add": ["queue", "add", "job"],
    "queue cancel": ["queue", "cancel", "run1"],
    "queue run": ["queue", "run"],
}
NOT_ADMIN_ARGV = [
    ["status"],
    ["-C", ".", "status"],
    ["session", "nudge", "--session-id", "x"],
    ["install", "--dry-run", "--notes", "{tmp}/Notes"],
    ["install", "--gate", "every-session", "--dry-run", "--notes", "{tmp}/Notes"],
    ["config"],
    ["gate", "check", "--runner", "claude-code"],
    ["help", "secret"],
    ["node", "show"],
    ["queue", "list"],
    ["queue", "show", "run1"],
]


def parsed(argv, tmp):
    _, rest = output.split_global([a.format(tmp=tmp) for a in argv])
    return cli.build_parser().parse_args(rest)


def asks(ns, **kw):
    """What `guard` does for `ns` with no terminal and no marker, under a runner unless the test says otherwise."""
    kw.setdefault("env", {})
    kw.setdefault("stdin_tty", False)
    kw.setdefault("stdout_tty", False)
    kw.setdefault("ancestry", lambda: UNDER_CLAUDE)
    agentsession.guard(ns, **kw)


def refused(ns, **kw) -> WsError | None:
    try:
        asks(ns, **kw)
    except WsError as e:
        return e
    return None


def ws_argv(line: str) -> list[str]:
    """The words after `ws` in a shell line (the first word whose name is `ws`)."""
    words = shlex.split(line)
    return words[next(i for i, w in enumerate(words) if posixpath.basename(w) == "ws") + 1 :]


# --- the spellings: the matcher and ws itself -------------------------------------------------------------------------


@pytest.mark.parametrize("line, argv", SPELLINGS, ids=[s[0] for s in SPELLINGS])
def test_every_spelling_is_refused_by_the_matcher_and_by_ws_itself(line, argv, tmp_path, monkeypatch, capsys):
    tmp = str(tmp_path)
    assert policy.is_denied_command(policy.default_policy(), line.format(tmp=tmp)), "the matcher reads the line"
    assert ws_argv(line.format(tmp=tmp)) == [a.format(tmp=tmp) for a in argv], "the test's argv is the line's"
    monkeypatch.setattr(agentsession, "read_ancestry", lambda: UNDER_CLAUDE)
    monkeypatch.chdir(tmp_path)
    code = cli.main([a.format(tmp=tmp) for a in argv])
    out = capsys.readouterr()
    assert code == 3, out
    assert "own terminal" in out.err


@pytest.mark.parametrize("line", MATCHER_ONLY)
def test_the_matcher_reads_launchers_module_forms_and_the_text_tmux_runs(line):
    assert policy.is_denied_command(policy.default_policy(), line), line


@pytest.mark.parametrize("line", MATCHER_ALLOWS)
def test_the_matcher_leaves_commands_that_are_not_admin(line):
    assert not policy.is_denied_command(policy.default_policy(), line), line


def test_the_refusal_is_one_typed_error_in_the_json_envelope(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agentsession, "read_ancestry", lambda: UNDER_CLAUDE)
    monkeypatch.chdir(tmp_path)
    assert cli.main(["--json", "secret", "list"]) == 3
    env = json.loads(capsys.readouterr().out)
    assert (env["error_kind"], env["exit_code"], env["status"]) == ("refused", 3, "error")
    assert "own terminal" in env["hint"] and "secret" in env["message"]


@pytest.mark.parametrize("name", sorted(ADMIN_ARGV))
def test_each_admin_command_is_refused_by_ws_itself(name, tmp_path):
    ns = parsed(ADMIN_ARGV[name], tmp_path)
    err = refused(ns)
    assert err is not None and (err.kind, err.exit_code) == ("refused", 3)
    assert err.message.startswith(f"ws {name}:") and err.hint == "run it in your own terminal"


@pytest.mark.parametrize("name", sorted(ADMIN_ARGV))
def test_each_admin_command_is_refused_by_the_matcher_too(name):
    line = "ws " + ("install --gate launch" if name == "install --gate" else name) + " x"
    assert policy.is_denied_command(policy.default_policy(), line), line


def test_the_matcher_and_ws_read_one_table():
    pol = policy.default_policy()
    for word in sorted(agentsession.ADMIN):
        assert policy.is_denied_command(pol, f"ws {word} x"), word
    for first, second in sorted(agentsession.ADMIN_SUB):
        assert policy.is_denied_command(pol, f"ws {first} {second} x"), (first, second)


def test_the_table_names_commands_that_exist():
    """A rename must not drop an admin command out of the check without a test turning red."""
    sub = cli._subcommands(cli.build_parser())
    for word in agentsession.ADMIN:
        assert word in sub, word
    for first, second in agentsession.ADMIN_SUB:
        parser = sub[first]
        kids = next((a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)), {})
        topics = next((a.choices for a in parser._actions if a.dest == "topic"), None) or ()
        assert second in kids or second in topics, (first, second)


def test_every_command_the_parser_has_that_the_table_calls_admin_is_refused():
    sub = cli._subcommands(cli.build_parser())
    leaves = []
    for name, parser in sub.items():
        kids = next((a.choices for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
        leaves += [f"{name}.{k}" for k in kids] if kids else [name]
    admin = [c for c in leaves if agentsession.admin_name(c, argparse.Namespace()) is not None]
    assert {"secret.set", "broker.serve", "approve", "uninstall", "gate.generate", "gate.init"} <= set(admin)
    for command in admin:
        if tuple(command.split(".")[:2]) in agentsession.NOT_GUARDED:
            continue
        assert refused(argparse.Namespace(ws_command=command)) is not None, command


# --- where it runs ----------------------------------------------------------------------------------------------------

A_SECRET = argparse.Namespace(ws_command="secret.list")


def test_the_marker_refuses_even_with_a_terminal():
    err = refused(A_SECRET, env={agentsession.ENV_MARKER: "1"}, stdin_tty=True, stdout_tty=True, ancestry=list)
    assert err is not None and agentsession.ENV_MARKER in err.message


def test_a_runner_above_it_and_no_terminal_is_refused():
    assert refused(A_SECRET) is not None


@pytest.mark.parametrize("stdin_tty, stdout_tty", [(True, False), (False, True), (False, False)])
def test_both_ends_must_be_a_terminal_to_be_a_person(stdin_tty, stdout_tty):
    assert refused(A_SECRET, stdin_tty=stdin_tty, stdout_tty=stdout_tty) is not None


def test_the_foreground_of_the_runners_own_terminal_is_a_person():
    assert refused(A_SECRET, stdin_tty=True, stdout_tty=True, runner_terminal=lambda: True) is None


def test_one_end_a_terminal_is_refused_even_on_the_runners_terminal():
    assert refused(A_SECRET, stdin_tty=True, stdout_tty=False, runner_terminal=lambda: True) is not None
    assert refused(A_SECRET, stdin_tty=False, stdout_tty=True, runner_terminal=lambda: True) is not None


def test_a_tree_deeper_than_the_walk_counts_as_a_runner(tmp_path):
    for pid in range(100, 170):
        plant(tmp_path, pid, pid + 1, "sh", ["sh"])
    names = agentsession._proc_ancestry(100, str(tmp_path))
    assert names[-1] == agentsession._TOO_DEEP
    assert refused(A_SECRET, ancestry=lambda: names) is not None
    assert refused(A_SECRET, ancestry=lambda: names, stdin_tty=True, stdout_tty=True, runner_terminal=lambda: False)
    assert agentsession._proc_terminals(100, str(tmp_path)) is None


def test_a_terminal_that_is_not_the_runners_is_refused():
    """A pty the session made answers isatty on both ends; it is still not the terminal the runner holds."""
    err = refused(A_SECRET, stdin_tty=True, stdout_tty=True, runner_terminal=lambda: False)
    assert err is not None and err.exit_code == 3


def test_no_runner_and_no_marker_is_allowed_with_or_without_a_terminal():
    assert refused(A_SECRET, ancestry=lambda: UNDER_NOTHING) is None
    assert refused(A_SECRET, ancestry=lambda: UNDER_NOTHING, stdin_tty=True, stdout_tty=True) is None


def test_a_tree_that_cannot_be_read_is_a_session_unless_there_is_a_terminal():
    assert refused(A_SECRET, ancestry=lambda: None) is not None
    assert refused(A_SECRET, ancestry=lambda: None, stdin_tty=True, stdout_tty=True) is None


@pytest.mark.parametrize("runner", sorted(agentsession.RUNNERS))
def test_each_runner_is_recognised(runner):
    assert refused(A_SECRET, ancestry=lambda: ["bash", runner]) is not None


def test_the_tree_is_read_only_when_nothing_else_settles_it():
    def boom():
        raise AssertionError("the tree was read")

    assert refused(A_SECRET, env={agentsession.ENV_MARKER: "1"}, ancestry=boom) is not None
    assert refused(argparse.Namespace(ws_command="status"), ancestry=boom) is None


def test_native_windows_has_the_marker_and_nothing_else():
    def boom():
        raise AssertionError("the tree was read")

    assert refused(A_SECRET, ancestry=boom, windows=True) is None
    assert refused(A_SECRET, env={agentsession.ENV_MARKER: "1"}, ancestry=boom, windows=True) is not None


def test_ws_approve_reads_the_one_marker():
    assert approvals.ENV_MARKER == agentsession.ENV_MARKER


def test_there_is_no_environment_switch(monkeypatch):
    """Whatever the environment says, a session is refused: the only inputs are the marker, the tree and the terminals."""
    for name in ("WS_ALLOW_ADMIN", "WS_ADMIN", "WS_TESTING", "CI", "PYTEST_CURRENT_TEST"):
        monkeypatch.setenv(name, "1")
    assert refused(A_SECRET, env=dict(os.environ)) is not None


# --- commands that are not admin --------------------------------------------------------------------------------------


@pytest.mark.parametrize("argv", NOT_ADMIN_ARGV, ids=lambda a: " ".join(a))
def test_a_command_that_is_not_admin_is_never_refused(argv, tmp_path):
    ns = parsed(argv, tmp_path)
    assert refused(ns) is None
    assert refused(ns, env={agentsession.ENV_MARKER: "1"}) is None


def test_session_start_is_the_hooks_and_is_not_refused_in_a_session():
    ns = parsed(["session", "start", "--session-id", "x"], "")
    assert refused(ns, env={agentsession.ENV_MARKER: "1"}) is None
    assert policy.is_denied_command(policy.default_policy(), "ws session start --session-id x"), (
        "the matcher still does"
    )


def test_broker_serve_is_what_ws_run_starts_so_only_it_passes_among_the_broker_commands(tmp_path):
    serve = parsed(["broker", "serve", "--socket", "{tmp}/b.sock"], tmp_path)
    assert refused(serve) is None, "a runner above it and no terminal"
    assert refused(serve, env={agentsession.ENV_MARKER: "1"}) is None
    for rest in (["status"], ["request", "x", "--purpose", "p", "--host", "h"]):
        err = refused(parsed(["broker", *rest], tmp_path))
        assert err is not None and err.exit_code == 3, rest
    sub = cli._subcommands(cli.build_parser())["broker"]
    kids = next(a.choices for a in sub._actions if isinstance(a, argparse._SubParsersAction))
    for kid in kids:
        wanted = kid != "serve"
        assert (refused(argparse.Namespace(ws_command=f"broker.{kid}")) is not None) is wanted, kid


def test_the_matcher_still_denies_a_typed_broker_serve():
    pol = policy.default_policy()
    for line in (
        "ws broker serve",
        "uv run --with pkg ws broker serve --socket /x",
        "env A=1 /opt/bin/ws -C . broker serve",
    ):
        assert policy.is_denied_command(pol, line), line


def test_a_plain_command_runs_in_a_session_through_main(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(agentsession, "read_ancestry", lambda: UNDER_CLAUDE)
    monkeypatch.setenv(agentsession.ENV_MARKER, "1")
    monkeypatch.chdir(tmp_path)
    assert cli.main(["-C", ".", "version"]) == 0


# --- whose terminal it is ---------------------------------------------------------------------------------------------


def plant_tty(root, pid, ppid, comm, tty, argv=()):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} {tty} -1 4194560\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))


def test_the_terminals_of_this_process_and_of_the_runner_above_it(tmp_path):
    plant_tty(tmp_path, 40, 30, "ws", 34817, ["ws"])
    plant_tty(tmp_path, 30, 20, "script", 34816, ["script"])
    plant_tty(tmp_path, 20, 1, "claude", 34816, ["claude"])
    assert agentsession._proc_terminals(40, str(tmp_path)) == (34817, {34816})


def test_a_runner_with_no_terminal_holds_none(tmp_path):
    plant_tty(tmp_path, 40, 20, "ws", 34817, ["ws"])
    plant_tty(tmp_path, 20, 1, "claude", 0, ["claude"])
    assert agentsession._proc_terminals(40, str(tmp_path)) == (34817, {0})


def test_a_process_named_after_a_runner_on_a_sessions_pty_does_not_hide_the_real_one(tmp_path):
    plant_tty(tmp_path, 40, 30, "claude", 34827, ["claude"])
    plant_tty(tmp_path, 30, 20, "script", 34827, ["script"])
    plant_tty(tmp_path, 20, 1, "claude", 0, ["claude"])
    assert agentsession._proc_terminals(40, str(tmp_path)) == (34827, {34827, 0})


def test_the_ps_walk_reads_the_terminals(monkeypatch):
    rows = {"90": ("80", "bash", "bash", "ttys004"), "80": ("1", "/x/claude", "claude", "ttys001")}

    def fake(argv, **kw):
        ppid, comm, args, tty = rows[argv[-1]]
        text = {"ppid=": ppid, "comm=": comm, "args=": args, "tty=": tty}[argv[2]]
        return subprocess.CompletedProcess(argv, 0, stdout=f"{text}\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert agentsession._ps_terminals(90) == ("ttys004", {"ttys001"})


def test_a_link_that_cannot_be_read_is_no_terminals(tmp_path):
    plant_tty(tmp_path, 40, 39, "ws", 34817, ["ws"])
    assert agentsession._proc_terminals(40, str(tmp_path)) is None


def test_on_runner_terminal_says_no_in_the_background(monkeypatch):
    monkeypatch.setattr(os, "tcgetpgrp", lambda fd: 5)
    monkeypatch.setattr(os, "getpgrp", lambda: 6)
    assert agentsession.on_runner_terminal() is False


def test_on_runner_terminal_says_no_when_stdin_has_no_terminal(monkeypatch):
    def none(fd):
        raise OSError

    monkeypatch.setattr(os, "tcgetpgrp", none)
    assert agentsession.on_runner_terminal() is False


@pytest.mark.parametrize(
    "found, want",
    [((7, {7}), True), ((8, {7}), False), ((7, {7, 0}), False), ((7, {7, 8}), False), ((0, {0}), False), (None, False)],
)
def test_on_runner_terminal_compares_the_two(monkeypatch, found, want):
    monkeypatch.setattr(os, "tcgetpgrp", lambda fd: 5)
    monkeypatch.setattr(os, "getpgrp", lambda: 5)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(agentsession, "_proc_terminals", lambda pid: found)
    assert agentsession.on_runner_terminal() is want


# --- reading the process tree -----------------------------------------------------------------------------------------


def plant(root, pid, ppid, comm, argv=(), exe=None):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560\n")
    (d / "cmdline").write_bytes(b"".join(a.encode() + b"\0" for a in argv))
    if exe:
        os.symlink(exe, d / "exe")


def test_a_runner_started_as_a_script_is_found_by_the_script_name(tmp_path):
    plant(tmp_path, 40, 30, "bash", ["bash", "-c", "ws"])
    plant(tmp_path, 30, 20, "node", ["node", "--flag", "/usr/lib/node_modules/bin/claude", "--resume"])
    plant(tmp_path, 20, 1, "zsh", ["-zsh"])
    names = agentsession._proc_ancestry(40, str(tmp_path))
    assert "claude" in names and "node" in names and "zsh" in names


def test_a_runner_is_found_by_its_executable_and_by_argv0(tmp_path):
    plant(tmp_path, 12, 11, "sh", ["sh"])
    plant(tmp_path, 11, 10, "2.1.5", ["claude"], exe="/home/x/.local/share/claude/versions/2.1.5")
    plant(tmp_path, 10, 1, "init", ["/sbin/init"])
    assert "claude" in agentsession._proc_ancestry(12, str(tmp_path))
    plant(tmp_path, 22, 21, "main", ["x"], exe="/opt/opencode/bin/opencode")
    plant(tmp_path, 21, 1, "init", ["/sbin/init"])
    assert "opencode" in agentsession._proc_ancestry(22, str(tmp_path))


def test_command_text_is_not_a_runner(tmp_path):
    """`grep claude notes`, `vim claude.md` and `node server.js claude` are not a runner above the command."""
    plant(tmp_path, 33, 32, "grep", ["grep", "claude", "notes"])
    plant(tmp_path, 32, 31, "vim", ["vim", "claude"])
    plant(tmp_path, 31, 30, "node", ["node", "server.js", "claude"])
    plant(tmp_path, 30, 1, "my (claude) prog", ["/bin/sh", "-c", "claude --version"])
    names = agentsession._proc_ancestry(33, str(tmp_path))
    assert names is not None and not set(names) & agentsession.RUNNERS


def test_a_name_with_brackets_and_spaces_reads_whole(tmp_path):
    plant(tmp_path, 5, 1, "a) S 1 (b", ["x"])
    assert agentsession._proc_ancestry(5, str(tmp_path)) == ["a) s 1 (b", "x"]


def test_a_link_in_the_tree_that_cannot_be_read_is_none(tmp_path):
    plant(tmp_path, 50, 49, "bash", ["bash"])  # 49 is not there
    assert agentsession._proc_ancestry(50, str(tmp_path)) is None
    plant(tmp_path, 60, 59, "bash", ["bash"])
    (tmp_path / "59").mkdir()
    (tmp_path / "59" / "stat").write_text("garbage")
    assert agentsession._proc_ancestry(60, str(tmp_path)) is None


def test_a_loop_in_the_tree_ends(tmp_path):
    plant(tmp_path, 70, 71, "a", ["a"])
    plant(tmp_path, 71, 70, "b", ["b"])
    assert agentsession._proc_ancestry(70, str(tmp_path)) == ["a", "b"]


def test_the_ps_walk_reads_each_link(monkeypatch):
    table = {"90": ("80", "/usr/bin/bash", "bash -c x"), "80": ("1", "/Applications/Claude Code/claude", "claude")}

    def fake(argv, **kw):
        assert argv[0] == "/bin/ps", "ps by its full path, not through PATH"
        pid = argv[-1]
        ppid, comm, args = table[pid]
        text = f" {ppid} {comm}\n" if "ppid=" in argv else f"{args}\n"
        return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert "claude" in agentsession._ps_ancestry(90)


def test_the_ps_walk_says_when_the_tree_is_too_deep(monkeypatch):
    def fake(argv, **kw):
        text = f" {int(argv[-1]) + 1} sh\n" if "ppid=" in argv else "sh\n"
        return subprocess.CompletedProcess(argv, 0, stdout=text, stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert agentsession._ps_ancestry(100)[-1] == agentsession._TOO_DEEP


def test_a_ps_that_fails_is_none(monkeypatch):
    def fake(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake)
    assert agentsession._ps_ancestry(90) is None

    def missing(argv, **kw):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)
    assert agentsession._ps_ancestry(90) is None


def test_the_walk_used_depends_on_the_system(monkeypatch):
    monkeypatch.setattr(agentsession, "_proc_ancestry", lambda pid: ["linux"])
    monkeypatch.setattr(agentsession, "_ps_ancestry", lambda pid: ["mac"])
    monkeypatch.setattr(sys, "platform", "linux")
    assert READ_ANCESTRY() == ["linux"]
    monkeypatch.setattr(sys, "platform", "darwin")
    assert READ_ANCESTRY() == ["mac"]
    monkeypatch.setattr(sys, "platform", "win32")
    assert READ_ANCESTRY() is None


def test_upgrade_is_left_to_its_own_check_and_still_refused_by_the_gate():
    """`upgrade` makes its own check and then the same one as every admin command (tests/test_upgrade.py), so `guard`
    passes it on; the gate still refuses it on the command line."""
    assert ("upgrade",) in agentsession.NOT_GUARDED and "upgrade" in agentsession.ADMIN
    assert policy.is_denied_command(policy.default_policy(), "ws upgrade --hooks-only")
