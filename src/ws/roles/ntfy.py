"""Notify role, `ntfy`: a push to your phone through an ntfy topic.

    [roles.notify]
    adapter = "ntfy"
    url = "https://ntfy.example.org/my-topic"     the topic URL (https only); its host is the one the broker allows
    token = "ntfy"                                optional: the secret's name in secrets.toml (host = the URL's host)
    session_alerts = true                         optional, default true: `ws notify alert` pushes for your sessions
    alert_text = "title"                          optional: "title" sends the folder name, "full" adds the runner's message
    alerts_per_day = 50                           optional: the daily allowance for session alerts

The interruption cap governs ws's own sends, and only those. The numbers come from `[interrupts]` in policy.toml (1
digest and 3 pushes a day unless you changed them) and are counted in `$WS_HOME/logs/notify-count.json`: `logs` is a
control path, so a session cannot write the counter. It is not a wall: a session that runs ws with a `WS_HOME` of its
own gets a fresh counter, and one that can reach the topic (no token) can post to it directly. Counting notify leases
in the broker would close that and is a known gap.

The day is the date in `timezone` of `[roles.notify]`, else `[machine] timezone`, else the zone ws detects for the
machine (`$TZ`, `/etc/localtime`), else UTC. A day is never counted down: if the stored date is later than today's
(the clock or the zone moved back), the stored day keeps counting, so changing zone does not reset the count. A date
more than 2 days ahead is taken as a clock that jumped forward and is reset, so pushes do not stay frozen until then.
A push over the cap is refused, not queued, and says so. `ws notify test` sends a push and counts as one.
With a token, the send goes through a broker lease (the broker adds the Authorization header; ws never reads the
token); without one it is a plain request to the topic.

Session alerts are a third kind, `alert`, counted on their own in the same file against `alerts_per_day`, so a busy
morning of pushes leaves them room. `ws notify alert --runner R` is the hook entry: a Notification event pushes
"<folder> needs you" at high priority and a Stop event pushes "<folder> finished" at default priority. The folder is the
last part of the session's working directory. With `alert_text = "title"` that is all the topic sees; "full" adds the
runner's own message. Headless sessions (`WS_RUN_ID` set, `claude -p`, or a payload marked headless) send no alert.
Every send writes `$WS_HOME/logs/notify-last.json` as `{at, kind, ok, error}` (no message text) for the watchdog.
"""

from __future__ import annotations

import contextlib

try:
    import fcntl
except ImportError:  # native Windows: doctor must still load to point at WSL2
    fcntl = None  # type: ignore[assignment]
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, date, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

from ws import broker, modules, policy
from ws import secrets as wsecrets
from ws.profile import ProfileError, load, profile_path, ws_home
from ws.roles import FAIL, OK, WARN

ROLE, NAME = "notify", "ntfy"
KINDS = ("push", "digest", "alert")
MAX_AHEAD_DAYS = 2  # a stored date further ahead than this is a clock that jumped, not a zone change
NOUN = {"push": "pushes", "digest": "digest", "alert": "alerts"}
DEFAULT_ALERTS_PER_DAY = 50
COUNT_FILE = "logs/notify-count.json"
LAST_FILE = "logs/notify-last.json"
ALERT_EVENTS = {  # hook event name -> (what the alert says, priority)
    "Notification": ("needs you", "high"),
    "Stop": ("finished", None),
    "session.idle": ("finished", None),
}
HEADLESS_ENTRYPOINTS = ("sdk-cli",)
MAX_FOLDER = 64
MAX_RUNNER_MESSAGE = 300
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


def alert_cap(cfg: dict) -> int:
    """The daily allowance for session alerts: `alerts_per_day` of [roles.notify], default 50."""
    n = cfg.get("alerts_per_day", DEFAULT_ALERTS_PER_DAY)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise NotifyError("alerts_per_day must be a whole number, 0 or more")
    return n


def caps(env, cfg: dict | None = None) -> dict[str, int]:
    """Today's allowance per kind: pushes and digest from the policy's [interrupts], alerts from the profile. Raises
    NotifyError if the policy or the alert setting does not read."""
    try:
        it = policy.load(policy.policy_path(env)).interrupts
    except policy.PolicyError as e:
        raise NotifyError(f"policy: {e}") from e
    return {"push": it.pushes_per_day, "digest": it.digest_per_day, "alert": alert_cap(cfg or {})}


def cap_for(kind: str, cfg: dict, env) -> int:
    """One kind's allowance. Alerts come from the profile alone, so they don't wait on the policy file."""
    return alert_cap(cfg) if kind == "alert" else caps(env, cfg)[kind]


def zone(cfg: dict, env) -> tzinfo:
    """The time zone whose date counts as 'today': [roles.notify] timezone, else [machine] timezone, else the zone
    ws detects for this machine, else UTC. A name written in the profile that is not a zone is an error; a detected one
    that ZoneInfo cannot read is skipped."""
    name = cfg.get("timezone")
    if name is None:
        try:
            machine = (load(profile_path(env)) or {}).get("machine")
        except ProfileError:
            machine = None
        name = machine.get("timezone") if isinstance(machine, dict) else None
    if name is None:
        detected = modules._zone_name(env)
        try:
            return ZoneInfo(detected) if detected and detected != "UTC" else UTC
        except (ValueError, OSError, KeyError):
            return UTC
    if name == "UTC":
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
    return {"date": day, "push": 0, "digest": 0, "alert": 0}


def read_counts(env, day: str) -> dict:
    """The counts to use for `day`. A missing or damaged file is a fresh day. A stored date after `day` wins, so the count
    never goes back to zero because the date moved back; but one more than MAX_AHEAD_DAYS ahead is a clock that jumped
    forward, and starts the day afresh."""
    try:
        d = json.loads(counter_path(env).read_text(encoding="utf-8"))
        stored = date.fromisoformat(d["date"]).isoformat()
        ahead = (date.fromisoformat(stored) - date.fromisoformat(day)).days
        d.setdefault("alert", 0)  # a file written before alerts existed has no count for them
        if all(isinstance(d.get(k), int) and not isinstance(d[k], bool) and d[k] >= 0 for k in KINDS) and (
            0 <= ahead <= MAX_AHEAD_DAYS
        ):
            return {"date": stored, "push": d["push"], "digest": d["digest"], "alert": d["alert"]}
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
        if fcntl is not None:
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


def deliver(cfg: dict, env, title: str, message: str, send=None, priority: str | None = None) -> None:
    """One message to the topic, through a broker lease if a token is named. Raises NotifyError."""
    send = send or post
    url, host, topic = target(cfg)
    headers = {"Title": _header(title), "Content-Type": "text/plain; charset=utf-8"}
    if priority:
        headers["Priority"] = priority
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


def record_last(env, kind: str, ok: bool, error: str | None, now: datetime | None = None) -> None:
    """Write `$WS_HOME/logs/notify-last.json`: when the last send went, of which kind, and whether it landed. No message
    text, so the watchdog can read it freely. Best effort: a full disk doesn't turn a sent push into an error."""
    path = ws_home(env) / LAST_FILE
    doc = {"at": (now or datetime.now(UTC)).astimezone(UTC).isoformat(), "kind": kind, "ok": ok, "error": error}
    tmp = path.with_name(path.name + ".tmp")
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        os.replace(tmp, path)


def send(
    cfg: dict,
    env,
    message: str,
    title: str = "ws",
    kind: str = "push",
    now: datetime | None = None,
    sender=None,
    priority: str | None = None,
) -> str:
    """Send a push, a digest or an alert if today's cap allows; count it once it has gone. Returns a line for the
    person. Raises CapReached (nothing sent) or NotifyError."""
    if kind not in KINDS:
        raise NotifyError(f"kind must be push, digest or alert, not {kind!r}")
    if not message.strip():
        raise NotifyError("nothing to send: the message is empty")
    cap = cap_for(kind, cfg, env)
    day = today(zone(cfg, env), now)
    where = "alerts_per_day in [roles.notify]" if kind == "alert" else "[interrupts] in your policy"
    with _locked(env, day) as (counts, save):
        if counts[kind] >= cap:
            raise CapReached(
                f"not sent: {counts[kind]} of {cap} {NOUN[kind]} already went today (the limit is {where})"
            )
        try:
            deliver(cfg, env, title, message[:MAX_MESSAGE], sender, priority)
        except NotifyError as e:
            record_last(env, kind, False, str(e), now)
            raise
        counts[kind] += 1
        save()
        record_last(env, kind, True, None, now)
    return f"sent: {counts[kind]} of {cap} {NOUN[kind]} today"


# ---- session alerts ------------------------------------------------------------------------------------------------


def alerts_on(cfg: dict) -> bool:
    """`session_alerts` of [roles.notify]: true unless the person set it to false."""
    v = cfg.get("session_alerts", True)
    if not isinstance(v, bool):
        raise NotifyError("session_alerts must be true or false")
    return v


def is_headless(payload: dict, env) -> bool:
    """A `ws run` job, a `claude -p` session or a payload marked headless: nobody is at that session to be called."""
    return (
        bool(env.get("WS_RUN_ID"))
        or env.get("CLAUDE_CODE_ENTRYPOINT") in HEADLESS_ENTRYPOINTS
        or payload.get("headless") is True
    )


def _line(text: object, limit: int) -> str:
    return _CONTROL.sub(" ", text).strip()[:limit] if isinstance(text, str) else ""


def alert(cfg: dict, env, payload: object, runner: str, now: datetime | None = None, sender=None) -> str | None:
    """Push the alert a hook payload asks for. Returns the line from `send`, or None when there is nothing to send: a
    payload that isn't a Notification or Stop event, a headless session, or session_alerts set to false. Raises
    CapReached or NotifyError."""
    if not isinstance(payload, dict) or not alerts_on(cfg):
        return None
    name = payload.get("hook_event_name") or payload.get("event")
    event = ALERT_EVENTS.get(name) if isinstance(name, str) else None
    if event is None or is_headless(payload, env):
        return None
    said, priority = event
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else ""
    folder = _line(os.path.basename(cwd.rstrip("/\\")), MAX_FOLDER) or "session"
    message = f"{folder} {said}"
    mode = cfg.get("alert_text", "title")
    if mode not in ("title", "full"):
        raise NotifyError('alert_text must be "title" or "full"')
    if mode == "full" and (extra := _line(payload.get("message"), MAX_RUNNER_MESSAGE)):
        message = f"{message}: {extra}"
    return send(cfg, env, message, f"ws {_line(runner, 32)}".strip(), "alert", now, sender, priority)


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
        cap, tz = caps(env, cfg), zone(cfg, env)
    except NotifyError as e:
        return FAIL, str(e)
    counts = read_counts(env, today(tz))
    used = (
        f"{counts['push']}/{cap['push']} pushes, {counts['digest']}/{cap['digest']} digest,"
        f" {counts['alert']}/{cap['alert']} alerts today"
    )
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
