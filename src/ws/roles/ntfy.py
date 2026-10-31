"""Notify role, `ntfy`: a push to your phone through an ntfy topic.

    [roles.notify]
    adapter = "ntfy"
    url = "https://ntfy.example.org/my-topic"     the topic URL (https only); its host is the one the broker allows
    token = "ntfy"                                optional: the secret's name in secrets.toml (host = the URL's host)

The interruption cap is part of the design. The numbers come from `[interrupts]` in policy.toml (1 digest and 3 pushes
a day unless you changed them) and are counted in `$WS_HOME/logs/notify-count.json`: `logs` is a control path, so a
session cannot write the counter. The day is the date in the profile's time zone (`[machine] timezone`, or `timezone`
in `[roles.notify]`; UTC if neither is set). A day is never counted down: if the stored date is later than today's
(the clock or the zone moved back), the stored day keeps counting, so changing zone does not reset the count. A push
over the cap is refused, not queued, and says so. `ws notify test` sends a push and counts as one.
With a token, the send goes through a broker lease (the broker adds the Authorization header; ws never reads the
token); without one it is a plain request to the topic.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from ws import broker, policy
from ws import secrets as wsecrets
from ws.profile import ProfileError, load, profile_path, ws_home
from ws.roles import FAIL, OK, WARN

ROLE, NAME = "notify", "ntfy"
KINDS = ("push", "digest")
NOUN = {"push": "pushes", "digest": "digest"}
COUNT_FILE = "logs/notify-count.json"
MAX_MESSAGE = 4000
TIMEOUT_S = 10.0
_TOPIC = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class NotifyError(Exception):
    """A problem the person can fix; the message is safe to print (no token, no lease URL)."""


class CapReached(NotifyError):
    pass


def target(cfg: dict) -> tuple[str, str, str]:
    """(topic URL, host, topic). Raises NotifyError naming the key."""
    url = cfg.get("url")
    if not isinstance(url, str) or not url:
        raise NotifyError("no url set: the ntfy topic URL, for example https://ntfy.example.org/my-topic")
    u = urllib.parse.urlsplit(url)
    topic = u.path.strip("/")
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.query or u.fragment:
        raise NotifyError("url must be an https topic URL with no login, query or fragment")
    if not _TOPIC.match(topic):
        raise NotifyError("url must end in one topic name (letters, digits, - and _)")
    host = u.netloc.lower()
    if not wsecrets.HOST_RE.match(host):
        raise NotifyError("url has a host name ws cannot use")
    return f"https://{host}/{topic}", host, topic


def token_name(cfg: dict) -> str | None:
    t = cfg.get("token")
    if t is None:
        return None
    if not isinstance(t, str) or not wsecrets.NAME_RE.match(t):
        raise NotifyError("token must be the name of a secret (letters, digits, - and _)")
    return t


# ---- the daily counter ---------------------------------------------------------------------------------------------


def counter_path(env) -> Path:
    return ws_home(env) / COUNT_FILE


def caps(env) -> dict[str, int]:
    """Today's allowance per kind, from the policy's [interrupts]. Raises NotifyError if the policy does not read."""
    try:
        it = policy.load(policy.policy_path(env)).interrupts
    except policy.PolicyError as e:
        raise NotifyError(f"policy: {e}") from e
    return {"push": it.pushes_per_day, "digest": it.digest_per_day}


def zone(cfg: dict, env) -> tzinfo:
    """The time zone whose date counts as 'today': [roles.notify] timezone, else [machine] timezone, else UTC."""
    name = cfg.get("timezone")
    if name is None:
        try:
            machine = (load(profile_path(env)) or {}).get("machine")
        except ProfileError:
            machine = None
        name = machine.get("timezone") if isinstance(machine, dict) else None
    if name is None or name == "UTC":
        return UTC
    try:
        if not isinstance(name, str):
            raise TypeError(name)
        return ZoneInfo(name)
    except (TypeError, ValueError, OSError, KeyError) as e:  # ZoneInfoNotFoundError is a KeyError
        raise NotifyError(f"timezone {name!r} is not a time zone name (for example Pacific/Auckland)") from e


def today(tz: tzinfo = UTC, now: datetime | None = None) -> str:
    return (now or datetime.now(UTC)).astimezone(tz).date().isoformat()


def _fresh(day: str) -> dict:
    return {"date": day, "push": 0, "digest": 0}


def read_counts(env, day: str) -> dict:
    """The counts to use for `day`. A missing or damaged file is a fresh day. A stored date after `day` wins: the count
    never goes back to zero because the date moved back."""
    try:
        d = json.loads(counter_path(env).read_text(encoding="utf-8"))
        stored = date.fromisoformat(d["date"]).isoformat()
        if all(isinstance(d.get(k), int) and not isinstance(d[k], bool) and d[k] >= 0 for k in KINDS) and stored >= day:
            return {"date": stored, "push": d["push"], "digest": d["digest"]}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return _fresh(day)


@contextlib.contextmanager
def _locked(env, day: str):
    """Hold the counter file's lock across 'check the cap, send, count', so two sends cannot both take the last slot.
    Yields (counts, save)."""
    path = counter_path(env)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        counts = read_counts(env, day)

        def save() -> None:
            os.ftruncate(fd, 0)
            os.pwrite(fd, (json.dumps(counts) + "\n").encode(), 0)
            os.fsync(fd)

        yield counts, save
    finally:
        os.close(fd)  # closing releases the lock


# ---- sending -----------------------------------------------------------------------------------------------------


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


def post(url: str, body: bytes, headers: dict[str, str]) -> int:
    """POST and return the status. Never follows a redirect. The default sender; tests pass their own."""
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    handlers: list = [_NoRedirect]
    if urllib.parse.urlsplit(url).hostname == "127.0.0.1":
        handlers.append(urllib.request.ProxyHandler({}))  # a broker lease is on this machine: never via a proxy
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(req, timeout=TIMEOUT_S) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise NotifyError(f"the push did not go out ({type(e).__name__})") from e


def _header(text: str) -> str:
    """A header value: one line of ASCII (ntfy decodes UTF-8 headers, but a plain value is safe everywhere)."""
    return _CONTROL.sub(" ", text).encode("ascii", "replace").decode("ascii")[:200]


def deliver(cfg: dict, env, title: str, message: str, send=None) -> None:
    """One message to the topic, through a broker lease if a token is named. Raises NotifyError."""
    send = send or post
    url, host, topic = target(cfg)
    headers = {"Title": _header(title), "Content-Type": "text/plain; charset=utf-8"}
    body = message.encode("utf-8")
    name = token_name(cfg)
    lease = None
    if name:
        try:
            lease = broker.request(name, "notify", host, socket=wsecrets.socket_path(env))
        except broker.BrokerError as e:
            raise NotifyError(f"the broker would not lease {name} for {host}: {e}") from e
        url = f"{lease.base_url}/{topic}"
    try:
        status = send(url, body, headers)
    finally:
        if lease is not None:
            with contextlib.suppress(broker.BrokerError):
                broker.release(lease, wsecrets.socket_path(env))
    if not 200 <= status < 300:
        raise NotifyError(f"ntfy answered {status}")


def send(
    cfg: dict, env, message: str, title: str = "ws", kind: str = "push", now: datetime | None = None, sender=None
) -> str:
    """Send a push or a digest if today's cap allows; count it once it has gone. Returns a line for the person.
    Raises CapReached (nothing sent) or NotifyError."""
    if kind not in KINDS:
        raise NotifyError(f"kind must be push or digest, not {kind!r}")
    if not message.strip():
        raise NotifyError("nothing to send: the message is empty")
    cap = caps(env)
    day = today(zone(cfg, env), now)
    with _locked(env, day) as (counts, save):
        if counts[kind] >= cap[kind]:
            raise CapReached(
                f"not sent: {counts[kind]} of {cap[kind]} {NOUN[kind]} already went today"
                " (the limit is [interrupts] in your policy)"
            )
        deliver(cfg, env, title, message[:MAX_MESSAGE], sender)
        counts[kind] += 1
        save()
    return f"sent: {counts[kind]} of {cap[kind]} {NOUN[kind]} today"


def send_test(cfg: dict, env, now: datetime | None = None, sender=None) -> str:
    """A test push. It counts as a push, so a loop of tests cannot get past the cap."""
    return send(cfg, env, "ws notify test: if you can read this, pushes work.", "ws test", "push", now, sender)


def check(cfg, osi, env, network):
    try:
        _, host, topic = target(cfg)
        name = token_name(cfg)
    except NotifyError as e:
        return FAIL, str(e)
    try:
        cap, tz = caps(env), zone(cfg, env)
    except NotifyError as e:
        return FAIL, str(e)
    counts = read_counts(env, today(tz))
    used = f"{counts['push']}/{cap['push']} pushes, {counts['digest']}/{cap['digest']} digest today"
    if name is None:
        return OK, f"ntfy topic {topic} at {host} (no token: anyone who knows the topic can read it); {used}"
    try:
        entry = wsecrets.load_registry(wsecrets.registry_path(env)).get(name)
    except wsecrets.SecretsError as e:
        return WARN, f"ntfy at {host}; the secrets registry does not read: {e}"
    if entry is None or host not in entry.hosts:
        return WARN, f"ntfy at {host}; token {name} is not in the secrets registry for that host, so sends will fail"
    return OK, f"ntfy topic {topic} at {host}, token {name} through the broker; {used}"


def steps(cfg, answers, env):
    """None: the topic is in the profile and the token is the person's to add (`ws secret set`)."""
    return []
