"""`ws walls`: set up a walled data class as an operating-system boundary, and check that it is in place.

A walled class (a `[classes.NAME]` with an `os_user`) is built from controls the runner and the OS already have: the class's
own account, folder modes, and a settings file for the class that `ws gate launch` merges into its sessions (ADR 0027,
ws.wallsettings). ws enforces nothing new here; it writes the policy table, and reads and checks what is in place. Walled
classes are optional: they need a second account on a Linux or macOS machine.

  * `ws walls init NAME` prints a `[classes.NAME]` table for you to read. With `--write` it appends the table to your
    policy file: the whole file validated, a backup, then an atomic replace, all under the lock `ws install` takes on a
    file it writes. It writes the `prepare` dial and never `act`.
  * `ws walls edit` opens the policy file in `$VISUAL` or `$EDITOR` on a temporary copy, validates the whole file and
    refuses an invalid one, leaving the original as it was. It is your own edit of your own file, so a dial you set
    stays set. The file is replaced only if it is still the bytes the edit started from; a link is refused.
  * `ws walls check` runs the named cases in `CASES` without sudo and checks that each part of the boundary is in place:
    the settings, the account and the file modes are read, and the gate's own functions are asked about a probe call. It
    is not a live test of the runner. Exit 0 when all pass; exit 1 when the host is not set up (it prints the commands,
    and the way to take it all out again) or a case fails, with a typed error naming the next command.

  * `ws walls canary` runs every case but the on-disk owner check on a world planted under ws's probe folder (three
    walled classes with accounts that don't exist, their files as ws renders them, the generated settings), so it needs
    no set-up host; it then lists what the cases cover (`COVERS`). Exit 0 when all pass, exit 1 when a setting the wall
    writes is missing.

`CASES` is a plain module-level table of (name, check function, the setting the case depends on, whether it needs the class
file): a later task adds a row and nothing else. Creating accounts and root-owned files is your step; this module never
runs sudo, `useradd` or `chown`, and writes nothing under `/etc`.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import shlex
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, NamedTuple

from ws import osinfo, output, paths, policy, userconfig, wallsettings
from ws.output import WsError

if TYPE_CHECKING:
    from ws import session

COMMAND = "walls"

REVIEW_DAYS = 180  # `ws walls init` proposes a review this long after today
PROBE_COMMAND = "ws-walls-probe --now"  # a shell command the probe policy below denies
PROBE_POLICY = '[gate]\ndeny_commands = ["ws-walls-probe*"]\n'
PROBE_SENDER = "ws-walls-probe"  # a session id no class lists
PROBE_FOLDER = (
    "ws-walls-probe"  # a folder name under a class's repo (or, alone, outside every class) the lookup is asked about
)


@contextlib.contextmanager
def _environ(**changes: str | None) -> Iterator[None]:
    """`os.environ` with these names set (a string) or removed (None) for the block, and put back as they were after."""
    saved = {k: os.environ.get(k) for k in changes}
    try:
        for k, v in changes.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


@contextlib.contextmanager
def _probe_folder(env: Mapping[str, str]) -> Iterator[Path]:
    """A fresh empty folder (its real path) for a probe to run in, under ws's own state folder: its ancestors are the
    person's, not the system temp root, where another account could leave a `.mcp.json` or a settings file that every
    folder below it would then read. Removed after."""
    base = paths.state_dir(dict(env)) / "walls-probe"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="p-", dir=base) as tmp:
        yield Path(tmp).resolve()


def _now() -> datetime:
    """The clock every walls command reads (UTC, aware): one place, so a test sets it."""
    return datetime.now(UTC)


class Outcome(NamedTuple):
    ok: bool
    detail: str


@dataclass
class Ctx:
    """What a case reads: the policy, the system kind, the classes with an OS user, each one's file in place (parsed, when
    it reads) and the settings the launch copies start from (None when none are generated)."""

    pol: policy.Policy
    kind: str
    env: Mapping[str, str]
    now: datetime
    classes: list[policy.Class] = field(default_factory=list)  # the classes the host cases run for: set up, with a file
    docs: dict[str, dict] = field(default_factory=dict)  # each class's launch file
    base: dict | None = None
    managed: dict[str, dict] = field(default_factory=dict)  # each class's managed-settings file


class Case(NamedTuple):
    """One named case: `check(ctx)` says whether it holds; `setting` names what it depends on (the key, rule or policy line
    whose removal makes it fail); `host` is True when it reads a class file in place, so it is skipped while none is."""

    name: str
    check: Callable[[Ctx], Outcome]
    setting: str
    host: bool = True


# ---- the cases -----------------------------------------------------------------------------------------------------------


def _each(ctx: Ctx, test: Callable[[policy.Class, dict], str | None]) -> Outcome:
    """Run `test(class, class file)` for each class the host cases cover: it returns what is wrong, or None."""
    bad = []
    for cls in ctx.classes:
        why = test(cls, ctx.docs.get(cls.name, {}))
        if why:
            bad.append(f"{cls.name}: {why}")
    if bad:
        return Outcome(False, "; ".join(bad))
    names = ", ".join(c.name for c in ctx.classes)
    return Outcome(True, f"holds for {names}")


def _perms(doc: Mapping) -> Mapping:
    return doc.get("permissions") if isinstance(doc.get("permissions"), Mapping) else {}


def _deny(doc: Mapping) -> list:
    deny = _perms(doc).get("deny")
    return deny if isinstance(deny, list) else []


def _env(doc: Mapping) -> Mapping:
    return doc.get("env") if isinstance(doc.get("env"), Mapping) else {}


def _sender_refused(cls: policy.Class) -> str | None:
    """None when `msg.admit` refuses a message from a session outside the class (none of its sessions, none on its
    allowlist); else what let it through."""
    from ws import msg, session

    sender = session.Record(PROBE_SENDER, "walls", role="worker", class_=None)
    target = session.Record("ws-walls-target", "walls", role="worker", class_=cls.name)
    try:
        msg.admit(sender, target, None, cls, "note", None, None, None)
    except WsError as e:
        return None if e.kind == "refused" else f"the probe failed with {e.kind}, not a refusal"
    return f"a session outside the class could send in (inbound is {cls.inbound})"


def _case_inbound(ctx: Ctx) -> Outcome:
    def test(cls: policy.Class, doc: dict) -> str | None:
        if doc.get(wallsettings.CROSS_KEY) != wallsettings.CROSS_VALUE:
            return f"the class file does not set {wallsettings.CROSS_KEY} to {wallsettings.CROSS_VALUE!r}"
        return _sender_refused(cls)

    return _each(ctx, test)


def _case_sealed(ctx: Ctx) -> Outcome:
    def test(cls: policy.Class, doc: dict) -> str | None:
        home = wallsettings.class_home(cls, ctx.kind)
        deny = _deny(doc)
        lacks = [
            f"{tool}({s})"
            for tool in ("Read", "Edit")
            for s in wallsettings.sealed_paths(cls, home, ctx.kind, ctx.pol)
            if f"{tool}({s})" not in deny
        ]
        return f"the class file does not deny {', '.join(lacks)}" if lacks else None

    return _each(ctx, test)


def _flat(conf: Mapping) -> list[str]:
    fs = (conf.get("sandbox") or {}).get("filesystem") or {}
    return [str(x) for key in ("allowWrite", "allowRead") for x in (fs.get(key) or [])]


def _launch_copy(ctx: Ctx, project: str, doc: Mapping) -> dict:
    """The settings `ws gate launch` would start a session in `project` with: the generated settings made into the launch
    copy for that folder (the policy's `allow_read` and the git folders, with the launch file's `denyRead` applied before
    they are filtered, as the launch does), then the launch file `doc` merged in. Only what exists is in `allowRead`. The
    launch asks git for the project's git folders; `ws walls check` starts no process, so here git finds none."""
    from ws import sandbox
    from ws.adapters import claude_code

    allow, deny = policy.launch_read_rules(ctx.pol)
    with sandbox.no_git():
        text = claude_code.for_project(
            json.dumps(ctx.base or {}),
            project,
            allow_read=allow,
            deny_read=[*deny, *wallsettings.read_denied(doc)],
            env=dict(ctx.env),
        )
    return wallsettings.merge(json.loads(text), doc)


def _case_other_class(ctx: Ctx) -> Outcome:
    """A session is given only its own class's launch file, and that file keeps the other classes' folders out of sight.
    The class a launch looks up for a folder is `policy.class_of` of its real path (`sandbox.session_class`): a folder of
    each class is looked up as that class, a folder in no class as a class with no account (so no launch file is read),
    each class's launch file has every other class's repos in `sandbox.filesystem.denyRead` and none in `allowWrite` or
    `allowRead` of the launch copy for one of its folders (the generated settings made per project as the launch makes
    them, then the file merged in), and no two classes share one file."""
    walled = wallsettings.class_users(ctx.pol)
    own_roots = {c.name: wallsettings.class_roots(c, wallsettings.class_home(c, ctx.kind)) for c in walled}
    base = ctx.base or {}
    bad = []
    for name, roots in own_roots.items():  # the settings every session starts from hold no class's folder
        given = [r for r in roots if r in _flat(base)]
        if given:
            bad.append(f"the generated settings, which every session starts from, give {name}'s {', '.join(given)}")
    stray = policy.class_of(ctx.pol, os.path.join(os.sep, PROBE_FOLDER))
    if stray is not None and stray.os_user:
        bad.append(f"a folder in no class is looked up as {stray.name}, so it is given that class's file")
    for cls in ctx.classes:
        mine = own_roots[cls.name]
        for root in mine:
            got = policy.class_of(ctx.pol, os.path.join(root, PROBE_FOLDER))
            if got is None or got.name != cls.name:
                bad.append(f"a folder of {cls.name} ({root}) is looked up as {got.name if got else 'no class'}")
        doc = ctx.docs.get(cls.name, {})
        denied = [str(x) for x in ((doc.get("sandbox") or {}).get("filesystem") or {}).get("denyRead") or []]
        reach = _flat(_launch_copy(ctx, mine[0], doc) if mine else wallsettings.merge(base, doc))
        for other in walled:
            if other.name == cls.name:
                continue
            theirs = [r for r in own_roots[other.name] if not any(wallsettings.overlaps(r, m) for m in mine)]
            open_ = [r for r in theirs if r not in denied]
            if open_:
                bad.append(f"{cls.name}'s launch file does not deny reading {other.name}'s {', '.join(open_)}")
            given = [r for r in theirs if any(x == r or x.startswith(r.rstrip("/") + "/") for x in reach)]
            if given:
                bad.append(f"a session of {cls.name} is given {other.name}'s {', '.join(given)}")
    files = [wallsettings.launch_file_path(c.name, ctx.kind) for c in ctx.classes]
    if len(set(files)) != len(files):
        bad.append("two classes share one file")
    return Outcome(
        not bad, "; ".join(bad) or "each session gets its own class's file, which hides the other classes' folders"
    )


def _gate_denies(ctx: Ctx, mode: str) -> str | None:
    """None when the gate denies `PROBE_COMMAND` (under a policy that denies it, `PROBE_POLICY`) for a payload whose
    permission_mode is `mode`, and the denial is the probe policy's own rule: the gate reads the call, never the mode. The
    probe runs in a folder of ws's own with `CLAUDE_PROJECT_DIR` and the walled marker cleared, so a project's settings or
    the marker cannot make the gate deny it for another reason (the preflight, a guard). Else what happened."""
    from ws import gate

    with _environ(CLAUDE_PROJECT_DIR=None, **{wallsettings.WALL_MARKER: None}), _probe_folder(ctx.env) as cwd:
        payload = {
            "tool_name": "Bash",
            "tool_input": {"command": PROBE_COMMAND},
            "permission_mode": mode,
            "cwd": str(cwd),
            "session_id": "ws-walls-probe",
        }
        layer: list[str] = []
        try:
            got = gate.check("claude-code", json.dumps(payload), policy.loads(PROBE_POLICY), layer=layer)
        except Exception as e:  # noqa: BLE001 - a gate that cannot judge it does not deny it
            return f"the gate raised {type(e).__name__} instead of denying"
    if got is None or isinstance(got, gate.Ask):
        return f"the gate did not deny the probe command under {mode}"
    if not (layer and layer[0].startswith("rule:")):
        by = ", ".join(layer) or "an unknown layer"
        return f"the gate denied the probe command under {mode}, but not by the probe policy's own rule ({by})"
    return None


def _hook_registered(ctx: Ctx) -> str | None:
    """None when the generated settings run the gate hook for every tool and block on a failure; else what is missing."""
    if ctx.base is None:
        return "no generated Claude Code settings to read (run `ws install`)"
    groups = (ctx.base.get("hooks") or {}).get("PreToolUse") or []
    for g in groups:
        hooks = g.get("hooks") if isinstance(g, dict) else None
        if not isinstance(g, dict) or g.get("matcher") != "*" or not hooks:
            continue
        if all(isinstance(h, dict) and h.get("onFailure") == "block" for h in hooks):
            return None
    return "the generated settings do not run the gate hook for every tool (matcher `*`) and block on failure"


def _case_hooks_bypass(ctx: Ctx) -> Outcome:
    def test(cls: policy.Class, doc: dict) -> str | None:
        # the bypass key is the managed-settings file's (a bare `claude` run is bound by it), not the launch file's
        have = _perms(ctx.managed.get(cls.name) or {}).get(wallsettings.BYPASS_KEY)
        if have != wallsettings.BYPASS_OFF:
            key = f"permissions.{wallsettings.BYPASS_KEY}"
            return f"the managed-settings file does not set {key} to {wallsettings.BYPASS_OFF!r}"
        return None

    out = _each(ctx, test)
    why = None if not out.ok else _hook_registered(ctx) or _gate_denies(ctx, "bypassPermissions")
    return out if not out.ok else Outcome(why is None, why or "the hook runs and denies under bypassPermissions")


def _case_hooks_auto(ctx: Ctx) -> Outcome:
    why = _hook_registered(ctx) or _gate_denies(ctx, "auto")
    return Outcome(why is None, why or "the hook runs and denies under auto mode")


def _project_probe(ctx: Ctx, plants: list[tuple[str, dict]]) -> tuple[WsError | None, list[WsError | None]]:
    """(the refusal for an empty project, the refusal for each of `plants`) as `claude_code.preflight_refusal` gives them with
    the walled marker on: each plant is a (load file, content) written alone into a fresh project folder of ws's own
    (`_probe_folder`), so nothing above it is another account's."""
    from ws.adapters import claude_code

    planted: list[WsError | None] = []
    with _environ(CLAUDE_PROJECT_DIR=None, **{wallsettings.WALL_MARKER: "1"}):
        with _probe_folder(ctx.env) as root:
            clean = claude_code.preflight_refusal({"cwd": str(root)})
        for rel, doc in plants:
            with _probe_folder(ctx.env) as root:
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text(json.dumps(doc), encoding="utf-8")
                planted.append(claude_code.preflight_refusal({"cwd": str(root)}))
    return clean, planted


def _marker_off(ctx: Ctx) -> str | None:
    marker = {c.name: _env(ctx.docs.get(c.name, {})).get(wallsettings.WALL_MARKER) for c in ctx.classes}
    off = [n for n, v in marker.items() if v != "1"]
    return f"{', '.join(off)}: the class file does not set env {wallsettings.WALL_MARKER} to '1'" if off else None


def _case_project_load(ctx: Ctx) -> Outcome:
    """A walled session whose project adds an MCP server or a hook is refused on a call: the class file's marker turns the
    check on, and a planted `.mcp.json` trips it."""
    off = _marker_off(ctx)
    if off:
        return Outcome(False, off)
    clean, (planted,) = _project_probe(ctx, [(".mcp.json", {"mcpServers": {"probe": {"command": "x"}}})])
    if clean is not None:
        return Outcome(False, f"an empty project was refused: {clean.message}")
    if planted is None or planted.kind != "project_load_refused":
        return Outcome(False, "a project .mcp.json that adds an MCP server was not refused")
    return Outcome(True, "a project .mcp.json or settings file that adds anything is refused")


# A project settings file that would switch the gate hook off or bring hooks in by another door: each is refused.
_HOOK_PLANTS = [
    (".claude/settings.json", {wallsettings.HOOKS_KEY: True}),
    (".claude/settings.local.json", {wallsettings.HOOKS_KEY: True}),
    (".claude/settings.json", {"enabledPlugins": {"probe@probe-market": True}}),
    (".claude/settings.json", {"extraKnownMarketplaces": {"probe-market": {"source": {"source": "github"}}}}),
]


def _case_hooks_not_disabled(ctx: Ctx) -> Outcome:
    """A project cannot switch the gate hook off: the class file sets `disableAllHooks` false (it outranks a project's), and
    a project file that sets it, or turns on a plugin, is refused."""

    def test(cls: policy.Class, doc: dict) -> str | None:
        if doc.get(wallsettings.HOOKS_KEY) is not False:
            return f"the class file does not set {wallsettings.HOOKS_KEY} to false"
        return None

    out = _each(ctx, test)
    if not out.ok:
        return out
    off = _marker_off(ctx)
    if off:
        return Outcome(False, off)
    _, planted = _project_probe(ctx, _HOOK_PLANTS)
    missed = [
        rel.split("/")[-1] + " " + next(iter(doc)) for (rel, doc), got in zip(_HOOK_PLANTS, planted) if got is None
    ]
    if missed:
        return Outcome(False, f"a project that sets {', '.join(missed)} was not refused")
    return Outcome(True, "the class file keeps hooks on, and a project that turns them off or adds plugins is refused")


# A project sandbox table that would weaken the wall: each is refused, whatever its value.
_SANDBOX_PLANTS = [
    (".claude/settings.json", {"sandbox": {key: value}}) for key in wallsettings.WEAKER_KEYS for value in (True, False)
] + [
    (".claude/settings.json", {"sandbox": {"excludedCommands": ["probe"]}}),
    (".claude/settings.local.json", {"sandbox": {"filesystem": {"allowRead": ["/probe"]}}}),
]


def _case_sandbox_narrows(ctx: Ctx) -> Outcome:
    """A project's sandbox table can only narrow: the class file sets the two weaker-sandbox switches false (it outranks a
    project's), and a project file that sets either, or opens the sandbox another way, is refused."""

    def test(cls: policy.Class, doc: dict) -> str | None:
        sb = doc.get("sandbox") if isinstance(doc.get("sandbox"), Mapping) else {}
        on = [f"sandbox.{k}" for k in wallsettings.WEAKER_KEYS if sb.get(k) is not False]
        return f"the class file does not set {', '.join(on)} to false" if on else None

    out = _each(ctx, test)
    if not out.ok:
        return out
    off = _marker_off(ctx)
    if off:
        return Outcome(False, off)
    _, planted = _project_probe(ctx, _SANDBOX_PLANTS)
    missed = [
        rel.split("/")[-1] + " sandbox." + next(iter(doc["sandbox"]))
        for (rel, doc), got in zip(_SANDBOX_PLANTS, planted)
        if got is None
    ]
    if missed:
        return Outcome(False, f"a project that sets {', '.join(missed)} was not refused")
    return Outcome(
        True, "the class file keeps the weaker-sandbox switches off, and a project that opens the sandbox is refused"
    )


def _case_per_repo(ctx: Ctx) -> Outcome:
    """A per-repo table can only narrow: a widening key is refused when the policy is read, and the narrowing it applies adds
    denials and changes nothing else."""
    try:
        policy.parse_repo_settings({"/p/**": {"allow": ["x"]}}, ("/p/**",), "")
    except policy.PolicyError:
        pass
    else:
        return Outcome(False, "a per-repo table with an allow key was accepted")
    doc = {
        "permissions": {"deny": ["SendMessage"]},
        "sandbox": {
            "filesystem": {"allowWrite": ["/p"], "denyRead": ["~/"]},
            "network": {"allowedDomains": ["h.example"]},
        },
    }
    narrow = policy.RepoSettings("/p/**", ("Bash(probe *)",), ("/p/secret",), ("/p/locked",))
    got = wallsettings.narrowed(doc, narrow)
    fs, was = got["sandbox"]["filesystem"], doc["sandbox"]["filesystem"]
    if not (
        "Bash(probe *)" in got["permissions"]["deny"]
        and "/p/secret" in fs.get("denyRead", [])
        and "/p/locked" in fs.get("denyWrite", [])
    ):
        return Outcome(False, "a per-repo table's denials were not applied")
    if fs["allowWrite"] != was["allowWrite"] or got["sandbox"]["network"] != doc["sandbox"]["network"]:
        return Outcome(False, "a per-repo table changed something other than a denial")
    return Outcome(True, "per-repo tables only add denials, and a widening key is refused")


def _case_otel(ctx: Ctx) -> Outcome:
    def test(cls: policy.Class, doc: dict) -> str | None:
        env = _env(doc)
        on = [k for k, v in wallsettings.OTEL_OFF.items() if env.get(k) != v]
        return f"the class file does not set env {', '.join(on)} to '0'" if on else None

    return _each(ctx, test)


def _case_owner_review(ctx: Ctx) -> Outcome:
    classes = wallsettings.class_users(ctx.pol)
    bad = []
    for cls in classes:
        if not cls.owner:
            bad.append(f"{cls.name} has no owner")
        elif cls.review_date is None:
            bad.append(f"{cls.name} has no review_date")
        elif cls.review_due(ctx.now):  # the one judge doctor's WS017 asks too
            bad.append(f"{cls.name} was due for review on {cls.review_date}")
    return Outcome(not bad, "; ".join(bad) or f"owner and review date set and not due for {len(classes)} class(es)")


def _case_messaging(ctx: Ctx) -> Outcome:
    def test(cls: policy.Class, doc: dict) -> str | None:
        lacks = [t for t in wallsettings.MESSAGING_DENY if t not in _deny(doc)]
        return f"the class file does not deny {', '.join(lacks)}" if lacks else None

    return _each(ctx, test)


def _case_file_safe(ctx: Ctx) -> Outcome:
    def test(cls: policy.Class, doc: dict) -> str | None:
        problem = wallsettings.owner_problem(wallsettings.launch_file_path(cls.name, ctx.kind))
        return f"the class file {problem[0]}" if problem else None

    return _each(ctx, test)


# ---- the flow cases: what leaves a walled class through ws's own shared stores ----------------------------------------------
#
# Each asks a question of ws's own code on a throw-away world under ws's probe folder (two walled classes, one full class,
# their repos, a shared message store, a notes and a memory folder, a policy file), so none needs a class file and none
# touches the person's real stores. A positive control comes first in each: the planted value is shown to be where it
# belongs before it is shown to be nowhere else, so a case can't pass because nothing was ever written.

PROBE_OTHER = "ws-walls-other"  # a session id no class lists
PROBE_REMOTE = "push.example.invalid"  # a git remote no class lists: a push to it is a connection to this host
PROBE_BODY = policy.CANARY_PREFIX  # the start of the value planted at run time, which has a random tail
FIXED_KINDS = (
    "ack",
    "done",
    "stop-and-hand-over:done",
    "ask:pending",
    "counts",
)  # all a walled session may send, no text


class _World(NamedTuple):
    """A scratch world for a flow case: the policy, three folders and a receiver in each class."""

    root: Path
    pol: policy.Policy
    env: dict[str, str]
    sender: session.Record  # PROBE_SENDER, in the full class
    to_alpha: session.Record  # a session in alpha's repo
    to_beta: session.Record
    to_open: session.Record  # a session in the full class


def _world_policy(root: Path, inbound: str = "allowlist", readable_by: str = "") -> str:
    """The scratch policy: alpha (walled, with an allowlist naming `PROBE_SENDER`), beta (walled) and open (full)."""
    allow = f"inbound_allow = [{json.dumps(PROBE_SENDER)}]\n" if inbound == "allowlist" else ""
    return (
        f'[classes.alpha]\naccess = "walled"\nrepos = [{json.dumps(f"{root}/alpha/**")}, '
        f'{json.dumps(f"{root}/notes/alpha/**")}]\ninbound = "{inbound}"\n{allow}{readable_by}\n'
        f'[classes.beta]\naccess = "walled"\nrepos = [{json.dumps(f"{root}/beta/**")}]\n\n'
        f'[classes.open]\naccess = "full"\nrepos = [{json.dumps(f"{root}/open/**")}]\n'
    )


@contextlib.contextmanager
def _world(ctx: Ctx, inbound: str = "allowlist", readable_by: str = "") -> Iterator[_World]:
    """A scratch world under a fresh probe folder, removed after. The two walled repos are git repos by their folder alone
    (a `.git` folder, no process)."""
    from ws import profile, session

    with _probe_folder(ctx.env) as root:
        for name in ("alpha", "beta", "open"):
            (root / name / "src").mkdir(parents=True)
            (root / name / ".git").mkdir()
        (root / "notes" / "alpha").mkdir(parents=True)
        (root / "memory").mkdir()
        config = root / "config"
        config.mkdir()
        (config / "policy.toml").write_text(_world_policy(root, inbound, readable_by), encoding="utf-8")
        roles = {"notes": {"path": str(root / "notes")}, "memory": {"path": str(root / "memory")}}
        (config / "profile.toml").write_text(profile.dumps({"roles": roles}), encoding="utf-8")
        pol = policy.load(config / "policy.toml")

        def record(sid: str, where: str, role: str = "worker") -> session.Record:
            cls = policy.class_of(pol, root / where)
            return session.Record(sid, "walls", cwd=str(root / where), role=role, class_=cls.name if cls else None)

        yield _World(
            root,
            pol,
            {paths.OVERRIDE_VARS[0]: str(config), "HOME": str(root / "home")},
            record(PROBE_SENDER, "open/src"),
            record("ws-walls-alpha", "alpha/src"),
            record("ws-walls-beta", "beta/src"),
            record("ws-walls-open", "open/src"),
        )


@contextlib.contextmanager
def _chdir(path: Path) -> Iterator[None]:
    """The process's working folder is `path` for the block (a caller's class is that of its real working folder)."""
    before = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)


def _plant() -> str:
    """A value that is nowhere until a case puts it somewhere: the probe's marker and a random tail made now."""
    return PROBE_BODY + os.urandom(8).hex()


def _shared_bytes(folder: Path) -> bytes:
    """Every byte of the shared message database in `folder` and its journal files."""
    return b"".join(p.read_bytes() for p in sorted(folder.glob("messages.db*")) if p.is_file())


def _log_of(db: Path) -> list[tuple]:
    """(sender, class, kind, decision, reason) for each row of a class store's `inbound_log`, oldest first."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return conn.execute("SELECT sender, class, kind, decision, reason FROM inbound_log ORDER BY id").fetchall()
        finally:
            conn.close()
    except sqlite3.Error:  # no store, or one with no log: a case that finds no rows says so
        return []


def _body_of(db: Path, message_id: int) -> str | None:
    """The body a class store holds for a message, opened read-only as a session of the class opens it."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT body FROM bodies WHERE message_id=?", (message_id,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:  # no store: there is no body
        return None
    return row[0] if row else None


def _case_allowed_sender(ctx: Ctx) -> Outcome:
    """A message to a walled class is delivered only from a session its `inbound_allow` names, with `inbound` set to
    `allowlist`; each message, delivered or refused, leaves one row in the class's own store."""
    from ws import msg, session

    with _world(ctx) as w:
        alpha = w.pol.classes["alpha"]
        db = msg.class_store_path(w.pol, alpha, w.to_alpha.cwd)
        shared = w.root / "shared"
        try:
            msg.scratch_delivery(shared, w.pol, w.sender, w.to_alpha, "note", _plant())
        except WsError as e:
            return Outcome(False, f"a session on alpha's allowlist was not let in ({e.kind})")
        other = session.Record(PROBE_OTHER, "walls", cwd=w.sender.cwd, class_="open")
        try:
            msg.scratch_delivery(shared, w.pol, other, w.to_alpha, "note", _plant())
        except WsError as e:
            if e.kind != "refused":
                return Outcome(False, f"a session off alpha's allowlist failed with {e.kind}, not a refusal")
        else:
            return Outcome(False, "a session that alpha's allowlist does not name could send in")
        want = [
            (PROBE_SENDER, "alpha", "note", "delivered", None),
            (PROBE_OTHER, "alpha", "note", "refused", "refused"),
        ]
        if _log_of(db) != want:
            return Outcome(False, "the class store's log does not hold one row for each message, delivered or refused")
        if len(msg.scratch_envelopes(shared)) != 1:
            return Outcome(False, "a refused message left an envelope row")
        for inbound in ("lead-only", "off"):  # a walled class that names no one takes no one, the lead included
            with _world(ctx, inbound) as v:
                lead = session.Record(PROBE_SENDER, "walls", cwd=v.sender.cwd, role="lead", class_="open")
                try:
                    msg.scratch_delivery(v.root / "shared", v.pol, lead, v.to_alpha, "note", _plant())
                except WsError as e:
                    if e.kind != "refused":
                        return Outcome(False, f"inbound {inbound} failed with {e.kind}, not a refusal")
                else:
                    return Outcome(False, f"a lead was let in to a walled class whose inbound is {inbound}")
    return Outcome(True, "only a session on a walled class's allowlist is let in, and each message leaves one log row")


def _modes_problem(folder: Path) -> str | None:
    """None when the shared store's folder is 0700 and its file 0600, both ours; else what is wrong. (Without `os.getuid`
    there is no account to ask, and only the modes are read.)"""
    import stat

    getuid = getattr(os, "getuid", None)
    for path, want in ((folder, 0o700), (folder / "messages.db", 0o600)):
        st = path.stat()
        if stat.S_IMODE(st.st_mode) != want:
            return f"{path.name} is {oct(stat.S_IMODE(st.st_mode))}, not {oct(want)}"
        if getuid is not None and st.st_uid != getuid():
            return f"{path.name} is not owned by this account"
    return None


def _case_bodies_in_class_store(ctx: Ctx) -> Outcome:
    """A walled message's body is kept only in the class store under the class's repo; the shared database (envelope and all)
    holds no body and no hash of one, and is private to the account, so a session running as the class's user cannot open it
    while the class store, under the class's own repo, is where its sessions read."""
    import hashlib

    from ws import msg

    with _world(ctx) as w:
        alpha = w.pol.classes["alpha"]
        text = _plant()
        shared = w.root / "shared"
        out = msg.scratch_delivery(shared, w.pol, w.sender, w.to_alpha, "note", text)
        db = msg.class_store_path(w.pol, alpha, w.to_alpha.cwd)
        if not db.resolve().is_relative_to(w.root.resolve() / "alpha"):
            return Outcome(False, "the class store is not under the class's repo")
        if _body_of(db, out["id"]) != text:
            return Outcome(False, "the class store does not hold the body (a session of the class could not read it)")
        raw = _shared_bytes(shared)
        if text.encode() in raw or hashlib.sha256(text.encode()).hexdigest().encode() in raw:
            return Outcome(False, "the shared message database holds the body, or a hash of it")
        why = _modes_problem(shared)
        if why:
            return Outcome(False, f"the shared message store is not private: {why}")
    return Outcome(
        True, "the body is in the class store under the class's repo and nowhere in the private shared database"
    )


def _case_fixed_kinds(ctx: Ctx) -> Outcome:
    """A session in a walled class sends only the fixed kinds, with no body, no key and no thread name."""
    from ws import msg, session

    with _world(ctx) as w:
        walled = policy.class_of(w.pol, w.to_alpha.cwd)
        open_ = policy.class_of(w.pol, w.sender.cwd)
        lead = session.Record("ws-walls-lead", "walls", cwd=w.sender.cwd, role="lead", class_="open")

        def verdict(kind: str, body: str | None = None, key: str | None = None, thread: str | None = None) -> bool:
            try:
                msg.admit(w.to_alpha, lead, walled, open_, kind, body, key, thread)
            except WsError as e:
                if e.kind != "refused":
                    raise
                return False
            return True

        if tuple(msg.FIXED_KINDS) != FIXED_KINDS:
            return Outcome(False, f"the fixed kinds are {', '.join(msg.FIXED_KINDS)}, not {', '.join(FIXED_KINDS)}")
        held_back = [k for k in FIXED_KINDS if not verdict(k)]
        if held_back:
            return Outcome(False, f"a walled session could not send the fixed kind {', '.join(held_back)}")
        loose = [k for k in msg.KINDS if k not in FIXED_KINDS and verdict(k)]
        if loose:
            return Outcome(False, f"a walled session could send the free-text kind {', '.join(loose)}")
        if verdict("done", body="some words"):
            return Outcome(False, "a walled session could send a body with a fixed kind")
        if verdict("done", key="a-key") or verdict("done", thread="a-thread"):
            return Outcome(False, "a walled session could set an idempotency key or a thread name")
    return Outcome(True, f"a walled session sends only {', '.join(FIXED_KINDS)}, with no body, key or thread")


def _network(doc: Mapping) -> Mapping:
    net = (doc.get("sandbox") or {}).get("network") if isinstance(doc.get("sandbox"), Mapping) else None
    return net if isinstance(net, Mapping) else {}


def _case_providers(ctx: Ctx) -> Outcome:
    """The hosts in a class's launch file are the class's own `allowed_hosts` and the hosts of the providers it names, and no
    others: a provider it does not name has none, and a `local_only` class has loopback only."""
    from ws import sandbox

    def test(cls: policy.Class, doc: dict) -> str | None:
        listed = _network(doc).get("allowedDomains")
        if not isinstance(listed, list):
            return "the launch file has no sandbox.network.allowedDomains"
        named = () if cls.local_only else (cls.allowed_providers or ())
        own = {*cls.allowed_hosts, *(h for pv in named for h in sandbox.PROVIDER_HOSTS.get(pv, ()))}
        stray = [h for h in listed if h not in own]
        if stray:
            return f"the launch file lists {', '.join(map(str, stray))}, which neither allowed_hosts nor an allowed provider gives"
        unnamed = {h for pv, hosts in sandbox.PROVIDER_HOSTS.items() if pv not in named for h in hosts} - set(
            cls.allowed_hosts
        )
        taken = [h for h in listed if h in unnamed]
        if taken:
            return f"the launch file lists {', '.join(taken)}, the host of a provider the class does not allow"
        if cls.local_only:
            far = [h for h in listed if sandbox.split_entry(str(h))[0] not in sandbox.LOOPBACK_NAMES]
            if far:
                return f"the launch file lists {', '.join(map(str, far))} for a local_only class"
        if listed != sandbox.class_domains(cls):
            return "the launch file's allowedDomains are not what the class's own list gives now"
        return None

    return _each(ctx, test)


def _case_push_refused(ctx: Ctx) -> Outcome:
    """A git push to a remote outside the class's list is refused: a class's remote list is its network allowlist, which
    the launch file holds as `sandbox.network.allowedDomains` under `strictAllowlist` (a host not on it is refused, not asked
    about), and which ws's own egress plan reads from the same policy."""
    from ws import sandbox

    def test(cls: policy.Class, doc: dict) -> str | None:
        net = _network(doc)
        if net.get("strictAllowlist") is not True:
            return "the launch file does not set sandbox.network.strictAllowlist to true"
        listed = net.get("allowedDomains")
        if listed != sandbox.class_domains(cls):
            return "the launch file's allowedDomains are not the class's own list"
        for entry in listed:
            if sandbox.host_matches(PROBE_REMOTE, 443, str(entry)):
                return f"the launch file lets a push to {PROBE_REMOTE} through ({entry})"
        plan = sandbox.egress_plan(ctx.pol, cls, "", ctx.now)
        if plan is None or plan.decide(PROBE_REMOTE, 443).allow:
            return f"ws's egress plan lets a push to {PROBE_REMOTE} through"
        plain = [h for h in cls.allowed_hosts if not sandbox.entry_problem(h) and not h.startswith("*.")]
        host, port = sandbox.split_entry(plain[0]) if plain else ("", None)
        if plain and not plan.decide(host, port or 443).allow:
            return f"ws's egress plan refuses {plain[0]}, a remote the class lists"
        return None

    return _each(ctx, test)


def _case_memory_notes(ctx: Ctx) -> Outcome:
    """Memory and notes keep a walled class's content out of the shared stores: a session whose class is walled (the class
    of its real working folder) can add no memory file and capture no note, a search for a session of another class leaves
    out a walled class's files unless its `readable_by` lets that class read them, and a policy that will not load refuses."""
    from ws import memory, notes

    with _world(ctx) as w:
        mem, vault = w.root / "memory", w.root / "notes"
        alpha_file = vault / "alpha" / "plan.md"
        marked = vault / "copied.md"
        word = "quince" + os.urandom(4).hex()
        alpha_file.write_text(f"# Plan\n{word} in alpha's repo\n", encoding="utf-8")
        marked.write_text(f"---\nclass: alpha\n---\n{word} copied out of alpha\n", encoding="utf-8")
        (vault / "mine.md").write_text(f"{word} that is nobody's class\n", encoding="utf-8")

        def refused(call: Callable[[], object]) -> bool:
            try:
                call()
            except WsError as e:
                return e.kind == "refused"
            return False

        def found() -> set[str]:
            return {Path(h.path).name for h in memory.search(word, 10, w.env)}

        with _chdir(w.root / "alpha" / "src"):
            if not refused(lambda: memory.add("alpha's own words", "T", env=w.env)):
                return Outcome(False, "a session in a walled class could add a memory file")
            if not refused(lambda: notes.capture("alpha's own words", vault, env=w.env)):
                return Outcome(False, "a session in a walled class could capture a note")
            if (vault / "_inbox").exists() or any(mem.rglob("*.md")):
                return Outcome(False, "a refused call left a file in a shared store")
            if found() != {"plan.md", "copied.md", "mine.md"}:
                return Outcome(False, "a session in the class could not find its own files (the search was not run)")
        with _chdir(w.root / "beta" / "src"):
            if found() != {"mine.md"}:
                return Outcome(False, "a session of another class was shown a walled class's file")
        with _chdir(w.root / "open" / "src"):
            notes.capture(
                "a person's own words", vault, env=w.env
            )  # a class that is not walled may: nothing is refused
            if not (vault / "_inbox").is_dir():
                return Outcome(False, "a session in a full class could not capture a note")
        for allowed in ('readable_by = ["beta"]', 'readable_by = ["*"]'):
            (w.root / "config" / "policy.toml").write_text(_world_policy(w.root, readable_by=allowed), encoding="utf-8")
            with _chdir(w.root / "beta" / "src"):
                if found() != {"plan.md", "copied.md", "mine.md"}:
                    return Outcome(False, f"{allowed} did not let beta read alpha's files")
        (w.root / "config" / "policy.toml").write_text('[classes.alpha]\naccess = "nonsense"\n', encoding="utf-8")
        with _chdir(w.root / "open" / "src"):
            if not (refused(lambda: memory.add("x", "T", env=w.env)) and refused(lambda: found())):
                return Outcome(False, "a policy that will not load did not refuse memory")
            if not refused(lambda: notes.capture("x", vault, env=w.env)):
                return Outcome(False, "a policy that will not load did not refuse a note")
    return Outcome(True, "a walled session adds and captures nothing, and a search hides a walled class's files")


def _case_no_counts(ctx: Ctx) -> Outcome:
    """A walled message's card (the body-free envelope row `ws msg watch` and the viewer show) is the envelope columns and
    nothing else: no count or counts key, and no number but `seq` and `message_id` (the size is a bucket letter)."""
    from ws import msg

    with _world(ctx) as w:
        shared = w.root / "shared"
        msg.scratch_delivery(shared, w.pol, w.sender, w.to_alpha, "note", _plant())
        (card,) = msg.scratch_envelopes(shared)
        if card.get("class") != "alpha":
            return Outcome(False, "the probe message was not a walled one")
        if tuple(card) != msg.ENVELOPE_KEYS:
            return Outcome(False, f"the card has the keys {', '.join(card)}, not the envelope's")
        counted = [k for k in card if "count" in k.lower()]
        if counted:
            return Outcome(False, f"the card has a count: {', '.join(counted)}")
        numbers = [k for k, v in card.items() if isinstance(v, int | float) and k not in ("seq", "message_id")]
        if numbers:
            return Outcome(False, f"the card carries a number in {', '.join(numbers)}")
        if card["size"] not in ("S", "M", "L"):
            return Outcome(False, "the card's size is not a bucket")
    return Outcome(True, "a walled card is the envelope: seq, message_id and a size bucket are the only numbers")


def _case_tracer(ctx: Ctx) -> Outcome:
    """A value planted in a walled class (the body of a message to it, and the text of a file in its repo) is shown to be in
    the class first, then to be in no shared sink ws writes: the shared message database (envelope included), memory and
    notes. Each sink is shown to work by a control, so a value that was never written can't pass."""
    from ws import memory, msg, notes, session

    with _world(ctx) as w:
        value, control = _plant(), _plant()
        shared, vault, mem = w.root / "shared", w.root / "notes", w.root / "memory"
        repo_file = w.root / "alpha" / "src" / "finding.md"
        repo_file.write_text(f"# Finding\n{value}\n", encoding="utf-8")
        out = msg.scratch_delivery(shared, w.pol, w.sender, w.to_alpha, "note", f"see {value}")
        db = msg.class_store_path(w.pol, w.pol.classes["alpha"], w.to_alpha.cwd)
        if value not in (_body_of(db, out["id"]) or "") or value not in repo_file.read_text(encoding="utf-8"):
            return Outcome(False, "the planted value is not in the class (the check would pass for nothing)")
        lead = session.Record("ws-walls-lead", "walls", cwd=w.sender.cwd, role="lead", class_="open")
        msg.scratch_delivery(
            shared, w.pol, lead, w.to_open, "note", control
        )  # a body that is allowed in the shared store
        if control.encode() not in _shared_bytes(shared):
            return Outcome(False, "the shared message database can't be seen to hold a body (the scan is blind)")
        if value.encode() in _shared_bytes(shared):
            return Outcome(False, "the planted value reached the shared message database")
        for row in msg.scratch_envelopes(shared):
            if value in repr(row):
                return Outcome(False, "the planted value reached an envelope row")
        with _chdir(w.root / "alpha" / "src"):
            for call in (lambda: memory.add(value, "T", env=w.env), lambda: notes.capture(value, vault, env=w.env)):
                try:
                    call()
                except WsError:
                    pass
        with _chdir(w.root / "open" / "src"):
            notes.capture(control, vault, env=w.env)  # the notes scan sees what is written there
        stored = b"".join(p.read_bytes() for d in (mem, vault) for p in d.rglob("*") if p.is_file())
        if control.encode() not in stored:
            return Outcome(False, "notes can't be seen to hold a note (the scan is blind)")
        if value.encode() in stored:
            return Outcome(False, "the planted value reached memory or notes")
    return Outcome(
        True, "a value planted in a walled class is in the class and in no shared message store, memory or notes"
    )


def _case_launch_blocks(ctx: Ctx) -> Outcome:
    """C360: the class's own launch file blocks file-tool writes outside the working folders and keeps the sign-in tokens
    from shell commands, so a class session has both whatever the generated settings say. They are not in the managed
    file, which binds every account on the machine."""

    def test(cls: policy.Class, doc: dict) -> str | None:
        lacks = []
        if _perms(doc).get(wallsettings.WRITE_BLOCK_KEY) is not True:
            lacks.append(f"permissions.{wallsettings.WRITE_BLOCK_KEY} true")
        missing = wallsettings.tokens_missing(doc)
        if missing:
            lacks.append(f"sandbox.credentials.envVars deny of {', '.join(missing)}")
        return f"the launch file does not set {'; '.join(lacks)}" if lacks else None

    return _each(ctx, test)


def _case_remote_off(ctx: Ctx) -> Outcome:
    """G11: a class session can't be driven from Remote Control and fetches no claude.ai connector."""

    def test(cls: policy.Class, doc: dict) -> str | None:
        on = [k for k in (wallsettings.REMOTE_KEY, wallsettings.CONNECTORS_KEY) if doc.get(k) is not True]
        return f"the launch file does not set {', '.join(on)} to true" if on else None

    return _each(ctx, test)


def _case_headless(ctx: Ctx) -> Outcome:
    """A headless job of the class (`ws run`, attack S1) gets the settings a launch gets, plus the no-prompt key: the
    generated settings made unattended, made into the launch copy for a class folder, the launch file merged in. In them a
    write outside the job's folders fails: the file tools' write block is on, the shell's writes are held to the class's
    repos and scratch by the managed file, no command runs outside the sandbox, and a call that would wait on a prompt
    is denied instead."""
    from ws.adapters import claude_code

    if ctx.base is None:
        return Outcome(False, "no generated Claude Code settings to read (run `ws install`)")

    def test(cls: policy.Class, doc: dict) -> str | None:
        roots = wallsettings.class_roots(cls, wallsettings.class_home(cls, ctx.kind))
        if not roots:
            return "the class names no repo folder"
        job = wallsettings.merge(json.loads(claude_code.unattended(json.dumps(_launch_copy(ctx, roots[0], {})))), doc)
        perms, sb = _perms(job), job.get("sandbox") if isinstance(job.get("sandbox"), Mapping) else {}
        bad = []
        if perms.get(wallsettings.WRITE_BLOCK_KEY) is not True:
            bad.append(f"permissions.{wallsettings.WRITE_BLOCK_KEY} is not true")
        if perms.get(wallsettings.UNATTENDED_KEY) is not True:
            bad.append(f"permissions.{wallsettings.UNATTENDED_KEY} is not true")
        if sb.get("allowUnsandboxedCommands") is not False:
            bad.append("sandbox.allowUnsandboxedCommands is not false")
        managed = ctx.managed.get(cls.name) or {}
        writes = ((managed.get("sandbox") or {}).get("filesystem") or {}).get("allowWrite") or []
        outside = [w for w in writes if w not in roots and w not in wallsettings.scratch_dirs(ctx.kind)]
        if not writes or outside:
            bad.append("the managed file's allowWrite is not the class's repos and scratch")
        return "a job's settings: " + "; ".join(bad) if bad else None

    return _each(ctx, test)


# The top-level values every class's launch file holds, a local model's class too (a fixed list, not the file ws renders,
# so a key the renderer drops is a failure here).
LAUNCH_WALL = {
    wallsettings.CROSS_KEY: wallsettings.CROSS_VALUE,
    wallsettings.PEER_KEY: True,
    wallsettings.HOOKS_KEY: False,
    wallsettings.REMOTE_KEY: True,
    wallsettings.CONNECTORS_KEY: True,
}


def _case_local_model(ctx: Ctx) -> Outcome:
    """rr-M3, run as a local-model session: a `local_only` class takes a model on this machine and refuses a hosted
    one, and the local session gets the same wall as a Claude session of the class: its launch file has the same keys,
    and its network is loopback only."""
    from ws import sandbox

    local = [c for c in ctx.classes if c.local_only]
    if not local:
        return Outcome(True, "no local_only class in the policy (`ws walls canary` plants one)")
    bad = []
    for cls in local:
        if sandbox.provider_problem(cls, "local") is not None:
            bad.append(f"{cls.name}: a local model is refused")
        if sandbox.provider_problem(cls, "anthropic") is None:
            bad.append(f"{cls.name}: a hosted provider is allowed for a local_only class")
        doc = ctx.docs.get(cls.name) or {}
        lacks = [k for k, v in LAUNCH_WALL.items() if doc.get(k) != v]
        lacks += [
            f"permissions.{k}"
            for k in (wallsettings.READ_BLOCK_KEY, wallsettings.WRITE_BLOCK_KEY)
            if _perms(doc).get(k) is not True
        ]
        lacks += [f"deny {t}" for t in wallsettings.MESSAGING_DENY if t not in _deny(doc)]
        if lacks:
            bad.append(f"{cls.name}: the launch file lacks {', '.join(lacks)}")
        far = [
            h
            for h in _network(doc).get("allowedDomains") or []
            if sandbox.split_entry(str(h))[0] not in sandbox.LOOPBACK_NAMES
        ]
        if far:
            bad.append(f"{cls.name}: a local session can reach {', '.join(map(str, far))}")
    return Outcome(not bad, "; ".join(bad) or "a local model session gets the class's wall and loopback only")


CASES: list[Case] = [
    Case("inbound-refused", _case_inbound, "crossSessionInbound, [classes.NAME] inbound"),
    Case("sealed-read-refused", _case_sealed, "permissions.deny Read() and Edit() of the sealed paths"),
    Case(
        "class-file-not-seen-by-other-class",
        _case_other_class,
        "the launch's class lookup, and sandbox.filesystem.denyRead of each launch file",
    ),
    Case("hooks-under-bypass", _case_hooks_bypass, "permissions.disableBypassPermissionsMode, the PreToolUse hook"),
    Case("hooks-under-auto", _case_hooks_auto, "the PreToolUse hook on every tool"),
    Case("project-load-check", _case_project_load, "env WS_WALLED_CLASS in the class file"),
    Case(
        "hooks-not-disabled",
        _case_hooks_not_disabled,
        "disableAllHooks false in the class file, the project load check",
    ),
    Case(
        "sandbox-only-narrows",
        _case_sandbox_narrows,
        "sandbox.enableWeakerNestedSandbox and sandbox.enableWeakerNetworkIsolation false in the class file, the project "
        "load check",
    ),
    Case("per-repo-settings", _case_per_repo, "[classes.NAME.repo_settings] only narrows", host=False),
    Case("otel-off", _case_otel, "env OTEL_LOG_USER_PROMPTS and OTEL_LOG_TOOL_DETAILS in the class file"),
    Case("owner-and-review-date", _case_owner_review, "owner and review_date under [classes.NAME]", host=False),
    Case("messaging-denied", _case_messaging, "permissions.deny SendMessage and ListAgents"),
    Case("class-file-root-owned", _case_file_safe, "the class file's owner and mode"),
    Case(
        "launch-write-block-and-token-deny",
        _case_launch_blocks,
        "permissions.blockWritesOutsideWorkingDirectories and sandbox.credentials.envVars in the launch file",
    ),
    Case(
        "remote-and-connectors-off",
        _case_remote_off,
        "disableRemoteControl and disableClaudeAiConnectors in the launch file",
    ),
    Case(
        "headless-job-in-wall",
        _case_headless,
        "permissions.denyInsteadOfPrompting for a job, the write block, the managed file's allowWrite",
    ),
    Case(
        "local-model-in-wall", _case_local_model, "[classes.NAME] local_only, the launch file's keys and loopback hosts"
    ),
    Case(
        "allowed-sender-delivered",
        _case_allowed_sender,
        "[classes.NAME] inbound = allowlist and inbound_allow, msg.admit, the class store's inbound_log",
        host=False,
    ),
    Case(
        "bodies-in-class-store",
        _case_bodies_in_class_store,
        "msg.class_store_path under the class's repo, and the shared store's 0700 folder and 0600 file",
        host=False,
    ),
    Case(
        "walled-sends-fixed-kinds", _case_fixed_kinds, "msg.FIXED_KINDS and msg.admit for a walled sender", host=False
    ),
    Case(
        "provider-allowlist",
        _case_providers,
        "[classes.NAME] allowed_providers, allowed_hosts and local_only; sandbox.network.allowedDomains in the launch file",
    ),
    Case(
        "class-push-refused",
        _case_push_refused,
        "sandbox.network.strictAllowlist and allowedDomains in the launch file, [classes.NAME] allowed_hosts",
    ),
    Case(
        "memory-notes-refused",
        _case_memory_notes,
        "notes.refuse_walled in memory.add and notes.capture, memory.readable_by_caller, [classes.NAME] readable_by",
        host=False,
    ),
    Case("walled-card-no-counts", _case_no_counts, "msg.ENVELOPE_KEYS, the envelope table's columns", host=False),
    Case(
        "tracer-stays-in-class",
        _case_tracer,
        "msg's class store, the shared store's envelope, and the memory and notes refusals",
        host=False,
    ),
]


# ---- the check -----------------------------------------------------------------------------------------------------------


def _read_json(path: Path) -> dict | None:
    return wallsettings.read(path)


def _context(env: Mapping[str, str], now: datetime) -> tuple[Ctx, list[policy.Class], list[policy.Class]]:
    """(the context, the walled classes that are set up, those that are not): a class is set up when its OS user is in the
    system's user database and its file is in place. Nothing from `env` chooses a user or a file."""
    pol_file = policy.policy_path(dict(env))
    try:
        pol = policy.load(pol_file)
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), f"fix {pol_file}, or run `ws walls edit`") from e
    kind = osinfo.detect().kind
    ready, missing = [], []
    for cls in wallsettings.class_users(pol):
        has_user = wallsettings.passwd(cls.os_user or "") is not None
        if has_user and wallsettings.launch_file_found(cls.name, kind) is not None:
            ready.append(cls)
        else:
            missing.append(cls)
    docs = {c.name: _read_json(wallsettings.launch_file_path(c.name, kind)) or {} for c in ready}
    managed = {c.name: _read_json(found) or {} for c in ready if (found := wallsettings.class_file_found(c.name))}
    base = _read_json(paths.state_dir(env) / "gate" / "claude-code" / "settings.json")
    return Ctx(pol, kind, env, now, ready, docs, base, managed), ready, missing


def run_cases(ctx: Ctx, cases: list[Case] | None = None) -> list[dict]:
    """Each case as {name, status (pass, fail or skip), setting, detail}. A host case is skipped while no class is set up."""
    out = []
    for case in CASES if cases is None else cases:
        if case.host and not ctx.classes:
            status, detail = "skip", "needs a class user and its class file, which are not in place"
        else:
            try:
                ok, detail = case.check(ctx)
            except Exception as e:  # noqa: BLE001 - a case that cannot run has not passed
                ok, detail = False, f"the case could not run ({type(e).__name__})"
            status = "pass" if ok else "fail"
        out.append({"name": case.name, "status": status, "setting": case.setting, "detail": detail})
    return out


def setup_lines(pol: policy.Policy, missing: list[policy.Class], kind: str, env: Mapping[str, str]) -> list[str]:
    """The commands that set a class up and the way to remove it, as text (ws runs none of it)."""
    if not wallsettings.class_users(pol):
        return [
            "no class with an OS user is in your policy yet. Walled classes are optional (Linux or macOS, a second account).",
            "To add one, print a table to read: `ws walls init NAME` (`--write` appends it to your policy).",
        ]
    lines: list[str] = []
    for cls in missing:
        lines += [
            (
                f"class {cls.name}: its account or its class file is not in place. Print the commands with "
                f"`ws install --class {cls.name}`, run them yourself, then run `ws walls check` again."
            ),
            f"  to take class {cls.name} out again:",
            *wallsettings.removal_text(cls, kind),
        ]
    return lines


def _print_cases(cases: list[dict]) -> None:
    for c in cases:
        print(f"{c['status']:<5} {c['name']}  {c['detail']}")


def cmd_check(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    ctx, _, missing = _context(env, _now())
    cases = run_cases(ctx)
    failed = [c for c in cases if c["status"] == "fail"]
    setup = setup_lines(ctx.pol, missing, ctx.kind, env) if (missing or not wallsettings.class_users(ctx.pol)) else []
    data = {"set_up": not setup, "cases": cases, "setup": setup}
    if not setup and not failed:
        if args.json:
            output.result(data)
        else:
            _print_cases(cases)
            print(f"ws: {len(cases)} cases hold")
        return 0
    if not args.json:
        _print_cases(cases)
        for line in setup:
            print(line)
    if setup:
        first = missing[0].name if missing else None
        nxt = f"ws install --class {first}" if first else "ws walls init NAME"
        raise WsError(
            "walls_not_set_up",
            "the host is not set up for a walled class" + ("" if not failed else f", and {len(failed)} case(s) fail"),
            f"run `{nxt}`, follow what it prints, then run `ws walls check` again",
            1,
            data,
        )
    names = ", ".join(c["name"] for c in failed)
    raise WsError(
        "walls_check_failed",
        f"{len(failed)} case(s) fail: {names}",
        "the detail of each is above; fix it and run `ws walls check` again (`ws walls check --json` has them all)",
        1,
        data,
    )


# ---- the canary: every case on a planted world -----------------------------------------------------------------------------

# What `ws walls canary` proves, each item with the case that proves it. A case fails when the setting it names is removed
# (tests/test_walls.py removes each one). Two items share the tracer: G4 asks for the planted token over every shared sink.
COVERS: dict[str, str] = {
    "inbound message from outside the class": "inbound-refused",
    "headless job writes outside its workspace": "headless-job-in-wall",
    "canary token reaches no shared sink": "tracer-stays-in-class",
    "push to a non-class remote": "class-push-refused",
    "G3 message bodies stay in the class": "bodies-in-class-store",
    "G4 a token over every shared sink": "tracer-stays-in-class",
    "G5 tool-filled cards": "walled-sends-fixed-kinds",
    "G6 messaging tools denied": "messaging-denied",
    "G7 provider allowlist": "provider-allowlist",
    "G8 class-gated memory and notes": "memory-notes-refused",
    "G10 no counts but buckets": "walled-card-no-counts",
    "G11 Remote Control and connectors off": "remote-and-connectors-off",
    "G14 OpenTelemetry content off": "otel-off",
    "G16 owner and review date": "owner-and-review-date",
    "C360 the launch file's write block and token deny": "launch-write-block-and-token-deny",
    "rr-M3 a local-model session": "local-model-in-wall",
    "a session outside the area reads or edits nothing sealed": "sealed-read-refused",
}
# Cases that read the host itself (a file's owner and mode on disk): `ws walls check` runs them, the canary can't.
HOST_ONLY = ("class-file-root-owned",)
CANARY_CLASSES = ("canary", "canary-other", "canary-local")  # names of the planted classes, none of them anyone's


def canary_policy(root: Path, now: datetime) -> str:
    """The planted policy: three walled classes with their own (nonexistent) accounts and repos under `root`, an owner and
    a review date; the first names a provider and a remote host, the last is `local_only` with a loopback model host."""
    review = (now.astimezone(UTC) + timedelta(days=30)).date().isoformat()
    extra = {
        "canary": 'allowed_providers = ["anthropic"]\nallowed_hosts = ["git.example.com"]\n',
        "canary-other": "",
        "canary-local": 'local_only = true\nallowed_hosts = ["localhost:11434"]\n',
    }
    return "".join(
        f'[classes.{name}]\naccess = "walled"\nos_user = "ws-{name}"\nrepos = [{json.dumps(f"{root}/{name}/**")}]\n'
        f'inbound = "lead-only"\nowner = "canary"\nreview_date = {review}\n{extra[name]}\n'
        for name in CANARY_CLASSES
    )


@contextlib.contextmanager
def canary_context(env: Mapping[str, str], now: datetime) -> Iterator[Ctx]:
    """A context for every case on a world planted under ws's probe folder: the canary policy, each class's managed and
    launch files as `ws install --class` renders them, and the generated settings `ws install` writes. No account, no
    file under /etc and no real store is read or written. Removed after."""
    from ws import selfcmd
    from ws.adapters import claude_code

    kind = osinfo.detect().kind
    with _probe_folder(env) as root:
        pol_file = root / "policy.toml"
        pol_file.write_text(canary_policy(root, now), encoding="utf-8")
        pol = policy.load(pol_file)
        classes = wallsettings.class_users(pol)
        docs, managed = {}, {}
        for cls in classes:
            home = wallsettings.class_home(cls, kind)
            docs[cls.name] = wallsettings.launch_settings(cls, home, kind, pol, env)
            managed[cls.name] = wallsettings.class_settings(cls, home, kind)
        files = claude_code.generate(pol, selfcmd.argv(), root / "gate", pol_file)
        yield Ctx(pol, kind, env, now, classes, docs, json.loads(files[claude_code.FILE]), managed)


def canary_cases() -> list[Case]:
    return [c for c in CASES if c.name not in HOST_ONLY]


def cmd_canary(args: argparse.Namespace) -> int:
    env = dict(os.environ)
    with canary_context(env, _now()) as ctx:
        cases = run_cases(ctx, canary_cases())
    status = {c["name"]: c["status"] for c in cases}
    covers = [{"item": item, "case": name, "status": status.get(name, "missing")} for item, name in COVERS.items()]
    failed = [c for c in cases if c["status"] != "pass"]
    data = {"cases": cases, "covers": covers}
    if args.json:
        if not failed:
            output.result(data)
    else:
        _print_cases(cases)
        for c in covers:
            print(f"{c['status']:<5} covers  {c['item']}  ({c['case']})")
    if failed:
        names = ", ".join(c["name"] for c in failed)
        raise WsError(
            "walls_canary_failed",
            f"{len(failed)} case(s) fail on the planted world: {names}",
            "a setting the wall writes is missing; the detail of each is above (`ws walls canary --json` has them all)",
            1,
            data,
        )
    if not args.json:
        print(f"ws: {len(cases)} cases hold on a planted world, covering {len(covers)} items")
    return 0


# ---- init and edit -------------------------------------------------------------------------------------------------------


def class_table(name: str, kind: str, now: datetime, owner: str) -> str:
    """The `[classes.NAME]` table `ws walls init` prints: a walled class with its own account, nothing coming in, an owner
    and a review date. It sets no dial."""
    repos = f"/Users/Shared/work/{name}/**" if kind == "macos" else f"/srv/work/{name}/**"
    review = (now.astimezone(UTC) + timedelta(days=REVIEW_DAYS)).date().isoformat()
    return "\n".join(
        [
            f"[classes.{name}]",
            'access = "walled"',
            f'os_user = "ws-{name}"',
            f"repos = [{json.dumps(repos)}]",
            'inbound = "off"      # nobody can send a message in; lead-only or allowlist are for you to choose',
            f"owner = {json.dumps(owner)}",
            f"review_date = {review}   # `ws doctor` (WS017) warns from this day",
            "",
        ]
    )


def _owner_default() -> str:
    from ws import sandbox

    return sandbox.real_user() or "owner"


def _check_name(name: str) -> None:
    if not policy.NAME_PATTERN.fullmatch(name) or name == policy.CLASS_RESERVED:
        raise WsError(
            "usage",
            f"{name!r} is not a usable class name",
            "use letters, digits, _ and -, for example `ws walls init alpha`",
            2,
        )


def _stamp(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def read_policy(path: Path) -> bytes | None:
    """The policy file's bytes, or None when there is no file. A link is refused as `userconfig.load` refuses one (ws
    writes to the file itself, never through a link), and a file that is not a plain UTF-8 file or cannot be read is a
    typed error, never a traceback."""
    if path.is_symlink():
        raise WsError(
            "refused",
            f"{path} is a link; ws changed nothing",
            "replace the link with the file it points to (or point ws at the real folder), then run the command again",
        )
    if not os.path.lexists(path):
        return None
    try:
        if not path.is_file():
            raise WsError("refused", f"{path} is not a plain file; ws changed nothing", "fix it, then run it again")
        raw = path.read_bytes()
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise WsError("bad_config", f"{path} is not UTF-8 text; ws changed nothing", f"fix or remove {path}") from e
    except OSError as e:
        raise WsError("io", f"could not read {path}: {e.strerror}; ws changed nothing", "check its permissions") from e
    return raw


def _backup(path: Path, data: bytes, now: datetime) -> Path:
    """Write `data` to `policy.toml.bak-STAMP` beside the policy (0600). The name is made with O_EXCL and a counter
    (`-1`, `-2`) when it is taken, so a second backup in the same second never overwrites the first."""
    stem = f"{path.name}.bak-{_stamp(now)}"
    for n in range(1000):
        target = path.with_name(stem if n == 0 else f"{stem}-{n}")
        try:
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                target.unlink()
            raise
        return target
    raise OSError(f"no free backup name beside {path}")


def _changed(path: Path) -> WsError:
    return WsError(
        "conflict",
        f"{path} changed while ws was working on it; ws changed nothing",
        "look at what changed, then run the command again",
    )


def replace_policy(path: Path, text: str, now: datetime, based_on: bytes | None) -> Path | None:
    """Replace the policy file with `text`, if it is still the bytes `based_on` (what the caller read and built `text`
    from; None: there was no file). The whole of `text` is validated first (a PolicyError is a refusal and nothing is
    touched). Under the lock `ws install` takes on a file it writes (`userconfig.locked`) it refuses a link, refuses with a
    typed `conflict` error when the file is no longer `based_on`, copies it to `policy.toml.bak-STAMP` (0600, a counter on
    a clash), then replaces it atomically. Returns the backup, or None when there was no file. The one write path of
    `init --write` and `edit`."""
    try:
        policy.loads(text)
    except policy.PolicyError as e:
        raise WsError("bad_config", f"{path}: {e}", "the file was not changed", 1) from e
    backup = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with userconfig.locked(path):
            if read_policy(path) != based_on:
                raise _changed(path)
            if based_on is not None:
                try:
                    backup = _backup(path, based_on, now)
                except OSError as e:
                    raise WsError("io", f"could not back up {path}: {e}", "the file was not changed") from e
            try:
                userconfig.replace(path, text.encode("utf-8"), based_on, 0o600 if backup is None else None)
            except userconfig.Changed:
                raise _changed(path) from None
            except OSError as e:
                raise WsError("io", f"could not write {path}: {e}", "the file was not changed") from e
    except WsError:
        if backup is not None:  # nothing was replaced, so the copy says nothing
            with contextlib.suppress(OSError):
                backup.unlink()
        raise
    except OSError as e:
        raise WsError("io", f"could not write {path}: {e}", "the file was not changed") from e
    return backup


def cmd_init(args: argparse.Namespace, now: datetime | None = None) -> int:
    now = now or _now()
    _check_name(args.name)
    env = dict(os.environ)
    pol_file = policy.policy_path(env)
    kind = osinfo.detect().kind
    table = class_table(args.name, kind, now, _owner_default())
    try:
        current = policy.load(pol_file)
    except policy.PolicyError as e:
        raise WsError("bad_config", str(e), f"fix {pol_file}, or run `ws walls edit`") from e
    if args.name in current.classes:
        raise WsError("usage", f"the policy already has a class named {args.name!r}", "pick another name", 2)
    backup = None
    if args.write:
        raw = read_policy(pol_file)
        base = raw.decode("utf-8") if raw is not None else policy.default_toml()
        backup = replace_policy(pol_file, base.rstrip("\n") + "\n\n" + table, now, raw)
    data = {
        "class": args.name,
        "table": table,
        "written": bool(args.write),
        "policy": str(pol_file),
        "backup": str(backup) if backup else None,
    }
    if args.json:
        output.result(data)
        return 0
    print(table)
    if args.write:
        print(f"ws: added [classes.{args.name}] to {pol_file}" + (f" (the old file is at {backup})" if backup else ""))
    else:
        print(f"ws: nothing written. `ws walls init --write {args.name}` appends it to {pol_file}.")
    print(f"next: `ws install --class {args.name}` prints the account and the class file to set up yourself.")
    return 0


def _editor() -> list[str]:
    cmd = os.environ.get("VISUAL") or os.environ.get("EDITOR")
    if not cmd:
        raise WsError("usage", "no editor is set", "set EDITOR (or VISUAL), for example `EDITOR=nano ws walls edit`", 2)
    return shlex.split(cmd)


def cmd_edit(args: argparse.Namespace, now: datetime | None = None) -> int:
    """Edit the policy on a copy. The copy is removed on every path but one: an edit that is refused (invalid, or the file
    changed meanwhile) keeps it, and the error's hint names it so the work is not lost."""
    now = now or _now()
    env = dict(os.environ)
    pol_file = policy.policy_path(env)
    editor = _editor()
    raw = read_policy(pol_file)
    original = raw.decode("utf-8") if raw is not None else policy.default_toml()
    work = Path(tempfile.mkdtemp(prefix="ws-walls-edit-"))
    copy = work / "policy.toml"
    keep = False
    try:
        copy.write_text(original, encoding="utf-8")
        copy.chmod(0o600)
        try:
            code = subprocess.run([*editor, str(copy)], check=False).returncode
        except OSError as e:
            raise WsError("io", f"could not start the editor: {e}", "set EDITOR to a program that exists", 2) from e
        if code != 0:
            raise WsError("failed", f"the editor exited {code}", f"{pol_file} was not changed")
        try:
            edited = copy.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            keep = True
            raise WsError(
                "bad_config",
                f"the edited copy could not be read as UTF-8 text ({e})",
                f"{pol_file} was not changed; the copy is at {copy}",
            ) from e
        if edited == original:
            print(f"ws: no change to {pol_file}")
            return 0
        try:
            backup = replace_policy(pol_file, edited, now, raw)
        except WsError as e:  # not applied: the person's edit stays on disk and the error says where
            keep = True
            raise WsError(e.kind, e.message, f"{e.hint}; your edit is kept at {copy}", e.exit_code, e.details) from e
        print(f"ws: {pol_file} updated and checked" + (f" (the old file is at {backup})" if backup else ""))
        return 0
    finally:
        if not keep:
            shutil.rmtree(work, ignore_errors=True)


# ---- the command ---------------------------------------------------------------------------------------------------------


def register(sub) -> None:
    w = sub.add_parser(
        "walls",
        help="walled data classes as an OS boundary: print a class table, edit the policy, check it is in place",
    )
    ws_ = w.add_subparsers(dest="walls_command", required=True)

    i = ws_.add_parser(
        "init", help="print a [classes.NAME] table for a walled class (--write appends it to your policy)"
    )
    i.add_argument("name", help="the class name: letters, digits, _ and -")
    i.add_argument(
        "--write", action="store_true", help="append it to your policy file (backed up first, validated whole)"
    )
    i.set_defaults(func=cmd_init)

    e = ws_.add_parser(
        "edit", help="edit your policy in $EDITOR on a copy; an invalid file is refused and nothing changes"
    )
    e.set_defaults(func=cmd_edit, raw=True)

    c = ws_.add_parser(
        "check",
        help="check the walled classes' settings are in place, without sudo (exit 1: not set up, or a case fails)",
    )
    c.set_defaults(func=cmd_check)

    k = ws_.add_parser(
        "canary",
        help="run every case on a planted world, no account or root file needed (exit 1: a setting the wall writes is missing)",
    )
    k.set_defaults(func=cmd_canary)
