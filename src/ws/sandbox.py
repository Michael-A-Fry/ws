"""The sandbox as the real wall: `ws gate launch --runner R -- argv` starts a runner inside an operating-system sandbox
that forbids writing ws's own files and reading the deny_read paths, whatever program the runner starts.

Why a wall at all. The command matcher (ws.policy) and the runner's deny rules read text. A program that builds a path
at run time, an interpreter, a script written then run, or the version-control tool restoring a file all do their harm
below the text. A sandbox acts on the system calls themselves, so none of those get through: the write fails with a
read-only error and the read finds nothing. The matcher stays as the first, friendlier layer (it explains itself); this
is the layer that does not depend on spelling. Claude Code has its own sandbox, switched on by the settings the
claude-code adapter writes; this module is for the runners that do not (OpenCode).

Why not Anthropic's sandbox-runtime (`srt`, npm @anthropic-ai/sandbox-runtime 0.0.81, Apache-2.0, 20 Oct 2026). It was
installed and run, and it does stop the escapes below (seccomp on Unix sockets, pid namespace, new session). It was not
adopted because it takes the network namespace away from the whole runner: every connection must go through its proxy,
and its own NO_PROXY keeps loopback off the proxy. A local model server (Ollama, vLLM, the e2e stub) then cannot be
reached at all, which breaks "any provider, including local". With no `allow_hosts` in the policy it also has no way to
say "network as today". It is a 0.0.x research preview, and a Node dependency to pin on every machine. Its design is
copied where it fits (the points below); Claude Code, which has the same engine built in, keeps using its own.

Decisions, and why:
  * A write allow-list, not a deny-list. Root is read-only; writable are the project folder (where the runner is started;
    never the home folder or above it), a private tmp the launcher makes and points TMPDIR at, the runner's own state
    and cache folders (OpenCode: the `opencode` folders under XDG data, state and cache; found by running it read-only
    and adding what it needed), `allow_write` from the policy and `--allow-write`. A deny-list left everything else
    writable: `~/.bashrc`, `~/.local/bin`, `~/.config/systemd/user` and ws's own installed source, each of which runs
    outside the wall at the next shell or the next hook. ws's own files (`$WS_HOME`, the policy, the generated folder)
    are read-only by default and stay so even where one sits inside the project. The project's `.git/hooks`,
    `.git/config`, `.claude` and `.opencode` are read-only inside it (Claude Code's sandbox protects the same). The
    price: a program that writes a hard-coded path outside those (not TMPDIR) fails; the policy's `allow_write` is how
    a person lets one through. A read-only mount is enforced by the kernel for every process in the sandbox, including the
    runner's own tool and plugin code, which a path rule in the runner's config is not.
  * A protected path reached through a symbolic link the session could replace is refused, with the real path in the
    message. A read-only bind holds the real path; delete a link in a writable folder and plant a folder under its name,
    and the hook's fixed --policy path reads the plant. (Links in folders the user cannot write are fine.)
  * Every ancestor of a protected path that is inside a writable folder is made a mount point. A read-only bind holds
    the path it covers, not the folders above it: `mv ~/.config ~/.config2; mkdir -p ~/.config/ws; echo x >
    ~/.config/ws/policy.toml` plants a policy where the hook's fixed path points. rename(2) refuses a mount point
    (EBUSY), so each such ancestor is bound onto itself. Nothing is refused: the binds cost nothing and change no access.
  * Two holes in that, on purpose: `$WS_HOME/approvals/pending` and `$WS_HOME/sessions` stay writable. The gate hook and
    the session nudge run inside the wall (the plugin lives in the runner's process) and must be able to record a
    pending approval and a release marker. Neither grants anything: see ws.approvals for why a forged pending record is
    harmless. `approvals/granted`, the policy, the secrets and the jobs stay read-only; a grant is used up by the
    launcher, outside the wall, over a socket the runner inherits (WS_APPROVAL_FD, ws.approvals).
  * The generated config folder is wholly read-only. OpenCode 1.19.4 loads `plugin/`, `plugins/`, `tool/`, `tools/`,
    `agent/`, `agents/`, `mode/`, `command/` and `opencode.jsonc` from it, so a planted file would run next session, even
    one started with `--no-sandbox`. The one thing OpenCode writes there at start, a `.gitignore`, is generated with it
    (adapters.opencode.IGNORE), and the shim needs no `node_modules`, so nothing has to be writable.
  * Host sockets are shut. A bind of `/` leaves every Unix socket on the host reachable, and a daemon on one runs outside
    the wall: `tmux -L x run-shell '...'` ran as the person and rewrote the policy, `tmux new-window 'ws approve ID'`
    gave a real terminal for self-approval, and systemd-run --user, docker and screen are the same shape. A seccomp
    filter (compiled below, no library needed) makes socket(AF_UNIX) fail with EPERM, and io_uring_setup too, since
    io_uring can open a socket without the system call. socketpair() stays: Node and Bun spawn children through it, and a
    socketpair connects only to itself. The one Unix socket the runner needs, to the launcher, is inherited, not created.
    A 32-bit or x32 process (other system call numbers) is refused. XDG_RUNTIME_DIR and /tmp/tmux-UID are hidden under a
    tmpfs as well, so a path-based client also finds nothing. Linux on x86_64 and aarch64 only; another CPU is refused.
  * `--unshare-pid --unshare-ipc --proc /proc`, `--die-with-parent`. Without its own pid namespace the runner sees the
    person's processes, and `/proc/PID/root/...` of one is a path to every file as that process sees it, around every
    bind here. A process left behind dies with the launcher.
  * The terminal. The filter refuses the TIOCSTI and TIOCLINUX ioctls (typing into the person's shell; kernel 6.1 has no
    sysctl for it). `--new-session` (no controlling terminal) as well when stdin is not a terminal; an interactive runner
    keeps its terminal for Ctrl-C and window size.
  * `--dev-bind /dev /dev` after the bind of `/`. A bind inside a user namespace marks the nested /dev mounts `nodev`, and
    OpenCode's shell tool then fails to start a child ("PermissionDenied: ChildProcess.spawn", seen on 1.19.4) because it
    cannot open /dev/null. This gives the runner the host's /dev as it had before; it does not widen the write wall.
  * Read denial by hiding. Each deny_read rule that names a folder (`~/.ssh/**`) becomes an empty tmpfs over it; a file
    (`~/.aws/credentials`) becomes /dev/null. Only rules with no wildcard other than a trailing `**` can be hidden this
    way, and only paths that exist (bubblewrap cannot mount over nothing); the matcher still covers the rest.
  * Fail closed. No bubblewrap (or no sandbox-exec), or a bubblewrap that cannot start (unprivileged user namespaces
    switched off), refuses to launch with the install line. `--no-sandbox` starts the runner bare and prints what that
    gives up, so skipping the wall is a visible choice, not a silent fallback.
  * No `--unshare-net`: the runner needs the model's API, local or remote. Host filtering is Claude Code's sandbox proxy
    only (policy `allow_hosts`); here the network is as today. That is a limit, written in ws.policy's docstring.
  * macOS: `sandbox-exec` with a generated profile, `(allow default)` then `(deny file-write*)` over the protected paths
    and `(deny file-read*)` over the hidden ones, then the two writable holes re-allowed (the last matching rule wins),
    and `(deny network-outbound (remote unix-socket))`. Seatbelt matches paths, not mount points, so a renamed ancestor
    does not help an attacker there. sandbox-exec is deprecated by Apple but still ships and is what Claude Code and Codex
    use. The profile text is unit tested; no CI job runs it, so the unix-socket rule in particular is unverified.
"""

from __future__ import annotations

import argparse
import os
import platform
import posixpath
import shlex
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from ws import adapters, approvals, policy
from ws.approvals import ENV_MARKER, FD_ENV, approvals_dir
from ws.profile import ws_home

INSTALL = {
    "linux": "install bubblewrap: `sudo apt install bubblewrap` (Debian, Ubuntu) or `sudo dnf install bubblewrap` (Fedora)",
    "darwin": "sandbox-exec ships with macOS (/usr/bin/sandbox-exec) and was not found; the system may be damaged",
}
NO_SANDBOX_NOTE = (
    "ws: --no-sandbox: starting {runner} with NO wall. The deny rules and the hook still apply, but anything they do not "
    "see (`python3 -c`, a path built by a command, a script it writes then runs, git restoring a protected file, a "
    "daemon reached over a Unix socket such as tmux) can write ws's own files, including the policy, and read the "
    "denied folders."
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
            out.append(os.path.realpath(p))
    return list(dict.fromkeys(out))


def socket_dirs(env: dict | None = None) -> list[str]:
    """Folders where host daemons keep their Unix sockets, hidden under a tmpfs (a second layer behind the filter)."""
    env = os.environ if env is None else env
    found = [env.get("XDG_RUNTIME_DIR", ""), f"/tmp/tmux-{os.getuid()}"]
    return [os.path.realpath(p) for p in found if p.startswith("/") and os.path.isdir(p)]


# --- the seccomp filter -------------------------------------------------------------------------------------------

_AUDIT_ARCH = {"x86_64": 0xC000003E, "aarch64": 0xC00000B7}
# (socket, ioctl, io_uring_setup) system call numbers
_NR = {"x86_64": (41, 16, 425), "aarch64": (198, 29, 425)}
_AF_UNIX = 1
_TIOCSTI, _TIOCLINUX = 0x5412, 0x541C
_LD, _JEQ, _JGE, _RET = 0x20, 0x15, 0x35, 0x06  # BPF_LD|W|ABS, BPF_JMP|JEQ|K, BPF_JMP|JGE|K, BPF_RET|K
_ALLOW, _EPERM = 0x7FFF0000, 0x00050001  # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO | EPERM


def seccomp_program(machine: str | None = None) -> bytes:
    """A classic-BPF seccomp program (`struct sock_filter[]`, what `bwrap --seccomp FD` reads) that fails with EPERM:
      * any call from another architecture, and x32 calls (number >= 0x40000000): their numbers are not the ones below;
      * socket(AF_UNIX, ...), and io_uring_setup (io_uring can open a socket without the call);
      * ioctl TIOCSTI and TIOCLINUX (typing into, or reading, the person's terminal).
    Everything else is allowed. Raises SandboxError on a CPU it has no numbers for."""
    machine = machine or platform.machine()
    if machine not in _AUDIT_ARCH:
        raise SandboxError(f"no seccomp filter for {machine or 'this CPU'}; ws builds one for x86_64 and aarch64")
    nr_socket, nr_ioctl, nr_uring = _NR[machine]
    # (label, code, jump if true, jump if false, k); a jump is a label or 0 (fall through)
    prog = [
        (None, _LD, 0, 0, 4),  # seccomp_data.arch
        (None, _JEQ, 0, "deny", _AUDIT_ARCH[machine]),
        (None, _LD, 0, 0, 0),  # seccomp_data.nr
        (None, _JGE, "deny", 0, 0x40000000),
        (None, _JEQ, "sock", 0, nr_socket),
        (None, _JEQ, "deny", 0, nr_uring),
        (None, _JEQ, "ioc", "allow", nr_ioctl),
        ("sock", _LD, 0, 0, 16),  # args[0], the low word: the domain
        (None, _JEQ, "deny", "allow", _AF_UNIX),
        ("ioc", _LD, 0, 0, 24),  # args[1], the low word: the request
        (None, _JEQ, "deny", 0, _TIOCSTI),
        (None, _JEQ, "deny", "allow", _TIOCLINUX),
        ("allow", _RET, 0, 0, _ALLOW),
        ("deny", _RET, 0, 0, _EPERM),
    ]
    at = {label: i for i, (label, *_rest) in enumerate(prog) if label}

    def rel(target, i: int) -> int:
        return 0 if target == 0 else at[target] - (i + 1)

    return b"".join(
        struct.pack("<HBBI", code, rel(jt, i), rel(jf, i), k) for i, (_, code, jt, jf, k) in enumerate(prog)
    )


# --- the plan ------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Plan:
    """What the wall does to the filesystem, in the order it is applied (a later mount wins over an earlier one):
    everything read-only; then the allow-list writable; then ws's own files read-only again; then the two holes the
    gate needs writable; then the project's own config read-only again; then the hidden paths made empty."""

    home: Path
    protect: tuple[str, ...] = ()  # read-only, with `home`, even where they sit inside a writable folder
    hidden: tuple[str, ...] = ()  # deny_read paths, made empty
    sockets: tuple[str, ...] = ()  # socket folders, made empty
    writable: tuple[str, ...] = ()  # the allow-list: project, private tmp, the runner's state, policy allow_write
    locked: tuple[str, ...] = ()  # inside a writable folder but read-only (the project's .git/hooks, .claude, ...)

    def read_only(self) -> list[str]:
        return list(dict.fromkeys(os.path.realpath(p) for p in [self.home, *self.protect]))

    def holes(self) -> list[str]:
        return [os.path.realpath(h) for h in writable_holes(self.home)]

    def roots(self) -> list[str]:
        return list(dict.fromkeys(os.path.realpath(p) for p in self.writable))

    def symlinked(self) -> str | None:
        """A protected path (as given) that is reached through a link the session could replace, or None. A read-only bind
        holds the real path; delete a link in a writable folder and plant a folder under its name, and the hook's fixed
        --policy path reads the plant. A link whose own folder the user cannot write cannot be replaced, so it is fine."""
        for raw in [self.home, *self.protect]:
            p = Path(os.path.abspath(raw))
            for q in [p, *p.parents]:
                if q != Path("/") and q.is_symlink() and os.access(q.parent, os.W_OK | os.X_OK):
                    return f"{q} -> {os.path.realpath(q)}"
        return None

    def ancestors(self) -> list[str]:
        """Folders inside a writable folder and above a read-only path, outermost first. A read-only bind holds the path
        it covers, not the folders above it: `mv ~/.config ~/.config2; mkdir -p ~/.config/ws; echo x > ~/.config/ws/policy.toml`
        plants a policy where the hook's fixed path points. rename(2) refuses a mount point (EBUSY), so each such folder
        is bound onto itself (read-write: it already was)."""
        roots = self.roots()
        out: list[str] = []
        for p in self.read_only():
            for a in reversed(Path(p).parents):
                s = str(a)
                if s not in out and any(r in map(str, a.parents) for r in roots):
                    out.append(s)
        return out


def bwrap_argv(argv: list[str], plan: Plan, tty: bool, seccomp_fd: int) -> list[str]:
    out = ["bwrap", "--ro-bind", "/", "/", "--dev-bind", "/dev", "/dev", "--proc", "/proc"]
    out += ["--unshare-pid", "--unshare-ipc", "--die-with-parent", "--seccomp", str(seccomp_fd)]
    if not tty:
        out.append("--new-session")
    for p in plan.roots():
        if os.path.isdir(p):  # bubblewrap cannot bind what is not there
            out += ["--bind", p, p]
    for p in plan.ancestors():  # after the writable folders, before the read-only mounts that hang under them
        if os.path.isdir(p):
            out += ["--bind", p, p]
    for p in plan.read_only():
        if os.path.lexists(p):
            out += ["--ro-bind", p, p]
    for p in plan.holes():
        if os.path.lexists(p):
            out += ["--bind", p, p]
    for p in plan.locked:
        if os.path.lexists(p):
            out += ["--ro-bind", p, p]
    for p in (*plan.sockets, *plan.hidden):
        out += ["--ro-bind", "/dev/null", p] if os.path.isfile(p) else ["--tmpfs", p]
    return [*out, "--", *argv]


def _sbpl(path: str) -> str:
    return '"' + str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_exec_profile(plan: Plan) -> str:
    """The macOS profile, the same allow-list: deny every write, allow the writable folders (and /dev), deny ws's own
    files again, allow the two holes, deny the project's config again. Paths are resolved first: /var is /private/var
    there, and the profile matches real paths. Seatbelt matches paths, not mount points, so a renamed ancestor does not
    help an attacker there."""
    lines = ["(version 1)", "(allow default)", "(deny file-write*)", '(allow file-write* (subpath "/dev"))']
    lines += [f"(allow file-write* (subpath {_sbpl(p)}))" for p in plan.roots()]
    lines += [f"(deny file-write* (subpath {_sbpl(p)}))" for p in plan.read_only()]
    lines += [f"(allow file-write* (subpath {_sbpl(p)}))" for p in plan.holes()]
    lines += [f"(deny file-write* (subpath {_sbpl(p)}))" for p in plan.locked]
    for p in (*plan.sockets, *plan.hidden):
        kind = "literal" if os.path.isfile(p) else "subpath"
        lines.append(f"(deny file-read* ({kind} {_sbpl(p)}))")
    lines.append("(deny network-outbound (remote unix-socket))")
    return "\n".join(lines) + "\n"


@dataclass
class Wrapped:
    argv: list[str]
    pass_fds: tuple[int, ...] = ()


def wrap(
    argv: list[str],
    pol: policy.Policy,
    home: Path,
    protect: list[str],
    *,
    writable: list[str] = (),
    locked: list[str] = (),
    system: str | None = None,
    which=shutil.which,
    probe=None,
    tty: bool = False,
    env: dict | None = None,
    machine: str | None = None,
) -> Wrapped:
    """`argv` inside the sandbox for this OS, with the descriptors that must stay open for it. Raises SandboxError (with
    the install line) when it cannot be built."""
    system = system or _system()
    plan = Plan(
        home,
        tuple(protect),
        tuple(hidden_paths(pol, env)),
        tuple(socket_dirs(env)),
        tuple(writable),
        tuple(locked),
    )
    link = plan.symlinked()
    if link:
        raise SandboxError(
            f"{link}: a protected path is reached through a symbolic link that the session could delete and replace, "
            "and the wall can only hold the real path. Point WS_HOME (and --policy) at the real folder"
        )
    if system == "linux":
        if not which("bwrap"):
            raise SandboxError(f"bubblewrap (bwrap) is not installed; {INSTALL['linux']}")
        program = seccomp_program(machine)
        if not (probe or _bwrap_starts)():
            raise SandboxError(
                "bubblewrap is installed but cannot start here (unprivileged user namespaces are probably off: "
                "see `sysctl kernel.unprivileged_userns_clone` and kernel.apparmor_restrict_unprivileged_userns)"
            )
        make_holes(home)
        r, w = os.pipe()
        with os.fdopen(w, "wb") as f:  # the program is small: it fits the pipe, and bwrap reads it to the end
            f.write(program)
        os.set_inheritable(r, True)
        return Wrapped(bwrap_argv(argv, plan, tty, r), (r,))
    if system == "darwin":
        if not which("sandbox-exec"):
            raise SandboxError(INSTALL["darwin"])
        make_holes(home)
        return Wrapped(["sandbox-exec", "-p", sandbox_exec_profile(plan), *argv])
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

# Inside the project folder, ws's own read-only list: what Claude Code's sandbox also always protects. A hook or config
# written here runs outside the wall at the next `git commit` or the next start of the runner.
PROJECT_LOCKED = (".git/hooks", ".git/config", ".claude", ".opencode")


def launch_env(env: dict, tmp: str | None = None) -> dict:
    return {**env, ENV_MARKER: "1", **({"TMPDIR": tmp} if tmp else {})}


def expand(path: str, env: dict) -> str:
    home = env.get("HOME") or str(Path.home())
    return home + path[1:] if path == "~" or path.startswith("~/") else path


def _home_or_above(real: str, env: dict) -> bool:
    home = os.path.realpath(env.get("HOME") or str(Path.home()))
    return real == "/" or real == home or home.startswith(real + "/")


def project_folder(cwd: str, env: dict) -> str:
    """The folder the wall leaves writable: where the runner is started. Not the home folder or anything above it: that
    would put `~/.bashrc`, `~/.local/bin` and the ws install itself in reach, which is what the allow-list is for."""
    real = os.path.realpath(cwd)
    if _home_or_above(real, env):
        raise SandboxError(
            f"{cwd} is your home folder or above it: start the runner from a project folder, so the wall can leave only "
            "that writable (everything in the home folder, shell start-up files included, would be writable otherwise)"
        )
    return real


def protected(policy_file: Path, runner: str, extra: list[str] | None = None, env: dict | None = None) -> list[str]:
    """Everything the wall makes read-only besides `$WS_HOME` itself: the policy (wherever --policy put it), the control
    files of both its folder and `$WS_HOME`, anything named with --protect, and the generated config folder. With the
    allow-list most of this is read-only already; it matters where one of these sits inside a writable folder."""
    env = os.environ if env is None else env
    out = [str(policy_file), *policy.control_paths(policy_file.parent), *policy.control_paths(ws_home(env))]
    out += [str(Path(p).expanduser().absolute()) for p in extra or []]
    config_dir = env.get(getattr(adapters.get(runner), "CONFIG_DIR_ENV", ""), "")
    if config_dir:  # the generated folder the runner was told to use
        out.append(str(Path(config_dir).expanduser().absolute()))
    return list(dict.fromkeys(out))


def writable_folders(pol: policy.Policy, runner: str, project: str, tmp: str, extra: list[str], env: dict) -> list[str]:
    """The allow-list: the project, the private tmp, the runner's own state and cache folders (made if missing, since
    bubblewrap cannot bind what is not there), `allow_write` from the policy and --allow-write. A listed folder that does
    not exist is skipped, except the runner's own."""
    out = [project, tmp]
    adapter = adapters.get(runner)
    getattr(adapter, "wall_prepare", lambda _env: None)(env)
    state = getattr(adapter, "wall_writable", None)
    for p in state(env) if state else []:
        Path(p).mkdir(parents=True, exist_ok=True)
        out.append(p)
    for p in [*policy.sandbox_write_paths(pol), *extra]:
        real = os.path.realpath(expand(posixpath.normpath(p), env))
        if _home_or_above(real, env):
            raise SandboxError(
                f"{p} is the home folder or above it (or /): it cannot be made writable, because that would put shell "
                "start-up files, ~/.local/bin and the ws install in reach. Name a folder inside it"
            )
        out.append(real)
    return [p for p in dict.fromkeys(out) if os.path.isdir(p)]


def project_locked(project: str) -> list[str]:
    return [str(Path(project) / rel) for rel in PROJECT_LOCKED if os.path.lexists(Path(project) / rel)]


def run_with_broker(cmd: list[str], env: dict, pass_fds: tuple[int, ...], root: Path) -> int:
    """Start the wrapped runner and stay beside it, outside the wall, to use up approvals it claims (WS_APPROVAL_FD)."""
    mine, theirs = socket.socketpair()
    theirs.set_inheritable(True)
    env = {**env, FD_ENV: str(theirs.fileno())}
    proc = subprocess.Popen(cmd, env=env, pass_fds=(*pass_fds, theirs.fileno()))
    theirs.close()
    for fd in pass_fds:
        os.close(fd)
    threading.Thread(target=approvals.serve_claims, args=(mine, root), daemon=True).start()
    signal.signal(signal.SIGTERM, lambda *_: proc.terminate())
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:  # Ctrl-C reaches the runner too; the runner decides
            continue


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
    home = ws_home().absolute()  # whatever --policy says, the gate keeps its approvals and sessions here
    env = launch_env(dict(os.environ))
    if args.no_sandbox:
        print(NO_SANDBOX_NOTE.format(runner=args.runner), file=sys.stderr)
        try:
            os.execvpe(argv[0], argv, env)
        except OSError as e:
            print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
            return 127
        return 0  # exec does not return; this is for a test that replaces it. --no-sandbox never reaches the wall code
    tmp = tempfile.mkdtemp(prefix="ws-wall-")  # the wall's /tmp: the rest of the disk is read-only
    try:
        project = project_folder(os.getcwd(), env)
        wrapped = wrap(
            argv,
            pol,
            home,
            protected(policy_file, args.runner, args.protect),
            writable=writable_folders(pol, args.runner, project, tmp, args.allow_write or [], env),
            locked=project_locked(project),
            tty=sys.stdin.isatty(),
        )
        return run_with_broker(wrapped.argv, launch_env(dict(os.environ), tmp), wrapped.pass_fds, approvals_dir(home))
    except SandboxError as e:
        print(
            f"ws: refusing to start {args.runner} without its wall: {e}\n(or `--no-sandbox`, which has none)",
            file=sys.stderr,
        )
        return 1
    except OSError as e:
        print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
        return 127
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def add_launch(gs, names: list[str]) -> None:
    p = gs.add_parser(
        "launch",
        help="start a runner inside the sandbox wall (bubblewrap on Linux, sandbox-exec on macOS); fails closed",
    )
    p.add_argument("--runner", required=True, choices=names)
    p.add_argument("--policy", metavar="FILE", help="policy file (default $WS_HOME/policy.toml)")
    p.add_argument("--protect", action="append", metavar="PATH", help="another file or folder to make read-only")
    p.add_argument(
        "--allow-write",
        action="append",
        metavar="DIR",
        help="another folder the session may write (besides the project)",
    )
    p.add_argument("--no-sandbox", action="store_true", help="start the runner with no wall (says what that gives up)")
    p.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the runner's command line")
    p.set_defaults(func=cmd_launch)
