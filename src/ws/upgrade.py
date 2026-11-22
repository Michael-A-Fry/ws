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
back between the old installer and the old hooks step. The same files are copied to `<state>/upgrade-backups/` before the
installer runs, and a `swap-start` history line names the folder and the policy's hash before the upgrade, so a process
that dies mid-swap still leaves the way back (`previous_release` reads that line too). A folder a promote or an unfinished
swap names is never pruned; a swap whose installer failed removes its own. A manual `--rollback` puts the policy back from
the copy while the policy is still as the upgrade left it; one the person has edited since keeps their edits and loses only
the guards the upgrade added; and if even that can't be done safely the rollback is refused before anything is swapped.

Unattended. `ws upgrade --unattended` is the entry of a systemd user service, never of a person or a session: it is refused
with any sign of a session, without `INVOCATION_ID`, or when its parent is not the user manager (`unattended_refusal`), and
that replaces the terminal test on this path only. It takes the one request in `request_path` (a tag or `rollback`, read as
data and consumed; `ws.upgradeproof.parse_request`), refuses a tag at or below the installed version and a second upgrade
within an hour, proves the release itself from the installed wheel's trust root (`ws.upgradeproof.verify_release`: signed
tag, hashes and cosign bundles, green checks, no high advisory), waits up to 30 minutes for nothing to be running
(`idle_reason`; a job is never stopped), installs the installer it proved with no mirror or skip-the-checks variable in its
environment (`MIRROR_VARS`), and runs the acceptance checks of `ws.upgradecheck` as the check step: any problem is the
automatic rollback above. `rollback` goes to the release before the last promote and nowhere else. Every outcome is a row
in the release log. The installer a person's `ws upgrade` downloads is the release's own `install.sh` asset (or the file
at that tag), never a branch (`installer_urls`).

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
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import urllib.request
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ws import (
    __version__,
    agentsession,
    approvals,
    install,
    jsonl,
    output,
    paths,
    policy,
    selfcmd,
    upgradecheck,
    upgradeproof,
    userconfig,
)
from ws import version as wsversion
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
REQUEST_WINDOW_S = 3600  # an unattended upgrade is taken at most once in this time (a rollback is not counted)
IDLE_WAIT_S, IDLE_EVERY_S = (
    1800.0,
    30.0,
)  # how long an unattended upgrade waits for nothing to run, and how often it looks
# what an installer reads to change where a release comes from or whether it is checked; an unattended swap runs it without
MIRROR_VARS = (
    "WS_RELEASE_BASE_URL",
    "WS_SKIP_VERIFY",
    "UV_INDEX_URL",
    "UV_DEFAULT_INDEX",
    "UV_EXTRA_INDEX_URL",
    "UV_FIND_LINKS",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
    "PIP_FIND_LINKS",
)
DEFAULT_REPO = "Michael-A-Fry/ws"  # where releases come from when the installed wheel carries no trust root
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


def _backup_root(env: Mapping[str, str]) -> Path:
    return paths.state_dir(dict(env)) / BACKUPS_DIR


def _is_backup(folder: Path) -> bool:
    return folder.name.isdigit() and folder.is_dir() and not folder.is_symlink()


def _age(folder: Path) -> int:
    return int(folder.name)


def _named_backups(env: Mapping[str, str]) -> set[str]:
    """The folder names a history line that still matters names: every promote's, and the unfinished swap's."""
    last = _last_swap(env)
    lines = [d for d in history(env) if d.get("verb") == "promote"] + ([last] if last else [])
    return {Path(str(d["backup"])).name for d in lines if d.get("backup")}


def _backup_dir(env: Mapping[str, str], named: object) -> Path | None:
    """The folder a history line names, when it is one `keep` made: a number for a name, straight under
    `<state>/upgrade-backups/`, not a link. Anything else in a history line is not trusted as a place to read from."""
    if not isinstance(named, str) or not named:
        return None
    folder = Path(named)
    return folder if folder.parent == _backup_root(env) and _is_backup(folder) else None


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
        """Copy these files into a folder of their own under `<state>/upgrade-backups/` (a number for a name), with an
        `index.json` saying which copy is which file, and return the folder. A manual rollback days later reads the policy
        back from it (`_rollback`). Older folders beyond the newest five go, except any the history still names: a promote's,
        and the one a swap that never finished (a crash) left. None when it could not be written: the in-memory copy
        still serves the automatic rollback."""
        root = _backup_root(env)
        folder = root / str(time.time_ns())
        try:
            root.mkdir(parents=True, mode=0o700, exist_ok=True)
            folder.mkdir(mode=0o700)
            index = {}
            for n, (p, data) in enumerate(self.files.items()):
                index[str(p)] = None if data is None else str(n)
                if data is not None:
                    (folder / str(n)).write_bytes(data)
            (folder / "index.json").write_text(json.dumps(index), encoding="utf-8")
            named = _named_backups(env)
            old = sorted((d for d in root.iterdir() if _is_backup(d) and d != folder and d.name not in named), key=_age)
            for d in old[:-BACKUPS_KEPT]:
                shutil.rmtree(d, ignore_errors=True)
        except OSError:
            shutil.rmtree(folder, ignore_errors=True)
            return None
        return folder

    @classmethod
    def from_backup(cls, folder: Path, only: Path, sha256: object) -> _Held | None:
        """What `keep` wrote for the one file `only`, or None when the folder has no copy of it or the copy is not the one
        the history recorded (`sha256`: the hash of the file as it was before the upgrade)."""
        try:
            name = json.loads((folder / "index.json").read_text(encoding="utf-8")).get(str(only))
            if not isinstance(name, str) or not name.isdigit():
                return None
            data = (folder / name).read_bytes()
        except (OSError, ValueError, AttributeError):
            return None
        return cls({only: data}) if isinstance(sha256, str) and hashlib.sha256(data).hexdigest() == sha256 else None

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


def installer_urls(version: str | None) -> list[str]:
    """Where the installer for `version` is read from, best first: the release's own `install.sh` asset (the newest release
    when no version is named), then the file at that tag in the repository. Never a branch: a branch moves, a tag does not.
    The repository is the one the installed wheel's stamp names (`ws.version.trust_root`), else `DEFAULT_REPO`."""
    root = wsversion.trust_root()
    base = f"https://github.com/{root['repo'] if root else DEFAULT_REPO}"
    if not version:
        return [f"{base}/releases/latest/download/install.sh"]
    tag = _tag(version)
    return [
        f"{base}/releases/download/{tag}/install.sh",
        f"{base.replace('//github.com', '//raw.githubusercontent.com')}/{tag}/install.sh",
    ]


def fetch_installer(dest: Path, version: str | None = None) -> Path:
    """Download install.sh into `dest` (a folder of its own) and return the file. https only; a file that does not look like
    the installer is not kept. The unattended service never comes here: it installs the file it has proved."""
    data, error = b"", None
    for url in installer_urls(version):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read(_DOWNLOAD_LIMIT + 1)
            break
        except OSError as e:
            error = e
    else:
        raise WsError(
            "failed",
            f"could not download the installer ({type(error).__name__})",
            "check the network, or pass --installer with a copy of install.sh (docs/how-to/install.md)",
        ) from error
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
    if getattr(args, "unattended", False):  # the installer runs without any way to move its source or skip its checks
        argv = ["env", *(w for name in MIRROR_VARS for w in ("-u", name)), *argv]
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
    script = Path(args.installer).expanduser() if args.installer else fetch_installer(scratch, args.version)
    if not script.is_file():
        raise WsError("failed", f"{script} is not a file", "pass --installer with a copy of install.sh")
    limit = getattr(args, "install_timeout", None) or INSTALL_TIMEOUT_S
    return _step(args, "installer", _installer_argv(script, args), limit)


# ---- history, health check, rollback ----------------------------------------------------------------------------------------


def history_path(env: Mapping[str, str]) -> Path:
    return paths.state_dir(dict(env)) / "upgrade-history.jsonl"


def record(env: Mapping[str, str], verb: str, frm: str, to: str | None, **extra: object) -> None:
    """One line of history, in one file that is never rotated (the way back must stay on record). `extra` is a swap's
    `backup` (the folder `_Held.keep` wrote) and `policy_before_sha`; a promote adds `policy_sha` (the policy as the
    upgrade left it) and `added_guards` (what the rewrite put in `[guards] on`). A history that cannot be written never
    stops an upgrade."""
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


def _last_swap(env: Mapping[str, str]) -> dict | None:
    """The newest line that says a release was put in and nothing has put the old one back: a promote, or a `swap-start`
    that nothing closed (the process died between the installer and the promote, so the installed release is unknown and
    the one it left is the way back). A swap that was abandoned (its installer failed, nothing changed), an automatic
    rollback or a rollback that worked closes the swap before it; one that failed put nothing back."""
    skip = False
    for d in reversed(history(env)):
        verb = d.get("verb")
        if verb == "promote":
            return d
        if verb == "swap-abandoned":
            skip = True
        elif verb == "swap-start":
            if not skip:
                return d
            skip = False
        elif verb in ("rollback", "auto-rollback") and d.get("to"):
            return None
    return None


def previous_release(env: Mapping[str, str]) -> str | None:
    """The release the last swap came from (see `_last_swap`), unless a rollback has put it back since."""
    last = _last_swap(env)
    return last.get("from") if last else None


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
    policy_before = held.files.get(_policy_file(env))
    # the way back is on record before the installer runs: a process that dies after it still has a line that names the
    # release it left and the copy of the policy from before, and `previous_release` and a rollback read it
    record(
        env,
        "swap-start",
        before,
        _tag(args.version) if args.version else None,
        backup=str(kept or ""),
        policy_before_sha=_sha256_of(policy_before),
    )
    with tempfile.TemporaryDirectory(prefix="ws-upgrade-") as scratch:
        os.chmod(scratch, 0o700)
        ran = swap(args, Path(scratch))
    if ran.timed_out:
        return _auto_rollback(args, env, before, "the installer ran past its time limit", held)
    code = ran.code
    if code != 0:
        _say(args, f"the installer failed (exit {code}); you still have ws {before} and its hooks, unchanged")
        if kept:  # nothing changed: the copy is of no use, and a run of failed tries must not push out a promote's
            shutil.rmtree(kept, ignore_errors=True)
        record(env, "swap-abandoned", before, None)
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
    check_fn = getattr(args, "check_fn", None)
    if check_fn:  # the unattended service's own checks (`ws.upgradecheck.acceptance`), run in this process
        t = time.monotonic()
        problems = check_fn(new)
        tail = tuple(problems[:TAIL_LINES])
        log_step(args, env, "check", Ran(1 if problems else 0, tail=tail), time.monotonic() - t)
        for line in problems:
            _say(args, f"check: {line}")
        if problems:
            return _auto_rollback(args, env, before, f"the check found {len(problems)} problem(s)", held)
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
    now = _policy_file(env)
    added = [
        g for g in DEFAULT_GUARDS if g in (_guards_on(_read(now)) or []) and g not in (_guards_on(policy_before) or [])
    ]
    saved = {"backup": str(kept), "policy_before_sha": _sha256_of(policy_before)} if kept else {}
    record(env, "promote", before, new.version, **saved, policy_sha=_sha256(now), added_guards=added)
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


def _read(path: Path) -> bytes | None:
    try:
        return path.read_bytes()
    except OSError:
        return None


def _sha256_of(data: bytes | None) -> str:
    return "" if data is None else hashlib.sha256(data).hexdigest()


def _sha256(path: Path) -> str:
    return _sha256_of(_read(path))


def _guards_on(raw: bytes | None) -> list[str] | None:
    """The `[guards] on` list of a policy's bytes, or None when it is missing, unreadable or not a list of names."""
    try:
        guards = tomllib.loads((raw or b"").decode("utf-8")).get("guards")
    except ValueError:  # bad TOML and bad UTF-8 both
        return None
    on = guards.get("on") if isinstance(guards, dict) else None
    return on if isinstance(on, list) and all(isinstance(x, str) for x in on) else None


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


def _policy_for_rollback(args: argparse.Namespace, env: Mapping[str, str], target: str) -> _Held | None:
    """The policy the release this rollback returns to can load, or None when nothing needs to change (no swap on record
    from `target`, or the policy lacks nothing the upgrade added).

    - The policy is as the upgrade left it (or as it was): the copy from before goes back, byte for byte.
    - The person edited it since, or the upgrade died before its promote line: their edits stay and only the guards the
      upgrade added are taken out of `[guards] on` (the release going back may not know them, and would refuse the whole
      policy: the gate then fails closed for every session, C706).
    - The guards can't be taken out safely (or the copy is missing and the history does not say what was added): the
      rollback is refused before anything is swapped, and says what to change by hand.
    A copy that is missing, not under the backups folder, or not the one the history recorded is said so, loudly."""
    last = _last_swap(env)
    if not last or _tag(str(last.get("from"))) != _tag(target):
        return None
    path = _policy_file(env)
    folder = _backup_dir(env, last.get("backup"))
    copy = _Held.from_backup(folder, path, last.get("policy_before_sha")) if folder else None
    if copy is None and last.get("policy_before_sha"):
        _say(
            args,
            f"WARNING: the copy of the policy from before the upgrade is missing or is not the one that was recorded "
            f"({last.get('backup') or 'no folder named'}); ws can only take out what the upgrade added",
        )
    if copy is not None and _sha256(path) in (last.get("policy_sha"), last.get("policy_before_sha")):
        return copy
    added = last.get("added_guards")
    if not isinstance(added, list):  # no promote line (the upgrade died): what the copy lacked is what was added
        was = _guards_on(copy.files[path]) if copy is not None else None
        added = [g for g in DEFAULT_GUARDS if g not in was] if was is not None else None
    if added is None:
        raise WsError(
            "refused",
            f"the policy at {path} may list guards the release you are going back to does not know, and ws has no copy of "
            "the policy from before the upgrade to put back",
            f"take what the upgrade added ({', '.join(DEFAULT_GUARDS)}) out of [guards] on in that file, then run this again",
            EXIT_REFUSED,
        )
    text, took = (_read(path) or b"").decode("utf-8", "replace"), []
    for name in added:
        if name not in (_guards_on(text.encode()) or []):
            continue
        more = policy.remove_from_guards_on(text, str(name))
        if more is None:
            raise WsError(
                "refused",
                f"the policy at {path} was changed after the upgrade and lists {name}, which the release you are going "
                "back to may not know; ws could not take it out safely",
                f"take {name} out of [guards] on in that file, then run this again",
                EXIT_REFUSED,
            )
        text, took = more, [*took, str(name)]
    if not took:
        return None
    _say(
        args,
        f"the policy at {path} changed after the upgrade: your edits stay, and only {', '.join(took)} (added by the "
        "upgrade) is taken out of [guards] on",
    )
    return _Held({path: text.encode("utf-8")})


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
    ran, new = reinstall(args, env, target, _policy_for_rollback(args, env, target))
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


# ---- unattended: the service's own upgrade ----------------------------------------------------------------------------------


def request_path(env: Mapping[str, str]) -> Path:
    """Where the one-line request file is dropped: outside every protected root on purpose (the request is a wish)."""
    base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME") or os.path.expanduser("~")) / ".local" / "state")
    return Path(base) / "ws-upgrade" / "request.json"


def _parent_comm(pid: int) -> str | None:
    try:
        return Path(f"/proc/{pid}/comm").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def unattended_refusal(env: Mapping[str, str], parent_comm: str | None = None) -> str | None:
    """Why `--unattended` is refused, or None: no sign of a session, a systemd invocation (`INVOCATION_ID`), and the user
    manager as the parent. It replaces the terminal test on this path only; everything else still fails towards refusing."""
    signs = session_signs(env)
    if signs:
        return f"ws upgrade --unattended is for the service, not for a session ({signs[0]} is set here)"
    if not env.get("INVOCATION_ID"):
        return "ws upgrade --unattended runs only from its systemd unit (no INVOCATION_ID here)"
    comm = parent_comm if parent_comm is not None else _parent_comm(os.getppid())
    if comm != "systemd":
        return f"ws upgrade --unattended runs only from its systemd unit (the parent here is {comm or 'unknown'})"
    return None


def idle_reason(env: Mapping[str, str]) -> str | None:
    """Why a swap can't start now, or None when nothing is running: a queued or running job (`queue.Ledger.receipts`) or a
    broker with a lease out. A job is never stopped to make room, and what can't be read counts as busy."""
    from ws import queue

    try:
        active = queue.Ledger(paths.state_dir(dict(env))).receipts(active_only=True)
    except (queue.StoreError, OSError, sqlite3.Error) as e:  # a store that cannot be read is no evidence of idle
        return f"the queue could not be read ({type(e).__name__})"
    if active:
        return f"{len(active)} queued or running job(s)"
    try:
        leases = ask_broker(broker_socket(env)).status.get("leases")
    except (OSError, ValueError):
        return None  # no broker answering: nothing holds a lease
    return None if leases == 0 else f"the broker has {leases} lease(s) open"


def wait_idle(env: Mapping[str, str], sleep=time.sleep, now=time.monotonic) -> None:
    """Poll `idle_reason` every `IDLE_EVERY_S` seconds for up to `IDLE_WAIT_S`; raise `Refused` with the last reason."""
    end = now() + IDLE_WAIT_S
    while (why := idle_reason(env)) is not None:
        if now() >= end:
            raise upgradeproof.Refused(f"still busy after {IDLE_WAIT_S / 60:.0f} minutes ({why}); nothing was changed")
        sleep(IDLE_EVERY_S)


def _recent_promote(env: Mapping[str, str], now: float) -> bool:
    """Did the service swap (and keep the result, or roll it back) within `REQUEST_WINDOW_S` of `now`? A refused request
    changed nothing and is not counted."""
    from datetime import datetime

    for row in reversed(upgradecheck.last_rows(env)):
        if row["verb"] in ("promote", "auto-rollback") and row["result"] in ("ok", "rolled-back"):
            try:
                return now - datetime.fromisoformat(row["at"]).timestamp() < REQUEST_WINDOW_S
            except ValueError:
                return False
    return False


def _log(args: argparse.Namespace, env: Mapping[str, str], t0: float, verb: str, to: str | None, result: str, why: str):
    upgradecheck.log_row(env, verb, __version__, to, time.monotonic() - t0, result, why)
    _say(args, " ".join(w for w in (verb, to or "", f"{result}:" if why else result, why) if w))


def cmd_unattended(args: argparse.Namespace, env: Mapping[str, str], source=None, sleep=time.sleep) -> int:
    """`ws upgrade --unattended`: take the one request in `request_path`, prove it, wait for idle, swap, check, and roll
    back on any failure. The request is read as data and consumed; every outcome is a row in the release log."""
    why = unattended_refusal(env) or agentsession.refusal(env, False, False)
    if why:
        raise WsError("refused", why, "it is started by `ws-upgrade.service`, not by hand", EXIT_REFUSED)
    args.log_verb = "unattended"
    t0 = time.monotonic()
    req = request_path(env)
    try:
        text = req.read_text(encoding="utf-8", errors="replace")
        req.unlink(missing_ok=True)  # taken: the unit's path watch must not fire for it again
    except OSError:
        _say(args, "no request to act on")
        return 0
    want = upgradeproof.parse_request(text)
    if want is None:
        _log(args, env, t0, "request", None, "dropped", "not a release tag or the word rollback")
        return 0
    rolling = want == upgradeproof.ROLLBACK
    try:
        return (
            _unattended_rollback(args, env, t0) if rolling else _unattended_upgrade(args, env, want, t0, source, sleep)
        )
    except upgradeproof.Refused as r:
        _log(args, env, t0, "rollback" if rolling else "promote", None if rolling else want, "refused", str(r))
        return EXIT_REFUSED


def _unattended_upgrade(args, env, tag: str, t0: float, source, sleep) -> int:
    upgradeproof.refuse_old(tag, __version__)
    if _recent_promote(env, time.time()):
        raise upgradeproof.Refused(f"an upgrade was done in the last {REQUEST_WINDOW_S // 60} minutes; one at a time")
    root = wsversion.trust_root()
    if root is None:
        raise upgradeproof.Refused(
            "this ws carries no trust root (it was built before the release workflow stamped one)"
        )
    wait_idle(env, sleep)
    src = source or upgradeproof.GitHub(root["repo"])
    with tempfile.TemporaryDirectory(prefix="ws-upgrade-") as scratch:
        os.chmod(scratch, 0o700)
        installer = upgradeproof.verify_release(tag, root, src, Path(scratch), env)
        before = upgradecheck.snapshot(env)
        args.installer, args.version, args.channel, args.log_verb = str(installer), tag, None, "upgrade"
        session_check = getattr(args, "session_check", None)
        args.check_fn = lambda new: upgradecheck.acceptance(env, tag, new.argv, before, session_check)
        mark = len(history(env))
        code = _both(args, env)
    last = history(env)[-1] if len(history(env)) > mark else {}
    verb = last.get("verb") or "promote"
    _log(args, env, t0, verb, tag, "ok" if code == 0 else "failed" if verb == "promote" else "rolled-back", "")
    return code


def _unattended_rollback(args, env, t0: float) -> int:
    """`rollback` goes to the release before the last promote and nowhere else; a request never names a tag."""
    target = previous_release(env)
    if target is None:
        raise upgradeproof.Refused("there is no earlier release on record to go back to")
    args.version, args.rollback, args.log_verb = None, True, "rollback"
    code = _rollback(args, env)
    problems = upgradecheck.acceptance(env, target, installed_ws(env).argv, {}) if code == 0 else []
    _log(args, env, t0, "rollback", target, "ok" if code == 0 and not problems else "failed", "; ".join(problems))
    return code or (EXIT_FAILED if problems else 0)


def cmd_upgrade(args: argparse.Namespace) -> int:
    env = os.environ
    if getattr(args, "unattended", False):
        return cmd_unattended(args, env)
    # the hooks step of an unattended swap is this command in a child of the service: no terminal, the service's invocation
    child = bool(args.hooks_only and env.get(STEP_ENV) and env.get("INVOCATION_ID"))
    why = refusal(env, sys.stdin.isatty() or args.dry_run or child) or agentsession.refusal(
        env, sys.stdin.isatty() or child, sys.stdout.isatty() or child
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
    u.add_argument(
        "--unattended", action="store_true", help=argparse.SUPPRESS
    )  # the systemd unit's entry (ws.upgradeproof)
    u.add_argument(
        "--session-check", metavar="COMMAND", help="after an unattended swap, run this as well; non-zero rolls it back"
    )
    u.add_argument("--check", metavar="COMMAND", help="run this after the swap; a non-zero exit rolls the swap back")
    u.add_argument("--step-timeout", type=float, metavar="SECONDS", help="limit for the hooks step and the check (120)")
    u.add_argument("--install-timeout", type=float, metavar="SECONDS", help="limit for the installer (600)")
    u.set_defaults(func=cmd_upgrade)
