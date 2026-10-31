"""Tier 0 pass test (PLAN section 6, row 3): a denied command is blocked on both runners, with a stub model and no paid
provider. Each runner is the real binary, pointed at a stub server on 127.0.0.1 with a fake key; the stub asks it to
run a denied command that would also create a marker file, so a blocked run and an executed run look different.

Each runner is tried layer by layer, so one layer cannot hide that another is broken:
  claude-code  native   --bare skips hooks, so only permissions.deny can block
  claude-code  hook     the deny list is stripped from the generated settings; only the PreToolUse hook can block
  opencode     native   the shim plugin is removed; only the permission block can block
  opencode     shim     the permission block is stripped; only the plugin can block
Each has a control: a command the policy allows (`touch ALLOWED`) does run, so a pass cannot be vacuous.

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
BLOCKED = ("has been denied", "ws gate: blocked", "prevents you from using")


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


def generate(tmp_path, runner: str) -> Path:
    out = tmp_path / f"gate-{runner}"
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


def assert_blocked(stub, work, proc) -> None:
    results = stub.tool_results()
    where = f"rc={proc.returncode}\nstdout={proc.stdout[-800:]}\nstderr={proc.stderr[-800:]}"
    assert len(results) == 1, f"the runner never reported the tool call back to the model\n{where}"
    assert any(w in results[0].lower() for w in BLOCKED), f"tool result does not say blocked: {results[0]!r}"
    assert not (work / "DENIED").exists(), "the denied command ran"
    assert proc.returncode == 0, where


# --- Claude Code -------------------------------------------------------------------------------------------------


def claude_run(tmp_path, stub, settings, bare: bool, work) -> subprocess.CompletedProcess:
    env = base_env(tmp_path)
    env.update(
        ANTHROPIC_BASE_URL=stub.url,
        ANTHROPIC_API_KEY="sk-ant-" + "stub" * 8,  # fake, built at run time
        CLAUDE_CONFIG_DIR=str(tmp_path / "claude-config"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    cmd = [runner_bin("claude"), "-p", "run the command", "--settings", str(settings), "--allowedTools", "Bash"]
    cmd += ["--model", "claude-sonnet-4-5", "--no-session-persistence"]
    if bare:
        cmd.append("--bare")
    return run(cmd, env, work)


@pytest.fixture
def work(tmp_path):
    w = tmp_path / "work"
    w.mkdir()
    return w


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_blocks_a_denied_command(tmp_path, work, layer):
    out = generate(tmp_path, "claude-code")
    settings = out / "settings.json"
    if layer == "hook":
        conf = json.loads(settings.read_text())
        del conf["permissions"]
        settings.write_text(json.dumps(conf))
    bare = layer == "native"  # --bare skips hooks
    with AnthropicStub([Call("Bash", {"command": denied_command(work)})]) as stub:
        proc = claude_run(tmp_path, stub, settings, bare, work)
        assert_blocked(stub, work, proc)


@pytest.mark.parametrize("layer", ["native", "hook"])
def test_claude_code_still_runs_an_allowed_command(tmp_path, work, layer):
    out = generate(tmp_path, "claude-code")
    settings = out / "settings.json"
    if layer == "hook":
        conf = json.loads(settings.read_text())
        del conf["permissions"]
        settings.write_text(json.dumps(conf))
    with AnthropicStub([Call("Bash", {"command": f"touch {work / 'ALLOWED'}"})]) as stub:
        proc = claude_run(tmp_path, stub, settings, layer == "native", work)
        assert (work / "ALLOWED").exists(), f"control did not run: {stub.tool_results()}\n{proc.stderr[-800:]}"
        assert proc.returncode == 0


# --- OpenCode ----------------------------------------------------------------------------------------------------


def opencode_run(tmp_path, stub, out, work) -> subprocess.CompletedProcess:
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
    env.update(
        OPENCODE_CONFIG=str(out / "opencode.json"),
        OPENCODE_CONFIG_DIR=str(out),
        OPENCODE_CONFIG_CONTENT=json.dumps(provider),
        OPENCODE_DISABLE_AUTOUPDATE="true",
        OPENCODE_DISABLE_MODELS_FETCH="true",
        OPENCODE_DISABLE_LSP_DOWNLOAD="true",
        XDG_DATA_HOME=str(home / "data"),
        XDG_STATE_HOME=str(home / "state"),
        XDG_CACHE_HOME=os.environ.get("WS_E2E_CACHE") or str(home / "cache"),
    )
    return run([runner_bin("opencode"), "run", "--model", "stub/m", "run the command"], env, work)


def strip_opencode(out, layer: str) -> None:
    if layer == "native":
        (out / "plugins" / "ws-gate.js").unlink()
    else:
        conf = json.loads((out / "opencode.json").read_text())
        del conf["permission"]
        (out / "opencode.json").write_text(json.dumps(conf))


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_blocks_a_denied_command(tmp_path, work, layer):
    out = generate(tmp_path, "opencode")
    strip_opencode(out, layer)
    with OpenAIStub([Call("bash", {"command": denied_command(work), "description": "run it"})]) as stub:
        proc = opencode_run(tmp_path, stub, out, work)
        assert_blocked(stub, work, proc)


@pytest.mark.parametrize("layer", ["native", "shim"])
def test_opencode_still_runs_an_allowed_command(tmp_path, work, layer):
    out = generate(tmp_path, "opencode")
    strip_opencode(out, layer)
    with OpenAIStub([Call("bash", {"command": f"touch {work / 'ALLOWED'}", "description": "run it"})]) as stub:
        proc = opencode_run(tmp_path, stub, out, work)
        assert (work / "ALLOWED").exists(), f"control did not run: {stub.tool_results()}\n{proc.stderr[-800:]}"
        assert proc.returncode == 0
