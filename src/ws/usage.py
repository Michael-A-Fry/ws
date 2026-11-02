"""The Console usage check: can ws read the provider's own usage report, the billing truth (DECISIONS 168)?

Anthropic: GET /v1/organizations/usage_report/messages with an Admin API key (sk-ant-admin...). Admin keys exist only
for organisations, not individual accounts; the check says so rather than failing silently. The key is read from the
environment variable the profile names and is never printed.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

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
