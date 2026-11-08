"""OpenCode: an `opencode.json` `permission` block (the runner's own rules, the primary layer) plus a shim plugin that
runs `ws gate check` on every governed tool call and throws on a deny (the second layer; it also fails closed: any
error in the shim blocks the call).

The permission block holds every kind of refusal the policy has: `deny_commands` (with the patterns the guards that are
on contribute), `deny_read`, `deny_tools` and the protected paths as `deny`, and `ask_commands` as `ask`. OpenCode takes
the last rule that matches, so each block runs allow-all, then asks, then denies, and a deny beats an ask on the same
command. The shim runs the guards on OpenCode's own events (see EVENT_NAMES) through the thin client, with the check
before a call the only one that can block.

Also denied: edits of the policy file and of the generated folder, so a session cannot loosen its own gate, and of the
skills folders OpenCode reads (its own, and the ones it shares with other runners), because a skill is read as
instructions. The home-level folders are denied by absolute path and as OpenCode sees them from a project (`../...`).
The project's own `.opencode/skills` is denied too; its `.claude/skills` and `.agents/skills` are the project's own.

Point OpenCode at the output so its global config is never touched, and so a project's own `opencode.json` cannot
loosen the gate: the inline config (OPENCODE_CONFIG_CONTENT) is read after the global, OPENCODE_CONFIG and project
files, so it wins.

    OPENCODE_CONFIG_CONTENT="$(cat OUT/opencode.json)"  OPENCODE_CONFIG_DIR=OUT  opencode

OPENCODE_CONFIG_DIR is searched for `plugins/`, which is where the shim sits.

The wall is `ws gate launch --runner opencode -- opencode ...` (ws.sandbox: bubblewrap on Linux, sandbox-exec on macOS),
because OpenCode has no sandbox of its own. The launch hint says so, and launch puts WS_GATED_SESSION in the
environment so `ws approve` knows it is being run from a gated session. The whole generated folder is read-only in the
wall; see IGNORE below for what that needs.
"""

from __future__ import annotations

import dataclasses
import json
import math
import re
import shlex
from pathlib import Path

from ws import guards, jobs, paths
from ws.adapters import Call, check_argv, client_argv, event_argv, protected_paths
from ws.adapters.opencode_plugin import SOURCE
from ws.output import WsError
from ws.policy import Policy, native_patterns

NAME = "opencode"
CONFIG = "opencode.json"
PLUGIN = "plugins/ws-gate.js"
_TOOLS = {"web_fetch": "webfetch", "web_search": "websearch"}
# For ws.sandbox: the env var that names the generated folder. The whole folder is read-only in the wall (OpenCode loads
# plugin/, tool/, agent/, command/ and opencode.jsonc from it, so a planted file there would outlive the session).
CONFIG_DIR_ENV = "OPENCODE_CONFIG_DIR"
ASKS_SCHEMA = 1  # the `schema` of each line the shim appends to the asks file (docs/reference/stores.md)

# OpenCode's plugin hooks and events, as ws.guards names them (checked against the 1.19.4 binary: tool.execute.before,
# tool.execute.after, chat.message and session.created are plugin hooks or events it fires, and session.idle is the event
# that ends a turn). OpenCode has one idle event for a root session and a subagent's alike; the shim tells them apart
# (a child session has a parent) and sends the second as "session.idle.child", ws's own name, never one OpenCode sends.
EVENT_NAMES = {
    "tool.execute.before": "pre",
    "tool.execute.after": "post",
    "session.created": "start",
    "chat.message": "prompt",
    "session.idle": "stop",
    "session.idle.child": "subagent-stop",
}
# The tools a Call kind comes from, in the order a list of them is written. OpenCode has no send-message tool, so no
# "message" call ever comes from here: a guard that reads only those has nothing to see on this runner.
KIND_TOOLS = {
    "command": ("bash",),
    "read": ("read",),
    "search": ("grep", "glob", "list"),
    "write": ("edit", "write", "multiedit", "patch", "apply_patch"),
    "tool": tuple(_TOOLS.values()),
    "agent": ("task",),
    "skill": ("skill",),
}
PRODUCES = tuple(KIND_TOOLS)  # the Call kinds `parse` can make
_ALWAYS_PRE = (
    "command",
    "read",
    "search",
    "write",
    "tool",
)  # checked before every call; the rest only when a guard reads them
# OpenCode's `policies` key (checked on 1.19.4): the only one there is `experimental.policies`, which governs provider
# access, not tool calls. ws writes none until a version's schema has a permission key of that name; set this to that
# version then (docs/reference/gate.md says what was checked).
POLICIES_SINCE: str | None = None


def guard_gaps(guard) -> list[str]:
    """Why this runner cannot give a guard what it reads, one line for each event it cannot reach: the event has no name
    in EVENT_NAMES, or (before and after a call) every Call kind the guard reads is one `parse` never makes."""
    events = set(EVENT_NAMES.values())
    gaps = []
    for event in guard.EVENTS:
        if event not in events:
            gaps.append(f"{event}: OpenCode has no event ws reads for it")
        elif event in ("pre", "post") and guard.KINDS and not set(guard.KINDS) & set(PRODUCES):
            gaps.append(f"{event}: it reads only {', '.join(guard.KINDS)} calls, which OpenCode never makes")
    return gaps


def unsupported_guards() -> tuple[str, ...]:
    """The guards (ws.guards) one of whose events this runner cannot reach, worked out from what each guard declares
    against EVENT_NAMES and the kinds `parse` makes, so a new guard needs no edit here."""
    return tuple(name for name, guard in guards.all_guards().items() if guard_gaps(guard))


# The guards the generated shim cannot run. `ws doctor` names them when a policy has one on.
UNSUPPORTED_GUARDS = unsupported_guards()


# OpenCode writes this file into its config folder at start and will not run if it cannot (1.19.4). Written here at
# generate time, with the content OpenCode writes, so the folder can stay read-only; it needs no node_modules.
def wall_writable(env: dict) -> list[str]:
    """The folders OpenCode itself writes (its database and logs, key-value state, model and package cache), which the
    wall leaves writable besides the project: the `opencode` folder under XDG data, state and cache."""
    home = env.get("HOME") or str(Path.home())
    return [
        f"{env.get(var) or f'{home}/{default}'}/opencode"
        for var, default in (
            ("XDG_DATA_HOME", ".local/share"),
            ("XDG_STATE_HOME", ".local/state"),
            ("XDG_CACHE_HOME", ".cache"),
        )
    ]


def wall_prepare(env: dict) -> None:
    """What OpenCode makes at start in its global config folder, and fails (EROFS) if it cannot: the folder and its
    `.gitignore` (it keeps one in every config folder). Done by the launcher, outside the wall, where the folder is
    writable; inside it stays read-only, so a session cannot plant a global plugin."""
    home = env.get("HOME") or str(Path.home())
    folder = Path(f"{env.get('XDG_CONFIG_HOME') or f'{home}/.config'}/opencode")
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / IGNORE
    if not ignore.exists():
        ignore.write_text(IGNORE_TEXT, encoding="utf-8")


IGNORE = ".gitignore"
IGNORE_TEXT = "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore"
_NEUTRAL = {v: k for k, v in _TOOLS.items()}

# Where this runner keeps what ws installs globally, and where a repo may shadow it (read by ws.globals).
# Both the older singular and the newer plural folder names are listed. kinds: see adapters/claude_code.py.
GLOBALS = {
    "user_dir": "~/.config/opencode",
    "xdg": "opencode",
    "project_dir": ".opencode",
    "kinds": {
        "skills": "dir",
        "skill": "dir",
        "agents": "name",
        "agent": "name",
        "commands": "name",
        "command": "name",
        "plugins": "stem",
        "plugin": "stem",
    },
}


def _read_forms(rule: str) -> list[str]:
    """OpenCode matches read and edit rules against the path as it sees it (relative to the project, so a file in
    another folder arrives as `../../x/y`), and an absolute rule never matches that (tried on 1.19.4: `~/.ssh/**`
    and `/abs/.ssh/**` let a read through; `**/.ssh/**` blocks it). So each rule is also given as `**/` plus the
    path below home, or its last 3 segments if it is absolute. That over-blocks a same-named folder elsewhere,
    which is the safe side."""
    parts = rule.removeprefix("~/").lstrip("/").split("/")
    if rule.startswith("/"):
        parts = parts[-3:]
    return list(dict.fromkeys([rule, "**/" + "/".join(parts)]))


def _protect_forms(path: str, is_out: bool) -> list[str]:
    """Edit rules for a protected file or folder, in the same two spellings (see _read_forms). A folder name like
    `jobs` or `logs` is common, so a pattern never drops the parent folder: a ws control path keeps one
    (`.ws/logs/**`), the generated folder two (`.ws/gate/opencode/**`), whatever else is above it."""
    parts = Path(path).parts
    tail = "/".join(parts[-3:] if is_out else parts[-2:])
    return [path, f"{path}/**", f"**/{tail}", f"**/{tail}/**"]


def _skill_forms(path: str, project: Path | None = None) -> list[str]:
    """Edit rules for a skills folder in the person's home or config folder: the absolute path, and how OpenCode sees it
    from a project (`../` first, the last two segments of the path below). A bare `**/.claude/skills` would also catch a
    project's own `.claude/skills`, which is the project's to change, so the relative forms start with `../`: they only
    reach a folder outside the project. When the project is the folder's parent or an ancestor of it (OpenCode run in the
    home folder), the folder is inside the project and is seen as a plain relative path, so that path is denied too, and
    only then."""
    tail = "/".join(Path(path).parts[-2:])
    forms = [path, f"{path}/**", f"../{tail}", f"../{tail}/**", f"../**/{tail}", f"../**/{tail}/**"]
    if project is not None and Path(path).is_relative_to(project) and Path(path) != project:
        rel = Path(path).relative_to(project).as_posix()
        forms += [rel, f"{rel}/**"]
    return forms


# The skills folders of a project that OpenCode reads on its own (`.opencode/skills`, and the older singular name): they
# steer the next session as much as a home-level one does. A project's `.claude/skills` and `.agents/skills` are not on
# the list: they are the project's own, shared with other runners.
PROJECT_SKILL_FOLDERS = (".opencode/skills", ".opencode/skill")


def _project_skill_forms(folder: str) -> list[str]:
    return [folder, f"{folder}/**", f"**/{folder}", f"**/{folder}/**"]


def native_asks(policy: Policy) -> list[str]:
    """The `ask_commands` a runner can enforce from its own rule list (`native_patterns` decides which and in what
    spellings: a pattern only the hook can read is left to the hook), without the patterns the guards add."""
    gate = dataclasses.replace(policy.gate, deny_commands=policy.gate.ask_commands)
    return native_patterns(dataclasses.replace(policy, gate=gate, guards=dataclasses.replace(policy.guards, on=())))


def skill_folders(pin: paths.Pin) -> list[str]:
    """The skills folders OpenCode reads, as absolute paths: its own (both spellings of the folder name) and the ones
    it shares with other runners (`ws.skills.ALSO_READS`)."""
    from ws import adapters, install, skills  # here, not at the top: ws.skills imports the adapters

    env = {**paths.environ(), "HOME": str(pin.home)}
    own = skills.skills_dir(NAME, env)
    found = [own, own.with_name("skill")]
    for where in skills.ALSO_READS.get(NAME, ()):
        found.append(skills.skills_dir(where, env) if where in adapters.all_adapters() else install.expand(where, env))
    return list(dict.fromkeys(str(p) for p in found))


def asks_file(pin: paths.Pin) -> str:
    """The file the shim appends OpenCode's open permission asks to, for ws home to show: `[home.sources] asks_jsonl` in
    the profile, else the default under the home folder. A profile that does not read is no reason to fail a generate."""
    from ws.home import api

    try:
        env = api.Env.make({**paths.environ(), "HOME": str(pin.home)})
        return str(env.path("asks_jsonl"))
    except WsError:
        return str(pin.home / ".claude-usage" / "asks" / "asks.jsonl")


def _tools_of(kinds) -> list[str]:
    return [t for kind in KIND_TOOLS if kind in kinds for t in KIND_TOOLS[kind]]


def generate(
    policy: Policy,
    ws_cmd: list[str],
    out: Path,
    policy_file: Path,
    *,
    protect: tuple[str, ...] = (),
    pin: paths.Pin | None = None,
    project: Path | None = None,
) -> dict[str, str]:
    protect = tuple(protect)
    pin = pin or paths.pin_of()
    project = Path(project) if project is not None else Path.cwd()  # the folder OpenCode will be started in
    # Last matching rule wins, so the order is the catch-all, then the asks, then the denies: a deny beats an ask.
    denies = native_patterns(policy)
    asks = [p for p in native_asks(policy) if p not in denies]  # one asked and denied is denied
    bash: dict[str, str] = {"*": "allow", **dict.fromkeys(asks, "ask"), **dict.fromkeys(denies, "deny")}
    permission: dict = {"bash": bash}
    if policy.gate.deny_read:
        rules = {"*": "allow", **dict.fromkeys((f for r in policy.gate.deny_read for f in _read_forms(r)), "deny")}
        permission.update(read=rules, grep=dict(rules), glob=dict(rules), list=dict(rules))
    edit = {"*": "allow"}  # covers write and patch too; a path may be a file or a folder: cover both
    for p in protected_paths(out, policy_file, protect):
        edit.update(dict.fromkeys(_protect_forms(p, p == str(out)), "deny"))
    for p in skill_folders(pin):  # a skill is read as instructions: a planted one would steer the next session
        edit.update(dict.fromkeys(_skill_forms(p, project), "deny"))
    for folder in PROJECT_SKILL_FOLDERS:
        edit.update(dict.fromkeys(_project_skill_forms(folder), "deny"))
    permission["edit"] = edit
    for t in policy.gate.deny_tools:
        permission[_TOOLS[t]] = "deny"
    config: dict = {"$schema": "https://opencode.ai/config.json", "permission": permission}
    if (
        POLICIES_SINCE is not None
    ):  # a version whose schema has the key: ws writes it empty until it has a rule to put in
        config["policies"] = {}
    on = policy.guards.on
    pre_kinds = set(_ALWAYS_PRE) | (guards.kinds_for(on, "pre") & set(PRODUCES))
    post_kinds = guards.kinds_for(on, "post") & set(PRODUCES)
    events = [ev for ev in ("start", "prompt", "post", "stop", "subagent-stop") if guards.wanted(on, ev)]
    check = client_argv("check", check_argv(ws_cmd, NAME, policy_file, out, protect, pin), pin)
    event = client_argv("event", event_argv(ws_cmd, NAME, policy_file, pin), pin)
    plugin = SOURCE.replace("/*__CHECK__*/ []", json.dumps(check)).replace("/*__EVENT__*/ []", json.dumps(event))
    plugin = plugin.replace("/*__WS__*/ []", json.dumps(ws_cmd)).replace("/*__PIN__*/ []", json.dumps(pin.args()))
    plugin = plugin.replace("/*__GOVERNED__*/ []", json.dumps(_tools_of(pre_kinds)))
    plugin = plugin.replace("/*__POST_TOOLS__*/ []", json.dumps(_tools_of(post_kinds)))
    plugin = plugin.replace("/*__ADVICE__*/ []", json.dumps([e for e in events if e != "post" or post_kinds]))
    plugin = plugin.replace("/*__ASK__*/ []", json.dumps(asks)).replace('/*__ASKS__*/ ""', json.dumps(asks_file(pin)))
    plugin = plugin.replace("/*__ASKS_SCHEMA__*/ 0", str(ASKS_SCHEMA))
    return {CONFIG: json.dumps(config, indent=2) + "\n", PLUGIN: plugin, IGNORE: IGNORE_TEXT}


_PATCH_PATH = re.compile(r"^\*\*\* (?:Add File|Update File|Delete File|Move to):[ \t]*(\S.*?)[ \t\r]*$", re.MULTILINE)


def patch_paths(patch: str) -> list[str]:
    """The files a patch (OpenCode's `*** Begin Patch` text) adds, updates, deletes or moves to, in order. A patch
    that names none cannot be judged, so it raises: the call is denied."""
    paths = list(dict.fromkeys(_PATCH_PATH.findall(patch)))
    if not paths:
        raise TypeError("args.patchText names no file (expected `*** Update File: path` lines)")
    return paths


def parse(payload: dict) -> Call | list[Call] | None:
    """What the shim sends: {"tool": "bash", "args": {"command": ...}}. A patch tool is a write of each file in the
    patch; `task` is an agent call and `skill` a skill call, as Claude Code's `Agent` and `Skill` are. The kinds this can
    make are KIND_TOOLS. A tool it does not know (an MCP tool among them) is None."""
    tool, args = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise TypeError("payload has no tool and args")
    if tool == "bash":
        return Call("command", _text(args, "command"))
    if tool == "read":
        return Call("read", _text(args, "filePath"))
    if tool in ("edit", "write", "multiedit"):
        return Call.of("write", _text(args, "filePath"), tool=tool, **_write_data(tool, args))
    if tool == "task":
        found = {k: args[k] for k in ("prompt", "description", "subagent_type") if isinstance(args.get(k), str)}
        return Call.of("agent", found.get("subagent_type", ""), tool=tool, **found)
    if tool == "skill":
        name = args["name"] if isinstance(args.get("name"), str) else ""
        return Call.of("skill", name, tool=tool, name=name, args="")
    if tool in ("patch", "apply_patch"):
        kept = payload.get(
            "paths"
        )  # only a line record-calls kept has these (the shim sends none): text -> fingerprint
        if isinstance(args.get("patchText"), dict) and isinstance(kept, list) and kept:
            if not all(isinstance(p, str) and p for p in kept):
                raise TypeError("paths must be non-empty text")
            return [Call.of("write", p, tool=tool) for p in kept]
        return [Call.of("write", p, tool=tool) for p in patch_paths(_text(args, "patchText"))]
    if tool in ("grep", "glob", "list"):
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise TypeError("args.path must be text")
        return Call("search", path or ".")
    if tool in _NEUTRAL:
        return Call("tool", _NEUTRAL[tool])
    return None


def _write_data(tool: str, args: dict) -> dict:
    """What a write tool gives besides the path, under the names `ws.adapters.Call` documents: `old` and `new` (edit),
    `content` (write), `edits` (multiedit, a tuple of {old, new}). Only text the tool really sent."""
    found: dict = {}
    for key, name in (("oldString", "old"), ("newString", "new"), ("content", "content")):
        if isinstance(args.get(key), str):
            found[name] = args[key]
    edits = args.get("edits")
    if tool == "multiedit" and isinstance(edits, list):
        found["edits"] = tuple(
            {
                k: e[key] if isinstance(e.get(key), str) else None
                for k, key in (("old", "oldString"), ("new", "newString"))
            }
            for e in edits
            if isinstance(e, dict)
        )  # a recorded call (record-calls) holds a size and fingerprint where the text was: None here
    return found


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"args.{key} is missing")
    return v


def launch_hint(out: str) -> str:
    q = shlex.quote(out)
    return (
        f'start OpenCode with: OPENCODE_CONFIG_CONTENT="$(cat {q}/{CONFIG})" OPENCODE_CONFIG_DIR={q} '
        "ws gate launch --runner opencode -- opencode"
    )


# --- ws run: the command line, and the event stream read back as a run record --------------------------------------

EXE = "opencode"
PLACEHOLDER_KEY = "ws-lease-placeholder"  # the runner holds this; the broker swaps in the real key on the way out
OPTIONS = {"exe", "env", "provider", "base_path", "offline"}
PROMPT_VIA_STDIN = True  # `opencode run` reads the message from stdin when none is given (checked on 1.19.4)


def priced(job) -> bool:
    """Can OpenCode price this job's model? Its built-in providers come with prices; a provider block in the job
    does only if every model in it carries a `cost`. Otherwise OpenCode reports 0, which is not free."""
    block = job.runner_options.get("provider")
    if not isinstance(block, dict) or not block:
        return True
    models = block.get("models")
    return (
        isinstance(models, dict) and bool(models) and all(isinstance(m, dict) and "cost" in m for m in models.values())
    )


def build_command(job, run_dir: Path, lease) -> tuple[list[str], dict[str, str]]:
    """(argv, env) for `opencode run --format json`. The generated gate config in run_dir/gate is passed as the inline
    config (OPENCODE_CONFIG_CONTENT, which beats a project's own opencode.json) with OPENCODE_CONFIG_DIR as the plugin
    folder. The lease (if any) becomes the provider's baseURL plus runner_options.base_path (default /v1), with a
    placeholder key, added to that inline config. runner_options.provider is merged into the provider block, for a
    provider OpenCode does not know (npm, name, models). Raises ValueError naming the key that is wrong."""
    jobs.check_options(job, OPTIONS)
    exe = jobs.option(job, "exe", str, EXE)
    provider_id, slash, _ = job.model.partition("/")
    if not slash or not provider_id:
        raise ValueError("model must be provider/model for opencode, e.g. anthropic/claude-haiku-4-5")
    if job.max_turns is not None:
        raise ValueError("max_turns: opencode run has no turn limit; remove it or use claude-code")
    extra = jobs.option(job, "provider", dict, {})
    base_path = jobs.option(job, "base_path", str, "/v1")
    offline = jobs.option(job, "offline", bool, False)
    out = Path(run_dir) / "gate"
    argv = [exe, "run", "--model", job.model, "--format", "json"]
    env = jobs.extra_env(job)  # first: what ws sets below always wins
    env.update(OPENCODE_CONFIG_DIR=str(out), OPENCODE_DISABLE_AUTOUPDATE="true")
    if offline:  # no model-list fetch or language-server download: for a stub or an air-gapped machine
        env.update(OPENCODE_DISABLE_MODELS_FETCH="true", OPENCODE_DISABLE_LSP_DOWNLOAD="true")
    gate_file = out / CONFIG
    config = json.loads(gate_file.read_text(encoding="utf-8")) if gate_file.is_file() else {}
    if lease is not None or extra:
        block = json.loads(json.dumps(extra))  # a copy: the job's own table is not touched
        options = block.setdefault("options", {})
        if not isinstance(options, dict):
            raise ValueError("runner_options.provider.options must be a table")
        if lease is not None:
            options.update(baseURL=lease.base_url + base_path, apiKey=PLACEHOLDER_KEY)
        config["provider"] = {**(config.get("provider") or {}), provider_id: block}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return argv, env


def parse_result(stdout: str, stderr: str, exit_code: int | None) -> dict:
    """The part of a run record that the `--format json` event stream can fill. Never raises. The stream does not
    name the model, so `models_used` has one entry with model None for the caller to fill with the requested model."""
    session = error = None
    steps = 0
    sums = {"input": None, "output": None, "cache_creation": None, "cache_read": None}
    cost = None

    def add(key: str, v) -> None:
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            sums[key] = (sums[key] or 0) + v

    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if session is None and isinstance(ev.get("sessionID"), str):
            session = ev["sessionID"]
        if ev.get("type") == "error":
            e = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            error = str(data.get("message") or e.get("name") or "opencode reported an error")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if ev.get("type") == "step_finish":
            steps += 1
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            add("input", tok.get("input"))
            add("output", tok.get("output"))
            add("cache_creation", cache.get("write"))
            add("cache_read", cache.get("read"))
            c = part.get("cost")
            if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and c >= 0:
                cost = (cost or 0.0) + c
    counted = any(v is not None for v in sums.values())
    # OpenCode reports cost 0 for a model it has no price for, so a zero beside real tokens is "unknown", not free.
    unpriced = counted and not cost
    if not steps:
        cost = None
    return {
        "session_id": session,
        "models_used": [{"model": None, **sums, "cost_usd": None if unpriced else cost}] if steps else [],
        "tokens": sums,
        "cost_usd": None if unpriced or not steps else cost,
        "cost_basis": None if not steps else "unpriced" if unpriced else "runner",
        "num_turns": steps if steps else None,
        "error": error,
    }


def transcript_path(session_id: str | None, cwd: str, env: dict[str, str]) -> str | None:
    """OpenCode 1.19 keeps every session in one SQLite file, `<XDG_DATA_HOME or ~/.local/share>/opencode/opencode.db`;
    the session id says which rows are this run's. None when the file is not there."""
    if not session_id:
        return None
    data = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")) / ".local" / "share")
    db = Path(data).expanduser() / "opencode" / "opencode.db"
    return str(db) if db.is_file() else None
