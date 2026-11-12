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
reached at all, which breaks "any provider, including local" (re-run in November 2026 with the local server's address
in `allowedDomains`: still refused, since on Linux the sandbox's loopback is its own and `allowLocalBinding` is
ignored). With no `allow_hosts` in the policy it also has no way to say "network as today". It is a 0.0.x research preview, and a Node dependency to pin on every machine. Its design is
copied where it fits (the points below); Claude Code, which has the same engine built in, keeps using its own.

Decisions, and why:
  * A write allow-list, not a deny-list. Root is read-only; writable are the project folder (where the runner is started;
    never the home folder or above it), a private tmp the launcher makes and points TMPDIR at, the runner's own private
    state folders (next point), `allow_write` from the policy and `--allow-write`. A deny-list left everything else
    writable: `~/.bashrc`, `~/.local/bin`, `~/.config/systemd/user` and ws's own installed source, each of which runs
    outside the wall at the next shell or the next hook. ws's own files (its four folders, the policy, the generated folder)
    are read-only by default and stay so even where one sits inside the project. The price: a program that writes a
    hard-coded path outside those (not TMPDIR) fails; the policy's `allow_write` is how a person lets one through. A
    read-only mount is enforced by the kernel for every process in the sandbox, including the runner's own tool and
    plugin code, which a path rule in the runner's config is not.
  * The runner's own state is private to the wall. OpenCode keeps programs in its XDG data and cache folders (`bin/rg`,
    `node_modules/.bin` plugins, language servers) and runs them later; if the wall let it write the person's real
    ones, a session could plant one that runs outside the wall at the next unwalled start (the P1 the third cold review
    found). So a walled run gets XDG_DATA_HOME, XDG_STATE_HOME and XDG_CACHE_HOME of its own under
    `<state>/wall/<runner>/` (`private_homes`): kept between walled runs, never read by an unwalled one. The person's
    real folders stay visible and read-only. The runner's sign-in files are copied across at each start, unless a
    deny_read rule hides them. `ws uninstall` takes the folder with the rest of the state folder.
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
  * Several sessions in one project share the placeholders. A record under `<state>/wall/projects/` (a file lock, the
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
    stderr and appends a line to `<state>/logs/wall-report.jsonl` (ws.jsonl), and removes nothing. This is what covers
    a runner started later in a sub-folder (`sub/.claude`), a repository the session creates with `git init` (which has
    to keep working), and a submodule added during the session. It runs as the person on a tree the session wrote, so
    it trusts nothing in it: every entry is lstat'd first, a link is reported as a link (its target text) and never read
    through (a planted `sub/.git` that links to a `deny_read` file would otherwise be copied into a log the next session
    reads), only regular files are opened (O_NOFOLLOW | O_NONBLOCK, a size cap, the time budget checked while reading; a
    FIFO named `.mcp.json` or a link to /dev/zero cannot hang it), and git is never run on it.
  * Claude Code has no bubblewrap wall, so `ws gate launch --runner claude-code -- claude` makes its own sandbox
    project-specific: it adds the locked set of THIS project (git folders resolved as above) to `denyWrite` in a copy of
    the generated settings under `<state>/gate/claude-code/projects/`, starts `claude --settings` with it, and writes the
    same end-of-run report. The shared settings carry the Edit deny rules (`**/NAME`) only, because a sandbox path has no
    project-relative spelling in a `--settings` file. See ws.adapters.claude_code.
  * A protected path reached through a symbolic link the session could replace is refused, with the real path in the
    message. A read-only bind holds the real path; delete a link in a writable folder and plant a folder under its name,
    and the hook's fixed --policy path reads the plant. (Links in folders the user cannot write are fine.)
  * Every ancestor of a protected path that is inside a writable folder is made a mount point. A read-only bind holds
    the path it covers, not the folders above it: `mv ~/.config ~/.config2; mkdir -p ~/.config/ws; echo x >
    ~/.config/ws/policy.toml` plants a policy where the hook's fixed path points. rename(2) refuses a mount point
    (EBUSY), so each such ancestor is bound onto itself. Nothing is refused: the binds cost nothing and change no access.
  * Two holes in that, on purpose: `<state>/approvals/pending` and `<state>/sessions` stay writable. The gate hook and
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
  * No `--unshare-net`: the runner needs the model's API, local or remote, and a model on this machine must stay
    reachable. A session of a data class (policy `[classes]`) gets default-deny egress instead, in the section on egress
    below: the class's hosts become Claude Code's `allowedDomains`, and for the other runners ws starts a proxy of its
    own and sets the proxy variables. On Linux that is the route and not a fence (a program that ignores the variables
    is not stopped by the wall; the host's firewall for the class's OS user is what closes it, and ws does not write
    one: it needs root). On macOS the profile also denies every outbound connection but those to the proxy and the model
    ports. A policy with no classes keeps the network as it was.
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
import base64
import contextlib
import errno
import glob
import hashlib
import hmac
import ipaddress
import json
import os
import platform
import posixpath
import re
import secrets as pysecrets
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
import urllib.parse
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import NamedTuple

from ws import adapters, approvals, jsonl, paths, policy
from ws.approvals import ENV_MARKER, FD_ENV, approvals_dir
from ws.output import WsError

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
    """The folders inside `home` (ws's state folder) that the wall leaves writable (see the module docstring)."""
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
    net: tuple[int, ...] | None = (
        None  # macOS: the only loopback ports the runner may connect to; None = network as today
    )

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
    if (
        plan.net is not None
    ):  # the proxy and the local model ports are the only way out; the unix-socket deny stays last
        lines.append("(deny network-outbound)")
        lines += [f'(allow network-outbound (remote ip "localhost:{p}"))' for p in plan.net]
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
    net: tuple[int, ...] | None = None,
    extra_hidden: list[str] = (),
) -> Wrapped:
    """`argv` inside the sandbox for this OS, with the descriptors that must stay open for it. `extra_hidden` is hidden
    besides the policy's `deny_read` paths (a walled class's other classes' folders). Raises SandboxError (with the
    install line) when it cannot be built."""
    system = system or _system()
    plan = Plan(
        home,
        tuple(protect),
        tuple(dict.fromkeys([*hidden_paths(pol, env), *extra_hidden])),
        tuple(socket_dirs(env)),
        tuple(writable),
        tuple(locked),
        tuple(homes),
        tuple(net) if net is not None else None,
    )
    link = plan.symlinked()
    if link:
        raise SandboxError(
            f"{link}: a protected path is reached through a symbolic link that the session could delete and replace, "
            "and the wall can only hold the real path. Point WS_HOME (or the XDG_*_HOME variables) and --policy at the real folders"
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
# The project paths Claude Code itself loads settings, hooks and MCP servers from. A Claude session holds them with its
# own sandbox's `denyWrite`, which can only hold a path that exists, so a missing one is made first (`claude_loaded`).
# Not the whole of `PROJECT_LOCKED`: the other names there are OpenCode's, and a Claude session leaves no placeholder
# for them in the person's `git status`.
CLAUDE_LOAD_PATHS = (".claude", ".mcp.json")
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


# What `ws gate launch` hands the runner from the launching shell. Everything else is left behind, so a key or token
# exported in that shell reaches the session only when the person names it with `--pass-env`. These names say where
# things are or how to draw the terminal; none is a credential.
LAUNCH_ENV = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "LANG", "LANGUAGE", "TZ", "TERM", "COLORTERM", "NO_COLOR",
    "COLUMNS", "LINES", "TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TMUX", "TMUX_PANE", "TMPDIR", "EDITOR", "VISUAL",
    "PAGER", "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "WS_HOME", "WS_OUTPUT", "WS_BROKER_SOCKET",
    "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_COLLATE", "LC_NUMERIC", "LC_TIME", "LC_MONETARY",
    "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME",
)  # fmt: skip
# The runner's own variables, besides the folder its adapter names in CONFIG_DIR_ENV. An adapter may list more in a
# `LAUNCH_ENV` tuple of its own.
RUNNER_ENV = {
    "claude-code": (
        "CLAUDE_CONFIG_DIR", "ANTHROPIC_MODEL", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
        "MCP_CONNECTION_NONBLOCKING", "MCP_TIMEOUT", "MCP_CONNECT_TIMEOUT_MS",
    ),
    "opencode": (
        "OPENCODE_CONFIG", "OPENCODE_CONFIG_CONTENT", "OPENCODE_DISABLE_AUTOUPDATE", "OPENCODE_DISABLE_MODELS_FETCH",
        "OPENCODE_DISABLE_LSP_DOWNLOAD",
    ),
}  # fmt: skip
ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def check_pass_env(names: list[str] | None) -> list[str]:
    """The `--pass-env` names, each a plain variable name (letters, digits, `_`), without repeats. Anything else is a
    usage error: a name is never taken from a `NAME=value` or a pattern."""
    out: list[str] = []
    for name in names or []:
        if not ENV_NAME.fullmatch(name):
            raise WsError(
                "usage", f"--pass-env takes a variable name, not {name!r}", "e.g. `--pass-env ANTHROPIC_API_KEY`"
            )
        if name not in out:
            out.append(name)
    return out


def passed_env(env: dict, runner: str = "", pass_env: list[str] | None = None) -> dict:
    """The part of `env` the runner may have: LAUNCH_ENV, the runner's own variables and the names the person passed. A name that isn't set is simply absent."""
    mod = adapters.get(runner) if runner else None
    own = (*RUNNER_ENV.get(runner, ()), *getattr(mod, "LAUNCH_ENV", ()), getattr(mod, "CONFIG_DIR_ENV", ""))
    names = {*LAUNCH_ENV, *own, *check_pass_env(pass_env)}
    return {k: v for k, v in env.items() if k in names}


def launch_env(
    env: dict,
    tmp: str | None = None,
    homes: dict | None = None,
    runner: str = "",
    pass_env: list[str] | None = None,
    layout_env: dict | None = None,
) -> dict:
    """The runner's environment: the allow-listed part of `env` (`passed_env`). In the wall, TMPDIR is the private tmp
    and so is Python's bytecode cache (PYTHONPYCACHEPREFIX): a `.pyc` written into the project by a walled `python`
    would be loaded by the next unwalled one, and the report would have to look for them. `layout_env` (from
    `paths.env_for`) tells the runner's hooks where ws's folders are, because `homes` moves the XDG variables they would
    otherwise find them by."""
    return {**passed_env(env, runner, pass_env), **(layout_env or {}), ENV_MARKER: "1", **wall_env(tmp, homes)}


def wall_env(tmp: str | None, homes: dict | None = None) -> dict:
    """The variables the wall itself sets for a runner in it: TMPDIR and the bytecode cache in the private `tmp`, and the
    runner's private XDG folders (`homes`)."""
    walled = {"TMPDIR": tmp, "PYTHONPYCACHEPREFIX": f"{tmp}/pycache"} if tmp else {}
    return {**walled, **(homes or {})}


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


def _wall_roots(env: dict) -> list[Path]:
    """The folders the wall makes read-only: those of the layout in use, and the other layouts' only where they exist
    (a link above a folder that isn't there is nothing a session could swap in)."""
    try:
        live = set(paths.roots(env))
    except WsError:
        live = set()  # an unclear layout: every folder that exists
    return [r for r in paths.protected_roots(env) if r in live or os.path.lexists(r)]


def protected(policy_file: Path, runner: str, extra: list[str] | None = None, env: dict | None = None) -> list[str]:
    """Everything the wall makes read-only besides ws's state folder itself: the policy (wherever --policy put it), the
    control files of both its folder and ws's own, every folder ws keeps its files in (in each layout it could use),
    anything named with --protect, and the generated config folder. With the allow-list most of this is read-only
    already; it matters where one of these sits inside a writable folder."""
    env = os.environ if env is None else env
    out = [
        str(policy_file),
        *policy.control_paths(policy_file.parent),
        *paths.control_paths(env),
        *map(str, _wall_roots(env)),
    ]
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

# XDG variable -> (folder under `wall/<runner>/` in ws's state folder, whether it is kept per project).
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


def private_homes(
    home: Path, runner: str, pol: policy.Policy, env: dict, project: str, extra_hidden: list[str] = ()
) -> dict[str, str]:
    """XDG data, state and cache folders for a walled run of `runner` in `project`, as {variable: folder}; {} for a
    runner that keeps no state of its own (one without `wall_writable` in its adapter, which is the marker).

    Why private. OpenCode keeps programs in the XDG cache and data folders (`bin/rg`, `node_modules/.bin` plugins,
    language servers) and runs them later, and the walled run has to write there. If it wrote the person's real
    folders, whatever a session planted would run outside the wall at the next unwalled start. So the walled run gets
    its own, under `wall/<runner>/` in ws's state folder, which no unwalled run is pointed at; the real ones stay visible
    and read-only. Why there and not the private tmp: the session history and the model cache are worth keeping between
    runs, and `ws uninstall` removes the state folder. They are made 0700, and a `.gitignore` of `*` keeps them out of a
    synced state folder.

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
    hidden = [*hidden_paths(pol, env), *extra_hidden]
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


def under_hidden(entries: list[str], hidden: list[str], home: str) -> list[str]:
    """The `entries` (`~/x` or `/x`) whose real path is one of the `hidden` folders (the same spellings) or inside one:
    what the launch copy leaves out of `allowRead` because another class's folder is denied. `home` is what `~` means."""

    def real(p: str) -> str:
        return os.path.realpath(home + p[1:] if p == "~" or p.startswith("~/") else p)

    folders = [real(h) for h in hidden]
    return [e for e in entries if any(real(e) == f or _inside(real(e), f) for f in folders)]


class GitFacts(NamedTuple):
    hooks: str | None  # the core.hooksPath folder, when it is inside the project
    files: list[str]  # config files in the project that git reads (an `[include]` of one)
    dirs: list[str]  # git folders inside the project: the one git finds from here, and its common folder


_git_off = False


@contextlib.contextmanager
def no_git():
    """Within the block every git question below answers as if git were missing and no process starts: for a caller that
    must start none (`ws walls check`) and asks about a folder's launch settings."""
    global _git_off
    before, _git_off = _git_off, True
    try:
        yield
    finally:
        _git_off = before


def _git(project: str, *args: str) -> str | None:
    """`git -C project ARGS` output, or None when git is missing, fails or takes longer than 10 seconds."""
    if _git_off:
        return None
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


def git_read_folders(project: str) -> list[str]:
    """The git folder and the common git folder of `project` that sit OUTSIDE it, as real paths: a linked worktree's
    folder under the main checkout's `.git`, a `--separate-git-dir`. A shell that cannot read the home folder needs them
    for git to work in the project (the ones inside the project are readable with it). Read with `git rev-parse`, which
    runs nothing. Empty where git is missing, this is not a repository, `.git` is a link (never run git on one), or git
    takes longer than 10 seconds. A `.git` file (a linked worktree) is followed out of the project only to a folder under
    the `worktrees/` folder of the common git folder; a pointer to anywhere else is left out."""
    real = os.path.realpath(project)
    dotgit = os.path.join(real, ".git")
    if os.path.islink(dotgit):
        return []
    where = _git(real, "rev-parse", "--absolute-git-dir", "--git-common-dir")
    if where is None or len(where.splitlines()) != 2:
        return []
    found = [os.path.realpath(os.path.join(real, f)) for f in where.splitlines()]
    if os.path.isfile(dotgit) and not _is_worktree(*found):
        return []  # a pointer file that names some other folder is not a worktree: it is not followed out of the project
    return [d for d in dict.fromkeys(found) if os.path.isdir(d) and not _inside(d, real) and d != real]


def _is_worktree(git_dir: str, common: str) -> bool:
    """True when `git_dir` is `<common>/worktrees/<name>`: where `git worktree add` puts a linked worktree's git folder."""
    parent, name = os.path.split(git_dir)
    return bool(name) and os.path.basename(parent) == "worktrees" and os.path.dirname(parent) == common


def git_facts(project: str) -> GitFacts:
    """What git, started in `project`, would load that is not a plain file under `.git`: the git folders it resolves
    (`rev-parse --absolute-git-dir --git-common-dir`; a `--separate-git-dir` or a worktree can put them anywhere in the
    project), the folder named by core.hooksPath, and the config files pulled in by `[include]`. All of it is code to
    git: a hook runs at the next commit, and a config sets `core.fsmonitor`, an alias or a credential helper. Read with
    `git rev-parse` and `git config`, which run nothing. `.git` itself is always counted when it is a folder. Empty
    where git is missing, this is not a repository, or git takes longer than 10 seconds."""

    real = os.path.realpath(project)
    if os.path.islink(os.path.join(real, ".git")):
        return GitFacts(None, [], [])  # never run git on a linked `.git`; lock_project refuses it
    dirs = [os.path.join(real, ".git")] if os.path.isdir(os.path.join(real, ".git")) else []
    where = _git(project, "rev-parse", "--show-toplevel", "--absolute-git-dir", "--git-common-dir")
    listing = _git(project, "config", "--list", "--show-origin", "-z")
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


def lock_project(project: str, names: tuple[str, ...] | None = None) -> tuple[list[str], list[tuple[str, str]]]:
    """(locked, created): the paths for the wall to hold read-only, and the placeholders made so a missing one cannot be
    created inside the wall. bubblewrap can only bind what is there, and a read-only bind of a folder or file that is
    in the way is what stops `mkdir .claude` and `echo '{...}' > .mcp.json`. They are made here, by the launcher, before
    the wall, and removed by `unlock_project` after it (only if still what was made; see `enter_project` for who
    removes them when several sessions share the project). Raises SandboxError for a locked path that is a symbolic
    link: it could be deleted and replaced by a real folder, which no bind prevents. `names` limits it to those paths of
    the project (relative to its root, as `PROJECT_LOCKED` spells them); the default is all of `lock_wanted`."""
    locked: list[str] = []
    created: list[tuple[str, str]] = []
    wanted = lock_wanted(project)
    if names is not None:
        wanted = [(p, fill) for p, fill in wanted if os.path.relpath(p, project) in names]
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
    (`wall/projects/<key>.json` in ws's state folder, outside the wall's reach: it is read-only in the wall)."""
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


def enter_project(home: Path, project: str, names: tuple[str, ...] | None = None) -> list[str]:
    """Lock the project for this launcher and record it. Placeholders are shared: a second walled session in the same
    project finds them and adds its pid, and only the last one out removes them (`leave_project`); the first launcher
    taking them down under a second one would detach the read-only bind in the second's mount namespace. If every
    launcher the record names is dead (killed), the placeholders it left are taken down first. `names` is passed to
    `lock_project`. Returns the locked paths."""
    with _project_state(home, project) as state:
        if not state["pids"] and state["created"]:
            unlock_project([tuple(c) for c in state["created"]])
            state["created"] = []
        locked, created = lock_project(project, names)
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
    """Print what the session left (stderr), and append one JSON line to `<state>/logs/wall-report.jsonl` (ws.jsonl:
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
        now = time.time()  # one clock read feeds both `ts` and the legacy `at`
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "project": project,
            "runner": runner,
            "incomplete": stopped,
            "findings": findings,
            "notes": notes,
        }
        jsonl.append(home / "logs" / "wall-report.jsonl", jsonl.stamped(record, WALL_REPORT_SCHEMA, now))
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


# --- egress: the network, shut by default per data class ---------------------------------------------------------------
#
# What a session of a class may reach is the class's `allowed_hosts`, the host of the model provider it was started
# with, and the hosts of the `[[egress_exceptions]]` that name the class and have not expired. Nothing else. The same
# list feeds two enforcers: Claude Code's own sandbox proxy (as `sandbox.network.allowedDomains`, written by
# claude_code.for_project and the class file), and, for a runner with no proxy of its own (OpenCode), the small proxy
# below, which `ws gate launch` starts beside the runner. The proxy is ws's own because the one that exists puts the
# whole runner in a network namespace of its own, where a model on this machine cannot be reached (top of this file).

# Hosts a named set of services needs, for `explain_host` (a policy lists hosts; it does not name these sets).
PRESETS = {
    "claude-code": ("api.anthropic.com", "platform.claude.com"),
    "openai": ("api.openai.com",),
    "pypi": ("pypi.org", "files.pythonhosted.org"),
    "npm": ("registry.npmjs.org",),
    "github": ("github.com", "api.github.com"),
    "huggingface": ("huggingface.co", "*.huggingface.co"),
}
# The hosts a model provider is reached at, added to the allowlist of a session started with `--provider NAME`.
PROVIDER_HOSTS = {
    "anthropic": PRESETS["claude-code"],
    "openai": PRESETS["openai"],
    "google": ("generativelanguage.googleapis.com",),
    "openrouter": ("openrouter.ai",),
}
LOCAL_PROVIDERS = ("local", "ollama", "lmstudio", "vllm", "llamacpp")  # a model that runs on this machine
DEFAULT_PROVIDER = {"claude-code": "anthropic"}  # what a runner talks to unless `--provider` says otherwise
LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")
# Tools that open a socket themselves and ignore the proxy variables. In a session with an egress plan each is replaced
# on PATH by a shim that says so and exits 126, so the attempt is a visible event and not a silent timeout.
SHIM_TOOLS = ("nc", "ncat", "netcat", "telnet", "ssh", "scp", "sftp", "ftp", "socat")
SHIM_LOG = "egress-denied.log"  # one line per refused tool, in the launch's temporary folder; only counted
# Instance-metadata endpoints (IPv4 as octets, so no address is written out as text: the PII gate would refuse it).
_METADATA = (
    *(
        ipaddress.ip_network((ipaddress.IPv4Address(bytes(o)), 32))
        for o in ((169, 254, 169, 254), (100, 100, 100, 200))
    ),
    *(ipaddress.ip_network((ipaddress.IPv4Address(bytes(o)), 32)) for o in ((168, 63, 129, 16), (192, 0, 0, 192))),
    ipaddress.ip_network("fd00:ec2::254/128"),
    ipaddress.ip_network("fd20:ce::254/128"),
)
_NAT64 = ipaddress.ip_network("64:ff9b::/96")
_LEGACY_IP = re.compile(r"(0x[0-9a-f]+|[0-9]+)(\.(0x[0-9a-f]+|[0-9]+))*")


def split_entry(entry: str) -> tuple[str, int | None]:
    """An allow entry as `(host, port or None)`: lower case, no trailing dot. A port is a number; anything else after the
    colon leaves the host as written, which `entry_problem` then refuses."""
    host, _, port = entry.strip().lower().partition(":")
    return host.rstrip("."), (int(port) if port.isdigit() else None)


def _ip(host: str):
    try:
        return ipaddress.ip_address(host.strip("[]").split("%")[0])
    except ValueError:
        return None


def _is_loopback(host: str) -> bool:
    ip = _ip(host)
    return host in LOOPBACK_NAMES or (ip is not None and ip.is_loopback)


_DEST = re.compile(r"[a-z0-9.-]+")
_SECOND_LEVEL = (
    "ac",
    "co",
    "com",
    "edu",
    "go",
    "gov",
    "ne",
    "net",
    "or",
    "org",
)  # registry labels under a country code


def valid_destination(host: str) -> bool:
    """May `host` be looked at as a destination at all? Letters, digits, dots and hyphens, or an IPv6 address (brackets
    allowed). Anything else (a NUL, a space, `%`, `@`, `/`) is refused before it is matched or resolved, since a resolver
    may read such a name differently from a matcher: `8.8.8.8\\x00.example.org` is a match for `*.example.org` to one and
    an address to the other."""
    h = host.lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):
        h = h[1:-1]
    if _DEST.fullmatch(h):
        return True
    return ":" in h and re.fullmatch(r"[0-9a-f:.]+", h) is not None and _ip(h) is not None


def legacy_literal(host: str) -> bool:
    """True for a host written as digits and dots (or hex) that is not a plain dotted address: `127.1`, `2130706433`,
    `0x7f.1`, `0177.0.0.1`. A resolver may read these as an address while a string match sees a name; they are refused."""
    h = host.lower().rstrip(".")
    return bool(_LEGACY_IP.fullmatch(h)) and _ip(h) is None


def entry_problem(entry: str) -> str | None:
    """Why `entry` can not be an allow-list entry as the proxies read it, or None. This is the policy's host check plus
    what the proxies need: a name has a dot (or is `localhost`), a loopback entry names its port, and a host written in
    a form a resolver reads as an address is refused."""
    try:
        policy.check_host(entry, "entry")
    except policy.PolicyError as e:
        return str(e).removeprefix("entry: ")
    host, port = split_entry(entry)
    if not valid_destination(host.removeprefix("*.")):
        return f"{entry!r} has characters a host name does not"
    if legacy_literal(host.removeprefix("*.")):
        return f"{entry!r} is an address in a form resolvers read differently; write it as four decimal numbers"
    if _is_loopback(host):
        return None if port is not None else f"{entry!r} is loopback: name the port of the model server"
    if (ip := _ip(host)) is not None:
        kind = blocked_address(ip)
        if kind is not None:
            return f"{entry!r} is a {kind} address; a session does not reach those, listed or not"
    base = host.removeprefix("*.").split(".")
    if host.startswith("*.") and len(base) == 2 and len(base[1]) == 2 and base[0] in _SECOND_LEVEL:
        return f"{entry!r} is a wildcard over a whole registry (like *.co.uk); name the domain under it"
    if _ip(host) is None and "." not in host:
        return f"{entry!r} has no dot; the proxies read only fully qualified names (and localhost)"
    return None


def host_matches(host: str, port: int, entry: str) -> bool:
    """Does the allow entry cover a request to `host:port`? A name matches itself; `*.name` matches every name below it
    but not `name` and never an address; an entry with a port matches only that port, one without matches any."""
    pat, pport = split_entry(entry)
    h = host.strip().lower().rstrip(".").strip("[]")
    if pport is not None and pport != port:
        return False
    if pat.startswith("*."):
        return _ip(h) is None and h.endswith("." + pat[2:])
    a, b = _ip(h), _ip(pat)
    return a == b if a is not None and b is not None else h == pat


def blocked_address(ip) -> str | None:
    """The kind of address `ip` is when a session must not reach it by name, or None for a public one: `metadata`,
    `unspecified`, `loopback`, `link-local`, `multicast` or `private`. An IPv4 address carried in an IPv6 one (mapped,
    6to4, the NAT64 prefix) is judged as the IPv4 address it holds."""
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and ip in _NAT64:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)
        ip = inner or ip
    if any(ip in net for net in _METADATA if net.version == ip.version):
        return "metadata"
    if ip.is_unspecified:
        return "unspecified"
    if ip.is_loopback:
        return "loopback"
    if ip.is_link_local:
        return "link-local"
    if ip.is_multicast:
        return "multicast"
    return None if ip.is_global else "private"


@dataclass(frozen=True)
class Decision:
    allow: bool
    reason: str  # the text of a refusal: it never names another host or the class's list
    via: str = ""  # "class", "exception:NAME" or ""


@dataclass(frozen=True)
class EgressPlan:
    """What one session may reach. `hosts` is the class's list plus the provider's; `granted` are the exceptions in force
    (name, hosts) and `lapsed` the ones that name the class but have expired: they cover nothing, and a request they
    would have covered is refused with their name. `dropped` are entries the proxies can not read (never applied)."""

    cls: str
    provider: str
    hosts: tuple[str, ...] = ()
    granted: tuple[tuple[str, tuple[str, ...], datetime | None], ...] = ()
    lapsed: tuple[tuple[str, tuple[str, ...]], ...] = ()
    dropped: tuple[str, ...] = ()

    def domains(self) -> list[str]:
        """The list for `sandbox.network.allowedDomains`: every entry in force, once."""
        return list(dict.fromkeys([*self.hosts, *(h for _, hs, _ in self.granted for h in hs)]))

    def local_ports(self) -> tuple[int, ...]:
        """The loopback ports of the list: where a model on this machine listens."""
        found = []
        for e in self.domains():
            host, port = split_entry(e)
            if port is not None and _is_loopback(host):
                found.append(port)
        return tuple(dict.fromkeys(found))

    def decide(self, host: str, port: int, now: datetime | None = None) -> Decision:
        """Is a request to `host:port` allowed? An exception is judged at `now` (default: the clock), so one that ends
        during a session stops covering from that moment."""
        h = host.lower().rstrip(".")
        if not isinstance(host, str) or not h or isinstance(port, bool) or not isinstance(port, int):
            return Decision(False, "not a destination")
        if not 1 <= port <= 65535 or not valid_destination(h):
            return Decision(False, "not a destination")
        if legacy_literal(h):
            return Decision(False, "an address written in a form resolvers read differently")
        if any(host_matches(h, port, e) for e in self.hosts):
            return Decision(True, "allowed by the class", "class")
        now = datetime.now(UTC) if now is None else now
        ended = None  # an exception that covers the host but has lapsed: it denies only if no other one is in force
        for name, hs, ends in self.granted:
            if any(host_matches(h, port, e) for e in hs):
                if ends is not None and now >= ends:
                    ended = ended or name
                    continue
                return Decision(True, f"allowed by exception {name!r}", f"exception:{name}")
        for name, hs in self.lapsed:
            if any(host_matches(h, port, e) for e in hs):
                ended = ended or name
        if ended:
            return Decision(False, f"exception {ended!r} has expired", f"exception:{ended}")
        return Decision(False, "not in this class's allowlist")

    def address_ok(self, host: str, ip, port: int) -> bool:
        """May a connection for `host:port` go to `ip`? A public address, yes. A metadata address, never. Loopback only for
        a loopback name or address on a port the list names (a model server): a name that resolves to loopback (DNS
        rebinding) is refused. Another non-public address only when `host` is that address, written out in the list."""
        kind = blocked_address(ip)
        if kind is None:
            return True
        if kind == "metadata":
            return False
        h = host.strip().lower().rstrip(".").strip("[]")
        if kind == "loopback":
            return (_is_loopback(h) or h.endswith(".localhost")) and port in self.local_ports()
        return _ip(h) is not None and _ip(h) == ip


def egress_plan(pol: policy.Policy, cls: policy.Class | None, provider: str | None, now: datetime | None = None):
    """The plan for a session of class `cls` using `provider`, or None when the policy has no classes (nothing is
    classified: today's behaviour). `local_only` keeps loopback entries and nothing else. An exception that names the
    class is in force until it expires; an expired one is listed in `lapsed` and covers nothing. `now` is aware (UTC)."""
    if cls is None:
        return None
    now = datetime.now(UTC) if now is None else now
    prov = (provider or "").lower()
    wanted = [*cls.allowed_hosts, *(() if prov in LOCAL_PROVIDERS else PROVIDER_HOSTS.get(prov, ()))]
    granted, lapsed = [], []
    for x in pol.egress_exceptions:
        if cls.name in x.classes:
            if x.expired(now):
                lapsed.append((x.name, x.hosts))
            else:
                granted.append((x.name, x.hosts, x.expires))

    def keep(entries) -> tuple[list[str], list[str]]:
        ok, bad = [], []
        for e in entries:
            if cls.local_only and not _is_loopback(split_entry(e)[0]):
                continue
            (bad if entry_problem(e) else ok).append(e)
        return ok, bad

    hosts, dropped = keep(wanted)
    kept = []
    for name, hs, ends in granted:
        ok, bad = keep(hs)
        dropped += bad
        kept.append((name, tuple(ok), ends))
    return EgressPlan(
        cls.name,
        prov,
        tuple(dict.fromkeys(hosts)),
        tuple(kept),
        tuple((n, tuple(hs)) for n, hs in lapsed),
        tuple(dropped),
    )


def job_provider(runner: str, model: str = "", options: dict | None = None) -> str:
    """The model provider a headless job uses: the runner's own (claude-code is anthropic), else the job's `provider`
    option, else the part of `provider/model` before the slash, else none (a limited class then refuses it)."""
    if runner in DEFAULT_PROVIDER:
        return DEFAULT_PROVIDER[runner]
    named = (options or {}).get("provider")
    if isinstance(named, str) and named:
        return named
    return model.split("/", 1)[0] if "/" in model else ""


def class_domains(cls: policy.Class) -> list[str]:
    """The hosts a class file (managed settings) holds for `cls`, for the sessions that start with no `--provider`: its
    `allowed_hosts` and the host of each provider it allows by name, entries the proxies can read only. A class that
    does not name its providers gets none of theirs, and `local_only` keeps loopback entries and nothing else."""
    named = () if cls.local_only else (cls.allowed_providers or ())
    wanted = [*cls.allowed_hosts, *(h for pv in named if pv not in LOCAL_PROVIDERS for h in PROVIDER_HOSTS.get(pv, ()))]
    keep = [e for e in wanted if not entry_problem(e) and (not cls.local_only or _is_loopback(split_entry(e)[0]))]
    return list(dict.fromkeys(keep))


def provider_problem(cls: policy.Class | None, provider: str | None) -> str | None:
    """Why a session of `cls` may not use `provider`, or None. The words name the class and nothing else. No classes
    configured (None) allows anything. A class that does not say (`allowed_providers` unset) allows any provider if it is
    `full` and only a model on this machine otherwise; `[]` allows none; `local_only` allows only a local model."""
    if cls is None:
        return None
    name = (provider or "").lower()
    allowed = cls.allowed_providers
    if allowed is None:
        allowed = None if cls.access == "full" else LOCAL_PROVIDERS
    if not name:
        if allowed is None and not cls.local_only:
            return None
        return f"class {cls.name!r} limits the model provider and none was named"
    if not re.fullmatch(r"[a-z0-9_-]+", name) or (allowed is not None and name not in allowed):
        return f"class {cls.name!r} does not allow that model provider"
    if cls.local_only and name not in LOCAL_PROVIDERS:
        return f"class {cls.name!r} allows only a model that runs on this machine"
    return None


def session_class(pol: policy.Policy, cwd: str | None = None) -> policy.Class | None:
    """The class of the folder a session starts in (None: the policy has no classes). The real path decides; a folder that
    can't be read is refused (exit 3), never guessed. `cwd` is the folder the session works in when it is not this
    process's own (a headless job's); the default is this process's current folder."""
    try:
        here = os.path.realpath(os.getcwd() if cwd is None else cwd)
    except OSError as e:
        raise WsError(
            "refused",
            f"refusing to start: the current folder can't be read ({e.strerror}), so its class is not known",
            "start it from a project folder",
            3,
        ) from e
    return policy.class_of(pol, here)


def require_provider(cls: policy.Class | None, provider: str | None) -> None:
    """Refuse (exit 3) a session whose model provider the class does not allow. Before anything is written or started."""
    why = provider_problem(cls, provider)
    if why:
        raise WsError(
            "refused",
            f"refusing to start: {why}",
            "use a provider the class allows (`--provider NAME`), or ask whoever owns the policy to change the class",
            3,
        )


def explain_host(pol: policy.Policy, cls: policy.Class | None, host: str, port: int = 443, now=None) -> str:
    """Why a request to `host:port` from a session of `cls` is allowed or refused, and the policy line that would change
    it. A refusal says which kind it is (the list, an expired exception), and an allowed host says that a 401 or 403 from
    it is the service's answer and not the proxy's."""
    plan = egress_plan(pol, cls, "", now)
    if plan is None:
        return "no classes are configured, so ws sets no allowlist; the runner's own network rules apply"
    d = plan.decide(host, port)
    if d.allow:
        return f"allowed ({d.reason}); a 401 or 403 from it is the service's own answer, not the proxy's"
    if d.via:
        return f"refused: {d.reason}; ask its owner to renew it, or add the host to the class's allowed_hosts"
    covers = [n for n, hs in PRESETS.items() if any(host_matches(host, port, e) for e in hs)]
    if covers:
        add = ", ".join(f'"{h}"' for h in PRESETS[covers[0]])
        return f"refused: {d.reason}; it is part of the {covers[0]} set: add {add} to allowed_hosts in [classes.{plan.cls}]"
    return f'refused: {d.reason}; to allow it add "{host}" to allowed_hosts in [classes.{plan.cls}]'


class EgressProxy:
    """A forward proxy on 127.0.0.1 that carries `CONNECT host:port` and plain `http://` requests only to what the plan
    allows, and connects to an address it has checked itself (so a name that resolves somewhere else a moment later
    changes nothing). Stdlib only. `denials` collects `(via, reason)` for each refusal; `on_deny(host, port, decision)` is
    called too. It is the route and not a fence where the operating system does not confine the runner (see the wall)."""

    def __init__(self, plan: EgressPlan, on_deny=None, resolver=None, timeout: float = 15.0, secret=None, clock=None):
        self.plan, self.on_deny, self.timeout, self.secret = plan, on_deny, timeout, secret
        self.clock = clock or (lambda: datetime.now(UTC))
        self.resolver = resolver or socket.getaddrinfo
        self.denials: list[tuple[str, str]] = []
        self._srv: socket.socket | None = None
        self.port = 0

    def start(self) -> int:
        srv = socket.create_server(("127.0.0.1", 0))
        self._srv, self.port = srv, srv.getsockname()[1]
        threading.Thread(target=self._accept, args=(srv,), daemon=True).start()
        return self.port

    def stop(self) -> None:
        srv, self._srv = self._srv, None
        if srv is not None:
            with contextlib.suppress(OSError):
                srv.shutdown(socket.SHUT_RDWR)  # wakes a thread blocked in accept(); close alone does not on Linux
            srv.close()

    def _accept(self, srv: socket.socket) -> None:
        srv.settimeout(0.5)  # where shutdown() does not wake accept(), the loop still sees the proxy has stopped
        while self._srv is srv:
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            conn.settimeout(None)
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _deny(self, conn: socket.socket, host: str, port: int, d: Decision, status: str = "403 Forbidden") -> None:
        self.denials.append((d.via or "list", d.reason))
        if self.on_deny:
            with contextlib.suppress(Exception):
                self.on_deny(host, port, d)
        body = f"ws: {d.reason}\n".encode()
        head = f"HTTP/1.1 {status}\r\nX-Ws-Egress: refused\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        with contextlib.suppress(OSError):
            conn.sendall(head.encode() + body)

    def _dial(self, host: str, port: int) -> socket.socket | str:
        """A connection to a checked address of `host`, or the reason there is none."""
        try:
            infos = self.resolver(host.strip("[]"), port, type=socket.SOCK_STREAM)
        except (OSError, UnicodeError):  # a name the resolver can not encode (an empty label) is a refusal, logged
            return "does not resolve"
        why = "no usable address"
        for family, _, _, _, addr in infos:
            ip = _ip(addr[0])
            if ip is None or not self.plan.address_ok(host, ip, port):
                why = f"resolves to a {blocked_address(ip) or 'refused'} address" if ip is not None else why
                continue
            s = socket.socket(family, socket.SOCK_STREAM)
            s.settimeout(self.timeout)
            try:
                s.connect(addr)
            except OSError:
                s.close()
                why = "could not connect"
                continue
            s.settimeout(None)
            return s
        return why

    def _request(self, lines: list[str]) -> tuple[str, int, bytes | None]:
        """(host, port, head to send on) of a request; the head is None for CONNECT. ValueError when it is neither."""
        if any("\n" in x or "\r" in x for x in lines):  # a bare LF would start a header the checks below never see
            raise ValueError("bare line feed")
        method, target, version = lines[0].split(" ")
        if method.upper() == "CONNECT":
            return (*_split_hostport(target, 443), None)
        u = urllib.parse.urlsplit(target)
        if u.scheme != "http" or not u.hostname:
            raise ValueError(target)
        path = (u.path or "/") + (f"?{u.query}" if u.query else "")
        if not valid_destination(u.hostname):
            raise ValueError(target)
        port = u.port or 80
        drop = ("proxy-connection", "connection", "proxy-authorization", "host")
        kept = [x for x in lines[1:] if x.split(":", 1)[0].strip().lower() not in drop]
        shown = f"[{u.hostname}]" if ":" in u.hostname else u.hostname
        target_host = shown if port == 80 else f"{shown}:{port}"  # the Host the server sees is the one that was checked
        head = "\r\n".join([f"{method} {path} {version}", f"Host: {target_host}", *kept, "Connection: close"])
        return u.hostname, port, (head + "\r\n\r\n").encode("latin-1")

    def _authorized(self, lines: list[str]) -> bool:
        """True when no secret is set, or the request carries it as the password of Basic proxy credentials."""
        if not self.secret:
            return True
        for x in lines[1:]:
            name, _, value = x.partition(":")
            if name.strip().lower() == "proxy-authorization":
                scheme, _, token = value.strip().partition(" ")
                try:
                    given = base64.b64decode(token.strip(), validate=True).decode("utf-8").partition(":")[2]
                except ValueError:
                    return False
                return scheme.lower() == "basic" and hmac.compare_digest(given.encode(), str(self.secret).encode())
        return False

    def _serve(self, conn: socket.socket) -> None:
        up: socket.socket | str | None = None
        try:
            conn.settimeout(self.timeout)
            data = b""
            while b"\r\n\r\n" not in data:
                chunk = conn.recv(8192)
                if not chunk or len(data) > 65536:
                    return
                data += chunk
            top, _, rest = data.partition(b"\r\n\r\n")
            lines = top.decode("latin-1").split("\r\n")
            if not self._authorized(lines):
                head = 'HTTP/1.1 407 Proxy Authentication Required\r\nProxy-Authenticate: Basic realm="ws"\r\n'
                conn.sendall((head + "Content-Length: 0\r\nConnection: close\r\n\r\n").encode())
                return
            try:
                host, port, outgoing = self._request(lines)
            except ValueError:
                self._deny(conn, "", 0, Decision(False, "not a proxy request"), "400 Bad Request")
                return
            d = self.plan.decide(host, port, self.clock())
            if not d.allow:
                self._deny(conn, host, port, d)
                return
            up = self._dial(host, port)
            if isinstance(up, str):
                self._deny(conn, host, port, Decision(False, up, "address"))
                return
            if outgoing is None:
                conn.sendall(b"HTTP/1.1 200 Connection Established\r\n\r\n")
                if rest:
                    up.sendall(rest)
            else:
                up.sendall(outgoing + rest)
            conn.settimeout(None)
            back = threading.Thread(target=_pipe, args=(up, conn), daemon=True)
            back.start()
            _pipe(conn, up)
            back.join()
        except OSError:
            pass
        finally:
            for s in (conn, up):
                if isinstance(s, socket.socket):
                    with contextlib.suppress(OSError):
                        s.close()


def _pipe(src: socket.socket, dst: socket.socket) -> None:
    try:
        while data := src.recv(65536):
            dst.sendall(data)
    except OSError:
        pass
    finally:
        with contextlib.suppress(OSError):
            dst.shutdown(socket.SHUT_WR)


def _split_hostport(target: str, default: int) -> tuple[str, int]:
    """`host:port`, `[v6]:port` or `host` as (host, port); ValueError for anything else."""
    if target.startswith("["):
        host, closed, port = target[1:].partition("]")
        if not closed:
            raise ValueError(target)
        port = port.removeprefix(":")
    else:
        host, _, port = target.partition(":")
    if not host or (port and not (port.isascii() and port.isdigit())) or not valid_destination(host):
        raise ValueError(target)
    return host, int(port) if port else default


def new_secret() -> str:
    """A fresh password for one proxy: another session's proxy, or a program that just finds the port, can not use it."""
    return pysecrets.token_urlsafe(24)


def proxy_env(port: int, secret: str | None = None) -> dict:
    """The variables that send a runner's traffic through the proxy, with `secret` as the password of its credentials.
    Loopback stays off the proxy, so a model on this machine is reached directly (the macOS wall allows exactly those
    ports: `Plan.net`): that also means a loopback port the session can reach is not filtered. Both spellings, since
    tools differ."""
    url = f"http://ws:{secret}@127.0.0.1:{port}" if secret else f"http://127.0.0.1:{port}"
    env = {k: url for k in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")}
    env.update(NO_PROXY="localhost,127.0.0.1,::1", no_proxy="localhost,127.0.0.1,::1", NODE_USE_ENV_PROXY="1")
    return env


def write_deny_shims(folder: str) -> str:
    """Make `folder/bin` with one script per SHIM_TOOLS name that appends a line to `folder/SHIM_LOG`, says why on stderr
    and exits 126; return the `bin` path, for the front of PATH. A tool called by its full path is not caught: the
    proxy and the host's firewall are what stop that."""
    bin_dir = Path(folder) / "bin"
    bin_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    log = shlex.quote(str(Path(folder) / SHIM_LOG))
    for name in SHIM_TOOLS:
        script = (
            "#!/bin/sh\n"
            f'printf \'%s\\n\' \'{{"event":"command_denied","tool":"{name}"}}\' >> {log} 2>/dev/null\n'
            f"echo 'ws: {name} is blocked in this session: it opens a network connection ws does not filter' >&2\n"
            "exit 126\n"
        )
        shim = bin_dir / name
        shim.write_text(script, encoding="utf-8")
        shim.chmod(0o700)
    return str(bin_dir)


def shim_events(folder: str) -> int:
    """How many times a shim ran in this session."""
    try:
        return len((Path(folder) / SHIM_LOG).read_text(encoding="utf-8").splitlines())
    except OSError:
        return 0


def egress_note(plan: EgressPlan, refused: int, shims: int) -> str | None:
    """One line for the end-of-run report: how many requests the allowlist refused and how many blocked tools were
    called. Counts and the class only; no host is named."""
    if not refused and not shims:
        return None
    return f"egress, class {plan.cls!r}: {refused} request(s) refused by the allowlist, {shims} blocked tool call(s)"


def real_user() -> str | None:
    """The account this process runs as, from the system's user database and the effective user id: never `$USER`,
    `$HOME` or any other environment value, which the caller (or a project's settings) can set. None when the database
    has no entry for it or this system has none."""
    try:
        import pwd  # here, not at the top: POSIX only (tests/test_windows_import.py)
    except ImportError:
        return None
    try:
        return pwd.getpwuid(os.geteuid()).pw_name
    except (KeyError, OSError, AttributeError):
        return None


def classes_of_user(pol: policy.Policy) -> list[policy.Class]:
    """The classes whose `os_user` is the account this process runs as (`real_user()`), by name. [] for a person's own
    account, a policy with no classes, and a system whose user database has no entry for this process."""
    me = real_user()
    return [c for _, c in sorted(pol.classes.items()) if me and c.os_user == me]


def class_user_refusal(pol: policy.Policy, cls: policy.Class | None) -> WsError | None:
    """Why a session in class `cls` (None: the policy has no classes; `policy.UNCLASSIFIED`: a folder in no class) may
    not start as this account, or None. Two refusals, both exit 3 and both naming the class, never the account: the
    class has an `os_user` and this is not it; or this account is the `os_user` of a class and the folder is not in one
    of those classes (a class account does its work in its class's folders only)."""
    if cls is not None and cls.os_user and real_user() != cls.os_user:
        return WsError(
            "refused",
            f"refusing to start: this folder is in class {cls.name!r}, whose sessions run as the OS user set for that "
            "class, and this process does not",
            f"start it as the OS user of class {cls.name!r} (`ws install --class {cls.name}` prints how to set that up)",
            3,
        )
    mine = classes_of_user(pol)
    if mine and (cls is None or cls.name not in {c.name for c in mine}):
        names = ", ".join(repr(c.name) for c in mine)
        where = "in no class" if cls is None or cls.name == policy.CLASS_RESERVED else f"in class {cls.name!r}"
        return WsError(
            "refused",
            f"refusing to start: this process runs as the OS user of class {names}, whose sessions start in that "
            f"class's folders only, and this folder is {where}",
            f"start it from a folder of class {names}, or as your own account",
            3,
        )
    return None


def require_class_user(pol: policy.Policy, cwd: str | None = None) -> None:
    """Refuse to start a session unless this process's account fits the class of the folder it starts in. The class is the
    policy's `class_of` for the real path of `cwd` (default: the current folder), and the account is `real_user()`:
    nothing from the environment is read, so a forged `$USER` or `$HOME` changes nothing. A class with an `os_user` is
    started as that user only; an account that is the `os_user` of a class starts in that class's folders only (a folder
    of no class, or of another class, is refused). A class with no `os_user`, a policy with no classes and a person's own
    account are not held to this. Exit 3, with a hint that names the class and not the account (`class_user_refusal`)."""
    refusal = class_user_refusal(pol, session_class(pol, cwd))
    if refusal is not None:
        raise refusal


def _launch_policy(args: argparse.Namespace) -> tuple[Path, policy.Policy]:
    policy_file = Path(args.policy).expanduser().absolute() if args.policy else policy.policy_path()
    try:
        pol = policy.load(
            policy_file, required=args.policy is not None
        )  # a named policy that is not there is no default
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), f"fix {policy_file}, or run `ws install`") from e
    return policy_file, pol


def warn_old_runner(exe: str, env: dict, pol: policy.Policy) -> None:
    """Print the WS016 warning to stderr when the installed runner is older than `wallsettings.MIN_CLAUDE` for a key the
    wall writes under `pol`, or its version cannot be read: the start goes on (a key an old runner does not know is
    ignored, which leaves that part of the wall out). Nothing prints when the wall writes no such key or `exe` is not
    found (the start itself then fails and says so)."""
    from ws import doctor, run, wallsettings  # here, not at the top: both import this module

    keys = wallsettings.keys_written(pol)
    path = shutil.which(exe, path=env.get("PATH"))
    if not keys or not path:
        return
    version = run.exe_version(path, env)
    found = doctor._version_tuple(version) if version else None

    def need(k: str) -> str:
        return f"{k} (needs {'.'.join(map(str, wallsettings.MIN_CLAUDE[k]))})"

    if found is None:
        what = ", ".join(need(k) for k in keys)
        print(
            f"ws: warning (WS016): can't tell the Claude Code version, and the wall writes keys that need a recent "
            f"one: {what}. Update Claude Code, then run `ws doctor`.",
            file=sys.stderr,
        )
        return
    old = [k for k in keys if found < wallsettings.MIN_CLAUDE[k]]
    if old:
        print(
            f"ws: warning (WS016): Claude Code {version} does not read {', '.join(need(k) for k in old)}: that part of "
            "the wall is not in force. Update Claude Code.",
            file=sys.stderr,
        )


def class_launch_doc(cls: policy.Class | None) -> dict | None:
    """The launch file of a walled class (a class with an `os_user`), read and checked by
    `wallsettings.load_launch_file` (WsError `refused`, exit 3, when it is not in place or not root-owned); None for any
    other session."""
    from ws import osinfo, wallsettings

    if cls is not None and cls.os_user:
        return wallsettings.load_launch_file(cls, osinfo.detect().kind)
    return None


def warn_hidden_clash(clash: list[str], what: str) -> None:
    """Say, on stderr, which allowed folders (`clash`, from `under_hidden`) sit inside another class's folder and so are
    not reachable: `what` is what happens to them."""
    if clash:
        print(f"ws: warning: {what}, as it is inside another class's folder: {', '.join(clash)}", file=sys.stderr)


def class_hidden(launch_doc: dict, cls: policy.Class, cwd: str, env: dict) -> list[str]:
    """The folders a walled class's session in `cwd` may not read, for a runner that has no sandbox of its own: the other
    classes' folders in its launch file, and the `deny_read` of the per-repo narrowing the policy gives the class for
    `cwd`. Real paths, only those that exist and name no wildcard (as `hidden_paths` does for the policy's own)."""
    from ws import wallsettings

    found = [*wallsettings.read_denied(launch_doc), *cls.narrowing_for(os.path.realpath(cwd)).deny_read]
    out = []
    for entry in found:
        path = expand(entry, env)
        if path.startswith("/") and not re.search(r"[*?\[]", path) and os.path.lexists(path):
            out.append(os.path.realpath(path))
    return list(dict.fromkeys(out))


def claude_launch_text(
    pol: policy.Policy,
    cls: policy.Class | None,
    project: str,
    env: dict,
    plan: EgressPlan | None,
    base_text: str,
    *,
    cwd: str | None = None,
    forges: bool = False,
    launch_doc: dict | None = None,
) -> str:
    """The Claude Code settings for a session in `project`: `base_text` (the generated settings) with the project's locked
    paths, the home-folder denial and the policy's read rules added (`claude_code.for_project`), `plan`'s hosts as the
    network list (a headless job passes None: its settings carry the list already), the walled class's launch file
    merged in (its keys win) and last the per-repo narrowing the policy gives the class for `cwd` (default: the current
    folder), which can only add denials. The other classes' folders (`launch_doc`'s deny list) are denied before the
    allow list is filtered, so no entry can sit under one: it is dropped, and named. `forges` adds the forge hosts to
    the default network list (a session at a terminal; never a headless job). Settings with no sandbox block come back
    as they are. WsError `refused` (exit 3) when a class's list has nowhere to go."""
    from ws import wallsettings
    from ws.adapters import claude_code

    try:
        allow, deny = policy.launch_read_rules(pol)
        if launch_doc is not None:
            hidden = wallsettings.read_denied(launch_doc)
            warn_hidden_clash(
                under_hidden([*allow, *git_read_folders(project)], hidden, env.get("HOME") or str(Path.home())),
                "left out of the readable folders",
            )
            deny = [*deny, *hidden]
        text = claude_code.for_project(
            base_text,
            project,
            plan.domains() if plan else None,
            allow_read=allow,
            deny_read=deny,
            env=env,
            forges=forges,
        )
    except ValueError as e:  # a class's list has nowhere to go: the generated settings have no sandbox block
        raise WsError(
            "refused", f"refusing to start claude-code: {e}", "set `[gate] sandbox = true`, `ws install`", 3
        ) from e
    if launch_doc is not None and cls is not None:
        conf = wallsettings.merge(json.loads(text), launch_doc)
        conf = wallsettings.narrowed(conf, cls.narrowing_for(os.path.realpath(os.getcwd() if cwd is None else cwd)))
        text = json.dumps(conf, indent=2) + "\n"
    return text


def _guarded(what: str, step, *args):
    """Run one clean-up step; a step that raises is one warning line on stderr and None, so the steps after it still run
    (a run's lease, record and settle, and a wall's private tmp, never depend on an earlier step having worked)."""
    try:
        return step(*args)
    except Exception as e:  # noqa: BLE001 - clean-up never stops the rest of the clean-up
        print(f"ws: could not {what} ({type(e).__name__})", file=sys.stderr)
        return None


@contextlib.contextmanager
def claude_loaded(home: Path, project: str, runner: str = "claude-code"):
    """Around a Claude Code session in `project`: look at the project (`scan_project`), make the load paths Claude's
    sandbox holds but cannot hold while they are missing (`CLAUDE_LOAD_PATHS`: `.claude`, `.mcp.json`) and record this
    launcher in the project's state (`enter_project`, so several sessions share them and the last one out removes
    them), and on the way out take them down (`leave_project`) and print what the session left (`report_run`). A path
    that is a link is a SandboxError, before anything is made. Both launch paths use it: `ws gate launch` and `ws run`."""
    before, note = scan_project(project)
    notes = [note] if note else []
    entered = False
    try:
        enter_project(home, project, CLAUDE_LOAD_PATHS)
        entered = True
        yield
    finally:
        if entered:
            made = _guarded("let go of the project", leave_project, home, project) or []
            try:
                after, note = scan_project(project, tuple(made))
                report_run(home, project, runner, before, after, [*notes, *([note] if note else [])], not notes)
            except OSError as e:
                print(f"ws: could not write the wall report: {e}", file=sys.stderr)


@dataclass
class Wall:
    """What `walled` hands the caller: the runner's command inside the wall, the descriptors to keep open for it, and the
    variables the wall sets for it (add them to the runner's environment last). `proxy` is the egress proxy, when the
    session has a plan; `tmp` is the wall's private /tmp."""

    argv: list[str]
    pass_fds: tuple[int, ...]
    env: dict
    tmp: str
    proxy: EgressProxy | None = None


@contextlib.contextmanager
def walled(
    argv: list[str],
    pol: policy.Policy,
    home: Path,
    runner: str,
    project: str,
    env: dict,
    plan: EgressPlan | None,
    *,
    protect: list[str],
    allow_write: list[str] = (),
    cls: policy.Class | None = None,
    cwd: str | None = None,
    tty: bool = False,
):
    """Around a runner that has no sandbox of its own (OpenCode), the wall `ws gate launch` and `ws run` both put round it.
    On the way in: a walled class's launch file is read and the project checked against the runner's own
    `project_problem` (WsError `refused`, exit 3: before anything is made), the other classes' folders and the narrowing's
    `deny_read` are hidden (an allowed folder inside one is named), the private tmp and the runner's private XDG folders
    are made, the project is looked at and locked (`enter_project`), the egress proxy is started when `plan` has hosts, and
    `argv` is wrapped (`wrap`; SandboxError when it cannot be). It yields a `Wall`. On the way out, even on an error: the
    proxy is stopped, the project is let go of, the end-of-run report is written and the tmp is removed.

    `env` is where the real home and the runner's folders are looked up; `protect` and `allow_write` are the caller's
    (`protected`, `writable_folders`); `cwd` (default the current folder) is the folder the session starts in, which is
    what the class narrowing is judged by."""
    launch_doc = class_launch_doc(cls)
    hidden: list[str] = []
    if launch_doc is not None and cls is not None:
        start = os.getcwd() if cwd is None else cwd
        problem = getattr(adapters.get(runner), "project_problem", None)
        refusal = problem([project, os.path.realpath(start)], paths.home_dir()) if problem else None
        if refusal:
            raise refusal
        hidden = class_hidden(launch_doc, cls, start, env)
        allowed = [*policy.sandbox_write_paths(pol), *policy.launch_read_rules(pol)[0], *allow_write, project]
        warn_hidden_clash(under_hidden(allowed, hidden, env.get("HOME") or str(Path.home())), "hidden from the session")
    tmp = tempfile.mkdtemp(prefix="ws-wall-")  # the wall's /tmp: the rest of the disk is read-only
    entered, before, notes, proxy = False, {}, [], None
    try:
        homes = private_homes(home, runner, pol, env, project, hidden)
        before, note = scan_project(project)
        notes += [note] if note else []
        locked = enter_project(home, project)
        entered = True
        extra: dict = {}
        net = None
        if plan is not None:  # the runner reaches the network through ws's proxy, and the macOS wall shuts the rest
            secret = new_secret()
            proxy = EgressProxy(plan, secret=secret)
            extra = {**proxy_env(proxy.start(), secret), "PATH": f"{write_deny_shims(tmp)}:{env.get('PATH', '')}"}
            net = (proxy.port, *plan.local_ports())
        wrapped = wrap(
            argv,
            pol,
            home,
            protect,
            writable=writable_folders(pol, runner, project, tmp, list(allow_write), env),
            locked=locked,
            homes=list(homes.values()),
            tty=tty,
            net=net,
            extra_hidden=hidden,
        )
        yield Wall(wrapped.argv, wrapped.pass_fds, {**wall_env(tmp, homes), **extra}, tmp, proxy)
    finally:
        if proxy is not None:
            _guarded("stop the egress proxy", proxy.stop)
            line = _guarded("read the egress notes", lambda: egress_note(plan, len(proxy.denials), shim_events(tmp)))
            notes += [line] if line else []
        made = (_guarded("let go of the project", leave_project, home, project) or []) if entered else []
        if entered:
            try:
                after, note = scan_project(project, tuple(made))
                complete = not any("scan stopped" in n for n in notes)
                report_run(home, project, runner, before, after, [*notes, *([note] if note else [])], complete)
            except OSError as e:
                print(f"ws: could not write the wall report: {e}", file=sys.stderr)
        shutil.rmtree(tmp, ignore_errors=True)


def launch_claude(
    args: argparse.Namespace,
    argv: list[str],
    home: Path,
    env: dict,
    pol: policy.Policy | None = None,
    plan: EgressPlan | None = None,
) -> int:
    """`ws gate launch --runner claude-code -- claude ...`: Claude Code's own sandbox, with settings made for THIS
    project at the start of the session. The shared settings (`ws gate generate`, `ws install`) are made once for every
    project, and a sandbox path has no project-relative spelling in a `--settings` file, so they carry the Edit rules but
    no project paths. Here the project is known: the locked set (ws.sandbox PROJECT_LOCKED, GIT_LOCKED, the git folders
    git resolves) is added to `denyWrite` in a copy under `<state>/gate/claude-code/projects/`, and `claude --settings`
    is started with it. No bubblewrap: Claude Code's sandbox is the wall. The end-of-run report is the launcher's, as
    for OpenCode, and so are the placeholders for `.claude` and `.mcp.json` when the project has none (`claude_loaded`).
    With an egress `plan` the copy also carries the plan's hosts as `network.allowedDomains`, and nothing else is
    reachable.

    A session of a walled class (a class with an `os_user`) also gets the class's own launch file (ADR 0027, `ws install
    --class`), read from `wallsettings.launch_file_path` and merged into that copy, its keys winning: the class is the one
    `session_class` finds for the real path of the folder, never a name from the environment, and only that class's file is
    read. The start is refused (exit 3, the hint naming `ws install --class NAME`) when the file is not in place or is not
    root-owned, and when the project's `.mcp.json` or `.claude/settings*.json` holds anything the allow-list does not name
    (`wallsettings.project_problem`): a SessionStart hook cannot block, so the refusal is made here, before the runner
    starts, and again by the hook on every call. The per-repo narrowing the policy gives the class for this folder is
    applied last, and can only add denials. The copy is made by `claude_launch_text`, which `ws run` uses too."""
    from ws import wallsettings
    from ws.adapters import claude_code

    pol = pol if pol is not None else _launch_policy(args)[1]
    require_class_user(pol)  # before anything is written or started
    cls = session_class(pol)
    launch_doc = class_launch_doc(cls)
    warn_old_runner(argv[0], env, pol)
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
    if launch_doc is not None:
        refusal = wallsettings.project_problem([project, os.path.realpath(os.getcwd())], paths.home_dir())
        if refusal:
            raise refusal
    target = out / "projects" / f"{project_key(project)}.json"
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.is_symlink():
        target.unlink()
    text = claude_launch_text(
        pol,
        cls,
        project,
        env,
        plan,
        base.read_text(encoding="utf-8"),
        forges=plan is None and not pol.gate.allow_hosts,  # the default list only: a person is at this terminal
        launch_doc=launch_doc,
    )
    from ws import install  # here, not at the top: install imports the adapters this module does

    try:
        install._write_atomic(target, text.encode("utf-8"), 0o600)  # whole or not at all, and for this person alone
    except OSError as e:
        raise WsError("io", f"could not write the launch settings {target}: {e}", "check the folder is writable") from e
    cmd = [argv[0], "--settings", str(target), *argv[1:]]
    env = {**claude_code.MCP_WAIT_ENV, **env}  # the first model call sees its MCP tools; a value the person set wins
    old = _exit_on_hangup()
    try:
        with claude_loaded(home, project, "claude-code"):
            try:
                return run_plain(cmd, env)
            except OSError as e:
                print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
                return 127
    except SandboxError as e:
        raise WsError("refused", f"refusing to start claude-code: {e}", "start it from a project folder") from e
    finally:
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
    policy_file, pol = _launch_policy(args)
    require_class_user(pol)  # a session of a class with an OS user starts as that user or not at all
    cls = session_class(pol)
    provider = args.provider or DEFAULT_PROVIDER.get(args.runner)
    require_provider(cls, provider)  # G7: a provider outside the class's list is refused, before anything starts
    plan = egress_plan(pol, cls, provider)  # None while the policy has no classes: the network is as it was
    if plan is not None and (args.no_sandbox or (args.runner == "claude-code" and not pol.gate.sandbox)):
        raise WsError(  # a class's network list is applied by the wall; with none, the session would have no list at all
            "refused",
            f"refusing to start: this folder is in class {cls.name!r}, whose network list is applied by the sandbox, "
            "and this session would run without one",
            "start it without --no-sandbox and with `[gate] sandbox = true` in the policy",
            3,
        )
    home = paths.state_dir().absolute()  # whatever --policy says, the gate keeps its approvals and sessions here
    pass_env = check_pass_env(args.pass_env)
    if pass_env:
        print(f"ws: note: passing {', '.join(pass_env)} from this shell to {args.runner}", file=sys.stderr)
    env = launch_env(dict(os.environ), runner=args.runner, pass_env=pass_env)
    if args.runner == "claude-code" and not args.no_sandbox:
        return launch_claude(args, argv, home, env, pol, plan)
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
    old = _exit_on_hangup()
    try:
        project = project_folder(os.getcwd(), env)
        with walled(
            argv,
            pol,
            home,
            args.runner,
            project,
            env,
            plan,
            protect=protected(policy_file, args.runner, args.protect),
            allow_write=args.allow_write or [],
            cls=cls,
            tty=sys.stdin.isatty(),
        ) as wall:
            return run_with_broker(
                wall.argv,
                {**launch_env(env, None, None, args.runner, pass_env, paths.env_for()), **wall.env},
                wall.pass_fds,
                approvals_dir(home),
            )
    except SandboxError as e:
        raise WsError(
            "refused", f"refusing to start {args.runner} without its wall: {e}", "or `--no-sandbox`, which has none"
        ) from e
    except OSError as e:
        print(f"ws: could not start {shlex.join(argv[:1])}: {e}", file=sys.stderr)
        return 127
    finally:
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
    p.add_argument("--policy", metavar="FILE", help="policy file (default: policy.toml in the config folder)")
    p.add_argument("--protect", action="append", metavar="PATH", help="another file or folder to make read-only")
    p.add_argument(
        "--allow-write",
        action="append",
        metavar="DIR",
        help="another folder the session may write (besides the project)",
    )
    p.add_argument(
        "--pass-env",
        action="append",
        metavar="NAME",
        help="an environment variable to hand the runner (repeat for more); the rest of this shell's is left behind",
    )
    p.add_argument(
        "--provider",
        metavar="NAME",
        help="the model provider this session uses (default: anthropic for claude-code); a data class limits which "
        "providers it may use, and its network list gets that provider's host",
    )
    p.add_argument("--no-sandbox", action="store_true", help="start the runner with no wall (says what that gives up)")
    p.add_argument("argv", nargs=argparse.REMAINDER, help="-- then the runner's command line")
    p.set_defaults(func=cmd_launch, raw=True)  # it hands the terminal to the runner
