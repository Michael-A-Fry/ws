"""Where ws keeps its files: four folders that follow the XDG base directory rules, or one folder you name.

Every module asks this one for a folder and builds its paths below it; none spells `~/.ws` or reads `WS_HOME` itself
(`tests/test_paths.py` fails on one that does). The four folders:

    config  `$XDG_CONFIG_HOME/ws`  (default `~/.config/ws`)       what you write: policy, profile, jobs, secrets registry
    state   `$XDG_STATE_HOME/ws`   (default `~/.local/state/ws`)  what ws writes as it works: logs, run records,
                                                                  approvals, sessions, the generated gate folder
    data    `$XDG_DATA_HOME/ws`    (default `~/.local/share/ws`)  what you would miss: the secrets store, eval tasks
    cache   `$XDG_CACHE_HOME/ws`   (default `~/.cache/ws`)        what ws can rebuild: the memory search index

The defaults are the same on macOS and Linux. A relative `XDG_*` value is ignored, as the specification says. Native
Windows is not supported (use WSL2).

Which layout is in use, first match wins:
  1. `WS_HOME` set: that one folder holds all four, laid out as ws always did (`policy.toml`, `logs/`, `secrets/` side
     by side). Tests and scripts use it. `WS_CONFIG_DIR`, `WS_STATE_DIR`, `WS_DATA_DIR` and `WS_CACHE_DIR` each
     override one folder (an absolute path); `env_for` returns the variables that hand a child the same layout.
  2. No `WS_HOME`, and a `~/.ws` folder from an earlier ws, and none of ws's own entries in the XDG folders: ws keeps
     using `~/.ws` and `ws doctor` says to run `ws migrate`. Nothing moves by itself.
  3. No `~/.ws`: the XDG folders.
  4. A `~/.ws` folder AND ws's entries in the XDG folders: `layout` raises a typed `WsError` naming both, because ws
     can't tell which holds the truth. `probe` reports it without raising, for `ws doctor` and `ws migrate`.

Elsewhere in the code and docs, `<config>`, `<state>`, `<data>` and `<cache>` stand for these four folders.

The table below is the one list of what ws keeps at the top of each folder. `ws migrate` plans from it, the gate's
self-protection and the wall's read-only folders come from it, and nothing outside it is moved or protected by name.
Python 3.11+, standard library only; nothing here imports a POSIX-only module.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from ws.output import WsError

KINDS = ("config", "state", "data", "cache")

# The entries ws keeps at the top of each folder (a name is a file or a folder).
NAMES: dict[str, tuple[str, ...]] = {
    "config": ("policy.toml", "profile.toml", "secrets.toml", "jobs", "release"),
    "state": (
        "logs",
        "runs",
        "approvals",
        "sessions",
        "gate",
        "wall",
        "backups",
        "run",
        "home",
        "install-manifest.json",
        "ratings.jsonl",
        "versions.json",
        "migrate-manifest.json",
    ),
    "data": ("secrets", "eval"),
    "cache": ("index",),
}
# Of those, the ones a session must never rewrite (the gate and the wall protect each as a path under its own folder).
CONTROL_NAMES = (
    "profile.toml",
    "policy.toml",
    "secrets.toml",
    "jobs",
    "release",
    "runs",
    "logs",
    "sessions",
    "approvals",
    "install-manifest.json",
    "ratings.jsonl",
)
ENV_DIRS = {"config": "WS_CONFIG_DIR", "state": "WS_STATE_DIR", "data": "WS_DATA_DIR", "cache": "WS_CACHE_DIR"}
_XDG = {  # kind -> (variable, default below HOME)
    "config": ("XDG_CONFIG_HOME", ".config"),
    "state": ("XDG_STATE_HOME", ".local/state"),
    "data": ("XDG_DATA_HOME", ".local/share"),
    "cache": ("XDG_CACHE_HOME", ".cache"),
}
LEGACY_NAME = ".ws"
_OWNED_KINDS = ("config", "state", "data")  # a cache folder can be made by anything; it does not say ws lives there


def kind_of(name: str) -> str | None:
    """The folder kind that keeps the top-level entry `name`, or None for a name ws does not know."""
    for kind, names in NAMES.items():
        if name in names:
            return kind
    return None


@dataclass(frozen=True)
class Layout:
    """The four folders, absolute. `mode` is "single" (WS_HOME or the per-folder variables), "xdg" or "legacy" (`~/.ws`)."""

    mode: str
    config: Path
    state: Path
    data: Path
    cache: Path

    def dir(self, kind: str) -> Path:
        return getattr(self, kind)

    def distinct(self) -> tuple[Path, ...]:
        """The folders without repeats, in config, state, data, cache order (one folder in a single-root layout)."""
        return tuple(dict.fromkeys(self.dir(k) for k in KINDS))

    def path_of(self, name: str) -> Path:
        """Where the top-level entry `name` lives. An unknown name is a bug in the caller, so it raises."""
        kind = kind_of(name)
        if kind is None:
            raise WsError("internal", f"ws keeps no top-level entry named {name!r}", "add it to ws.paths.NAMES")
        return self.dir(kind) / name


@dataclass(frozen=True)
class Probe:
    """What is on disk, without choosing: `state` is "single", "xdg", "legacy", "both" (a `~/.ws` and ws's own entries
    in the XDG folders) or "fresh" (neither). `owned` lists the ws entries found in the XDG folders."""

    state: str
    legacy: Path
    xdg: Layout
    owned: tuple[Path, ...]


def _env(env: Mapping[str, str] | None) -> Mapping[str, str]:
    return os.environ if env is None else env


def _abs(value: str, e: Mapping[str, str]) -> Path:
    """`value` as an absolute path: a leading `~` is the HOME of `e`, anything else relative to the folder ws runs in."""
    if value == "~" or value.startswith("~/"):
        value = str(home_dir(e)) + value[1:]
    return Path(os.path.abspath(value))


def home_dir(env: Mapping[str, str] | None = None) -> Path:
    """The person's home folder: `HOME` in `env`, else the system's. Always absolute (anything else is an error)."""
    e = _env(env)
    raw = e.get("HOME")
    home = Path(raw) if raw else Path.home()
    if not home.is_absolute():
        raise WsError(
            "bad_config", f"HOME is {str(home)!r}, which is not an absolute path", "set HOME to your home folder"
        )
    return home


def legacy_dir(env: Mapping[str, str] | None = None) -> Path:
    """`~/.ws`, where ws kept everything before the XDG folders."""
    return home_dir(env) / LEGACY_NAME


def xdg_layout(env: Mapping[str, str] | None = None) -> Layout:
    """The XDG folders for this environment, whether or not they exist."""
    e = _env(env)
    home = home_dir(e)
    out = {}
    for kind, (var, default) in _XDG.items():
        base = e.get(var)
        out[kind] = (Path(base) if base and os.path.isabs(base) else home / default) / "ws"
    return Layout("xdg", out["config"], out["state"], out["data"], out["cache"])


def _owned_entries(xdg: Layout) -> tuple[Path, ...]:
    found = []
    for kind in _OWNED_KINDS:
        for name in NAMES[kind]:
            p = xdg.dir(kind) / name
            if os.path.lexists(p):
                found.append(p)
    return tuple(found)


def _overrides(e: Mapping[str, str]) -> dict[str, Path]:
    out = {}
    for kind, var in ENV_DIRS.items():
        raw = e.get(var)
        if raw:
            if not (raw == "~" or raw.startswith("~/") or os.path.isabs(raw)):
                raise WsError(
                    "bad_config", f"{var} is {raw!r}, which is not an absolute path", f"set {var} to a full path"
                )
            out[kind] = _abs(raw, e)
    return out


def _single(root: Path, over: dict[str, Path]) -> Layout:
    return Layout("single", *(over.get(k, root) for k in KINDS))


def probe(env: Mapping[str, str] | None = None) -> Probe:
    """Look at the disk and say which layout it holds, without raising for an unclear one."""
    e = _env(env)
    xdg = xdg_layout(e)
    legacy = legacy_dir(e)
    owned = _owned_entries(xdg)
    if e.get("WS_HOME") or any(e.get(v) for v in ENV_DIRS.values()):
        return Probe("single", legacy, xdg, owned)
    has_legacy = legacy.is_dir()
    if has_legacy and owned:
        return Probe("both", legacy, xdg, owned)
    if has_legacy:
        return Probe("legacy", legacy, xdg, owned)
    return Probe("xdg" if owned else "fresh", legacy, xdg, owned)


def layout(env: Mapping[str, str] | None = None) -> Layout:
    """The layout in use (see the module docstring). Raises `WsError` (kind `ambiguous_layout`) when a `~/.ws` and ws's
    own entries in the XDG folders both exist, and `bad_config` for a relative `WS_*_DIR` or a `~/.ws` that is a file."""
    e = _env(env)
    over = _overrides(e)
    if e.get("WS_HOME"):
        return _single(_abs(e["WS_HOME"], e), over)
    pr = probe(e)
    if over:  # per-folder overrides without a root: the rest follow the XDG rules
        base = xdg_layout(e) if pr.state != "legacy" else _single(pr.legacy, {})
        return Layout("single", *(over.get(k, base.dir(k)) for k in KINDS))
    if pr.state == "both":
        shown = ", ".join(str(p) for p in pr.owned[:3]) + (" ..." if len(pr.owned) > 3 else "")
        raise WsError(
            "ambiguous_layout",
            f"ws files are in two places: {pr.legacy} (the old folder) and {shown}",
            "run `ws migrate` to finish moving to the new folders, or move the old folder aside if it is a leftover",
        )
    if pr.state == "legacy":
        return Layout("legacy", *(pr.legacy for _ in KINDS))
    if os.path.lexists(pr.legacy) and not pr.legacy.is_dir():
        raise WsError(
            "bad_config", f"{pr.legacy} exists and is not a folder", "move it aside; ws keeps its files elsewhere now"
        )
    return pr.xdg


def config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Policy, profile, jobs and the secrets registry: what you write."""
    return layout(env).config


def state_dir(env: Mapping[str, str] | None = None) -> Path:
    """Logs, run records, approvals, sessions and the generated gate folder: what ws writes as it works."""
    return layout(env).state


def data_dir(env: Mapping[str, str] | None = None) -> Path:
    """The secrets store and the eval tasks: what you would miss."""
    return layout(env).data


def cache_dir(env: Mapping[str, str] | None = None) -> Path:
    """What ws can rebuild: the memory search index."""
    return layout(env).cache


def roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Every distinct ws folder in the layout in use: what the wall makes read-only and the gate refuses writes to."""
    return layout(env).distinct()


def work_dir(env: Mapping[str, str] | None = None) -> Path:
    """`$XDG_STATE_HOME/ws-work` (default `~/.local/state/ws-work`): the folders of jobs with no `cwd`. Beside the ws
    folders and not in one, because the wall makes those read-only and a job must be able to write its work."""
    e = _env(env)
    state = e.get("XDG_STATE_HOME")
    base = Path(state) if state and os.path.isabs(state) else home_dir(e) / ".local" / "state"
    return base / "ws-work"


def old_work_dir(env: Mapping[str, str] | None = None) -> Path:
    """Where jobs with no `cwd` worked before 0.1.8: `<state home>/ws/work`. `ws uninstall` still tidies it."""
    e = _env(env)
    state = e.get("XDG_STATE_HOME")
    base = Path(state) if state and os.path.isabs(state) else home_dir(e) / ".local" / "state"
    return base / "ws" / "work"


def candidates(env: Mapping[str, str] | None = None) -> tuple[Layout, ...]:
    """Every layout ws could be using here: the one in use (when it can be told), the XDG folders and `~/.ws`. What guards
    ws's files covers all of them, so a session can't plant files in the layout that is not in use to confuse the one
    that is (a `~/.config/ws` beside a `~/.ws` makes the layout ambiguous, which stops ws)."""
    e = _env(env)
    out: list[Layout] = []
    try:
        out.append(layout(e))
    except WsError:
        pass  # an unclear layout is still guarded by the other two
    legacy = legacy_dir(e)
    out += [xdg_layout(e), Layout("legacy", legacy, legacy, legacy, legacy)]
    return tuple(out)


def protected_roots(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Every folder of every layout in `candidates`, without repeats: what the gate refuses writes to and the wall makes
    read-only. Unlike `roots`, it never raises."""
    return tuple(dict.fromkeys(d for lay in candidates(env) for d in lay.distinct()))


def control_paths(env: Mapping[str, str] | None = None) -> list[str]:
    """ws's own control files and folders in the layout in use, as absolute paths (a path covers what is under it). When
    the layout can't be told, those of every layout in `candidates`. Never raises."""
    try:
        lays: tuple[Layout, ...] = (layout(env),)
    except WsError:
        lays = candidates(env)
    return list(dict.fromkeys(str(lay.path_of(n)) for lay in lays for n in CONTROL_NAMES))


def secret_dirs(env: Mapping[str, str] | None = None) -> tuple[Path, ...]:
    """Where the secrets store is: in the layout in use. When that can't be told (both `~/.ws` and the XDG folders exist),
    where it may be in any layout, so nothing is left readable. Never raises."""
    try:
        return (layout(env).path_of("secrets"),)
    except WsError:
        return tuple(dict.fromkeys(lay.path_of("secrets") for lay in candidates(env)))


def control_paths_in(root: Path) -> list[str]:
    """The same names under one folder, for a policy file that sits in a folder of its own (a test, a copy)."""
    return [str(Path(root).expanduser().absolute() / n) for n in CONTROL_NAMES]


def env_for(env: Mapping[str, str] | None = None, *, persist: bool = False) -> dict[str, str]:
    """The variables that give a child process this layout, whatever HOME and XDG variables it has: `WS_HOME` for a
    single folder, else the four `WS_*_DIR` ones. `persist` is for a file that outlives this moment (a timer unit): an
    earlier ws's `~/.ws` is then not pinned, so the unit follows the files to the XDG folders after `ws migrate`."""
    e = _env(env)
    lay = layout(e)
    if lay.mode == "legacy":
        return {} if persist else {"WS_HOME": str(lay.config)}
    if lay.mode == "single" and len(lay.distinct()) == 1:
        return {"WS_HOME": str(lay.config)}
    return {ENV_DIRS[k]: str(lay.dir(k)) for k in KINDS}


def describe(env: Mapping[str, str] | None = None) -> str:
    """Where the layout comes from, in a few words (for `ws config`)."""
    e = _env(env)
    if e.get("WS_HOME"):
        return "$WS_HOME"
    if any(e.get(v) for v in ENV_DIRS.values()):
        return "$WS_*_DIR"
    state = probe(e).state
    return {"legacy": "default (~/.ws, from an earlier ws; run `ws migrate`)", "both": "ambiguous"}.get(
        state, "default (XDG folders)"
    )
