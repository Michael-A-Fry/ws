"""skill-gate: a write to certain files waits for a skill to have been run first. Each rule is a
`[[guards.skill-gate]]` table with a `skill` and the `paths` (globs) it guards; for example, `taste-gate` before any
`.css` file or file under `templates/` is written.

Rules also come from each skill's own front matter: a `gates` list of path globs in its `SKILL.md`, under the folders in
`skill_dirs` (default `~/.claude/skills` for Claude Code). A policy rule for the same skill replaces that skill's front
matter rules. A skill folder is read through a link but never when the link leads outside HOME, and a `SKILL.md` that
can't be read gives one stderr line and no rules.

A Skill tool call for the skill, or a prompt that starts with `/skill` (or `/plugin:skill`), records the skill in the
session's own record at `$WS_HOME/sessions/ID.skill-gate.json`. A write to a path a rule guards, before that skill is
recorded, is denied with a reason that names the skill; the person can override it once with `ws approve`. Once the
skill is recorded, writes to those paths go through for the rest of the session. A skill name matches with or without a
`plugin:` prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ws import frontmatter
from ws.guards import Finding, text_list
from ws.guards._paths import matches, relative
from ws.policy import PolicyError

NAME = "skill-gate"
EVENTS = ("pre", "prompt")
KINDS = ("write", "skill")

STATE = "skill-gate"
DEFAULT_DIRS = {"claude-code": ("~/.claude/skills",)}  # a runner's user skills folder; other runners read none
MAX_SKILLS = 200  # folders read in one skill folder
MAX_SKILL_FILE = 16384  # bytes of a SKILL.md read: the front matter sits at the top
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_SLASH = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_.:-]*)(?:\s|$)")


@dataclass(frozen=True)
class Rule:
    skill: str
    paths: tuple[str, ...]


class Rules(tuple):
    """The policy's rules (a tuple of Rule) and `skill_dirs`: the folders whose skills carry their own `gates`, or
    None when the policy leaves it out and the runner's user skills folder is used."""

    skill_dirs: tuple[str, ...] | None = None


def config(table, where: str) -> Rules:
    if table == {}:
        return Rules()  # the policy has no rules for this guard
    dirs = None
    if isinstance(table, dict) and set(table) <= {"skill_dirs", "rules"}:
        dirs = _dirs(table["skill_dirs"], where) if "skill_dirs" in table else None
        table, where = table.get("rules", []), f"{where}.rules"
    if not isinstance(table, list):
        raise PolicyError(
            f"{where} is a list of tables: write [[guards.skill-gate]], one for each skill "
            "(beside skill_dirs, write [guards.skill-gate] and [[guards.skill-gate.rules]])"
        )
    rules = []
    for i, t in enumerate(table, 1):
        at = f"{where}[{i}]"
        if not isinstance(t, dict):
            raise PolicyError(f"{at} must be a table")
        extra = sorted(set(t) - {"skill", "paths"})
        if extra:
            raise PolicyError(f"{at}: unknown key {extra[0]!r} (known: paths, skill)")
        skill = t.get("skill")
        if not isinstance(skill, str) or not _SKILL.fullmatch(skill):
            raise PolicyError(f"{at}: skill must be a skill name such as taste-gate")
        paths = text_list(t, "paths", at)
        if not paths:
            raise PolicyError(f"{at}: paths must name at least one glob")
        rules.append(Rule(skill, paths))
    out = Rules(rules)
    out.skill_dirs = dirs
    return out


def _dirs(value, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(x, str) and x.strip() for x in value):
        raise PolicyError(f"{where}: skill_dirs must be a list of folders such as ~/.claude/skills")
    for d in value:
        if not (d.startswith(("/", "~/")) or d == "~") or "\n" in d or "\0" in d:
            raise PolicyError(f"{where}: skill_dirs: {d!r} must be an absolute path or start with ~/")
    return tuple(value)


def _same(run: str, skill: str) -> bool:
    return run == skill or run.endswith(":" + skill)


def _record(ctx, name: str) -> None:
    def add(state: dict) -> None:
        names = state.get("skills", [])
        if name not in names:
            state["skills"] = [*names, name]

    ctx.state(STATE, add)


# --- the rules in the skills' own front matter ---------------------------------------------------------------------


def _home() -> Path | None:
    try:
        return Path.home().resolve()
    except (RuntimeError, OSError):
        return None


def _inside(path: Path, home: Path) -> Path | None:
    """`path` with its links followed, when that stays inside HOME; else None."""
    try:
        real = path.resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    return real if real.is_relative_to(home) else None


def _skill_dirs(cfg: Rules, ctx) -> tuple[str, ...]:
    dirs = getattr(cfg, "skill_dirs", None)
    return DEFAULT_DIRS.get(ctx.runner, ()) if dirs is None else dirs


def _front_rules(cfg: Rules, ctx, skipped: list[str]) -> list[Rule]:
    """The rules the skills under `skill_dirs` carry in their front matter. Each unreadable SKILL.md adds a line to
    `skipped`."""
    home = _home()
    out: list[Rule] = []
    if home is None:
        return out
    for d in _skill_dirs(cfg, ctx):
        base = _inside(Path(d).expanduser(), home)
        if base is None or not base.is_dir():
            continue
        try:
            folders = sorted(base.iterdir())[:MAX_SKILLS]
        except OSError:
            continue
        for folder in folders:
            file = _inside(folder / "SKILL.md", home)
            if file is None or not file.is_file():
                if folder.is_symlink() or (folder / "SKILL.md").is_symlink():
                    skipped.append(f"{folder.name}/SKILL.md leads outside HOME or nowhere")
                continue
            rule = _read_gates(file, folder.name, skipped)
            if rule is not None:
                out.append(rule)
    return out


def _read_gates(file: Path, folder: str, skipped: list[str]) -> Rule | None:
    try:
        with file.open("rb") as f:
            text = f.read(MAX_SKILL_FILE).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        skipped.append(f"{folder}/SKILL.md can't be read")
        return None
    doc = frontmatter.parse(text)
    if doc.raw is None or ("gates" not in doc.meta and not any(u.lstrip().startswith("gates") for u in doc.unparsed)):
        return None  # no front matter, or it names no gates
    gates = doc.meta.get("gates")
    if not isinstance(gates, list) or not gates or not all(isinstance(g, str) and g.strip() for g in gates):
        skipped.append(f"{folder}/SKILL.md: gates must be a list of path globs")
        return None
    name = doc.meta.get("name")
    skill = name if isinstance(name, str) and _SKILL.fullmatch(name) else folder
    if not _SKILL.fullmatch(skill):
        skipped.append(f"{folder}/SKILL.md: the skill name {skill!r} isn't a plain name")
        return None
    return Rule(skill, tuple(gates))


def _rules(cfg: Rules, ctx, skipped: list[str]) -> list[Rule]:
    """The policy's rules, then the front matter rules of every skill the policy has no rule for."""
    policy_rules = list(cfg)
    front = _front_rules(cfg, ctx, skipped)
    owned = [p.skill for p in policy_rules]
    return policy_rules + [r for r in front if not any(_same(r.skill, o) or _same(o, r.skill) for o in owned)]


def _can_gate(cfg: Rules, ctx) -> bool:
    """True when there is something to watch for: a policy rule, or a skill folder that exists."""
    if cfg:
        return True
    home = _home()
    return home is not None and any(
        (base := _inside(Path(d).expanduser(), home)) is not None and base.is_dir() for d in _skill_dirs(cfg, ctx)
    )


def _once(ctx, lines: list[str]) -> list[str]:
    """The lines not yet shown in this session, marked as shown."""
    fresh: list[str] = []

    def mark(state: dict) -> None:
        seen = [s for s in state.get("skipped", []) if isinstance(s, str)]
        fresh.extend(x for x in lines if x not in seen)
        if fresh:
            state["skipped"] = [*seen, *fresh]

    try:
        ctx.state(STATE, mark)
    except (OSError, ValueError):  # advice only: with no record to keep, say it again rather than not at all
        return lines
    return fresh


def judge(call, cfg: Rules, ctx) -> Finding | None:
    if not _can_gate(cfg, ctx):
        return None
    if ctx.event == "prompt":
        prompt = ctx.payload.get("prompt")
        m = _SLASH.match(prompt.lstrip()) if isinstance(prompt, str) else None
        if m:
            _record(ctx, m.group(1))
        return None
    if call.kind == "skill":
        name = call.data.get("name") or call.value
        if isinstance(name, str) and name:
            _record(ctx, name)
        return None
    rel = relative(call.value, ctx.cwd)
    skipped: list[str] = []
    needed = [r for r in _rules(cfg, ctx, skipped) if matches(r.paths, rel)]
    run = ctx.state(STATE).get("skills", []) if needed else []
    for rule in needed:
        if not any(_same(r, rule.skill) for r in run if isinstance(r, str)):
            return Finding(
                "deny",
                f"{rel} is written after the {rule.skill} skill has run: run the {rule.skill} skill first",
                approvable=True,
            )
    fresh = _once(ctx, skipped) if skipped else []
    if fresh:
        return Finding("note", "skill-gate skipped " + "; ".join(fresh))
    return None
