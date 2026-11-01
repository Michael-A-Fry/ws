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

When the session stops, the claims in the runner's last message (if the stop payload carries it) are appended to
`$WS_HOME/logs/claims.jsonl` with whether each commit was on REF. That is a log only; nothing is stopped.
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
LOG = "claims.jsonl"
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


def config(table: dict, where: str) -> Config:
    known_keys(table, where, ("words", "ref"))
    words = text_list(table, "words", where, DEFAULT_WORDS)
    if not words:
        raise PolicyError(f"{where}: words must name at least one claim word")
    ref = table.get("ref", DEFAULT_REF)
    if not isinstance(ref, str) or not re.fullmatch(r"[A-Za-z0-9_./@{}^~-]+", ref) or ref.startswith("-"):
        raise PolicyError(f"{where}: ref must be a branch, tag or remote branch name such as origin/main")
    return Config(words, ref)


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


# --- the guard -----------------------------------------------------------------------------------------------------


def judge(call, cfg: Config, ctx) -> Finding | None:
    if ctx.event == "stop":
        return _log(cfg, ctx)
    text = call.data.get("text", "")
    claims = find_claims(text, cfg.words, cfg.ref) if isinstance(text, str) else []
    wrong, notes = [], []
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
        return Finding(
            "deny",
            f"the message says {', '.join(wrong)} but that commit isn't on {cfg.ref} here. "
            f"If {cfg.ref} may be out of date, run `git fetch` first. Check with `git branch -r --contains SHA`, then "
            "send it again with what is true.",
        )
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
    stamp = datetime.fromtimestamp(ctx.now(), UTC).isoformat(timespec="seconds")
    rec = {
        "at": stamp,
        "runner": ctx.runner,
        "session": ctx.session_id,
        "ref": cfg.ref,
        "claims": [{"word": w, "commit": s, "on_ref": on_ref(ctx.cwd, s, cfg.ref, budget)} for w, s in claims],
    }
    try:
        jsonl.append((ctx.home if ctx.home is not None else ws_home()) / "logs" / LOG, rec)
    except OSError as e:
        return Finding("warn", f"claims could not write {LOG} ({type(e).__name__})")
    return None
