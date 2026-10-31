"""PLAN section 6, row 3 pass test: a job's run record is complete against a schema, on both runners, Tier 0 (a stub
model on 127.0.0.1, no paid provider).

`ws run` starts the real Claude Code (bare) and the real OpenCode. Each is pointed, through a real broker lease, at a
stub upstream that stands in for the provider; the key the runner holds is a placeholder and the stub sees the planted
real one, injected by the broker. Then the record in runs.jsonl must validate against the shipped JSON Schema, and every
field the runner can supply must be there (not null). A field a runner cannot supply is null, and the test says which.

A missing runner skips the test, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import threading
from pathlib import Path

import jsonschema
import pytest

from e2e.stubs import AnthropicStub, Call, OpenAIStub
from e2e.test_gate_runners import BLOCKED, denied_command, runner_bin
from ws import broker, cli, runrecord
from ws import secrets as wsecrets

VALUE = "planted-" + os.urandom(12).hex()
VALIDATOR = jsonschema.Draft202012Validator(runrecord.schema())


class Recording:
    """Mixin for the stubs: remember the credential header of every model request."""

    def respond(self, handler, body):
        self.keys.append(handler.headers.get("x-api-key") or handler.headers.get("authorization"))
        super().respond(handler, body)


class SeenAnthropic(Recording, AnthropicStub):
    def __init__(self, calls):
        super().__init__(calls)
        self.keys: list[str | None] = []


class SeenOpenAI(Recording, OpenAIStub):
    def __init__(self, calls):
        super().__init__(calls)
        self.keys: list[str | None] = []


class Rig:
    def __init__(self, base: Path):
        self.base = base
        self.home = base / "h"
        self.user = base / "user"
        self.sock = base / "run" / "b.sock"
        self.log = self.home / "logs" / "runs.jsonl"
        self.broker: broker.Broker | None = None

    def start(self, upstream_host: str, secret: str, header: str) -> None:
        """A registry that lets `secret` go to the stub only, the planted value in the store, and a running broker."""
        self.home.mkdir(parents=True)
        self.user.mkdir()
        (self.home / "jobs").mkdir()
        (self.home / "profile.toml").write_text("[roles.budget]\nmonthly_cap_usd = 20\n", encoding="utf-8")
        (self.home / "secrets.toml").write_text(
            f'[{secret}]\nhosts = ["{upstream_host}"]\npurposes = ["run"]\nheader = "{header}"\n', encoding="utf-8"
        )
        store = wsecrets.Store(self.home / "secrets")
        store.write(secret, VALUE)
        self.broker = broker.Broker(
            self.sock, self.home / "secrets.toml", store, log_path=self.home / "logs" / "broker.jsonl",
            allow_plain_http=True,
        ).start()  # fmt: skip

    def job(self, name: str, runner: str, model: str, host: str, secret: str, options: str, top: str = "") -> None:
        text = (
            f'runner = {json.dumps(runner)}\nmodel = {json.dumps(model)}\nprompt = "run the command"\ntimeout_s = 150\n{top}\n'
            f"[runner_options]\n{options}\n[[secrets]]\nname = {json.dumps(secret)}\nhost = {json.dumps(host)}\n"
            'purpose = "run"\n'
        )
        (self.home / "jobs" / f"{name}.toml").write_text(text, encoding="utf-8")

    def records(self) -> list[dict]:
        return runrecord.read(self.log)

    def stop(self) -> None:
        if self.broker:
            self.broker.stop()


@pytest.fixture
def rig(monkeypatch):
    base = Path(tempfile.mkdtemp(prefix="wsr"))  # short: a Unix socket path tops out near 100 characters
    r = Rig(base)
    monkeypatch.setenv("WS_HOME", str(r.home))
    monkeypatch.setenv("WS_BROKER_SOCKET", str(r.sock))
    monkeypatch.setenv("HOME", str(r.user))
    yield r
    r.stop()
    shutil.rmtree(base, ignore_errors=True)


def host_of(stub) -> str:
    return stub.url.removeprefix("http://")


def check_complete(rec: dict, may_be_null: set[str]) -> None:
    errors = [f"{list(e.path)}: {e.message}" for e in VALIDATOR.iter_errors(rec)]
    assert not errors, errors
    nulls = {k for k, v in rec.items() if v is None} - {"eval"}  # null unless `ws eval run` made the record
    assert nulls == may_be_null, f"null fields differ: {sorted(nulls)} (expected {sorted(may_be_null)})"
    t = rec["tokens"]
    assert all(isinstance(t[k], int) for k in t), t
    assert t["input"] > 0 and t["output"] > 0
    assert rec["status"] == "ok" and rec["exit_code"] == 0
    assert re.fullmatch(r"\d+\.\d+\.\d+", rec["runner_version"]), rec["runner_version"]
    assert rec["duration_ms"] > 0 and rec["models_used"] and rec["session_id"]
    assert rec["policy"] == {"dial": "prepare", "rule_of_two": "ok"}
    assert rec["ws_version"] and rec["cwd"] and Path(rec["cwd"]).is_dir()
    assert Path(rec["exe_path"]).is_file() and rec["output_truncated"] is False
    assert Path(rec["transcript_path"]).is_file()


def check_no_secret_left_behind(rig: Rig, stub, rec: dict) -> None:
    """The upstream got the planted key, the runner never did, and the value is in none of ws's own files."""
    assert stub.keys and all(VALUE in k for k in stub.keys), "the broker did not inject the key"
    for path in (rig.home / "runs" / rec["run_id"], rig.log):
        files = [path] if path.is_file() else [p for p in path.rglob("*") if p.is_file()]
        for f in files:
            assert VALUE not in f.read_text(encoding="utf-8", errors="replace"), f"the key is in {f.name}"
    assert broker.status(rig.sock)["leases"] == 0, "a lease was not released"


def test_claude_code_bare_record_is_complete(rig):
    exe = runner_bin("claude")
    with SeenAnthropic([Call("Bash", {"command": "echo hi"})]) as stub:
        rig.start(host_of(stub), "anthropic", "x-api-key")
        rig.job(
            "claude-job", "claude-code", "claude-sonnet-4-5", host_of(stub), "anthropic",
            f'exe = {json.dumps(exe)}\nallowed_tools = ["Bash"]', top='green = "targeted"',
        )  # fmt: skip
        assert cli.main(["run", "claude-job"]) == 0
        (rec,) = rig.records()
        check_complete(rec, may_be_null={"error"})
        assert rec["green"] == "targeted" and rec["recovered"] == [] and rec["schema"] == 2
        assert rec["runner"] == "claude-code" and rec["model_requested"] == "claude-sonnet-4-5"
        assert rec["models_used"][0]["model"] == "claude-sonnet-4-5"
        assert rec["cost_usd"] > 0 and rec["cost_basis"] and rec["duration_api_ms"] is not None
        assert rec["num_turns"] == 2  # the tool call, then the answer
        assert str(rig.user) in rec["transcript_path"] and rec["transcript_path"].endswith(f"{rec['session_id']}.jsonl")
        assert rec["secrets_used"] == [{"name": "anthropic", "host": host_of(stub)}]
        check_no_secret_left_behind(rig, stub, rec)
        assert len(stub.tool_results()) == 1  # the runner ran the command and told the model


def test_opencode_record_is_complete(rig):
    exe = runner_bin("opencode")
    cache = os.environ.get("WS_E2E_CACHE") or str(rig.base / "cache")
    with SeenOpenAI([Call("bash", {"command": "echo hi", "description": "run it"})]) as stub:
        rig.start(host_of(stub), "stubkey", "authorization")
        rig.job(
            "oc-job", "opencode", "stub/m", host_of(stub), "stubkey",
            f"exe = {json.dumps(exe)}\noffline = true\n"
            'provider = { npm = "@ai-sdk/openai-compatible", name = "stub", models = { m = { name = "m" } } }\n'
            f"env = {{ XDG_CACHE_HOME = {json.dumps(cache)} }}",
            top="allow_unpriced = true\ngreen = \"package\"",  # a stub model has no price; without this ws refuses
        )  # fmt: skip
        assert cli.main(["run", "oc-job"]) == 0
        (rec,) = rig.records()
        # the stub has no price list, so OpenCode reports no cost; the stream has no API time and no model name
        check_complete(rec, may_be_null={"error", "duration_api_ms", "cost_usd"})
        assert rec["runner"] == "opencode" and rec["cost_basis"] == "unpriced" and rec["allow_unpriced"] is True
        assert rec["green"] == "package"
        assert rec["models_used"][0]["model"] == "stub/m"  # filled in from the request: the stream does not name it
        assert rec["num_turns"] == 2 and rec["session_id"].startswith("ses_")
        assert rec["transcript_path"].endswith("opencode.db") and str(rig.user) in rec["transcript_path"]
        check_no_secret_left_behind(rig, stub, rec)
        assert len(stub.tool_results()) == 1


def test_a_lease_the_broker_refuses_is_a_refused_record(rig):
    exe = runner_bin("claude")
    with SeenAnthropic([]) as stub:
        rig.start(host_of(stub), "anthropic", "x-api-key")
        rig.job(
            "claude-job", "claude-code", "claude-sonnet-4-5", "elsewhere.example.com", "anthropic",
            f"exe = {json.dumps(exe)}",
        )  # fmt: skip
        assert cli.main(["run", "claude-job"]) == 3
        (rec,) = rig.records()
        assert not list(VALIDATOR.iter_errors(rec))
        assert rec["status"] == "refused" and "host_not_allowed" in rec["error"] and rec["secrets_used"] == []
        assert stub.requests == [] and stub.keys == []


def test_two_runs_at_once_leave_two_whole_records(rig):
    """Two `ws run`s finishing together: both lines land whole in the log."""
    exe = runner_bin("claude")
    with SeenAnthropic([]) as stub:
        rig.start(host_of(stub), "anthropic", "x-api-key")
        rig.job("a", "claude-code", "claude-sonnet-4-5", host_of(stub), "anthropic", f"exe = {json.dumps(exe)}")
        rig.job("b", "claude-code", "claude-sonnet-4-5", host_of(stub), "anthropic", f"exe = {json.dumps(exe)}")
        codes: dict[str, int] = {}
        threads = [threading.Thread(target=lambda n=n: codes.update({n: cli.main(["run", n])})) for n in ("a", "b")]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=170)
        assert codes == {"a": 0, "b": 0}
        recs = rig.records()
        assert sorted(r["job"] for r in recs) == ["a", "b"] and recs[0]["run_id"] != recs[1]["run_id"]
        assert all(not list(VALIDATOR.iter_errors(r)) for r in recs)


@pytest.mark.parametrize("runner", ["claude-code", "opencode"])
def test_the_run_is_under_the_gate(rig, runner):
    """The gate config `ws run` writes for the run is the one in force: a denied command is blocked, not run."""
    work = rig.base / "gated-work"
    work.mkdir()
    denied = denied_command(work)  # makes a DENIED marker, then does what the default policy forbids
    if runner == "claude-code":
        stub_cls, call = SeenAnthropic, Call("Bash", {"command": denied})
        secret, header, model = "anthropic", "x-api-key", "claude-sonnet-4-5"
        options = f'exe = {json.dumps(runner_bin("claude"))}\nallowed_tools = ["Bash"]'
    else:
        stub_cls, call = SeenOpenAI, Call("bash", {"command": denied, "description": "x"})
        secret, header, model = "stubkey", "authorization", "stub/m"
        cache = os.environ.get("WS_E2E_CACHE") or str(rig.base / "cache")
        options = (
            f"exe = {json.dumps(runner_bin('opencode'))}\noffline = true\n"
            'provider = { npm = "@ai-sdk/openai-compatible", name = "stub", models = { m = { name = "m" } } }\n'
            f"env = {{ XDG_CACHE_HOME = {json.dumps(cache)} }}"
        )
    with stub_cls([call]) as stub:
        rig.start(host_of(stub), secret, header)
        rig.job("gated", runner, model, host_of(stub), secret, options, top=f'allow_unpriced = true\ncwd = "{work}"')
        assert cli.main(["run", "gated"]) == 0
        results = stub.tool_results()
        assert len(results) == 1, results
        assert any(w in results[0].lower() for w in BLOCKED), results
        assert not (work / "DENIED").exists(), "the denied command ran"
        assert rig.records()[0]["status"] == "ok"
