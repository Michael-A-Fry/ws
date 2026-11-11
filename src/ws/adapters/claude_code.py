"""Claude Code: a settings file with `permissions.deny` (the runner's own rules, the primary layer) plus a PreToolUse
hook that asks the gate (the second layer). `claude --bare` skips hooks, so the deny rules must stand alone. The hook
command is the thin client (ws.gateclient), which asks the resident gate (`ws gate serve`) and, when that can't answer,
runs `ws gate check` itself. It ends `|| exit 2` so a missing or crashing ws blocks the call instead of letting it
through, and the hook sets `onFailure: "block"` (Claude Code 2.1.295 and newer; `ws doctor` WS011 checks the version), so
a crash or a time-out blocks it too: without that field any other hook failure is non-blocking in Claude Code.

Also denied: Edit/Write of the policy file and of the generated folder, so a session cannot loosen its own gate.

`permissions.disableBypassPermissionsMode` is set to `"disable"` (ws.wallsettings BYPASS_OFF) in both files, so
`--dangerously-skip-permissions` and a subagent's `permissionMode: bypassPermissions` are refused while a ws-gated
session runs. A user setting of another value is a conflict, never overwritten (ws.userconfig).

The third layer is Claude Code's own `sandbox` block (the wall; documented keys only, checked on 2.1.308), which
confines every shell command Claude runs, whatever program it starts:
  * `enabled` with `failIfUnavailable`: if the sandbox cannot start (no bubblewrap or socat), Claude Code refuses to
    start, instead of running commands unsandboxed. Fail closed, as the hook is.
  * `allowUnsandboxedCommands: false`: the model's `dangerouslyDisableSandbox` retry is ignored, so a command the
    sandbox stops cannot be re-run outside it. Nothing is "approved" past the wall: what must leave it, a person runs.
  * `filesystem.denyWrite`: the folder the policy sits in and every protected path (ws's folders included). It beats any allowWrite
    a project adds, so a project's own settings cannot reopen it.
  * the project's own config is held too (ws.sandbox PROJECT_LOCKED and GIT_LOCKED: `.claude`, `.opencode`, `.mcp.json`,
    `opencode.json(c)`, `tui.json(c)`, `.git/hooks`, `.git/config`, `.git/commondir`, `.git/worktrees`, `.git/modules`), so a
    session cannot plant a plugin or hook that runs at the next start outside the sandbox. Two layers, because the settings
    file is one for every project and a sandbox path has no project-relative spelling here (`./x` means `~/.claude/x` in
    a `--settings` file; tried on 2.1.308): the Edit deny rules say `**/x`, which is project-independent, covers a
    sub-folder's own config as well, and stops the Edit and Write tools and a shell redirect Claude can read; and
    `denyWrite` names the absolute paths of the project, which is what stops an interpreter. Those are added when the
    session starts: **start a walled Claude Code session with `ws gate launch --runner claude-code -- claude`** (from the
    project folder). It writes a copy of these settings with the project's paths (git folders resolved as the OpenCode
    wall does) to `projects/` beside them, starts `claude --settings` with it, and reports afterwards what the session
    left. Started with plain `claude --settings .../settings.json`, only the Edit rules hold: an interpreter in Bash can
    still write `.opencode`, `opencode.json` and `.git/commondir` (Claude Code's sandbox already denies `.git/hooks` and
    `.git/config` itself). Claude Code asks before it writes `.claude` whatever the settings say.
  * `filesystem.allowWrite`: the policy's allow_write, if it names any. Claude Code's own default (the project folder
    and its tmp; nothing else) is already an allow-list.
  * `filesystem.denyRead`: the deny_read rules that name a folder or file (ws.policy.sandbox_read_paths).
  * `network.allowedDomains`: the policy's allow_hosts when it names some, else `DEFAULT_DOMAINS` (the runner's API
    host and the common package indexes; a session started with `ws gate launch` also gets `FORGE_DOMAINS`, a headless
    job does not), which only spares the prompts for hosts nearly every project uses:
    a host on neither list still asks (interactive) or is refused (`-p`). The defaults are in the launch settings
    only, never in the copy merged into a person's own `~/.claude/settings.json` (`user_scope`): Claude Code adds up
    the lists of every settings scope, so a default host there would reach a class session too. With the sandbox off
    there is no block and no list (ADR 0024). A session of a data class started by
    `ws gate launch` gets its own list instead (for_project): the class's allowed_hosts, its provider's host and the
    exceptions in force, so a class with none reaches nothing. The class file (ws.wallsettings) holds the same list as
    managed settings, with `allowManagedDomainsOnly`, so no other settings file adds a host.
  * `autoAllowBashIfSandboxed: false`, set on purpose: with it on, Claude Code runs sandboxed shell commands without
    asking. ws leaves that choice to the person's own permission mode, so a sandboxed command still prompts as before.
Project settings can widen all of this: in 2.1.308 `sandbox.excludedCommands` merges across settings sources and
`filesystem.allowRead` beats `denyRead`, and `claude -p` shows no trust dialog. Two guards, both tested: gated headless
runs pass `--setting-sources user` (ws run and the e2e), so a repo's `.claude/settings*.json` is never loaded; and the
hook (`preflight`) denies, with a plain message, every call made from a folder whose project or local settings turn the
sandbox off or open it up, because an interactive session cannot be started with that flag by ws.
The sandbox covers shell commands; Claude's own Read, Edit and Write tools are covered by the permission rules above and
the hook, not by it. Reads by those tools outside the working folders are held by
`permissions.blockReadsOutsideWorkingDirectories` (Claude Code 2.1.257 and newer), set in the launch settings and the
class file, never merged into a person's own settings file; the policy's `[gate] read_outside_project = true` leaves it
out. Claude Code applies a `true` from any settings file, so a project's own settings cannot lift it. `env` carries WS_GATED_SESSION so `ws approve` knows it is being run from a gated session.

Guards (ws.guards) add hooks, only for the events and tools the guards that are on need: PreToolUse matches more tools
(Agent, Task, Skill, SendMessage) when a guard reads them; PostToolUse, SubagentStop and the prompt event run
`ws gate event`; SessionStart and Stop run it beside what they already run.

Which hooks can block: PreToolUse (`ws gate check`) denies a call, and its command ends `|| exit 2` so a missing or
crashing ws blocks too. PostToolUse (`ws gate event`) exits 2 on purpose when a check finds a problem, and Claude Code
shows that stderr to the model; it carries no `|| exit`, and sets `onFailure: "block"` like PreToolUse. Every other generated hook command (SessionStart,
UserPromptSubmit, Stop, SubagentStop) ends `|| true`, because an exit 2 there would block the person's prompt or keep the
session going: those hooks add context and findings, and the session carries on whatever they return. The one
exception is the message wake hook (`wake_hook`): a Stop hook with `asyncRewake: true` that runs
`ws msg wait --session-hook`. It runs in the background, so it never blocks a call, and its exit 2 is how an idle
session is woken, so its command does not end `|| true`. It is left out when the guard `inbox` is not on (messages off).

Notification and Stop also run `ws notify alert --runner claude-code || true`, the phone alert.

The block is on by default, in the settings `ws gate launch` passes and in the ones `ws install` merges into the user's
own file (ws.userconfig): the policy's `[gate] sandbox = false` is the one switch that leaves it out. `env` then carries
WS_SANDBOXED=0; otherwise `env` carries WS_SANDBOXED=1, which tells the hook this session has the sandbox block above.
No hook command turns a refusal into an allow: the sandbox block is the wall for shell commands, and the gate reads the
same text whether or not it is up.
`preflight` also counts a project's `network` keys as a widening, but only when `env` carries WS_EGRESS_CLASS=1, which
`with_hosts` sets in the settings of a data-class session; with no classes the network is as it was.
`preflight` runs for every session except one whose settings set WS_SANDBOXED=0, the marker of a settings file written
without the sandbox block; settings written before the marker existed carry the block and no marker, so `preflight` runs
for them. The
marker can be trusted where it matters: the file `ws gate launch` passes with `--settings` outranks a project's settings,
so a project cannot set it back. Every hook command carries `--pin`: the home folder and ws's four folders as `ws install`
resolved them (see ws.paths). The environment, unlike the command, is the project's to set, so the hook uses the pinned
folders and refuses when its environment points them elsewhere.

Pass the file with `claude --settings OUT/settings.json`. Nothing in this module writes ~/.claude: `ws install` merges
the hooks, deny rules and env of this file into the user's own `settings.json` when the profile says
`gate = "every-session"` (ws.userconfig). That copy is built with `user_scope=True`, which swaps the project-lock Edit
rules above for `USER_LOCKS`. In every session, not only inside the wall, a rule such as `Edit(**/.claude/**)` would
also block a session working in a `.claude/worktrees/` checkout, so the shared file locks only the files Claude Code
or the version-control tool runs as code. It also leaves out WS_GATED_SESSION, so `ws approve` still works from the
person's own Claude shell (the hook denies it to the model whatever the env says).
"""

from __future__ import annotations

import json
import math
import os
import re
import shlex
from collections.abc import Iterable, Mapping
from pathlib import Path

from ws import guards, jobs, paths, wallsettings
from ws.adapters import Call, check_argv, client_argv, event_argv, protected_paths
from ws.approvals import ENV_MARKER, SANDBOX_MARKER, ApprovalError
from ws.approvals import _read_regular as read_regular
from ws.output import WsError
from ws.policy import Policy, native_patterns, sandbox_read_paths, sandbox_write_paths

NAME = "claude-code"
FILE = "settings.json"
# `mcp__.*` makes Claude Code read the matcher as a regular expression, which is not anchored: the hook also runs for
# a tool whose name merely contains one of the others (TodoWrite), and `parse` lets those through.
MATCHER = "Bash|Read|Grep|Glob|WebFetch|WebSearch|Edit|Write|MultiEdit|NotebookEdit|mcp__.*"
_TOOLS = {"web_fetch": "WebFetch", "web_search": "WebSearch"}
_NEUTRAL = {v: k for k, v in _TOOLS.items()}
# The project files that run as code, at any depth. These are the Edit locks in the user's own settings.json (every
# session); the wide set from ws.sandbox.claude_denied() stays in the file `ws gate launch` uses, inside the wall.
USER_LOCKS = (
    "Edit(**/.claude/settings.json)",
    "Edit(**/.claude/settings.local.json)",
    "Edit(**/.mcp.json)",
    "Edit(**/.git/hooks/**)",
    "Edit(**/.git/config)",
)
# Hosts a sandboxed shell may reach without asking when the policy's allow_hosts names none: the runner's API host and
# the common package indexes. Generic on purpose; allow_hosts replaces it, and a class session carries its class's list
# instead (`with_hosts`). The forges are not here: a headless job (`ws run`) reads this list as it stands, and a
# prompt-injected job that can reach a forge can push the project to it (ADR 0025). `FORGE_DOMAINS` are added only for a
# session started with `ws gate launch` (`for_project`), where a person is at the terminal.
DEFAULT_DOMAINS = (
    "api.anthropic.com",
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
    "crates.io",
    "index.crates.io",
    "static.crates.io",
)
FORGE_DOMAINS = (
    "github.com",
    "api.github.com",
    "codeload.github.com",
    "raw.githubusercontent.com",
    "objects.githubusercontent.com",
    "gitlab.com",
)
# Claude Code's hook event names, as ws.guards names them.
EVENT_NAMES = {
    "PreToolUse": "pre",
    "PostToolUse": "post",
    "SessionStart": "start",
    "UserPromptSubmit": "prompt",
    "Stop": "stop",
    "SubagentStop": "subagent-stop",
}
# The tools a Call kind comes from, in the order a matcher lists them.
_KIND_TOOLS = {
    "command": ("Bash",),
    "read": ("Read",),
    "search": ("Grep", "Glob"),
    "tool": ("WebFetch", "WebSearch"),
    "write": ("Edit", "Write", "MultiEdit", "NotebookEdit"),
    "agent": ("Agent", "Task"),
    "skill": ("Skill",),
    "message": ("SendMessage",),
}
_EXTRA_PRE_KINDS = ("agent", "skill", "message")  # the PreToolUse matcher covers the other kinds already
# seconds a hook may take. More than the thin client's worst case (ws.gateclient.WORST_CASE_S: the wait for the daemon,
# then the check in its own process), or the runner kills a hook that was about to answer and "block" denies the call.
HOOK_TIMEOUT = 30
# Set in the `env` of the settings a class session starts with (`with_hosts`), like WS_SANDBOXED: the --settings file
# outranks a project's, so a project cannot unset it. The hook has no policy to ask, so this is how `preflight` knows.
EGRESS_MARKER = "WS_EGRESS_CLASS"
# seconds the message wake hook may run: Claude Code still enforces `timeout` on an `asyncRewake` hook (600 by default),
# and the hook waits as long as the session is idle, so it is a day. `ws msg wait --session-hook` gives up on its own
# at `msg.HOOK_WAIT_MAX_S`, well before this, or the runner kills a waiter that was about to exit cleanly.
WAKE_TIMEOUT = 86400
INBOX_GUARD = "inbox"  # messages are on when this guard is in policy.guards.on

# Where this runner keeps what ws installs globally, and where a repo may shadow it (read by ws.globals).
# kinds: sub-folder -> how a name is taken: "dir" the folder name, "name" the file name, "stem" the name without extension.
GLOBALS = {
    "user_dir": "~/.claude",
    "user_dir_env": "CLAUDE_CONFIG_DIR",
    "project_dir": ".claude",
    "kinds": {"skills": "dir", "agents": "name", "commands": "name", "workflows": "stem", "hooks": "name"},
}


def session_hooks(ws_cmd: list[str], pin: paths.Pin) -> dict:
    """SessionStart records the release; UserPromptSubmit prints the one-line nudge (its stdout reaches the model).
    `|| true`: this is advice, and an exit 2 from UserPromptSubmit would block the user's prompt."""
    base = shlex.join(ws_cmd)
    where = shlex.join(pin.args())

    def hook(sub: str) -> list[dict]:
        cmd = f"{base} session {sub} --runner {NAME} {where} || true"
        return [{"hooks": [{"type": "command", "command": cmd, "timeout": 5}]}]

    return {"SessionStart": hook("start"), "UserPromptSubmit": hook("nudge")}


def alert_hook(ws_cmd: list[str], pin: paths.Pin) -> dict:
    """The phone alert: `ws notify alert` reads the hook payload and always exits 0; `|| true` keeps it advice."""
    cmd = f"{shlex.join(ws_cmd)} notify alert --runner {NAME} {shlex.join(pin.args())} || true"
    return {"type": "command", "command": cmd, "timeout": 5}


def wake_hook(ws_cmd: list[str], pin: paths.Pin) -> dict:
    """The message wake: a Stop hook that runs `ws msg wait --session-hook` in the background (`asyncRewake`), so it never
    holds up a call, and whose exit 2 wakes an idle session with what it printed. No `|| true`: that would hide the 2."""
    cmd = f"{shlex.join(ws_cmd)} msg wait --session-hook {shlex.join(pin.args())}"
    return {"type": "command", "command": cmd, "asyncRewake": True, "timeout": WAKE_TIMEOUT}


def _matcher(kinds: set[str], base: tuple[str, ...] = ()) -> str:
    tools = [*base]
    for kind, names in _KIND_TOOLS.items():
        if kind in kinds:
            tools += [t for t in names if t not in tools]
    return "|".join(tools)


def _event_hook(ws_cmd: list[str], policy_file: Path, pin: paths.Pin, *, may_block: bool = False) -> dict:
    """A `ws gate event` hook, run through the thin client. It ends `|| true` unless it may block (PostToolUse, whose
    exit 2 reaches the model); the one that may block also fails closed (`onFailure`), the rest are advice."""
    argv = client_argv("event", event_argv(ws_cmd, NAME, policy_file, pin), pin)
    hook = {"type": "command", "command": shlex.join(argv) + ("" if may_block else " || true"), "timeout": HOOK_TIMEOUT}
    return {**hook, "onFailure": "block"} if may_block else hook


def guard_hooks(policy: Policy, ws_cmd: list[str], policy_file: Path, pin: paths.Pin) -> dict:
    """The hooks beyond the PreToolUse check and the session hooks: `ws gate event` for each event a guard that is on
    runs at (PostToolUse, Stop, SubagentStop). Returns {event name: [matcher groups]} to merge with the session hooks."""
    on = policy.guards.on
    hooks: dict[str, list[dict]] = {}
    post = guards.kinds_for(on, "post")
    if post:
        hooks["PostToolUse"] = [
            {"matcher": _matcher(post), "hooks": [_event_hook(ws_cmd, policy_file, pin, may_block=True)]}
        ]
    for name, ev in (("Stop", "stop"), ("SubagentStop", "subagent-stop")):
        if guards.wanted(on, ev):
            hooks[name] = [{"hooks": [_event_hook(ws_cmd, policy_file, pin)]}]
    return hooks


def _path(path: str) -> str:
    """Claude's path syntax: `~/x` is home, `//x` is an absolute path, anything else is cwd-relative gitignore."""
    return f"/{path}" if path.startswith("/") else path


def sandbox_block(
    policy: Policy, out: Path, policy_file: Path, protect: tuple[str, ...] = (), *, defaults: bool = True
) -> dict:
    """The `sandbox` settings: see the module docstring for what each key is for. `defaults` False (the block merged
    into a person's own settings file) leaves `DEFAULT_DOMAINS` out: Claude Code concatenates array settings across
    scopes, so a default host in that file would be added to a class session's own list (`with_hosts` replaces the
    list only in the `--settings` copy). The person's own allow_hosts still go in, as they always did."""
    home = str(Path(policy_file).parent)
    fs: dict = {"denyWrite": list(dict.fromkeys([home, *protected_paths(out, policy_file, protect)]))}
    if policy.gate.deny_read:
        fs["denyRead"] = sandbox_read_paths(policy)
    if policy.gate.allow_write:  # Claude Code already allows its project folder and tmp; these are the person's extras
        fs["allowWrite"] = sandbox_write_paths(policy)
    block: dict = {
        "enabled": True,
        "autoAllowBashIfSandboxed": False,
        "failIfUnavailable": True,
        "allowUnsandboxedCommands": False,
        "filesystem": fs,
    }
    hosts = policy.gate.allow_hosts or (DEFAULT_DOMAINS if defaults else ())
    if hosts:
        block["network"] = {"allowedDomains": list(hosts)}
    return block


def preflight(payload: dict) -> str | None:
    """The reason `preflight_refusal` finds, as the one line the gate prints: the message, then what to delete or run."""
    e = preflight_refusal(payload)
    return f"{e.message}. To fix it: {e.hint}" if e else None


def preflight_refusal(payload: dict) -> WsError | None:
    """A reason to deny every call from this project, as a typed error (kind `project_settings_widen_sandbox` or
    `project_settings_unreadable`, each with a hint naming the file and what to delete): a `.claude/settings.json` or
    `settings.local.json`, in the project folder or any folder above it, that turns the sandbox off or opens it up (see
    the module docstring). The project
    folder is CLAUDE_PROJECT_DIR (set for hooks; Claude reads project settings from there, wherever the session has since
    changed to) and the payload's cwd. A file that does not parse is ignored, as Claude Code ignores it. A settings
    file that is not a plain file (a link, a FIFO), is too big or cannot be read is a deny: it is read without following a
    link and without blocking, because a hook that hangs fails open (only exit 2 blocks). It is skipped only when the
    settings `env` says WS_SANDBOXED=0 (the marker of settings written without the sandbox block); settings written
    before the marker existed have the block and no marker, so it runs for them. Both folders are checked, so one
    (CLAUDE_PROJECT_DIR is a variable a project's settings could set) cannot hide the other."""
    if os.environ.get(SANDBOX_MARKER) == "0":
        return None
    starts = [
        p
        for p in (os.environ.get("CLAUDE_PROJECT_DIR"), payload.get("cwd"))
        if isinstance(p, str) and p.startswith("/")
    ]
    home = paths.home_dir()
    seen: set[Path] = set()
    for d in [x for start in starts for x in (Path(start), *Path(start).parents)]:
        if d == home or d in seen:  # ~/.claude/settings.json is the person's own (user scope), not a project's
            continue
        seen.add(d)
        for name in ("settings.json", "settings.local.json"):
            f = d / ".claude" / name
            if not os.path.lexists(f):
                continue
            try:
                conf = json.loads(read_regular(f, 1 << 20))
            except ApprovalError as e:
                return WsError(
                    "project_settings_unreadable",
                    f"{f} is not a plain readable settings file ({e}); blocked to be safe",
                    f"make it a plain JSON file under 1 MB, or delete it with `rm {shlex.quote(str(f))}`",
                    details={"file": str(f)},
                )
            except ValueError:
                continue
            key = _widens_sandbox(conf, os.environ.get(EGRESS_MARKER) == "1")
            if key:
                name = key.split(" ")[0]  # "enabled = false" -> the key, "network.allowedDomains" as it is
                return WsError(
                    "project_settings_widen_sandbox",
                    f"{f} sets sandbox.{key}, which would open the sandbox ws generated",
                    f"delete `sandbox.{name}` from {f} (ws gated runs start with --setting-sources user and never "
                    "load it)",
                    details={"file": str(f), "key": f"sandbox.{name}"},
                )
    return None


def _widens_sandbox(conf, class_session: bool = False) -> str | None:
    """The first setting in a project's `conf` that opens the sandbox, or None. The `network` keys count only in a class
    session (`class_session`): outside one, the policy's network is as it always was and a project may name its own hosts."""
    sb = conf.get("sandbox") if isinstance(conf, dict) else None
    if not isinstance(sb, dict):
        return None
    if sb.get("enabled") is False:
        return "enabled = false"
    if sb.get("allowUnsandboxedCommands") is True:
        return "allowUnsandboxedCommands = true"
    if sb.get("excludedCommands"):
        return "excludedCommands"
    fs = sb.get("filesystem")
    if isinstance(fs, dict):
        if fs.get("disabled") is True:
            return "filesystem.disabled = true"
        for k in ("allowRead", "allowWrite"):
            if fs.get(k):
                return f"filesystem.{k}"
    net = sb.get("network")
    if class_session and isinstance(
        net, dict
    ):  # a host the generated list does not name, a local port, a socket or a proxy of its own
        for k in ("allowedDomains", "allowUnixSockets", "httpProxyPort", "socksProxyPort"):
            if net.get(k):
                return f"network.{k}"
        for k in ("allowLocalBinding", "allowAllUnixSockets"):
            if net.get(k) is True:
                return f"network.{k} = true"
    return None


def with_hosts(settings: str, hosts: list[str]) -> str:
    """The generated settings text with `hosts` (a data class's list: ws.sandbox.EgressPlan.domains) as
    `network.allowedDomains`, replacing any list in it, and `strictAllowlist` on, so a host outside the list is refused
    and not asked about (honoured from `--settings`, not from a project file; Claude Code 2.1.219 or later, and an older
    one asks). An empty list reaches no host at all. ValueError when the settings have no sandbox block, since the list
    would have nowhere to go: the network would be open and the caller must refuse."""
    conf = json.loads(settings)
    if "sandbox" not in conf:
        raise ValueError("the generated settings have no sandbox block, so the class's network list can not be applied")
    conf.setdefault("env", {})[EGRESS_MARKER] = "1"  # tells the hook this is a class session (see preflight)
    net = conf["sandbox"].setdefault("network", {})
    net["allowedDomains"] = list(hosts)
    net["strictAllowlist"] = True
    return json.dumps(conf, indent=2) + "\n"


def _xdg(env: Mapping[str, str], var: str, home: str, rel: str) -> str:
    """An XDG folder as the launching shell has it (absolute values only, as the spec says), else its default."""
    value = env.get(var, "")
    return value if value.startswith("/") else os.path.join(home, rel)


def _home_spellings(env: Mapping[str, str]) -> list[str]:
    """The home folder as a real path and, when it differs (a symlinked /home), as it is spelled: both are written to the
    settings, because the sandbox may meet it either way."""
    home = env.get("HOME") or str(Path.home())
    return list(dict.fromkeys([os.path.realpath(home), os.path.normpath(os.path.abspath(home))]))


def _under(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder.rstrip("/") + "/")


# What a sandboxed shell command reads from the runner's state folder (`read_defaults`).
STATE_READABLE = ("shell-snapshots", "skills", "plugins")


def read_defaults(env: Mapping[str, str]) -> tuple[list[str], list[str]]:
    """(allow, deny) for the home folder's minimum: what a sandboxed shell needs from it to run the common tools, as
    absolute paths resolved from HOME and the XDG variables of the launching shell. `allow` is the three parts of the
    runner's state folder a shell command needs (`STATE_READABLE`: `shell-snapshots/`, which every sandboxed command
    sources, and `skills/` and `plugins/`, which Claude Code's sandboxing page names as what commands need), never the
    folder whole: its settings, history, transcripts, IDE locks and sign-in file stay unreadable from the shell, and the
    runner reads its own state outside the sandbox, so that cannot break it. It also holds the uv, npm, cargo, rustup
    and Go folders, `~/.local/bin` and `~/.local/lib`, git's own config; the rest of `~/.local` is left out, since
    `~/.local/share` holds other tools' sign-in files. `deny` is the credential files inside those folders. Which of
    `allow` exist is the caller's to check."""
    home = os.path.normpath(os.path.abspath(env.get("HOME") or str(Path.home())))
    config = _xdg(env, "XDG_CONFIG_HOME", home, ".config")
    cache = _xdg(env, "XDG_CACHE_HOME", home, ".cache")
    data = _xdg(env, "XDG_DATA_HOME", home, ".local/share")
    state = env.get("CLAUDE_CONFIG_DIR", "")
    state = os.path.normpath(state) if state.startswith("/") else os.path.join(home, ".claude")
    allow = [
        *(os.path.join(state, part) for part in STATE_READABLE),
        os.path.join(cache, "uv"),
        os.path.join(home, ".npm"),
        os.path.join(home, ".cargo"),
        os.path.join(home, ".rustup"),
        os.path.join(home, "go"),
        os.path.join(cache, "go-build"),
        os.path.join(home, ".local", "bin"),
        os.path.join(home, ".local", "lib"),
        os.path.join(data, "uv"),
        os.path.join(home, ".gitconfig"),
        os.path.join(config, "git"),
    ]
    deny = [
        os.path.join(state, ".credentials.json"),
        os.path.join(home, ".cargo", "credentials"),
        os.path.join(home, ".cargo", "credentials.toml"),
        os.path.join(config, "git", "credentials"),
    ]
    return allow, deny


def _absolute(path: str, home: str) -> str:
    """`~/x` or `/x` as an absolute path under `home`."""
    return home + path[1:] if path == "~" or path.startswith("~/") else path


def deny_home(
    conf: dict, project: str, allow_read: Iterable[str], deny_read: Iterable[str], env: Mapping[str, str]
) -> None:
    """Add deny-home to the `sandbox.filesystem` of `conf` (a launch copy with a sandbox block): the home folder in
    `denyRead`, and in `allowRead` the project, its git folders outside it, the defaults of `read_defaults` and the
    policy's own `allow_read`. Every path is absolute: in a `--settings` file `.` means `~/.claude`, so none is relative.
    Claude Code keeps an exact `denyRead` path blocked inside a broader `allowRead`, so the deny_read paths and the
    credential files of the defaults stay hidden (each with its real path too, so a link cannot reach it). An entry is left out when it is not there (nothing to read), or when
    its real path is the home folder, above it, or a denied path: a link cannot re-open what it names."""
    from ws import sandbox  # here, not at the top: ws.sandbox imports the adapters

    fs = conf["sandbox"].setdefault("filesystem", {})
    homes = _home_spellings(env)
    defaults, credentials = read_defaults(env)
    exact = [_absolute(p, homes[0]) for p in (*fs.get("denyRead", []), *deny_read)]
    exact_real = [os.path.realpath(e) for e in exact]  # a link in a deny path must not be read through its real folder
    denied = [*exact, *exact_real, *(os.path.realpath(c) for c in credentials)]

    def allowed(path: str) -> str | None:
        real = os.path.realpath(path)
        if not os.path.lexists(real) or any(real == h or h.startswith(real.rstrip("/") + "/") for h in homes):
            return None
        return None if any(_under(real, d) for d in denied) else real

    deny = [*fs.get("denyRead", []), *deny_read, *homes]
    deny += [_absolute(p, h) for p in (*fs.get("denyRead", []), *deny_read) if p.startswith("~/") for h in homes]
    deny += [*credentials, *(os.path.realpath(c) for c in credentials), *exact_real]
    folders = [project, *sandbox.git_read_folders(project), *defaults, *(_absolute(p, homes[0]) for p in allow_read)]
    fs["denyRead"] = list(dict.fromkeys(deny))
    fs["allowRead"] = list(dict.fromkeys([*fs.get("allowRead", []), *filter(None, map(allowed, folders))]))


def for_project(
    settings: str,
    project: str,
    hosts: list[str] | None = None,
    *,
    allow_read: Iterable[str] = (),
    deny_read: Iterable[str] = (),
    env: Mapping[str, str] | None = None,
    forges: bool = False,
) -> str:
    """The generated settings text with the project's locked paths added to the sandbox `denyWrite`, absolute: what
    `ws gate launch --runner claude-code` starts claude with. The shared settings cannot hold them (see the docstring).
    `hosts`, when given, are applied by `with_hosts` (ValueError if there is no sandbox block to hold them).

    The launch copy also hides the home folder (`deny_home`): `allow_read` is the policy's `[gate] allow_read` and
    `deny_read` its exact deny_read paths (ws.policy.launch_read_rules), both `~/x` or `/x`; `env` is the launching
    shell's environment (default: this process's), where HOME and the XDG folders come from. Settings with no sandbox
    block (`[gate] sandbox = false`, ADR 0024) get none of this. `forges` adds `FORGE_DOMAINS` to the default list the
    block carries (a session at a terminal; a headless job never has them) and does nothing when the block carries no
    list or `hosts` replaces it."""
    from ws import sandbox  # here, not at the top: ws.sandbox imports the adapters

    if hosts is not None:
        settings = with_hosts(settings, hosts)
    conf = json.loads(settings)
    if "sandbox" not in conf:  # the policy switched the sandbox off: there is no block to add the project's paths to
        return json.dumps(conf, indent=2) + "\n"
    net = conf["sandbox"].get("network")
    if forges and hosts is None and net and net.get("allowedDomains"):
        net["allowedDomains"] = list(dict.fromkeys([*net["allowedDomains"], *FORGE_DOMAINS]))
    fs = conf["sandbox"].setdefault("filesystem", {})
    fs["denyWrite"] = list(dict.fromkeys([*fs.get("denyWrite", []), *sandbox.claude_denied_paths(project)]))
    deny_home(conf, project, allow_read, deny_read, os.environ if env is None else env)
    return json.dumps(conf, indent=2) + "\n"


def generate(
    policy: Policy,
    ws_cmd: list[str],
    out: Path,
    policy_file: Path,
    *,
    protect: tuple[str, ...] = (),
    user_scope: bool = False,
    pin: paths.Pin | None = None,
) -> dict[str, str]:
    protect = tuple(protect)
    deny = [f"Bash({p})" for p in native_patterns(policy)]
    deny += [f"Read({_path(p)})" for p in policy.gate.deny_read]
    for p in protected_paths(out, policy_file, protect):  # a file or a folder: cover both; Edit rules cover Write too
        deny += [f"Edit({_path(p)})", f"Edit({_path(p)}/**)"]
    if user_scope:
        deny += USER_LOCKS
    else:
        from ws import sandbox

        for rel in sandbox.claude_denied():  # the project's own config and git hooks: see ws.sandbox PROJECT_LOCKED
            deny += [
                f"Edit(**/{rel})",
                f"Edit(**/{rel}/**)",
            ]  # at any depth: a runner started in a sub-folder reads that one
    # an MCP tool named exactly is denied natively too; a glob is the hook's alone (Claude Code's rules have no glob there)
    deny += [_TOOLS.get(t, t) for t in policy.gate.deny_tools if t in _TOOLS or not re.search(r"[*?]", t)]
    pin = pin or paths.pin_of()
    wall = policy.gate.sandbox
    hook = (
        shlex.join(client_argv("check", check_argv(ws_cmd, NAME, policy_file, out, protect, pin), pin)) + " || exit 2"
    )
    matcher = _matcher(guards.kinds_for(policy.guards.on, "pre") & set(_EXTRA_PRE_KINDS), tuple(MATCHER.split("|")))
    permissions: dict = {"deny": list(dict.fromkeys(deny)), wallsettings.BYPASS_KEY: wallsettings.BYPASS_OFF}
    if not user_scope and not policy.gate.read_outside_project:
        # never in a person's own settings file (user_scope): it would bind every Claude Code session they start
        permissions[wallsettings.READ_BLOCK_KEY] = True
    settings = {
        "permissions": permissions,
        # `sandbox = false` in the policy: no block, and the marker says so (the hook then skips the preflight)
        **({"sandbox": sandbox_block(policy, out, policy_file, protect, defaults=not user_scope)} if wall else {}),
        "env": {ENV_MARKER: "1", SANDBOX_MARKER: "1" if wall else "0"},
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": matcher,
                    "hooks": [{"type": "command", "command": hook, "timeout": HOOK_TIMEOUT, "onFailure": "block"}],
                }
            ]
        },
    }
    settings["hooks"].update(session_hooks(ws_cmd, pin))
    for name, ev in (("SessionStart", "start"), ("UserPromptSubmit", "prompt")):
        if guards.wanted(policy.guards.on, ev):
            settings["hooks"][name][0]["hooks"].append(_event_hook(ws_cmd, policy_file, pin))
    for name, groups in guard_hooks(policy, ws_cmd, policy_file, pin).items():
        settings["hooks"][name] = groups
    settings["hooks"]["Notification"] = [{"hooks": [alert_hook(ws_cmd, pin)]}]
    settings["hooks"].setdefault("Stop", [{"hooks": []}])[0]["hooks"].append(alert_hook(ws_cmd, pin))
    if INBOX_GUARD in policy.guards.on:  # messages on: an idle session is woken when one arrives
        settings["hooks"]["Stop"][0]["hooks"].append(wake_hook(ws_cmd, pin))
    return {FILE: json.dumps(settings, indent=2) + "\n"}


def parse(payload: dict) -> Call | list[Call] | None:
    """A PreToolUse payload: {"tool_name": "Bash", "tool_input": {"command": ...}, ...}. An MCP tool
    (`mcp__SERVER__TOOL`) is a Call of kind "mcp" holding its arguments."""
    name = payload.get("tool_name")
    inp = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(inp, dict):
        raise TypeError("payload has no tool_name and tool_input")
    if name.startswith(
        "mcp__"
    ):  # an MCP tool: any arguments, judged by ws.gate (deny_tools, allow_mcp, path-like values)
        return Call.of("mcp", name, args=inp)
    if name == "Bash":
        return Call("command", _text(inp, "command"))
    if name == "Read":
        return Call("read", _text(inp, "file_path"))
    if name in ("Edit", "Write", "MultiEdit"):
        return Call.of("write", _text(inp, "file_path"), tool=name, **_write_data(name, inp))
    if name == "NotebookEdit":
        return Call.of("write", _text(inp, "notebook_path"), tool=name, **_write_data(name, inp))
    if name in ("Agent", "Task"):
        found = {k: inp[k] for k in ("prompt", "description", "subagent_type") if isinstance(inp.get(k), str)}
        return Call.of("agent", found.get("subagent_type", ""), tool=name, **found)
    if name == "Skill":
        skill = inp.get("skill") if isinstance(inp.get("skill"), str) else _optional(inp, "name") or ""
        return Call.of("skill", skill, tool=name, name=skill, args=_optional(inp, "args") or "")
    if name == "SendMessage":
        text = next((inp[k] for k in ("message", "text", "content") if isinstance(inp.get(k), str)), "")
        return Call.of("message", _optional(inp, "to") or "", tool=name, text=text)
    if name in ("Grep", "Glob"):
        calls = [Call("search", _optional(inp, "path") or ".")]
        pattern = inp.get("pattern")
        if name == "Glob" and isinstance(pattern, str) and pattern.startswith(("/", "~")):
            calls.append(Call("read", pattern))  # an absolute glob names its own folder
        return calls
    if name in _NEUTRAL:
        return Call("tool", _NEUTRAL[name])
    return None


def _write_data(tool: str, inp: dict) -> dict:
    """What a write tool gives besides the path: `old` and `new` (Edit), `content` (Write), `edits` (MultiEdit, as a
    tuple of {old, new}), `new` (NotebookEdit). Only text the tool really sent."""
    found: dict = {}
    for key, name in (("old_string", "old"), ("new_string", "new"), ("content", "content"), ("new_source", "new")):
        if isinstance(inp.get(key), str):
            found[name] = inp[key]
    edits = inp.get("edits")
    if tool == "MultiEdit" and isinstance(edits, list):
        found["edits"] = tuple(
            {
                k: e[key] if isinstance(e.get(key), str) else None
                for k, key in (("old", "old_string"), ("new", "new_string"))
            }
            for e in edits
            if isinstance(e, dict)
        )  # a recorded call (record-calls) holds a size and fingerprint where the text was: None here
    return found


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"tool_input.{key} is missing")
    return v


def _optional(d: dict, key: str) -> str | None:
    v = d.get(key)
    if v is not None and not isinstance(v, str):
        raise TypeError(f"tool_input.{key} must be text")
    return v


def launch_hint(out: str) -> str:
    return (
        "start Claude Code with: ws gate launch --runner claude-code -- claude "
        f"(settings for the project; the shared file is {shlex.quote(out)}/{FILE})"
    )


# --- ws run: the command line, and the result read back as a run record --------------------------------------------

EXE = "claude"
# Claude Code connects its MCP servers without waiting by default, so a first model call can start with no MCP tools and
# answer without them (measured: about 1 in 5 first calls). Headless runs wait for the connection, up to 5 minutes.
MCP_WAIT_ENV = {"MCP_CONNECTION_NONBLOCKING": "false", "MCP_TIMEOUT": "300000", "MCP_CONNECT_TIMEOUT_MS": "300000"}
PLACEHOLDER_KEY = "ws-lease-placeholder"  # the runner holds this; the broker swaps in the real key on the way out
OPTIONS = {"exe", "allowed_tools", "env"}
PROMPT_VIA_STDIN = True  # `claude -p` reads the prompt from stdin: it stays out of argv and `ps`


def priced(job) -> bool:
    """Does the runner report what a run cost? Claude Code always does (at list price)."""
    return True


def build_command(job, run_dir: Path, lease) -> tuple[list[str], dict[str, str]]:
    """(argv, env) for `claude -p`. argv[0] is the executable name (or runner_options.exe); ws finds it on PATH.
    The gate settings in run_dir/gate are passed with --settings, and the lease (if any) becomes the base URL, so
    the key the runner holds is a placeholder. Raises ValueError naming the runner_options key that is wrong."""
    jobs.check_options(job, OPTIONS)
    exe = jobs.option(job, "exe", str, EXE)
    tools = jobs.string_list(job, "allowed_tools")
    argv = [exe, "-p", "--output-format", "json", "--model", job.model, "--settings", str(run_dir / "gate" / FILE)]
    argv += ["--setting-sources", "user"]  # a repo's own .claude/settings*.json must not widen the sandbox
    if job.bare:
        argv.append("--bare")
    if job.max_turns is not None:
        argv += ["--max-turns", str(job.max_turns)]
    if job.max_budget_usd is not None:
        argv += ["--max-budget-usd", f"{job.max_budget_usd:.4f}"]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    env = jobs.extra_env(job)  # first: what ws sets below always wins
    env.update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1", **MCP_WAIT_ENV)
    if lease is not None:
        env.update(ANTHROPIC_BASE_URL=lease.base_url, ANTHROPIC_API_KEY=PLACEHOLDER_KEY)
    return argv, env


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _num(v) -> float | None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    return float(v) if ok else None


def parse_result(stdout: str, stderr: str, exit_code: int | None) -> dict:
    """The part of a run record that `claude -p --output-format json` can fill. Never raises."""
    try:
        r = json.loads(stdout)
    except ValueError:
        r = None
    if not isinstance(r, dict) or r.get("type") != "result":
        tail = (stderr or stdout or "").strip().splitlines()
        return {"error": "claude printed no result" + (f": {tail[-1][:150]}" if tail else "")}
    usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    models = []
    bases = set()
    for name, m in (r.get("modelUsage") if isinstance(r.get("modelUsage"), dict) else {}).items():
        if isinstance(m, dict):
            models.append(
                {
                    "model": name,
                    "input": _int(m.get("inputTokens")),
                    "output": _int(m.get("outputTokens")),
                    "cache_creation": _int(m.get("cacheCreationInputTokens")),
                    "cache_read": _int(m.get("cacheReadInputTokens")),
                    "cost_usd": _num(m.get("costUSD")),
                }
            )
            if isinstance(m.get("costBasis"), str):
                bases.add(m["costBasis"])
    cost = _num(r.get("total_cost_usd"))
    error = None
    if r.get("is_error"):
        errs = r.get("errors") if isinstance(r.get("errors"), list) else []
        error = str(r.get("result") or (errs[0] if errs else None) or r.get("subtype") or "claude reported an error")
    return {
        "session_id": r.get("session_id") if isinstance(r.get("session_id"), str) else None,
        "models_used": models,
        "tokens": {
            "input": _int(usage.get("input_tokens")),
            "output": _int(usage.get("output_tokens")),
            "cache_creation": _int(usage.get("cache_creation_input_tokens")),
            "cache_read": _int(usage.get("cache_read_input_tokens")),
        },
        "cost_usd": cost,
        "cost_basis": (bases.pop() if len(bases) == 1 else "mixed" if bases else None) if cost is not None else None,
        "duration_api_ms": _int(r.get("duration_api_ms")),
        "num_turns": _int(r.get("num_turns")),
        "error": error,
    }


def transcript_path(session_id: str | None, cwd: str, env: dict[str, str]) -> str | None:
    """Claude keeps `<config dir>/projects/<cwd with every non-alphanumeric as ->/<session id>.jsonl`. None when there
    is no such file (e.g. --no-session-persistence). The slug rule is checked against claude 2.1.308; a very long path
    is shortened by claude, so fall back to looking the session up in any project folder."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        return None
    config = Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME", "~")) / ".claude").expanduser()
    projects = config / "projects"
    direct = projects / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return str(direct)
    found = sorted(projects.glob(f"*/{session_id}.jsonl")) if projects.is_dir() else []
    return str(found[0]) if found else None
