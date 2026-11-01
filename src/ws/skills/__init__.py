"""The skills ws ships (`align`, `checkpoint`, `taste-gate`, `dataviz`, `resume`, `ship`) and how they get onto a machine.

Each is an Agent Skill: a folder with a `SKILL.md` that starts with `name` and `description` front matter, the open
format both Claude Code and OpenCode read. The text lives in one Python module per skill (`ws secret wall` copies only
`.py` files, so package data is a string); the install writes it out as `<runner skills folder>/<name>/SKILL.md`.

The install goes through the same `install.plan/apply` steps as everything else, so the manifest records each file and
`ws uninstall` removes it. A skill the person already has under that name is never touched: ws reports it and moves on.
That includes one a runner reads from another runner's folder (OpenCode reads `~/.claude/skills` and `~/.agents/skills`).

    ws skills [list] [--json]      each shipped skill, per runner: installed by ws, adopted, yours (kept), removed by you,
                                   in a linked folder, or not installed
    ws skills add NAME             put a skill back that you removed (or add one that is not installed yet)
    ws skills adopt NAME [--runner R]
                                   swap your own skill of that name for ws's: yours moves to
                                   `$WS_HOME/backups/skills/<runner>/<NAME>-<UTC stamp>` and ws installs its own
    ws skills restore NAME [--runner R]
                                   take ws's copy out and move yours back where it was

A skill ws installed and you then deleted stays deleted: ws takes that as your choice, says so on `ws install`, and
only `ws skills add NAME` brings it back. Each adoption is recorded in `$WS_HOME/backups/skills/adopted.json`, which
`list` reads to say where your copy is kept. A runner skills folder that is itself a link to somewhere else is read and
listed; ws writes nothing through the link.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import json
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ws import adapters, install, output, profile
from ws import globals as ws_globals
from ws.install import Step
from ws.output import WsError
from ws.skills import align, checkpoint, dataviz, resume, ship, taste_gate

SKILLS: dict[str, str] = {m.NAME: m.TEXT for m in (align, checkpoint, taste_gate, dataviz, resume, ship)}
FILE = "SKILL.md"
ADOPTED = "adopted.json"
# Folders other than its own that a runner also reads skills from, relative to $HOME (OpenCode's documented list).
ALSO_READS = {"opencode": ("claude-code", "~/.agents/skills")}


@dataclass(frozen=True)
class Item:
    name: str
    runner: str
    path: Path  # where ws would put it: <skills folder>/<name>
    # "install" (new or ws's own) | "yours" (kept) | "shared" (read elsewhere) | "removed" (by the person) |
    # "linked" (the runner's skills folder is a link to somewhere else: ws installs nothing there)
    action: str
    note: str = ""
    backup: str = ""  # where the person's own copy is kept, when ws adopted the name


def skills_dir(runner: str, env) -> Path:
    """Where a runner looks for the person's own skills."""
    return ws_globals.user_dir(adapters.get(runner).GLOBALS, env) / "skills"


def _also_reads(runner: str, env) -> list[Path]:
    out = []
    for where in ALSO_READS.get(runner, ()):
        out.append(skills_dir(where, env) if where in adapters.all_adapters() else install.expand(where, env))
    return out


def installed_runners(env) -> list[str]:
    """The runners that have a config folder on this machine."""
    found = []
    for name, mod in adapters.all_adapters().items():
        layout = getattr(mod, "GLOBALS", None)
        if layout and ws_globals.user_dir(layout, env).is_dir():
            found.append(name)
    return found


def roots(env) -> list[Path]:
    """Every folder ws could have put a shipped skill in: the places `ws uninstall` may remove from. Worked out from
    the shipped names, never from the manifest."""
    out = []
    for name, mod in adapters.all_adapters().items():
        if getattr(mod, "GLOBALS", None):
            out += [skills_dir(name, env) / s for s in SKILLS]
    return out


def _ours(path: Path, entries: dict[str, dict]) -> bool:
    e = entries.get(str(path / FILE))
    return bool(e and e.get("created_by_ws") is True)


def backups_dir(env) -> Path:
    return profile.ws_home(dict(env)) / "backups" / "skills"


def _read_adopted(env) -> dict[str, dict]:
    """The adoption record, by `<runner>/<name>`. A file that does not read stops the command that needed it."""
    path = backups_dir(env) / ADOPTED
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))["adopted"]
        if not isinstance(raw, dict) or not all(
            isinstance(v, dict) and isinstance(v.get("backup"), str) for v in raw.values()
        ):
            raise TypeError("adopted is not a table of backups")
    except (OSError, ValueError, KeyError, TypeError) as e:
        raise WsError(
            "bad_config", f"{path} does not read as the adoption record ({type(e).__name__})", f"fix or remove {path}"
        ) from e
    return raw


def _write_adopted(env, adopted: dict[str, dict]) -> None:
    path = backups_dir(env) / ADOPTED
    path.parent.mkdir(parents=True, exist_ok=True)
    install._write_atomic(path, (json.dumps({"adopted": adopted}, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def items(runners: list[str], env, entries: dict[str, dict] | None = None) -> list[Item]:
    """What to do with each shipped skill for each runner. `entries` is the install manifest's entries by path."""
    entries = install._read_manifest(env)[0] if entries is None else entries
    adopted = _read_adopted(env)
    out = []
    for runner in runners:
        own = skills_dir(runner, env)
        elsewhere = _also_reads(runner, env)
        if own.is_symlink():
            note = f"your skills folder is a link to {os.path.realpath(own)}: ws installs nothing there"
            out += [Item(name, runner, own / name, "linked", note) for name in SKILLS]
            continue
        for name in SKILLS:
            target = own / name
            kept = adopted.get(f"{runner}/{name}", {}).get("backup", "")
            kept = kept if kept and os.path.lexists(kept) else ""
            if _ours(target, entries) and (target / FILE).is_file():
                note = f"adopted (yours kept at {kept})" if kept else "installed by ws"
                out.append(Item(name, runner, target, "install", note, kept))
            elif _ours(target, entries):  # ws made it, and it has gone since: that was the person's doing
                note = f"you removed it: `ws skills add {name}` brings it back"
                out.append(Item(name, runner, target, "removed", note, kept))
            elif target.exists() or target.is_symlink():
                out.append(Item(name, runner, target, "yours", f"you already have {target}: ws left it"))
            elif other := next((d / name for d in elsewhere if (d / name).exists()), None):
                out.append(Item(name, runner, target, "shared", f"{runner} already reads yours at {other}"))
            else:
                note = "not installed yet" + (f" (yours is kept at {kept})" if kept else "")
                out.append(Item(name, runner, target, "install", note, kept))
    return out


def steps(runner: str, env, add: str | None = None, only: str | None = None) -> list[Step]:
    """The install steps for one runner: a folder and a SKILL.md per skill that is new or already ws's own. A skill
    the person removed is left out, unless it is the one named in `add`. `only` limits it to that one skill."""
    out: list[Step] = []
    for it in items([runner], env):
        if only and it.name != only:
            continue
        if it.action == "install" or (it.action == "removed" and it.name == add):
            out.append(Step(it.path, "dir", None, f"the {it.name} skill for {runner}", root=True))
            out.append(Step(it.path / FILE, "file", SKILLS[it.name], f"the {it.name} skill for {runner}"))
    return out


def runners_for(runner: str, env) -> list[str]:
    """The profile's runner, else every runner found on this machine."""
    return [runner] if runner in adapters.all_adapters() else installed_runners(env)


def notices(runner: str, env) -> list[str]:
    """One line per skill the person removed, for `ws install` to print (ws leaves it out each time)."""
    return [
        f"{it.name} ({it.runner}): {it.note}" for it in items(runners_for(runner, env), env) if it.action == "removed"
    ]


# ---- the command -----------------------------------------------------------------------------------------------------


def cmd_list(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        runner = install.answers_from_profile(env).runner
        rows = items(runners_for(runner, env), env)
    except install.InstallError as e:
        raise WsError("bad_config", str(e), "run `ws doctor`") from e
    if args.json:
        output.result([{**vars(r), "path": str(r.path)} for r in rows])
        return 0
    if not rows:
        print("ws: no runner found to put skills in; set one with `ws install --runner claude-code|opencode`")
        return 0
    for r in rows:
        state = {
            "install": r.note,
            "yours": "yours, kept",
            "shared": "yours, read from elsewhere",
            "removed": "removed by you",
            "linked": "skills folder is a link",
        }[r.action]
        print(f"{r.name:<12} {r.runner:<12} {state}")
        if r.action != "install":
            print(f"{'':12} {'':12} {r.note}")
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    env = os.environ
    try:
        runner = install.answers_from_profile(env).runner
        runners = runners_for(runner, env)
        if not runners:
            raise WsError(
                "not_found",
                "no runner found to put skills in",
                "set one with `ws install --runner claude-code|opencode`",
            )
        for r in runners:
            (it,) = (i for i in items([r], env) if i.name == args.name)
            if it.action in ("yours", "shared", "linked"):
                print(f"ws: {it.note}")
                continue
            result = install.apply(steps(r, env, add=args.name))
            print(f"ws: {args.name} for {r}: {result.summary()}")
    except (install.InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    return 0


# ---- adopting a skill of yours that has a shipped name ------------------------------------------------------------------


def _item(runner: str, name: str, env) -> Item:
    (it,) = (i for i in items([runner], env) if i.name == name)
    return it


def _backup_path(runner: str, name: str, env) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    base = backups_dir(env) / runner / f"{name}-{stamp}"
    path, n = base, 1
    while os.path.lexists(path):
        n += 1
        path = base.with_name(f"{base.name}-{n}")
    return path


def _move(src: Path, dst: Path) -> None:
    """Rename `src` to `dst` (a link is renamed, its target untouched). Another filesystem is a typed error."""
    made = [p for p in (*dst.parents,) if not p.exists()]
    try:
        for d in reversed(made):
            d.mkdir()
        os.rename(src, dst)
    except OSError as e:
        for d in made:  # nothing changed: take back the folders made on the way
            with contextlib.suppress(OSError):
                d.rmdir()
        if e.errno == errno.EXDEV:
            raise WsError(
                "failed",
                f"{src} and {dst} are on different filesystems, so ws cannot move it in one step",
                "put WS_HOME on the same filesystem as the skills folder, or move the folder aside yourself and run "
                "`ws install`",
            ) from e
        raise WsError("failed", f"could not move {src} to {dst}: {e.strerror}") from e


def adopt(runner: str, name: str, env) -> Path:
    """Swap the person's own `name` skill in this runner's folder for ws's. Returns where theirs is kept."""
    it = _item(runner, name, env)
    if it.action == "linked":
        raise WsError("refused", it.note, "move your skills out of the linked folder, or install the skill by hand")
    if it.action == "shared":
        raise WsError(
            "not_found",
            f"{runner} reads your {name} from another folder: {it.note}",
            "run it again with `--runner` set to the runner whose folder holds it",
        )
    if it.action == "removed":
        raise WsError("refused", f"{name} for {runner}: {it.note}", f"`ws skills add {name}` puts ws's back")
    if it.action == "install":
        if os.path.lexists(it.path):
            raise WsError("not_found", f"{name} for {runner} is already ws's own: {it.note}")
        raise WsError("not_found", f"you have no {name} in {it.path.parent}", f"`ws skills add {name}` installs ws's")
    backup = _backup_path(runner, name, env)
    _move(it.path, backup)
    try:
        result = install.apply(steps(runner, env, only=name))
        _write_adopted(
            env,
            {
                **_read_adopted(env),
                f"{runner}/{name}": {"backup": str(backup), "path": str(it.path), "adopted": _now()},
            },
        )
    except (install.InstallError, OSError, WsError) as e:
        with contextlib.suppress(OSError):
            os.rename(backup, it.path)  # put yours back exactly as it was
        if isinstance(e, WsError):
            raise
        raise WsError("failed", str(e)) from e
    print(f"ws: {name} for {runner}: {result.summary()}; yours is kept at {backup}")
    return backup


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def restore(runner: str, name: str, env) -> Path:
    """Take ws's copy of `name` out of this runner's folder and move the person's own back. Returns the folder."""
    adopted = _read_adopted(env)
    rec = adopted.get(f"{runner}/{name}")
    if rec is None:
        raise WsError("not_found", f"no adopted {name} recorded for {runner}", "`ws skills list` shows what is adopted")
    backup, target = Path(rec["backup"]), skills_dir(runner, env) / name
    if not os.path.lexists(backup):
        raise WsError("not_found", f"your {name} is no longer at {backup}", "nothing was changed")
    if skills_dir(runner, env).is_symlink():
        raise WsError("refused", _item(runner, name, env).note, "nothing was changed")
    if os.path.lexists(target):
        entries = install._read_manifest(env)[0]
        skill = target / FILE
        mine = entries.get(str(skill))
        clean = (
            _ours(target, entries)
            and not target.is_symlink()
            and skill.is_file()
            and install.sha256(skill.read_bytes()) == mine.get("sha256")
            and [p.name for p in target.iterdir()] == [FILE]
        )
        if not clean:
            raise WsError(
                "refused",
                f"{target} is not ws's untouched copy, so ws left it",
                f"move it aside yourself, then run `ws skills restore {name}` again",
            )
        skill.unlink()
        target.rmdir()
        install.forget([skill, target], env)
    _move(backup, target)
    del adopted[f"{runner}/{name}"]
    _write_adopted(env, adopted)
    print(f"ws: {name} for {runner}: yours is back at {target}")
    return target


def _runners_for_name(args: argparse.Namespace, env) -> list[str]:
    if args.runner:
        return [args.runner]
    runners = runners_for(install.answers_from_profile(env).runner, env)
    if not runners:
        raise WsError(
            "not_found", "no runner found to put skills in", "set one with `ws install --runner claude-code|opencode`"
        )
    return runners


def _each_runner(args: argparse.Namespace, act) -> int:
    env = os.environ
    try:
        runners = _runners_for_name(args, env)
        failed: list[WsError] = []
        done = 0
        for r in runners:
            try:
                act(r, args.name, env)
                done += 1
            except WsError as e:
                if len(runners) == 1:
                    raise
                failed.append(e)
                print(f"ws: {r}: {e.message}")
        if not done and failed:
            raise failed[0]
    except (install.InstallError, OSError) as e:
        raise WsError("failed", str(e)) from e
    return 0


def cmd_adopt(args: argparse.Namespace) -> int:
    return _each_runner(args, adopt)


def cmd_restore(args: argparse.Namespace) -> int:
    return _each_runner(args, restore)


def _runner_names() -> list[str]:
    return [n for n, m in adapters.all_adapters().items() if getattr(m, "GLOBALS", None)]


def register(sub) -> None:
    s = sub.add_parser("skills", help="the skills ws ships, and where they are installed")
    ss = s.add_subparsers(dest="skills_command")
    ls = ss.add_parser("list", help="each skill, per runner")
    ls.set_defaults(func=cmd_list)
    add = ss.add_parser("add", help="put a skill back (one you removed), or add one that is not installed yet")
    add.add_argument("name", choices=list(SKILLS))
    add.set_defaults(func=cmd_add)
    for verb, func, text in (
        ("adopt", cmd_adopt, "swap your own skill of this name for ws's; yours is kept in $WS_HOME/backups/skills"),
        ("restore", cmd_restore, "take ws's copy out and move the skill you adopted from back"),
    ):
        p = ss.add_parser(verb, help=text)
        p.add_argument("name", choices=list(SKILLS))
        p.add_argument("--runner", choices=_runner_names(), help="the runner (default: every runner found)")
        p.set_defaults(func=func)
    s.set_defaults(func=cmd_list, json=False)
