"""The wall canary: proof, at the moment it is asked, that the sandbox hides every deny_read path and holds every ws path
read-only.

The static reader (ws.policy) refuses a path word it cannot resolve because it cannot tell what the shell would put
there. Where the sandbox is up that refusal is not the boundary, so ws.gate turns it into an allow that is logged
(`policy.Soften`). That is only safe if the sandbox is really up, so the gate asks this module first, for the call at
hand, and acts on the answer for that call alone.

What it does. It builds the wall ws's own sandbox builds from the policy on this host (ws.sandbox.wrap: the same
bubblewrap or sandbox-exec facility Claude Code's sandbox uses, with the same hidden and read-only paths), and runs one
small probe program inside it:
  * every deny_read path that exists and that a sandbox can hide must read as empty (a file has no bytes, a folder no
    entries). A path that still shows content is a `hidden-readable` failure;
  * every protected path (the policy, ws's control files and folders, what the hook was told with `--protect`, the state
    folder) must refuse a write. A folder is tried by creating a file with a random name; an existing file by opening it
    for writing without truncating; a path that does not exist by trying its nearest existing folder, because a session
    could create it there. A path that accepts the write is a `protected-writable` failure. The probe removes what it
    made, and says so if it could not (`probe-left`);
  * a control write to a folder the wall leaves writable must work, so a probe that fails everything for another reason
    is not mistaken for a wall;
  * the probe must print the one-use number it was given. A probe that does not run, times out or prints anything else
    is `probe-failed`; a wall that cannot be built is `wall-unavailable`.
All of it must pass. A half-pass is a fail, with the kinds listed and never the paths.

What it promises. The answer is computed for this call and never stored: no file, no environment variable, no memory
between calls. A session therefore has nothing to set, and a forged "proven" state has nothing to be read by. What it
does not prove: that Claude Code's own sandbox, which the settings ws writes switch on, is the one running this session
(that is held by `failIfUnavailable`, by the hook's check of project settings that open the sandbox, and by `ws doctor`
WS012). It proves the host can build this wall and that the paths ws relies on are held by it.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ws import paths, policy, sandbox

TIMEOUT = 20  # seconds for the probe; a wall that is slower than this is not one a call should wait for

# What a failure is called. A kind names what went wrong, never the path.
HIDDEN_READABLE = "hidden-readable"
PROTECTED_WRITABLE = "protected-writable"
PROBE_LEFT = "probe-left"
PROBE_FAILED = "probe-failed"
WALL_UNAVAILABLE = "wall-unavailable"

# The program the wall runs. It takes one JSON argument: the nonce, the paths to read, the paths to try to write and the
# folder for the control write. It prints one JSON line.
PROBE = r"""
import json, os, sys
spec = json.loads(sys.argv[1])
tag = ".ws-wall-probe-" + spec["nonce"]
left = []

def shows(p):
    try:
        if os.path.isdir(p):
            return bool(os.listdir(p))
        with open(p, "rb") as f:
            return bool(f.read(1))
    except OSError:
        return False

def make(folder):
    q = os.path.join(folder, tag)
    try:
        fd = os.open(q, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        return False
    os.close(fd)
    try:
        os.unlink(q)
    except OSError:
        left.append(1)
    return True

def writes(p):
    try:
        if os.path.isdir(p):
            return make(p)
        if os.path.lexists(p):
            os.close(os.open(p, os.O_WRONLY))
            return True
        up = os.path.dirname(p.rstrip("/"))
        while up and not os.path.isdir(up):
            up = os.path.dirname(up)
        return bool(up) and make(up)
    except OSError:
        return False

out = {
    "nonce": spec["nonce"],
    "shown": sum(1 for p in spec["read"] if shows(p)),
    "written": sum(1 for p in spec["write"] if writes(p)),
    "control": make(spec["control"]),
    "left": len(left),
}
print(json.dumps(out))
"""


@dataclass(frozen=True)
class Proof:
    """`ok` only when every check passed; else `kinds` says which failed (kinds, never paths)."""

    ok: bool
    kinds: tuple[str, ...] = ()


def wall_paths(pol: policy.Policy, protect: tuple[str, ...], state: Path, env: dict) -> tuple[list[str], list[str]]:
    """(the paths that must read as empty, the paths that must refuse a write), as the wall is built from them."""
    read = sandbox.hidden_paths(pol, env)
    # ws's own wall leaves two folders of its state writable because OpenCode's gate runs inside it; Claude Code's hook runs
    # outside the sandbox, and its settings deny writes to them, so they are not what this proof is about
    holes = {os.path.realpath(h) for h in sandbox.writable_holes(state)}
    write = [p for p in dict.fromkeys([str(state), *protect]) if os.path.realpath(p) not in holes]
    return read, write


def prove(
    pol: policy.Policy,
    protect: tuple[str, ...],
    *,
    state: Path | None = None,
    project: str | None = None,
    env: dict | None = None,
    wrap=sandbox.wrap,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Proof:
    """Build the wall for `pol` and `protect`, run the probe in it and judge the result. Never raises. `project` is the
    folder the session works in (writable in the wall, as it is for a runner); `wrap` and `run` are for tests."""
    env = dict(os.environ if env is None else env)
    state = (state or paths.state_dir()).absolute()
    read, write = wall_paths(pol, protect, state, env)
    tmp = tempfile.mkdtemp(prefix="ws-wall-probe-")
    try:
        folders = [tmp]
        if project:
            try:
                folders.append(sandbox.project_folder(project, env))
            except sandbox.SandboxError:
                pass  # the home folder or above is never writable in the wall: the probe's tmp is all there is
        nonce = secrets.token_hex(8)
        spec = {"nonce": nonce, "read": read, "write": write, "control": tmp}
        try:
            wrapped = wrap(
                [sys.executable, "-I", "-c", PROBE, json.dumps(spec)],
                pol,
                state,
                list(protect),
                writable=[p for p in folders if os.path.isdir(p)],
                env=env,
            )
        except (sandbox.SandboxError, OSError, ValueError):
            return Proof(False, (WALL_UNAVAILABLE,))
        try:
            done = run(
                wrapped.argv,
                capture_output=True,
                text=True,
                timeout=TIMEOUT,
                check=False,
                stdin=subprocess.DEVNULL,
                pass_fds=wrapped.pass_fds,
            )
        except (OSError, subprocess.SubprocessError):
            return Proof(False, (PROBE_FAILED,))
        finally:
            for fd in wrapped.pass_fds:
                try:
                    os.close(fd)
                except OSError:
                    pass
        return _judge(done, nonce)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _judge(done: subprocess.CompletedProcess, nonce: str) -> Proof:
    lines = (done.stdout or "").strip().splitlines() if done.returncode == 0 else []
    try:
        got = json.loads(lines[-1]) if lines else None
    except ValueError:
        got = None
    keys = ("nonce", "shown", "written", "control", "left")
    if not isinstance(got, dict) or not all(k in got for k in keys) or got["nonce"] != nonce:
        return Proof(False, (PROBE_FAILED,))
    if not all(type(got[k]) is int for k in ("shown", "written", "left")):
        return Proof(False, (PROBE_FAILED,))
    return _verdict(got)


def _verdict(got: dict) -> Proof:
    kinds = []
    if got["control"] is not True:
        return Proof(
            False, (PROBE_FAILED,)
        )  # the probe could not write where the wall allows it: its denials mean nothing
    if got["shown"] != 0:
        kinds.append(HIDDEN_READABLE)
    if got["written"] != 0:
        kinds.append(PROTECTED_WRITABLE)
    if got["left"] != 0:
        kinds.append(PROBE_LEFT)
    return Proof(not kinds, tuple(kinds))


KIND_WORDS = {
    HIDDEN_READABLE: "a path the policy hides can still be read",
    PROTECTED_WRITABLE: "a ws path can still be written",
    PROBE_LEFT: "the probe could not remove what it wrote",
    PROBE_FAILED: "the probe did not run",
    WALL_UNAVAILABLE: "the wall cannot be built here",
}


def describe(proof: Proof) -> str:
    """One line for a session or a doctor: what failed, by kind."""
    if proof.ok:
        return "the wall is proven: every hidden path reads empty and every ws path refuses a write"
    return "; ".join(KIND_WORDS.get(k, k) for k in proof.kinds)


# ---- ws doctor, WS012 ---------------------------------------------------------------------------------------------------

INSTALL = "sudo apt install bubblewrap socat (Debian, Ubuntu) or sudo dnf install bubblewrap socat (Fedora)"
FOR_FEWER = "run under `ws gate launch` for fewer refusals"
STRICT = "every command is read strictly, so a path ws can't read in full is refused"
FIXES = {
    HIDDEN_READABLE: "a deny_read path shows content inside the wall: check the sandbox facility, then run `ws doctor` again",
    PROTECTED_WRITABLE: "a ws path accepts a write inside the wall: a protected path sits in a writable folder; move it",
    PROBE_LEFT: "the probe could not clean up after itself: check the disk, then run `ws doctor` again",
    PROBE_FAILED: "the probe did not run: run `ws doctor` again, and check that python3 starts inside bubblewrap",
}


def host_problem(kind: str, which=None) -> str | None:
    """Why this machine cannot run Claude Code's sandbox, or None. Linux and WSL2 need bubblewrap and socat; macOS has
    its own (sandbox-exec); native Windows has none."""
    which = which or shutil.which
    if kind in ("linux", "wsl"):
        missing = [n for n in ("bwrap", "socat") if not which(n)]
        if missing:
            return f"{' and '.join(missing)} {'is' if len(missing) == 1 else 'are'} not installed"
        return None
    if kind == "macos":
        return None if which("sandbox-exec") else "sandbox-exec was not found"
    return "this system has no sandbox (use macOS, Linux or WSL2)"


def doctor_check(env, kind: str, *, prover=None) -> tuple[str, str, str]:
    """(status, detail, fix) for WS012: the sandbox state for this machine and policy. It runs the canary, so what it
    reports is what a call would be given right now."""
    from ws import userconfig  # late: userconfig is only needed here

    pol_file = policy.policy_path(dict(env))
    try:
        pol = policy.load(pol_file)
    except (policy.PolicyError, OSError):
        return "warn", f"the policy doesn't load, so the sandbox state is unknown; {STRICT}", f"fix {pol_file}"
    if not pol.gate.sandbox:
        return (
            "warn",
            f"unsandboxed: the policy sets sandbox = false under [gate]; {STRICT}",
            "set sandbox = true under [gate] in the policy, then run `ws install`",
        )
    problem = host_problem(kind)
    try:
        runner, gate = userconfig._profile_runner(env)
        where = userconfig.target(runner, env) if runner in userconfig.RUNNERS else None
    except Exception:  # noqa: BLE001 - a profile that doesn't read is WS003's to say
        runner, gate, where = "", "", None
    fix = f"install them: {INSTALL}; or set sandbox = false under [gate] in the policy"
    if problem:
        if kind not in ("linux", "wsl", "macos"):
            return "warn", f"unsandboxed: {problem}; {STRICT}", "install WSL2 and run ws inside it"
        in_file = runner == "claude-code" and gate == "every-session"
        tail = "Claude Code refuses to start with the sandbox on" if in_file else STRICT
        return ("fail" if in_file else "warn"), f"unsandboxed: {problem}; {tail}", fix
    if runner == "claude-code" and gate == "every-session" and where is not None:
        try:
            doc, _ = userconfig.load(where)
        except Exception:  # noqa: BLE001 - WS008 says why the file doesn't read
            doc = {}
        block = doc.get("sandbox") if isinstance(doc, dict) else None
        if not (isinstance(block, dict) and block.get("enabled") is True):
            return (
                "warn",
                f"unsandboxed: {where} has no sandbox block; {STRICT}; {FOR_FEWER}",
                "run `ws install`",
            )
    elif runner and runner != "claude-code":
        return "warn", f"unsandboxed: {runner} has no sandbox of its own; {STRICT}; {FOR_FEWER}", ""
    protect = tuple(sandbox.protected(pol_file, "claude-code", env=dict(env)))
    proof = (prover or prove)(pol, protect, env=dict(env))
    if proof.ok:
        return "ok", f"sandbox on and proven: {describe(proof)}", ""
    if proof.kinds == (WALL_UNAVAILABLE,):
        return "warn", f"unsandboxed: {describe(proof)}; {STRICT}", fix
    return "warn", f"sandbox on, not proven: {describe(proof)}; {STRICT}", FIXES.get(proof.kinds[0], "")
