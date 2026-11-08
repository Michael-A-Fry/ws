"""OpenCode gated as fully as Claude Code, on the real runner against a stub model (tests/test_opencode_parity.py has the
same rules without one): the native `ask` rule alone and the shim alone, an approved call that runs once and is not asked
twice, a guard's finding that reaches the model after a call, the start text in the model's context, the last message
read at stop, and the asks file `ws home` reads.

Each check has a control (a call or folder the rule does not touch) so a pass cannot be vacuous. A missing runner skips
the test, unless WS_E2E_REQUIRE=1 (CI), when it fails."""

from __future__ import annotations

import json
import mmap
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from e2e import test_gate_runners as gr
from e2e.stubs import Call, OpenAIStub
from ws import approvals, policy
from ws.adapters import opencode
from ws.home import api
from ws.home.sources import asks_jsonl

ws_home = gr.ws_home
work = gr.work

ASK = 'ask_commands = ["touch *APPROVED"]'
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def write_policy(tmp_path, *, ask: bool = False, guards: str | None = None) -> Path:
    """The built-in policy as the policy file the generated shim names, with an ask rule and/or another guard list."""
    text = policy.default_toml()
    if ask:
        text = text.replace("ask_commands = []", ASK)
        assert ASK in text
    if guards is not None:
        text = text.replace('on = ["syntax-check", "session-context", "dev-server"]', f"on = {json.dumps(guards)}")
        assert json.dumps(guards) in text
    path = tmp_path / "wshome" / "policy.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def layer(tmp_path, which: str) -> Path:
    """The generated folder with one layer left: "native" has no shim, "shim" has no permission block, "both" has both."""
    out = gr.generate(tmp_path, "opencode")
    if which == "native":
        (out / "plugins" / "ws-gate.js").unlink()
    elif which == "shim":
        conf = json.loads((out / "opencode.json").read_text())
        del conf["permission"]
        (out / "opencode.json").write_text(json.dumps(conf))
    return out


def bash(command: str) -> Call:
    return Call("bash", {"command": command, "description": "run it"})


def step(tmp_path, out, work, calls: list[Call], *, first: bool = False, text: str | None = None):
    """One `opencode run` (a later one continues the session) against a fresh stub that replays `calls`."""
    with OpenAIStub(calls, **({"final_text": text} if text else {})) as stub:
        proc = gr.opencode_run(tmp_path, stub, out, work, extra_args=() if first else ("--continue",))
    return stub, proc


def asks_lines(tmp_path) -> list[dict]:
    file = tmp_path / "home" / ".claude-usage" / "asks" / "asks.jsonl"
    return [json.loads(ln) for ln in file.read_text().splitlines() if ln.strip()] if file.is_file() else []


# --- the event names, against the binary ----------------------------------------------------------------------------


def test_the_event_names_are_ones_the_installed_binary_knows():
    binary = Path(gr.runner_bin("opencode")).resolve()
    names = [n for n in opencode.EVENT_NAMES if n != "session.idle.child"] + [
        "permission.asked",
        "permission.replied",
        "experimental.chat.system.transform",
    ]
    with binary.open("rb") as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as blob:
        missing = [n for n in names if blob.find(n.encode()) < 0]
    assert not missing, f"{binary.name} has no {missing}: the names in EVENT_NAMES came from another version"


# --- an ask, one layer at a time ------------------------------------------------------------------------------------


@pytest.mark.parametrize("which", ["native", "shim"])
def test_ask_refused_without_approval(tmp_path, work, which):
    write_policy(tmp_path, ask=True)
    out = layer(tmp_path, which)
    target = work / "APPROVED"
    stub, proc = step(tmp_path, out, work, [bash(f"touch {target}")], first=True)
    assert not target.exists(), f"the ask let the call through\n{gr.where(proc)}"
    if which == "shim":  # the shim says how to get the yes, and the model is told
        results = stub.tool_results()
        assert len(results) == 1 and "ws approve" in results[0], f"{results}\n{gr.where(proc)}"
    else:  # a headless OpenCode turns an ask nobody answers into a refusal, and ends the turn
        assert "rejected permission" in proc.stderr, gr.where(proc)
    control = work / "OTHER"
    step(tmp_path, out, work, [bash(f"touch {control}")], first=True)
    assert control.exists(), "control: a command the policy does not ask about must run"


def test_approved_call_runs_once(tmp_path, work):
    write_policy(tmp_path, ask=True)
    out = layer(tmp_path, "both")
    target = work / "APPROVED"
    call = bash(f"touch {target}")
    root = tmp_path / "wshome" / "approvals"

    stub, proc = step(tmp_path, out, work, [call], first=True)
    assert not target.exists() and "ws approve" in stub.tool_results()[0], gr.where(proc)
    pending = sorted((root / "pending").glob("*.json"))
    assert len(pending) == 1
    approvals.grant(root, pending[0].stem[:12])  # the person, in a terminal of their own

    stub, proc = step(tmp_path, out, work, [call] * 2)
    assert target.exists(), f"the approved call did not run (asked twice?): {stub.tool_results()}\n{gr.where(proc)}"
    assert "rejected" not in " ".join(stub.tool_results()).lower()
    assert not list((root / "granted").glob("*.json")), "the approval was not used up"

    target.unlink()
    stub, proc = step(tmp_path, out, work, [call] * 3)
    assert not target.exists() and "ws approve" in stub.tool_results()[-1], "a second use must be refused"


def test_approved_compound_call_runs_once(tmp_path, work):
    """OpenCode lists every shell segment in one ask (`touch X, touch Y`); when every one is ws's, the shim answers it."""
    write_policy(tmp_path, ask=True)
    out = layer(tmp_path, "both")
    target = work / "APPROVED"
    call = bash(f"touch {work / 'FIRST_APPROVED'} && touch {target}")
    root = tmp_path / "wshome" / "approvals"

    stub, proc = step(tmp_path, out, work, [call], first=True)
    assert not target.exists() and "ws approve" in stub.tool_results()[0], gr.where(proc)
    pending = sorted((root / "pending").glob("*.json"))
    assert len(pending) == 1
    approvals.grant(root, pending[0].stem[:12])

    stub, proc = step(tmp_path, out, work, [call] * 2)
    assert target.exists(), f"the approved compound call did not run: {stub.tool_results()}\n{gr.where(proc)}"
    assert "rejected" not in " ".join(stub.tool_results()).lower(), gr.where(proc)
    assert not list((root / "granted").glob("*.json")), "the approval was not used up"
    asked = [ln for ln in asks_lines(tmp_path) if ln.get("event") == "added"]
    assert len(asked) == 1, f"asked more than once: {asked}"

    target.unlink()
    stub, proc = step(tmp_path, out, work, [call] * 3)
    assert not target.exists() and "ws approve" in stub.tool_results()[-1], "a second use must be refused"


def test_asks_file_gets_open_and_closed_lines(tmp_path, work):
    write_policy(tmp_path, ask=True)
    out = layer(tmp_path, "both")
    target = work / "APPROVED"
    call = bash(f"touch {target}")
    root = tmp_path / "wshome" / "approvals"
    step(tmp_path, out, work, [call], first=True)
    assert asks_lines(tmp_path) == [], "the gate refused it before the runner asked: no ask yet"
    approvals.grant(root, next((root / "pending").glob("*.json")).stem[:12])
    step(tmp_path, out, work, [call] * 2)
    assert target.exists()

    lines = asks_lines(tmp_path)
    added = [ln for ln in lines if ln["event"] == "added"]
    closed = [ln for ln in lines if ln["event"] == "answered"]
    assert len(added) == len(closed) == 1, lines
    assert added[0]["id"] == closed[0]["id"] and added[0]["source"] == "permission"
    assert added[0]["session"]["session_id"].startswith("ses_") and "touch" in added[0]["question"]
    assert added[0]["repo"] == str(work) or Path(added[0]["repo"]).resolve() == work.resolve()
    # the reader ws home uses folds the two lines into nothing open; with the closing line cut off it is one open ask
    env = api.Env.make({"HOME": str(tmp_path / "home"), "WS_HOME": str(tmp_path / "wshome")})
    assert asks_jsonl.read("asks", env) == []
    file = tmp_path / "home" / ".claude-usage" / "asks" / "asks.jsonl"
    file.write_text(json.dumps(added[0]) + "\n")
    (open_ask,) = asks_jsonl.read("asks", env)
    assert open_ask.kind == "permission" and open_ask.session_id == added[0]["session"]["session_id"]


# --- the guards on OpenCode's other events --------------------------------------------------------------------------


def test_post_finding_reaches_the_model(tmp_path, work):
    write_policy(tmp_path)
    out = layer(tmp_path, "both")
    bad, good = work / "bad.py", work / "good.py"
    calls = [Call("write", {"filePath": str(bad), "content": "def f(:\n"})]
    stub, proc = step(tmp_path, out, work, calls, first=True)
    assert bad.exists(), gr.where(proc)  # a finding after the write does not undo it
    (result,) = stub.tool_results()
    assert "syntax-check" in result and "bad.py" in result, f"the finding never reached the model: {result!r}"
    stub, proc = step(tmp_path, out, work, [Call("write", {"filePath": str(good), "content": "x = 1\n"})], first=True)
    assert "syntax-check" not in stub.tool_results()[0], "control: a file that compiles gets no finding"


def test_start_text_reaches_the_model(tmp_path, work):
    write_policy(tmp_path)
    out = layer(tmp_path, "both")
    (work / "STATE.md").write_text("where it is at\n")
    with OpenAIStub([]) as stub:
        proc = gr.opencode_run(tmp_path, stub, out, work)
    assert proc.returncode == 0, gr.where(proc)
    system = [m["content"] for r in stub.requests for m in r.get("messages", []) if m.get("role") == "system"]
    assert any("STATE.md exists" in str(text) for text in system), f"the start text never reached the model: {system!r}"
    empty = tmp_path / "empty"
    empty.mkdir()
    with OpenAIStub([]) as stub:
        gr.opencode_run(tmp_path, stub, out, empty)
    system = [m["content"] for r in stub.requests for m in r.get("messages", []) if m.get("role") == "system"]
    assert not any("STATE.md exists" in str(text) for text in system), "control: no state file, no line"


@needs_git
def test_stop_reads_the_last_message_through_the_plugin_client(tmp_path, work):
    write_policy(tmp_path, guards=["claims"])
    out = layer(tmp_path, "both")
    env = {"GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"}
    flags = ["-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false"]
    for argv in (["init", "-q", "-b", "main"], ["commit", "-q", "--allow-empty", "-m", "first"]):
        subprocess.run(["git", *flags, *argv], cwd=work, env={**gr.base_env(tmp_path), **env}, check=True)
    sha = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=work, capture_output=True, text=True, check=True
    ).stdout.strip()[:10]
    step(tmp_path, out, work, [], first=True, text=f"merged {sha} into main")
    log = tmp_path / "wshome" / "logs" / "claims.jsonl"
    end, records = time.time() + 15, []  # a headless run exits at idle, and the guard's process finishes just after it
    while not records and time.time() < end:
        records = [json.loads(ln) for ln in log.read_text().splitlines()] if log.is_file() else []
        time.sleep(0.2)
    assert records and records[0]["runner"] == "opencode" and records[0]["claims"], records
    assert records[0]["claims"][0]["commit"] == sha


def test_a_task_is_an_agent_call_and_a_subagent_stops_as_one(tmp_path, work):
    write_policy(tmp_path, guards=["spawn-cap"])
    out = layer(tmp_path, "both")
    task = Call("task", {"description": "d", "prompt": "say hi", "subagent_type": "general"})
    stub, proc = step(tmp_path, out, work, [task], first=True)
    assert "task_result" in stub.tool_results()[0], f"the check refused the task call\n{gr.where(proc)}"
    log = tmp_path / "wshome" / "logs" / "gate.jsonl"
    want = {
        "tool.execute.before",
        "tool.execute.after",
        "session.idle.child",
    }  # what spawn-cap reads, as OpenCode sent it
    end, seen = time.time() + 15, set()
    while not want <= seen and time.time() < end:
        rows = [json.loads(ln) for ln in log.read_text().splitlines()] if log.is_file() else []
        seen = {r["event"] for r in rows if any(g["guard"] == "spawn-cap" for g in r["guards"])}
        time.sleep(0.2)
    assert want <= seen, f"spawn-cap was not asked at {sorted(want - seen)}: {sorted(seen)}"
    assert "session.idle" not in seen, "the root session's idle is a stop, which spawn-cap does not read"


# --- the native floor, alone ----------------------------------------------------------------------------------------


def test_a_skills_folder_cannot_be_written_natively(tmp_path, work):
    write_policy(tmp_path)
    out = layer(tmp_path, "native")
    skill = tmp_path / "home" / ".agents" / "skills" / "evil" / "SKILL.md"
    stub, proc = step(tmp_path, out, work, [Call("write", {"filePath": str(skill), "content": "x"})], first=True)
    gr.assert_all_blocked(stub, proc)
    assert not skill.exists()
    fine = work / "notes.txt"
    step(tmp_path, out, work, [Call("write", {"filePath": str(fine), "content": "x"})], first=True)
    assert fine.exists(), "control: a write elsewhere must go through"
