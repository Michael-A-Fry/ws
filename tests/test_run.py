"""`ws run` and `ws runs`: refusals still leave a record, the month's spend is summed from the log, the runner gets a
scrubbed environment, leases are released however the run ends, and every record validates against the schema.

The runner is a small fake `claude` on PATH that prints a real result captured from claude 2.1.308 (tests/fixtures).
The broker is faked at its client functions; tests/e2e/test_run_record.py runs the real runners and the real broker."""

from __future__ import annotations

import _thread
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jsonschema
import pytest

from ws import broker, cli, doctor, jobs, osinfo, policy, run, runrecord
from ws.adapters import claude_code, opencode
from ws.profile import dumps

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN = "T" * 43
URL = f"http://127.0.0.1:1/l/{TOKEN}"
VALIDATOR = jsonschema.Draft202012Validator(runrecord.schema())

FAKE = f"""#!{sys.executable}
import json, os, signal, subprocess, sys, time
if "--version" in sys.argv:
    print("9.9.9 (Fake Code)")
    sys.exit(0)
home = os.environ["WS_HOME"]
prompt = sys.stdin.read()
with open(os.path.join(home, "fake-saw.json"), "w") as f:
    json.dump({{"env": dict(os.environ), "argv": sys.argv[1:], "cwd": os.getcwd(), "stdin": prompt, "pid": os.getpid()}}, f)
if prompt == "sleep":
    time.sleep(60)
if prompt == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = (
        "import os, signal, time\\nsignal.signal(signal.SIGTERM, signal.SIG_IGN)\\n"
        "open(os.path.join(os.environ['WS_HOME'], 'grandchild.pid'), 'w').write(str(os.getpid()))\\ntime.sleep(60)"
    )
    subprocess.Popen([sys.executable, "-c", child])
    time.sleep(60)
if prompt == "flood":
    for _ in range(200):
        sys.stdout.write("x" * 10000)
        sys.stdout.flush()
    sys.stdout.write("\\n")
if prompt == "garbage":
    print("this is not json")
if prompt == "fail":
    print("boom at " + os.environ.get("ANTHROPIC_BASE_URL", ""), file=sys.stderr)
    sys.exit(2)
if prompt == "ok":
    sys.stdout.write(open({str(FIXTURES / "claude-result-ok.json")!r}).read())
if prompt == "max-turns":
    sys.stdout.write(open({str(FIXTURES / "claude-result-max-turns.json")!r}).read())
    sys.exit(1)
"""

JOB = """
runner = "claude-code"
model = "claude-haiku-4-5"
prompt = "{prompt}"
timeout_s = {timeout}
{extra}
[[secrets]]
name = "anthropic"
host = "api.anthropic.com"
"""


class Leases:
    """A stand-in for the broker client: hands out a lease, remembers what was released."""

    def __init__(self):
        self.requested: list[tuple] = []
        self.released: list[str] = []
        self.refuse: broker.BrokerError | None = None
        self.unavailable_times = 0  # the next N requests find no broker
        self.not_running = True  # ... and it is really not running (not a timeout, a permission error or a bad reply)

    def request(self, name, purpose, host, socket=None, ttl_s=None):
        self.requested.append((name, purpose, host))
        if self.unavailable_times > 0:
            self.unavailable_times -= 1
            raise broker.BrokerUnavailable("no broker answering", not_running=self.not_running)
        if self.refuse:
            raise self.refuse
        return broker.Lease(
            lease=f"lease{len(self.requested)}", base_url=URL, header="x-api-key", expires_at="2099-01-01T00:00:00Z"
        )

    def release(self, lease, socket=None):
        self.released.append(lease.lease if isinstance(lease, broker.Lease) else lease)


class Ws:
    """A scratch WS_HOME with a fake runner on PATH, plus helpers to write jobs and read what happened."""

    def __init__(self, tmp_path: Path, leases: Leases):
        self.home, self.leases, self.bin = tmp_path / "wshome", leases, tmp_path / "bin"
        self.log = self.home / "logs" / "runs.jsonl"

    def cap(self, usd) -> None:
        (self.home / "profile.toml").write_text(
            dumps({"roles": {"budget": {"monthly_cap_usd": usd}}}), encoding="utf-8"
        )

    def job(self, name="daily", prompt="ok", timeout=30, extra="") -> None:
        text = JOB.format(prompt=prompt, timeout=timeout, extra=extra)
        (self.home / "jobs" / f"{name}.toml").write_text(text, encoding="utf-8")

    def records(self) -> list[dict]:
        return runrecord.read(self.log)

    def saw(self) -> dict:
        return json.loads((self.home / "fake-saw.json").read_text(encoding="utf-8"))


@pytest.fixture
def ws(tmp_path, monkeypatch):
    w = Ws(tmp_path, Leases())
    (w.home / "jobs").mkdir(parents=True)
    w.bin.mkdir()
    exe = w.bin / "claude"
    exe.write_text(FAKE, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("WS_HOME", str(w.home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("PATH", f"{w.bin}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(broker, "request", w.leases.request)
    monkeypatch.setattr(broker, "release", w.leases.release)
    w.started = []  # sockets ws was asked to start a broker on
    monkeypatch.setattr(run, "_start_broker", lambda sock: w.started.append(sock) or False)  # never a real broker
    monkeypatch.setattr(run, "RECOVERY_WAIT_S", 0)
    w.cap(20)
    return w


def valid(rec: dict) -> dict:
    errors = sorted(VALIDATOR.iter_errors(rec), key=lambda e: list(e.path))
    assert not errors, [f"{list(e.path)}: {e.message}" for e in errors]
    return rec


# --- a normal run ------------------------------------------------------------------------------------------------


def test_a_run_leaves_one_complete_record(ws, capsys, tmp_path):
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "ok" and rec["exit_code"] == 0 and rec["error"] is None
    assert rec["job"] == "daily" and rec["runner"] == "claude-code" and rec["runner_version"] == "9.9.9"
    assert rec["model_requested"] == "claude-haiku-4-5"
    assert rec["models_used"][0]["model"] == "claude-sonnet-4-5"  # what the runner says it used, not what was asked
    assert rec["tokens"] == {"input": 20, "output": 10, "cache_creation": 0, "cache_read": 0}
    assert rec["cost_usd"] == 0.00021 and rec["cost_basis"] == "list" and rec["num_turns"] == 2
    assert rec["session_id"] and rec["duration_api_ms"] == 82 and rec["duration_ms"] >= 0
    assert rec["secrets_used"] == [{"name": "anthropic", "host": "api.anthropic.com"}]
    assert rec["policy"] == {"dial": "prepare", "rule_of_two": "ok"}
    assert rec["cwd"] == str(tmp_path / "userhome" / ".local" / "state" / "ws" / "work" / rec["run_id"])
    assert stat.S_IMODE(ws.log.stat().st_mode) == 0o600
    assert ws.leases.requested == [("anthropic", "run", "api.anthropic.com")]
    assert ws.leases.released == ["lease1"]
    assert rec["run_id"] in capsys.readouterr().out


def test_the_runner_gets_the_gate_config_the_lease_and_bare(ws):
    ws.job()
    cli.main(["run", "daily"])
    saw = ws.saw()
    rec = ws.records()[0]
    settings = Path(saw["argv"][saw["argv"].index("--settings") + 1])
    assert settings == ws.home / "runs" / rec["run_id"] / "gate" / "settings.json" and settings.is_file()
    assert "--bare" in saw["argv"] and "ok" not in saw["argv"]
    assert saw["stdin"] == "ok", "the prompt goes in on stdin, not in argv"
    prompt = ws.home / "runs" / rec["run_id"] / "prompt.txt"
    assert prompt.read_text() == "ok" and stat.S_IMODE(prompt.stat().st_mode) == 0o600
    assert rec["exe_path"] == str(ws.bin / "claude")
    assert saw["env"]["ANTHROPIC_BASE_URL"] == URL
    assert saw["env"]["ANTHROPIC_API_KEY"] == claude_code.PLACEHOLDER_KEY


def test_bare_false_and_max_turns_reach_the_command_line(ws):
    ws.job(extra='bare = false\nmax_turns = 3\n[runner_options]\nallowed_tools = ["Read", "Grep"]\n')
    cli.main(["run", "daily"])
    argv = ws.saw()["argv"]
    assert "--bare" not in argv
    assert argv[argv.index("--max-turns") + 1] == "3"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Grep"


def test_the_runner_sees_a_scrubbed_environment(ws, monkeypatch):
    planted = "PLANTED_" + os.urandom(4).hex().upper()
    value = "v-" + os.urandom(8).hex()
    monkeypatch.setenv(planted, value)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k-" + os.urandom(8).hex())
    monkeypatch.setenv("GITHUB_TOKEN", "g-" + os.urandom(8).hex())
    monkeypatch.setenv("WS_BROKER_SOCKET", str(ws.home / "run" / "b.sock"))
    ws.job()
    cli.main(["run", "daily"])
    env = ws.saw()["env"]
    assert planted not in env and "GITHUB_TOKEN" not in env and "WS_BROKER_SOCKET" not in env
    assert not any(value in v for v in env.values())
    assert env["ANTHROPIC_API_KEY"] == claude_code.PLACEHOLDER_KEY  # the parent's key did not leak through
    allowed = set(run.PASS_ENV) | {"WS_HOME", "DISABLE_AUTOUPDATER", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"}
    added_by_the_os = {k for k in env if k.startswith("__CF")}  # macOS adds its own
    ok_extra = {"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", "PWD", "SHLVL", "_", "LC_CTYPE"}
    assert set(env) - added_by_the_os <= allowed | ok_extra


def test_job_env_is_the_only_way_to_add_variables(ws):
    ws.job(extra='[runner_options]\nenv = { MY_FLAG = "on" }\n')
    cli.main(["run", "daily"])
    assert ws.saw()["env"]["MY_FLAG"] == "on"


@pytest.mark.parametrize("name", ["CLAUDE_CONFIG_DIR", "ANTHROPIC_BASE_URL", "PATH", "WS_HOME", "FOO_API_KEY"])
def test_job_env_cannot_set_what_ws_or_the_broker_owns(ws, capsys, name):
    ws.job(extra=f'[runner_options]\nenv = {{ {name} = "x" }}\n')
    assert cli.main(["run", "daily"]) == 2
    assert f"runner_options.env: {name} is reserved" in capsys.readouterr().err
    assert not (ws.home / "fake-saw.json").exists()


# --- refusals ----------------------------------------------------------------------------------------------------


def refused(ws, capsys, *, expect: str) -> dict:
    assert cli.main(["run", "daily"]) == 3
    assert expect in capsys.readouterr().err
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "refused" and expect in rec["error"] and rec["exit_code"] is None
    assert not (ws.home / "fake-saw.json").exists(), "the runner started"
    assert ws.leases.requested == [], "a lease was taken for a refused job"
    return rec


def test_no_cap_refuses_and_still_records(ws, capsys):
    (ws.home / "profile.toml").unlink()
    ws.job()
    refused(ws, capsys, expect="WS103")


def test_a_cap_of_zero_or_a_string_is_no_cap(ws, capsys):
    ws.cap("twenty")
    ws.job()
    refused(ws, capsys, expect="WS103")


def seed(ws, cost: float, when: datetime | None = None, status: str = "ok") -> None:
    when = when or datetime.now(UTC)
    runrecord.append(
        ws.log,
        runrecord.new(
            run_id=runrecord.new_run_id(when), job="old", runner="claude-code", started_at=runrecord.iso(when),
            ended_at=runrecord.iso(when), status=status, cost_usd=cost, ws_version="0",
        ),
    )  # fmt: skip


def test_spend_at_the_cap_refuses(ws, capsys):
    ws.cap(5)
    seed(ws, 3.0)
    seed(ws, 2.0)
    ws.job()
    rec = refused_with_prior(ws, capsys, "at the monthly cap of US$5")
    assert rec["job"] == "daily"


def refused_with_prior(ws, capsys, expect) -> dict:
    assert cli.main(["run", "daily"]) == 3
    assert expect in capsys.readouterr().err
    rec = ws.records()[-1]
    valid(rec)
    assert rec["status"] == "refused" and not (ws.home / "fake-saw.json").exists()
    return rec


def test_spend_under_the_cap_runs_and_last_months_spend_does_not_count(ws):
    ws.cap(5)
    seed(ws, 4.99)
    seed(ws, 100.0, datetime.now(UTC).replace(day=1) - timedelta(days=2))
    ws.job()
    assert cli.main(["run", "daily"]) == 0


def test_month_spend_sums_only_this_month_and_ignores_nulls():
    now = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
    recs = [
        {"started_at": "2026-10-01T00:00:00.000Z", "cost_usd": 1.5},
        {"started_at": "2026-10-31T23:59:59.000Z", "cost_usd": 2},
        {"started_at": "2026-09-30T23:59:59.000Z", "cost_usd": 40},
        {"started_at": "2026-10-05T00:00:00.000Z", "cost_usd": None},
        {"started_at": "2026-10-06T00:00:00.000Z", "cost_usd": True},
        {"cost_usd": 9},
    ]
    assert runrecord.month_spend(recs, now) == 3.5


def test_the_rule_of_two_refuses(ws, capsys):
    ws.job(extra='sources = ["mail", "notes"]')
    rec = refused(ws, capsys, expect="Rule of Two")
    assert rec["policy"]["rule_of_two"] == "refused"


def test_a_named_exception_lets_the_job_run_and_is_recorded(ws):
    (ws.home / "policy.toml").write_text(
        policy.default_toml()
        + '\n[[exceptions]]\nname = "digest-ok"\njob = "daily"\nsources = ["mail", "notes"]\nreason = "writes only to me"\n',
        encoding="utf-8",
    )
    ws.job(extra='sources = ["mail", "notes"]')
    assert cli.main(["run", "daily"]) == 0
    assert valid(ws.records()[0])["policy"]["rule_of_two"] == "exception:digest-ok"


def test_a_leave_source_refuses(ws, capsys):
    (ws.home / "policy.toml").write_text(
        policy.default_toml() + '\n[sources.vault]\nmode = "leave"\n', encoding="utf-8"
    )
    ws.job(extra='sources = ["vault"]')
    refused(ws, capsys, expect="leave")


def test_the_broker_refusing_a_secret_is_a_refusal(ws, capsys):
    ws.leases.refuse = broker.BrokerRefused("host_not_allowed", "anthropic is not allowed at api.anthropic.com")
    ws.job()
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "refused" and "host_not_allowed" in rec["error"] and rec["secrets_used"] == []
    assert not (ws.home / "fake-saw.json").exists()


def test_no_broker_is_an_error_not_a_refusal(ws):
    ws.leases.refuse = broker.BrokerUnavailable("no broker answering")
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    assert valid(ws.records()[0])["status"] == "error"


# --- one recovery per known failure ------------------------------------------------------------------------------


def test_a_broker_that_is_down_is_started_once_and_the_lease_asked_for_again(ws, monkeypatch):
    ws.leases.unavailable_times = 1
    monkeypatch.setattr(run, "_start_broker", lambda sock: ws.started.append(sock) or True)
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "ok" and rec["secrets_used"] == [{"name": "anthropic", "host": "api.anthropic.com"}]
    assert rec["recovered"] == [{"kind": "broker_unavailable", "action": "started the broker", "ok": True}]
    assert ws.started == [ws.home.absolute() / "run" / "broker.sock"]
    assert len(ws.leases.requested) == 2 and ws.leases.released == ["lease2"]


def test_a_broker_that_will_not_start_ends_the_run_with_the_typed_error_and_its_hint(ws, capsys):
    ws.leases.unavailable_times = 5
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and "no broker answering" in rec["error"] and "ws doctor" in rec["error"]
    assert rec["recovered"] == [{"kind": "broker_unavailable", "action": "started the broker", "ok": False}]
    assert len(ws.leases.requested) == 1  # it did not ask again after a fix that failed
    assert not (ws.home / "fake-saw.json").exists()  # the runner never started
    assert "ws doctor" in capsys.readouterr().err


def test_a_failure_that_comes_back_after_the_fix_is_tried_only_once(ws, monkeypatch):
    ws.leases.unavailable_times = 5
    monkeypatch.setattr(run, "_start_broker", lambda sock: ws.started.append(sock) or True)
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and "already started the broker, once" in rec["error"]
    assert rec["recovered"] == [{"kind": "broker_unavailable", "action": "started the broker", "ok": True}]
    assert len(ws.leases.requested) == 2 and len(ws.started) == 1


def test_a_broker_ws_does_not_own_is_waited_for_not_started(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("WS_BROKER_SOCKET", str(tmp_path / "elsewhere.sock"))
    monkeypatch.setattr(broker, "status", lambda socket=None, timeout=3.0: {"leases": 0})  # it is up again
    ws.leases.unavailable_times = 1
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    assert rec["recovered"] == [{"kind": "broker_unavailable", "action": "waited and asked again", "ok": True}]
    assert ws.started == []


def test_a_broker_that_is_there_but_not_answering_properly_is_never_started_over(ws, monkeypatch):
    """A timeout, a permission error or a bad reply is not "not running": starting a second broker would not help."""
    ws.leases.unavailable_times = 1
    ws.leases.not_running = False
    monkeypatch.setattr(broker, "status", lambda socket=None, timeout=3.0: {"leases": 0})
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    assert rec["recovered"] == [{"kind": "broker_unavailable", "action": "waited and asked again", "ok": True}]
    assert ws.started == []


def test_a_subclass_of_a_known_failure_is_mended_too(ws, monkeypatch):
    class Slow(broker.BrokerUnavailable):
        pass

    real = ws.leases.request

    def request(*a, **kw):
        if not ws.leases.requested:
            ws.leases.requested.append(("anthropic", "run", "api.anthropic.com"))
            raise Slow("slow", not_running=True)
        return real(*a, **kw)

    monkeypatch.setattr(broker, "request", request)
    monkeypatch.setattr(run, "_start_broker", lambda sock: ws.started.append(sock) or True)
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    assert ws.records()[0]["recovered"][0]["kind"] == "broker_unavailable" and len(ws.started) == 1


def test_the_broker_client_says_when_nothing_is_running_and_when_it_is_something_else(request):
    import socket as pysocket
    import tempfile
    import threading

    short = Path(tempfile.mkdtemp(prefix="wsb", dir="/tmp"))  # a Unix socket path tops out near 100 characters
    request.addfinalizer(lambda: shutil.rmtree(short, ignore_errors=True))
    tmp_path = short
    missing = tmp_path / "none.sock"
    with pytest.raises(broker.BrokerUnavailable) as e:
        broker.status(missing)
    assert e.value.not_running is True  # no socket file

    stale = tmp_path / "stale.sock"
    s = pysocket.socket(pysocket.AF_UNIX)
    s.bind(str(stale))
    s.close()  # the file is left behind with nothing listening
    with pytest.raises(broker.BrokerUnavailable) as e:
        broker.status(stale)
    assert e.value.not_running is True

    live = tmp_path / "live.sock"
    srv = pysocket.socket(pysocket.AF_UNIX)
    srv.bind(str(live))
    srv.listen(4)
    replies = [b"not json\n", None]  # a bad reply, then silence

    def serve():
        for reply in replies:
            conn, _ = srv.accept()
            conn.recv(4096)
            if reply:
                conn.sendall(reply)
            else:
                threading.Event().wait(1.0)
            conn.close()

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    with pytest.raises(broker.BrokerUnavailable) as e:
        broker.status(live, timeout=2.0)
    assert e.value.not_running is False  # it answered, badly
    with pytest.raises(broker.BrokerUnavailable) as e:
        broker.status(live, timeout=0.2)
    assert e.value.not_running is False  # a timeout
    t.join(timeout=5)
    srv.close()


def test_a_refusal_is_not_a_known_failure(ws):
    ws.leases.refuse = broker.BrokerRefused("host_not_allowed", "no")
    ws.job()
    assert cli.main(["run", "daily"]) == 3
    assert ws.records()[0]["recovered"] == [] and ws.started == []


def test_the_table_is_keyed_by_exception_class_not_message():
    assert all(isinstance(k, type) and issubclass(k, Exception) for k in run.RECOVERIES)
    assert broker.BrokerUnavailable in run.RECOVERIES


# --- green levels ------------------------------------------------------------------------------------------------


def test_the_job_declares_what_its_verification_covers_and_the_record_says_so(ws):
    ws.job(extra='green = "package"')
    assert cli.main(["run", "daily"]) == 0
    assert valid(ws.records()[0])["green"] == "package"


def test_green_is_null_when_the_job_says_nothing_and_a_refusal_keeps_it(ws):
    ws.job()
    cli.main(["run", "daily"])
    assert valid(ws.records()[0])["green"] is None
    ws.cap(0.0001)
    ws.job(name="other", extra='green = "full"')
    cli.main(["run", "other"])
    assert valid(ws.records()[-1])["green"] == "full"


def test_a_bad_green_level_is_a_bad_job_with_a_record(ws, capsys):
    ws.job(extra='green = "great"')
    assert cli.main(["run", "daily"]) == 2
    assert "green must be one of targeted, package, full, merge_ready" in capsys.readouterr().err
    assert valid(ws.records()[0])["green"] is None


def test_the_schema_knows_the_levels_the_job_file_and_the_record_share():
    assert jobs.GREEN_LEVELS == runrecord.GREEN_LEVELS
    levels = runrecord.schema()["properties"]["green"]["enum"]
    assert levels == [*runrecord.GREEN_LEVELS, None]
    assert not VALIDATOR.is_valid({**runrecord.new(), "green": "great"})


def test_the_log_rotates_and_ws_runs_and_the_budget_read_across_the_files(ws, monkeypatch, capsys):
    from ws import jsonl

    ws.job()
    for _ in range(4):
        assert cli.main(["run", "daily"]) == 0
        capsys.readouterr()
    line = len(ws.log.read_text(encoding="utf-8").splitlines()[0]) + 1
    monkeypatch.setattr(jsonl, "DEFAULT_MAX_BYTES", line * 2 + 10)
    monkeypatch.setattr(jsonl, "DEFAULT_KEEP", 1)
    for _ in range(4):
        assert cli.main(["run", "daily"]) == 0
        capsys.readouterr()
    assert (ws.log.parent / "runs.jsonl.1").exists()
    assert len(ws.records()) == 8, "this month's records are kept past the limit, so the spend is never undercounted"
    assert cli.main(["--json", "runs", "--last", "8"]) == 0
    assert len(json.loads(capsys.readouterr().out)["data"]) == 8


# --- how a run can end -------------------------------------------------------------------------------------------


def test_a_timeout_kills_the_runner_and_still_releases_the_lease(ws):
    ws.job(prompt="sleep", timeout=1)
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "timeout" and rec["exit_code"] is None and "timeout" in rec["error"]
    assert rec["duration_ms"] < 20_000
    assert ws.leases.released == ["lease1"]


def test_a_failing_runner_is_an_error_with_its_exit_code(ws):
    ws.job(prompt="fail")
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and rec["exit_code"] == 2 and "boom" in rec["error"]
    assert ws.leases.released == ["lease1"]


def test_the_lease_url_never_reaches_the_record_or_the_saved_output(ws):
    ws.job(prompt="fail")  # the fake prints the base URL to stderr
    cli.main(["run", "daily"])
    (rec,) = ws.records()
    assert TOKEN not in json.dumps(rec) and "<lease>" in rec["error"]
    run_dir = ws.home / "runs" / rec["run_id"]
    assert TOKEN not in (run_dir / "stderr.txt").read_text() + (run_dir / "stdout.txt").read_text()


def test_output_that_is_not_a_result_is_an_error_with_nulls(ws):
    ws.job(prompt="garbage")
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and "no result" in rec["error"]
    assert rec["session_id"] is None and rec["cost_usd"] is None and rec["tokens"]["input"] is None


def test_a_runner_reported_error_keeps_the_numbers(ws):
    ws.job(prompt="max-turns")
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and "maximum number of turns" in rec["error"] and rec["tokens"]["input"] == 10


def test_a_missing_runner_is_an_error_record(ws, monkeypatch):
    monkeypatch.setenv("PATH", str(ws.home))
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and "not on PATH" in rec["error"]


def test_a_bad_job_exits_2_and_leaves_an_error_record(ws, capsys):
    (ws.home / "jobs" / "daily.toml").write_text('runner = "claude-code"\n', encoding="utf-8")
    assert cli.main(["run", "daily"]) == 2
    assert "model is required" in capsys.readouterr().err
    assert cli.main(["run", "nope"]) == 2
    ws.job(extra='cwd = "/does/not/exist"')
    assert cli.main(["run", "daily"]) == 2
    first, second, third = (valid(r) for r in ws.records())
    assert [r["job"] for r in (first, second, third)] == ["daily", "nope", "daily"]
    assert first["status"] == "error" and "model is required" in first["error"] and first["runner"] is None
    assert "no job 'nope'" in second["error"] and second["exit_code"] is None
    assert third["runner"] == "claude-code" and "not a folder" in third["error"]
    assert ws.leases.requested == []


def test_a_bad_job_name_that_is_not_safe_leaves_no_record_and_says_so(ws, capsys):
    assert cli.main(["run", "../etc/passwd"]) == 2
    err = capsys.readouterr().err
    assert "no record" in err and "not a safe job name" in err
    assert ws.records() == []


def test_a_dry_run_of_a_bad_job_writes_nothing(ws):
    assert cli.main(["run", "nope", "--dry-run"]) == 2
    assert not ws.log.exists()


def test_a_job_file_others_can_write_is_refused_with_a_record(ws, capsys):
    ws.job()
    (ws.home / "jobs" / "daily.toml").chmod(0o666)
    assert cli.main(["run", "daily"]) == 2
    assert "can be written by others" in capsys.readouterr().err
    assert "can be written by others" in valid(ws.records()[0])["error"]
    assert not (ws.home / "fake-saw.json").exists()


def test_an_unknown_runner_option_exits_2(ws, capsys):
    ws.job()
    p = ws.home / "jobs" / "daily.toml"
    p.write_text(p.read_text(encoding="utf-8") + "[runner_options]\nturbo = true\n", encoding="utf-8")
    assert cli.main(["run", "daily"]) == 2
    assert "runner_options.turbo" in capsys.readouterr().err
    assert "runner_options.turbo" in valid(ws.records()[0])["error"]


def test_the_default_cwd_is_outside_ws_home_so_the_wall_leaves_it_writable(ws, tmp_path, monkeypatch):
    """The wall makes WS_HOME read-only; a job with no cwd must still get a folder it can write in."""
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    cwd = Path(ws.records()[0]["cwd"])
    assert cwd.is_dir() and stat.S_IMODE(cwd.stat().st_mode) == 0o700
    assert not cwd.is_relative_to(ws.home)
    assert Path(ws.saw()["cwd"]).resolve() == cwd.resolve()
    # the gate's protected folders are still where they were: the run folder and the log stay under WS_HOME
    assert (ws.home / "runs").is_dir() and ws.log.is_file()
    # XDG_STATE_HOME moves it
    state = tmp_path / "state with space"
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    assert cli.main(["run", "daily"]) == 0
    assert Path(ws.records()[1]["cwd"]) == state / "ws" / "work" / ws.records()[1]["run_id"]


def test_a_ws_home_that_holds_the_state_folder_falls_back_to_the_temp_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path / "tmp"))
    home = tmp_path / "h" / ".local" / "state" / "ws"
    got = run.default_cwd(home, "r1")
    assert not got.is_relative_to(home) and got.name == "r1" and got.is_relative_to(tmp_path / "tmp")


def test_a_job_with_its_own_cwd_runs_there(ws, tmp_path):
    work = tmp_path / "mywork"
    work.mkdir()
    ws.job(extra=f'cwd = "{work}"')
    assert cli.main(["run", "daily"]) == 0
    assert Path(ws.saw()["cwd"]).resolve() == work.resolve()
    assert ws.records()[0]["cwd"] == str(work)


# --- dry run, ws runs ----------------------------------------------------------------------------------------------


def test_dry_run_shows_the_plan_and_starts_nothing(ws, capsys):
    ws.job()
    assert cli.main(["run", "daily", "--dry-run"]) == 0
    plan = json.loads(capsys.readouterr().out)
    assert plan["would_refuse"] is None and plan["runner"] == "claude-code" and "--bare" in plan["argv"]
    assert "ANTHROPIC_API_KEY" in plan["env"] and URL not in json.dumps(plan)
    assert not ws.log.exists() and not (ws.home / "runs").exists() and ws.leases.requested == []


def test_dry_run_says_when_it_would_refuse_and_writes_nothing(ws, capsys):
    ws.job(extra='sources = ["mail", "notes"]')
    assert cli.main(["run", "daily", "--dry-run"]) == 3
    assert "Rule of Two" in json.loads(capsys.readouterr().out)["would_refuse"]
    assert not ws.log.exists()


def test_ws_runs_shows_the_last_records(ws, capsys):
    ws.job()
    for _ in range(3):
        cli.main(["run", "daily"])
    capsys.readouterr()
    assert cli.main(["runs", "--last", "1", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)["data"]
    assert len(shown) == 1 and shown[0] == ws.records()[-1]
    assert cli.main(["runs", "--last", "2"]) == 0
    lines = capsys.readouterr().out.strip().splitlines()
    assert len(lines) == 2 and "daily" in lines[0] and "20/10 tok" in lines[0]


def test_ws_runs_with_no_log_says_so(ws, capsys):
    assert cli.main(["runs"]) == 0
    assert "no runs yet" in capsys.readouterr().out
    assert cli.main(["runs", "--last", "0"]) == 2
    assert "must be 1 or more" in capsys.readouterr().err


# --- the record itself ----------------------------------------------------------------------------------------------


def test_appends_from_many_threads_never_interleave(tmp_path):
    log = tmp_path / "logs" / "runs.jsonl"
    rec = runrecord.new(run_id="x", job="j", runner="r", started_at="t", ended_at="t", status="ok", ws_version="0")
    rec["error"] = "é" * 500  # long enough to be more than one buffer, and not ASCII-only
    threads = [threading.Thread(target=lambda: [runrecord.append(log, rec) for _ in range(25)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(runrecord.read(log)) == 200 and len(log.read_text().splitlines()) == 200
    assert stat.S_IMODE(log.stat().st_mode) == 0o600 and stat.S_IMODE(log.parent.stat().st_mode) == 0o700


def test_a_torn_line_is_skipped_and_a_partial_record_is_refused(tmp_path):
    log = tmp_path / "runs.jsonl"
    log.write_text('{"run_id": 1}\nnot json\n[1]\n', encoding="utf-8")
    assert runrecord.read(log) == [{"run_id": 1}]
    with pytest.raises(ValueError, match="missing"):
        runrecord.append(log, {"run_id": 1})
    with pytest.raises(KeyError):
        runrecord.new(colour="red")


def test_the_schema_rejects_a_missing_key_an_extra_key_and_a_bad_status(ws):
    ws.job()
    cli.main(["run", "daily"])
    rec = ws.records()[0]
    for broken in (
        {k: v for k, v in rec.items() if k != "cost_usd"},
        {**rec, "extra": 1},
        {**rec, "status": "great"},
        {**rec, "tokens": {"input": 1}},
        {**rec, "policy": {"dial": "prepare", "rule_of_two": "maybe"}},
        {**rec, "run_id": "not-sortable"},
    ):
        assert not VALIDATOR.is_valid(broken)
    assert VALIDATOR.is_valid(rec)


def test_run_ids_sort_by_start_time():
    a = runrecord.new_run_id(datetime(2026, 1, 2, tzinfo=UTC))
    b = runrecord.new_run_id(datetime(2026, 1, 10, tzinfo=UTC))
    assert a < b and runrecord.new_run_id() != runrecord.new_run_id()


# --- the adapters' half --------------------------------------------------------------------------------------------


def test_claude_parse_reads_a_captured_result():
    out = (FIXTURES / "claude-result-ok.json").read_text(encoding="utf-8")
    p = claude_code.parse_result(out, "", 0)
    assert p["tokens"] == {"input": 20, "output": 10, "cache_creation": 0, "cache_read": 0}
    assert p["models_used"] == [
        {
            "model": "claude-sonnet-4-5",
            "input": 20,
            "output": 10,
            "cache_creation": 0,
            "cache_read": 0,
            "cost_usd": 0.00021,
        }
    ]
    assert (p["cost_usd"], p["cost_basis"], p["num_turns"], p["duration_api_ms"], p["error"]) == (
        0.00021,
        "list",
        2,
        82,
        None,
    )


def test_claude_parse_never_raises():
    for junk in ("", "[]", "{", '{"type": "result", "usage": 3, "modelUsage": []}', '{"type": "other"}'):
        assert isinstance(claude_code.parse_result(junk, "stderr tail", 1), dict)


def test_claude_transcript_slug_rule(tmp_path):
    cwd = "/tmp/a_b.c d/é"
    folder = tmp_path / "cfg" / "projects" / "-tmp-a-b-c-d--"
    folder.mkdir(parents=True)
    (folder / "abc-123.jsonl").write_text("{}", encoding="utf-8")
    env = {"CLAUDE_CONFIG_DIR": str(tmp_path / "cfg"), "HOME": "/nowhere"}
    assert claude_code.transcript_path("abc-123", cwd, env) == str(folder / "abc-123.jsonl")
    assert claude_code.transcript_path("abc-123", "/elsewhere", env) == str(folder / "abc-123.jsonl")  # found by id
    assert claude_code.transcript_path("nope", cwd, env) is None
    assert claude_code.transcript_path(None, cwd, env) is None
    assert claude_code.transcript_path("../x", cwd, env) is None
    assert claude_code.transcript_path("abc-123", cwd, {"HOME": str(tmp_path)}) is None  # default ~/.claude: empty


def test_opencode_parse_sums_the_steps():
    out = (FIXTURES / "opencode-events-ok.jsonl").read_text(encoding="utf-8")
    p = opencode.parse_result(out, "", 0)
    assert p["tokens"] == {"input": 20, "output": 10, "cache_creation": 0, "cache_read": 0}
    assert p["session_id"].startswith("ses_") and p["num_turns"] == 2 and p["error"] is None
    # the stub has no price, so OpenCode says 0; that is "unknown", and must not count as free
    assert p["cost_usd"] is None and p["cost_basis"] == "unpriced"
    assert p["models_used"][0]["model"] is None and p["models_used"][0]["input"] == 20


def test_opencode_parse_prices_a_priced_model_and_reads_errors():
    step = {
        "type": "step_finish",
        "sessionID": "ses_x",
        "part": {"tokens": {"input": 5, "output": 1, "cache": {"write": 2, "read": 3}}, "cost": 0.5},
    }
    p = opencode.parse_result(json.dumps(step) + "\n" + json.dumps(step) + "\nnot json\n", "", 0)
    assert p["cost_usd"] == 1.0 and p["cost_basis"] == "runner" and p["tokens"]["cache_read"] == 6
    err = opencode.parse_result((FIXTURES / "opencode-events-error.jsonl").read_text(encoding="utf-8"), "", 1)
    assert "Cannot connect" in err["error"] and err["session_id"].startswith("ses_")
    assert err["tokens"] == {"input": None, "output": None, "cache_creation": None, "cache_read": None}
    assert err["models_used"] == [] and err["cost_usd"] is None
    for junk in ("", "[]", "{", '{"type": "step_finish", "part": 1}'):
        assert isinstance(opencode.parse_result(junk, "", 1), dict)


def test_opencode_transcript_is_the_session_database(tmp_path):
    db = tmp_path / "data" / "opencode" / "opencode.db"
    db.parent.mkdir(parents=True)
    db.write_text("", encoding="utf-8")
    assert opencode.transcript_path("ses_x", "/w", {"XDG_DATA_HOME": str(tmp_path / "data")}) == str(db)
    assert opencode.transcript_path("ses_x", "/w", {"HOME": str(tmp_path / "other")}) is None
    assert opencode.transcript_path(None, "/w", {"XDG_DATA_HOME": str(tmp_path / "data")}) is None


def job_for(runner: str, model: str, extra: str = "") -> jobs.Job:
    return jobs.loads("j", f'runner = "{runner}"\nmodel = "{model}"\nprompt = "-hello"\n{extra}', Path("."))


def test_opencode_build_command(tmp_path):
    lease = broker.Lease("l1", URL, "authorization", "")
    (tmp_path / "gate").mkdir()
    (tmp_path / "gate" / "opencode.json").write_text('{"permission": {"bash": {"*": "allow"}}}', encoding="utf-8")
    j = job_for(
        "opencode", "stub/m", '[runner_options]\nbase_path = "/v2"\nprovider = { npm = "@ai-sdk/openai-compatible" }\n'
    )
    argv, env = opencode.build_command(j, tmp_path, lease)
    assert argv == ["opencode", "run", "--model", "stub/m", "--format", "json"]  # the prompt goes in on stdin
    assert opencode.PROMPT_VIA_STDIN and "OPENCODE_DISABLE_MODELS_FETCH" not in env
    assert "OPENCODE_CONFIG" not in env and env["OPENCODE_CONFIG_DIR"] == str(tmp_path / "gate")
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert content["permission"] == {"bash": {"*": "allow"}}  # the gate config is the inline config
    block = content["provider"]["stub"]
    assert block["npm"] == "@ai-sdk/openai-compatible"
    assert block["options"] == {"baseURL": URL + "/v2", "apiKey": opencode.PLACEHOLDER_KEY}
    assert j.runner_options["provider"] == {"npm": "@ai-sdk/openai-compatible"}  # the job's own table is untouched
    _, plain = opencode.build_command(job_for("opencode", "anthropic/x"), tmp_path / "other", None)
    assert json.loads(plain["OPENCODE_CONFIG_CONTENT"]) == {}  # no gate file yet and no provider block: just empty


@pytest.mark.parametrize(
    ("runner", "model", "extra", "names"),
    [
        ("opencode", "no-slash", "", "provider/model"),
        ("opencode", "a/b", "max_turns = 3\n", "max_turns"),
        ("opencode", "a/b", "[runner_options]\nturbo = 1\n", "runner_options.turbo"),
        ("opencode", "a/b", "[runner_options]\nprovider = 3\n", "runner_options.provider"),
        ("claude-code", "m", '[runner_options]\nallowed_tools = "Bash"\n', "runner_options.allowed_tools"),
        ("claude-code", "m", "[runner_options]\nenv = { A = 1 }\n", "runner_options.env"),
    ],
)
def test_build_command_names_the_option_that_is_wrong(tmp_path, runner, model, extra, names):
    mod = {"opencode": opencode, "claude-code": claude_code}[runner]
    with pytest.raises(ValueError, match=names):
        mod.build_command(job_for(runner, model, extra), tmp_path, None)


def test_claude_build_command_keeps_the_prompt_out_of_argv(tmp_path):
    argv, env = claude_code.build_command(job_for("claude-code", "m"), tmp_path, None)
    assert "-hello" not in argv and claude_code.PROMPT_VIA_STDIN and "ANTHROPIC_API_KEY" not in env


def test_what_ws_sets_is_applied_after_the_jobs_env(tmp_path, monkeypatch):
    """extra_env refuses the names, but the order is the second lock: ws's own values are written last."""
    lease = broker.Lease("l1", URL, "x-api-key", "")
    sneaky = {
        "ANTHROPIC_BASE_URL": "http://elsewhere.invalid",
        "OPENCODE_CONFIG_CONTENT": '{"permission": "allow"}',
        "MY_FLAG": "1",
    }
    monkeypatch.setattr(jobs, "extra_env", lambda job: dict(sneaky))
    _, env = claude_code.build_command(job_for("claude-code", "m"), tmp_path, lease)
    assert env["ANTHROPIC_BASE_URL"] == URL and env["MY_FLAG"] == "1"
    _, env = opencode.build_command(job_for("opencode", "a/b"), tmp_path, lease)
    content = json.loads(env["OPENCODE_CONFIG_CONTENT"])
    assert "permission" not in content and content["provider"]["a"]["options"]["baseURL"] == URL + "/v1"


# --- doctor WS101 ---------------------------------------------------------------------------------------------------


def linux() -> osinfo.OSInfo:
    return osinfo.OSInfo(kind="linux", release="x", arch="x86_64", scheduler="systemd", supported=True)


def test_doctor_reports_the_runner_version(ws):
    status, detail = doctor._runner({"adapter": "claude-code"}, linux(), dict(os.environ), False)
    assert status == "ok" and "claude-code 9.9.9" in detail


def test_doctor_fails_a_runner_that_will_not_say_its_version(ws):
    (ws.bin / "claude").write_text(f"#!{sys.executable}\nimport sys\nsys.exit(3)\n", encoding="utf-8")
    status, detail = doctor._runner({"adapter": "claude-code"}, linux(), dict(os.environ), False)
    assert status == "fail" and "--version" in detail
    status, detail = doctor._runner({"adapter": "opencode"}, linux(), {"PATH": str(ws.home)}, False)
    assert status == "fail" and "not on PATH" in detail


def test_exe_version_gives_up_after_its_timeout(ws):
    (ws.bin / "claude").write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    assert run.exe_version(str(ws.bin / "claude"), dict(os.environ), timeout=0.5) is None


# --- killing, signals, output ----------------------------------------------------------------------------------------


def alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except OSError:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True
    return state != "Z"  # a zombie is dead; it only waits to be reaped


def wait_for(path: Path, seconds: float = 20.0) -> None:
    end = time.monotonic() + seconds
    while not path.exists():
        assert time.monotonic() < end, f"{path.name} never appeared"
        time.sleep(0.05)


def test_a_child_that_ignores_sigterm_does_not_survive_the_timeout(ws, monkeypatch, tmp_path):
    """The runner and its child both ignore SIGTERM: after the grace, the whole group gets SIGKILL."""
    monkeypatch.setattr(run, "TERM_GRACE_S", 1.0)
    prompt, out = tmp_path / "prompt.txt", tmp_path / "out"
    prompt.write_text("stubborn", encoding="utf-8")
    out.mkdir()
    env = {**os.environ, "WS_HOME": str(ws.home)}
    t0 = time.monotonic()
    code, timed_out, truncated = run._spawn([str(ws.bin / "claude")], env, tmp_path, 1.5, prompt, out)
    elapsed = time.monotonic() - t0
    assert (code, timed_out, truncated) == (None, True, False)
    assert elapsed < 1.5 + 1.0 + 1.0, f"took {elapsed:.1f} s"
    assert not alive(int((ws.home / "grandchild.pid").read_text())), "the grandchild survived"
    assert not alive(ws.saw()["pid"])


ORPHAN = f"""#!{sys.executable}
import subprocess, sys, time
sys.stdin.read()
child = "import os, time\\nopen(os.environ['WS_HOME'] + '/orphan.pid', 'w').write(str(os.getpid()))\\ntime.sleep(60)"
subprocess.Popen([sys.executable, "-c", child])
time.sleep(0.5)
"""


def test_a_grandchild_left_behind_by_a_runner_that_exits_is_killed_too(ws, tmp_path):
    (ws.bin / "claude").write_text(ORPHAN, encoding="utf-8")
    out = tmp_path / "out"
    out.mkdir()
    env = {**os.environ, "WS_HOME": str(ws.home)}
    t0 = time.monotonic()
    code, timed_out, _ = run._spawn([str(ws.bin / "claude")], env, tmp_path, 20, None, out)
    assert code == 0 and not timed_out and time.monotonic() - t0 < 10  # not held up by the orphan holding the pipes
    assert not alive(int((ws.home / "orphan.pid").read_text()))


def test_ctrl_c_kills_the_runner_writes_the_record_and_releases_the_lease(ws, capsys):
    ws.job(prompt="sleep", timeout=60)

    def interrupt():
        wait_for(ws.home / "fake-saw.json")
        _thread.interrupt_main()

    threading.Thread(target=interrupt, daemon=True).start()
    with pytest.raises(KeyboardInterrupt):
        cli.main(["run", "daily"])
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and rec["error"].startswith("interrupted") and rec["ended_at"]
    assert ws.leases.released == ["lease1"]
    assert not alive(ws.saw()["pid"])
    assert "interrupted" in capsys.readouterr().err


def test_sigterm_to_ws_run_kills_the_runner_and_still_writes_the_record(ws):
    (ws.home / "jobs" / "nosecret.toml").write_text(
        'runner = "claude-code"\nmodel = "m"\nprompt = "sleep"\ntimeout_s = 60\n', encoding="utf-8"
    )
    proc = subprocess.Popen(
        [sys.executable, "-m", "ws", "run", "nosecret"], env=dict(os.environ), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True,
    )  # fmt: skip
    try:
        wait_for(ws.home / "fake-saw.json")
        proc.send_signal(signal.SIGTERM)
        proc.communicate(timeout=30)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
    assert proc.returncode == 128 + signal.SIGTERM
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "error" and rec["error"].startswith("interrupted") and rec["job"] == "nosecret"
    assert not alive(ws.saw()["pid"])


def test_output_over_the_cap_is_cut_and_flagged(ws, monkeypatch):
    monkeypatch.setattr(run, "OUTPUT_CAP", 100_000)
    ws.job(prompt="flood")
    assert cli.main(["run", "daily"]) == 1  # a cut-off result is not a result
    (rec,) = ws.records()
    valid(rec)
    assert rec["output_truncated"] is True
    assert (ws.home / "runs" / rec["run_id"] / "stdout.txt").stat().st_size == 100_000


def test_a_normal_run_is_not_flagged_truncated_and_keeps_its_output_in_files(ws):
    ws.job()
    cli.main(["run", "daily"])
    rec = ws.records()[0]
    assert rec["output_truncated"] is False
    f = ws.home / "runs" / rec["run_id"] / "stdout.txt"
    assert json.loads(f.read_text())["type"] == "result" and stat.S_IMODE(f.stat().st_mode) == 0o600


# --- the budget -----------------------------------------------------------------------------------------------------


def seed_unpriced(
    ws, *, tokens=None, status="ok", exit_code=0, runner="claude-code", model="claude-sonnet-4-5", error=None
):
    now = datetime.now(UTC)
    runrecord.append(
        ws.log,
        runrecord.new(
            run_id=runrecord.new_run_id(now), job="old", runner=runner, model_requested=model,
            started_at=runrecord.iso(now), ended_at=runrecord.iso(now), status=status, exit_code=exit_code,
            error=error, tokens={**runrecord.blank_tokens(), **(tokens or {})}, ws_version="0",
        ),
    )  # fmt: skip


def budget(ws, **kw) -> None:
    (ws.home / "profile.toml").write_text(dumps({"roles": {"budget": {"monthly_cap_usd": 10, **kw}}}), encoding="utf-8")


def test_with_a_fallback_price_a_run_with_no_cost_counts_its_tokens_at_that_rate(ws, capsys):
    budget(ws, fallback_usd_per_mtok=12.5)
    seed_unpriced(ws, tokens={"input": 600_000, "output": 200_000})  # 800k tokens x US$12.5 per 1M = US$10 = the cap
    ws.job()
    assert cli.main(["run", "daily"]) == 3
    assert "at the monthly cap of US$10" in capsys.readouterr().err


def test_a_little_under_the_fallback_price_still_runs(ws):
    budget(ws, fallback_usd_per_mtok=12.5)
    seed_unpriced(ws, tokens={"input": 700_000})  # US$8.75
    ws.job()
    assert cli.main(["run", "daily"]) == 0


def test_without_a_fallback_price_a_run_with_no_cost_counts_the_flat_amount_whatever_its_tokens(ws, capsys):
    budget(ws, unknown_run_usd=4)
    seed_unpriced(ws, tokens={"input": 1_000_000_000})  # a huge token count is not priced: no per-token rate is set
    seed_unpriced(ws, tokens={"input": 1})
    seed_unpriced(ws)  # 3 x US$4 = 12 >= 10
    ws.job()
    assert cli.main(["run", "daily"]) == 3
    assert "at the monthly cap" in capsys.readouterr().err


def test_the_flat_amount_defaults_to_one_dollar(ws):
    budget(ws)
    for _ in range(9):
        seed_unpriced(ws)  # 9 x US$1 = 9 < 10
    ws.job()
    assert cli.main(["run", "daily"]) == 0


def test_the_fallback_figures_must_be_positive_numbers(ws):
    prof = {"roles": {"budget": {"monthly_cap_usd": 5, "fallback_usd_per_mtok": -1, "unknown_run_usd": "lots"}}}
    assert run._budget(prof) == (5.0, None, None)
    assert run._budget({}) == (None, None, None)


def test_there_is_no_price_table_in_ws():
    assert not [n for n in dir(runrecord) if "PRICE" in n.upper()]


def test_a_reported_cost_is_used_as_it_is_and_a_non_finite_one_is_not():
    base = {"status": "ok", "exit_code": 0, "tokens": {"input": 1_000_000, "output": None}}
    assert runrecord.record_cost({**base, "cost_usd": 0.25}) == 0.25
    for bad in (float("nan"), float("inf"), -1, True, "3"):
        assert runrecord.record_cost({**base, "cost_usd": bad}, fallback_usd_per_mtok=7) == pytest.approx(7.0)
        assert runrecord.record_cost({**base, "cost_usd": bad}) == 1.0


def test_record_cost_counts_all_four_token_kinds_at_the_fallback_rate():
    rec = runrecord.new(
        status="ok", exit_code=0, tokens={"input": 1, "output": 2, "cache_creation": 3, "cache_read": 4}
    )
    assert runrecord.record_cost(rec, fallback_usd_per_mtok=1_000_000) == 10.0


def test_a_run_that_started_but_reported_nothing_counts_a_flat_amount():
    ok = runrecord.new(status="ok", exit_code=0)
    assert runrecord.record_cost(ok) == runrecord.UNKNOWN_RUN_USD == 1.0
    assert runrecord.record_cost(ok, unknown_run_usd=4) == 4.0
    assert runrecord.record_cost(ok, unknown_run_usd=4, fallback_usd_per_mtok=9) == 4.0  # no tokens to price
    for started in (
        runrecord.new(status="timeout"),
        runrecord.new(status="error", exit_code=2),
        runrecord.new(status="error", error="interrupted before the runner finished"),
    ):
        assert runrecord.record_cost(started) == 1.0
    for spent_nothing in (
        runrecord.new(status="refused"),
        runrecord.new(status="error", error="claude-code: `claude` is not on PATH"),
        runrecord.new(status="error", error="no broker answering"),
    ):
        assert runrecord.record_cost(spent_nothing, fallback_usd_per_mtok=9) == 0.0


def write_opencode_job(ws, top="", options=""):
    (ws.home / "jobs" / "oc.toml").write_text(
        f'runner = "opencode"\nmodel = "stub/m"\nprompt = "p"\n{top}\n[runner_options]\n{options}\n', encoding="utf-8"
    )


def test_a_runner_that_cannot_price_the_model_is_refused_unless_the_job_allows_it(ws, capsys):
    write_opencode_job(ws, options='provider = { npm = "@ai-sdk/openai-compatible", models = { m = { name = "m" } } }')
    assert cli.main(["run", "oc"]) == 3
    assert "allow_unpriced" in capsys.readouterr().err
    (rec,) = ws.records()
    valid(rec)
    assert rec["status"] == "refused" and rec["allow_unpriced"] is False
    write_opencode_job(
        ws,
        top="allow_unpriced = true",
        options='provider = { npm = "@ai-sdk/openai-compatible", models = { m = { name = "m" } } }',
    )
    assert cli.main(["run", "oc", "--dry-run"]) == 0


def test_a_provider_block_whose_models_carry_a_cost_is_priced(ws):
    write_opencode_job(ws, options='provider = { models = { m = { name = "m", cost = { input = 1, output = 2 } } } }')
    assert cli.main(["run", "oc", "--dry-run"]) == 0
    write_opencode_job(ws)  # no provider block: one of OpenCode's own, which come with prices
    assert cli.main(["run", "oc", "--dry-run"]) == 0


def test_allow_unpriced_is_recorded(ws):
    ws.job(extra="allow_unpriced = true")
    assert cli.main(["run", "daily"]) == 0
    assert valid(ws.records()[0])["allow_unpriced"] is True


def test_the_help_says_the_cap_is_checked_before_a_run_not_during_it(capsys):
    with pytest.raises(SystemExit):
        cli.main(["run", "--help"])
    assert "checked before the run, not enforced during it" in " ".join(capsys.readouterr().out.split())


# --- the log --------------------------------------------------------------------------------------------------------


def test_an_unreadable_log_is_a_clean_error_not_a_traceback(ws, capsys):
    ws.log.mkdir(parents=True)  # a folder where the file should be
    assert cli.main(["runs"]) == 1
    assert "cannot read" in capsys.readouterr().err
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    assert "cannot read" in capsys.readouterr().err and not (ws.home / "fake-saw.json").exists()


def test_a_log_that_cannot_be_written_is_said_and_the_exit_is_not_ok(ws, capsys):
    (ws.home / "logs").write_text("in the way", encoding="utf-8")
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    assert "could not write the record" in capsys.readouterr().err
    assert (ws.home / "fake-saw.json").exists() and ws.leases.released == ["lease1"]


def test_an_existing_log_with_a_wider_mode_is_closed_up(tmp_path):
    log = tmp_path / "runs.jsonl"
    log.write_text("", encoding="utf-8")
    log.chmod(0o644)
    runrecord.append(
        log, runrecord.new(run_id="x", job="j", runner="r", started_at="t", ended_at="t", status="ok", ws_version="0")
    )
    assert stat.S_IMODE(log.stat().st_mode) == 0o600


def test_non_finite_numbers_from_a_runner_are_not_believed():
    nan = '{"type": "result", "total_cost_usd": NaN, "usage": {"input_tokens": Infinity}, "modelUsage": {"m": {"costUSD": Infinity}}}'
    p = claude_code.parse_result(nan, "", 0)
    assert p["cost_usd"] is None and p["tokens"]["input"] is None and p["models_used"][0]["cost_usd"] is None
    step = {"type": "step_finish", "sessionID": "ses_x", "part": {"tokens": {"input": 1}, "cost": float("inf")}}
    q = opencode.parse_result(json.dumps(step), "", 0)
    assert q["cost_usd"] is None and q["cost_basis"] == "unpriced"


def test_ws_secret_wall_can_still_copy_the_package():
    """The wall snapshots ws as .py files only, so the run-record schema is a Python dict, not a .json file."""
    from ws import secrets as wsecrets

    pkg = Path(run.__file__).parent
    assert wsecrets.tree_digest(pkg)  # raises SecretsError on any file that is not .py
    assert not [p for p in pkg.rglob("*") if p.is_file() and p.suffix != ".py" and "__pycache__" not in p.parts]


def test_the_schema_as_json_is_a_valid_schema_and_matches():
    text = runrecord.schema_json()
    jsonschema.Draft202012Validator.check_schema(json.loads(text))
    assert json.loads(text) == runrecord.schema()


# --- a lock held to stall the log must not hide spend ------------------------------------------------------------

HOLD = (
    "import fcntl, os, sys\n"
    "os.makedirs(os.path.dirname(sys.argv[1]), exist_ok=True)\n"
    "fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n"
    "fcntl.flock(fd, fcntl.LOCK_SH if sys.argv[2] == 'shared' else fcntl.LOCK_EX)\n"
    "print('held', flush=True)\n"
    "sys.stdin.read()\n"
)


@pytest.fixture
def lock_held(ws, monkeypatch):
    """Another process (as a session's background process could) holds the run log's lock until the test ends."""
    from ws import jsonl

    monkeypatch.setattr(jsonl, "LOCK_TIMEOUT_S", 0.3)
    procs = []

    def hold(mode: str) -> None:
        lock = str(ws.log.parent / "runs.jsonl.lock")
        p = subprocess.Popen(
            [sys.executable, "-I", "-c", HOLD, lock, mode],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True,
        )  # fmt: skip
        procs.append(p)
        assert p.stdout.readline().strip() == "held"

    yield hold
    for p in procs:
        p.stdin.close()
        p.wait(timeout=30)
        p.stdout.close()


@pytest.mark.parametrize("mode", ["shared", "exclusive"])
def test_a_lock_held_by_another_process_refuses_the_run_and_the_refusal_is_still_recorded(ws, lock_held, capsys, mode):
    ws.job()
    lock_held(mode)
    assert cli.main(["run", "daily"]) == 3
    err = capsys.readouterr().err
    lock = str(ws.log.parent / "runs.jsonl.lock")
    assert "holding the lock" in err and lock in err and "fuser" in err and "lsof" in err
    assert not (ws.home / "fake-saw.json").exists(), "the runner started although the run could not be recorded"
    assert not ws.log.exists() or ws.log.read_text() == ""  # nothing could go into the locked log
    fb = runrecord.fallback_path(ws.log)
    (rec,) = [json.loads(line) for line in fb.read_text().splitlines()]
    assert valid(rec)["status"] == "refused" and "lock" in rec["error"]
    if mode == "shared":  # an exclusive holder would stall this read too, which is why the run was refused
        assert [r["run_id"] for r in ws.records()] == [rec["run_id"]]  # read() includes the fallback


def test_a_dry_run_with_the_lock_held_says_it_would_refuse_and_writes_nothing(ws, lock_held, capsys):
    ws.job()
    lock_held("shared")
    assert cli.main(["run", "daily", "--dry-run"]) == 3
    assert "holding the lock" in json.loads(capsys.readouterr().out)["would_refuse"]
    assert not runrecord.fallback_path(ws.log).exists()


def test_a_probe_with_no_lock_file_yet_writes_nothing(ws, capsys):
    ws.job()
    assert cli.main(["run", "daily", "--dry-run"]) == 0
    assert not (ws.home / "logs").exists()


def test_a_record_that_cannot_be_written_goes_to_the_fallback_and_still_counts_towards_the_cap(ws, monkeypatch, capsys):
    from ws import jsonl

    real = jsonl.append

    def broken(*a, **kw):
        raise jsonl.LockTimeout("runs.jsonl.lock has been locked for 10 s by another process")

    ws.job()
    monkeypatch.setattr(jsonl, "append", broken)
    assert cli.main(["run", "daily"]) == 0  # the run itself was fine, and its record is safe
    err = capsys.readouterr().err
    assert "runs.fallback.jsonl" in err
    (rec,) = [json.loads(line) for line in runrecord.fallback_path(ws.log).read_text().splitlines()]
    assert valid(rec)["status"] == "ok" and rec["cost_usd"] == 0.00021
    monkeypatch.setattr(jsonl, "append", real)
    assert not ws.log.exists()
    spend = runrecord.month_spend(ws.records(), datetime.now(UTC))
    assert spend == pytest.approx(0.00021)
    ws.cap(0.0001)  # below what that one run cost: the next run must be refused on the strength of the fallback alone
    assert cli.main(["run", "daily"]) == 3
    assert "monthly cap" in capsys.readouterr().err


def test_ws_runs_says_when_records_are_waiting_in_the_fallback(ws, monkeypatch, capsys):
    from ws import jsonl

    ws.job()
    monkeypatch.setattr(jsonl, "append", lambda *a, **kw: (_ for _ in ()).throw(OSError("full")))
    assert cli.main(["run", "daily"]) == 0
    capsys.readouterr()
    assert cli.main(["runs"]) == 0
    out = capsys.readouterr()
    assert "1 record(s) are in" in out.err and "runs.fallback.jsonl" in out.err
    assert "daily" in out.out


def test_both_the_log_and_the_fallback_failing_is_a_failed_run_with_a_message(ws, monkeypatch, capsys):
    from ws import jsonl

    def boom(*a, **kw):
        raise OSError("disk full")

    ws.job()
    monkeypatch.setattr(jsonl, "append", boom)
    monkeypatch.setattr(jsonl, "append_plain", boom)
    assert cli.main(["run", "daily"]) == 1
    assert "could not write the record" in capsys.readouterr().err


def test_a_socket_path_that_is_too_long_is_its_own_typed_case_with_a_hint(tmp_path, capsys):
    long = tmp_path / ("d" * 120) / "broker.sock"
    with pytest.raises(broker.SocketPathTooLong) as e:
        broker.status(long)
    assert isinstance(e.value, broker.BrokerUnavailable) and e.value.not_running is False
    assert str(broker.UNIX_PATH_MAX - 1) in str(e.value) and "WS_BROKER_SOCKET" in e.value.hint
    with pytest.raises(broker.SocketPathTooLong):
        broker.Broker(long, tmp_path / "secrets.toml", None)._bind_unix()
    assert cli.main(["broker", "status", "--socket", str(long)]) == 1
    err = capsys.readouterr().err
    assert "limit is" in err and "shorter WS_HOME" in err


def test_a_run_whose_socket_path_is_too_long_ends_with_the_hint_and_tries_no_recovery(ws, monkeypatch, tmp_path):
    monkeypatch.setenv("WS_BROKER_SOCKET", str(tmp_path / ("d" * 120) / "b.sock"))
    monkeypatch.setattr(broker, "request", lambda *a, **kw: broker.check_socket_path(os.environ["WS_BROKER_SOCKET"]))
    ws.job()
    assert cli.main(["run", "daily"]) == 1
    (rec,) = ws.records()
    assert rec["recovered"] == [] and "shorter WS_HOME" in rec["error"] and ws.started == []
