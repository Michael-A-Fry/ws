"""The sandbox as the real wall: `ws gate launch --runner R -- argv` starts a runner inside an operating-system sandbox
that forbids writing ws's own files and reading the deny_read paths, whatever program the runner starts.

Why a wall at all. The command matcher (ws.policy) and the runner's deny rules read text. A program that builds a path
at run time, an interpreter, a script written then run, or the version-control tool restoring a file all do their harm
below the text. A sandbox acts on the system calls themselves, so none of those get through: the write fails with a
read-only error and the read finds nothing. The matcher stays as the first, friendlier layer (it explains itself); this
is the layer that does not depend on spelling. Claude Code has its own sandbox, switched on by the settings the
claude-code adapter writes; this module is for the runners that do not (OpenCode).

Decisions, and why:
  * Write wall, not a read-only root. The runner has to work: it writes its project, its caches, /tmp. So the view of the
    machine is "as today" (a bind of `/`), with ro-bind layered over `$WS_HOME` and every protected path (the policy,
    the generated gate folder, ws's control files). A read-only mount is enforced by the kernel for every process in
    the sandbox, including the runner's own tool and plugin code, which a path rule in the runner's config is not.
  * Two holes in that, on purpose: `$WS_HOME/approvals/pending` and `$WS_HOME/sessions` stay writable. The gate hook and
    the session nudge run inside the wall (the plugin lives in the runner's process) and must be able to record a
    pending approval and a release marker. Neither grants anything: see ws.approvals for why a forged pending record is
    harmless. `approvals/granted`, the policy, the secrets and the jobs stay read-only.
  * A third, for runners that keep their own files in the generated folder. OpenCode writes `.gitignore`, `package.json`
    and `node_modules` (its plugin package) into OPENCODE_CONFIG_DIR at start and will not run if it cannot (seen on
    1.19.4). So that folder is writable and the files that carry the gate (`opencode.json`, `plugins/`) are locked
    read-only inside it (the adapter's WALL_PROTECT). What a session can still do there is plant a file the runner
    loads next time, which runs inside the wall again and cannot take away the shim's deny.
  * `--dev-bind /dev /dev` after the bind of `/`. A bind inside a user namespace marks the nested /dev mounts `nodev`, and
    OpenCode's shell tool then fails to start a child ("PermissionDenied: ChildProcess.spawn", seen on 1.19.4) because it
    cannot open /dev/null. This gives the runner the host's /dev as it had before; it does not widen the write wall.
  * Read denial by hiding. Each deny_read rule that names a folder (`~/.ssh/**`) becomes an empty tmpfs over it; a file
    (`~/.aws/credentials`) becomes /dev/null. Only rules with no wildcard other than a trailing `**` can be hidden this
    way, and only paths that exist (bubblewrap cannot mount over nothing); the matcher still covers the rest.
  * Fail closed. No bubblewrap (or no sandbox-exec), or a bubblewrap that cannot start (unprivileged user namespaces
    switched off), refuses to launch with the install line. `--no-sandbox` starts the runner bare and prints what that
    gives up, so skipping the wall is a visible choice, not a silent fallback.
  * No `--unshare-net`: the runner needs the model's API. Host filtering is Claude Code's sandbox proxy only (policy
    `allow_hosts`); here the network is as today. That is a limit, written in ws.policy's docstring.
  * `--new-session` (no controlling terminal, which closes the terminal-injection escape bubblewrap warns of) only when
    stdin is not a terminal: an interactive runner needs its terminal for Ctrl-C and window size.
  * macOS: `sandbox-exec` with a generated profile, `(allow default)` then `(deny file-write*)` over the protected paths
    and `(deny file-read*)` over the hidden ones, then the two writable holes re-allowed (the last matching rule wins).
    sandbox-exec is deprecated by Apple but still ships and is what Claude Code and Codex use. The profile text is unit
    tested; no CI job runs it.
"""

from __future__ import annotations

import argparse
import os
import platform
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ws import adapters, policy
from ws.approvals import ENV_MARKER, approvals_dir

INSTALL = {
    "linux": "install bubblewrap: `sudo apt install bubblewrap` (Debian, Ubuntu) or `sudo dnf install bubblewrap` (Fedora)",
    "darwin": "sandbox-exec ships with macOS (/usr/bin/sandbox-exec) and was not found; the system may be damaged",
}
NO_SANDBOX_NOTE = (
    "ws: --no-sandbox: starting {runner} with NO wall. The deny rules and the hook still apply, but anything they do not "
    "see (`python3 -c`, a path built by a command, a script it writes then runs, git restoring a protected file) can "
    "write ws's own files, including the policy, and read the denied folders."
)


class SandboxError(Exception):
    pass


def _system() -> str:
    return platform.system().lower()


def writable_holes(home: Path) -> list[Path]:
    """The folders inside `home` that the wall leaves writable (see the module docstring)."""
    return [approvals_dir(home) / "pending", home / "sessions"]


def hidden_paths(pol: policy.Policy, env: dict | None = None) -> list[str]:
    """Absolute paths that exist and that deny_read names without wildcards, for hiding inside the wall."""
    home = (env or os.environ).get("HOME") or str(Path.home())
    out = []
    for p in policy.sandbox_read_paths(pol):
        p = home + p[1:] if p == "~" or p.startswith("~/") else p
        if p.startswith("/") and os.path.lexists(p):
            out.append(os.path.realpath(p) if os.path.islink(p) else p)
    return list(dict.fromkeys(out))


@dataclass(frozen=True)
class Plan:
    """What the wall does to the filesystem, in the order it is applied (a later mount wins over an earlier one)."""

    home: Path
    protect: tuple[str, ...] = ()  # read-only, with `home`
    scratch: tuple[str, ...] = ()  # writable again, though under a read-only path (see the module docstring)
    lock: tuple[str, ...] = ()  # read-only again, inside a scratch folder
    hidden: tuple[str, ...] = ()  # deny_read paths, made empty

    def read_only(self) -> list[str]:
        return list(dict.fromkeys([str(self.home), *self.protect]))

    def writable(self) -> list[str]:
        return [*(str(h) for h in writable_holes(self.home)), *self.scratch]


def bwrap_argv(argv: list[str], plan: Plan, tty: bool) -> list[str]:
    out = ["bwrap", "--bind", "/", "/", "--dev-bind", "/dev", "/dev", "--die-with-parent"]  # see the module docstring
    if not tty:
        out.append("--new-session")
    for p in plan.read_only():
        if os.path.lexists(p):  # bubblewrap cannot bind what is not there
            out += ["--ro-bind", p, p]
    for p in plan.writable():
        if os.path.lexists(p):
            out += ["--bind", p, p]
    for p in plan.lock:
        if os.path.lexists(p):
            out += ["--ro-bind", p, p]
    for p in plan.hidden:
        out += ["--ro-bind", "/dev/null", p] if os.path.isfile(p) else ["--tmpfs", p]
    return [*out, "--", *argv]


def _sbpl(path: str) -> str:
    return '"' + str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_exec_profile(plan: Plan) -> str:
    """The macOS profile. Paths are resolved first: /var is /private/var there, and the profile matches real paths."""
    lines = ["(version 1)", "(allow default)"]
    lines += [f"(deny file-write* (subpath {_sbpl(p)}))" for p in plan.read_only()]
    lines += [f"(allow file-write* (subpath {_sbpl(p)}))" for p in plan.writable()]
    lines += [f"(deny file-write* (subpath {_sbpl(p)}))" for p in plan.lock]
    for p in plan.hidden:
        kind = "literal" if os.path.isfile(p) else "subpath"
        lines.append(f"(deny file-read* ({kind} {_sbpl(p)}))")
    return "\n".join(lines) + "\n"


def wrap(
    argv: list[str],
    pol: policy.Policy,
    home: Path,
    protect: list[str],
    *,
    scratch: list[str] = (),
    lock: list[str] = (),
    system: str | None = None,
    which=shutil.which,
    probe=None,
    tty: bool = False,
    env: dict | None = None,
) -> list[str]:
    """`argv` inside the sandbox for this OS. Raises SandboxError (with the install line) when it cannot be built."""
    system = system or _system()
    plan = Plan(home, tuple(protect), tuple(scratch), tuple(lock), tuple(hidden_paths(pol, env)))
    if system == "linux":
        if not which("bwrap"):
            raise SandboxError(f"bubblewrap (bwrap) is not installed; {INSTALL['linux']}")
        if not (probe or _bwrap_starts)():
            raise SandboxError(
                "bubblewrap is installed but cannot start here (unprivileged user namespaces are probably off: "
                "see `sysctl kernel.unprivileged_userns_clone` and kernel.apparmor_restrict_unprivileged_userns)"
            )
        make_holes(home)
        return bwrap_argv(argv, plan, tty)
    if system == "darwin":
        if not which("sandbox-exec"):
            raise SandboxError(INSTALL["darwin"])
        make_holes(home)
        return ["sandbox-exec", "-p", sandbox_exec_profile(plan), *argv]
    raise SandboxError(
        f"no sandbox for {system or 'this system'}; ws supports Linux (bubblewrap) and macOS (sandbox-exec)"
    )


def make_holes(home: Path) -> None:
    for h in writable_holes(home):
        h.mkdir(mode=0o700, parents=True, exist_ok=True)


def _bwrap_starts() -> bool:
    try:
        r = subprocess.run(
            ["bwrap", "--bind", "/", "/", "--", "true"],
            capture_output=True,
            timeout=20,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


# --- ws gate launch ------------------------------------------------------------------------------------------------


def launch_env(env: dict) -> dict:
    return {**env, ENV_MARKER: "1"}


def cmd_launch(args: argparse.Namespace) -> int:
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        print(
            "ws: give the runner's command after --, e.g. `ws gate launch --runner opencode -- opencode`",
            file=sys.stderr,
        )
        return 1
    if args.runner == "claude-code":
        print(
            "ws: Claude Code has its own sandbox: `ws gate generate --runner claude-code` already writes it into the "
            "settings, so start claude with `--settings` as the hint says; there is nothing to wrap",
            file=sys.stderr,
        )
        return 1
    policy_file = Path(args.policy).expanduser().absolute() if args.policy else policy.policy_path()
    try:
        pol = policy.load(policy_file)
    except policy.PolicyError as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    home = policy_file.parent  # the gate reads and writes its approvals beside the policy it was given
    env = os.environ
    protect = [
        str(policy_file),
        *policy.control_paths(home),
        *(str(Path(p).expanduser().absolute()) for p in args.protect or []),
    ]
    scratch: list[str] = []
    lock: list[str] = []
    adapter = adapters.get(args.runner)
    config_dir = env.get(getattr(adapter, "CONFIG_DIR_ENV", ""), "")
    if config_dir:  # the generated folder the runner was told to use
        base = Path(config_dir).expanduser().absolute()
        kept = getattr(adapter, "WALL_PROTECT", None)
        if kept:  # the runner keeps its own housekeeping here, so only the gate's own files are locked
            scratch.append(str(base))
            lock += [str(base / n) for n in kept]
        else:
            protect.append(str(base))
    if args.no_sandbox:
        print(NO_SANDBOX_NOTE.format(runner=args.runner), file=sys.stderr)
        cmd = argv
    else:
        try:
            cmd = wrap(argv, pol, home, protect, scratch=scratch, lock=lock, tty=sys.stdin.isatty())
        except SandboxError as e:
            print(
                f"ws: refusing to start {args.runner} without its wall: {e}\n(or `--no-sandbox`, which has none)",
                file=sys.stderr,
            )
            return 1
    try:
        os.execvpe(cmd[0], cmd, launch_env(dict(env)))
    except OSError as e:
        print(f"ws: could not start {shlex.join(cmd[:1])}: {e}", file=sys.stderr)
        return 127
    return 0  # not reached


def add_launch(gs, names: list[str]) -> None:
    p = gs.add_parser(
        "launch",
        help="start a runner inside the sandbox wall (bubblewrap on Linux, sandbox-exec on macOS); fails closed",
    )
    p.add_argument("--runner", required=True, choices=names)
    p.add_argument("--policy", metavar="FILE", help="policy file (default $WS_HOME/policy.toml)")
    p.add_argument("--protect", action="append", metavar="PATH", help="another file or folder to make read-only")
    p.add_argument("--no-sandbox", action="store_true", help="start the runner with no wall (says what that gives up)")
    p.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the runner's command line")
    p.set_defaults(func=cmd_launch)
