"""The policy: one vendor-neutral `policy.toml` that says what a session may do. Each runner's own config is generated
from it (ws.gate, ws.adapters), so the runner itself enforces the deny list; the matcher here backs the hooks and shims
that cover what a runner's native rules cannot.

Stdlib only. Three jobs:
  * load and validate the file (PolicyError names the bad key);
  * `is_denied_command` / `is_denied_read` / `is_denied_search`: the one matcher every hook and shim calls. It is the
    second layer (the runner's own deny rules are the first, a sandbox the real wall): it catches the plausible
    retries, not every spelling a shell allows. It reads a command through ws.shellwords, the one reader every check
    and guard shares, so a path is recognised however the shell is asked to spell it (quotes, brace groups, `~` and
    `$HOME`, redirections, the value of an option);
  * `rule_of_two`: refuse a session that holds untrusted input, private data and a way out at the same time.

Interfaces other modules read (each is optional in the file; a policy without them loads as before, and an older ws
refuses a policy that uses them, which is the fail-closed behaviour wanted):
  * `Policy.classes` and `class_of(policy, path)`: data classes (`[classes.NAME]`), the `Class` dataclass and the
    built-in `UNCLASSIFIED` (counts only, nothing inbound, readable by nobody, no hosts). `class_of` answers None when
    the policy has no classes, so "no classes configured" keeps today's behaviour.
  * `Policy.budget_purposes`: `[budget.purposes]`, a monthly USD cap per purpose. A month is a UTC calendar month;
    `budget_month(now)` is that window, for everything that reserves or settles against a cap.
  * `Policy.autonomy`: `Autonomy(level, interrupts, always_ask)`. Separate from the `[interrupts]` counts.
  * `RuleException.owner`, `.expires` (an aware UTC datetime) and `.expired(now)`; a date means 00:00Z that day.
  * `Policy.egress_exceptions`: `[[egress_exceptions]]`, hosts a named class may reach beyond its own `allowed_hosts`.
    `owner` and `expires` are both required, and one that has expired covers nothing (ws.sandbox reads them).

Known limits of the matcher. It reads command text, so it cannot see:
  * what an interpreter does inside its own program (`python3 -c`, `perl -e`, `node -e`, a script it runs);
  * a script that is downloaded and run in two steps (`curl -o a.sh ... && bash a.sh`);
  * a path built entirely by a command, or by a variable set outside the command line (a built part followed by a
    protected path is refused);
  * the version-control tool restoring a protected tracked file (`git -C ~/.config/ws checkout policy.toml`);
  * OpenCode's `patch` tool, which carries no single file path (its native `edit` rules still apply).
These stay allowed by the matcher. What closes them is the sandbox (ws.sandbox, and the `sandbox` block the
claude-code adapter writes, on by default: `[gate] sandbox = false` leaves it out), which acts on what a process does,
not on what its command line says. It is the boundary and this matcher is defence in depth; the matcher refuses the same
lines whether or not a sandbox is up:
  * closed, for a shell command, the first four lines above: a write to ws's own files (its folders, the policy, the
    generated folder) fails read-only whatever program makes it, and a read under a deny_read rule finds an empty folder
    or file. The tested cases are in tests/e2e/test_sandbox.py, on both runners, Linux only (bubblewrap). macOS gets a
    generated sandbox-exec profile, which CI does not run. Also closed and tested (tests/e2e/test_wall_escapes.py): a
    daemon on a host Unix socket (tmux, screen, systemd-run --user, docker), the host's process list, typing into the
    person's terminal (TIOCSTI), renaming a folder above a protected one, a plugin or agent planted in the OpenCode
    config folder, a repo's `.claude/settings.json` that opens Claude Code's sandbox, and any write outside the project,
    TMPDIR and the runner's private folders (`~/.bashrc`, `~/.local/bin`, systemd user units, ws's installed source).
    Also closed, as a class and not a list (tests/e2e/test_wall_loadpaths.py has the table of what a runner or git
    loads at start): the config, hook and plugin paths a runner or git reads, and the folders a runner keeps programs
    in, are not writable by a walled run in the places an unwalled one reads them (the project's own source, a
    Makefile or a package script are the person's to review: the project folder is writable on purpose). OpenCode's XDG
    data and cache folders are private to the wall and to the project (`<state>/wall/opencode/{data,cache}/NAME-HASH`;
    state is shared, being preferences and prompt history), so a program planted in `~/.cache/opencode` or a plugin's
    `node_modules/.bin` cannot run outside the wall, or in another project's walled session. In the project, `.claude`,
    `.opencode`, `.mcp.json`, `opencode.json(c)`, `tui.json(c)`, and in every git folder the project holds (found with
    `git rev-parse`, so a `--separate-git-dir` or a worktree is included) the hooks, config, `config.worktree`,
    `commondir` and the worktree pointer files, the submodule repositories, the `core.hooksPath` folder when it is in the
    project and `[include]`d config files are read-only whether or not they exist yet (a missing one is made for the run
    and removed after, by the last session out of the project). `mv .git .git2` and `mv .claude x` are refused. Claude
    Code has the same list: its Edit deny rules (`**/NAME`, any project and any depth) stop the Edit and Write tools and
    a shell redirect it can read, and `ws gate launch --runner claude-code -- claude` adds this project's absolute paths
    to its sandbox `denyWrite`, which is what stops an interpreter (started with plain `claude --settings`, only the Edit
    rules hold). The seccomp filter refuses ptrace, process_vm_readv and process_vm_writev; the OpenCode shim marks the
    runner non-dumpable, which closes /proc/PID/mem for it.
    After every walled run the launcher compares the project with how it was and reports, never deleting, a new or
    changed git folder, runner or tool config, or auto-loaded program at any depth (stderr, and
    `<state>/logs/wall-report.jsonl`): that covers what the lists cannot name.
  * still open: a `deny_commands` rule is not enforced inside an interpreter (`python3 -c 'os.system("git push -f")'`
    pushes; only the sandbox's file rules apply to it). Reading is denied only for deny_read paths that exist when the
    runner starts and have no wildcard below their first folder. OpenCode's `patch` tool is not a shell command, so it
    is not sandboxed: its native `edit` rules are all that stop it. A runner started without the wrapper
    (`ws gate launch --no-sandbox`) has no wall at all, and the command says so.
  * not closed: a protected path that sits inside the project folder or an `allow_write` folder and does not exist when
    the runner starts (a --policy file never written) can be created; a program that writes a hard-coded path outside
    the project, TMPDIR and the runner's private folders fails (the wall is a write allow-list; `allow_write` lets one
    through), and so does `git config` or a git hook in the project (`.git/hooks` and `.git/config` are read-only);
    `ws gate launch` refuses a protected path, or a project config path (`.claude`, `.opencode`, `opencode.json`, ...),
    behind a symbolic link the session could replace, and a start from the home folder or above; the macOS
    sandbox-exec profile (including its Unix-socket rule and the `literal` rules that stop a rename of `.git`) is not
    run in CI and is unit-tested as text only: the macOS wall is UNTESTED on a real Mac, treat it as unproven. Gaps in
    the project lock, left on purpose or not closable by a path: a repository the session creates itself (`git init`
    must keep working inside the wall, so its hooks and config are writable), a submodule added during the session, and
    config in a sub-folder of the project (`sub/.claude/settings.json` is read only when a runner is started in `sub`;
    the Claude Code Edit rules reach it, the OpenCode wall does not): the end-of-run report names all three, but only
    after the fact. /proc/PID/mem and ptrace of a runner that does not mark itself non-dumpable, on a host whose Yama
    ptrace_scope is 0 or absent, stay open to a walled child of the same user (`ws doctor` WARNs, WS007, with the sysctl
    fix; the launcher prints a line). CLAUDE.md,
    AGENTS.md and `.gitmodules` are content and stay writable. A sign-in the walled OpenCode refreshes is not carried back
    to the real data folder (it is copied in at each start). A killed launcher leaves empty placeholders until the next
    launch in that project.
    A `deny_read` rule with a wildcard in the middle is matcher-only; Claude
    Code's own sandbox covers its Bash tool, not its Read/Edit tools (permission rules and the hook cover those); an
    interactive Claude session can still be started in a folder whose project settings open the sandbox, and the hook
    then denies every call there rather than reconfiguring it; the approval socket's close-on-exec mark needs Bun's
    FFI, and where that is missing the descriptor stays open in children (Bun's own child spawning does not pass it on;
    the worst a child can do with it is waste an approval); Anthropic's sandbox-runtime was evaluated and not adopted
    (see ws.sandbox).
  * `allow_hosts` limits the network on Claude Code only (its sandbox proxy filters by host). The OpenCode wall does not
    filter the network by it: bubblewrap can only cut it off whole, which would cut off the model too. A session of a
    data class has its own default-deny list (`allowed_hosts`, the provider's host, `[[egress_exceptions]]`), which
    ws.sandbox applies in `ws gate launch` and `ws run`: Claude Code's proxy, or ws's own for the others. ws's own is
    the route and not a fence on Linux (a program that ignores the proxy variables, or reaches a loopback port, is not
    stopped by it), and the list is fixed at launch for Claude Code, so an exception that ends mid-session still
    holds there until the next start.
  * An MCP tool call (`mcp__SERVER__TOOL`) is judged by the gate on Claude Code: `deny_tools` takes MCP names and globs;
    a tool `allow_mcp` does not name is asked for (a person approves it, as for `ask_commands`); and the path-like
    arguments of the call go through `deny_read` and, for a tool whose name says it writes, the protected-write check.
    OpenCode's hook payload does not say which tools are MCP tools, so there only `deny_tools` is written into its own
    permission block, for a literal server name.
A call the policy lets through only with a human's say-so is `ask_commands`: see ws.approvals.
"""

from __future__ import annotations

import contextlib
import itertools
import math
import os
import posixpath
import re
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from functools import lru_cache
from pathlib import Path

from ws import agentsession, paths, shellwords, unknownpath

SCHEMA = 1
DIALS = ("prepare", "allowlist", "act")
MODES = ("read", "act", "leave")
# Neutral tool names a policy can deny; each adapter maps them to its runner's own tool name.
KNOWN_TOOLS = ("web_fetch", "web_search")
# Tools that can carry data out of the session (Rule of Two, leg C).
EGRESS_TOOLS = ("web_fetch", "web_search")
# An MCP tool's name as the runner sends it, `mcp__SERVER__TOOL`, or a glob over it (`mcp__SERVER__*`, `mcp__*__delete_*`).
_MCP_PATTERN = re.compile(r"mcp__[A-Za-z0-9_.*?-]+")
# The tools `ws mcp serve` offers (tests keep this in step with ws.mcp_server.TOOLS). They are allowed when the policy has
# no `allow_mcp`; naming one's own list, even an empty one, replaces them.
MCP_SHIPPED = (
    "mcp__ws__memory_search",
    "mcp__ws__memory_add",
    "mcp__ws__note_capture",
    "mcp__ws__msg_send",
    "mcp__ws__msg_read",
    "mcp__ws__msg_ack",
    "mcp__ws__msg_list",
)
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_HOST = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$")
_CATCH_ALL = ("0.0.0.0", "0", "::", "*")  # an entry that names every address, however it is spelled


def check_host(h: str, where: str) -> None:
    """One host entry (`[gate] allow_hosts`, a class's `allowed_hosts`, an egress exception's `hosts`): a name, `*.name`
    or an IP with an optional port from 1 to 65535. A catch-all (the unspecified address) and a wildcard over a top-level
    name (`*.com`) or over an IP are refused: they open the network to everyone, and the runner's own proxy refuses
    them too. The message names the table and the entry, never anything else."""
    if not isinstance(h, str) or not _HOST.fullmatch(h):
        raise PolicyError(f"{where}: {h!r} is not a host (a name, `*.name` or an IP, optional :port)")
    name, _, port = h.partition(":")
    if port and not 1 <= int(port) <= 65535:
        raise PolicyError(f"{where}: {h!r} has a port outside 1 to 65535")
    if name in _CATCH_ALL:
        raise PolicyError(f"{where}: {h!r} would let a session reach every address")
    if name.startswith("*."):
        base = name[2:]
        if "." not in base:
            raise PolicyError(
                f"{where}: {h!r} is a wildcard over a whole top-level name; name the domain (`*.example.org`)"
            )
        if all(part.isdigit() for part in base.split(".")):
            raise PolicyError(f"{where}: {h!r} is a wildcard over an IP address, which names nothing")


ACCESS = ("full", "walled", "counts-only")  # a data class's access, loosest first
INBOUND = ("off", "lead-only", "allowlist")
AUTONOMY_LEVELS = ("ask", "decide-and-tell", "autonomous")
AUTONOMY_INTERRUPTS = ("never", "when-broken", "daily-summary")
_CLASS_RESERVED = "unclassified"  # the name of the built-in class a path in no class gets
_OS_USER = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]{0,31}$")
_MAX_DEPTH = 4
_TOO_DEEP = "the command nests shells, substitutions or eval too deeply to check; write the inner command out plainly"
MAX_COMMAND_CHARS = 64 * 1024  # a longer command takes longer to judge than a runner's hook may run


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class Interrupts:
    digest_per_day: int = 1
    pushes_per_day: int = 3


@dataclass(frozen=True)
class Source:
    name: str
    mode: str = "read"
    untrusted: bool = False
    sensitive: bool = False
    acts: tuple[str, ...] = ()
    outward: tuple[str, ...] = ()


@dataclass(frozen=True)
class Gate:
    deny_commands: tuple[str, ...] = ()
    deny_read: tuple[str, ...] = ()
    deny_tools: tuple[str, ...] = ()
    ask_commands: tuple[str, ...] = ()  # run only with a single-use approval from a human (ws.approvals)
    allow_hosts: tuple[str, ...] = ()  # empty = the policy says nothing about the network
    allow_write: tuple[str, ...] = ()  # folders besides the project a sandboxed session may write (`~/x` or `/x`)
    sandbox: bool = True  # the runner's own sandbox is switched on in what ws writes (Claude Code); false = unsandboxed
    allow_mcp: tuple[str, ...] | None = None  # MCP tools allowed outright; None = the policy says nothing (MCP_SHIPPED)


@dataclass(frozen=True)
class Guards:
    """The guards that run (ws.guards): `on` is their names; the order they run in is fixed by each guard's PRIORITY, not
    by this list. `config` maps each to what its `config()` made of its `[guards.NAME]` table. A policy with no
    `[guards]` has none on."""

    on: tuple[str, ...] = ()
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleException:
    """A named Rule of Two exception. `expires` is an aware UTC datetime or None (never): a TOML date in the file means
    00:00Z that day, an offset date-time is taken as written. `owner` is who answers for it (free text, optional)."""

    name: str
    job: str
    sources: tuple[str, ...]
    reason: str
    owner: str | None = None
    expires: datetime | None = None

    def expired(self, now: datetime) -> bool:
        """True from the moment `expires` is reached (`now >= expires`). `now` must be timezone-aware: ws compares
        instants and never assumes a local timezone."""
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be timezone-aware (use datetime.now(UTC))")
        return self.expires is not None and now >= self.expires


@dataclass(frozen=True)
class EgressException:
    """A named, owned and dated grant of hosts to the sessions of some classes (`[[egress_exceptions]]`). `expires` is an
    aware UTC datetime, as for RuleException; both `owner` and `expires` are required, so no grant is open-ended."""

    name: str
    classes: tuple[str, ...]
    hosts: tuple[str, ...]
    reason: str
    owner: str
    expires: datetime

    def expired(self, now: datetime) -> bool:
        """True from the moment `expires` is reached (`now >= expires`). `now` must be timezone-aware."""
        return now >= self.expires


@dataclass(frozen=True)
class Class:
    """A data class (`[classes.NAME]`): which repos belong to it and what a session working in them may do and see.
    Consumers read it, they do not re-parse it. `access` is full, walled or counts-only. `allowed_providers` is None
    when the file does not say (the consumer picks the walled default); `inbound` defaults to off for walled and
    counts-only and lead-only for full. `readable_by` holds class names, or `*` alone for everyone; () is nobody."""

    name: str
    access: str
    repos: tuple[str, ...] = ()
    os_user: str | None = None
    allowed_providers: tuple[str, ...] | None = None
    local_only: bool = False
    allowed_hosts: tuple[str, ...] = ()
    readable_by: tuple[str, ...] = ()
    inbound: str = "off"
    inbound_allow: tuple[str, ...] = ()
    retention_days: int = 30

    def readable_by_class(self, reader: str) -> bool:
        """May a session of class `reader` read this class's content?"""
        return "*" in self.readable_by or reader in self.readable_by


# What a path in no class is: counts only, nothing comes in, nobody else reads it, no network.
UNCLASSIFIED = Class(_CLASS_RESERVED, "counts-only")


@dataclass(frozen=True)
class Autonomy:
    """`[autonomy]`: how much a session decides alone (`level`), when it may interrupt the owner (`interrupts`), and the
    plain words of what is always asked first (`always_ask`). Not the `[interrupts]` counts, which cap ws's own sends."""

    level: str = "decide-and-tell"
    interrupts: str = "when-broken"
    always_ask: tuple[str, ...] = ()


@dataclass(frozen=True)
class Policy:
    schema: int
    dial: str
    interrupts: Interrupts
    sources: dict[str, Source]
    gate: Gate
    exceptions: tuple[RuleException, ...]
    guards: Guards = Guards()
    classes: dict[str, Class] = field(default_factory=dict)  # empty = no [classes] table: class_of answers None
    budget_purposes: dict[str, float] = field(default_factory=dict)  # purpose -> monthly USD cap (UTC calendar month)
    autonomy: Autonomy = Autonomy()
    egress_exceptions: tuple[EgressException, ...] = ()


@dataclass(frozen=True)
class Verdict:
    status: str  # "ok" | "refused" | "exception"
    reason: str = ""
    exception: str | None = None

    @property
    def ok(self) -> bool:
        """True when the session may run (clean, or by a named exception)."""
        return self.status != "refused"


# --- the default policy ------------------------------------------------------------------------------------------

# System directories a recursive delete must never target (Linux and macOS).
_SYSTEM_DIRS = ("bin", "boot", "dev", "etc", "lib", "lib64", "proc", "root", "sbin", "sys", "usr", "System", "Library")
_SHELLS = ("sh", "bash", "zsh", "dash")


def default_deny_commands() -> list[str]:
    """The built-in deny list. `*` matches anything; `\\*` is a literal star. Flags are normalised before matching
    (see _canon_tokens), so `rm -rf` here also covers `rm -fr`, `rm -r -f` and `rm -Rf`."""
    out = ["rm -* /", "rm -* /\\*", "rm -* ~", "rm -* ~/\\*", "rm -* /home", "rm -* /Users"]
    out += [f"rm -* /{d}*" for d in _SYSTEM_DIRS]
    # A forcing push in any spelling (-f, -uf, +refspec, --force-with-lease), with or without git's global options.
    out += ["git push*--force*", "git push* -f*", "git -* push*--force*", "git -* push* -f*"]
    out += ["mkfs*", "dd*of=/dev/*"]
    # A shell reading its script from stdin is the end of `curl ... | sh` (and of `echo ... | sh`). Written as a
    # plain command so the runner's own deny list, which splits pipes, catches it too.
    for s in _SHELLS:
        out += [s, f"{s} -s*", f"{s} -"]
    return out


def default_toml() -> str:
    """The commented policy `ws gate init` writes. Missing policy file = this text, parsed."""

    def arr(items: list[str]) -> str:
        return "[\n" + "".join(f'  "{i.replace(chr(92), chr(92) * 2)}",\n' for i in items) + "]"

    return f"""\
# ws policy. One file, any runner: `ws gate generate --runner claude-code|opencode --out DIR` turns it into that
# runner's own deny rules, so the runner blocks what is denied here. Edit this file, then regenerate.
schema = 1

# How far a timer or session may go.
#   prepare   = drafts only, no acts (the safe default)
#   allowlist = allow-listed acts, with you at the keyboard
#   act       = timers act within the rules below
dial = "prepare"

# At most one digest and three pushes a day. Config, not code.
[interrupts]
digest_per_day = 1
pushes_per_day = 3

# One table per source of content. Rule of Two: a session must not hold untrusted input (leg A), private data (leg B)
# and a way to send things out (leg C) all at once, unless a named exception below covers the job.
#   mode      read = look only | act = may act, as far as the dial allows | leave = hands off entirely
#   untrusted content from outside (anyone can write to it)
#   sensitive private data
#   acts      what ws may do here when mode = "act" and the dial allows
#   outward   acts that reach outside (send, post, publish)
[sources.mail]
mode = "read"
untrusted = true
sensitive = false
acts = ["draft"]
outward = ["send"]

[sources.notes]
mode = "read"
untrusted = false
sensitive = true
acts = ["write"]
outward = []

# What no session may do, whatever the dial says. Globs: `*` matches anything, `\\*` is a literal star. Each command
# is checked per shell segment (`a && b`, pipes, $(...), bash -c "..."). Flags are normalised first, so one `rm -*`
# rule covers `-rf`, `-fr`, `-r -f` and `-Rf`.
[gate]
deny_commands = {arr(default_deny_commands())}
# Paths no session may read. `~/` is your home; `**` crosses folders.
deny_read = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/credentials", "~/.config/ws/secrets/**", "~/.local/share/ws/secrets/**"]
# Neutral tool names to switch off: {", ".join(KNOWN_TOOLS)}. An allowed web tool counts as a way out (leg C). An MCP
# tool is named as `mcp__SERVER__TOOL`, and a glob works (`mcp__SERVER__*`, `mcp__*__delete_*`).
deny_tools = []
# Commands that run only after you approve that exact call, once: the gate refuses the first try and prints an id; in a
# terminal of your own (not the session's) run `ws approve ID`, then the session retries the identical call. Same globs
# as deny_commands; a command that also matches deny_commands stays denied. Example: ["git push*", "terraform apply*"].
ask_commands = []
# Hosts a session's shell may reach, for runners whose sandbox filters the network (Claude Code). Empty = no limit
# from the policy. Example: ["api.anthropic.com", "github.com", "*.npmjs.org"].
allow_hosts = []
# MCP tools a session may call without asking, as `mcp__SERVER__TOOL` or a glob (`mcp__SERVER__*`). An MCP tool this list
# does not name is asked for: a person approves that exact call, once, as for ask_commands. Leave the key out and only the
# tools `ws mcp serve` offers are allowed; naming it, even as [], replaces that. deny_tools beats it. Example:
# ["mcp__ws__*", "mcp__docs__search"].
# allow_mcp = ["mcp__ws__*"]
# Folders a sandboxed session may write besides the project folder it starts in (and the runner's own state and cache
# folders, which ws knows). Everything else is read-only inside the wall. Absolute or `~/`, no wildcards. Example:
# ["~/.npm", "~/.cache/uv"].
allow_write = []
# The sandbox is the wall: on by default, it makes ws's own files read-only and hides the deny_read paths from every
# program a shell command starts, in what ws writes for Claude Code. false = no sandbox block, and `ws doctor` says so.
sandbox = true

# Guards: small named checks that run beside the rules above. `ws gate generate` writes the runner hooks they need.
#   syntax-check     after an edit, checks the file by its extension (.py, .json, .toml, .sh, .js) and tells the model the
#                    line that does not parse
#   session-context  at session start, lists which of the files below exist in the project, warns when an API key (not
#                    the subscription) bills the session, and says once for each ws version what changed
#   bulk-staging     stops `git add -A`, `git add .` and `git commit -a`, so a commit holds only named files
#   record-calls     appends each tool call to <state>/logs/gate-calls.jsonl, for `ws gate replay`
#   test-weakening   before a test file is written, denies a skip marker added to an existing test
#   claims           checks a commit named beside merged, pushed or on the default branch against a ref; with
#                    release_dir, a release called live must be the one its `current` link points at
#   spawn-cap        denies a subagent start at max_agents running (default from the machine's CPUs and memory)
#   skill-gate       a write to chosen paths waits for a skill ([[guards.skill-gate]] skill, paths); a skill's own
#                    SKILL.md front matter can list `gates` (path globs) too, from skill_dirs
#   dev-server       denies a dev server that listens on every interface (python -m http.server, php -S, serve, vite
#                    --host, flask run --host, jupyter --ip, rails s -b, uvicorn --host, hugo server --bind); the reason
#                    names the flag that keeps it on this machine
#   checkout-pin     denies git checkout, switch, reset --hard, rebase, merge, pull, cherry-pick and revert in a checkout
#                    that has linked worktrees (and in any folder named in paths), so work happens in a worktree
#   inbox            hands a session the messages left for it, as untrusted text, at its next prompt, after a tool call
#                    and (an urgent one) when it is about to stop; a walled session is told how many, never what
#   presence         records in the session registry whether a session is busy or idle, from the hooks it already runs
#   pane-close       closes a tmux window or pane by id only when the registry says it is idle (the lead), or it is the
#                    session's own pane after its hand-over was delivered
[guards]
on = ["syntax-check", "session-context", "dev-server"]
# [guards.syntax-check]
# extra = {{ yaml = ["yamllint", "-d", "relaxed"] }}   # an extension and the checker that gets the file path appended
# [guards.session-context]
# files = ["STATE.md", ".state/STATE.md", "HANDOFF.md", "TODO.md"]   # the default; list the state files your projects keep
# warn_api_key = true   # on by default: a line when ANTHROPIC_API_KEY, not the subscription, bills a session
# [guards.claims]
# release_dir = "~/releases"   # a folder whose `current` link names the promoted release; left out, claims are off
# release_tags = ["v[0-9]+[.][0-9]+([.][0-9]+)*"]   # regexes for a release's name; the default reads v1.2 and v1.2.3
# [guards.skill-gate]
# skill_dirs = ["~/.claude/skills"]   # where skills keep their own `gates`; add rules as [[guards.skill-gate.rules]]
# [guards.spawn-cap]
# max_agents = 4   # optional: left out, half the CPUs or half the GiB of memory, whichever is less (2 to 8)
# [guards.dev-server]
# allow_hosts = ["203.0.113.5"]   # optional: addresses a dev server may bind besides loopback (a VPN or tailnet address)
# [guards.checkout-pin]
# paths = ["~/code/app"]   # optional: checkouts to pin even with no linked worktree (absolute or ~/)
# [guards.pane-close]
# live_socket = "default"   # the tmux socket whose panes are guarded (a path, or a name under the tmux folder); other servers are left alone

# Rule of Two, leg C (a way out): besides a source's outward acts and the web tools above, the SHELL counts as a way
# out while any of curl, wget, nc, ssh, scp, python -c ... is not in deny_commands. To close it, deny the lot
# (for example "curl*", "wget*", "nc *", "ssh *", "scp *", "python* -c*"); there is no half measure.

# Named Rule of Two exceptions: a job that may break the rule, for these sources only, and why. The job and every
# source the session reads must match, or the exception does not apply.
# [[exceptions]]
# name = "morning-digest"
# job = "digest"
# sources = ["mail", "notes"]
# reason = "reads mail, writes only to me"
"""


# --- load and validate -------------------------------------------------------------------------------------------


# A key that was renamed: old dotted key -> its replacement (`ws config` warns). Policy fails closed, so `loads` must keep
# accepting the old spelling, with the same meaning, for the release that deprecates it. Empty now.
DEPRECATED: dict[str, str] = {}


def policy_path(env: dict | None = None) -> Path:
    return paths.config_dir(env) / "policy.toml"


# What an agent must never rewrite in ws's folders: its own policy, profile, secrets, jobs, release, approvals, install
# manifest, ratings and the run records (`ws.paths.CONTROL_NAMES`, each under the folder that keeps it). (Reading
# logs/runs.jsonl stays allowed; only writes are denied.) Generated gate folders are added by the caller, since they can
# sit anywhere.
CONTROL_NAMES = paths.CONTROL_NAMES


def secret_read_rules(home: Path | None = None) -> tuple[str, ...]:
    """deny_read rules for where the secrets store keeps its values: `secrets/` in the data folder of the layout in use
    (every layout ws could use, when that can't be told) and, when the profile names another folder (`[roles.secrets] store`),
    that one too. With `home`, a policy kept in a folder of its own: that folder's `secrets`, as a single-folder layout.
    Each folder is named as written (made absolute) and as the real path it resolves to, so a ws folder that is a
    symlink is covered whichever way a command spells it. They are added to every policy that is read, so a session
    can't read them whatever its policy file says; the runners' own rules, the sandbox and the hook all see them.
    Without `home`, each `~/.ws.migrated-*` folder (what `ws migrate` left of the old one) is covered whole too."""
    from ws import secrets  # here, not at the top: it is a command module that imports more than the policy needs

    if home is None:
        folders = [*paths.secret_dirs()]
        with contextlib.suppress(Exception):  # a profile that does not read leaves the default folders, always covered
            folders.append(secrets.store_dir().expanduser().absolute())
        with contextlib.suppress(Exception):
            folders += paths.migrated_dirs()
    else:
        base = Path(home).expanduser().absolute()
        folders = [base / "secrets"]
        with contextlib.suppress(Exception):
            folders.append(secrets.store_dir({"WS_HOME": str(base)}).expanduser().absolute())
    spellings = [f for folder in folders for f in (folder, Path(os.path.realpath(folder)))]
    return tuple(dict.fromkeys(f"{f}/**" for f in spellings))


def control_paths(home: Path) -> list[str]:
    """ws's own control files under a folder that holds them all (a policy kept in a folder of its own), as absolute
    paths (files and folders alike; a path covers what is under it). For the folders in use, `ws.paths.control_paths`."""
    return paths.control_paths_in(home)


def valid_mcp_pattern(name) -> bool:
    """True for an MCP tool name or glob a `[gate]` list may hold: `mcp__` and then letters, digits, `_`, `-`, `.`, `*`
    and `?`, with no empty part (`mcp__`, `mcp__a____b` and a trailing `__` are refused)."""
    return (
        isinstance(name, str)
        and _MCP_PATTERN.fullmatch(name) is not None
        and "____" not in name
        and not name.endswith("__")
    )


def _table(d, where: str, allowed: set[str]) -> dict:
    if not isinstance(d, dict):
        raise PolicyError(f"{where} must be a table")
    extra = sorted(set(d) - allowed)
    if extra:
        raise PolicyError(f"{where}: unknown key {extra[0]!r} (known: {', '.join(sorted(allowed))})")
    return d


def _get(d: dict, key: str, kind: type, where: str, default):
    v = d.get(key, default)
    if isinstance(v, bool) != (kind is bool) or not isinstance(v, kind):
        raise PolicyError(
            f"{where}{key} must be {'a number' if kind is int else 'true or false' if kind is bool else 'text'}"
        )
    return v


def _strings(d: dict, key: str, where: str) -> tuple[str, ...]:
    v = d.get(key, [])
    if not isinstance(v, list) or not all(isinstance(x, str) and x.strip() for x in v):
        raise PolicyError(f"{where}{key} must be a list of non-empty text")
    return tuple(v)


def loads(text: str) -> Policy:
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"not valid TOML: {e}") from e
    raw = _table(
        raw,
        "policy",
        {
            "schema",
            "dial",
            "interrupts",
            "sources",
            "gate",
            "exceptions",
            "guards",
            "classes",
            "budget",
            "autonomy",
            "egress_exceptions",
        },
    )
    schema = raw.get("schema", SCHEMA)
    if schema != SCHEMA or isinstance(schema, bool):
        raise PolicyError(f"schema must be {SCHEMA}, not {schema!r}")
    dial = _get(raw, "dial", str, "", "prepare")
    if dial not in DIALS:
        raise PolicyError(f"dial must be one of {', '.join(DIALS)}, not {dial!r}")

    it = _table(raw.get("interrupts", {}), "[interrupts]", {"digest_per_day", "pushes_per_day"})
    interrupts = Interrupts(
        _get(it, "digest_per_day", int, "[interrupts] ", 1), _get(it, "pushes_per_day", int, "[interrupts] ", 3)
    )
    if interrupts.digest_per_day < 0 or interrupts.pushes_per_day < 0:
        raise PolicyError("[interrupts]: counts cannot be negative")

    sources: dict[str, Source] = {}
    srcs = raw.get("sources", {})
    if not isinstance(srcs, dict):
        raise PolicyError("[sources] must be a table of tables")
    for name, body in srcs.items():
        if not _NAME.match(name):
            raise PolicyError(f"[sources]: {name!r} is not a plain name (letters, digits, _ and -)")
        where = f"[sources.{name}] "
        body = _table(body, where.strip(), {"mode", "untrusted", "sensitive", "acts", "outward"})
        mode = _get(body, "mode", str, where, "read")
        if mode not in MODES:
            raise PolicyError(f"{where}mode must be one of {', '.join(MODES)}, not {mode!r}")
        sources[name] = Source(
            name,
            mode,
            _get(body, "untrusted", bool, where, False),
            _get(body, "sensitive", bool, where, False),
            _strings(body, "acts", where),
            _strings(body, "outward", where),
        )

    g = _table(
        raw.get("gate", {}),
        "[gate]",
        {
            "deny_commands",
            "deny_read",
            "deny_tools",
            "ask_commands",
            "allow_hosts",
            "allow_write",
            "sandbox",
            "allow_mcp",
        },
    )
    deny_read = _strings(g, "deny_read", "[gate] ")
    gate = Gate(
        _strings(g, "deny_commands", "[gate] "),
        (*deny_read, *(r for r in secret_read_rules() if r not in deny_read)),
        _strings(g, "deny_tools", "[gate] "),
        _strings(g, "ask_commands", "[gate] "),
        _strings(g, "allow_hosts", "[gate] "),
        _strings(g, "allow_write", "[gate] "),
        _get(g, "sandbox", bool, "[gate] ", True),
        _strings(g, "allow_mcp", "[gate] ") if "allow_mcp" in g else None,
    )
    for h in gate.allow_hosts:
        check_host(h, "[gate] allow_hosts")
    for w in gate.allow_write:
        n = posixpath.normpath(re.sub(r"^(\$HOME|\$\{HOME\})(?=/|$)", "~", w)) if w else ""
        if n in ("", ".", "/", "~") or not n.startswith(("~/", "/")) or re.search(r"[*?\[]", w):
            raise PolicyError(
                f"[gate] allow_write: {w!r} must be a folder, absolute or under ~/, with no wildcard, and not / or "
                "the home folder (after `.` and `..` are resolved)"
            )
    for t in gate.deny_tools:
        if t not in KNOWN_TOOLS and not valid_mcp_pattern(t):
            raise PolicyError(
                f"[gate] deny_tools: unknown tool {t!r} (known: {', '.join(KNOWN_TOOLS)}, or an MCP tool name or glob "
                "such as `mcp__server__tool` or `mcp__server__*`)"
            )
    for t in gate.allow_mcp or ():
        if not valid_mcp_pattern(t):
            raise PolicyError(
                f"[gate] allow_mcp: {t!r} is not an MCP tool name or glob (`mcp__server__tool`, `mcp__server__*`)"
            )

    ex_raw = raw.get("exceptions", [])
    if not isinstance(ex_raw, list):
        raise PolicyError("[[exceptions]] must be an array of tables")
    exceptions: list[RuleException] = []
    for i, e in enumerate(ex_raw, 1):
        where = f"[[exceptions]] #{i} "
        e = _table(e, where.strip(), {"name", "job", "sources", "reason", "owner", "expires"})
        for k in ("name", "job", "reason"):
            if not isinstance(e.get(k), str) or not e[k].strip():
                raise PolicyError(f"{where}{k} is required (text)")
        if "sources" not in e:
            raise PolicyError(f"{where}sources is required (the sources this exception covers)")
        covered = _strings(e, "sources", where)
        if not covered:
            raise PolicyError(f"{where}sources must name at least one source")
        for name in covered:
            if name not in sources:
                raise PolicyError(f"{where}sources: unknown source {name!r}")
        if any(x.name == e["name"] for x in exceptions):
            raise PolicyError(f"{where}name {e['name']!r} is used twice")
        owner = e.get("owner")
        if "owner" in e and (not isinstance(owner, str) or not owner.strip()):
            raise PolicyError(f"{where}owner must be non-empty text")
        expires = _expires(e["expires"], where) if "expires" in e else None
        exceptions.append(RuleException(e["name"], e["job"], covered, e["reason"], owner, expires))
    classes = _classes(raw.get("classes", {}))
    return Policy(
        schema,
        dial,
        interrupts,
        sources,
        gate,
        tuple(exceptions),
        _guards(raw.get("guards", {})),
        classes,
        _purposes(raw.get("budget", {})),
        _autonomy(raw.get("autonomy", {})),
        _egress_exceptions(raw.get("egress_exceptions", []), classes),
    )


def _egress_exceptions(raw, classes: dict[str, Class]) -> tuple[EgressException, ...]:
    """`[[egress_exceptions]]`: every key is required, and each class and host must be well formed. An exception with no
    owner or no expiry is refused, so no grant of hosts is open-ended or nobody's."""
    if not isinstance(raw, list):
        raise PolicyError("[[egress_exceptions]] must be an array of tables")
    out: list[EgressException] = []
    for i, e in enumerate(raw, 1):
        where = f"[[egress_exceptions]] #{i} "
        e = _table(e, where.strip(), {"name", "classes", "hosts", "reason", "owner", "expires"})
        for k in ("name", "reason", "owner"):
            if not isinstance(e.get(k), str) or not e[k].strip():
                raise PolicyError(f"{where}{k} is required (text)")
        if "expires" not in e:
            raise PolicyError(f"{where}expires is required (a date or an offset date-time): a grant must end")
        if "classes" not in e or "hosts" not in e:
            raise PolicyError(f"{where}classes and hosts are required")
        if any(x.name == e["name"] for x in out):
            raise PolicyError(f"{where}name {e['name']!r} is used twice")
        named = _plain_names(e, "classes", where)
        if not named:
            raise PolicyError(f"{where}classes must name at least one class")
        for c in named:
            if c not in classes:
                raise PolicyError(f"{where}classes: unknown class {c!r}")
        hosts = _strings(e, "hosts", where)
        if not hosts:
            raise PolicyError(f"{where}hosts must name at least one host")
        for h in hosts:
            check_host(h, f"{where}hosts")
        out.append(EgressException(e["name"], named, hosts, e["reason"], e["owner"], _expires(e["expires"], where)))
    return tuple(out)


def _expires(v, where: str) -> datetime:
    """An exception's `expires`: a TOML date (00:00Z that day) or an offset date-time, as an aware UTC datetime. A local
    date-time or a time has no instant to compare, so it is refused, and so is text."""
    if isinstance(v, datetime):  # before date: a datetime is a date too
        if v.tzinfo is None or v.utcoffset() is None:
            raise PolicyError(
                f"{where}expires has no timezone; add an offset or Z (2026-12-31T09:00:00+13:00) or use a date"
            )
        return v.astimezone(UTC)
    if isinstance(v, date):
        return datetime(v.year, v.month, v.day, tzinfo=UTC)
    raise PolicyError(f"{where}expires must be a TOML date (2026-12-31) or an offset date-time, not {type(v).__name__}")


def _plain_names(d: dict, key: str, where: str) -> tuple[str, ...]:
    """A list of plain names (letters, digits, `_` and `-`)."""
    items = _strings(d, key, where)
    for item in items:
        if not _NAME.fullmatch(item):
            raise PolicyError(f"{where}{key}: {item!r} is not a plain name (letters, digits, _ and -)")
    return items


def _classes(raw) -> dict[str, Class]:
    """`[classes.NAME]`: each table is checked whole; the readable_by names are checked once all the names are known."""
    if not isinstance(raw, dict):
        raise PolicyError("[classes] must be a table of tables")
    out: dict[str, Class] = {}
    for name, body in raw.items():
        if not _NAME.fullmatch(name):
            raise PolicyError(f"[classes]: {name!r} is not a plain name (letters, digits, _ and -)")
        if name == _CLASS_RESERVED:
            raise PolicyError(f"[classes]: {name!r} is the built-in class for a path in no class; pick another name")
        where = f"[classes.{name}] "
        body = _table(
            body,
            where.strip(),
            {
                "access",
                "os_user",
                "allowed_providers",
                "local_only",
                "allowed_hosts",
                "readable_by",
                "inbound",
                "inbound_allow",
                "retention_days",
                "repos",
            },
        )
        if "access" not in body:
            raise PolicyError(f"{where}access is required ({', '.join(ACCESS)})")
        access = _get(body, "access", str, where, "")
        if access not in ACCESS:
            raise PolicyError(f"{where}access must be one of {', '.join(ACCESS)}, not {access!r}")
        os_user = _get(body, "os_user", str, where, "") if "os_user" in body else None
        if os_user is not None and not _OS_USER.fullmatch(os_user):
            raise PolicyError(f"{where}os_user: {os_user!r} is not a user name (letters, digits, _ . -; at most 32)")
        providers = _plain_names(body, "allowed_providers", where) if "allowed_providers" in body else None
        hosts = _strings(body, "allowed_hosts", where)
        for h in hosts:
            check_host(h, f"{where}allowed_hosts")
        readable = _strings(body, "readable_by", where)
        if "*" in readable and len(readable) > 1:
            raise PolicyError(f'{where}readable_by: "*" means everyone and can not be listed with class names')
        inbound = _get(body, "inbound", str, where, "lead-only" if access == "full" else "off")
        if inbound not in INBOUND:
            raise PolicyError(f"{where}inbound must be one of {', '.join(INBOUND)}, not {inbound!r}")
        allow = _plain_names(body, "inbound_allow", where)
        if inbound == "allowlist" and not allow:
            raise PolicyError(f"{where}inbound_allow is required (at least one name) when inbound is allowlist")
        if inbound != "allowlist" and allow:
            raise PolicyError(f"{where}inbound_allow only applies when inbound is allowlist")
        days = _get(body, "retention_days", int, where, 30)
        if not 1 <= days <= 3650:
            raise PolicyError(f"{where}retention_days must be from 1 to 3650, not {days}")
        repos = _strings(body, "repos", where)
        if not repos:
            raise PolicyError(f"{where}repos is required (a non-empty list of path globs)")
        out[name] = Class(
            name,
            access,
            repos,
            os_user,
            providers,
            _get(body, "local_only", bool, where, False),
            hosts,
            readable,
            inbound,
            allow,
            days,
        )
    for c in out.values():
        for r in c.readable_by:
            if r != "*" and r not in out:
                raise PolicyError(f"[classes.{c.name}] readable_by: unknown class {r!r}")
    return out


def _purposes(raw) -> dict[str, float]:
    """`[budget.purposes]`: purpose name -> monthly USD cap (a UTC calendar month), a finite number of 0 or more."""
    t = _table(raw, "[budget]", {"purposes"})
    purposes = t.get("purposes", {})
    if not isinstance(purposes, dict):
        raise PolicyError("[budget.purposes] must be a table of purpose = monthly USD cap")
    out: dict[str, float] = {}
    for name, v in purposes.items():
        if not _NAME.fullmatch(name):
            raise PolicyError(f"[budget.purposes]: {name!r} is not a plain name (letters, digits, _ and -)")
        if isinstance(v, bool) or not isinstance(v, int | float):
            raise PolicyError(f"[budget.purposes] {name} must be a number (monthly USD)")
        try:
            cap = float(v)
        except OverflowError:
            cap = math.inf
        if not math.isfinite(cap) or cap < 0:
            raise PolicyError(f"[budget.purposes] {name} must be a finite number of 0 or more, not {v!r}")
        out[name] = cap
    return out


def budget_month(now: datetime) -> tuple[datetime, datetime]:
    """The window a `[budget.purposes]` cap covers: the UTC calendar month holding `now`, as (start, end) with the start
    included and the end not (00:00Z on the 1st, to 00:00Z on the 1st of the next month). `now` must be timezone-aware.
    Everything that reserves or settles spend against a purpose cap uses this one window."""
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware (use datetime.now(UTC))")
    now = now.astimezone(UTC)
    start = datetime(now.year, now.month, 1, tzinfo=UTC)
    end = datetime(now.year + (now.month == 12), now.month % 12 + 1, 1, tzinfo=UTC)
    return start, end


def _autonomy(raw) -> Autonomy:
    """`[autonomy]`: level, interrupts and the always_ask words. Separate from the `[interrupts]` counts."""
    t = _table(raw, "[autonomy]", {"level", "interrupts", "always_ask"})
    level = _get(t, "level", str, "[autonomy] ", "decide-and-tell")
    if level not in AUTONOMY_LEVELS:
        raise PolicyError(f"[autonomy] level must be one of {', '.join(AUTONOMY_LEVELS)}, not {level!r}")
    when = _get(t, "interrupts", str, "[autonomy] ", "when-broken")
    if when not in AUTONOMY_INTERRUPTS:
        raise PolicyError(f"[autonomy] interrupts must be one of {', '.join(AUTONOMY_INTERRUPTS)}, not {when!r}")
    return Autonomy(level, when, tuple(dict.fromkeys(_plain_names(t, "always_ask", "[autonomy] "))))


def _guards(raw) -> Guards:
    """`[guards]`: `on` names the guards that run; `[guards.NAME]` is that guard's own table, checked by the guard."""
    from ws import guards  # here, not at the top: the guards import this module

    known = guards.names()
    t = _table(raw, "[guards]", {"on", *known})
    on = _strings(t, "on", "[guards] ")
    for name in on:
        if name not in known:
            raise PolicyError(f"[guards] on: unknown guard {name!r} (known: {', '.join(known)})")
    if len(set(on)) != len(on):
        raise PolicyError("[guards] on: a guard is named twice")
    config = {name: guards.get(name).config(t.get(name, {}), f"[guards.{name}]") for name in known}
    return Guards(on, {name: config[name] for name in on})


def default_policy() -> Policy:
    return loads(default_toml())


def load(path: Path | None = None, *, required: bool = False) -> Policy:
    """The policy at `path` (default <config>/policy.toml). No file = the built-in defaults, unless `required`: a policy
    that was named (a hook's `--policy`) and is not there is an error, never a quiet fall back to the defaults."""
    path = path or policy_path()
    if not path.is_file():
        if required:
            raise PolicyError(f"{path}: the policy file is missing or is not a plain file")
        return default_policy()
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise PolicyError(f"{path}: {e}") from e
    try:
        return _with_secret_reads(loads(text), path.parent)
    except PolicyError as e:
        raise PolicyError(f"{path}: {e}") from e


def _with_secret_reads(pol: Policy, home: Path) -> Policy:
    """The policy with the secrets folder of the ws home the file sits in denied too (`loads` covers the one ws uses; a
    policy kept in another folder, such as the one a generated hook is told to read, covers its own)."""
    extra = tuple(r for r in secret_read_rules(home) if r not in pol.gate.deny_read)
    return replace(pol, gate=replace(pol.gate, deny_read=(*pol.gate.deny_read, *extra))) if extra else pol


# --- data classes ------------------------------------------------------------------------------------------------

_STRICTNESS = {a: i for i, a in enumerate(ACCESS)}  # a higher number is stricter


def _class_places(path) -> list[str]:
    """Where `path` really is: every link resolved (never the spelling it was given, so a link cannot make a path look like
    a looser class), then the folders above it, so a session started in a sub-folder of a repo is in that repo. A trailing
    `/` form lets `repo/**` cover the repo folder itself."""
    name = os.path.realpath(os.fspath(path))
    out = [name.rstrip("/") + "/"]
    while name and name != "/":
        out.append(name)
        name = posixpath.dirname(name)
    return out


def _class_patterns(repos: tuple[str, ...]) -> list[tuple[re.Pattern, int]]:
    """Each repo glob as (regex, how specific it is). A glob whose literal folder is a link is also read with that link
    resolved, because the path being classified is. Specific = the length of the literal folder before the first wildcard
    (a bare name, which matches at any depth, has none)."""
    out = []
    for repo in repos:
        out.append((_path_regex(repo), len(_rule_root(repo) or "")))
        expanded, root = _expand(repo), _rule_root(repo)
        if root and expanded.startswith(root) and (real := os.path.realpath(root)) != root:
            out.append((_glob_regex_path(real + expanded[len(root) :]), len(real)))
    return out


def class_of(pol: Policy, path) -> Class | None:
    """The data class of a session working at `path`.

    None when the policy has no `[classes]`: nothing is classified and today's behaviour holds. Otherwise the path is
    resolved with every link followed and matched against every class's `repos` (caseless where CASELESS_PATHS says).
    Only the real path counts, never the spelling it was given, so a link into a walled repo is that repo whatever
    class the link itself sits in. The most specific match wins (the longest literal folder before a wildcard); a tie
    goes to the stricter access (counts-only, then walled, then full) and then to the first name. A path in no class is
    UNCLASSIFIED, and so is one that cannot be read as a path: the strictest answer, never a guess."""
    if not pol.classes:
        return None
    try:
        places = _class_places(path)
        best: tuple[int, int] | None = None
        found = UNCLASSIFIED
        for name in sorted(pol.classes):
            c = pol.classes[name]
            hits = [spec for rx, spec in _class_patterns(c.repos) if any(rx.match(s) for s in places)]
            if not hits:
                continue
            key = (max(hits), _STRICTNESS[c.access])
            if best is None or key > best:
                best, found = key, c
        return found
    except (ValueError, OSError):  # an embedded NUL or a path the system will not resolve
        return UNCLASSIFIED


# --- Rule of Two -------------------------------------------------------------------------------------------------


def shell_ways_out(policy: Policy) -> list[str]:
    """The network commands the policy does not deny: each is a way for a shell to send data out."""
    return [p.split()[0] for p in NET_PROBES if is_denied_command(policy, p) is None]


def rule_of_two(
    policy: Policy, sources: list[str], job: str | None = None, shell: bool = True, now: datetime | None = None
) -> Verdict:
    """May a session read `sources` and run `job`? Refused when it would hold untrusted input AND private data AND a
    way out. A way out is an outward act of a source in mode "act" (only when the dial is not "prepare"), an allowed
    egress tool (web_fetch, web_search), or, when the session has a shell (`shell`, default yes), a network command
    the policy does not deny (see shell_ways_out). A named exception lifts the refusal when its job matches and
    every source the session reads is one it names, and it has not expired: an expired exception covers nothing, and
    the refusal names when it ended. `now` is an aware datetime (default: now, UTC); tests pass their own clock."""
    for name in sources:
        src = policy.sources.get(name)
        if src is None:
            return Verdict("refused", f"unknown source {name!r}")
        if src.mode == "leave":
            return Verdict("refused", f"source {name!r} is set to leave (hands off)")
    held = [policy.sources[n] for n in sources]
    untrusted = [s.name for s in held if s.untrusted]
    sensitive = [s.name for s in held if s.sensitive]
    ways_out = [f"{s.name}:{a}" for s in held if s.mode == "act" and policy.dial != "prepare" for a in s.outward]
    ways_out += [t for t in EGRESS_TOOLS if t not in policy.gate.deny_tools]
    if shell and (open_cmds := shell_ways_out(policy)):
        ways_out.append(f"shell:{'/'.join(dict.fromkeys(open_cmds))}")
    if not (untrusted and sensitive and ways_out):
        return Verdict("ok")
    why = (
        f"untrusted input ({', '.join(untrusted)}), private data ({', '.join(sensitive)}) and a way out "
        f"({', '.join(ways_out)}) together"
    )
    now = datetime.now(UTC) if now is None else now
    lapsed = ""
    for e in policy.exceptions:
        if job is not None and e.job == job and set(sources) <= set(e.sources):
            if e.expired(now):
                lapsed = f"; exception {e.name!r} expired at {e.expires.isoformat(timespec='seconds')}"
                continue
            return Verdict("exception", f"{why}; allowed by exception {e.name!r}: {e.reason}", e.name)
    return Verdict("refused", f"Rule of Two: {why}{lapsed}")


# --- the matcher -------------------------------------------------------------------------------------------------
#
# The matcher is the SECOND layer. The first is each runner's own deny rules (generated from this policy), and the
# real wall is a sandbox, which comes later. A shell line can always be spelled in a way a text matcher misses
# (variables, interpreters, encoded payloads), so this does not try to be complete: it catches the plausible ways a
# model retries a blocked command (other flags, a wrapper, a pipe, a nested shell, a different path spelling). What it
# cannot judge it denies or leaves to the layers either side.

_FLAGS = re.compile(r"^-[A-Za-z]+$")
_SHELL_NAMES = {
    "sh",
    "bash",
    "zsh",
    "dash",
    "ksh",
    "fish",
    "ash",
    "csh",
    "tcsh",
    "rbash",
    "mksh",
    "yash",
    "posh",
    "lksh",
    "oksh",
    "loksh",
    "pdksh",
    "hush",
}
# A here-document or here-string given to one of these is read as code: a shell runs it (any command whose name ends in
# `sh`, and `ksh93`), `source` and `.` run it as a script, `eval` runs it as text, `ssh` hands it to the shell on the
# other end, and `xargs` builds commands from it. The names are not a closed list, because shells keep being written.
_UNKNOWN_VALUE = "${_WS_UNKNOWN}"  # what a variable holds when a command substitution set it: a part no one can read
_STDIN_IS_CODE = frozenset({*_SHELL_NAMES, "source", ".", "eval", "ssh", "ksh93"})


# A program name with its version or build in it is the same program: `bash5.2`, `zsh5`, `sh5`, `bash-static`,
# `mksh-static`, `python3.12`, `node22`. `_stem` is the name without them (a word that doesn't look like that is its own).
_VERSIONED = re.compile(r"([A-Za-z]+?)(?:[0-9][0-9.]*)?(?:-static)?")


def _stem(word: str) -> str:
    base = posixpath.basename(word)
    found = _VERSIONED.fullmatch(base)
    return found.group(1) if found else base


def _reads_stdin_as_code(cmd: str) -> bool:
    name = _stem(cmd)
    return name in _STDIN_IS_CODE or name.endswith("sh")


_GIT_ARG_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
# Commands whose operands are each judged on their own: `rm -rf / /tmp` is a delete of `/`.
_OPERAND_CMDS = {"rm", "rmdir", "shred", "unlink", "chmod", "chown", "chgrp", "mv"}
_ALWAYS_RECURSIVE = {"tar", "find", "zip", "rsync", "tree", "du", "7z", "bsdtar", "cpio"}
# Commands that write to every operand / only to the last one (or -t DIR).
_WRITE_ALL = {"tee", "mv", "rm", "rmdir", "truncate", "chmod", "chown", "chgrp", "touch", "shred", "unlink", "patch"}
_WRITE_LAST = {"cp", "install", "ln", "rsync", "scp"}
_INTERP = re.compile(r"^(python[0-9.]*|perl|ruby|node|php)$")
# Probes for "can the shell reach the network": a policy that denies all of them closes the shell as a way out.
NET_PROBES = (
    "curl http://example.com",
    "wget http://example.com",
    "nc example.com 80",
    "ncat example.com 80",
    "ssh example.com",
    "scp a example.com:b",
    "sftp example.com",
    "ftp example.com",
    "telnet example.com 80",
    "socat - TCP:example.com:80",
    "python3 -c x",
    "python -c x",
    "node -e x",
    "perl -e x",
    "ruby -e x",
)


@dataclass
class _Ctx:
    """What earlier parts of the same command line did: the folder (cd), variables, and paths ws protects."""

    cwd: str
    protect: tuple[str, ...] = ()
    vars: dict[str, str] | None = None
    heredocs: bool = False  # read the command line with here-document bodies left out (a second reading, never alone)
    budget: shellwords.Budget = field(default_factory=shellwords.Budget)  # shared by every part of one command line
    oldpwd: str = ""  # the folder before the last `cd` (`cd -`, `~-`, `$OLDPWD`)
    loops: dict[str, list[str]] | None = None  # a `for` variable and the words it takes in turn
    unknown: frozenset[str] = frozenset()  # names set more than once, appended to, or set inside a block
    distrust: bool = False  # a part of the line ran a command that can set any variable: trust no value after it
    cwd_unknown: bool = False  # a `cd` went to a place the reader could not resolve: a relative path is anywhere
    bodies_as_commands: bool = (
        False  # here-document bodies are read as commands here; the other reading judges the rest
    )
    arith: bool = False  # the line holds arithmetic, which runs the command substitutions in the text it is given

    def fork(self) -> _Ctx:
        return _Ctx(
            self.cwd,
            self.protect,
            dict(self.vars or {}),
            heredocs=self.heredocs,
            budget=self.budget,
            oldpwd=self.oldpwd,
            loops=self.loops,
            unknown=self.unknown,
            distrust=self.distrust,
            cwd_unknown=self.cwd_unknown,
            bodies_as_commands=self.bodies_as_commands,
            arith=self.arith,
        )


def _home() -> str:
    return paths.environ().get("HOME") or str(Path.home())


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return "/"


_LAYOUT_VARS = (
    "HOME",
    "WS_HOME",
    *paths.ENV_DIRS.values(),
    "XDG_CONFIG_HOME",
    "XDG_STATE_HOME",
    "XDG_DATA_HOME",
    "XDG_CACHE_HOME",
)
_WS_NAMES = ("WS_HOME", *paths.ENV_DIRS.values())


def _env_key() -> tuple[str | None, ...]:
    """The variables the folders ws uses come from: the cache key for what is worked out from them."""
    env = paths.environ()
    return (*(env.get(v) for v in _LAYOUT_VARS), str(paths.tmp_dir()))


@lru_cache(maxsize=8)
def _base_env(key: tuple[str | None, ...]) -> tuple[tuple[str, str], ...]:
    """`HOME`, `WS_HOME` and the four `WS_*_DIR` names as a command word sees them, made once for the environment they
    come from (`key`, from `_env_key`). `WS_HOME` is the one folder of a single-folder layout, else the config folder."""
    lay = paths.layout()
    return (
        ("HOME", _home().rstrip("/") or "/"),
        ("WS_HOME", str(lay.config)),
        *((var, str(lay.dir(kind))) for kind, var in paths.ENV_DIRS.items()),
    )


def _env(known: dict[str, str] | None = None, cwd: str = "", oldpwd: str = "") -> dict[str, str]:
    """The variables a command word may use: `HOME` and `WS_HOME`, the folder the command runs in as `PWD` (and the one
    before the last `cd` as `OLDPWD`) when it is given, then what earlier parts of the line set."""
    where = {"PWD": cwd} if cwd else {}
    if oldpwd:
        where["OLDPWD"] = oldpwd
    return {**dict(_base_env(_env_key())), **where, **(known or {})}


def _expand(path: str) -> str:
    """~, ~user, $HOME, ${HOME}, $WS_HOME, $WS_STATE_DIR (and the other ws names, with or without braces) to a real prefix: a
    path as a rule or a tool writes it."""
    return shellwords.expand_path(path, _env()) if path[:1] in ("~", "$") else path


def _abs(path: str, cwd: str, expand: bool = True) -> str:
    """The path as an absolute, normalised one. A word of a command was expanded when it was read (a quoted `~` is a
    folder of that name), so it is passed with `expand=False`; a path from a rule or a tool is expanded here."""
    p = _expand(path) if expand else path
    if not p.startswith("/"):
        p = posixpath.join(cwd, p)
    return posixpath.normpath(re.sub(r"^/{2,}", "/", p))


def _canon_path(tok: str) -> str:
    """One spelling for the same place: ~, $HOME, ${HOME} and the real home dir all become ~; // and /./ and
    trailing slashes go. Only for tokens that look like a path."""
    home = _home().rstrip("/")
    if not re.search(r"[*?\[]", tok):
        full = _expand(tok)
        if not full.startswith("/"):
            return tok
        full = _abs(full, "/")  # resolve .. and // first: ~/../x is not under ~
        if home and (full == home or full.startswith(home + "/")):
            return "~" + full[len(home) :]
        return full
    for pre in ("${HOME}", "$HOME"):
        if tok == pre or tok.startswith(pre + "/"):
            tok = "~" + tok[len(pre) :]
    if home and (tok == home or tok.startswith(home + "/")):
        tok = "~" + tok[len(home) :]
    if tok.startswith("~/") or tok == "~":
        rest = tok[1:]
        if "*" not in rest and "?" not in rest and "[" not in rest:
            rest = posixpath.normpath(rest) if rest else ""
            if rest in (".", "/"):
                rest = ""
        return "~" + rest.rstrip("/") if rest != "/" else "~"
    if tok.startswith("/"):
        tok = re.sub(r"/{2,}", "/", tok)
        if not re.search(r"[*?\[]", tok):
            tok = posixpath.normpath(tok)
        return tok.rstrip("/") or "/"
    return tok


def _is_pathlike(tok: str) -> bool:
    return tok.startswith(("/", "~", "$HOME", "${HOME}", *(p for n in _WS_NAMES for p in (f"${n}", f"${{{n}}}"))))


def _merge_flags(tokens: list[str]) -> list[str]:
    """Merge the short flags straight after the command word into one sorted token, so `-r -f`, `-fr` and `-rf` are
    one spelling. For rm, `-R`, `--recursive` and `--force` fold into -r and -f."""
    if not tokens:
        return tokens
    cmd, i, letters, rest_at = tokens[0], 1, set(), 1
    rm = cmd == "rm"
    while i < len(tokens):
        t = tokens[i]
        if _FLAGS.match(t):
            letters |= set(t[1:])
        elif rm and t in ("--recursive", "--force"):
            letters.add("r" if t == "--recursive" else "f")
        else:
            break
        i += 1
        rest_at = i
    if rm and "R" in letters:
        letters = (letters - {"R"}) | {"r"}
    if not letters:
        return tokens
    return [cmd, "-" + "".join(sorted(letters)), *tokens[rest_at:]]


def git_split(toks: list[str]) -> tuple[list[str], list[str]]:
    """(the global options with their values, the command without them) for the words of a `git` command:
    `git -C d -c k=v add x` gives (["-C", "d", "-c", "k=v"], ["git", "add", "x"])."""
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in _GIT_ARG_OPTS else 1
    return toks[1:i], ["git", *toks[i:]]


def _git_normal(toks: list[str]) -> list[str]:
    """Drop git's global options (`-C dir`, `-c k=v`, `--git-dir=...`) so the subcommand is where a rule expects it;
    spell any forcing or destroying push (`-f`, `-uf`, `+refspec`, `:ref`, `--mirror`, `--delete`) as `--force`."""
    toks = git_split(toks)[1]
    forcing = len(toks) > 1 and toks[1] == "push" and "--force" not in toks
    if forcing and any(
        re.fullmatch(r"-[A-Za-z]*f[A-Za-z]*", t)
        or t.startswith(("+", ":"))
        or ":+" in t
        or t in ("--mirror", "--delete", "-d")
        for t in toks[2:]
    ):
        toks.append("--force")
    return toks


def _canon_tokens(tokens: list[str]) -> list[str]:
    if tokens and tokens[0] == "git":
        tokens = _git_normal(tokens)
    tokens = _merge_flags(tokens)
    return [_canon_path(t) if _is_pathlike(t) else t for t in tokens]


# --- paths: deny_read, protected files ----------------------------------------------------------------------------


def _glob_class(pattern: str, i: int) -> tuple[str, int] | None:
    """The bracket class opening at `pattern[i]` as a regex class and the index of its `]`, or None when it is not
    one (no `]` before a `/`, or a range like `s-c` that runs backwards), so the `[` is matched as itself."""
    j = pattern.find("]", i + 2)
    if j < 0 or "/" in pattern[i + 1 : j]:
        return None
    body = pattern[i + 1 : j]
    neg = body[0] in "!^"
    body = body[1:] if neg else body
    parts, k = [], 0
    while k < len(body):
        if k + 2 < len(body) and body[k + 1] == "-":
            if body[k] > body[k + 2]:
                return None
            parts.append(re.escape(body[k]) + "-" + re.escape(body[k + 2]))
            k += 3
        else:
            parts.append(re.escape(body[k]))
            k += 1
    return "[" + ("^" if neg else "") + "".join(parts) + "]", j


# macOS file systems are case-insensitive by default: `~/.SSH/ID_RSA` is the same file as `~/.ssh/id_rsa`, so path rules
# match without regard to case there. (Linux file systems are case-sensitive.)
CASELESS_PATHS = sys.platform == "darwin"


def _glob_regex_path(pattern: str) -> re.Pattern:
    out, i = [], 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
            continue
        if pattern.startswith("**", i):
            out.append(".*")
            i += 2
            continue
        if c == "*":
            out.append("[^/]*")
        elif c == "?":
            out.append("[^/]")
        elif c == "[" and (cls := _glob_class(pattern, i)) is not None:
            out.append(cls[0])
            i = cls[1]
        else:
            out.append(re.escape(c))
        i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL | (re.IGNORECASE if CASELESS_PATHS else 0))


def _path_regex(pattern: str) -> re.Pattern:
    """gitignore-style path glob: `**` crosses folders, `*` stays in one, a bare name matches at any depth."""
    pattern = _expand(pattern)
    if not pattern.startswith("/"):
        pattern = "**/" + pattern.removeprefix("./")
    return _glob_regex_path(pattern)


def _rule_root(rule: str) -> str | None:
    """The folder (or file) a deny_read rule is about: `~/.ssh/**` -> ~/.ssh, `~/.aws/credentials` as it is."""
    p = _expand(rule)
    if not p.startswith("/"):
        return None
    parts = []
    for seg in p.split("/"):
        if re.search(r"[*?\[]", seg):
            break
        parts.append(seg)
    return posixpath.normpath("/".join(parts) or "/")


def _glob_segment(seg: str) -> re.Pattern | None:
    """One path segment as a pattern (`*`, `?`, bracket classes), or None when it cannot be read."""
    try:
        return _glob_regex_path(seg)
    except re.error:
        return None


def _segments_reach(glob: list[str], root: list[str]) -> bool:
    """Can the glob's segments, taken one for one over the root's segments, all match? `**` takes any number of
    segments (none too). What the glob has after the root does not matter: it is already inside it. A glob that ends
    above the root names a folder above it, not the root. A segment it cannot read counts as a match (fails closed)."""
    seen: set[tuple[int, int]] = set()

    def walk(gi: int, ri: int) -> bool:
        if ri == len(root):
            return True
        if gi == len(glob) or (gi, ri) in seen:
            return False
        seen.add((gi, ri))
        g = glob[gi]
        if g == "**":
            return walk(gi, ri + 1) or walk(gi + 1, ri) or walk(gi + 1, ri + 1)
        rx = _glob_segment(g)
        return (rx is None or rx.match(root[ri]) is not None) and walk(gi + 1, ri + 1)

    return walk(0, 0)


def _glob_reaches(policy: Policy, token_abs: str) -> str | None:
    """Could this glob (`~/.s*/id_rsa`, `~/.config/ws/secret?/KEY`) name something a deny_read rule covers? It does when,
    segment by segment over the rule's folder, each of the glob's segments can match the rule's."""
    glob = [g for g in token_abs.split("/") if g]
    for rule in policy.gate.deny_read:
        root = _rule_root(rule)
        if root is None:
            continue
        if _segments_reach(glob, [r for r in root.split("/") if r]):
            return rule
    return None


@lru_cache(maxsize=16)
def _rules(
    deny_read: tuple[str, ...], home: str, env_key: tuple[str | None, ...]
) -> tuple[tuple[str, re.Pattern, str | None], ...]:
    """Each deny_read rule with its pattern and root, made once for a home and the ws folders (`home` and `env_key` are only
    the cache key: the expansion reads the same environment)."""
    return tuple((rule, _path_regex(rule), _rule_root(rule)) for rule in deny_read)


_KNOWN_NAMES = ("HOME", *_WS_NAMES, "PWD", "OLDPWD")
_UNSET = re.compile(r"\$(?:\{[^}]*\}|\([^)]*\)|[A-Za-z_][A-Za-z0-9_]*)|`[^`]*`")


def _unset_hit(policy: Policy, path: str, cwd: str) -> str | None:
    """A word of a command that still holds a variable or command the reader could not know (`/home/$USER/x`,
    `$(pwd)/x`) could name a protected path for some value of it. It is refused when what follows the last such part
    is a protected path under the home folder (`/home/$USER/` then a denied folder), or on its own; a word that is only
    the variable (`$FILE`) or ends in it says nothing about where it points, and is left alone."""
    found = [
        m for m in _UNSET.finditer(path) if m.group().strip("${}") not in _KNOWN_NAMES
    ]  # (a known name is quoted text)
    rest = path[found[-1].end() :] if found else ""
    if not rest.startswith("/"):
        return None
    for form in (_home().rstrip("/") + rest, rest):
        if hit := _read_hit(policy, form, cwd, expand=False):
            return hit
    return None


def _read_hit(
    policy: Policy, path: str, cwd: str, recursive: bool = False, folder: bool = True, expand: bool = True
) -> str | None:
    """The deny_read rule a path falls under, or None. With `folder`, naming the denied folder itself counts; with
    `recursive`, so does a folder above it (`grep -r x ~` reads `~/.ssh` too). `expand` is False for a word of a
    command, which the reader has already expanded."""
    if not policy.gate.deny_read or not path:
        return None
    p = _abs(path, cwd, expand)
    resolvable = shellwords.check_path(p)
    if not expand and ("$" in path or "`" in path) and (hit := _unset_hit(policy, path, cwd)):
        return hit
    if re.search(r"[*?\[]", p):
        return _glob_reaches(policy, p)
    forms = {p}
    if resolvable:
        try:
            forms.add(os.path.realpath(p))  # a symlink into a denied folder is still that folder
        except OSError:
            pass
    for rule, rx, root in _rules(policy.gate.deny_read, _home(), _env_key()):
        for f in forms:
            if rx.match(f):
                return rule
            if root is None:
                continue
            if folder and f == root:
                return rule
            if recursive and (f == "/" or root.startswith(f + "/")):
                return rule
    return None


def _under(paths: tuple[str, ...], path: str, cwd: str, ancestors: bool = False) -> str | None:
    """The protected path that `path` is, or is inside. With `ancestors`, also one that is inside `path`: removing,
    moving or unpacking over a folder takes everything under it too."""
    p = _abs(path, cwd)
    forms = {p, os.path.realpath(p)} if shellwords.check_path(p) else {p}
    if CASELESS_PATHS:
        forms = {f.casefold() for f in forms}
    for q in paths:
        qa = _abs(q, cwd)
        for target in {qa, os.path.realpath(qa)}:
            target = target.casefold() if CASELESS_PATHS else target
            if any(
                f == target or f.startswith(target + "/") or (ancestors and target.startswith(f + "/")) for f in forms
            ):
                return q
    return None


# --- one command line ---------------------------------------------------------------------------------------------


_FETCHERS = {"curl", "wget", "nc", "ncat", "ssh", "scp", "ftp", "fetch"}


def _dotted(op: str, cwd: str) -> str:
    """`.`, `..`, `*` and `../..` name a place that depends on where the command runs: spell it out."""
    if op and not op.startswith(("/", "~", "$", "-")) and re.fullmatch(r"[./*]+", op):
        return _canon_path(_abs(op, cwd))
    return op


def _gate_flag(rest: list[str]) -> bool:
    """`--gate launch` / `--gate=launch` (argparse takes any prefix of the flag, from `--g`) on a `ws install` line."""
    for i, t in enumerate(rest):
        name, eq, value = t.partition("=")
        if len(name) >= 3 and "--gate".startswith(name) and name.startswith("--g"):
            nxt = value if eq else (rest[i + 1] if i + 1 < len(rest) else "")
            if nxt != "every-session":
                return True
    return False


def _drop_cwd_flag(tokens: list[str]) -> list[str]:
    """`tokens` without `-C PATH`, `-CPATH`, `-C=PATH`, `--cwd PATH`, `--cwd=PATH` and the prefixes argparse accepts of
    `--cwd` (`--cw`, `--c`), which `ws` takes anywhere before a bare `--`. This is the only global flag with a value
    (`--json`, `--no-color` and `--version` take none), so once it is gone the first two words are the subcommand.
    A flag with no value left at the end is dropped too. Mirrors `output.split_global`, which raises on that and
    does not know the prefixes."""
    out: list[str] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            out.extend(tokens[i:])
            break
        name, eq, _ = tok.partition("=")
        if name.startswith("-C") and not name.startswith("--") and (len(name) > 2 or eq or tok == "-C"):
            i += 1 if tok != "-C" else 2
        elif len(name) >= 3 and name.startswith("--c") and "--cwd".startswith(name):
            i += 1 if eq else 2
        else:
            out.append(tok)
            i += 1
    return out


def _launched_by_package_runner(canon: list[str]) -> bool:
    """`uvx ...`, `uv run ...`, `uv tool run ...`, `pipx run ...` and `env ...`: a line whose own words may sit between the
    launcher and `ws` (`uv run --with pkg ws`, `uvx --from x ws`), which no single word before `ws` gives away."""
    head = posixpath.basename(canon[0]) if canon else ""
    return (
        head
        in (
            "uvx",
            "env",
            "taskset",
            "chrt",
            "ionice",
            "flock",
            "setpriv",
            "unshare",
            "systemd-run",
            "screen",
            "dtach",
            "abduco",
        )
        or (head == "uv" and (canon[1:2] == ["run"] or canon[1:3] == ["tool", "run"]))
        or (head in ("pipx", "pipenv", "poetry", "hatch", "pdm") and "run" in canon[1:3])
    )


_PY_GLUED_WS = re.compile(r"-[A-Za-z]*m(ws(\.__main__)?)\Z")  # `python3 -mws`, `python -Imws.__main__`


def _is_ws_word(canon: list[str], i: int) -> bool:
    """Whether word `i` names the ws program: `ws`, a path ending in it, or the module after python's `-m`."""
    word = canon[i]
    return posixpath.basename(word) == "ws" or (word == "ws.__main__" and i > 0 and canon[i - 1].startswith("-"))


_PY_VALUE_FLAGS = frozenset({"-X", "-W"})  # python's flags whose value is the next word


def _unglue_python_module(toks: list[str]) -> list[str] | None:
    """`python3 -mws ...` with the module as a word of its own, or None when the line isn't that. It reads the words as
    they were written, because normalising flags takes a glued `-mws` apart into letters."""
    if not (toks and posixpath.basename(toks[0]).startswith(("python", "pypy"))):
        return None
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        found = _PY_GLUED_WS.match(toks[i])
        if found:
            return [*toks[:i], "-m", found.group(1), *toks[i + 1 :]]
        i += 2 if toks[i] in _PY_VALUE_FLAGS else 1  # anything after the flags is the script's own words
    return None


def _tmux_admin(toks: list[str]) -> str | None:
    """An admin command anywhere in a tmux line. tmux runs text from many places (commands chained with `;`, options,
    hooks, `#()` in a format), so the line is not parsed: quotes and separators are flattened and every word is read. The
    one word left out is the value of `-t`, a pane's name. A message or title that names an admin command is refused
    too, which costs a rewording and is why this reads the whole line."""
    if not (toks and posixpath.basename(toks[0]) == "tmux"):
        return None
    words: list[str] = []
    after_target = False
    previous = ""
    for word in toks[1:]:
        # `-n -t ws ...`: a `-t` straight after another flag is that flag's value, so what follows it is still read
        is_target = word == "-t" and not previous.startswith("-")
        if not after_target and not is_target:
            joined = re.sub(r"""['"\\]""", "", word)  # the shell joins what these split
            for piece in re.sub(r"""[;&|\n()`#<>{},]+""", " ", joined).split():
                found = _PY_GLUED_WS.match(piece)
                words += ["-m", found.group(1)] if found else [piece]
        after_target = not after_target and is_target
        previous = word
    return _ws_admin(words, read_anywhere=True)


def _ws_admin(canon: list[str], xargs: bool = False, read_anywhere: bool = False) -> str | None:
    """`ws gate generate|init`, `ws secret ...`, `ws broker ...`, `ws approve ...`, `ws uninstall`, `ws upgrade`, `ws install
    --gate launch`, `ws skills adopt|restore`, `ws config fallback`, `ws session prepare|start`, `ws node join|remove`,
    `ws queue add|cancel|run` (also `uv run ws ...`, `python -m ws ...`): a gated session must not regenerate or take off its own gate, touch secrets, approve its own
    call, swap what the person installed or mint the role of a session (`prepare`) or overwrite its record (`start`;
    the hooks that run it are not tool calls, so they still work). A plain `ws install` only refreshes, so it stays
    allowed. `ws` is read after a launcher's own words (`uv run --with pkg ws`, `uvx --from x ws`) and at the end of
    a path (`/opt/bin/ws`, `./ws`). The text tmux is told to run is read too, and under `xargs` a `ws` whose subcommand
    may arrive on the pipe is refused, because it can't be seen. The table of admin commands is `ws.agentsession`, which
    makes `ws` refuse them too."""
    gate_words = {b for a, b in agentsession.ADMIN_SUB if a == "gate"}
    launched = read_anywhere or _launched_by_package_runner(canon)
    for i, t in enumerate(canon):
        if not _is_ws_word(canon, i) or not (
            i == 0
            or launched
            or "/" in t
            or canon[i - 1] in ("run", "-m", "--", "exec", "tool")
            or canon[i - 1].startswith("-")
        ):
            continue
        rest = _drop_cwd_flag(canon[i + 1 :])
        sub = [x for x in rest if not x.startswith("-")][:2]
        if xargs and (i == 0 or canon[i - 1] == "-m"):
            return "ws under xargs is not for a gated session: the pipe decides its words, so they can't be read"
        if sub[:1] == ["gate"] and gate_words & set(rest):
            return "ws gate generate/init is not for a gated session: it would rewrite its own gate"
        if sub[:1] and sub[0] in agentsession.ADMIN:
            return f"ws {sub[0]} is not for a gated session"
        if tuple(sub[:2]) in agentsession.ADMIN_SUB:
            return f"ws {sub[0]} {sub[1]} is not for a gated session"
        if sub[:1] == ["install"] and _gate_flag(rest):
            return "ws install --gate launch is not for a gated session: it would take off its own gate"
    return None


def _walk(policy: Policy, cmd: str, ctx: _Ctx, depth: int, cands: list[str]) -> str | None:
    """Look at every part of a command line. Fills `cands` with the strings the deny globs are tried on and returns
    the reason at once for the checks that are not globs (pipe into a shell, denied paths, protected files)."""
    if depth > _MAX_DEPTH:
        raise shellwords.ShellError("too_deep", _TOO_DEEP)
    pipelines, inner = shellwords.scan(shellwords.prep(cmd), heredocs=ctx.heredocs)
    nested: list[str] = list(inner)
    ctx.arith = ctx.arith or _ARITH_LINE.search(cmd) is not None
    for segs in pipelines:
        canon_segs = []
        for idx, seg in enumerate(segs):
            reason, canon, more = _segment(policy, seg, ctx, idx > 0, cands)
            if reason:
                return reason
            canon_segs.append(canon)
            nested += more
        if len(canon_segs) > 1:
            cands.append(" | ".join(canon_segs))
    for script in nested:
        reason = _walk(policy, script, ctx.fork(), depth + 1, cands)
        if reason:
            return reason
    return None


_DECLARERS = ("export", "declare", "typeset", "local", "readonly")
_READ_TAKES = frozenset(["a", "d", "i", "n", "N", "p", "t", "u"])  # the options of `read` that take a value
_MAX_LOOP_WORDS = 64  # different words a `for` list may hold before a command that uses its variable is refused
_MAX_LOOP_RUNS = 256  # readings of one command for the words of the `for` loops around it


def _read_values(toks: list[str], redirs: list[shellwords.Redirect]) -> dict[str, str]:
    """The variables `read -r NAME... <<< word` sets: the word split on blanks, the last name taking the rest."""
    here = [t for op, t in redirs if op == "<<<"]
    if not here:
        return {}
    names: list[str] = []
    i = 1
    while i < len(toks):
        t = toks[i]
        if t == "--":
            names += toks[i + 1 :]
            break
        if t.startswith("-") and len(t) > 1:
            i += 2 if t[-1] in _READ_TAKES else 1
        else:
            names.append(t)
            i += 1
    if not names:
        return {"REPLY": here[-1]}
    parts = here[-1].split(None, len(names) - 1)
    return {n: (parts[k] if k < len(parts) else "") for k, n in enumerate(names) if shellwords.ASSIGN.match(n + "=")}


def _loop_words(words: list[str]) -> list[str]:
    """The words of a `for` list, one reading each: a run of numbers counts as three (it is the same command with
    other numbers in it), and a list longer than _MAX_LOOP_WORDS is refused."""
    out: list[str] = []
    numbers = 0
    for w in words:
        if w.isdigit():
            numbers += 1
            if numbers > 3:
                continue
        if w not in out:
            out.append(w)
    if len(out) > _MAX_LOOP_WORDS:
        raise shellwords.ShellError(
            "too_many_words",
            f"a for loop takes more than {_MAX_LOOP_WORDS} different words; use a file and a read loop",
        )
    return out


def _segment(policy: Policy, seg: str, ctx: _Ctx, in_pipe: bool, cands: list[str]) -> tuple[str | None, str, list[str]]:
    """One pipeline segment, judged once for each word its `for` variables take (a loop's body runs for every word of
    its list), and the first refusal returned."""
    used = [n for n in ctx.loops or {} if re.search(r"\$\{?" + re.escape(n) + r"(?![A-Za-z0-9_])", seg)]
    if not used:
        return _segment_once(policy, seg, ctx, in_pipe, cands, {})
    assert ctx.loops is not None
    runs = list(itertools.product(*(ctx.loops[n] for n in used)))
    if len(runs) > _MAX_LOOP_RUNS:
        raise shellwords.ShellError(
            "too_many_words", "the for loops around this command take too many word combinations"
        )
    nested: list[str] = []
    last: tuple[str, list[str]] = ("", [])
    for run in runs:
        reason, canon, more = _segment_once(policy, seg, ctx, in_pipe, cands, dict(zip(used, run, strict=True)))
        if reason:
            return reason, canon, more
        last = (canon, more)
        nested += more
    return None, last[0], nested


_APPEND = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\+=")
_BLOCK_WORDS = ("do", "then", "else", "elif", "{", "(", "!", "time", "if", "while", "until")


def _assign(ctx: _Ctx, values: dict[str, str]) -> None:
    """Record what a part of the line sets. A name set a second time becomes unknown: the reader doesn't follow which
    assignment ran (a subshell, a branch that didn't run, a pipe stage), so it never guesses."""
    known = dict(ctx.vars or {})
    again = {name for name in values if name in known or name in ctx.unknown or ctx.distrust}
    if ctx.loops:
        again |= set(values) & set(ctx.loops)
        ctx.loops = {k: v for k, v in ctx.loops.items() if k not in values}
    for name in again:
        known.pop(name, None)
    known.update({name: value for name, value in values.items() if name not in again})
    ctx.vars, ctx.unknown = known, ctx.unknown | again


def _mark_unknown(ctx: _Ctx, seg: str, words: list[str]) -> None:
    """Names this part of the line sets in a way the reader doesn't follow become unknown: an append (`X+=`), an
    assignment inside a block or a loop body, and one written ahead of a command (it lasts for that command only)."""
    names = {m.group(1) for w in words if (m := _APPEND.match(w))}
    names |= {m.group(1) for m in re.finditer(r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*)\+=", seg)}
    rest = list(words)
    while rest and rest[0] in _BLOCK_WORDS:  # `do X=1`, `then X=1`, `{ X=1`: set inside a block or a loop body
        rest = rest[1:]
    lead = list(itertools.takewhile(shellwords.ASSIGN.match, rest))
    if lead and (len(rest) > len(lead) or len(rest) < len(words)):  # ahead of a command, or inside a block
        names |= {w.split("=", 1)[0] for w in lead}
    if words and words[0] in _ENV_WRAPPERS:  # env X=/ cmd, sudo X=/ cmd: set for the command it runs
        names |= {w.split("=", 1)[0] for w in words[1:] if shellwords.ASSIGN.match(w)}
    if names:
        _mark_names(ctx, names)


def _mark_names(ctx: _Ctx, names: set[str]) -> None:
    ctx.vars = {k: v for k, v in (ctx.vars or {}).items() if k not in names}
    ctx.unknown = ctx.unknown | names
    if ctx.loops:
        ctx.loops = {k: v for k, v in ctx.loops.items() if k not in names}


_STACK_ARG = re.compile(r"[+-][0-9]+")
# Where arithmetic is done on text the line gives: it runs the command substitutions inside an array subscript, quoted or not.
_ARITH_LINE = re.compile(r"\(\(|\[\[|(?<![\w-])let\s|\b(?:declare|typeset|local)\b[^\n;&|]*\s-\w*i")
_ARITH_CMDS = frozenset(
    [
        "let",
        "declare",
        "typeset",
        "local",
        "readonly",
        "export",
        "read",
        "unset",
        "getopts",
        "mapfile",
        "readarray",
        "wait",
        "[[",
    ]
)
_ENV_WRAPPERS = ("env", "sudo", "doas", "run0")
_SETTERS = frozenset(["eval", "source", ".", "coproc"])  # run text or a file that can set any variable
_TARGET_SETTERS = frozenset(["read", "mapfile", "readarray", "getopts", "let"])  # set the names they're given


def _sets_anything(toks: list[str], seg: str) -> bool:
    """Whether this part of the line runs a command that can set variables in a way the reader doesn't follow: eval
    and source text, read and its kin, printf -v, wait -p, a nameref (declare -n), or arithmetic that assigns."""
    if not toks:
        return False
    head, flags = toks[0], [t for t in toks[1:] if t.startswith("-")]
    if head in _SETTERS:
        return True
    if head == "printf" and any(f.startswith("-") and "v" in f[1:] for f in flags):
        return True
    if head == "wait" and any("p" in f[1:] for f in flags):
        return True
    return head in _DECLARERS and any(not f.startswith("--") and "n" in f[1:] for f in flags)


def _targets(toks: list[str]) -> set[str]:
    """The names `read`, `mapfile`, `readarray`, `getopts` and `let` set: their values come from input or arithmetic,
    which the reader doesn't follow, so a name that held a value before becomes unknown."""
    if not toks or toks[0] not in _TARGET_SETTERS:
        return set()
    if toks[0] == "let":
        return {m.group(1) for t in toks[1:] if (m := re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*[-+*/%&|^<>]*=", t))}
    names = [t for t in toks[1:] if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t)]
    return set(names[1:2]) if toks[0] == "getopts" else set(names)


def _distrust(ctx: _Ctx, folders: bool) -> None:
    """After a command that can set any variable, no value the reader holds is known; with `folders`, the home and
    working folders aren't either (eval text and a sourced file can set them, and a sourced file is whatever the
    project holds)."""
    gone = set(ctx.vars or {}) | set(ctx.loops or {}) | ({"HOME", "PWD", "OLDPWD", *_WS_NAMES} if folders else set())
    ctx.unknown = ctx.unknown | gone
    ctx.vars, ctx.loops, ctx.distrust = {}, {}, True


def _segment_once(
    policy: Policy, seg: str, ctx: _Ctx, in_pipe: bool, cands: list[str], loop_vars: dict[str, str]
) -> tuple[str | None, str, list[str]]:
    cands.append(" ".join(seg.split()))

    def read_env(defaults_unread: bool) -> shellwords.Unknown:
        return shellwords.Unknown(
            _env({**(ctx.vars or {}), **loop_vars}, ctx.cwd, ctx.oldpwd),
            ctx.unknown - set(loop_vars),
            ctx.distrust,
            keep={"HOME", "PWD", "OLDPWD", *_WS_NAMES} - ctx.unknown,  # after a command that sets anything but these
            defaults_unread=defaults_unread,
        )

    env = read_env(False)  # a default is its word (`${A:-x}` is x), as the checks of named paths have always read it
    env_u = read_env(True) if "${" in seg else None  # ...and left as written, for the part the reader cannot resolve
    lexed = shellwords.lex(seg, env, ctx.budget)
    words, redirs = lexed.words, lexed.redirs
    if not words and not redirs:
        return None, "", []
    _mark_unknown(ctx, seg, words)
    if words and all(shellwords.ASSIGN.match(w) for w in words):  # X=/ ; later $X
        _assign(ctx, _settled(ctx, env, dict(w.split("=", 1) for w in words)))
    elif len(words) > 3 and words[0] == "for" and words[2] == "in":  # for i in a b; do ... $i
        listed = _loop_words(words[3:])
        ctx.loops = {**(ctx.loops or {}), words[1]: listed}
        ctx.vars = {**(ctx.vars or {}), words[1]: words[-1]}  # what the variable holds once the loop is done
    nested: list[str] = []
    dropped: list[str] = []
    toks, wrappers = shellwords.strip_wrappers(words, lexenv=env, budget=ctx.budget, dropped=dropped, scripts=nested)
    held = _targets(toks) & (set(ctx.vars or {}) | set(ctx.loops or {}) | set(ctx.unknown))
    if held:  # read X / let X=.. after X held a value: which value it has now isn't followed
        _mark_names(ctx, held)
    if toks and toks[0] in _DECLARERS:  # export X=/ ; declare -x X=/ ; local X=/
        _assign(ctx, _settled(ctx, env, dict(w.split("=", 1) for w in toks[1:] if shellwords.ASSIGN.match(w))))
    elif toks and toks[0] == "read":  # read -r X <<< /
        _assign(ctx, _read_values(toks, redirs))
    if _sets_anything(toks, seg):
        # a sourced file or a printf -v can set the home and working folders as eval text can; in the reading that takes a
        # here-document body for commands, a body line that starts with such a word is prose, and the other reading
        # judges the real ones
        _distrust(ctx, folders=not ctx.bodies_as_commands or toks[0] in ("eval", "coproc"))
    canon = _canon_tokens(toks)
    line = " ".join(canon)
    if line:
        cands.append(line)
    cmd = canon[0] if canon else ""
    flags, operands = shellwords.split_args(canon)

    if cmd in ("popd", "dirs") or (cmd == "pushd" and (flags or not operands or _STACK_ARG.fullmatch(operands[0]))):
        # the directory stack is not followed, so where these leave the shell is not known
        ctx.cwd_unknown = True
        _mark_names(ctx, {"PWD", "OLDPWD"})
    elif cmd in ("cd", "pushd"):
        target = operands[0] if operands else "~"
        where = unknownpath.split(target, env, _producer(ctx, env))
        unread = _unread_cd_target(ctx, seg, env_u) if env_u is not None else None
        # a default on the target (`cd ${DIR:-.}`) may not be the place the shell goes to
        unresolved = unread is not None and unknownpath.has_unknown(unknownpath.split(unread, env, _producer(ctx, env)))
        if unknownpath.has_unknown(where) or unresolved or _cdpath_may_apply(ctx, "".join(where)):
            # where it goes is not known: nor is a relative path
            ctx.cwd_unknown = True
            _mark_names(ctx, {"PWD", "OLDPWD"})
        elif target != "-" or ctx.oldpwd:
            ctx.oldpwd, ctx.cwd = ctx.cwd, _abs(ctx.oldpwd if target == "-" else "".join(where), ctx.cwd)
            if ctx.cwd_unknown and target.startswith(("/", "~")):
                ctx.cwd_unknown = False  # a full path says where it is
    unglued = _unglue_python_module(toks)
    if toks and posixpath.basename(toks[0]) == "tmux":
        admin = _tmux_admin(toks)  # the words as written: normalising flags moves them
    else:
        admin = _ws_admin(canon, "xargs" in wrappers) or (_ws_admin(unglued) if unglued else None)
    if admin:
        return admin, line, []
    nested += _arith_scripts(ctx, cmd, flags, toks, words)
    if cmd == "eval":
        text = " ".join(toks[1:])
        fetched = re.match(r"\s*(?:\$\(|`)\s*(\S+)", text)
        if fetched and posixpath.basename(fetched.group(1)) in _FETCHERS:
            return "eval of text a network command fetches", line, []
        nested.append(text)
    if _stem(cmd) in _SHELL_NAMES:
        reason, more = _shell(toks, canon, flags, operands, redirs, in_pipe)
        if reason:
            return reason, line, []
        nested += more
    if not ctx.bodies_as_commands and (started := _starts_a_shell(cmd, toks)):
        why = (
            f"`{cmd}` starts `{started}`, a shell, and ws cannot read what the shell runs; run `{started}` "
            "on its own, or write the commands to a file with the Write tool and run the file"
        )
        return why, line, []
    if any(op in ("<<", "<<-", "<<<") for op, _ in redirs) and (_reads_stdin_as_code(cmd) or "xargs" in wrappers):
        raise shellwords.ShellError(
            "stdin-is-code",
            f"{'xargs' if 'xargs' in wrappers else cmd} runs the text of the "
            "here-document or here-string it is given as code, and ws cannot tell what that is; write it to a file with "
            "the Write tool and run the file",
        )
    if (
        in_pipe
        and _INTERP.match(cmd)
        and operands in ([], ["-"])
        and not any(shellwords.has_flag(flags, c) for c in "cemp")
    ):
        return "an interpreter is reading its program from a pipe", line, []
    if cmd in ("source", ".") and shellwords.PROCSUB in operands:
        return "a script is being sourced from a process substitution", line, []
    if cmd in _OPERAND_CMDS:
        cands += [" ".join([cmd, *flags, _dotted(op, ctx.cwd)]) for op in operands[:64]]
    if cmd == "find":
        cands += _find_deletes(canon, ctx.cwd)
    if (
        "xargs" in wrappers
        and cmd == "rm"
        and (shellwords.has_flag(flags, "r") or shellwords.has_flag(flags, "R"))
        and all(o == "{}" for o in operands)
        and any(p.startswith("rm ") for p in policy.gate.deny_commands)
    ):
        return "recursive rm fed by xargs: the operands cannot be seen, and rm is on the deny list", line, []

    # Paths this command names: operands, what a wrapper took (`xargs -a FILE`), the places inside a word a program
    # reads a path from (`f=@P`, `if=P`, `--opt=P`, `-oP`), redirect targets.
    named = [*shellwords.split_args(toks)[1], *dropped, *lexed.pieces, *(t for _, t in redirs if t)]
    recursive = (
        cmd in _ALWAYS_RECURSIVE
        or shellwords.has_flag(flags, "r")
        or shellwords.has_flag(flags, "R")
        or "--recursive" in flags
        or "--archive" in flags
        or (cmd in ("cp", "rsync") and shellwords.has_flag(flags, "a"))
    )
    if recursive and _searches_cwd(cmd, canon, operands):
        named.append(".")  # `cd ~ && grep -r x`: no path given means here
    written = _written(cmd, flags, operands, redirs, canon, toks) if ctx.protect else []
    if not ctx.bodies_as_commands:  # text read as a command only to keep a body from hiding one: not a word of the line
        plain = _written(cmd, *shellwords.split_args(toks), redirs, toks, toks) if ctx.protect else []
        reason = _unresolved_code(ctx, toks, env) or _unknown_refusal(
            policy, ctx, cmd, toks, toks, redirs, _call_pieces(lexed, env), dropped, plain, env
        )
        if not reason and env_u is not None:  # the same line with each default left as written
            reason = _unknown_unread(policy, ctx, seg, env_u)
        if reason:
            return reason, line, nested
    for p in named:
        rule = _read_hit(policy, p, ctx.cwd, recursive=recursive, expand=False)
        if rule:
            return f"command names a path denied by deny_read rule {rule!r}", line, nested
    for p, ancestors in written:
        hit = _under(ctx.protect, p, ctx.cwd, ancestors)
        if hit:
            return f"command writes to {hit}, which ws protects (policy and generated gate)", line, nested
    return None, line, nested


# --- a part of a path the reader cannot resolve ----------------------------------------------------------------------

HOW_TO_SPELL = 'write the path out in full, or set the variable on its own first (F=notes/report.txt; cat "$F")'
"""The one-line way out every refusal of an unresolved part carries."""


def _names(text: str) -> frozenset[str]:
    return frozenset(text.split())


# The commands that never open the words they are given as files, so a word with a part the reader cannot resolve
# (`echo $X`, `sleep $N`, `kill $PID`) is not read as a file name. This is the ONE list of exemptions; every other command
# is read as one that may open any word it is given, however it is spelled (a program no list names included): the
# list is of what is known to be safe, never of what is known to be dangerous. The value is the short flags (letters) and
# long flags that make the command open a file after all, which end its exemption.
_NO_FILE_CMDS: dict[str, tuple[str, tuple[str, ...]]] = {
    # print or test: the words are text, numbers, names or metadata
    "echo": ("", ()),
    "printf": ("v", ()),
    "true": ("", ()),
    "false": ("", ()),
    ":": ("", ()),
    "test": ("", ()),
    "[": ("", ()),
    "[[": ("", ()),
    "type": ("", ()),
    "which": ("", ()),
    "date": ("fr", ("--file", "--reference")),
    "sleep": ("", ()),
    "seq": ("", ()),
    "expr": ("", ()),
    "kill": ("", ()),
    "wait": ("", ()),
    # leave the shell, or set what the shell holds
    "exit": ("", ()),
    "return": ("", ()),
    "shift": ("", ()),
    "break": ("", ()),
    "continue": ("", ()),
    "unset": ("", ()),
    "export": ("", ()),
    "declare": ("", ()),
    "typeset": ("", ()),
    "local": ("", ()),
    "readonly": ("", ()),
    "let": ("", ()),
    "eval": ("", ()),  # its words are shell text, which is read as a script of its own
    "read": ("", ()),
    "getopts": ("", ()),
    # move the shell about (a `cd` to a place the reader cannot resolve makes relative paths unknown instead)
    "cd": ("", ()),
    "pushd": ("", ()),
    "popd": ("", ()),
    "pwd": ("", ()),
    "dirs": ("", ()),
    # the words of a loop or a case, which are values and not files (what the body does is read on its own)
    "for": ("", ()),
    "select": ("", ()),
    "case": ("", ()),
}
_OPTION_TAKERS = frozenset({"date"})  # exempt commands with an option that opens a file: a bare unknown word may be it
_ADDRESS_CMDS = frozenset(("curl", "wget", "sftp", "ftp", "ssh", "ping", "nc", "ncat", "telnet"))
_PATTERN_FIRST = frozenset(
    ("grep", "egrep", "fgrep", "rg", "ag", "ack", "sed", "awk", "gawk", "mawk", "nawk", "jq", "yq")
)
_VALUE_FLAGS = {  # short flags that take a number, a pattern or a delimiter, not a file
    "head": "nc",
    "tail": "nc",
    "cut": "dfcb",
    "sort": "kt",
    "uniq": "fsw",
    "fold": "w",
    "grep": "eABCmdD",
    "egrep": "eABCmdD",
    "fgrep": "eABCmdD",
    "rg": "eABCmgtTjM",
    "ag": "ABCm",
    "sed": "e",
    "awk": "Fv",
    "gawk": "Fv",
    "mawk": "Fv",
    "nawk": "Fv",
    "split": "abCnl",
    "truncate": "s",
    "chmod": "",
}
_REDIRECT_FILES = frozenset(("<", ">", ">>", ">|", "&>", "&>>", "<>"))


_HAS_PART = re.compile(r"[$`]")
_FILE_FLAGS = {  # address commands: the options that name a file (their positional words are hosts or URLs)
    "curl": ("TK", ("--upload-file", "--config")),
    "wget": ("i", ("--input-file", "--post-file")),
    "sftp": ("bF", ()),
    "ssh": ("iF", ()),
    "ftp": ("", ()),
    "rclone": ("", ("--config", "--files-from", "--filter-from", "--include-from", "--exclude-from")),
}


# --- a command the reader doesn't know, given a shell -----------------------------------------------------------------
#
# `taskset 1 sh -c TEXT`, `fakeroot sh`, `chrt 1 bash -c TEXT`, `unshare -r sh`, `ssh-agent sh`, `busybox sh` and `toybox sh`
# start a shell, and the reader, which looks only at the command a line runs, would take the shell's script for a word of
# something else. The wrappers it knows (`nice`, `sudo`, `env`, `busybox` ...) are taken off first, so the shell they start
# is the command that is read. For any other first word the rule is the allowlist's: a command the reader has no entry for,
# given a word that is a shell, is refused. A word is judged by the name of its file with a version or `-static` taken off
# (`bash5.2`, `/usr/bin/zsh5`, `mksh-static`, `sh5`), against the shell names the reader already has (`_SHELL_NAMES`; a
# name that merely ends in `sh` is not enough here, as it is for a here-document, or `push` and `publish` would be shells).
# Interpreters (`python3`, `node` ...) are not in this rule: run on their own they are a listed limit of the gate, so a
# launcher that starts one (`uv run python`) goes no further than that.
#
# A command is KNOWN when the reader has an entry for it: it is a key of one of the tables above (the exempt commands, the
# pattern-first and value-flag commands, the commands that remove, write or search, the address commands, the declarers,
# setters and arithmetic commands), a shell or an interpreter (read on their own), or one of the few the reader names in
# code (`git`, `find`, `ls`, `du`, `tree`) or that only print, list, show or check the text they are given (`_VIEWERS`).
# The words of a known command are read as its table says, so a shell named among them is a file or a pattern (`grep
# bash notes`, `ls /bin/sh`, `man bash`, `shellcheck -s bash x`, `which sh`, `git log -- foo.sh`).
_VIEWERS = frozenset(
    ("cat", "less", "more", "man", "file", "stat", "wc", "diff", "cmp", "dirname", "basename", "shellcheck", "shfmt")
)
_KNOWN_COMMANDS = frozenset(
    {
        *_NO_FILE_CMDS,
        *_PATTERN_FIRST,
        *_VALUE_FLAGS,
        *_OPERAND_CMDS,
        *_ALWAYS_RECURSIVE,
        *_WRITE_ALL,
        *_WRITE_LAST,
        *_FILE_FLAGS,
        *_ADDRESS_CMDS,
        *_FETCHERS,
        *_DECLARERS,
        *_ARITH_CMDS,
        *_SETTERS,
        *_TARGET_SETTERS,
        *_STDIN_IS_CODE,
        *_VIEWERS,
        "git",
        "find",
        "ls",
        "du",
        "tree",
    }
)


def _starts_a_shell(cmd: str, toks: list[str]) -> str | None:
    """The word of a command the reader doesn't know that is a shell, or None."""
    if cmd in _KNOWN_COMMANDS or _stem(cmd) in _SHELL_NAMES or _INTERP.match(_stem(cmd)):
        return None
    for word in toks[1:]:
        if _stem(word) in _SHELL_NAMES:
            return word
    return None


def _opens_files(cmd: str, canon: list[str]) -> bool:
    """Whether the words of this command are read as file names: every command except those `_NO_FILE_CMDS` lists, and
    those only while none of their file-taking flags is given."""
    exempt = _NO_FILE_CMDS.get(cmd)
    if exempt is None:
        return True
    letters, longs = exempt
    for t in canon[1:]:
        if t.startswith("--"):
            if shellwords.long_option(t.split("=", 1)[0], longs):
                return True
        elif t.startswith("-") and len(t) > 1 and any(c in letters for c in t[1:]):
            return True
    return False


_PROGRAM_TEXT = {  # an interpreter's short flags whose value is the program, not a file
    "python": "c",
    "perl": "eE",
    "ruby": "e",
    "node": "e",
    "php": "r",
}


def _value_letters(cmd: str) -> str:
    interp = re.match(r"(python|perl|ruby|node|php)[0-9.]*$", cmd)
    return _VALUE_FLAGS.get(cmd, "") + (_PROGRAM_TEXT[interp.group(1)] if interp else "")


def _file_operands(cmd: str, canon: list[str]) -> tuple[list[str], list[str]]:
    """The words of a file command that name files (not its flags, the values of the flags that take a number, a pattern
    or a program, nor the search pattern or script a grep, sed or awk starts with) and the words set aside, which are
    text: the places in them a program may read a path from (`a=b`, `<x`) are not files either."""
    letters = _value_letters(cmd)
    out: list[str] = []
    text: list[str] = []
    ended, skip, has_pattern_flag = False, False, False
    for t in canon[1:]:
        if skip:
            skip = False
            text.append(t)
            continue
        if not ended and t == "--":
            ended = True
        elif not ended and t.startswith("-") and len(t) > 1:
            if t.startswith("--"):
                has_pattern_flag |= t.split("=")[0] in ("--regexp", "--file", "--expression")
            elif t[-1] in letters:
                skip = True
            has_pattern_flag |= not t.startswith("--") and any(c in t[1:] for c in "ef")
        else:
            out.append(t)
    if cmd in _PATTERN_FIRST and not has_pattern_flag and out:
        text.append(out[0])
        out = out[1:]
    return out, text


def _deny_alts(policy: Policy) -> list[tuple[str, tuple]]:
    """Each deny_read rule with the patterns a path under it can be written as: the rule's glob (a bare name matches at
    any depth), and the folder the rule is about."""
    out = []
    for rule in policy.gate.deny_read:
        glob = _expand(rule)
        if not glob.startswith("/"):
            glob = "**/" + glob.removeprefix("./")
        alts = unknownpath.tokens(glob)
        root = _rule_root(rule)
        if root:
            alts = (*alts, *unknownpath.literal(root))
        out.append((rule, alts))
    return out


def _produced_hit(policy: Policy, ctx: _Ctx, text: str, word: str, how: str, written) -> str | None:
    """A refusal for a word whose command substitutions all printed something known (see `_produced`), when the path
    it spells is one the policy denies to read or ws protects: the same checks a path written out in full gets."""
    rule = _read_hit(policy, text, ctx.cwd, recursive=False, expand=False)
    if rule:
        return f"command names {word!r}, which is {text}, a path denied by deny_read rule {rule!r}"
    for p, ancestors in written:
        if p == word and (hit := _under(ctx.protect, text, ctx.cwd, ancestors)):
            return f"command writes {word!r}, which is {text}, and that is inside {hit}, which ws protects"
    return None


def _pattern_of(word: str, ctx: _Ctx, known, prod=None) -> list | None:
    """The word as a pattern with None for what is not known (a glob character counts: it matches anything), absolute;
    None when it holds nothing unresolved and the folder it is relative to is known."""
    if word == "~" or word.startswith("~/"):  # the reader writes a path under the home folder as ~/...
        word = _home().rstrip("/") + word[1:]
    items = unknownpath.split(word, known, prod)
    relative = bool(items) and items[0] not in ("/", None)
    if not unknownpath.has_unknown(items) and not (ctx.cwd_unknown and relative):
        return None
    items = unknownpath.unconfined(unknownpath.anchor(items, ctx.cwd, not ctx.cwd_unknown))
    return [None if c in ("*", "?", "[") else c for c in items]


def _protect_hit(items: list, protect: tuple[str, ...], cwd: str, ancestors: bool) -> str | None:
    for q in protect:
        qa = _abs(q, cwd)
        alts = (*unknownpath.literal(qa), *(tuple(("c", c) for c in qa) + (("c", "/"), ("**", "")),))
        if unknownpath.reaches(items, alts, CASELESS_PATHS):
            return q
        if ancestors:
            cut = [k for k, c in enumerate(qa) if c == "/" and k]
            if any(unknownpath.reaches(items, unknownpath.literal(qa[:k]), CASELESS_PATHS) for k in cut):
                return q
    return None


def _unknown_refusal(
    policy: Policy, ctx: _Ctx, cmd: str, canon, toks, redirs, pieces, dropped, written, env
) -> str | None:
    """A refusal for a path word whose value the reader cannot know, when the word could be a path ws protects.

    An allow-list reading: the unresolved part (an unknown variable, a command substitution) stands for any string, and
    a word that some such string makes a protected path is refused, whatever the shell would really put there. It
    applies to the commands that read or write the files they are given, and to every redirect target. A word that is
    only the unresolved part (`cat $X`) is refused for those commands too: it names any file there is. `canon` is the
    command's words as the reader found them: not tidied, because `..` after an unresolved part cannot be taken out."""
    known = frozenset(env)
    operands, aside = _file_operands(cmd, canon)
    text = {p for w in aside for p in shellwords.pieces_of(w, env)}
    pieces = [p for p in pieces if p not in text]
    is_file = _opens_files(cmd, canon) or (
        # a whole word the reader cannot resolve may be an option (`--file=P`), which the exemption is for words without
        cmd in _OPTION_TAKERS and any(unknownpath.is_bare(unknownpath.split(t, known, None)) for t in canon[1:])
    )
    if not (is_file or redirs or written) or not (policy.gate.deny_read or ctx.protect):
        return None
    deny = _deny_alts(policy)
    words: list[tuple[str, str]] = []  # (word, how the command takes it: operand, piece, flag, redirect or written)
    if is_file:
        words += [(w, "operand") for w in operands]
        words += [(w, "piece") for w in (*dropped, *pieces)]
        # an option's value glued to its letter (`-o$X`, `-i$X`), which no list of options could name
        words += [(t[2:], "flag") for t in canon[1:] if re.match(r"-[^-]", t) and len(t) > 2 and _HAS_PART.search(t)]
        files = _FILE_FLAGS.get(cmd)  # the files they send or read: -T FILE, -K FILE, -b FILE, --post-file FILE
        if files:
            words += [(w, "piece") for w in shellwords.option_values(toks, files[0], files[1])]
    words += [(t, "redirect") for op, t in redirs if t and op in _REDIRECT_FILES]
    words += [(p, "written") for p, _ in written]
    prod = _producer(ctx, env)
    seen: set[tuple[str, str]] = set()
    for word, how in words:
        if (word, how) in seen:
            continue
        seen.add((word, how))
        items = unknownpath.split(word, known, prod)
        if unknownpath.is_bare(items):
            takes = {"redirect": "a redirect to", "written": f"{cmd} writing to"}.get(how, f"{cmd} the file")
            return (
                f"command gives {takes} {word!r}, which ws cannot read: it could be any file, including one ws "
                f"protects; {HOW_TO_SPELL}"
            )
        if how == "operand" and cmd in _ADDRESS_CMDS and "@" not in word and _URL.match(word):
            continue  # a scheme ahead of the unresolved part: an address, and not the file a bare word could be
        pattern = _pattern_of(word, ctx, known, prod)
        if pattern is None:
            if not unknownpath.has_unknown(items) and ("$(" in word or "`" in word):
                reason = _produced_hit(policy, ctx, "".join(items), word, how, written)
                if reason:
                    return reason
            continue
        if len(pattern) > unknownpath.MAX_WORD:
            return f"a path word is too long to check ({len(pattern)} characters); {HOW_TO_SPELL}"
        for p, ancestors in written:
            if p == word and (hit := _protect_hit(pattern, ctx.protect, ctx.cwd, ancestors)):
                return (
                    f"command writes {word!r}, which holds a part ws cannot read, and some value of it would be {hit}, "
                    f"which ws protects; {HOW_TO_SPELL}"
                )
        for rule, alts in deny:
            if unknownpath.reaches(pattern, alts, CASELESS_PATHS):
                return (
                    f"command names {word!r}, which holds a part ws cannot read, and some value of it would be a path "
                    f"denied by deny_read rule {rule!r}; {HOW_TO_SPELL}"
                )
    return None


# --- what a command prints, when it is known ------------------------------------------------------------------------

_MKTEMP_FLAGS = frozenset(("-d", "-u", "-q", "-t", "--directory", "--dry-run", "--quiet"))
_REALPATH_FLAGS = frozenset(("-m", "-e", "-s", "-f", "--canonicalize", "--canonicalize-missing"))


def _producer(ctx: _Ctx, env, depth: int = 0) -> Callable[[str], str | None]:
    """The function `unknownpath.split` asks about each `$(...)`: what the command inside prints."""
    return lambda inner: _produced(inner, ctx, env, depth)


def _produced(inner: str, ctx: _Ctx, env, depth: int = 0) -> str | None:
    """The text a command substitution prints, when the command is one of a short list whose output is a path in a known
    place and every word it is given is literal or known: `pwd`, `mktemp -p ABS` (or `--tmpdir=ABS`, or the `TMPDIR` the
    line itself set), `dirname`, `basename`, `readlink -f` and `realpath`. A word it is given that is not known makes its
    output unknown (None), and so does every other command or option: the caller then reads the part as one it cannot
    resolve. The shell's own `TMPDIR` and `PATH` are not known (a project's settings can set them), so a bare `mktemp`,
    `which` and `command -v` print an unknown path; so does the repository root (`core.worktree`, `GIT_WORK_TREE` and
    `GIT_DIR` move it). The caller checks the text against the protected paths like any path the command line spells
    out."""
    if depth > 4 or len(inner) > 512 or re.search(r"[;&|<>\n]", inner):
        return None
    try:
        words = shellwords.lex(inner, env, ctx.budget).words
    except shellwords.ShellError:
        return None
    known = frozenset(env)
    nested = _producer(ctx, env, depth + 1)

    def literal(word: str) -> str | None:
        items = unknownpath.split(word, known, nested)
        return None if unknownpath.has_unknown(items) else "".join(items)

    args = [literal(w) for w in words[1:]]
    if not words or None in args:
        return None
    cmd, argv = words[0], [a for a in args if a is not None]
    flags = [a for a in argv if a.startswith("-")]
    ops = [a for a in argv if not a.startswith("-")]
    here = None if ctx.cwd_unknown else ctx.cwd
    if cmd == "pwd" and set(flags) <= {"-L", "-P"} and not ops:
        return here
    if cmd == "mktemp":
        return _mktemp(argv, here, env.get("TMPDIR"))
    if cmd in ("dirname", "basename") and not flags and len(ops) == 1 and ops[0].strip("/") and "\n" not in ops[0]:
        # (the trailing slashes go first, as the tools do: `dirname /a/b/` is `/a`)
        return (
            posixpath.dirname(ops[0].rstrip("/")) or "." if cmd == "dirname" else posixpath.basename(ops[0].rstrip("/"))
        )
    if cmd in ("realpath", "readlink") and len(ops) == 1 and set(flags) <= _REALPATH_FLAGS and "\n" not in ops[0]:
        if cmd == "readlink" and flags != ["-f"]:
            return None
        if not ops[0].startswith("/") and here is None:
            return None
        return os.path.realpath(posixpath.join(here or "/", ops[0]))
    return None


def _mktemp(argv: list[str], here: str | None, tmpdir: str | None) -> str | None:
    """Where `mktemp` puts what it makes: the folder given with `-p DIR` or `--tmpdir=DIR` (a full path), the working
    folder for a template given on its own, or the `TMPDIR` the line itself set (a bare `mktemp`, `-t` and `--tmpdir`
    use it). Anywhere else it is the shell's own `TMPDIR`, which is not known, and so is any other option (None)."""
    folder: str | None = None
    in_tmp = False
    template = ""
    skip = False
    for k, a in enumerate(argv):
        if skip:
            skip = False
        elif a == "-p":
            folder, skip = (argv[k + 1] if k + 1 < len(argv) else None), True
            if folder is None or not folder.startswith("/"):
                return None
        elif a.startswith("--tmpdir="):
            folder = a.split("=", 1)[1]
            if not folder.startswith("/"):
                return None
        elif a in ("--tmpdir", "-t"):
            in_tmp = True
        elif a.startswith("--suffix=") or a in _MKTEMP_FLAGS:
            continue
        elif a.startswith("-") or template:
            return None
        else:
            template = a
    name = template or "tmp.XXXXXXXXXX"
    if name.startswith("/"):
        return posixpath.normpath(name)
    if folder is None and (in_tmp or not template):
        if not tmpdir or not tmpdir.startswith("/"):
            return None
        folder = tmpdir
    if folder is None:
        folder = here
    return posixpath.normpath(posixpath.join(folder, name)) if folder else None


def _cdpath_may_apply(ctx: _Ctx, target: str) -> bool:
    """Whether `cd TARGET` may go somewhere other than the folder it names under the working folder. A relative name that
    does not start with `./` or `../` is looked for in each folder of `CDPATH` first, and `CDPATH` (like `cdable_vars`) is
    set in the shell the runner started, which no hook sees. So such a name is only known to mean the folder beside the
    working one when that folder is there: a name that is not an existing child of a known working folder is a place the
    reader cannot resolve. (A folder of that name in a `CDPATH` folder as well is the one limit; `./name` is exact.)"""
    if target in (".", "..", "-") or target.startswith(("/", "~", "./", "../")):
        return False
    if ctx.cwd_unknown or "CDPATH" in ctx.unknown or ctx.distrust:
        return True
    return not os.path.isdir(posixpath.join(ctx.cwd, target))


_URL = re.compile(r"(?!file:)[A-Za-z][A-Za-z0-9+.-]*://")
HOW_TO_NAME = "write the program out in full (`ls`, not `$CMD`), or run it from a script file"


def _substitutions(text: str) -> list[str]:
    """The text of every `$(...)` and pair of backticks in `text`, however it is quoted: arithmetic runs the ones in a
    word that single quotes held back from the shell."""
    out: list[str] = []
    i, n = 0, len(text)
    while i < n:
        if text.startswith("$(", i):
            j = shellwords.close_paren(text, i + 2)
            out.append(text[i + 2 : j])
            i = j + 1
        elif text[i] == "`":
            j = shellwords.backtick_end(text, i)
            if j < 0:
                out.append(text[i + 1 :])
                break
            out.append(shellwords.unbacktick(text[i + 1 : j]))
            i = j + 1
        else:
            i += 1
    return out


def _arith_scripts(ctx: _Ctx, cmd: str, flags: list[str], toks: list[str], words: list[str]) -> list[str]:
    """The command substitutions that arithmetic runs. A shell evaluates the text it is given by `let`, `declare`,
    `read`, `unset`, `test -v`, `[[ ... ]]`, `$(( ))`, `(( ))` and the like as an expression, and an array subscript in it
    (`a[$(cmd)]`) runs `cmd` even where single quotes kept the shell from reading it. In a line that does arithmetic, or
    for one of those commands, each word that holds `$(` or a backtick is read as the script it may run."""
    if ctx.bodies_as_commands:
        return []
    sub = bool(flags) and (shellwords.has_flag(flags, "v") or "-R" in flags)
    if not (ctx.arith or cmd in _ARITH_CMDS or (cmd in ("test", "[", "printf") and sub)):
        return []
    leading = list(itertools.takewhile(shellwords.ASSIGN.match, words))
    out: list[str] = []
    for w in [*toks, *leading]:
        if "$(" in w or "`" in w:
            out += _substitutions(w)
    return out


def _settled(ctx: _Ctx, env, values: dict[str, str]) -> dict[str, str]:
    """The values a line sets, with a command substitution in one replaced by what the command prints when that is known
    (`T=$(mktemp -d -p /tmp)`), else by one unresolved part without a blank in it: a later `$T` is then one word that is
    not known, and not the words of the command's text."""
    out = {}
    for name, value in values.items():
        if "$(" in value or "`" in value:
            items = unknownpath.split(value, frozenset(env), _producer(ctx, env))
            value = "".join(_UNKNOWN_VALUE if c is None else c for c in items)
        out[name] = value
    return out


def _unresolved_code(ctx: _Ctx, toks: list[str], env) -> str | None:
    """A refusal for a command word, or the text of an `eval`, that holds a part the reader cannot resolve: it is whatever
    the shell finds there, any program or any script, and the reader cannot tell what it would run."""
    if not toks or shellwords.ASSIGN.match(toks[0]):
        return None
    known = frozenset(env)

    def unresolved(word: str, made) -> bool:
        return unknownpath.has_unknown(unknownpath.split(word, known, made))

    if unresolved(toks[0], _producer(ctx, env)):
        return (
            f"the program to run is {toks[0]!r}, which holds a part ws cannot read, so ws cannot tell what runs; "
            f"{HOW_TO_NAME}"
        )
    if toks[0] == "eval" and any(unresolved(w, None) for w in toks[1:]):
        return (
            "eval would run text that holds a part ws cannot read, so ws cannot tell what runs; "
            "write the command out instead of eval"
        )
    return None


def _call_pieces(lexed: shellwords.Lexed, env) -> list[str]:
    """The places inside the words of a command that a program may read a path from, without those of the `NAME=value`
    words ahead of it: they set a variable for the command and are not files it is given."""
    lead = itertools.takewhile(shellwords.ASSIGN.match, lexed.words)
    own = {p for w in lead for p in shellwords.pieces_of(w, env)}
    return [p for p in lexed.pieces if p not in own]


def _unread_cd_target(ctx: _Ctx, seg: str, env: shellwords.Unknown) -> str | None:
    """The target of a `cd` or `pushd` in this part of the line with each default left as written, or None."""
    lexed = shellwords.lex(seg, env, ctx.budget)
    toks, _ = shellwords.strip_wrappers(lexed.words, lexenv=env, budget=ctx.budget, scripts=[])
    if not toks or toks[0] not in ("cd", "pushd"):
        return None
    operands = shellwords.split_args(toks)[1]
    return operands[0] if operands else None


def _unknown_unread(policy: Policy, ctx: _Ctx, seg: str, env: shellwords.Unknown) -> str | None:
    """`_unknown_refusal` for a part of the line that holds a default or alternative (`${A:-x}`), read with the default
    left as written: the shell's own environment may hold `A`, so the word is not `x`. The reading that expands the
    default stays the one the checks of named paths use."""
    lexed = shellwords.lex(seg, env, ctx.budget)
    dropped: list[str] = []
    toks, _ = shellwords.strip_wrappers(lexed.words, lexenv=env, budget=ctx.budget, dropped=dropped, scripts=[])
    cmd = toks[0] if toks else ""
    written = _written(cmd, *shellwords.split_args(toks), lexed.redirs, toks, toks) if ctx.protect else []
    return _unresolved_code(ctx, toks, env) or _unknown_refusal(
        policy, ctx, cmd, toks, toks, lexed.redirs, _call_pieces(lexed, env), dropped, written, env
    )


def _shell(toks, canon, flags, operands, redirs, in_pipe):
    """A shell interpreter: returns (reason, nested scripts). Denied when it is fed its script on stdin from a pipe
    or here-document, or from a process substitution; `bash script.sh` and `bash -c '...'` are fine (the -c text is
    checked on its own)."""
    here = any(op in ("<<<", "<<", "<<-") for op, _ in redirs)
    nested = [t for op, t in redirs if op == "<<<"]
    operands = list(operands)
    for f in flags:  # `-o pipefail` and `-O extglob` take a word that is not the script
        if f[1:2] != "-" and ("o" in f[1:] or "O" in f[1:]) and operands:
            operands.pop(0)
    if shellwords.PROCSUB in operands:
        return "a shell is reading its script from a process substitution", nested
    if shellwords.has_flag(flags, "c"):
        if operands and (operands[0].lstrip().startswith(("$(", "`")) or _UNKNOWN_VALUE in operands[0]):
            return "a shell is running text another command produces", nested
        if operands:
            return None, [*nested, operands[0]]
        stdin = True
    else:
        first = operands[0] if operands else None
        stdin = (
            shellwords.has_flag(flags, "s")
            or first is None
            or first in ("-", "/dev/stdin")
            or first.startswith(("/dev/fd/", "/proc/self/fd/"))
        )
    if stdin and (in_pipe or here):
        return "a shell is reading its script from a pipe or here-document", nested
    return None, nested


def _searches_cwd(cmd: str, canon: list[str], operands: list[str]) -> bool:
    """A recursive search that names no folder searches the one it runs in."""
    if cmd in ("grep", "egrep", "fgrep", "rg", "ag"):
        return len(operands) <= 1
    if cmd in ("ls", "du", "tree"):
        return not operands
    if cmd == "find":
        return not canon[1:2] or canon[1].startswith(("-", "(", "!"))
    return False


def _find_deletes(canon: list[str], cwd: str) -> list[str]:
    """`find P -delete` and `find P -exec rm ...` as the `rm -rf P` they amount to."""
    rest = canon[1:]
    deleting = "-delete" in rest or any(
        rest[i] in ("-exec", "-execdir") and rest[i + 1 : i + 2] == ["rm"] for i in range(len(rest))
    )
    if not deleting:
        return []
    starts = []
    for t in rest:
        if t.startswith(("-", "(", "!", ",")):
            break
        starts.append(t)
    return [f"rm -fr {_dotted(p, cwd)}" for p in (starts or ["."])]


def _written(cmd, flags, operands, redirs, canon, toks) -> list[tuple[str, bool]]:
    """Paths this command writes or removes, as (path, ancestors): redirect targets, operands of the commands that
    write, and where curl, wget, tar, unzip and zip put files. `ancestors` is true where a folder above a protected
    path is just as bad (rm, mv, and unpacking or copying a tree over it)."""
    out: list[tuple[str, bool]] = [(t, False) for op, t in redirs if op in shellwords.WRITE_OPS]
    recursive = any(shellwords.has_flag(flags, c) for c in "rRa")
    if cmd in _WRITE_ALL:
        out += [(o, cmd in ("rm", "rmdir", "mv", "shred", "unlink")) for o in operands]
    elif cmd in _WRITE_LAST:
        deep = recursive or cmd == "rsync"
        out += [(o, deep) for o in operands[-1:]]
        out += [(v, deep) for v in shellwords.option_values(toks, "t", ("--target-directory",))]
    elif cmd in ("sed", "perl", "ruby") and any(
        f.startswith("--in-place") or re.match(r"^-[A-Za-z]*i", f) for f in flags
    ):
        out += [(o, False) for o in operands]
    elif cmd == "dd":
        out += [(o[3:], False) for o in operands if o.startswith("of=")]
    elif cmd == "curl":
        out += [(v, False) for v in shellwords.option_values(toks, "o", ("--output",))]
        if any(t == "--remote-name" or re.fullmatch(r"-[A-Za-z]*O[A-Za-z]*", t) for t in toks[1:]):
            out.append((".", True))
    elif cmd == "wget":
        files = shellwords.option_values(toks, "O", ("--output-document",))
        dirs = shellwords.option_values(toks, "P", ("--directory-prefix",))
        out += [(v, False) for v in files] + [(v, True) for v in dirs]
        if not files and not dirs:
            out.append((".", True))
    elif cmd in ("tar", "bsdtar"):
        if len(toks) > 1 and re.fullmatch(r"[A-Za-z]+", toks[1]):  # old style: tar czf x.tgz
            toks = [toks[0], "-" + toks[1], *toks[2:]]
        modes = "".join(t[1:] for t in toks[1:] if re.fullmatch(r"-[A-Za-z]+", t))
        archives = shellwords.option_values(toks, "f", ("--file",))
        if "x" in modes:
            dirs = shellwords.option_values(toks, "C", ("--directory",))
            out += [(d, True) for d in dirs or ["."]]
        elif any(m in modes for m in "cru"):
            out += [(a, False) for a in archives]
    elif cmd == "unzip":
        out += [(d, True) for d in shellwords.option_values(toks, "d") or ["."]]
    elif cmd == "zip":
        out += [(o, False) for o in operands[:1]]
    return out


def _glob_regex(pattern: str) -> re.Pattern:
    """`*` matches anything, `\\*` a literal star, all else literal. A trailing ' *' also matches the bare command."""
    out, i, bare = [], 0, False
    if pattern.endswith(" *") and not pattern.endswith("\\ *"):
        pattern, bare = pattern[:-2], True
    while i < len(pattern):
        c = pattern[i]
        if c == "\\" and pattern[i + 1 : i + 2] == "*":
            out.append(r"\*")
            i += 2
            continue
        out.append(".*" if c == "*" else re.escape(c))
        i += 1
    return re.compile("".join(out) + ("(?: .*)?" if bare else "") + r"\Z", re.DOTALL)


def _canon_pattern(pattern: str) -> str:
    """Spell a deny pattern the way commands are spelled after normalising (flags merged, paths canonical)."""
    toks = _merge_flags(pattern.split())
    toks = [_canon_path(t) if _is_pathlike(t) and "\\" not in t else t for t in toks]
    return " ".join(toks)


def simple_commands(command: str, _depth: int = 0, *, git_options: bool = False) -> list[list[str]]:
    """Every simple command in a command line, as its words: quotes resolved, `sudo`/`env`/`timeout` wrappers and git's
    global options (`-C dir`, `-c k=v`) dropped (kept with `git_options`, for a check of the options themselves; see
    git_split). It reads the line as the matcher does (segments split on `;`, `&&`, pipes and newlines, and the text of
    `$(...)`, backticks, `bash -c` and `eval` read too), and leaves out what is not a command: quoted text, comments
    and here-document bodies. For a check that wants one command's words, such as a guard."""
    return [words for words, _ in simple_commands_env(command, _depth, git_options=git_options) if words]


def _seg_words(
    seg: str, git_options: bool, budget: shellwords.Budget, known: dict[str, str]
) -> tuple[list[str], list[str], list[str]]:
    """(words, assignments, nested command texts) of one pipeline segment: what simple_commands_env reads from it.
    `known` holds the variables earlier segments of the same line set; a segment of only assignments adds to it."""
    env: list[str] = []
    nested: list[str] = []
    lexenv = _env(known)
    toks, _ = shellwords.strip_wrappers(
        shellwords.words_of(seg, lexenv, budget), env, lexenv=lexenv, budget=budget, scripts=nested
    )
    if toks and toks[0] == "git" and not git_options:
        toks = _git_normal(toks)
    if toks and toks[0] == "eval" and len(toks) > 1:  # eval joins its words and runs the text as a command line
        nested.append(" ".join(toks[1:]))
    if toks and _stem(toks[0]) in _SHELL_NAMES:
        flags, operands = shellwords.split_args(toks)
        if shellwords.has_flag(flags, "c") and operands:
            nested.append(operands[0])
    if not toks:  # `X=/` on its own: later words may use it
        known.update(w.split("=", 1) for w in env)
    return toks, env, nested


def simple_commands_env(
    command: str, _depth: int = 0, *, git_options: bool = False, _budget: shellwords.Budget | None = None
) -> list[tuple[list[str], list[str]]]:
    """simple_commands with the `VAR=value` words each command was given before it (a prefix, or after `env`), as
    (words, assignments). A line that only sets variables comes back with no words, so a check sees `X=1; cmd`.
    Raises ShellError for a command that expands to more words than the reader allows, nests too deeply or takes
    too long to read."""
    with shellwords.time_limit():
        return _commands_env(command, _depth, git_options, _budget)


def _commands_env(
    command: str, _depth: int, git_options: bool, _budget: shellwords.Budget | None
) -> list[tuple[list[str], list[str]]]:
    if not isinstance(command, str):
        raise TypeError("command must be text")
    if _depth > _MAX_DEPTH:
        raise shellwords.ShellError("too_deep", _TOO_DEEP)
    budget = _budget or shellwords.Budget()
    pipelines, inner = shellwords.scan(shellwords.prep(command), heredocs=not shellwords.folds(command))
    found: list[tuple[list[str], list[str]]] = []
    nested = list(inner)
    known: dict[str, str] = {}
    for segs in pipelines:
        for seg in segs:
            toks, env, more = _seg_words(seg, git_options, budget, known)
            nested += more  # also where the wrapper took the whole command (`su -c TEXT`): the text runs
            if toks or env:
                found.append((toks, env))
    for text in nested:
        found += _commands_env(text, _depth + 1, git_options, budget)
    return found


def simple_commands_flow(
    command: str, _depth: int = 0, *, git_options: bool = False, _budget: shellwords.Budget | None = None
) -> list[tuple[list[str], list[str], str, bool]]:
    """simple_commands_env in the order the line runs, for a check that follows where the shell is (a `cd`). Each item
    is (words, assignments, link, piped). A command has the `link` that joins it to the one before (`;`, `&&` or `||`)
    and `piped` when it is one of several joined by `|`, so each runs in a subshell. Items with no words mark the
    structure: `(` opens and `)` closes a subshell (also what a nested `bash -c`, `eval` or `$(...)` runs in; the
    text of a `$(...)` has no known place, so its mark is `(?`), and `&` says the command before it ran in the
    background."""
    with shellwords.time_limit():
        return _commands_flow(command, _depth, git_options, _budget)


def _commands_flow(
    command: str, _depth: int, git_options: bool, _budget: shellwords.Budget | None
) -> list[tuple[list[str], list[str], str, bool]]:
    if not isinstance(command, str):
        raise TypeError("command must be text")
    if _depth > _MAX_DEPTH:
        raise shellwords.ShellError("too_deep", _TOO_DEEP)
    budget = _budget or shellwords.Budget()
    flow: list = []
    pipelines, inner = shellwords.scan(shellwords.prep(command), heredocs=not shellwords.folds(command), flow=flow)
    steps: list[tuple[list[str], list[str], str, bool]] = []
    link = ";"
    known: dict[str, str] = {}

    def nest(text: str, mark: str) -> None:
        steps.append(([], [], mark, False))
        steps.extend(_commands_flow(text, _depth + 1, git_options, budget))
        steps.append(([], [], ")", False))

    for item in flow:
        if isinstance(item, int):
            segs = pipelines[item]
            for seg in segs:
                toks, env, more = _seg_words(seg, git_options, budget, known)
                if toks or env:
                    steps.append((toks, env, link, len(segs) > 1))
                for text in more:
                    nest(text, "(")
        elif item in ("(", ")", "&"):
            steps.append(([], [], item, False))
            link = ";"
        else:
            link = item
    for text in inner:
        nest(text, "(?")
    return steps


def _readings(command: str) -> list[str]:
    """The texts to judge for one command: itself and, when it holds a backslash-newline, that pair removed wherever it
    is (the way older ws read it, which also joins a comment's trailing backslash to the next line). The shell reads
    one of them; a rule that denies either reading denies the command."""
    return [command, command.replace("\\\n", "")] if "\\\n" in command else [command]


def _walk_command(policy: Policy, command: str, cwd: str, protect: tuple[str, ...], cands: list[str]) -> str | None:
    """_walk for a whole command. In the one plain shape shellwords.without_bodies accepts, the bodies are text and the lines
    around them are judged. Any other command is judged with every body read as commands (so a body can't hide
    anything) and then once more with the bodies left out, so a stray quote in a body can't hide the lines after it."""
    data = shellwords.without_bodies(command)
    if data is not None:
        return _walk(policy, data, _Ctx(cwd, protect), 0, cands)
    second = "<<" in command and not shellwords.folds(command)  # the reading that leaves the bodies out
    for text in _readings(command):
        reason = _walk(policy, text, _Ctx(cwd, protect, bodies_as_commands=second), 0, cands)
        if reason:
            return reason
    if second:
        return _walk(policy, command, _Ctx(cwd, protect, None, True), 0, cands)
    return None


def is_denied_command(
    policy: Policy, command: str, *, protect: tuple[str, ...] = (), cwd: str | None = None
) -> str | None:
    """None if the command may run, else the reason. Every shell segment is checked (split on ; && || | & newline,
    $(...), backticks, <(...), `bash -c`), after stripping sudo/env/timeout/xargs wrappers and normalising flags,
    paths, git's global options, `cd` and simple `VAR=value`. Also denied: a shell fed its script by a pipe or
    here-document, a path under deny_read (or the folder itself, or a folder above it used recursively, or a glob
    that reaches it), and a write to a `protect` path (ws's own policy and generated gate files). Raises on input
    it cannot judge: callers treat that as a deny. This is the second layer; see the note above."""
    if not isinstance(command, str):
        raise TypeError("command must be text")
    if len(command) > MAX_COMMAND_CHARS:
        return (
            f"the command is {len(command):,} characters, too long to check in time (the limit is "
            f"{MAX_COMMAND_CHARS:,}); write it to a file with the Write tool and run that file"
        )
    cands: list[str] = []
    try:
        with shellwords.time_limit():
            shellwords.require_closed(command)
            reason = _walk_command(policy, command, cwd or _cwd(), tuple(protect), cands)
    except shellwords.ShellError as e:  # text the reader will not expand: a refusal, with the kind kept in the error
        return e.message
    if reason:
        return reason
    for pat in policy.gate.deny_commands:
        rx = _glob_regex(_canon_pattern(pat))
        if any(rx.match(c) for c in cands):
            return f"command matches deny_commands rule {pat!r}"
    return None


def needs_approval(policy: Policy, command: str, *, cwd: str | None = None) -> bool:
    """True if some shell segment of the command matches an `ask_commands` rule. It reads the line as
    is_denied_command does (so `bash -c "git push"` asks as `git push` does). Call it after is_denied_command: a
    command that is denied outright never gets as far as asking."""
    if not isinstance(command, str):
        raise TypeError("command must be text")
    if not policy.gate.ask_commands:
        return False
    cands: list[str] = []
    with shellwords.time_limit():
        _walk_command(policy, command, cwd or _cwd(), (), cands)
    return any(_glob_regex(_canon_pattern(pat)).match(c) for pat in policy.gate.ask_commands for c in cands)


def sandbox_write_paths(policy: Policy) -> list[str]:
    """The allow_write folders, `~/x` or `/x` as written (`$HOME` spelled `~`), normalised."""
    out = []
    for w in policy.gate.allow_write:
        for pre in ("${HOME}", "$HOME"):
            if w.startswith(pre + "/"):
                w = "~" + w[len(pre) :]
        out.append(posixpath.normpath(w))
    return list(dict.fromkeys(out))


def sandbox_read_paths(policy: Policy) -> list[str]:
    """The deny_read rules a sandbox can enforce by hiding the path: a folder or file, written `~/x` or `/x`, with at
    most a trailing `**` (`~/.ssh/**` -> `~/.ssh`). A rule with any other wildcard is left to the matcher; so is one
    that would hide the whole home or the root, which would end the session rather than guard it."""
    out = []
    for rule in policy.gate.deny_read:
        r = rule
        for pre in ("${HOME}", "$HOME"):
            if r == pre or r.startswith(pre + "/"):
                r = "~" + r[len(pre) :]
        while r.endswith("/**"):
            r = r[:-3]
        if r in ("", "~", "/") or re.search(r"[*?\[]", r) or not r.startswith(("~/", "/")):
            continue
        out.append(posixpath.normpath(r))
    return list(dict.fromkeys(out))


def is_denied_read(policy: Policy, path: str, *, cwd: str | None = None) -> str | None:
    """None if the path (a file a tool wants to read) may be read, else the reason."""
    if not isinstance(path, str) or not path:
        raise TypeError("path must be non-empty text")
    rule = _read_hit(policy, path, cwd or _cwd(), folder=False)
    return f"path matches deny_read rule {rule!r}" if rule else None


def is_denied_search(policy: Policy, path: str, *, cwd: str | None = None) -> str | None:
    """None if a tool may search under `path` (grep, glob, list), else the reason: the path is a denied folder, is
    under one, or is a folder above one."""
    if not isinstance(path, str) or not path:
        raise TypeError("path must be non-empty text")
    rule = _read_hit(policy, path, cwd or _cwd(), recursive=True)
    return f"search of {path!r} reaches deny_read rule {rule!r}" if rule else None


def is_protected_write(protect: tuple[str, ...], path: str, *, cwd: str | None = None) -> str | None:
    """None if a tool may write `path`, else the reason: it is one of ws's own files."""
    if not isinstance(path, str) or not path:
        raise TypeError("path must be non-empty text")
    hit = _under(tuple(protect), path, cwd or _cwd())
    return f"{hit} is protected: it is ws's own policy or generated gate" if hit else None


# A pattern with these can never match one subcommand in a runner that splits the line before it matches, and no runner
# has a literal star (tried: `\\*` matches nothing in Claude Code 2.1 or OpenCode 1.19). Such rules are hook-only.
_HOOK_ONLY = ("|", ";", "&", "\n", "`", "$(", "\\*")


def native_patterns(policy: Policy) -> list[str]:
    """The deny_commands a runner can enforce from its own rule list, each in every flag spelling (see variants), and
    the patterns the guards that are on contribute. The rest are enforced by the hook alone."""
    from ws import guards

    out: list[str] = []
    for pat in policy.gate.deny_commands:
        if not any(c in pat for c in _HOOK_ONLY):
            out += variants(pat)
    out += guards.native_deny(policy.guards.on)
    return list(dict.fromkeys(out))


def variants(pattern: str, limit: int = 24) -> list[str]:
    """Spellings of a deny pattern for runners that match the raw command text with no normalising: the flag cluster
    in every order and split (`-rf` -> `-rf`, `-fr`, `-r -f`, `-f -r`). The pattern itself comes first."""
    toks = pattern.split()
    if len(toks) < 2 or not _FLAGS.match(toks[1]) or len(toks[1]) > 4:
        return [pattern]
    from itertools import permutations

    letters = toks[1][1:]
    forms = {toks[1]}
    for perm in permutations(letters):
        forms.add("-" + "".join(perm))
        forms.add(" ".join("-" + ch for ch in perm))
    ordered = [toks[1]] + sorted(forms - {toks[1]})
    return [" ".join([toks[0], f, *toks[2:]]) for f in ordered[:limit]]
