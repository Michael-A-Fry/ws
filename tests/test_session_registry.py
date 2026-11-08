"""The session registry v2: the record, the locked setters, and the launch token that is the only way a role is set."""

from __future__ import annotations

import inspect
import io
import json
import os
import stat
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from ws import cli, paths, session
from ws.home import records as home_records
from ws.home.sources import ws_sessions
from ws.output import WsError

LEAD = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    for var in ("WS_RELEASE", "WS_RELEASE_FILE", "TMUX_PANE", session.LAUNCH_ENV):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(session, "_ancestors", list)  # no runner process unless a test names one
    return tmp_path


def folder(home):
    return home / "wshome" / "sessions"


def start(monkeypatch, capsys, sid="s1", cwd="/work/here", *extra, payload=None):
    body = {"session_id": sid, "cwd": cwd, "hook_event_name": "SessionStart", **(payload or {})}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(body)))
    code = cli.main(["session", "start", "--runner", "claude-code", *paths.pin_of().args(), *extra])
    assert (code, capsys.readouterr().out) == (0, "")


def on_disk(home, sid="s1") -> dict:
    return json.loads((folder(home) / f"{sid}.json").read_text())


def policy_with_classes(home, cwd):
    (home / "wshome").mkdir(exist_ok=True)
    (home / "wshome" / "policy.toml").write_text(
        f'[classes.private]\naccess = "walled"\nrepos = ["{cwd}"]\n[classes.open]\naccess = "full"\nrepos = ["{home}/open"]\n'
    )


# --- the record ------------------------------------------------------------------------------------------------


def test_v2_round_trip(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r1")
    monkeypatch.setenv("TMUX_PANE", "%12")
    start(monkeypatch, capsys, "abc", "/work/here")
    raw = on_disk(home, "abc")
    assert raw["schema"] == 2 == session.SCHEMA_VERSION
    assert set(raw) == {
        *("schema", "ts", "session_id", "release", "started_at", "runner", "cwd"),  # the v1 keys stay
        *("pane", "role", "class", "state", "state_ts", "last_delivered_outbound", "rev"),
    }
    assert stat.S_IMODE((folder(home) / "abc.json").stat().st_mode) == 0o600
    rec = session.read("abc")
    assert rec == session.Record(
        session_id="abc",
        release=raw["release"],
        ts=raw["ts"],
        started_at=raw["started_at"],
        runner="claude-code",
        cwd="/work/here",
        pane="%12",
        role="worker",
        class_=None,
        state=None,
        state_ts=None,
        last_delivered_outbound=None,
        rev=1,
        nudged=False,
        schema=2,
    )
    assert rec.to_dict()["class"] is None and "nudged" not in rec.to_dict()


@pytest.mark.parametrize("pane", ["", "12", "%", "%x", "%1;rm", "%1\n", "%12345678901"])
def test_pane_is_only_a_tmux_pane_id(home, monkeypatch, capsys, pane):
    monkeypatch.setenv("TMUX_PANE", pane)
    start(monkeypatch, capsys)
    assert on_disk(home)["pane"] is None


def test_the_class_comes_from_the_working_folder(home, monkeypatch, capsys):
    secret = home / "secret"
    policy_with_classes(home, secret)
    start(monkeypatch, capsys, "a", str(secret))
    start(monkeypatch, capsys, "b", str(home / "open" / "x"))
    start(monkeypatch, capsys, "c", str(home / "elsewhere"))
    assert [session.read(s).class_ for s in "abc"] == ["private", "open", "unclassified"]


def test_no_classes_or_a_policy_that_does_not_load_is_class_none(home, monkeypatch, capsys):
    start(monkeypatch, capsys, "a")
    assert session.read("a").class_ is None
    (home / "wshome").mkdir(exist_ok=True)
    (home / "wshome" / "policy.toml").write_text("this is not toml = = =\n")
    start(monkeypatch, capsys, "b")
    assert session.read("b").class_ is None  # a consumer treats None as the strictest


def test_a_v1_record_reads_with_the_defaults(home):
    folder(home).mkdir(parents=True)
    (folder(home) / "old.json").write_text(
        json.dumps(
            {
                "schema": 1,
                "ts": "2026-11-01T00:00:00.000Z",
                "session_id": "old",
                "release": "0.1.8",
                "started_at": "2026-11-01T00:00:00+00:00",
                "runner": "claude-code",
                "cwd": "/w",
                "nudged": True,
            }
        )
    )
    rec = session.read("old")
    assert (rec.role, rec.class_, rec.rev, rec.state, rec.pane, rec.schema, rec.nudged) == (
        "worker",
        None,
        0,
        None,
        None,
        1,
        True,
    )
    assert rec.last_delivered_outbound is None and rec.cwd == "/w"
    # the first write migrates it
    assert session.update("old", state="busy").rev == 1
    assert on_disk(home, "old")["schema"] == 2 and on_disk(home, "old")["nudged"] is True


def test_unreadable_missing_or_corrupt_reads_as_none(home, monkeypatch, capsys):
    assert session.read("nope") is None
    folder(home).mkdir(parents=True)
    (folder(home) / "bad.json").write_text("{not json")
    (folder(home) / "list.json").write_text("[]")
    (folder(home) / "norelease.json").write_text(json.dumps({"session_id": "norelease"}))
    for sid in ("bad", "list", "norelease"):
        assert session.read(sid) is None
    # start and nudge stay silent and replace a corrupt record
    start(monkeypatch, capsys, "bad")
    assert session.read("bad").rev == 1
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "list"})))
    assert cli.main(["session", "nudge", *paths.pin_of().args()]) == 0
    assert capsys.readouterr().out == ""


def test_odd_values_in_a_record_take_their_defaults(home):
    folder(home).mkdir(parents=True)
    (folder(home) / "odd.json").write_text(
        json.dumps(
            {
                "release": "1",
                "role": "root",
                "state": "asleep",
                "pane": "%1; evil",
                "rev": True,
                "last_delivered_outbound": {"id": 5},
                "schema": "two",
            }
        )
    )
    rec = session.read("odd")
    assert (rec.role, rec.state, rec.pane, rec.rev, rec.last_delivered_outbound, rec.schema) == (
        "worker",
        None,
        None,
        0,
        None,
        1,
    )


# --- update: compare and set -----------------------------------------------------------------------------------


def test_update_changes_state_and_delivery_and_bumps_rev(home, monkeypatch, capsys):
    start(monkeypatch, capsys)
    assert session.read("s1").rev == 1
    busy = session.update("s1", state="busy")
    assert (busy.state, busy.rev) == ("busy", 2) and busy.state_ts
    sent = session.update("s1", last_delivered_outbound={"id": "m1", "ts": "2026-11-08T00:00:00Z", "extra": 1})
    assert sent.state == "busy" and sent.rev == 3  # a field left out is kept
    assert sent.last_delivered_outbound == {"id": "m1", "ts": "2026-11-08T00:00:00Z"}
    idle = session.update("s1", state="idle", last_delivered_outbound=None)
    assert (idle.state, idle.last_delivered_outbound, idle.rev) == ("idle", None, 4)
    assert session.update("s1", state=None).state is None
    assert session.read("s1") == session.update("s1", expect_rev=5)  # nothing to change: no write, no bump


def test_update_refuses_a_record_that_changed_since_it_was_read(home, monkeypatch, capsys):
    start(monkeypatch, capsys)
    seen = session.read("s1").rev
    session.update("s1", state="busy")  # someone else writes
    with pytest.raises(WsError) as e:
        session.update("s1", state="idle", expect_rev=seen)
    assert e.value.kind == "conflict"
    assert session.read("s1").state == "busy"  # nothing was written
    assert session.update("s1", state="idle", expect_rev=seen + 1).state == "idle"


def test_update_checks_its_values_and_the_record(home, monkeypatch, capsys):
    with pytest.raises(WsError) as e:
        session.update("missing", state="busy")
    assert e.value.kind == "not_found"
    start(monkeypatch, capsys)
    for bad in ({"state": "asleep"}, {"last_delivered_outbound": {"id": "x"}}, {"last_delivered_outbound": "m1"}):
        with pytest.raises(WsError) as e:
            session.update("s1", **bad)
        assert e.value.kind == "bad_input"
    assert session.read("s1").rev == 1


def test_update_cannot_set_the_role_or_the_class(home, monkeypatch, capsys):
    start(monkeypatch, capsys)
    for field in ("role", "class_", "class", "pane", "rev", "release"):
        with pytest.raises(TypeError):
            session.update("s1", **{field: "lead"})
    assert session.read("s1").role == "worker"


def test_concurrent_updates_lose_no_write(home, monkeypatch, capsys):
    start(monkeypatch, capsys)
    errors = []

    def work():
        try:
            for _ in range(10):
                session.update("s1", state="busy")
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == [] and session.read("s1").rev == 1 + 80


def test_two_writers_with_the_same_rev_have_one_winner(home, monkeypatch, capsys):
    start(monkeypatch, capsys)
    rev = session.read("s1").rev
    results = []

    def work(state):
        try:
            session.update("s1", state=state, expect_rev=rev)
            results.append("won")
        except WsError as e:
            results.append(e.kind)

    threads = [threading.Thread(target=work, args=("busy" if i % 2 else "idle",)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(results) == ["conflict"] * 5 + ["won"]
    assert session.read("s1").rev == rev + 1


# --- the launch token: the only way a role is set --------------------------------------------------------------


def prepare(role="lead") -> str:
    return session.prepare_launch(role)


def test_prepare_writes_one_private_single_use_token(home):
    token = prepare("lead")
    path = folder(home) / "launch" / token
    assert len(token) == 32 and stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE((folder(home) / "launch").stat().st_mode) == 0o700
    assert json.loads(path.read_text())["role"] == "lead"
    assert prepare("worker") != token
    with pytest.raises(WsError) as e:
        prepare("root")
    assert e.value.kind == "usage"


def test_the_command_prints_the_variable_and_json_has_it_too(home, capsys):
    assert cli.main(["session", "prepare", "--role", "reviewer"]) == 0
    out = capsys.readouterr().out
    name, _, token = out.strip().partition("=")
    assert name == "WS_LAUNCH" and (folder(home) / "launch" / token).is_file()
    assert cli.main(["session", "prepare", "--role", "lead", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["role"] == "lead" and data["ttl_seconds"] == session.LAUNCH_TTL == 600
    assert (folder(home) / "launch" / data["WS_LAUNCH"]).is_file()
    assert cli.main(["session", "prepare", "--role", "boss"]) == 2  # argparse refuses an unknown role
    assert cli.main(["session", "prepare"]) == 2


def test_start_claims_the_token_and_deletes_it(home, monkeypatch, capsys):
    token = prepare("lead")
    monkeypatch.setenv(session.LAUNCH_ENV, token)
    start(monkeypatch, capsys, "s1")
    assert session.read("s1").role == "lead"
    assert not (folder(home) / "launch" / token).exists()
    assert list((folder(home) / "launch").iterdir()) == []


def test_a_token_works_once(home, monkeypatch, capsys):
    token = prepare("lead")
    monkeypatch.setenv(session.LAUNCH_ENV, token)
    start(monkeypatch, capsys, "first")
    start(monkeypatch, capsys, "second")  # the same environment, replayed
    assert (session.read("first").role, session.read("second").role) == ("lead", "worker")


def test_expired_missing_and_forged_tokens_give_worker(home, monkeypatch, capsys):
    clock = [1_000_000.0]
    monkeypatch.setattr(session, "_now", lambda: clock[0])
    old = prepare("lead")
    clock[0] += session.LAUNCH_TTL + 1
    monkeypatch.setenv(session.LAUNCH_ENV, old)
    start(monkeypatch, capsys, "late")
    fresh = prepare("lead")
    clock[0] += session.LAUNCH_TTL  # exactly at the limit still counts
    monkeypatch.setenv(session.LAUNCH_ENV, fresh)
    start(monkeypatch, capsys, "inside")
    monkeypatch.delenv(session.LAUNCH_ENV)
    start(monkeypatch, capsys, "none")
    for forged in (LEAD, "../" + old, "x" * 32, "", "../../etc/passwd", old.upper()):
        monkeypatch.setenv(session.LAUNCH_ENV, forged)
        start(monkeypatch, capsys, "forged")
    assert [session.read(s).role for s in ("late", "inside", "none", "forged")] == [
        "worker",
        "lead",
        "worker",
        "worker",
    ]


def test_a_token_planted_in_the_folder_is_not_taken_on_trust(home, monkeypatch, capsys):
    launch = folder(home) / "launch"
    launch.mkdir(parents=True)
    cases = {
        "a" * 32: ({"role": "lead", "created": session._now()}, 0o644),  # not private
        "b" * 32: ({"role": "lead", "created": session._now() + 3600}, 0o600),  # from the future
        "c" * 32: ({"role": "admin", "created": session._now()}, 0o600),  # not a role
        "d" * 32: ({"role": "lead", "created": True}, 0o600),
        "e" * 32: ({"role": "lead"}, 0o600),
    }
    for name, (body, mode) in cases.items():
        (launch / name).write_text(json.dumps(body))
        (launch / name).chmod(mode)
    (launch / ("f" * 32)).symlink_to(home / "elsewhere")
    (home / "elsewhere").write_text(json.dumps({"role": "lead", "created": session._now()}))
    (home / "elsewhere").chmod(0o600)
    (launch / ("9" * 32)).write_text("not json")
    (launch / ("9" * 32)).chmod(0o600)
    for name in [*cases, "f" * 32, "9" * 32]:
        monkeypatch.setenv(session.LAUNCH_ENV, name)
        start(monkeypatch, capsys, name)
        assert session.read(name).role == "worker", name
        assert not (launch / name).exists() and not (launch / name).is_symlink()


def test_a_role_or_class_in_the_hook_payload_is_ignored(home, monkeypatch, capsys):
    start(monkeypatch, capsys, "s1", payload={"role": "lead", "class": "open", "rev": 99, "pane": "%1"})
    rec = session.read("s1")
    assert (rec.role, rec.class_, rec.rev, rec.pane) == ("worker", None, 1, None)


def test_a_resumed_or_compacted_start_keeps_the_role_and_the_cursor(home, monkeypatch, capsys):
    monkeypatch.setenv(session.LAUNCH_ENV, prepare("reviewer"))
    start(monkeypatch, capsys, "s1")
    session.update("s1", state="busy", last_delivered_outbound={"id": "m1", "ts": "t"})
    monkeypatch.delenv(session.LAUNCH_ENV)
    start(monkeypatch, capsys, "s1", payload={"source": "compact"})
    rec = session.read("s1")
    assert (rec.role, rec.rev, rec.state, rec.last_delivered_outbound) == ("reviewer", 3, None, {"id": "m1", "ts": "t"})
    monkeypatch.setenv(session.LAUNCH_ENV, prepare("lead"))  # the launcher may start the id again as something else
    start(monkeypatch, capsys, "s1", payload={"source": "resume"})
    assert session.read("s1").role == "lead"


def test_nudge_keeps_the_v2_fields_and_bumps_rev(home, monkeypatch, capsys):
    monkeypatch.setenv("WS_RELEASE", "r1")
    monkeypatch.setenv(session.LAUNCH_ENV, prepare("lead"))
    start(monkeypatch, capsys, "s1")
    session.update("s1", state="busy")
    monkeypatch.setenv("WS_RELEASE", "r2")
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/w"})))
    assert cli.main(["session", "nudge", *paths.pin_of().args()]) == 0
    assert "is out" in capsys.readouterr().out
    rec = session.read("s1")
    assert (rec.role, rec.state, rec.nudged, rec.rev) == ("lead", "busy", True, 3)


# --- the folder: guard files, locks and the launch folder are not records --------------------------------------


def test_guard_files_and_the_launch_folder_are_not_records(home, monkeypatch, capsys):
    start(monkeypatch, capsys, "s1")
    prepare("lead")
    (folder(home) / "s1.presence.json").write_text(json.dumps({"seen": 1}))
    (folder(home) / "s1.pane_close.json").write_text(json.dumps({"release": "x"}))
    assert session.read("s1.presence") is None
    assert session.read("launch") is None
    # the registry lock sits beside the folder, so the folder holds records only
    assert (home / "wshome" / ".sessions.lock").is_file() and not (folder(home) / ".registry.lock").exists()
    assert sorted(p.name for p in folder(home).glob("*.json")) == ["s1.json", "s1.pane_close.json", "s1.presence.json"]


def test_prune_keeps_the_thirty_days_and_leaves_the_launch_folder(home, monkeypatch, capsys):
    start(monkeypatch, capsys, "old")
    start(monkeypatch, capsys, "new")
    prepare("lead")
    os.utime(folder(home) / "old.json", (1, 1))
    start(monkeypatch, capsys, "newer")
    assert not (folder(home) / "old.json").exists() and (folder(home) / "new.json").exists()
    assert (folder(home) / "launch").is_dir() and session._KEEP_DAYS == 30


def test_the_home_page_source_still_reads_v2(home, monkeypatch, capsys):
    monkeypatch.setenv("TMUX_PANE", "%3")
    start(monkeypatch, capsys, "s1", "/work/here")
    session.update("s1", state="busy")
    (folder(home) / "s1.presence.json").write_text(json.dumps({"session_id": "s1", "ts": "x"}))
    prepare("lead")

    class Env:
        state = home / "wshome"

    got = ws_sessions.read("sessions", Env())
    assert [type(s) for s in got] == [home_records.Session]
    assert (got[0].id, got[0].runner) == ("s1", "claude-code") and got[0].started_at and got[0].last_seen


def test_prepare_and_start_are_refused_from_a_gated_shell():
    from ws import policy

    pol = policy.default_policy()
    for cmd in ("ws session prepare --role lead", "ws session start --session-id x"):
        assert policy.is_denied_command(pol, cmd), cmd


# --- who is calling: the runner stamp and resolve_caller -------------------------------------------------------


def proc(pid, name="claude", start=None, ppid=1) -> session.Proc:
    return session.Proc(pid, start or f"s{pid}", ppid, name)


def chain(monkeypatch, *procs):
    monkeypatch.setattr(session, "_ancestors", lambda: list(procs))


def test_start_stamps_the_runner_process(home, monkeypatch, capsys):
    # hook -> sh -c -> uv -> the runner: the shell and the launcher are passed over
    chain(monkeypatch, proc(50, "sh"), proc(40, "uv"), proc(30, "claude"), proc(20, "-bash"), proc(10, "tmux"))
    start(monkeypatch, capsys, "a")
    raw = on_disk(home, "a")
    assert (raw["runner_pid"], raw["runner_start"]) == (30, "s30")
    rec = session.read("a")
    assert (rec.runner_pid, rec.runner_start) == (30, "s30") and rec.schema == session.SCHEMA_VERSION == 2
    assert rec.to_dict()["runner_pid"] == 30
    chain(monkeypatch, proc(31, "claude"))  # a resumed start is a new runner process: it is stamped again
    start(monkeypatch, capsys, "a")
    assert session.read("a").runner_pid == 31 and session.read("a").rev == 2
    chain(monkeypatch, proc(50, "bash"), proc(20, "zsh"))  # nothing that is not a shell: no stamp, and none kept
    start(monkeypatch, capsys, "a")
    assert "runner_pid" not in on_disk(home, "a") and session.read("a").runner_pid is None


def test_the_real_ancestors_are_this_process_s_parents(home):
    mine = session.proc_info(os.getpid())
    assert mine and mine.pid == os.getpid() and mine.ppid == os.getppid() and mine.start
    assert session.proc_info(os.getpid()) == mine  # the same process reads the same, each time
    assert session.proc_info(2**22 + 12345) is None


def test_resolve_caller_finds_the_nearest_ancestor(home, monkeypatch, capsys):
    chain(monkeypatch, proc(300, "claude"))
    start(monkeypatch, capsys, "outer")
    chain(monkeypatch, proc(200, "claude"))
    start(monkeypatch, capsys, "inner")
    chain(monkeypatch, proc(5, "bash"), proc(200, "claude"), proc(6, "bash"), proc(300, "claude"), proc(1, "init"))
    assert session.resolve_caller().session_id == "inner"  # nearest first
    chain(monkeypatch, proc(5, "bash"), proc(300, "claude"))
    assert session.resolve_caller().session_id == "outer"
    # a cleared session leaves two records for one runner: the newest wins
    chain(monkeypatch, proc(300, "claude"))
    start(monkeypatch, capsys, "outer-2")
    raw = on_disk(home, "outer")
    raw["started_at"] = "2020-01-01T00:00:00+00:00"
    (folder(home) / "outer.json").write_text(json.dumps(raw))
    assert session.resolve_caller().session_id == "outer-2"
    # the env argument picks the registry, as elsewhere
    assert session.resolve_caller({"WS_HOME": str(home / "elsewhere")}) is None


def test_resolve_caller_none_without_a_match(home, monkeypatch, capsys):
    chain(monkeypatch, proc(300, "claude", start="1111"))
    start(monkeypatch, capsys, "a")
    env = {"WS_HOME": str(home / "wshome")}
    chain(monkeypatch, proc(5, "bash"), proc(300, "claude", start="2222"))  # the pid was reused: another start time
    assert session.resolve_caller(env) is None
    chain(monkeypatch, proc(5, "bash"), proc(301, "claude", start="1111"))
    assert session.resolve_caller(env) is None
    chain(monkeypatch)  # no ancestors at all
    assert session.resolve_caller(env) is None
    # a record with no stamp, or a damaged one, is never a match
    (folder(home) / "bare.json").write_text(json.dumps({"release": "1", "session_id": "bare"}))
    junk = {"release": "1", "session_id": "j", "runner_pid": "9", "runner_start": 3}
    (folder(home) / "junk.json").write_text(json.dumps(junk))
    (folder(home) / "broken.json").write_text("{nope")
    chain(monkeypatch, proc(9, "claude", start="3"))
    assert session.resolve_caller(env) is None


def test_resolve_caller_works_for_a_real_child_and_not_a_real_orphan(home, monkeypatch, capsys):
    me = session.proc_info(os.getpid())
    chain(monkeypatch, me)
    start(monkeypatch, capsys, "me")
    out = home / "out.txt"
    code = textwrap.dedent(
        """
        import os, sys, time
        out, orphan = sys.argv[1], sys.argv[2] == "orphan"
        if orphan:
            if os.fork():
                os._exit(0)
            os.setsid()
            if os.fork():
                os._exit(0)
            time.sleep(0.3)
        from ws import session
        rec = session.resolve_caller()
        open(out, "w").write(rec.session_id if rec else "none")
        """
    )
    subprocess.run([sys.executable, "-c", code, str(out), "child"], check=True, timeout=60)
    assert out.read_text() == "me"  # a child of the stamped runner
    out.unlink()
    subprocess.run([sys.executable, "-c", code, str(out), "orphan"], check=True, timeout=60)
    deadline = time.monotonic() + 15
    while not out.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert out.read_text() == "none"  # owned by init: not a person and not a session


def test_setters_cannot_set_the_stamp(home, monkeypatch, capsys):
    chain(monkeypatch, proc(30, "claude"))
    start(monkeypatch, capsys, "a")
    params = set(inspect.signature(session.update).parameters)
    assert params == {"session_id", "state", "last_delivered_outbound", "expect_rev", "env"}
    with pytest.raises(TypeError):
        session.update("a", runner_pid=99)  # type: ignore[call-arg]
    assert session.update("a", state="busy").runner_pid == 30  # an update keeps the stamp it found
    assert on_disk(home, "a")["runner_start"] == "s30"
    # a stamp in the hook's own payload is not read: it is the process tree that stamps
    chain(monkeypatch, proc(31, "claude"))
    start(monkeypatch, capsys, "b", payload={"runner_pid": 1, "runner_start": "x", "role": "lead"})
    assert (session.read("b").runner_pid, session.read("b").role) == (31, "worker")
    # nudge rewrites the record and carries the stamp over; it does not make one
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "a"})))
    assert cli.main(["session", "nudge", *paths.pin_of().args()]) == 0
    capsys.readouterr()
    assert session.read("a").runner_pid == 30
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "fresh"})))
    assert cli.main(["session", "nudge", *paths.pin_of().args()]) == 0
    assert session.read("fresh").runner_pid is None


def test_old_records_still_read(home):
    folder(home).mkdir(parents=True)
    for name, extra in (("v1", {"schema": 1}), ("v2", {"schema": 2, "role": "lead", "class": "x", "rev": 3})):
        base = {"ts": "2026-11-01T00:00:00.000Z", "session_id": name, "release": "0.1.8", "cwd": "/w"}
        (folder(home) / f"{name}.json").write_text(json.dumps({**base, **extra}))
    old, new = session.read("v1"), session.read("v2")
    assert (old.runner_pid, old.runner_start, old.role) == (None, None, "worker")
    assert (new.runner_pid, new.role, new.class_, new.rev) == (None, "lead", "x", 3)
    assert "runner_pid" not in old.to_dict() and session.SCHEMA_VERSION == 2
    assert session.update("v2", state="idle").rev == 4 and session.read("v2").runner_pid is None
    assert session.resolve_caller() is None  # neither is stamped, so neither can be a caller
