"""The runner conformance table: every runner adapter ws finds is asked the same seven questions.

  gate-parity         the same neutral calls get the same decision and the same deciding layer (`want_rule`)
  sandbox-holds       what the runner's generated config protects covers ws's own files and the deny_read paths
  budget-refuses      a job over the monthly cap is refused (exit 3) before any runner or lease is touched
  run-record          a refusal still leaves one complete, schema-valid record, and it maps to run events
  skill-loads         a SKILL.md in the runner's skills folder is valid, and the runner's skill call is read as one
  mcp-tool-call       `ws mcp serve` lists its tools on the very first call and answers a tool call
  message-round-trip  send, list, read, ack leave the golden trace, and the runner's prompt event delivers the message

What a runner is expected to do comes from its adapter (ws.conformance.expectations), so a new adapter needs no edit
here. A row a runner is known to fail is listed in its `CONFORMANCE["known_failures"]`; see ws.conformance.settle.
The last section is the live tier: the real runner against a stub model on 127.0.0.1, no provider and no key.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from e2e import test_gate_runners as live
from test_run import VALIDATOR as RUN_VALIDATOR
from test_run import Leases

from ws import (
    adapters,
    broker,
    cli,
    conformance,
    gate,
    gatecorpus,
    guards,
    mcp_server,
    msg,
    paths,
    policy,
    run,
    runrecord,
    session,
)
from ws import skills as ws_skills
from ws.profile import dumps

# --- gate parity -------------------------------------------------------------------------------------------------------


def _rows_for(exp: conformance.Expect, wanted: dict) -> tuple[list[gatecorpus.Row], list[dict]]:
    """The corpus rows for this runner and the fixture rows they came from; a call ws cannot write for this runner is
    left out (and the test says how many)."""
    rows, kept = [], []
    for fx in conformance.GATE_ROWS:
        if bool(fx.get("policy")) != wanted["ask"]:
            continue
        data = {k: v for k, v in fx.items() if k in ("content",)}
        payload = conformance.payload_for(exp, fx["kind"], fx["value"], data, "{HOME}", "{CWD}")
        if payload is None:
            continue
        rows.append(gatecorpus.Row(fx["id"], payload, fx["want"], "control", fx["id"], False, exp.runner))
        kept.append(fx)
    return rows, kept


def _decisions(exp: conformance.Expect, box: Path) -> list[tuple[dict, dict]]:
    """(fixture row, judged result) for every row this runner can be asked, the ask rows under their own policy."""
    out = []
    for ask in (False, True):
        rows, kept = _rows_for(exp, {"ask": ask})
        if not rows:
            continue
        pfile = None
        if ask:
            pfile = box / "ask-policy" / "policy.toml"
            pfile.parent.mkdir(exist_ok=True)
            pfile.write_text(conformance.ASK_POLICY, encoding="utf-8")
        judged = gate.replay_corpus(exp.runner, rows, pfile)
        out += list(zip(kept, judged, strict=True))
    return out


def test_gate_parity(exp, box, attempt):
    pairs = _decisions(exp, box)
    if not pairs:
        pytest.skip(f"ws cannot write a neutral call as a {exp.runner} payload; give CONFORMANCE['payloads']")

    def row():
        for fx, got in pairs:
            where = f"{exp.runner}/{fx['id']}"
            assert got["result"] == fx["want"], f"{where}: {got['result']} ({got['reason']})"
            assert got["kind"] == fx["want_rule"], f"{where}: decided by {got['kind']}, want {fx['want_rule']}"
            rec = conformance.decision(
                exp.runner, fx["kind"], fx["kind"], got["result"], got["kind"], got["reason"] if got["kind"] else None
            )
            assert conformance.problems(rec, conformance.GATE_DECISION) == []
            event = conformance.event_from_gate(rec, "run-1", 0, "2026-01-01T00:00:00Z")
            assert conformance.problems(event, conformance.RUN_EVENT) == []

    attempt(exp, "gate-parity", row)


def test_gate_parity_is_the_same_for_every_runner(box):
    """The table above compares each runner to the fixture; this compares the runners to each other, row by row."""
    seen: dict[str, dict[str, tuple]] = {}
    for name, exp in conformance.all_expectations().items():
        for fx, got in _decisions(exp, box):
            seen.setdefault(fx["id"], {})[name] = (got["result"], got["kind"])
    assert seen, "no runner could be asked any row"
    for row, by_runner in seen.items():
        assert len(set(by_runner.values())) == 1, f"{row}: runners disagree: {by_runner}"


def test_the_decision_contract_refuses_a_half_formed_record():
    ok = conformance.decision("r", "Bash", "command", "deny", "rule:command", "denied", "try x")
    assert conformance.problems(ok, conformance.GATE_DECISION) == []
    assert conformance.problems({**ok, "decision": "maybe"}, conformance.GATE_DECISION)
    with pytest.raises(ValueError):
        conformance.decision("r", "Bash", "command", "deny")  # a deny says which rule and why
    with pytest.raises(ValueError):
        conformance.decision("r", "Bash", "command", "allow", "rule:command", "x")  # an allow is silent


# --- sandbox holds -----------------------------------------------------------------------------------------------------


def test_sandbox_holds(exp, box, attempt):
    def row():
        pol = policy.default_policy()
        out, pfile = box / ".ws" / "gate" / exp.runner, box / ".ws" / "policy.toml"
        files = exp.mod.generate(pol, ["ws"], out, pfile)
        assert files, "the adapter generated nothing"
        text = "\n".join(files.values())
        for must in (str(pfile), str(out)):
            assert must in text, f"{must} (ws's own file) is not protected in the generated config"
        for rule in pol.gate.deny_read:
            folder = rule.removesuffix("/**").removeprefix("~/").removeprefix("$HOME/")
            if folder.startswith(".") and "*" not in folder:  # a hidden folder in the home: .ssh, .gnupg
                assert folder in text, f"deny_read {rule} does not reach the generated config"
        assert "gate" in text and "check" in text and exp.runner in text, "the config does not call `ws gate check`"
        unsupported = set(getattr(exp.mod, "UNSUPPORTED_GUARDS", ()))
        assert unsupported <= set(guards.names()), f"UNSUPPORTED_GUARDS names an unknown guard: {unsupported}"
        # a protected path is refused by the gate too, whatever the runner's native layer does
        protect = adapters.protected_paths(out, pfile)
        assert gate.evaluate(pol, adapters.Call("write", str(pfile)), tuple(protect))

    attempt(exp, "sandbox-holds", row)


# --- budget refuses, and the record a run leaves ----------------------------------------------------------------------


@pytest.fixture
def jobbed(box, monkeypatch):
    """A WS_HOME with a profile, a job for the runner under test, and nothing a refused run may touch: no runner on PATH,
    no broker, no lease."""
    home = box / ".ws"
    (home / "jobs").mkdir(parents=True)
    empty = box / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    leases = Leases()
    monkeypatch.setattr(broker, "request", leases.request)
    monkeypatch.setattr(broker, "release", leases.release)
    monkeypatch.setattr(run, "_start_broker", lambda sock: False)
    return home, leases


def _write(home: Path, exp: conformance.Expect, cap) -> Path:
    if cap is not None:
        (home / "profile.toml").write_text(dumps({"roles": {"budget": {"monthly_cap_usd": cap}}}), encoding="utf-8")
    job = (
        f'runner = "{exp.runner}"\nmodel = "{exp.model}"\nprompt = "conformance"\ntimeout_s = 30\n'
        '[[secrets]]\nname = "anthropic"\nhost = "api.anthropic.com"\n'
    )
    (home / "jobs" / "conformance.toml").write_text(job, encoding="utf-8")
    return home / "logs" / "runs.jsonl"


def _spend(log: Path, runner: str, usd: float) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    rec = runrecord.new(
        run_id=runrecord.new_run_id(now), job="earlier", runner=runner, started_at=runrecord.iso(now),
        ended_at=runrecord.iso(now), status="ok", cost_usd=usd, ws_version="0",
    )  # fmt: skip
    runrecord.append(log, rec)


def test_budget_refuses(exp, jobbed, capsys, attempt):
    home, leases = jobbed

    def row():
        log = _write(home, exp, 2)
        _spend(log, exp.runner, 3.0)
        assert cli.main(["run", "conformance"]) == 3
        assert "cap" in capsys.readouterr().err
        rec = runrecord.read(log)[-1]
        assert rec["status"] == "refused" and rec["runner"] == exp.runner
        assert leases.requested == [], "a lease was asked for before the refusal"

    attempt(exp, "budget-refuses", row)


def test_run_record(exp, jobbed, capsys, attempt):
    """No cap at all is a different refusal from a spent one; it still leaves exactly one record, schema-valid."""
    home, leases = jobbed

    def row():
        log = _write(home, exp, None)
        assert cli.main(["run", "conformance"]) == 3
        capsys.readouterr()
        (rec,) = runrecord.read(log)
        errors = [f"{list(e.path)}: {e.message}" for e in RUN_VALIDATOR.iter_errors(rec)]
        assert not errors, errors
        assert rec["status"] == "refused" and rec["runner"] == exp.runner and rec["job"] == "conformance"
        assert rec["error"] and rec["stop_reason"].startswith("refused:")
        events = conformance.event_from_record(rec)
        assert [e["kind"] for e in events] == ["run.start", "run.end"] and events[1]["status"] == "refused"
        for event in events:
            assert conformance.problems(event, conformance.RUN_EVENT) == []
            span = conformance.to_otel(event)
            assert set(span["attributes"]) <= set(conformance.OTEL["attributes"].values())
            assert span["attributes"]["gen_ai.agent.name"] == exp.runner
        assert leases.requested == []

    attempt(exp, "run-record", row)


def test_the_otel_table_covers_every_event_kind_and_field():
    assert set(conformance.OTEL["operation"]) == set(conformance.EVENT_KINDS)
    ev = conformance.run_event(
        "r1", 3, "2026-01-01T00:00:00Z", "x", "model.call", model="m", input_tokens=5, output_tokens=2
    )
    span = conformance.to_otel(ev)
    assert span == {
        "name": "chat m",
        "attributes": {
            "gen_ai.operation.name": "chat", "gen_ai.agent.name": "x", "gen_ai.conversation.id": "r1",
            "gen_ai.request.model": "m", "gen_ai.usage.input_tokens": 5, "gen_ai.usage.output_tokens": 2,
        },
    }  # fmt: skip
    tool = conformance.run_event(
        "r1", 4, "2026-01-01T00:00:00Z", "x", "tool.call", name="Bash", status="error", error="timeout"
    )
    assert conformance.to_otel(tool) == {
        "name": "execute_tool Bash",
        "attributes": {
            "gen_ai.operation.name": "execute_tool", "gen_ai.agent.name": "x", "gen_ai.conversation.id": "r1",
            "gen_ai.tool.name": "Bash", "error.type": "timeout",
        },
    }  # fmt: skip
    with pytest.raises(KeyError):
        conformance.run_event("r1", 0, "2026-01-01T00:00:00Z", "x", "run.start", prompt="never carried")


# --- a skill loads -----------------------------------------------------------------------------------------------------


def test_skill_loads(exp, box, attempt):
    def row():
        env = {"HOME": str(box), "WS_HOME": str(box / ".ws")}
        folder = ws_skills.skills_dir(exp.runner, env) / conformance.SKILL_NAME
        folder.mkdir(parents=True)
        (folder / ws_skills.FILE).write_text(conformance.SKILL_TEXT, encoding="utf-8")
        text = (folder / ws_skills.FILE).read_text(encoding="utf-8")
        assert conformance.skill_problems(text, folder.name) == []
        for name, body in ws_skills.SKILLS.items():  # what ws installs for this runner is itself a valid skill
            assert conformance.skill_problems(body, name) == [], name
        if exp.skill_payload is None:
            pytest.skip(f"no skill payload for {exp.runner}; give CONFORMANCE['skill_payload']")
        calls = exp.mod.parse(exp.skill_payload(conformance.SKILL_NAME))
        calls = [calls] if isinstance(calls, adapters.Call) else calls or []
        assert [(c.kind, c.value) for c in calls] == [("skill", conformance.SKILL_NAME)], calls
        # reading the skill file is an ordinary read: the gate does not stand between a runner and its skills
        read = conformance.payload_for(exp, "read", str(folder / ws_skills.FILE), None, str(box), str(box))
        if read is not None:
            pol = policy.default_policy()
            assert gate.check(exp.runner, json.dumps(read), pol, (), None, home=box / ".ws") is None

    attempt(exp, "skill-loads", row)


def test_skill_format_checks_catch_the_usual_mistakes():
    good = conformance.SKILL_TEXT
    assert conformance.skill_problems(good, conformance.SKILL_NAME) == []
    assert conformance.skill_problems(good, "other-name")  # the name is the folder
    assert conformance.skill_problems(good.replace("name: conformance-probe", "name: Bad_Name"), "Bad_Name")
    assert conformance.skill_problems(good.replace("description:", "color: red\ndescription:"), conformance.SKILL_NAME)
    assert conformance.skill_problems("# no front matter", "x") == ["no front matter"]
    assert conformance.skill_problems(good + "x" * conformance.SKILL_MAX_BYTES, conformance.SKILL_NAME)


# --- an MCP tool call --------------------------------------------------------------------------------------------------

SERVE = (
    "import json, sys; from ws import session; env = json.loads(sys.argv[1]); session.trusted_env = lambda: env; "
    "from ws.cli import main; raise SystemExit(main(['mcp', 'serve']))"
)


class Mcp:
    """`ws mcp serve` as a child over stdio, spoken to the way a runner would."""

    def __init__(self, env: dict):
        self.proc = subprocess.Popen(
            [sys.executable, "-c", SERVE, json.dumps({k: env[k] for k in ("HOME", "WS_HOME")})],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8", env=env,
        )  # fmt: skip
        self.n = 0
        self._timer = threading.Timer(90, self.proc.kill)  # a hang is a failure, not a stuck run
        self._timer.start()

    def call(self, method: str, params: dict | None = None) -> dict:
        self.n += 1
        frame = {"jsonrpc": "2.0", "id": self.n, "method": method, **({"params": params} if params is not None else {})}
        self.proc.stdin.write(json.dumps(frame) + "\n")
        self.proc.stdin.flush()
        reply = json.loads(self.proc.stdout.readline())
        assert reply["id"] == self.n, reply
        return reply

    def tool(self, name: str, **arguments) -> dict:
        return self.call("tools/call", {"name": name, "arguments": arguments})["result"]

    def close(self) -> None:
        self._timer.cancel()
        if self.proc.poll() is None:
            self.proc.stdin.close()
            self.proc.wait(timeout=20)
        self.proc.stderr.close()
        self.proc.stdout.close()


@pytest.fixture
def mcp(box, tmp_path):
    from ws import memory

    (tmp_path / "vault").mkdir()
    (box / ".ws").mkdir()
    (box / ".ws" / "profile.toml").write_text(
        dumps({"roles": {"notes": {"path": str(tmp_path / "vault")}, "memory": {"path": str(tmp_path / "mem")}}}),
        encoding="utf-8",
    )
    memory.init(tmp_path / "mem")
    env = {**os.environ, "HOME": str(box), "WS_HOME": str(box / ".ws")}
    client = Mcp(env)
    yield client
    client.close()


def test_mcp_tool_call(exp, mcp, attempt):
    """The tools are there on the FIRST request, before any handshake: a client that connects late and a model that
    asks at once must not meet an empty list (a runner that connects MCP in the background has been seen to lose them
    on a fifth of first calls). Only then the handshake, a stable order, and a call that works."""

    def row():
        wanted = [t["name"] for t in mcp_server.TOOLS]
        first = mcp.call("tools/list")["result"]["tools"]
        assert [t["name"] for t in first] == wanted, "the first tools/list is missing tools"
        mcp.call(
            "initialize", {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": exp.runner}}
        )
        assert [t["name"] for t in mcp.call("tools/list")["result"]["tools"]] == wanted  # deterministic order
        for t in first:
            assert 1 <= len(t["name"]) <= 128 and all(c.isalnum() or c in "_-." for c in t["name"]), t["name"]
            assert t["description"] and t["inputSchema"]["type"] == "object"
        added = mcp.tool("memory_add", text=f"conformance probe for {exp.runner}", title="Probe", kind="fact")
        assert added["isError"] is False
        found = mcp.tool("memory_search", query="conformance probe")
        assert found["isError"] is False and found["structuredContent"]["results"]
        bad = mcp.tool("memory_add", text="x", kind="not-a-kind")  # a failing tool says so in its result
        assert bad["isError"] is True

    attempt(exp, "mcp-tool-call", row)


# --- a message round trip ----------------------------------------------------------------------------------------------


class Sessions:
    """Two sessions with stamped runners, and a current caller, as ws.msg finds the caller (the process tree)."""

    def __init__(self, box: Path, exp: conformance.Expect, mp: pytest.MonkeyPatch):
        self.procs = {
            sid: session.Proc(100 + i, f"t{100 + i}", 1, "claude") for i, sid in enumerate(("alpha", "bravo"))
        }
        self.who: str | None = None
        self.mp = mp
        mp.setattr(session, "_ancestors", self._chain)
        # the account's own folders are the test's (the real ones are the system's home, which a test must not touch)
        mp.setattr(session, "trusted_env", lambda: {"HOME": str(box), "WS_HOME": str(box / ".ws")})
        for var in ("WS_RELEASE", "WS_RELEASE_FILE", "TMUX_PANE", session.LAUNCH_ENV):
            mp.delenv(var, raising=False)
        for sid, proc in self.procs.items():
            folder = box / sid
            folder.mkdir()
            self.who = sid
            with mp.context() as m:
                m.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": sid, "cwd": str(folder)})))
                assert cli.main(["session", "start", "--runner", exp.runner, *paths.pin_of().args()]) == 0
            assert session.read(sid).runner_pid == proc.pid

    def _chain(self) -> list[session.Proc]:
        shell = session.Proc(5, "5", 4, "bash")
        return [shell, self.procs[self.who]] if self.who else [shell]


def _replay(sessions: Sessions, trace) -> list[dict]:
    """Play the golden steps through the message store; each step comes back as the facts it left."""
    seen, last = [], None
    for step in trace:
        who = step["as"]
        sessions.who = who
        if step["step"] == "send":
            sent = msg.send(step["to"], step["kind"], step["body"], ack_required=step["ack_required"])
            last = sent["id"]
            got = msg.inbox(step["to"])[0]
            facts = {
                "from": got["from"], "to": step["to"], "kind": got["kind"], "ack_required": got["ack_required"],
                "read": got["read_ts"] is not None,
            }  # fmt: skip
        elif step["step"] == "list":
            items = msg.inbox(who, unread_only=step.get("unread", False))
            facts = {"count": len(items)} | (
                {} if step.get("unread") else {"unread": sum(1 for m in items if m["read_ts"] is None)}
            )
        elif step["step"] == "read":
            got = msg.read(last, who)
            facts = {"body": got["body"], "from": got["from"]}
        else:
            facts = {"acked": bool(msg.ack(last, who)["ack_ts"])}
        seen.append({**{k: v for k, v in step.items() if k != "want"}, "want": facts})
    return seen


def test_message_round_trip(exp, box, monkeypatch, attempt):
    sessions = Sessions(box, exp, monkeypatch)

    def row():
        got = _replay(sessions, conformance.MSG_TRACE)
        assert conformance.golden_lines(got) == conformance.golden_lines(list(conformance.MSG_TRACE))
        # delivery: a second message reaches the model at the runner's own prompt event, marked as untrusted data
        names = [k for k, v in getattr(exp.mod, "EVENT_NAMES", {}).items() if v == "prompt"]
        if not names:
            pytest.skip(f"{exp.runner} has no prompt event to deliver a message at")
        sessions.who = "alpha"
        sent = msg.send("bravo", "note", "conformance delivery text")
        sessions.who = "bravo"
        payload = {"hook_event_name": names[0], "session_id": "bravo", "cwd": str(box / "bravo"), "prompt": "hello"}
        pol = policy.loads('[guards]\non = ["inbox"]\n')
        code, out, _ = gate.event(exp.runner, json.dumps(payload), pol, home=box / ".ws")
        assert code == 0 and "conformance delivery text" in out and "untrusted" in out.lower(), out
        assert msg.inbox("bravo", after=sent["id"] - 1)[0]["delivered_ts"] is not None

    attempt(exp, "message-round-trip", row)


# --- the live tier -----------------------------------------------------------------------------------------------------
# The real runner, pointed at a stub model on 127.0.0.1 (no provider, no key), with only ws's hook able to refuse: the
# native deny list is stripped for Claude Code and the permission block for OpenCode, so what blocks is the gate. The
# drivers are the end-to-end tests' own (tests/e2e/test_gate_runners.py). A runner with no driver here skips this tier.
# `known_failures` covers the in-process table only: a live failure is always a failure.


def _drive_claude(tmp_path, work, calls):
    stub, proc, _ = live.claude_go(tmp_path, work, "hook", calls)
    return stub, proc


def _drive_opencode(tmp_path, work, calls):
    return live.opencode_go(tmp_path, work, "shim", calls)


LIVE = {"claude-code": ("claude", _drive_claude), "opencode": ("opencode", _drive_opencode)}


@pytest.fixture
def live_box(tmp_path, monkeypatch):
    """The folders the end-to-end helpers expect: WS_HOME and HOME under tmp_path, and a project folder."""
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    (tmp_path / "home").mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    work = tmp_path / "work"
    work.mkdir()
    return work


def test_live_gate_blocks_and_allows(exp, tmp_path, live_box):
    if exp.runner not in LIVE:
        pytest.skip(f"no live driver for {exp.runner}")
    binary, drive = LIVE[exp.runner]
    live.runner_bin(binary)  # skips when the runner is not installed, fails under WS_E2E_REQUIRE=1

    calls, expect = live.scenario("denied_command", exp.runner, live_box)
    stub, proc = drive(tmp_path, live_box, calls)
    live.assert_all_blocked(stub, proc, calls=expect["tool_results"])
    live.assert_paths(expect)
    # the control: a call the policy allows does run through the same hook, so the block above is not vacuous
    calls, expect = live.scenario("first_task", exp.runner, live_box)
    stub, proc = drive(tmp_path, live_box, calls)
    live.assert_paths(expect, proc)
    assert len(stub.tool_results()) == expect["tool_results"]


# --- known failures only shrink ----------------------------------------------------------------------------------------


def test_a_known_failure_that_passes_is_stale(exp):
    one = conformance.Expect(exp.runner, exp.mod, known_failures=("gate-parity",))
    assert conformance.settle(one, "gate-parity", None)["status"] == "FAILURE"  # stale: take it off the list
    assert conformance.settle(one, "gate-parity", AssertionError("x"))["status"] == "INFO"  # known, tolerated
    assert conformance.settle(one, "sandbox-holds", AssertionError("x"))["status"] == "FAILURE"  # not listed
    assert conformance.settle(one, "sandbox-holds", None)["status"] == "SUCCESS"


def test_conformance_attribute_is_read_and_checked():
    class Mod:
        NAME = "x"

    Mod.CONFORMANCE = {"known_failures": ("run-record",), "model": "p/m"}
    got = conformance.expectations(Mod)
    assert got.known_failures == ("run-record",) and got.model == "p/m" and got.payloads == {}
    Mod.CONFORMANCE = {"known_failures": ("not-a-row",)}
    with pytest.raises(KeyError):
        conformance.expectations(Mod)
    Mod.CONFORMANCE = {"colour": "red"}
    with pytest.raises(KeyError):
        conformance.expectations(Mod)
    del Mod.CONFORMANCE
    assert conformance.expectations(Mod).known_failures == ()
