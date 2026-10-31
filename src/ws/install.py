"""`ws install` and `ws uninstall`: one setup engine that the terminal command and the setup screens both drive.

    plan(answers, env)    every file and folder ws would create or change, each with its final content. Pure: it
                          reads the machine but writes nothing.
    apply(steps)          writes only what differs, and records what ws created (path + sha256) in
                          `$WS_HOME/install-manifest.json`. A second run reports 0 changes.
    uninstall(env)        removes what the manifest says ws created and nothing else. A file you changed since is
                          left and listed; folders are removed only when empty, so notes and memory you wrote stay.

The manifest is the whole contract: ws never deletes a path it did not create.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws import __version__, adapters, notesdetect, osinfo, policy
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


@dataclass
class Result:
    created: list[Path] = field(default_factory=list)
    changed: list[Path] = field(default_factory=list)
    unchanged: list[Path] = field(default_factory=list)

    @property
    def changes(self) -> int:
        return len(self.created) + len(self.changed)

    def summary(self) -> str:
        if not self.changes:
            return f"0 changes ({len(self.unchanged)} unchanged)"
        return f"{len(self.created)} created, {len(self.changed)} changed, {len(self.unchanged)} unchanged"


@dataclass
class Removal:
    removed: list[Path] = field(default_factory=list)
    edited: list[Path] = field(default_factory=list)  # a file whose hash no longer matches
    not_empty: list[Path] = field(default_factory=list)  # a folder with something of yours in it
    not_ours: list[Path] = field(default_factory=list)  # in the manifest but ws did not create it
    missing: list[Path] = field(default_factory=list)  # already gone


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
        cap=float(cap) if isinstance(cap, (int, float)) and not isinstance(cap, bool) else None,
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
        if answers.cap <= 0:
            raise InstallError("the monthly cap must be above 0")
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
    steps.append(Step(policy_file, "file", text, "your policy (kept as it is if you already have one)"))

    steps.append(
        Step(profile_path(env).absolute(), "file", dumps(compose_profile(prof, answers, env, osi)), "your profile")
    )
    for m in modules.REGISTRY.values():
        if m.name in answers.modules:
            steps.extend(m.steps(answers, env))
    if answers.runner:
        try:
            adapter = adapters.get(answers.runner)
        except KeyError as e:
            raise InstallError(str(e.args[0])) from e
        out = home / "gate" / answers.runner
        files = adapter.generate(pol, ["ws"], out, policy_file.resolve())
        steps.extend(Step(out / name, "file", body, f"{answers.runner} deny rules") for name, body in files.items())
    return steps


# ---- apply ---------------------------------------------------------------------------------------------------------


def _read_manifest(env) -> dict[str, dict]:
    path = manifest_path(env)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        entries = data["entries"]
        return {e["path"]: e for e in entries if isinstance(e, dict) and isinstance(e.get("path"), str)}
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise InstallError(f"{path} does not read as a ws manifest ({type(e).__name__}); not touching anything") from e


def _manifest_text(entries: dict[str, dict]) -> str:
    doc = {"schema": SCHEMA, "ws_version": __version__, "entries": [entries[k] for k in sorted(entries)]}
    return json.dumps(doc, indent=2) + "\n"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".ws-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        raise


def _missing_ancestors(path: Path) -> list[Path]:
    """Folders above path that do not exist yet, outermost first."""
    out: list[Path] = []
    for parent in path.parents:
        if parent.exists():
            break
        out.append(parent)
    return out[::-1]


def classify(step: Step) -> str:
    p = step.path
    if step.kind == "dir":
        if p.is_dir():
            return "unchanged"
        if p.exists() or p.is_symlink():
            raise InstallError(f"{p} is in the way: ws needs a folder there")
        return "created"
    if p.is_dir():
        raise InstallError(f"{p} is a folder: ws needs a file there")
    if not p.exists():
        return "created"
    return "unchanged" if p.read_bytes() == (step.content or "").encode("utf-8") else "changed"


def apply(steps: list[Step], env=None, dry_run: bool = False) -> Result:
    """Write what differs. Records every path ws creates in the manifest; a file ws changes but did not create is
    recorded with created_by_ws false so uninstall knows to leave it."""
    env = os.environ if env is None else env
    result = Result()
    entries = _read_manifest(env)
    for step in steps:
        action = classify(step)
        getattr(result, action).append(step.path)
        if dry_run or action == "unchanged":
            continue
        for parent in _missing_ancestors(step.path):
            parent.mkdir()
            entries[str(parent)] = {"path": str(parent), "kind": "dir", "sha256": None, "created_by_ws": True}
        key = str(step.path)
        if step.kind == "dir":
            step.path.mkdir()
            entries[key] = {"path": key, "kind": "dir", "sha256": None, "created_by_ws": True}
            continue
        data = (step.content or "").encode("utf-8")
        _write_atomic(step.path, data)
        ours = action == "created" or entries.get(key, {}).get("created_by_ws") is True
        entries[key] = {"path": key, "kind": "file", "sha256": sha256(data), "created_by_ws": ours}
    if not dry_run and entries:
        path, text = manifest_path(env), _manifest_text(entries)
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            _write_atomic(path, text.encode("utf-8"))
    return result


# ---- uninstall -------------------------------------------------------------------------------------------------------


def uninstall(env=None, keep_data: bool = True, dry_run: bool = False) -> Removal:
    """Remove what the manifest says ws created. Files first, then folders deepest first, and only empty ones."""
    if not keep_data:
        raise InstallError("deleting your notes and memory is not built: ws only removes what it made")
    env = os.environ if env is None else env
    entries = _read_manifest(env)
    out = Removal()
    mine = [e for e in entries.values() if Path(e["path"]).is_absolute()]
    for e in sorted((e for e in mine if e.get("kind") == "file"), key=lambda e: e["path"]):
        p = Path(e["path"])
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
    mpath = manifest_path(env)
    gone = {*out.removed, mpath}
    if not dry_run and mpath.is_file():
        mpath.unlink()
    ours = {Path(e["path"]) for e in mine if e.get("kind") == "dir" and e.get("created_by_ws") is True}
    home = Path(os.path.abspath(ws_home(env)))
    for d in sorted({*ours, home} if entries else set(), key=lambda p: (-len(p.parts), str(p))):
        if d.is_symlink() or not d.is_dir():
            if d in ours:
                out.missing.append(d)
        elif _removable(d, gone, dry_run):
            out.removed.append(d)
            gone.add(d)
        elif d in ours:
            out.not_empty.append(d)
    return out


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
    for name, paths in (("create", result.created), ("change", result.changed), ("keep", result.unchanged)):
        action.update(dict.fromkeys(paths, name))
    return [{"path": str(s.path), "kind": s.kind, "what": s.what, "action": action[s.path]} for s in steps]


def cmd_install(args: argparse.Namespace) -> int:
    try:
        steps = plan(answers_from_args(args, os.environ))
        result = apply(steps, dry_run=True)
        if not args.dry_run:
            if result.changes and not args.yes and not args.json and sys.stdin.isatty():
                print(f"ws: this will make {result.summary()}")
                if input("Go ahead? [Y/n]: ").strip().lower() in ("n", "no"):
                    print("ws: nothing changed")
                    return 1
            result = apply(steps)
    except (InstallError, OSError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    if args.json:
        counts = {k: len(getattr(result, k)) for k in ("created", "changed", "unchanged")}
        print(json.dumps({**counts, "dry_run": args.dry_run, "steps": _rows(steps, result)}, indent=2))
        return 0
    if args.dry_run:
        for row in _rows(steps, result):
            if row["action"] != "keep":
                print(f"would {row['action']} {row['path']} ({row['what'] or row['kind']})")
    print(f"ws: {'dry run: ' if args.dry_run else ''}{result.summary()}")
    if result.changes and not args.dry_run:
        print("next: `ws doctor` lists what is still to set up")
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    if not args.dry_run and not args.yes and not sys.stdin.isatty():
        print("ws: uninstall removes files: pass --yes (or --dry-run to see what it would do)", file=sys.stderr)
        return 2
    try:
        if not args.dry_run and not args.yes and input("Remove what ws installed? [y/N]: ").strip().lower() != "y":
            print("ws: nothing changed")
            return 1
        r = uninstall(os.environ, dry_run=args.dry_run)
    except (InstallError, OSError) as e:
        print(f"ws: {e}", file=sys.stderr)
        return 1
    for p in r.edited:
        print(f"you changed it, so ws left it: {p}")
    for p in r.not_empty:
        print(f"has your files in it, so ws left it: {p}")
    for p in r.not_ours:
        print(f"ws did not create it, so ws left it: {p}")
    left = len(r.edited) + len(r.not_empty) + len(r.not_ours)
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
    i.add_argument("--json", action="store_true", help="machine-readable result")
    i.set_defaults(func=cmd_install)

    u = sub.add_parser("uninstall", help="remove what ws installed; your notes and memory are kept")
    u.add_argument("--dry-run", action="store_true", help="show what would be removed")
    u.add_argument("-y", "--yes", action="store_true", help="no prompt")
    u.set_defaults(func=cmd_uninstall)
