"""What `ws setup` knows and decides, apart from how it looks.

That is the machine's facts, the person's choices and the checks on them. The terminal walk and the browser page both
stand on this module, so they cannot disagree. Nothing here writes a file except through `ws.install`: `preview` plans
and does a dry run, `run_install` applies the same steps. Anything that comes from outside ws (a folder name, a
runner's `--version` text, a profile value, a typed path) is cleaned of control characters before it is shown, so it
cannot move a cursor, set a window title or ring a bell.
"""

from __future__ import annotations

import math
import os
import re
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws import install, modules, notesdetect, osinfo, profile, runrecord
from ws.install import Answers, InstallError, Step

CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # every C0 and C1 control character
ENGINE_ERRORS = (InstallError, OSError, ValueError, UnicodeError)
MAX_PATH = 4096
MAX_CAP = 1_000_000.0
VERSION_ROOM = 40  # characters kept of what a runner prints for --version
VERSION_WAIT = 3.0  # seconds a runner has to answer --version

RUNNER_LABELS = {"claude-code": "Claude Code", "opencode": "OpenCode"}
OS_LABELS = {"macos": "macOS", "linux": "Linux", "wsl": "WSL2"}

# What is always on: (key, label, one line).
CORE = (
    ("memory", "Memory", "what ws remembers, as Markdown in git"),
    ("notes", "Notes capture", "a drop folder inside your own notes"),
    ("skills", "Skills", "align, checkpoint, taste-gate, dataviz"),
    ("rules", "Rules and gate", "what a session may and may not do"),
)

# What each opt-in plugin is called and says. Short, because the plugin row has little room.
PLUGIN_TEXT = {
    "mail-drafts": ("Mail drafts", "Mail replies, drafted"),
    "calendar": ("Calendar", "Plan around your day"),
    "finance": ("Finance", "Money, kept local"),
    "knowledge-graph": ("Knowledge graph", "Links across notes"),
}

# The install screen's grouping: the first row whose words appear in a step's description, else the last name.
GROUPS = (
    (("policy", "profile"), "Profile and policy"),
    (("memory",), "Memory"),
    (("notes",), "Notes"),
    (("skill",), "Skills"),
    (("deny rules",), "Runner gate"),
)
OTHER_GROUP = "Jobs and backups"


class ChoiceError(InstallError):
    """A choice ws cannot use. Carries the line that says what to try instead, so no caller reads the message."""

    def __init__(self, message: str, hint: str = "") -> None:
        super().__init__(message)
        self.hint = hint


# ---- text from outside ------------------------------------------------------------------------------------------------


def clean(text: object) -> str:
    """`text` as a string with every control character removed (only the characters: what was around them stays)."""
    return CONTROL.sub("", str(text))


def has_control(text: object) -> bool:
    return CONTROL.search(str(text)) is not None


def _home(env) -> str:
    return clean(env.get("HOME") or str(Path.home()))


def tilde(path: object, env) -> str:
    """`path` cleaned, with the home folder written as `~`."""
    text = clean(path)
    home = _home(env).rstrip("/")
    if home and (text == home or text.startswith(home + "/")):
        return "~" + text[len(home) :]
    return text


def saved_in(env) -> str:
    return tilde(os.path.abspath(profile.profile_path(env)), env)


# ---- the data ----------------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Runner:
    name: str
    label: str
    exe: str
    version: str | None


@dataclass(frozen=True)
class Plugin:
    name: str
    label: str
    line: str
    built: bool
    tag: str
    available: bool
    why: str = ""


@dataclass(frozen=True)
class Row:
    label: str
    value: str
    tone: str = ""  # "ok" is a tick, "warn" needs a look, "" is plain


@dataclass
class Facts:
    env: object
    osi: osinfo.OSInfo
    region: str | None = None
    region_source: str | None = None  # flag | profile | zone | locale
    runners: list[Runner] = field(default_factory=list)
    base: Answers = field(default_factory=Answers)
    has_profile: bool = False
    set_up: bool = False
    spent: float = 0.0
    vaults: list[notesdetect.Candidate] = field(default_factory=list)
    plugins: list[Plugin] = field(default_factory=list)

    @property
    def cap(self) -> float | None:
        return self.base.cap

    @property
    def home(self) -> str:
        return self.env.get("HOME") or str(Path.home())


@dataclass(frozen=True)
class Choices:
    plugins: frozenset[str] = frozenset()
    notes: str = notesdetect.PLAIN
    runner: str = ""
    cap: float | None = None
    region: str | None = None  # only when the person gave one; it goes into [machine] region


@dataclass
class Preview:
    steps: list[Step]
    result: install.Result
    rows: list[dict]

    @property
    def new_files(self) -> list[Path]:
        made = set(self.result.created)
        return [s.path for s in self.steps if s.kind == "file" and s.path in made]

    @property
    def new_folders(self) -> list[Path]:
        made = set(self.result.created)
        return [s.path for s in self.steps if s.kind == "dir" and s.path in made]

    @property
    def changes(self) -> int:
        return self.result.changes

    def headline(self) -> str:
        if not self.changes:
            return "0 changes"
        text = f"+{len(self.new_files)} files, {len(self.result.changed)} changed"
        if self.new_folders:
            text += f", +{len(self.new_folders)} folders"
        return text


@dataclass
class Outcome:
    result: install.Result
    notes: list[str]


# ---- reading the machine -----------------------------------------------------------------------------------------------


def _runner_version(exe: str, env) -> str | None:
    from ws import run  # late: it pulls in the whole job runner

    found = run.exe_version(exe, dict(env), timeout=VERSION_WAIT)
    text = clean(found or "")[:VERSION_ROOM].strip()
    return text or None


def _find_runners(env, probe: bool) -> list[Runner]:
    from ws import doctor

    found = []
    for name, exe in doctor.RUNNERS.items():
        path = shutil.which(exe, path=env.get("PATH"))
        if path:
            version = _runner_version(path, env) if probe else None
            found.append(Runner(name, RUNNER_LABELS.get(name, name), path, version))
    return found


def _spent(env) -> float:
    try:
        return float(runrecord.month_spend(runrecord.read(runrecord.runs_path(env))))
    except (OSError, ValueError, TypeError):
        return 0.0


def _plugin_list(region: str | None, source: str | None, env=None) -> list[Plugin]:
    out = []
    for m in modules.offered(env).values():
        if m.default_on:
            continue
        label, line = PLUGIN_TEXT.get(m.name, (m.name, m.what))
        tag = "/".join(m.region) + " only" if m.region else ""
        available, why = True, ""
        if m.region and region is not None and region not in m.region:
            if source == "locale":
                why = f"your locale says {region}, which is only a guess"
            else:
                available, why = False, f"for {'/'.join(m.region)} only; this machine is in {region}"
        out.append(Plugin(m.name, label, line, m.built, tag, available, why))
    return out


def _is_set_up(base: Answers, env, osi) -> bool:
    """Is everything the profile and the defaults ask for already in place? A dry run says so."""
    if not install.manifest_path(env).is_file():
        return False
    try:
        answers = replace(base, modules=base.modules | modules.default_on())
        return install.apply(install.plan(answers, env, osi), env, dry_run=True).changes == 0
    except ENGINE_ERRORS:
        return False


def gather(env=None, region: str | None = None, osi=None, probe: bool = True) -> Facts:
    """Read the machine; write nothing."""
    env = os.environ if env is None else env
    osi = osi or osinfo.detect(env=env)
    base = install.answers_from_profile(env)
    if has_control(base.notes):
        base = replace(base, notes=notesdetect.PLAIN)
    if region:
        where, source = region.upper(), "flag"
    else:
        where, source = modules.region_of(env)
    home = Path(env.get("HOME") or Path.home())
    vaults = [v for v in notesdetect.find(home) if not has_control(v.path)]
    return Facts(
        env=env,
        osi=osi,
        region=where,
        region_source=source,
        runners=_find_runners(env, probe),
        base=base,
        has_profile=profile.profile_path(env).is_file(),
        set_up=_is_set_up(base, env, osi),
        spent=_spent(env),
        vaults=vaults,
        plugins=_plugin_list(where, source, env),
    )


def notes_app(facts: Facts, path: str) -> str:
    """Which notes app owns this folder: a detected app's name, `your folder` if it exists, else `new folder`."""
    try:
        target = install.expand(path, facts.env)
        for v in facts.vaults:
            if Path(os.path.abspath(v.path)) == target:
                return v.app
        return "your folder" if target.is_dir() else "new folder"
    except (OSError, ValueError):
        return "new folder"


def picked_runner(facts: Facts, requested: str = "") -> Runner | None:
    """The runner that will be used: the one asked for, else the profile's, else the first found."""
    for name in (requested, facts.base.runner):
        if name and (hit := next((r for r in facts.runners if r.name == name), None)):
            return hit
    return facts.runners[0] if facts.runners else None


def status_rows(facts: Facts) -> list[Row]:
    osi = facts.osi
    where = clean(facts.region) if facts.region else "region unknown"
    rows = [Row("Machine", f"{clean(OS_LABELS.get(osi.kind, osi.kind))} {clean(osi.release)} · {where}".strip())]
    runner = picked_runner(facts)
    if runner:
        rows.append(Row("Runner", f"{runner.label} {clean(runner.version or '')}".strip(), "ok"))
    else:
        rows.append(Row("Runner", "none found: install Claude Code or OpenCode, then run ws setup again", "warn"))
    rows.append(Row("Model", clean(facts.base.model) or "the runner's default"))
    if facts.cap:
        rows.append(Row("Budget", f"US${facts.spent:.2f} of US${facts.cap:g} this month"))
    else:
        rows.append(Row("Budget", "no monthly limit set: add one with ws setup --cap USD", "warn"))
    where_notes = tilde(facts.base.notes, facts.env)
    app = notes_app(facts, facts.base.notes)
    if app in ("Obsidian", "Logseq"):
        rows.append(Row("Notes", f"{app} vault found at {where_notes}", "ok"))
    else:
        rows.append(Row("Notes", f"{where_notes} ({app})"))
    return rows


def plugin_names(facts: Facts) -> list[str]:
    return [p.name for p in facts.plugins]


# ---- choices and the rules on them --------------------------------------------------------------------------------------


def choices_from(facts, plugins=(), notes=None, runner=None, cap=None, region=None) -> Choices:
    on = {n for n in facts.base.modules if n in plugin_names(facts)}
    if not runner and (p := picked_runner(facts)):
        runner = p.name
    return Choices(
        plugins=frozenset(on | set(plugins)),
        notes=notes or tilde(facts.base.notes, facts.env),
        runner=runner or "",
        cap=cap if cap is not None else facts.base.cap,
        region=region.upper() if region else None,
    )


def validate_notes(path: object, env) -> None:
    """Refuse a notes folder ws cannot manage. Raises InstallError (a ChoiceError, with a hint)."""
    example = "for example: ws setup --notes ~/Notes"
    if not isinstance(path, str) or not path.strip():
        raise ChoiceError("the notes folder cannot be empty", example)
    if has_control(path):
        raise ChoiceError("the notes folder has a control character in it; type a plain path", "type a plain path")
    if len(path) > MAX_PATH:
        raise ChoiceError("the notes folder path is too long", example)
    if not (path.startswith("~/") or os.path.isabs(path)):
        raise ChoiceError(f"the notes folder must start with ~/ or be a full path, not {clean(repr(path))}", example)
    try:
        here = install.expand(path, env).resolve()
        home = Path(_home(env)).resolve()
    except (OSError, RuntimeError) as e:
        raise ChoiceError(f"the notes folder cannot be followed to the end: {clean(e)}", example) from e
    if here.parent == here or here == home or here in home.parents:
        raise ChoiceError(f"{path} is too broad for ws to manage; pick a folder of its own", example)


def parse_cap(text: str) -> float | None:
    """A typed cap as a number; empty means none. Anything else that is not a usable amount is an error."""
    said = "type a number above 0 for the cap, or leave it empty to skip"
    body = text.strip().removeprefix("US$").lstrip().removeprefix("$").strip()
    if not body:
        return None
    try:
        value = float(body)
    except ValueError:
        raise ChoiceError(said, "for example: 20") from None
    if not (math.isfinite(value) and 0 < value <= MAX_CAP):
        raise ChoiceError(said, "for example: 20")
    return value


def _usable_cap(cap: object) -> bool:
    return (
        isinstance(cap, (int, float))
        and not isinstance(cap, bool)
        and math.isfinite(cap)
        and 0 < cap <= MAX_CAP
    )  # fmt: skip


def check_choices(facts: Facts, c: Choices) -> None:
    """Raise InstallError for a choice that cannot be used, before anything is planned or written."""
    from ws import doctor

    known = {p.name: p for p in facts.plugins}
    for name in sorted(c.plugins):
        plugin = known.get(name)
        if plugin is None:
            raise ChoiceError(
                f"unknown plugin {clean(repr(name))}; known: {', '.join(known)}", "list them with: ws plugin list"
            )
        if not plugin.available:
            cc = modules.REGISTRY[name].region[0] if modules.REGISTRY[name].region else "NZ"
            raise ChoiceError(
                f"{name} is {plugin.why}; if that is wrong, run setup with --region {cc}",
                f"for example: ws setup --region {cc}",
            )
    if c.runner and c.runner not in doctor.RUNNERS:
        names = ", ".join(doctor.RUNNERS)
        raise ChoiceError(f"unknown runner {clean(repr(c.runner))}; known: {names}", f"pick one of: {names}")
    if c.cap is not None and not _usable_cap(c.cap):
        raise ChoiceError("the monthly cap must be a number above 0", "for example: ws setup --cap 20")
    validate_notes(c.notes, facts.env)
    if c.region is not None and not re.fullmatch(r"[A-Z]{2}", c.region):
        raise ChoiceError("the region must be two letters such as NZ", "for example: ws setup --region NZ")


def answers_for(facts: Facts, c: Choices) -> Answers:
    kept = frozenset(modules.unbuilt_on(facts.base.modules, facts.env))  # a hidden plugin the profile has on stays on
    return replace(
        facts.base, modules=modules.default_on() | c.plugins | kept, notes=c.notes, runner=c.runner, cap=c.cap
    )  # fmt: skip


# ---- the preview and the install ----------------------------------------------------------------------------------------


def check_steps(steps: list[Step]) -> None:
    """Refuse, before the first write, a step that could fail half way through."""
    for step in steps:
        shown = clean(repr(str(step.path)))
        if has_control(step.path):
            raise ChoiceError(f"a path with a control character in it: {shown}", "type a plain path")
        try:
            (step.content or "").encode("utf-8")
        except UnicodeEncodeError as e:
            raise ChoiceError(f"{clean(step.path)}: its content cannot be written as text") from e


def _with_region(steps: list[Step], region: str, env) -> list[Step]:
    """Put `[machine] region` into the profile step, so the diff shows it and the install writes it."""
    target = Path(os.path.abspath(profile.profile_path(env)))
    out = []
    for step in steps:
        if step.path == target and step.content is not None:
            doc = tomllib.loads(step.content)
            machine = doc.get("machine")
            doc["machine"] = {**(machine if isinstance(machine, dict) else {}), "region": region}
            step = replace(step, content=profile.dumps(doc))
        out.append(step)
    return out


def preview(facts: Facts, c: Choices) -> Preview:
    """What the install would do. Writes nothing."""
    check_choices(facts, c)
    steps = install.plan(answers_for(facts, c), facts.env, facts.osi)
    if c.region:
        steps = _with_region(steps, c.region, facts.env)
    check_steps(steps)
    result = install.apply(steps, facts.env, dry_run=True)
    action = {}
    for names, word in (
        (result.unchanged, "keep"),
        (result.edited, "left"),
        (result.changed, "change"),
        (result.created, "create"),
    ):
        action.update({p: word for p in names})
    rows = [
        {"path": tilde(s.path, facts.env), "kind": s.kind, "what": clean(s.what), "action": action.get(s.path, "keep")}
        for s in steps
    ]
    return Preview(steps, result, rows)


def group_of(step: Step) -> str:
    what = step.what.lower()
    for words, name in GROUPS:
        if any(w in what for w in words):
            return name
    return OTHER_GROUP


def groups(steps: list[Step]) -> list[tuple[str, list[Step]]]:
    """The steps by group, groups in order of first appearance, steps in plan order."""
    found: dict[str, list[Step]] = {}
    for step in steps:
        found.setdefault(group_of(step), []).append(step)
    return list(found.items())


class Progress:
    """Counts finished steps by group, so a screen can tick a group off the moment its last step lands."""

    def __init__(self, steps: list[Step]) -> None:
        self.groups = groups(steps)
        self.names = [name for name, _ in self.groups]
        self._left = {name: len(members) for name, members in self.groups}
        self._files = dict.fromkeys(self.names, 0)
        self._folders = dict.fromkeys(self.names, 0)
        self._changed = dict.fromkeys(self.names, 0)

    def step(self, step: Step, action: str) -> str | None:
        """One step finished. Returns the group's name if that was its last step."""
        name = group_of(step)
        if name not in self._left:
            return None
        if action == "created":
            (self._files if step.kind == "file" else self._folders)[name] += 1
        elif action == "changed":
            self._changed[name] += 1
        self._left[name] -= 1
        return name if self._left[name] == 0 else None

    def detail(self, name: str) -> str:
        files, folders, changed = self._files[name], self._folders[name], self._changed[name]
        parts = []
        if files:
            parts.append(f"+{files} file{'s' if files != 1 else ''}")
        if folders:
            parts.append(f"+{folders} folder{'s' if folders != 1 else ''}")
        if changed:
            parts.append(f"{changed} changed")
        return ", ".join(parts) or "kept as it was"


def run_install(facts: Facts, c: Choices, steps: list[Step], on_step=None) -> Outcome:
    """Apply the steps. `on_step(step, action)` is called after each one."""
    from ws import skills  # late: ws.skills uses ws.install, as this module does

    check_choices(facts, c)
    check_steps(steps)
    result = install.apply(steps, facts.env, progress=on_step)
    notes = [clean(n) for n in result.notes]
    notes += [clean(n) for n in skills.notices(c.runner, facts.env)]
    return Outcome(result, notes)


def _first_unchosen(facts: Facts, c: Choices) -> str | None:
    return next((p.name for p in facts.plugins if p.available and p.name not in c.plugins), None)


def next_steps(facts: Facts, c: Choices) -> list[tuple[str, str]]:
    """What to run next, as (command, what it does) pairs."""
    left = _first_unchosen(facts, c)
    return [
        ("ws doctor", "check that everything works"),
        (f"ws plugin add {left}", "switch a plugin on") if left else ("ws plugin list", "see the plugins"),
        ('ws note "first note"', "file your first note"),
    ]


def next_commands(facts: Facts, c: Choices) -> list[str]:
    return [cmd for cmd, _ in next_steps(facts, c)]


def change_commands(facts: Facts, c: Choices) -> list[tuple[str, str]]:
    return [
        (f"ws plugin add {_first_unchosen(facts, c) or 'NAME'}", "switch a plugin on"),
        ("ws plugin remove NAME", "switch one off"),
        ("ws setup --notes PATH", "point ws at another notes folder"),
        ("ws doctor", "check that everything works"),
        ("ws uninstall", "your notes and memory stay"),
    ]
