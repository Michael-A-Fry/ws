"""claims: a message that says a commit is merged, pushed or on main names a commit that is. Before a message goes to
another agent, each claim word (default `merged`, `pushed`, `on main`) that sits in the same sentence as a commit-like
token (7 to 40 lower-case hex characters, with at least one digit and one of a to f) is checked with
`git merge-base --is-ancestor TOKEN REF` in the session's folder (default REF `origin/main`). A commit that isn't an
ancestor of REF, or isn't a commit in this repository, denies the message and the reason names it.

Not a claim: a claim word in quotes or backticks, and one with a negation or a future form in the four words before it
(`not merged`, `will be pushed`, `once it's merged`). A folder that isn't a repository, no git on the machine, or a REF
that doesn't exist here lets the message through with a note.

When the session stops, the claims in the runner's last message (if the stop payload carries it) are appended to
`$WS_HOME/logs/claims.jsonl` with whether each commit was on REF. That is a log only; nothing is stopped.
"""

from __future__ import annotations

import os
import re
import subprocess
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

DEFAULT_WORDS = ("merged", "pushed", "on main")
DEFAULT_REF = "origin/main"
LOG = "claims.jsonl"
GIT_TIMEOUT_S = 10
MAX_TEXT = 20000  # a message is read to this length

_HEX = re.compile(r"(?<![\w-])[0-9a-f]{7,40}(?![\w-])")
_SENTENCE = re.compile(r"(?<=[.!?])\s+|\n+")
_QUOTES = "\"'`‘’“”"
_NEGATION = {"not", "no", "never", "without"}
_FUTURE = {"will", "shall", "would", "should", "going", "gonna", "be", "if", "once", "when", "until", "unless"}
_FOUR = re.compile(r"[a-z]+(?:'[a-z]+)?")


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


def _claim_word(sentence: str, words: tuple[str, ...]) -> str | None:
    """The first claim word in the sentence that is a claim, else None."""
    for word in words:
        pattern = r"(?<![\w-])" + r"\s+".join(re.escape(w) for w in word.split()) + r"(?![\w-])"
        for m in re.finditer(pattern, sentence, re.IGNORECASE):
            before, after = sentence[: m.start()], sentence[m.end() :]
            if before[-1:] in _QUOTES and after[:1] in _QUOTES:
                continue  # quoted: the word is being talked about
            previous = _FOUR.findall(before.lower())[-4:]
            if any(w in _NEGATION | _FUTURE or w.endswith(("n't", "'ll")) for w in previous):
                continue  # "not merged", "isn't pushed", "will be merged", "it'll be on main"
            return m.group(0)
    return None


def find_claims(text: str, words: tuple[str, ...]) -> list[tuple[str, str]]:
    """[(claim word as written, commit-like token)] for each sentence that has both, in order, without repeats."""
    found: list[tuple[str, str]] = []
    for sentence in _SENTENCE.split(text[:MAX_TEXT]):
        tokens = [t for t in _HEX.findall(sentence) if _commit_like(t)]
        if not tokens:
            continue
        word = _claim_word(sentence, words)
        if word is None:
            continue
        found += [(word, t) for t in dict.fromkeys(tokens) if (word, t) not in found]
    return found


# --- asking git ----------------------------------------------------------------------------------------------------


def _git(cwd: str, *args: str) -> int | None:
    """The exit code of `git -C cwd args`, or None when git can't run (not installed, or it timed out). It gets a bare
    environment: the runner's GIT_DIR and the like must not point it somewhere else."""
    env = {k: os.environ[k] for k in ("PATH", "HOME") if k in os.environ}
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        done = subprocess.run(
            ["git", "-C", cwd, *args], capture_output=True, timeout=GIT_TIMEOUT_S, env=env, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return done.returncode


def on_ref(cwd: str, sha: str, ref: str) -> bool | str:
    """True when `sha` is an ancestor of `ref` in the repository at `cwd`; False when it isn't (or isn't a commit
    there); a text note when that can't be known."""
    if not cwd or not Path(cwd).is_dir():
        return "no folder to check in"
    inside = _git(cwd, "rev-parse", "--is-inside-work-tree")
    if inside is None:
        return "git can't run here"
    if inside != 0:
        return "the folder isn't a git repository"
    have = _git(cwd, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if have != 0:
        return f"{ref} doesn't exist in this repository"
    return _git(cwd, "merge-base", "--is-ancestor", sha, ref) == 0


# --- the guard -----------------------------------------------------------------------------------------------------


def judge(call, cfg: Config, ctx) -> Finding | None:
    if ctx.event == "stop":
        return _log(cfg, ctx)
    text = call.data.get("text", "")
    claims = find_claims(text, cfg.words) if isinstance(text, str) else []
    wrong, notes = [], []
    for word, sha in claims:
        result = on_ref(ctx.cwd, sha, cfg.ref)
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
            f"Check with `git branch -r --contains SHA`, then send it again with what is true.",
        )
    if notes:
        return Finding("note", f"claims not checked: {notes[0]}")
    return None


def _log(cfg: Config, ctx) -> Finding | None:
    """On stop: append the claims in the runner's last message to the claims log."""
    text = ctx.payload.get("last_assistant_message")
    claims = find_claims(text, cfg.words) if isinstance(text, str) else []
    if not claims:
        return None
    stamp = datetime.fromtimestamp(ctx.now(), UTC).isoformat(timespec="seconds")
    rec = {
        "at": stamp,
        "runner": ctx.runner,
        "session": ctx.session_id,
        "ref": cfg.ref,
        "claims": [{"word": w, "commit": s, "on_ref": on_ref(ctx.cwd, s, cfg.ref)} for w, s in claims],
    }
    try:
        jsonl.append((ctx.home if ctx.home is not None else ws_home()) / "logs" / LOG, rec)
    except OSError as e:
        return Finding("warn", f"claims could not write {LOG} ({type(e).__name__})")
    return None
