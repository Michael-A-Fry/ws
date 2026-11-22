"""`ws triage run`: the inbox job, rules first, one small model call per thread, drafts only.

    ws triage run --job NAME [--dry-run]     the settings are the `[triage]` table of the job file `jobs/NAME.toml`
                                             (`command = "triage"`, so a `schedule` runs it; see ws.jobs)
    ws triage run --config FILE [--dry-run]  the same settings from any TOML file (its `[triage]` table, or the whole
                                             file when it has none)

Why it is shaped like this: most mail needs no model, and a model that reads mail is steerable by it. So the job is a
workflow with programmatic gates (Anthropic, "Building effective agents", read 22 Nov 2026: use a workflow, with
programmatic checks, where the steps are known) and the model gets one narrow step:

  1. Fetch, no model. Per mailbox, the messages newer than the last uid seen (a state file, `triage/NAME.json` in the
     state folder). Nothing new is exit 0 with no model call.
  2. Rules, no model. A plain TOML file maps a sender, a domain, bulk-mail headers (List-Unsubscribe, List-Id,
     Precedence, Auto-Submitted) and subject patterns to one of `ws.mail.LABELS` or to `ask-model`, which is a state and
     never a label. The first rule that matches wins and its id is logged. A rule only labels: it never drafts.
  3. Model, for `ask-model` threads only: one call per thread, no tools, the thread trimmed to the newest messages
     inside a token cap, plus the person's voice notes. The answer must parse to `{needs_reply, reason, draft}`; two
     retries for a bad answer, then the thread is flagged, not drafted. The model is config: the `claude-code` runner
     with a small model by default (no key, but ~31k tokens in a call, mostly Claude Code's own context), `anthropic-api`
     (`DirectModel`: one Messages API call, the key leased through the broker, ~2k tokens in), or an OpenCode local or
     API model, by changing `[triage.model]`. A run is capped in calls, tokens and money.
  4. Checks, no model. A draft that names an amount, a date or a time the thread does not carry, promises a payment,
     holds a link or address the thread does not carry, opens with "Dear" or is not 2 to 6 sentences is refused and
     flagged with the reason. A thread that asks for a time is drafted without one and flagged "scheduling: check your
     calendar" (ws has no calendar source yet).
  5. Output, through `ws.mail`: the label and the draft (his Drafts folder). One line per run in `logs/triage.jsonl`
     (counts, tokens, cost, rule ids: never mail text) and, when `summary_dir` is set, `summary-DATE.md` for the day.

Nothing is sent, moved or deleted: this module calls only `ws.mail`'s search, fetch, label and draft. `--dry-run` makes
no label or draft call, saves no state and writes no summary (it still makes the model calls, so it measures what a
real run costs). A mailbox whose `class` names a data class that is not `full` access in the policy is refused before
any mail is read (a walled area's mail is not this job's). Mail is untrusted input: the model's answer is data, parsed
and checked, never acted on beyond the label and the draft.

Exit: 0 ran (flags are in the summary, not the exit), 1 a mailbox or the model failed, 2 the settings could not be
read, 3 refused (a walled mailbox, or the job lock held is a quiet 0).

Swap note: the model is `[triage.model]` (runner, model, runner_options) and nothing else in this module names a
provider (`make_model` picks the adapter); the mail route is `MailAdapter`, the one place that calls ws.mail (and two of its private reads, marked).
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import io
import json
import os
import re
import tempfile
import tomllib
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from email import policy as _policy
from email.parser import BytesParser
from email.utils import parseaddr
from pathlib import Path
from typing import Protocol

from ws import broker, joblock, jobs, jsonl, mail, output, paths, policy, runrecord
from ws import secrets as wsecrets
from ws.minters import imap
from ws.output import WsError
from ws.secrets import HOST_RE, NAME_RE

COMMAND = "triage"
SCHEMA_VERSION = 1  # of a line in logs/triage.jsonl and of the state file
ASK_MODEL = "ask-model"  # a rule's answer that is a state, never a label
RULE_LABELS = (*mail.LABELS, ASK_MODEL)
LABEL_REPLY, LABEL_FYI = "ws/needs-reply", "ws/fyi"
SCHEDULING_FLAG = "scheduling: check your calendar"
MAX_RETRIES = 2  # for a bad answer
MAX_DECISIONS = 200  # decision lines kept in one run's record
MAX_PENDING = 100  # uids held for the next run when a cap stopped the model
MAX_FLAGS = 25  # flagged threads kept in a day's summary
REASON_MAX = 200
VOICE_MAX = 3000  # characters of the person's voice notes sent with each call
CHARS_PER_TOKEN = 4  # the cap on a thread is counted this way: a cap is a bound, not a bill
FOLDER_NAME = "triage"
DIRECT = "anthropic-api"  # the runner name of the direct Messages API call (see DirectModel)
DIRECT_OPTIONS = {"max_tokens", "price_in_usd_per_mtok", "price_out_usd_per_mtok"}
API_VERSION = "2023-06-01"
MAX_REPLY_BYTES = 1024 * 1024
EXIT_FAILED, EXIT_USAGE, EXIT_REFUSED = 1, 2, 3
_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,59}$")


class TriageError(WsError):
    """The settings, rules or state could not be used. `kind` is `bad_triage`, `refused` or one named below."""


def _bad(message: str, hint: str = "") -> TriageError:
    return TriageError("bad_triage", message, hint, EXIT_USAGE)


# ---------------------------------------------------------------- settings


@dataclass(frozen=True)
class ModelConfig:
    runner: str = "claude-code"
    model: str = "haiku"
    runner_options: dict = field(default_factory=dict)
    budget_usd: float = 0.10  # the most one run spends on the model, all its calls together
    run_tokens: int = 400_000  # the most tokens (in and out) one run uses, all its calls together
    thread_tokens: int = 1_500  # the most of a thread (counted by characters) one call carries
    max_calls: int = 12  # calls in one run, retries included
    timeout_s: float = 120.0
    allow_unpriced: bool = False
    purpose: str | None = None
    secrets: tuple[jobs.SecretUse, ...] = ()  # the key of an API model, leased through the broker
    bare: bool | None = None  # None: bare, except a Claude Code run on the subscription login (see `is_bare`)


@dataclass(frozen=True)
class Config:
    name: str
    mailboxes: tuple[str, ...]
    query: dict[
        str, str
    ]  # mailbox -> the search query (Gmail syntax for a gmail mailbox) that leaves out what is not ours
    classes: dict[str, str]  # mailbox -> the data class it belongs to
    rules: Path
    prompt: str
    voice: str
    summary_dir: Path | None
    first_run_days: int  # 0 = on the first run, remember where the mailbox is and read nothing
    max_drafts: int
    model: ModelConfig
    work: Path | None = None  # where a model call's input file goes (default: under the state folder)


def _path(value, key: str, base: Path, env: dict | None = None) -> Path:
    if not isinstance(value, str) or not value.strip() or "\0" in value:
        raise _bad(f"triage: {key} must be a path")
    if value.startswith("~/") or value == "~":
        value = ((env or os.environ).get("HOME") or str(Path.home())) + value[1:]
    p = Path(value)
    return Path(os.path.normpath(p if p.is_absolute() else base / p))


def _number(t: dict, key: str, default, where: str, integer: bool = False, low: float = 0):
    v = t.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (integer and not isinstance(v, int)) or v < low:
        raise _bad(f"{where}: {key} must be {'a whole number' if integer else 'a number'} of at least {low:g}")
    return v


def _table(doc: dict, key: str, allowed: set[str], where: str) -> dict:
    t = doc.get(key, {})
    if not isinstance(t, dict):
        raise _bad(f"{where}: {key} must be a table")
    extra = sorted(set(t) - allowed)
    if extra:
        raise _bad(f"{where}: unknown key {extra[0]!r} in {key} (known: {', '.join(sorted(allowed))})")
    return t


MODEL_KEYS = {
    "runner", "model", "runner_options", "budget_usd", "run_tokens", "thread_tokens", "max_calls", "timeout_s",
    "allow_unpriced", "purpose", "secrets", "bare",
}  # fmt: skip
TOP_KEYS = {
    "mailboxes", "mailbox", "rules", "prompt_file", "voice", "summary_dir", "first_run_days", "max_drafts", "model",
    "work",
}  # fmt: skip


def _secrets(v, where: str) -> tuple[jobs.SecretUse, ...]:
    """`[[triage.model.secrets]]`: name, host and optionally purpose, checked as a job file's `[[secrets]]` are."""
    if not (isinstance(v, list) and all(isinstance(x, dict) and set(x) <= {"name", "host", "purpose"} for x in v)):
        raise _bad(f"{where}: secrets must be [[triage.model.secrets]] tables of name, host and purpose")
    out = []
    for x in v:
        name, host = x.get("name"), x.get("host")
        if not (
            isinstance(name, str) and NAME_RE.match(name) and isinstance(host, str) and HOST_RE.match(host.lower())
        ):
            raise _bad(f"{where}: a secret needs a secret name and a host name (no scheme, no path)")
        out.append(jobs.SecretUse(name, host.lower(), x.get("purpose", "run")))
    return tuple(out)


def _check_direct(m: dict, where: str) -> None:
    """The direct API runner: its own options, a key leased through the broker, and a price (ws ships none) unless the
    person says to run unpriced (then only the token caps bound a run)."""
    opts = m.get("runner_options", {})
    if not isinstance(opts, dict) or set(opts) - DIRECT_OPTIONS:
        raise _bad(f"{where}: runner {DIRECT} takes runner_options {', '.join(sorted(DIRECT_OPTIONS))} only")
    for k in DIRECT_OPTIONS:
        v = opts.get(k)
        if k in opts and (isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0):
            raise _bad(f"{where}: runner_options.{k} must be a number above 0")
    if not m.get("secrets"):
        raise _bad(f"{where}: runner {DIRECT} needs [[triage.model.secrets]] (the key, leased through the broker)")
    if not m.get("allow_unpriced") and not {"price_in_usd_per_mtok", "price_out_usd_per_mtok"} <= set(opts):
        raise _bad(
            f"{where}: runner {DIRECT} needs runner_options price_in_usd_per_mtok and price_out_usd_per_mtok "
            "(ws ships no prices), or allow_unpriced = true"
        )


def parse_config(doc: dict, name: str, base: Path, env: dict | None = None) -> Config:
    """The settings of `[triage]` (`doc`), with relative paths taken from `base`. Raises TriageError naming the key."""
    where = f"triage {name}"
    extra = sorted(set(doc) - TOP_KEYS)
    if extra:
        raise _bad(f"{where}: unknown key {extra[0]!r} (known: {', '.join(sorted(TOP_KEYS))})")
    boxes = doc.get("mailboxes")
    if not (isinstance(boxes, list) and boxes and all(isinstance(b, str) and mail.MAILBOX_RE.match(b) for b in boxes)):
        raise _bad(f"{where}: mailboxes must be a list of the profile's mailbox names, as in [mail.NAME]")
    if len(set(boxes)) != len(boxes):
        raise _bad(f"{where}: a mailbox is listed twice")
    per = _table(doc, "mailbox", set(boxes), where)
    query, classes = {}, {}
    for b, t in per.items():
        if not isinstance(t, dict) or set(t) - {"query", "class"}:
            raise _bad(f"{where}: [mailbox.{b}] takes query and class only")
        if "query" in t:
            q = t["query"]
            if not (isinstance(q, str) and imap.QUERY_RE.match(q)):
                raise _bad(f"{where}: [mailbox.{b}] query must be printable ASCII, 1 to 200 characters")
            query[b] = q
        if "class" in t:
            if not (isinstance(t["class"], str) and jobs.NAME.match(t["class"])):
                raise _bad(f"{where}: [mailbox.{b}] class must be a data class name from the policy")
            classes[b] = t["class"]
    if "rules" not in doc or "prompt_file" not in doc:
        raise _bad(f"{where}: rules and prompt_file are required (paths to the rules file and the model's prompt)")
    prompt_path = _path(doc["prompt_file"], "prompt_file", base, env)
    try:
        prompt = prompt_path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as e:
        raise _bad(f"{where}: prompt_file {str(prompt_path)!r} cannot be read ({type(e).__name__})") from None
    if not prompt:
        raise _bad(f"{where}: prompt_file {str(prompt_path)!r} is empty")
    voice = ""
    if "voice" in doc:
        voice_path = _path(doc["voice"], "voice", base, env)
        try:
            voice = mail.clean(voice_path.read_text(encoding="utf-8")).strip()[:VOICE_MAX]
        except (OSError, UnicodeDecodeError) as e:
            raise _bad(f"{where}: voice {str(voice_path)!r} cannot be read ({type(e).__name__})") from None
    m = _table(doc, "model", MODEL_KEYS, where)
    for k in ("runner", "model", "purpose"):
        if k in m and not (isinstance(m[k], str) and m[k].strip()):
            raise _bad(f"{where}: model.{k} must be a non-empty string")
    if "runner_options" in m and not isinstance(m["runner_options"], dict):
        raise _bad(f"{where}: model.runner_options must be a table")
    if m.get("runner") == DIRECT:
        _check_direct(m, where)
    if "bare" in m and not isinstance(m["bare"], bool):
        raise _bad(f"{where}: model.bare must be true or false")
    if "allow_unpriced" in m and not isinstance(m["allow_unpriced"], bool):
        raise _bad(f"{where}: model.allow_unpriced must be true or false")
    mw = f"{where}: model"
    secrets = _secrets(m.get("secrets", []), mw)
    model = ModelConfig(
        runner=m.get("runner", "claude-code"),
        model=m.get("model", "haiku"),
        runner_options=m.get("runner_options", {}),
        budget_usd=float(_number(m, "budget_usd", 0.10, mw, low=0.0001)),
        run_tokens=_number(m, "run_tokens", 400_000, mw, integer=True, low=100),
        thread_tokens=_number(m, "thread_tokens", 1_500, mw, integer=True, low=50),
        max_calls=_number(m, "max_calls", 12, mw, integer=True, low=1),
        timeout_s=float(_number(m, "timeout_s", 120, mw, low=5)),
        allow_unpriced=m.get("allow_unpriced", False),
        purpose=m.get("purpose"),
        secrets=secrets,
        bare=m.get("bare"),
    )
    return Config(
        name=name,
        mailboxes=tuple(boxes),
        query=query,
        classes=classes,
        rules=_path(doc["rules"], "rules", base, env),
        prompt=prompt,
        voice=voice,
        summary_dir=_path(doc["summary_dir"], "summary_dir", base, env) if "summary_dir" in doc else None,
        first_run_days=_number(doc, "first_run_days", 1, where, integer=True),
        max_drafts=_number(doc, "max_drafts", 15, where, integer=True),
        model=model,
        work=_path(doc["work"], "work", base, env) if "work" in doc else None,
    )


# ---------------------------------------------------------------- rules (no model)


@dataclass(frozen=True)
class Rule:
    id: str
    label: str  # one of RULE_LABELS
    sender: tuple[str, ...] = ()  # addresses, lower-case
    domain: tuple[str, ...] = ()  # a domain and every sub-domain of it, lower-case
    bulk: bool | None = None  # True: bulk-mail headers present; False: absent
    subject: re.Pattern | None = None
    mailbox: tuple[str, ...] = ()  # only these mailboxes (empty: all)


RULE_KEYS = {"id", "label", "sender", "domain", "bulk", "subject", "mailbox"}


@dataclass(frozen=True)
class Rules:
    rules: tuple[Rule, ...]
    default: str = ASK_MODEL


def _strings(v, key: str, where: str) -> tuple[str, ...]:
    items = [v] if isinstance(v, str) else v
    if not (isinstance(items, list) and items and all(isinstance(x, str) and x.strip() for x in items)):
        raise _bad(f"{where}: {key} must be a string or a list of strings")
    return tuple(x.strip().lower() for x in items)


def parse_rules(text: str, where: str = "rules") -> Rules:
    """A rules file: `default = "ask-model"` and `[[rule]]` tables (id, label, and one or more of sender, domain, bulk,
    subject, mailbox). Whole file or an error naming the rule."""
    try:
        doc = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise _bad(f"{where}: {e}") from None
    extra = sorted(set(doc) - {"default", "rule"})
    if extra:
        raise _bad(f"{where}: unknown key {extra[0]!r} (known: default, rule)")
    default = doc.get("default", ASK_MODEL)
    if default not in RULE_LABELS:
        raise _bad(f"{where}: default must be one of {', '.join(RULE_LABELS)}")
    raw = doc.get("rule", [])
    if not isinstance(raw, list) or not all(isinstance(r, dict) for r in raw):
        raise _bad(f"{where}: rule must be [[rule]] tables")
    out: list[Rule] = []
    seen: set[str] = set()
    for i, r in enumerate(raw, 1):
        rid = r.get("id")
        at = f"{where}: rule #{i}" + (f" ({rid})" if isinstance(rid, str) else "")
        extra = sorted(set(r) - RULE_KEYS)
        if extra:
            raise _bad(f"{at}: unknown key {extra[0]!r} (known: {', '.join(sorted(RULE_KEYS))})")
        if not (isinstance(rid, str) and _ID.match(rid)):
            raise _bad(f"{at}: id must be lower-case letters, digits, - _ and . (it is logged with every decision)")
        if rid in seen:
            raise _bad(f"{at}: the id is used twice")
        seen.add(rid)
        if r.get("label") not in RULE_LABELS:
            raise _bad(f"{at}: label must be one of {', '.join(RULE_LABELS)}")
        if not set(r) & {"sender", "domain", "bulk", "subject"}:
            raise _bad(f"{at}: a rule needs at least one of sender, domain, bulk, subject")
        if "bulk" in r and not isinstance(r["bulk"], bool):
            raise _bad(f"{at}: bulk must be true or false")
        pattern = None
        if "subject" in r:
            if not (isinstance(r["subject"], str) and 0 < len(r["subject"]) <= 200):
                raise _bad(f"{at}: subject must be a pattern of at most 200 characters")
            try:
                pattern = re.compile(r["subject"], re.IGNORECASE)
            except re.error:
                raise _bad(f"{at}: subject is not a valid pattern") from None
        mailbox = _strings(r["mailbox"], "mailbox", at) if "mailbox" in r else ()
        out.append(
            Rule(
                rid,
                r["label"],
                sender=_strings(r["sender"], "sender", at) if "sender" in r else (),
                domain=tuple(d.lstrip(".@") for d in _strings(r["domain"], "domain", at)) if "domain" in r else (),
                bulk=r.get("bulk"),
                subject=pattern,
                mailbox=mailbox,
            )
        )
    return Rules(tuple(out), default)


@dataclass(frozen=True)
class Facts:
    """What a rule can see of a message: header facts only, never the body."""

    mailbox: str
    sender: str  # the address, lower-case
    subject: str
    bulk: bool


def is_bulk(msg) -> bool:
    """Bulk or automated mail by its headers: List-Unsubscribe, List-Id, Precedence bulk/list/junk, Auto-Submitted."""

    def h(name: str) -> str:
        try:
            v = msg.get(name)
        except Exception:  # noqa: BLE001 (a header the parser cannot read is not a bulk header)
            return ""
        return str(v).strip().lower() if v is not None else ""

    return bool(
        h("list-unsubscribe")
        or h("list-id")
        or h("precedence") in ("bulk", "list", "junk")
        or h("auto-submitted") not in ("", "no")
    )


def matches(rule: Rule, f: Facts) -> bool:
    """Every condition the rule names holds (they are ANDed)."""
    domain = f.sender.rpartition("@")[2]
    return (
        (not rule.mailbox or f.mailbox in rule.mailbox)
        and (not rule.sender or f.sender in rule.sender)
        and (not rule.domain or any(domain == d or domain.endswith("." + d) for d in rule.domain))
        and (rule.bulk is None or rule.bulk == f.bulk)
        and (rule.subject is None or rule.subject.search(f.subject) is not None)
    )


def decide(rules: Rules, f: Facts) -> tuple[str, str]:
    """(label or ASK_MODEL, the id of the rule that said so). The first match wins; no match is the file's default,
    under the id `default`."""
    for r in rules.rules:
        if matches(r, f):
            return r.label, r.id
    return rules.default, "default"


# ---------------------------------------------------------------- the model's answer


class SchemaError(ValueError):
    """The model's answer did not parse to the fixed schema."""


@dataclass(frozen=True)
class Verdict:
    needs_reply: bool
    reason: str
    draft: str | None


def parse_verdict(text: str) -> Verdict:
    """`{needs_reply: bool, reason: str <= 200, draft: str | null}` from the model's text, nothing else accepted (no extra
    key, a draft only when a reply is needed). A fence or words around the object are tolerated; two objects are not."""
    s = text.strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-z]*\s*|\s*```$", "", s, flags=re.IGNORECASE).strip()
    start, end = s.find("{"), s.rfind("}")
    if start < 0 or end < start:
        raise SchemaError("no JSON object in the answer")
    try:
        d = json.loads(s[start : end + 1])
    except ValueError:
        raise SchemaError("the answer is not valid JSON") from None
    if not isinstance(d, dict) or set(d) != {"needs_reply", "reason", "draft"}:
        raise SchemaError("the object must have needs_reply, reason and draft, and nothing else")
    if not isinstance(d["needs_reply"], bool):
        raise SchemaError("needs_reply must be true or false")
    if not isinstance(d["reason"], str) or len(d["reason"]) > REASON_MAX:
        raise SchemaError(f"reason must be a string of at most {REASON_MAX} characters")
    draft = d["draft"]
    if draft is not None and not isinstance(draft, str):
        raise SchemaError("draft must be a string or null")
    if d["needs_reply"] and not (isinstance(draft, str) and draft.strip()):
        raise SchemaError("needs_reply is true, so draft must be the reply")
    return Verdict(d["needs_reply"], mail.clean(d["reason"], keep_newlines=False), draft if d["needs_reply"] else None)


# ---------------------------------------------------------------- checks on a draft (no model)

_MONTHS = ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec")
_WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
_AMOUNT = re.compile(
    r"(?:[$€£]\s?(\d[\d,]*(?:\.\d+)?))|(?:\b(\d[\d,]*(?:\.\d+)?)\s?(?:nzd|usd|aud|dollars?|bucks|k)\b)", re.IGNORECASE
)
_DATE_ISO = re.compile(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b")
_DATE_NUM = re.compile(r"\b(\d{1,2})[/.](\d{1,2})(?:[/.](\d{2,4}))?\b")
_DATE_DM = re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+(?:of\s+)?{_MONTH_RE}\b", re.IGNORECASE)
_DATE_MD = re.compile(rf"\b{_MONTH_RE}\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?\b", re.IGNORECASE)
_WEEKDAY = re.compile(r"\b(mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?\b", re.IGNORECASE)
_TIME_AMPM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s?([ap])\.?m\b\.?", re.IGNORECASE)
_TIME_24 = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_PAYMENT = re.compile(
    r"\b(?:i|we)(?:'ll| will| shall| am going to|'m going to)\s+(?:\w+\s+){0,2}?(?:pay|transfer|deposit|settle)\b"
    r"|\b(?:i|we)(?:'ve| have)\s+(?:already\s+)?(?:paid|transferred|deposited|settled)\b"
    r"|\b(?:will|shall)\s+(?:be\s+)?(?:paid|transferred|deposited|settled)\b"
    r"|\b(?:payment|funds|money)\s+(?:has|have|is|are|will)\s+(?:been\s+|being\s+|be\s+)?"
    r"(?:made|sent|processed|transferred|paid|on its way)\b"
    r"|\b(?:i|we)(?:'ll| will)\s+(?:send|make)\s+(?:the |a |your )?(?:payment|money|funds|transfer)\b"
    r"|\bpay(?:ing)?\s+(?:you|it|this|that|the (?:invoice|bill|balance|amount))\b",
    re.IGNORECASE,
)
_LINK = re.compile(
    r"\b(?:https?://|www\.)[^\s<>\"')\]]+|"
    r"\b[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*\.(?:com|net|org|nz|co|io|gov|edu|app|dev|me|ly|ai|info|biz|au|uk|us)\b(?:/[^\s<>\"')\]]*)?",
    re.IGNORECASE,
)
_GREETING = re.compile(
    r"^(?:kia ora|hi|hello|hey|morning|good (?:morning|afternoon|evening)|ata marie)\b[^.!?]{0,60}[,!:]?$",
    re.IGNORECASE,
)
_SIGNOFF = re.compile(
    r"^(?:cheers|thanks|thank you|many thanks|regards|kind regards|warm regards|best|best wishes|ngā mihi|nga mihi|ka kite)\b[,!.]?(?:\s+[\w\u2019'-]+)?[,!.]?$",  # a sign-off word and at most a name
    re.IGNORECASE,
)
_HEADER_NOISE = re.compile(r"^(?:Date|Message-ID):.*$", re.MULTILINE)  # a header's own date or time is no one's offer
_ASKS_TIME = re.compile(
    r"\b(?:when (?:are|can|could|would|works)|what time|are you (?:free|available)|can we (?:meet|catch up|talk|chat|call)"
    r"|could we (?:meet|catch up|talk|chat|call)|(?:free|available) (?:on|for|at|this|next|tomorrow)"
    r"|(?:re)?schedul\w*|book (?:a|in) (?:time|call|meeting|slot)|find a time|suitable time|what day|which day"
    r"|time (?:to|that) (?:meet|talk|chat|suits|works))\b",
    re.IGNORECASE,
)


def _month(s: str) -> int:
    return _MONTHS.index(s[:3].lower()) + 1


def _weekday(s: str) -> int:
    return _WEEKDAYS.index(s[:3].lower())


def _amounts(text: str) -> set[str]:
    out = set()
    for m in _AMOUNT.finditer(text):
        n = (m.group(1) or m.group(2)).replace(",", "")
        out.add(n.rstrip("0").rstrip(".") if "." in n else n)
    return out


def _dates(text: str) -> set[tuple]:
    """Dates as (month, day) pairs and weekdays as ("wd", n): the same date in different words compares equal."""
    out: set[tuple] = set()
    for m in _DATE_ISO.finditer(text):
        out.add((int(m[2]), int(m[3])))
    for m in _DATE_NUM.finditer(text):  # day first, as in NZ
        out.add((int(m[2]), int(m[1])))
    for m in _DATE_DM.finditer(text):
        out.add((_month(m[2]), int(m[1])))
    for m in _DATE_MD.finditer(text):
        out.add((_month(m[1]), int(m[2])))
    for m in _WEEKDAY.finditer(text):
        out.add(("wd", _weekday(m[1])))
    return out


def _times(text: str) -> set[tuple[int, int]]:
    out = set()
    for m in _TIME_AMPM.finditer(text):
        h = int(m[1]) % 12 + (12 if m[3].lower() == "p" else 0)
        out.add((h, int(m[2] or 0)))
    for m in _TIME_24.finditer(text):
        out.add((int(m[1]), int(m[2])))
    return out


def _links(text: str) -> set[str]:
    return {m.group(0).rstrip(".,;:!?").lower() for m in _LINK.finditer(text)}


def _fold(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def asks_for_time(thread_text: str) -> bool:
    """The thread asks when, or for a time (so the reply would want a calendar)."""
    return _ASKS_TIME.search(thread_text) is not None


def _sentences(draft: str) -> int:
    lines = [ln.strip() for ln in draft.strip().splitlines() if ln.strip()]
    if lines and _GREETING.match(lines[0]) and len(lines[0]) <= 70:
        lines = lines[1:]
    while lines and (
        _SIGNOFF.match(lines[-1]) or (len(lines) > 1 and _SIGNOFF.match(lines[-2]) and len(lines[-1]) <= 40)
    ):
        lines = lines[:-1] if _SIGNOFF.match(lines[-1]) else lines[:-2]
    body = " ".join(lines)
    return len([s for s in re.split(r"(?<=[.!?])\s+", body) if re.search(r"\w", s)])


def check_draft(draft: str, thread_text: str) -> str | None:
    """None when the draft passes, else the reason it is refused (a fixed phrase: it goes in a log and a summary). The
    thread is the text the model was given."""
    t = _fold(_HEADER_NOISE.sub("", thread_text))
    d = _fold(draft)
    if re.match(r"^\s*dear\b", d):
        return "voice: opens with Dear"
    n = _sentences(draft)
    if not 2 <= n <= 6:
        return f"length: {n} sentences, not 2 to 6"
    if _PAYMENT.search(d):
        return "promises a payment"
    if not _amounts(d) <= _amounts(t):
        return "names an amount not in the thread"
    if not _dates(d) <= _dates(t):
        return "names a date not in the thread"
    if not _times(d) <= _times(t):
        return "names a time not in the thread"
    if not _links(d) <= _links(t):
        return "holds a link or address not in the thread"
    return None


# ---------------------------------------------------------------- the model call


@dataclass
class Reply:
    """One model call: its text and what it cost. `ok` False says why in `error` (never mail text)."""

    ok: bool
    text: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    run_id: str | None = None


Model = Callable[[str, str, float], Reply]  # (instructions, the thread as data, the money this call may spend) -> Reply


def answer_text(runner: str, stdout: str) -> str:
    """The model's text from a runner's captured output: Claude Code's one JSON result, or OpenCode's event stream."""
    if runner == "claude-code":
        try:
            r = json.loads(stdout)
        except ValueError:
            return ""
        return r.get("result", "") if isinstance(r, dict) and isinstance(r.get("result"), str) else ""
    parts = []
    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        part = ev.get("part") if isinstance(ev, dict) and isinstance(ev.get("part"), dict) else {}
        if ev.get("type") == "text" and isinstance(part.get("text"), str):
            parts.append(part["text"])
    return "".join(parts).strip()


def is_bare(m: ModelConfig) -> bool:
    """Whether the model run is `bare` (no hooks, plugins or auto-discovered context). Claude Code's `--bare` does not
    read the subscription login (it takes an API key only), so a Claude Code run with no key leased is not bare unless
    `[triage.model] bare = true` says so. The run's folder is empty either way, so no project context is found."""
    if m.bare is not None:
        return m.bare
    return not (m.runner == "claude-code" and not m.secrets)


class RunnerModel:
    """The model call, by ws's own runner path: `ws.run.run_job` with an in-memory job (no tools asked for, one turn, the
    thread as the job's `input`, so it reaches the model as marked untrusted data). Its run record lands in runs.jsonl
    under `NAME-model`, so the cap, the spend and the tokens are counted where every run is counted."""

    def __init__(self, cfg: Config, work: Path, env: dict | None = None):
        self.cfg, self.work, self.env = cfg, work, env

    def __call__(self, instructions: str, data: str, budget_usd: float) -> Reply:
        from ws import run  # late: ws.run imports most of ws

        m = self.cfg.model
        self.work.mkdir(mode=0o700, parents=True, exist_ok=True)
        (self.work / "thread.md").write_text(data, encoding="utf-8")
        os.chmod(self.work / "thread.md", 0o600)
        job = jobs.Job(
            name=f"{self.cfg.name}-model",
            runner=m.runner,
            model=m.model,
            prompt=instructions,
            cwd=str(self.work),
            input="thread.md",
            timeout_s=m.timeout_s,
            max_turns=1 if m.runner == "claude-code" else None,
            bare=is_bare(m),
            allow_unpriced=m.allow_unpriced,
            secrets=m.secrets,
            sources=("mail",),
            runner_options=m.runner_options,
            purpose=m.purpose,
            budget_usd=budget_usd,
            green="targeted",
        )
        seen: list[dict] = []
        sink = io.StringIO()
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):  # the run's own report is not ours
            code = run.run_job(argparse.Namespace(job=job.name, dry_run=False), job, hook=seen.append)
        rec = seen[0] if seen else {}
        t = rec.get("tokens") or {}
        tin = sum(t.get(k) or 0 for k in ("input", "cache_creation", "cache_read"))
        cost = rec.get("cost_usd")
        reply = Reply(
            ok=False,
            tokens_in=tin,
            tokens_out=t.get("output") or 0,
            cost_usd=float(cost) if isinstance(cost, (int, float)) else 0.0,
            run_id=rec.get("run_id"),
        )
        if code != 0 or rec.get("status") != "ok":
            reply.error = f"the model run ended {rec.get('status') or 'without a record'}" + (
                f" ({rec['stop_reason']})" if rec.get("stop_reason") else ""
            )
            return reply
        out = paths.state_dir(self.env) / "runs" / str(rec.get("run_id")) / "stdout.txt"
        try:
            reply.text = answer_text(m.runner, out.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            reply.error = "the model's output could not be read back"
            return reply
        reply.ok = True
        return reply


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # a redirect would carry the lease to another address


_OPENER = urllib.request.build_opener(_NoRedirect)
_LOOPBACK = ("127.0.0.1", "localhost", "::1")


class DirectModel:
    """The model call straight to the Messages API: no Claude Code, no tools, no run folder. One POST per call, the
    instructions as `system` and the thread (already marked untrusted data) as the one user message, so a call is the
    prompt, the voice and the trimmed thread and nothing else (`claude -p` measured ~31k tokens in for the same job, almost
    all its own context). The key never reaches this process: the broker leases a loopback address that forwards to the
    allowed host and puts the real key on, and this sends a placeholder. The lease is released after each call, and its
    address (it carries a token) is never put in a message or a record.

    It skips what `ws.run` adds around a runner (the monthly ledger and the run folder); the caps that remain are this
    job's own: budget_usd, run_tokens, max_calls, and the worst case of a call checked against what is left."""

    def __init__(self, cfg: Config, env: dict | None = None, lease=None, release=None):
        self.cfg = cfg
        self.lease = lease or (lambda s: broker.request(s.name, s.purpose, s.host, socket=wsecrets.socket_path(env)))
        self.release = release or (lambda ls: broker.release(ls, socket=wsecrets.socket_path(env)))

    def __call__(self, instructions: str, data: str, budget_usd: float) -> Reply:
        m = self.cfg.model
        o = m.runner_options
        max_out = int(o.get("max_tokens", 400))
        p_in, p_out = o.get("price_in_usd_per_mtok"), o.get("price_out_usd_per_mtok")
        if p_in is not None and p_out is not None:  # the worst case of this call must fit in what is left
            worst = ((len(instructions) + len(data)) / CHARS_PER_TOKEN * p_in + max_out * p_out) / 1_000_000
            if worst > budget_usd:
                return Reply(ok=False, error="the call could pass the run's budget_usd, so it was not made")
        body = json.dumps(
            {
                "model": m.model,
                "max_tokens": max_out,
                "system": instructions,
                "messages": [{"role": "user", "content": data}],
            }
        ).encode()
        try:
            lease = self.lease(m.secrets[0])
        except (broker.BrokerError, OSError) as e:
            return Reply(ok=False, error=f"the key could not be leased ({type(e).__name__})")
        try:
            return self._post(lease, body, p_in, p_out)
        finally:
            with contextlib.suppress(broker.BrokerError, OSError):
                self.release(lease)

    def _post(self, lease, body: bytes, p_in, p_out) -> Reply:
        from ws.adapters import claude_code  # late: the placeholder key lives with the adapter that first used it

        u = urllib.parse.urlsplit(lease.base_url)
        if u.scheme != "http" or u.hostname not in _LOOPBACK:
            return Reply(ok=False, error="the broker's lease was not a loopback address, so nothing was sent")
        req = urllib.request.Request(
            lease.base_url.rstrip("/") + "/v1/messages", data=body, method="POST",
            headers={lease.header: claude_code.PLACEHOLDER_KEY, "anthropic-version": API_VERSION,
                     "content-type": "application/json", "user-agent": "ws-triage"},
        )  # fmt: skip
        try:
            with _OPENER.open(
                req, timeout=self.cfg.model.timeout_s
            ) as r:  # the broker's loopback address, checked above
                raw = r.read(MAX_REPLY_BYTES + 1)
        except urllib.error.HTTPError as e:  # the code only: a body or an address could carry the lease
            return Reply(ok=False, error=f"the API answered HTTP {e.code}")
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            return Reply(ok=False, error=f"the API could not be reached ({type(getattr(e, 'reason', e)).__name__})")
        except ValueError:
            return Reply(ok=False, error="the API request could not be built")
        try:
            if len(raw) > MAX_REPLY_BYTES:
                raise ValueError
            doc = json.loads(raw)
            use = doc["usage"]
            tin = sum(
                int(use.get(k) or 0) for k in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
            )
            tout = int(use.get("output_tokens") or 0)
            text = "".join(b["text"] for b in doc["content"] if isinstance(b, dict) and b.get("type") == "text")
        except (ValueError, KeyError, TypeError, AttributeError):
            return Reply(ok=False, error="the API's answer was not a message")
        cost = (tin * p_in + tout * p_out) / 1_000_000 if p_in is not None and p_out is not None else 0.0
        return Reply(ok=True, text=text.strip(), tokens_in=tin, tokens_out=tout, cost_usd=cost)


def make_model(cfg: Config, work: Path, env: dict | None = None) -> Model:
    """The model the settings name: the direct API call, or a ws runner (claude-code, opencode)."""
    return DirectModel(cfg, env) if cfg.model.runner == DIRECT else RunnerModel(cfg, work, env)


# ---------------------------------------------------------------- the mail route


@dataclass(frozen=True)
class Raw:
    ref: str  # `mailbox:uidvalidity-uid`
    raw: bytes
    thread: str | None
    truncated: bool


class Route(Protocol):
    """What the pipeline needs of a mailbox; `MailAdapter` is the one that calls ws.mail."""

    def search(self, since: date, query: str | None) -> tuple[int, list[int], int]: ...
    def ref(self, uidvalidity: int, uid: int) -> str: ...
    def fetch(self, ref: str) -> Raw: ...
    def user(self) -> str: ...
    def label(self, ref: str, label: str) -> None: ...
    def draft(self, ref: str, body: str) -> None: ...


class MailAdapter:
    """One mailbox through `ws.mail.Mail`. Two reads are not public there: `_fetch` (the whole message: `get` gives five
    headers and the text, and a rule needs List-Unsubscribe) and `_user` (the mailbox's own address, so a message it sent
    is left alone). They are used here and nowhere else, so a public `Mail.fetch_raw` and `Mail.address` replace them
    in one place."""

    def __init__(self, client: mail.Mail, name: str):
        self.m, self.name = client, name

    def search(self, since: date, query: str | None) -> tuple[int, list[int], int]:
        r = self.m.search(self.name, query=query, since=since)
        uvs, uids = set(), []
        for ref in r["uids"]:
            uv, uid = mail.split_uid(ref, self.name)
            uvs.add(uv)
            uids.append(uid)
        return (next(iter(uvs)) if uvs else 0), uids, r["total"]

    def ref(self, uidvalidity: int, uid: int) -> str:
        return mail.make_uid(self.name, uidvalidity, uid)

    def fetch(self, ref: str) -> Raw:
        ref2, r = self.m._fetch(self.m._box(self.name), ref, "full")
        return Raw(ref2, base64.b64decode(r["raw"]), r.get("thread"), bool(r.get("truncated")))

    def user(self) -> str:
        return self.m._user(self.m._box(self.name))

    def label(self, ref: str, label: str) -> None:
        self.m.label(self.name, ref, label)

    def draft(self, ref: str, body: str) -> None:
        self.m.draft(self.name, ref, body)


# ---------------------------------------------------------------- state, record, summary


def state_path(name: str, env: dict | None = None) -> Path:
    return paths.state_dir(env) / FOLDER_NAME / f"{name}.json"


def log_path(env: dict | None = None) -> Path:
    return paths.state_dir(env) / "logs" / "triage.jsonl"


def load_state(path: Path) -> dict:
    """The saved state, or a fresh one. A file that cannot be read is a fresh state (the run then starts from the
    first-run window and labels are idempotent), never a crash."""
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"schema": SCHEMA_VERSION, "mailboxes": {}, "day": {}}
    if not isinstance(d, dict) or not isinstance(d.get("mailboxes"), dict):
        return {"schema": SCHEMA_VERSION, "mailboxes": {}, "day": {}}
    d.setdefault("day", {})
    return d


def save_state(path: Path, state: dict) -> None:
    """Whole file replaced by rename: a stop in the middle leaves the old one."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".triage-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _short(text: str, n: int = 60) -> str:
    t = mail.neutral(mail.clean(text or "", keep_newlines=False))
    return t if len(t) <= n else t[: n - 1].rstrip() + "…"


def summary_text(day: dict, when: datetime) -> str:
    """The day's summary in the shape the morning note reads: counts, `Drafts created: N`, and "should check" lines."""
    labels = day.get("labels", {})
    parts = ", ".join(f"{k} {labels.get(k, 0)}" for k in ("ws/needs-reply", "ws/fyi", "ws/low", "ws/waiting"))
    cost = f"{day.get('cost_usd', 0.0):.4f}"
    lines = [
        (
            f"Inbox triage {when.astimezone():%Y-%m-%d %H:%M}: {day.get('threads', 0)} threads. "
            f"Labels applied: {parts}. Drafts created: {day.get('drafts', 0)}."
        ),
        (
            f"Model calls today: {day.get('model_calls', 0)}, {day.get('tokens_in', 0)} tokens in and "
            f"{day.get('tokens_out', 0)} out, cost US$ {cost}. Rules decided {day.get('by_rule', 0)} threads."
        ),
    ]
    if not day.get("threads"):
        lines.insert(0, "No new mail.")
    flags = day.get("flags", [])
    if flags:
        lines.append("Worth a look:")
        lines += [f"- {f}" for f in flags[-MAX_FLAGS:]]
    return "\n".join(lines) + "\n"


def write_summary(folder: Path, day: dict, when: datetime) -> Path:
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"summary-{day['date']}.md"  # the name is built from a date, never from mail
    fd, tmp = tempfile.mkstemp(dir=folder, prefix=".summary-")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(summary_text(day, when))
    os.replace(tmp, out)
    return out


# ---------------------------------------------------------------- the pipeline


@dataclass
class Message:
    uid: int
    ref: str
    sender: str
    name: str
    subject: str
    bulk: bool
    thread: str
    text: str  # the message as ws.mail's delimited plain text


@dataclass
class Totals:
    seen: int = 0
    by_rule: int = 0
    labelled: dict = field(default_factory=dict)
    asked: int = 0
    calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    drafts: int = 0
    refused: list = field(default_factory=list)
    flagged: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    notes: list = field(default_factory=list)  # what happened that needs no action (so it doesn't fail the run)
    decisions: list = field(default_factory=list)
    deferred: int = 0

    def label(self, label: str) -> None:
        self.labelled[label] = self.labelled.get(label, 0) + 1


def _h(msg, name: str, cap: int = mail.MAX_HEADER) -> str:
    """One header as plain text, cleaned and defused the way ws.mail does it; "" when it is missing or unreadable."""
    try:
        v = msg.get(name)
    except Exception:  # noqa: BLE001 (a header the parser cannot read is left out)
        return ""
    return mail.neutral(mail.clean(str(v), keep_newlines=False))[:cap] if v is not None else ""


def read_message(mailbox: str, raw: Raw, uid: int) -> Message:
    msg = BytesParser(policy=_policy.default).parsebytes(raw.raw)
    name, addr = parseaddr(_h(msg, "From"))
    thread = raw.thread or _h(msg, "Message-ID") or raw.ref
    refs = mail.MSGID_RE.findall(_h(msg, "References", 2000))
    if not raw.thread and refs:
        thread = refs[0]  # no provider thread id: the first message of the chain names the thread
    return Message(
        uid=uid,
        ref=raw.ref,
        sender=addr.strip().lower(),
        name=name,
        subject=_h(msg, "Subject"),
        bulk=is_bulk(msg),
        thread=thread,
        text=mail.as_text(raw.raw, mailbox, raw.ref, raw.thread, raw.truncated),
    )


def thread_data(msgs: list[Message], cap_tokens: int) -> str:
    """The newest messages of a thread that fit the cap (counted by characters), oldest first; the newest message is
    always there, cut if it alone is over."""
    cap = cap_tokens * CHARS_PER_TOKEN
    kept: list[str] = []
    used = 0
    for m in sorted(msgs, key=lambda x: x.uid, reverse=True):
        if kept and used + len(m.text) > cap:
            break
        kept.append(m.text if len(m.text) <= cap else m.text[:cap].rstrip() + "\n[ws: message cut short]")
        used += len(m.text)
    return "\n\n".join(reversed(kept))


class Run:
    """One run of the pipeline over the configured mailboxes. `routes` maps a mailbox name to its `Route`."""

    def __init__(
        self,
        cfg: Config,
        rules: Rules,
        routes: dict[str, Route],
        model: Model,
        state: dict,
        now: datetime,
        dry_run: bool = False,
    ):
        self.cfg, self.rules, self.routes, self.model = cfg, rules, routes, model
        self.state, self.now, self.dry = state, now, dry_run
        self.t = Totals()
        self.model_down = False  # a runner failure stops further calls this run (the cause would repeat)

    # -- the caps on the model

    def room(self) -> str | None:
        m = self.cfg.model
        if self.model_down:
            return "model_error"
        if self.t.calls >= m.max_calls:
            return "max_calls"
        if self.t.tokens_in + self.t.tokens_out >= m.run_tokens:
            return "run_tokens"
        if self.t.cost_usd >= m.budget_usd:
            return "budget_usd"
        return None

    def instructions(self, retry_why: str | None = None) -> str:
        parts = [self.cfg.prompt]
        if self.cfg.voice:
            parts.append("## Reply voice\n" + self.cfg.voice)
        if retry_why:
            parts.append(f"Your last answer was refused: {retry_why}. Answer again with only the JSON object.")
        return "\n\n".join(parts)

    def ask(self, data: str) -> tuple[Verdict | None, str | None]:
        """(the verdict, None) or (None, why not). Up to MAX_RETRIES more calls for a bad answer."""
        why: str | None = None
        for _ in range(1 + MAX_RETRIES):
            if capped := self.room():
                return None, capped
            left = self.cfg.model.budget_usd - self.t.cost_usd
            r = self.model(self.instructions(why), data, left)
            self.t.calls += 1
            self.t.tokens_in += r.tokens_in
            self.t.tokens_out += r.tokens_out
            self.t.cost_usd += r.cost_usd
            if not r.ok:
                self.model_down = True
                return None, "model_error"
            try:
                return parse_verdict(r.text), None
            except SchemaError as e:
                why = str(e)
        return None, "bad_answer"

    # -- the pipeline

    def run(self) -> Totals:
        for name in self.cfg.mailboxes:
            try:
                self.mailbox(name)
            except mail.MailError as e:
                self.fail(name, e)
        return self.t

    def since(self, st: dict | None):
        if st and st.get("updated"):  # a day back, because a search by date is by day; the uid decides what is new
            back = 1 if st.get("last_uid") else 0  # an empty mailbox "from now" has nothing earlier to find
            return datetime.fromisoformat(st["updated"]).astimezone().date() - timedelta(days=back)
        return self.now.astimezone().date() - timedelta(days=self.cfg.first_run_days)

    def mailbox(self, name: str) -> None:
        route = self.routes[name]
        st = self.state["mailboxes"].get(name)
        uv, found, total = route.search(self.since(st), self.cfg.query.get(name))
        fresh = st is None or st.get("uidvalidity") != uv
        if fresh and st is not None:
            self.t.notes.append({"mailbox": name, "kind": "uidvalidity_changed"})
        last = 0 if fresh else int(st.get("last_uid", 0))
        if fresh and self.cfg.first_run_days == 0:  # "start from now": remember where the mailbox is, read nothing
            self.keep(name, uv, max(found, default=0), [])
            return
        pending = [] if fresh else [int(u) for u in st.get("pending", [])]
        new = sorted({u for u in found if u > last} | set(pending))
        if total > len(found):
            self.t.notes.append({"mailbox": name, "kind": "more_than_one_search_page", "total": total})
        if not new:
            self.keep(name, uv, max([last, *found]), [])
            return
        own = route.user().lower()
        asks: dict[str, list[Message]] = {}
        done = last
        for uid in new:
            try:
                msg = read_message(name, route.fetch(route.ref(uv, uid)), uid)
            except mail.MailError as e:
                self.fail(name, e)
                break  # this one and the rest are tried again next run
            self.t.seen += 1
            label, rule = (
                (None, "builtin:self")
                if msg.sender == own
                else decide(self.rules, Facts(name, msg.sender, msg.subject, msg.bulk))
            )
            if len(self.t.decisions) < MAX_DECISIONS:
                self.t.decisions.append({"mailbox": name, "uid": uid, "rule": rule, "label": label or "none"})
            if label == ASK_MODEL:
                asks.setdefault(msg.thread, []).append(msg)
            elif label:
                self.t.by_rule += 1
                try:
                    self.apply(route, msg.ref, label)
                except mail.MailError as e:
                    self.fail(name, e)
                    self.t.seen -= 1
                    break
            done = max(done, uid)
        deferred = self.threads(name, route, asks)
        self.keep(name, uv, done, deferred)

    def apply(self, route: Route, ref: str, label: str) -> None:
        if not self.dry:
            route.label(ref, label)
        self.t.label(label)

    def flag(self, name: str, m: Message, why: str, show: bool = True) -> None:
        who = f"{_short(m.name or m.sender, 30)}, \u201c{_short(m.subject, 40)}\u201d"
        self.t.flagged.append({"mailbox": name, "uid": m.uid, "why": why, "who": who, "show": show})

    def fail(self, name: str, e: mail.MailError) -> None:
        self.t.errors.append({"mailbox": name, "kind": e.kind})

    def threads(self, name: str, route: Route, asks: dict[str, list[Message]]) -> list[int]:
        """The model step for each thread; returns the uids of the threads a cap held back."""
        held: list[int] = []
        for msgs in asks.values():
            newest = max(msgs, key=lambda m: m.uid)
            self.t.asked += 1
            data = thread_data(msgs, self.cfg.model.thread_tokens)
            verdict, why = self.ask(data)
            if verdict is None:
                if why == "bad_answer":
                    self.flag(name, newest, "the model's answer did not parse")
                else:  # a cap or a failed runner: the thread waits for the next run
                    held += [m.uid for m in msgs]
                    self.t.deferred += 1
                    self.flag(name, newest, f"held for the next run: {why}", show=False)
                continue
            try:
                self.apply(route, newest.ref, LABEL_REPLY if verdict.needs_reply else LABEL_FYI)
            except mail.MailError as e:
                self.fail(name, e)
                continue
            if not verdict.needs_reply:
                continue
            if asks_for_time(data):
                self.flag(name, newest, SCHEDULING_FLAG)
            if self.t.drafts >= self.cfg.max_drafts:
                self.flag(name, newest, "needs a reply; the draft limit for the run was reached")
                continue
            refusal = check_draft(verdict.draft or "", data)
            if refusal:
                self.t.refused.append({"mailbox": name, "uid": newest.uid, "reason": refusal})
                self.flag(name, newest, f"needs a reply; draft refused ({refusal})")
                continue
            if not self.dry:
                try:
                    route.draft(newest.ref, verdict.draft or "")
                except mail.MailError as e:
                    self.fail(name, e)
                    self.flag(name, newest, f"needs a reply; the draft was not saved ({e.kind})")
                    continue
            self.t.drafts += 1
        return held

    def keep(self, name: str, uv: int, last: int, held: list[int]) -> None:
        if self.dry:
            return
        self.state["mailboxes"][name] = {
            "uidvalidity": uv,
            "last_uid": last,
            "pending": sorted(set(held))[-MAX_PENDING:],
            "updated": self.now.astimezone(UTC).isoformat(timespec="seconds"),
        }

    def day_totals(self) -> dict:
        """Today's running totals, kept in the state file so each run of the day adds to the summary."""
        date = f"{self.now.astimezone():%Y-%m-%d}"
        day = self.state.get("day") or {}
        if day.get("date") != date:
            day = {"date": date, "threads": 0, "by_rule": 0, "labels": {}, "drafts": 0, "model_calls": 0,
                   "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0, "flags": []}  # fmt: skip
        t = self.t
        day["threads"] += t.seen
        day["by_rule"] += t.by_rule
        for k, v in t.labelled.items():
            day["labels"][k] = day["labels"].get(k, 0) + v
        day["drafts"] += t.drafts
        day["model_calls"] += t.calls
        day["tokens_in"] += t.tokens_in
        day["tokens_out"] += t.tokens_out
        day["cost_usd"] = round(day["cost_usd"] + t.cost_usd, 6)
        day["flags"] = [*day["flags"], *(f"{f['who']}: {f['why']}" for f in t.flagged if f["show"])][-MAX_FLAGS:]
        return day


def record_of(cfg: Config, run: Run, started: datetime, ended: datetime, run_id: str) -> dict:
    """The run's line for logs/triage.jsonl: counts, ids, tokens and money, never a sender, a subject or any text."""
    t = run.t
    return {
        "type": "ws.triage-run/1",
        "run_id": run_id,
        "job": cfg.name,
        "dry_run": run.dry,
        "started_at": runrecord.iso(started),
        "ended_at": runrecord.iso(ended),
        "mailboxes": list(cfg.mailboxes),
        "threads_seen": t.seen,
        "rule_labelled": t.by_rule,
        "labels": t.labelled,
        "threads_asked": t.asked,
        "model_calls": t.calls,
        "tokens_in": t.tokens_in,
        "tokens_out": t.tokens_out,
        "cost_usd": round(t.cost_usd, 6),
        "drafts": t.drafts,
        "refused": t.refused,
        "flagged": [{"mailbox": f["mailbox"], "uid": f["uid"], "why": f["why"]} for f in t.flagged],
        "deferred_threads": t.deferred,
        "errors": t.errors,
        "notes": t.notes,
        "decisions": t.decisions,
    }


# ---------------------------------------------------------------- the command


def refuse_walled(cfg: Config, pol: policy.Policy) -> None:
    """A mailbox whose class is not full access (or is not in the policy at all) is not this job's: refuse the run before
    any mail is read."""
    for box, cls in cfg.classes.items():
        c = pol.classes.get(cls)
        if c is None or c.access != "full":
            raise TriageError(
                "refused",
                f"mailbox {box!r} belongs to class {cls!r}, which the policy does not give full access",
                "take the mailbox out of this job's mailboxes",
                EXIT_REFUSED,
            )


def execute(
    cfg: Config,
    rules: Rules,
    routes: dict[str, Route],
    model: Model,
    *,
    state_file: Path,
    log: Path | None,
    now: datetime | None = None,
    dry_run: bool = False,
) -> dict:
    """One run: the pipeline, then the state, the record and the day's summary (a dry run writes none of the three
    files). Returns the record."""
    started = now or datetime.now(UTC)
    state = load_state(state_file)
    run = Run(cfg, rules, routes, model, state, started, dry_run)
    totals = run.run()
    ended = datetime.now(UTC) if now is None else started
    rec = record_of(cfg, run, started, ended, runrecord.new_run_id(started))
    if not dry_run:
        state["schema"] = SCHEMA_VERSION
        day = run.day_totals()
        state["day"] = day
        save_state(state_file, state)
        if log is not None:
            jsonl.append(log, jsonl.stamped(rec, SCHEMA_VERSION))
        if cfg.summary_dir is not None and (
            totals.seen or not (cfg.summary_dir / f"summary-{day['date']}.md").exists()
        ):
            write_summary(cfg.summary_dir, day, started)
    return rec


def _load(args: argparse.Namespace, env: dict | None) -> tuple[Config, Path | None]:
    """(settings, the folder their relative paths start from) from --job or --config."""
    if args.job:
        try:
            d = jobs.command_job(args.job, env)
        except FileNotFoundError:
            raise _bad(f"no job file for {args.job!r}", "the job is jobs/NAME.toml in ws's config folder") from None
        except jobs.JobError as e:
            raise _bad(str(e)) from None
        if d.get("command") != "triage" or not isinstance(d.get("triage"), dict):
            raise _bad(f"job {args.job}: not a triage job", 'it needs command = "triage" and a [triage] table')
        return parse_config(d["triage"], args.job, jobs.jobs_dir(env), env), jobs.jobs_dir(env)
    path = Path(args.config).expanduser()
    try:
        doc = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as e:
        raise _bad(f"{path}: {e if isinstance(e, tomllib.TOMLDecodeError) else type(e).__name__}") from None
    table = doc.get("triage", doc)
    if not isinstance(table, dict):
        raise _bad(f"{path}: triage must be a table")
    name = re.sub(r"[^A-Za-z0-9_-]", "-", path.stem) or "triage"  # the name is a lock and a state file
    return parse_config(table, name, path.resolve().parent, env), path.resolve().parent


def cmd_run(args: argparse.Namespace) -> int:
    env = os.environ
    cfg, _ = _load(args, env)
    try:
        rules = parse_rules(cfg.rules.read_text(encoding="utf-8"), str(cfg.rules))
    except (OSError, UnicodeDecodeError) as e:
        raise _bad(f"rules {str(cfg.rules)!r} cannot be read ({type(e).__name__})") from None
    try:
        refuse_walled(cfg, policy.load())
        client = mail.Mail.from_profile(env)
    except policy.PolicyError as e:
        raise TriageError("bad_config", str(e), "fix the policy, then run again", EXIT_USAGE) from None
    except mail.MailError as e:
        raise TriageError(e.kind, e.message, e.hint, EXIT_USAGE) from None
    unknown = [b for b in cfg.mailboxes if b not in client.boxes]
    if unknown:
        raise TriageError(
            "unknown_mailbox",
            f"no mailbox {unknown[0]!r} in the profile",
            "add it with `ws mail add` or `ws mail connect --web`",
            EXIT_USAGE,
        )
    routes = {b: MailAdapter(client, b) for b in cfg.mailboxes}
    work = cfg.work or paths.state_dir(env) / FOLDER_NAME / cfg.name / "work"
    with joblock.hold(f"triage-{cfg.name}", env) as lock:
        if lock is None:
            print(f"ws: triage {cfg.name} is already running; this start is skipped")
            return 0
        rec = execute(
            cfg, rules, routes, make_model(cfg, work, env),
            state_file=state_path(cfg.name, env), log=log_path(env), dry_run=args.dry_run,
        )  # fmt: skip
    if getattr(args, "json", False):
        output.result(rec)
    else:
        say(rec)
    return EXIT_FAILED if rec["errors"] else 0


def say(rec: dict) -> None:
    labels = ", ".join(f"{k} {v}" for k, v in sorted(rec["labels"].items())) or "none"
    print(
        f"ws: triage {rec['job']}{' (dry run)' if rec['dry_run'] else ''}: {rec['threads_seen']} new, "
        f"{rec['rule_labelled']} labelled by rule, {rec['threads_asked']} to the model in {rec['model_calls']} call(s), "
        f"{rec['tokens_in']} tokens in / {rec['tokens_out']} out, US${rec['cost_usd']:.4f}, "
        f"{rec['drafts']} draft(s), {len(rec['refused'])} refused, {len(rec['flagged'])} flagged"
    )
    print(f"ws: labels: {labels}")
    for e in rec["errors"]:
        print(f"ws: {e['mailbox']}: {e['kind']}")


def register(sub) -> None:
    t = sub.add_parser("triage", help="the inbox job: rules first, one small model call per thread, drafts only")
    ts = t.add_subparsers(dest="triage_command", required=True)
    r = ts.add_parser("run", help="triage the new mail of the job's mailboxes (labels and drafts, never a send)")
    src = r.add_mutually_exclusive_group(required=True)
    src.add_argument("--job", help='the job file jobs/NAME.toml: command = "triage" and a [triage] table')
    src.add_argument("--config", help="a TOML file with the same settings (its [triage] table, or the whole file)")
    r.add_argument(
        "--dry-run", action="store_true", help="no label, no draft, no state, no summary; the model is still called"
    )
    r.set_defaults(func=cmd_run)
