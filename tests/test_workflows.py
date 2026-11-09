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
    doc = (ROOT / "docs" / "reference" / "lab.md").read_text(encoding="utf-8")
    for needed in ("generate-jitconfig", "fork-pr-contributor-approval", "all_external_contributors", "--token-file"):
        assert needed in doc, needed


def test_lab_workflow_refuses_in_its_first_step_unless_it_is_the_owners_tag_or_dispatch():
    text = (WORKFLOWS / "lab.yml").read_text(encoding="utf-8")
    first = text.split("    steps:\n", 1)[1].split("      - uses:", 1)[0]
    assert "github.repository_owner" in first and "github.actor" in first and "github.repository }}" in first
    assert 'REPO" != "Michael-A-Fry/ws"' in first and 'ACTOR" != "$OWNER"' in first
    assert "push:refs/tags/v*" in first and "workflow_dispatch:*" in first
    assert first.count("exit 1") == 3  # a wrong repository, a wrong actor, a wrong event


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


# --- ci.yml: the jobs (docs/reference/ci.md) -------------------------------------------------------------------------


def ci_jobs() -> dict[str, str]:
    """The text of each top-level job in ci.yml, by name, without whole-line comments (the comment above a job would
    otherwise be read as the end of the job before it)."""
    body = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8").split("\njobs:\n", 1)[1]
    body = re.sub(r"^\s*#.*\n", "", body, flags=re.MULTILINE)
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def run_commands(job: str) -> list[str]:
    """The text of each `run:` step of a job (a one-line command, a `|` block or a `>-` block), in order."""
    out = []
    lines = job.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^(\s*)(?:- )?run:\s*(.*)$", line)
        if not m:
            continue
        if m.group(2) not in ("|", ">", ">-"):
            out.append(m.group(2))
            continue
        block = []
        for later in lines[i + 1 :]:
            if later.strip() and len(later) - len(later.lstrip()) <= len(m.group(1)):
                break
            block.append(later.strip())
        out.append("\n".join(block))
    return out


def test_ci_job_deselects_the_oracle_and_runs_holdout():
    text = ci_jobs()["ci"]
    suite = next(c for c in run_commands(text) if "pytest" in c)
    assert "--locked pytest -q" in suite
    deselected = re.findall(r"--deselect\s+(\S+)", suite)
    assert sorted(deselected) == ["tests/test_gate_oracle.py", "tests/test_shellwords_oracle.py"]
    # tests/holdout/ is collected with everything else: no path to narrow the run, nothing that drops it
    assert "--ignore" not in suite and "holdout" not in suite and "-k " not in suite and " -m " not in suite
    assert not re.search(r"pytest\b[^\n]*\btests?/", suite.replace("--deselect tests/", ""))
    assert (ROOT / "tests" / "holdout" / "test_holdout_core.py").is_file()
    assert "WS_ORACLE_REQUIRE" not in text  # it belongs to the job that has the oracle
    assert "test_integrity.py" not in text  # T1 runs from the base branch (test_t1_runs_only_from_the_base_branch)
    assert "fetch-depth: 0" in text  # gitleaks and the PII gate read the whole history


def test_the_pull_request_body_and_base_never_reach_a_command_text():
    for name, job in ci_jobs().items():
        for command in run_commands(job):
            assert "github.event.pull_request" not in command and "${{" not in command, f"{name}: {command}"


def test_t1_runs_only_from_the_base_branch():
    """A pull request must not be able to change the code that judges it: the trigger reads the workflow from the base,
    the checkout is the base, and the pull request's commits only ever reach git."""
    text = (WORKFLOWS / "test-integrity.yml").read_text(encoding="utf-8")
    code = "\n".join(line.split(" # ", 1)[0] for line in text.splitlines() if not line.lstrip().startswith("#"))
    triggers = [m.group(1) for line in top_level(text, "on") if (m := re.match(r"^  (\w+):", line))]
    assert triggers == ["pull_request_target"]
    assert re.search(r"types:\s*\[[^\]]*\bedited\b", text), "adding the line to the body has to run the check again"
    assert re.findall(r"uses:\s*(\S+)", code) == ["actions/checkout@3d3c42e5aac5ba805825da76410c181273ba90b1"]
    assert "ref: ${{ github.sha }}" in code and "persist-credentials: false" in code  # the base branch's tip
    assert "fetch-depth: 0" in code  # the merge base has to be in the clone
    assert "secrets." not in code and "write" not in code and "permissions: {}" in code
    runs = re.findall(r"run:\s*(.+)", code)
    assert runs == [
        'git fetch --no-tags origin "refs/pull/$PR_NUMBER/head"',
        'python3 -I -S scripts/test_integrity.py --base "$BASE_SHA" --head "$HEAD_SHA"',
    ]  # a step added here runs with the base's rights: it has to be looked at, whatever it is
    assert "PR_BODY: ${{ github.event.pull_request.body }}" in code
    assert "BASE_SHA: ${{ github.event.pull_request.base.sha }}" in code
    assert "HEAD_SHA: ${{ github.event.pull_request.head.sha }}" in code  # the base's sha here would judge nothing


def test_oracle_job_has_bubblewrap_and_requires_it():
    text = ci_jobs()["oracle"]
    assert re.search(r'WS_ORACLE_REQUIRE:\s*"1"', text)
    assert "apt-get install -y --no-install-recommends bubblewrap" in text and "bwrap --version" in text
    suite = next(c for c in run_commands(text) if "pytest" in c)
    assert "tests/test_gate_oracle.py" in suite and "tests/test_shellwords_oracle.py" in suite
    assert "--deselect" not in suite and "-k " not in suite
    assert "runs-on: ubuntu-24.04" in text


def test_bubblewrap_stays_in_the_ci_job():
    """test_sandbox, test_wallsettings, test_gate_corpus and test_eval* use it and skip quietly without it."""
    text = ci_jobs()["ci"]
    assert "apt-get install -y --no-install-recommends bubblewrap" in text
    assert text.index("bubblewrap") < text.index("--deselect"), "installed before the suite runs"
    for needs in ("test_sandbox.py", "test_eval.py"):  # the ones that name it; others reach it through ws itself
        source = (ROOT / "tests" / needs).read_text(encoding="utf-8")
        assert "bubblewrap" in source or "bwrap" in source, f"{needs} no longer uses bubblewrap"


def test_nightly_quarantine_and_node_jobs_exist():
    ci = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    triggers = [m.group(1) for line in top_level(ci, "on") if (m := re.match(r"^  (\w+):", line))]
    assert {"schedule", "workflow_dispatch", "push", "pull_request"} <= set(triggers), triggers
    # nothing in this workflow reads the body, so editing it must not run the whole workflow again
    pr = "\n".join(top_level(ci, "on")).split("pull_request:", 1)[1].split("\n  schedule:", 1)[0]
    assert "edited" not in pr
    jobs = ci_jobs()
    nightly = jobs["nightly-quarantine"]
    assert "github.event_name == 'schedule'" in nightly and "github.event_name == 'workflow_dispatch'" in nightly
    assert "pytest -q -rs --run-quarantine -m quarantine" in nightly
    assert "-eq 5" in nightly, "pytest's exit 5, nothing collected, is a pass"
    node = jobs["node"]
    assert re.search(r'WS_TEST_NODE_REQUIRE:\s*"1"', node)
    assert re.search(r"WS_TEST_NODE_IMAGE:\s*\S+@sha256:[0-9a-f]{64}\s*$", node, re.MULTILINE), (
        "the image is pinned by digest"
    )
    assert "docker pull" in node and "tests/test_node.py" in node
    # every job but the nightly one is off on the schedule
    for name, job in jobs.items():
        if name != "nightly-quarantine":
            assert "if: github.event_name != 'schedule'" in job, name


def test_browser_job_runs_the_glob():
    text = ci_jobs()["term-browser"]
    assert "tests/e2e/test_*_browser.py" in text
    assert list((ROOT / "tests" / "e2e").glob("test_*_browser.py")), "the glob finds a file"
    assert re.search(r'WS_E2E_REQUIRE:\s*"1"', text)


def test_every_job_ends_with_the_budget_step():
    jobs = ci_jobs()
    assert set(jobs) == {"ci", "oracle", "nightly-quarantine", "node", "term-browser"}
    for name, job in jobs.items():
        steps = re.split(r"^      - ", job.split("\n    steps:\n", 1)[1], flags=re.MULTILINE)[1:]
        last = steps[-1]
        assert "if: always()" in last, name
        assert f"scripts/ci_time_budget.py --job {name} " in last, name
        assert '--stamp "$RUNNER_TEMP/job-start"' in last, name
        assert "job-start" in steps[0] and "date +%s" in steps[0], f"{name}: the first step stamps the start time"
        assert len(re.findall(r"ci_time_budget\.py", job)) == 1, name


def test_the_workflow_asks_for_no_more_than_it_reads():
    text = (WORKFLOWS / "ci.yml").read_text(encoding="utf-8")
    assert [line.strip() for line in top_level(text, "permissions")] == []  # `permissions: {}` at the top
    assert re.search(r"^permissions: \{\}$", text, re.MULTILINE)
    for name, job in ci_jobs().items():
        assert re.search(r"^    permissions:\n      contents: read$", job, re.MULTILINE), name
        assert "persist-credentials: false" in job, name


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


@posix_only
def test_the_host_cycle_script_is_valid_bash_and_keeps_the_token_off_the_command_line():
    script = ROOT / "scripts" / "lab-host-cycle.sh"
    assert subprocess.run([BASH, "-n", str(script)], check=False).returncode == 0
    text = script.read_text(encoding="utf-8")
    assert "generate-jitconfig" in text and "--config -" in text and "--token-file" in text
    assert not re.search(r"curl[^\n]*(Bearer \$TOKEN|-H \"Authorization)", text.replace("printf", ""))


@posix_only
def test_the_host_cycle_script_refuses_a_token_file_open_to_others(tmp_path: Path):
    token = tmp_path / "token"
    token.write_text("x", encoding="utf-8")
    token.chmod(0o644)
    r = subprocess.run(
        [BASH, str(ROOT / "scripts" / "lab-host-cycle.sh"), "--token-file", str(token), "--vm-id", "1",
         "--node", "n", "--snapshot", "s"],
        capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    assert r.returncode == 1 and "must be 0600" in r.stderr
    assert (
        subprocess.run([BASH, str(ROOT / "scripts" / "lab-host-cycle.sh")], capture_output=True, check=False).returncode
        == 2
    )


@posix_only
def test_the_host_cycle_waits_for_a_real_404_and_keeps_the_jit_config_off_the_command_line(tmp_path: Path):
    """With stub qm, curl and sleep: GitHub blips (502, no answer) do not end the cycle; 404 does; the VM is stopped last."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    jit = "jit-" + os.urandom(8).hex()
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "qm").write_text(
        f'#!/bin/sh\necho "qm $*" >> "{tmp_path}/qm.log"\ncase "$1 $2" in "guest exec") cat > "{tmp_path}/jit.in";; esac\n',
        encoding="utf-8",
    )
    (bin_dir / "curl").write_text(
        f"""#!/bin/sh
cat > /dev/null
case "$*" in
  *http_code*)
    n=$(cat "{tmp_path}/count" 2>/dev/null || echo 0); n=$((n + 1)); echo $n > "{tmp_path}/count"
    case $n in 1) printf 502;; 2) printf 000;; 3) printf 200;; *) printf 404;; esac ;;
  *generate-jitconfig*) echo '{{"runner": {{"id": 42}}, "encoded_jit_config": "{jit}"}}' ;;
  *) echo '{{}}' ;;
esac
""",
        encoding="utf-8",
    )
    for f in bin_dir.iterdir():
        f.chmod(f.stat().st_mode | stat.S_IXUSR)
    token = tmp_path / "token"
    token.write_text("t", encoding="utf-8")
    token.chmod(0o600)
    env = {"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}", "HOME": str(tmp_path)}
    r = subprocess.run(
        [BASH, str(ROOT / "scripts" / "lab-host-cycle.sh"), "--token-file", str(token), "--vm-id", "7", "--node", "n",
         "--snapshot", "clean"],
        env=env, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    assert r.returncode == 0, r.stdout + r.stderr
    calls = (tmp_path / "qm.log").read_text(encoding="utf-8").splitlines()
    assert calls[:3] == ["qm stop 7", "qm rollback 7 clean", "qm start 7"]
    assert calls[-1] == "qm stop 7" and calls.count("qm stop 7") == 2  # at the start, and once the job was done
    assert (tmp_path / "jit.in").read_text(encoding="utf-8") == jit  # on stdin
    assert jit not in "\n".join(calls) and jit not in r.stdout + r.stderr  # never on a command line
    assert (tmp_path / "count").read_text(encoding="utf-8").strip() == "4"  # 502, no answer, 200, then 404
