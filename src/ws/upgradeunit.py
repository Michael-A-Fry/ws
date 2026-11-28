"""The systemd user units of the unattended upgrade, as text made from the installed ws: `ws upgrade --print-units`.

Two units, made here and written by `--write` into the person's systemd user folder (never by a session: the two files,
their drop-in folders, the `user.control` copies and the `.wants` folders an enable links into are protected paths of the
gate, see `ws.userconfig` and `protected_paths` here):

- `ws-upgrade.timer` fires daily (at a random minute of the hour, and ten minutes after boot when a day was missed) and
  starts
- `ws-upgrade.service`, which runs `ws upgrade --unattended` once and exits: it finds the newest release itself and
  does nothing when that is installed. A failed or refused run starts `ws-alert@` (`OnFailure=`), the ntfy alert every
  ws job has.

The switch (DEC-131) is the timer: writing the units starts nothing, and the timer runs only once the person enables it
(`systemctl --user enable --now ws-upgrade.timer`, from a terminal of his own; the upgrade-service guard refuses it from a
session). Turning it off is `systemctl --user disable --now ws-upgrade.timer`.

`ExecStart` names the ws command inside the installed tool's own folder (`<tool folder>/bin/ws`), a folder the gate
already protects from a session. It is never a path in a checkout, a repository or `~/.local/bin`, so nothing a session
can edit runs as the service. A ws that does not run from an installed tool folder makes no units.

Swap note: the units are plain systemd; to use cron, a launchd agent or anything else, run `ws upgrade --unattended`
under it (it needs `INVOCATION_ID` and a systemd parent today: `ws.upgrade.unattended_refusal` is the one place to
widen) once a day.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ws import paths, selfcmd
from ws.output import WsError

SERVICE, TIMER = "ws-upgrade.service", "ws-upgrade.timer"
UNITS = (SERVICE, TIMER)
# where `enable` links a unit, for each target a user manager reaches at start: a link there is the switch turned on
TARGETS = ("default", "basic", "timers", "sockets", "paths")
LINK_DIRS = tuple(f"{t}.target.{kind}" for t in TARGETS for kind in ("wants", "requires", "upholds"))
UPGRADE_TIMEOUT_S = 3600  # a 30 minute wait for idle, the installer's 10 minutes, the checks and a way back


def _xdg(env: Mapping[str, str], var: str, default: str) -> Path:
    value = env.get(var)
    return Path(value) if value and os.path.isabs(value) else Path(env.get("HOME") or os.path.expanduser("~")) / default


def unit_dir(env: Mapping[str, str]) -> Path:
    return _xdg(env, "XDG_CONFIG_HOME", ".config") / "systemd" / "user"


def unit_paths(env: Mapping[str, str]) -> tuple[Path, ...]:
    """The two files `--write` makes: what the gate protects from a session (`ws.userconfig._claude_protect`)."""
    return tuple(unit_dir(env) / name for name in UNITS)


def protected_paths(env: Mapping[str, str]) -> tuple[Path, ...]:
    """Every place a session could write to change what the two units run, or to switch the timer on.

    systemd builds a unit from its file, a drop-in folder for it (`<unit>.d`, and the shared `ws-.service.d`, `service.d`
    and the like) and a copy of the unit in a folder it reads first (`user.control`, the one `systemctl edit` writes). So
    besides the two files these are named, whether or not they exist yet: the drop-in folders of each unit, of the
    shared prefix and of the type, in the user folder and in the data folder's `systemd/user`; the two files again in
    that data folder; the `.wants`, `.requires` and `.upholds` folders of each start target in both
    (`LINK_DIRS`: an enable is a link there); the whole of `user.control` (its only writer is `systemctl edit`); and the runtime copies under
    the runtime folder's `systemd`. Other units' own files in the user folder stay writable: nothing else is named."""
    config = _xdg(env, "XDG_CONFIG_HOME", ".config") / "systemd"
    data = _xdg(env, "XDG_DATA_HOME", ".local/share") / "systemd" / "user"
    kinds = sorted({name.rsplit(".", 1)[1] for name in UNITS})
    drop_ins = [f"{name}.d" for name in UNITS] + [f"ws-.{kind}.d" for kind in kinds] + [f"{kind}.d" for kind in kinds]
    runtime = env.get("XDG_RUNTIME_DIR")
    out = [*unit_paths(env), *(unit_dir(env) / d for d in (*drop_ins, *LINK_DIRS))]
    out += [data / name for name in (*UNITS, *drop_ins, *LINK_DIRS)]
    out.append(config / "user.control")
    if runtime and os.path.isabs(runtime):
        out += [Path(runtime) / "systemd" / folder for folder in ("user", "user.control", "transient", "generator")]
    return tuple(dict.fromkeys(out))


def ws_command() -> Path:
    """The `ws` command in the installed tool's folder, or a refusal: units made from anything else would run code a
    session can edit."""
    tree = selfcmd.tree()
    cmd = tree / "bin" / "ws" if tree else None
    why = selfcmd.unstable()
    if cmd is None or not cmd.is_file() or why:
        raise WsError(
            "refused",
            f"there is no installed ws command to run as a service ({why or 'this ws is not in a tool folder'})",
            "install ws with install.sh, then run this from that copy",
        )
    return cmd


# what the user manager may hold from `systemctl --user set-environment` or `import-environment` that would change what a
# program loads: a unit never sees any of it
UNSET = (
    "PYTHONPATH PYTHONHOME PYTHONSTARTUP PYTHONUSERBASE PYTHONSAFEPATH PYTHONBREAKPOINT PYTHONINSPECT PYTHONEXECUTABLE "
    "LD_PRELOAD LD_LIBRARY_PATH LD_AUDIT BASH_ENV ENV GIT_CONFIG_GLOBAL GIT_CONFIG_SYSTEM GIT_CONFIG_COUNT GIT_SSH_COMMAND "
    "GIT_EXEC_PATH SSL_CERT_FILE SSL_CERT_DIR HTTPS_PROXY HTTP_PROXY ALL_PROXY NO_PROXY WS_RELEASE_BASE_URL WS_SKIP_VERIFY"
)


def units(ws: Path) -> dict[str, str]:
    """The text of each unit, for `ws` (an absolute path) as the command. Nothing here is a host, a user or a secret.

    The service runs the installed tool's own Python in isolated mode (`-I -m ws`: no `PYTHON*` variable, no user site, no
    working-folder import), with the system folders as its PATH, fixed here when the unit is written (never a folder in the
    person's home, which a session can write), and `UnsetEnvironment=` for what the manager's own environment could add.
    It runs in its own cgroup outside every session, which is how it may rewrite the hooks a session can't. On macOS
    there is no systemd: `ws upgrade --print-units` there prints these for a Linux user manager only, and the unattended
    upgrade is refused until a launchd form is built."""
    run = " ".join([str(ws.with_name("python")), *selfcmd.argv()[1:], "upgrade"])  # the isolated flags are selfcmd's
    env = f"Environment=PATH={paths.tool_path()}\nUnsetEnvironment={UNSET}\n"
    return {
        SERVICE: (
            "[Unit]\nDescription=ws: unattended upgrade (newest release, proved, checked, rolled back offline on any failure)\n"
            "OnFailure=ws-alert@%n.service\n\n"
            f"[Service]\nType=oneshot\nExecStart={run} --unattended\nTimeoutStartSec={UPGRADE_TIMEOUT_S}\n"
            f"NoNewPrivileges=yes\n{env}"
        ),
        TIMER: (
            "[Unit]\nDescription=ws: look for a new ws release once a day\n\n"
            f"[Timer]\nOnCalendar=daily\nRandomizedDelaySec=1h\nPersistent=true\nOnBootSec=10min\nUnit={SERVICE}\n\n"
            "[Install]\nWantedBy=timers.target\n"
        ),
    }


def write(env: Mapping[str, str], ws: Path) -> list[Path]:
    """Write the units into the systemd user folder (it is made if needed). An existing file is replaced; the folder's
    other files are not touched, and nothing is enabled or started."""
    made = []
    folder = unit_dir(env)
    folder.mkdir(parents=True, exist_ok=True)
    for name, text in units(ws).items():
        path = folder / name
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
        made.append(path)
    return made
