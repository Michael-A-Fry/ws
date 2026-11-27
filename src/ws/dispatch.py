"""`ws dispatch`: start a runner's session on a prompt, in a new tmux window, in one repository's folder.

    ws dispatch REPO [--name WORDS] [--model M] [--runner R] [--session S] [--code-root DIR] [--dry-run] PROMPT

REPO is a folder name under the code root. The code root is `--code-root`, else `WS_CODE_ROOT`, else `code_root` in the
profile's `[dispatch]` table, else `~/code`. REPO is one name, never a path: a slash, `.` or `..` is a usage error, so a
dispatch cannot leave the code root.

What it promises, each pinned by a test:

- Every flag reaches the runner as its own argument. The window runs the runner's argv directly (`tmux new-window ...
  -- ARGV`, which tmux 3.0 and later executes without a shell), so `--model=M` is one item and the prompt is another,
  whatever spaces or quotes it holds. A prompt that starts with `-` is refused: the runner would read it as a flag.
- `--help` prints the help and does nothing else; `--dry-run` prints what it would run and does nothing else.
- One window, named from `--name` (lower case, spaces to dashes, at most 40 characters; the repo's name when it is not
  given), opened in the background (`-d`), so the person's own window keeps the focus. The window goes in the tmux
  session this command runs in (`$TMUX`), or in `--session S`; outside tmux with no `--session` it is refused.
- The runner is found on `PATH` before anything starts, and a missing one is refused with its name.
- A Claude session is always started with a name and, unless the repo's data class forbids it, with remote control on, so
  nobody has to type `/rename` or `/remote-control` into its pane afterwards (the second one opens a modal status dialog
  that holds every queued message until a key is pressed). The label for both is exactly the window's name, the slug
  above, so the session list, the phone and tmux all show one string: `--name Fix The Login` gives `fix-the-login`, and a
  `·` or any other character outside `a-z 0-9 _` becomes a dash (it does not survive). `--name` is two argv items
  (`--name`, LABEL). `--remote-control` takes an optional value, so it is ONE item, `--remote-control=LABEL`, and can
  never read the prompt that follows as its value; the prompt stays the last item.
- Remote control sends the session's transcript through the vendor's servers, beyond this machine. It is left off, with
  one line on stderr, for a repo whose class in the policy is not `access = "full"` (walled or counts-only, and a repo in
  no class once any class exists), and when the policy cannot be read (fail closed). `--name` is still passed.

Runners: `claude-code` runs `claude --name LABEL [--remote-control=LABEL] [--model=M] PROMPT`; `opencode` runs
`opencode [--model=M] --prompt PROMPT` (no name flags).
The code root's `~` means your home only when it is `~` alone or starts `~/`.
Every tmux command goes through `ws.term.tmux`'s injectable runner, so tests never touch a real tmux server.
- A usage reserve (C840) keeps the end of the weekly plan for the lead. Before a window opens, dispatch runs
  `wsn usage gate` (ws-next owns the rule and the figure). Its exit 75 refuses the dispatch with its own line: the plan
  is in the reserve and the caller is not the lead. The lead is told apart by its process ancestry, never by a flag.
  No `wsn` on PATH, or any other exit, lets the dispatch go with one line on stderr: the reserve is a guard for the
  lead, so it fails open.
Exit codes: 0 started (or shown), 1 something to fix (no repo folder, no runner, no tmux, tmux refused), 2 usage.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path

from ws import output, paths, policy
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path
from ws.term import tmux

COMMAND = "dispatch"
CODE_ROOT_ENV = "WS_CODE_ROOT"
DEFAULT_CODE_ROOT = "~/code"
MIN_TMUX = (3, 0)  # new-window runs a multi-item command without a shell from 3.0
NAME_MAX = 40

# runner name -> (binary, how the model and the prompt are passed)
RUNNERS: dict[str, tuple[str, str]] = {
    "claude-code": ("claude", "positional"),
    "opencode": ("opencode", "--prompt"),
}


def code_root(arg: str | None = None, env: Mapping[str, str] | None = None) -> Path:
    """Where the repositories are: the flag, the environment variable, the profile, then the default."""
    e = os.environ if env is None else env
    value = arg or e.get(CODE_ROOT_ENV)
    if not value:
        try:
            prof = load(profile_path(dict(e))) or {}
        except ProfileError as err:
            raise WsError("bad_config", str(err), "fix the profile, or pass --code-root", 1) from err
        table = prof.get("dispatch") if isinstance(prof.get("dispatch"), dict) else {}
        found = table.get("code_root")
        if found is not None and not isinstance(found, str):
            raise WsError("bad_config", "[dispatch] code_root must be text", "fix the profile", 1)
        value = found or DEFAULT_CODE_ROOT
    if value == "~" or value.startswith("~/"):  # only your own home: `~name/` is left as written
        return Path(str(paths.home_dir(e)) + value[1:])
    return Path(value)


def window_name(name: str | None, repo: str) -> str:
    """A tmux-safe window name from the person's words: lower case, runs of anything else to one dash."""
    slug = re.sub(r"[^a-z0-9_]+", "-", (name or repo).lower()).strip("-")[:NAME_MAX].strip("-")
    return slug or repo


def remote_control_off(folder: Path, env: Mapping[str, str] | None = None) -> str:
    """Why remote control must not be switched on for a session in `folder`, or "" when it may be. The policy's data
    class decides: only `access = "full"` (or no classes at all) may leave the machine. A policy that cannot be read is
    a refusal too, never a guess."""
    e = os.environ if env is None else env
    try:
        pol = policy.load(policy.policy_path(dict(e)))
    except policy.PolicyError:
        return "the policy could not be read"
    cls = policy.class_of(pol, folder)
    if cls is None or cls.access == "full":
        return ""
    return f"its data class is {cls.name} ({cls.access})"


def runner_argv(
    runner: str,
    prompt: str,
    model: str | None,
    which=shutil.which,
    path: str | None = None,
    label: str | None = None,
    remote_control: bool = False,
) -> list[str]:
    """The runner's argv, each flag its own item and the prompt one item (always the last). For claude-code a `label`
    adds `--name LABEL` and, with `remote_control`, `--remote-control=LABEL` (one item: its value is optional, so the
    prompt must never follow it as a separate word). Other runners ignore both."""
    if prompt.startswith("-"):
        raise WsError("bad_prompt", "the prompt starts with '-', so the runner would read it as a flag", "reword it", 2)
    if not prompt.strip():
        raise WsError("bad_prompt", "the prompt is empty", "give the session something to do", 2)
    binary, style = RUNNERS[runner]
    found = which(binary, path=path)
    if not found:
        raise WsError("runner_missing", f"{binary} is not on PATH", f"install {runner}, or pick another --runner", 1)
    argv = [found]
    if label and runner == "claude-code":
        argv += ["--name", label, *([f"--remote-control={label}"] if remote_control else [])]
    argv += [f"--model={model}"] if model else []  # one item, so a model that starts with - is no flag
    return [*argv, prompt] if style == "positional" else [*argv, "--prompt", prompt]


def plan(args: argparse.Namespace, env: Mapping[str, str] | None = None) -> dict:
    """What a dispatch would do: the folder, the window name, the tmux target and the runner's argv."""
    e = os.environ if env is None else env
    repo = args.repo
    if not repo or "/" in repo or os.sep in repo or repo in (".", ".."):
        raise WsError("bad_repo", f"{repo!r} is not a repository name", "give the folder's name under the code root", 2)
    root = code_root(args.code_root, e)
    folder = root / repo
    if not folder.is_dir():
        raise WsError("no_repo", f"no folder {folder}", f"check the name, or set {CODE_ROOT_ENV}", 1)
    if args.session is None and not e.get("TMUX"):
        raise WsError("no_tmux", "not inside tmux, and no --session given", "run it inside tmux, or pass --session", 1)
    if args.session is not None and not tmux.NAME_RE.fullmatch(args.session):
        raise WsError("bad_session", f"{args.session!r} is not a tmux session name", "letters, digits, - and _", 2)
    window = window_name(args.name, repo)
    why_not = remote_control_off(folder, e) if args.runner == "claude-code" else ""
    return {
        "repo": repo,
        "cwd": str(folder),
        "window": window,
        "session": args.session,
        "runner": args.runner,
        "remote_control_off": why_not,
        "argv": runner_argv(
            args.runner, args.prompt, args.model, path=e.get("PATH"), label=window, remote_control=not why_not
        ),
    }


def start(p: dict, run: tmux.Run = tmux.run_tmux) -> str:
    """Open the window and start the runner in it; returns tmux's window id."""
    code, out = run(["-V"])
    if code != 0:
        raise WsError("no_tmux", f"tmux did not answer: {out.strip()}", "install tmux 3.0 or later", 1)
    if tmux.parse_version(out) < MIN_TMUX:
        raise WsError("old_tmux", f"{out.strip()} is older than 3.0", "upgrade tmux to 3.0 or later", 1)
    target = ["-t", f"={p['session']}:"] if p["session"] else []
    argv = ["new-window", "-d", "-P", "-F", "#{window_id}", *target, "-n", p["window"], "-c", p["cwd"], "--"]
    code, out = run([*argv, *p["argv"]])
    if code != 0:
        raise WsError("tmux_failed", f"tmux new-window failed: {out.strip()}", "check the session name and tmux", 1)
    return out.strip()


RESERVE_GATE = ("wsn", "usage", "gate")
RESERVE_HELD = 75


def reserve_gate(run=subprocess.run) -> None:
    """Refuse when ws-next's usage reserve holds new sessions for the lead (C840); fail open otherwise."""
    if not shutil.which(RESERVE_GATE[0]):
        print("ws dispatch: usage reserve not checked (wsn is not on PATH)", file=sys.stderr)
        return
    try:
        r = run(list(RESERVE_GATE), capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as e:
        print(f"ws dispatch: usage reserve not checked ({type(e).__name__})", file=sys.stderr)
        return
    if r.returncode == RESERVE_HELD:
        why = (r.stderr or "").strip().splitlines()[-1:] or ["the weekly plan is in the reserve kept for the lead"]
        raise WsError("usage_reserve", why[0], "wait for the reset, or ask the lead to start it", 1)
    if r.returncode != 0:
        print(f"ws dispatch: usage reserve not checked (wsn usage gate exited {r.returncode})", file=sys.stderr)


def cmd_dispatch(args: argparse.Namespace) -> int:
    p = plan(args)
    if not args.dry_run:
        reserve_gate()
    if p["remote_control_off"]:
        print(f"ws dispatch: remote control left off ({p['remote_control_off']})", file=sys.stderr)
    if not args.dry_run:
        p["window_id"] = start(p, tmux.run_tmux)  # looked up now, so a test can swap it
    if args.json:
        output.result({**p, "started": not args.dry_run})
    elif args.dry_run:
        print(f"would open window {p['window']} in {p['cwd']}: {p['argv']}")
    else:
        print(f"started {p['runner']} in window {p['window']} ({p['window_id']}), in {p['cwd']}")
    return 0


def register(sub) -> None:
    d = sub.add_parser(
        "dispatch",
        help="start a runner's session on a prompt in a new tmux window, in a repository's folder",
        description="Start a runner's session on PROMPT in a new tmux window, in REPO's folder under the code root "
        f"(--code-root, else ${CODE_ROOT_ENV}, else [dispatch] code_root in the profile, else {DEFAULT_CODE_ROOT}).",
    )
    d.add_argument("repo", metavar="REPO", help="the repository's folder name under the code root")
    d.add_argument("prompt", metavar="PROMPT", help="what the session should do (one argument: quote it)")
    d.add_argument("--name", help="a few words for the window's name (default: the repo's name)")
    d.add_argument("--model", help="the model the runner should use, passed as its own --model")
    d.add_argument("--runner", choices=sorted(RUNNERS), default="claude-code", help="the runner (default claude-code)")
    d.add_argument("--session", help="the tmux session to open the window in (default: the one this runs in)")
    d.add_argument("--code-root", metavar="DIR", help=f"where the repositories are (default ${CODE_ROOT_ENV})")
    d.add_argument("--dry-run", action="store_true", help="print what would run, and start nothing")
    d.set_defaults(func=cmd_dispatch)
