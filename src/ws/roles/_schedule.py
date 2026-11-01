"""What the two scheduler adapters share: where the unit files go, the command they run, and the injectable runner.

Unit files are written to `$WS_HOME/jobs/units/`, inside ws's own folders, so `install.apply` records them and
`ws uninstall` removes them. The folder is under `jobs`, which a session may not write to: a timer runs code, so the
agent must not be able to edit what it runs. systemd links them into place (`systemctl --user link`, which leaves a
symlink per file in `~/.config/systemd/user`); launchd is pointed at them (`launchctl bootstrap`, which keeps its own
record). Both records are outside `$WS_HOME`, so removing a job's timer, and `ws uninstall`, switch them off through the
adapter's `deactivate` before the files go.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from ws import jobs, selfcmd
from ws.profile import ws_home

Runner = Callable[[list[str]], tuple[int, str]]
CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def units_dir(env) -> Path:
    return Path(os.path.abspath(ws_home(env))) / "jobs" / "units"


def user_unit_dir(env) -> Path:
    """Where `systemctl --user link` puts its symlinks: `$XDG_CONFIG_HOME/systemd/user` (default `~/.config/systemd/user`)."""
    cfg = env.get("XDG_CONFIG_HOME")
    base = Path(cfg) if cfg and os.path.isabs(cfg) else Path(env.get("HOME") or Path.home()) / ".config"
    return base / "systemd" / "user"


def is_foreign(env, name: str, marker: str) -> bool:
    """True when the user manager's folder already holds a unit called `name` that ws did not write: a regular file whose
    first line isn't `marker`, or a link that doesn't lead into ws's own `jobs/units/` folder. A name nothing holds is
    not foreign; nor is a link ws made (dangling or not)."""
    path = user_unit_dir(env) / name
    try:
        if path.is_symlink():
            target = Path(os.path.abspath(path.parent / os.readlink(path)))
            return not target.is_relative_to(units_dir(env))
        if not path.exists():
            return False
        with path.open(encoding="utf-8", errors="replace") as f:
            return not f.readline().startswith(marker)
    except OSError:
        return True  # a unit ws can't read is not one it can say it wrote


def logs_dir(env) -> Path:
    return Path(os.path.abspath(ws_home(env))) / "logs"


def ws_command(env) -> list[str]:
    """How a timer starts ws: the `ws` on PATH, else this Python isolated with -m ws (`selfcmd.argv`). Absolute either
    way."""
    exe = shutil.which("ws", path=env.get("PATH"))
    return [os.path.abspath(exe)] if exe else selfcmd.argv()


def run_cmd(argv: list[str]) -> tuple[int, str]:
    """Run a command; (exit code, its output). The default runner: tests pass their own and never call this."""
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=60, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError) as e:
        return 127, type(e).__name__
    return p.returncode, (p.stdout + p.stderr).strip()


def path_value(env) -> str | None:
    """The PATH a timer's job should see (a user manager's own is short), or None if it holds anything odd."""
    v = env.get("PATH")
    return v if v and not CONTROL.search(v) and "%" not in v else None


def scheduled(env) -> list[jobs.Scheduled]:
    """The scheduled jobs, with a JobError turned into a ValueError the caller prints."""
    try:
        return jobs.scheduled(env)
    except jobs.JobError as e:
        raise ValueError(str(e)) from e


def stale(want: dict[Path, str]) -> list[Path]:
    """Unit files whose content is not what ws would write now (or that are missing)."""
    out = []
    for path, text in want.items():
        try:
            if path.read_text(encoding="utf-8") != text:
                out.append(path)
        except OSError:
            out.append(path)
    return out


def orphans(env, prefix: str, suffixes: tuple[str, ...], keep: set[str]) -> list[Path]:
    """Unit files ws wrote for a job that no longer has a schedule."""
    d = units_dir(env)
    if not d.is_dir():
        return []
    return sorted(
        p for p in d.iterdir() if p.name.startswith(prefix) and p.name.endswith(suffixes) and p.name not in keep
    )
