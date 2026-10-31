"""The policy: one vendor-neutral `policy.toml` that says what a session may do. Each runner's own config is generated
from it (ws.gate, ws.adapters), so the runner itself enforces the deny list; the matcher here backs the hooks and shims
that cover what a runner's native rules cannot.

Stdlib only. Three jobs:
  * load and validate the file (PolicyError names the bad key);
  * `is_denied_command` / `is_denied_read`: the one matcher every hook and shim calls;
  * `rule_of_two`: refuse a session that holds untrusted input, private data and a way out at the same time.
"""

from __future__ import annotations

import os
import posixpath
import re
import shlex
import tomllib
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


@dataclass(frozen=True)
class RuleException:
    name: str
    job: str
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
    out = ["rm -* /", "rm -* /\\*", "rm -* ~", "rm -* ~/\\*"]
    out += [f"rm -* /{d}*" for d in _SYSTEM_DIRS]
    out += ["git push*--force*", "git push* -f*", "mkfs*", "dd * of=/dev/*"]
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

# Named Rule of Two exceptions: a job that may break the rule, and why.
# [[exceptions]]
# name = "morning-digest"
# job = "digest"
# reason = "reads mail, writes only to me"
"""


# --- load and validate -------------------------------------------------------------------------------------------


def policy_path(env: dict | None = None) -> Path:
    return ws_home(env) / "policy.toml"


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

    g = _table(raw.get("gate", {}), "[gate]", {"deny_commands", "deny_read", "deny_tools"})
    gate = Gate(
        _strings(g, "deny_commands", "[gate] "),
        _strings(g, "deny_read", "[gate] "),
        _strings(g, "deny_tools", "[gate] "),
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
        e = _table(e, where.strip(), {"name", "job", "reason"})
        for k in ("name", "job", "reason"):
            if not isinstance(e.get(k), str) or not e[k].strip():
                raise PolicyError(f"{where}{k} is required (text)")
        if any(x.name == e["name"] for x in exceptions):
            raise PolicyError(f"{where}name {e['name']!r} is used twice")
        exceptions.append(RuleException(e["name"], e["job"], e["reason"]))
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


def rule_of_two(policy: Policy, sources: list[str], job: str | None = None) -> Verdict:
    """May a session read `sources` and run `job`? Refused when it would hold untrusted input AND private data AND a
    way out. A way out is an outward act of a source in mode "act" (only when the dial is not "prepare") or an
    allowed egress tool (web_fetch, web_search). A named exception for the job lifts the refusal."""
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
    if not (untrusted and sensitive and ways_out):
        return Verdict("ok")
    why = (
        f"untrusted input ({', '.join(untrusted)}), private data ({', '.join(sensitive)}) and a way out "
        f"({', '.join(ways_out)}) together"
    )
    for e in policy.exceptions:
        if job is not None and e.job == job:
            return Verdict("exception", f"{why}; allowed by exception {e.name!r}: {e.reason}", e.name)
    return Verdict("refused", f"Rule of Two: {why}")


# --- the matcher -------------------------------------------------------------------------------------------------

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_FLAGS = re.compile(r"^-[A-Za-z]+$")
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "time", "coproc"}
# wrapper -> options that take a following argument
_WRAPPERS = {
    "sudo": {"-u", "-g", "-h", "-p", "-C", "-D", "-R", "-T", "-U"},
    "doas": {"-u", "-C"},
    "env": {"-u", "-C", "-S"},
    "command": set(),
    "builtin": set(),
    "exec": {"-a"},
    "nohup": set(),
    "nice": {"-n"},
    "ionice": {"-c", "-n", "-p"},
    "setsid": set(),
    "stdbuf": set(),
    "timeout": {"-s", "-k"},
}
_SHELL_NAMES = {"sh", "bash", "zsh", "dash", "ksh", "fish"}


def _home() -> str:
    return os.environ.get("HOME") or str(Path.home())


def _canon_path(tok: str) -> str:
    """One spelling for the same place: ~, $HOME, ${HOME} and the real home dir all become ~; // and /./ and
    trailing slashes go. Only for tokens that look like a path."""
    home = _home().rstrip("/")
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


def _strip_wrappers(tokens: list[str]) -> list[str]:
    """Drop leading keywords, VAR=value, and exec wrappers (sudo, env, timeout, nice ...): what is left is the command
    that really runs."""
    while tokens:
        w = tokens[0]
        if w in _KEYWORDS or _ASSIGN.match(w):
            tokens = tokens[1:]
            continue
        if w.startswith("/") and "/" in w[1:]:
            w = tokens[0] = posixpath.basename(w)  # /usr/bin/rm -> rm
        if w not in _WRAPPERS:
            break
        arg_opts, i = _WRAPPERS[w], 1
        while i < len(tokens):
            t = tokens[i]
            if t in arg_opts:
                i += 2
            elif t.startswith("-") and len(t) > 1 or (w == "env" and _ASSIGN.match(t)):
                i += 1
            else:
                break
        if w == "timeout" and i < len(tokens):
            i += 1  # the duration
        tokens = tokens[i:]
    return tokens


def _tokens(seg: str) -> list[str]:
    try:
        return shlex.split(seg)
    except ValueError:
        return seg.split()


def _canon_tokens(tokens: list[str]) -> list[str]:
    tokens = _merge_flags(_strip_wrappers(tokens))
    return [_canon_path(t) if _is_pathlike(t) else t for t in tokens]


def _scan(cmd: str) -> tuple[list[list[str]], list[str]]:
    """Split a shell command line. Returns (pipelines, inner): each pipeline is a list of segments (split on |, |&),
    pipelines are split on ; && || & newline ( ); inner holds the text of $(...) and backticks, which run too."""
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
        elif c in ";\n()":
            end_pipeline()
        elif c == "&":
            if i > 0 and cmd[i - 1] in "<>" or cmd[i + 1 : i + 2] == ">":  # 2>&1, &>file
                buf.append(c)
            else:
                end_pipeline()
                if cmd[i + 1 : i + 2] == "&":
                    i += 1
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


def _nested_scripts(tokens: list[str]) -> list[str]:
    """The script text a command hands to a shell: `bash -c '...'`, `eval ...`."""
    if not tokens:
        return []
    if tokens[0] == "eval":
        return [" ".join(tokens[1:])]
    if tokens[0] in _SHELL_NAMES:
        for i, t in enumerate(tokens[1:], 1):
            if _FLAGS.match(t) and "c" in t[1:]:
                return [tokens[i + 1]] if i + 1 < len(tokens) else []
            if not t.startswith("-"):
                break
    return []


def _candidates(cmd: str, depth: int = 0) -> tuple[list[str], list[list[str]]]:
    """Every command string to test, and the token lists behind them. Each segment appears raw (whitespace
    normalised) and canonical; each pipeline also appears as one string, segments joined with ' | '."""
    if depth > _MAX_DEPTH:
        raise ValueError("command nests too deeply to check")
    pipelines, inner = _scan(cmd)
    cands: list[str] = []
    toks: list[list[str]] = []
    nested = list(inner)
    for segs in pipelines:
        canon_segs = []
        for seg in segs:
            raw = _tokens(seg)
            cands.append(" ".join(seg.split()))
            ct = _canon_tokens(raw)
            toks.append(ct)
            canon_segs.append(" ".join(ct))
            nested += _nested_scripts(_strip_wrappers(raw))
        cands += canon_segs
        if len(canon_segs) > 1:
            cands.append(" | ".join(canon_segs))
    for script in nested:
        c, t = _candidates(script, depth + 1)
        cands += c
        toks += t
    return cands, toks


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


def is_denied_command(policy: Policy, command: str) -> str | None:
    """None if the command may run, else the reason. Every shell segment is checked (split on ; && || | & newline,
    $(...), backticks, `bash -c`), after stripping sudo/env/timeout wrappers and normalising flags and paths. Also
    denies a segment that names a deny_read path. Raises on input it cannot judge: callers treat that as a deny."""
    if not isinstance(command, str):
        raise TypeError("command must be text")
    cands, toks = _candidates(command)
    for pat in policy.gate.deny_commands:
        rx = _glob_regex(_canon_pattern(pat))
        if any(rx.match(c) for c in cands):
            return f"command matches deny_commands rule {pat!r}"
    for t in (x for ts in toks for x in ts):
        for part in (t, t.split("=", 1)[-1]):
            if _is_pathlike(part) and (rule := _read_rule(policy, part)):
                return f"command names a path denied by deny_read rule {rule!r}"
    return None


def _path_regex(pattern: str) -> re.Pattern:
    """gitignore-style path glob: `**` crosses folders, `*` stays in one, a bare name matches at any depth."""
    pattern = _expand(pattern)
    if not pattern.startswith("/"):
        pattern = "**/" + pattern.removeprefix("./")
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z", re.DOTALL)


def _expand(path: str) -> str:
    home = _home().rstrip("/")
    for pre in ("${HOME}", "$HOME", "~"):
        if path == pre or path.startswith(pre + "/"):
            return home + path[len(pre) :]
    return path


def _read_rule(policy: Policy, path: str) -> str | None:
    p = _expand(path)
    forms = {posixpath.normpath(p) if p.startswith("/") else p}
    if p.startswith("/") and "*" not in p:
        forms.add(os.path.realpath(p))  # a symlink into a denied folder is still that folder
    elif not p.startswith("/") and "*" not in p:
        forms.add(os.path.realpath(p))
        forms.add(os.path.abspath(p))
    for rule in policy.gate.deny_read:
        rx = _path_regex(rule)
        if any(rx.match(f) for f in forms):
            return rule
    return None


def is_denied_read(policy: Policy, path: str) -> str | None:
    """None if the path may be read, else the reason."""
    if not isinstance(path, str) or not path:
        raise TypeError("path must be non-empty text")
    rule = _read_rule(policy, path)
    return f"path matches deny_read rule {rule!r}" if rule else None


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
