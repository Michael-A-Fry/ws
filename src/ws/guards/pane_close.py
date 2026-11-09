"""pane-close: a session closes a tmux window or pane only when the registry says nobody is working in it.

The rule. `tmux kill-window` and `tmux kill-pane` on the live tmux server are allowed only as one plain command with a
target given by its stable id (`@N` for a window, `%N` for a pane), and only if the session registry says what is in the
target:

  * the target's sessions are all idle (`presence.effective_state`: a busy report that has gone quiet is not idle) and the
    caller is the lead, or
  * the target is the caller's own pane (the registry's `pane` for it, and the caller's runner running under that pane),
    nothing else is in it, and the caller's hand-over has been delivered (a `done` or `stop-and-hand-over:done` message
    whose receiver has had it delivered).

A session that is not the lead may close only itself, and only after that. A pane with no registered session is not
closed from here. Refused whatever the registry says: `-a`, a target by index or name, `kill-session` (a `$N`) and
`kill-server`, a close inside a loop, a command list, a pipe or a command substitution, a command that sets its own
environment or goes through a wrapper (`xargs`, `sudo`, `env`), a tmux command word or target that is not plain text (a
variable, a quote-split or glob word), a registry that can't be read, a target tmux doesn't have, and a registry that
changed while the check ran.

What is read. The command is read first, whatever its words say; one that does not name `tmux` costs nothing. Text a
shell runs is read as a command of its own: a shell's `-c` text, `eval`, an `env -S` string, a `$(...)`, and what a pipe
hands a shell (`echo ... | sh`, refused when tmux is in the line). A tmux command that runs text is read the same way:
`new-window`, `split-window`, `new-session`, `respawn-pane`, `display-popup`, `pipe-pane`, `run-shell`, `if-shell` and
`send-keys` hold shell text; `bind-key`, `set-hook`, `confirm-before` and `command-prompt` hold a tmux command; a
`#(...)` in any argument is a shell command; `source-file` is read as the file it names (a file that can't be read, or
`-`, is refused). A kill found in any of them is refused. A script file that a shell is told to run is not read.

Who is the lead, and who is calling. Both come from the registry and the process tree, never from what a session can
write: the caller is `session.resolve_caller(session_id)` (the hook runs under that session's stamped runner; a forged id
is nobody), and the role is the identity folder's (`ws.session`), which a session cannot write. A window's name, a tmux
user option, `TMUX_PANE` or any other environment value, and the payload's session id on its own, decide nothing.

Which server. A command is judged against the tmux server it will talk to: `-S path`, else `-L name` (under
`TMUX_TMPDIR`, else /tmp), else the socket in `$TMUX`, else the default. That is known only for one plain command: a
tmux call that follows another command (a `cd`, `unset` or `export` may have changed what its flags and `$TMUX` mean), or
is reached through a wrapper, a variable assignment or a shell's text, is taken to be aimed at the live server, so a kill
in it is refused whatever socket it names. A relative `-S` path is read from the session's working folder. The live
socket is the `live_socket` key of `[guards.pane-close]` (a path, or a bare name under the tmux folder; default: this
user's default socket). A plain command aimed at any other socket is a test server and is none of this guard's
business. Every read-only query (`list-panes
-a`) goes to the command's own socket with `TMUX` unset, so a pane id (`%3` exists on every server) is looked up on the
server it will be killed on. Which sessions are in a pane is found from the registry's `pane` and by walking `/proc`
from the pane's process down (a runner started under a shell in the pane has no `TMUX_PANE` to be recorded by). The
registry is read before the query and after it, and a record whose revision moved in between refuses the close.

Inert without tmux: if `tmux` isn't installed, the guard says nothing. The tmux command is only read: this guard never
runs a kill.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ws import msg, session, shellwords
from ws.guards import Finding, known_keys, presence
from ws.output import WsError

NAME = "pane-close"
EVENTS = ("pre",)
KINDS = ("command",)
PRIORITY = 10  # protects the machine

QUERY_TIMEOUT_S = 3
_LOOP_WORDS = frozenset({"for", "while", "until", "do", "done", "select", "then", "else", "elif", "fi", "if", "case"})
_KILLS = ("kill-pane", "kill-window", "kill-server", "kill-session")
_ALIASES = {"killp": "kill-pane", "killw": "kill-window"}
# tmux commands that hold text to run, by the full name (tmux takes any unambiguous prefix, and these short names)
_SHELL_TEXT = ("new-window", "split-window", "new-session", "respawn-pane", "respawn-window", "display-popup")
_SHELL_TEXT += ("pipe-pane", "run-shell", "if-shell", "send-keys")
_TMUX_TEXT = ("bind-key", "set-hook", "if-shell", "confirm-before", "command-prompt", "display-menu")
_FILE_TEXT = ("source-file",)
_SHORT = {
    "neww": "new-window",
    "splitw": "split-window",
    "new": "new-session",
    "respawnp": "respawn-pane",
    "respawnw": "respawn-window",
    "popup": "display-popup",
    "pipep": "pipe-pane",
    "run": "run-shell",
    "if": "if-shell",
    "send": "send-keys",
    "bind": "bind-key",
    "confirm": "confirm-before",
    "menu": "display-menu",
    "source": "source-file",
}
_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "fish", "ksh", "mksh", "ash", "csh", "tcsh", "busybox"})
# programs that print or read their words and run none of them: a line `echo tmux kill-server` is text, not a call
_INERT = frozenset(
    {
        "echo",
        "printf",
        "cat",
        "grep",
        "egrep",
        "fgrep",
        "rg",
        "ag",
        "less",
        "more",
        "head",
        "tail",
        "wc",
        "sort",
        "uniq",
    }
    | {"cut", "tr", "jq", "ls", "stat", "file", "diff", "cmp", "test", "[", "true", "false", "which", "whereis"}
    | {"type", "pgrep", "pkill", "ps", "git", "tee"}
)
_PLAIN_FLAGS = re.compile(r"^-[u2vqCND]+$")  # tmux's global flags that take no value (-CC is two of them)
_VALUE_FLAGS = ("-L", "-S", "-c", "-f", "-T")
_MAX_DEPTH = 6  # shell texts inside shell texts
_MAX_FILE = 262144  # bytes of a sourced tmux file that are read
_OPAQUE = re.compile(r"[$`*?\[\]{}~\\]")  # a word the shell or tmux would still change: not the text it appears to be
_FORMAT = re.compile(r"#\(([^)]*)\)")  # a tmux format that runs a shell command
_TARGET = re.compile(r"^[@%][0-9]{1,9}$")
_PANE_ROW = re.compile(r"^(%[0-9]+) (@[0-9]+) ([0-9]+)$")
_MAX_PROCS = 20000


@dataclass(frozen=True)
class Config:
    live_socket: str = ""  # a path, or a bare name under the tmux folder; "" is this user's default socket


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("live_socket",))
    live = table.get("live_socket", "")
    if not isinstance(live, str) or "\0" in live:
        from ws.policy import PolicyError

        raise PolicyError(f"{where}: live_socket must be text (a socket path, or a name under the tmux folder)")
    return Config(live)


# --- which server ----------------------------------------------------------------------------------------------------


def _tmux_dir(env: dict) -> Path:
    return Path(env.get("TMUX_TMPDIR") or "/tmp") / f"tmux-{os.getuid()}"


def _live_socket(cfg: Config | None, env: dict) -> Path:
    live = cfg.live_socket if cfg else ""
    if not live:
        return _tmux_dir(env) / "default"
    return Path(live) if "/" in live else _tmux_dir(env) / live


def _socket(flags: dict, env: dict, cwd: str = "") -> Path:
    """The socket a tmux command with these global flags talks to: -S (a relative path is under `cwd`, the session's
    folder), else -L, else $TMUX's, else the default."""
    if "-S" in flags:
        path = Path(flags["-S"])
        return path if path.is_absolute() or not cwd else Path(cwd) / path
    if "-L" in flags:
        return _tmux_dir(env) / flags["-L"]
    if env.get("TMUX"):
        return Path(env["TMUX"].split(",")[0])
    return _tmux_dir(env) / "default"


def _same(a: Path, b: Path) -> bool:
    return os.path.realpath(a) == os.path.realpath(b)


# --- reading the command ---------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Invocation:
    words: list[str]  # the tmux words after `tmux`
    wrapped: bool  # reached through a wrapper or with variables set in front
    nested: bool  # inside a command substitution, a shell's text or another tmux command's text
    via: str = ""  # what runs it, when it is not a command of its own ("new-window", "a shell's -c text")
    sock: Path | None = None  # the socket it inherits from the tmux command that runs it, when that is known
    opaque: bool = False  # a program word that can't be read, in a line that names tmux
    depth: int = 0


def _mentions_tmux(text: str) -> bool:
    """True when `text` spells tmux once its quotes, backslashes and braces are taken out."""
    return "tmux" in re.sub(r"[\\'\"${}`]", "", text.lower())


def _shell_c_text(words: list[str]) -> str | None:
    """The `-c TEXT` of a shell command (`bash -c`, `sh -lc`), or None."""
    i = 1
    while i < len(words):
        w = words[i]
        if w == "--" or not w.startswith("-") or w == "-":
            return None  # a script file or the end of the options
        if "c" in w[1:] and not w.startswith("--"):
            return words[i + 1] if i + 1 < len(words) else ""
        i += 2 if w in ("-o", "-O", "+O", "--rcfile", "--init-file") else 1
    return None


def _reads_stdin(words: list[str]) -> bool:
    """True for a shell started with no `-c` text and no script to run, which reads its commands from its input."""
    if _shell_c_text(words) is not None:
        return False
    i = 1
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        i += 2 if words[i] in ("-o", "-O", "+O", "--rcfile", "--init-file") else 1
    if i < len(words) and words[i] == "--":
        i += 1
    return i >= len(words)


def _invocations(
    command: str, env: dict, *, depth: int = 0, via: str = "", sock: Path | None = None
) -> tuple[list[Invocation], bool, bool]:
    """Every tmux command in `command`: (them, whether the top level is a loop or a list rather than one plain command,
    whether a shell in it reads its commands from input). Text a shell runs is read in turn."""
    if depth > _MAX_DEPTH:
        raise shellwords.ShellError("too_deep", "the command nests shell text too deeply to check")
    top, inner = shellwords.scan(command, heredocs=True)
    nested = depth > 0
    found: list[Invocation] = []
    texts: list[tuple[str, str]] = [(t, "a command substitution") for t in inner]
    stdin_shell = False

    def add(words: list[str], wrapped: bool, **kw) -> None:
        found.append(Invocation(words, wrapped, nested, via=via, sock=sock, depth=depth, **kw))

    for seg in (seg for pipeline in top for seg in pipeline):
        lexed = shellwords.lex(seg, env)
        assigned: list[str] = []
        scripts: list[str] = []
        words, wrappers = shellwords.strip_wrappers(lexed.words, assigned, lexenv=env, scripts=scripts)
        texts += [(t, "a wrapper's shell text") for t in scripts]
        if not words:
            continue
        prog = os.path.basename(words[0]).lstrip("-")
        if prog == "tmux":
            add(words[1:], bool(wrappers or assigned))
        elif prog in _SHELLS:
            text = _shell_c_text(words)
            if text is not None:
                texts.append((text, "a shell's -c text"))
            elif _reads_stdin(words):
                stdin_shell = True
        elif prog == "eval":
            texts.append((" ".join(words[1:]), "eval"))
        elif ("$" in words[0] or "`" in words[0]) and _mentions_tmux(command):
            add(words, True, opaque=True)  # the program is a variable, and tmux is named in the line
        elif prog not in _INERT or (prog == "git" and "-c" in words):  # `git -c alias.x=!...` runs a command
            for k, w in enumerate(words):
                if os.path.basename(w) == "tmux" and k:
                    add(words[k + 1 :], True)  # tmux somewhere in a command that isn't it
                elif k and "tmux" in w and re.search(r"\s", w):  # a word that holds a command line (`find -exec`, awk)
                    for m in re.finditer(r"\btmux\s+([^\n;|&)'\"]*)", w):
                        add(m.group(1).split(), True)
    for text, how in texts:
        sub_found, _, sub_stdin = _invocations(text, env, depth=depth + 1, via=how, sock=sock)
        found += sub_found
        stdin_shell = stdin_shell or sub_stdin
    plain = len(top) == 1 and len(top[0]) == 1 and not inner
    looped = any(seg.split(None, 1)[0] in _LOOP_WORDS for pipeline in top for seg in pipeline if seg.split())
    return found, (not plain) or looped, stdin_shell


def _kill_name(word: str) -> str | None:
    """The kill command a tmux command word names (tmux takes aliases and unambiguous prefixes), "ambiguous" for a word
    that starts like one but isn't a single one, or None."""
    if word in _ALIASES:
        return _ALIASES[word]
    if not word.startswith("kill"):
        return None
    hits = [k for k in _KILLS if k.startswith(word)]
    return hits[0] if len(hits) == 1 else "ambiguous"


def _separated(words: list[str]) -> list[str]:
    """tmux's words with a `;` at the end of a word as a word of its own: tmux ends a command there, as at a bare `;`."""
    out: list[str] = []
    for w in words:
        if len(w) > 1 and w.endswith(";"):
            out += [w[:-1].removesuffix("\\"), ";"]
        else:
            out.append(w)
    return out


def _split(args: list[str]) -> tuple[dict, str | None, list[str]] | None:
    """(global flags, the sub-command word, the rest) of tmux's words, or None for a global flag we don't read."""
    flags: dict = {}
    i = 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        flag = args[i]
        if flag in _VALUE_FLAGS and i + 1 < len(args):
            flags[flag] = args[i + 1]
            i += 2
        elif _PLAIN_FLAGS.match(flag):
            i += 1
        else:
            return None
    if i < len(args) and args[i] == "--":  # tmux takes the word after `--` as the command
        i += 1
    words = _separated(args[i:])
    return flags, (words[0] if words else None), words[1:]


def _command_target(rest: list[str]) -> tuple[str | None, str | None]:
    """(target, the reason it can't be used) of `kill-window`/`kill-pane`'s own words: only `-t TARGET` is taken."""
    target = None
    i = 0
    while i < len(rest):
        w = rest[i]
        if w == "-t" and i + 1 < len(rest) and target is None:
            target, i = rest[i + 1], i + 2
        elif w.startswith("-t") and len(w) > 2 and target is None:
            target, i = w[2:], i + 1
        elif w == "-a" or (w.startswith("-") and "a" in w[1:] and not w.startswith("-t")):
            return None, "-a closes every other pane or window"
        else:
            return None, "only `-t TARGET` is taken"
    if target is None:
        return None, "a close needs `-t @N` or `-t %N`: the current pane is not a target"
    if not _TARGET.match(target):
        return None, f"the target must be a stable id (@N or %N), not an index or a name ({target!r})"
    return target, None


# --- reading the server and the registry ------------------------------------------------------------------------------


def _query(sock: Path, env: dict) -> list[tuple[str, str, int]]:
    """Every pane on the server at `sock` as (pane id, window id, pane pid). Read-only; `TMUX` is unset so the answer is
    the command's own server's, not the session's. A server that doesn't answer is a `WsError`."""
    clean = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")}
    try:
        out = subprocess.run(
            ["tmux", "-S", str(sock), "list-panes", "-a", "-F", "#{pane_id} #{window_id} #{pane_pid}"],
            capture_output=True,
            text=True,
            timeout=QUERY_TIMEOUT_S,
            env=clean,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WsError("unavailable", "tmux didn't answer", "check that the server is running") from e
    if out.returncode != 0:
        raise WsError("unavailable", "tmux didn't answer", "check that the server is running")
    rows = []
    for line in out.stdout.splitlines():
        m = _PANE_ROW.match(line.strip())
        if m:
            rows.append((m.group(1), m.group(2), int(m.group(3))))
    return rows


def _descendants(root: int) -> set[int] | None:
    """`root` and every process under it, from /proc; None where there is no /proc to walk."""
    if not Path("/proc/self/stat").exists():
        return None
    parents: dict[int, list[int]] = {}
    try:
        names = [n for n in os.listdir("/proc") if n.isdigit()][:_MAX_PROCS]
    except OSError:
        return None
    for n in names:
        try:
            data = (Path("/proc") / n / "stat").read_text(encoding="utf-8", errors="replace")
            ppid = int(data[data.rindex(")") + 2 :].split()[1])
        except (OSError, ValueError, IndexError):
            continue
        parents.setdefault(ppid, []).append(int(n))
    seen, todo = {root}, [root]
    while todo:
        for child in parents.get(todo.pop(), []):
            if child not in seen:
                seen.add(child)
                todo.append(child)
    return seen


def _in_pane(rec: session.Record, pane_id: str, below: set[int] | None) -> bool:
    """True when `rec` is working in the pane: its runner is under the pane's process (where /proc can say), or, where it
    can't, its recorded pane is this one."""
    if below is not None:
        return rec.runner_pid in below
    return rec.pane == pane_id


def _snapshot(records: list[session.Record]) -> dict[str, tuple]:
    return {r.session_id: (r.rev, r.state, r.state_ts) for r in records}


def _refuse(why: str) -> Finding:
    return Finding("deny", f"closing a tmux window or pane is refused: {why}")


# --- the decision -----------------------------------------------------------------------------------------------------


def _decide(kill: str, target: str, sock: Path, ctx, env: dict, now: float) -> Finding | None:
    """The registry rules for one close on the live server. None to allow, else the refusal."""
    caller = session.resolve_caller(ctx.session_id) if ctx.session_id else None
    if caller is None or caller.session_id != ctx.session_id:
        return _refuse("ws can't tell which session is calling (the hook is not under that session's runner)")
    try:
        before = session.records()
        panes = _query(sock, env)
        after = session.records()
    except WsError as e:
        return _refuse(f"{e.message}, so nothing can be checked")
    if _snapshot(before) != _snapshot(after):
        return _refuse("the session registry changed while it was checked; try again")
    window = {p: w for p, w, _ in panes}
    if target.startswith("%"):
        if target not in window:
            return _refuse(f"tmux has no pane {target} on that server")
        wanted = (
            [p for p in panes if p[1] == window[target]]
            if kill == "kill-window"
            else [p for p in panes if p[0] == target]
        )
    else:
        wanted = [p for p in panes if p[1] == target]
        if not wanted:
            return _refuse(f"tmux has no window {target} on that server")
    for pane_id, _, pid in wanted:
        below = _descendants(pid)
        inside = [r for r in after if _in_pane(r, pane_id, below)]
        if not inside:
            return _refuse(f"no registered session is in {pane_id}, so the registry can't say it is idle")
        if any(r.session_id == caller.session_id for r in inside):
            own = _own(caller, pane_id, inside)
            if own:
                return _refuse(own)
        elif caller.role != "lead":
            return _refuse(f"only the lead may close another session's pane ({pane_id})")
        else:
            for r in inside:
                if presence.effective_state(r, now) != "idle":
                    return _refuse(f"the session in {pane_id} is not idle by the registry (busy, or no recent report)")
    return None


def _own(caller: session.Record, pane_id: str, inside: list[session.Record]) -> str | None:
    """Why the caller may not close its own pane, or None when it may."""
    if len(inside) > 1:
        return f"another session is in {pane_id} too"
    try:
        delivered = msg.handover_delivered(caller.session_id)
    except WsError:
        delivered = False
    if not delivered:
        return "a session closes its own pane only after its hand-over (a done message) has been delivered"
    return None


class _Unsure(Exception):
    """Text a tmux command runs that can't be read well enough to check; the reason goes in the refusal."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def _is(sub: str, names: tuple[str, ...]) -> bool:
    """True when the tmux command word `sub` (a name, a short name or a prefix tmux would take) is one of `names`."""
    full = _SHORT.get(sub, sub)
    return bool(full) and any(n.startswith(full) for n in names)


def _tmux_commands(text: str, env: dict) -> list[list[str]]:
    """The commands in tmux command text (a config line, a `bind-key` argument): one list of words each. Lines that are
    comments are left out and a `;` ends a command."""
    out: list[list[str]] = []
    for line in re.sub(r"\\\n", " ", text).splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        command: list[str] = []
        for w in shellwords.lex(line, env).words:
            if w == ";" or w.endswith(";"):
                command += [w[:-1]] if w != ";" else []
                out.append(command)
                command = []
            else:
                command.append(w)
        out.append(command)
    return [c for c in out if c]


def _keys_text(args: list[str]) -> str:
    """What `send-keys` types, as text: key names spelt out (`Space`, `Enter`) and single keys run together. Its own
    options come first; everything after them is a key."""
    keys, i = [], 0
    while i < len(args) and args[i].startswith("-") and args[i] != "--":
        i += 2 if args[i] in ("-t", "-N", "-c") else 1
    if i < len(args) and args[i] == "--":
        i += 1
    for w in args[i:]:
        keys.append(" " if w == "Space" else "\n" if w in ("Enter", "C-m", "KPEnter") else w)
    return "".join(keys)


def _source_commands(rest: list[str], env: dict, cwd: str) -> list[tuple[str, list[list[str]]]]:
    """(path, commands) for each file `source-file` names. A file that does not exist runs nothing; one that can't be
    read, is too big or is `-` (standard input) is `_Unsure`."""
    import glob

    paths, i = [], 0
    while i < len(rest):
        if rest[i] == "-t":
            i += 2
            continue
        if not rest[i].startswith("-") or rest[i] == "-":
            paths.append(rest[i])
        i += 1
    out = []
    for given in paths:
        if given == "-":
            raise _Unsure("source-file reads its commands from standard input")
        path = os.path.expanduser(given)
        path = path if os.path.isabs(path) else os.path.join(cwd, path)
        for one in sorted(glob.glob(path))[:50] if re.search(r"[*?\[]", path) else [path]:
            if not os.path.lexists(one):
                continue
            if not os.path.isfile(one):  # a pipe could be waited on for ever, a folder has nothing to read
                raise _Unsure(f"source-file {given} is not a plain file")
            try:
                with open(one, "rb") as f:
                    data = f.read(_MAX_FILE + 1)
            except OSError as e:
                raise _Unsure(f"source-file {given} can't be read") from e
            if len(data) > _MAX_FILE:
                raise _Unsure(f"source-file {given} is too big to check")
            out.append((given, _tmux_commands(data.decode("utf-8", errors="replace"), env)))
    return out


def _children(
    inv: Invocation, flags: dict, sub: str | None, rest: list[str], sock: Path | None, env: dict, cwd: str
) -> list[Invocation]:
    """The tmux commands held in the text one tmux command runs: its `-c` text, a `#(...)` format, the shell text of
    `new-window` and the like, the tmux command text of `bind-key` and the like, and the file `source-file` names."""
    d = inv.depth + 1
    if d > _MAX_DEPTH:
        raise _Unsure("it nests commands too deeply to check")
    out: list[Invocation] = []

    def shell(text: str, how: str, hint: Path | None) -> None:
        found, _, stdin = _invocations(text, env, depth=d, via=how, sock=hint)
        if stdin and _mentions_tmux(text):
            raise _Unsure(f"a shell run by {how} reads its commands from input, and tmux is in the line")
        out.extend(found)

    def tmux_words(words: list[str], how: str) -> None:
        if words and not words[0].startswith("-"):
            out.append(Invocation(words, False, True, via=how, sock=sock, depth=d))

    if "-c" in flags:
        shell(flags["-c"], "tmux -c", None)  # the shell that runs it has its own environment, not this server's
    if not sub:
        return out
    how = f"tmux {sub}"
    for arg in rest:
        for m in _FORMAT.finditer(arg):
            shell(m.group(1), "a #() format", sock)
    if _is(sub, _SHELL_TEXT):
        for arg in [*rest, " ".join(rest)] + ([_keys_text(rest)] if _is(sub, ("send-keys",)) else []):
            if arg:
                shell(arg, how, sock)
    if _is(sub, _TMUX_TEXT):
        for k, arg in enumerate(rest):
            tmux_words(rest[k:], how)
            for words in _tmux_commands(arg, env):
                tmux_words(words, how)
    if _is(sub, _FILE_TEXT):
        for given, commands in _source_commands(rest, env, cwd):
            for words in commands:
                tmux_words(words, f"{how} {given}")
    return out


def _judge_one(inv: Invocation, queue: list[Invocation], live: Path, listy: bool, env: dict, cwd: str, ctx):
    """One tmux command: a Finding to refuse, None to allow the whole command, or _GO_ON when this one is not a close
    on the live server. Text it runs is added to `queue`."""
    if inv.opaque:
        return _refuse("a command here can't be read, and tmux is in the line")
    split = _split(inv.words)
    if split is None:  # an option this check doesn't read: ours only if a kill is in it
        if any("kill" in w or _OPAQUE.search(w) for w in inv.words):
            return _refuse("a tmux option here isn't one this check reads")
        return _GO_ON
    flags, sub, rest = split
    own = "-S" in flags or "-L" in flags
    if inv.sock is not None and not own:
        sock: Path | None = inv.sock  # a tmux command run by the server's own: the server's socket
    elif inv.wrapped or inv.nested or listy:
        sock = (
            None  # not known: an earlier command, a wrapper or a shell may have changed what the flags and $TMUX mean
        )
    else:
        sock = _socket(flags, env, cwd)
    on_live = sock is None or _same(sock, live)
    try:
        queue += _children(inv, flags, sub, rest, sock, env, cwd)
    except _Unsure as e:
        if on_live:  # on a test server a file that can't be read is not our concern
            return _refuse(e.reason)
    opaque = bool(sub) and bool(_OPAQUE.search(sub or ""))
    killed = _kill_name(sub) if sub and not opaque else None
    chained = ";" in rest and any(_kill_name(w) for w in rest)
    if (killed is None and not opaque and not chained) or not on_live:
        return _GO_ON
    if opaque:
        return _refuse(f"the tmux command word is not plain text ({sub!r}), so what it does can't be checked")
    if sock is None or inv.wrapped or inv.nested or listy or chained:
        by = f" (run by {inv.via})" if inv.via else ""
        return _refuse(f"it is inside a loop, a command list, a command substitution or a wrapper{by}")
    if killed not in ("kill-window", "kill-pane"):
        return _refuse("only kill-window and kill-pane by id are allowed here (not a session or the server)")
    target, why = _command_target(rest)
    if why or target is None:
        return _refuse(why or "no target")
    return _decide(killed, target, sock, ctx, env, ctx.now())


_GO_ON = object()
_MAX_QUEUE = 500  # tmux commands, counting the ones found in text, that one command line may hold


def judge(call, cfg, ctx) -> Finding | None:
    command = call.value
    if shutil.which("tmux") is None:
        return None
    env = dict(os.environ)
    cwd = ctx.cwd if ctx.cwd and os.path.isabs(ctx.cwd) else os.getcwd()
    try:
        found, listy, stdin_shell = _invocations(command, env)
        if stdin_shell and _mentions_tmux(command):
            return _refuse("a shell here reads its commands from input, and tmux is in the line")
        live = _live_socket(cfg, env)
        queue = list(found)
        i = 0
        while i < len(queue):
            if i >= _MAX_QUEUE:
                return _refuse("it holds too many tmux commands to check")
            got = _judge_one(queue[i], queue, live, listy, env, cwd, ctx)
            i += 1
            if got is not _GO_ON:
                return got
    except shellwords.ShellError:
        return _refuse("the command can't be read well enough to check it") if _mentions_tmux(command) else None
    return None
