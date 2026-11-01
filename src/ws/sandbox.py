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
    never the home folder or above it), a private tmp the launcher makes and points TMPDIR at, the runner's own private
    state folders (next point), `allow_write` from the policy and `--allow-write`. A deny-list left everything else
    writable: `~/.bashrc`, `~/.local/bin`, `~/.config/systemd/user` and ws's own installed source, each of which runs
    outside the wall at the next shell or the next hook. ws's own files (`$WS_HOME`, the policy, the generated folder)
    are read-only by default and stay so even where one sits inside the project. The price: a program that writes a
    hard-coded path outside those (not TMPDIR) fails; the policy's `allow_write` is how a person lets one through. A
    read-only mount is enforced by the kernel for every process in the sandbox, including the runner's own tool and
    plugin code, which a path rule in the runner's config is not.
  * The runner's own state is private to the wall. OpenCode keeps programs in its XDG data and cache folders (`bin/rg`,
    `node_modules/.bin` plugins, language servers) and runs them later; if the wall let it write the person's real
    ones, a session could plant one that runs outside the wall at the next unwalled start (the P1 the third cold review
    found). So a walled run gets XDG_DATA_HOME, XDG_STATE_HOME and XDG_CACHE_HOME of its own under
    `$WS_HOME/wall/<runner>/` (`private_homes`): kept between walled runs, never read by an unwalled one. The person's
    real folders stay visible and read-only. The runner's sign-in files are copied across at each start, unless a
    deny_read rule hides them. `ws uninstall` takes the folder with `$WS_HOME`.
  * The project's own config is the second half of that class: the folders and files a runner or git LOADS at start
    (`PROJECT_LOCKED`, `GIT_LOCKED`, `git_facts`, `submodule_repos`; the table of what and why is
    tests/e2e/test_wall_loadpaths.py). They are read-only whether or not they exist. A path that exists is bound
    read-only onto itself; one that does not is made by the launcher first (an empty folder, or an empty valid config
    file) and removed after the run, because bubblewrap can only bind what is there. Every folder between the project and
    a locked path is bound onto itself too (`Plan.ancestors`): a locked path is a mount point, but `mv .git .git2` moves
    the folder above it, and rename(2) refuses a mount point. What git reads is asked of git: `rev-parse
    --absolute-git-dir --git-common-dir` names the git folders in the project (a `--separate-git-dir` or a worktree can
    put them anywhere in it), and in each the hooks, config, `config.worktree`, `commondir` (it decides which folder git
    reads the others from; its placeholder is "." because an empty file makes git die), the pointer files of every
    `worktrees/NAME`, and the submodule repositories are locked; `git config --list --show-origin` gives the
    `core.hooksPath` folder and the `[include]`d files. A locked path that is a symbolic link is refused, since a link in
    a writable folder can be deleted and replaced.
  * Several sessions in one project share the placeholders. A record under `$WS_HOME/wall/projects/` (a file lock, the
    pids inside, the placeholders made) lets only the last launcher out take them down: the first one removing a
    folder that the second's wall has bound would detach the bind in the second's mount namespace. A launcher that is
    killed leaves its record; the next launch for that project finds every pid dead and cleans up first. SIGHUP and
    SIGTERM (and SIGINT when there is no terminal) stop the runner and leave through the same path.
  * The end-of-run report, because a list of paths is always one path short. After the runner exits, outside the wall,
    the launcher compares a bounded scan of the project taken before it started with one taken after (8 seconds and
    300000 entries; a scan that stops says INCOMPLETE on the first line of the report, and then the list is a lower
    bound): a new or changed `.git` at any depth (non-sample hooks, config keys beyond a stock `git init`, submodules),
    any runner or tool config at any depth (`.claude`, `.opencode`, `.mcp.json`, `opencode.json(c)`, `tui.json(c)`,
    `.vscode/settings.json|tasks.json`, `mise.toml`, `.pre-commit-config.yaml`, `.envrc`), the auto-loaded files of a
    `.venv`, `venv` or `env` (`bin/*`, `*.pth`, `site-packages/*.py`), `node_modules` (each `.bin` link and the file it
    resolves to inside the project, and the folder time of each package), and new `.pyc` files. Walled Python writes its
    bytecode to the private tmp (PYTHONPYCACHEPREFIX), so a `.pyc` in the project is not the wall's doing. It prints to
    stderr and appends a line to `$WS_HOME/logs/wall-report.jsonl` (ws.jsonl), and removes nothing. This is what covers
    a runner started later in a sub-folder (`sub/.claude`), a repository the session creates with `git init` (which has
    to keep working), and a submodule added during the session. It runs as the person on a tree the session wrote, so
    it trusts nothing in it: every entry is lstat'd first, a link is reported as a link (its target text) and never read
    through (a planted `sub/.git` that links to a `deny_read` file would otherwise be copied into a log the next session
    reads), only regular files are opened (O_NOFOLLOW | O_NONBLOCK, a size cap, the time budget checked while reading; a
    FIFO named `.mcp.json` or a link to /dev/zero cannot hang it), and git is never run on it.
  * Claude Code has no bubblewrap wall, so `ws gate launch --runner claude-code -- claude` makes its own sandbox
    project-specific: it adds the locked set of THIS project (git folders resolved as above) to `denyWrite` in a copy of
    the generated settings under `$WS_HOME/gate/claude-code/projects/`, starts `claude --settings` with it, and writes the
    same end-of-run report. The shared settings carry the Edit deny rules (`**/NAME`) only, because a sandbox path has no
    project-relative spelling in a `--settings` file. See ws.adapters.claude_code.
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
  * Other processes' memory. The filter also refuses ptrace, process_vm_readv and process_vm_writev: with Yama's
    ptrace_scope at 0 a shell child of the runner could attach to it and patch the in-process gate plugin. /proc/PID/mem
    is the same reach by file, which no system-call filter can refuse, so the OpenCode shim calls prctl(PR_SET_DUMPABLE,
    0) on its own process through the Bun FFI it already uses: a non-dumpable process is refused for /proc/PID/mem and
    ptrace unless the caller has CAP_SYS_PTRACE, which the wall drops (tested against the real runner). Exec makes a
    child dumpable again, so only the runner is hardened. Where the shim cannot load, or for a runner without one, the
    gap is open on a host whose ptrace_scope is 0 or absent: `ws doctor` WARNs (WS007, with the sysctl line) and the
    launcher prints one line. The macOS profile has no rule for this: the Seatbelt syntax could not be checked on a Mac,
    and a wrong rule would stop the profile loading.
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
    and `(deny file-read*)` over the hidden ones, then the two writable holes and the runner's private folders
    re-allowed (the last matching rule wins), the project's locked paths denied again, and
    `(deny network-outbound (remote unix-socket))`. Seatbelt matches paths, not mount points, so a renamed ancestor of a
    protected path does not help an attacker there; a renamed ancestor of a locked project path does (`mv .git .git2`,
    plant in `.git2/hooks`, `mv .git2 .git`), so those folders get a `literal` deny of their own. sandbox-exec is deprecated by Apple but still ships and is what Claude Code and Codex
    use. The profile text is unit tested; no CI job runs it and it has not been run on a Mac: the macOS wall is UNTESTED,
    the unix-socket and `literal` rules in particular.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import glob
import hashlib
import json
import os
import platform
import posixpath
import re
import shlex
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from ws import adapters, approvals, jsonl, policy
from ws.approvals import ENV_MARKER, FD_ENV, approvals_dir
from ws.output import WsError
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
# (ptrace, process_vm_readv, process_vm_writev): reading or patching another process's memory
_PEEK = {"x86_64": (101, 310, 311), "aarch64": (117, 270, 271)}
_AF_UNIX = 1
_TIOCSTI, _TIOCLINUX = 0x5412, 0x541C
_LD, _JEQ, _JGE, _RET = 0x20, 0x15, 0x35, 0x06  # BPF_LD|W|ABS, BPF_JMP|JEQ|K, BPF_JMP|JGE|K, BPF_RET|K
_ALLOW, _EPERM = 0x7FFF0000, 0x00050001  # SECCOMP_RET_ALLOW, SECCOMP_RET_ERRNO | EPERM


def seccomp_program(machine: str | None = None) -> bytes:
    """A classic-BPF seccomp program (`struct sock_filter[]`, what `bwrap --seccomp FD` reads) that fails with EPERM:
      * any call from another architecture, and x32 calls (number >= 0x40000000): their numbers are not the ones below;
      * socket(AF_UNIX, ...), and io_uring_setup (io_uring can open a socket without the call);
      * ptrace, process_vm_readv and process_vm_writev: with Yama's ptrace_scope at 0 a shell child could attach to the
        runner that started it and patch the in-process gate plugin, or read a sibling's secrets and nonces. NOT closed:
        opening `/proc/PID/mem` of such a process is the same reach by file, which no system-call filter can refuse (the
        path is not visible to it); the runner is dumpable and the wall's user namespace has no capability to change
        that. tests/e2e/test_wall_loadpaths.py pins it as a known limit.
      * ioctl TIOCSTI and TIOCLINUX (typing into, or reading, the person's terminal).
    Everything else is allowed. Raises SandboxError on a CPU it has no numbers for."""
    machine = machine or platform.machine()
    if machine not in _AUDIT_ARCH:
        raise SandboxError(f"no seccomp filter for {machine or 'this CPU'}; ws builds one for x86_64 and aarch64")
    nr_socket, nr_ioctl, nr_uring = _NR[machine]
    peeks = _PEEK[machine]
    # (label, code, jump if true, jump if false, k); a jump is a label or 0 (fall through)
    prog = [
        (None, _LD, 0, 0, 4),  # seccomp_data.arch
        (None, _JEQ, 0, "deny", _AUDIT_ARCH[machine]),
        (None, _LD, 0, 0, 0),  # seccomp_data.nr
        (None, _JGE, "deny", 0, 0x40000000),
        (None, _JEQ, "sock", 0, nr_socket),
        (None, _JEQ, "deny", 0, nr_uring),
        *((None, _JEQ, "deny", 0, nr) for nr in peeks),
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
    gate needs writable, and the runner's private folders; then the project's own config read-only again; then the
    hidden paths made empty."""

    home: Path
    protect: tuple[str, ...] = ()  # read-only, with `home`, even where they sit inside a writable folder
    hidden: tuple[str, ...] = ()  # deny_read paths, made empty
    sockets: tuple[str, ...] = ()  # socket folders, made empty
    writable: tuple[str, ...] = ()  # the allow-list: project, private tmp, policy allow_write
    locked: tuple[str, ...] = ()  # inside a writable folder but read-only (the project's .git/hooks, .claude, ...)
    homes: tuple[
        str, ...
    ] = ()  # the runner's private XDG folders: writable, though they sit inside ws's read-only home

    def read_only(self) -> list[str]:
        return list(dict.fromkeys(os.path.realpath(p) for p in [self.home, *self.protect]))

    def holes(self) -> list[str]:
        return [os.path.realpath(h) for h in writable_holes(self.home)]

    def roots(self) -> list[str]:
        return list(dict.fromkeys(os.path.realpath(p) for p in self.writable))

    def private(self) -> list[str]:
        return [os.path.realpath(h) for h in self.homes]

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
        """Folders inside a writable folder and above a read-only or locked path, outermost first. A read-only bind holds
        the path it covers, not the folders above it: `mv ~/.config ~/.config2; mkdir -p ~/.config/ws; echo x >
        ~/.config/ws/policy.toml` plants a policy where the hook's fixed path points, and in the project `mv .git .git2`
        takes the hooks and the config with it. rename(2) refuses a mount point (EBUSY), so each such folder is bound
        onto itself (read-write: it already was). A locked path is a mount point itself, so it needs no more."""
        roots = self.roots()
        out: list[str] = []
        for p in [*self.read_only(), *(os.path.realpath(q) for q in self.locked)]:
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
    for p in plan.private():  # the runner's own state: bound over ws's read-only home, like the holes
        if os.path.isdir(p):
            out += ["--bind", p, p]
    for p in plan.locked:
        if os.path.lexists(p):
            out += ["--ro-bind", p, p]
    for p in (*plan.sockets, *plan.hidden):
        out += ["--ro-bind", "/dev/null", p] if os.path.isfile(p) else ["--tmpfs", p]
    return [*out, "--", *argv]


def _locked_above(plan: Plan, folder: str) -> bool:
    return any(os.path.realpath(q).startswith(folder + "/") for q in plan.locked)


def _sbpl(path: str) -> str:
    return '"' + str(Path(path).resolve()).replace("\\", "\\\\").replace('"', '\\"') + '"'


def sandbox_exec_profile(plan: Plan) -> str:
    """The macOS profile, the same allow-list: deny every write, allow the writable folders (and /dev), deny ws's own
    files again, allow the two holes and the runner's private folders, deny the project's config again. Paths are
    resolved first: /var is /private/var there, and the profile matches real paths. Seatbelt matches paths, not mount
    points, so a renamed ancestor of a read-only path does not help an attacker. The ancestors of a locked path are the
    exception: `mv .git .git2; echo x > .git2/hooks/h; mv .git2 .git` writes at a path no rule names and then renames it
    into place, so each such folder gets a `literal` deny, which stops a rename of the folder itself but not a write
    to a file below it."""
    lines = ["(version 1)", "(allow default)", "(deny file-write*)", '(allow file-write* (subpath "/dev"))']
    lines += [f"(allow file-write* (subpath {_sbpl(p)}))" for p in plan.roots()]
    lines += [f"(deny file-write* (subpath {_sbpl(p)}))" for p in plan.read_only()]
    lines += [f"(allow file-write* (subpath {_sbpl(p)}))" for p in plan.holes()]
    lines += [f"(allow file-write* (subpath {_sbpl(p)}))" for p in plan.private()]
    lines += [f"(deny file-write* (subpath {_sbpl(p)}))" for p in plan.locked]
    lines += [f"(deny file-write* (literal {_sbpl(p)}))" for p in plan.ancestors() if _locked_above(plan, p)]
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
    homes: list[str] = (),
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
        tuple(homes),
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

# Inside the project folder, ws's own read-only list: everything a runner or git loads or runs at start that a session
# could plant, and that would then run outside the wall (at the next `git commit`, the next start of the runner).
# Built from the runners' and git's own documentation and checked by tests/e2e/test_wall_loadpaths.py, which has the
# table and says how to add a runner. (path, what a missing one is made into so it cannot be created: a folder, a file
# with this text, or None for nothing.) CLAUDE.md, AGENTS.md and the skill and rule text are content, not code: they
# stay writable.
DIR = "dir"
PROJECT_LOCKED = (
    (".claude", DIR),
    (".opencode", DIR),
    (".mcp.json", '{"mcpServers": {}}\n'),
    ("opencode.json", "{}\n"),
    ("opencode.jsonc", "{}\n"),
    ("tui.json", "{}\n"),
    ("tui.jsonc", "{}\n"),
)
# In every git folder the project holds (`.git`, and a separate or common one inside the project). Hooks and config are
# code to git; `commondir` and the pointer files in `worktrees/NAME` decide WHICH folder git reads them from, so a
# planted `commondir` that names a git folder made in the project is as good as writing the hooks themselves. The
# placeholder for `commondir` is "." ("this folder"): an empty file makes git die.
GIT_LOCKED = (("hooks", DIR), ("config", None), ("config.worktree", ""), ("commondir", ".\n"))
GIT_WORKTREE_FILES = (("commondir", None), ("gitdir", None), ("config.worktree", ""))  # in worktrees/NAME
GIT_OTHER_DIRS = ("worktrees", "modules")  # held whole by the Claude Code settings, which cannot name a glob


def claude_denied_paths(cwd: str) -> list[str]:
    """Absolute paths for the claude-code adapter's `denyWrite`, for the project at `cwd`: the names above, and what the
    OpenCode wall resolves (`lock_wanted`: a separate git folder, worktree pointers, submodules, an included config). []
    where `cwd` is the home folder or above it, or git is slow: Claude Code's settings then carry the Edit rules only."""
    project = os.path.realpath(cwd)
    if _home_or_above(project, os.environ):
        return []
    named = [os.path.join(project, rel) for rel in claude_denied()]
    return list(dict.fromkeys([*named, *(path for path, _ in lock_wanted(project))]))


def claude_denied() -> list[str]:
    """The project paths, relative to the project root, that the claude-code adapter puts in its sandbox denyWrite and
    its Edit deny rules: the same set as above, with what a name cannot say (a separate git folder, the files under
    `worktrees/*`) held by the folder."""
    return [
        *(rel for rel, _ in PROJECT_LOCKED),
        *(f".git/{n}" for n, _ in GIT_LOCKED),
        *(f".git/{n}" for n in GIT_OTHER_DIRS),
    ]


def launch_env(env: dict, tmp: str | None = None, homes: dict | None = None) -> dict:
    """The runner's environment. In the wall, TMPDIR is the private tmp and so is Python's bytecode cache
    (PYTHONPYCACHEPREFIX): a `.pyc` written into the project by a walled `python` would be loaded by the next unwalled
    one, and the report would have to look for them."""
    walled = {"TMPDIR": tmp, "PYTHONPYCACHEPREFIX": f"{tmp}/pycache"} if tmp else {}
    return {**env, ENV_MARKER: "1", **walled, **(homes or {})}


def ptrace_scope(path: str = "/proc/sys/kernel/yama/ptrace_scope") -> int | None:
    """Yama's ptrace_scope, or None where the kernel has none (a process can then read a same-user process's memory)."""
    try:
        return int(Path(path).read_text().strip())
    except (OSError, ValueError):
        return None


PTRACE_NOTE = (
    "ws: note: this host's ptrace_scope is 0 (or absent): a process in the wall can read the runner's memory through "
    "/proc/PID/mem unless the runner marks itself undumpable (the OpenCode shim does). `ws doctor` WS007 has the fix."
)


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
    """The allow-list: the project, the private tmp, `allow_write` from the policy and --allow-write. A listed folder
    that does not exist is skipped. The runner's own state is not here: it gets private folders (`private_homes`)."""
    getattr(adapters.get(runner), "wall_prepare", lambda _env: None)(env)
    out = [project, tmp]
    for p in [*policy.sandbox_write_paths(pol), *extra]:
        real = os.path.realpath(expand(posixpath.normpath(p), env))
        if _home_or_above(real, env):
            raise SandboxError(
                f"{p} is the home folder or above it (or /): it cannot be made writable, because that would put shell "
                "start-up files, ~/.local/bin and the ws install in reach. Name a folder inside it"
            )
        out.append(real)
    return [p for p in dict.fromkeys(out) if os.path.isdir(p)]


WALL_REPORT_SCHEMA = 1  # the `schema` of a line in logs/wall-report.jsonl

# --- the runner's own state --------------------------------------------------------------------------------------

# XDG variable -> (folder under `$WS_HOME/wall/<runner>/`, whether it is kept per project).
XDG_HOMES = (("XDG_DATA_HOME", "data", True), ("XDG_STATE_HOME", "state", False), ("XDG_CACHE_HOME", "cache", True))
# The files in the runner's real data folder that a walled run needs to be signed in, copied in at each start.
SEED = {"opencode": ("auth.json", "mcp-auth.json")}


def _dir_flags() -> int:
    """open() flags for a folder that is never reached through a link. In a function: native Windows has neither
    O_DIRECTORY nor O_NOFOLLOW, and `ws` (doctor included) must still import there; the wall itself is POSIX only."""
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def project_key(project: str) -> str:
    """A folder name for a project: its name, made safe, and a hash of its real path."""
    name = re.sub(r"[^A-Za-z0-9._-]", "_", os.path.basename(project))[:40] or "project"
    return f"{name}-{hashlib.sha256(project.encode()).hexdigest()[:10]}"


def _subdir(parent_fd: int, name: str) -> int:
    """A descriptor for `name` below `parent_fd`, made (0700) if missing, never through a symbolic link: the walled
    session can write inside the private folders, so a link it plants must not be followed by the launcher (it is
    removed and the folder made again)."""
    try:
        return os.open(name, _dir_flags(), dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    except OSError:
        if not stat.S_ISLNK(os.lstat(name, dir_fd=parent_fd).st_mode):
            raise
        os.unlink(name, dir_fd=parent_fd)  # a planted link is removed itself, not followed
    os.mkdir(name, 0o700, dir_fd=parent_fd)
    return os.open(name, _dir_flags(), dir_fd=parent_fd)


def _unlink(dir_fd: int, name: str) -> None:
    try:
        os.unlink(name, dir_fd=dir_fd)  # a link is removed itself, not followed
    except FileNotFoundError:
        pass


def _seed(data_fd: int, runner: str, real_data: Path, hidden: list[str]) -> None:
    """Copy the runner's sign-in files from the real data folder into the private one, or remove the copy when the real
    file is gone or hidden by deny_read. Every step is relative to a descriptor, opened without following links, and the
    file is made with O_EXCL | O_NOFOLLOW: a planted `auth.json.new` link, or a link where `opencode/` should be, cannot
    make the launcher (which runs outside the wall) overwrite or delete anything else."""
    app_fd = _subdir(data_fd, runner)
    try:
        for name in SEED.get(runner, ()):
            src = real_data / name
            real = os.path.realpath(src)
            tmp = name + ".new"
            _unlink(app_fd, tmp)
            if not src.is_file() or any(real == h or _inside(real, h) for h in hidden):
                _unlink(app_fd, name)
                continue
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=app_fd)
            with os.fdopen(fd, "wb") as f:
                f.write(src.read_bytes())
            os.rename(tmp, name, src_dir_fd=app_fd, dst_dir_fd=app_fd)
    finally:
        os.close(app_fd)


def private_homes(home: Path, runner: str, pol: policy.Policy, env: dict, project: str) -> dict[str, str]:
    """XDG data, state and cache folders for a walled run of `runner` in `project`, as {variable: folder}; {} for a
    runner that keeps no state of its own (one without `wall_writable` in its adapter, which is the marker).

    Why private. OpenCode keeps programs in the XDG cache and data folders (`bin/rg`, `node_modules/.bin` plugins,
    language servers) and runs them later, and the walled run has to write there. If it wrote the person's real
    folders, whatever a session planted would run outside the wall at the next unwalled start. So the walled run gets
    its own, under `$WS_HOME/wall/<runner>/`, which no unwalled run is pointed at; the real ones stay visible and
    read-only. Why under `$WS_HOME` and not the private tmp: the session history and the model cache are worth keeping
    between runs, and `ws uninstall` removes `$WS_HOME`. They are made 0700, and a `.gitignore` of `*` keeps them out of
    a synced `$WS_HOME`.

    Which are per project. Data and cache are (`<kind>/<name>-<hash of the real path>`): the programs live there, and
    a program planted by a session in one project must not run in a walled session of another. State stays shared: it
    holds small preference and prompt-history files, nothing a runner executes (if that is wrong it is data, not code,
    that crosses). OpenCode keys its sessions by project, so per-project data loses no history, except when a project
    moves. What is lost: a sign-in is copied from the real data folder at each start (a token the run refreshes is not
    carried back), and one the policy hides with deny_read is not copied. A session that holds its project's folder can
    swap it for a link between the check and the bind; that needs a second walled session in the same project."""
    if not hasattr(adapters.get(runner), "wall_writable"):
        return {}
    base = home / "wall"
    base.mkdir(parents=True, exist_ok=True)
    base.chmod(0o700)
    if not (base / ".gitignore").exists():
        (base / ".gitignore").write_text("*\n", encoding="utf-8")
    root = Path(os.path.realpath(base / runner))
    key, out = project_key(project), {}
    real_data = Path(env.get("XDG_DATA_HOME") or f"{env.get('HOME') or Path.home()}/.local/share") / runner
    hidden = hidden_paths(pol, env)
    for var, kind, per_project in XDG_HOMES:
        folder = root / kind
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not per_project:
            out[var] = str(folder)
            continue
        top = os.open(folder, _dir_flags())
        try:
            fd = _subdir(top, key)
            try:
                if var == "XDG_DATA_HOME":
                    _seed(fd, runner, real_data, hidden)
            finally:
                os.close(fd)
        except OSError as e:
            raise SandboxError(f"{folder / key}: {e.strerror}; remove it, it may be a link planted in a session") from e
        finally:
            os.close(top)
        out[var] = str(folder / key)
    return out


# --- the project's own config -------------------------------------------------------------------------------------


def _inside(path: str, folder: str) -> bool:
    return path.startswith(folder.rstrip("/") + "/")


class GitFacts(NamedTuple):
    hooks: str | None  # the core.hooksPath folder, when it is inside the project
    files: list[str]  # config files in the project that git reads (an `[include]` of one)
    dirs: list[str]  # git folders inside the project: the one git finds from here, and its common folder


def git_facts(project: str) -> GitFacts:
    """What git, started in `project`, would load that is not a plain file under `.git`: the git folders it resolves
    (`rev-parse --absolute-git-dir --git-common-dir`; a `--separate-git-dir` or a worktree can put them anywhere in the
    project), the folder named by core.hooksPath, and the config files pulled in by `[include]`. All of it is code to
    git: a hook runs at the next commit, and a config sets `core.fsmonitor`, an alias or a credential helper. Read with
    `git rev-parse` and `git config`, which run nothing. `.git` itself is always counted when it is a folder. Empty
    where git is missing, this is not a repository, or git takes longer than 10 seconds."""

    def git(*args: str) -> str | None:
        try:
            r = subprocess.run(
                ["git", "-C", project, *args],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        return r.stdout if r.returncode == 0 else None

    real = os.path.realpath(project)
    if os.path.islink(os.path.join(real, ".git")):
        return GitFacts(None, [], [])  # never run git on a linked `.git`; lock_project refuses it
    dirs = [os.path.join(real, ".git")] if os.path.isdir(os.path.join(real, ".git")) else []
    where = git("rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    listing = git("config", "--list", "--show-origin", "-z")
    if where is None or listing is None or len(where.splitlines()) != 3:
        return GitFacts(None, [], [d for d in dirs if os.path.isdir(d)])
    top, *folders = where.splitlines()
    dirs += [os.path.realpath(os.path.join(real, f)) for f in folders]
    files, hooks = [], None
    for origin, entry in zip(*(iter(listing.split("\0")),) * 2, strict=False):  # origin NUL "key" NL "value" NUL
        key, _, value = entry.partition("\n")
        here = os.path.realpath(os.path.join(top, origin[5:])) if origin.startswith("file:") else None
        if here:
            files.append(here)
        if key == "include.path" or (key.startswith("includeif.") and key.endswith(".path")):
            # relative to the including file; named even if it is not there (yet) or its condition is false
            files.append(os.path.realpath(os.path.join(os.path.dirname(here or top), expand(value, os.environ))))
        if key == "core.hookspath":
            hooks = os.path.realpath(os.path.join(top, expand(value, os.environ)))
    mine = [f for f in dict.fromkeys(files) if _inside(f, real) and not os.path.isdir(f)]
    return GitFacts(
        hooks if hooks and _inside(hooks, real) else None,
        mine,
        [d for d in dict.fromkeys(dirs) if _inside(d, real) and os.path.isdir(d)],
    )


def submodule_repos(git_dir: Path) -> list[str]:
    """The hooks folders and config files of the submodule repositories under `.git/modules`, which git runs and reads
    like the project's own. Ones the session adds later are not seen (the end-of-run report names them)."""
    out: list[str] = []
    for here, dirs, files in os.walk(git_dir / "modules"):
        if "HEAD" in files:  # a repository: its hooks and config, then only its own `modules` below
            out += [str(Path(here) / "hooks")] if "hooks" in dirs else []
            out += [str(Path(here) / "config")] if "config" in files else []
            dirs[:] = [d for d in dirs if d == "modules"]
    return out


def lock_wanted(project: str) -> list[tuple[str, str | None]]:
    """(path, placeholder) for everything in the project the wall locks: `PROJECT_LOCKED`; in each git folder
    `GIT_LOCKED`, the pointer files of its worktrees and its submodule repositories; the `.git` pointer when it is a
    file (or a link, which is then refused); and what git is set up to load (`git_facts`). The placeholder is what a
    missing path is made into (`DIR`, the text of a file, or None to leave it missing)."""
    root = Path(project)
    out: list[tuple[str, str | None]] = [(str(root / rel), fill) for rel, fill in PROJECT_LOCKED]
    pointer = root / ".git"
    if os.path.lexists(pointer) and (pointer.is_symlink() or pointer.is_file()):
        out.append((str(pointer), None))  # a pointer file must not be swapped; a link is refused later
    facts = git_facts(project)
    for g in facts.dirs:
        out += [(f"{g}/{name}", fill) for name, fill in GIT_LOCKED]
        out += [(p, None) for p in submodule_repos(Path(g))]
        for w in sorted(Path(g, "worktrees").glob("*")):
            out += [(str(w / name), fill) for name, fill in GIT_WORKTREE_FILES if w.is_dir()]
    out += [(f, "") for f in facts.files]  # a config file that is missing is made empty
    if facts.hooks and facts.hooks != os.path.realpath(project):
        out.append((facts.hooks, DIR))
    first: dict[str, str | None] = {}
    for path, fill in out:
        first.setdefault(path, fill)
    return list(first.items())


def lock_project(project: str) -> tuple[list[str], list[tuple[str, str]]]:
    """(locked, created): the paths for the wall to hold read-only, and the placeholders made so a missing one cannot be
    created inside the wall. bubblewrap can only bind what is there, and a read-only bind of a folder or file that is
    in the way is what stops `mkdir .claude` and `echo '{...}' > .mcp.json`. They are made here, by the launcher, before
    the wall, and removed by `unlock_project` after it (only if still what was made; see `enter_project` for who
    removes them when several sessions share the project). Raises SandboxError for a locked path that is a symbolic
    link: it could be deleted and replaced by a real folder, which no bind prevents."""
    locked: list[str] = []
    created: list[tuple[str, str]] = []
    wanted = lock_wanted(project)
    for path, _ in wanted:  # all of them first, so a refusal leaves nothing made
        if os.path.islink(path):
            raise SandboxError(
                f"{path} -> {os.path.realpath(path)}: a project config path that is a symbolic link could be deleted "
                "and replaced by a real one inside the wall. Replace the link with the real folder or file"
            )
    for path, fill in wanted:
        if fill is not None and not os.path.lexists(path):
            try:
                parents = [str(a) for a in Path(path).parents if _inside(str(a), project)]
                for q in reversed([*([path] if fill == DIR else []), *parents]):  # a missing parent is made too
                    if not os.path.lexists(q):
                        os.mkdir(q)
                        created.append((q, DIR))
                if fill != DIR:
                    Path(path).write_text(fill, encoding="utf-8")
                    created.append((path, fill))
            except OSError:
                continue  # a project folder we cannot write to: nothing can be planted there either
        if os.path.lexists(path):
            locked.append(path)
        for name, text in project_files(project, path).items():
            f = Path(path) / name
            if not os.path.lexists(f):
                try:
                    f.write_text(text, encoding="utf-8")
                    created.append((str(f), text))
                except OSError:
                    pass
    return locked, created


def project_files(project: str, path: str) -> dict[str, str]:
    """Files a runner writes into a locked folder at start and will not run without (EROFS), so the launcher makes them
    first, outside the wall, with the text the runner writes. OpenCode keeps a `.gitignore` in every config folder,
    `.opencode` of the project included (the same as `adapters.opencode.wall_prepare` does for the global one)."""
    if os.path.relpath(path, project) == ".opencode" and os.path.isdir(path):
        runner = adapters.get("opencode")
        return {runner.IGNORE: runner.IGNORE_TEXT}
    return {}


def unlock_project(created: list[tuple[str, str]]) -> None:
    """Remove the placeholders `lock_project` made, newest first. A folder with anything in it, or a file that is no
    longer the text that was written, is left alone."""
    for path, fill in reversed(created):
        try:
            if fill == DIR:
                os.rmdir(path)
            elif Path(path).read_text(encoding="utf-8") == fill:
                os.unlink(path)
        except OSError:
            pass


# --- several sessions in one project, and a launcher that dies ------------------------------------------------------


@contextlib.contextmanager
def _project_state(home: Path, project: str):
    """The record of which launchers are inside `project` and which placeholders they made, as a dict, under a lock
    (`$WS_HOME/wall/projects/<key>.json`, outside the wall's reach: `$WS_HOME` is read-only in it)."""
    import fcntl  # POSIX only; the wall is Linux and macOS

    folder = home / "wall" / "projects"
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = hashlib.sha256(project.encode()).hexdigest()[:16]
    with open(folder / f"{key}.lock", "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        record = folder / f"{key}.json"
        try:
            state = json.loads(record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            state = {}
        state["project"] = project
        state["pids"] = [p for p in state.get("pids", []) if _alive(p)]
        state.setdefault("created", [])
        yield state
        if state["pids"] or state["created"]:
            record.write_text(json.dumps(state), encoding="utf-8")
        else:
            record.unlink(missing_ok=True)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except OSError:
        pass  # exists, owned by someone else
    return True


def enter_project(home: Path, project: str) -> list[str]:
    """Lock the project for this launcher and record it. Placeholders are shared: a second walled session in the same
    project finds them and adds its pid, and only the last one out removes them (`leave_project`); the first launcher
    taking them down under a second one would detach the read-only bind in the second's mount namespace. If every
    launcher the record names is dead (killed), the placeholders it left are taken down first. Returns the locked paths."""
    with _project_state(home, project) as state:
        if not state["pids"] and state["created"]:
            unlock_project([tuple(c) for c in state["created"]])
            state["created"] = []
        locked, created = lock_project(project)
        state["created"] += created
        state["pids"].append(os.getpid())
    return locked


def leave_project(home: Path, project: str) -> list[str]:
    """Take this launcher out of the record; the last one out removes the placeholders. Returns the paths of the
    placeholders that exist for the project (the end-of-run report leaves them out)."""
    with _project_state(home, project) as state:
        state["pids"] = [p for p in state["pids"] if p != os.getpid()]
        made = [c[0] for c in state["created"]]
        if not state["pids"]:
            unlock_project([tuple(c) for c in state["created"]])
            state["created"] = []
    return made


# --- the end-of-run report --------------------------------------------------------------------------------------------

# The wall is a list of paths, and a list is always one path short. So the launcher also looks, outside the wall, after
# the runner exits, at what the session left that the person would run next: a git folder it made, a config or plugin
# file anywhere in the project (a runner started in a sub-folder reads that folder's own `.claude`, `opencode.json`...),
# and the auto-loaded files of a virtual environment or `node_modules`. It reports and never deletes: it cannot tell a
# planted hook from one the person asked for.
#
# It runs as the person, outside the wall, on a tree the session wrote, so it trusts nothing in it. It looks at every
# entry with lstat first. A link is reported as a link (its target text, at most) and never read through: a planted
# `sub/.git` that links to a file `deny_read` hides would otherwise be copied by the launcher into a log the next session
# can read. Only regular files are opened, with O_NOFOLLOW | O_NONBLOCK (a FIFO named `.mcp.json` would hang a plain
# open), after an fstat, with a size cap and the scan's time budget checked while reading. git is never run on it.
REPORT_NAMES = (
    ".claude",
    ".opencode",
    ".mcp.json",
    "opencode.json",
    "opencode.jsonc",
    "tui.json",
    "tui.jsonc",
    "mise.toml",
    ".mise.toml",
    ".pre-commit-config.yaml",
    ".envrc",
)
REPORT_VSCODE = ("settings.json", "tasks.json")  # in a `.vscode` folder
VENV_DIRS = (".venv", "venv", "env")
VENV_AUTO = (  # what a virtual environment runs without being asked
    "bin/*",
    "Scripts/*",
    "lib*/python*/site-packages/*.pth",
    "lib*/python*/site-packages/*.py",
    "Lib/site-packages/*.pth",
    "Lib/site-packages/*.py",
)
STOCK_CONFIG = {
    "core.repositoryformatversion",
    "core.filemode",
    "core.bare",
    "core.logallrefupdates",
    "core.ignorecase",
    "core.precomposeunicode",
    "core.symlinks",
}
SCAN_ENTRIES, SCAN_SECONDS = 300_000, 8.0  # the scan stops at either, and says so
READ_CAP = 2 * 1024 * 1024  # a file bigger than this is reported by its size, not read
PACKAGES_CAP = 5000  # the packages of one node_modules whose folder time is checked
PYC_SHOWN = 5  # new .pyc files named in a report; the rest are counted


class _Out(Exception):
    """The scan's time or entry budget ran out."""


def _kind(path: str) -> str:
    """link | dir | file | other (a FIFO, a socket, a device), by lstat: never through a link."""
    try:
        mode = os.lstat(path).st_mode
    except OSError:
        return "gone"
    return "link" if stat.S_ISLNK(mode) else "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"


def _link_print(path: str) -> dict:
    try:
        return {"link": os.readlink(path)[:200]}
    except OSError:
        return {"link": "?"}


def _open_regular(path: str) -> tuple[int | None, str]:
    """(fd, "") for a regular file opened without following a link or blocking, else (None, why)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as e:
        return None, "a link" if e.errno == errno.ELOOP else "unreadable"
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None, "not a regular file"
    except OSError:
        os.close(fd)
        return None, "unreadable"
    return fd, ""


def _read(path: str, deadline: float, clock, cap: int = READ_CAP) -> tuple[bytes | None, str]:
    """(content, "") of a regular file of at most `cap` bytes, else (None, why). Raises _Out past the deadline."""
    fd, why = _open_regular(path)
    if fd is None:
        return None, why
    try:
        size = os.fstat(fd).st_size
        if size > cap:
            return None, f"too big: {size}"
        chunks, got = [], 0
        while got < size:
            if clock() > deadline:
                raise _Out
            chunk = os.read(fd, min(1 << 16, size - got))
            if not chunk:
                break
            chunks.append(chunk)
            got += len(chunk)
        return b"".join(chunks), ""
    except OSError:
        return None, "unreadable"
    finally:
        os.close(fd)


def _digest(path: str, deadline: float, clock) -> str:
    data, why = _read(path, deadline, clock)
    return hashlib.sha256(data).hexdigest()[:16] if data is not None else f"!{why}"


def _stat_print(path: str) -> str:
    try:
        st = os.lstat(path)
    except OSError:
        return "gone"
    return f"{st.st_size}:{st.st_mtime_ns}"


def _tree_print(folder: str, deadline: float, clock) -> str:
    """One print for a folder and everything under it: names, sizes and times (not contents); links are named, never
    followed."""
    h = hashlib.sha256()
    for here, dirs, files in os.walk(folder):
        if clock() > deadline:
            raise _Out
        dirs.sort()
        for name in sorted([*files, *(d for d in dirs if _kind(os.path.join(here, d)) == "link")]):
            p = os.path.join(here, name)
            h.update(f"{os.path.relpath(p, folder)}:{_stat_print(p)}:{os.path.islink(p)}\n".encode())
    return h.hexdigest()[:16]


def _config_keys(text: str) -> list[str]:
    """`section.key` (or `section.sub.key`) for every setting in a git config file, lower-cased where git does."""
    out, section = [], ""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("[") and "]" in line:
            head = line[1 : line.index("]")].strip()
            name, _, sub = head.partition(" ")
            section = name.lower() + (("." + sub.strip().strip('"')) if sub else "")
        elif line and not line.startswith(("#", ";")) and section:
            out.append(f"{section}.{line.split('=')[0].strip().lower()}")
    return sorted(set(out))


def _git_print(git_dir: str, skip_set: set[str], deadline: float, clock) -> dict:
    """What matters about a git folder (a real folder: the caller has checked it is not a link): hooks that are not
    samples, config keys beyond a stock `git init`, the config's digest, and the submodule repositories."""
    hooks: dict[str, str] = {}
    hooks_dir = os.path.join(git_dir, "hooks")
    if _kind(hooks_dir) == "dir":
        for name in sorted(os.listdir(hooks_dir)):
            if not name.endswith(".sample"):
                hooks[name] = _digest(os.path.join(hooks_dir, name), deadline, clock)
    elif _kind(hooks_dir) != "gone":
        hooks["(hooks is not a folder)"] = _kind(hooks_dir)
    text, _ = _read(os.path.join(git_dir, "config"), deadline, clock)
    modules_dir = os.path.join(git_dir, "modules")
    pointer = os.path.join(git_dir, "commondir")
    return {
        "hooks": hooks,
        "extra_config": [k for k in _config_keys((text or b"").decode("utf-8", "replace")) if k not in STOCK_CONFIG],
        "config": _digest(os.path.join(git_dir, "config"), deadline, clock),
        "modules": sorted(os.listdir(modules_dir)) if _kind(modules_dir) == "dir" else [],
        "pointers": _digest(pointer, deadline, clock)
        if _kind(pointer) != "gone" and os.path.realpath(pointer) not in skip_set
        else "",
    }


def scan_project(
    project: str, skip: tuple[str, ...] = (), clock=time.monotonic
) -> tuple[dict[str, object], str | None]:
    """({key: print}, note): what is in the project that a person's next command could run. Keys are `git:REL` (a git
    folder or `.git` file, at any depth), `config:REL` (a `REPORT_NAMES` entry or a `.vscode` settings or tasks file),
    `auto:REL` (a virtual environment's `bin/*`, `*.pth` and `site-packages/*.py`; `node_modules/.bin/*` with what each
    link resolves to inside the project; the folder time of each package) and `pyc:REL` (compiled Python files). `skip`
    is paths not to look at (the placeholders of another session). Bounded: `SCAN_ENTRIES` entries or `SCAN_SECONDS`
    seconds, then it stops and the note says where. See the comment above for how entries are looked at."""
    found: dict[str, object] = {}
    seen, start, where = 0, clock(), "."
    deadline = start + SCAN_SECONDS
    skip_set = {os.path.realpath(p) for p in skip}
    real = os.path.realpath(project)

    def add(prefix: str, path: str, value) -> None:
        if os.path.realpath(os.path.dirname(path)) + "/" + os.path.basename(path) not in skip_set:
            found[f"{prefix}:{os.path.relpath(path, project)}"] = value

    def folder(prefix: str, path: str, build) -> None:
        kind = _kind(path)
        if kind == "link":
            add(prefix, path, _link_print(path))
        elif kind == "dir":
            value = build(path)
            if value is not None:  # a builder that adds its own entries returns None
                add(prefix, path, value)
        elif kind != "gone":
            add(prefix, path, {"special": kind})

    def regular(prefix: str, path: str, build) -> None:
        kind = _kind(path)
        if kind == "link":
            add(prefix, path, _link_print(path))
        elif kind == "file":
            add(prefix, path, build(path))
        elif kind != "gone":
            add(prefix, path, {"special": kind})

    def node_modules(path: str) -> None:
        bin_dir = os.path.join(path, ".bin")
        if _kind(bin_dir) == "dir":
            for name in sorted(os.listdir(bin_dir)):
                p = os.path.join(bin_dir, name)
                if _kind(p) == "link":  # the target text, and the file it resolves to if that is in the project
                    target = os.path.realpath(p)
                    inside = target.startswith(real + "/")
                    add(
                        "auto",
                        p,
                        {"link": os.readlink(p)[:200], "resolved": _stat_print(target) if inside else "outside"},
                    )
                else:
                    add("auto", p, _stat_print(p))
        try:
            packages = sorted(os.listdir(path))[:PACKAGES_CAP]
        except OSError:
            return
        for name in packages:
            if name.startswith("."):
                continue  # .bin (done above), .cache, .package-lock.json
            p = os.path.join(path, name)
            subs = (
                [os.path.join(p, s) for s in sorted(os.listdir(p))[:PACKAGES_CAP]]
                if name.startswith("@") and _kind(p) == "dir"
                else [p]
            )
            for q in subs:
                if _kind(q) == "dir":
                    add("auto", q, f"{_stat_print(q)}|{_stat_print(os.path.join(q, 'package.json'))}")

    def venv(path: str) -> None:
        for pattern in VENV_AUTO:
            for f in sorted(glob.glob(os.path.join(path, pattern))):
                if not os.path.realpath(os.path.dirname(f)).startswith(real + "/"):
                    continue  # reached through a link that leaves the project: not even its names are listed
                add("auto", f, _link_print(f) if _kind(f) == "link" else _stat_print(f))

    def pycache(path: str) -> None:
        for name in sorted(os.listdir(path)):
            if name.endswith(".pyc"):
                add("pyc", os.path.join(path, name), _stat_print(os.path.join(path, name)))

    try:
        for here, dirs, files in os.walk(project):
            seen += len(dirs) + len(files)
            where = os.path.relpath(here, project)
            if seen > SCAN_ENTRIES or clock() > deadline:
                raise _Out
            keep = []
            for d in dirs:
                p = os.path.join(here, d)
                if d == ".git":
                    folder("git", p, lambda q: _git_print(q, skip_set, deadline, clock))
                elif d in REPORT_NAMES:
                    folder("config", p, lambda q: _tree_print(q, deadline, clock))
                elif d == ".vscode":
                    if _kind(p) == "dir":
                        for name in REPORT_VSCODE:
                            regular("config", os.path.join(p, name), lambda q: _digest(q, deadline, clock))
                    else:
                        folder("config", p, lambda q: "")
                elif d == "node_modules" or d in VENV_DIRS:
                    folder("auto", p, node_modules if d == "node_modules" else venv)
                elif d == "__pycache__":
                    if _kind(p) == "dir":
                        pycache(p)
                else:
                    keep.append(d)
            dirs[:] = keep
            for f in files:
                p = os.path.join(here, f)
                if f == ".git":
                    regular(
                        "git",
                        p,
                        lambda q: {"pointer": (_read(q, deadline, clock, 200)[0] or b"").decode("utf-8", "replace")},
                    )
                elif f in REPORT_NAMES:
                    regular("config", p, lambda q: _digest(q, deadline, clock))
        return found, None
    except _Out:
        return found, f"INCOMPLETE: the scan stopped after {seen} entries in {clock() - start:.1f}s, at {where}"


def compare_scans(before: dict, after: dict, complete: bool = True) -> list[dict]:
    """The findings: every key in `after` that is new or has a different print. Removed ones are not findings. If the
    first scan was cut short, a key it did not reach cannot be called new: only changes are reported then. New `.pyc`
    files are named up to `PYC_SHOWN`, then counted."""
    out, pyc = [], []
    for key in sorted(after):
        change = (
            "new"
            if key not in before and complete
            else "changed"
            if key in before and before[key] != after[key]
            else None
        )
        if not change:
            continue
        kind, _, rel = key.partition(":")
        item = {
            "kind": kind,
            "path": rel,
            "change": change,
            "detail": after[key] if isinstance(after[key], dict) else "",
        }
        (pyc if kind == "pyc" else out).append(item)
    if len(pyc) > PYC_SHOWN:
        pyc = [
            *pyc[:PYC_SHOWN],
            {"kind": "pyc", "path": f"... and {len(pyc) - PYC_SHOWN} more", "change": "new", "detail": ""},
        ]
    return [*out, *pyc]


def describe(finding: dict) -> str:
    what = {
        "git": "a git folder",
        "config": "a runner or tool config",
        "auto": "an auto-loaded program",
        "pyc": "a compiled Python file",
    }[finding["kind"]]
    line = f"{finding['change']} {what}: {finding['path']}"
    d = finding["detail"]
    if isinstance(d, dict) and "link" in d:
        line += f" (a link to {d['link']})"
    elif isinstance(d, dict) and "special" in d:
        line += f" (not a regular file: {d['special']})"
    elif isinstance(d, dict) and "hooks" in d:
        extra = [f"hooks: {', '.join(d['hooks'])}"] if d["hooks"] else []
        extra += [f"config keys beyond a stock init: {', '.join(d['extra_config'])}"] if d["extra_config"] else []
        extra += [f"submodules: {', '.join(d['modules'])}"] if d["modules"] else []
        line += f" ({'; '.join(extra)})" if extra else ""
    return line


def report_run(
    home: Path, project: str, runner: str, before: dict, after: dict, notes: list[str], complete: bool = True
) -> list[dict]:
    """Print what the session left (stderr), and append one JSON line to `$WS_HOME/logs/wall-report.jsonl` (ws.jsonl:
    bounded, rotated, link-safe; the line carries `schema` and `ts`, and `at`, the legacy spelling of `ts`). Nothing is
    printed or written when there is nothing to say. A scan that stopped says INCOMPLETE on the first line: what is
    listed is then a lower bound. Never removes anything."""
    findings = compare_scans(before, after, complete)
    if not findings and not notes:
        return []
    stopped = any(n.startswith("INCOMPLETE") for n in notes)
    head = f"ws: wall report for {project}" + (" - INCOMPLETE, so this list is a lower bound:" if stopped else ":")
    lines = [head, "  the session left what you may run next, outside the wall:"] if findings else [head]
    lines += [f"  {describe(f)}" for f in findings]
    lines += [f"  note: {n}" for n in notes]
    lines.append("  Read these before you run git, a runner or a build here. ws removed nothing.")
    print("\n".join(lines), file=sys.stderr)
    try:
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "project": project,
            "runner": runner,
            "incomplete": stopped,
            "findings": findings,
            "notes": notes,
        }
        jsonl.append(home / "logs" / "wall-report.jsonl", jsonl.stamped(record, WALL_REPORT_SCHEMA))
    except OSError:
        pass  # the stderr lines are the report; a log that cannot be written is not worth failing the run for
    return findings


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
    _forward_signals(proc)
    return _wait(proc)


def _forward_signals(proc: subprocess.Popen) -> None:
    stop = [signal.SIGTERM, signal.SIGHUP]
    if not sys.stdin.isatty():  # at a terminal Ctrl-C reaches the runner itself, and the runner decides
        stop.append(signal.SIGINT)
    for sig in stop:
        signal.signal(sig, lambda *_: proc.terminate())  # the launcher stays up to tidy after the runner has gone


def _wait(proc: subprocess.Popen) -> int:
    while True:
        try:
            return proc.wait()
        except KeyboardInterrupt:  # Ctrl-C reaches the runner too; the runner decides
            continue


def run_plain(cmd: list[str], env: dict) -> int:
    """Start `cmd` as it is (Claude Code has its own sandbox: nothing to wrap) and stay beside it, to tidy up after."""
    proc = subprocess.Popen(cmd, env=env)
    _forward_signals(proc)
    return _wait(proc)


def launch_claude(args: argparse.Namespace, argv: list[str], home: Path, env: dict) -> int:
    """`ws gate launch --runner claude-code -- claude ...`: Claude Code's own sandbox, with settings made for THIS
    project at the start of the session. The shared settings (`ws gate generate`, `ws install`) are made once for every
    project, and a sandbox path has no project-relative spelling in a `--settings` file, so they carry the Edit rules but
    no project paths. Here the project is known: the locked set (ws.sandbox PROJECT_LOCKED, GIT_LOCKED, the git folders
    git resolves) is added to `denyWrite` in a copy under `$WS_HOME/gate/claude-code/projects/`, and `claude --settings`
    is started with it. No bubblewrap: Claude Code's sandbox is the wall. The end-of-run report is the launcher's, as
    for OpenCode."""
    from ws.adapters import claude_code

    out = home / "gate" / "claude-code"
    base = out / claude_code.FILE
    if not base.is_file():
        raise WsError(
            "not_found",
            f"no generated Claude Code settings at {base}",
            f"run `ws gate generate --runner claude-code --out {out}` (or `ws setup`) first",
        )
    try:
        project = project_folder(os.getcwd(), env)
    except SandboxError as e:
        raise WsError("refused", f"refusing to start claude-code: {e}", "start it from a project folder") from e
    target = out / "projects" / f"{project_key(project)}.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    target.write_text(claude_code.for_project(base.read_text(encoding="utf-8"), project), encoding="utf-8")
    cmd = [argv[0], "--settings", str(target), *argv[1:]]
    before, note = scan_project(project)
    notes = [note] if note else []
    old = _exit_on_hangup()
    try:
        return run_plain(cmd, env)
    except OSError as e:
        print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
        return 127
    finally:
        try:
            after, note = scan_project(project)
            report_run(home, project, "claude-code", before, after, [*notes, *([note] if note else [])], not notes)
        except OSError as e:
            print(f"ws: could not write the wall report: {e}", file=sys.stderr)
        for sig, handler in old.items():
            signal.signal(sig, handler)


def cmd_launch(args: argparse.Namespace) -> int:
    argv = list(args.argv)
    if argv[:1] == ["--"]:
        argv = argv[1:]
    if not argv:
        raise WsError(
            "usage", "give the runner's command after --", "e.g. `ws gate launch --runner opencode -- opencode`"
        )
    policy_file = Path(args.policy).expanduser().absolute() if args.policy else policy.policy_path()
    try:
        pol = policy.load(policy_file)
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), f"fix {policy_file}") from e
    home = ws_home().absolute()  # whatever --policy says, the gate keeps its approvals and sessions here
    env = launch_env(dict(os.environ))
    if args.runner == "claude-code" and not args.no_sandbox:
        return launch_claude(args, argv, home, env)
    if args.no_sandbox:
        print(NO_SANDBOX_NOTE.format(runner=args.runner), file=sys.stderr)
        try:
            os.execvpe(argv[0], argv, env)
        except OSError as e:
            print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
            return 127
        return 0  # exec does not return; this is for a test that replaces it. --no-sandbox never reaches the wall code
    if ptrace_scope() in (0, None) and sys.platform == "linux":
        print(PTRACE_NOTE, file=sys.stderr)
    tmp = tempfile.mkdtemp(prefix="ws-wall-")  # the wall's /tmp: the rest of the disk is read-only
    project, entered, before, notes = "", False, {}, []
    old = _exit_on_hangup()
    try:
        project = project_folder(os.getcwd(), env)
        homes = private_homes(home, args.runner, pol, env, project)
        before, note = scan_project(project)
        notes += [note] if note else []
        locked = enter_project(home, project)
        entered = True
        wrapped = wrap(
            argv,
            pol,
            home,
            protected(policy_file, args.runner, args.protect),
            writable=writable_folders(pol, args.runner, project, tmp, args.allow_write or [], env),
            locked=locked,
            homes=list(homes.values()),
            tty=sys.stdin.isatty(),
        )
        return run_with_broker(
            wrapped.argv, launch_env(dict(os.environ), tmp, homes), wrapped.pass_fds, approvals_dir(home)
        )
    except SandboxError as e:
        raise WsError(
            "refused", f"refusing to start {args.runner} without its wall: {e}", "or `--no-sandbox`, which has none"
        ) from e
    except OSError as e:
        print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
        return 127
    finally:
        made = leave_project(home, project) if entered else []
        if entered:
            try:
                after, note = scan_project(project, tuple(made))
                complete = not any("scan stopped" in n for n in notes)
                report_run(home, project, args.runner, before, after, [*notes, *([note] if note else [])], complete)
            except OSError as e:
                print(f"ws: could not write the wall report: {e}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)
        for sig, handler in old.items():
            signal.signal(sig, handler)


def _exit_on_hangup() -> dict:
    """Make a hang-up or a terminate before the runner starts leave through `finally` (placeholders taken down, the
    report written), like the handlers `run_with_broker` installs afterwards. Returns the old handlers, to restore."""
    old: dict = {}

    def leave(signum, _frame):
        raise SystemExit(128 + signum)

    try:
        for sig in (signal.SIGHUP, signal.SIGTERM):
            old[sig] = signal.signal(sig, leave)
    except ValueError:  # not the main thread
        pass
    return old


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
    p.set_defaults(func=cmd_launch, raw=True)  # it hands the terminal to the runner
