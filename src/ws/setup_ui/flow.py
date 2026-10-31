"""What setup knows and does, apart from how it looks: real facts about this machine, the person's choices, the plan
and the install. The inline form and the web page both call this; neither writes a file itself. Everything that
changes the machine goes through `ws.install.plan` and `apply`, so setup, `ws install` and `ws plugin add` agree.
"""

from __future__ import annotations

import os
import shutil
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws import doctor, install, modules, notesdetect, osinfo, runrecord, skills
from ws import run as ws_run
from ws.install import Answers, InstallError, Step
from ws.profile import dumps, profile_path

RUNNER_LABELS = {"claude-code": "Claude Code", "opencode": "OpenCode"}
OS_LABELS = {"macos": "macOS", "linux": "Linux", "wsl": "WSL2"}
CORE = (
    ("memory", "Memory", "Remembers what you tell it, in plain files you own."),
    ("notes", "Notes capture", "Files new notes in your notes folder."),
    ("skills", "Skills", "Adds align, checkpoint, taste-gate and dataviz to your runner."),
    ("rules", "Rules and gate", "Keeps your rules and checks what an AI may do."),
)
PLUGIN_TEXT = {
    "mail-drafts": ("Mail drafts", "Drafts replies. Never sends."),
    "calendar": ("Calendar", "Reads your day to plan."),
    "finance": ("Finance", "Reads budgets. Stays local."),
    "knowledge-graph": ("Knowledge graph", "Links notes into a map."),
}
GROUPS = (  # (words in Step.what, the name shown while it installs)
    (("policy", "profile"), "Profile and policy"),
    (("memory",), "Memory"),
    (("notes",), "Notes"),
    (("skill",), "Skills"),
    (("deny rules",), "Runner gate"),
)


@dataclass(frozen=True)
class Runner:
    name: str  # the adapter name: claude-code
    label: str  # Claude Code
    exe: str
    version: str | None


@dataclass(frozen=True)
class Plugin:
    name: str
    label: str
    line: str
    built: bool
    tag: str  # "NZ only" or ""
    available: bool
    why: str = ""  # why it is not available, or a gentle warning when it still is


@dataclass(frozen=True)
class Row:
    label: str
    value: str
    tone: str = ""  # "ok" (a tick), "warn" or "" (plain)


@dataclass
class Facts:
    env: Mapping[str, str]
    osi: osinfo.OSInfo
    region: str | None
    region_source: str | None  # flag | profile | zone | locale
    runners: list[Runner]
    base: Answers  # what the profile says, with defaults for the rest
    has_profile: bool
    set_up: bool  # the manifest is there and `ws install` with no flags would change nothing
    spent: float
    vaults: list[notesdetect.Candidate]
    plugins: list[Plugin]

    @property
    def cap(self) -> float | None:
        return self.base.cap

    @property
    def home(self) -> Path:
        return Path(self.env.get("HOME") or Path.home())


@dataclass(frozen=True)
class Choices:
    plugins: frozenset[str] = frozenset()
    notes: str = notesdetect.PLAIN
    runner: str = ""
    cap: float | None = None
    region: str | None = None  # only set when the person gave one: it goes into [machine] region


@dataclass
class Preview:
    steps: list[Step]
    result: install.Result
    rows: list[dict] = field(default_factory=list)

    @property
    def new_files(self) -> list[Path]:
        kinds = {s.path: s.kind for s in self.steps}
        return [p for p in self.result.created if kinds.get(p) == "file"]

    @property
    def new_folders(self) -> list[Path]:
        kinds = {s.path: s.kind for s in self.steps}
        return [p for p in self.result.created if kinds.get(p) == "dir"]

    @property
    def changes(self) -> int:
        return self.result.changes

    def headline(self) -> str:
        if not self.changes:
            return "0 changes"
        out = f"+{len(self.new_files)} files, {len(self.result.changed)} changed"
        return out + (f", +{len(self.new_folders)} folders" if self.new_folders else "")


def saved_in(env) -> str:
    """Where the answers live, for the line after setup (claw-code says where its settings were saved too)."""
    return tilde(profile_path(env).absolute(), env)


def tilde(path: str | Path, env) -> str:
    """A path as the person would write it: under their home it starts with ~."""
    p, home = str(path), env.get("HOME") or str(Path.home())
    return "~" + p[len(home) :] if p == home or p.startswith(home.rstrip("/") + "/") else p


# ---- what this machine says ----------------------------------------------------------------------------------------


def _runners(env, probe: bool) -> list[Runner]:
    found = []
    for name, exe in doctor.RUNNERS.items():
        path = shutil.which(exe, path=env.get("PATH"))
        if path:
            version = ws_run.exe_version(path, dict(env), timeout=3.0) if probe else None
            found.append(Runner(name, RUNNER_LABELS.get(name, name), path, version or None))
    return found


def _plugin_state(m: modules.Module, here: str | None, source: str | None) -> tuple[bool, str]:
    """(available, why). A region from the profile, `--region` or the time zone refuses; a locale only warns."""
    if not m.region or here is None or here in m.region:
        return True, ""
    want = "/".join(m.region)
    if source == "locale":
        return True, f"your locale says {here}, which is only a guess"
    return False, f"for {want} only; this machine is in {here}"


def _plugins(here: str | None, source: str | None) -> list[Plugin]:
    out = []
    for m in modules.REGISTRY.values():
        if m.default_on:
            continue
        label, line = PLUGIN_TEXT.get(m.name, (m.name, m.what))
        ok, why = _plugin_state(m, here, source)
        out.append(Plugin(m.name, label, line, m.built, "/".join(m.region) + " only" if m.region else "", ok, why))
    return out


def _spent(env) -> float:
    try:
        return float(runrecord.month_spend(runrecord.read(runrecord.runs_path(dict(env)))))
    except (OSError, ValueError, TypeError):
        return 0.0


def gather(env=None, region: str | None = None, osi: osinfo.OSInfo | None = None, probe: bool = True) -> Facts:
    """Read the machine: its OS, region, runners, profile, notes apps and whether setup is already done. Writes nothing.
    `probe` runs `<runner> --version`; a test can leave it out."""
    env = os.environ if env is None else env
    osi = osi or osinfo.detect()
    base = install.answers_from_profile(env)
    if region:
        here, source = region.upper(), "flag"
    else:
        here, source = modules.region_of(env)
    facts = Facts(
        env=env,
        osi=osi,
        region=here,
        region_source=source,
        runners=_runners(env, probe),
        base=base,
        has_profile=profile_path(env).is_file(),
        set_up=False,
        spent=_spent(env),
        vaults=notesdetect.find(Path(env.get("HOME") or Path.home())),
        plugins=_plugins(here, source),
    )
    facts.set_up = _is_set_up(facts)
    return facts


def _is_set_up(facts: Facts) -> bool:
    """Setup has been run and a plain `ws install` would now change nothing."""
    if not install.manifest_path(facts.env).is_file():
        return False
    try:
        answers = replace(facts.base, modules=facts.base.modules | modules.default_on())
        return install.apply(install.plan(answers, facts.env, facts.osi), facts.env, dry_run=True).changes == 0
    except (InstallError, OSError):
        return False


def notes_app(facts: Facts, path: str) -> str:
    """What the notes folder is: the app that owns it, a folder that is there, or one that will be made."""
    p = install.expand(path, facts.env)
    for v in facts.vaults:
        if Path(os.path.abspath(v.path)) == p:
            return v.app
    return "your folder" if p.is_dir() else "new folder"


def status_rows(facts: Facts) -> list[Row]:
    """The label and value rows of the status block, from the real machine and profile."""
    osname = OS_LABELS.get(facts.osi.kind, facts.osi.kind)
    where = facts.region or "region unknown"
    machine = f"{osname} {facts.osi.release} · {where}"
    pick = _pick_runner(facts)
    if pick:
        runner = Row("Runner", f"{pick.label} {pick.version or ''}".strip(), "ok")
    else:
        runner = Row("Runner", "none found: install Claude Code or OpenCode, then run ws setup again", "warn")
    cap = facts.cap
    if cap:
        budget = Row("Budget", f"US${facts.spent:.2f} of US${cap:g} this month")
    else:
        budget = Row("Budget", "no monthly cap yet: set one with `ws setup --cap USD`", "warn")
    notes = facts.base.notes
    app = notes_app(facts, notes)
    if app in ("Obsidian", "Logseq"):
        found = Row("Notes", f"{app} vault found at {tilde(install.expand(notes, facts.env), facts.env)}", "ok")
    else:
        found = Row("Notes", f"{tilde(install.expand(notes, facts.env), facts.env)} ({app})")
    model = facts.base.model or "the runner's default"
    return [Row("Machine", machine), runner, Row("Model", model), budget, found]


def _pick_runner(facts: Facts, name: str = "") -> Runner | None:
    """The runner setup would use: the one named, else the profile's, else the first one found."""
    for want in (name, facts.base.runner):
        for r in facts.runners:
            if want and r.name == want:
                return r
    return facts.runners[0] if facts.runners else None


# ---- choices, plan, install ------------------------------------------------------------------------------------------


def plugin_names(facts: Facts) -> list[str]:
    return [p.name for p in facts.plugins]


def choices_from(
    facts: Facts,
    plugins=(),
    notes: str | None = None,
    runner: str | None = None,
    cap: float | None = None,
    region: str | None = None,
) -> Choices:
    """The profile's answers with whatever was given on top. A plugin already on stays on."""
    chosen = frozenset(n for n in facts.base.modules if n in plugin_names(facts)) | frozenset(plugins)
    pick = _pick_runner(facts, runner or "")
    return Choices(
        plugins=chosen,
        notes=notes or tilde(facts.base.notes, facts.env),
        runner=runner or (pick.name if pick else ""),
        cap=cap if cap is not None else facts.base.cap,
        region=region.upper() if region else None,
    )


def check_choices(facts: Facts, c: Choices) -> None:
    """Refuse what ws would refuse: an unknown plugin, a plugin made for another region, a runner it does not know."""
    known = {p.name: p for p in facts.plugins}
    for name in sorted(c.plugins):
        if name not in known:
            raise InstallError(f"unknown plugin {name!r}; known: {', '.join(known)}")
        if not known[name].available:
            raise InstallError(f"{name} is {known[name].why}; if that is wrong, run setup with --region {_cc(name)}")
    if c.runner and c.runner not in doctor.RUNNERS:
        raise InstallError(f"unknown runner {c.runner!r}; known: {', '.join(doctor.RUNNERS)}")
    if c.cap is not None and not (c.cap > 0 and c.cap != float("inf")):
        raise InstallError("the monthly cap must be a number above 0")


def _cc(plugin: str) -> str:
    m = modules.REGISTRY.get(plugin)
    return m.region[0] if m and m.region else "NZ"


def answers_for(facts: Facts, c: Choices) -> Answers:
    return replace(
        facts.base,
        modules=modules.default_on() | c.plugins,
        notes=c.notes,
        runner=c.runner,
        cap=c.cap,
    )


def _with_region(steps: list[Step], region: str | None, env) -> list[Step]:
    """Put `[machine] region` in the profile step, so a region the person gave is in the diff and the install."""
    if not region:
        return steps
    target = profile_path(env).absolute()
    out = []
    for s in steps:
        if s.path == target and s.content is not None:
            doc = tomllib.loads(s.content)
            machine = dict(doc.get("machine") or {})
            machine["region"] = region
            s = replace(s, content=dumps({**doc, "machine": machine}))
        out.append(s)
    return out


def preview(facts: Facts, c: Choices) -> Preview:
    """What the install would do, from the real plan and a dry run. Writes nothing."""
    check_choices(facts, c)
    steps = _with_region(install.plan(answers_for(facts, c), facts.env, facts.osi), c.region, facts.env)
    result = install.apply(steps, facts.env, dry_run=True)
    action = {}
    for name, paths in (
        ("create", result.created),
        ("change", result.changed),
        ("keep", result.unchanged),
        ("left", result.edited),
    ):
        action.update(dict.fromkeys(paths, name))
    rows = [
        {"path": tilde(s.path, facts.env), "kind": s.kind, "what": s.what, "action": action.get(s.path, "keep")}
        for s in steps
    ]
    return Preview(steps, result, rows)


def group_of(step: Step) -> str:
    what = step.what.lower()
    for words, name in GROUPS:
        if any(w in what for w in words):
            return name
    return "Jobs and backups"


def groups(steps: list[Step]) -> list[tuple[str, list[Step]]]:
    """The steps in the order they run, gathered under a few names for the install screen."""
    out: dict[str, list[Step]] = {}
    for s in steps:
        out.setdefault(group_of(s), []).append(s)
    return list(out.items())


class Progress:
    """Where an install is up to, in the screen's terms: the steps gathered into named groups, each finished when its
    last step is done. Feed it the `(step, action)` calls of `install.apply`."""

    def __init__(self, steps: list[Step]) -> None:
        self.groups = groups(steps)
        self.names = [name for name, _ in self.groups]
        self._index = {s.path: i for i, (_, grp) in enumerate(self.groups) for s in grp}
        self._left = [len(grp) for _, grp in self.groups]
        self._made: dict[tuple[str, str], int] = {}
        self._changed: dict[str, int] = {}

    def step(self, step: Step, action: str) -> str | None:
        """Count one finished step. Returns the group's name if that was its last one."""
        name = self.names[self._index[step.path]]
        kind = "file" if step.kind == "file" else "folder"
        if action == "created":
            self._made[(name, kind)] = self._made.get((name, kind), 0) + 1
        elif action == "changed":
            self._changed[name] = self._changed.get(name, 0) + 1
        self._left[self.names.index(name)] -= 1
        return name if self._left[self.names.index(name)] == 0 else None

    def detail(self, name: str) -> str:
        """`+2 files, +1 folder, 1 changed`, or `kept as it was`."""
        parts = []
        for kind in ("file", "folder"):
            n = self._made.get((name, kind), 0)
            if n:
                parts.append(f"+{n} {kind}{'s' if n != 1 else ''}")
        if self._changed.get(name):
            parts.append(f"{self._changed[name]} changed")
        return ", ".join(parts) or "kept as it was"


@dataclass
class Outcome:
    result: install.Result
    notes: list[str]


def run_install(
    facts: Facts, c: Choices, steps: list[Step], on_step: Callable[[Step, str], None] | None = None
) -> Outcome:
    """Apply the plan with `install.apply`. `on_step(step, action)` is called as each step finishes."""
    result = install.apply(steps, facts.env, progress=on_step)
    notes = list(result.notes)
    if "skills" in answers_for(facts, c).modules:
        notes += skills.notices(c.runner, facts.env)
    return Outcome(result, notes)


def next_commands(facts: Facts, c: Choices) -> list[str]:
    cmds = ["ws doctor"]
    left = [p.name for p in facts.plugins if p.available and p.name not in c.plugins]
    cmds.append(f"ws plugin add {left[0]}" if left else "ws plugin list")
    cmds.append('ws note "first note"')
    return cmds


def change_commands(facts: Facts, c: Choices) -> list[tuple[str, str]]:
    """What to run to change something once setup is done: (command, what it does)."""
    left = [p.name for p in facts.plugins if p.available and p.name not in c.plugins]
    return [
        (f"ws plugin add {left[0] if left else 'NAME'}", "switch a plugin on"),
        ("ws plugin remove NAME", "switch one off"),
        ("ws setup --notes PATH", "point ws at a different notes folder"),
        ("ws doctor", "check everything"),
        ("ws uninstall", "remove what ws installed (your notes and memory stay)"),
    ]
