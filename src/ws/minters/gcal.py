"""Google Calendar kind: the broker holds the OAuth client and refresh token and runs a fixed table of calendar operations.

Registry entry (the stored value is written by `ws gcal connect NAME`, never typed by hand):

    [gcal]
    kind = "gcal"
    allow = ["read", "write"]          # optional; which `needs` of OPS this name may use (default both)
    timezone = "Pacific/Auckland"      # optional; the ws calendar's time zone when `connect` makes it

Why the broker and not the job: the refresh token is long-lived, so it stays in the process that is not an LLM (DEC-122),
and which calendar actions exist is fixed here, before any event or mail is read. Google holds the outer wall: the
consent asks only for SCOPES, so writes can reach only a calendar ws created ("ws"), and reads of his own calendars are
read-only. This table is the inner wall:

- OPS is the only table: list, freebusy, get (reads) and insert, patch (writes). Nothing deletes, moves, imports, uses
  quickAdd or changes a calendar; there is no way to name another API path.
- Writes go to the ws calendar only (its id is in the stored value, set when `connect` made it).
- An event body is a whitelist of fields (EVENT_FIELDS). `attendees` and `conferenceData` are refused by name, so no
  invite and no meeting link is ever sent; `sendUpdates=none` is forced on every write; `status` cannot be set, so
  ws never cancels (Google treats a cancelled event as deleted). Undo is a patch of the title and transparency.
- An insert's event id is derived from the caller's ws id (event_id), so a retried insert gets 409 from Google and the
  existing event is returned instead of a second one. `extendedProperties.private.ws_id` is the "ws made this" marker,
  and a patch on an event without it is refused.
- Each write type has its own switch, default off (DEC-131), kept by the broker (ws.broker, `gcal_switch`); this
  module never reads it.

What a caller gets back is data only: trimmed events (EVENT_OUT) and busy blocks. The client secret, refresh token and
access token never appear in a reply, an error or an exception text: Google's error text is never passed on, only the
HTTP status, and every reply is checked for the held values before it leaves.

Swap note (DEC-017): another calendar (CalDAV, Microsoft Graph) is a new module with the same OPS names and checks; the
broker calls `run` and nothing else, and ws-next talks to the OPS names only.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import re
import secrets as pysecrets
import ssl
import threading
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

KIND = "gcal"
OP = "gcal"
SWITCH_OP = "gcal_switch"
EXTRA_KEYS = frozenset({"allow", "timezone"})
NEEDS = ("read", "write")
WRITES = ("insert", "patch")  # one switch each, default off
SWITCH_PIN_NAME = "gcal-switch-pin"  # the store name of the PIN that turns a switch on (a person pastes it once)
PIN_MIN = 6
PIN_TRIES = 3
SCOPES = (
    "https://www.googleapis.com/auth/calendar.app.created",
    "https://www.googleapis.com/auth/calendar.events.owned.readonly",
    "https://www.googleapis.com/auth/calendar.freebusy",
)
API_HOST = "www.googleapis.com"
TOKEN_HOST = "oauth2.googleapis.com"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API = "/calendar/v3"
SOCKET_TIMEOUT_S = 20.0
CALL_DEADLINE_S = 45.0
MAX_REPLY = 2 * 1024 * 1024  # bytes read from Google for one call
MAX_EVENTS = 250
MAX_TEXT = 8000  # characters of a description, in and out
MAX_RANGE_S = 92 * 86400  # one list or freebusy call covers at most about a quarter
CALENDARS = ("primary", "ws")
EVENT_FIELDS = frozenset({"summary", "description", "location", "start", "end", "transparency", "reminders"})
REFUSED_FIELDS = frozenset({"attendees", "conferenceData", "status", "id", "recurrence", "extendedProperties"})
EVENT_OUT = ("id", "summary", "location", "start", "end", "status", "transparency", "updated", "htmlLink")
WS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$")
EVENT_ID_RE = re.compile(r"^[a-v0-9]{5,1024}$")  # Google's rule for a client-chosen id (base32hex)
GOOGLE_ID_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_@.-]{0,1023}$"
)  # a leading letter or digit: never "." or ".."  # ids Google made (a read of his own calendar)
CAL_ID_RE = re.compile(r"^[A-Za-z0-9_.@-]{1,300}$")
RFC3339_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d{1,6})?)?(Z|[+-]\d{2}:\d{2})$")
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
TZ_RE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+){0,2}$")
CLIENT_ID_RE = re.compile(r"^[0-9]{6,30}-[a-z0-9]{10,64}\.apps\.googleusercontent\.com$")
TOKEN_SKEW_S = 120.0


class GcalError(Exception):
    """Never carries a credential or Google's text. `code` is a broker refusal code."""

    def __init__(self, code: str, reason: str):
        super().__init__(f"{code}: {reason}")
        self.code = code
        self.reason = reason


@dataclass(frozen=True)
class Options:
    allow: tuple[str, ...] = NEEDS
    timezone: str = "Pacific/Auckland"


def parse(path: Path, name: str, t: dict) -> Options:
    from ws.secrets import SecretsError

    where = f"{path}: [{name}]"
    allow = t.get("allow", list(NEEDS))
    if not (isinstance(allow, list) and allow and all(a in NEEDS for a in allow)):
        raise SecretsError(f"{where} allow must be a list from {', '.join(NEEDS)}")
    tz = t.get("timezone", "Pacific/Auckland")
    if not (isinstance(tz, str) and TZ_RE.match(tz)):
        raise SecretsError(f'{where} timezone must be an IANA zone such as "Pacific/Auckland"')
    return Options(allow=tuple(n for n in NEEDS if n in allow), timezone=tz)


# ---------------------------------------------------------------- the stored value


@dataclass(frozen=True)
class Held:
    client_id: str
    client_secret: str
    refresh_token: str
    calendar_id: str

    def needles(self) -> tuple[str, ...]:
        return tuple(v for v in (self.client_secret, self.refresh_token) if len(v) >= 8)


def load_held(value: str) -> Held:
    try:
        d = json.loads(value)
    except ValueError:
        raise GcalError("store_unreadable", "the stored value is not one `ws gcal connect` wrote") from None
    if not isinstance(d, dict):
        raise GcalError("store_unreadable", "the stored value is not one `ws gcal connect` wrote")
    vals = [d.get(k) for k in ("client_id", "client_secret", "refresh_token", "calendar_id")]
    if not all(isinstance(v, str) and v for v in vals) or not CAL_ID_RE.match(vals[3]):
        raise GcalError("store_unreadable", "the stored value is missing a part: run `ws gcal connect` again")
    return Held(*vals)


def dump_held(h: Held) -> str:
    return json.dumps(
        {"client_id": h.client_id, "client_secret": h.client_secret, "refresh_token": h.refresh_token,
         "calendar_id": h.calendar_id, "v": 1},
        separators=(",", ":"),
    )  # fmt: skip


def event_id(ws_id: str) -> str:
    """The Google event id for a ws id: base32hex (a-v, 0-9) of its SHA-256, 52 characters. The same ws id always gives
    the same event id, so a retried insert is a 409, never a second event."""
    raw = base64.b32hexencode(hashlib.sha256(ws_id.encode()).digest()).decode().rstrip("=").lower()
    return raw


# ---------------------------------------------------------------- HTTPS (one seam, replaced in tests)


def _https(host: str, method: str, path: str, headers: dict, body: bytes | None, timeout: float) -> tuple[int, bytes]:
    """One request to a fixed Google host over verified TLS. Returns (status, body bytes up to MAX_REPLY)."""
    if host not in (API_HOST, TOKEN_HOST):
        raise GcalError("host_not_allowed", "not a Google Calendar host")
    conn = http.client.HTTPSConnection(host, 443, timeout=timeout, context=ssl.create_default_context())
    try:
        conn.request(method, path, body=body, headers=headers)
        r = conn.getresponse()
        data = r.read(MAX_REPLY + 1)
        if len(data) > MAX_REPLY:
            raise GcalError("upstream_refused", "Google's answer was larger than the broker reads")
        return r.status, data
    finally:
        conn.close()


def _call(host, method, path, headers, body, deadline) -> tuple[int, bytes]:
    left = deadline - time.monotonic()
    if left <= 0:
        raise GcalError("upstream_unreachable", "the call ran out of time")
    try:
        return _https(host, method, path, headers, body, min(SOCKET_TIMEOUT_S, left))
    except GcalError:
        raise
    except ssl.SSLError:
        raise GcalError("tls_failed", f"the TLS check of {host} failed") from None
    except (OSError, http.client.HTTPException):
        raise GcalError("upstream_unreachable", f"cannot reach {host}") from None


# ---------------------------------------------------------------- access tokens (in memory only)

_tokens: dict[str, tuple[str, float]] = {}  # sha256(refresh token) -> (access token, expiry)
_tokens_lock = threading.Lock()


def _key(h: Held) -> str:
    return hashlib.sha256(h.refresh_token.encode()).hexdigest()


def _refresh(h: Held, deadline: float) -> str:
    body = urllib.parse.urlencode(
        {"client_id": h.client_id, "client_secret": h.client_secret, "refresh_token": h.refresh_token,
         "grant_type": "refresh_token"}
    ).encode()  # fmt: skip
    status, data = _call(
        TOKEN_HOST, "POST", "/token", {"Content-Type": "application/x-www-form-urlencoded"}, body, deadline
    )
    if status in (400, 401):
        raise GcalError("login_refused", "Google refused the refresh token: run `ws gcal connect` again")
    if status != 200:
        raise GcalError("upstream_refused", f"Google's token endpoint answered {status}")
    try:
        d = json.loads(data)
        tok, life = d["access_token"], float(d.get("expires_in", 3600))
    except (ValueError, KeyError, TypeError):
        raise GcalError("upstream_refused", "Google's token answer could not be read") from None
    if not isinstance(tok, str) or not tok:
        raise GcalError("upstream_refused", "Google's token answer could not be read")
    with _tokens_lock:
        _tokens[_key(h)] = (tok, time.monotonic() + life - TOKEN_SKEW_S)
    return tok


def _access(h: Held, deadline: float, fresh: bool = False) -> str:
    with _tokens_lock:
        got = _tokens.get(_key(h))
    if got and not fresh and got[1] > time.monotonic():
        return got[0]
    return _refresh(h, deadline)


def forget_tokens() -> None:
    with _tokens_lock:
        _tokens.clear()


class _Api:
    """Calendar API calls for one op, with the access token added here and nowhere else."""

    def __init__(self, h: Held, deadline: float):
        self.h = h
        self.deadline = deadline
        self.seen: set[str] = set()  # access tokens used, to check the reply for

    def __call__(self, method: str, path: str, query: dict | None = None, body: dict | None = None):
        url = API + path + ("?" + urllib.parse.urlencode(query) if query else "")
        raw = None if body is None else json.dumps(body).encode()
        for attempt in (0, 1):
            tok = _access(self.h, self.deadline, fresh=attempt == 1)
            self.seen.add(tok)
            headers = {"Authorization": f"Bearer {tok}", "Accept": "application/json"}
            if raw is not None:
                headers["Content-Type"] = "application/json"
            status, data = _call(API_HOST, method, url, headers, raw, self.deadline)
            if status != 401:
                break
        try:
            parsed = json.loads(data) if data else {}
        except ValueError:
            parsed = None
        return status, parsed


# ---------------------------------------------------------------- checks


def _time(v: object, what: str) -> str:
    if not (isinstance(v, str) and RFC3339_RE.match(v)):
        raise GcalError("bad_request", f"{what} must be an RFC 3339 time with a zone, e.g. 2026-11-25T09:00:00+13:00")
    return v


def _epoch(v: str) -> float:
    from datetime import datetime

    return datetime.fromisoformat(v).timestamp()


def _range(a: dict) -> tuple[str, str]:
    lo, hi = _time(a.get("time_min"), "time_min"), _time(a.get("time_max"), "time_max")
    span = _epoch(hi) - _epoch(lo)
    if not 0 < span <= MAX_RANGE_S:
        raise GcalError("bad_request", "time_max must be after time_min and at most 92 days on")
    return lo, hi


def _calendar(a: dict) -> str:
    c = a.get("calendar", "primary")
    if c not in CALENDARS:
        raise GcalError("bad_request", f"calendar must be one of {', '.join(CALENDARS)}")
    return c


def _when(v: object, what: str) -> dict:
    if not isinstance(v, dict) or not v or set(v) - {"dateTime", "date", "timeZone"}:
        raise GcalError("bad_request", f"{what} must be {{dateTime[, timeZone]}} or {{date}}")
    if ("dateTime" in v) == ("date" in v):
        raise GcalError("bad_request", f"{what} takes dateTime or date, one of them")
    if "dateTime" in v:
        _time(v["dateTime"], f"{what}.dateTime")
    elif not (isinstance(v["date"], str) and DATE_RE.match(v["date"])):
        raise GcalError("bad_request", f"{what}.date must be YYYY-MM-DD")
    if "timeZone" in v and not (isinstance(v["timeZone"], str) and TZ_RE.match(v["timeZone"])):
        raise GcalError("bad_request", f"{what}.timeZone must be an IANA zone")
    return dict(v)


def _text(v: object, what: str, cap: int) -> str:
    if not isinstance(v, str) or len(v) > cap or re.search(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", v):
        raise GcalError("bad_request", f"{what} must be text of at most {cap} characters")
    return v


def _reminders(v: object) -> dict:
    if not isinstance(v, dict) or set(v) - {"useDefault", "overrides"} or not isinstance(v.get("useDefault"), bool):
        raise GcalError("bad_request", "reminders must be {useDefault: bool[, overrides]}")
    out: dict = {"useDefault": v["useDefault"]}
    if "overrides" in v:
        ov = v["overrides"]
        if not isinstance(ov, list) or len(ov) > 5:
            raise GcalError("bad_request", "reminders.overrides is a list of at most 5")
        clean = []
        for o in ov:
            if not (
                isinstance(o, dict) and set(o) == {"method", "minutes"} and o["method"] == "popup"
                and isinstance(o["minutes"], int) and not isinstance(o["minutes"], bool) and 0 <= o["minutes"] <= 40320
            ):  # fmt: skip
                raise GcalError("bad_request", "a reminder override is {method: popup, minutes: 0..40320}")
            clean.append({"method": "popup", "minutes": o["minutes"]})
        out["overrides"] = clean
    return out


def check_fields(ev: object, need_times: bool) -> dict:
    """The event body ws may send: only EVENT_FIELDS, each checked. Raises GcalError (bad_request or field_refused)."""
    if not isinstance(ev, dict) or not ev:
        raise GcalError("bad_request", "event must be an object of event fields")
    for k in ev:
        if k in REFUSED_FIELDS:
            raise GcalError("field_refused", f"{k} is never sent: ws adds no guests, links or status changes")
        if k not in EVENT_FIELDS:
            raise GcalError("field_refused", f"{k!r} is not one of {', '.join(sorted(EVENT_FIELDS))}")
    out: dict = {}
    if "summary" in ev:
        out["summary"] = _text(ev["summary"], "summary", 300)
    for k in ("description", "location"):
        if k in ev:
            out[k] = _text(ev[k], k, MAX_TEXT if k == "description" else 500)
    for k in ("start", "end"):
        if k in ev:
            out[k] = _when(ev[k], k)
    if "transparency" in ev:
        if ev["transparency"] not in ("opaque", "transparent"):
            raise GcalError("bad_request", "transparency is opaque or transparent")
        out["transparency"] = ev["transparency"]
    if "reminders" in ev:
        out["reminders"] = _reminders(ev["reminders"])
    if need_times and not ("summary" in out and "start" in out and "end" in out):
        raise GcalError("bad_request", "an insert needs summary, start and end")
    if ("start" in out) != ("end" in out):
        raise GcalError("bad_request", "start and end change together")
    if "start" in out and ("date" in out["start"]) != ("date" in out["end"]):
        raise GcalError("bad_request", "start and end are both dates or both times")
    return out


def _ws_id(a: dict) -> str:
    w = a.get("ws_id")
    if not (isinstance(w, str) and WS_ID_RE.match(w)):
        raise GcalError("bad_request", "ws_id must be a ws id: letters, digits and ._:- , at most 200")
    return w


def _marker(ev: dict) -> str | None:
    p = ev.get("extendedProperties")
    p = p.get("private") if isinstance(p, dict) else None
    w = p.get("ws_id") if isinstance(p, dict) else None
    return w if isinstance(w, str) else None


def _out(ev: dict) -> dict:
    o = {k: ev[k] for k in EVENT_OUT if k in ev}
    if isinstance(ev.get("description"), str):
        o["description"] = ev["description"][:MAX_TEXT]
    w = _marker(ev)
    if w is not None:
        o["ws_id"] = w
    return o


def _cal_id(h: Held, c: str) -> str:
    return urllib.parse.quote(h.calendar_id if c == "ws" else "primary", safe="")


def _status_error(status: int, what: str) -> GcalError:
    if status in (401, 403):
        return GcalError("upstream_refused", f"Google refused the {what} ({status}): the consent may not cover it")
    if status == 404:
        return GcalError("not_found", f"Google has no such {what}")
    return GcalError("upstream_refused", f"Google answered {status} to the {what}")


# ---------------------------------------------------------------- operations


def _check_list(a: dict) -> dict:
    lo, hi = _range(a)
    n = a.get("max", MAX_EVENTS)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= MAX_EVENTS:
        raise GcalError("bad_request", f"max is 1 to {MAX_EVENTS}")
    return {"calendar": _calendar(a), "time_min": lo, "time_max": hi, "max": n}


def _op_list(api: _Api, h: Held, opts: Options, a: dict) -> dict:
    q = {"timeMin": a["time_min"], "timeMax": a["time_max"], "singleEvents": "true", "orderBy": "startTime",
         "maxResults": str(a["max"]), "timeZone": opts.timezone}  # fmt: skip
    status, d = api("GET", f"/calendars/{_cal_id(h, a['calendar'])}/events", q)
    if status != 200 or not isinstance(d, dict):
        raise _status_error(status, "event list")
    items = [i for i in d.get("items", []) if isinstance(i, dict) and i.get("status") != "cancelled"]
    return {"events": [_out(i) for i in items], "more": bool(d.get("nextPageToken")), "calendar": a["calendar"]}


def _op_freebusy(api: _Api, h: Held, opts: Options, a: dict) -> dict:
    body = {"timeMin": a["time_min"], "timeMax": a["time_max"], "timeZone": opts.timezone,
            "items": [{"id": "primary"}, {"id": h.calendar_id}]}  # fmt: skip
    status, d = api("POST", "/freeBusy", None, body)
    if status != 200 or not isinstance(d, dict):
        raise _status_error(status, "free/busy query")
    busy = []
    for cid, cal in (d.get("calendars") or {}).items():
        for b in (cal or {}).get("busy", []) if isinstance(cal, dict) else []:
            if isinstance(b, dict) and isinstance(b.get("start"), str) and isinstance(b.get("end"), str):
                busy.append(
                    {"start": b["start"], "end": b["end"], "calendar": "ws" if cid == h.calendar_id else "primary"}
                )
    busy.sort(key=lambda b: b["start"])
    return {"busy": busy}


def _check_freebusy(a: dict) -> dict:
    lo, hi = _range(a)
    return {"time_min": lo, "time_max": hi}


def _check_get(a: dict) -> dict:
    c = _calendar(a)
    eid = a.get("event_id")
    if not (isinstance(eid, str) and GOOGLE_ID_RE.match(eid)):
        raise GcalError("bad_request", "event_id must be a Google event id")
    return {"calendar": c, "event_id": eid}


def _get(api: _Api, h: Held, c: str, eid: str) -> dict:
    status, d = api("GET", f"/calendars/{_cal_id(h, c)}/events/{urllib.parse.quote(eid, safe='')}")
    if status != 200 or not isinstance(d, dict):
        raise _status_error(status, "event")
    return d


def _op_get(api: _Api, h: Held, opts: Options, a: dict) -> dict:
    return {"event": _out(_get(api, h, a["calendar"], a["event_id"]))}


def _check_insert(a: dict) -> dict:
    return {"ws_id": _ws_id(a), "event": check_fields(a.get("event"), need_times=True)}


def _op_insert(api: _Api, h: Held, opts: Options, a: dict) -> dict:
    eid = event_id(a["ws_id"])
    body = {**a["event"], "id": eid, "extendedProperties": {"private": {"ws_id": a["ws_id"]}}}
    status, d = api("POST", f"/calendars/{_cal_id(h, 'ws')}/events", {"sendUpdates": "none"}, body)
    if status in (200, 201) and isinstance(d, dict):
        return {"event": _out(d), "existing": False}
    if status == 409:  # this ws id was inserted before: the retry gets the first event, never a second
        ev = _get(api, h, "ws", eid)
        if _marker(ev) != a["ws_id"]:
            raise GcalError("conflict", "an event with this id exists and is not this ws id's")
        if ev.get("status") == "cancelled":  # he deleted it by hand: that is his answer, not a success to report
            raise GcalError("deleted", "this ws id's event was deleted by hand; ws does not make it again")
        return {"event": _out(ev), "existing": True}
    raise _status_error(status, "insert")


def _check_patch(a: dict) -> dict:
    eid = a.get("event_id")
    if not (isinstance(eid, str) and EVENT_ID_RE.match(eid)):
        raise GcalError("bad_request", "event_id must be an id ws gave (a-v, 0-9)")
    return {"event_id": eid, "event": check_fields(a.get("event"), need_times=False)}


def _op_patch(api: _Api, h: Held, opts: Options, a: dict) -> dict:
    cur = _get(api, h, "ws", a["event_id"])
    if _marker(cur) is None:
        raise GcalError("not_ws_event", "this event has no ws marker, so ws does not change it")
    path = f"/calendars/{_cal_id(h, 'ws')}/events/{urllib.parse.quote(a['event_id'], safe='')}"
    status, d = api("PATCH", path, {"sendUpdates": "none"}, a["event"])
    if status != 200 or not isinstance(d, dict):
        raise _status_error(status, "patch")
    return {"event": _out(d)}


@dataclass(frozen=True)
class Op:
    needs: str
    check: Callable[[dict], dict]
    handler: Callable[[_Api, Held, Options, dict], dict]

    @property
    def write(self) -> bool:
        return self.needs == "write"


OPS: dict[str, Op] = {
    "list": Op("read", _check_list, _op_list),
    "freebusy": Op("read", _check_freebusy, _op_freebusy),
    "get": Op("read", _check_get, _op_get),
    "insert": Op("write", _check_insert, _op_insert),
    "patch": Op("write", _check_patch, _op_patch),
}
assert tuple(k for k, o in OPS.items() if o.write) == WRITES


def check(opts: Options, cmd: object, args: object) -> tuple[Op, dict]:
    """The op and its checked args, before any credential is read. Raises GcalError."""
    if not isinstance(cmd, str) or cmd not in OPS:
        raise GcalError("op_refused", f"the calendar operations are {', '.join(OPS)}")
    op = OPS[cmd]
    if op.needs not in opts.allow:
        raise GcalError("op_refused", f"this name does not allow {op.needs}")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        raise GcalError("bad_request", "args must be an object")
    return op, op.check(args)


def _scrub(out: dict, needles: set[str]) -> dict:
    text = json.dumps(out)
    if any(n and n in text for n in needles):
        raise GcalError("upstream_refused", "Google's answer held a credential, so it is not given")
    return out


def run(opts: Options, value: str, cmd: str, args: object) -> dict:
    """Run one OPS entry with the stored value. Raises GcalError, whose text never holds a credential."""
    op, checked = check(opts, cmd, args)
    h = load_held(value)
    api = _Api(h, time.monotonic() + CALL_DEADLINE_S)
    try:
        out = op.handler(api, h, opts, checked)
    except GcalError:
        raise
    except (ValueError, KeyError, TypeError, AttributeError):
        raise GcalError("upstream_refused", "Google's answer could not be read") from None
    return _scrub(out, set(h.needles()) | api.seen)


# ---------------------------------------------------------------- consent (run by `ws gcal connect`, never the broker)


def pkce() -> tuple[str, str]:
    verifier = pysecrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
    return verifier, challenge


def auth_url(client_id: str, redirect: str, state: str, challenge: str) -> str:
    q = {"client_id": client_id, "redirect_uri": redirect, "response_type": "code", "scope": " ".join(SCOPES),
         "access_type": "offline", "prompt": "consent", "state": state, "code_challenge": challenge,
         "code_challenge_method": "S256"}  # fmt: skip
    return AUTH_URL + "?" + urllib.parse.urlencode(q, quote_via=urllib.parse.quote)


def code_from(pasted: str, state: str) -> str:
    """The authorization code from the address the browser ended on (or the bare code). Checks the state."""
    s = pasted.strip()
    if "?" not in s and "=" not in s:
        if not re.fullmatch(r"[A-Za-z0-9/_.~-]{10,512}", s):
            raise GcalError("bad_request", "that is not an address from Google's consent or a code")
        return s
    q = urllib.parse.parse_qs(urllib.parse.urlsplit(s).query)
    if q.get("error"):
        raise GcalError("login_refused", f"Google said: {q['error'][0][:40]} (the consent was not given)")
    if q.get("state", [""])[0] != state:
        raise GcalError("bad_request", "the address is from another consent attempt: start again")
    code = q.get("code", [""])[0]
    if not code:
        raise GcalError("bad_request", "the address has no code")
    got = set(q.get("scope", [""])[0].split())
    if got and not set(SCOPES) <= got:
        raise GcalError("login_refused", "not every calendar permission was ticked: start again and tick them all")
    return code


def exchange(client_id: str, client_secret: str, code: str, verifier: str, redirect: str) -> str:
    """The refresh token for an authorization code."""
    body = urllib.parse.urlencode(
        {"client_id": client_id, "client_secret": client_secret, "code": code, "code_verifier": verifier,
         "redirect_uri": redirect, "grant_type": "authorization_code"}
    ).encode()  # fmt: skip
    status, data = _call(
        TOKEN_HOST, "POST", "/token", {"Content-Type": "application/x-www-form-urlencoded"}, body,
        time.monotonic() + CALL_DEADLINE_S,
    )  # fmt: skip
    if status != 200:
        raise GcalError("login_refused", f"Google did not accept the code ({status}): start again")
    try:
        rt = json.loads(data)["refresh_token"]
    except (ValueError, KeyError, TypeError):
        raise GcalError("login_refused", "Google gave no refresh token: remove ws's access and start again") from None
    if not isinstance(rt, str) or not rt:
        raise GcalError("login_refused", "Google gave no refresh token")
    return rt


def ensure_calendar(client_id: str, client_secret: str, refresh_token: str, tz: str, old_id: str | None) -> str:
    """The ws calendar's id: the one made before when it is still there, else a new one named "ws"."""
    h = Held(client_id, client_secret, refresh_token, old_id or "none")
    api = _Api(h, time.monotonic() + CALL_DEADLINE_S)
    if old_id:
        status, d = api("GET", f"/calendars/{urllib.parse.quote(old_id, safe='')}")
        if status == 200 and isinstance(d, dict) and d.get("id") == old_id:
            return old_id
    status, d = api(
        "POST", "/calendars", None, {"summary": "ws", "description": "Made and changed by ws.", "timeZone": tz}
    )
    if status != 200 or not isinstance(d, dict) or not isinstance(d.get("id"), str) or not CAL_ID_RE.match(d["id"]):
        raise _status_error(status, "new ws calendar")
    return d["id"]
