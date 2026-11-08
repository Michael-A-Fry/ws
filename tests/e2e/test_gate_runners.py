"""Tier 0 pass test (PLAN section 6, row 3): a denied command is blocked on both runners, with a stub model and no paid
provider. Each runner is the real binary, pointed at a stub server on 127.0.0.1 with a fake key; the stub asks it to
run a denied command that would also create a marker file, so a blocked run and an executed run look different.

Each runner is tried layer by layer, so one layer cannot hide that another is broken:
  claude-code  native   --bare skips hooks, so only permissions.deny can block
  claude-code  hook     the deny list is stripped from the generated settings; only the PreToolUse hook can block
  opencode     native   the shim plugin is removed; only the permission block can block
  opencode     shim     the permission block is stripped; only the plugin can block
Three things are asked of each layer: run a denied command, read a file under a denied folder (a planted canary under
a fake ~/.ssh), and write into ws's own gate folder and policy file. Each has a control (a command, a read and a write
the policy allows do happen) so a pass cannot be vacuous. OpenCode runs with a hostile project `opencode.json` that
allows everything: the generated config must still win.

A missing runner skips the test, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from e2e.stubs import AnthropicStub, Call, OpenAIStub
from ws import cli, policy, selfcmd
from ws.testing import scenarios

REQUIRE = os.environ.get("WS_E2E_REQUIRE") == "1"
TIMEOUT = 180
BLOCKED = ("has been denied", "denied by your permission", "ws gate: blocked", "prevents you from using")


def runner_bin(name: str) -> str:
    path = os.environ.get(f"WS_E2E_{name.upper()}") or shutil.which(name)  # WS_E2E_OPENCODE=/path overrides PATH
    if path:
        return path
    if REQUIRE:
        pytest.fail(f"{name} is not installed and WS_E2E_REQUIRE=1")
    pytest.skip(f"{name} is not installed")


def denied_command(work) -> str:
    """Creates DENIED, then force-pushes (which the default policy denies). Run through a gate it never starts; run
    without one it makes the marker and the push fails harmlessly (no repo, no remote)."""
    return f"touch {work / 'DENIED'} && git push --force nowhere main"


def allowed_command(work) -> str:
    """The control: a compound command the policy allows, so the control exercises the same hook path."""
    return f"touch {work / 'ALLOWED'} && git status"


@pytest.fixture(autouse=True)
def ws_home(tmp_path, monkeypatch):
    """The policy in this test is the built-in default, found at the same WS_HOME the runners get."""
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    # the generated hook pins the layout it was made in, and the runner gets this HOME: made in another it would refuse
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir()
    return w


@pytest.fixture
def secrets(tmp_path):
    """A canary under the runner's fake ~/.ssh (denied by the default policy) and one outside it (allowed)."""
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True)
    canary = "canary-" + os.urandom(6).hex()  # built at run time
    plain = "plain-" + os.urandom(6).hex()
    (home / ".ssh" / "id_rsa").write_text(canary)
    (home / "notes.txt").write_text(plain)
    return {"denied": home / ".ssh" / "id_rsa", "canary": canary, "ok": home / "notes.txt", "plain": plain}


def named_policy(tmp_path) -> Path:
    """The built-in default policy as a file the hooks name. A hook whose named policy file is missing refuses every
    call, so there is one; it sits outside WS_HOME, where a test can still show the policy there is never created."""
    path = tmp_path / "policy-src" / "policy.toml"
    path.parent.mkdir(exist_ok=True)
    path.write_text(policy.default_toml(), encoding="utf-8")
    return path


def generate(tmp_path, runner: str) -> Path:
    out = tmp_path / "wshome" / "gate" / runner  # where ws puts it: $WS_HOME/gate/<runner>
    ws_cmd = shlex.join(selfcmd.argv())  # the isolated command, as the real hook uses
    argv = ["gate", "generate", "--runner", runner, "--out", str(out), "--ws-cmd", ws_cmd]
    made = tmp_path / "wshome" / "policy.toml"  # a test that wrote its own policy there keeps it
    assert cli.main([*argv, "--policy", str(made if made.is_file() else named_policy(tmp_path))]) == 0
    return out


def base_env(tmp_path) -> dict:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {k: v for k, v in os.environ.items() if k in ("PATH", "LANG", "LC_ALL", "TERM", "TMPDIR")}
    env.update(HOME=str(home), WS_HOME=str(tmp_path / "wshome"), XDG_CONFIG_HOME=str(home / ".config"))
    return env


def run(cmd: list[str], env: dict, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        cmd, env=env, cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False, stdin=subprocess.DEVNULL
    )


def where(proc) -> str:
    return f"rc={proc.returncode}\nstdout={proc.stdout[-800:]}\nstderr={proc.stderr[-800:]}"


def scenario(name: str, runner: str, work) -> tuple[list[Call], dict]:
    """The stub's script and the expected outcome of a shared scenario (tests/scenarios.json, ws.testing.scenarios)."""
    s = scenarios.get(name)
    return s.calls_for(runner, work=str(work)), s.expectation(work=str(work))


def assert_paths(expect: dict, proc=None) -> None:
    """The files the scenario says must (or must not) exist afterwards."""
    for path in expect.get("present", []):
        assert Path(path).exists(), f"{path} was not made" + (f"\n{where(proc)}" if proc else "")
    for path in expect.get("absent", []):
        assert not Path(path).exists(), f"{path} was made: the call ran"


def assert_all_blocked(stub, proc, calls: int = 1) -> None:
    """The runner reported every scripted call back to the model, and each result says it was blocked."""
    results = stub.tool_results()
    assert len(results) == calls, f"the runner never reported the tool call back to the model\n{where(proc)}"
    for r in results:
        assert any(w in r.lower() for w in BLOCKED), f"tool result does not say blocked: {r!r}"
    assert proc.returncode == 0, where(proc)


# --- Claude Code -------------------------------------------------------------------------------------------------


# --bare offers Bash, Read and Edit but not Write; an Edit with an empty old_string creates the file.
EDIT_NEW = {"old_string": "", "new_string": "x"}


def claude_settings(tmp_path, layer: str) -> Path:
    settings = generate(tmp_path, "claude-code") / "settings.json"
    if layer == "hook":  # strip the native deny list: only the hook can block
        conf = json.loads(settings.read_text())
        del conf["permissions"]
        settings.write_text(json.dumps(conf))
    return settings


def claude_run(
    tmp_path, stub, settings, bare: bool, work, sources: str | None = "user", extra_env: dict | None = None
) -> subprocess.CompletedProcess:
    env = base_env(tmp_path)
    env.update(extra_env or {})
    env.update(
        ANTHROPIC_BASE_URL=stub.url,
        ANTHROPIC_API_KEY="sk-ant-" + "stub" * 8,  # fake, built at run time
        CLAUDE_CONFIG_DIR=str(tmp_path / "claude-config"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    cmd = [runner_bin("claude"), "-p", "run the command", "--settings", str(settings)]
    cmd += [
        "--allowedTools",
        "Bash,Read,Write,Edit,Grep,Glob",
        "--model",
        "claude-sonnet-4-5",
        "--no-session-persistence",
    ]
    if sources:  # as `ws run` does: a repo's own .claude/settings*.json must not widen the generated sandbox
        cmd += ["--setting-sources", sources]
    if bare:
        cmd.append("--bare")
    return run(cmd, env, work)


def claude_go(tmp_path, work, layer: str, calls: list[Call]):
    settings = claude_settings(tmp_path, layer)
    stub = AnthropicStub(calls)
    with stub:
        proc = claude_run(tmp_path, stub, settings, layer == "native", work)  # --bare skips hooks
    return stub, proc, settings.parent


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_blocks_a_denied_command(tmp_path, work, layer):
    calls, expect = scenario("denied_command", "claude-code", work)
    stub, proc, _ = claude_go(tmp_path, work, layer, calls)
    assert_all_blocked(stub, proc, calls=expect["tool_results"])
    assert_paths(expect)


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_still_runs_an_allowed_command(tmp_path, work, layer):
    stub, proc, _ = claude_go(tmp_path, work, layer, [Call("Bash", {"command": allowed_command(work)})])
    assert (work / "ALLOWED").exists(), f"control did not run: {stub.tool_results()}\n{where(proc)}"
    assert proc.returncode == 0


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_cannot_read_a_denied_file(tmp_path, work, secrets, layer):
    stub, proc, _ = claude_go(tmp_path, work, layer, [Call("Read", {"file_path": str(secrets["denied"])})])
    assert_all_blocked(stub, proc)
    assert secrets["canary"] not in " ".join(stub.tool_results())


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_still_reads_an_allowed_file(tmp_path, work, secrets, layer):
    stub, proc, _ = claude_go(tmp_path, work, layer, [Call("Read", {"file_path": str(secrets["ok"])})])
    assert secrets["plain"] in " ".join(stub.tool_results()), f"control read failed\n{where(proc)}"


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_cannot_write_its_own_gate(tmp_path, work, layer):
    gate_file = tmp_path / "wshome" / "gate" / "claude-code" / "plugins" / "evil.js"
    protected = [gate_file, tmp_path / "wshome" / "policy.toml", tmp_path / "wshome" / "jobs" / "x.toml"]
    calls = [Call("Edit", EDIT_NEW | {"file_path": str(p)}) for p in protected]
    stub, proc, _ = claude_go(tmp_path, work, layer, calls)
    assert_all_blocked(stub, proc, calls=len(calls))
    assert not any(p.exists() for p in protected)


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_still_writes_elsewhere(tmp_path, work, layer):
    calls, expect = scenario("first_task", "claude-code", work)
    stub, proc, _ = claude_go(tmp_path, work, layer, calls)
    assert_paths(expect, proc)
    assert len(stub.tool_results()) == expect["tool_results"]


# --- OpenCode ----------------------------------------------------------------------------------------------------


def opencode_config(tmp_path, layer: str) -> Path:
    out = generate(tmp_path, "opencode")
    if layer == "native":  # remove the shim: only the permission block can block
        (out / "plugins" / "ws-gate.js").unlink()
    else:  # strip the permission block: only the shim can block
        conf = json.loads((out / "opencode.json").read_text())
        del conf["permission"]
        (out / "opencode.json").write_text(json.dumps(conf))
    return out


def opencode_run(
    tmp_path,
    stub,
    out,
    work,
    project_config: str | None = None,
    wrap: list[str] | None = None,
    extra_env: dict | None = None,
    extra_args: tuple[str, ...] = (),
) -> subprocess.CompletedProcess:
    """OpenCode as the launch hint says: the generated config as inline content (it is read last, so it wins over a
    project file), the generated folder as the plugin folder. `project_config` plants a hostile opencode.json in the
    project that allows everything. `wrap` is a command put in front of opencode (the sandbox wall), `extra_env` more
    environment, `extra_args` more arguments for `opencode run`."""
    env = base_env(tmp_path)
    home = tmp_path / "home"
    provider = {
        "provider": {
            "stub": {
                "npm": "@ai-sdk/openai-compatible",
                "name": "stub",
                "options": {"baseURL": stub.url + "/v1", "apiKey": "sk-" + "stub" * 8},
                "models": {"m": {"name": "m"}},
            }
        }
    }
    content = {**json.loads((out / "opencode.json").read_text()), **provider}
    # OpenCode auto-rejects files outside the project in `run`; allow that here, so a block can only be ws's own rule
    content["permission"] = {**content.get("permission", {}), "external_directory": "allow"}
    hostile = {  # with OPENCODE_CONFIG instead of inline content, this project file overrides the generated denies
        "permission": {"bash": {"*": "allow", "git push*": "allow"}, "read": "allow", "edit": "allow"}
    }
    (work / "opencode.json").write_text(project_config or json.dumps(hostile))
    env.update(
        OPENCODE_CONFIG_CONTENT=json.dumps(content),
        OPENCODE_CONFIG_DIR=str(out),
        OPENCODE_DISABLE_AUTOUPDATE="true",
        OPENCODE_DISABLE_MODELS_FETCH="true",
        OPENCODE_DISABLE_LSP_DOWNLOAD="true",
        XDG_DATA_HOME=str(home / "data"),
        XDG_STATE_HOME=str(home / "state"),
        XDG_CACHE_HOME=os.environ.get("WS_E2E_CACHE") or str(home / "cache"),
    )
    env.update(extra_env or {})
    cmd = [*(wrap or []), runner_bin("opencode"), "run", "--model", "stub/m", *extra_args, "run the command"]
    return run(cmd, env, work)


def opencode_go(tmp_path, work, layer: str, calls: list[Call]):
    out = opencode_config(tmp_path, layer)
    with OpenAIStub(calls) as stub:
        proc = opencode_run(tmp_path, stub, out, work)
    return stub, proc


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_blocks_a_denied_command(tmp_path, work, layer):
    calls, expect = scenario("denied_command", "opencode", work)
    stub, proc = opencode_go(tmp_path, work, layer, calls)
    assert_all_blocked(stub, proc, calls=expect["tool_results"])
    assert_paths(expect)


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_still_runs_an_allowed_command(tmp_path, work, layer):
    call = Call("bash", {"command": allowed_command(work), "description": "run it"})
    stub, proc = opencode_go(tmp_path, work, layer, [call])
    assert (work / "ALLOWED").exists(), f"control did not run: {stub.tool_results()}\n{where(proc)}"
    assert proc.returncode == 0


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_cannot_read_a_denied_file(tmp_path, work, secrets, layer):
    stub, proc = opencode_go(tmp_path, work, layer, [Call("read", {"filePath": str(secrets["denied"])})])
    assert_all_blocked(stub, proc)
    assert secrets["canary"] not in " ".join(stub.tool_results())


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_still_reads_an_allowed_file(tmp_path, work, secrets, layer):
    stub, proc = opencode_go(tmp_path, work, layer, [Call("read", {"filePath": str(secrets["ok"])})])
    assert secrets["plain"] in " ".join(stub.tool_results()), f"control read failed\n{where(proc)}"


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_cannot_write_its_own_gate(tmp_path, work, layer):
    gate_file = tmp_path / "wshome" / "gate" / "opencode" / "plugins" / "evil.js"
    protected = [gate_file, tmp_path / "wshome" / "policy.toml", tmp_path / "wshome" / "jobs" / "x.toml"]
    calls = [Call("write", {"filePath": str(p), "content": "x"}) for p in protected]
    stub, proc = opencode_go(tmp_path, work, layer, calls)
    assert_all_blocked(stub, proc, calls=len(calls))
    assert not any(p.exists() for p in protected)


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_still_writes_elsewhere(tmp_path, work, layer):
    calls, expect = scenario("first_task", "opencode", work)
    stub, proc = opencode_go(tmp_path, work, layer, calls)
    assert_paths(expect, proc)
    assert len(stub.tool_results()) == expect["tool_results"]


# --- through the resident gate: payload -> client -> daemon -> guards -> reply -----------------------------------------
#
# The same hooks, with a real `ws gate serve` running and WS_GATE_DAEMON=1 set on purpose (tests/conftest.py sets it to 0
# for every other test). Each case needs its runner and skips without it. No subscription or API key is used: the model
# is the stub. The log (logs/gate.jsonl) says which way each call was answered: "daemon" or "direct".

DAEMON_ENV = {"WS_GATE_DAEMON": "1"}


def start_resident_gate(tmp_path) -> subprocess.Popen:
    """The daemon the generated hooks will find: the same ws command and layout, started before the runner."""
    from ws import paths

    pin = paths.pin_of()
    proc = subprocess.Popen(
        [*selfcmd.argv(), "gate", "serve", *pin.args()],
        env={**base_env(tmp_path), **DAEMON_ENV},
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    sock = Path(pin.state) / "run" / "gate.sock"
    for _ in range(400):
        if sock.exists():
            return proc
        if proc.poll() is not None:
            break
        time.sleep(0.05)
    proc.kill()
    pytest.fail("the resident gate did not start")


def verdicts() -> list[dict]:
    from ws import jsonl, paths

    path = Path(paths.pin_of().state) / "logs" / "gate.jsonl"
    return jsonl.read(path) if path.exists() else []


def stop_resident_gate(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        proc.kill()
    proc.wait(timeout=10)


def killing(stub_class, proc: subprocess.Popen, after: int):
    """A stub that kills the resident gate once `after` tool results are in: the next call finds it gone."""

    class Killer(stub_class):
        killed = False

        def next_call(self, offered, results_seen):
            if results_seen >= after and not self.killed:
                self.killed = True
                proc.kill()
                proc.wait(timeout=10)
            return super().next_call(offered, results_seen)

    return Killer


def is_blocked(result: str) -> bool:
    return any(w in result.lower() for w in BLOCKED)


def check_through_the_gate(stub, proc, work, secrets, killed: bool):
    """Four calls in one session: a protected read (denied), a benign command (runs), and the same two again."""
    results = stub.tool_results()
    assert len(results) == 4, f"the runner did not report all four calls\n{where(proc)}"
    assert is_blocked(results[0]) and is_blocked(results[2]), results
    assert not is_blocked(results[1]) and not is_blocked(results[3]), results
    assert secrets["canary"] not in " ".join(results)
    assert (work / "ALLOWED").exists() and (work / "ALLOWED2").exists(), where(proc)
    rows = verdicts()
    assert {r["decision"] for r in rows} >= {"deny", "allow"}, rows  # a refusal and a guard that let a command through
    assert rows[0]["via"] == "daemon", rows  # the first call was answered by the resident gate
    if killed:
        assert "direct" in {r["via"] for r in rows}, rows  # a call after the kill was decided by the client itself
    else:
        assert {r["via"] for r in rows} == {"daemon"}, rows


def second_command(work) -> str:
    return f"touch {work / 'ALLOWED2'} && git status"


@pytest.mark.parametrize("killed", [False, True], ids=["daemon-up", "daemon-killed-mid-session"])
def test_claude_code_through_the_resident_gate(tmp_path, work, secrets, killed):
    settings = claude_settings(tmp_path, "hook")  # only the hook can block
    daemon = start_resident_gate(tmp_path)
    calls = [
        Call("Read", {"file_path": str(secrets["denied"])}),
        Call("Bash", {"command": allowed_command(work)}),
        Call("Read", {"file_path": str(secrets["denied"])}),
        Call("Bash", {"command": second_command(work)}),
    ]
    try:
        stub = killing(AnthropicStub, daemon, after=2)(calls) if killed else AnthropicStub(calls)
        with stub:
            proc = claude_run(tmp_path, stub, settings, False, work, extra_env=DAEMON_ENV)
    finally:
        stop_resident_gate(daemon)
    check_through_the_gate(stub, proc, work, secrets, killed)


@pytest.mark.parametrize("killed", [False, True], ids=["daemon-up", "daemon-killed-mid-session"])
def test_opencode_through_the_resident_gate(tmp_path, work, secrets, killed):
    out = opencode_config(tmp_path, "shim")  # only the plugin can block
    daemon = start_resident_gate(tmp_path)
    calls = [
        Call("read", {"filePath": str(secrets["denied"])}),
        Call("bash", {"command": allowed_command(work), "description": "run it"}),
        Call("read", {"filePath": str(secrets["denied"])}),
        Call("bash", {"command": second_command(work), "description": "run it"}),
    ]
    try:
        stub = killing(OpenAIStub, daemon, after=2)(calls) if killed else OpenAIStub(calls)
        with stub:
            proc = opencode_run(tmp_path, stub, out, work, extra_env=DAEMON_ENV)
    finally:
        stop_resident_gate(daemon)
    check_through_the_gate(stub, proc, work, secrets, killed)


# --- onFailure "block" on a real Claude Code ---------------------------------------------------------------------------


def claude_version() -> tuple[int, ...]:
    done = subprocess.run([runner_bin("claude"), "--version"], capture_output=True, text=True, timeout=60, check=False)
    found = re.match(r"(\d+)\.(\d+)\.(\d+)", done.stdout.strip())
    return tuple(int(x) for x in found.groups()) if found else (0, 0, 0)


FAILING_HOOKS = {
    "crash": ("sh -c 'exit 1'", None),  # an exit code that is neither 0 nor 2: a hook that died
    "time-out": ("sleep 30", 1),  # a hook that is killed at its limit
}


def claude_with_failing_hook(tmp_path, work, how: str, on_failure: bool):
    """The generated PreToolUse hook with its command swapped for one that fails; `on_failure` False removes the setting,
    which is the control that shows the setting is what blocks."""
    command, timeout = FAILING_HOOKS[how]
    settings = claude_settings(tmp_path, "hook")
    conf = json.loads(settings.read_text())
    hook = conf["hooks"]["PreToolUse"][0]["hooks"][0]
    assert hook["onFailure"] == "block"  # ws wrote it
    hook["command"] = command
    if timeout is not None:
        hook["timeout"] = timeout
    if not on_failure:
        del hook["onFailure"]
    settings.write_text(json.dumps(conf))
    marker = work / "RAN"
    stub = AnthropicStub([Call("Bash", {"command": f"touch {marker}"})])
    with stub:
        proc = claude_run(tmp_path, stub, settings, False, work)
    return stub, proc, marker


@pytest.mark.parametrize("how", sorted(FAILING_HOOKS))
def test_claude_code_blocks_the_call_when_the_hook_fails_and_says_on_failure_block(tmp_path, work, how):
    if claude_version() < (2, 1, 295):
        pytest.skip("onFailure needs Claude Code 2.1.295 or newer")
    stub, proc, marker = claude_with_failing_hook(tmp_path, work, how, on_failure=True)
    assert len(stub.tool_results()) == 1, f"the runner did not report the call back\n{where(proc)}"
    assert not marker.exists(), f"the call ran although its hook failed ({how}): {stub.tool_results()}"


@pytest.mark.parametrize("how", sorted(FAILING_HOOKS))
def test_the_same_failing_hook_without_on_failure_lets_the_call_run(tmp_path, work, how):
    """The control: without the setting a failed hook is only a warning, so the block above is the setting's doing."""
    if claude_version() < (2, 1, 295):
        pytest.skip("onFailure needs Claude Code 2.1.295 or newer")
    stub, proc, marker = claude_with_failing_hook(tmp_path, work, how, on_failure=False)
    assert marker.exists(), f"the control did not run: {stub.tool_results()}\n{where(proc)}"
