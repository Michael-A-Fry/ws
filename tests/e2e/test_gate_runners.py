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
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from e2e.stubs import AnthropicStub, Call, OpenAIStub
from ws import cli

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
    """The policy in this test is the built-in default (no file), found at the same WS_HOME the runners get."""
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))


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


def generate(tmp_path, runner: str) -> Path:
    out = tmp_path / "wshome" / "gate" / runner  # where ws puts it: $WS_HOME/gate/<runner>
    ws_cmd = shlex.join([sys.executable, "-m", "ws"])
    assert cli.main(["gate", "generate", "--runner", runner, "--out", str(out), "--ws-cmd", ws_cmd]) == 0
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


def claude_run(tmp_path, stub, settings, bare: bool, work) -> subprocess.CompletedProcess:
    env = base_env(tmp_path)
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
    stub, proc, _ = claude_go(tmp_path, work, layer, [Call("Bash", {"command": denied_command(work)})])
    assert_all_blocked(stub, proc)
    assert not (work / "DENIED").exists(), "the denied command ran"


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
    target = work / "ok.txt"
    stub, proc, _ = claude_go(tmp_path, work, layer, [Call("Edit", EDIT_NEW | {"file_path": str(target)})])
    assert target.exists(), f"control write failed: {stub.tool_results()}\n{where(proc)}"


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


def opencode_run(tmp_path, stub, out, work, project_config: str | None = None) -> subprocess.CompletedProcess:
    """OpenCode as the launch hint says: the generated config as inline content (it is read last, so it wins over a
    project file), the generated folder as the plugin folder. `project_config` plants a hostile opencode.json in the
    project that allows everything."""
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
    return run([runner_bin("opencode"), "run", "--model", "stub/m", "run the command"], env, work)


def opencode_go(tmp_path, work, layer: str, calls: list[Call]):
    out = opencode_config(tmp_path, layer)
    with OpenAIStub(calls) as stub:
        proc = opencode_run(tmp_path, stub, out, work)
    return stub, proc


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_blocks_a_denied_command(tmp_path, work, layer):
    call = Call("bash", {"command": denied_command(work), "description": "run it"})
    stub, proc = opencode_go(tmp_path, work, layer, [call])
    assert_all_blocked(stub, proc)
    assert not (work / "DENIED").exists(), "the denied command ran"


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
    target = work / "ok.txt"
    stub, proc = opencode_go(tmp_path, work, layer, [Call("write", {"filePath": str(target), "content": "x"})])
    assert target.exists(), f"control write failed: {stub.tool_results()}\n{where(proc)}"
