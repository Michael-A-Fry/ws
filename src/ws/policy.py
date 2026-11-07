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

Known limits of the matcher. It reads command text, so it cannot see:
  * what an interpreter does inside its own program (`python3 -c`, `perl -e`, `node -e`, a script it runs);
  * a script that is downloaded and run in two steps (`curl -o a.sh ... && bash a.sh`);
  * a path built entirely by a command, or by a variable set outside the command line (a built part followed by a
    protected path is refused);
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

import contextlib
import itertools
import os
import posixpath
import re
import tomllib
from dataclasses import dataclass, field, replace
from functools import lru_cache
from pathlib import Path

from ws import shellwords
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
deny_read = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/credentials", "~/.config/ws/secrets/**"]
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
        if not _HOST.match(h):
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

_FLAGS = re.compile(r"^-[A-Za-z]+$")
_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh", "fish", "ash", "csh", "tcsh"}
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

    def fork(self) -> _Ctx:
        return _Ctx(
            self.cwd,
            self.protect,
            dict(self.vars or {}),
            budget=self.budget,
            oldpwd=self.oldpwd,
            loops=self.loops,
            unknown=self.unknown,
            distrust=self.distrust,
        )


def _home() -> str:
    return os.environ.get("HOME") or str(Path.home())


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return "/"


@lru_cache(maxsize=8)
def _base_env(home: str | None, ws: str | None) -> tuple[tuple[str, str], ...]:
    """`HOME` and `WS_HOME` as a command word sees them, made once for the environment they come from."""
    return (("HOME", _home().rstrip("/") or "/"), ("WS_HOME", str(ws_home())))


def _env(known: dict[str, str] | None = None, cwd: str = "", oldpwd: str = "") -> dict[str, str]:
    """The variables a command word may use: `HOME` and `WS_HOME`, the folder the command runs in as `PWD` (and the one
    before the last `cd` as `OLDPWD`) when it is given, then what earlier parts of the line set."""
    where = {"PWD": cwd} if cwd else {}
    if oldpwd:
        where["OLDPWD"] = oldpwd
    return {**dict(_base_env(os.environ.get("HOME"), os.environ.get("WS_HOME"))), **where, **(known or {})}


def _expand(path: str) -> str:
    """~, ~user, $HOME, ${HOME}, $WS_HOME and ${WS_HOME} to a real prefix: a path as a rule or a tool writes it."""
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
    """Could this glob (`~/.s*/id_rsa`, `~/.ws/secret?/KEY`) name something a deny_read rule covers? It does when,
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
def _rules(deny_read: tuple[str, ...], home: str, ws: str) -> tuple[tuple[str, re.Pattern, str | None], ...]:
    """Each deny_read rule with its pattern and root, made once for a home and ws home (`home` and `ws` are only the
    cache key: the expansion reads the same environment)."""
    return tuple((rule, _path_regex(rule), _rule_root(rule)) for rule in deny_read)


_KNOWN_NAMES = ("HOME", "WS_HOME", "PWD", "OLDPWD")
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
    for rule, rx, root in _rules(policy.gate.deny_read, _home(), os.environ.get("WS_HOME", "")):
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
    for q in paths:
        qa = _abs(q, cwd)
        for target in {qa, os.path.realpath(qa)}:
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
        raise shellwords.ShellError("too_deep", _TOO_DEEP)
    pipelines, inner = shellwords.scan(shellwords.prep(cmd), heredocs=ctx.heredocs and depth == 0)
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
    working folders aren't either (eval text can set them; a sourced file is trusted to leave them)."""
    gone = set(ctx.vars or {}) | set(ctx.loops or {}) | ({"HOME", "PWD", "OLDPWD", "WS_HOME"} if folders else set())
    ctx.unknown = ctx.unknown | gone
    ctx.vars, ctx.loops, ctx.distrust = {}, {}, True


def _segment_once(
    policy: Policy, seg: str, ctx: _Ctx, in_pipe: bool, cands: list[str], loop_vars: dict[str, str]
) -> tuple[str | None, str, list[str]]:
    cands.append(" ".join(seg.split()))
    env = shellwords.Unknown(
        _env({**(ctx.vars or {}), **loop_vars}, ctx.cwd, ctx.oldpwd),
        ctx.unknown - set(loop_vars),
        ctx.distrust,
        keep={"HOME", "PWD", "OLDPWD", "WS_HOME"} - ctx.unknown,
    )
    lexed = shellwords.lex(seg, env, ctx.budget)
    words, redirs = lexed.words, lexed.redirs
    if not words and not redirs:
        return None, "", []
    _mark_unknown(ctx, seg, words)
    if words and all(shellwords.ASSIGN.match(w) for w in words):  # X=/ ; later $X
        _assign(ctx, dict(w.split("=", 1) for w in words))
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
        _assign(ctx, dict(w.split("=", 1) for w in toks[1:] if shellwords.ASSIGN.match(w)))
    elif toks and toks[0] == "read":  # read -r X <<< /
        _assign(ctx, _read_values(toks, redirs))
    if _sets_anything(toks, seg):
        _distrust(ctx, folders=bool(toks) and toks[0] in ("eval", "coproc"))
    canon = _canon_tokens(toks)
    line = " ".join(canon)
    if line:
        cands.append(line)
    cmd = canon[0] if canon else ""
    flags, operands = shellwords.split_args(canon)

    if cmd in ("cd", "pushd"):
        target = operands[0] if operands else "~"
        if target != "-" or ctx.oldpwd:
            ctx.oldpwd, ctx.cwd = ctx.cwd, _abs(ctx.oldpwd if target == "-" else target, ctx.cwd)
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
        reason, more = _shell(toks, canon, flags, operands, redirs, in_pipe)
        if reason:
            return reason, line, []
        nested += more
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
    for p in named:
        rule = _read_hit(policy, p, ctx.cwd, recursive=recursive, expand=False)
        if rule:
            return f"command names a path denied by deny_read rule {rule!r}", line, nested
    if ctx.protect:
        for p, ancestors in _written(cmd, flags, operands, redirs, canon, toks):
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
    if shellwords.PROCSUB in operands:
        return "a shell is reading its script from a process substitution", nested
    if shellwords.has_flag(flags, "c"):
        if operands and operands[0].lstrip().startswith(("$(", "`")):
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
    if toks and toks[0] in _SHELL_NAMES:
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
    for text in _readings(command):
        reason = _walk(policy, text, _Ctx(cwd, protect), 0, cands)
        if reason:
            return reason
    if "<<" in command and not shellwords.folds(command):
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
