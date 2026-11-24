"""Test-temp clean-up: a systemd-tmpfiles age rule for pytest's shared base folder, and the doctor's line about /tmp.

pytest keeps each run's temp folders under `/tmp/pytest-of-<user>`, and parallel agents run the suite all day. pytest's own
retention settings (pyproject.toml) limit what a run keeps; this rule ages out the rest with the system's own tool, so ws
has no pruner to maintain. On Linux with systemd, `ws install` writes `<config>/user-tmpfiles.d/ws-pytest.conf`
(`e /tmp/pytest-of-%u - - - <temp_age_hours>h`: clean an existing folder by age, never create it) and, when it has just
written it, runs `systemctl --user enable --now systemd-tmpfiles-clean.timer`, which runs the rule. That call acts on the
account's own systemd manager, so it runs only when HOME is the account's real home. macOS and Linux without systemd are
not handled, and WS021 says so. WS021 also warns when the rule is missing or your files in `/tmp` pass `temp_warn_gb`
(the count stops at the limit or after two seconds). The rule is in the install manifest like any file `ws install`
writes, so the uninstall removes it and its folder once empty; the timer is the system's, so the uninstall only says it
is left enabled. Swap note: delete the file and the call in `ws install`.
"""

from __future__ import annotations

import os
import stat
import time
from pathlib import Path

from ws import install, osinfo
from ws.roles import _schedule
from ws.worktrees import GB, Settings, walk

NAME = "ws-pytest.conf"
TIMER = "systemd-tmpfiles-clean.timer"
COUNT_BUDGET_S = 2.0


def handled(osi: osinfo.OSInfo) -> bool:
    return osi.kind in ("linux", "wsl") and osi.scheduler == "systemd"


def rule(settings: Settings) -> str:
    return f"e /tmp/pytest-of-%u - - - {settings.temp_age_hours}h\n"


def rule_path(env) -> Path:
    home = Path(env.get("HOME") or os.path.expanduser("~"))
    return Path(env.get("XDG_CONFIG_HOME") or home / ".config") / "user-tmpfiles.d" / NAME


def step(env, osi: osinfo.OSInfo, settings: Settings) -> install.Step | None:
    """The install step for the rule, or None where it is not handled."""
    return (
        install.Step(rule_path(env), "file", rule(settings), "an age rule for pytest's temp folder")
        if handled(osi)
        else None
    )


def _real_home(env) -> bool:
    """Is HOME the account's own home? `systemctl --user` acts on that account's manager, never on a stand-in home."""
    try:
        import pwd

        return os.path.realpath(env.get("HOME", "")) == os.path.realpath(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError, OSError):
        return False


def enable_timer(env, osi: osinfo.OSInfo, runner=None) -> list[str]:
    """Switch on the user's tmpfiles clean-up timer. Returns what went wrong (nothing when it is not handled)."""
    if not (handled(osi) and _real_home(env)):
        return []
    code, out = (runner or _schedule.run_cmd)(["systemctl", "--user", "enable", "--now", TIMER])
    return [f"`systemctl --user enable --now {TIMER}` failed: {out[:200] or code}"] if code else []


def uninstall_notes(env, removed, osi: osinfo.OSInfo | None = None, runner=None) -> list[str]:
    """What the uninstall says about the clean-up timer: it is left as it is, because the timer is the system's, and one
    that is enabled is noted, not switched off. Nothing when the rule is not among what was removed."""
    if rule_path(env) not in removed:
        return []
    osi = osinfo.detect(env=dict(env)) if osi is None else osi
    if not (handled(osi) and _real_home(env)):
        return []
    code, _ = (runner or _schedule.run_cmd)(["systemctl", "--user", "is-enabled", TIMER])
    if code:
        return []
    return [f"left {TIMER} enabled: it is the system's (`systemctl --user disable --now {TIMER}` turns it off)"]


def temp_bytes(root: str, limit: int, budget_s: float = COUNT_BUDGET_S) -> tuple[int, bool]:
    """(bytes this account owns under `root`, whether the count finished). It stops past `limit` bytes or the time budget,
    and then the figure is "at least"."""
    uid, total, stop = os.getuid(), 0, time.monotonic() + budget_s
    for _, st in walk(root):
        if st.st_uid == uid and not stat.S_ISDIR(st.st_mode):
            total += getattr(st, "st_blocks", 0) * 512 or st.st_size
        if total >= limit or time.monotonic() > stop:
            return total, False
    return total, True


def check(env, osi: osinfo.OSInfo, settings: Settings) -> tuple[str, str, str]:
    """(status, detail, fix) for the doctor's WS021."""
    if not handled(osi):
        return (
            "ok",
            "pytest temp clean-up is not handled here (no systemd user timer); pytest's own retention still applies",
            "",
        )
    if not rule_path(env).is_file():
        return "warn", f"no age rule for pytest's temp folder at {rule_path(env)}", "run `ws install`"
    limit = int(settings.temp_warn_gb * GB)
    used, done = temp_bytes(env.get("TMPDIR") or "/tmp", limit)
    if used >= limit:
        gb = f"{used / GB:.1f}" + ("" if done else " or more")
        return (
            "warn",
            f"your files in /tmp hold {gb} GB (limit {settings.temp_warn_gb:g})",
            "remove old test folders (for example `rm -rf /tmp/pytest-of-$USER` when no tests are running)",
        )
    return (
        "ok",
        f"pytest temp folder aged out after {settings.temp_age_hours} h; your files in /tmp: {used / GB:.1f} GB",
        "",
    )
