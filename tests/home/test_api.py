"""The home data API: the envelope, merging, the opaque rule, the [home] profile table and the failure of one source."""

from __future__ import annotations

import importlib
import json
import os
import unicodedata
from dataclasses import fields

import pytest

from home import NOW, SID, SID2, Box, agent, iso
from ws import approvals, cli, output
from ws.approvals import Bound
from ws.home import api, records
from ws.home.records import Ask, Event, Session
from ws.home.sources import _files, claude_code
from ws.output import WsError


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def by_name(snap):
    return {r.name: r for r in snap.sources}


# --- the envelope ----------------------------------------------------------------------------------------------------


def test_the_envelope_is_ws_home_1_and_only_has_the_listed_keys(box):
    snap = api.snapshot(box.env([agent()]))
    d = snap.to_dict()
    assert list(d) == ["schema", "ts", "host", "lead", "asks", "sessions", "plugins", "evals", "events", "sources"]
    assert d["schema"] == "ws.home/1" == records.SCHEMA
    assert d["ts"] == iso() and d["host"] == "testhost"
    assert all(set(r) == {"name", "ok", "kind", "why", "n"} for r in d["sources"])
    assert {f.name for f in fields(Session)} >= {
        "id", "source", "runner", "name", "host", "tmux", "cwd", "repo", "state", "waiting_for", "started_at",
        "last_seen", "tokens", "cost_usd", "cost_basis", "lead", "opaque", "opens",
    }  # fmt: skip
    assert {f.name for f in fields(Ask)} >= {
        "id", "source", "kind", "session_id", "repo", "title", "options", "decision", "created", "opaque",
        "answer_in", "opens",
    }  # fmt: skip
    json.dumps(d)  # plain JSON all the way down


def test_records_hold_the_documented_states_and_kinds():
    assert records.STATES == ("working", "waiting", "idle", "ended", "unknown")
    assert records.ASK_KINDS == ("question", "permission", "approval", "waiting", "draft", "plugin")


def test_every_read_returns_records_and_agrees_with_the_snapshot(box):
    box.ws_session(SID, cwd="/work/app")
    env = box.env([agent(SID, "/work/app", waitingFor="a yes or no")])
    snap = api.snapshot(env)
    assert [s.id for s in api.sessions(box.env([agent(SID, "/work/app", waitingFor="a yes or no")]))] == [
        s.id for s in snap.sessions
    ]
    assert [a.id for a in api.asks(box.env([agent(SID, "/work/app", waitingFor="a yes or no")]))] == [
        a.id for a in snap.asks
    ]
    assert [p.name for p in api.plugins(box.env())] == [p.name for p in snap.plugins]
    assert api.evals(box.env()) == [] and api.events(env=box.env()) == []


# --- one broken source -----------------------------------------------------------------------------------------------


def test_a_broken_source_is_one_row_not_a_crash(box, monkeypatch):
    box.ws_session(SID)

    def boom(kind, env):
        raise ValueError("a source with a bug in it")

    monkeypatch.setattr("ws.home.sources.ws_sessions.read", boom)
    snap = api.snapshot(box.env([agent()]))
    row = by_name(snap)["ws_sessions"]
    assert (row.ok, row.kind) == (False, "failed") and row.why == "ValueError while reading"
    assert "a source with a bug" not in json.dumps(snap.to_dict())  # the message isn't passed on
    assert by_name(snap)["claude_code"].ok and [s.id for s in snap.sessions] == [SID]  # the others are untouched


@pytest.mark.parametrize(
    ("run", "kind"),
    [
        (lambda argv, t: (0, "not json at all"), "bad_format"),
        (lambda argv, t: (0, '{"nope": 1}'), "bad_format"),
        (lambda argv, t: (0, "[1, 2]"), "bad_format"),
        (lambda argv, t: (1, ""), "failed"),
    ],
)
def test_claude_agents_that_cant_be_used_is_one_row(box, run, kind):
    box.pid_file(7, SID)
    snap = api.snapshot(box.env(run=run))
    row = by_name(snap)["claude_code"]
    assert (row.ok, row.kind) == (False, kind) and row.why
    assert snap.sessions == () and snap.asks == ()  # the pid files aren't used either: a source is whole or not there


def test_a_missing_binary_and_a_timeout_are_rows(box):
    def missing(argv, timeout_s):
        raise FileNotFoundError(argv[0])

    def slow(argv, timeout_s):
        raise TimeoutError

    assert by_name(api.snapshot(box.env(run=missing)))["claude_code"].kind == "unavailable"
    assert by_name(api.snapshot(box.env(run=slow)))["claude_code"].kind == "timeout"


def test_a_source_that_fails_on_its_second_kind_gives_nothing(box, monkeypatch):
    real = claude_code.read

    def read(kind, env):
        if kind == "asks":
            raise ValueError("second kind")
        return real(kind, env)

    monkeypatch.setattr(claude_code, "read", read)
    snap = api.snapshot(box.env([agent()]))
    assert snap.sessions == () and by_name(snap)["claude_code"].ok is False


def test_the_claude_program_is_called_with_a_timeout_and_no_shell(box):
    calls = []

    def run(argv, timeout_s):
        calls.append((argv, timeout_s))
        return 0, "[]"

    api.snapshot(box.env(run=run))
    assert calls == [(["claude", "agents", "--json"], claude_code.TIMEOUT_S)]  # once, though it serves two kinds


# --- merging ---------------------------------------------------------------------------------------------------------


def test_sessions_de_dupe_by_runner_and_session_id(box):
    box.ws_session(SID, cwd="/work/app")
    box.pid_file(7, SID, tmux="ws:@9.%9")
    snap = api.snapshot(box.env([agent(SID, "/work/app")]))
    assert len(snap.sessions) == 1
    s = snap.sessions[0]
    assert s.source == "claude_code+ws_sessions" and s.tmux == "ws:@9.%9" and s.runner == "claude-code"
    assert s.state == "working" and s.host == "testhost"


def test_the_same_id_under_another_runner_is_another_session(box):
    box.ws_session(SID, cwd="/work/app", runner="opencode")
    snap = api.snapshot(box.env([agent(SID, "/work/app")]))
    assert sorted(s.runner for s in snap.sessions) == ["claude-code", "opencode"]


def test_a_session_the_live_list_no_longer_names_has_ended(box):
    box.ws_session(SID, cwd="/work/app")
    box.ws_session(SID2, cwd="/work/app", runner="opencode")
    snap = api.snapshot(box.env([]))
    states = {s.runner: s.state for s in snap.sessions}
    assert states == {"claude-code": "ended", "opencode": "unknown"}  # only the runner whose live list was read


def test_waiting_for_makes_a_waiting_session_and_an_ask(box):
    snap = api.snapshot(box.env([agent(SID, "/work/app", waitingFor="allow the build?")]))
    s = snap.sessions[0]
    assert s.state == "waiting" and s.waiting_for == "allow the build?"
    (a,) = snap.asks
    assert (a.kind, a.session_id, a.title, a.source) == ("waiting", SID, "allow the build?", "claude_code")


def test_sessions_sort_by_host_then_waiting_first(box):
    snap = api.snapshot(
        box.env([agent(SID, "/work/a", status="idle"), agent(SID2, "/work/b", status="idle", waitingFor="x")])
    )
    assert [s.id for s in snap.sessions] == [SID2, SID]


def test_asks_come_oldest_first(box):
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [
            {"event": "added", "id": "bbbbbbb2", "ts": iso(-10), "repo": "app", "question": "later", "redacted": False},
            {
                "event": "added",
                "id": "aaaaaaa1",
                "ts": iso(-500),
                "repo": "app",
                "question": "earlier",
                "redacted": False,
            },
        ],
    )
    assert [a.id for a in api.asks(box.env([]))] == ["aaaaaaa1", "bbbbbbb2"]


# --- the lead --------------------------------------------------------------------------------------------------------


def test_lead_by_cwd_and_by_name(box):
    box.profile('[home]\nlead = "cwd:~/code"\n')
    home_dir = str(box.home / "code")
    snap = api.snapshot(box.env([agent(SID, home_dir, name="Home"), agent(SID2, home_dir + "/app", name="app")]))
    assert snap.lead is not None and snap.lead.id == SID and snap.lead.lead is True
    assert [s.lead for s in snap.sessions if s.id == SID2] == [False]
    box.profile('[home]\nlead = "name:Home*"\n')
    assert api.lead(box.env([agent(SID, "/x", name="Home"), agent(SID2, "/y", name="other")])).id == SID  # type: ignore[union-attr]


def test_no_lead_setting_means_no_lead(box):
    assert api.lead(box.env([agent()])) is None


def test_a_lead_that_is_opaque_is_not_pinned(box):
    box.profile('[home]\nlead = "cwd:/secret/place"\nopaque_paths = ["/secret"]\n')
    assert api.lead(box.env([agent(SID, "/secret/place")])) is None


# --- the opaque rule -------------------------------------------------------------------------------------------------


def test_a_redacted_ask_is_opaque_and_keeps_only_the_listed_fields(box):
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [
            {"event": "added", "id": "x1", "ts": iso(-9), "repo": "app", "question": "SECRET TEXT", "decision": "SECRET",
             "options": [{"label": "SECRET OPTION", "description": "d"}], "session": {"name": "SECRET NAME", "tmux": "ws:@1.%2"},
             "rc_url": "https://example.test/secret", "redacted": True},
        ],
    )  # fmt: skip
    (a,) = api.asks(box.env([]))
    assert a.opaque is True
    blank = Ask(id="x1", source="asks_jsonl", kind="question")
    for f in fields(Ask):
        keep = f.name in ("id", "source", "kind", "repo", "tmux", "created", "opaque")
        assert keep or getattr(a, f.name) == getattr(blank, f.name), f.name
    assert (a.repo, a.tmux, a.created, a.kind) == ("app", "ws:@1.%2", iso(-9), "question")
    assert "SECRET" not in json.dumps(a.to_dict()) and a.answer_in == "terminal"


def test_an_ask_that_does_not_say_redacted_false_is_opaque(box):
    box.lines(
        box.usage / "asks" / "asks.jsonl", [{"event": "added", "id": "x", "ts": iso(), "repo": "app", "question": "q"}]
    )
    assert api.asks(box.env([]))[0].opaque is True


def test_a_session_under_an_opaque_path_keeps_only_the_listed_fields(box):
    box.profile('[home]\nopaque_paths = ["/work/private", "~/code/hidden"]\n')
    env = box.env(
        [
            agent(SID, "/work/private/case-1", name="NAME", waitingFor="TEXT"),
            agent(SID2, str(box.home / "code" / "hidden"), name="N2"),
            agent("33333333-aaaa-bbbb-cccc-000000000003", "/work/public", name="public"),
        ]
    )
    sessions = {s.id: s for s in api.sessions(env)}
    assert [s.opaque for s in sessions.values()].count(True) == 2
    s = sessions[SID]
    blank = Session(id=SID, source="claude_code")
    for f in fields(Session):
        keep = f.name in ("id", "source", "repo", "tmux", "state", "started_at", "opaque", "cost_basis")
        assert keep or getattr(s, f.name) == getattr(blank, f.name), f.name
    assert (s.repo, s.state, s.cost_basis) == ("case-1", "waiting", "hidden")
    assert s.name == "" and s.cwd == "" and s.waiting_for == "" and s.tokens is None and s.cost_usd is None
    assert sessions["33333333-aaaa-bbbb-cccc-000000000003"].name == "public"


def test_a_ask_whose_session_is_opaque_is_opaque(box):
    box.profile('[home]\nopaque_paths = ["/work/private"]\n')
    snap = api.snapshot(box.env([agent(SID, "/work/private/x", waitingFor="TEXT")]))
    (a,) = snap.asks
    assert a.opaque and a.title == "" and a.session_id == "" and a.repo == "x"


def test_an_ask_whose_repo_path_is_under_an_opaque_path_is_opaque(box):
    box.profile('[home]\nopaque_paths = ["/work/private"]\n')
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "p", "ts": iso(), "repo": "/work/private/case", "question": "q", "redacted": False}],
    )
    (a,) = api.asks(box.env([]))
    assert a.opaque and a.title == ""


def test_when_ws_cant_tell_it_is_opaque(box):
    snap = api.snapshot(box.env([agent(SID, "")]))  # no folder, no repo: can't be placed
    assert snap.sessions[0].opaque is True and snap.sessions[0].name == ""
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "n", "ts": iso(), "question": "q", "redacted": False}],
    )
    assert api.asks(box.env([]))[0].opaque is True  # no repo and no session


def test_the_opaque_rule_is_applied_last_so_a_merge_cannot_unhide(box):
    box.profile('[home]\nopaque_paths = ["/work/private"]\n')
    box.ws_session(SID, cwd="/work/public")  # one source puts the session in the open ...
    snap = api.snapshot(box.env([agent(SID, "/work/private/x")]))  # ... the live list puts it under an opaque path
    (s,) = snap.sessions
    assert s.opaque and s.name == "" and s.cwd == ""  # one source's folder is enough to hide the merged session


def test_seal_keeps_only_the_listed_fields_and_nothing_else():
    s = Session(id="abcdef01", source="s", runner="r", name="n", cwd="/c", repo="rp", tmux="t", state="idle", started_at="x",
                tokens={"input": 1}, cost_usd=1.0, cost_basis="otel", waiting_for="w", host="h", rc_url="u")  # fmt: skip
    sealed = records.seal(s, True)
    assert sealed == Session(id="abcdef01", source="s", repo="rp", tmux="t", state="idle", started_at="x", opaque=True,
                             cost_basis="hidden")  # fmt: skip
    assert records.seal(s, False) is s
    e = Event(id="e0000001", ts="t", source="s", type="ty", session_id="sid", summary="sum")
    assert records.seal(e, True) == Event(id="e0000001", ts="t", source="s", type="ty", opaque=True)


def test_under_follows_folders_not_prefixes(tmp_path):
    roots = records.roots_of([str(tmp_path / "private")])
    assert records.under(str(tmp_path / "private"), roots)
    assert records.under(str(tmp_path / "private" / "deep" / "x"), roots)
    assert not records.under(str(tmp_path / "private-too"), roots)
    assert not records.under("", roots)


def test_under_follows_links(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    assert records.under(str(link / "x"), records.roots_of([str(real)]))
    assert records.under(str(real / "x"), records.roots_of([str(link)]))


# --- cost and tokens -------------------------------------------------------------------------------------------------


def test_an_opaque_sessions_transcript_is_never_opened(box, monkeypatch):
    box.profile('[home]\nopaque_paths = ["/work/private"]\n')
    private = box.transcript(SID, [{"message": {"id": "m", "usage": {"input_tokens": 5}}}])
    public = box.transcript(SID2, [{"message": {"id": "m", "usage": {"input_tokens": 7}}}])
    opened = []
    real = os.open

    def spy(path, *a, **k):
        opened.append(str(path))
        return real(path, *a, **k)

    monkeypatch.setattr(os, "open", spy)
    snap = api.snapshot(box.env([agent(SID, "/work/private/x"), agent(SID2, "/work/public")]))
    assert str(private) not in opened and str(public) in opened
    by_id = {s.id: s for s in snap.sessions}
    assert by_id[SID].tokens is None and by_id[SID].cost_basis == "hidden"
    assert by_id[SID2].tokens["input"] == 7  # type: ignore[index]


# --- the [home] table of the profile ---------------------------------------------------------------------------------


def test_the_home_table_is_read(box):
    box.profile(
        '[home]\nlead = "name:Home*"\nopaque_paths = ["~/code/x"]\n'
        '[home.sources]\nasks_jsonl = "~/a.jsonl"\nevents_dir = "/e"\notel_dir = "/o"\n'
        '[home.open]\nsession = "terminal"\nask = "browser"\nopaque = "terminal"\n'
    )
    cfg = api.Env.make(box.environ, now=NOW).cfg
    assert cfg.lead == "name:Home*" and cfg.opaque_paths == ("~/code/x",)
    assert cfg.sources == {"asks_jsonl": str(box.home / "a.jsonl"), "events_dir": "/e", "otel_dir": "/o"}
    assert cfg.open == {"session": "terminal", "ask": "browser", "opaque": "terminal"}
    assert str(box.home / "code" / "x") in cfg.roots


def test_no_profile_is_the_defaults(box):
    env = api.Env.make(box.environ, now=NOW)
    assert env.cfg == api.HomeConfig()
    assert env.path("asks_jsonl") == box.home / ".claude-usage" / "asks" / "asks.jsonl"
    assert env.path("claude_dir") == box.home / ".claude"


@pytest.mark.parametrize(
    ("text", "named"),
    [
        ("[home]\nleed = 1\n", "leed"),
        ('[home.sources]\nasks = "x"\n', "asks"),
        ('[home.open]\nbrowser = "x"\n', "browser"),
        ('[home]\nlead = "somewhere"\n', "home.lead"),
        ('[home]\nopaque_paths = "x"\n', "home.opaque_paths"),
        ("[home.open]\nsession = 3\n", "[home.open].session"),
        ('home = "x"\n', "[home] must be a table"),
    ],
)
def test_an_unknown_or_bad_home_key_is_an_error_naming_it(box, text, named):
    box.profile(text)
    with pytest.raises(WsError) as e:
        api.Env.make(box.environ, now=NOW)
    assert e.value.kind == "bad_config" and named in e.value.message and e.value.hint


def test_a_profile_that_does_not_parse_is_a_bad_config(box):
    box.profile("[home\n")
    with pytest.raises(WsError) as e:
        api.Env.make(box.environ, now=NOW)
    assert e.value.kind == "bad_config"


def test_the_api_never_writes_a_profile(box):
    api.snapshot(box.env([agent()]))
    assert not (box.ws / "profile.toml").exists()


# --- the command -----------------------------------------------------------------------------------------------------


def test_ws_home_data_json_has_the_snapshot_shape(box, monkeypatch, capsys):
    for k, v in box.environ.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(box.home)
    box.ws_session(SID, cwd="/work/app")
    assert cli.main(["home", "data", "--json"]) == 0
    env = json.loads(capsys.readouterr().out)
    assert (env["schema"], env["kind"], env["status"]) == (output.SCHEMA, "home.data", "ok")
    data = env["data"]
    assert data["schema"] == "ws.home/1"
    assert list(data) == ["schema", "ts", "host", "lead", "asks", "sessions", "plugins", "evals", "events", "sources"]
    rows = {r["name"]: r for r in data["sources"]}
    assert rows["claude_code"]["ok"] is False and rows["claude_code"]["kind"] == "unavailable"  # no claude on PATH
    assert rows["collie"]["why"].startswith("not built") and rows["ws_sessions"]["ok"] is True
    assert [s["id"] for s in data["sessions"]] == [SID] and len(data["plugins"]) >= 7


def test_ws_home_data_prints_text_without_json(box, monkeypatch, capsys):
    for k, v in box.environ.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(box.home)
    assert cli.main(["home", "data", "--no-color"]) == 0
    out = capsys.readouterr().out
    assert "sources" in out and "collie" in out and "not built" in out


def test_a_bad_home_table_is_a_typed_error_from_the_command(box, monkeypatch, capsys):
    for k, v in box.environ.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(box.home)
    box.profile("[home]\nleed = 1\n")
    assert cli.main(["home", "data", "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["status"] == "error" and env["error_kind"] == "bad_config" and "leed" in env["message"]


# --- events ----------------------------------------------------------------------------------------------------------


def test_events_are_newest_first_and_cut_at_since(box):
    day = box.usage / "events"
    for name, ts in (("2026-11-02", iso(-60)), ("2026-11-01", iso(-86400 - 60)), ("2026-10-20", iso(-86400 * 13))):
        box.lines(
            day / f"{name}.jsonl",
            [
                {
                    "id": f"e-{name}",
                    "time": ts,
                    "source": "s",
                    "type": "t.x",
                    "data": {"session_id": SID, "repo": "app", "cwd": "/w/app"},
                }
            ],
        )
    got = api.events(env=box.env([agent(SID, "/w/app")]))  # the session is open, so the events are
    assert [e.id for e in got] == ["e-2026-11-02"]  # the default window is the last 24 hours
    longer = api.events(NOW - 86400 * 2, env=box.env([agent(SID, "/w/app")]))
    assert [e.id for e in longer] == ["e-2026-11-02", "e-2026-11-01"]
    assert longer[0].summary == "t.x in app"


def test_an_event_with_no_session_or_under_an_opaque_path_is_opaque(box):
    box.profile('[home]\nopaque_paths = ["/work/private"]\n')
    box.lines(
        box.usage / "events" / "2026-11-02.jsonl",
        [
            {
                "id": "aaaaaaa1",
                "time": iso(-5),
                "source": "s",
                "type": "ty",
                "data": {"session_id": SID, "repo": "r", "cwd": "/work/private/x"},
            },
            {"id": "bbbbbbb2", "time": iso(-6), "source": "s", "type": "ty", "data": {"repo": "r"}},
            {
                "id": "ccccccc3",
                "time": iso(-7),
                "source": "s",
                "type": "ty",
                "data": {"session_id": SID2, "repo": "r", "cwd": "/work/public"},
            },
        ],
    )
    got = {e.id: e for e in api.events(env=box.env([agent(SID2, "/work/public")]))}
    assert got["aaaaaaa1"].opaque and got["bbbbbbb2"].opaque and not got["ccccccc3"].opaque
    assert got["aaaaaaa1"].summary == "" and got["aaaaaaa1"].session_id == "" and got["ccccccc3"].summary == "ty in r"


# --- the opaque rule, fail closed (fix round 1) ----------------------------------------------------------------------

FIU = "~/code/fiu"


def fiu_profile(box, root: str = FIU) -> None:
    box.profile(f'[home]\nopaque_paths = ["{root}"]\n')


def timed_out(argv, timeout_s):
    raise TimeoutError("claude agents timed out")


def test_an_ask_whose_session_is_not_in_the_snapshot_is_opaque(box):
    fiu_profile(box)  # claude agents timed out, so session S isn't known
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "q", "ts": iso(), "redacted": False, "session": {"session_id": SID},
          "question": "SECRET Q"}],
    )  # fmt: skip
    (a,) = api.asks(box.env(run=timed_out))
    assert a.opaque and a.title == "" and "SECRET" not in json.dumps(a.to_dict())


def test_an_ask_with_a_repo_name_but_no_session_or_folder_is_opaque(box):
    fiu_profile(box)
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "q", "ts": iso(), "redacted": False, "repo": "case-1", "question": "SECRET Q"}],
    )
    (a,) = api.asks(box.env([]))
    assert a.opaque and a.title == ""


def test_an_ask_on_an_open_session_is_shown_and_a_folder_in_the_file_is_not_enough(box):
    fiu_profile(box)
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [
            {"event": "added", "id": "aaaaaaa1", "ts": iso(), "redacted": False, "repo": "/work/app", "question": "own folder"},
            {"event": "added", "id": "bbbbbbb2", "ts": iso(), "redacted": False, "session": {"session_id": SID},
             "question": "own session"},
        ],
    )  # fmt: skip
    got = {a.id: a for a in api.asks(box.env([agent(SID, "/work/app")]))}
    assert got["aaaaaaa1"].opaque and got["aaaaaaa1"].title == ""  # the file's own word about a folder places nothing
    assert not got["bbbbbbb2"].opaque and got["bbbbbbb2"].title == "own session"


def test_an_ask_naming_an_unknown_session_is_opaque_whatever_folder_it_claims(box):
    fiu_profile(box)
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "q", "ts": iso(), "redacted": False, "repo": "/work/app",
          "session": {"session_id": SID}, "question": "SECRET Q"}],
    )  # fmt: skip
    (a,) = api.asks(box.env([]))  # S is not in the snapshot
    assert a.opaque and a.title == "" and "SECRET" not in json.dumps(a.to_dict())


def test_an_ask_whose_folder_drifted_from_its_session_is_opaque(box):
    fiu_profile(box)
    box.ws_session(SID, cwd=str(box.home / "code" / "fiu"))  # ws recorded S under the opaque folder
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "q", "ts": iso(), "redacted": False, "repo": "/work/app",
          "session": {"session_id": SID}, "question": "SECRET Q"}],
    )  # fmt: skip
    (a,) = api.asks(box.env([]))
    assert a.opaque and a.title == "" and "SECRET" not in json.dumps(a.to_dict())


def test_a_pending_approval_with_no_folder_and_an_unknown_session_is_opaque(box):
    fiu_profile(box)
    root = box.ws / "approvals"
    approvals.require(root, Bound("claude-code", "command", "cat ~/code/fiu/case.csv", "", SID), NOW - 60)
    approvals.require(root, Bound("claude-code", "command", "ls", "/work/app", SID2), NOW - 60)
    asks = api.asks(box.env([]))
    assert [a.opaque for a in asks].count(True) == 1
    assert "fiu" not in json.dumps([a.to_dict() for a in asks if a.opaque])
    (shown,) = [a for a in asks if not a.opaque]
    assert shown.title == "command: ls"  # a folder ws knows, and that isn't opaque, clears an approval


def test_an_event_with_an_unknown_session_and_no_folder_is_opaque(box):
    fiu_profile(box)
    box.lines(
        box.usage / "events" / "2026-11-02.jsonl",
        [
            {
                "id": "e0000001",
                "time": iso(-5),
                "source": "s",
                "type": "ty",
                "data": {"session_id": SID, "repo": "case-1"},
            },
            {
                "id": "e0000002",
                "time": iso(-6),
                "source": "s",
                "type": "ty",
                "data": {"session_id": SID, "cwd": "/work/app"},
            },
        ],
    )
    got = {e.id: e for e in api.events(env=box.env([]))}
    assert got["e0000001"].opaque and got["e0000001"].summary == ""
    assert got["e0000002"].opaque  # the file's own cwd places nothing; the session isn't in the snapshot
    (shown,) = [e for e in api.events(env=box.env([agent(SID, "/work/app")])) if e.id == "e0000002"]
    assert not shown.opaque  # the same event on a session that is open


def test_a_session_with_no_folder_is_opaque_even_with_a_repo_name(box):
    fiu_profile(box)
    box.ws_session(SID, cwd="")
    (s,) = api.sessions(box.env([]))
    assert s.opaque and s.cwd == "" and s.cost_basis == "hidden"


def test_a_relative_folder_cant_be_placed_and_is_opaque(box):
    fiu_profile(box)
    (s,) = api.sessions(box.env([agent(SID, "code/app")]))
    assert s.opaque


def test_one_sources_opaque_folder_survives_the_merge_in_either_order(box):
    fiu_profile(box)
    here = str(box.home / "code" / "fiu")
    box.ws_session(SID, cwd=here)  # ws_sessions says S is in the opaque folder
    (s,) = api.sessions(box.env([agent(SID, "/work/app")]))  # claude agents says it is at /work/app
    assert s.opaque and s.cwd == "" and s.name == ""
    open_first = Session(id=SID, source="a", runner="r", cwd="/work/app", name="n")
    shut_second = Session(id=SID, source="b", runner="r", cwd=here)
    for parts in ([open_first, shut_second], [shut_second, open_first]):
        (m,) = api.merge_sessions(parts, records.roots_of([here]))
        assert m.opaque


def test_a_part_with_no_folder_does_not_hide_a_session_another_part_places(box):
    fiu_profile(box)
    box.transcript(SID, [{"message": {"id": "m", "usage": {"input_tokens": 5}}}])
    (s,) = api.sessions(box.env([agent(SID, "/work/app")]))
    assert not s.opaque and s.tokens["input"] == 5  # type: ignore[index]


@pytest.mark.parametrize("gap", ["  ", "\t", "  "])
def test_a_folder_with_runs_of_spaces_in_its_name_is_still_opaque(box, gap):
    name = f"My{gap}Drive"
    fiu_profile(box, f"~/{name}/fiu")
    folder = str(box.home / name / "fiu" / "case-1")
    box.ws_session(SID, cwd=folder)
    box.lines(
        box.ws / "logs" / "runs.jsonl",
        [{"run_id": "r1", "job": "J", "runner": "claude-code", "cwd": folder, "started_at": iso(-600),
          "ended_at": iso(-540), "status": "ok", "cost_usd": 1.0}],
    )  # fmt: skip
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [{"event": "added", "id": "q", "ts": iso(), "redacted": False, "repo": folder, "question": "SECRET Q"}],
    )
    box.lines(
        box.usage / "events" / "2026-11-02.jsonl",
        [{"id": "e", "time": iso(-5), "source": "s", "type": "ty", "data": {"session_id": SID2, "cwd": folder}}],
    )
    approvals.require(box.ws / "approvals", Bound("claude-code", "command", "SECRET C", folder, SID2), NOW - 60)
    snap = api.snapshot(box.env([agent(SID2, folder, waitingFor="SECRET W")]))
    assert snap.sessions and all(s.opaque for s in snap.sessions)
    assert len(snap.asks) == 3 and all(a.opaque for a in snap.asks)  # the file, the approval, the waiting session
    assert len(snap.events) == 1 and snap.events[0].opaque
    assert "SECRET" not in json.dumps(snap.to_dict())


def test_a_path_is_capped_but_never_normalised():
    assert _files.path("/a  b\t/c ") == "/a  b\t/c "
    assert _files.path(5) == "" and _files.path(None) == ""
    got = _files.path("/" + "x" * 5000)
    assert records.under(got, records.roots_of(["/work"])) and not records.open_path(got, ())


@pytest.mark.parametrize("entry", ["code/private", "private", "./x", "  relative/dir  "])
def test_a_relative_opaque_path_is_a_profile_error_naming_the_key(box, entry):
    box.profile(f'[home]\nopaque_paths = ["/ok", "{entry}"]\n')
    with pytest.raises(WsError) as e:
        api.Env.make(box.environ, now=NOW)
    assert e.value.kind == "bad_config" and "home.opaque_paths" in e.value.message and entry.strip() in e.value.message
    assert e.value.hint


def test_a_home_relative_opaque_path_is_accepted(box):
    box.profile('[home]\nopaque_paths = ["~/code/fiu", "~"]\n')
    assert api.Env.make(box.environ, now=NOW).cfg.opaque_paths == ("~/code/fiu", "~")


def test_a_case_insensitive_file_system_matches_by_normcase(tmp_path, monkeypatch):
    roots = records.roots_of([str(tmp_path / "code" / "fiu")])
    other = str(tmp_path / "Code" / "FIU" / "case.csv")
    assert not records.under(other, roots)  # a case-sensitive system keeps the two apart
    monkeypatch.setattr(os.path, "normcase", str.lower)
    assert records.under(other, roots) and not records.open_path(other, roots)
    assert not records.under(str(tmp_path / "Code" / "FIU-too"), roots)


def test_a_folder_that_is_the_same_on_disk_under_another_spelling_is_under(tmp_path, monkeypatch):
    real = tmp_path / "code" / "fiu"
    real.mkdir(parents=True)
    roots = records.roots_of([str(real)])
    spelled = tmp_path / "CODE" / "FIU"  # exists only as far as the file system folds case
    deep = str(spelled / "a" / "b.csv")
    assert not records.under(deep, roots)
    # a case-insensitive file system answers for the folder above the file, so the check walks up to it
    monkeypatch.setattr(os.path, "lexists", lambda p: str(p).lower() in (str(spelled).lower(), str(real).lower()))
    monkeypatch.setattr(os.path, "samefile", lambda a, b: str(a).lower() == str(b).lower())
    assert records.under(deep, roots)
    assert not records.under(str(tmp_path / "CODE" / "other" / "x"), roots)


def test_a_folder_ws_cant_look_at_counts_as_under(tmp_path, monkeypatch):
    real = tmp_path / "fiu"
    real.mkdir()
    roots = records.roots_of([str(real)])

    def refuse(a, b):
        raise PermissionError("no")

    monkeypatch.setattr(os.path, "samefile", refuse)
    assert records.under(str(tmp_path / "elsewhere" / "x"), roots) is True  # can't compare: closed
    assert records.under("/bad\0path", roots) is True


def test_an_opaque_record_leaves_with_a_machine_id_or_a_hash(box):
    fiu_profile(box)
    odd = "SECRET case: name/with spaces " + "x" * 200
    ask_id = "a20261102T120000-k3x9"
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [
            {"event": "added", "id": odd, "ts": iso(), "redacted": True, "question": "SECRET Q"},
            {"event": "added", "id": ask_id, "ts": iso(), "redacted": True, "question": "SECRET Q"},
            {"event": "added", "id": "FIU-CASE-1234", "ts": iso(), "redacted": True, "question": "SECRET Q"},
        ],
    )
    got = {a.id for a in api.asks(box.env([]))}
    assert ask_id in got and odd not in got and "FIU-CASE-1234" not in got
    assert got - {ask_id} == {records.safe_id(odd), records.safe_id("FIU-CASE-1234")}
    assert all(len(h) == 16 for h in got - {ask_id})
    assert "FIU" not in json.dumps(sorted(got))


@pytest.mark.parametrize(
    "ident",
    [SID, f"waiting:{SID}", "a20261102T120000-k3x9", "deadbeef", "0123456789abcdef" * 4],
)
def test_a_machine_shaped_id_is_kept_on_an_opaque_record(ident):
    assert records.safe_id(ident) == ident
    assert records.seal(Session(id=ident, source="s"), True).id == ident


@pytest.mark.parametrize("ident", ["FIU-CASE-1234", "plain-id_1:2.3", "abc", "", "g" * 12, "0123456789abcdef" * 5])
def test_any_other_id_is_hashed_to_sixteen_characters(ident):
    hashed = records.safe_id(ident)
    assert hashed != ident and len(hashed) == 16 and records.MACHINE_ID.fullmatch(hashed)
    assert records.safe_id(hashed) == hashed  # hashing twice changes nothing
    assert records.seal(Event(id=ident, ts="t", source="s", type="ty"), True).id == hashed
    assert records.seal(Session(id=ident, source="s"), True).id == hashed
    assert records.seal(Session(id=ident, source="s"), False).id == ident  # an open record keeps its id


@pytest.mark.parametrize("form", ["NFC", "NFD"])
def test_a_label_and_a_path_match_whichever_unicode_form_they_are_written_in(form):
    def fm(text):
        return unicodedata.normalize(form, text)

    cafe = unicodedata.normalize("NFC", "café")
    roots = records.roots_of(["/work/" + cafe])
    assert records.label_matches(fm("café"), roots)
    assert records.under("/work/" + fm("café") + "/case", roots)
    other = records.roots_of(["/work/" + fm("café")])
    assert records.label_matches(cafe, other)
    assert records.under("/work/" + cafe + "/case", other)
    assert not records.under("/work/cafe/case", roots)


def test_the_internal_placed_flag_is_not_in_the_output():
    assert "placed" not in Ask(id="a", source="s", kind="question", placed=True).to_dict()
    assert "placed" not in Event(id="e", ts="t", source="s", type="ty", placed=True).to_dict()


def test_a_source_that_wont_import_is_one_row_and_the_rest_still_read(box, monkeypatch):
    real = importlib.import_module

    def broken(name, *a, **k):
        if name.endswith(".ws_runs"):
            raise ImportError("a dependency that isn't here: /secret/path")
        return real(name, *a, **k)

    monkeypatch.setattr("ws.home.sources.importlib.import_module", broken)
    snap = api.snapshot(box.env([agent()]))
    row = by_name(snap)["ws_runs"]
    assert (row.ok, row.kind, row.why) == (False, "failed", "ImportError while loading")
    assert "secret" not in json.dumps(snap.to_dict()["sources"])
    assert by_name(snap)["claude_code"].ok and len(snap.sessions) == 1
    assert api.sessions(box.env([agent()]))  # the single reads survive it too
