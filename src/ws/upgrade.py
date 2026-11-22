"""`ws upgrade`: swap the ws binary for a newer release and rewrite the runner's hooks in the same step.

    ws upgrade [--version TAG | --channel NAME]   run the installer, then rewrite the hooks with the new binary
    ws upgrade --hooks-only                       rewrite the hooks only (a newer ws is already installed)
    ws upgrade --dry-run                          say what would change and change nothing
    ws upgrade --rollback [--version TAG]         put the previous release back (the one the last upgrade came from)
    ws upgrade --check "COMMAND"                  after the swap, run COMMAND; a non-zero exit rolls the swap back

Why one step. The hooks in a runner's config name the ws that runs them by path, so installing a newer ws also changes
what every old hook runs. When the newer ws wants something the old hooks lack, the person is stuck: every call of every
session is judged by the new binary against the old hooks, and the fix (`ws install`) cannot run from inside a gated
session. `ws upgrade` does both halves from a terminal of the person's own, and says which pair they are left with:

- the installer fails: the old binary and the old hooks, unchanged;
- the installer works and the rewrite fails: the new binary and the old hooks, put back byte for byte (the runner's
  config, the install record and the OpenCode plugin). Those hooks still work, because the gate runs a hook that has no
  pin in full and prints a note (see `ws.paths.pinned_by`); the note names this command;
- both work: the new binary and the new hooks.

The rewrite runs in a new process of the new binary (`--hooks-only`), never in the process that started the install: the
files it would import have just been replaced. Which binary is read back from what the installer left (`installed_ws`:
the `ws` command in the folder uv or pipx puts it, and the Python its `#!` line names), not assumed to be the Python
this process runs; the version on record is asked of the same one, and the step says which it ran (C648).

The rewrite is `ws install`'s own gate steps (`ws.install.gate_plan`) and nothing else, bar one file: the profile, the
modules and the folders are not touched. It never switches on what the person did not have (a config written without the
sandbox block stays without it, see `ws.userconfig.keeps_sandbox_off`), and never takes ws out of a config (that is
`ws install --gate launch`). The one file is an older OpenCode plugin that ws put there and has since withdrawn from
(`ws.userconfig.withdrawn_plugin`): it is still loaded, so it is taken out, and said so. The other is the policy: a
guard that became a default since the person's policy was written (`DEFAULT_GUARDS`, today `pane-keys`) is added to the
end of an existing `[guards] on` list, once, as a textual insert that leaves every comment and other key as written. A
policy with no `[guards]` table or no `on` list, or one that already names the guard, is not touched; one line says what
was added.

A runner ws gated whose config it can't merge into now (a link, JSONC, a value of the person's in the way) is left as it
is, named with the reason and the way on, and the exit code is 1: the other runners are rewritten, and the person is
never told "done" while a runner still has the old hooks.

Promote and rollback. Every swap is a promote and is recorded in `<state>/upgrade-history.jsonl` (time, verb, from, to).
`--rollback` reinstalls the release the last promote came from (or `--version`), and rewrites the hooks for it, so one
command goes each way. Each step of a cut-over has its own watchdog: the installer, the hooks rewrite and the health
check (`--check`, a command of the person's own: a page that must answer, a service that must be up) each get a time
limit (`--install-timeout`, 600 seconds, and `--step-timeout`, 120), and a step that runs past it is stopped. A swap
whose installer or hooks step times out, or whose health check fails or times out, is rolled back at once and recorded
as `auto-rollback`; a swap whose installer simply fails changed nothing and is not rolled back.

A way back puts the policy back too (C706). A release can change the policy (`DEFAULT_GUARDS`), and the release it is rolled
back to may not know what was added: it would refuse the policy, and the gate fails closed for every session. So the swap
first holds the policy, the install record and each runner config the rewrite may change; an automatic rollback puts them
back between the old installer and the old hooks step. The same files are copied to `<state>/upgrade-backups/` (the newest
five swaps), and the promote's history line names the folder and the policy's hash as the upgrade left it. A manual
`--rollback` of that promote restores the policy from the copy only while the policy still has that hash: a policy the
person has edited since is theirs, so it is left and the copy is named.

Swap note: the release is whatever the installer installs; to promote from somewhere else (a package manager, a release
tree), pass `--installer` with your own script that takes `--version TAG`, and keep the history and the watchdog.

It is refused inside a gated session, and fails towards refusing. `WS_GATED_SESSION` is left out of the user-scope
settings so that `ws approve` works from the person's own Claude shell, so the other signs are read as well: the sandbox
marker the user config sets in every session, the claim socket, and the variables a runner sets in its own sessions. A
run without a terminal on stdin is refused too (a session's shell has none), as `ws approve` is.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ws import __version__, agentsession, approvals, install, jsonl, output, paths, policy, selfcmd, userconfig
from ws.output import WsError

COMMAND = "upgrade"
EXIT_FAILED, EXIT_REFUSED, EXIT_NOT_RESTORED = 1, 3, 4
EXIT_TIMEOUT = 124  # a step the watchdog stopped (the shell's own code for a timeout)
HISTORY_SCHEMA = 1  # of a line in upgrade-history.jsonl
LOG_SCHEMA = 1  # of a line in logs/upgrade.jsonl
TAIL_LINES, _TAIL_WIDTH = 20, 500  # of each step's output, kept for the log
_TAIL_STEPS = ("installer", "hooks")  # the steps whose output the log keeps
BROKER_STOP_S = 10.0  # how long a stopped broker has to close its socket
BROKER_START_S = 30.0  # how long the new broker has to answer
DEFAULT_GUARDS = (
    "pane-keys",
)  # guards added to a policy's `[guards] on` list by the rewrite, once, when it is missing
STEP_ENV = "WS_UPGRADE_STEP"  # set for each step the upgrade runs: a hooks rewrite there leaves the log line to it
INSTALL_TIMEOUT_S, STEP_TIMEOUT_S = 600.0, 120.0  # the watchdog per step: the installer, then the hooks and the check
BACKUPS_DIR, BACKUPS_KEPT = "upgrade-backups", 5  # what a swap keeps of the files it may change, and how many swaps
INSTALLER_URL = "https://raw.githubusercontent.com/Michael-A-Fry/ws/main/install.sh"
_DOWNLOAD_LIMIT = 1 << 20  # bytes: install.sh is about 40 KB


# ---- refusing a session -----------------------------------------------------------------------------------------------------


def session_signs(env: Mapping[str, str]) -> list[str]:
    """The names of the variables in `env` that say this process is inside a runner's session, gated or not. A person's own
    terminal has none of them."""
    from ws.term import bridge  # late: the names live with the code that strips them for a new terminal

    names = (approvals.ENV_MARKER, approvals.SANDBOX_MARKER, approvals.FD_ENV, *bridge.RUNNER_SESSION_NAMES)
    return [n for n in names if env.get(n)]


def refusal(env: Mapping[str, str], stdin_is_tty: bool) -> str | None:
    """Why this run is refused, or None. Fails towards refusing: any sign of a session, or no terminal. `cmd_upgrade`
    then makes the check every admin command gets (`ws.agentsession.refusal`: the process tree and whose terminal)."""
    signs = session_signs(env)
    if signs:
        return f"ws upgrade is for a person, not for a session ({signs[0]} is set here)"
    if not stdin_is_tty:
        return "ws upgrade needs a terminal (stdin is not one): run it yourself, not from a session or a script"
    return None


# ---- the rewrite ------------------------------------------------------------------------------------------------------------


@dataclass
class _Held:
    """The bytes of the files a rewrite can change, to put back if it fails."""

    files: dict[Path, bytes | None]

    @classmethod
    def of(cls, paths: list[Path]) -> _Held:
        held: dict[Path, bytes | None] = {}
        for p in dict.fromkeys(paths):
            try:
                held[p] = p.read_bytes() if p.is_file() and not p.is_symlink() else None
            except OSError:
                held[p] = None
        return cls(held)

    def keep(self, env: Mapping[str, str]) -> Path | None:
        """Copy these files into a folder of their own under `<state>/upgrade-backups/` (newest five kept), with an
        `index.json` saying which copy is which file, and return the folder. A manual rollback days later reads the
        policy back from it (`_rollback`). None when it could not be written: the in-memory copy still serves the
        automatic rollback."""
        root = paths.state_dir(dict(env)) / BACKUPS_DIR
        folder = root / time.strftime("%Y%m%dT%H%M%S", time.gmtime())
        try:
            folder.mkdir(parents=True, mode=0o700, exist_ok=True)
            index = {}
            for n, (p, data) in enumerate(self.files.items()):
                index[str(p)] = None if data is None else str(n)
                if data is not None:
                    (folder / str(n)).write_bytes(data)
            (folder / "index.json").write_text(json.dumps(index), encoding="utf-8")
            for old in sorted(d for d in root.iterdir() if d.is_dir())[:-BACKUPS_KEPT]:
                shutil.rmtree(old, ignore_errors=True)
        except OSError:
            return None
        return folder

    @classmethod
    def from_backup(cls, folder: Path, only: Path) -> _Held | None:
        """What `keep` wrote for the one file `only`, or None when the folder has no copy of it."""
        try:
            name = json.loads((folder / "index.json").read_text(encoding="utf-8")).get(str(only))
            return cls({only: (folder / name).read_bytes()}) if name else None
        except (OSError, ValueError, AttributeError):
            return None

    def restore(self) -> list[Path]:
        """Write back what changed, and return the files that could not be written back (none, when all went well). A file
        that did not exist is left if it is there now: it is ws's own and harmless."""
        failed: list[Path] = []
        for p, data in self.files.items():
            if data is None:
                continue
            try:
                if p.is_file() and p.read_bytes() == data:
                    continue
                tmp = p.with_name(p.name + ".ws-upgrade")
                tmp.write_bytes(data)
                os.chmod(tmp, p.stat().st_mode & 0o777 if p.exists() else 0o600)
                os.replace(tmp, p)
            except OSError:
                failed.append(p)
        return failed


def _put_back(held: _Held, error: BaseException) -> WsError:
    """The error to raise after a failed rewrite, once the files were put back: it says so only if they were. When some
    could not be, it names them and where the first copy of the config is, and exits with `EXIT_NOT_RESTORED`."""
    failed = held.restore()
    cause = error.message if isinstance(error, WsError) else f"{type(error).__name__}: {error}"
    if not failed:
        hint = error.hint if isinstance(error, WsError) and error.hint else "nothing was changed; `ws doctor` says why"
        return WsError("failed", f"{cause}; ws put the files back as they were", hint)
    names = ", ".join(str(p) for p in failed)
    return WsError(
        "not_restored",
        f"{cause}; and ws could not put these back as they were: {names}",
        "the config's first copy is in the backups folder of ws's state: copy it over the file, or run "
        "`ws install --gate launch` in a terminal of your own, then `ws doctor`",
        EXIT_NOT_RESTORED,
    )


def migrate_policy(env: Mapping[str, str], dry_run: bool = False) -> list[str]:
    """Add each of `DEFAULT_GUARDS` that the person's `[guards] on` list lacks (`policy.add_to_guards_on`), and return a
    note for each one added (or, on a dry run, that would be). A policy that is missing, unreadable, without that list, or
    that already names the guard is left exactly as it is, so a second run changes nothing."""
    path = _policy_file(env)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    done: list[str] = []
    for name in DEFAULT_GUARDS:
        more = policy.add_to_guards_on(text, name)
        if more is not None:
            text, done = more, [*done, name]
    if not done:
        return []
    if not dry_run:
        tmp = path.with_name(path.name + ".ws-upgrade")
        tmp.write_text(text, encoding="utf-8")
        os.chmod(tmp, path.stat().st_mode & 0o777)
        os.replace(tmp, path)
    verb = "would add" if dry_run else "added"
    return [f"{verb} {', '.join(done)} to [guards] on in {path}, which lacked it (a guard that is now on by default)"]


def _policy_file(env: Mapping[str, str]) -> Path:
    return Path(os.path.realpath(policy.policy_path(dict(env))))


def _rewrite_paths(env: Mapping[str, str], steps: list, plug: Path | None) -> list[Path]:
    """The files a rewrite can change: the install record, the policy, each runner config and plugin it merges into."""
    return [
        install.manifest_path(env),
        _policy_file(env),
        *(s.path for s in steps if s.kind == "merge" or s.path.suffix == ".js"),
        *([plug] if plug else []),
    ]


def held_now(env: Mapping[str, str]) -> _Held:
    """The files the new ws's rewrite may change, as they are now: what an automatic rollback puts back (C706: a release
    that adds a guard to the policy must not leave it there for the release it is rolled back to, which may reject the
    name and so fail the gate closed for every session). Always the policy and the install record, even when the plan
    for the rest cannot be made."""
    try:
        steps, _ = install.gate_plan(env)
        return _Held.of(_rewrite_paths(env, steps, userconfig.withdrawn_plugin(env)))
    except (install.InstallError, OSError, WsError):
        return _Held.of([install.manifest_path(env), _policy_file(env)])


def rewrite(env: Mapping[str, str], dry_run: bool = False) -> install.Result:
    """Rewrite the runner's hooks (and the generated gate files) with this ws. On any failure the runner's config, the
    install record and the OpenCode plugin are put back as they were, and the error is raised; if any could not be put
    back it is `not_restored` (exit 4) and names them. A runner that could not be rewritten is in `result.skipped`; a
    withdrawn older OpenCode plugin that was taken out is in `result.removed`."""
    steps, skipped = install.gate_plan(env)
    plug = userconfig.withdrawn_plugin(env)
    if dry_run:
        result = install.apply(steps, env, dry_run=True)
        result.notes += migrate_policy(env, dry_run=True)
        result.skipped = skipped
        result.removed += [plug] if plug else []
        return result
    held = _Held.of(_rewrite_paths(env, steps, plug))
    try:
        notes = migrate_policy(env)
        result = install.apply(steps, env)
        result.notes += notes
        result.skipped = skipped
        rewritten = {s.merge.runner for s in steps if s.kind == "merge" and s.merge is not None and s.merge.ops}
        if plug:
            plug.unlink()
            install.forget([plug], env)
            result.removed.append(plug)
            rewritten.add("opencode")
        left = [r for r in userconfig.hooks_without_pin(env) if r in rewritten]
        if left:
            raise WsError("failed", f"the hooks in {', '.join(left)} still carry no pin after the rewrite", "")
    except BaseException as e:
        raise _put_back(held, e) from e
    return result


# ---- swapping the binary ----------------------------------------------------------------------------------------------------


def fetch_installer(dest: Path) -> Path:
    """Download install.sh into `dest` (a folder of its own) and return the file. https only; a file that does not look like
    the installer is not kept."""
    if not INSTALLER_URL.startswith("https://"):
        raise WsError("failed", "the installer address is not https", "use --installer with a copy you have checked")
    try:
        with urllib.request.urlopen(INSTALLER_URL, timeout=30) as resp:
            data = resp.read(_DOWNLOAD_LIMIT + 1)
    except OSError as e:
        raise WsError(
            "failed",
            f"could not download the installer ({type(e).__name__})",
            "check the network, or pass --installer with a copy of install.sh (docs/how-to/install.md)",
        ) from e
    if len(data) > _DOWNLOAD_LIMIT or not data.startswith(b"#!/usr/bin/env bash"):
        raise WsError(
            "failed", "what came back is not the ws installer", "pass --installer with a copy you have checked"
        )
    path = dest / "install.sh"
    path.write_bytes(data)
    return path


class Ran(NamedTuple):
    """How one step ended: its exit code, whether the watchdog stopped it (then `code` is EXIT_TIMEOUT), and the last
    lines it printed (for the log)."""

    code: int
    timed_out: bool = False
    tail: tuple[str, ...] = ()


def _signal_group(p: subprocess.Popen, hard: bool) -> None:
    """TERM (or KILL) the step's whole process group, so a child it started dies with it; a plain terminate where
    there are no process groups."""
    killpg = getattr(os, "killpg", None)
    try:
        if killpg is None:
            p.kill() if hard else p.terminate()
        else:
            killpg(p.pid, signal.SIGKILL if hard else signal.SIGTERM)
    except OSError:  # the group is already gone
        pass


def _stop(p: subprocess.Popen, drained: bool = False) -> None:
    """Stop a step and everything it started: TERM, a bounded grace, then KILL to the whole group. The KILL is sent even
    when the step itself has gone, because a child that ignores TERM, or that the TERM did not stop, outlives it. A
    process that left the group and still holds the pipe cannot hang this: the pipes are closed and the step is left to
    the kernel. `drained`: a thread of the caller reads the pipe, so this only waits (reading it here too would race,
    and closing it under a blocked read would hang)."""

    def settle() -> bool:
        try:
            p.wait(timeout=10) if drained else p.communicate(timeout=10)
            return True
        except subprocess.TimeoutExpired:
            return False

    _signal_group(p, hard=False)
    reaped = settle()
    _signal_group(p, hard=True)
    if reaped or settle() or drained:
        return
    for f in (p.stdout, p.stderr):
        if f is not None:
            f.close()
    try:
        p.wait(timeout=10)
    except subprocess.TimeoutExpired:
        pass


def _pump(src, dest, tail: deque[str]) -> None:
    """Pass each line of a step's output on as it arrives, and keep the last few for the log."""
    try:
        for raw in iter(src.readline, b""):
            line = raw.decode("utf-8", "replace")
            tail.append(line.rstrip("\n")[:_TAIL_WIDTH])
            try:
                dest.write(line)
                dest.flush()
            except (OSError, ValueError):
                pass
    except (OSError, ValueError):
        pass


def _run(argv: list[str], capture: bool, timeout: float | None = None) -> Ran:
    """Run a step: its stdin is the person's terminal, and its output (stdout and stderr, as one stream) is passed on line
    by line as it comes, to stdout, or to stderr under `--json`; the last `TAIL_LINES` lines come back in `Ran.tail` for
    the log. A run that is still going after `timeout` seconds is stopped with every process it started: the watchdog of
    one step. The step is told it runs under the upgrade (`STEP_ENV`), so a hooks rewrite leaves the log line to it."""
    p = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env={**os.environ, STEP_ENV: "1"},
        start_new_session=True,  # its own group, so the watchdog can stop all of it
    )
    tail: deque[str] = deque(maxlen=TAIL_LINES)
    pump = threading.Thread(target=_pump, args=(p.stdout, sys.stderr if capture else sys.stdout, tail), daemon=True)
    pump.start()
    try:
        p.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _stop(p, drained=True)
        pump.join(timeout=2)
        return Ran(EXIT_TIMEOUT, timed_out=True, tail=tuple(tail))
    except BaseException:  # Ctrl-C reaches ws, not the step's own session: stop the step too
        _stop(p, drained=True)
        raise
    pump.join(timeout=10)  # a child that left the group may hold the pipe: the step's own exit is what counts
    return Ran(p.returncode, tail=tuple(tail))


def log_path(env: Mapping[str, str]) -> Path:
    return paths.state_dir(dict(env)) / "logs" / "upgrade.jsonl"


def log_step(
    args: argparse.Namespace,
    env: Mapping[str, str],
    step: str,
    ran: Ran,
    seconds: float,
    python: str | None = None,
    version: str | None = None,
) -> None:
    """One line in `<state>/logs/upgrade.jsonl` for one step of one run: what ran, with which Python and ws, how it ended,
    and the last lines it printed. Not rotated: a few lines per upgrade. A line that cannot be written is said once and
    never stops the upgrade (C650)."""
    rec = {
        "verb": getattr(args, "log_verb", None) or "upgrade",
        "dry_run": bool(getattr(args, "dry_run", False)),
        "step": step,
        "python": python,
        "version": version,
        "exit": ran.code,
        "timed_out": ran.timed_out,
        "seconds": round(seconds, 3),
        "tail": list(ran.tail),
    }
    try:
        jsonl.append_plain(log_path(env), jsonl.stamped(rec, LOG_SCHEMA))
    except (OSError, ValueError) as e:
        if not getattr(args, "log_warned", False):
            args.log_warned = True
            print(f"ws: could not write {log_path(env)} ({type(e).__name__}); the upgrade goes on", file=sys.stderr)


def _step(args: argparse.Namespace, step: str, argv: list[str], timeout: float, version: str | None = None) -> Ran:
    """Run one step under its watchdog and log how it went."""
    t = time.monotonic()
    ran = _run(argv, args.json, timeout)
    kept = ran if step in _TAIL_STEPS else ran._replace(tail=())  # a check is the person's own: its output stays theirs
    log_step(args, os.environ, step, kept, time.monotonic() - t, argv[0] if argv else None, version)
    return ran


def _step_limit(args: argparse.Namespace) -> float:
    return getattr(args, "step_timeout", None) or STEP_TIMEOUT_S


def _installer_argv(script: Path, args: argparse.Namespace) -> list[str]:
    argv = ["bash", str(script), "--skip-smoke-test"]
    if args.version:
        argv += ["--version", args.version]
    if args.channel:
        argv += ["--channel", args.channel]
    return argv


class NewWs(NamedTuple):
    """The ws the installer just wrote: the argv prefix that runs it, its Python, its version (None when it did not
    answer), and where ws found it."""

    argv: list[str]
    python: str
    version: str | None
    found: str


def _bin_dirs(env: Mapping[str, str]) -> list[Path]:
    """The folders an installer puts the `ws` command in, in the order uv and then pipx look them up."""
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    dirs = [env.get("UV_TOOL_BIN_DIR"), env.get("XDG_BIN_HOME")]
    if env.get("XDG_DATA_HOME"):
        dirs.append(str(Path(env["XDG_DATA_HOME"]).parent / "bin"))
    dirs += [env.get("PIPX_BIN_DIR"), str(home / ".local" / "bin")]
    return [Path(d) for d in dict.fromkeys(d for d in dirs if d)]


def _script_python(script: Path) -> str | None:
    """The Python a console script runs: its `#!` line, or the `'''exec' PYTHON "$0"` line a long-path script uses. The
    script is read, never run."""
    try:
        with open(script, "rb") as f:
            head = f.read(4096).decode("utf-8", "replace").splitlines()[:2]
    except OSError:
        return None
    if not head or not head[0].startswith("#!"):
        return None
    try:
        words = shlex.split(head[0][2:]) if head[0][2:].strip() else []
        if words and os.path.basename(words[0]).startswith("python"):
            return words[0]
        if len(head) > 1 and head[1].startswith("'''exec'"):
            rest = shlex.split(head[1][len("'''exec'") :])
            return rest[0] if rest else None
    # an unbalanced quote: not a script ws can read, so not one to run (and this runs after the swap: never raise)
    except ValueError:
        return None
    return None


def _ask_version(prefix: list[str]) -> str | None:
    """The version a ws answers with, asked of a new process."""
    try:
        p = subprocess.run([*prefix, "--version"], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    words = p.stdout.split()
    return words[-1] if p.returncode == 0 and len(words) >= 2 and words[0] == "ws" else None


def installed_ws(env: Mapping[str, str]) -> NewWs:
    """The ws the installer just wrote, read back from the `ws` command it put in place: that script's own Python, which
    must answer `--version` as ws. The running process is the fallback (and is named as such): it is the same Python
    whenever the installer rebuilt the environment it runs from, but nothing else says so (C648)."""
    seen: set[str] = set()
    which = shutil.which("ws", path=env.get("PATH"))
    for script in [*(d / "ws" for d in _bin_dirs(env)), *([Path(which)] if which else [])]:
        py = _script_python(script) if script.is_file() else None
        if not py or py in seen or not os.path.isabs(py) or not os.access(py, os.X_OK):
            continue
        seen.add(py)
        prefix = [py, *selfcmd.argv()[1:]]  # the same isolated start, with the found Python
        if selfcmd.unstable(prefix):
            continue
        version = _ask_version(prefix)
        if version:
            return NewWs(prefix, py, version, f"the ws command at {script}")
    prefix = selfcmd.argv()
    return NewWs(prefix, prefix[0], _ask_version(prefix), "this process (no installed ws command answered)")


def _hooks_argv(new: NewWs | None = None) -> list[str]:
    """The new binary's own rewrite, in a new process of the ws the installer wrote (this one runs the old code)."""
    return [*(new.argv if new else selfcmd.argv()), "upgrade", "--hooks-only"]


def _using(args: argparse.Namespace, new: NewWs) -> None:
    """Say which ws the next step runs, so a step that runs the wrong one shows it (C648)."""
    _say(args, f"rewriting the hooks with ws {new.version or '(no answer)'} at {new.python}, found as {new.found}")
    asked = getattr(args, "version", None)
    if asked and new.version and _tag(new.version) != _tag(asked):
        _say(args, f"note: the installer was asked for {asked}, and the ws it left answers {new.version}")


def swap(args: argparse.Namespace, scratch: Path) -> Ran:
    """Run the installer under its watchdog."""
    script = Path(args.installer).expanduser() if args.installer else fetch_installer(scratch)
    if not script.is_file():
        raise WsError("failed", f"{script} is not a file", "pass --installer with a copy of install.sh")
    limit = getattr(args, "install_timeout", None) or INSTALL_TIMEOUT_S
    return _step(args, "installer", _installer_argv(script, args), limit)


# ---- history, health check, rollback ----------------------------------------------------------------------------------------


def history_path(env: Mapping[str, str]) -> Path:
    return paths.state_dir(dict(env)) / "upgrade-history.jsonl"


def record(env: Mapping[str, str], verb: str, frm: str, to: str | None, **extra: str) -> None:
    """One line of history, in one file that is never rotated (the way back must stay on record). `extra` is a promote's
    `backup` (the folder `_Held.keep` wrote) and `policy_sha` (the policy as the upgrade left it). A history that cannot
    be written never stops an upgrade."""
    try:
        line = jsonl.stamped({"verb": verb, "from": frm, "to": to, **extra}, HISTORY_SCHEMA)
        jsonl.append_plain(history_path(env), line)
    except OSError:
        pass


def history(env: Mapping[str, str]) -> list[dict]:
    try:
        lines = history_path(env).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            d = json.loads(line)
        except ValueError:
            continue
        if isinstance(d, dict):
            out.append(d)
    return out


def previous_release(env: Mapping[str, str]) -> str | None:
    """The release the last promote came from, unless a rollback has put it back since."""
    for d in reversed(history(env)):
        if d.get("verb") == "promote":
            return d.get("from")
        if d.get("verb") in ("rollback", "auto-rollback") and d.get("to"):  # one that failed put nothing back
            return None
    return None


def _tag(version: str) -> str:
    return version if version.startswith("v") else f"v{version}"


def reinstall(
    args: argparse.Namespace, env: Mapping[str, str], version: str, held: _Held | None = None
) -> tuple[Ran, NewWs | None]:
    """Install `version` and rewrite the hooks for it with the ws the installer left: the way back. Code 0 when both
    steps worked. `held` (files as they were before the release being left changed them) is put back between the two:
    the old ws must not read a policy the new one wrote (C706)."""
    ns = argparse.Namespace(**{**vars(args), "version": _tag(version), "channel": None})
    with tempfile.TemporaryDirectory(prefix="ws-rollback-") as scratch:
        os.chmod(scratch, 0o700)
        ran = swap(ns, Path(scratch))
    if ran.code != 0:
        return ran, None
    if held is not None:
        failed = held.restore()
        if failed:
            _say(args, "could not put these back as they were: " + ", ".join(str(p) for p in failed))
        else:
            _say(args, "the policy and the hooks are back as they were before the upgrade")
    new = installed_ws(env)
    _using(ns, new)
    return _step(ns, "hooks", _hooks_argv(new), _step_limit(args), new.version), new


# ---- the broker -------------------------------------------------------------------------------------------------------------


class Peer(NamedTuple):
    """What a broker said about itself on one connection, and who served it (from the kernel, not from the reply)."""

    status: dict
    pid: int | None
    uid: int | None
    pidfd: int | None = None


def broker_socket(env: Mapping[str, str]) -> Path:
    """The person's own broker socket: the one `ws run` starts a broker on. No other socket is ever touched."""
    return paths.state_dir(dict(env)).absolute() / "run" / "broker.sock"


def _peer_cred(conn: socket.socket) -> tuple[int | None, int | None]:
    """(pid, uid) of the process at the other end, from SO_PEERCRED; (None, None) where the kernel does not say."""
    opt = getattr(socket, "SO_PEERCRED", None)
    if opt is None:
        return None, None
    try:
        pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, opt, struct.calcsize("3i")))
    except OSError:
        return None, None
    return pid, uid


def _pidfd(pid: int | None) -> int | None:
    """A pidfd for `pid` (Linux 5.3 and later), or None. Opened while the connection to that process is still open, so
    the process it names is the one that answered: a signal sent through it can never reach a later process that was
    given the same pid."""
    opener = getattr(os, "pidfd_open", None)
    if opener is None or not pid or pid <= 0:
        return None
    try:
        return opener(pid)
    except OSError:
        return None


def ask_broker(sock: Path, timeout: float = 3.0, hold: bool = False) -> Peer:
    """Ask the broker on `sock` for `status` and read who answered on the same connection. With `hold`, also a pidfd of
    that process, opened while the connection is open (the caller closes it). Raises OSError or ValueError when nothing
    answers or the reply is not a status."""
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(timeout)
    fd = None
    try:
        conn.connect(str(sock))
        pid, uid = _peer_cred(conn)
        fd = _pidfd(pid) if hold else None
        conn.sendall(b'{"op": "status"}\n')
        buf = b""
        while b"\n" not in buf and len(buf) < 65536:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
        reply = json.loads(buf.split(b"\n", 1)[0])
        if not isinstance(reply, dict) or reply.get("ok") is not True:
            raise ValueError("the broker's reply is not a status")
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    finally:
        conn.close()
    return Peer(reply, pid, uid, fd)


def _gone(sock: Path, limit: float) -> bool:
    """Whether nothing answers on `sock` any more, within `limit` seconds."""
    end = time.monotonic() + limit
    while True:
        try:
            probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                probe.settimeout(1.0)
                probe.connect(str(sock))
            finally:
                probe.close()
        except (FileNotFoundError, ConnectionRefusedError):
            return True
        except OSError:
            pass
        if time.monotonic() >= end:
            return False
        time.sleep(0.1)


_START = (
    "import sys\nfrom pathlib import Path\nfrom ws import run\n"
    "sys.exit(0 if run._start_broker(Path(sys.argv[1])) else 1)\n"
)


def start_broker(new: NewWs, sock: Path) -> bool:
    """Start a broker of the ws the installer wrote, the way `ws run` does (`ws.run._start_broker`), in a process of
    that ws so the broker runs its code."""
    try:
        p = subprocess.run(
            [new.python, "-I", "-c", _START, str(sock)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=BROKER_START_S + 10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return p.returncode == 0


BROKER_DONE = ("not running", "current", "restarted", "would restart")  # the states that leave nothing to do


def restart_broker(args: argparse.Namespace, env: Mapping[str, str], new: NewWs) -> str:
    """After a swap whose hooks step and check both passed: a broker still running older code than the ws now installed
    is stopped and a new one started, so no step is left to the person (C651). Only the person's own socket; only when
    it holds no lease; only a server the kernel says is this user's (SO_PEERCRED, a pid above 0), signalled through a
    pidfd opened while it was answering. Anything else is left running and the command that finishes it is printed.

    Two windows are accepted, each about one status round trip long: a one-shot call (imap, mint, sign) in flight when
    the stop lands fails, and its caller asks again; and a lease asked for between the status reply (0 leases) and the
    stop is lost with the broker, so the run that asked for it fails the call and asks again, of the new broker.

    Returns the state, one of `BROKER_DONE` or "left running" / "failed"; it is logged, and the caller reports it."""
    t = time.monotonic()
    state = _restart_broker(args, env, new)
    code = 0 if state in BROKER_DONE else EXIT_FAILED
    log_step(args, env, "broker", Ran(code), time.monotonic() - t, new.python, new.version)
    return state


def _restart_broker(args: argparse.Namespace, env: Mapping[str, str], new: NewWs) -> str:
    sock = broker_socket(env)
    try:
        peer = ask_broker(sock, hold=True)
    except (OSError, ValueError):
        return "not running"  # the next `ws run` starts one of the new ws
    try:
        return _stop_and_start(args, sock, peer, new)
    finally:
        if peer.pidfd is not None:
            os.close(peer.pidfd)


def _stop_and_start(args: argparse.Namespace, sock: Path, peer: Peer, new: NewWs) -> str:
    running, leases = peer.status.get("version"), peer.status.get("leases")
    if not new.version or running == new.version:
        return "current"
    ours = bool(peer.pid) and peer.pid > 0  # SO_PEERCRED says 0 across pid namespaces; kill(0) is our own group
    finish = f"kill {peer.pid}" if ours else "stop the broker"
    finish += "; the next `ws run` starts one of the new ws"
    if leases != 0:
        _say(
            args,
            f"the broker still runs ws {running} with {leases} lease(s) open, so it is left running; when they are done: {finish}",
        )
        return "left running"
    getuid = getattr(os, "getuid", None)
    send = getattr(signal, "pidfd_send_signal", None)
    if not ours or getuid is None or peer.uid != getuid() or peer.pidfd is None or send is None:
        _say(
            args,
            f"the broker runs ws {running}, and ws cannot tell that its process is yours, so it is left running: {finish}",
        )
        return "left running"
    if args.dry_run:
        _say(args, f"would stop the broker (ws {running}, pid {peer.pid}) and start one of ws {new.version}")
        return "would restart"
    _say(args, f"the broker runs ws {running}: stopping it (pid {peer.pid}) and starting one of ws {new.version}")
    try:
        send(peer.pidfd, signal.SIGTERM)
    except OSError as e:
        _say(args, f"could not stop the broker ({type(e).__name__}): {finish}")
        return "failed"
    if not _gone(sock, BROKER_STOP_S):
        _say(args, f"the broker did not stop within {BROKER_STOP_S:.0f} seconds: {finish}")
        return "failed"
    if not start_broker(new, sock):
        _say(args, "the old broker is stopped; the new one did not answer yet, and the next `ws run` starts it")
        return "failed"
    _say(args, f"the broker is restarted on ws {new.version}")
    return "restarted"


# ---- the command ------------------------------------------------------------------------------------------------------------


def _say(args: argparse.Namespace, text: str) -> None:
    """A line for the person: stdout, or stderr under `--json` (stdout is the envelope)."""
    print(f"ws: {text}", file=sys.stderr if args.json else sys.stdout)


def _skipped_line(sk: install.Skipped) -> str:
    return f"{sk.runner}: not rewritten, left as it was: {sk.reason}" + (f". {sk.hint}" if sk.hint else "")


def _hooks_logged(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    """The hooks rewrite in this process, with its log line; under a parent upgrade (`STEP_ENV`) the parent logs it."""
    if env.get(STEP_ENV):
        return _hooks_only(args, env, [])
    said: list[str] = []
    t, code = time.monotonic(), EXIT_FAILED
    try:
        code = _hooks_only(args, env, said)
        return code
    except WsError as e:
        code = e.exit_code
        said.append(e.message)
        raise
    finally:
        ran = Ran(code, tail=tuple(line[:_TAIL_WIDTH] for line in said[-TAIL_LINES:]))
        log_step(args, env, "hooks", ran, time.monotonic() - t, sys.executable, __version__)


def _hooks_only(args: argparse.Namespace, env: Mapping[str, str], said: list[str]) -> int:
    def say(line: str, err: bool = False) -> None:
        said.append(line)
        print(line, file=sys.stderr if err else sys.stdout)

    try:
        result = rewrite(env, dry_run=args.dry_run)
    except (install.InstallError, OSError) as e:
        raise WsError("failed", str(e), "nothing was changed; `ws doctor` says what is wrong") from e
    left = [s.runner for s in result.skipped]
    if args.json:
        output.result(
            {
                "version": __version__,
                "hooks": "dry-run" if args.dry_run else "rewritten" if result.changes else "current",
                "changes": result.changes,
                "removed": [str(p) for p in result.removed],
                "skipped": [{"runner": s.runner, "reason": s.reason, "hint": s.hint} for s in result.skipped],
                "notes": list(result.notes),
            }
        )
        return EXIT_FAILED if left else 0
    for note in result.notes:
        say(f"ws: {note}")
    verb = "would remove" if args.dry_run else "removed"
    for path in result.removed:
        say(f"ws: {verb} {path} (an older OpenCode plugin that ws put there and had withdrawn)")
    verb = "would make" if args.dry_run else "made"
    say(
        f"ws {__version__} ({sys.executable}): {verb} {result.summary()}"
        if result.changes
        else f"ws {__version__} ({sys.executable}): hooks are current ({result.summary()})"
    )
    for sk in result.skipped:
        say(f"ws: {_skipped_line(sk)}")
    if left:
        say(
            f"ws: the hooks in {', '.join(left)} could not be rewritten this run. Do what is named above, "
            "then run `ws upgrade --hooks-only` again",
            err=True,
        )
        return EXIT_FAILED
    return 0


def _both(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    before = __version__
    if args.dry_run:
        _say(args, "would run the installer, then `ws upgrade --hooks-only` with the new ws")
        _say(args, "then, if the broker runs older code and holds no lease, would stop it and start one of the new ws")
        return _hooks_logged(args, env)
    tree = selfcmd.unstable()
    if tree:
        raise WsError(
            "refused",
            f"this ws runs from a place the installer does not manage ({tree})",
            "use `ws upgrade --hooks-only` here, or install ws with install.sh first",
            EXIT_REFUSED,
        )
    held = held_now(env)  # before the swap: what an automatic rollback puts back (C706)
    kept = held.keep(env)
    with tempfile.TemporaryDirectory(prefix="ws-upgrade-") as scratch:
        os.chmod(scratch, 0o700)
        ran = swap(args, Path(scratch))
    if ran.timed_out:
        return _auto_rollback(args, env, before, "the installer ran past its time limit", held)
    code = ran.code
    if code != 0:
        _say(args, f"the installer failed (exit {code}); you still have ws {before} and its hooks, unchanged")
        return code or EXIT_FAILED
    new = installed_ws(env)
    _using(args, new)
    ran = _step(args, "hooks", _hooks_argv(new), _step_limit(args), new.version)
    if ran.timed_out:
        return _auto_rollback(args, env, before, "the hooks rewrite ran past its time limit", held)
    code = ran.code
    if code == EXIT_NOT_RESTORED:
        _say(
            args,
            "the new ws is installed, and the hooks rewrite failed in a way that left files that were not put back "
            "(named above). Fix them as the message says, then run `ws doctor`",
        )
        return code
    if code != 0:
        _say(
            args,
            f"the new ws is installed, but the hooks were not all rewritten (exit {code}). Any that were not are as they "
            f"were and still work: each call prints a note. Fix what the line above says, then run `ws upgrade --hooks-only`; "
            f"to go back to ws {before}, run the installer with --version v{before}",
        )
        return EXIT_FAILED
    check = getattr(args, "check", None)
    if check:
        _say(args, f"checking the new release: {check}")
        try:
            ran = _step(args, "check", shlex.split(check), _step_limit(args))
        except (OSError, ValueError) as e:
            ran = Ran(EXIT_FAILED)
            log_step(args, env, "check", ran, 0.0)
            _say(args, f"the check could not start ({type(e).__name__})")
        if ran.code != 0:
            why = "the check ran past its time limit" if ran.timed_out else f"the check failed (exit {ran.code})"
            return _auto_rollback(args, env, before, why, held)
    after = {"policy_sha": _sha256(_policy_file(env))} if kept else {}
    record(env, "promote", before, new.version, **({"backup": str(kept)} if kept else {}), **after)
    broker_state = restart_broker(args, env, new)
    if args.json:
        output.result(
            {"from": before, "binary": "swapped", "hooks": "rewritten", "checked": bool(check), "broker": broker_state},
            status=output.OK if broker_state in BROKER_DONE else output.ATTENTION,
        )
    else:
        print(f"ws: upgraded from {before}; the hooks are rewritten" + _broker_note(broker_state))
    return 0 if broker_state in BROKER_DONE else EXIT_FAILED


def _broker_note(state: str) -> str:
    """The end of the last line: nothing when the broker needs nothing, else that it still runs the old code."""
    if state in BROKER_DONE:
        return ""
    return "; the broker is left running the old code" if state == "left running" else "; the broker restart failed"


def _sha256(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _auto_rollback(args: argparse.Namespace, env: Mapping[str, str], before: str, why: str, held: _Held) -> int:
    """A cut-over step went wrong after the swap began: put `before` back at once, with the policy, hooks and install
    record as they were (`held`)."""
    _say(args, f"{why}; going back to ws {before}")
    args.log_verb = "auto-rollback"
    code = reinstall(args, env, before, held)[0].code
    record(env, "auto-rollback", before, before if code == 0 else None)
    if code != 0:
        _say(
            args,
            f"going back failed (exit {code}). Run the installer with --version v{before}, then the hooks-only rewrite",
        )
        return EXIT_NOT_RESTORED
    _say(args, f"rolled back: ws {before} and its hooks are back")
    return EXIT_FAILED


def _promote_policy(args: argparse.Namespace, env: Mapping[str, str], target: str) -> _Held | None:
    """The policy as it was before the promote this rollback undoes, when the policy is still the one that promote left
    (a policy the person has edited since is theirs: it is left, and the way to fix it by hand is said). None when
    there is no such promote, backup or copy."""
    last = next((d for d in reversed(history(env)) if d.get("verb") == "promote"), None)
    if not last or _tag(str(last.get("from"))) != _tag(target) or not last.get("backup") or not last.get("policy_sha"):
        return None
    path = _policy_file(env)
    if _sha256(path) != last["policy_sha"]:
        _say(
            args,
            f"the policy at {path} changed after the upgrade, so it is left; the copy from before is in {last['backup']}",
        )
        return None
    return _Held.from_backup(Path(last["backup"]), path)


def _rollback(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    target = args.version or previous_release(env)
    if not target:
        raise WsError(
            "failed", "there is no earlier release on record to go back to", "name one with --rollback --version TAG"
        )
    before = __version__
    if args.dry_run:
        line = f"would reinstall ws {target} (now {before}) and rewrite the hooks for it"
        _say(args, line)
        log_step(args, env, "plan", Ran(0, tail=(f"ws: {line}",)), 0.0, sys.executable, __version__)
        return 0
    tree = selfcmd.unstable()
    if tree:
        raise WsError(
            "refused",
            f"this ws runs from a place the installer does not manage ({tree})",
            "install ws with install.sh first",
            EXIT_REFUSED,
        )
    ran, new = reinstall(args, env, target, _promote_policy(args, env, target))
    if ran.code != 0:
        record(env, "rollback", before, None)
        _say(
            args,
            f"the rollback to {target} failed (exit {ran.code}); ws and the hooks are as they were or as named above",
        )
        return EXIT_FAILED if ran.timed_out else ran.code
    record(env, "rollback", before, target)
    broker_state = restart_broker(args, env, new) if new is not None else "not running"
    if args.json:
        output.result(
            {"from": before, "to": target, "binary": "swapped", "hooks": "rewritten", "broker": broker_state},
            status=output.OK if broker_state in BROKER_DONE else output.ATTENTION,
        )
    else:
        print(f"ws: rolled back from {before} to {target}; the hooks are rewritten" + _broker_note(broker_state))
    return 0 if broker_state in BROKER_DONE else EXIT_FAILED


def cmd_upgrade(args: argparse.Namespace) -> int:
    env = os.environ
    why = refusal(env, sys.stdin.isatty() or args.dry_run) or agentsession.refusal(
        env, sys.stdin.isatty(), sys.stdout.isatty()
    )
    if why:
        raise WsError("refused", why, "run `ws upgrade` yourself, in a terminal of your own", EXIT_REFUSED)
    if args.hooks_only:
        args.log_verb = "hooks-only"
        return _hooks_logged(args, env)
    if getattr(args, "rollback", False):
        args.log_verb = "rollback"
        return _rollback(args, env)
    args.log_verb = "upgrade"
    return _both(args, env)


def register(sub) -> None:
    u = sub.add_parser(
        "upgrade",
        help="swap ws for a newer release and rewrite the runner's hooks as one step (from a terminal of your own)",
    )
    u.add_argument("--version", metavar="TAG", help="install this release (a tag like v0.2.0) instead of the latest")
    u.add_argument("--channel", choices=("stable", "rc", "edge"), help="the release channel to follow")
    u.add_argument("--installer", metavar="FILE", help="run this copy of install.sh instead of downloading it")
    u.add_argument("--hooks-only", action="store_true", help="rewrite the hooks only; a newer ws is already installed")
    u.add_argument("--dry-run", action="store_true", help="show what would change and change nothing")
    u.add_argument("--rollback", action="store_true", help="put the previous release back (or --version TAG)")
    u.add_argument("--check", metavar="COMMAND", help="run this after the swap; a non-zero exit rolls the swap back")
    u.add_argument("--step-timeout", type=float, metavar="SECONDS", help="limit for the hooks step and the check (120)")
    u.add_argument("--install-timeout", type=float, metavar="SECONDS", help="limit for the installer (600)")
    u.set_defaults(func=cmd_upgrade)
