"""claims: a message that says a commit is merged, pushed or on the default branch names a commit that is. Before a
message goes to another agent, each claim word (default `on B`, `merged into B`, `merged to B` and `pushed to B`, where B
is the repository's default branch) is matched to the commit-like tokens (7 to 40 lower-case hex characters, with at
least one digit and one of a to f) it is about, and each such commit is checked with `git merge-base --is-ancestor TOKEN
REF` in the session's folder. A token belongs to a claim word when it sits inside the phrase (`merged abc1234 into main`)
or next to it with nothing between but a few plain function words and other commit ids (`abc1234 is on main`, `pushed the
fix abc1234 to main`, `merged into main: abc1234 and def5678`). Any other word between them means the claim is about
something else, so a hex-looking word (`the ed25519 key is on main`), a session id, or a draft commit in the same
sentence as `on main` (`abc1234 is a draft and the fix is on main`) is not checked. A claim word the policy adds, such as
`pushed`, is matched the same way, so `pushed work abc1234` (a branch name between) is not a claim about the default
branch. REF is the policy's `ref`, else the default branch on `origin`. B and that REF come from
`git symbolic-ref refs/remotes/origin/HEAD`; with no `origin/HEAD` they come from the local default branch
(`init.defaultBranch`, else `main`), taking `origin/B` when that exists and B itself when it doesn't. A commit that isn't
an ancestor of REF, or isn't a commit in this repository, denies the message and the reason names it; the reason says to
`git fetch` first when REF may be out of date.

Not a claim: a claim word in quotes or backticks, one with a negation or a future form in the four words before it
(`not merged`, `will be pushed`, `once it's merged`), and any sentence that names another branch (`origin/work`, or
`pushed to work`, `merged into work`), because the check is against one ref. A folder that isn't a repository, no git on
the machine, or a REF that doesn't exist here, lets the message through with a note: there is nothing to check against.
Git that is there but can't answer is different: git taking more than 8 seconds in total for one message, or failing to
start, denies a message that makes a claim, and the reason names the time-out (the gate fails closed, ADR 0017: "can't
tell" is not "true"). If the time ran out before the default branch was known, any phrase of the shape `on B`, `merged
into B`, `merged to B` or `pushed to B` that a commit binds to counts as a claim. Git runs without lazy fetching
(`GIT_NO_LAZY_FETCH=1`), so a check never goes to the network.

Release claims are off until `release_dir` is set: a folder whose `current` link points at the promoted release. A clause
(a sentence cut at `;`, `but`, `while`, `whereas`) that says a release is live or promoted (`v0.2 is live`, `promoted
v0.2 to live`, `live now: v0.2`) must name only the release `current` points at. A release is a tag whose shape is one of
`release_tags` (regexes, default `v1.2` and `v1.2.3`) and it matches the link's target folder name, with or without a
leading `v`. A clause that names another release is denied, and so is a live claim when `current` is missing, isn't a
link or can't be read. The same quote, negation and future rules apply (`goes live`, `will be live`, `once promoted`
aren't claims). It reads the link and nothing else: no fetch, no network, nothing written.

When the session stops, the claims in the runner's last message (if the stop payload carries it) are appended to
`<state>/logs/claims.jsonl` with whether each commit was on REF. That is a log only; nothing is stopped. Each line
carries `schema` and `ts`, and `at` (the legacy spelling of `ts`) beside them.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from ws import jsonl, paths
from ws.guards import Finding, known_keys, text_list
from ws.policy import PolicyError

NAME = "claims"
EVENTS = ("pre", "stop")
KINDS = ("message",)
PRIORITY = 30

FALLBACK_BRANCH = "main"  # the default branch of a repository that doesn't say (no origin/HEAD, no init.defaultBranch)
REMOTE = "origin"
DEFAULT_REF = f"{REMOTE}/{FALLBACK_BRANCH}"
DEFAULT_TAGS = (r"v\d+\.\d+(?:\.\d+)*",)
MAX_TAG_PATTERN = 200
LOG = "claims.jsonl"
SCHEMA_VERSION = 1
GIT_BUDGET_S = 8.0  # all the git time one hook may use
MAX_TEXT = 20000  # a message is read to this length


def claim_words(branch: str) -> tuple[str, ...]:
    """The claim phrases for a default branch."""
    return (f"on {branch}", f"merged into {branch}", f"merged to {branch}", f"pushed to {branch}")


DEFAULT_WORDS = claim_words(FALLBACK_BRANCH)
_REF_SHAPE = re.compile(r"[A-Za-z0-9_./@{}^~-]+")

_HEX = re.compile(r"(?<![\w-])[0-9a-f]{7,40}(?![\w-])")
# The words that may sit between a claim word and the commit it is about. Any other word there (a noun, a name) means the
# commit is mentioned for another reason.
_FILLER = frozenset(
    [
        "a",
        "an",
        "the",
        "this",
        "that",
        "these",
        "those",
        "it",
        "its",
        "they",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "has",
        "have",
        "had",
        "now",
        "already",
        "also",
        "just",
        "finally",
        "then",
        "still",
        "safely",
        "successfully",
        "both",
        "all",
        "and",
        "which",
        "who",
        "commit",
        "commits",
        "sha",
        "shas",
        "hash",
        "hashes",
        "change",
        "changes",
        "fix",
        "fixes",
        "patch",
        "patches",
        "pr",
        "really",
        "currently",
        "yet",
        "indeed",
    ]
)
_LEAD = frozenset(
    [
        "in",
        "at",
        "of",
        "see",
        "via",
        "from",
        "with",
        "as",
        "by",
        "for",
        "on",
        "into",
        "onto",
        "to",
        "after",
        "before",
        "since",
    ]
)
MAX_GAP_WORDS = 4
_GAP_BREAK = re.compile(r"[.;!?]")
_GAP_WORD = re.compile(r"[A-Za-z']+|\d+")
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
    words: tuple[str, ...] = ()  # () = the phrases for the default branch
    ref: str = ""  # "" = the default branch on origin
    release_dir: str = ""  # "" = release claims are off
    release_tags: tuple[str, ...] = DEFAULT_TAGS


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("words", "ref", "release_dir", "release_tags"))
    words = text_list(table, "words", where)
    if "words" in table and not words:
        raise PolicyError(f"{where}: words must name at least one claim word")
    ref = table.get("ref", "")
    if "ref" in table and not _ref_ok(ref):
        raise PolicyError(f"{where}: ref must be a branch, tag or remote branch name such as origin/main")
    return Config(words, ref, _release_dir(table, where), _release_tags(table, where))


def _ref_ok(ref: object) -> bool:
    return isinstance(ref, str) and _REF_SHAPE.fullmatch(ref) is not None and not ref.startswith("-")


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


def _names_other_branch(sentence: str, ref: str, branch: str = FALLBACK_BRANCH) -> bool:
    """True when the sentence names a branch other than the default and the ref's own (`origin/work`, `pushed to
    work`)."""
    own = {branch.lower(), ref.lower(), ref.lower().split("/", 1)[-1]}
    named = [m.group(1) for m in _REMOTE_BRANCH.finditer(sentence)]
    named += [m.group(1) for m in _TO_BRANCH.finditer(sentence)]
    return any(n.rstrip(".").lower() not in own for n in named)


def _claim_spans(sentence: str, words: tuple[str, ...]) -> list[tuple[str, int, int]]:
    """[(claim word as written, start, end)] for each place in the sentence a claim word is a claim: not quoted, not
    negated and not in the future."""
    found: list[tuple[str, int, int]] = []
    for word in words:
        parts = [f"({re.escape(w)})" for w in word.split()]  # a group for each word, to give the phrase back as written
        # a commit may sit inside the phrase ("merged abc1234 into main"), and so may a few plain words and a commit
        # after its verb ("pushed the fix abc1234 to main")
        inside = r"(?:\s+[0-9a-f]{7,40})?\s+"
        verb = rf"(?:\s+(?:[0-9a-f]{{7,40}}|{'|'.join(sorted(_FILLER))})){{0,{MAX_GAP_WORDS + 1}}}\s+"
        pattern = (
            r"(?<![\w-])"
            + (parts[0] + verb + inside.join(parts[1:]) if len(parts) > 2 else inside.join(parts))
            + r"(?![\w-])"
        )
        for m in re.finditer(pattern, sentence, re.IGNORECASE):
            before, after = sentence[: m.start()], sentence[m.end() :]
            if before[-1:] and after[:1] and before[-1] in _QUOTES and after[0] in _QUOTES:
                continue  # quoted: the word is being talked about
            previous = _FOUR.findall(_HEX.sub(" ", before.lower()))[-4:]  # a commit id's letters are not words
            if any(w in _NEGATION | _FUTURE or w.endswith(("n't", "'ll")) for w in previous):
                continue  # "not merged", "isn't pushed", "will be merged", "it'll be on main"
            written = " ".join(m.groups())  # as written, without a commit or a plain word inside it
            found.append((written, m.start(), m.end()))
    return sorted(found, key=lambda f: f[1])


def _plain_gap(gap: str) -> bool:
    """Is the text between a claim word and a commit only function words (and other commit ids, as in a list)?"""
    if _GAP_BREAK.search(gap):
        return False
    words = _GAP_WORD.findall(_HEX.sub(" ", gap).lower())
    return len(words) <= MAX_GAP_WORDS and all(w in _FILLER for w in words)


def _binds(sentence: str, span: tuple[str, int, int], token: re.Match) -> bool:
    """Is this commit-like token what the claim word at `span` is about?"""
    _, start, end = span
    if start <= token.start() and token.end() <= end:
        return True
    if token.end() <= start:  # the commit comes first: `abc1234 is on main`; what leads into it must be plain too
        return _plain_gap(sentence[token.end() : start]) and _plain_lead(sentence[: token.start()])
    return _plain_gap(sentence[end : token.start()])


def _plain_lead(before: str) -> bool:
    """Is the word before a commit-like token one that leads into a commit (`the fix abc1234`, `see abc1234`), and not
    a name for something else (`session 9f8e7d6c`, `key ed25519`)? The start of the sentence is plain."""
    words = _GAP_WORD.findall(before.lower())
    return not words or words[-1] in _FILLER or words[-1] in _LEAD


def _has_commit(text: str) -> bool:
    return any(_commit_like(t) for t in _HEX.findall(text[:MAX_TEXT]))


def find_claims(
    text: str, words: tuple[str, ...], ref: str = DEFAULT_REF, branch: str = FALLBACK_BRANCH
) -> list[tuple[str, str]]:
    """[(claim word as written, commit-like token)] for each claim word and the commit it is about (see the module
    docstring), in order, without repeats. A sentence that names a branch other than the default `branch` and `ref` is
    skipped."""
    found: list[tuple[str, str]] = []
    for sentence in _SENTENCE.split(text[:MAX_TEXT]):
        tokens = [m for m in _HEX.finditer(sentence) if _commit_like(m.group(0))]
        if not tokens or _names_other_branch(sentence, ref, branch):
            continue
        for span in _claim_spans(sentence, words):
            for token in tokens:
                pair = (span[0], token.group(0))
                if pair not in found and _binds(sentence, span, token):
                    found.append(pair)
    return found


# --- asking git ----------------------------------------------------------------------------------------------------


class Budget:
    """The git time one hook may use, shared by every call it makes. `failed` is set when git was found but a call to it
    timed out or wouldn't start: from then on the answer for a claim is "not known", which the guard denies."""

    def __init__(self, total: float = GIT_BUDGET_S, clock=time.monotonic) -> None:
        self.total, self.clock, self.start = total, clock, clock()
        self.failed = False

    def left(self) -> float:
        return self.total - (self.clock() - self.start)

    def spent(self) -> bool:
        """Is git out of time, or did a call to it just fail?"""
        return self.failed or self.left() <= 0


class OutOfTime(str):
    """The note `on_ref` gives when git was there and didn't answer (out of time, or it wouldn't run). The guard denies
    it rather than let a claim through unchecked."""


def _git_run(cwd: str, *args: str, budget: Budget | None = None) -> subprocess.CompletedProcess | None:
    """`git -C cwd args`, or None when git can't run (not installed; or out of time, or it wouldn't start, which set
    `budget.failed`). It gets a bare environment: the
    runner's GIT_DIR and the like must not point it somewhere else, and `GIT_NO_LAZY_FETCH=1` keeps a partial clone from
    going to the network."""
    env = {"PATH": paths.tool_path()}  # never the session's PATH: a project can put its own program first
    if "HOME" in os.environ:
        env["HOME"] = os.environ["HOME"]
    env["GIT_OPTIONAL_LOCKS"] = "0"
    env["GIT_NO_LAZY_FETCH"] = "1"
    budget = budget or Budget()
    exe = shutil.which("git", path=env["PATH"])
    if exe is None:  # never a bare name: an empty PATH entry is the folder the session works in
        return None
    try:
        return subprocess.run(
            [exe, "-C", cwd, *args],
            capture_output=True,
            timeout=max(budget.left(), 0.01),
            env=env,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):  # a time-out is a SubprocessError
        budget.failed = True
        return None


def _git(cwd: str, *args: str, budget: Budget | None = None) -> int | None:
    """The exit code of `git -C cwd args`, or None when git can't run."""
    done = _git_run(cwd, *args, budget=budget)
    return None if done is None else done.returncode


def _git_text(cwd: str, *args: str, budget: Budget) -> str | None:
    """The one line `git -C cwd args` prints when it succeeds, else None."""
    if budget.left() <= 0:
        return None
    done = _git_run(cwd, *args, budget=budget)
    if done is None or done.returncode != 0 or not isinstance(done.stdout, bytes):
        return None
    return done.stdout.decode("utf-8", "replace").strip() or None


@dataclass(frozen=True)
class Target:
    ref: str  # what a commit must be an ancestor of
    branch: str  # the repository's default branch: the one the claim words are about


def default_target(cwd: str, budget: Budget) -> Target:
    """The default branch and the ref to check against, for the repository at `cwd`.

    From `origin/HEAD` when the repository has one (`git symbolic-ref refs/remotes/origin/HEAD`). Otherwise the local
    default branch: `init.defaultBranch`, else `main`; its remote-tracking `origin/B` is the ref when that exists, and B
    itself when it doesn't. A folder that isn't a repository, no git or no time left gives `main` and `origin/main`, and
    the check that follows says why it can't go on."""
    fallback = Target(DEFAULT_REF, FALLBACK_BRANCH)
    if not cwd or not Path(cwd).is_dir():
        return fallback
    head = _git_text(cwd, "symbolic-ref", "--short", f"refs/remotes/{REMOTE}/HEAD", budget=budget)
    prefix = f"{REMOTE}/"
    if head and head.startswith(prefix) and _ref_ok(head) and len(head) > len(prefix):
        return Target(head, head.removeprefix(prefix))
    named = _git_text(cwd, "config", "--get", "init.defaultBranch", budget=budget)
    branch = named if named and _ref_ok(named) else FALLBACK_BRANCH
    if budget.left() <= 0:
        return fallback
    remote = f"{prefix}{branch}"
    if _git(cwd, "rev-parse", "--verify", "--quiet", f"{remote}^{{commit}}", budget=budget) == 0:
        return Target(remote, branch)
    return Target(branch, branch)


def _out_of_time(budget: Budget) -> OutOfTime:
    return OutOfTime(f"git took more than {budget.total:g} seconds or wouldn't run, so the commit wasn't checked")


def on_ref(cwd: str, sha: str, ref: str, budget: Budget | None = None) -> bool | str:
    """True when `sha` is an ancestor of `ref` in the repository at `cwd`; False when it isn't (or isn't a commit
    there); a text note when that can't be known (an `OutOfTime` when git was there and didn't answer). `budget` is the
    git time left for this hook."""
    if not cwd or not Path(cwd).is_dir():
        return "no folder to check in"
    budget = budget or Budget()

    def ask(*args: str) -> int | None:
        return None if budget.left() <= 0 else _git(cwd, *args, budget=budget)

    inside = ask("rev-parse", "--is-inside-work-tree")
    if inside is None:
        return _out_of_time(budget) if budget.spent() else "git can't run here"
    if inside != 0:
        return "the folder isn't a git repository"
    have = ask("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if have is None:
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
    r"\b(?:is|are|was|were|went|gone|got|been|now|stays|remains)\s+(?:now\s+)?live(?![\w-])"
    r"|\blive\s+now\b|\bnow\s+live(?![\w-])|\blive\s*:"
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
    budget = Budget()
    ref, claims = "", []
    if isinstance(text, str) and _has_commit(text):  # a message with no commit-like token never asks git
        ref, claims = _claims_in(text, cfg, ctx.cwd, budget)
        if budget.spent():  # git didn't answer before the default branch was known: more phrases may be claims
            claims += [c for c in blind_claims(text, cfg) if c not in claims]
    wrong, late, notes = [], [], []
    release = check_releases(text, cfg) if isinstance(text, str) else None
    for word, sha in claims:
        result = _out_of_time(budget) if budget.spent() else on_ref(ctx.cwd, sha, ref, budget)
        if result is True:
            continue
        if result is False:
            wrong.append(f"{sha} ({word})")
        elif isinstance(result, OutOfTime):
            late.append(f"{sha} ({word})")
        elif result not in notes:
            notes.append(result)
    if wrong:
        reason = (
            f"the message says {', '.join(wrong)} but that commit isn't on {ref} here. "
            f"If {ref} may be out of date, run `git fetch` first. Check with `git branch -r --contains SHA`, then "
            "send it again with what is true."
        )
        return Finding("deny", f"{reason} Also: {release}" if release else reason)
    if release:
        return Finding("deny", release)
    if late:
        named = ", ".join(late)
        return Finding("deny", f"claims not checked: {_out_of_time(budget)}. The message says {named}. {LATE_ADVICE}")
    if notes:
        return Finding("note", f"claims not checked: {notes[0]}")
    return None


LATE_ADVICE = (
    "The gate denies a claim it can't check. Send it again once `git status` answers here, or without the claim."
)
# the word after "on", "into" or "to" in a claim phrase, when the default branch isn't known
_BRANCH_AFTER = re.compile(r"\b(?:on|into|to)\s+(?:the\s+)?(?:origin/)?([\w][\w./-]*)", re.IGNORECASE)
_NOT_A_BRANCH = frozenset(
    {"a", "an", "the", "my", "your", "our", "his", "her", "their", "this", "that", "it", "me", "you"}
)
MAX_GUESSES = 6


def blind_claims(text: str, cfg: Config) -> list[tuple[str, str]]:
    """The claims in `text` when git ran out of time before it could name the default branch: the claim phrases for each
    word that follows `on`, `into` or `to` (the first few), unless the policy names its own `words`. Used only to decide
    that something is a claim that can't be checked, never to say it is true or false."""
    if cfg.words:
        return []
    names: list[str] = []
    for m in _BRANCH_AFTER.finditer(text[:MAX_TEXT]):
        name = m.group(1).rstrip(".,:;)").lower()
        if name and name not in _NOT_A_BRANCH and name not in names:
            names.append(name)
    found: list[tuple[str, str]] = []
    for name in names[:MAX_GUESSES]:
        found += [c for c in find_claims(text, claim_words(name), f"{REMOTE}/{name}", name) if c not in found]
    return found


def _claims_in(text: str, cfg: Config, cwd: str, budget: Budget) -> tuple[str, list[tuple[str, str]]]:
    """(the ref to check against, the claims in `text`). The policy's `words` and `ref` win; what they leave out comes
    from the repository's default branch."""
    target = default_target(cwd, budget)
    ref = cfg.ref or target.ref
    words = cfg.words or claim_words(target.branch)
    return ref, find_claims(text, words, ref, target.branch)


def _log(cfg: Config, ctx) -> Finding | None:
    """On stop: append the claims in the runner's last message to the claims log."""
    text = ctx.payload.get("last_assistant_message")
    budget = Budget()
    ref, claims = _claims_in(text, cfg, ctx.cwd, budget) if isinstance(text, str) and _has_commit(text) else ("", [])
    if not claims:
        return None
    now = ctx.now()  # one clock read feeds both `ts` and the legacy `at`
    stamp = datetime.fromtimestamp(now, UTC).isoformat(timespec="seconds")
    rec = {
        "at": stamp,
        "runner": ctx.runner,
        "session": ctx.session_id,
        "ref": ref,
        "claims": [{"word": w, "commit": s, "on_ref": on_ref(ctx.cwd, s, ref, budget)} for w, s in claims],
    }
    try:
        jsonl.append(
            (ctx.home if ctx.home is not None else paths.state_dir()) / "logs" / LOG,
            jsonl.stamped(rec, SCHEMA_VERSION, now),
        )
    except OSError as e:
        return Finding("warn", f"claims could not write {LOG} ({type(e).__name__})")
    return None
