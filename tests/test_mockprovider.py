"""The mock provider package and its scenarios: tests/scenarios.json is well formed and equals the built-in set, a
scenario scripts both stubs, the old `e2e.stubs` import still works, and `python -m ws.testing.mockprovider` announces
its URL on the first line and serves."""

from __future__ import annotations

import copy
import json
import os
import shutil
import signal
import ssl
import subprocess
import sys
import urllib.request
from pathlib import Path

import pytest

from ws.testing import mockprovider, scenarios

FILE = Path(__file__).parent / "scenarios.json"
NAMES = {"denied_command", "approved_command", "broker_lease", "budget_refusal", "first_task", "plain_text"}


def doc() -> dict:
    return json.loads(FILE.read_text(encoding="utf-8"))


def test_the_scenarios_file_is_well_formed():
    assert scenarios.problems(doc()) == []
    assert NAMES <= set(doc()["scenarios"])


def test_the_built_in_set_is_the_file():
    """`ws secret wall` copies only .py files, so the package carries the set as Python; the two must not drift."""
    assert doc() == scenarios.DEFAULT


@pytest.mark.parametrize(
    ("change", "says"),
    [
        (lambda d: d.update(schema=2), "schema"),
        (lambda d: d["scenarios"]["plain_text"].update(colour=1), "unknown key 'colour'"),
        (lambda d: d["scenarios"]["plain_text"].update(description=""), "description"),
        (lambda d: d["scenarios"]["plain_text"]["expect"].update(outcome="great"), "expect.outcome"),
        (lambda d: d["scenarios"]["first_task"]["calls"][0]["tool"].pop("opencode"), "tool must be given for"),
        (lambda d: d["scenarios"]["first_task"]["calls"][0].update(extra=1), "exactly tool and args"),
        (lambda d: d["scenarios"]["first_task"]["expect"].update(present="x"), "lists of paths"),
    ],
)
def test_a_malformed_file_is_named(change, says):
    d = copy.deepcopy(doc())
    change(d)
    assert any(says in p for p in scenarios.problems(d)), scenarios.problems(d)


def test_every_scenario_scripts_both_runners_with_the_work_folder_filled_in():
    for name in NAMES:
        s = scenarios.get(name)
        for runner in scenarios.RUNNERS:
            calls = s.calls_for(runner, work="/w")
            assert all("${work}" not in json.dumps(c.args) for c in calls), name
            assert len(calls) == len(s.calls)
        assert s.expect["outcome"] in scenarios.OUTCOMES


def test_the_harmless_first_task_writes_one_file_and_the_denied_command_would_too():
    (call,) = scenarios.get("first_task").calls_for("claude-code", work="/w")
    assert (call.tool, call.args["file_path"]) == ("Edit", "/w/hello.txt")
    (call,) = scenarios.get("first_task").calls_for("opencode", work="/w")
    assert (call.tool, call.args["filePath"]) == ("write", "/w/hello.txt")
    (call,) = scenarios.get("denied_command").calls_for("opencode", work="/w")
    assert call.tool == "bash" and "/w/DENIED" in call.args["command"] and "git push --force" in call.args["command"]
    assert scenarios.get("denied_command").expectation(work="/w")["absent"] == ["/w/DENIED"]
    assert scenarios.get("budget_refusal").calls == () and scenarios.get("plain_text").final_text == "ok, done"


def test_an_unknown_scenario_or_runner_is_an_error_that_lists_the_known_ones():
    with pytest.raises(scenarios.ScenarioError, match="no scenario 'nope'.*first_task"):
        scenarios.get("nope")
    with pytest.raises(scenarios.ScenarioError, match="runner must be"):
        scenarios.get("first_task").calls_for("emacs")


def test_another_file_can_be_loaded_by_path_or_environment(tmp_path):
    d = copy.deepcopy(doc())
    d["scenarios"] = {"mine": d["scenarios"]["plain_text"]}
    f = tmp_path / "s.json"
    f.write_text(json.dumps(d), encoding="utf-8")
    assert set(scenarios.load(f)) == {"mine"}
    assert set(scenarios.load(env={"WS_MOCK_SCENARIOS": str(f)})) == {"mine"}
    f.write_text("{not json", encoding="utf-8")
    with pytest.raises(scenarios.ScenarioError, match="cannot read"):
        scenarios.load(f)


def test_the_old_import_path_gives_the_same_classes():
    from e2e import stubs

    assert stubs.AnthropicStub is mockprovider.AnthropicStub
    assert stubs.OpenAIStub is mockprovider.OpenAIStub and stubs.Call is mockprovider.Call


def post(url: str, body: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def test_a_scenario_drives_the_anthropic_stub_through_its_calls_then_text():
    with mockprovider.for_scenario("first_task", "anthropic", work="/w") as stub:
        body = {"model": "m", "tools": [{"name": "Edit"}], "messages": [{"role": "user", "content": "go"}]}
        first = post(stub.url + "/v1/messages", body)
        assert first["stop_reason"] == "tool_use" and first["content"][0]["input"]["file_path"] == "/w/hello.txt"
        done = [
            *body["messages"],
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "x", "content": "done"}]},
        ]
        last = post(stub.url + "/v1/messages", {**body, "messages": done})
        assert last["stop_reason"] == "end_turn" and last["content"][0]["text"] == "I wrote hello.txt."


def test_a_scenario_drives_the_openai_stub():
    with mockprovider.for_scenario("denied_command", "openai", work="/w") as stub:
        body = {
            "model": "m",
            "tools": [{"function": {"name": "bash"}}],
            "messages": [{"role": "user", "content": "go"}],
        }
        reply = post(stub.url + "/v1/chat/completions", body)
        call = reply["choices"][0]["message"]["tool_calls"][0]["function"]
        assert call["name"] == "bash" and "/w/DENIED" in json.loads(call["arguments"])["command"]


@pytest.mark.parametrize(
    ("provider", "var"), [("anthropic", "MOCK_ANTHROPIC_BASE_URL"), ("openai", "MOCK_OPENAI_BASE_URL")]
)
def test_the_module_prints_its_url_first_and_serves_until_killed(provider, var):
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    p = subprocess.Popen(
        [sys.executable, "-m", "ws.testing.mockprovider", "--scenario", "first_task", "--provider", provider],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )  # fmt: skip
    try:
        line = p.stdout.readline().strip()
        assert line.startswith(f"{var}=http://127.0.0.1:"), line
        url = line.split("=", 1)[1]
        assert int(url.rsplit(":", 1)[1]) > 0
        with urllib.request.urlopen(url + "/v1/models", timeout=10) as r:  # it answers
            assert r.status == 200
    finally:
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0
        p.stdout.close()
        p.stderr.close()


def test_an_unknown_scenario_on_the_command_line_exits_2_naming_the_known_ones():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    p = subprocess.run(
        [sys.executable, "-m", "ws.testing.mockprovider", "--scenario", "nope"],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )  # fmt: skip
    assert p.returncode == 2 and "first_task" in p.stderr and p.stdout == ""


CERT_CNF = """[req]
distinguished_name = dn
x509_extensions = v3
prompt = no
[dn]
CN = 127.0.0.1
[v3]
basicConstraints = critical,CA:TRUE
keyUsage = critical,keyCertSign,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:127.0.0.1
"""


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs the openssl command")
def test_the_module_serves_https_with_a_certificate_and_key(tmp_path):
    (tmp_path / "c.cnf").write_text(CERT_CNF, encoding="utf-8")
    cert, key = tmp_path / "c.pem", tmp_path / "k.pem"
    subprocess.run(
        ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", str(key), "-out", str(cert),
         "-days", "2", "-config", str(tmp_path / "c.cnf")],
        check=True, capture_output=True, timeout=60,
    )  # fmt: skip
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    p = subprocess.Popen(
        [sys.executable, "-m", "ws.testing.mockprovider", "--scenario", "first_task", "--tls-cert", str(cert),
         "--tls-key", str(key)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )  # fmt: skip
    try:
        line = p.stdout.readline().strip()
        assert line.startswith("MOCK_ANTHROPIC_BASE_URL=https://127.0.0.1:"), line
        ctx = ssl.create_default_context(cafile=str(cert))  # trusted by file, and the IP is in the certificate
        with urllib.request.urlopen(line.split("=", 1)[1] + "/v1/models", timeout=10, context=ctx) as r:
            assert r.status == 200
    finally:
        p.send_signal(signal.SIGTERM)
        assert p.wait(timeout=30) == 0
        p.stdout.close()
        p.stderr.close()


def test_a_certificate_without_a_key_is_a_usage_error():
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    p = subprocess.run(
        [sys.executable, "-m", "ws.testing.mockprovider", "--scenario", "first_task", "--tls-cert", "c.pem"],
        capture_output=True, text=True, env=env, timeout=30, check=False,
    )  # fmt: skip
    assert p.returncode == 2 and "go together" in p.stderr
