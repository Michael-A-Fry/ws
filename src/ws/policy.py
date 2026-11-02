"""The policy: one vendor-neutral `policy.toml` that says what a session may do. Each runner's own config is generated
from it (ws.gate, ws.adapters), so the runner itself enforces the deny list; the matcher here backs the hooks and shims
that cover what a runner's native rules cannot.

Stdlib only. Three jobs:
  * load and validate the file (PolicyError names the bad key);
  * `is_denied_command` / `is_denied_read` / `is_denied_search`: the one matcher every hook and shim calls. It is the
    second layer (the runner's own deny rules are the first, a sandbox the real wall): it catches the plausible
    retries, not every spelling a shell allows;
  * `rule_of_two`: refuse a session that holds untrusted input, private data and a way out at the same time.

Known limits of the matcher. It reads command text, so it cannot see:
  * what an interpreter does inside its own program (`python3 -c`, `perl -e`, `node -e`, a script it runs);
  * a script that is downloaded and run in two steps (`curl -o a.sh ... && bash a.sh`);
  * a path built by a command (`cat "$(echo ~)/.ssh/id_rsa"`), or by a variable set outside the command line;
  * the version-control tool restoring a protected tracked file (`git -C ~/.ws checkout policy.toml`);
  * OpenCode's `patch` tool, which carries no single file path (its native `edit` rules still apply).
These stay allowed by the matcher. What closes them is the sandbox (ws.sandbox, and the `sandbox` block the
claude-code adapter writes), which acts on what a process does, not on what its command line says:
  * closed, for a shell command, the first four lines above: a write to ws's own files (`$WS_HOME`, the policy, the
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
    data and cache folders are private to the wall and to the project (`$WS_HOME/wall/opencode/{data,cache}/NAME-HASH`;
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
    `$WS_HOME/logs/wall-report.jsonl`): that covers what the lists cannot name.
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
    filter the network: bubblewrap can only cut it off whole, which would cut off the model too.
A call the policy lets through only with a human's say-so is `ask_commands`: see ws.approvals.
"""

from __future__ import annotations

import bisect
import contextlib
import functools
import itertools
import os
import posixpath
import re
import tomllib
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws.profile import ws_home

SCHEMA = 1
DIALS = ("prepare", "allowlist", "act")
MODES = ("read", "act", "leave")
# Neutral tool names a policy can deny; each adapter maps them to its runner's own tool name.
KNOWN_TOOLS = ("web_fetch", "web_search")
# Tools that can carry data out of the session (Rule of Two, leg C).
EGRESS_TOOLS = ("web_fetch", "web_search")
_NAME = re.compile(r"^[A-Za-z0-9_-]+$")
_HOST = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?(:[0-9]{1,5})?$")
_MAX_DEPTH = 4
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


@dataclass(frozen=True)
class Guards:
    """The guards that run (ws.guards): `on` is their names in order; `config` maps each to what its `config()` made of
    its `[guards.NAME]` table. A policy with no `[guards]` has none on."""

    on: tuple[str, ...] = ()
    config: dict = field(default_factory=dict)


@dataclass(frozen=True)
class RuleException:
    name: str
    job: str
    sources: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class Policy:
    schema: int
    dial: str
    interrupts: Interrupts
    sources: dict[str, Source]
    gate: Gate
    exceptions: tuple[RuleException, ...]
    guards: Guards = Guards()


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

# System directories a recursive delete must never target (Linux and macOS), at any depth below them.
_SYSTEM_DIRS = (
    *("bin", "boot", "dev", "etc", "lib", "lib64", "proc", "root", "sbin", "sys", "usr", "var", "opt", "srv"),
    *("snap", "System", "Library", "Applications"),
)
# Folders where mounts, volumes and run state sit: the folder and what is directly in it (`/mnt/data`) are denied; a
# path further down (`/mnt/data/scratch/build`) is ordinary work. `/private` is macOS system folders and its temp
# folders. Judged in code (_system_folder), because a glob's `*` would also take every path below.
_MOUNT_DIRS = ("mnt", "media", "Volumes", "run")
_PRIVATE_TEMP = ("/private/var/folders/", "/private/tmp/")
# Where people's own folders live: the folder and `/dir/*` are named here, and one level down (a whole home folder) is
# judged in code (see _home_folder), because a glob's `*` would also take every path below it.
_HOME_PARENTS = ("home", "Users")
_SHELLS = ("sh", "bash", "zsh", "dash")
# Paths no session may read by default: the keys, tokens and credential files of the common tools, and ws's own secrets.
DEFAULT_DENY_READ = (
    "~/.ssh/**",
    "~/.gnupg/**",
    "~/.aws/**",
    "~/.config/gcloud/**",
    "~/.config/gh/hosts.yml",
    "~/.config/ws/secrets/**",
    "~/.docker/config.json",
    "~/.kube/config",
    "~/.claude/.credentials.json",
    "~/.netrc",
    "~/.npmrc",
    "~/.pypirc",
    "~/.git-credentials",
    "~/.config/git/credentials",
    "**/.env",
    "**/.env.*",
    "/proc/*/environ",
    "/proc/*/task/*/environ",
)
# `**/.env.*` leaves the committed template files readable: they hold names, not values (see _read_hit).
_ENV_TEMPLATES = frozenset({".env.example", ".env.sample", ".env.template"})
_ENV_RULES = frozenset({"**/.env", "**/.env.*"})


def default_deny_commands() -> list[str]:
    """The built-in deny list. `*` matches anything; `\\*` is a literal star. Flags are normalised before matching
    (see _canon_tokens), so `rm -rf` here also covers `rm -fr`, `rm -r -f` and `rm -Rf`. A system folder is named whole
    and as `/dir/*`, so `/var` and `/var/log` are covered and `/variant` is not."""
    out = ["rm -* /", "rm -* /\\*", "rm -* ~", "rm -* ~/\\*"]
    for d in _HOME_PARENTS:
        out += [f"rm -* /{d}", f"rm -* /{d}/\\*"]
    for d in _SYSTEM_DIRS:
        out += [f"rm -* /{d}", f"rm -* /{d}/*"]
    for d in (*_MOUNT_DIRS, "private"):
        out += [f"rm -* /{d}", f"rm -* /{d}/\\*"]
    # A forcing push in any spelling (-f, -uf, +refspec, --force-with-lease), with or without git's global options.
    out += ["git push*--force*", "git push* -f*", "git -* push*--force*", "git -* push* -f*"]
    out += ["mkfs*", "dd*of=/dev/*"]
    # Reading the process environment: `env` with no command to run (see _segment) and `printenv`.
    out += ["env", "printenv *"]
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
deny_read = {arr(list(DEFAULT_DENY_READ))}
# Neutral tool names to switch off: {", ".join(KNOWN_TOOLS)}. An allowed web tool counts as a way out (leg C).
deny_tools = []
# Commands that run only after you approve that exact call, once: the gate refuses the first try and prints an id; in a
# terminal of your own (not the session's) run `ws approve ID`, then the session retries the identical call. Same globs
# as deny_commands; a command that also matches deny_commands stays denied. Example: ["git push*", "terraform apply*"].
ask_commands = []
# Hosts a session's shell may reach, for runners whose sandbox filters the network (Claude Code). Empty = no limit
# from the policy. Example: ["api.anthropic.com", "github.com", "*.npmjs.org"].
allow_hosts = []
# Folders a sandboxed session may write besides the project folder it starts in (and the runner's own state and cache
# folders, which ws knows). Everything else is read-only inside the wall. Absolute or `~/`, no wildcards. Example:
# ["~/.npm", "~/.cache/uv"].
allow_write = []

# Guards: small named checks that run beside the rules above. `ws gate generate` writes the runner hooks they need.
#   syntax-check     after an edit, checks the file by its extension (.py, .json, .toml, .sh, .js) and tells the model the
#                    line that does not parse
#   session-context  at session start, lists which of the files below exist in the project, warns when an API key (not
#                    the subscription) bills the session, and says once for each ws version what changed
#   bulk-staging     stops `git add -A`, `git add .` and `git commit -a`, so a commit holds only named files
#   record-calls     appends each tool call to $WS_HOME/logs/gate-calls.jsonl, for `ws gate replay`
#   test-weakening   before a test file is written, denies a skip marker added to an existing test
#   claims           checks a commit named beside merged, pushed or on main against a ref; with release_dir, a release
#                    called live must be the one its `current` link points at
#   spawn-cap        denies a subagent start at max_agents running (default from the machine's CPUs and memory)
#   skill-gate       a write to chosen paths waits for a skill ([[guards.skill-gate]] skill, paths); a skill's own
#                    SKILL.md front matter can list `gates` (path globs) too, from skill_dirs
[guards]
on = ["syntax-check", "session-context"]
# [guards.syntax-check]
# extra = {{ yaml = ["yamllint", "-d", "relaxed"] }}   # an extension and the checker that gets the file path appended
# [guards.session-context]
# files = [".claude-state/STATE.md"]
# warn_api_key = true   # on by default: a line when ANTHROPIC_API_KEY, not the subscription, bills a session
# [guards.claims]
# release_dir = "~/releases"   # a folder whose `current` link names the promoted release; left out, claims are off
# release_tags = ["v[0-9]+[.][0-9]+([.][0-9]+)*"]   # regexes for a release's name; the default reads v1.2 and v1.2.3
# [guards.skill-gate]
# skill_dirs = ["~/.claude/skills"]   # where skills keep their own `gates`; add rules as [[guards.skill-gate.rules]]
# [guards.spawn-cap]
# max_agents = 4   # optional: left out, half the CPUs or half the GiB of memory, whichever is less (2 to 8)

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
    return ws_home(env) / "policy.toml"


# What an agent must never rewrite under $WS_HOME: its own policy, profile, secrets, jobs, release, approvals, install
# manifest, ratings and the run records. (Reading logs/runs.jsonl stays allowed; only writes are denied.) Generated
# gate folders are added by the caller, since they can sit anywhere.
CONTROL_NAMES = (
    "profile.toml",
    "policy.toml",
    "secrets.toml",
    "jobs",
    "release",
    "runs",
    "logs",
    "sessions",
    "approvals",
    "install-manifest.json",
    "ratings.jsonl",
)


def secret_read_rules(home: Path | None = None) -> tuple[str, ...]:
    """deny_read rules for where the secrets store keeps its values: `$WS_HOME/secrets` and, when the profile names
    another folder (`[roles.secrets] store`), that one too. Each folder is named as written (made absolute) and as the
    real path it resolves to, so a ws home that is a symlink is covered whichever way a command spells it. They are
    added to every policy that is read, so a session can't read them whatever its policy file says; the runners' own
    rules, the sandbox and the hook all see them."""
    from ws import secrets  # here, not at the top: it is a command module that imports more than the policy needs

    base = ws_home() if home is None else Path(home).expanduser().absolute()
    folders = [base / "secrets"]
    with contextlib.suppress(Exception):  # a profile that does not read leaves the default folder, always covered
        folders.append(secrets.store_dir({"WS_HOME": str(base)}).expanduser().absolute())
    spellings = [f for folder in folders for f in (folder, Path(os.path.realpath(folder)))]
    return tuple(dict.fromkeys(f"{f}/**" for f in spellings))


def control_paths(home: Path) -> list[str]:
    """ws's own control files under a ws home, as absolute paths (files and folders alike; a path covers what is
    under it)."""
    return [str(Path(home).expanduser().absolute() / n) for n in CONTROL_NAMES]


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
    raw = _table(raw, "policy", {"schema", "dial", "interrupts", "sources", "gate", "exceptions", "guards"})
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
        if not _NAME.fullmatch(name):
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
        {"deny_commands", "deny_read", "deny_tools", "ask_commands", "allow_hosts", "allow_write"},
    )
    deny_read = _strings(g, "deny_read", "[gate] ")
    gate = Gate(
        _strings(g, "deny_commands", "[gate] "),
        (*deny_read, *(r for r in secret_read_rules() if r not in deny_read)),
        _strings(g, "deny_tools", "[gate] "),
        _strings(g, "ask_commands", "[gate] "),
        _strings(g, "allow_hosts", "[gate] "),
        _strings(g, "allow_write", "[gate] "),
    )
    for h in gate.allow_hosts:
        if not _HOST.fullmatch(h):
            raise PolicyError(f"[gate] allow_hosts: {h!r} is not a host (a name, `*.name` or an IP, optional :port)")
    for w in gate.allow_write:
        n = posixpath.normpath(re.sub(r"^(\$HOME|\$\{HOME\})(?=/|$)", "~", w)) if w else ""
        if n in ("", ".", "/", "~") or not n.startswith(("~/", "/")) or re.search(r"[*?\[]", w):
            raise PolicyError(
                f"[gate] allow_write: {w!r} must be a folder, absolute or under ~/, with no wildcard, and not / or "
                "the home folder (after `.` and `..` are resolved)"
            )
    for t in gate.deny_tools:
        if t not in KNOWN_TOOLS:
            raise PolicyError(f"[gate] deny_tools: unknown tool {t!r} (known: {', '.join(KNOWN_TOOLS)})")

    ex_raw = raw.get("exceptions", [])
    if not isinstance(ex_raw, list):
        raise PolicyError("[[exceptions]] must be an array of tables")
    exceptions: list[RuleException] = []
    for i, e in enumerate(ex_raw, 1):
        where = f"[[exceptions]] #{i} "
        e = _table(e, where.strip(), {"name", "job", "sources", "reason"})
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
        exceptions.append(RuleException(e["name"], e["job"], covered, e["reason"]))
    return Policy(schema, dial, interrupts, sources, gate, tuple(exceptions), _guards(raw.get("guards", {})))


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


def load(path: Path | None = None) -> Policy:
    """The policy at `path` (default $WS_HOME/policy.toml). No file = the built-in defaults."""
    path = path or policy_path()
    if not path.is_file():
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
    """The policy with the secrets folder of the ws home the file sits in denied too (`loads` covers the one `$WS_HOME`
    names; a policy kept in another ws home, such as the one a generated hook is told to read, covers its own)."""
    extra = tuple(r for r in secret_read_rules(home) if r not in pol.gate.deny_read)
    return replace(pol, gate=replace(pol.gate, deny_read=(*pol.gate.deny_read, *extra))) if extra else pol


# --- Rule of Two -------------------------------------------------------------------------------------------------


def shell_ways_out(policy: Policy) -> list[str]:
    """The network commands the policy does not deny: each is a way for a shell to send data out."""
    return [p.split()[0] for p in NET_PROBES if is_denied_command(policy, p) is None]


def rule_of_two(policy: Policy, sources: list[str], job: str | None = None, shell: bool = True) -> Verdict:
    """May a session read `sources` and run `job`? Refused when it would hold untrusted input AND private data AND a
    way out. A way out is an outward act of a source in mode "act" (only when the dial is not "prepare"), an allowed
    egress tool (web_fetch, web_search), or, when the session has a shell (`shell`, default yes), a network command
    the policy does not deny (see shell_ways_out). A named exception lifts the refusal when its job matches and
    every source the session reads is one it names."""
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
    for e in policy.exceptions:
        if job is not None and e.job == job and set(sources) <= set(e.sources):
            return Verdict("exception", f"{why}; allowed by exception {e.name!r}: {e.reason}", e.name)
    return Verdict("refused", f"Rule of Two: {why}")


# --- the matcher -------------------------------------------------------------------------------------------------
#
# The matcher is the SECOND layer. The first is each runner's own deny rules (generated from this policy), and the
# real wall is a sandbox, which comes later. A shell line can always be spelled in a way a text matcher misses
# (variables, interpreters, encoded payloads), so this does not try to be complete: it catches the plausible ways a
# model retries a blocked command (other flags, a wrapper, a pipe, a nested shell, a different path spelling). What it
# cannot judge it denies or leaves to the layers either side.

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FLAGS = re.compile(r"^-[A-Za-z]+$")
_VAR = re.compile(r"\$\{(\w+)\}|\$(\w+)")
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "coproc"}
# wrapper -> short options that take the next word; _LONG_ARG: long options that do
_WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "setsid": set(),
    "stdbuf": {"-i", "-o", "-e"},
    "timeout": {"-s", "-k"},
    "time": set(),
    "watch": {"-n"},
    "xargs": {"-I", "-n", "-P", "-d", "-E", "-s", "-L", "-a", "-J"},
    "busybox": set(),
}
_LONG_ARG = {"timeout": {"--signal", "--kill-after"}, "sudo": {"--user", "--group", "--host", "--prompt"}}
_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "csh", "tcsh"}
_GIT_ARG_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix", "--config-env"}
# Commands whose operands are each judged on their own: `rm -rf / /tmp` is a delete of `/`.
_OPERAND_CMDS = {"rm", "rmdir", "shred", "unlink", "chmod", "chown", "chgrp", "mv"}
_ALWAYS_RECURSIVE = {"tar", "find", "zip", "rsync", "tree", "du", "7z", "bsdtar", "cpio"}
# Commands that write to every operand / only to the last one (or -t DIR).
_WRITE_ALL = {"tee", "mv", "rm", "rmdir", "truncate", "chmod", "chown", "chgrp", "touch", "shred", "unlink", "patch"}
_WRITE_LAST = {"cp", "install", "ln", "rsync", "scp"}
_INTERP = re.compile(r"^(python[0-9.]*|perl|ruby|node|php)$")
_PROCSUB = "/dev/fd/63"  # what `<(...)` becomes
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
    # What the line has cost to judge so far, shared by every part of it: [path-like pieces tested, words read].
    spent: list[int] = field(default_factory=lambda: [0, 0])

    def fork(self) -> _Ctx:
        return _Ctx(self.cwd, self.protect, dict(self.vars or {}), spent=self.spent)


def _home() -> str:
    return os.environ.get("HOME") or str(Path.home())


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return "/"


def _expand(path: str) -> str:
    """~, ~user, $HOME, ${HOME}, $WS_HOME and ${WS_HOME} to a real prefix."""
    home = _home().rstrip("/")
    for pre in ("${HOME}", "$HOME", "~"):
        if path == pre or path.startswith(pre + "/"):
            return home + path[len(pre) :]
    for pre in ("${WS_HOME}", "$WS_HOME"):
        if path == pre or path.startswith(pre + "/"):
            return str(ws_home()).rstrip("/") + path[len(pre) :]
    return os.path.expanduser(path) if path.startswith("~") else path


def _abs(path: str, cwd: str) -> str:
    p = _expand(path)
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
    return tok.startswith(("/", "~", "$HOME", "${HOME}", "$WS_HOME", "${WS_HOME}"))


def _merge_flags(tokens: list[str]) -> list[str]:
    """Merge the short flags straight after the command word into one sorted token, so `-r -f`, `-fr` and `-rf` are
    one spelling. For rm, `-R`, `--recursive` and `--force` fold into -r and -f."""
    if not tokens:
        return tokens
    cmd, i, letters, rest_at = tokens[0], 1, set(), 1
    rm = cmd == "rm"
    while i < len(tokens):
        t = tokens[i]
        if _FLAGS.fullmatch(t):
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


_ANSI_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_ANSI_NUMBER = {
    "x": re.compile(r"[0-9A-Fa-f]{1,2}"),
    "u": re.compile(r"[0-9A-Fa-f]{1,4}"),
    "U": re.compile(r"[0-9A-Fa-f]{1,8}"),
}
_ANSI_OCTAL = re.compile(r"[0-7]{1,3}")


def _ansi_c(seg: str, i: int) -> tuple[str, int]:
    """The text of a `$'...'` word whose body starts at i, and the index after its closing quote: backslash escapes
    are decoded as bash does (newline, hex, octal and unicode escapes, `\\'`), so `$'r\\x6d'` is the word `rm`. An
    escape it does not know stays as written."""
    out: list[str] = []
    n = len(seg)
    while i < n:
        c = seg[i]
        if c == "'":
            return "".join(out), i + 1
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        d = seg[i + 1]
        if d in _ANSI_ESCAPES:
            out.append(_ANSI_ESCAPES[d])
            i += 2
        elif d in "\\'\"?":
            out.append(d)
            i += 2
        elif d in _ANSI_NUMBER and (m := _ANSI_NUMBER[d].match(seg, i + 2)):
            out.append(chr(min(int(m.group(), 16), 0x10FFFF)))
            i = m.end()
        elif d in "01234567":
            m = _ANSI_OCTAL.match(seg, i + 1)
            out.append(chr(int(m.group(), 8) & 0xFF))
            i = m.end()
        elif d == "c" and i + 2 < n:
            out.append(chr(ord(seg[i + 2]) & 0x1F))
            i += 3
        else:
            out.append("\\" + d)
            i += 2
    return "".join(out), n


def _lex(seg: str) -> tuple[list[str], list[tuple[str, str]]]:
    """Split one shell segment into words (quotes and escapes resolved) and redirections [(op, target)]. A
    redirection glued to a word (`/>/dev/null`, `<~/.ssh/id`, `2>&1`) is split off it."""
    words: list[str] = []
    redirs: list[tuple[str, str]] = []
    n, i = len(seg), 0

    def word(i: int) -> tuple[str, int]:
        buf: list[str] = []
        while i < n:
            c = seg[i]
            if c in " \t\r\n<>":
                break
            if c == "\\" and i + 1 < n:
                buf.append(seg[i + 1])
                i += 2
            elif c == "'":
                j = seg.find("'", i + 1)
                j = n if j < 0 else j
                buf.append(seg[i + 1 : j])
                i = j + 1
            elif seg.startswith("$'", i):
                text, i = _ansi_c(seg, i + 2)
                buf.append(text)
            elif seg.startswith('$"', i):  # locale quoting reads as double quotes
                i += 1
            elif c == '"':
                i += 1
                while i < n and seg[i] != '"':
                    if seg[i] == "\\" and i + 1 < n and seg[i + 1] in '"\\$`':
                        i += 1
                    buf.append(seg[i])
                    i += 1
                i += 1
            elif seg.startswith("$(", i):
                j = _close_paren(seg, i + 2)
                buf.append(seg[i : j + 1])
                i = j + 1
            else:
                buf.append(c)
                i += 1
        return "".join(buf), i

    while i < n:
        c = seg[i]
        if c in " \t\r\n":
            i += 1
        elif c in "<>":
            for op in ("<<<", "<<-", "<<", "<&", "<>", "<", ">>", ">|", ">&", ">"):
                if seg.startswith(op, i) and op[0] == c:
                    break
            i += len(op)
            while i < n and seg[i] in " \t":
                i += 1
            target, i = word(i)
            if op.endswith("&") and (target.isdigit() or target == "-"):
                continue  # 2>&1, <&-
            redirs.append((op, target))
        else:
            start = i
            w, i = word(i)
            glued_fd = w.isdigit() and i < n and seg[i] in "<>"
            if not (glued_fd or (w == "&" and seg[i : i + 1] == ">") or (i == start)):
                words.append(w)
    return words, redirs


def _strip_wrappers(words: list[str], env: list[str] | None = None) -> tuple[list[str], set[str]]:
    """Drop leading keywords, VAR=value and exec wrappers (sudo, env, timeout, nice, xargs, busybox ...): what is left
    is the command that really runs. Also returns the wrappers it removed. If `env` is given, the VAR=value words it
    drops (a prefix, or the ones `env` sets) are added to it."""
    toks, seen = list(words), set()
    while toks:
        w = toks[0]
        if w in _KEYWORDS or _ASSIGN.match(w):
            if env is not None and _ASSIGN.match(w):
                env.append(w)
            toks = toks[1:]
            continue
        if w.startswith("/") and "/" in w[1:]:
            w = toks[0] = posixpath.basename(w)  # /usr/bin/rm -> rm
        if w not in _WRAPPERS:
            break
        seen.add(w)
        arg_opts, i, resplit = _WRAPPERS[w], 1, None
        while i < len(toks):
            t = toks[i]
            if t == "--":
                i += 1
                break
            if w == "env" and t in ("-S", "--split-string"):
                resplit = toks[i + 1] if i + 1 < len(toks) else ""
                i += 2
                break
            if t.startswith("--"):
                i += 2 if t in _LONG_ARG.get(w, ()) else 1
            elif t.startswith("-") and len(t) > 1:
                i += 2 if "-" + t[-1] in arg_opts else 1  # `-u root`, and the `u` of `-iu root`
            elif w == "env" and _ASSIGN.match(t):
                if env is not None:
                    env.append(t)
                i += 1
            else:
                break
        if w == "timeout" and i < len(toks):
            i += 1  # the duration
        toks = (_lex(resplit)[0] if resplit else []) + toks[i:]
    return toks, seen


def _canon_tokens(tokens: list[str]) -> list[str]:
    if tokens and tokens[0] == "git":
        tokens = _git_normal(tokens)
    tokens = _merge_flags(tokens)
    return [_canon_path(t) if _is_pathlike(t) else t for t in tokens]


def _prep(cmd: str) -> str:
    """Line continuations joined, full-width characters folded, ${IFS} read as the space it is."""
    cmd = _join_lines(unicodedata.normalize("NFKC", cmd))
    return re.sub(r"\$\{IFS\}|\$IFS\b", " ", cmd)


def _join_lines(cmd: str) -> str:
    """Backslash-newline pairs removed, as the shell does, except in a comment: a comment ends at its newline whatever
    precedes it, so a comment's trailing backslash must not swallow the line below. Quotes, `$'...'`, `$(...)` and
    backticks are tracked so that a `#` inside them is not taken for a comment; an escaped character is a pair."""
    out: list[str] = []
    n, i, quote = len(cmd), 0, ""
    while i < n:
        c = cmd[i]
        if quote == "'":  # no escapes inside; a backslash-newline is joined here as it always was
            if cmd.startswith("\\\n", i):
                i += 2
                continue
            quote = "" if c == "'" else quote
        elif c == "\\" and i + 1 < n:
            if cmd[i + 1] != "\n":
                out.append(cmd[i : i + 2])
            i += 2
            continue
        elif quote == "$'":
            quote = "" if c == "'" else quote
        elif cmd.startswith("$(", i) or (c == "`" and _backtick_end(cmd, i) > 0):
            if c == "`":
                j, head, tail = _backtick_end(cmd, i), "`", "`"
            else:
                j, head, tail = _close_paren(cmd, i + 2), "$(", ")"
            out += [head, _join_lines(cmd[i + len(head) : j])]
            if j < n:
                out.append(tail)
            i = j + 1
            continue
        elif quote == '"':
            quote = "" if c == '"' else quote
        elif c == "$" and cmd.startswith("$'", i):
            quote = "$'"
            out.append("$")
            i += 1
            c = "'"
        elif c in "'\"":
            quote = c
        elif c == "#" and (not out or out[-1][-1:] in (" ", "\t", "\n", ";", "&", "|", "(", ")")):
            eol = cmd.find("\n", i)
            eol = n if eol < 0 else eol
            out.append(cmd[i:eol].rstrip("\\"))  # a trailing backslash would escape the newline in a later scan
            i = eol
            continue
        out.append(c)
        i += 1
    return "".join(out)


_HEREDOC_WORD = re.compile(r"<<(-?)[ \t]*(?:'([^']*)'|\"([^\"]*)\"|\\?([A-Za-z_][A-Za-z0-9_]*))")

_IFS = re.compile(r"\$\{IFS\}|\$IFS\b")


def _folds(command: str) -> bool:
    """Would _prep change the characters the shell sees (full-width letters read as plain ones, `$IFS` as a space)?
    Closing words are matched on the text the shell reads, so such a line can't be trusted to close a body."""
    return unicodedata.normalize("NFKC", command) != command or _IFS.search(command) is not None


# The one shape of command whose here-document bodies are left unread, because the shell can only treat them as text:
# steps of `cat` or `tee`, each with plain-word arguments, at most one `>` or `>>` to a plain-word path and one
# here-document opener with a quoted name, joined by `;`, `&&` or a new line. Anything else anywhere in the command
# (a pipe, a group, a function, a variable, a substitution, a comment, an unquoted opener, a here-string, another
# program) and every body is read as commands.
_PLAIN = r"[A-Za-z0-9_./,:@%+-]+"
_OPENER = r"<<-?(?:'[A-Za-z_][A-Za-z0-9_]*'|\"[A-Za-z_][A-Za-z0-9_]*\")"
_REDIRECT = rf"[ \t]+>>?[ \t]*{_PLAIN}"
_STEP = re.compile(rf"(?:cat|tee)(?:[ \t]+{_PLAIN})*(?P<r1>{_REDIRECT})?[ \t]+(?P<open>{_OPENER})(?P<r2>{_REDIRECT})?")
_STEPS = re.compile(r";|&&")


def _without_bodies(command: str) -> str | None:
    """The command with the body and closing line of each here-document taken out, when every line of it is the
    shape above and every body closes on its exact closing line; None otherwise (read the whole text as commands)."""
    if _prep(command) != command or "\\\n" in command:
        return None
    out: list[str] = []
    i, n = 0, len(command)
    while i < n:
        eol = command.find("\n", i)
        eol = n if eol < 0 else eol
        line, i = command[i:eol], eol + 1
        out.append(line)
        if not line:
            continue
        opened: list[tuple[str, bool]] = []
        for step in _STEPS.split(line):
            m = _STEP.fullmatch(step.strip(" \t"))
            if not m or (m["r1"] and m["r2"]):
                return None
            opened.append((m["open"].lstrip("<-")[1:-1], m["open"].startswith("<<-")))
        i = _after_bodies(command, i, opened)
        if i is None:
            return None
    return "\n".join(out) + "\n" if any(out) else None


def _closes(line: str, word: str, indented: bool) -> bool:
    """Does this line end a here-document? The shell takes the line as it is, word and nothing else; `<<-` also lets
    leading tabs (only tabs) go."""
    return (line.lstrip("\t") if indented else line) == word


def _arithmetic(body: str) -> bool:
    """Is the text between `$(` and its closing `)` really `((...))`, shell arithmetic?"""
    return body.startswith("(") and body.endswith(")")


def _after_bodies(cmd: str, i: int, opened: list[tuple[str, bool]]) -> int | None:
    """The index after the closing lines of the here-documents a line opened (closing word, may be indented), whose
    bodies start at `i`, the start of the next line. None when one never closes (the shell would take the rest of the
    text as its body; here that is read as commands, so a typo can't hide a command)."""
    n = len(cmd)
    for word, indented in opened:
        while True:
            if i >= n:
                return None
            eol = cmd.find("\n", i)
            eol = n if eol < 0 else eol
            line, i = cmd[i:eol], eol + 1
            if _closes(line, word, indented):
                break
    return i


def _scan(cmd: str, heredocs: bool = False, end: dict | None = None) -> tuple[list[list[str]], list[str]]:
    """Split a shell command line. Returns (pipelines, inner): each pipeline is a list of segments (split on |, |&),
    pipelines are split on ; && || & newline ( ); inner holds the text of $(...), backticks and <(...), which run too.
    With `heredocs`, the body and closing line of each here-document are left out (that text is data for a program,
    not commands): a `<<WORD` starts one only outside quotes and comments, and not as `<<<` or a shift in `((...))` or inside `${...}` and `$[...]`, which are one word. If `end` is given,
    end["quote"] is the quote still open at the end of the text (empty if none)."""
    pipelines: list[list[str]] = []
    inner: list[str] = []
    segs: list[str] = []
    buf: list[str] = []
    waiting: list[tuple[str, bool]] = []  # (closing word, may be indented) of the here-documents the line opened
    nest: list[str] = []  # the closers still owed by the ${...} and $[...] we are inside
    n, i, quote = len(cmd), 0, ""

    def sub(body: str) -> str:  # the text of a $(...) that is really `$((a<<b))` arithmetic: its `<<` is a shift
        return body.replace("<<", "< <") if heredocs and _arithmetic(body) else body

    def end_seg() -> None:
        s = "".join(buf).strip()
        buf.clear()
        if s:
            segs.append(s)

    def end_pipeline() -> None:
        nonlocal segs
        end_seg()
        if segs:
            pipelines.append(segs)
        segs = []

    while i < n:
        c = cmd[i]
        if c == "\\" and quote != "'" and i + 1 < n:
            buf.append(cmd[i : i + 2])
            i += 2
            continue
        if quote:
            if c == quote[-1]:
                quote = ""
            elif quote == '"' and c == "`":
                j = _backtick_end(cmd, i)
                if j > 0:
                    inner.append(_unbacktick(cmd[i + 1 : j]))
            elif quote == '"' and cmd.startswith("$(", i):
                j = _close_paren(cmd, i + 2)
                inner.append(sub(cmd[i + 2 : j]))
            buf.append(c)
            i += 1
            continue
        if c == "$" and cmd[i + 1 : i + 2] in ("{", "["):  # a parameter expansion or $[...] arithmetic: one word
            nest.append("}" if cmd[i + 1] == "{" else "]")
            buf.append(cmd[i : i + 2])
            i += 2
            continue
        if nest and c == "[" and nest[-1] == "]":
            nest.append("]")
        elif nest and c == nest[-1]:
            nest.pop()
        heredocs_here = (
            heredocs and not nest
        )  # in ${...} and $[...], `#` and `<<` are operators, not comment or here-doc
        if heredocs_here and c == "#" and (not buf or buf[-1] in " \t"):  # a comment: not commands, no here-document
            eol = cmd.find("\n", i)
            i = n if eol < 0 else eol
            continue
        if heredocs_here and c == "<" and cmd.startswith("<<<", i):  # a here-string: its word is data, not a delimiter
            buf.append("<<<")
            i += 3
            continue
        if heredocs_here and c == "<" and cmd.startswith("<<", i):
            m = _HEREDOC_WORD.match(cmd, i)
            if m:
                waiting.append((m.group(2) or m.group(3) or m.group(4) or "", m.group(1) == "-"))
                buf.append(m.group(0))
                i = m.end()
                continue
        if heredocs and c == "\n" and waiting:
            end_pipeline()
            i += 1
            for word, indented in waiting:  # skip each body, up to and including its closing line
                while i < n:
                    eol = cmd.find("\n", i)
                    eol = n if eol < 0 else eol
                    line = cmd[i:eol]
                    i = eol + 1
                    if _closes(line, word, indented):
                        break
            waiting.clear()
            continue
        if heredocs_here and c == "(" and cmd.startswith("((", i) and not buf:  # an arithmetic command: `((x<<2))`
            j = _close_paren(cmd, i + 1)
            if _arithmetic(cmd[i + 1 : j]):
                inner.append(sub(cmd[i + 1 : j])[1:-1])
                i = j + 1
                continue
        if c == "$" and cmd.startswith("$'", i):  # ANSI-C quoting: backslash escapes work inside, `\'` included
            quote = "$'"
            buf.append("$'")
            i += 2
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
        elif c == "`":
            j = _backtick_end(cmd, i)
            j = n if j < 0 else j
            inner.append(_unbacktick(cmd[i + 1 : j]))
            buf.append(cmd[i : j + 1])
            i = j + 1
            continue
        elif cmd.startswith("$(", i):
            j = _close_paren(cmd, i + 2)
            inner.append(sub(cmd[i + 2 : j]))
            buf.append(cmd[i : j + 1])
            i = j + 1
            continue
        elif c in "<>" and cmd.startswith("(", i + 1):  # process substitution: a file name to the outer command
            j = _close_paren(cmd, i + 2)
            inner.append(cmd[i + 2 : j])
            buf.append(" " + _PROCSUB + " " if c == "<" else " /dev/null ")
            i = j + 1
            continue
        elif c in ";\n()":
            end_pipeline()
        elif c == "&":
            if i > 0 and cmd[i - 1] in "<>" or cmd[i + 1 : i + 2] == ">":  # 2>&1, &>file
                buf.append(c)
            else:
                end_pipeline()
                if cmd[i + 1 : i + 2] == "&":
                    i += 1
        elif c == "|" and i > 0 and cmd[i - 1] == ">":  # >| (clobber redirect)
            buf.append(c)
        elif c == "|":
            if cmd[i + 1 : i + 2] == "|":
                end_pipeline()
                i += 1
            else:
                end_seg()
                if cmd[i + 1 : i + 2] == "&":
                    i += 1
        else:
            buf.append(c)
        i += 1
    end_pipeline()
    if end is not None:
        end["quote"] = quote
    return pipelines, inner


def _backtick_end(cmd: str, i: int) -> int:
    """Index of the backtick that closes the one at `i`, or -1. A backslash escapes the next character, so an escaped
    backtick belongs to a command nested inside."""
    i += 1
    while i < len(cmd):
        if cmd[i] == "\\":
            i += 2
        elif cmd[i] == "`":
            return i
        else:
            i += 1
    return -1


def _unbacktick(text: str) -> str:
    """The command inside a backtick pair, as the shell reads it: `\\``, `\\$` and `\\\\` lose their backslash."""
    return re.sub(r"\\([`$\\])", r"\1", text)


def _close_paren(cmd: str, i: int) -> int:
    """Index of the ')' that closes a '$(' whose body starts at i (the end of the text if it never closes)."""
    depth, quote = 1, ""
    while i < len(cmd):
        c = cmd[i]
        if c == "\\" and quote != "'":
            i += 2
            continue
        if quote:
            quote = "" if c == quote[-1] else quote
        elif c == "$" and cmd.startswith("$'", i):  # ANSI-C quoting: a backslash escapes inside it
            quote = "$'"
            i += 1
        elif c in "'\"":
            quote = c
        elif c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(cmd)


def _args(toks: list[str]) -> tuple[list[str], list[str]]:
    """(flags, operands) of a command: anything starting with '-' is a flag until a bare `--`."""
    flags, operands, after = [], [], False
    for t in toks[1:]:
        if after:
            operands.append(t)
        elif t == "--":
            after = True
        elif t.startswith("-") and len(t) > 1:
            flags.append(t)
        else:
            operands.append(t)
    return flags, operands


def _cluster(flags: list[str], letter: str) -> bool:
    """Is `letter` in any single-dash flag group (`-c`, `-lc`, `-ec`)?"""
    return any(f[0] == "-" and f[1:2] != "-" and letter in f[1:] for f in flags)


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
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def _path_regex(pattern: str) -> re.Pattern:
    """gitignore-style path glob: `**` crosses folders, `*` stays in one, a bare name matches at any depth."""
    pattern = _expand(pattern)
    if not pattern.startswith(("/", "**/")):
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


@functools.lru_cache(maxsize=512)
def _rule_forms(rule: str, home: str, wshome: str) -> tuple[re.Pattern, str | None, bool]:
    """A deny_read rule as (its regex, its root, whether the root is the whole folder). Built once per rule and
    environment: `home` and `wshome` are only the cache key, since `~` and `$WS_HOME` read them."""
    root = _rule_root(rule)
    # A folder is the thing denied when the rule's first wildcard is its last segment (`~/.ssh/**`); a rule with names
    # after the wildcard (`/proc/*/environ`) denies those files, not the folder above them.
    whole = root is not None and "/" not in _expand(rule)[len(root) :].lstrip("/")
    return _path_regex(rule), root, whole


def _glob_reaches(policy: Policy, token_abs: str) -> str | None:
    """Could this glob (`~/.s*/id_rsa`, `~/.ws/secret?/KEY`) name something a deny_read rule covers? It does when,
    segment by segment over the rule's folder, each of the glob's segments can match the rule's."""
    glob = [g for g in token_abs.split("/") if g]
    key = (_home(), str(ws_home()))
    for rule in policy.gate.deny_read:
        root = _rule_forms(rule, *key)[1]
        if root is None:
            continue
        if _segments_reach(glob, [r for r in root.split("/") if r]):
            return rule
    return None


# A process's own folder (and its threads'): a recursive read there reaches its `environ` files.
_PROC_TREE = re.compile(r"/proc/(?:\d+|self|thread-self)(?:/task(?:/\d+)?)?")


def _read_hit(policy: Policy, path: str, cwd: str, recursive: bool = False, folder: bool = True) -> str | None:
    """The deny_read rule a path falls under, or None. With `folder`, naming the denied folder itself counts; with
    `recursive`, so does a folder above it (`grep -r x ~` reads `~/.ssh` too)."""
    if not policy.gate.deny_read or not path:
        return None
    p = _abs(path, cwd)
    if re.search(r"[*?\[]", p):
        return _glob_reaches(policy, p)
    forms = {p}
    try:
        forms.add(os.path.realpath(p))  # a symlink into a denied folder is still that folder
    except OSError:
        pass
    if recursive and any(_PROC_TREE.fullmatch(f) for f in forms):
        proc_rule = next((r for r in policy.gate.deny_read if r.startswith("/proc/")), None)
        if proc_rule:
            return proc_rule
    key = (_home(), str(ws_home()))
    for rule in policy.gate.deny_read:
        rx, root, whole = _rule_forms(rule, *key)
        for f in forms:
            if rx.match(f):
                if rule in _ENV_RULES and posixpath.basename(f) in _ENV_TEMPLATES:
                    continue
                return rule
            if root is None:
                continue
            if folder and whole and f == root:
                return rule
            if recursive and (f == "/" or root.startswith(f + "/") or f == root):
                return rule
    return None


def _under(paths: tuple[str, ...], path: str, cwd: str, ancestors: bool = False) -> str | None:
    """The protected path that `path` is, or is inside. With `ancestors`, also one that is inside `path`: removing,
    moving or unpacking over a folder takes everything under it too."""
    p = _abs(path, cwd)
    forms = {p, os.path.realpath(p)}
    for q in paths:
        qa = _abs(q, cwd)
        for target in {qa, os.path.realpath(qa)}:
            if any(
                f == target or f.startswith(target + "/") or (ancestors and target.startswith(f + "/")) for f in forms
            ):
                return q
    return None


# --- one command line ---------------------------------------------------------------------------------------------


def _expand_vars(seg: str, ctx: _Ctx) -> str:
    known = ctx.vars or {}
    return _VAR.sub(lambda m: known.get(m.group(1) or m.group(2), m.group(0)), seg) if known else seg


_BRACE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_FETCHERS = {"curl", "wget", "nc", "ncat", "ssh", "scp", "ftp", "fetch"}


def _braces(word: str, limit: int = 32) -> list[str]:
    """`x{1,2}y` -> x1y, x2y (one shell word becomes several)."""
    m = _BRACE.search(word)
    if not m:
        return [word]
    out: list[str] = []
    for alt in m.group(1).split(","):
        out += _braces(word[: m.start()] + alt + word[m.end() :], limit)
        if len(out) >= limit:
            break
    return out[:limit]


def _dotted(op: str, cwd: str) -> str:
    """`.`, `..`, `*` and `../..` name a place that depends on where the command runs: spell it out."""
    if op and not op.startswith(("/", "~", "$", "-")) and re.fullmatch(r"[./*]+", op):
        return _canon_path(_abs(op, cwd))
    return op


_VALUE_BREAKS = re.compile(r"[=@<>:,]")
_PATH_START = "~/$."  # a piece that starts like a path is tested wherever it sits in the word
_VALUE_LEAD = "=@<"  # a piece straight after one of these is a value, so it is tested whatever it starts with
_FILE_URL = re.compile(r"file://(?:localhost|127\.0\.0\.1|\[::1\])?(?=/)", re.IGNORECASE)
_PATH_MAX = 4096  # the longest path the system takes; a piece is judged by its first PATH_MAX characters
WORD_PIECE_CAP = 256  # path-like pieces one word may hold before the matcher refuses it rather than test them all
COMMAND_PIECE_CAP = 1024  # the same for a whole command line
COMMAND_WORD_CAP = 4096  # words a command line may hold before the matcher refuses it
_SUFFIX_TESTS = 8  # pieces per word that are also judged together with the rest of the word


class TooManyPieces(Exception):
    """A word or command holds more path-like pieces than the matcher will test. The matcher denies it, because
    testing them all takes longer than a runner's hook may run."""


def _word_paths(word: str, spent: list[int] | None = None) -> list[str]:
    """The places a word may name besides itself: the text after `=`, `@`, `<`, `>`, `:` or `,` (`if=P`, `f=@P`,
    `-d@P`, `host:P`, `file://P`), each piece between them, and the value glued to a short option (`-oP`, `-sSLoP`). A
    word that looks like a path is also judged whole, which covers the argument of an option the matcher does not
    know. Only a piece that starts like a path (`~`, `/`, `$`, `.`) or follows `=`, `@` or `<` is tested. `spent[0]`
    counts the pieces tested over a command line; one word past WORD_PIECE_CAP, or the line past COMMAND_PIECE_CAP,
    raises TooManyPieces."""
    flag = word.startswith("-") and len(word) > 1
    breaks = [m.start() for m in _VALUE_BREAKS.finditer(word)]
    starts: list[int] = [] if flag else [0]
    free = len(starts)
    if flag and word[1] != "-" and len(word) > 2:
        # A value glued to a cluster of short options: `-sT~/x`, `-sSLT$HOME/x`, `-0a~/x`.
        starts.append(2)
        starts += [k for k in range(3, min(len(word), 66)) if word[k] in _PATH_START]
        free = 1
    starts += [
        e for e in (b + 1 for b in breaks) if e < len(word) and (word[e] in _PATH_START or word[e - 1] in _VALUE_LEAD)
    ]
    extra = len(starts) - free
    if extra > WORD_PIECE_CAP or (spent is not None and spent[0] + extra > COMMAND_PIECE_CAP):
        raise TooManyPieces
    if spent is not None:
        spent[0] += extra
    found: list[str] = []
    if not flag and ("/" in word or _is_pathlike(word)):
        found.append(word[:_PATH_MAX])
    for n, st in enumerate(starts):
        i = bisect.bisect_left(breaks, st)
        end = breaks[i] if i < len(breaks) else len(word)
        found.append(word[st : min(end, st + _PATH_MAX)])
        if end < len(word) and (n < free or n - free < _SUFFIX_TESTS):
            found.append(word[st : st + _PATH_MAX])
    for m in itertools.islice(_FILE_URL.finditer(word), _SUFFIX_TESTS):
        found.append(word[m.end() : m.end() + _PATH_MAX])
    out = []
    for p in found:
        if not p:
            continue
        out.append(p)
        if p.startswith("//") and p.lstrip("/")[:1] in ("~", "$"):
            out.append(p.lstrip("/"))
    return list(dict.fromkeys(out))


def _home_folder(op: str, cwd: str) -> bool:
    """Is this operand one person's whole home folder (`/home/NAME`, `/Users/NAME`)? The folders above it are rules in
    the deny list; a path further down is ordinary work."""
    return re.fullmatch(f"/(?:{'|'.join(_HOME_PARENTS)})/[^/]+", _abs(op, cwd)) is not None


def _system_folder(policy: Policy, op: str, cwd: str) -> str | None:
    """The folder a recursive delete of this operand would take, when the deny_commands list names the root it sits in:
    what is directly in `/mnt`, `/media`, `/Volumes` or `/run` (`/mnt/data`), and anything under `/private` except its
    temp folders. A path further down in the mount folders is ordinary work."""
    p = _abs(op, cwd)
    for d in _MOUNT_DIRS:
        shape = f"/{d}/[^/]+" + (
            "|/run/user/[^/]+" if d == "run" else ""
        )  # a person's runtime folder is `/run/user/UID`
        if f"rm -* /{d}" in policy.gate.deny_commands and re.fullmatch(shape, p):
            return p
    if "rm -* /private" in policy.gate.deny_commands and p.startswith("/private/") and not p.startswith(_PRIVATE_TEMP):
        return p
    return None


# Settings that make git run a command, or change what a later git command means (an alias can spell a forcing push).
_GIT_RUNS = (
    "core.hookspath", "core.sshcommand", "core.fsmonitor", "core.pager", "core.editor", "core.askpass",
    "core.gitproxy", "core.alternaterefscommand", "credential.helper", "diff.external", "gpg.program",
    "sequence.editor", "include.path",
)  # fmt: skip
# The same, for keys with a name in the middle (`filter.NAME.clean`, `credential.URL.helper`, `pager.COMMAND`).
_GIT_RUNS_NAMED = re.compile(
    r"alias\..+|pager\..+|filter\..+\.(?:clean|smudge|process)|diff\..+\.(?:textconv|command)|merge\..+\.driver"
    r"|(?:diff|merge)tool\..+\.(?:cmd|path)|gpg\..+\.program|credential\.(?:.+\.)?helper"
    r"|remote\..+\.(?:uploadpack|receivepack|vcs)"
)
_GIT_MIRROR = re.compile(r"remote\..+\.mirror")
_GIT_PUSH_KEY = re.compile(r"remote\..+\.push")
_GIT_OFF = {"false", "no", "off", "0", ""}
# Environment names that give git a program to run or a config file of the caller's choosing.
_GIT_ENV_RUNS = frozenset({
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT", "GIT_SSH_COMMAND", "GIT_SSH", "GIT_EXTERNAL_DIFF",
    "GIT_PAGER", "GIT_ASKPASS", "GIT_PROXY_COMMAND",
})  # fmt: skip
_GIT_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "--list", "-l", "--show-origin", "--show-scope"}
_GIT_WRITES = {"--add", "--replace-all", "--unset", "--unset-all", "--edit", "-e"}
_GIT_ENV_WORD = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=(.*)", re.DOTALL)
_GIT_PARAM = re.compile(r"'([^']*)'(?:='([^']*)')?")
# Files git reads its settings from: a repository's, the person's, the system's.
_GIT_CONFIG_FILE = re.compile(
    r"(?:/[^/]*\.git|/\.git/(?:modules|worktrees)/.+)/config(?:\.worktree)?\Z|/\.gitconfig\Z"
    r"|/\.config/git/config\Z|\A/etc/gitconfig\Z"
)


def _git_setting_runs(key: str, value: str | None) -> bool:
    """Does this setting give git an alias or a command to run, or turn on a mirror or forcing push? `value` is None
    when none was given (a bare `-c KEY`, which reads as true) and "?" when it can't be seen (`--config-env`)."""
    k = key.lower()
    if k in _GIT_RUNS or _GIT_RUNS_NAMED.fullmatch(k):
        return True
    if _GIT_MIRROR.fullmatch(k):
        return value is None or value.strip().lower() not in _GIT_OFF
    if _GIT_PUSH_KEY.fullmatch(k):
        return value == "?" or (value is not None and (value.startswith(("+", ":")) or ":+" in value))
    return False


def _git_env_hazard(words: list[str]) -> str | None:
    """The environment a command line sets for git (`NAME=value` words, also after `env` and `export`)."""
    assigned = {m.group(1): m.group(2) for w in words if (m := _GIT_ENV_WORD.fullmatch(w))}
    for name, value in assigned.items():
        if name in _GIT_ENV_RUNS:
            return f"{name} in the environment gives git a program to run or a config file"
        m = re.fullmatch(r"GIT_CONFIG_KEY_(\d+)", name)
        if m and _git_setting_runs(value, assigned.get(f"GIT_CONFIG_VALUE_{m.group(1)}", "?")):
            return "an environment setting gives git an alias or a command to run"
        if name == "GIT_CONFIG_PARAMETERS":
            pairs = [(k, v or None) for k, v in _GIT_PARAM.findall(value)]
            if any(_git_setting_runs(k, v) for k, v in pairs or [(t, "?") for t in re.findall(r"[\w.*-]+", value)]):
                return "an environment setting gives git an alias or a command to run"
    return None


def _git_config_hazard(words: list[str], toks: list[str]) -> str | None:
    """A reason when this command gives git an alias or a command to run, or a mirror or forcing push: `git config
    KEY VALUE` (and `git config --edit`, which opens an editor), `git -c KEY=VALUE` and `--config-env` for such a
    key, and the environment names git reads (GIT_CONFIG_KEY_n, GIT_CONFIG_PARAMETERS, GIT_SSH_COMMAND ...). Reading
    a key stays allowed."""
    env = _git_env_hazard(words)
    if env:
        return env
    if not toks or toks[0] != "git":
        return None
    opts, rest = git_split(toks)
    for i, o in enumerate(opts):
        if o == "-c" and i + 1 < len(opts):
            key, eq, value = opts[i + 1].partition("=")
            runs = _git_setting_runs(key, value if eq else None)
        elif o == "--config-env" and i + 1 < len(opts) or o.startswith("--config-env="):
            key = (opts[i + 1] if o == "--config-env" else o.split("=", 1)[1]).split("=", 1)[0]
            runs = _git_setting_runs(key, "?")
        else:
            continue
        if runs:
            return f"git option {key.lower()} gives git an alias or a command to run"
    if rest[1:2] == ["remote"] and any(f.startswith("--mirror") for f in _args(rest[1:])[0]):
        return "git remote would set up a mirror, which pushes every ref"
    if rest[1:2] == ["config"]:
        flags, operands = _args(rest[1:])
        if "--edit" in flags or _cluster(flags, "e"):
            return "git config --edit opens an editor, which is a command for git to run"
        if not any(f in _GIT_READS for f in flags) and (len(operands) > 1 or any(f in _GIT_WRITES for f in flags)):
            for n, key in enumerate(operands):
                if _git_setting_runs(key, operands[n + 1] if n + 1 < len(operands) else None):
                    return "git config would set an alias or a command for git to run"
    return None


def _opt_values(toks: list[str], letters: str = "", longs: tuple[str, ...] = ()) -> list[str]:
    """The values given to an option: `-o X`, `-sSLo X` (the option last in a group), `--output X`, `--output=X`."""
    out = []
    for i, t in enumerate(toks[1:], 1):
        nxt = toks[i + 1] if i + 1 < len(toks) else None
        if t.startswith("--"):
            for lg in longs:
                if t == lg and nxt is not None:
                    out.append(nxt)
                elif t.startswith(lg + "="):
                    out.append(t.split("=", 1)[1])
        elif re.fullmatch(r"-[A-Za-z]+", t) and t[-1] in letters and nxt is not None:
            out.append(nxt)
    return out


_ADMIN = {"secret", "broker", "approve", "uninstall"}
_ADMIN_SUB = {("skills", "adopt"), ("skills", "restore"), ("config", "fallback")}


def _gate_flag(rest: list[str]) -> bool:
    """`--gate launch` / `--gate=launch` (argparse takes any prefix of the flag, from `--g`) on a `ws install` line."""
    for i, t in enumerate(rest):
        name, eq, value = t.partition("=")
        if len(name) >= 3 and "--gate".startswith(name) and name.startswith("--g"):
            nxt = value if eq else (rest[i + 1] if i + 1 < len(rest) else "")
            if nxt != "every-session":
                return True
    return False


def _ws_admin(canon: list[str]) -> str | None:
    """`ws gate generate|init`, `ws secret ...`, `ws broker ...`, `ws approve ...`, `ws uninstall`, `ws install --gate
    launch`, `ws skills adopt|restore`, `ws config fallback` (also `uv run ws ...`, `python -m ws ...`): a gated session
    must not regenerate or take off its own gate, touch secrets, approve its own call or swap what the person installed.
    A plain `ws install` only refreshes, so it stays allowed."""
    for i, t in enumerate(canon):
        if posixpath.basename(t) != "ws" or not (
            i == 0 or canon[i - 1] in ("run", "-m", "--", "exec", "tool") or canon[i - 1].startswith("-")
        ):
            continue
        sub = [x for x in canon[i + 1 :] if not x.startswith("-")][:2]
        if sub[:1] == ["gate"] and ("generate" in canon[i + 2 :] or "init" in canon[i + 2 :]):
            return "ws gate generate/init is not for a gated session: it would rewrite its own gate"
        if sub[:1] and sub[0] in _ADMIN:
            return f"ws {sub[0]} is not for a gated session"
        if tuple(sub[:2]) in _ADMIN_SUB:
            return f"ws {sub[0]} {sub[1]} is not for a gated session"
        if sub[:1] == ["install"] and _gate_flag(canon[i + 1 :]):
            return "ws install --gate launch is not for a gated session: it would take off its own gate"
    return None


def _walk(policy: Policy, cmd: str, ctx: _Ctx, depth: int, cands: list[str]) -> str | None:
    """Look at every part of a command line. Fills `cands` with the strings the deny globs are tried on and returns
    the reason at once for the checks that are not globs (pipe into a shell, denied paths, protected files)."""
    if depth > _MAX_DEPTH:
        raise ValueError("command nests too deeply to check")
    pipelines, inner = _scan(_prep(cmd), heredocs=ctx.heredocs and depth == 0)
    nested: list[str] = list(inner)
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


def _segment(policy: Policy, seg: str, ctx: _Ctx, in_pipe: bool, cands: list[str]) -> tuple[str | None, str, list[str]]:
    cands.append(" ".join(seg.split()))
    words, redirs = _lex(_expand_vars(seg, ctx))
    words = [b for w in words for b in _braces(w)]
    if not words and not redirs:
        return None, "", []
    ctx.spent[1] += len(words)
    if ctx.spent[1] > COMMAND_WORD_CAP:
        return (
            f"the command holds more than {COMMAND_WORD_CAP:,} words, too many to check in time",
            " ".join(words[:8]),
            [],
        )
    if words and all(_ASSIGN.match(w) for w in words):  # X=/ ; later $X
        ctx.vars = {**(ctx.vars or {}), **dict(w.split("=", 1) for w in words)}
    elif len(words) > 3 and words[0] == "for" and words[2] == "in":  # for i in /; do ... $i
        ctx.vars = {**(ctx.vars or {}), words[1]: words[3]}
    toks, wrappers = _strip_wrappers(words)
    canon = _canon_tokens(toks)
    line = " ".join(canon)
    if line:
        cands.append(line)
    elif "env" in wrappers:
        cands.append("env")  # `env`, `env -i`, `env A=b`: nothing after it to run, so it prints the environment
    cmd = canon[0] if canon else ""
    flags, operands = _args(canon)
    nested: list[str] = []

    if cmd in ("cd", "pushd"):
        ctx.cwd = _abs(operands[0] if operands else "~", ctx.cwd)
    admin = _ws_admin(canon)
    if admin:
        return admin, line, []
    if cmd == "eval":
        text = " ".join(toks[1:])
        fetched = re.match(r"\s*(?:\$\(|`)\s*(\S+)", text)
        if fetched and posixpath.basename(fetched.group(1)) in _FETCHERS:
            return "eval of text a network command fetches", line, []
        nested.append(text)
    if cmd in _SHELL_NAMES:
        reason, nested = _shell(toks, canon, flags, operands, redirs, in_pipe)
        if reason:
            return reason, line, []
    if in_pipe and _INTERP.match(cmd) and operands in ([], ["-"]) and not any(_cluster(flags, c) for c in "cemp"):
        return "an interpreter is reading its program from a pipe", line, []
    if cmd in ("source", ".") and _PROCSUB in operands:
        return "a script is being sourced from a process substitution", line, []
    if cmd in _OPERAND_CMDS:
        cands += [" ".join([cmd, *flags, _dotted(op, ctx.cwd)]) for op in operands[:64]]
    if cmd == "find":
        cands += _find_deletes(canon, ctx.cwd)
    if (
        "xargs" in wrappers
        and cmd == "rm"
        and (_cluster(flags, "r") or _cluster(flags, "R"))
        and all(o == "{}" for o in operands)
        and any(p.startswith("rm ") for p in policy.gate.deny_commands)
    ):
        return "recursive rm fed by xargs: the operands cannot be seen, and rm is on the deny list", line, []

    # Paths this command names: operands, --opt=value, redirect targets.
    named = [*operands, *(f.split("=", 1)[1] for f in flags if "=" in f), *(t for _, t in redirs if t)]
    recursive = (
        cmd in _ALWAYS_RECURSIVE
        or _cluster(flags, "r")
        or _cluster(flags, "R")
        or "--recursive" in flags
        or "--archive" in flags
        or (cmd in ("cp", "rsync") and _cluster(flags, "a"))
    )
    if recursive and _searches_cwd(cmd, canon, operands):
        named.append(".")  # `cd ~ && grep -r x`: no path given means here
    # What a copy writes is made, not read: `cp .env.example .env` creates the env file, so it is the one place an
    # env-file rule does not stop the last operand. A source, or any other rule, still does.
    into_dir = _cluster(flags, "t") or any(f.startswith("--target-directory") for f in flags)
    made = operands[-1] if cmd in _WRITE_LAST and len(operands) > 1 and not into_dir else None
    for p in named:
        rule = _read_hit(policy, p, ctx.cwd, recursive=recursive)
        if rule and not (p == made and rule in _ENV_RULES):
            return f"command names a path denied by deny_read rule {rule!r}", line, nested
    # Every word as typed, wrappers and their options included: a path sits in an option's value (`if=P`, `f=@P`,
    # `-a P`) as often as it is an operand, whichever option the matcher knows. A folder above a protected one is
    # judged only for the operands above, so a bare `/` inside a value is not a recursive read of everything.
    try:
        for word in words:
            for p in _word_paths(word, ctx.spent):
                rule = _read_hit(policy, p, ctx.cwd, recursive=recursive and p != "/")
                if rule and not (p == made and rule in _ENV_RULES):
                    return f"command names a path denied by deny_read rule {rule!r}", line, nested
    except TooManyPieces:
        reason = (
            f"a word or the command holds more than {WORD_PIECE_CAP} (or {COMMAND_PIECE_CAP} in all) path-like pieces, "
            "too many to check in time; shorten it or move the text to a file"
        )
        return reason, line, nested
    doomed = _find_starts(canon) if cmd == "find" else None
    if cmd == "rm" and (_cluster(flags, "r") or "--recursive" in flags):
        doomed = operands
    for op in doomed or ():
        if _home_folder(op, ctx.cwd):
            return "a recursive delete of a whole home folder", line, nested
        folder = _system_folder(policy, op, ctx.cwd)
        if folder:
            return f"a recursive delete of {folder}, which the policy keeps", line, nested
    config = _git_config_hazard(words, toks)
    if config:
        return config, line, nested
    written = _written(cmd, flags, operands, redirs, canon, toks)
    for p, _ in written:
        if _GIT_CONFIG_FILE.search(_abs(p, ctx.cwd)):
            return (
                "a write to a git config file can give git an alias or a command to run; use git config",
                line,
                nested,
            )
    if ctx.protect:
        for p, ancestors in written:
            hit = _under(ctx.protect, p, ctx.cwd, ancestors)
            if hit:
                return f"command writes to {hit}, which ws protects (policy and generated gate)", line, nested
    return None, line, nested


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
    if _PROCSUB in operands:
        return "a shell is reading its script from a process substitution", nested
    if _cluster(flags, "c"):
        if operands and operands[0].lstrip().startswith(("$(", "`")):
            return "a shell is running text another command produces", nested
        if operands:
            return None, [*nested, operands[0]]
        stdin = True
    else:
        first = operands[0] if operands else None
        stdin = (
            _cluster(flags, "s")
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


def _find_starts(canon: list[str]) -> list[str] | None:
    """The folders a `find` deletes from (`find P -delete`, `find P -exec rm ...`), or None when it deletes nothing. Its
    leading `-H`, `-L`, `-P`, `-Olevel` and `-D opts` are not folders."""
    rest = canon[1:]
    deleting = "-delete" in rest or any(
        rest[i] in ("-exec", "-execdir") and rest[i + 1 : i + 2] == ["rm"] for i in range(len(rest))
    )
    if not deleting:
        return None
    starts, i = [], 0
    while i < len(rest):
        t = rest[i]
        if t == "-D":
            i += 2
        elif t in ("-H", "-L", "-P") or re.fullmatch(r"-O\d*", t):
            i += 1
        elif t.startswith(("-", "(", "!", ",")):
            break
        else:
            starts.append(t)
            i += 1
    return starts or ["."]


def _find_deletes(canon: list[str], cwd: str) -> list[str]:
    """`find P -delete` and `find P -exec rm ...` as the `rm -rf P` they amount to."""
    return [f"rm -fr {_dotted(p, cwd)}" for p in _find_starts(canon) or []]


def _written(cmd, flags, operands, redirs, canon, toks) -> list[tuple[str, bool]]:
    """Paths this command writes or removes, as (path, ancestors): redirect targets, operands of the commands that
    write, and where curl, wget, tar, unzip and zip put files. `ancestors` is true where a folder above a protected
    path is just as bad (rm, mv, and unpacking or copying a tree over it)."""
    out: list[tuple[str, bool]] = [(t, False) for op, t in redirs if op.startswith(">") or op == "<>"]
    recursive = any(_cluster(flags, c) for c in "rRa")
    if cmd in _WRITE_ALL:
        out += [(o, cmd in ("rm", "rmdir", "mv", "shred", "unlink")) for o in operands]
    elif cmd in _WRITE_LAST:
        deep = recursive or cmd == "rsync"
        out += [(o, deep) for o in operands[-1:]]
        out += [(v, deep) for v in _opt_values(toks, "t", ("--target-directory",))]
    elif cmd in ("sed", "perl", "ruby") and any(
        f.startswith("--in-place") or re.match(r"^-[A-Za-z]*i", f) for f in flags
    ):
        out += [(o, False) for o in operands]
    elif cmd == "dd":
        out += [(o[3:], False) for o in operands if o.startswith("of=")]
    elif cmd == "curl":
        out += [(v, False) for v in _opt_values(toks, "o", ("--output",))]
        if any(t == "--remote-name" or re.fullmatch(r"-[A-Za-z]*O[A-Za-z]*", t) for t in toks[1:]):
            out.append((".", True))
    elif cmd == "wget":
        files = _opt_values(toks, "O", ("--output-document",))
        dirs = _opt_values(toks, "P", ("--directory-prefix",))
        out += [(v, False) for v in files] + [(v, True) for v in dirs]
        if not files and not dirs:
            out.append((".", True))
    elif cmd in ("tar", "bsdtar"):
        if len(toks) > 1 and re.fullmatch(r"[A-Za-z]+", toks[1]):  # old style: tar czf x.tgz
            toks = [toks[0], "-" + toks[1], *toks[2:]]
        modes = "".join(t[1:] for t in toks[1:] if re.fullmatch(r"-[A-Za-z]+", t))
        archives = _opt_values(toks, "f", ("--file",))
        if "x" in modes:
            dirs = _opt_values(toks, "C", ("--directory",))
            out += [(d, True) for d in dirs or ["."]]
        elif any(m in modes for m in "cru"):
            out += [(a, False) for a in archives]
    elif cmd == "unzip":
        out += [(d, True) for d in _opt_values(toks, "d") or ["."]]
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


def simple_commands_env(
    command: str, _depth: int = 0, *, git_options: bool = False
) -> list[tuple[list[str], list[str]]]:
    """simple_commands with the `VAR=value` words each command was given before it (a prefix, or after `env`), as
    (words, assignments). A line that only sets variables comes back with no words, so a check sees `X=1; cmd`."""
    if not isinstance(command, str):
        raise TypeError("command must be text")
    if _depth > _MAX_DEPTH:
        raise ValueError("command nests too deeply to check")
    pipelines, inner = _scan(_prep(command), heredocs=not _folds(command))
    found: list[tuple[list[str], list[str]]] = []
    nested = list(inner)
    for segs in pipelines:
        for seg in segs:
            env: list[str] = []
            toks, _ = _strip_wrappers(_lex(seg)[0], env)
            if toks and toks[0] == "git" and not git_options:
                toks = _git_normal(toks)
            if not toks and not env:
                continue
            found.append((toks, env))
            if toks and toks[0] == "eval" and len(toks) > 1:  # eval joins its words and runs the text as a command line
                nested.append(" ".join(toks[1:]))
            if toks and toks[0] in _SHELL_NAMES:
                flags, operands = _args(toks)
                if _cluster(flags, "c") and operands:
                    nested.append(operands[0])
    for text in nested:
        found += simple_commands_env(text, _depth + 1, git_options=git_options)
    return found


def _readings(command: str) -> list[str]:
    """The texts to judge for one command: itself and, when it holds a backslash-newline, that pair removed wherever it
    is (the way older ws read it, which also joins a comment's trailing backslash to the next line). The shell reads
    one of them; a rule that denies either reading denies the command."""
    return [command, command.replace("\\\n", "")] if "\\\n" in command else [command]


def _walk_command(policy: Policy, command: str, cwd: str, protect: tuple[str, ...], cands: list[str]) -> str | None:
    """_walk for a whole command. In the one plain shape _without_bodies accepts, the bodies are text and the lines
    around them are judged. Any other command is judged with every body read as commands (so a body can't hide
    anything) and then once more with the bodies left out, so a stray quote in a body can't hide the lines after it."""
    data = _without_bodies(command)
    if data is not None:
        return _walk(policy, data, _Ctx(cwd, protect), 0, cands)
    for text in _readings(command):
        reason = _walk(policy, text, _Ctx(cwd, protect), 0, cands)
        if reason:
            return reason
    if "<<" in command and not _folds(command):
        return _walk(policy, command, _Ctx(cwd, protect, None, True), 0, cands)
    return None


def _open_quote(command: str) -> str:
    """The quote still open at the end of the command text (`'`, `"` or `$'`), or "" if every quote is closed. Comments
    and here-document bodies are not read: an apostrophe in prose there is not a quote."""
    end: dict = {}
    _scan(_prep(command), heredocs=True, end=end)
    return end.get("quote", "")


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
    quote = _open_quote(command)
    if quote:
        return f"the command ends inside an unclosed {quote} quote, so what runs cannot be read; close the quote"
    cands: list[str] = []
    reason = _walk_command(policy, command, cwd or _cwd(), tuple(protect), cands)
    # A deny_commands rule that matched is named first, whether or not a path check also stopped the walk (the parts it
    # had read by then are in `cands`), so the reason says the rule that applies to the command.
    for pat in policy.gate.deny_commands:
        rx = _glob_regex(_canon_pattern(pat))
        if any(rx.match(c) for c in cands):
            rule = f"command matches deny_commands rule {pat!r}"
            return f"{rule}; also {reason}" if reason else rule
    return reason


def needs_approval(policy: Policy, command: str, *, cwd: str | None = None) -> bool:
    """True if some shell segment of the command matches an `ask_commands` rule. It reads the line as
    is_denied_command does (so `bash -c "git push"` asks as `git push` does). Call it after is_denied_command: a
    command that is denied outright never gets as far as asking."""
    if not isinstance(command, str):
        raise TypeError("command must be text")
    if not policy.gate.ask_commands:
        return False
    cands: list[str] = []
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
    if len(toks) < 2 or not _FLAGS.fullmatch(toks[1]) or len(toks[1]) > 4:
        return [pattern]
    from itertools import permutations

    letters = toks[1][1:]
    forms = {toks[1]}
    for perm in permutations(letters):
        forms.add("-" + "".join(perm))
        forms.add(" ".join("-" + ch for ch in perm))
    ordered = [toks[1]] + sorted(forms - {toks[1]})
    return [" ".join([toks[0], f, *toks[2:]]) for f in ordered[:limit]]
