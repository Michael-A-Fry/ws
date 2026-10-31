"""`ws install` and `ws uninstall`: the engine that plans, writes and undoes what ws puts on a machine.

The terminal commands, the setup screens, `ws plugin add|remove`, the skills installer and the scheduler role all go
through the same three calls, so they agree: `plan` works out the files and folders the answers call for (it writes
nothing), `apply` writes only what differs and records what ws itself created in a manifest, and `uninstall` removes
only what that record says ws made. A second run with the same answers reports 0 changes and leaves every byte alone.

The promises, each pinned by a test:

- ws never deletes a path it did not create, and never acts outside the folders it manages. Those folders are worked
  out from the live profile each time; the manifest is untrusted input and cannot widen them.
- A file ws made that the person has since edited is left in place and listed, never overwritten or deleted.
- Every write is atomic and keeps the file's permission bits. A run that fails part way still records what it made.
- Removal uses `unlink` and `rmdir` only; nothing is ever deleted recursively.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import math
import os
import secrets
import stat
import sys
import tomllib
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws import __version__, adapters, notesdetect, osinfo, output, policy, profile, roles
from ws.jobs import JobError
from ws.output import WsError

MANIFEST = "install-manifest.json"
SCHEMA = 1
DEFAULT_MEMORY = "~/ws-memory"
_DEFAULT_MEMORY_ADAPTER = "markdown-git"
_TMP_SUFFIX = ".ws-tmp"


class InstallError(Exception):
    """A problem the person can fix. The message is safe to print as it is."""


def _default_modules() -> frozenset[str]:
    from ws import modules  # late: ws.modules imports this module

    return modules.default_on()


@dataclass(frozen=True)
class Answers:
    """Everything `plan` needs to know about the person's choices."""

    stays_on: bool = False
    server: str = ""
    notes: str = "~/Notes"
    modules: frozenset[str] = field(default_factory=_default_modules)
    runner: str = ""
    model: str = ""
    cap: float | None = None  # monthly spend cap in US$
    memory: str = ""  # empty = keep the profile's memory folder, else DEFAULT_MEMORY


@dataclass(frozen=True)
class Step:
    """One file or folder ws wants to exist."""

    path: Path
    kind: str  # "file" | "dir"
    content: str | None = None  # the whole text of a file; None for a dir
    what: str = ""
    root: bool = False  # a folder ws manages (notes, memory): uninstall may act inside it
    toml: bool = False  # compare as TOML data, so a re-formatted file with the same data counts as unchanged


@dataclass
class Result:
    created: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    edited: list[Path] = field(default_factory=list)  # generated files the person changed: left alone
    notes: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.created) + len(self.changed)

    def summary(self) -> str:
        left = f", {len(self.edited)} left because you edited them" if self.edited else ""
        if self.changes == 0:
            return f"0 changes ({len(self.unchanged)} unchanged{left})"
        return f"{len(self.created)} created, {len(self.changed)} changed, {len(self.unchanged)} unchanged{left}"


@dataclass
class Removal:
    removed: list[Path] = field(default_factory=list)
    edited: list[Path] = field(default_factory=list)  # the hash no longer matches what ws wrote
    not_empty: list[Path] = field(default_factory=list)  # a folder with something of the person's in it
    not_ours: list[Path] = field(default_factory=list)  # in the manifest, but ws did not create it
    missing: list[Path] = field(default_factory=list)  # already gone
    refused: list[Path] = field(default_factory=list)  # outside the managed folders, or behind a symlink
    problems: list[str] = field(default_factory=list)


# ---- paths -----------------------------------------------------------------------------------------------------------


def _home(env: Mapping[str, str]) -> str:
    return env.get("HOME") or str(Path.home())


def expand(p: str, env: Mapping[str, str]) -> Path:
    """`~` and `~/...` mean the HOME in env; the result is absolute with `..` collapsed. `~user` is not expanded."""
    if p == "~" or p.startswith("~/"):
        p = _home(env) + p[1:]
    return Path(os.path.abspath(p))


def store_path(p: str, env: Mapping[str, str]) -> str:
    """How a folder is written to the profile: `~` forms stay as typed, anything else becomes absolute."""
    return p if p == "~" or p.startswith("~/") else str(expand(p, env))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ws_home(env: Mapping[str, str]) -> Path:
    return Path(os.path.abspath(profile.ws_home(dict(env))))


def manifest_path(env: Mapping[str, str] | None = None) -> Path:
    return _ws_home(os.environ if env is None else env) / MANIFEST


def profile_path(env: Mapping[str, str] | None = None) -> Path:
    return _ws_home(os.environ if env is None else env) / "profile.toml"


def _too_broad(root: Path, env: Mapping[str, str]) -> bool:
    """The filesystem root, the person's home, or a folder that holds their home."""
    home = expand("~", env)
    return root.parent == root or root == home or root in home.parents


# ---- the profile -----------------------------------------------------------------------------------------------------


def _load_profile(env: Mapping[str, str]) -> dict:
    try:
        return profile.load(profile_path(env)) or {}
    except profile.ProfileError as e:
        raise InstallError(str(e)) from e


def _sub(doc: dict, key: str) -> dict:
    v = doc.get(key)
    return v if isinstance(v, dict) else {}


def _text(v) -> str:
    return v if isinstance(v, str) else ""


def answers_from_profile(env: Mapping[str, str] | None = None, home: Path | None = None) -> Answers:
    """What the profile already says, with defaults for the rest, so a re-run with no flags changes nothing."""
    from ws import modules  # late: ws.modules imports this module

    env = os.environ if env is None else env
    doc = _load_profile(env)
    machine, rls = _sub(doc, "machine"), _sub(doc, "roles")

    notes = _text(machine.get("notes")) or _text(_sub(rls, "notes").get("path"))
    if not notes:
        notes = notesdetect.suggest(Path(_home(env)))[0]

    table = doc.get("modules")
    if isinstance(table, dict):
        on = frozenset(n for n, v in table.items() if v is True and n in modules.REGISTRY)
    else:
        on = modules.default_on()

    runner = _text(_sub(rls, "runner").get("adapter"))
    cap = _sub(rls, "budget").get("monthly_cap_usd")
    good_cap = isinstance(cap, (int, float)) and not isinstance(cap, bool) and math.isfinite(cap)
    return Answers(
        stays_on=machine.get("stays_on") is True,
        server=_text(machine.get("server")),
        notes=notes,
        modules=on,
        runner=runner if runner in adapters.all_adapters() else "",
        model=_text(_sub(rls, "model").get("default")),
        cap=cap if good_cap else None,
        memory=_text(_sub(rls, "memory").get("path")),
    )


def _table_of(doc: dict, key: str, label: str) -> dict:
    v = doc.get(key, {})
    if not isinstance(v, dict):
        raise InstallError(f"profile: [{label}] must be a table")
    return dict(v)


def compose_profile(prof: dict, answers: Answers, env: Mapping[str, str], osi) -> dict:
    """The profile after these answers: everything present is kept and only the answered keys change."""
    from ws import modules

    unknown = sorted(answers.modules - set(modules.REGISTRY))
    if unknown:
        raise InstallError(f"unknown module '{unknown[0]}'; known: {', '.join(modules.REGISTRY)}")
    if answers.cap is not None and not (math.isfinite(answers.cap) and answers.cap > 0):
        raise InstallError("the monthly cap must be a number above 0")

    if not answers.memory:
        saved = _text(_sub(_sub(prof, "roles"), "memory").get("path"))
        answers = replace(answers, memory=saved or DEFAULT_MEMORY)

    out = {k: (dict(v) if isinstance(v, dict) else v) for k, v in prof.items()}
    machine = _table_of(out, "machine", "machine")
    rls = _table_of(out, "roles", "roles")
    mods = _table_of(out, "modules", "modules")

    machine.update(os=osi.kind, stays_on=answers.stays_on, server=answers.server, notes=store_path(answers.notes, env))

    def role(name: str) -> dict:
        v = rls.get(name, {})
        if not isinstance(v, dict):
            raise InstallError(f"profile: [roles.{name}] must be a table")
        rls[name] = dict(v)
        return rls[name]

    if answers.runner:
        role("runner")["adapter"] = answers.runner
    if answers.model:
        role("model")["default"] = answers.model
    if answers.cap is not None:
        whole = answers.cap == int(answers.cap)
        role("budget")["monthly_cap_usd"] = int(answers.cap) if whole else answers.cap

    for name, mod in modules.REGISTRY.items():
        if name in answers.modules:
            for rname, keys in mod.roles(answers, env).items():
                role(rname).update(keys)
        elif mods.get(name) is True or (name not in mods and mod.default_on):
            for rname in mod.role_names:
                rls.pop(rname, None)

    mods.update({name: name in answers.modules for name in modules.REGISTRY})
    out["machine"], out["modules"] = machine, mods
    if rls or "roles" in out:
        out["roles"] = rls
    return out


# ---- plan ------------------------------------------------------------------------------------------------------------


def _policy_step(env: Mapping[str, str]) -> tuple[Step, policy.Policy]:
    path = _ws_home(env) / "policy.toml"
    try:
        text = path.read_text(encoding="utf-8") if path.exists() else policy.default_toml()
        parsed = policy.loads(text)
    except (policy.PolicyError, OSError, UnicodeDecodeError) as e:
        raise InstallError(f"{path}: {e}") from e
    what = "your policy (kept as it is if you already have one)"
    return Step(path, "file", text, what, toml=True), parsed


def _role_steps(composed: dict, answers: Answers, env: Mapping[str, str], taken: set) -> list[Step]:
    out: list[Step] = []
    for role in ("memory", "backup"):
        cfg = _sub(composed, "roles").get(role)
        if not isinstance(cfg, dict):
            continue
        name = cfg.get("adapter", _DEFAULT_MEMORY_ADAPTER if role == "memory" else None)
        mod = roles.adapters(role).get(name) if isinstance(name, str) else None
        if mod is None:
            continue
        try:
            made = mod.steps(cfg, answers, env)
        except (JobError, ValueError, OSError) as e:
            raise InstallError(f"[roles.{role}]: {e}") from e
        out += [s for s in made if (s.path, s.kind) not in taken]
    return out


def plan(answers: Answers, env: Mapping[str, str] | None = None, osi=None) -> list[Step]:
    """The steps these answers call for, in order. Reads the machine; writes nothing."""
    from ws import modules

    env = os.environ if env is None else env
    osi = osinfo.detect(env=dict(env)) if osi is None else osi
    if not osi.supported:
        raise InstallError("native Windows is not supported; install WSL2 and run ws inside it")

    prof = _load_profile(env)
    if not answers.memory:
        saved = _text(_sub(_sub(prof, "roles"), "memory").get("path"))
        answers = replace(answers, memory=saved or DEFAULT_MEMORY)

    policy_step, parsed = _policy_step(env)
    composed = compose_profile(prof, answers, env, osi)
    steps = [policy_step, Step(profile_path(env), "file", profile.dumps(composed), "your profile", toml=True)]
    for name, mod in modules.REGISTRY.items():
        if name in answers.modules:
            steps += mod.steps(answers, env)

    taken = {(s.path, s.kind) for s in steps}
    steps += _role_steps(composed, answers, env, taken)

    if answers.runner:
        try:
            adapter = adapters.get(answers.runner)
        except KeyError as e:
            raise InstallError(str(e.args[0])) from e
        out = _ws_home(env) / "gate" / answers.runner
        files = adapter.generate(parsed, ["ws"], out, policy_step.path.resolve())
        steps += [Step(out / name, "file", text, f"{answers.runner} deny rules") for name, text in files.items()]
    return steps


# ---- the manifest ----------------------------------------------------------------------------------------------------


def _read_manifest(env: Mapping[str, str] | None = None) -> tuple[dict[str, dict], list[str]]:
    """(entries by path, recorded roots). A missing manifest is empty; one that does not read stops everything."""
    path = manifest_path(env)
    if not path.is_file():
        return {}, []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        raw = doc["entries"]
        if not isinstance(raw, list):
            raise TypeError("entries is not a list")
        listed = doc.get("roots", [])
        roots = [r for r in listed if isinstance(r, str)] if isinstance(listed, list) else []
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise InstallError(f"{path} does not read as a ws manifest ({type(e).__name__}); not touching anything") from e
    entries = {e["path"]: dict(e) for e in raw if isinstance(e, dict) and isinstance(e.get("path"), str)}
    return entries, roots


def _missing_parents(path: Path, virtual: set[Path]) -> list[Path]:
    """The folders above `path` that do not exist yet (nor will, from an earlier step), outermost first."""
    missing = []
    p = path.parent
    while p not in virtual and not p.exists():
        missing.append(p)
        p = p.parent
    return missing[::-1]


def _dir_entry(path: Path) -> dict:
    return {"path": str(path), "kind": "dir", "sha256": None, "created_by_ws": True}


def _save_manifest(env: Mapping[str, str], entries: dict[str, dict], roots: Iterable[str]) -> None:
    path = manifest_path(env)
    for d in _missing_parents(path, set()):  # ws's own home may not exist yet
        d.mkdir()
        entries[str(d)] = _dir_entry(d)
    doc = {
        "schema": SCHEMA,
        "ws_version": __version__,
        "roots": sorted(set(roots)),
        "entries": sorted(entries.values(), key=lambda e: e["path"]),
    }
    data = (json.dumps(doc, indent=2) + "\n").encode("utf-8")
    with contextlib.suppress(OSError):
        if path.is_file() and path.read_bytes() == data:
            return
    _write_atomic(path, data)


def forget(paths: Iterable[Path], env: Mapping[str, str] | None = None) -> None:
    """Drop these paths from the manifest because ws removed them itself."""
    env = os.environ if env is None else env
    entries, roots = _read_manifest(env)
    gone = [entries.pop(str(p)) for p in paths if str(p) in entries]
    if gone:
        _save_manifest(env, entries, roots)


# ---- writing ---------------------------------------------------------------------------------------------------------


def _write_atomic(path: Path, data: bytes) -> None:
    """Write beside the target, fsync, set the mode, then rename over it. No temp file survives a failure."""
    try:
        mode = stat.S_IMODE(os.stat(path).st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    tmp = path.parent / f".{path.name}.{secrets.token_hex(6)}{_TMP_SUFFIX}"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _same_toml(a: bytes, b: bytes) -> bool:
    try:
        return tomllib.loads(a.decode("utf-8")) == tomllib.loads(b.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False


def classify(step: Step, entries: Mapping[str, dict] | None = None, virtual: set[Path] | None = None) -> str:
    """What applying this step would do: created, changed, unchanged, or edited (a generated file the person changed)."""
    path = step.path
    if step.kind == "dir":
        if path.is_dir() or (virtual is not None and path in virtual):
            return "unchanged"
        if os.path.lexists(path):
            raise InstallError(f"{path} is in the way: ws needs a folder there")
        return "created"
    if path.is_dir():
        raise InstallError(f"{path} is a folder: ws needs a file there")
    try:
        disk = path.read_bytes()
    except FileNotFoundError:
        return "created"
    want = (step.content or "").encode("utf-8")
    if disk == want or (step.toml and _same_toml(disk, want)):
        return "unchanged"
    prior = (entries or {}).get(str(path))
    if not step.toml and prior and prior.get("created_by_ws") is True and prior.get("sha256") != sha256(disk):
        return "edited"
    return "changed"


def apply(
    steps: Iterable[Step],
    env: Mapping[str, str] | None = None,
    dry_run: bool = False,
    progress: Callable[[Step, str], None] | None = None,
) -> Result:
    """Write what differs and record what ws created. `progress(step, action)` hears each finished step (never on a
    dry run). If a step fails, what was made so far is still recorded and the first error is the one reported."""
    env = os.environ if env is None else env
    steps = list(steps)
    entries, roots = _read_manifest(env)
    managed = {str(r) for r in roots}
    for r in [_ws_home(env), *(s.path for s in steps if s.root)]:
        r = Path(os.path.abspath(r))
        if _too_broad(r, env):
            raise InstallError(f"{r} is too broad for ws to manage; pick a folder of its own")
        managed.add(str(r))

    result = Result()
    virtual: set[Path] = set()
    failed = True
    try:
        for step in steps:
            action = classify(step, entries, virtual)
            getattr(result, action).append(step.path)
            if action in ("unchanged", "edited"):
                if not dry_run and progress:
                    progress(step, action)
                continue
            parents = _missing_parents(step.path, virtual)
            virtual.update(parents)
            if step.kind == "dir":
                virtual.add(step.path)
            if dry_run:
                continue
            for d in parents:
                d.mkdir()
                entries[str(d)] = _dir_entry(d)
            if step.kind == "dir":
                step.path.mkdir()
                entries[str(step.path)] = _dir_entry(step.path)
            else:
                _record_file(step, action, entries, result)
            if progress:
                progress(step, action)
        failed = False
    finally:
        if not dry_run and entries:
            try:
                _save_manifest(env, entries, managed)
            except OSError:
                if not failed:
                    raise
    return result


def _record_file(step: Step, action: str, entries: dict[str, dict], result: Result) -> None:
    key = str(step.path)
    prior = entries.get(key)
    ours = action == "created" or bool(prior and prior.get("created_by_ws") is True)
    if action == "changed" and ours and prior and prior.get("sha256") != sha256(step.path.read_bytes()):
        ours = False  # the person edited a file ws made: ws keeps their values, but will not delete it later
        result.notes.append(f"{step.path} had your edits: ws kept your values and rewrote it")
    data = (step.content or "").encode("utf-8")
    _write_atomic(step.path, data)
    entries[key] = {"path": key, "kind": "file", "sha256": sha256(data), "created_by_ws": ours}


# ---- uninstall -------------------------------------------------------------------------------------------------------


def current_roots(env: Mapping[str, str] | None = None) -> list[Path]:
    """The folders uninstall may act in, worked out now from the profile and never from the manifest."""
    from ws import skills

    env = os.environ if env is None else env
    found = [_ws_home(env)]
    try:
        rls = _sub(profile.load(profile_path(env)) or {}, "roles")
    except (profile.ProfileError, OSError):
        rls = {}
    for name in ("notes", "memory"):
        p = _sub(rls, name).get("path")
        if isinstance(p, str) and p:
            found.append(expand(p, env))
    found += [Path(os.path.abspath(r)) for r in skills.roots(env)]
    out: list[Path] = []
    for r in found:
        if r not in out and not _too_broad(r, env):
            out.append(r)
    return out


def _may_touch(path: Path, kind: str, roots: list[Path]) -> bool:
    if not path.is_absolute():
        return False
    if kind == "dir" and not path.is_symlink() and any(path == r or path in r.parents for r in roots):
        return True
    for r in roots:
        if r in path.parents:
            for p in path.parents:
                if p == r:
                    return True
                if p.is_symlink():
                    return False
    return False


def _owned(path: Path) -> bool:
    getuid = getattr(os, "getuid", None)  # not on native Windows
    return getuid is None or os.stat(path).st_uid == getuid()


def _tidy_work_root(root: Path, removal: Removal, gone: set[Path], dry_run: bool) -> None:
    """Tidy the folder headless jobs without a `cwd` get as their working folder. ws makes it without a manifest entry,
    so only empty run folders go; whatever a job wrote is the person's."""
    if not (root.exists() or root.is_symlink()):
        return
    try:
        if root.is_symlink() or not root.is_dir() or not _owned(root):
            removal.refused.append(root)
            return
        work = root / "work"
        if work.is_dir() and not work.is_symlink():
            for child in sorted(work.iterdir()):
                if child.is_symlink() or not child.is_dir() or any(child.iterdir()):
                    removal.not_empty.append(child)
                else:
                    _rmdir(child, removal, gone, dry_run)
        for folder in (work, root):
            if folder.is_symlink() or not folder.is_dir():
                continue
            kids = list(folder.iterdir())
            if all(k in gone for k in kids):
                _rmdir(folder, removal, gone, dry_run)
            elif not any(n == k or k in n.parents for k in kids for n in removal.not_empty):
                removal.not_empty.append(folder)  # something else of the person's is in it
    except OSError as e:
        removal.problems.append(f"could not tidy {root}: {e.strerror}")


def _rmdir(path: Path, removal: Removal, gone: set[Path], dry_run: bool) -> None:
    if not dry_run:
        try:
            path.rmdir()
        except OSError as e:
            removal.problems.append(f"could not remove {path}: {e.strerror}")
            return
    removal.removed.append(path)
    gone.add(path)


def _work_root(env: Mapping[str, str]) -> Path:
    state = env.get("XDG_STATE_HOME", "")
    base = Path(state) if os.path.isabs(state) else Path(_home(env)) / ".local" / "state"
    return Path(os.path.abspath(base)) / "ws"


def uninstall(
    env: Mapping[str, str] | None = None,
    keep_data: bool = True,
    dry_run: bool = False,
    deactivate: Callable[[list[Path], Mapping[str, str]], list[str]] | None = None,
) -> Removal:
    """Remove what the manifest says ws created, inside the folders ws manages and nowhere else."""
    if not keep_data:
        raise InstallError("deleting your notes and memory is not built: ws only removes what it made")
    env = os.environ if env is None else env
    entries, _ = _read_manifest(env)
    roots = current_roots(env)
    removal = Removal()

    mine: list[tuple[Path, dict]] = []
    for key, entry in entries.items():
        path = Path(os.path.normpath(key))
        if entry.get("kind") in ("file", "dir") and _may_touch(path, entry["kind"], roots):
            mine.append((path, entry))
        else:
            removal.refused.append(Path(key))
    files = sorted((p, e) for p, e in mine if e["kind"] == "file")
    dirs = [(p, e) for p, e in mine if e["kind"] == "dir" and e.get("created_by_ws") is True]

    ours = [p for p, e in files if e.get("created_by_ws") is True]
    if ours and not dry_run:
        if deactivate is None:
            deactivate = roles.deactivate_units
        removal.problems += deactivate(ours, env)

    gone: set[Path] = set()
    for path, entry in files:
        if entry.get("created_by_ws") is not True:
            removal.not_ours.append(path)
        elif path.is_symlink() or not path.is_file():
            removal.missing.append(path)
        elif sha256(path.read_bytes()) != entry.get("sha256"):
            removal.edited.append(path)
        else:
            if not dry_run:
                try:
                    path.unlink()
                except OSError as e:
                    removal.problems.append(f"could not remove {path}: {e.strerror}")
                    continue
            removal.removed.append(path)
            gone.add(path)

    _tidy_work_root(_work_root(env), removal, gone, dry_run)

    manifest = manifest_path(env)
    if manifest.is_file():
        if not dry_run:
            try:
                manifest.unlink()
            except OSError as e:
                removal.problems.append(f"could not remove {manifest}: {e.strerror}")
        gone.add(manifest)

    for path, _entry in sorted(dirs, key=lambda pe: (-len(pe[0].parts), str(pe[0]))):
        if path.is_symlink() or not path.is_dir():
            removal.missing.append(path)
            continue
        try:
            kids = list(path.iterdir())
        except OSError:
            kids = None
        if kids is not None and all(k in gone for k in kids):
            if not dry_run:
                try:
                    path.rmdir()
                except OSError:
                    removal.not_empty.append(path)
                    continue
            removal.removed.append(path)
            gone.add(path)
        else:
            removal.not_empty.append(path)
    return removal


# ---- the commands ----------------------------------------------------------------------------------------------------

_YES = {"y", "yes", "true", "1", "on"}
_NO = {"n", "no", "false", "0", "off"}


def _yes_no(text: str) -> bool:
    t = text.strip().lower()
    if t in _YES:
        return True
    if t in _NO:
        return False
    raise InstallError(f"answer yes or no, not '{text}'")


def _answers_from_args(args: argparse.Namespace, env: Mapping[str, str]) -> Answers:
    a = answers_from_profile(env)
    if args.stays_on is not None:
        a = replace(a, stays_on=_yes_no(args.stays_on))
    if args.server is not None:
        a = replace(a, server="" if args.server.strip().lower() == "none" else args.server.strip())
    if args.notes is not None and args.notes.strip():
        a = replace(a, notes=args.notes)
    if args.module:
        a = replace(a, modules=a.modules | frozenset(args.module))
    if args.runner:
        a = replace(a, runner=args.runner)
    if args.model:
        a = replace(a, model=args.model)
    if args.cap is not None:
        a = replace(a, cap=args.cap)
    return a


def cmd_install(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        answers = _answers_from_args(args, env)
        steps = plan(answers, env)
        preview = apply(steps, env, dry_run=True)
        result = preview
        if not args.dry_run:
            if preview.changes and not args.yes and not args.json and sys.stdin.isatty():
                print(f"ws: this will make {preview.summary()}")
                try:
                    reply = input("Go ahead? [Y/n]: ")
                except EOFError:
                    reply = "n"
                if reply.strip().lower() in ("n", "no"):
                    print("ws: nothing changed")
                    return 1
            result = apply(steps, env)
            if "skills" in answers.modules:
                from ws import skills

                result.notes += skills.notices(answers.runner, env)
    except (InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e

    act = {}
    for names, label in (
        (result.created, "create"),
        (result.changed, "change"),
        (result.unchanged, "keep"),
        (result.edited, "left"),
    ):
        act.update({p: label for p in names})
    rows = [(s, act.get(s.path, "keep")) for s in steps]
    if args.json:
        output.result(_install_data(rows, result, args.dry_run))
        return 0
    if args.dry_run:
        for s, a in rows:
            if a != "keep":
                print(f"would {a} {s.path} ({s.what or s.kind})")
    for p in result.edited:
        print(f"you changed it, so ws left it: {p}")
    for note in result.notes:
        print(f"ws: {note}")
    print(f"ws: {'dry run: ' if args.dry_run else ''}{result.summary()}")
    if result.changes and not args.dry_run:
        print("first: `ws doctor` lists what is still to set up")
    return 0


def _install_data(rows: list[tuple[Step, str]], result: Result, dry_run: bool) -> dict:
    status = {
        "create": "deferred" if dry_run else "created",
        "change": "deferred" if dry_run else "updated",
        "keep": "skipped",
        "left": "left",
    }
    by_status: dict[str, list[str]] = {k: [] for k in ("created", "updated", "skipped", "deferred", "left")}
    for s, a in rows:
        by_status[status[a]].append(str(s.path))
    return {
        **by_status,
        "artifacts": [{"path": str(s.path), "status": status[a], "kind": s.kind, "what": s.what} for s, a in rows],
        "counts": {
            "created": len(result.created),
            "changed": len(result.changed),
            "unchanged": len(result.unchanged),
            "edited": len(result.edited),
        },
        "dry_run": dry_run,
        "steps": [{"path": str(s.path), "kind": s.kind, "what": s.what, "action": a} for s, a in rows],
        "notes": list(result.notes),
        "first": "ws doctor",
    }


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not (args.dry_run or args.yes):
        if not sys.stdin.isatty():
            raise WsError("usage", "uninstall removes files", "pass --yes (or --dry-run to see what it would do)", 2)
        try:
            reply = input("Remove what ws installed? [y/N]: ")
        except EOFError:
            reply = ""
        if reply.strip().lower() != "y":
            print("ws: nothing changed")
            return 1
    try:
        r = uninstall(dry_run=args.dry_run)
    except (InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    left = [*r.edited, *r.not_empty, *r.not_ours, *r.refused]
    if args.json:
        output.result(
            {
                "dry_run": args.dry_run,
                "removed": [str(p) for p in r.removed],
                "left": [str(p) for p in left],
                "problems": r.problems,
            }
        )
        return 0
    for label, paths in (
        ("you changed it, so ws left it", r.edited),
        ("has your files in it, so ws left it", r.not_empty),
        ("ws did not create it, so ws left it", r.not_ours),
        ("outside the folders ws manages, so ws left it", r.refused),
    ):
        for p in paths:
            print(f"{label}: {p}")
    for problem in r.problems:
        print(f"ws: {problem}", file=sys.stderr)
    print(f"ws: {'would remove' if args.dry_run else 'removed'} {len(r.removed)}; left {len(left)}")
    return 0


def register(sub) -> None:
    i = sub.add_parser("install", help="set up ws on this machine (safe to run again: a second run changes nothing)")
    i.add_argument("--stays-on", metavar="yes|no", help="does this machine stay on all the time?")
    i.add_argument("--server", metavar="ADDRESS|none", help="a server to connect to")
    i.add_argument("--notes", metavar="PATH", help="your notes folder")
    i.add_argument("--module", action="append", metavar="NAME", help="switch a module on; repeat for more")
    i.add_argument("--runner", choices=sorted(adapters.all_adapters()), help="the runner to write deny rules for")
    i.add_argument("--model", metavar="ID", help="the default model")
    i.add_argument("--cap", type=float, metavar="USD", help="a monthly spend cap in US$")
    i.add_argument("-y", "--yes", action="store_true", help="do not ask before writing")
    i.add_argument("--dry-run", action="store_true", help="show what would change and write nothing")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="remove what ws installed (only what it made; your data stays)")
    u.add_argument("--dry-run", action="store_true", help="show what would be removed and remove nothing")
    u.add_argument("-y", "--yes", action="store_true", help="do not ask first")
    u.set_defaults(func=cmd_uninstall, raw=lambda a: not (a.dry_run or a.yes))
