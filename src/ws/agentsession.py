"""Admin commands refuse to run inside an agent session: ws itself checks, whatever way the command was spelled.

The gate's text matcher (ws.policy) reads a shell command line and denies `ws secret ...` and the other admin commands in
it. Text cannot cover every spelling (a launcher, a path, a wrapper that has not been thought of), so the command checks
for itself. `ws.cli.main` calls `guard` once a command is parsed, before it runs, so a new admin command cannot forget
it. The admin commands are the one table here, which the matcher reads too: secrets and the broker, `approve`,
`uninstall`, and the ones that rewrite or mint what gates a session (`gate init|generate`, `install --gate`,
`skills adopt|restore`, `config fallback`, `session prepare`). `session start` is in the table for the matcher but not
for `guard`: the runner's own SessionStart hook runs it inside the session, with no terminal, so refusing it here would
stop the registry being written. `broker serve` is the same: `ws run` starts it for a job's secrets, with no terminal,
from a session when jobs are dispatched from one (the matcher still denies it typed; the other broker commands are
refused here).

A call is in an agent session when either of these holds:

  1. `WS_GATED_SESSION` is set (`ws gate launch` and the generated project settings set it).
  2. A process above this one is an agent runner (claude, opencode, codex, gemini, goose), read from the process
     tree (`/proc` on Linux, `ps` elsewhere), matched on a process's name or the name of the program it runs, never on
     command text. The user-level install leaves the marker out on purpose (it would block a person's own shell), so
     this is what recognises a session started the usual way. If the tree cannot be read, it counts as a session
     unless both ends are a terminal; a tree deeper than the walk reads counts as a runner, terminal or not.
     Native Windows has no tree to read here, and only (1) applies.

A session's tool shell has no terminal, and a session can make itself one (`script`, a pty from any language). So having
a terminal is not enough under a runner: a person is let through only on the terminal the runner itself holds, with the
command in that terminal's foreground, which is you typing `ws secret` in a shell the runner started for you. A pty a
session made is a different terminal. With no runner in the tree a terminal is yours. Marker set: refused with a terminal
too. There is no environment switch to turn the check off: it would be the way round it. The tests hand `guard` the tree
and the terminals as arguments instead.

What a process can't see is a command that has left the tree: a daemon's child (`tmux new -d`, a double fork, a service
manager) with the marker unset looks like a person's own pane or a service. The gate's matcher reads those spellings as
the first line, and the secret wall and the sandbox are what stand behind it. This is not a defence against a determined
session that can run arbitrary programs outside the wall. It stops the model running an admin command by accident, by a
plain instruction, or in a spelling the matcher did not know.

Refusal is a `WsError` of kind `refused`, exit 3 (as for `ws run`), with the hint to run it in your own terminal.
"""

from __future__ import annotations

import os
import posixpath
import sys
from collections.abc import Callable, Mapping

ENV_MARKER = "WS_GATED_SESSION"  # set in a gated session's env; ws.approvals reads the same name

# the runners a session can be inside (the name of the process, or of the program it runs)
RUNNERS = frozenset({"claude", "opencode", "codex", "gemini", "goose"})
_INTERPRETERS = frozenset({"node", "nodejs", "bun", "deno"})  # a script runner: the program is the next word

# The admin commands, one table. `ADMIN` is a first word with any subcommand; `ADMIN_SUB` a first and second word.
# `install` is admin only with `--gate` (anything but every-session): see `admin_name`.
ADMIN = frozenset({"secret", "broker", "approve", "uninstall", "upgrade"})
ADMIN_SUB = frozenset(
    {
        ("gate", "init"),
        ("gate", "generate"),
        ("skills", "adopt"),
        ("skills", "restore"),
        ("config", "fallback"),
        ("session", "prepare"),  # mints the role a launcher gives a session
        ("session", "start"),  # writes a session's registry record, role included
        ("node", "join"),  # changes the machine's own files
        ("node", "remove"),
        ("queue", "add"),  # queues unattended runs, which spend without a person watching
        ("queue", "cancel"),
        ("queue", "run"),
    }
)
# in the table for the text matcher, but run by the runner's own hook inside the session, so `guard` lets it through
HOOK_RUN = frozenset({("session", "start")})
# ...and `ws run` starts the broker for a job's secrets with no terminal, from a session when the lead dispatches jobs.
# The matcher still denies a typed `ws broker serve`; every other broker command is refused here.
WS_SPAWNED = frozenset({("broker", "serve")})
# ...and `upgrade` makes its own check first (every sign of a session, a terminal) and then `refusal`, in ws.upgrade.
SELF_CHECKED = frozenset({("upgrade",)})
NOT_GUARDED = HOOK_RUN | WS_SPAWNED | SELF_CHECKED  # the commands `guard` lets through to their own code

_HOPS = 64  # most ancestors read before the walk stops
_TOO_DEEP = "a process tree too deep to read"  # the walk's last name when it stopped with processes still above it
Ancestry = Callable[[], "list[str] | None"]


def admin_name(command: str, args: object) -> str | None:
    """The admin command `command` (the dotted name the parser gave it, `secret.set`, `gate.generate`) is, as it is
    typed, or None. `install` counts only with a `--gate` that is not `every-session`. `config fallback` is the command
    `config` with the topic `fallback`, so a topic that is a single word counts as the second."""
    parts = command.split(".")
    topic = getattr(args, "topic", None)
    if len(parts) == 1 and isinstance(topic, str):
        parts.append(topic)
    if parts[0] in ADMIN:
        return parts[0]
    if tuple(parts[:2]) in ADMIN_SUB:
        return " ".join(parts[:2])
    if parts[0] == "install" and getattr(args, "gate", None) not in (None, "every-session"):
        return "install --gate"
    return None


def _name(text: str) -> str:
    base = posixpath.basename(text.strip().replace("\\", "/")).lower()
    return base.removesuffix(".exe")


def _program_names(comm: str, exe: str, argv: list[str]) -> list[str]:
    """The names one process answers to: its own name, its executable, argv[0] and, for a script run by an interpreter,
    the script's name (`node /x/bin/claude`)."""
    names = [comm, exe, argv[0] if argv else ""]
    if argv and _name(argv[0]) in _INTERPRETERS:
        names += [a for a in argv[1:] if not a.startswith("-")][:1]
    return list(dict.fromkeys(n for n in (_name(x) for x in names) if n))


def _proc_ancestry(pid: int, proc: str = "/proc") -> list[str] | None:
    """The names of `pid` and every process above it, read from `proc`; None when a link in the chain cannot be read."""
    out: list[str] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(seen) < _HOPS:
        seen.add(pid)
        base = f"{proc}/{pid}"
        try:
            with open(f"{base}/stat", encoding="utf-8", errors="replace") as f:
                stat = f.read()
            head, _, tail = stat.rpartition(")")
            comm = head.partition("(")[2]
            ppid = int(tail.split()[1])
        except (OSError, ValueError, IndexError):
            return None
        try:
            exe = os.readlink(f"{base}/exe")
        except OSError:
            exe = ""
        try:
            with open(f"{base}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            argv = []
        out += _program_names(comm, exe, argv)
        pid = ppid
    return [*out, _TOO_DEEP] if pid > 1 and pid not in seen else out


def _ps_ancestry(pid: int) -> list[str] | None:
    """The same walk with `ps`, for macOS and other systems without `/proc`. `/bin/ps` by its full path, so a PATH the
    session controls cannot answer for it."""
    import subprocess

    def ps(*fields: str) -> str | None:
        try:
            done = subprocess.run(
                ["/bin/ps", *fields, "-p", str(pid)], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None

    out: list[str] = []
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(seen) < _HOPS:
        seen.add(pid)
        row = ps("-o", "ppid=", "-o", "comm=")
        if row is None:
            return None
        ppid_text, _, comm = row.partition(" ")
        try:
            ppid = int(ppid_text)
        except ValueError:
            return None
        line = ps("-o", "args=") or ""
        out += _program_names(comm, "", line.split())
        pid = ppid
    return [*out, _TOO_DEEP] if pid > 1 and pid not in seen else out


def read_ancestry() -> list[str] | None:
    """The names of every process above this one, nearest first, or None when the tree cannot be read."""
    if sys.platform == "win32":
        return None
    if sys.platform.startswith("linux"):
        return _proc_ancestry(os.getppid())
    return _ps_ancestry(os.getppid())


def _proc_terminals(pid: int, proc: str = "/proc") -> tuple[int, set[int]] | None:
    """The controlling terminal of `pid` and of each agent runner at or above it, as the kernel's numbers (0 is none,
    and stays in the set: a runner with no terminal is what a real session has above a pty it made), or None when a
    link cannot be read or the tree is deeper than the walk."""
    mine: int | None = None
    runners: set[int] = set()
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(seen) < _HOPS:
        seen.add(pid)
        base = f"{proc}/{pid}"
        try:
            with open(f"{base}/stat", encoding="utf-8", errors="replace") as f:
                head, _, tail = f.read().rpartition(")")
            fields = tail.split()
            ppid, tty = int(fields[1]), int(fields[4])
        except (OSError, ValueError, IndexError):
            return None
        if mine is None:
            mine = tty
        try:
            exe = os.readlink(f"{base}/exe")
        except OSError:
            exe = ""
        try:
            with open(f"{base}/cmdline", "rb") as f:
                argv = [a.decode("utf-8", "replace") for a in f.read().split(b"\0") if a]
        except OSError:
            argv = []
        if set(_program_names(head.partition("(")[2], exe, argv)) & RUNNERS:
            runners.add(tty)
        pid = ppid
    if pid > 1 and pid not in seen:
        return None
    return (mine, runners) if mine is not None else None


def _ps_terminals(pid: int) -> tuple[str, set[str]] | None:
    """The same with `ps`, whose terminal is a name (`ttys003`, `??` for none)."""
    import subprocess

    def ps(field: str, at: int) -> str | None:
        try:
            done = subprocess.run(
                ["/bin/ps", "-o", field, "-p", str(at)], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return done.stdout.strip() if done.returncode == 0 and done.stdout.strip() else None

    mine = ps("tty=", pid)
    if mine is None:
        return None
    runners: set[str] = set()
    seen: set[int] = set()
    while pid > 1 and pid not in seen and len(seen) < _HOPS:
        seen.add(pid)
        ppid_text, comm, line, tty = ps("ppid=", pid), ps("comm=", pid), ps("args=", pid) or "", ps("tty=", pid)
        if ppid_text is None or comm is None or tty is None:
            return None
        try:
            ppid = int(ppid_text)
        except ValueError:
            return None
        if set(_program_names(comm, "", line.split())) & RUNNERS:
            runners.add(tty)
        pid = ppid
    return None if pid > 1 and pid not in seen else (mine, runners)


def on_runner_terminal() -> bool:
    """Whether this process is in the foreground of a terminal that every agent runner above it holds. One runner on
    another terminal or none, which a process named after a runner on a session's own pty still leaves above it, is a
    no, and so is anything that can't be read."""
    try:
        if os.tcgetpgrp(0) != os.getpgrp():
            return False
    except (OSError, AttributeError):
        return False
    pid = os.getpid()
    found = _proc_terminals(pid) if sys.platform.startswith("linux") else _ps_terminals(pid)
    return found is not None and found[0] not in (0, "??") and found[1] == {found[0]}


def _is_tty(stream) -> bool:
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError, OSError):
        return False


def refusal(
    env: Mapping[str, str],
    stdin_tty: bool,
    stdout_tty: bool,
    ancestry: Ancestry | None = None,
    windows: bool | None = None,
    runner_terminal: Callable[[], bool] | None = None,
) -> str | None:
    """Why an admin command must not run here, or None when it may. `ancestry` reads the process tree and
    `runner_terminal` says whether this is the foreground of a runner's own terminal (the tests give planted ones)."""
    if env.get(ENV_MARKER):
        return f"it is for a person, not for a gated session ({ENV_MARKER} is set here)"
    if (sys.platform == "win32") if windows is None else windows:
        return None
    terminal = stdin_tty and stdout_tty
    names = (ancestry or read_ancestry)()
    if names is None:
        return None if terminal else "it is for a person, and ws cannot tell whether this runs under an agent runner"
    for n in names:
        if n in RUNNERS or n == _TOO_DEEP:
            if terminal and (runner_terminal or on_runner_terminal)():
                return None
            return f"it is for a person, not for an agent session (it runs under {n}, not at the terminal {n} holds)"
    return None


def guard(
    args: object,
    *,
    env: Mapping[str, str] | None = None,
    stdin_tty: bool | None = None,
    stdout_tty: bool | None = None,
    ancestry: Ancestry | None = None,
    windows: bool | None = None,
    runner_terminal: Callable[[], bool] | None = None,
) -> None:
    """Raise a `WsError` (kind `refused`, exit 3) when `args` is an admin command and this is an agent session. Called
    by `ws.cli.main` for every parsed command. The keywords are for tests; the defaults are the real process."""
    command = getattr(args, "ws_command", "")
    name = admin_name(command, args)
    if name is None or tuple(command.split(".")[:2]) in NOT_GUARDED:
        return
    why = refusal(
        os.environ if env is None else env,
        _is_tty(sys.stdin) if stdin_tty is None else stdin_tty,
        _is_tty(sys.stdout) if stdout_tty is None else stdout_tty,
        ancestry,
        windows,
        runner_terminal,
    )
    if why:
        from ws.output import WsError

        raise WsError("refused", f"ws {name}: {why}", "run it in your own terminal", 3)
