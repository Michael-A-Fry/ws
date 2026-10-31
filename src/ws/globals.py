"""`ws globals`: what ws installs globally stays global.

A repo may carry its own skills, agents, workflows and hooks, but a copy of a global one (same name, or the same bytes
under another name) forks it quietly and the fix never reaches that repo. `ws globals check` fails on such a shadow
unless the repo lists it in `.ws/overrides.toml` with a reason; a listed override passes and is reported so it can be
promoted to global or dropped.

Each runner adapter says where its globals live (`GLOBALS` in ws/adapters/*.py). The global set is read from the
runner's user config when that exists, else from a manifest (`ws globals manifest`: names and sha256, no content), so
CI, which has no user config, can still check.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ws import adapters

OVERRIDES = Path(".ws") / "overrides.toml"
MANIFEST_ENV = "WS_GLOBALS_MANIFEST"
SCHEMA = 1
_SKIP_DIRS = {"__pycache__", "node_modules"}
_WALK_SKIP = {".git", "node_modules", ".venv", "__pycache__"}  # never searched for nested config folders
_MIN_BYTES = 64  # a smaller file ("pyyaml", "exit 0") is too common to call a copy
_MAX_BYTES = 5_000_000
_MAX_DEPTH = 8
_WALK_DEPTH = 6
MANIFEST_FILE = Path(".ws") / "globals.json"  # a manifest committed in the repo is used when none is given


@dataclass(frozen=True)
class Entry:
    kind: str  # the sub-folder, e.g. "skills"
    name: str  # what a repo copy is matched on: the skill folder, the agent file, the workflow stem
    path: str  # relative to the runner's config folder, posix
    sha256: str


@dataclass(frozen=True)
class Shadow:
    path: str  # relative to the repo, posix
    why: str


@dataclass
class Report:
    status: str = "ok"  # ok | warn | fail
    compared: int = 0  # global items compared against
    source: str = ""
    shadows: list[Shadow] = field(default_factory=list)  # unlisted: these fail
    overridden: list[tuple[Shadow, str]] = field(default_factory=list)  # listed, with the reason
    stale: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "schema": SCHEMA,
            "status": self.status,
            "compared": self.compared,
            "source": self.source,
            "shadows": [{"path": s.path, "why": s.why} for s in self.shadows],
            "overrides": [{"path": s.path, "why": s.why, "reason": r} for s, r in self.overridden],
            "stale": self.stale,
            "errors": self.errors,
            "warnings": self.warnings,
        }


# --- reading a config folder ---------------------------------------------------------------------------------------


def _expand(template: str, env: dict) -> Path:
    if template.startswith("~"):
        home = env.get("HOME") or str(Path.home())
        return Path(home + template[1:])
    return Path(template)


def user_dir(layout: dict, env: dict) -> Path:
    if layout.get("user_dir_env") and env.get(layout["user_dir_env"]):
        return Path(env[layout["user_dir_env"]]).expanduser()
    if layout.get("xdg"):
        base = env.get("XDG_CONFIG_HOME")
        if base:
            return Path(base) / layout["xdg"]
    return _expand(layout["user_dir"], env)


def _sha(path: Path) -> str | None:
    """sha256 of a file's bytes; None for a huge or unreadable file."""
    try:
        if path.stat().st_size < _MAX_BYTES:
            return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        pass
    return None


def _files(top: Path, depth: int = 0):
    if top.is_file():
        yield top
    elif top.is_dir() and depth <= _MAX_DEPTH:
        try:
            children = sorted(top.iterdir())
        except OSError:
            return
        for c in children:
            if c.name.startswith(".") or c.name in _SKIP_DIRS:
                continue
            yield from _files(c, depth + 1)


def _key(top: Path, how: str) -> str:
    return top.name if (how in ("dir", "name") or top.is_dir()) else top.stem


def _main_file(top: Path, files: list[Path], how: str) -> Path | None:
    """The file that stands for an item: the file itself, or SKILL.md of a skill folder."""
    if top.is_file():
        return top
    if how == "dir":
        return next((f for f in files if f.parent == top and f.name.lower() == "skill.md"), None)
    return None


def scan(root: Path, kinds: dict[str, str], skip_inside: list[Path] | None = None) -> list[Entry]:
    """What lives under root/<kind>/, as Entries. Each item (a skill folder, an agent file ...) gets one entry for its
    name, carrying the hash of its main file; a folder with several files gets a second, carrying a digest of the
    whole folder. Files that are too small to mean anything are matched on name only. A top-level item that resolves
    inside one of `skip_inside` (a repo link to the global itself) is left out: that is using the global."""
    out: list[Entry] = []
    for kind, how in kinds.items():
        base = root / kind
        try:
            tops = sorted(base.iterdir()) if base.is_dir() else []
        except OSError:
            continue
        for top in tops:
            if top.name.startswith(".") or top.name in _SKIP_DIRS:
                continue
            if skip_inside:
                real = _resolved(top)
                if any(real.is_relative_to(s) for s in skip_inside):
                    continue
            name, folder = _key(top, how), f"{kind}/{top.name}"
            files = list(_files(top))
            main = _main_file(top, files, how)
            digest = _sha(main) if main else None
            big = digest and main.stat().st_size >= _MIN_BYTES
            out.append(Entry(kind, name, main.relative_to(root).as_posix() if main else folder, digest if big else ""))
            if top.is_dir() and len(files) > 1:
                parts = [(f.relative_to(top).as_posix(), _sha(f)) for f in files]
                if all(h for _, h in parts) and sum(f.stat().st_size for f in files) >= _MIN_BYTES:
                    whole = hashlib.sha256("\n".join(f"{n}:{h}" for n, h in parts).encode()).hexdigest()
                    out.append(Entry(kind, name, folder, whole))
    return out


def _resolved(p: Path) -> Path:
    try:
        return p.resolve()
    except OSError:
        return p


def user_entries(env: dict, only: str | None = None) -> dict[str, list[Entry]]:
    """runner -> the globals found in its user config folder (runners with none are left out)."""
    found = {}
    for name, mod in adapters.all_adapters().items():
        layout = getattr(mod, "GLOBALS", None)
        if layout is None or (only and name != only):
            continue
        entries = scan(user_dir(layout, env), layout["kinds"])
        if entries:
            found[name] = entries
    return found


def manifest(env: dict, only: str | None = None) -> dict:
    runners = {
        name: [{"kind": e.kind, "name": e.name, "path": e.path, "sha256": e.sha256} for e in entries]
        for name, entries in user_entries(env, only).items()
    }
    return {"schema": SCHEMA, "runners": runners}


def read_manifest(path: Path) -> dict[str, list[Entry]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or not isinstance(data.get("runners"), dict):
        raise ValueError(f"{path}: not a ws globals manifest (schema {SCHEMA})")
    try:
        return {
            runner: [Entry(e["kind"], e["name"], e["path"], e["sha256"]) for e in entries]
            for runner, entries in data["runners"].items()
        }
    except (KeyError, TypeError) as e:
        raise ValueError(f"{path}: manifest entry is malformed") from e


# --- overrides -----------------------------------------------------------------------------------------------------


def _too_broad(path: str, layouts: list[dict]) -> bool:
    """An override must name an item (<project_dir>/<kind>/<name>) or something inside one, not a whole folder."""
    parts = path.split("/")
    if ".." in parts:
        return True
    for lay in layouts:
        for i, p in enumerate(parts):
            if p == lay["project_dir"] and i + 2 < len(parts) and parts[i + 1] in lay["kinds"]:
                return False
    return True


def load_overrides(repo: Path, layouts: list[dict] | None = None) -> tuple[dict[str, str], list[str]]:
    """path -> reason, plus errors. A bad override (no reason, too broad, listed twice) is an error and is not applied."""
    f = repo / OVERRIDES
    if not f.is_file():
        return {}, []
    try:
        data = tomllib.loads(f.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        return {}, [f"{OVERRIDES.as_posix()} does not parse: {e}"]
    rows = data.get("override", [])
    if not isinstance(rows, list):
        return {}, [f"{OVERRIDES.as_posix()}: use [[override]] tables"]
    layouts = (
        layouts
        if layouts is not None
        else [m.GLOBALS for m in adapters.all_adapters().values() if hasattr(m, "GLOBALS")]
    )
    out: dict[str, str] = {}
    errors = []
    for i, row in enumerate(rows, 1):
        path = row.get("path") if isinstance(row, dict) else None
        reason = row.get("reason") if isinstance(row, dict) else None
        if not isinstance(path, str) or not path.strip():
            errors.append(f"override {i} has no path")
            continue
        path = path.strip().removeprefix("./").rstrip("/")
        if not isinstance(reason, str) or not reason.strip():
            errors.append(f"override {path}: a reason is required (why is this repo's copy better than the global?)")
        elif _too_broad(path, layouts):
            errors.append(f"override {path}: too broad; name an item, like .claude/skills/<name>, or a file inside one")
        elif path in out:
            errors.append(f"override {path}: listed twice")
        else:
            out[path] = reason.strip()
    return out, errors


def _covers(override: str, shadow_path: str) -> bool:
    return shadow_path == override or shadow_path.startswith(override + "/")


# --- the check -----------------------------------------------------------------------------------------------------


def find_repo(start: Path) -> Path | None:
    start = _resolved(start)
    for d in (start, *start.parents):
        if (d / ".git").exists():
            return d
    return None


def project_dirs(repo: Path, name: str) -> list[Path]:
    """Every `name` folder in the repo, nested ones included (a monorepo has several). Not searched: .git, node_modules,
    .venv, a folder with a .git of its own (another repo), or anything inside a config folder (worktrees live there)."""
    found: list[Path] = []
    for dirpath, dirs, _ in os.walk(repo):
        here = Path(dirpath)
        depth = len(here.relative_to(repo).parts)
        keep = []
        for d in sorted(dirs):
            if d == name:
                found.append(here / d)
            elif d not in _WALK_SKIP and depth < _WALK_DEPTH and not (here / d / ".git").exists():
                keep.append(d)
        dirs[:] = keep
    return found


def shadows_in(repo: Path, proj: Path, layout: dict, globals_: list[Entry], user: Path) -> list[Shadow]:
    if _resolved(proj) == _resolved(user):
        return []  # the repo is the config folder itself (a run from ~): nothing to shadow
    prefix = proj.relative_to(repo).as_posix()
    names = {(g.kind, g.name.lower()): g for g in globals_}
    hashes = {g.sha256: g for g in globals_ if g.sha256}
    # a repo link into the global tree, however the global itself is linked, is using it
    real_roots = [_resolved(user), *(_resolved(user / k) for k in layout["kinds"])]
    out: list[Shadow] = []
    by_item: dict[tuple[str, str], list[Entry]] = {}
    for e in scan(proj, layout["kinds"], skip_inside=real_roots):
        by_item.setdefault((e.kind, e.path.split("/")[1]), []).append(e)
    for (kind, top), items in sorted(by_item.items()):
        name = items[0].name.lower()
        if (kind, name) in names:
            out.append(
                Shadow(f"{prefix}/{kind}/{top}", f"same name as global {kind}/{names[kind, name].path.split('/')[1]}")
            )
            continue
        for e in items:
            g = hashes.get(e.sha256)
            if g:
                out.append(Shadow(f"{prefix}/{e.path}", f"same content as global {g.path}"))
                break
    return out


def check(repo: Path, env: dict, manifest_path: Path | None = None) -> Report:
    rep = Report()
    mod_layouts = {n: m.GLOBALS for n, m in adapters.all_adapters().items() if hasattr(m, "GLOBALS")}
    overrides, rep.errors = load_overrides(repo, list(mod_layouts.values()))
    from_user = user_entries(env)
    from_manifest: dict[str, list[Entry]] = {}
    if manifest_path is None and env.get(MANIFEST_ENV):
        manifest_path = Path(env[MANIFEST_ENV]).expanduser()
    if manifest_path is None and (repo / MANIFEST_FILE).is_file():
        manifest_path = repo / MANIFEST_FILE
    if manifest_path is not None:
        try:
            from_manifest = read_manifest(manifest_path)
        except (OSError, ValueError) as e:
            rep.errors.append(f"manifest: {e}")
    sources = set()
    found: list[Shadow] = []
    for runner, layout in mod_layouts.items():
        if runner in from_user:
            entries, src = from_user[runner], "user config"
        elif runner in from_manifest:
            entries, src = from_manifest[runner], "manifest"
        else:
            continue
        sources.add(src)
        rep.compared += len({(e.kind, e.name) for e in entries})
        for proj in project_dirs(repo, layout["project_dir"]):
            found += shadows_in(repo, proj, layout, entries, user_dir(layout, env))
    rep.source = " and ".join(sorted(sources))
    used: set[str] = set()
    for s in found:
        match = next((o for o in overrides if _covers(o, s.path)), None)
        if match is None:
            rep.shadows.append(s)
        else:
            used.add(match)
            rep.overridden.append((s, overrides[match]))
    if sources:  # with nothing to compare against, no override can be called stale
        rep.stale = sorted(set(overrides) - used)
    if not sources and not rep.errors:
        rep.warnings.append(
            "nothing to compare: no user config and no manifest (--manifest, $WS_GLOBALS_MANIFEST or "
            f"{MANIFEST_FILE.as_posix()}), so this run checked nothing; --strict makes that a failure"
        )
    rep.status = "fail" if rep.shadows or rep.errors else "warn" if rep.warnings else "ok"
    return rep


def render(rep: Report) -> str:
    out = [f"error: {e}" for e in rep.errors]
    out += [
        f"shadow: {s.path} — {s.why} (delete it, or list it in .ws/overrides.toml with a reason)" for s in rep.shadows
    ]
    out += [f"override: {s.path} — {r} (promote to global or drop)" for s, r in rep.overridden]
    out += [f"stale override: {p} (shadows nothing now; drop it)" for p in rep.stale]
    out += [f"warn: {w}" for w in rep.warnings]
    out.append(
        f"ws globals: {len(rep.shadows)} shadows, {len(rep.overridden)} overrides, {len(rep.stale)} stale"
        f" ({rep.compared} global items from {rep.source or 'nowhere'})"
    )
    return "\n".join(out)


def summary(rep: Report) -> tuple[str, str, str]:
    """(status, detail, fix) for `ws doctor`."""
    n = f"{rep.compared} global items from {rep.source or 'nowhere'}"
    if rep.errors:
        return "fail", "; ".join(rep.errors), "fix .ws/overrides.toml or the manifest"
    if rep.shadows:
        first = ", ".join(s.path for s in rep.shadows[:3]) + (" ..." if len(rep.shadows) > 3 else "")
        return "fail", f"{len(rep.shadows)} shadow(s) of a global: {first}", "run `ws globals check`"
    if rep.warnings:
        return "warn", rep.warnings[0], ""
    extra = f"; {len(rep.overridden)} listed override(s)" if rep.overridden else ""
    extra += f"; {len(rep.stale)} stale" if rep.stale else ""
    return "ok", f"no shadows ({n}){extra}", ""


def doctor_check(env: dict, cwd: Path | None = None) -> tuple[str, str, str]:
    """The WS005 check for the repo at the current directory."""
    try:
        repo = find_repo(cwd or Path.cwd())
    except OSError:
        repo = None
    if repo is None:
        return "ok", "not in a repo", ""
    return summary(check(repo, env))


# --- commands ------------------------------------------------------------------------------------------------------


def cmd_check(args: argparse.Namespace) -> int:
    start = Path(args.repo) if args.repo else Path.cwd()
    repo = find_repo(start) or start
    rep = check(repo, dict(os.environ), Path(args.manifest).expanduser() if args.manifest else None)
    print(json.dumps(rep.as_dict(), indent=2) if args.json else render(rep))
    return 1 if rep.status == "fail" or (args.strict and rep.status == "warn") else 0


def cmd_manifest(args: argparse.Namespace) -> int:
    data = manifest(dict(os.environ), args.runner)
    if not data["runners"]:
        print("ws: no globals found in any runner's user config", file=sys.stderr)
        return 1
    print(json.dumps(data, indent=2, sort_keys=True))
    return 0


def register(sub) -> None:
    g = sub.add_parser("globals", help="keep what ws installs globally global: no quiet repo copies")
    gs = g.add_subparsers(dest="globals_command", required=True)

    c = gs.add_parser("check", help="fail on a repo copy of a global skill, agent, workflow or hook")
    c.add_argument("--repo", metavar="DIR", help="the repo to check (default: the current directory)")
    c.add_argument("--manifest", metavar="FILE", help="globals manifest, used when there is no user config")
    c.add_argument("--json", action="store_true", help="machine-readable report")
    c.add_argument("--strict", action="store_true", help="also exit 1 when there was nothing to compare")
    c.set_defaults(func=cmd_check)

    m = gs.add_parser("manifest", help="print names and sha256 of this machine's globals (no content) as JSON")
    m.add_argument("--runner", choices=list(adapters.all_adapters()), help="only this runner")
    m.set_defaults(func=cmd_manifest)
