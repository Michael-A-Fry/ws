"""`ws install` and `ws uninstall`: one setup engine that the terminal command and the setup screens both drive.

    plan(answers, env)    every file and folder ws would create or change, each with its final content. Pure: it
                          reads the machine but writes nothing.
    apply(steps)          writes only what differs, and records what ws created (path + sha256) in
                          `$WS_HOME/install-manifest.json`. A second run reports 0 changes.
    uninstall(env)        removes what the manifest says ws created and nothing else. A file you changed since is
                          left and listed; folders are removed only when empty, so notes and memory you wrote stay.

The manifest is the whole contract: ws never deletes a path it did not create. It also records the folders ws manages
(`roots`: $WS_HOME, your notes and memory folders); uninstall refuses any entry outside them, so an edited manifest
cannot point it at your other files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import stat
import sys
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws import __version__, adapters, notesdetect, osinfo, output, policy
from ws.output import WsError
from ws.profile import ProfileError, dumps, load, profile_path, ws_home

MANIFEST = "install-manifest.json"
SCHEMA = 1
DEFAULT_MEMORY = "~/ws-memory"


class InstallError(Exception):
    """A problem the person can fix; the message is safe to print."""


def _default_modules() -> frozenset[str]:
    from ws import modules

    return modules.default_on()


@dataclass(frozen=True)
class Answers:
    stays_on: bool = False
    server: str = ""  # "" = none
    notes: str = notesdetect.PLAIN
    modules: frozenset[str] = field(default_factory=_default_modules)
    runner: str = ""  # an adapter name, or "" to leave the runner for later
    model: str = ""
    cap: float | None = None  # monthly spend cap in US$
    memory: str = ""  # "" = keep the profile's memory folder, else ~/ws-memory


@dataclass(frozen=True)
class Step:
    path: Path
    kind: str  # "file" | "dir"
    content: str | None = None  # the file's final text; None for a dir
    what: str = ""  # a few words for the person: "your profile"
    root: bool = False  # a folder ws manages (your notes, your memory): uninstall may act inside it
    toml: bool = False  # compared as TOML, so a re-formatted or commented file with the same data is unchanged


@dataclass
class Result:
    created: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)
    edited: list[Path] = field(default_factory=list)  # a file ws made that you edited: left as it is
    notes: list[str] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.created) + len(self.changed)

    def summary(self) -> str:
        tail = f", {len(self.edited)} left because you edited them" if self.edited else ""
        if not self.changes:
            return f"0 changes ({len(self.unchanged)} unchanged{tail})"
        return f"{len(self.created)} created, {len(self.changed)} changed, {len(self.unchanged)} unchanged{tail}"


@dataclass
class Removal:
    removed: list[Path] = field(default_factory=list)
    edited: list[Path] = field(default_factory=list)  # a file whose hash no longer matches
    not_empty: list[Path] = field(default_factory=list)  # a folder with something of yours in it
    not_ours: list[Path] = field(default_factory=list)  # in the manifest but ws did not create it
    missing: list[Path] = field(default_factory=list)  # already gone
    refused: list[Path] = field(default_factory=list)  # outside the folders ws manages, or behind a symlink
    problems: list[str] = field(default_factory=list)  # timers that could not be switched off first


# ---- paths -------------------------------------------------------------------------------------------------------


def expand(p: str, env) -> Path:
    """A path with `~` taken from env's HOME, made absolute."""
    if p == "~" or p.startswith("~/"):
        p = (env.get("HOME") or str(Path.home())) + p[1:]
    return Path(os.path.abspath(p))


def store_path(p: str, env) -> str:
    """How a folder is written to the profile: `~/...` stays as typed, anything else becomes absolute."""
    return p if p == "~" or p.startswith("~/") else str(expand(p, env))


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def manifest_path(env=None) -> Path:
    return Path(os.path.abspath(ws_home(env))) / MANIFEST


# ---- the profile ---------------------------------------------------------------------------------------------------


def _table(d: dict, key: str, where: str) -> dict:
    t = d.get(key, {})
    if not isinstance(t, dict):
        raise InstallError(f"{where}: [{key}] must be a table")
    return dict(t)


def _load_profile(env) -> dict:
    try:
        return load(profile_path(env)) or {}
    except ProfileError as e:
        raise InstallError(str(e)) from e


def answers_from_profile(env=None, home: Path | None = None) -> Answers:
    """What the profile already says, with defaults for the rest: so a re-run with no flags changes nothing."""
    env = os.environ if env is None else env
    prof = _load_profile(env)
    from ws import modules

    machine = prof.get("machine") if isinstance(prof.get("machine"), dict) else {}
    roles = prof.get("roles") if isinstance(prof.get("roles"), dict) else {}

    def role(name: str, key: str):
        cfg = roles.get(name)
        return cfg.get(key) if isinstance(cfg, dict) else None

    mods = prof.get("modules")
    enabled = (
        frozenset(k for k, v in mods.items() if v is True and k in modules.REGISTRY)
        if isinstance(mods, dict)
        else modules.default_on()
    )
    notes = machine.get("notes") or role("notes", "path")
    if not isinstance(notes, str) or not notes:
        notes = notesdetect.suggest(Path(env.get("HOME") or Path.home()))[0]
    runner = role("runner", "adapter")
    cap = role("budget", "monthly_cap_usd")
    model = role("model", "default")
    return Answers(
        stays_on=machine.get("stays_on") is True,
        server=machine["server"] if isinstance(machine.get("server"), str) else "",
        notes=notes,
        modules=enabled,
        runner=runner if isinstance(runner, str) and runner in adapters.all_adapters() else "",
        model=model if isinstance(model, str) else "",
        cap=float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) and math.isfinite(cap) else None,
        memory=role("memory", "path") if isinstance(role("memory", "path"), str) else "",
    )


def compose_profile(prof: dict, answers: Answers, env, osi: osinfo.OSInfo) -> dict:
    """The profile after these answers: what was there is kept, only the answered keys change."""
    from ws import modules

    where = "profile"
    prof = dict(prof)
    machine = _table(prof, "machine", where)
    machine.update(os=osi.kind, stays_on=answers.stays_on, server=answers.server, notes=store_path(answers.notes, env))
    prof["machine"] = machine
    roles = _table(prof, "roles", where)
    old_modules = _table(prof, "modules", where)

    def set_role(name: str, **keys) -> None:
        cfg = roles.get(name, {})
        if not isinstance(cfg, dict):
            raise InstallError(f"profile: [roles.{name}] must be a table")
        roles[name] = {**cfg, **keys}

    if answers.runner:
        set_role("runner", adapter=answers.runner)
    if answers.model:
        set_role("model", default=answers.model)
    if answers.cap is not None:
        if not math.isfinite(answers.cap) or answers.cap <= 0:
            raise InstallError("the monthly cap must be a number above 0")
        set_role("budget", monthly_cap_usd=answers.cap if answers.cap != int(answers.cap) else int(answers.cap))
    unknown = sorted(answers.modules - set(modules.REGISTRY))
    if unknown:
        raise InstallError(f"unknown module {unknown[0]!r}; known: {', '.join(modules.REGISTRY)}")
    for m in modules.REGISTRY.values():
        if m.name in answers.modules:
            for role_name, keys in m.roles(answers, env).items():
                set_role(role_name, **keys)
        elif old_modules.get(m.name, m.default_on) is True:  # switched off now: drop the tables it added
            for role_name in m.role_names:
                roles.pop(role_name, None)
    prof["roles"] = roles
    prof["modules"] = {**old_modules, **{n: n in answers.modules for n in modules.REGISTRY}}
    return prof


# ---- plan ----------------------------------------------------------------------------------------------------------


def plan(answers: Answers, env=None, osi: osinfo.OSInfo | None = None) -> list[Step]:
    """Every file and folder ws would create or change for these answers. Writes nothing."""
    from ws import modules

    env = os.environ if env is None else env
    osi = osi or osinfo.detect()
    if not osi.supported:
        raise InstallError("native Windows is not supported; install WSL2 and run ws inside it")
    home = Path(os.path.abspath(ws_home(env)))
    prof = _load_profile(env)
    if not answers.memory:
        existing = (prof.get("roles") or {}).get("memory") if isinstance(prof.get("roles"), dict) else None
        keep = existing.get("path") if isinstance(existing, dict) else None
        answers = replace(answers, memory=keep if isinstance(keep, str) and keep else DEFAULT_MEMORY)
    steps: list[Step] = []

    policy_file = home / "policy.toml"
    try:
        text = policy_file.read_text(encoding="utf-8") if policy_file.is_file() else policy.default_toml()
        pol = policy.loads(text)
    except (policy.PolicyError, OSError, UnicodeDecodeError) as e:
        raise InstallError(f"{policy_file}: {e}") from e
    steps.append(Step(policy_file, "file", text, "your policy (kept as it is if you already have one)", toml=True))

    composed = compose_profile(prof, answers, env, osi)
    steps.append(Step(profile_path(env).absolute(), "file", dumps(composed), "your profile", toml=True))
    for m in modules.REGISTRY.values():
        if m.name in answers.modules:
            steps.extend(m.steps(answers, env))
    steps.extend(_role_steps(composed, answers, env, {(s.path, s.kind) for s in steps}))
    if answers.runner:
        try:
            adapter = adapters.get(answers.runner)
        except KeyError as e:
            raise InstallError(str(e.args[0])) from e
        out = home / "gate" / answers.runner
        files = adapter.generate(pol, ["ws"], out, policy_file.resolve())
        steps.extend(Step(out / name, "file", body, f"{answers.runner} deny rules") for name, body in files.items())
    return steps


ROLE_STEPS = ("memory", "backup")  # the roles whose adapter brings files of its own (the backup job file)


def _role_steps(composed: dict, answers: Answers, env, have: set[tuple[Path, str]]) -> list[Step]:
    """The steps of the adapters the profile names for ROLE_STEPS, minus any a module already gave."""
    from ws import jobs, roles

    roles_cfg = composed.get("roles") if isinstance(composed.get("roles"), dict) else {}
    out: list[Step] = []
    for role in ROLE_STEPS:
        cfg = roles_cfg.get(role)
        name = cfg.get("adapter", "markdown-git" if role == "memory" else None) if isinstance(cfg, dict) else None
        if not isinstance(name, str) or name not in roles.adapters(role):
            continue
        try:
            found = roles.get(role, name).steps(cfg, answers, env)
        except (jobs.JobError, ValueError, OSError) as e:
            raise InstallError(f"[roles.{role}]: {e}") from e
        out += [s for s in found if (s.path, s.kind) not in have]
    return out


# ---- apply ---------------------------------------------------------------------------------------------------------


def _read_manifest(env) -> tuple[dict[str, dict], set[str]]:
    """(entries by path, roots). A manifest that does not read stops everything: ws touches nothing."""
    path = manifest_path(env)
    if not path.is_file():
        return {}, set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = {e["path"]: e for e in data["entries"] if isinstance(e, dict) and isinstance(e.get("path"), str)}
        roots = {r for r in data.get("roots", []) if isinstance(r, str)}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
        raise InstallError(f"{path} does not read as a ws manifest ({type(e).__name__}); not touching anything") from e
    return entries, roots


def _manifest_text(entries: dict[str, dict], roots: set[str]) -> str:
    doc = {
        "schema": SCHEMA,
        "ws_version": __version__,
        "roots": sorted(roots),
        "entries": [entries[k] for k in sorted(entries)],
    }
    return json.dumps(doc, indent=2) + "\n"


def _write_atomic(path: Path, data: bytes) -> None:
    """Write beside the target under a unique name, flush to disk, keep the target's mode, then replace it."""
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        umask = os.umask(0)
        os.umask(umask)
        mode = 0o666 & ~umask
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".ws-tmp")
    tmp = Path(name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:  # including Ctrl-C: never leave a temp file behind
        tmp.unlink(missing_ok=True)
        raise


def _missing_ancestors(path: Path, virtual: set[Path]) -> list[Path]:
    """Folders above path that do not exist yet (or will not until an earlier step makes them), outermost first."""
    out: list[Path] = []
    for parent in path.parents:
        if parent.exists() or parent in virtual:
            break
        out.append(parent)
    return out[::-1]


def _same_toml(a: bytes, b: bytes) -> bool:
    try:
        return tomllib.loads(a.decode("utf-8")) == tomllib.loads(b.decode("utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return False


def _edited_by_person(entry: dict | None, on_disk: bytes) -> bool:
    """A file ws made, whose content is no longer what ws wrote."""
    return bool(entry) and entry.get("created_by_ws") is True and entry.get("sha256") != sha256(on_disk)


def classify(step: Step, entries: dict[str, dict] | None = None, virtual: set[Path] | None = None) -> str:
    """created | changed | unchanged | edited. `virtual` holds folders an earlier step will make, so a dry run and a
    real run agree."""
    entries, virtual = entries or {}, virtual or set()
    p = step.path
    if step.kind == "dir":
        if p.is_dir() or p in virtual:
            return "unchanged"
        if p.exists() or p.is_symlink():
            raise InstallError(f"{p} is in the way: ws needs a folder there")
        return "created"
    if p.is_dir():
        raise InstallError(f"{p} is a folder: ws needs a file there")
    if not p.exists():
        return "created"
    disk, want = p.read_bytes(), (step.content or "").encode("utf-8")
    if disk == want or (step.toml and _same_toml(disk, want)):
        return "unchanged"
    if not step.toml and _edited_by_person(entries.get(str(p)), disk):
        return "edited"  # a generated file you changed: left alone
    return "changed"


def _root_problem(root: Path, env) -> str | None:
    """Why a folder cannot be a root: one that covers your whole home (or the disk) is not 'ws's folder'."""
    home = Path(os.path.abspath(env.get("HOME") or Path.home()))
    if root == Path(root.anchor) or root == home or root in home.parents:
        return f"{root} is too broad for ws to manage; pick a folder of its own"
    return None


def _save_manifest(env, entries: dict[str, dict], roots: set[str]) -> None:
    path = manifest_path(env)
    for parent in _missing_ancestors(path, set()):  # steps outside ws's home: its home may not exist yet
        parent.mkdir()
        entries[str(parent)] = {"path": str(parent), "kind": "dir", "sha256": None, "created_by_ws": True}
    text = _manifest_text(entries, roots)
    if not path.is_file() or path.read_text(encoding="utf-8") != text:
        _write_atomic(path, text.encode("utf-8"))


def apply(
    steps: list[Step], env=None, dry_run: bool = False, progress: Callable[[Step, str], None] | None = None
) -> Result:
    """Write what differs. Records every path ws creates in the manifest, even when a later step fails. A file ws
    changes but did not create is recorded with created_by_ws false so uninstall knows to leave it; a generated file
    you edited is left and reported. `progress(step, action)` is called after each step is done (action is created,
    changed, unchanged or edited), so a setup screen can show it; it changes nothing about what is written."""
    env = os.environ if env is None else env
    result = Result()
    entries, roots = _read_manifest(env)
    new_roots = {Path(os.path.abspath(ws_home(env)))} | {s.path for s in steps if s.root}
    for r in sorted(new_roots):
        if problem := _root_problem(r, env):
            raise InstallError(problem)
    roots |= {str(r) for r in new_roots}
    virtual: set[Path] = set()
    done = False
    try:
        for step in steps:
            action = classify(step, entries, virtual)
            getattr(result, action).append(step.path)
            if action in ("unchanged", "edited"):
                if progress and not dry_run:
                    progress(step, action)
                continue
            missing = _missing_ancestors(step.path, virtual)
            virtual.update(missing)
            if step.kind == "dir":
                virtual.add(step.path)
            if dry_run:
                continue
            for parent in missing:
                parent.mkdir()
                entries[str(parent)] = {"path": str(parent), "kind": "dir", "sha256": None, "created_by_ws": True}
            key = str(step.path)
            if step.kind == "dir":
                step.path.mkdir()
                entries[key] = {"path": key, "kind": "dir", "sha256": None, "created_by_ws": True}
                if progress:
                    progress(step, action)
                continue
            data = (step.content or "").encode("utf-8")
            entry = entries.get(key)
            ours = action == "created" or bool(entry and entry.get("created_by_ws") is True)
            if action == "changed" and ours and _edited_by_person(entry, step.path.read_bytes()):
                ours = (
                    False  # a TOML file you edited, rewritten to carry a new answer: yours now, so uninstall leaves it
                )
                result.notes.append(f"{step.path} had your edits: ws kept your values and rewrote it")
            _write_atomic(step.path, data)
            entries[key] = {"path": key, "kind": "file", "sha256": sha256(data), "created_by_ws": ours}
            if progress:
                progress(step, action)
        done = True
    finally:
        if not dry_run and entries:
            try:
                _save_manifest(env, entries, roots)
            except OSError:
                if done:  # a step already failed: that is the error worth reporting, not this one
                    raise
    return result


def forget(paths, env=None) -> None:
    """Drop these paths from the manifest (ws removed them itself), so uninstall does not look for them."""
    env = os.environ if env is None else env
    entries, roots = _read_manifest(env)
    gone = {str(p) for p in paths} & entries.keys()
    if gone:
        for k in gone:
            del entries[k]
        _save_manifest(env, entries, roots)


# ---- uninstall -------------------------------------------------------------------------------------------------------


def current_roots(env) -> list[Path]:
    """The folders uninstall may act in, worked out now from $WS_HOME and the profile's notes and memory folders.
    Never from the manifest, which is the file an attacker would edit."""
    roots = [Path(os.path.abspath(ws_home(env)))]
    try:
        roles = _load_profile(env).get("roles")
    except InstallError:
        roles = None
    for name in ("notes", "memory"):
        cfg = roles.get(name) if isinstance(roles, dict) else None
        p = cfg.get("path") if isinstance(cfg, dict) else None
        if isinstance(p, str) and p:
            roots.append(expand(p, env))
    from ws import skills  # the folders of the skills ws ships, in each runner's skills folder

    roots += skills.roots(env)
    return [r for r in dict.fromkeys(roots) if not _root_problem(r, env)]


def _inside(path: Path, roots: list[Path]) -> bool:
    """Under a root with no symlink on the way down (the root itself may be a link to where you keep it)."""
    for root in roots:
        if root in path.parents:
            between = [p for p in path.parents if root in p.parents]
            return not any(p.is_symlink() for p in between)
    return False


def _may_touch(e: dict, roots: list[Path]) -> bool:
    """An entry is acted on only if its normalised path is a root, inside one, or (a folder) above one."""
    p = Path(os.path.normpath(e["path"]))
    if not p.is_absolute():
        return False
    if e.get("kind") == "dir":
        if p.is_symlink():
            return False
        if p in roots or any(p in r.parents for r in roots):
            return True
    return _inside(p, roots)


def uninstall(env=None, keep_data: bool = True, dry_run: bool = False, deactivate=None) -> Removal:
    """Remove what the manifest says ws created, inside the folders it manages. Files first, then folders deepest
    first, and only empty ones. Timer and agent files are switched off in systemd or launchd first (`deactivate(paths,
    env)` returns what went wrong; the default runs the real thing), since those links live outside $WS_HOME."""
    if not keep_data:
        raise InstallError("deleting your notes and memory is not built: ws only removes what it made")
    env = os.environ if env is None else env
    entries, _ = _read_manifest(env)  # the manifest's own `roots` are information only: they widen nothing
    out = Removal()
    roots = current_roots(env)
    mine = []
    for e in entries.values():
        if _may_touch(e, roots):
            mine.append(e)
        else:
            out.refused.append(Path(e["path"]))
    units = [
        Path(os.path.normpath(e["path"])) for e in mine if e.get("kind") == "file" and e.get("created_by_ws") is True
    ]
    if units and not dry_run:
        from ws import roles

        out.problems += (deactivate or roles.deactivate_units)(units, env)
    for e in sorted((e for e in mine if e.get("kind") == "file"), key=lambda e: e["path"]):
        p = Path(os.path.normpath(e["path"]))
        if e.get("created_by_ws") is not True:
            out.not_ours.append(p)
        elif p.is_symlink() or not p.is_file():
            out.missing.append(p)
        elif sha256(p.read_bytes()) != e.get("sha256"):
            out.edited.append(p)
        else:
            out.removed.append(p)
            if not dry_run:
                p.unlink()
    _remove_work_root(env, out, dry_run)
    mpath = manifest_path(env)
    gone = {*out.removed, mpath}
    if not dry_run and mpath.is_file():
        mpath.unlink()
    ours = {
        Path(os.path.normpath(e["path"])) for e in mine if e.get("kind") == "dir" and e.get("created_by_ws") is True
    }
    for d in sorted(ours, key=lambda p: (-len(p.parts), str(p))):
        if d.is_symlink() or not d.is_dir():
            out.missing.append(d)
        elif _removable(d, gone, dry_run):
            out.removed.append(d)
            gone.add(d)
        else:
            out.not_empty.append(d)
    return out


def _remove_work_root(env: dict, out: Removal, dry_run: bool) -> None:
    """`$XDG_STATE_HOME/ws`, where `ws run` puts the working folder of a job with no `cwd`, is made by ws without a
    manifest entry. What a job wrote there is the person's, so only the empty run folders go, then `work/` and `ws/`
    when nothing is left in them; a run folder with files in it is left and listed. A link there, or a folder that is
    not ours, is left and listed too."""
    from ws import run

    root = run.work_root(env)
    if not os.path.lexists(root):
        return
    if root.is_symlink() or not root.is_dir() or root.lstat().st_uid != os.getuid():
        out.refused.append(root)
        return
    gone: set[Path] = set()

    def drop(d: Path) -> None:
        try:
            if not dry_run:
                d.rmdir()
        except OSError as e:
            out.problems.append(f"could not remove {d}: {e.strerror or type(e).__name__}")
            return
        out.removed.append(d)
        gone.add(d)

    work = root / "work"
    if work.is_dir() and not work.is_symlink():
        for folder in sorted(work.iterdir()):
            if folder.is_symlink() or not folder.is_dir() or any(folder.iterdir()):
                out.not_empty.append(folder)  # what a job wrote: the person's
            else:
                drop(folder)
    for d in (work, root):
        if d.is_symlink() or not d.is_dir():
            continue
        if all(c in gone for c in d.iterdir()):
            drop(d)
        elif not any(c in out.not_empty for c in d.iterdir()):
            out.not_empty.append(d)  # something else of yours is in it


def _removable(d: Path, gone: set[Path], dry_run: bool) -> bool:
    """True if d holds nothing but paths already removed; when not a dry run, removes it."""
    if any(c not in gone for c in d.iterdir()):
        return False
    if dry_run:
        return True
    try:
        d.rmdir()
    except OSError:
        return False
    return True


# ---- the command -----------------------------------------------------------------------------------------------------


def _yes_no(text: str) -> bool:
    t = text.strip().lower()
    if t in ("y", "yes", "true", "1", "on"):
        return True
    if t in ("n", "no", "false", "0", "off"):
        return False
    raise InstallError(f"answer yes or no, not {text!r}")


def answers_from_args(args: argparse.Namespace, env) -> Answers:
    """The profile's answers (or defaults), then whatever was given on the command line on top."""
    base = answers_from_profile(env)
    server = base.server if args.server is None else ("" if args.server.strip().lower() == "none" else args.server)
    return replace(
        base,
        stays_on=base.stays_on if args.stays_on is None else _yes_no(args.stays_on),
        server=server.strip(),
        notes=args.notes or base.notes,
        modules=base.modules | frozenset(args.module or ()),
        runner=args.runner or base.runner,
        model=args.model or base.model,
        cap=base.cap if args.cap is None else args.cap,
    )


def _rows(steps: list[Step], result: Result) -> list[dict]:
    action = {}
    for name, paths in (
        ("create", result.created),
        ("change", result.changed),
        ("keep", result.unchanged),
        ("left", result.edited),
    ):
        action.update(dict.fromkeys(paths, name))
    return [{"path": str(s.path), "kind": s.kind, "what": s.what, "action": action[s.path]} for s in steps]


def artifacts(steps: list[Step], result: Result, dry_run: bool) -> dict:
    """Each path with one status, the words claw-code's `init` uses: created, updated, skipped (already as wanted),
    deferred (a dry run: it would be created or updated), left (you changed it, so ws left it)."""
    status = {}
    for name, paths in (
        ("deferred" if dry_run else "created", result.created),
        ("deferred" if dry_run else "updated", result.changed),
        ("skipped", result.unchanged),
        ("left", result.edited),
    ):
        status.update(dict.fromkeys(paths, name))
    rows = [{"path": str(s.path), "status": status[s.path], "kind": s.kind, "what": s.what} for s in steps]
    lists = {
        k: [r["path"] for r in rows if r["status"] == k] for k in ("created", "updated", "skipped", "deferred", "left")
    }
    return {**lists, "artifacts": rows}


def cmd_install(args: argparse.Namespace) -> int:
    try:
        answers = answers_from_args(args, os.environ)
        steps = plan(answers)
        result = apply(steps, dry_run=True)
        if not args.dry_run:
            if result.changes and not args.yes and not args.json and sys.stdin.isatty():
                print(f"ws: this will make {result.summary()}")
                if input("Go ahead? [Y/n]: ").strip().lower() in ("n", "no"):
                    print("ws: nothing changed")
                    return 1
            result = apply(steps)
        if "skills" in answers.modules:  # skills you removed stay removed; say so
            from ws import skills

            result.notes += skills.notices(answers.runner, os.environ)
    except (InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    if args.json:
        counts = {k: len(getattr(result, k)) for k in ("created", "changed", "unchanged", "edited")}
        output.result(
            {
                **artifacts(steps, result, args.dry_run),
                "counts": counts,
                "dry_run": args.dry_run,
                "steps": _rows(steps, result),
                "notes": result.notes,
                "first": "ws doctor",
            }
        )
        return 0
    if args.dry_run:
        for row in _rows(steps, result):
            if row["action"] != "keep":
                print(f"would {row['action']} {row['path']} ({row['what'] or row['kind']})")
    for path in result.edited:
        print(f"you changed it, so ws left it: {path}")
    for note in result.notes:
        print(f"ws: {note}")
    print(f"ws: {'dry run: ' if args.dry_run else ''}{result.summary()}")
    if result.changes and not args.dry_run:
        print("first: `ws doctor` lists what is still to set up")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes and not sys.stdin.isatty():
        raise WsError("usage", "uninstall removes files", "pass --yes (or --dry-run to see what it would do)", 2)
    try:
        if not args.dry_run and not args.yes and input("Remove what ws installed? [y/N]: ").strip().lower() != "y":
            print("ws: nothing changed")
            return 1
        r = uninstall(os.environ, dry_run=args.dry_run)
    except (InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    if args.json:
        output.result(
            {
                "dry_run": args.dry_run,
                "removed": [str(p) for p in r.removed],
                "left": [str(p) for p in (*r.edited, *r.not_empty, *r.not_ours, *r.refused)],
                "problems": list(r.problems),
            }
        )
        return 0
    for p in r.edited:
        print(f"you changed it, so ws left it: {p}")
    for p in r.not_empty:
        print(f"has your files in it, so ws left it: {p}")
    for p in r.not_ours:
        print(f"ws did not create it, so ws left it: {p}")
    for p in r.refused:
        print(f"outside the folders ws manages, so ws left it: {p}")
    for problem in r.problems:
        print(f"ws: {problem}", file=sys.stderr)
    left = len(r.edited) + len(r.not_empty) + len(r.not_ours) + len(r.refused)
    print(f"ws: {'would remove' if args.dry_run else 'removed'} {len(r.removed)}; left {left}")
    return 0


def register(sub) -> None:
    i = sub.add_parser("install", help="set up ws on this machine; run it again and it reports 0 changes")
    i.add_argument("--stays-on", metavar="yes|no", help="does this machine stay on all the time?")
    i.add_argument("--server", metavar="ADDRESS|none", help="a server to connect to")
    i.add_argument("--notes", metavar="PATH", help="your notes folder (default: a vault found, else ~/Notes)")
    i.add_argument("--module", action="append", metavar="NAME", help="also switch on a module; repeat for more")
    i.add_argument("--runner", choices=list(adapters.all_adapters()), help="the AI tool ws runs jobs with")
    i.add_argument("--model", metavar="ID", help="the default model")
    i.add_argument("--cap", type=float, metavar="USD", help="your own monthly spend cap")
    i.add_argument("-y", "--yes", action="store_true", help="no prompts")
    i.add_argument("--dry-run", action="store_true", help="show what would change; write nothing")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="remove what ws installed; your notes and memory are kept")
    u.add_argument("--dry-run", action="store_true", help="show what would be removed")
    u.add_argument("-y", "--yes", action="store_true", help="no prompt")
    u.set_defaults(
        func=cmd_uninstall, raw=lambda a: not (a.dry_run or a.yes)
    )  # it may ask first: the prompt must be seen
