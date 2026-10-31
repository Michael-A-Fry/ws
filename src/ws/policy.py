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
    TMPDIR and the runner's own folders (`~/.bashrc`, `~/.local/bin`, systemd user units, ws's installed source).
  * still open: a `deny_commands` rule is not enforced inside an interpreter (`python3 -c 'os.system("git push -f")'`
    pushes; only the sandbox's file rules apply to it). Reading is denied only for deny_read paths that exist when the
    runner starts and have no wildcard below their first folder. OpenCode's `patch` tool is not a shell command, so it
    is not sandboxed: its native `edit` rules are all that stop it. A runner started without the wrapper
    (`ws gate launch --no-sandbox`) has no wall at all, and the command says so.
  * not closed: a protected path that sits inside the project folder or an `allow_write` folder and does not exist when
    the runner starts (a --policy file never written) can be created; a program that writes a hard-coded path outside
    the project, TMPDIR and the runner's own folders fails (the wall is a write allow-list; `allow_write` lets one
    through), and so does `git config` or a git hook in the project (`.git/hooks` and `.git/config` are read-only);
    `ws gate launch` refuses a protected path behind a symbolic link the session could replace, and a start from the
    home folder or above; the macOS sandbox-exec profile (including its Unix-socket rule) is not run in CI and is
    unit-tested as text only; two gaps the last check found, not yet fixed: (1) `~/.cache/opencode` is writable inside
    the wall and holds programs OpenCode later runs outside it (bin/rg, node_modules/.bin plugins, language servers);
    planned fix: a walled run gets its own XDG data, state and cache folders. (2) the project lock covers only paths
    that exist at launch and does not bind their parents, so a `.claude/settings.json` hook, an `.opencode/` plugin or a
    `.git/hooks` script can be created, and `mv .git .git2` unlocks the hooks and config; planned fix: bind the locked
    paths' in-project parents onto themselves and add read-only placeholders for a missing `.claude`, `.opencode` and
    `.git/hooks`; a `deny_read` rule with a wildcard in the middle is matcher-only; Claude
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

import os
import posixpath
import re
import tomllib
import unicodedata
from dataclasses import dataclass
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
deny_read = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/credentials"]
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
    raw = _table(raw, "policy", {"schema", "dial", "interrupts", "sources", "gate", "exceptions"})
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
    gate = Gate(
        _strings(g, "deny_commands", "[gate] "),
        _strings(g, "deny_read", "[gate] "),
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
    return Policy(schema, dial, interrupts, sources, gate, tuple(exceptions))


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
        return loads(text)
    except PolicyError as e:
        raise PolicyError(f"{path}: {e}") from e


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

    def fork(self) -> _Ctx:
        return _Ctx(self.cwd, self.protect, dict(self.vars or {}))


def _home() -> str:
    return os.environ.get("HOME") or str(Path.home())


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return "/"


def _expand(path: str) -> str:
    """~, ~user, $HOME and ${HOME} to a real prefix."""
    home = _home().rstrip("/")
    for pre in ("${HOME}", "$HOME", "~"):
        if path == pre or path.startswith(pre + "/"):
            return home + path[len(pre) :]
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
    return tok.startswith(("/", "~", "$HOME", "${HOME}"))


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


def _git_normal(toks: list[str]) -> list[str]:
    """Drop git's global options (`-C dir`, `-c k=v`, `--git-dir=...`) so the subcommand is where a rule expects it;
    spell any forcing or destroying push (`-f`, `-uf`, `+refspec`, `:ref`, `--mirror`, `--delete`) as `--force`."""
    i = 1
    while i < len(toks) and toks[i].startswith("-"):
        i += 2 if toks[i] in _GIT_ARG_OPTS else 1
    toks = ["git", *toks[i:]]
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


def _strip_wrappers(words: list[str]) -> tuple[list[str], set[str]]:
    """Drop leading keywords, VAR=value and exec wrappers (sudo, env, timeout, nice, xargs, busybox ...): what is left
    is the command that really runs. Also returns the wrappers it removed."""
    toks, seen = list(words), set()
    while toks:
        w = toks[0]
        if w in _KEYWORDS or _ASSIGN.match(w):
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
    cmd = unicodedata.normalize("NFKC", cmd).replace("\\\n", "")
    return re.sub(r"\$\{IFS\}|\$IFS\b", " ", cmd)


def _scan(cmd: str) -> tuple[list[list[str]], list[str]]:
    """Split a shell command line. Returns (pipelines, inner): each pipeline is a list of segments (split on |, |&),
    pipelines are split on ; && || & newline ( ); inner holds the text of $(...), backticks and <(...), which run too."""
    pipelines: list[list[str]] = []
    inner: list[str] = []
    segs: list[str] = []
    buf: list[str] = []
    n, i, quote = len(cmd), 0, ""

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
            if c == quote:
                quote = ""
            elif quote == '"' and c == "`":
                j = cmd.find("`", i + 1)
                if j > 0:
                    inner.append(cmd[i + 1 : j])
            elif quote == '"' and cmd.startswith("$(", i):
                j = _close_paren(cmd, i + 2)
                inner.append(cmd[i + 2 : j])
            buf.append(c)
            i += 1
            continue
        if c in "'\"":
            quote = c
            buf.append(c)
        elif c == "`":
            j = cmd.find("`", i + 1)
            j = n if j < 0 else j
            inner.append(cmd[i + 1 : j])
            buf.append(cmd[i : j + 1])
            i = j + 1
            continue
        elif cmd.startswith("$(", i):
            j = _close_paren(cmd, i + 2)
            inner.append(cmd[i + 2 : j])
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
    return pipelines, inner


def _close_paren(cmd: str, i: int) -> int:
    """Index of the ')' that closes a '$(' whose body starts at i (the end of the text if it never closes)."""
    depth, quote = 1, ""
    while i < len(cmd):
        c = cmd[i]
        if c == "\\":
            i += 2
            continue
        if quote:
            quote = "" if c == quote else quote
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
        elif c == "[" and (j := pattern.find("]", i + 2)) > 0:
            body = pattern[i + 1 : j]
            out.append("[" + ("^" + body[1:] if body[0] == "!" else body) + "]")
            i = j
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


def _glob_reaches(policy: Policy, token_abs: str) -> str | None:
    """Could this glob (`~/.s*/id_rsa`) name something a deny_read rule covers? Tried against the rule's folder and a
    few names a secrets folder holds."""
    rx = _glob_regex_path(token_abs)
    for rule in policy.gate.deny_read:
        root = _rule_root(rule)
        if root is None:
            continue
        samples = [root] + [f"{root}/{x}" for x in ("id_rsa", "id_ed25519", "credentials", "key", "x")]
        if any(rx.match(s) for s in samples):
            return rule
    return None


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
    for rule in policy.gate.deny_read:
        rx, root = _path_regex(rule), _rule_root(rule)
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


_ADMIN = {"secret", "broker", "approve"}


def _ws_admin(canon: list[str]) -> str | None:
    """`ws gate generate|init`, `ws secret ...`, `ws broker ...`, `ws approve ...` (also `uv run ws ...`, `python -m ws ...`): a gated
    session must not regenerate its own gate, touch secrets or approve its own call."""
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
    return None


def _walk(policy: Policy, cmd: str, ctx: _Ctx, depth: int, cands: list[str]) -> str | None:
    """Look at every part of a command line. Fills `cands` with the strings the deny globs are tried on and returns
    the reason at once for the checks that are not globs (pipe into a shell, denied paths, protected files)."""
    if depth > _MAX_DEPTH:
        raise ValueError("command nests too deeply to check")
    pipelines, inner = _scan(_prep(cmd))
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
    if words and all(_ASSIGN.match(w) for w in words):  # X=/ ; later $X
        ctx.vars = {**(ctx.vars or {}), **dict(w.split("=", 1) for w in words)}
    elif len(words) > 3 and words[0] == "for" and words[2] == "in":  # for i in /; do ... $i
        ctx.vars = {**(ctx.vars or {}), words[1]: words[3]}
    toks, wrappers = _strip_wrappers(words)
    canon = _canon_tokens(toks)
    line = " ".join(canon)
    if line:
        cands.append(line)
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
    for p in named:
        rule = _read_hit(policy, p, ctx.cwd, recursive=recursive)
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
    cands: list[str] = []
    reason = _walk(policy, command, _Ctx(cwd or _cwd(), tuple(protect)), 0, cands)
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
    _walk(policy, command, _Ctx(cwd or _cwd()), 0, cands)
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
    """The deny_commands a runner can enforce from its own rule list, each in every flag spelling (see variants).
    The rest are enforced by the hook alone."""
    out: list[str] = []
    for pat in policy.gate.deny_commands:
        if not any(c in pat for c in _HOOK_ONLY):
            out += variants(pat)
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
