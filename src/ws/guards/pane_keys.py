"""pane-keys: a session does not type a slash command into a tmux pane, so no dialog can be opened behind its back.

Why. A slash command typed into a Claude Code pane runs. Some of them open a modal dialog (`/remote-control` on a session
that is already connected shows a status dialog, `/config` and `/model` show pickers) that holds every later key and every
queued message until someone presses one. A session that typed one into its own pane, or another's, has ended its turn by
then and nothing presses it, so the pane can sit unseen for hours. The cause was a session that typed `/rename` and
`/remote-control` to name a session that had been started without a name; `ws dispatch` now starts it named, so nobody has
to type either.

The rule. `tmux send-keys` aimed at the live tmux server, or at one that can't be known, is refused (an approvable deny)
when what it types has a line that starts with `/` once key names are decoded. The same goes for text put in a tmux buffer
(`set-buffer`, `load-buffer`) and pasted (`paste-buffer`). Leading blanks don't hide it. The reason tells the model what to
do instead: start a session with `--name X --remote-control X`, and message another session with `ws msg send` or the
runner's own cross-session messaging, never as typed text.

What is read.
  * `send-keys` text. Its options come first (`-t`, `-N` and `-c` take a value; `-l` is literal text, where `Enter` is just
    the word; `-H` is hex bytes; `-X` and `-M` type nothing). Without `-l` a word that is a key name is decoded, in any case as tmux reads it (`enter`, `c-M` and `^M` too; `Enter`,
    `C-m`, `C-j` and `KPEnter` end a line, `Space` and `Tab` are blanks, `KP/` is a slash, `0x2f` is a code point) and any
    other word is the text it spells, so `'/re' 'name'` is one line. Text split across words and across `;`-joined
    commands is put together. `BSpace`, `C-u` and `C-w` are applied as well as ignored, and both readings are judged. A key
    that moves the cursor (`C-a`, `Home`, the arrows) beside a `/` in the text is refused, since the text may land in
    front of what is there.
  * Control mode. `tmux -C` and `-CC` read tmux commands from their input, which this guard can't see (a pipe, a here
    document, a file), and `send-keys` typed there is the same thing. So on the live server, or one that can't be known,
    control mode is refused outright; it has no use in a session's shell, where nothing is attached to its input.
  * A word the shell has not settled (`$NAME` that is unset, a command substitution's result) at the start of a line could
    be anything, so it is refused as well: the reason says so.
  * `pipe-pane -I` (alone or in a cluster, `-IO`) feeds a command's output to the pane as typed keys, which this guard
    can't see, so it is refused on the live server, or one that can't be known, like control mode. `pipe-pane -O` (the
    default) only reads the pane and is left alone.
  * Buffers. A `set-buffer` or `load-buffer` of a plain file (or a `printf`/`echo` piped into `load-buffer -`) in the same
    command, then a `paste-buffer`, is judged on the text the buffer holds. A `paste-buffer` of a buffer this command did
    not fill is judged on what `show-buffer` says the live server holds. Text that can't be read (a heredoc, a file made
    by an earlier step of the same line, a server that doesn't answer) is allowed: this guard reads text, it doesn't
    guess.
  * Everything pane-close reads: a shell's `-c` text, `eval`, `$(...)`, a pipe into a shell (refused when tmux is in the
    line), and the text that `new-window`, `run-shell`, `if-shell`, `bind-key`, `source-file` and the like carry. A
    `send-keys` found in any of them is judged as a command of its own, on a server that can't be known.

Which server. As for pane-close (`live_socket` in `[guards.pane-keys]`, a path or a bare name under the tmux folder; default
this user's default socket): one plain command aimed at another socket is a test server and is left alone; anything that
follows another command, goes through a wrapper or a shell's text, or sits in a loop is taken to be aimed at the live one.

It does not stop a person typing in their own terminal, and it is not a wall: a determined session can build a slash
command some way this check does not read (a file made by the same line, a script). It stops the honest habit, tells the
model the right way, and `ws watch panes` is the net under it. Inert without tmux. It only reads: it never sends a key.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

from ws import shellwords
from ws.guards import Finding, pane_close
from ws.guards.pane_close import Config, Invocation

NAME = "pane-keys"
EVENTS = ("pre",)
KINDS = ("command",)
PRIORITY = 10  # protects the machine

QUERY_TIMEOUT_S = 3
_MAX_BUFFER = 65536  # bytes of a buffer file that are read
_TARGETS = ("send-keys", "set-buffer", "load-buffer", "paste-buffer", "pipe-pane")
_ALIASES = {
    "send": "send-keys",
    "setb": "set-buffer",
    "loadb": "load-buffer",
    "pasteb": "paste-buffer",
    "pipep": "pipe-pane",
}
_VALUE_OPTS = {
    "send-keys": "tNc",
    "set-buffer": "bnt",  # -t is a client
    "load-buffer": "bt",
    "paste-buffer": "bst",
    "pipe-pane": "t",
}
# tmux reads a key name without regard to case (`enter`, `KP/`, `c-m` and `C-M` are the keys `Enter`, `kp/` and `C-m`), and
# `^M` is `C-m`, so every table below is in lower case and a word is lowered before it is looked up
_ENTER = frozenset({"enter", "c-m", "c-j", "kpenter"})
_BLANKS = {"space": " ", "tab": "\t", "c-i": "\t"}
_KEYPAD = {"kp/": "/", "kp*": "*", "kp-": "-", "kp+": "+", "kp.": ".", **{f"kp{n}": str(n) for n in range(10)}}
_ERASE = frozenset({"bspace", "c-h"})
_MOVERS = frozenset(
    {"c-a", "c-e", "c-b", "c-f", "c-p", "c-n", "home", "end", "left", "right", "up", "down", "m-b", "m-f", "c-k"}
)
_QUIET = frozenset({"escape", "pageup", "pagedown", "ic", "dc", "btab", "npage", "ppage"})  # keys that type nothing
_HEX_KEY = re.compile(r"^0x([0-9a-f]{1,6})$")
_KEY_LIKE = re.compile(r"^(?:[cms]-)+\S+$")  # c-x, m-x, c-m-x: a key, whatever it is, and not text
_DYNAMIC = ("$", "`")  # what the shell would still have changed: the text isn't the text it appears to be

_REASON = (
    "typing a slash command into a tmux pane is refused: {why}. A slash command can open a dialog that holds every later "
    "key and message until someone presses one, and no one may be looking. Start sessions with `--name X "
    "--remote-control X` (`ws dispatch` does), and send a message to another session with `ws msg send` or the runner's "
    "own cross-session messaging, never as typed text"
)


def config(table: dict, where: str) -> Config:
    return pane_close.config(table, where)


# --- what a command types --------------------------------------------------------------------------------------------


def _cmd(sub: str | None) -> str | None:
    """Which of the commands this guard reads a tmux command word names (an alias or a prefix tmux would take), or None."""
    if not sub:
        return None
    full = _ALIASES.get(sub, sub)
    if len(full) < 2:
        return None
    return next((n for n in _TARGETS if n.startswith(full)), None)


def _options(args: list[str], name: str) -> tuple[dict[str, str], set[str], list[str]]:
    """(values of the options that take one, the flags that don't, the words left) of one tmux command's own words. A
    cluster (`-lt %1`) is read as getopt reads it; `--` ends the options."""
    takes = _VALUE_OPTS[name]
    values: dict[str, str] = {}
    flags: set[str] = set()
    i = 0
    while i < len(args) and args[i].startswith("-") and len(args[i]) > 1:
        word = args[i]
        i += 1
        if word == "--":
            break
        for j, ch in enumerate(word[1:], start=2):
            if ch in takes:
                if j < len(word):
                    values[ch] = word[j:]
                elif i < len(args):
                    values[ch] = args[i]
                    i += 1
                break
            flags.add(ch)
    return values, flags, args[i:]


def _unkey(word: str) -> tuple[str, str | None]:
    """(text, edit) one `send-keys` word stands for when it is not literal: the text it types, and "erase", "kill-line",
    "kill-word", "move" or None for what it does to the line being typed."""
    key = word.lower()
    if len(key) == 2 and key[0] == "^":
        key = f"c-{key[1]}"
    if key in _ENTER:
        return "\n", None
    if key in _BLANKS:
        return _BLANKS[key], None
    if key in _KEYPAD:
        return _KEYPAD[key], None
    if key == "m-/":  # Escape, then `/`: the slash is typed
        return "/", None
    if key in _ERASE:
        return "", "erase"
    if key == "c-u":
        return "", "kill-line"
    if key == "c-w":
        return "", "kill-word"
    if key in _MOVERS:
        return "", "move"
    m = _HEX_KEY.match(key)
    if m:
        try:
            return chr(int(m.group(1), 16)), None
        except (ValueError, OverflowError):
            return "", None
    if _KEY_LIKE.match(key) or key in _QUIET or re.fullmatch(r"f[0-9]{1,2}", key):
        return "", None
    return word, None


def _typed(args: list[str]) -> list[str] | None:
    """What `send-keys`' words type, as text, in each reading worth judging (the keys as typed, and with the line-editing
    keys applied), or None when it types no text (`-X`, `-M`, `-R`, no words). A cursor key beside a `/` adds a reading
    that starts with it."""
    _, flags, words = _options(args, "send-keys")
    if flags & {"X", "M", "R"} or not words:
        return None
    if "H" in flags:
        raw = ""
        for w in words:
            try:
                raw += chr(int(w, 16))
            except (ValueError, OverflowError):
                pass
        return [raw]
    if "l" in flags:
        return ["".join(words)]
    plain, edited, moved = "", "", False
    for w in words:
        text, edit = _unkey(w)
        plain += text
        if edit == "erase":
            edited = edited[:-1] if edited and edited[-1] != "\n" else edited
        elif edit == "kill-line":
            edited = edited[: edited.rfind("\n") + 1]
        elif edit == "kill-word":
            head, nl, line = edited.rpartition("\n")
            edited = head + nl + re.sub(r"\S*\s*$", "", line)
        else:
            edited += text
        moved = moved or edit == "move"
    readings = [plain, edited]
    if moved and "/" in plain:
        readings.append("/" + plain)  # typed in front of what is there: the line may start with it
    return readings


def _slash_line(text: str) -> str | None:
    """The first line of `text` that is, or may be, a slash command: it starts with `/` (blanks first don't matter), or
    with a word the shell never settled. None when there is none."""
    for line in re.split(r"[\r\n]", text):
        head = line.lstrip()
        if head.startswith("/") or head.startswith(_DYNAMIC):
            return line.strip()
    return None


def _why(line: str) -> str:
    if line.startswith(_DYNAMIC):
        return f"the text it types starts with a word that is not settled ({line[:40]!r}), so it may be a slash command"
    return f"a line it types starts with a slash ({line[:60]!r})"


# --- buffers ---------------------------------------------------------------------------------------------------------


def _show_buffer(sock: Path, name: str, env: dict) -> str | None:
    """What the buffer `name` ("" for the newest) of the server at `sock` holds, or None when it can't be read. Read-only;
    `TMUX` is unset so the answer is the command's own server's."""
    clean = {k: v for k, v in env.items() if k not in ("TMUX", "TMUX_PANE")}
    argv = ["tmux", "-S", str(sock), "show-buffer", *(["-b", name] if name else [])]
    try:
        done = subprocess.run(
            argv, capture_output=True, text=True, timeout=QUERY_TIMEOUT_S, env=clean, check=False, errors="replace"
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _read_file(given: str, cwd: str) -> str | None:
    """The text of a plain file a buffer is loaded from, or None when it isn't one that can be read."""
    path = os.path.expanduser(given)
    path = path if os.path.isabs(path) else os.path.join(cwd, path)
    try:
        if not os.path.isfile(path):
            return None
        with open(path, "rb") as f:
            return f.read(_MAX_BUFFER).decode("utf-8", errors="replace")
    except OSError:
        return None


def _echoed(words: list[str]) -> str | None:
    """The text an `echo` or `printf` prints, for a pipe into `load-buffer -`; None for any other program."""
    prog = os.path.basename(words[0]) if words else ""
    args = [w for w in words[1:]]
    if prog == "echo":
        while args and re.fullmatch(r"-[neE]+", args[0]):
            args.pop(0)
        return " ".join(args)
    if prog == "printf" and args:
        fmt, rest = args[0], iter(args[1:])
        text = fmt.replace("\\n", "\n").replace("\\t", "\t").replace("\\\\", "\\")
        return re.sub(r"%[sb]", lambda _m: next(rest, ""), text)
    return None


def _feeds(command: str, env: dict) -> list[str]:
    """The text each `echo` or `printf` at the top of `command` hands the next program in its pipe, in order."""
    try:
        top, _ = shellwords.scan(command, heredocs=True)
    except shellwords.ShellError:
        return []
    out: list[str] = []
    for pipeline in top:
        for i, seg in enumerate(pipeline[:-1]):
            text = _echoed(shellwords.lex(seg, env).words)
            nxt = shellwords.lex(pipeline[i + 1], env).words
            if text is not None and nxt and os.path.basename(nxt[0]) == "tmux":
                out.append(text)
    return out


class _Buffers:
    """The text of the tmux buffers this one command line fills, by name ("" is the newest, which a command with no `-b`
    uses), so a paste later in the line is judged on what it pastes."""

    def __init__(self, command: str, env: dict, cwd: str):
        self.text: dict[str, str] = {}
        self.feeds = _feeds(command, env)
        self.cwd = cwd

    def put(self, name: str, text: str, append: bool = False) -> None:
        text = self.text.get(name, "") + text if append else text
        self.text[name] = text
        self.text[""] = text  # the newest

    def set(self, args: list[str]) -> None:
        values, flags, words = _options(args, "set-buffer")
        if words and "n" not in values:
            self.put(values.get("b", ""), words[-1], "a" in flags)

    def load(self, args: list[str]) -> None:
        values, _, words = _options(args, "load-buffer")
        if not words:
            return
        text = (self.feeds.pop(0) if self.feeds else None) if words[-1] == "-" else _read_file(words[-1], self.cwd)
        if text is not None:
            self.put(values.get("b", ""), text)

    def pasted(self, args: list[str]) -> str | None:
        """The text a `paste-buffer` would put in, when this line filled the buffer; None when it didn't."""
        values, _, _ = _options(args, "paste-buffer")
        return self.text.get(values.get("b", ""))


# --- the decision ----------------------------------------------------------------------------------------------------


def _control_mode(words: list[str]) -> bool:
    """True when tmux's own leading options (before the command word) hold `-C` or `-CC`, as a word of its own or in a
    cluster (`-uC`). The values of `-L`, `-S`, `-c`, `-f` and `-T` are skipped."""
    i = 0
    while i < len(words) and words[i].startswith("-") and words[i] != "--":
        if words[i] in ("-L", "-S", "-c", "-f", "-T"):
            i += 2
            continue
        if re.fullmatch(r"-[A-Za-z0-9]*C[A-Za-z0-9]*", words[i]):
            return True
        i += 1
    return False


def _refuse(why: str) -> Finding:
    return Finding("deny", _REASON.format(why=why), approvable=True)


_GO_ON = object()
_MAX_QUEUE = 500  # tmux commands, counting the ones found in text, that one command line may hold


def _judge_one(inv: Invocation, queue: list[Invocation], live: Path, listy: bool, env: dict, cwd: str, bufs: _Buffers):
    """One tmux command: a Finding to refuse, or _GO_ON. Text it runs, and the commands after a `;`, go on `queue`."""
    if inv.opaque:
        return _refuse("a command here can't be read, and tmux is in the line")
    split = pane_close._split(inv.words)
    if split is None:  # a global option this check doesn't read: ours only if it holds a command we read
        if any(_cmd(w) for w in inv.words if not w.startswith("-")) or any(c in w for w in inv.words for c in _DYNAMIC):
            return _refuse("a tmux option here isn't one this check reads")
        return _GO_ON
    flags, sub, rest = split
    own = "-S" in flags or "-L" in flags
    if inv.sock is not None and not own:
        sock: Path | None = inv.sock
    elif inv.wrapped or inv.nested or listy:
        sock = None  # not known: an earlier command, a wrapper or a shell may have changed what the flags mean
    else:
        sock = pane_close._socket(flags, env, cwd)
    on_live = sock is None or pane_close._same(sock, live)
    if ";" in rest:  # a command list: what follows the first `;` is a command of its own on the same server
        at = rest.index(";")
        tail = rest[at + 1 :]
        rest = rest[:at]
        if tail and inv.depth < pane_close._MAX_DEPTH:
            queue.append(Invocation(tail, inv.wrapped, True, via="a command list", sock=sock, depth=inv.depth + 1))
    try:
        queue += pane_close._children(inv, flags, sub, rest, sock, env, cwd)
    except pane_close._Unsure as e:
        if on_live:  # on a test server a file that can't be read is not our concern
            return _refuse(e.reason)
    if on_live and _control_mode(inv.words):
        return _refuse("tmux control mode (`-C`) takes its commands from its input, which can't be read here")
    if not sub or not on_live:
        return _GO_ON
    if any(c in sub for c in _DYNAMIC):
        return _refuse(f"the tmux command word is not plain text ({sub!r}), so what it does can't be checked")
    name = _cmd(sub)
    if name == "send-keys":
        readings = _typed(rest)
        for text in readings or []:
            line = _slash_line(text)
            if line is not None:
                return _refuse(_why(line))
    elif name == "pipe-pane":
        _, pflags, _ = _options(rest, "pipe-pane")
        if "I" in pflags:
            return _refuse("`pipe-pane -I` types its command's output into the pane, which can't be read here")
    elif name == "set-buffer":
        bufs.set(rest)
    elif name == "load-buffer":
        bufs.load(rest)
    elif name == "paste-buffer":
        values, _, _ = _options(rest, "paste-buffer")
        text = bufs.pasted(rest)
        if text is None:
            text = _show_buffer(sock if sock is not None else live, values.get("b", ""), env)
        line = _slash_line(text) if text else None
        if line is not None:
            return _refuse(f"the buffer it pastes has a line that starts with a slash ({line[:60]!r})")
    return _GO_ON


def judge(call, cfg, ctx) -> Finding | None:
    command = call.value
    if shutil.which("tmux") is None:
        return None
    env = dict(os.environ)
    cwd = ctx.cwd if ctx.cwd and os.path.isabs(ctx.cwd) else os.getcwd()
    try:
        found, listy, stdin_shell = pane_close._invocations(command, env)
        if stdin_shell and pane_close._mentions_tmux(command):
            return _refuse("a shell here reads its commands from input, and tmux is in the line")
        live = pane_close._live_socket(cfg, env)
        bufs = _Buffers(command, env, cwd)
        queue = list(found)
        i = 0
        while i < len(queue):
            if i >= _MAX_QUEUE:
                return _refuse("it holds too many tmux commands to check")
            got = _judge_one(queue[i], queue, live, listy, env, cwd, bufs)
            i += 1
            if got is not _GO_ON:
                return got
    except shellwords.ShellError:
        return (
            _refuse("the command can't be read well enough to check it") if pane_close._mentions_tmux(command) else None
        )
    return None
