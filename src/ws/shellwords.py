"""The shell command reader: one tokenizer that every policy check goes through.

A command line is read the way a shell reads it, so a path is recognised however it is spelled. In order:
  * `prep`, `join_lines` and `scan` split the text into pipelines of segments (on `;`, `&&`, `||`, `|`, `&`, new lines
    and parentheses), leave out comments and, when asked, here-document bodies, and collect the text of `$(...)`,
    backticks and `<(...)`, which run too (`inner`);
  * `lex` reads one segment into words and redirections. Brace expansion (comma lists and `{a..b}` sequences of any
    length) comes first and only reads braces that are not quoted; then `~`, `$HOME`, `${HOME}`, `$WS_HOME` and the
    variables the caller knows are expanded; then quotes are removed. The words are what the shell would run; `pieces`
    holds the places a program may read a path from inside one word (`f=@P`, `if=P`, `--opt=P`, `-oP`, `file://P`,
    each with `~` and `$HOME` expanded even where quoted, because the program reads the text, not the shell);
  * `strip_wrappers` takes `sudo`, `env`, `xargs`, `timeout` and the like off the front, `split_args` and
    `option_values` sort what is left into flags, operands and the values options take.

What it promises. A shell word that is quoted is the text between the quotes: `'{a,b}'` is one word and `'~/x'` names a
folder called `~`. Expansion is bounded: a `Budget` caps the words that brace expansion may make for one command, and
a command that goes over, or text it cannot read, raises `ShellError` with a `kind`; callers treat that as a refusal
and never as an allow. It reads text only: what a command substitution prints, or a variable the line does not set,
is not known and is left as written.

Stdlib only. Nothing here knows about a policy; ws.policy decides what a word means.
"""

from __future__ import annotations

import itertools
import os
import posixpath
import re
import unicodedata
from collections.abc import Mapping
from fnmatch import fnmatchcase
from typing import NamedTuple

MAX_WORDS = 4096  # words that brace expansion may add for one command; more is refused, not slowed down
MAX_BRACE_DEPTH = 64  # nested brace groups in one word
PROCSUB = "/dev/fd/63"  # what `<(...)` becomes
WRITE_OPS = (">", ">>", ">|", ">&", "&>", "&>>", "<>")  # redirections that write to their target


class ShellError(ValueError):
    """Text the reader cannot read or will not expand. `kind` is one of unclosed_quote, too_many_words,
    too_deep; callers branch on it, never on the message."""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


class Budget:
    """The words brace expansion may still add to one command line. One is shared by every segment of the line, so
    many small expansions are bounded as well as one large one."""

    def __init__(self, words: int = MAX_WORDS):
        self.limit = words
        self.left = words

    def check(self, n: int) -> None:
        """Refuse now if `n` more words would go over, before any of them is made."""
        if n > self.left:
            raise ShellError(
                "too_many_words",
                f"the command expands to more than {self.limit:,} words; write the names to a file or use a loop",
            )

    def spend(self, n: int) -> None:
        self.check(n)
        self.left -= n


class Redirect(NamedTuple):
    """One redirection: `op` is `<`, `>`, `>>`, `>|`, `<>`, `&>`, `&>>`, `>&`, `<&`, `<<<`, `<<` or `<<-` and `target` the
    word after it (brace-expanded: `> f{1,2}` is two). A file descriptor before the operator is dropped; a copy of a
    descriptor (`2>&1`, `<&-`) is not a redirection to a file and is left out."""

    op: str
    target: str


class Lexed(NamedTuple):
    words: list[str]
    redirs: list[Redirect]
    pieces: list[str]


ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


# --- text: line continuations, here-documents, segments --------------------------------------------------------------


_ANSI_ESCAPES = {"a": "\a", "b": "\b", "e": "\x1b", "E": "\x1b", "f": "\f", "n": "\n", "r": "\r", "t": "\t", "v": "\v"}
_ANSI_NUMBER = {
    "x": re.compile(r"[0-9A-Fa-f]{1,2}"),
    "u": re.compile(r"[0-9A-Fa-f]{1,4}"),
    "U": re.compile(r"[0-9A-Fa-f]{1,8}"),
}
_ANSI_OCTAL = re.compile(r"[0-7]{1,3}")


def ansi_c(seg: str, i: int) -> tuple[str, int]:
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


def prep(cmd: str) -> str:
    """Line continuations joined, full-width characters folded, ${IFS} read as the space it is. Raises ShellError (kind
    `too_deep`) for substitutions nested past what the reader follows."""
    try:
        cmd = join_lines(unicodedata.normalize("NFKC", cmd))
    except RecursionError:
        raise ShellError("too_deep", "the command nests substitutions too deeply to check") from None
    return re.sub(r"\$\{IFS\}|\$IFS\b", " ", cmd)


def join_lines(cmd: str) -> str:
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
        elif cmd.startswith("$(", i) or (c == "`" and backtick_end(cmd, i) > 0):
            if c == "`":
                j, head, tail = backtick_end(cmd, i), "`", "`"
            else:
                j, head, tail = close_paren(cmd, i + 2), "$(", ")"
            out += [head, join_lines(cmd[i + len(head) : j])]
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


def folds(command: str) -> bool:
    """Would prep change the characters the shell sees (full-width letters read as plain ones, `$IFS` as a space)?
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


def without_bodies(command: str) -> str | None:
    """The command with the body and closing line of each here-document taken out, when every line of it is the
    shape above and every body closes on its exact closing line; None otherwise (read the whole text as commands)."""
    if prep(command) != command or "\\\n" in command:
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


def scan(
    cmd: str, heredocs: bool = False, end: dict | None = None, flow: list | None = None
) -> tuple[list[list[str]], list[str]]:
    """Split a shell command line. Returns (pipelines, inner): each pipeline is a list of segments (split on |, |&),
    pipelines are split on ; && || & newline ( ); inner holds the text of $(...), backticks and <(...), which run too.
    A comment (a `#` that starts a word, outside quotes and `${...}`) is left out, unless the line holds a `<<` and
    `heredocs` is off: a body is then read as commands, and a `#` line in it is not a comment. With `heredocs`, the body
    and closing line of each here-document are left out too (that text is data for a program, not commands): a `<<WORD` starts one
    only outside quotes and comments, and not as `<<<` or a shift in `((...))` or inside `${...}` and `$[...]`, which are
    one word. If `end` is given, end["quote"] is the quote still open at the end of the text (empty if none). If `flow`
    is given, it gets the order the line runs in: the index of each pipeline as it ends, and between them the operator that came next (`;`, `&&`, `||`,
    `&`, `(` or `)`; a newline is `;`)."""
    pipelines: list[list[str]] = []
    inner: list[str] = []
    segs: list[str] = []
    buf: list[str] = []
    waiting: list[tuple[str, bool]] = []  # (closing word, may be indented) of the here-documents the line opened
    nest: list[str] = []  # the closers still owed by the ${...} and $[...] we are inside
    n, i, quote = len(cmd), 0, ""
    # Where a here-document may be, a `#` line could be a body line, and the body of an unquoted one is expanded; a line
    # that is read with its bodies as commands keeps its comments too, so that nothing in a body is passed over.
    comments = heredocs or "<<" not in cmd

    def sub(body: str) -> str:  # the text of a $(...) that is really `$((a<<b))` arithmetic: its `<<` is a shift
        return body.replace("<<", "< <") if heredocs and _arithmetic(body) else body

    def end_seg() -> None:
        s = "".join(buf).strip()
        buf.clear()
        if s:
            segs.append(s)

    def end_pipeline(op: str = "") -> None:
        nonlocal segs
        end_seg()
        if segs:
            pipelines.append(segs)
            if flow is not None:
                flow.append(len(pipelines) - 1)
        segs = []
        if op and flow is not None:
            flow.append(op)

    while i < n:
        c = cmd[i]
        if c == "\\" and quote != "'" and i + 1 < n:
            buf.append(cmd[i : i + 2])
            i += 2
            continue
        if quote:
            if c == quote[-1]:
                quote = ""
            elif quote == '"' and c == "`":  # a command inside the quotes runs, and its quotes are its own
                j = backtick_end(cmd, i)
                if j > 0:
                    inner.append(unbacktick(cmd[i + 1 : j]))
                    buf.append(cmd[i : j + 1])
                    i = j + 1
                    continue
            elif quote == '"' and cmd.startswith("$(", i):
                j = close_paren(cmd, i + 2)
                inner.append(sub(cmd[i + 2 : j]))
                if (
                    j < n
                ):  # closed: its quotes are its own; if not (a here-document inside), read on character by character
                    buf.append(cmd[i : j + 1])
                    i = j + 1
                    continue
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
        if comments and not nest and c == "#" and (not buf or buf[-1] in " \t"):  # a comment: not commands
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
            end_pipeline(";")
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
            j = close_paren(cmd, i + 1)
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
            j = backtick_end(cmd, i)
            j = n if j < 0 else j
            inner.append(unbacktick(cmd[i + 1 : j]))
            buf.append(cmd[i : j + 1])
            i = j + 1
            continue
        elif cmd.startswith("$(", i):
            j = close_paren(cmd, i + 2)
            inner.append(sub(cmd[i + 2 : j]))
            buf.append(cmd[i : j + 1])
            i = j + 1
            continue
        elif c in "<>" and cmd.startswith("(", i + 1):  # process substitution: a file name to the outer command
            j = close_paren(cmd, i + 2)
            inner.append(cmd[i + 2 : j])
            buf.append(" " + PROCSUB + " " if c == "<" else " /dev/null ")
            i = j + 1
            continue
        elif c in ";\n()":
            end_pipeline(";" if c == "\n" else c)
        elif c == "&":
            if i > 0 and cmd[i - 1] in "<>" or cmd[i + 1 : i + 2] == ">":  # 2>&1, &>file
                buf.append(c)
            elif cmd[i + 1 : i + 2] == "&":
                end_pipeline("&&")
                i += 1
            else:
                end_pipeline("&")
        elif c == "|" and i > 0 and cmd[i - 1] == ">":  # >| (clobber redirect)
            buf.append(c)
        elif c == "|":
            if cmd[i + 1 : i + 2] == "|":
                end_pipeline("||")
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


def backtick_end(cmd: str, i: int) -> int:
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


def unbacktick(text: str) -> str:
    """The command inside a backtick pair, as the shell reads it: `\\``, `\\$` and `\\\\` lose their backslash."""
    return re.sub(r"\\([`$\\])", r"\1", text)


def close_paren(cmd: str, i: int) -> int:
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


# --- wrappers -------------------------------------------------------------------------------------------------------

KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "coproc"}
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


def strip_wrappers(
    words: list[str],
    env: list[str] | None = None,
    *,
    lexenv: Mapping[str, str] | None = None,
    budget: Budget | None = None,
    dropped: list[str] | None = None,
    scripts: list[str] | None = None,
) -> tuple[list[str], set[str]]:
    """Drop leading keywords, VAR=value and exec wrappers (sudo, env, timeout, nice, xargs, busybox ...): what is left
    is the command that really runs. Also returns the wrappers it removed. If `env` is given, the VAR=value words it
    drops (a prefix, or the ones `env` sets) are added to it. `dropped` gets the words a wrapper took as its own
    (`-a FILE` of xargs, the user of `sudo -u`), and `scripts` the text of an `env -S` string, which runs as a command
    line of its own; `lexenv` and `budget` are for reading that string (see `lex`)."""
    toks, seen = list(words), set()
    while toks:
        w = toks[0]
        if w in KEYWORDS or ASSIGN.match(w):
            if env is not None and ASSIGN.match(w):
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
            elif w == "env" and ASSIGN.match(t):
                if env is not None:
                    env.append(t)
                i += 1
            else:
                break
        if w == "timeout" and i < len(toks):
            i += 1  # the duration
        if dropped is not None:
            dropped += [t for t in toks[1:i] if not t.startswith("-") and not ASSIGN.match(t)]
        if resplit and scripts is not None:
            scripts.append(resplit)
        toks = (words_of(resplit, lexenv, budget) if resplit else []) + toks[i:]
    return toks, seen


# --- words: braces, expansion, quotes, redirections -----------------------------------------------------------------


def _dq_close(s: str, i: int) -> int:
    """Index of the quote that closes the double quote whose body starts at `i` (the length of the text if it never
    does). A `$(...)`, backticks and `${...}` inside are skipped whole, so a quote in a nested command is not this one."""
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
        elif c == '"':
            return i
        elif s.startswith("$(", i):
            i = close_paren(s, i + 2) + 1
        elif c == "`":
            j = backtick_end(s, i)
            i = n if j < 0 else j + 1
        elif (e := _param_end(s, i)) is not None:
            i = e + 1
        else:
            i += 1
    return n


def _param_end(s: str, i: int) -> int | None:
    """Index of the `}` of the parameter expansion `${...}` that starts at `s[i]`, or None when none does: a `${` that
    is followed by a blank or `|` is a command group (`${ cmd; }`), and one that never closes is not an expansion."""
    if not s.startswith("${", i) or s[i + 2 : i + 3] in ("", " ", "\t", "\n", "|"):
        return None
    end = _brace_close(s, i + 2)
    return end if end < len(s) else None


def _brace_close(s: str, i: int) -> int:
    """Index of the `}` that closes a `${` whose body starts at `i` (the end of the text if it never does)."""
    depth, n = 1, len(s)
    while i < n:
        c = s[i]
        if c == "\\":
            i += 2
            continue
        if c == "'":
            j = s.find("'", i + 1)
            i = n if j < 0 else j + 1
            continue
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _read_raw(seg: str, i: int) -> int:
    """The index where the word starting at `i` ends: at a blank or an unquoted `<` or `>`. Quotes, `$(...)`, `${...}`
    and backticks are read whole, so the blanks and `>` inside them do not end the word."""
    n = len(seg)
    while i < n:
        c = seg[i]
        if c in " \t\r\n<>":
            break
        if c == "\\":
            i += 2
        elif c == "'":
            j = seg.find("'", i + 1)
            i = n if j < 0 else j + 1
        elif c == '"':
            i = _dq_close(seg, i + 1) + 1
        elif seg.startswith("$'", i):
            i = ansi_c(seg, i + 2)[1]
        elif seg.startswith("$(", i):
            i = close_paren(seg, i + 2) + 1
        elif c == "`":
            j = backtick_end(seg, i)
            i = n if j < 0 else j + 1
        elif (e := _param_end(seg, i)) is not None:
            i = e + 1
        else:
            i += 1
    return min(i, n)


def _units(raw: str) -> list[str]:
    """A raw word as the pieces brace expansion sees: a plain character is itself, and anything quoted, escaped or
    substituted is one piece, so a quoted `{`, `,` or `}` is never a brace."""
    out: list[str] = []
    n, i = len(raw), 0
    while i < n:
        c = raw[i]
        if c == "\\" and i + 1 < n:
            j = i + 2
        elif c == "'":
            k = raw.find("'", i + 1)
            j = n if k < 0 else k + 1
        elif c == '"':
            j = min(_dq_close(raw, i + 1) + 1, n)
        elif raw.startswith("$'", i):
            j = ansi_c(raw, i + 2)[1]
        elif raw.startswith("$(", i):
            j = min(close_paren(raw, i + 2) + 1, n)
        elif c == "`":
            k = backtick_end(raw, i)
            j = n if k < 0 else k + 1
        elif (e := _param_end(raw, i)) is not None:
            j = e + 1
        else:
            j = i + 1
        out.append(raw[i:j])
        i = j
    return out


_INT = re.compile(r"[+-]?[0-9]+\Z")


def _sequence(body: list[str], budget: Budget) -> list[str] | None:
    """The words of a `a..b` or `a..b..step` brace body (integers, or single characters), or None when the body is not
    one. The count is worked out before any word is made, so a huge sequence is refused rather than built."""
    if any(len(u) != 1 for u in body):
        return None
    parts = "".join(body).split("..")
    if len(parts) not in (2, 3):
        return None
    lo, hi = parts[0], parts[1]
    step = 1
    if len(parts) == 3:
        if not _INT.match(parts[2]):
            return None
        step = abs(int(parts[2])) or 1
    if _INT.match(lo) and _INT.match(hi):
        a, b = int(lo), int(hi)
        budget.check(abs(b - a) // step + 1)
        nums = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
        digits = max(len(lo.lstrip("+-")), len(hi.lstrip("+-")))
        pad = digits if any(len(x.lstrip("+-")) > 1 and x.lstrip("+-").startswith("0") for x in (lo, hi)) else 0
        return [("-" if v < 0 else "") + str(abs(v)).zfill(pad) for v in nums]
    if len(lo) == 1 and len(hi) == 1 and not lo.isdigit() and not hi.isdigit():
        a, b = ord(lo), ord(hi)
        budget.check(abs(b - a) // step + 1)
        chars = range(a, b + 1, step) if a <= b else range(a, b - 1, -step)
        return [chr(v) for v in chars]
    return None


def _closers(units: list[str]) -> dict[int, tuple[int, list[int]]]:
    """For each `{` that has a closing `}`: (index of the `}`, indexes of the commas that belong to it). One pass."""
    close: dict[int, tuple[int, list[int]]] = {}
    stack: list[tuple[int, list[int]]] = []
    for k, u in enumerate(units):
        if u == "{":
            stack.append((k, []))
        elif u == "," and stack:
            stack[-1][1].append(k)
        elif u == "}" and stack:
            start, commas = stack.pop()
            close[start] = (k, commas)
    return close


def _expand_units(units: list[str], budget: Budget, depth: int) -> list[str]:
    if depth > MAX_BRACE_DEPTH:
        raise ShellError("too_deep", f"braces nest more than {MAX_BRACE_DEPTH} deep")
    close = _closers(units)
    slots: list[list[str]] = []
    lit: list[str] = []
    i, n = 0, len(units)
    while i < n:
        if units[i] == "{" and i in close:
            end, commas = close[i]
            alts: list[str] | None
            if commas:
                alts = []
                edges = [i, *commas, end]
                for a, b in itertools.pairwise(edges):
                    alts += _expand_units(units[a + 1 : b], budget, depth + 1)
                    budget.check(len(alts))
            else:
                alts = _sequence(units[i + 1 : end], budget)
            if alts is not None:
                if lit:
                    slots.append(["".join(lit)])
                    lit = []
                slots.append(alts)
                i = end + 1
                continue
        lit.append(units[i])
        i += 1
    if not slots:
        return ["".join(lit)]
    if lit:
        slots.append(["".join(lit)])
    total = 1
    for s in slots:
        total *= len(s)
        budget.check(total)
    budget.spend(total)
    return ["".join(c) for c in itertools.product(*slots)]


def expand_braces(raw: str, budget: Budget | None = None) -> list[str]:
    """Brace expansion of one raw word (quotes still in it), as bash does it: `x{a,b}y` is `xay xby`, `{1..3}` is `1 2
    3`, `{a..c}` is `a b c`, groups nest and chain. Only a `{`, `,` and `}` that are not quoted count; a group with no
    comma and no valid sequence stays as written. `budget` caps the words made (ShellError, kind `too_many_words`)."""
    if "{" not in raw:
        return [raw]
    return _expand_units(_units(raw), budget or Budget(), 0)


_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TILDE = re.compile(r"~([A-Za-z0-9_.+-]*)")
_SPLIT = re.compile(r"([ \t\r\n]+)")


def _tilde(name: str, env: Mapping[str, str]) -> str | None:
    """What `~name` stands for: the home folder for a bare `~`, the named user's, or None when it is not known."""
    if not name:
        return env.get("HOME")
    path = os.path.expanduser("~" + name)
    return None if path.startswith("~") else path


def _param(body: str, env: Mapping[str, str]) -> str | None:
    """The value of `${body}` for a variable `env` knows (with `:-`, `:=`, `:+`, `%`, `#`, `/` and `:offset:length`
    operators), or None when the variable or the operator is not known: the text is then left as written."""
    m = _NAME.match(body)
    if not m or m.group() not in env:
        return None
    val, rest = env[m.group()], body[m.end() :]
    if not rest:
        return val
    colon = rest[0] == ":"
    op = rest[1:2] if colon else rest[:1]
    arg = rest[2:] if colon else rest[1:]
    if op in ("-", "="):
        return val if val or not colon else arg
    if op == "+":
        return arg if val or not colon else ""
    if op == "?":
        return val if val or not colon else None
    if rest[0] in "%#":
        long = rest[1:2] == rest[0]
        pat = rest[2:] if long else rest[1:]
        cuts = range(len(val) + 1)
        if rest[0] == "%":  # strip a suffix: the shortest match has the largest start
            hits = [k for k in reversed(cuts) if fnmatchcase(val[k:], pat)]
            return val[: (hits[-1] if long else hits[0])] if hits else val
        hits = [k for k in cuts if fnmatchcase(val[:k], pat)]
        return val[(hits[-1] if long else hits[0]) :] if hits else val
    if rest[0] == "/":
        every = rest[1:2] == "/"
        pat, _, rep = (rest[2:] if every else rest[1:]).partition("/")
        if not pat or re.search(r"[*?\[]", pat):
            return None
        return val.replace(pat, rep) if every else val.replace(pat, rep, 1)
    if colon and re.fullmatch(r":\s*-?[0-9]+(:\s*-?[0-9]+)?", rest):
        off, _, size = rest[1:].partition(":")
        start = int(off)
        start = max(len(val) + start, 0) if start < 0 else start
        return val[start : start + int(size)] if size else val[start:]
    return None


def _dollar(s: str, k: int, env: Mapping[str, str]) -> tuple[str | None, int]:
    """(value, end) for the `$NAME` or `${...}` at `s[k]`; the value is None when it is not known, and `end` is where
    the text of that expansion ends either way."""
    if (e := _param_end(s, k)) is not None:
        return _param(s[k + 2 : e], env), e + 1
    m = _NAME.match(s, k + 1)
    if m:
        return env.get(m.group()), m.end()
    return None, k + 1


def _vars(text: str, env: Mapping[str, str]) -> str:
    """`text` with the `$NAME` and `${NAME}` it holds expanded, and nothing else changed (the inside of single quotes,
    read the way a program that expands for itself would)."""
    if "$" not in text or not env:
        return text
    out: list[str] = []
    k, n = 0, len(text)
    while k < n:
        if text[k] == "$":
            val, e = _dollar(text, k, env)
            out.append(text[k:e] if val is None else val)
            k = e
        else:
            out.append(text[k])
            k += 1
    return "".join(out)


def _dq_text(body: str, env: Mapping[str, str]) -> str:
    """The text of a double-quoted body: `\\"`, `\\\\`, `\\$` and `\\`` lose their backslash, variables are expanded,
    and `$(...)` and backticks are kept as written (they are commands, read on their own)."""
    out: list[str] = []
    k, n = 0, len(body)
    while k < n:
        c = body[k]
        if c == "\\" and k + 1 < n and body[k + 1] in '"\\$`':
            out.append(body[k + 1])
            k += 2
        elif body.startswith("$(", k):
            e = close_paren(body, k + 2)
            out.append(body[k : e + 1])
            k = e + 1
        elif c == "`":
            e = backtick_end(body, k)
            e = n - 1 if e < 0 else e
            out.append(body[k : e + 1])
            k = e + 1
        elif c == "$":
            val, e = _dollar(body, k, env)
            out.append(body[k:e] if val is None else val)
            k = e
        else:
            out.append(c)
            k += 1
    return "".join(out)


def _expand_raw(raw: str, env: Mapping[str, str], loose: bool) -> list[str]:
    """One brace-expanded raw word as the words the shell makes of it: `~` at the start (and after `=` or `:` in a word
    that looks like an assignment), `$NAME` and `${...}` expanded, quotes removed. An unquoted variable whose value holds
    blanks makes several words. With `loose`, the inside of single quotes has its variables expanded too: that is the
    text a program reads when it expands for itself."""
    words: list[str] = []
    cur: list[str] = []
    state = {"started": False}
    n, i = len(raw), 0
    assign = ASSIGN.match(raw) is not None
    eq = raw.index("=") if assign else -1
    tilde_at = {0}

    def flush() -> None:
        if state["started"]:
            words.append("".join(cur))
            cur.clear()
            state["started"] = False

    def lit(text: str) -> None:
        cur.append(text)
        state["started"] = True

    while i < n:
        c = raw[i]
        if c == "~" and i in tilde_at and env:
            m = _TILDE.match(raw, i)
            end = m.end()
            val = _tilde(m.group(1), env) if end >= n or raw[end] == "/" or (assign and raw[end] == ":") else None
            if val is not None:
                lit(val)
                i = end
                continue
        if c == "\\" and i + 1 < n:
            lit(raw[i + 1])
            i += 2
            continue
        if c == "'":
            j = raw.find("'", i + 1)
            j = n if j < 0 else j
            lit(_vars(raw[i + 1 : j], env) if loose else raw[i + 1 : j])
            i = j + 1
            continue
        if raw.startswith("$'", i):
            text, i = ansi_c(raw, i + 2)
            lit(text)
            continue
        if raw.startswith('$"', i):  # locale quoting reads as double quotes
            i += 1
            continue
        if c == '"':
            j = _dq_close(raw, i + 1)
            lit(_dq_text(raw[i + 1 : j], env))
            i = j + 1
            continue
        if raw.startswith("$(", i):
            j = close_paren(raw, i + 2)
            lit(raw[i : j + 1])
            i = j + 1
            continue
        if c == "`":
            j = backtick_end(raw, i)
            j = n - 1 if j < 0 else j
            lit(raw[i : j + 1])
            i = j + 1
            continue
        if c == "$":
            val, end = _dollar(raw, i, env)
            if val is None:
                lit(raw[i:end])
            else:
                for k, piece in enumerate(_SPLIT.split(val)):
                    if k % 2:
                        flush()
                    elif piece:
                        cur.append(piece)
                        state["started"] = True
            i = end
            continue
        lit(c)
        if assign and ((i == eq) or (i > eq and c == ":")):
            tilde_at.add(i + 1)
        i += 1
    flush()
    return words


_DELIMS = "=@<>:,;"
_FILE_URL = re.compile(r"file:(?://(?:localhost)?)?(?=/)")
_REMOTE = re.compile(r"(?!file:)[A-Za-z0-9_.@+-]+:(?=[~/])")
_CUT = re.compile(r"[;,:]")
_HINT = re.compile(r"[=@<>:,;]|\A-[^-]|\Afile:")


def _pieces(word: str, env: Mapping[str, str], head: bool = False) -> list[str]:
    """Where inside one word a program may read a path from: after `=`, `@`, `<`, `>`, `:`, `,` or `;`, after the
    letters of a short option (`-sT/x`, `-0a~/x`), and after `file://`; each is given whole and cut at its first `;`, `,`
    or `:`. A leading `~` is expanded. With `head`, a word that is a path followed by a separator (`P:/in/container`,
    a mount) also gives that path. A word with a blank in it is prose or a program, and has none."""
    if not _HINT.search(word) or len(word) > 2048 or _SPLIT.search(word):
        return []
    starts: set[int] = set()
    for k in range(1, len(word)):
        if word[k - 1] in _DELIMS:
            starts.add(k)
    if word[:1] == "-" and word[1:2] != "-":
        k = 2
        while k < len(word) and word[k].isalnum():
            k += 1
        if k < len(word) and word[k] in "/~":
            starts.add(k)
    m = _FILE_URL.match(word)
    if m:
        starts.add(m.end())
    out = []
    if head and word.startswith("/") and (first := _CUT.split(word, 1)[0]) not in ("", word):
        out.append(first)
    remote = _REMOTE.match(word)  # `host:~/x` names a file on another machine
    if remote:
        starts.discard(remote.end())
    for s in sorted(starts)[:64]:
        piece = word[s:]
        if piece.startswith("~") and env:
            piece = expand_path(piece, env)
        if piece:
            out.append(piece)
            head = _CUT.split(piece, 1)[0]  # `P;type=text/plain`, `P:12`: the path ends at the separator
            if head and head != piece:
                out.append(head)
    return out


def _operator(seg: str, i: int) -> tuple[str, int]:
    """(redirection operator, index after it) at `seg[i]`, which is `<`, `>` or the `&` of `&>`."""
    if seg[i] == "&":
        op = "&>>" if seg.startswith("&>>", i) else "&>"
        return op, i + len(op)
    for op in ("<<<", "<<-", "<<", "<&", "<>", "<", ">>", ">|", ">&", ">"):
        if seg.startswith(op, i):
            return op, i + len(op)
    return seg[i], i + 1  # unreachable: the caller checked the first character


_DUP = re.compile(r"[0-9]+-?\Z|-\Z")
_FD = re.compile(r"(?:[0-9]+|\{[A-Za-z_][A-Za-z0-9_]*\})\Z")


def lex(seg: str, env: Mapping[str, str] | None = None, budget: Budget | None = None) -> Lexed:
    """Read one shell segment into words, redirections and pieces (see the module docstring). `env` holds the variables
    that are known (`HOME`, `WS_HOME` and what earlier parts of the line set); with none, `~` and `$NAME` stay as
    written. A redirection glued to a word (`/>/dev/null`, `<~/f`, `2>&1`) is split off it. Brace expansion makes
    several words (or several redirections) of one; `budget` caps how many (ShellError)."""
    env = env if env is not None else {}
    budget = budget if budget is not None else Budget()
    words: list[str] = []
    redirs: list[Redirect] = []
    pieces: list[str] = []
    n, i = len(seg), 0

    def read(raw: str) -> list[str]:
        out: list[str] = []
        for alt in expand_braces(raw, budget):
            got = _expand_raw(alt, env, False)
            out += got
            for w in got:
                pieces.extend(_pieces(w, env, head=True))
            if "'" in alt or "$" in alt:  # what a program reads when it expands for itself
                for w in _expand_raw(alt, env, True):
                    pieces.extend(_pieces(w, env))
        return out

    while i < n:
        c = seg[i]
        if c in " \t\r\n":
            i += 1
        elif c in "<>" or (c == "&" and seg.startswith(">", i + 1)):
            op, i = _operator(seg, i)
            while i < n and seg[i] in " \t":
                i += 1
            j = _read_raw(seg, i)
            raw, i = seg[i:j], j
            for target in read(raw):
                if op.endswith("&") and op != "&>" and _DUP.match(target):
                    continue  # 2>&1, <&-
                redirs.append(Redirect(op, target))
        else:
            j = _read_raw(seg, i)
            raw, i = seg[i:j], j
            if _FD.match(raw) and i < n and seg[i] in "<>":
                continue  # the 2 of 2>file
            words.extend(read(raw))
    return Lexed(words, redirs, pieces)


def words_of(text: str, env: Mapping[str, str] | None = None, budget: Budget | None = None) -> list[str]:
    """Just the words of `lex`: for text that is a command line on its own (an `env -S` string, a git alias)."""
    return lex(text, env, budget).words


# --- arguments and paths ---------------------------------------------------------------------------------------------


def split_args(toks: list[str]) -> tuple[list[str], list[str]]:
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


def has_flag(flags: list[str], letter: str) -> bool:
    """Is `letter` in any single-dash flag group (`-c`, `-lc`, `-ec`)?"""
    return any(f[0] == "-" and f[1:2] != "-" and letter in f[1:] for f in flags)


def option_values(toks: list[str], letters: str = "", longs: tuple[str, ...] = ()) -> list[str]:
    """The values given to an option: `-o X`, `-oX`, `-sSLo X` (the option last in a group), `--output X`,
    `--output=X`. `letters` are the short options that take a value and `longs` the long ones."""
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
        elif t.startswith("-") and len(t) > 2:
            k = next((k for k, ch in enumerate(t[1:], 1) if ch in letters), 0)
            if k and k + 1 < len(t):  # the value is glued to the letter: -oFILE, -sSLoFILE
                out.append(t[k + 1 :])
    return out


def expand_path(path: str, env: Mapping[str, str]) -> str:
    """A path as written in a rule or by a tool, with a leading `~`, `~user`, `$HOME`, `${HOME}`, `$WS_HOME` or
    `${WS_HOME}` made into the folder it stands for (the names `env` knows). The rest is left alone."""
    if path.startswith("~"):
        m = _TILDE.match(path)
        if m and (m.end() == len(path) or path[m.end()] == "/"):
            val = _tilde(m.group(1), env)
            if val is not None:
                return val.rstrip("/") + path[m.end() :] if val != "/" else path[m.end() :] or "/"
        return path
    for name in ("HOME", "WS_HOME"):
        for pre in ("${" + name + "}", "$" + name):
            if (path == pre or path.startswith(pre + "/")) and name in env:
                return env[name].rstrip("/") + path[len(pre) :]
    return path


def open_quote(command: str) -> str:
    """The quote still open at the end of the command text (`'`, `"` or `$'`), or "" if every quote is closed. Comments
    and here-document bodies are not read: an apostrophe in prose there is not a quote."""
    end: dict = {}
    scan(prep(command), heredocs=True, end=end)
    return end.get("quote", "")


def require_closed(command: str) -> None:
    """Raise ShellError (kind `unclosed_quote`) when the command ends inside a quote: what runs cannot be read."""
    quote = open_quote(command)
    if quote:
        raise ShellError(
            "unclosed_quote",
            f"the command ends inside an unclosed {quote} quote, so what runs cannot be read; close the quote",
        )
