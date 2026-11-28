"""The systemd user units of the unattended upgrade, as text made from the installed ws: `ws upgrade --print-units`.

Two units, made here and written by `--write` into the person's systemd user folder (never by a session: the two files,
their drop-in folders, the `user.control` copies and the `.wants` folders an enable links into are protected paths of the
gate, see `ws.userconfig` and `protected_paths` here):

- `ws-upgrade.timer` fires daily (at a random minute of the hour, and ten minutes after boot when a day was missed) and
  starts
- `ws-upgrade.service`, which runs `ws upgrade --unattended` once and exits: it finds the newest release itself and
  does nothing when that is installed. A failed or refused run starts `ws-alert@` (`OnFailure=`), the ntfy alert every
  ws job has.

The switch (DEC-131) is a file in ws's config folder, `auto-upgrade.on` (`switch_path`), which the gate protects
like the policy beside it: only `ws upgrade --auto on`, from a terminal of the person's own, writes it, and that also
writes the units and enables the timer; `--auto off` disables the timer and removes it. A timer some session found a way to
enable or start (its own unit with `Unit=`, `Also=`, `Wants=`, a drop-in, a `.wants` link, a preset) meets the service's
own start check (`self_check`): the switch must be on, the start must come from this timer just now, nothing else may pull
the units in, no drop-in may change them, and the unit files must be the text written here. Any of those failing is a
refusal, and an alert.

`ExecStart` names the ws command inside the installed tool's own folder (`<tool folder>/bin/ws`), a folder the gate
already protects from a session. It is never a path in a checkout, a repository or `~/.local/bin`, so nothing a session
can edit runs as the service. A ws that does not run from an installed tool folder makes no units.

Swap note: the units are plain systemd; to use cron, a launchd agent or anything else, run `ws upgrade --unattended`
under it (it needs `INVOCATION_ID` and a systemd parent today: `ws.upgrade.unattended_refusal` is the one place to
widen) once a day.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path

from ws import paths, selfcmd
from ws.output import WsError

Runner = Callable[..., subprocess.CompletedProcess]

SERVICE, TIMER = "ws-upgrade.service", "ws-upgrade.timer"
UNITS = (SERVICE, TIMER)
# where `enable` links a unit, for each target a user manager reaches at start: a link there is the switch turned on
TARGETS = ("default", "basic", "timers", "sockets", "paths")
LINK_DIRS = tuple(f"{t}.target.{kind}" for t in TARGETS for kind in ("wants", "requires", "upholds"))
SWITCH = "auto-upgrade.on"  # in ws's config folder: protected, so the switch is the person's
TRIGGER_SLACK_US = 120_000_000  # the timer fired at most this long before the service left inactive
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
    tree = ws.parent.parent  # a way back killed between its two renames leaves the copy beside it: finish it, no Python
    recover = (
        f'ExecStopPost=/bin/sh -c \'test -d "{tree}" || mv "{tree}.ws-restore" "{tree}" 2>/dev/null'
        f' || mv "{tree}.ws-failed" "{tree}"\'\n'
    )
    return {
        SERVICE: (
            "[Unit]\nDescription=ws: unattended upgrade (newest release, proved, checked, rolled back offline on any failure)\n"
            "OnFailure=ws-alert@%n.service\n\n"
            f"[Service]\nType=oneshot\nExecStart={run} --unattended\nTimeoutStartSec={UPGRADE_TIMEOUT_S}\n"
            f"NoNewPrivileges=yes\n{env}{recover}"
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


def switch_path(env: Mapping[str, str]) -> Path:
    return paths.config_dir(dict(env)) / SWITCH


def show(unit: str, props: tuple[str, ...], run: Runner = subprocess.run) -> dict[str, str]:
    """`systemctl --user show UNIT -p ...` as a dict; empty when systemctl can't say (which the check counts as a failure)."""
    try:
        r = run(
            ["systemctl", "--user", "show", unit, "-p", ",".join(props)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    if r.returncode != 0:
        return {}
    return dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)


PULLS = ("WantedBy", "RequiredBy", "RequisiteOf", "BoundBy", "UpheldBy", "OnFailureOf", "OnSuccessOf", "PartOf")
_PROPS = ("TriggeredBy", "DropInPaths", "FragmentPath", "NeedDaemonReload", "InvocationID", *PULLS)
_TIME = ("InactiveExitTimestampMonotonic", "LastTriggerUSecMonotonic")


def self_check(env: Mapping[str, str], ws: Path, run: Runner = subprocess.run) -> list[str]:
    """Why this start of the service is not the person's timer firing, or nothing. Read from systemd itself, never from a
    file a session wrote: the switch, who triggered this start and when, what else pulls either unit in, drop-ins, a
    pending reload, and the unit files' text."""
    if not switch_path(env).is_file():
        return ["the switch is off (`ws upgrade --auto on` turns it on, from a terminal of your own)"]
    svc, tim = show(SERVICE, (*_PROPS, *_TIME), run), show(TIMER, (*_PROPS, *_TIME), run)
    if not svc or not tim:
        return ["systemd could not say how this run was started"]
    out = []
    if svc.get("TriggeredBy", "").split() != [TIMER]:
        out.append(f"{SERVICE} is triggered by {svc.get('TriggeredBy') or 'nothing'}, not {TIMER} alone")
    if svc.get("InvocationID") != env.get("INVOCATION_ID"):
        out.append("this process is not the service's current run")
    try:
        fired, began = (
            int(tim.get("LastTriggerUSecMonotonic", "0")),
            int(svc.get("InactiveExitTimestampMonotonic", "0")),
        )
    except ValueError:
        fired, began = 0, 0
    if not fired or not 0 <= began - fired <= TRIGGER_SLACK_US:
        out.append(f"this run was not started by {TIMER} firing just now")
    want = {SERVICE: "", TIMER: "timers.target"}
    texts = units(ws)
    for name, props in ((SERVICE, svc), (TIMER, tim)):
        for key in PULLS:
            got = props.get(key, "").split()
            if got and got != ([want[name]] if key == "WantedBy" and want[name] else []):
                out.append(f"{name} is pulled in by {key}={' '.join(got)}")
        if props.get("DropInPaths", "").strip():
            out.append(f"{name} has a drop-in ({props['DropInPaths'].split()[0]})")
        if props.get("NeedDaemonReload", "no") != "no":
            out.append(f"{name} changed on disk since systemd loaded it")
        frag = unit_dir(env) / name
        if props.get("FragmentPath") != str(frag):
            out.append(f"{name} is loaded from {props.get('FragmentPath') or 'nowhere'}, not {frag}")
        else:
            try:
                same = frag.read_text(encoding="utf-8") == texts[name]
            except OSError:
                same = False
            if not same:
                out.append(f"{frag} is not the text ws writes")
    return out


def switch(env: Mapping[str, str], ws: Path, on: bool, run: Runner = subprocess.run) -> list[str]:
    """Turn the unattended upgrade on (write the units, reload, enable and start the timer, then the switch file) or off
    (stop and disable the timer, remove the switch). Returns what failed; the switch file is written only when all of it
    worked, so a half-done `on` stays off."""

    def ctl(*args: str) -> bool:
        try:
            return (
                run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=60, check=False).returncode
                == 0
            )
        except (OSError, subprocess.SubprocessError):
            return False

    path = switch_path(env)
    if not on:
        path.unlink(missing_ok=True)
        return [] if ctl("disable", "--now", TIMER) else [f"systemctl --user disable --now {TIMER} failed"]
    write(env, ws)
    for args in (("daemon-reload",), ("enable", "--now", TIMER)):
        if not ctl(*args):
            return [f"systemctl --user {' '.join(args)} failed"]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("on: the unattended upgrade (ws upgrade --auto off turns it off)\n", encoding="utf-8")
    return []
