"""`ws upgrade`: swap the ws binary for a newer release and rewrite the runner's hooks in the same step.

    ws upgrade [--version TAG | --channel NAME]   run the installer, then rewrite the hooks with the new binary
    ws upgrade --hooks-only                       rewrite the hooks only (a newer ws is already installed)
    ws upgrade --dry-run                          say what would change and change nothing

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
files it would import have just been replaced.

The rewrite is `ws install`'s own gate steps (`ws.install.gate_plan`) and nothing else, bar one file: the profile, the
modules and the folders are not touched. It never switches on what the person did not have (a config written without the
sandbox block stays without it, see `ws.userconfig.keeps_sandbox_off`), and never takes ws out of a config (that is
`ws install --gate launch`). The one file is an older OpenCode plugin that ws put there and has since withdrawn from
(`ws.userconfig.withdrawn_plugin`): it is still loaded, so it is taken out, and said so.

A runner ws gated whose config it can't merge into now (a link, JSONC, a value of the person's in the way) is left as it
is, named with the reason and the way on, and the exit code is 1: the other runners are rewritten, and the person is
never told "done" while a runner still has the old hooks.

It is refused inside a gated session, and fails towards refusing. `WS_GATED_SESSION` is left out of the user-scope
settings so that `ws approve` works from the person's own Claude shell, so the other signs are read as well: the sandbox
marker the user config sets in every session, the claim socket, and the variables a runner sets in its own sessions. A
run without a terminal on stdin is refused too (a session's shell has none), as `ws approve` is.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ws import __version__, approvals, install, output, selfcmd, userconfig
from ws.output import WsError

COMMAND = "upgrade"
EXIT_FAILED, EXIT_REFUSED, EXIT_NOT_RESTORED = 1, 3, 4
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
    """Why this run is refused, or None. Fails towards refusing: any sign of a session, or no terminal."""
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


def rewrite(env: Mapping[str, str], dry_run: bool = False) -> install.Result:
    """Rewrite the runner's hooks (and the generated gate files) with this ws. On any failure the runner's config, the
    install record and the OpenCode plugin are put back as they were, and the error is raised; if any could not be put
    back it is `not_restored` (exit 4) and names them. A runner that could not be rewritten is in `result.skipped`; a
    withdrawn older OpenCode plugin that was taken out is in `result.removed`."""
    steps, skipped = install.gate_plan(env)
    plug = userconfig.withdrawn_plugin(env)
    if dry_run:
        result = install.apply(steps, env, dry_run=True)
        result.skipped = skipped
        result.removed += [plug] if plug else []
        return result
    held = _Held.of(
        [
            install.manifest_path(env),
            *(s.path for s in steps if s.kind == "merge" or s.path.suffix == ".js"),
            *([plug] if plug else []),
        ]
    )
    try:
        result = install.apply(steps, env)
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


def _run(argv: list[str], capture: bool) -> int:
    """Run a command with the person's terminal (stdout and stderr captured and passed to stderr under `--json`)."""
    if not capture:
        return subprocess.run(argv, check=False).returncode
    done = subprocess.run(argv, check=False, capture_output=True, text=True)
    sys.stderr.write(done.stdout + done.stderr)
    return done.returncode


def _installer_argv(script: Path, args: argparse.Namespace) -> list[str]:
    argv = ["bash", str(script), "--skip-smoke-test"]
    if args.version:
        argv += ["--version", args.version]
    if args.channel:
        argv += ["--channel", args.channel]
    return argv


def _hooks_argv() -> list[str]:
    """The new binary's own rewrite: this Python (the tool's environment, which the installer just rebuilt), a new process."""
    return [*selfcmd.argv(), "upgrade", "--hooks-only"]


def swap(args: argparse.Namespace, scratch: Path) -> int:
    """Run the installer. Returns its exit code."""
    script = Path(args.installer).expanduser() if args.installer else fetch_installer(scratch)
    if not script.is_file():
        raise WsError("failed", f"{script} is not a file", "pass --installer with a copy of install.sh")
    return _run(_installer_argv(script, args), args.json)


# ---- the command ------------------------------------------------------------------------------------------------------------


def _say(args: argparse.Namespace, text: str) -> None:
    """A line for the person: stdout, or stderr under `--json` (stdout is the envelope)."""
    print(f"ws: {text}", file=sys.stderr if args.json else sys.stdout)


def _skipped_line(sk: install.Skipped) -> str:
    return f"{sk.runner}: not rewritten, left as it was: {sk.reason}" + (f". {sk.hint}" if sk.hint else "")


def _hooks_only(args: argparse.Namespace, env: Mapping[str, str]) -> int:
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
        print(f"ws: {note}")
    verb = "would remove" if args.dry_run else "removed"
    for path in result.removed:
        print(f"ws: {verb} {path} (an older OpenCode plugin that ws put there and had withdrawn)")
    verb = "would make" if args.dry_run else "made"
    print(
        f"ws {__version__}: {verb} {result.summary()}"
        if result.changes
        else f"ws {__version__}: hooks are current ({result.summary()})"
    )
    for sk in result.skipped:
        print(f"ws: {_skipped_line(sk)}")
    if left:
        print(
            f"ws: the hooks in {', '.join(left)} could not be rewritten this run. Do what is named above, "
            "then run `ws upgrade --hooks-only` again",
            file=sys.stderr,
        )
        return EXIT_FAILED
    return 0


def _both(args: argparse.Namespace, env: Mapping[str, str]) -> int:
    before = __version__
    if args.dry_run:
        _say(args, "would run the installer, then `ws upgrade --hooks-only` with the new ws")
        return _hooks_only(args, env)
    tree = selfcmd.unstable()
    if tree:
        raise WsError(
            "refused",
            f"this ws runs from a place the installer does not manage ({tree})",
            "use `ws upgrade --hooks-only` here, or install ws with install.sh first",
            EXIT_REFUSED,
        )
    with tempfile.TemporaryDirectory(prefix="ws-upgrade-") as scratch:
        os.chmod(scratch, 0o700)
        code = swap(args, Path(scratch))
    if code != 0:
        _say(args, f"the installer failed (exit {code}); you still have ws {before} and its hooks, unchanged")
        return code or EXIT_FAILED
    _say(args, "installed; now rewriting the hooks with the new ws")
    code = _run(_hooks_argv(), args.json)
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
    if args.json:
        output.result({"from": before, "binary": "swapped", "hooks": "rewritten"})
    else:
        print(f"ws: upgraded from {before}; the hooks are rewritten")
    return 0


def cmd_upgrade(args: argparse.Namespace) -> int:
    env = os.environ
    why = refusal(env, sys.stdin.isatty() or args.dry_run)
    if why:
        raise WsError("refused", why, "run `ws upgrade` yourself, in a terminal of your own", EXIT_REFUSED)
    if args.hooks_only:
        return _hooks_only(args, env)
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
    u.set_defaults(func=cmd_upgrade)
