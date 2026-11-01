"""claims: a message that says a commit is merged, pushed or on main names a commit that is. Before a message goes to
another agent, each claim word (default `on main`, `merged into main`, `merged to main`, `pushed to main`) that sits in
the same sentence as a commit-like token (7 to 40 lower-case hex characters, with at least one digit and one of a to f)
is checked with `git merge-base --is-ancestor TOKEN REF` in the session's folder (default REF `origin/main`). A commit
that isn't an ancestor of REF, or isn't a commit in this repository, denies the message and the reason names it; the
reason says to `git fetch` first when REF may be out of date.

Not a claim: a claim word in quotes or backticks, one with a negation or a future form in the four words before it
(`not merged`, `will be pushed`, `once it's merged`), and any sentence that names another branch (`origin/work`, or
`pushed to work`, `merged into work`), because the check is against one ref. A folder that isn't a repository, no git on
the machine, a REF that doesn't exist here, or git taking more than 8 seconds in total for one message lets the message
through with a note. Git runs without lazy fetching (`GIT_NO_LAZY_FETCH=1`), so a check never goes to the network.

Release claims are off until `release_dir` is set: a folder whose `current` link points at the promoted release. A clause
(a sentence cut at `;`, `but`, `while`, `whereas`) that says a release is live or promoted (`v0.2 is live`, `promoted
v0.2 to live`, `live now: v0.2`) must name only the release `current` points at. A release is a tag whose shape is one of
`release_tags` (regexes, default `v1.2` and `v1.2.3`) and it matches the link's target folder name, with or without a
leading `v`. A clause that names another release is denied, and so is a live claim when `current` is missing, isn't a
link or can't be read. The same quote, negation and future rules apply (`goes live`, `will be live`, `once promoted`
aren't claims). It reads the link and nothing else: no fetch, no network, nothing written.

When the session stops, the claims in the runner's last message (if the stop payload carries it) are appended to
`$WS_HOME/logs/claims.jsonl` with whether each commit was on REF. That is a log only; nothing is stopped. Each line
carries `schema` and `ts`, and `at` (the legacy spelling of `ts`) beside them.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ws import jsonl
from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError
from ws.profile import ws_home

NAME = "claims"
EVENTS = ("pre", "stop")
KINDS = ("message",)

DEFAULT_WORDS = ("on main", "merged into main", "merged to main", "pushed to main")
DEFAULT_REF = "origin/main"
DEFAULT_TAGS = (r"v\d+\.\d+(?:\.\d+)*",)
MAX_TAG_PATTERN = 200
LOG = "claims.jsonl"
SCHEMA_VERSION = 1
GIT_BUDGET_S = 8.0  # all the git time one hook may use
MAX_TEXT = 20000  # a message is read to this length

_HEX = re.compile(r"(?<![\w-])[0-9a-f]{7,40}(?![\w-])")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUOTES = "\"'`‘’“”"
_NEGATION = {"not", "no", "never", "without"}
_FUTURE = {"will", "shall", "would", "should", "going", "gonna", "be", "if", "once", "when", "until", "unless"}
_FOUR = re.compile(r"[a-z]+(?:'[a-z]+)?")
_REMOTE_BRANCH = re.compile(r"(?<![\w/-])origin/([\w][\w./-]*)", re.IGNORECASE)
_TO_BRANCH = re.compile(
    r"\b(?:merged|pushed|landed)\s+(?:[0-9a-f]{7,40}\s+)?(?:to|into|onto)\s+(?:the\s+)?(?:origin/)?([\w][\w./-]*)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Config:
    words: tuple[str, ...] = DEFAULT_WORDS
    ref: str = DEFAULT_REF
    release_dir: str = ""  # "" = release claims are off
    release_tags: tuple[str, ...] = DEFAULT_TAGS


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("words", "ref", "release_dir", "release_tags"))
    words = text_list(table, "words", where, DEFAULT_WORDS)
    if not words:
        raise PolicyError(f"{where}: words must name at least one claim word")
    ref = table.get("ref", DEFAULT_REF)
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./@{}^~-]+", ref) or ref.startswith("-"):
        raise PolicyError(f"{where}: ref must be a branch, tag or remote branch name such as origin/main")
    return Config(words, ref, _release_dir(table, where), _release_tags(table, where))


def _release_dir(table: dict, where: str) -> str:
    folder = table.get("release_dir", "")
    if not isinstance(folder, str):
        raise PolicyError(f"{where}: release_dir must be a folder path such as ~/.ws-release")
    if folder and (not (folder.startswith(("/", "~/")) or folder == "~") or "\n" in folder or "\0" in folder):
        raise PolicyError(f"{where}: release_dir must be an absolute path or start with ~/")
    return folder


def _release_tags(table: dict, where: str) -> tuple[str, ...]:
    tags = text_list(table, "release_tags", where, DEFAULT_TAGS)
    if not tags:
        raise PolicyError(f"{where}: release_tags must name at least one pattern")
    for t in tags:
        try:
            compiled = re.compile(t)
        except re.error as e:
            raise PolicyError(f"{where}: release_tags: {t!r} is not a regular expression ({e})") from None
        if len(t) > MAX_TAG_PATTERN or compiled.fullmatch("") or _zero_width(compiled):
            raise PolicyError(
                f"{where}: release_tags: {t!r} must be at most {MAX_TAG_PATTERN} characters and can't match nothing"
            )
        if _nested_quantifier(t):
            raise PolicyError(
                f"{where}: release_tags: {t!r} repeats a group that repeats (such as (\\d+)+), which can take "
                "minutes on a long message; write a simple shape such as v[0-9]+[.][0-9]+"
            )
    try:
        _tag_pattern(tags)  # the patterns are joined and wrapped: one that only works alone (an inline flag) is out
    except re.error as e:
        raise PolicyError(f"{where}: release_tags: the patterns don't work together ({e})") from None
    return tags


_PROBES = ("", "v1.2", "release v1.2.3 is live, v0.2")


def _zero_width(compiled: re.Pattern) -> bool:
    """True when the pattern matches nothing (an empty hit) anywhere in a few sample messages."""
    return any(m.start() == m.end() for probe in _PROBES for m in compiled.finditer(probe))


def _quantifier(pattern: str, i: int) -> tuple[str | None, int]:
    """(kind, next index) for the repeat at `i`: "many" for `*`, `+`, `{n,}`; "some" for `?` and `{n,m}`; else None.
    A lazy or possessive mark after it is read too."""
    m = re.match(r"[*+]|\?|\{\d+(,\d*)?\}", pattern[i:])
    if not m:
        return None, i
    text = m.group(0)
    many = text in ("*", "+") or (text.startswith("{") and text.endswith(",}"))
    j = i + len(text)
    if pattern[j : j + 1] in ("?", "+"):
        j += 1
    return ("many" if many else "some"), j


def _nested_quantifier(pattern: str) -> bool:
    """True when a group that is only repeats inside (`(\\d+)`, `(a*)`) is itself repeated without a bound: the
    shapes `(\\d+)+` and `(a*)*` that can take minutes on a long message. A group with a fixed part, such as the dot
    in `(\\.\\d+)*`, is fine. A structural check over escapes, classes and groups, not a proof."""
    stack: list[tuple[bool, bool]] = []  # the enclosing groups' (holds an unbounded repeat, has a fixed part)
    rep = fixed = False
    i, n = 0, len(pattern)
    while i < n:
        c = pattern[i]
        if c == "(":
            stack.append((rep, fixed))
            rep = fixed = False
            i += 1
            if pattern[i : i + 1] == "?":  # (?:  (?=  (?!  (?<=  (?P<name>
                while i < n and pattern[i] not in ":=!>":
                    i += 1
                i += 1
            continue
        if c == "|":
            i += 1
            continue
        if c == ")":
            inner_rep, inner_fixed = rep, fixed
            rep, fixed = stack.pop() if stack else (False, False)
            i += 1
        else:
            inner_rep, inner_fixed = False, True
            if c == "\\":
                i += 2
            elif c == "[":  # a class runs to its closing bracket; a bracket first in it is a member
                i += 2 if pattern[i + 1 : i + 2] == "^" else 1
                i += 1
                while i < n and pattern[i] != "]":
                    i += 2 if pattern[i] == "\\" else 1
                i += 1
            else:
                i += 1
        kind, i = _quantifier(pattern, i)
        if kind == "many":
            if inner_rep and not inner_fixed:
                return True
            rep = True
        elif kind == "some":
            rep = rep or inner_rep
        else:
            rep = rep or inner_rep
            fixed = fixed or inner_fixed
    return False


# --- reading the claims --------------------------------------------------------------------------------------------


def _commit_like(token: str) -> bool:
    return any(c.isdigit() for c in token) and any(c in "abcdef" for c in token)


def _names_other_branch(sentence: str, ref: str) -> bool:
    """True when the sentence names a branch other than main and the ref's own (`origin/work`, `pushed to work`)."""
    own = {"main", ref.lower(), ref.lower().split("/", 1)[-1]}
    named = [m.group(1) for m in _REMOTE_BRANCH.finditer(sentence)]
    named += [m.group(1) for m in _TO_BRANCH.finditer(sentence)]
    return any(n.rstrip(".").lower() not in own for n in named)


def _claim_word(sentence: str, words: tuple[str, ...]) -> str | None:
    """The first claim word in the sentence that is a claim, else None."""
    for word in words:
        # a commit may sit inside the phrase: "merged abc1234 into main"
        pattern = r"(?<![\w-])" + r"(?:\s+[0-9a-f]{7,40})?\s+".join(re.escape(w) for w in word.split()) + r"(?![\w-])"
        for m in re.finditer(pattern, sentence, re.IGNORECASE):
            before, after = sentence[: m.start()], sentence[m.end() :]
            if before[-1:] and after[:1] and before[-1] in _QUOTES and after[0] in _QUOTES:
                continue  # quoted: the word is being talked about
            previous = _FOUR.findall(before.lower())[-4:]
            if any(w in _NEGATION | _FUTURE or w.endswith(("n't", "'ll")) for w in previous):
                continue  # "not merged", "isn't pushed", "will be merged", "it'll be on main"
            return re.sub(r"\s+[0-9a-f]{7,40}(?=\s)", "", m.group(0))  # as written, without a commit inside it
    return None


def find_claims(text: str, words: tuple[str, ...], ref: str = DEFAULT_REF) -> list[tuple[str, str]]:
    """[(claim word as written, commit-like token)] for each sentence that has both, in order, without repeats. A
    sentence that names a branch other than main and `ref` is skipped."""
    found: list[tuple[str, str]] = []
    for sentence in _SENTENCE.split(text[:MAX_TEXT]):
        tokens = [t for t in _HEX.findall(sentence) if _commit_like(t)]
        if not tokens or _names_other_branch(sentence, ref):
            continue
        word = _claim_word(sentence, words)
        if word is None:
            continue
        found += [(word, t) for t in dict.fromkeys(tokens) if (word, t) not in found]
    return found


# --- asking git ----------------------------------------------------------------------------------------------------


class Budget:
    """The git time one hook may use, shared by every call it makes."""

    def __init__(self, total: float = GIT_BUDGET_S, clock=time.monotonic) -> None:
        self.total, self.clock, self.start = total, clock, clock()

    def left(self) -> float:
        return self.total - (self.clock() - self.start)


def _git(cwd: str, *args: str, budget: Budget | None = None) -> int | None:
    """The exit code of `git -C cwd args`, or None when git can't run (not installed, or out of time). It gets a bare
    environment: the runner's GIT_DIR and the like must not point it somewhere else, and `GIT_NO_LAZY_FETCH=1` keeps a
    partial clone from going to the network."""
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    budget = budget or Budget()
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, timeout=max(budget.left(), 0.01), env=env, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def _out_of_time(budget: Budget) -> str:
    return f"git took more than {budget.total:g} seconds, so the commit wasn't checked"


def on_ref(cwd: str, sha: str, ref: str, budget: Budget | None = None) -> bool | str:
    """True when `sha` is an ancestor of `ref` in the repository at `cwd`; False when it isn't (or isn't a commit
    there); a text note when that can't be known. `budget` is the git time left for this hook."""
    if not cwd or not Path(cwd).is_dir():
        return "no folder to check in"
    budget = budget or Budget()

    def ask(*args: str) -> int | None:
        return None if budget.left() <= 0 else _git(cwd, *args, budget=budget)

    inside = ask("rev-parse", "--is-inside-work-tree")
    if inside is None:
        return _out_of_time(budget) if budget.left() <= 0 else "git can't run here"
    if inside != 0:
        return "the folder isn't a git repository"
    have = ask("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if have is None and budget.left() <= 0:
        return _out_of_time(budget)
    if have != 0:
        return f"{ref} doesn't exist in this repository"
    found = ask("merge-base", "--is-ancestor", sha, ref)
    if found is None:
        return _out_of_time(budget)
    return found == 0


# --- release claims ------------------------------------------------------------------------------------------------

# `(?<!\s)` lets a run of spaces start a match only at its first space, so a long run is read once, not once for each space
_CLAUSE = re.compile(r"(?<=[.!?])\s+|\n+|(?<!\s)(?:\s*;\s*|\s+(?:but|while|whereas|although)\s+)", re.IGNORECASE)
_LIVE = re.compile(
    r"\b(?:is|are|was|were|went|gone|got|been|now|stays|remains)\s+(?:now\s+)?live\b"
    r"|\blive\s+now\b|\bnow\s+live\b|\blive\s*:"
    r"|\blive\s+(?:release|version|tag)\s+(?:is|=)"
    r"|\bpromoted\b[^;\n]{0,80}?\b(?:to|into)\s+(?:live|prod|production)\b",
    re.IGNORECASE,
)


def _tag_pattern(tags: tuple[str, ...]) -> re.Pattern:
    body = "|".join(f"(?:{t})" for t in tags)
    return re.compile(rf"(?<![\w./-])(?:{body})(?![\w-])(?!\.\w)")


def _quoted_spans(text: str) -> list[tuple[int, int]]:
    """The (open, close) positions of each matched pair of backticks, double quotes or curly double quotes, paired left
    to right. A quote with no partner in the clause (`He typed "v0.3 ...`) makes no span, so it hides nothing."""
    spans: list[tuple[int, int]] = []
    for q in ("`", '"'):
        marks = [i for i, c in enumerate(text) if c == q]
        spans += list(zip(marks[0::2], marks[1::2]))
    i = text.find("“")
    while i != -1:
        j = text.find("”", i + 1)
        if j == -1:
            break
        spans.append((i, j))
        i = text.find("“", j + 1)
    return spans


def _live_claim(clause: str) -> bool:
    """True when the clause says a release is live or promoted, now: not quoted, not negated, not in the future."""
    spans = _quoted_spans(clause)
    for m in _LIVE.finditer(clause):
        if any(a < m.start() < b for a, b in spans):
            continue
        previous = _FOUR.findall(clause[: m.start()].lower())[-4:]
        if any(w in _NEGATION | _FUTURE or w.endswith(("n't", "'ll")) for w in previous):
            continue
        after = _FOUR.findall(clause[m.end() :].lower())[:2]
        if any(w in _FUTURE for w in after):
            continue  # "live after the next promote"
        return True
    return False


def release_claims(text: str, tags: tuple[str, ...]) -> list[str]:
    """The release tags named in each clause that says a release is live, in order, without repeats."""
    pattern = _tag_pattern(tags)
    # a comma also ends a clause when a new one starts: "v0.3 is staged, v0.2 is live"
    new_clause = re.compile(rf",\s+(?=(?:and\s+|then\s+)?{pattern.pattern}\s+(?:is|are|was|went|now)\b)", re.IGNORECASE)
    found: list[str] = []
    for clause in (c for part in _CLAUSE.split(text[:MAX_TEXT]) if part for c in new_clause.split(part)):
        if not clause or not _live_claim(clause):
            continue
        found += [t for t in dict.fromkeys(m.group(0) for m in pattern.finditer(clause)) if t not in found]
    return found


def promoted_release(release_dir: str) -> str | None:
    """The name of the folder `release_dir/current` points at, or None when `current` is missing, isn't a link, points
    at nothing or can't be read. Reads the link and nothing else."""
    link = Path(os.path.expanduser(release_dir)) / "current"
    try:
        if not link.is_symlink() or not link.exists():
            return None
        target = os.readlink(link)
    except OSError:
        return None
    return Path(target.rstrip("/")).name or None


def _same_release(tag: str, promoted: str) -> bool:
    return tag.lower().removeprefix("v") == promoted.lower().removeprefix("v")


def check_releases(text: str, cfg: Config) -> str | None:
    """The deny reason when the message says a release is live and it isn't the one promoted, else None."""
    named = release_claims(text, cfg.release_tags) if cfg.release_dir else []
    if not named:
        return None
    promoted = promoted_release(cfg.release_dir)
    if promoted is None:
        return (
            f"the message says {', '.join(named)} is live but {cfg.release_dir}/current is missing, isn't a link, "
            "points at nothing or can't be read here. Name the commit on the ref, or the promoted release, and send "
            "it again."
        )
    wrong = [t for t in named if not _same_release(t, promoted)]
    if not wrong:
        return None
    return (
        f"the message says {', '.join(wrong)} is live but {cfg.release_dir}/current points at {promoted}. "
        "Name the commit on the ref, or the promoted release, and send it again with what is true."
    )


# --- the guard -----------------------------------------------------------------------------------------------------


def judge(call, cfg: Config, ctx) -> Finding | None:
    if ctx.event == "stop":
        return _log(cfg, ctx)
    text = call.data.get("text", "")
    claims = find_claims(text, cfg.words, cfg.ref) if isinstance(text, str) else []
    wrong, notes = [], []
    release = check_releases(text, cfg) if isinstance(text, str) else None
    budget = Budget()
    for word, sha in claims:
        result = on_ref(ctx.cwd, sha, cfg.ref, budget)
        if result is True:
            continue
        if result is False:
            wrong.append(f"{sha} ({word})")
        elif result not in notes:
            notes.append(result)
    if wrong:
        reason = (
            f"the message says {', '.join(wrong)} but that commit isn't on {cfg.ref} here. "
            f"If {cfg.ref} may be out of date, run `git fetch` first. Check with `git branch -r --contains SHA`, then "
            "send it again with what is true."
        )
        return Finding("deny", f"{reason} Also: {release}" if release else reason)
    if release:
        return Finding("deny", release)
    if notes:
        return Finding("note", f"claims not checked: {notes[0]}")
    return None


def _log(cfg: Config, ctx) -> Finding | None:
    """On stop: append the claims in the runner's last message to the claims log."""
    text = ctx.payload.get("last_assistant_message")
    claims = find_claims(text, cfg.words, cfg.ref) if isinstance(text, str) else []
    if not claims:
        return None
    budget = Budget()
    now = ctx.now()  # one clock read feeds both `ts` and the legacy `at`
    stamp = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")
    rec = {
        "at": stamp,
        "runner": ctx.runner,
        "session": ctx.session_id,
        "ref": cfg.ref,
        "claims": [{"word": w, "commit": s, "on_ref": on_ref(ctx.cwd, s, cfg.ref, budget)} for w, s in claims],
    }
    try:
        jsonl.append(
            (ctx.home if ctx.home is not None else ws_home()) / "logs" / LOG,
            jsonl.stamped(rec, SCHEMA_VERSION, now),
        )
    except OSError as e:
        return Finding("warn", f"claims could not write {LOG} ({type(e).__name__})")
    return None
