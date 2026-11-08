"""Usage signals: the provider's usage report, the subscription limits a statusline gives ws, spend counted once, and token units.

The Console usage check (DECISIONS 168) asks whether ws can read the provider's own usage report, the billing truth.
Anthropic: GET /v1/organizations/usage_report/messages with an Admin API key (sk-ant-admin...). Admin keys exist only
for organisations, not individual accounts; the check says so rather than failing silently. The key is read from the
environment variable the profile names and is never printed.

Subscription limits. Claude Code (2.1.80 and later, Pro and Max) pipes `rate_limits.five_hour` and
`rate_limits.seven_day` (`used_percentage` 0 to 100, `resets_at`) to the statusline command it runs. `ws usage
statusline` is that command, a raw one: it stashes them atomically at `<state>/usage/rate_limits.json` (a temporary
file, then a rename; 0600 in a 0700 folder) and writes a tombstone when the payload has none, so a plan change clears
old data. `read_rate_limits` is what admission and a checkpoint decision read: a capture older than `RATE_TTL_S`
(600 s), a tombstone, a window past its `resets_at` or a figure that is not a percentage all read as unknown, never as
room. ws never calls the OAuth usage endpoint with the subscription login. Installing the statusline command into a
person's Claude Code settings is a separate, deliberate step that this module does not take.

Spend counted once. `dedupe_usage` keeps one entry per model reply in a transcript: Claude Code writes the same reply
more than once (one line per content block, a replayed sidechain), so the key is the message id plus `requestId`; with no
`requestId` the key is scoped to the session and timestamp. A main-thread line beats a sidechain copy, then the larger
token total wins.

Token units. A runner's input count may or may not include its cache tokens. `otel_input_tokens` is the one converter at
the export edge: the OpenTelemetry convention counts cache in `input`, and Claude Code and OpenCode report it apart, so
they are added; Codex already includes it, so it is not added twice.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import paths
from ws.output import WsError

DEFAULT_BASE = "https://api.anthropic.com"
BASE_ENV = "WS_ANTHROPIC_API_BASE"
LOOPBACK = ("127.0.0.1", "localhost", "::1")
# Tests only: a base URL for a local stub server, set by a test with monkeypatch and read nowhere else. The environment
# cannot set it; it accepts http or https to a loopback address and nothing else.
TEST_BASE: str | None = None


class BaseRefused(WsError):
    """The base URL from the environment is not the provider's own host."""

    def __init__(self, why: str):
        super().__init__("refused", f"${BASE_ENV} {why}", f"unset ${BASE_ENV}, or set it to {DEFAULT_BASE}")


def env_base(environ: Mapping[str, str] | None = None) -> str:
    """The base URL the usage report is read from. `$WS_ANTHROPIC_API_BASE` is honoured only when it names the
    provider's own host (https, no user, port or query), because the Admin key is sent to it. Anything else raises
    BaseRefused, a loopback address included."""
    environ = os.environ if environ is None else environ
    value = environ.get(BASE_ENV)
    if not value:
        return DEFAULT_BASE
    if any(c.isspace() or ord(c) < 32 or c == "\\" for c in value):
        raise BaseRefused("has spaces, control characters or a backslash in it")
    try:
        parts = urllib.parse.urlsplit(value)
        host, port = (parts.hostname or "").lower(), parts.port
    except ValueError:
        raise BaseRefused("isn't a URL ws can read") from None
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise BaseRefused("names a user")
    if parts.query or parts.fragment or parts.path.strip("/"):
        raise BaseRefused("must be a host alone, with no path or query")
    own = parts.scheme == "https" and host == urllib.parse.urlsplit(DEFAULT_BASE).hostname and port in (None, 443)
    if not own:
        raise BaseRefused(f"must be {DEFAULT_BASE} (the provider's own host)")
    return value.rstrip("/")


def local_base(value: str) -> str:
    """A base URL for a local stub server (the `TEST_BASE` hook, or `check_anthropic`'s `base` argument): http or https
    to a loopback address, on any port, with no user, path or query. Anything else raises BaseRefused."""
    try:
        parts = urllib.parse.urlsplit(value)
        host = (parts.hostname or "").lower()
        parts.port  # noqa: B018 - raises ValueError for a port that isn't a number
    except ValueError:
        raise BaseRefused("isn't a URL ws can read") from None
    bare = not (parts.username or parts.password or "@" in parts.netloc or parts.query or parts.fragment)
    if parts.scheme not in ("http", "https") or host not in LOOPBACK or not bare or parts.path.strip("/"):
        raise BaseRefused("must be a loopback address, over http or https, with no path or query")
    return value.rstrip("/")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect would carry the admin key to another URL: treat it as an error instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)
API_VERSION = "2023-06-01"
TIMEOUT_S = 15


def check_anthropic(key: str, base: str | None = None, now: datetime | None = None) -> tuple[str, str]:
    """(status, detail) for one read of yesterday's usage. Status is ok or fail."""
    key = key.strip()
    if any(ord(c) < 33 or ord(c) == 127 for c in key):
        return "fail", "the key contains spaces or control characters; paste it again"
    if not key.startswith("sk-ant-admin"):
        return "fail", "the key is not an Admin API key (sk-ant-admin...); a normal API key cannot read usage"
    try:
        local = base or TEST_BASE
        base = local_base(local) if local else env_base()
    except BaseRefused as e:
        return "fail", f"{e}; the Admin key is sent only to the provider's own host"
    start = ((now or datetime.now(UTC)) - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    query = urllib.parse.urlencode({"starting_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "bucket_width": "1d"})
    req = urllib.request.Request(
        f"{base}/v1/organizations/usage_report/messages?{query}",
        headers={"x-api-key": key, "anthropic-version": API_VERSION, "user-agent": "ws-doctor"},
    )
    try:
        with _OPENER.open(req, timeout=TIMEOUT_S) as r:  # the provider over https, or a loopback stub (checked above)
            body = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return "fail", f"HTTP {e.code}: the key was refused (revoked, or not an organisation admin key)"
        if 300 <= e.code < 400:
            return "fail", f"HTTP {e.code}: the usage report redirected; ws does not follow it with the key"
        return "fail", f"HTTP {e.code} from the usage report"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return "fail", f"could not reach the usage report: {getattr(e, 'reason', e)}"
    except json.JSONDecodeError:
        return "fail", "the usage report did not return JSON"
    except ValueError:  # never echo: the message can carry the request headers
        return "fail", "the usage request could not be built"
    buckets = body.get("data")
    if not isinstance(buckets, list):
        return "fail", "the usage report had no data list"
    tokens = 0
    for b in buckets:
        for res in b.get("results", []) if isinstance(b, dict) else []:
            for field in ("uncached_input_tokens", "cache_read_input_tokens", "output_tokens"):
                v = res.get(field) if isinstance(res, dict) else None
                tokens += v if isinstance(v, int) else 0
    return "ok", f"usage report readable: {len(buckets)} day bucket(s), {tokens:,} tokens since {start:%Y-%m-%d}"


# ---------------------------------------------------------------------------------------------------------------------
# Subscription rate limits: the statusline capture

COMMAND = "usage"
RATE_TTL_S = 600  # a capture older than this reads as unknown; admission and checkpoint decisions share it
RATE_SCHEMA = 1
WINDOWS = ("five_hour", "seven_day")
STASH_NAME = "rate_limits.json"


@dataclass(frozen=True)
class Window:
    """One rate-limit window: how much is used (0 to 100) and when it resets (UTC)."""

    used_pct: float
    resets_at: datetime


@dataclass(frozen=True)
class RateLimits:
    """What the last statusline capture said, still fresh. A window that is unknown is None."""

    captured_at: datetime
    five_hour: Window | None
    seven_day: Window | None

    def exhausted(self) -> list[str]:
        """The windows at 100 percent or more."""
        return [n for n in WINDOWS if (w := getattr(self, n)) is not None and w.used_pct >= 100]


def stash_path(state: Path) -> Path:
    return Path(state) / "usage" / STASH_NAME


def _epoch_or_iso(v) -> datetime | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and math.isfinite(v) and v > 0:
        try:
            return datetime.fromtimestamp(v, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, str):
        try:
            t = datetime.fromisoformat(v)
        except ValueError:
            return None
        return t.astimezone(UTC) if t.tzinfo else None
    return None


def _pct(v) -> float | None:
    """A used percentage, or None. A figure above 101 is not a percentage (a known statusline bug puts an epoch there)."""
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v) if 0 <= v <= 101 else None


def _clean_windows(payload: object) -> dict[str, dict] | None:
    """{"five_hour": {...}, "seven_day": {...}} with the windows that hold a usable figure, or None when the payload has
    none. Each kept window is {"used_percentage": float, "resets_at": epoch seconds or None}."""
    limits = payload.get("rate_limits") if isinstance(payload, dict) else None
    if not isinstance(limits, dict):
        return None
    out: dict[str, dict] = {}
    for name in WINDOWS:
        w = limits.get(name)
        pct = _pct(w.get("used_percentage")) if isinstance(w, dict) else None
        if pct is None:
            continue
        resets = _epoch_or_iso(w.get("resets_at"))
        out[name] = {"used_percentage": pct, "resets_at": resets.timestamp() if resets else None}
    return out or None


def _write_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def capture_statusline(text: str, state: Path, now: datetime | None = None) -> str | None:
    """Stash the `rate_limits` of the JSON a statusline command gets on stdin. Returns "stashed" or "tombstone" for what
    was written, or None (nothing written) when `text` is not a JSON object. A payload with no usable window writes a
    tombstone, so data from an earlier plan is not read as current."""
    try:
        payload = json.loads(text)
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    when = (now or datetime.now(UTC)).astimezone(UTC)
    windows = _clean_windows(payload)
    doc = {"schema": RATE_SCHEMA, "captured_at": when.timestamp(), **(windows or {"absent": True})}
    _write_atomic(stash_path(state), doc)
    return "stashed" if windows else "tombstone"


def read_rate_limits(state: Path, now: datetime | None = None, ttl_s: float = RATE_TTL_S) -> RateLimits | None:
    """The fresh capture, or None for unknown: no file, an unreadable one, a tombstone, older than `ttl_s`, stamped in
    the future, or with no window still open. A window past its `resets_at` is dropped; the other is kept."""
    when = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        doc = json.loads(stash_path(state).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict) or doc.get("absent") or doc.get("schema") != RATE_SCHEMA:
        return None
    captured = _epoch_or_iso(doc.get("captured_at"))
    if captured is None or captured > when or (when - captured).total_seconds() > ttl_s:
        return None
    found: dict[str, Window | None] = {}
    for name in WINDOWS:
        w = doc.get(name)
        pct = _pct(w.get("used_percentage")) if isinstance(w, dict) else None
        resets = _epoch_or_iso(w.get("resets_at")) if isinstance(w, dict) else None
        found[name] = Window(pct, resets) if pct is not None and resets is not None and resets > when else None
    if not any(found.values()):
        return None
    return RateLimits(captured, found["five_hour"], found["seven_day"])


# ---------------------------------------------------------------------------------------------------------------------
# Spend counted once


def _usage_total(entry: dict) -> int:
    msg = entry.get("message")
    usage = msg.get("usage") if isinstance(msg, dict) else None
    if not isinstance(usage, dict):
        return 0
    return sum(v for v in usage.values() if isinstance(v, int) and not isinstance(v, bool))


def dedupe_key(entry: dict) -> tuple | None:
    """The key that names one model reply in a Claude Code transcript line, or None for a line with no message id (it
    carries no usage to count). `requestId` with the message id when there is one; otherwise the session and the
    timestamp stand in for it."""
    msg = entry.get("message")
    mid = msg.get("id") if isinstance(msg, dict) else None
    if not isinstance(mid, str) or not mid:
        return None
    request = entry.get("requestId")
    if isinstance(request, str) and request:
        return (mid, request)
    return (mid, entry.get("sessionId"), entry.get("timestamp"))


def dedupe_usage(entries: Iterable[dict]) -> list[dict]:
    """The transcript lines with each model reply once, in first-seen order. When a reply appears more than once, a
    main-thread line beats a sidechain copy, then the larger token total wins. Lines with no message id are dropped."""
    kept: dict[tuple, dict] = {}
    for e in entries:
        if not isinstance(e, dict) or (key := dedupe_key(e)) is None:
            continue
        old = kept.get(key)
        if old is None:
            kept[key] = e
            continue
        if (not e.get("isSidechain"), _usage_total(e)) > (not old.get("isSidechain"), _usage_total(old)):
            kept[key] = e
    return list(kept.values())


# ---------------------------------------------------------------------------------------------------------------------
# Token units

# Does the `input` count the runner reports already include its cache tokens?
INPUT_INCLUDES_CACHE = {"claude-code": False, "opencode": False, "codex": True}


def otel_input_tokens(runner: str, input_tokens: int, cache_read: int = 0, cache_creation: int = 0) -> int:
    """`input` as the OpenTelemetry convention counts it (cache included), from a runner's own figures. Raises KeyError
    for a runner whose convention is not known: guessing would count cache twice or not at all."""
    if INPUT_INCLUDES_CACHE[runner]:
        return int(input_tokens)
    return int(input_tokens) + int(cache_read) + int(cache_creation)


# ---------------------------------------------------------------------------------------------------------------------
# `ws usage statusline`


def cmd_statusline(args: argparse.Namespace) -> int:
    """The command Claude Code runs as its statusline: JSON on stdin, a line on stdout (empty here). It never fails the
    statusline: a payload it cannot read, or a folder it cannot write, ends quietly."""
    with contextlib.suppress(OSError, UnicodeDecodeError, ValueError):
        capture_statusline(sys.stdin.read(), paths.state_dir())
    print()
    return 0


def register(sub) -> None:
    u = sub.add_parser("usage", help="usage signals: the statusline command that stashes subscription limits")
    us = u.add_subparsers(dest="usage_command", required=True)
    s = us.add_parser(
        "statusline", help="a Claude Code statusline command: stash rate_limits from stdin (raw: no JSON envelope)"
    )
    s.set_defaults(func=cmd_statusline, raw=True)
