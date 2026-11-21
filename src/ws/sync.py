"""`ws sync`: fetch every git repository under the code root, a few at a time, each with a time limit.

    ws sync [--code-root DIR] [--jobs N] [--timeout-s S]

The code root is the one `ws dispatch` uses (`--code-root`, else `WS_CODE_ROOT`, else `[dispatch] code_root` in the
profile, else `~/code`). A repository is a folder directly under it that holds `.git` (a folder, or the file a worktree
has). Each one gets `git fetch --all --prune --quiet`, run without a terminal and with `GIT_TERMINAL_PROMPT=0`, so a
remote that wants a password fails instead of waiting.

What it promises, each pinned by a test:

- At most `--jobs` fetches at once (default 4).
- Each fetch has its own time limit (`--timeout-s`, default 120). A fetch past it is killed with its whole process group
  (git's helpers too: TERM, then KILL after a short grace, even if git itself went), named, and counted as a failure;
  the others go on and finish. Ctrl-C kills every fetch still running the same way.
- It fetches and nothing else: no merge, no checkout, nothing in a working tree is touched.
- Exit 0 when every fetch finished cleanly; exit 1 when any failed or timed out, each one named with why.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ws import output
from ws.dispatch import code_root
from ws.output import WsError

COMMAND = "sync"
JOBS = 4
TIMEOUT_S = 120.0
FETCH = ("git", "fetch", "--all", "--prune", "--quiet")
KILL_GRACE_S = 2.0


def repos(root: Path) -> list[Path]:
    """The git repositories directly under `root`, by name."""
    try:
        found = [p for p in root.iterdir() if p.is_dir() and not p.is_symlink() and (p / ".git").exists()]
    except OSError as e:
        raise WsError("no_code_root", f"cannot list {root} ({e.strerror or e})", "check --code-root", 1) from e
    return sorted(found, key=lambda p: p.name)


_live: set[subprocess.Popen] = set()  # the fetches running now, so an interrupt can stop them all
_live_lock = threading.Lock()


def _signal_group(proc: subprocess.Popen, sig: int) -> None:
    killpg = getattr(os, "killpg", None)
    with contextlib.suppress(OSError):  # ProcessLookupError: the group is gone already
        if killpg is not None:
            killpg(proc.pid, sig)
        else:
            proc.kill()


def _kill(proc: subprocess.Popen) -> None:
    """Stop a fetch and every process it started: its group gets TERM, a short grace, then KILL whatever happened, so
    a helper that ignores TERM (or outlives git) is not left holding the pipe."""
    _signal_group(proc, signal.SIGTERM)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(KILL_GRACE_S)
    _signal_group(proc, getattr(signal, "SIGKILL", signal.SIGTERM))
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(KILL_GRACE_S)


def fetch(repo: Path, timeout_s: float, argv: tuple[str, ...] | None = None) -> dict:
    """Fetch one repository; {repo, status: ok|failed|timeout, seconds, detail}."""
    argv = FETCH if argv is None else argv
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(
            list(argv), cwd=repo, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, start_new_session=True,
        )  # fmt: skip
    except OSError as e:
        return {"repo": repo.name, "status": "failed", "seconds": 0.0, "detail": e.strerror or str(e)}
    with _live_lock:
        _live.add(proc)
    try:
        _, err = proc.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        _kill(proc)
        with contextlib.suppress(OSError, ValueError, subprocess.TimeoutExpired):
            proc.communicate(timeout=KILL_GRACE_S)
        took = round(time.monotonic() - t0, 2)
        return {"repo": repo.name, "status": "timeout", "seconds": took, "detail": f"killed after {timeout_s:g} s"}
    finally:
        with _live_lock:
            _live.discard(proc)
    took = round(time.monotonic() - t0, 2)
    if proc.returncode != 0:
        last = (err or "").strip().splitlines()[-1:] or [f"exit {proc.returncode}"]
        return {"repo": repo.name, "status": "failed", "seconds": took, "detail": last[0][:200]}
    return {"repo": repo.name, "status": "ok", "seconds": took, "detail": ""}


def sync(
    root: Path, *, jobs: int = JOBS, timeout_s: float = TIMEOUT_S, argv: tuple[str, ...] | None = None
) -> list[dict]:
    """Fetch every repository under `root`, `jobs` at a time; the results in repository-name order. An interrupt
    (Ctrl-C) kills every fetch still running, with its group, and drops the ones not started."""
    found = repos(root)
    if not found:
        return []
    pool = ThreadPoolExecutor(max_workers=jobs)
    try:
        return list(pool.map(lambda r: fetch(r, timeout_s, argv), found))
    except BaseException:
        pool.shutdown(wait=False, cancel_futures=True)
        with _live_lock:
            running = list(_live)
        for proc in running:
            _kill(proc)
        raise
    finally:
        pool.shutdown(wait=True)


def _positive(kind):
    def parse(text: str):
        x = kind(text)
        if not x > 0:
            raise argparse.ArgumentTypeError("must be above 0")
        return x

    return parse


def cmd_sync(args: argparse.Namespace) -> int:
    root = code_root(args.code_root)
    results = sync(root, jobs=args.jobs, timeout_s=args.timeout_s)
    bad = [r for r in results if r["status"] != "ok"]
    if args.json:
        output.result({"root": str(root), "repos": results}, status=output.ATTENTION if bad else output.OK)
        return 1 if bad else 0
    for r in bad:
        print(f"{r['repo']}: {r['status']} ({r['detail']})")
    print(f"ws: fetched {len(results) - len(bad)} of {len(results)} repositories under {root}")
    return 1 if bad else 0


def register(sub) -> None:
    s = sub.add_parser(
        "sync",
        help="fetch every git repository under the code root, a few at a time, each with a time limit",
    )
    s.add_argument("--code-root", metavar="DIR", help="where the repositories are (default: as ws dispatch)")
    s.add_argument("--jobs", type=_positive(int), default=JOBS, metavar="N", help=f"fetches at once (default {JOBS})")
    s.add_argument(
        "--timeout-s", type=_positive(float), default=TIMEOUT_S, metavar="S",
        help=f"kill a fetch that takes longer (default {TIMEOUT_S:g})",
    )  # fmt: skip
    s.set_defaults(func=cmd_sync)
