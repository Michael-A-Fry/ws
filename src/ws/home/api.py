"""The home data API: `snapshot` and the reads behind it (`lead`, `asks`, `sessions`, `plugins`, `evals`, `events`) and the one write, `answer`.

Every area, app and script reads home data through here and nowhere else. It reads the sources (ws.home.sources) in
order, drops any source that can't be read whole into one `sources[]` row with a typed reason, merges sessions by
(runner, session id), and applies the opaque rule last: an opaque record leaves with only what ws.home.records.KEEP
lists. Reading changes nothing. `answer` hands one answer to the source that gave the ask, which runs the profile's
`[home.answer]` command; ws never writes a source's file itself, and an opaque ask, an approval or a permission can't
be answered this way. The profile's `[home]` table is read and checked here; an unknown key is an error naming it.
"""

from __future__ import annotations

import fnmatch
import os
import platform
import re
import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ws import jsonl
from ws.home import records, template
from ws.home.records import Ask, Event, Plugin, Session, Snapshot, SourceRow
from ws.home.sources import FAILED, TIMEOUT, UNAVAILABLE, SourceError, _files, discover
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path, ws_home

HOME_KEYS = ("lead", "opaque_paths", "sources", "open", "start", "answer", "workspaces", "models", "deny")
SOURCE_KEYS = ("asks_jsonl", "events_dir", "otel_dir", "claude_dir")
OPEN_KEYS = ("session", "ask", "opaque")
START_NAMES = ("what", "workspace", "where", "runner", "model", "request")  # what `[home.start] command` may use
ANSWER_NAMES = ("id", "choice", "text")  # what `[home.answer] command` may use
ANSWER_TEXT_MAX = 2000
_LEAD = re.compile(r"^(cwd|name):\S.*$")
EVENT_WINDOW_S = 24 * 3600  # how far back a snapshot's events reach
RUN_TIMEOUT_S = 10.0

Runner = Callable[[list[str], float], tuple[int, str]]


# --- the profile's [home] table ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class HomeConfig:
    lead: str = ""
    opaque_paths: tuple[str, ...] = ()
    roots: tuple[str, ...] = ()  # opaque_paths as absolute paths, links followed (records.roots_of)
    sources: Mapping[str, str] = field(default_factory=dict)
    open: Mapping[str, str] = field(default_factory=dict)
    start: tuple[str, ...] = ()  # `[home.start] command`: the argument template that starts a session
    answer: tuple[str, ...] = ()  # `[home.answer] command`: the argument template that answers an ask
    workspaces: Mapping[str, str] = field(default_factory=dict)  # workspace name -> its data class
    models: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # runner -> the models it offers
    deny: Mapping[str, tuple[str, ...]] = field(default_factory=dict)  # runner or runner/model -> classes it can't take


def _bad(path: Path, message: str, hint: str) -> WsError:
    return WsError("bad_config", f"{path}: {message}", hint)


def _expand(text: str, home: Path) -> str:
    if text == "~":
        return str(home)
    return str(home / text[2:]) if text.startswith(("~/", "~\\")) else text


def _strings(table: object, allowed: tuple[str, ...], where: str, path: Path) -> dict[str, str]:
    if not isinstance(table, dict):
        raise _bad(path, f"{where} must be a table", f"write {where} as a table in the profile")
    for key in table:
        if key not in allowed:
            raise _bad(path, f"unknown key {key!r} in {where}", f"the keys are: {', '.join(allowed)}")
    for key, value in table.items():
        if not isinstance(value, str) or not value.strip():
            raise _bad(path, f"{where}.{key} must be a non-empty string", f"set {where}.{key} to text")
    return {k: v.strip() for k, v in table.items()}


def _command(table: object, where: str, path: Path, allowed: tuple[str, ...], required: str) -> tuple[str, ...]:
    """`[home.start]` or `[home.answer]`: one key, `command`, a list of words checked as a template."""
    if table is None:
        return ()
    if not isinstance(table, dict):
        raise _bad(path, f"{where} must be a table", f"write {where} as a table with a command list")
    for key in table:
        if key != "command":
            raise _bad(path, f"unknown key {key!r} in {where}", "the key is: command")
    words = table.get("command")
    if words is None:
        return ()
    if not isinstance(words, list) or not all(isinstance(w, str) and w for w in words):
        raise _bad(path, f"{where}.command must be a list of words", f'e.g. command = ["prog", "{{{required}}}"]')
    why = template.check(words, allowed, required)
    if why:
        raise _bad(path, f"{where}.command {why}", f"the names you can use are: {', '.join(allowed)}")
    return tuple(words)


def _free(table: object, where: str, path: Path, lists: bool) -> dict:
    """A table whose keys the person picks: each value a text (a class) or, when `lists`, a list of texts."""
    if table is None:
        return {}
    if not isinstance(table, dict):
        raise _bad(path, f"{where} must be a table", f"write {where} as a table")
    out: dict = {}
    for key, value in table.items():
        if lists:
            if not (isinstance(value, list) and all(isinstance(v, str) and v.strip() for v in value)):
                raise _bad(path, f"{where}.{key} must be a list of text", f'e.g. {key} = ["name"]')
            out[key] = tuple(v.strip() for v in value)
        else:
            if not isinstance(value, str) or not value.strip():
                raise _bad(path, f"{where}.{key} must be text", f'e.g. {key} = "financial"')
            out[key] = value.strip()
    return out


def load_config(environ: Mapping[str, str], home: Path) -> HomeConfig:
    """The profile's `[home]` table, checked. No profile or no table is the defaults; a bad one raises WsError."""
    path = profile_path(dict(environ))
    try:
        doc = load(path) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {path}") from e
    table = doc.get("home")
    if table is None:
        return HomeConfig()
    if not isinstance(table, dict):
        raise _bad(path, "[home] must be a table", "write [home] as a table")
    for key in table:
        if key not in HOME_KEYS:
            raise _bad(path, f"unknown key {key!r} in [home]", f"the keys are: {', '.join(HOME_KEYS)}")
    lead = table.get("lead", "")
    if not isinstance(lead, str) or (lead and not _LEAD.match(lead)):
        raise _bad(path, "home.lead must look like cwd:~/code or name:Home*", 'e.g. lead = "cwd:~/code"')
    paths = table.get("opaque_paths", [])
    if not isinstance(paths, list) or not all(isinstance(p, str) and p.strip() for p in paths):
        raise _bad(path, "home.opaque_paths must be a list of folders", 'e.g. opaque_paths = ["~/code/private"]')
    sources = _strings(table.get("sources", {}), SOURCE_KEYS, "[home.sources]", path)
    opens = _strings(table.get("open", {}), OPEN_KEYS, "[home.open]", path)
    for p in paths:
        if not os.path.isabs(_expand(p.strip(), home)):
            raise _bad(
                path,
                f"home.opaque_paths entry {p!r} isn't an absolute folder",
                'start it with / or ~/, e.g. opaque_paths = ["~/code/private"]',
            )
    expanded = tuple(dict.fromkeys(f for p in paths for f in (_expand(p.strip(), home), _expand(p, home))))
    answer = _command(table.get("answer"), "[home.answer]", path, ANSWER_NAMES, "id")
    if answer and not {"choice", "text"} & template.names(answer):
        raise _bad(
            path,
            "[home.answer].command must use {choice} or {text}, or an answer would be sent with nothing in it",
            'e.g. command = ["ws-ask", "answer", "{id}", "{choice}"]',
        )
    return HomeConfig(
        lead=lead,
        opaque_paths=tuple(paths),
        roots=records.roots_of(expanded),
        sources={k: _expand(v, home) for k, v in sources.items()},
        open=opens,
        start=_command(table.get("start"), "[home.start]", path, START_NAMES, "request"),
        answer=answer,
        workspaces=_free(table.get("workspaces"), "[home.workspaces]", path, lists=False),
        models=_free(table.get("models"), "[home.models]", path, lists=True),
        deny=_free(table.get("deny"), "[home.deny]", path, lists=True),
    )


# --- the environment a source reads in -----------------------------------------------------------------------------


def default_run(argv: list[str], timeout_s: float) -> tuple[int, str]:
    """Run `argv` (no shell) and return (exit code, stdout). A binary that isn't there raises FileNotFoundError; one
    that takes too long raises TimeoutError."""
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s, check=False, shell=False)
    except subprocess.TimeoutExpired as e:
        raise TimeoutError(f"{argv[0]} took longer than {timeout_s:g} s") from e
    return done.returncode, done.stdout


@dataclass
class Env:
    """What a source reads in: the environment, the checked `[home]` table, the clock and the one way to run a program.
    Tests pass their own `run`, `now` and `host`; nothing here is global."""

    environ: Mapping[str, str]
    cfg: HomeConfig
    home: Path
    ws_home: Path
    host: str
    now: float
    run: Runner = default_run
    event_since: float = 0.0
    seen: list[Session] = field(default_factory=list)  # sessions earlier sources gave (partial, unmerged)
    cache: dict = field(default_factory=dict)  # what a source with two kinds reads once

    @classmethod
    def make(
        cls,
        environ: Mapping[str, str] | None = None,
        *,
        run: Runner | None = None,
        now: float | None = None,
        host: str | None = None,
    ) -> Env:
        env = dict(os.environ if environ is None else environ)
        home = Path(env["HOME"]) if env.get("HOME") else Path.home()
        t = time.time() if now is None else now
        return cls(
            environ=env,
            cfg=load_config(env, home),
            home=home,
            ws_home=ws_home(env),
            host=host if host is not None else platform.node(),
            now=t,
            run=run or default_run,
            event_since=t - EVENT_WINDOW_S,
        )

    def path(self, key: str) -> Path:
        """Where a `[home.sources]` input lives: the profile's path, else the default under HOME."""
        if key in self.cfg.sources:
            return Path(self.cfg.sources[key])
        if key == "claude_dir":
            given = self.environ.get("CLAUDE_CONFIG_DIR")
            return Path(given) if given else self.home / ".claude"
        usage = self.home / ".claude-usage"
        return {
            "asks_jsonl": usage / "asks" / "asks.jsonl",
            "events_dir": usage / "events",
            "otel_dir": usage / "otel",
        }[key]

    def sessions_so_far(self) -> list[Session]:
        """The sessions earlier sources gave, merged, each with its opaque verdict set. A source that must not open an
        opaque session's file asks here."""
        return [
            replace(s, opaque=records.is_opaque(s, self.cfg.roots)) for s in merge_sessions(self.seen, self.cfg.roots)
        ]


# --- merging --------------------------------------------------------------------------------------------------------

_RANK = {"waiting": 0, "working": 1, "idle": 2, "unknown": 3, "ended": 4}


def _first(*values):
    return next((v for v in values if v not in (None, "", ())), values[-1])


def _later(a: str | None, b: str | None) -> str | None:
    return max((x for x in (a, b) if x), default=None)


def _join(a: Session, b: Session) -> Session:
    """`a` filled in from `b`: the earlier source wins a field; the later of two times; opaque if either says so."""
    cost_from_b = a.cost_usd is None and b.cost_usd is not None
    sources = [*a.source.split("+"), *(s for s in b.source.split("+") if s not in a.source.split("+"))]
    return Session(
        id=a.id,
        source="+".join(sources),
        runner=_first(a.runner, b.runner),
        name=_first(a.name, b.name),
        host=_first(a.host, b.host),
        tmux=_first(a.tmux, b.tmux),
        cwd=_first(a.cwd, b.cwd),
        repo=_first(a.repo, b.repo),
        state=a.state if a.state != "unknown" else b.state,
        waiting_for=_first(a.waiting_for, b.waiting_for),
        started_at=min((x for x in (a.started_at, b.started_at) if x), default=None),
        last_seen=_later(a.last_seen, b.last_seen),
        tokens=a.tokens if a.tokens is not None else b.tokens,
        cost_usd=b.cost_usd if cost_from_b else a.cost_usd,
        cost_basis=b.cost_basis if cost_from_b else a.cost_basis,
        opaque=a.opaque or b.opaque,
        opens=(*a.opens, *b.opens),
        rc_url=_first(a.rc_url, b.rc_url),
    )


def merge_sessions(parts: list[Session], roots: tuple[str, ...] = ()) -> list[Session]:
    """One session per (runner, id): the parts from different sources joined. Order of first appearance. Each part is
    judged on its own folder before the join, and `opaque` is carried by the join, so one source's opaque folder
    can't be lost behind another source's open one."""
    merged: dict[tuple[str, str], Session] = {}
    for part in parts:
        part = replace(part, opaque=records.part_denied(part, roots))
        key = (part.runner, part.id)
        merged[key] = _join(merged[key], part) if key in merged else part
    return list(merged.values())


FRONT_DESK_NAMES = ("Home", "WS Lead")  # a session in ~/code whose name starts so is the front desk


def _front_desk(sessions: list[Session], home: Path) -> tuple[str, str] | None:
    """With no `[home] lead`: the one live session in the person's `~/code` folder named like a front desk, else None."""
    code = os.path.normpath(str(home / "code"))
    hits = [
        s
        for s in sessions
        if s.state != "ended" and s.cwd and os.path.normpath(s.cwd) == code and s.name.startswith(FRONT_DESK_NAMES)
    ]
    return (hits[0].runner, hits[0].id) if len(hits) == 1 else None


def _lead_key(sessions: list[Session], lead: str, home: Path) -> tuple[str, str] | None:
    """The (runner, id) of the session the `[home] lead` setting names; the most recently seen live one if several.
    With the setting empty, the front desk if exactly one session is."""
    if not lead:
        return _front_desk(sessions, home)
    how, _, want = lead.partition(":")
    if how == "cwd":
        target = os.path.normpath(_expand(want.strip(), home))
        hits = [s for s in sessions if s.cwd and os.path.normpath(s.cwd) == target]
    else:
        hits = [s for s in sessions if fnmatch.fnmatchcase(s.name, want.strip())]
    if not hits:
        return None
    best = max(hits, key=lambda s: (s.state != "ended", s.last_seen or ""))
    return (best.runner, best.id)


# --- reading the sources -------------------------------------------------------------------------------------------


@dataclass
class Loaded:
    by_kind: dict[str, list]
    rows: list[SourceRow]
    live: set[tuple[str, str]]  # sessions a live-list source named
    live_runners: set[str]


def _load(env: Env, kinds: set[str]) -> Loaded:
    """Read every source that provides one of `kinds`. A source is used whole or not at all; a failure is a row."""
    out = Loaded({k: [] for k in kinds}, [], set(), set())
    mods, broken = discover()
    out.rows.extend(SourceRow(name, False, FAILED, why) for name, why in broken)
    for mod in mods:
        wanted = [k for k in mod.PROVIDES if k in kinds]
        if not wanted:
            continue
        try:
            ok, why = mod.available(env)
            if not ok:
                out.rows.append(SourceRow(mod.NAME, False, UNAVAILABLE, why))
                continue
            got = {k: list(mod.read(k, env)) for k in wanted}
        except SourceError as e:
            out.rows.append(SourceRow(mod.NAME, False, e.kind, e.why))
            continue
        except TimeoutError:
            out.rows.append(SourceRow(mod.NAME, False, TIMEOUT, "took too long"))
            continue
        except Exception as e:  # noqa: BLE001 - a source must never take the home down; it becomes one row
            out.rows.append(SourceRow(mod.NAME, False, FAILED, f"{type(e).__name__} while reading"))
            continue
        for kind, recs in got.items():
            out.by_kind[kind].extend(recs)
        env.seen.extend(got.get("sessions", []))
        live_runner = getattr(mod, "LIVE_RUNNER", None)
        if live_runner and "sessions" in got:
            out.live_runners.add(live_runner)
            out.live |= {(s.runner, s.id) for s in got["sessions"]}
        out.rows.append(SourceRow(mod.NAME, True, "ok", "", sum(len(v) for v in got.values())))
    return out


def _finish_sessions(env: Env, loaded: Loaded) -> tuple[list[Session], dict[str, bool]]:
    """The merged sessions, sealed. Also the opaque verdict by session id (True opaque, False open); a session id not
    here is unknown, and asks and events that name one are opaque."""
    merged = []
    for s in merge_sessions(loaded.by_kind.get("sessions", []), env.cfg.roots):
        if s.state == "unknown" and s.runner in loaded.live_runners and (s.runner, s.id) not in loaded.live:
            s = replace(s, state="ended")  # the live list was read and this session isn't in it
        merged.append(s)
    lead = _lead_key(merged, env.cfg.lead, env.home)
    out: list[Session] = []
    verdict: dict[str, bool] = {}
    for s in merged:
        s = replace(s, host=s.host or env.host, lead=(s.runner, s.id) == lead)
        opaque = records.is_opaque(s, env.cfg.roots)
        verdict[s.id] = verdict.get(s.id, False) or opaque
        out.append(records.seal(s, opaque))
    out.sort(key=lambda s: s.last_seen or "", reverse=True)
    out.sort(key=lambda s: (s.host, _RANK.get(s.state, 3)))
    return out, verdict


def _known(verdict: dict[str, bool], session_id: str) -> bool | None:
    """The verdict on a session id; None when the id is empty or no session in the snapshot has it."""
    return verdict.get(session_id) if session_id else None


TMUX_TARGET = re.compile(r"^([^:.\s]+):([^.\s]+)(?:\.(\S+))?$")  # session:window[.pane]
TMUX_TIMEOUT_S = 5.0


def _tmux_target(text: str) -> tuple[str, str, str] | None:
    """(session, window, pane) of a tmux target like `ws:2`, `ws:@45` or `ws:@1.%1`; None for anything else."""
    m = TMUX_TARGET.match(text.strip())
    return (m.group(1), m.group(2), m.group(3) or "") if m else None


def _window_ids(env: Env) -> dict[tuple[str, str], str]:
    """{(tmux session, window index): window id} from `tmux list-windows`, read once. A window index changes when
    windows are moved, so it is only used to find the one window that has it now; no tmux, or any failure, is {}."""
    if "tmux_windows" not in env.cache:
        found: dict[tuple[str, str], str] = {}
        try:
            code, out = env.run(
                ["tmux", "list-windows", "-a", "-F", "#{session_name}:#{window_index} #{window_id}"], TMUX_TIMEOUT_S
            )
        except (OSError, TimeoutError):
            code, out = 1, ""
        for line in out.splitlines() if code == 0 else ():
            where, _, wid = line.strip().partition(" ")
            t = _tmux_target(where)
            if t and wid:
                found[(t[0], t[1])] = wid
        env.cache["tmux_windows"] = found
    return env.cache["tmux_windows"]


def _place(env: Env, ask: Ask, sessions: list[Session]) -> bool | None:
    """The opaque verdict on an ask that names no session id, from the one live Claude Code session in its tmux pane or
    window: that session's own verdict (claude_code saw its folder, so it may vouch). None when the ask names no
    target, nothing matches, or more than one session does (it stays opaque by the usual rules). When the ask and the
    session both carry a name they must agree: a window number can have moved since the ask was written."""
    want = _tmux_target(ask.tmux)
    if want is None:
        return None
    sess, win, pane = want
    by_number = not pane and win.isdigit()  # a window number moves; a window id or a pane doesn't
    if by_number:
        wid = _window_ids(env).get((sess, win))
        win = wid or win
    made = _files.epoch(ask.created) if ask.created else None
    hits = []
    for s in sessions:
        if s.state == "ended" or "claude_code" not in s.source.split("+"):
            continue
        have = _tmux_target(s.tmux)
        if have is None or have[0] != sess:
            continue
        if (pane and have[2] == pane) or (not pane and have[1] == win):
            began = _files.epoch(s.started_at) if s.started_at else None
            if began is not None and made is not None and began <= made:  # a newer session holds the pane now
                hits.append(s)
    if len(hits) != 1:
        return None
    hit = hits[0]
    if by_number and not (ask.session_name and hit.name):
        return None  # a bare number with no name on both sides could be any session that holds it now
    if not hit.opaque and ask.session_name and hit.name and ask.session_name != hit.name:
        return None
    return hit.opaque


def _finish_asks(env: Env, loaded: Loaded, verdict: dict[str, bool], sessions: list[Session]) -> list[Ask]:
    seen: dict[tuple[str, str], Ask] = {}
    for a in loaded.by_kind.get("asks", []):
        seen.setdefault((a.source, a.id), a)
    out = []
    for a in seen.values():
        known = _known(verdict, a.session_id)
        if known is None:
            known = _place(env, a, sessions)
        out.append(records.seal(a, records.is_opaque(a, env.cfg.roots, known)))
    return sorted(out, key=lambda a: (a.created is None, a.created or ""))


def _finish_events(env: Env, loaded: Loaded, verdict: dict[str, bool]) -> list[Event]:
    out = [
        records.seal(e, records.is_opaque(e, env.cfg.roots, _known(verdict, e.session_id)))
        for e in loaded.by_kind.get("events", [])
        if (_files.epoch(e.ts) >= env.event_since)
    ]
    return sorted(out, key=lambda e: e.ts, reverse=True)


# --- the API --------------------------------------------------------------------------------------------------------


def snapshot(env: Env | None = None) -> Snapshot:
    """Everything the home shows, from every source, with the opaque rule applied."""
    env = env or Env.make()
    loaded = _load(env, {"sessions", "asks", "events", "plugins", "evals"})
    sessions, verdict = _finish_sessions(env, loaded)
    return Snapshot(
        ts=jsonl.iso_utc(env.now),
        host=env.host,
        lead=next((s for s in sessions if s.lead), None),
        asks=tuple(_finish_asks(env, loaded, verdict, sessions)),
        sessions=tuple(sessions),
        plugins=tuple(loaded.by_kind["plugins"]),
        evals=tuple(loaded.by_kind["evals"]),
        events=tuple(_finish_events(env, loaded, verdict)),
        sources=tuple(loaded.rows),
    )


def sessions(env: Env | None = None) -> list[Session]:
    """Every session, by host then state (waiting first), then most recently seen."""
    env = env or Env.make()
    return _finish_sessions(env, _load(env, {"sessions"}))[0]


def lead(env: Env | None = None) -> Session | None:
    """The session `[home] lead` names, or None (no setting, no match, or the match is opaque)."""
    return next((s for s in sessions(env) if s.lead), None)


def asks(env: Env | None = None) -> list[Ask]:
    """What is waiting for the person, oldest first."""
    env = env or Env.make()
    loaded = _load(env, {"asks", "sessions"})
    sessions, verdict = _finish_sessions(env, loaded)
    return _finish_asks(env, loaded, verdict, sessions)


def plugins(env: Env | None = None) -> list[Plugin]:
    env = env or Env.make()
    return list(_load(env, {"plugins"}).by_kind["plugins"])


def evals(env: Env | None = None) -> list:
    env = env or Env.make()
    return list(_load(env, {"evals"}).by_kind["evals"])


def events(since: float | None = None, env: Env | None = None) -> list[Event]:
    """Events since `since` (epoch seconds; default the last 24 hours), newest first."""
    env = env or Env.make()
    env = replace(env, seen=[], event_since=env.event_since if since is None else float(since))
    loaded = _load(env, {"events", "sessions"})
    _, verdict = _finish_sessions(env, loaded)
    return _finish_events(env, loaded, verdict)


# --- the one write -------------------------------------------------------------------------------------------------


def answerable(ask: Ask) -> bool:
    """True when an ask is one a person may answer through the API: a question that isn't opaque, from a source that can
    take an answer. A permission, an approval, a waiting session and every opaque ask are answered where they live."""
    if ask.opaque or ask.kind != "question":
        return False
    return any(m.NAME == ask.source and callable(getattr(m, "answer", None)) for m in discover()[0])


def answer(ask_id: str, choice: str = "", text: str = "", env: Env | None = None) -> None:
    """Answer one ask with one of its options (`choice`, the option's label) or in your own words (`text`), never both.
    The source that gave the ask does it. Raises WsError: `not_found` (no such ask now), `refused` (it can't be answered
    here, or the answer isn't one of its options), `not_set` (`[home.answer] command` isn't in the profile) or `failed`
    (the program didn't run or stopped)."""
    env = env or Env.make()
    ask = next((a for a in asks(env) if a.id == ask_id), None)
    if ask is None:
        raise WsError("not_found", "that ask is gone", "reload the page to see what is waiting")
    if not answerable(ask):
        raise WsError("refused", "that ask is answered where it lives", "use Open to go to its session")
    if bool(choice) == bool(text):
        raise WsError("refused", "give one option or your own words, not both or neither", "pick an option or type")
    if choice and choice not in {o.get("label") for o in ask.options}:
        raise WsError("refused", "that isn't one of the ask's options", "reload the page and pick again")
    if len(text) > ANSWER_TEXT_MAX:
        raise WsError("refused", f"an answer is at most {ANSWER_TEXT_MAX} characters", "shorten it")
    if not env.cfg.answer:
        raise WsError("not_set", "no answer command is set", "set [home.answer] command in the profile")
    used = template.names(env.cfg.answer)
    if choice and "choice" not in used:
        raise WsError("refused", "the answer command takes typed answers only", "add {choice} to [home.answer] command")
    if text and "text" not in used:
        raise WsError("refused", "the answer command doesn't take typed text", "add {text} to [home.answer] command")
    mod = next(m for m in discover()[0] if m.NAME == ask.source)
    try:
        mod.answer(ask, choice, text, env)
    except SourceError as e:
        kind = "timeout" if e.kind == TIMEOUT else "failed"
        raise WsError(kind, e.why, "check the program in [home.answer] command") from e
