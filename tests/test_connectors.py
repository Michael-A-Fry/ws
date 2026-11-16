"""Connectors: `[connectors.NAME]` parsing, the gate's derived allow_mcp, the class rule, the per-run MCP config, the
allowlist narrowing, the audit hook and its log, and the job and launch wiring (ws.connectors)."""

import json
import os
import stat
from pathlib import Path

import pytest

from ws import agentsession, broker, connectors, gate, jobs, jsonl, paths, policy
from ws.adapters import Call, claude_code
from ws.output import WsError

HTTP = """
[connectors.ledger]
url = "https://ledger.example.test/mcp?v=1"
class = "money"
token = "LEDGER_TOKEN"
tools = ["balances", "transactions_summary"]
"""


def classes(work: Path, money_readable_by: str = '["planner"]') -> str:
    return (
        f'[classes.money]\naccess = "walled"\nrepos = ["{work}/money/**"]\nreadable_by = {money_readable_by}\n'
        f'[classes.planner]\naccess = "walled"\nrepos = ["{work}/planner/**"]\n'
        f'[classes.other]\naccess = "walled"\nrepos = ["{work}/other/**"]\n'
    )


def pol_of(tmp_path: Path, extra: str = HTTP, **kw) -> policy.Policy:
    return policy.loads(classes(tmp_path, **kw) + extra)


def fake_lessor(calls: list):
    def lease(name, host):
        calls.append((name, host))
        return broker.Lease("l1", "http://127.0.0.1:9/l/TOKEN", "authorization", "")

    return lease


# --- parsing -----------------------------------------------------------------------------------------------------------


def test_a_connector_parses_and_its_tools_join_the_shipped_allow_mcp(tmp_path):
    pol = pol_of(tmp_path)
    c = pol.connectors["ledger"]
    assert (c.cls, c.read_only, c.token, c.tools) == (
        "money",
        True,
        "LEDGER_TOKEN",
        ("balances", "transactions_summary"),
    )
    assert pol.gate.allow_mcp == (*policy.MCP_SHIPPED, "mcp__ledger__balances", "mcp__ledger__transactions_summary")
    assert policy.loads("").connectors == {} and policy.loads("").gate.allow_mcp is None


def test_an_own_allow_mcp_keeps_its_names_and_gains_the_connectors(tmp_path):
    pol = pol_of(tmp_path, '[gate]\nallow_mcp = ["mcp__x__y"]\n' + HTTP)
    assert pol.gate.allow_mcp == ("mcp__x__y", "mcp__ledger__balances", "mcp__ledger__transactions_summary")


def test_the_gate_allows_a_listed_tool_and_still_asks_for_another(tmp_path):
    pol = pol_of(tmp_path)
    assert gate.evaluate(pol, Call.of("mcp", "mcp__ledger__balances", args={})) is None
    assert isinstance(gate.evaluate(pol, Call.of("mcp", "mcp__ledger__pay_bill", args={})), gate.Ask)
    denied = pol_of(tmp_path, '[gate]\ndeny_tools = ["mcp__ledger__balances"]\n' + HTTP)
    assert "deny_tools" in gate.evaluate(denied, Call.of("mcp", "mcp__ledger__balances", args={}))


@pytest.mark.parametrize(
    ("body", "says"),
    [
        ('url = "https://a.test/"\ncommand = ["x"]\ntools = ["a"]', "exactly one"),
        ('tools = ["a"]', "exactly one"),
        ('url = "ftp://a.test/"\ntools = ["a"]', "http"),
        ('url = "https://u:p' + chr(64) + 'a.test/"\ntools = ["a"]', "no user"),
        ('url = "http://a.test/"\ntoken = "T"\ntools = ["a"]', "https"),
        ('command = ["srv"]\ntoken = "T"\ntools = ["a"]', "own auth"),
        ('url = "https://a.test/"\ntoken = "sk-live 123"\ntools = ["a"]', "never a value"),
        ('url = "https://a.test/"\nclass = "nope"\ntools = ["a"]', "unknown class"),
        ('url = "https://a.test/"\ntools = []', "at least one"),
        ('url = "https://a.test/"\ntools = ["delete_account"]', "read_only"),
        ('url = "https://a.test/"\ntools = ["createTransfer"]', "read_only"),
        ('url = "https://a.test/"\ntools = ["*"]', "not a tool name"),
        ('url = "https://a.test/"\ntools = ["a"]\nwrites = true', "unknown key"),
    ],
)
def test_a_bad_connector_is_refused_whole(tmp_path, body, says):
    with pytest.raises(policy.PolicyError, match=says):
        pol_of(tmp_path, f"[connectors.c]\n{body}\n")


@pytest.mark.parametrize("name", ["ws", "a__b", "has space"])
def test_a_connector_name_must_be_a_usable_server_name(tmp_path, name):
    with pytest.raises(policy.PolicyError, match="server name"):
        pol_of(tmp_path, f'[connectors."{name}"]\ncommand = ["x"]\ntools = ["a"]\n')


def test_read_only_false_allows_a_write_tool_and_the_wildcard(tmp_path):
    pol = pol_of(tmp_path, '[connectors.c]\ncommand = ["srv"]\nread_only = false\ntools = ["*", "post_note"]\n')
    assert "mcp__c__*" in pol.gate.allow_mcp and "mcp__c__post_note" in pol.gate.allow_mcp
    assert connectors.decide(pol, "mcp__c__anything")[1]


def test_a_token_may_go_to_loopback_over_plain_http(tmp_path):
    pol = pol_of(tmp_path, '[connectors.c]\nurl = "http://127.0.0.1:8123/mcp"\ntoken = "T"\ntools = ["a"]\n')
    assert connectors.lease_host(pol.connectors["c"].url) == ("127.0.0.1:8123", "/mcp")


# --- the class rule ----------------------------------------------------------------------------------------------------


def test_only_the_same_class_or_one_readable_by_names_may_have_it(tmp_path):
    pol = pol_of(tmp_path)
    for f in ("money", "planner", "other"):
        (tmp_path / f).mkdir()
    conn = pol.connectors["ledger"]
    assert connectors.refusal(pol, conn, connectors.session_class(pol, tmp_path / "money")) is None
    assert connectors.refusal(pol, conn, connectors.session_class(pol, tmp_path / "planner")) is None
    assert "may not read" in connectors.refusal(pol, conn, connectors.session_class(pol, tmp_path / "other"))
    assert "may not read" in connectors.refusal(pol, conn, connectors.session_class(pol, None))  # unclassified
    everyone = pol_of(tmp_path, money_readable_by='["*"]')
    assert connectors.refusal(everyone, everyone.connectors["ledger"], policy.UNCLASSIFIED) is None
    with pytest.raises(WsError, match="may not read") as e:
        connectors.pick(pol, ["ledger"], connectors.session_class(pol, tmp_path / "other"))
    assert e.value.exit_code == 3
    with pytest.raises(WsError, match="no connector"):
        connectors.pick(pol, ["nope"], None)


# --- the MCP config, the lease and the allowlist ---------------------------------------------------------------------


def test_wire_writes_a_private_config_through_a_lease_and_never_the_token(tmp_path):
    pol = pol_of(tmp_path, HTTP + '[connectors.notes]\ncommand = ["notes-mcp", "--stdio"]\ntools = ["search"]\n')
    calls: list = []
    w = connectors.wire(list(pol.connectors.values()), tmp_path / "run" / "gate", fake_lessor(calls))
    assert calls == [("LEDGER_TOKEN", "ledger.example.test")]
    cfg = Path(w.argv[1])
    assert w.argv[0] == "--mcp-config" and w.argv[2] == "--strict-mcp-config"
    assert stat.S_IMODE(cfg.stat().st_mode) == 0o600 and stat.S_IMODE(cfg.parent.stat().st_mode) == 0o700
    servers = json.loads(cfg.read_text(encoding="utf-8"))["mcpServers"]
    assert servers["ledger"] == {"type": "http", "url": "http://127.0.0.1:9/l/TOKEN/mcp?v=1"}
    assert servers["notes"] == {"type": "stdio", "command": "notes-mcp", "args": ["--stdio"]}
    assert w.tools == ["mcp__ledger__balances", "mcp__ledger__transactions_summary", "mcp__notes__search"]
    assert len(w.leases) == 1


def test_a_lease_the_broker_refuses_is_a_typed_refusal(tmp_path):
    def no(name, host):
        raise broker.BrokerRefused("host_not_allowed", "no")

    with pytest.raises(WsError, match="host_not_allowed") as e:
        connectors.wire(list(pol_of(tmp_path).connectors.values()), tmp_path / "g", no)
    assert e.value.kind == "refused"


def test_allowed_tools_gain_the_allowlist_and_may_not_go_past_it(tmp_path):
    conns = list(pol_of(tmp_path).connectors.values())
    got = connectors.narrow(conns, ["Read"])
    assert got == ["Read", "mcp__ledger__balances", "mcp__ledger__transactions_summary"]
    with pytest.raises(ValueError, match="not in \\[connectors.ledger\\] tools"):
        connectors.narrow(conns, ["mcp__ledger__pay_bill"])


# --- the audit hook ----------------------------------------------------------------------------------------------------


def payload(tool: str, cwd: Path | None = None, **args) -> dict:
    p = {"tool_name": tool, "tool_input": args, "session_id": "s1", "hook_event_name": "PreToolUse"}
    return {**p, "cwd": str(cwd)} if cwd else p


def test_each_call_is_one_line_with_names_not_values(tmp_path):
    pol = pol_of(tmp_path)
    env = {connectors.AGENT_ENV: "planner", connectors.WIRED_ENV: "ledger"}
    here = tmp_path / "planner"
    assert connectors.audit(payload("mcp__ledger__balances", here, account="ACME-SECRET-42"), pol, env) == (True, "")
    ok, why = connectors.audit(payload("mcp__ledger__pay_bill", here, amount=10), pol, env)
    assert not ok and "pay_bill" in why
    assert connectors.audit(payload("mcp__other__thing"), pol, env) == (True, "")  # not a connector's: no line
    lines = jsonl.read(connectors.log_path())
    assert [(x["decision"], x["tool"]) for x in lines] == [
        ("allow", "mcp__ledger__balances"),
        ("deny", "mcp__ledger__pay_bill"),
    ]
    first = lines[0]
    assert first["schema"] == connectors.SCHEMA_VERSION and first["ts"] and first["agent"] == "planner"
    assert first["connector"] == "ledger" and first["rule"] == "[connectors.ledger] tools: balances"
    assert first["arg_keys"] == ["account"] and "ACME-SECRET-42" not in connectors.log_path().read_text()


def test_a_line_that_cannot_be_written_is_a_deny(tmp_path, monkeypatch):
    def broken(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(connectors.jsonl, "append", broken)
    ok, why = connectors.audit(payload("mcp__ledger__balances"), pol_of(tmp_path), {})
    assert not ok and "could not be written" in why


def test_the_hook_command_fails_closed(tmp_path, monkeypatch, capsys):
    (tmp_path / "planner").mkdir()
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(classes(tmp_path) + HTTP, encoding="utf-8")
    pin = paths.pin_of().args()[1::2]

    class A:
        pass

    a = A()
    a.pin, a.policy = pin, None
    monkeypatch.setenv(connectors.WIRED_ENV, "ledger")
    monkeypatch.chdir(tmp_path / "planner")
    for text, code in [
        (json.dumps(payload("mcp__ledger__balances")), 0),
        (json.dumps(payload("mcp__ledger__pay_bill")), 2),
        ("not json", 2),
        ("[]", 2),
    ]:
        monkeypatch.setattr("sys.stdin", __import__("io").StringIO(text))
        assert connectors.cmd_audit(a) == code, text
    a.pin = None
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload("mcp__ledger__balances"))))
    assert connectors.cmd_audit(a) == 2  # a hook with no pin is refused, as every pinned hook is


def test_generated_settings_carry_the_hook_only_when_there_are_connectors(tmp_path):
    with_c = json.loads(
        claude_code.generate(pol_of(tmp_path), ["ws"], tmp_path / "g", tmp_path / "p.toml")["settings.json"]
    )
    groups = with_c["hooks"]["PreToolUse"]
    hook = next(g for g in groups if g["matcher"] == "mcp__(ledger)__.*")["hooks"][0]
    assert "connector audit" in hook["command"] and hook["command"].endswith("|| exit 2")
    assert hook["onFailure"] == "block" and "--pin" in hook["command"]
    assert f"--policy {tmp_path / 'p.toml'}" in hook["command"]
    without = json.loads(
        claude_code.generate(policy.loads(""), ["ws"], tmp_path / "g", tmp_path / "p.toml")["settings.json"]
    )
    assert len(without["hooks"]["PreToolUse"]) == 1


# --- the job and the launch --------------------------------------------------------------------------------------------


def job_in(cwd: Path, extra: str = "") -> jobs.Job:
    text = (
        f'runner = "claude-code"\nmodel = "m"\nprompt = "p"\ncwd = "{cwd}"\n'
        f'[runner_options]\nconnectors = ["ledger"]\n{extra}'
    )
    return jobs.loads("planner-job", text, Path("."))


def test_a_job_names_a_connector_and_gets_it_for_that_run_only(tmp_path, monkeypatch):
    (tmp_path / "planner").mkdir()
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(classes(tmp_path) + HTTP, encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(connectors, "default_lessor", lambda ttl_s=None, sock=None: fake_lessor(calls))
    run_dir = tmp_path / "runs" / "r1"
    argv, env = claude_code.build_command(job_in(tmp_path / "planner"), run_dir, None)  # the check: no folder yet
    assert calls == [] and not run_dir.exists()
    assert "--strict-mcp-config" in argv and env[connectors.AGENT_ENV] == "planner-job"
    assert argv[argv.index("--allowedTools") + 1] == "mcp__ledger__balances,mcp__ledger__transactions_summary"
    (run_dir / "gate").mkdir(parents=True)
    argv, _ = claude_code.build_command(job_in(tmp_path / "planner"), run_dir, None)  # the start
    cfg = Path(argv[argv.index("--mcp-config") + 1])
    assert calls == [("LEDGER_TOKEN", "ledger.example.test")] and cfg.parent == run_dir / "gate"


def test_a_job_whose_class_may_not_read_it_is_a_bad_job(tmp_path):
    (tmp_path / "other").mkdir()
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(classes(tmp_path) + HTTP, encoding="utf-8")
    with pytest.raises(ValueError, match="may not read"):
        claude_code.build_command(job_in(tmp_path / "other"), tmp_path / "r", None)
    (tmp_path / "planner").mkdir()
    bad = job_in(tmp_path / "planner", 'allowed_tools = ["mcp__ledger__pay_bill"]\n')
    with pytest.raises(ValueError, match="pay_bill"):
        claude_code.build_command(bad, tmp_path / "r", None)


def test_launch_is_an_admin_command_a_session_cannot_run():
    class A:
        ws_command = "connector.launch"

    assert agentsession.admin_name("connector.launch", A()) == "connector launch"
    assert agentsession.admin_name("connector.audit", A()) is None  # the hook runs inside the session
    with pytest.raises(WsError) as e:
        agentsession.guard(A(), env={agentsession.ENV_MARKER: "1"}, stdin_tty=True, stdout_tty=True)
    assert e.value.exit_code == 3


def test_launch_wires_gate_launch_and_cleans_up(tmp_path, monkeypatch):
    (tmp_path / "planner").mkdir()
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(classes(tmp_path) + HTTP, encoding="utf-8")
    monkeypatch.chdir(tmp_path / "planner")
    released, seen = [], {}
    monkeypatch.setattr(connectors, "default_lessor", lambda ttl_s=None, sock=None: fake_lessor([]))
    monkeypatch.setattr(connectors.broker, "release", lambda lease, socket=None: released.append(lease.lease))

    def run(argv, env, check):
        seen["argv"], seen["agent"] = argv, env[connectors.AGENT_ENV]
        seen["cfg"] = Path(argv[argv.index("--mcp-config") + 1])
        assert seen["cfg"].is_file()
        return type("R", (), {"returncode": 0})()

    monkeypatch.setattr(connectors.subprocess, "run", run)

    a = type("A", (), {})()
    a.connector, a.cmd = ["ledger"], ["--", "claude", "--agent", "finance"]
    assert connectors.cmd_launch(a) == 0
    assert seen["argv"][seen["argv"].index("gate") : seen["argv"].index("gate") + 4] == [
        "gate", "launch", "--runner", "claude-code"
    ]  # fmt: skip
    assert seen["agent"] == "finance" and released == ["l1"] and not seen["cfg"].exists()
    a.cmd = ["--", "opencode"]
    with pytest.raises(WsError, match="Claude Code only"):
        connectors.cmd_launch(a)


def test_no_secret_value_or_lease_token_reaches_the_audit_log(tmp_path):
    os.environ.pop(connectors.AGENT_ENV, None)
    connectors.audit(payload("mcp__ledger__balances", q="x"), pol_of(tmp_path), {})
    text = connectors.log_path().read_text(encoding="utf-8")
    assert "LEDGER_TOKEN" not in text and "/l/TOKEN" not in text


def test_the_hook_refuses_a_listed_tool_in_a_session_ws_did_not_wire_or_whose_class_may_not_read(tmp_path):
    pol = pol_of(tmp_path)
    ok, why = connectors.audit(payload("mcp__ledger__balances", tmp_path / "planner"), pol, {})
    assert not ok and "not wired" in why  # a server named ledger that the person's own config brought
    wired = {connectors.WIRED_ENV: "ledger"}
    ok, why = connectors.audit(payload("mcp__ledger__balances", tmp_path / "other"), pol, wired)
    assert not ok and "may not read" in why
    assert [x["decision"] for x in jsonl.read(connectors.log_path())] == ["deny", "deny"]


def test_the_hook_refuses_when_its_policy_file_is_gone(tmp_path, monkeypatch):
    a = type("A", (), {})()
    a.pin, a.policy = paths.pin_of().args()[1::2], str(tmp_path / "gone.toml")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload("mcp__ledger__balances"))))
    assert connectors.cmd_audit(a) == 2


def test_a_jobs_leases_are_handed_to_the_run_to_release(tmp_path, monkeypatch):
    (tmp_path / "planner").mkdir()
    policy.policy_path().parent.mkdir(parents=True, exist_ok=True)
    policy.policy_path().write_text(classes(tmp_path) + HTTP, encoding="utf-8")
    monkeypatch.setattr(connectors, "default_lessor", lambda ttl_s=None, sock=None: fake_lessor([]))
    run_dir = tmp_path / "runs" / "r2"
    (run_dir / "gate").mkdir(parents=True)
    _, env = claude_code.build_command(job_in(tmp_path / "planner"), run_dir, None)
    assert env[connectors.WIRED_ENV] == "ledger"
    assert [x.lease for x in claude_code.run_leases(run_dir)] == ["l1"] and claude_code.run_leases(run_dir) == []


@pytest.mark.parametrize(
    ("extra", "says"),
    [
        ('[connectors.c_]\ncommand = ["x"]\ntools = ["a"]\n', "server name"),
        ('[connectors.c]\ncommand = ["x"]\ntools = ["_t"]\n', "not a tool name"),
        ('[connectors.c]\nurl = "https://a.test:99999/"\ntools = ["a"]\n', "port"),
        ('[connectors.c]\ncommand = ["x"]\ntools = ["submits"]\n', "read_only"),
    ],
)
def test_names_that_would_be_ambiguous_or_wrong_are_refused(tmp_path, extra, says):
    with pytest.raises(policy.PolicyError, match=says):
        pol_of(tmp_path, extra)


def test_allowed_tools_may_not_name_the_whole_server(tmp_path):
    conns = list(pol_of(tmp_path).connectors.values())
    with pytest.raises(ValueError, match="mcp__ledger "):
        connectors.narrow(conns, ["mcp__ledger"])
    wide = list(
        pol_of(tmp_path, '[connectors.w]\ncommand = ["x"]\nread_only = false\ntools = ["*"]\n').connectors.values()
    )
    assert "mcp__w__anything" in connectors.narrow(wide, ["mcp__w__anything"])
