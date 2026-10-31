"""The workflow files and the end-to-end script: the lab workflow can never run on a pull request, every action is
pinned, and the credential-free smoke really runs with no provider key and a space in WS_HOME.

The workflows are read as text (stdlib only, no YAML parser); they are plain enough for that.
"""

import os
import re
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"
SCRIPT = ROOT / "scripts" / "e2e_first_task.sh"
BASH = shutil.which("bash")
SHA = re.compile(r"^[0-9a-f]{40}$")


def top_level(text: str, key: str) -> list[str]:
    """The indented lines under a top-level `key:` (an empty list when it is not there), without comments."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(key)}:\s*(#.*)?$", line):
            out = []
            for later in lines[i + 1 :]:
                if later.strip() and not later.startswith((" ", "#")):
                    break
                if later.strip() and not later.lstrip().startswith("#"):
                    out.append(later)
            return out
    return []


def test_lab_workflow_runs_on_tags_and_by_hand_only():
    text = (WORKFLOWS / "lab.yml").read_text(encoding="utf-8")
    triggers = [m.group(1) for line in top_level(text, "on") if (m := re.match(r"^  (\w+):", line))]
    assert triggers == ["push", "workflow_dispatch"], triggers
    assert not re.search(r"^\s*(pull_request|pull_request_target)\b", text, re.MULTILINE)
    push = "\n".join(top_level(text, "on")).split("workflow_dispatch")[0]
    assert re.search(r'tags:\s*\["v\*"\]', push) and "branches" not in push


def test_lab_workflow_asks_for_the_lab_runner_and_reads_only():
    text = (WORKFLOWS / "lab.yml").read_text(encoding="utf-8")
    runs_on = re.findall(r"^\s*runs-on:\s*(.+?)\s*$", text, re.MULTILINE)
    assert runs_on and set(runs_on) == {"[self-hosted, ws-lab]"}, runs_on
    assert [line.strip() for line in top_level(text, "permissions")] == ["contents: read"]
    assert "persist-credentials: false" in text
    assert "--ephemeral" in (ROOT / "docs" / "lab.md").read_text(encoding="utf-8")


def test_no_other_workflow_uses_the_lab_runner():
    for f in WORKFLOWS.glob("*.yml"):
        if f.name != "lab.yml":
            assert "self-hosted" not in f.read_text(encoding="utf-8"), f.name


@pytest.mark.parametrize("workflow", sorted(p.name for p in WORKFLOWS.glob("*.yml")))
def test_every_action_is_pinned_by_commit(workflow: str):
    text = (WORKFLOWS / workflow).read_text(encoding="utf-8")
    uses = re.findall(r"^\s*(?:- )?uses:\s*(\S+)", text, re.MULTILINE)
    assert uses
    for u in uses:
        assert "@" in u and SHA.match(u.split("@", 1)[1]), f"{workflow}: {u} is not pinned by commit"


def test_cli_workflow_times_the_first_task_on_linux_macos_and_wsl2_and_smokes_windows():
    text = (WORKFLOWS / "cli.yml").read_text(encoding="utf-8")
    block = text.split("  matrix-e2e:", 1)[1].split("\n  wsl:", 1)[0]
    assert "ubuntu-24.04" in block and "macos-26" in block and "e2e_first_task.sh" in block
    assert "upload-artifact" in block and "e2e-timing.json" in block
    wsl = text.split("\n  wsl:", 1)[1].split("\n  windows-native:", 1)[0]
    assert "e2e_first_task.sh --smoke" in wsl and "e2e_first_task.sh" in wsl.replace("--smoke", "")
    native = text.split("\n  windows-native:", 1)[1]
    assert "e2e_first_task.sh --smoke" in native


posix_only = pytest.mark.skipif(sys.platform == "win32" or BASH is None, reason="needs a POSIX shell")


def stub_ws(folder: Path, exit_code: int = 0, output: str = "") -> Path:
    """A `ws` that records the environment and WS_HOME it was started with, and exits as told."""
    folder.mkdir(exist_ok=True)
    stub = folder / "ws"
    stub.write_text(
        f'#!/bin/sh\nenv > "{folder}/env.txt"\nprintf "%s" "$WS_HOME" > "{folder}/ws_home.txt"\n'
        f'printf "%s\\n" "{output}"\nexit {exit_code}\n',
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return stub


def smoke(tmp_path: Path, stub: Path, **extra: str) -> subprocess.CompletedProcess[str]:
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "TMPDIR": str(tmp_path), "WS_SMOKE_CMD": str(stub)}
    env.update(extra)
    return subprocess.run(
        [BASH, str(SCRIPT), "--smoke"], env=env, capture_output=True, text=True, timeout=60, check=False
    )


@posix_only
def test_smoke_runs_without_any_provider_key_and_with_a_space_in_ws_home(tmp_path: Path):
    planted = "planted-" + os.urandom(8).hex()
    stub = stub_ws(tmp_path / "bin")
    r = smoke(tmp_path, stub, ANTHROPIC_API_KEY=planted, OPENAI_API_KEY=planted, AWS_BEARER_TOKEN_BEDROCK=planted)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "smoke: ok" in r.stdout
    seen = (tmp_path / "bin" / "env.txt").read_text(encoding="utf-8")
    assert planted not in seen and "ANTHROPIC_API_KEY" not in seen
    assert " " in (tmp_path / "bin" / "ws_home.txt").read_text(encoding="utf-8")


@posix_only
def test_smoke_fails_on_a_crash_but_not_on_exit_1(tmp_path: Path):
    assert smoke(tmp_path, stub_ws(tmp_path / "one", exit_code=1)).returncode == 0  # a fresh doctor says 1
    assert smoke(tmp_path, stub_ws(tmp_path / "two", exit_code=2)).returncode == 1
    traceback = stub_ws(tmp_path / "three", output="Traceback (most recent call last):")
    assert smoke(tmp_path, traceback).returncode == 1


@posix_only
def test_the_script_is_valid_bash_and_has_no_bash_4_only_features():
    assert subprocess.run([BASH, "-n", str(SCRIPT)], check=False).returncode == 0
    text = SCRIPT.read_text(encoding="utf-8")
    for bash4 in ("declare -A", "mapfile", "readarray", "${EPOCHREALTIME", "&>>", "|&", ";&", "${@^^}", ",,}"):
        assert bash4 not in text, bash4
    assert text.startswith("#!/usr/bin/env bash") and "set -u" in text
