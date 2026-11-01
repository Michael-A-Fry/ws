"""skill-gate: a write to certain files waits for a skill to have been run first. Each rule is a
`[[guards.skill-gate]]` table with a `skill` and the `paths` (globs) it guards; for example, `taste-gate` before any
`.css` file or file under `templates/` is written.

A Skill tool call for the skill, or a prompt that starts with `/skill` (or `/plugin:skill`), records the skill in the
session's own record at `$WS_HOME/sessions/ID.skill-gate.json`. A write to a path a rule guards, before that skill is
recorded, is denied with a reason that names the skill; the person can override it once with `ws approve`. Once the
skill is recorded, writes to those paths go through for the rest of the session. A skill name matches with or without a
`plugin:` prefix.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ws.guards import Finding, text_list
from ws.guards._paths import matches, relative
from ws.policy import PolicyError

NAME = "skill-gate"
EVENTS = ("pre", "prompt")
KINDS = ("write", "skill")

STATE = "skill-gate"
_SKILL = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]*")
_SLASH = re.compile(r"/([A-Za-z0-9][A-Za-z0-9_.:-]*)(?:\s|$)")


@dataclass(frozen=True)
class Rule:
    skill: str
    paths: tuple[str, ...]


def config(table, where: str) -> tuple[Rule, ...]:
    if table == {}:
        return ()  # the policy has no rules for this guard
    if not isinstance(table, list):
        raise PolicyError(f"{where} is a list of tables: write [[guards.skill-gate]], one for each skill")
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
    return tuple(rules)


def _same(run: str, skill: str) -> bool:
    return run == skill or run.endswith(":" + skill)


def _record(ctx, name: str) -> None:
    def add(state: dict) -> None:
        names = state.get("skills", [])
        if name not in names:
            state["skills"] = [*names, name]

    ctx.state(STATE, add)


def judge(call, cfg: tuple[Rule, ...], ctx) -> Finding | None:
    if not cfg:
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
    needed = [r for r in cfg if matches(r.paths, rel)]
    if not needed:
        return None
    run = ctx.state(STATE).get("skills", [])
    for rule in needed:
        if not any(_same(r, rule.skill) for r in run if isinstance(r, str)):
            return Finding(
                "deny",
                f"{rel} is written after the {rule.skill} skill has run: run the {rule.skill} skill first",
                approvable=True,
            )
    return None
