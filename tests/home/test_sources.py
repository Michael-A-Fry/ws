"""The home sources: each one's reading, the hard rules (no `.key` file opened, transcripts read for `usage` only) and
`evalstats.trend`. Every fixture is built in a tmp HOME."""

from __future__ import annotations

import hashlib
import io
import json
import os

import pytest

from home import NOW, SID, SID2, Box, agent, iso
from ws import approvals, evalstats
from ws.approvals import Bound
from ws.home import api
from ws.home.sources import SourceError, _files, all_sources, claude_code, collie, otel_jsonl, transcripts


@pytest.fixture
def box(tmp_path):
    return Box(tmp_path)


def read(name, kind, env):
    mod = next(m for m in all_sources() if m.NAME == name)
    ok, why = mod.available(env)
    assert ok, why
    return mod.read(kind, env)


# --- the folder is the registry --------------------------------------------------------------------------------------


def test_every_source_keeps_the_contract():
    names = [m.NAME for m in all_sources()]
    assert sorted(names) == sorted(
        [
            "ws_sessions",
            "ws_runs",
            "ws_approvals",
            "claude_code",
            "transcripts",
            "asks_jsonl",
            "events_dir",
            "otel_jsonl",
            "ws_eval",
            "ws_plugins",
            "collie",
        ]
    )
    assert len(set(names)) == len(names)
    for m in all_sources():
        assert set(m.PROVIDES) <= {"sessions", "asks", "events", "plugins", "evals"} and isinstance(m.ORDER, int)
    orders = [m.ORDER for m in all_sources()]
    assert orders == sorted(orders)
    # what a source reads from earlier sources, it reads after them
    assert claude_code.ORDER < transcripts.ORDER < otel_jsonl.ORDER


def test_the_collie_slot_says_it_is_not_built(box):
    ok, why = collie.available(box.env())
    assert not ok and why == "not built: waiting for the trial's read format"
    with pytest.raises(SourceError):
        collie.read("sessions", box.env())


# --- no .key file, ever ----------------------------------------------------------------------------------------------


def test_key_files_are_never_opened(box, monkeypatch):
    box.pid_file(7, SID)
    box.ws_session(SID, cwd="/work/app")
    box.transcript(SID, [{"message": {"id": "m", "usage": {"input_tokens": 3}}}])
    box.lines(box.usage / "otel" / "2026-11-02.jsonl", [{"event": "api_request", "sid": "abcd1234", "cost_usd": 1}])
    planted = [
        box.claude / "sessions" / "7.key",
        box.claude / "sessions" / f"{SID}.key",
        box.claude / "projects" / "-home-user-code-app" / f"{SID}.key",
        box.ws / "sessions" / "anything.key",
        box.usage / "otel" / "2026-11-02.key",
        box.usage / "events" / "2026-11-02.key",
        box.usage / "asks" / "asks.key",
    ]
    for p in planted:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("not-a-real-secret", encoding="utf-8")
    touched: list[str] = []
    real_open, real_os_open = open, os.open

    def spy_open(file, *a, **k):
        touched.append(str(file))
        return real_open(file, *a, **k)

    def spy_os_open(path, *a, **k):
        touched.append(str(path))
        return real_os_open(path, *a, **k)

    monkeypatch.setattr("builtins.open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    snap = api.snapshot(box.env([agent(SID, "/work/app")]))
    assert snap.sessions and any(t.endswith(".json") for t in touched) and any(t.endswith(".jsonl") for t in touched)
    assert [t for t in touched if t.endswith(".key")] == []
    assert "not-a-real-secret" not in json.dumps(snap.to_dict())


def test_the_file_helpers_refuse_a_key_file(box):
    key = box.ws / "x.key"
    key.parent.mkdir(parents=True)
    key.write_text("{}", encoding="utf-8")
    for call in (_files.read_object, lambda p: list(_files.iter_jsonl(p))):
        with pytest.raises(SourceError):
            call(key)
    (box.ws / "a.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in _files.json_files(box.ws)] == ["a.json"]


def test_a_fifo_or_a_folder_named_like_a_log_is_skipped_not_waited_on(box):
    box.ws.mkdir(parents=True)
    os.mkfifo(box.ws / "fifo.json")
    (box.ws / "dir.json").mkdir()
    assert _files.read_object(box.ws / "fifo.json") is None
    assert _files.read_object(box.ws / "dir.json") is None


# --- ws_sessions, ws_runs, ws_approvals ------------------------------------------------------------------------------


def test_ws_sessions_reads_session_records_and_skips_a_guards_record(box):
    box.ws_session(SID, cwd="/work/app")
    box.json(box.ws / "sessions" / f"{SID}.spawn.json", {"session_id": SID, "count": 3})
    (box.ws / "sessions" / "junk.json").write_text("[1]", encoding="utf-8")
    (s,) = read("ws_sessions", "sessions", box.env())
    assert (s.id, s.runner, s.cwd, s.repo, s.state) == (SID, "claude-code", "/work/app", "app", "unknown")
    assert s.started_at == iso(-3600) and s.last_seen == iso(-60)


def test_ws_runs_gives_finished_jobs_with_cost_and_leaves_eval_trials_to_evals(box):
    log = box.ws / "logs" / "runs.jsonl"
    base = {"runner": "claude-code", "cwd": "/work/app", "started_at": iso(-600), "ended_at": iso(-540), "status": "ok"}
    box.lines(
        log,
        [
            {**base, "run_id": "r1", "job": "nightly", "session_id": SID, "cost_usd": 0.25, "cost_basis": "reported",
             "tokens": {"input": 10, "output": 5, "cache_creation": 0, "cache_read": 100}},
            {**base, "run_id": "r2", "job": "unpriced", "cost_usd": None, "tokens": None},
            {**base, "run_id": "r3", "job": "trial", "eval": {"task": "t", "trial": 1, "arm": "a", "pass": True}},
            {**base, "run_id": "r4", "job": "old", "started_at": iso(-86400 * 30)},
        ],
    )  # fmt: skip
    got = {s.name: s for s in read("ws_runs", "sessions", box.env())}
    assert set(got) == {"nightly", "unpriced"}
    n = got["nightly"]
    assert (n.id, n.state, n.cost_usd, n.cost_basis) == (SID, "ended", 0.25, "reported")
    assert n.tokens == {"input": 10, "output": 5, "cache_creation": 0, "cache_read": 100}
    u = got["unpriced"]
    assert (u.id, u.cost_usd, u.cost_basis, u.tokens) == ("r2", None, "not measured", None)


def test_ws_runs_and_claude_code_join_on_the_session_id(box):
    box.lines(
        box.ws / "logs" / "runs.jsonl",
        [{"run_id": "r1", "job": "j", "runner": "claude-code", "session_id": SID, "cwd": "/work/app",
          "started_at": iso(-600), "ended_at": iso(-540), "cost_usd": 1.5}],
    )  # fmt: skip
    (s,) = api.snapshot(box.env([agent(SID, "/work/app")])).sessions
    assert s.source == "claude_code+ws_runs" and s.cost_usd == 1.5 and s.cost_basis == "reported"
    assert s.state == "working"  # the live list's state beats the log's "ended"


def test_ws_approvals_are_asks_answered_in_a_terminal(box):
    box.ws.mkdir(parents=True)
    root = box.ws / "approvals"
    bound = Bound("claude-code", "command", "terraform apply", "/work/app", SID)
    approvals.require(root, bound, NOW - 60)
    (a,) = read("ws_approvals", "asks", box.env())
    assert (a.kind, a.answer_in, a.session_id, a.repo) == ("approval", "terminal", SID, "app")
    assert a.id == bound.digest() and a.title == "command: terraform apply"
    assert a.decision == f"ws approve {a.id[:12]}" and a.created == iso(-60)
    assert a.opaque is False


# --- asks_jsonl ------------------------------------------------------------------------------------------------------


def test_asks_jsonl_folds_events_into_the_asks_still_open(box):
    opt = [{"label": "Yes", "description": "go"}, {"label": "No", "description": ""}, {"nope": 1}, "str"]
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [
            {"v": 1, "event": "added", "id": "q1", "ts": iso(-100), "repo": "app", "question": "Ship it?",
             "options": opt, "decision": "the release", "source": "session", "session": {"name": "bld", "tmux": "ws:@1.%1"},
             "rc_url": "https://claude.example/session_1", "redacted": False},
            {"v": 1, "event": "added", "id": "q2", "ts": iso(-90), "repo": "/work/app", "question": "Allow?",
             "options": [], "source": "permission", "redacted": False},
            {"v": 1, "event": "added", "id": "q3", "ts": iso(-80), "repo": "app", "question": "gone", "redacted": False},
            {"v": 1, "event": "added", "id": "q4", "ts": iso(-70), "repo": "app", "question": "done", "redacted": False},
            {"v": 1, "event": "answered", "id": "q3", "ts": iso(-60)},
            {"v": 1, "event": "done", "id": "q4", "ts": iso(-50)},
            {"v": 1, "event": "withdrawn", "id": "nothing", "ts": iso(-40)},
        ],
        raw='{"event": "added", "id": "torn',  # a line still being written
    )  # fmt: skip
    got = {a.id: a for a in read("asks_jsonl", "asks", box.env())}
    assert set(got) == {"q1", "q2"}
    q1 = got["q1"]
    assert (q1.kind, q1.title, q1.decision, q1.session_name, q1.tmux) == (
        "question",
        "Ship it?",
        "the release",
        "bld",
        "ws:@1.%1",
    )
    assert q1.options == ({"label": "Yes", "description": "go"}, {"label": "No", "description": ""})
    assert (q1.answer_in, q1.rc_url, q1.created, q1.opaque) == (
        "app",
        "https://claude.example/session_1",
        iso(-100),
        False,
    )
    assert (got["q2"].kind, got["q2"].repo, got["q2"].answer_in, got["q2"].rc_url) == (
        "permission",
        "app",
        "terminal",
        "",
    )


def test_a_link_that_is_not_https_is_dropped(box):
    box.lines(
        box.usage / "asks" / "asks.jsonl",
        [
            {
                "event": "added",
                "id": "q",
                "ts": iso(),
                "repo": "a",
                "question": "q",
                "rc_url": "javascript:alert(1)",
                "redacted": False,
            }
        ],
    )
    assert read("asks_jsonl", "asks", box.env())[0].rc_url == ""


def test_the_asks_path_can_be_set_in_the_profile(box):
    box.profile('[home.sources]\nasks_jsonl = "~/elsewhere/asks.jsonl"\n')
    box.lines(
        box.home / "elsewhere" / "asks.jsonl",
        [{"event": "added", "id": "z", "ts": iso(), "repo": "a", "question": "q", "redacted": False}],
    )
    assert [a.id for a in read("asks_jsonl", "asks", box.env())] == ["z"]


# --- claude_code -----------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "waiting", "state"),
    [("busy", None, "working"), ("idle", None, "idle"), ("IDLE", None, "idle"), ("exited", None, "ended"),
     ("busy", "approval", "waiting"), ("busy", {"kind": "permission", "x": 1}, "waiting"), ("odd", None, "unknown")],
)  # fmt: skip
def test_claude_code_maps_status_and_waiting_for(box, status, waiting, state):
    extra = {"waitingFor": waiting} if waiting else {}
    (s,) = read("claude_code", "sessions", box.env([agent(SID, "/work/app", status=status, **extra)]))
    assert s.state == state


def test_claude_code_waiting_for_text_comes_from_a_string_or_a_table():
    assert claude_code._waiting_text("allow the build?") == "allow the build?"
    assert claude_code._waiting_text({"prompt": "pick one"}) == "pick one"
    assert claude_code._waiting_text({"other": 1}) == "input"
    assert claude_code._waiting_text(None) == "" and claude_code._waiting_text("") == ""


def test_claude_code_adds_tmux_from_the_pid_file_and_ignores_a_pid_file_with_no_live_session(box):
    box.pid_file(7, SID, tmux="ws:@3.%4")
    box.pid_file(8, SID2, tmux="ws:@5.%6")  # not in the live list: not a live session
    (s,) = read("claude_code", "sessions", box.env([agent(SID, "/work/app")]))
    assert (s.id, s.tmux, s.name, s.started_at) == (SID, "ws:@3.%4", "builder", iso(-3600))


def test_claude_code_accepts_a_wrapped_list_and_skips_a_session_with_no_id(box):
    env = box.env(run=lambda argv, t: (0, json.dumps({"agents": [agent(SID), {"cwd": "/x"}]})))
    assert [s.id for s in read("claude_code", "sessions", env)] == [SID]


# --- transcripts -----------------------------------------------------------------------------------------------------


def test_transcripts_read_usage_and_nothing_else(box):
    secret = "PLANTED-CONTENT-" + "x" * 20
    box.transcript(
        SID,
        [
            {"type": "user", "message": {"role": "user", "content": secret}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"text": secret}],
             "usage": {"input_tokens": 10, "output_tokens": 1, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"text": secret}],
             "usage": {"input_tokens": 10, "output_tokens": 20, "cache_creation_input_tokens": 100, "cache_read_input_tokens": 1000}}},
            {"type": "assistant", "message": {"id": "m2", "usage": {"input_tokens": 5, "output_tokens": 5}}},
            {"type": "system", "message": "not a table"},
        ],
    )  # fmt: skip
    env = box.env([agent(SID, "/work/app")])
    snap = api.snapshot(env)
    assert snap.sessions[0].tokens == {"input": 15, "output": 25, "cache_creation": 100, "cache_read": 1000}
    assert secret not in json.dumps(snap.to_dict())  # a message counts once, at its last usage


def test_a_transcript_without_usage_leaves_tokens_unmeasured(box):
    box.transcript(SID, [{"type": "user", "message": {"content": "hi"}}])
    (s,) = api.snapshot(box.env([agent(SID, "/work/app")])).sessions
    assert s.tokens is None


def test_a_session_id_that_could_climb_out_of_the_projects_folder_finds_no_transcript(box):
    box.transcript("x", [{"message": {"id": "m", "usage": {"input_tokens": 1}}}])
    snap = api.snapshot(box.env([agent("../../.claude/projects/*/x", "/work/app")]))
    assert snap.sessions[0].tokens is None


def test_too_large_a_transcript_leaves_one_sessions_tokens_unmeasured_not_the_source(box, monkeypatch):
    box.transcript(SID, [{"message": {"id": "m", "usage": {"input_tokens": 1}}}])
    box.transcript(SID2, [{"message": {"id": "m", "usage": {"input_tokens": 2}}}])
    monkeypatch.setattr(transcripts, "MAX_BYTES", 40)
    snap = api.snapshot(box.env([agent(SID, "/work/a"), agent(SID2, "/work/b")]))
    assert {r.name: r for r in snap.sources}["transcripts"].ok is True
    assert all(s.tokens is None for s in snap.sessions)


# --- otel_jsonl ------------------------------------------------------------------------------------------------------


def sid8(session_id):
    return hashlib.sha256(session_id.encode()).hexdigest()[:8]


def test_otel_sums_the_cost_of_api_requests_by_hashed_session_id(box):
    box.lines(
        box.usage / "otel" / "2026-11-02.jsonl",
        [
            {"event": "api_request", "sid": sid8(SID), "cost_usd": 0.5, "ts": iso(-30)},
            {"event": "api_request", "sid": sid8(SID), "cost_usd": 0.25, "ts": iso(-20)},
            {"event": "tool_result", "sid": sid8(SID), "cost_usd": 99},
            {"event": "api_request", "sid": sid8(SID), "cost_usd": "free"},
            {"event": "api_request", "sid": sid8(SID), "cost_usd": -1},
            {"event": "api_request", "sid": "ffffffff", "cost_usd": 5},
        ],
    )
    box.lines(box.usage / "otel" / "2026-10-01.jsonl", [{"event": "api_request", "sid": sid8(SID), "cost_usd": 1000}])
    (s,) = api.snapshot(box.env([agent(SID, "/work/app")])).sessions
    assert (s.cost_usd, s.cost_basis) == (0.75, "otel")  # a file older than the window isn't read


def test_an_opaque_sessions_cost_is_hidden(box):
    box.profile('[home]\nopaque_paths = ["/work/private"]\n')
    box.lines(box.usage / "otel" / "2026-11-02.jsonl", [{"event": "api_request", "sid": sid8(SID), "cost_usd": 3}])
    (s,) = api.snapshot(box.env([agent(SID, "/work/private/x")])).sessions
    assert (s.cost_usd, s.cost_basis) == (None, "hidden")
    assert otel_jsonl.read("sessions", box.env([])) == []


def test_a_session_with_no_cost_line_is_not_measured(box):
    box.lines(box.usage / "otel" / "2026-11-02.jsonl", [{"event": "api_request", "sid": "ffffffff", "cost_usd": 3}])
    (s,) = api.snapshot(box.env([agent(SID, "/work/app")])).sessions
    assert (s.cost_usd, s.cost_basis) == (None, "not measured")


# --- ws_eval ---------------------------------------------------------------------------------------------------------


def trial(task, n, arm, ok, batch, ts, run_id=None):
    return {"schema": 1, "ts": ts, "batch": batch, "task": task, "trial": n, "arm": arm, "job": f"job-{arm}",
            "run_id": run_id, "status": "ok", "pass": ok}  # fmt: skip


def test_ws_eval_gives_one_arm_per_arm_with_its_numbers_and_reasons(box):
    runs, results = [], []
    for batch, offset in (("b1", -3 * 86400), ("b2", -86400)):
        for task in ("t1", "t2"):
            for n in (1, 2):
                rid = f"{batch}-{task}-{n}"
                ok = not (batch == "b1" and task == "t2")
                runs.append({"run_id": rid, "started_at": iso(offset), "status": "ok", "exit_code": 0, "duration_ms": 2000,
                             "tokens": {"input": 100, "output": 50, "cache_creation": 0, "cache_read": 0}, "cost_usd": 0.01})  # fmt: skip
                results.append(trial(task, n, "alpha", ok, batch, iso(offset), rid))
    results.append(trial("t1", 1, "beta", False, "b1", iso(-86400)))  # a trial with no run record
    box.lines(box.ws / "logs" / "runs.jsonl", runs)
    box.lines(box.ws / "eval" / "results.jsonl", results)
    arms = {a.arm: a for a in read("ws_eval", "evals", box.env())}
    a = arms["alpha"]
    assert (a.job, a.trials, a.solved, a.pass_rate) == ("job-alpha", 8, 6, 0.75)
    assert a.pass_hat_k == {"k": 4, "value": 0.5}  # t1 passes all four trials, t2 two of four
    assert a.time_per_solved == {"p50_s": 2.0, "p90_s": 2.0}
    assert (
        a.tokens_per_solved == 200.0
    )  # 8 trials of 150 tokens over 6 solved and a.cost_per_solved_usd == pytest.approx(0.08 / 6)
    assert [(t["batch"], t["n"], t["pass_rate"]) for t in a.trend] == [("b1", 4, 0.5), ("b2", 4, 1.0)]
    assert a.noise == {"points": evalstats.NOISE_POINTS, "wins_needed": evalstats.wins_needed(2)}
    assert a.not_measured == {}
    b = arms["beta"]
    assert (b.trials, b.solved, b.pass_rate) == (1, 0, 0.0)
    assert b.tokens_per_solved is None and set(b.not_measured) >= {"tokens_per_solved", "cost_per_solved", "time"}
    assert all(why for why in b.not_measured.values())


def test_ws_eval_leaves_out_trials_that_never_started(box):
    box.lines(box.ws / "eval" / "results.jsonl", [{**trial("t", 1, "a", False, "b", iso(-60)), "status": "error"}])
    assert read("ws_eval", "evals", box.env()) == []


def test_ws_eval_leaves_out_results_older_than_the_window(box):
    box.lines(box.ws / "eval" / "results.jsonl", [trial("t", 1, "a", True, "b", iso(-86400 * 120))])
    assert read("ws_eval", "evals", box.env()) == []


# --- ws_plugins ------------------------------------------------------------------------------------------------------


def test_ws_plugins_lists_built_modules_and_hides_unbuilt_ones_that_are_off(box):
    plugins = {p.name: p for p in read("ws_plugins", "plugins", box.env())}
    assert plugins["memory"].on and plugins["memory"].built
    assert "finance" not in plugins  # not built and not on: never shown


def test_ws_plugins_lists_the_modules_with_on_built_and_region(box):
    box.environ["WS_SHOW_UNBUILT"] = "1"  # a developer view: the unbuilt ones show, marked not built
    plugins = {p.name: p for p in read("ws_plugins", "plugins", box.env())}
    assert plugins["memory"].on and plugins["memory"].built
    assert not plugins["finance"].on and not plugins["finance"].built
    assert plugins["finance"].region_ok is True  # no region known on this box: ws doesn't refuse on a guess
    box.profile('[machine]\nregion = "AU"\n')
    assert {p.name: p for p in read("ws_plugins", "plugins", box.env())}["finance"].region_ok is False
    box.profile('[machine]\nregion = "NZ"\n')
    assert {p.name: p for p in read("ws_plugins", "plugins", box.env())}["finance"].region_ok is True


# --- evalstats.trend -------------------------------------------------------------------------------------------------


def test_trend_is_pure_and_empty_for_no_results():
    assert evalstats.trend([]) == []
    given = [{"batch": "b", "ts": iso(), "pass": True}]
    evalstats.trend(given)
    assert given == [{"batch": "b", "ts": iso(), "pass": True}]


def test_trend_groups_by_batch_oldest_first():
    results = [
        {"batch": "b2", "ts": "2026-11-02T00:00:00Z", "pass": True},
        {"batch": "b1", "ts": "2026-11-01T00:00:05Z", "pass": False},
        {"batch": "b1", "ts": "2026-11-01T00:00:01Z", "pass": True},
        {"batch": "b1", "ts": "2026-11-01T00:00:09Z", "pass": True},
        {"batch": "b2", "ts": "2026-11-02T00:00:03Z", "pass": True},
    ]
    assert evalstats.trend(results) == [
        {"batch": "b1", "ts": "2026-11-01T00:00:01Z", "n": 3, "pass_rate": 2 / 3},
        {"batch": "b2", "ts": "2026-11-02T00:00:00Z", "n": 2, "pass_rate": 1.0},
    ]


def test_trend_orders_by_time_across_offsets_and_skips_a_result_with_no_batch():
    results = [
        {"batch": "late", "ts": "2026-11-02T00:00:00+13:00", "pass": True},  # 2026-11-01T11:00Z
        {"batch": "early", "ts": "2026-11-01T12:00:00Z", "pass": False},
        {"ts": "2026-11-03T00:00:00Z", "pass": True},
        {"batch": "", "ts": "2026-11-03T00:00:00Z", "pass": True},
        {"batch": 7, "ts": "2026-11-03T00:00:00Z", "pass": True},
    ]
    assert [(r["batch"], r["pass_rate"]) for r in evalstats.trend(results)] == [("late", 1.0), ("early", 0.0)]


def test_trend_counts_only_a_true_pass_and_survives_a_missing_ts():
    rows = evalstats.trend([{"batch": "b", "pass": "yes"}, {"batch": "b", "pass": None}, {"batch": "b", "pass": True}])
    assert rows == [{"batch": "b", "ts": None, "n": 3, "pass_rate": 1 / 3}]
