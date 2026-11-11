"""docs/reference/hooks.md maps each hook of a hand-built set to the ws rule or guard that does the same job. Each
`covered` row has a test here that sends the same sample call to the ws side, through the real hook entry, and checks that
it is stopped (and that its harmless neighbour is not). The last test keeps the page and these tests in step."""

from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from ws import cli, gate, paths, policy
from ws.adapters import claude_code

ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "reference" / "hooks.md"
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "w"))
    (tmp_path / "w").mkdir()
    return tmp_path


def call(tool: str, cwd="/", session="s1", **tool_input) -> str:
    return json.dumps({"tool_name": tool, "tool_input": tool_input, "session_id": session, "cwd": str(cwd)})


def hook(box, policy_text: str | None, payload: str, *protect: str) -> int:
    """The exit code of `ws gate check` for a payload under this policy (None: the built-in one)."""
    argv = ["--runner", "claude-code", *paths.pin_of().args()]
    if policy_text is not None:
        file = box / "w" / "policy.toml"
        file.write_text(policy_text)
        argv += ["--policy", str(file)]
    for p in protect:
        argv += ["--protect", p]
    return gate.run_hook("check", argv, payload)[0]


def verdict(pol: policy.Policy, payload: str, state: Path | None = None) -> str:
    reason = gate.check("claude-code", payload, pol, home=state)
    return "allow" if reason is None else "ask" if isinstance(reason, gate.Ask) else "deny"


# --- row 1, 2: shell commands and credential reads ---------------------------------------------------------------


def test_parity_irreversible_shell_commands(box):
    for command in ("rm -rf /usr", "git push --force origin main", "mkfs.ext4 /dev/sda1", "git push origin main -f"):
        assert hook(box, None, call("Bash", command=command)) == gate.DENY, command
    for command in ("ls -la", "git push origin work", "rm -rf build", "git status"):
        assert hook(box, None, call("Bash", command=command)) == 0, command


def test_parity_credential_reads(box):
    denied = (
        call("Bash", command="cat ~/.ssh/id_rsa"),
        call("Bash", command="cat ~/.aws/credentials | head -3"),
        call("Read", file_path="~/.aws/credentials"),
        call("Grep", pattern="BEGIN", path="~/.gnupg"),
        call("Glob", pattern="~/.ssh/*"),
    )
    for payload in denied:
        assert hook(box, None, payload) == gate.DENY, payload
    for payload in (call("Read", file_path="/etc/hostname"), call("Bash", command="cat README.md")):
        assert hook(box, None, payload) == 0, payload


# --- row 4: sealed folders ---------------------------------------------------------------------------------------


def test_parity_sealed_folders(box):
    text = '[gate]\ndeny_read = ["~/sealed/**"]\n'
    sealed = (
        call("Read", file_path="~/sealed/a.txt"),
        call("Bash", command="cat ~/sealed/a.txt"),
        call("Bash", command="grep -r x ~/sealed"),
        call("Grep", pattern="x", path="~/sealed"),
    )
    for payload in sealed:
        assert hook(box, text, payload) == gate.DENY, payload
    assert hook(box, text, call("Read", file_path="~/open/a.txt")) == 0
    # and the runner's own layers carry the folder too, with no hook at all
    pol = policy.loads(text)
    conf = json.loads(claude_code.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml")["settings.json"])
    assert "Read(~/sealed/**)" in conf["permissions"]["deny"]
    assert any(p.endswith(("/sealed/**", "sealed")) for p in conf["sandbox"]["filesystem"]["denyRead"])


# --- row 5: the files that hold the guardrails -------------------------------------------------------------------


def test_parity_guardrail_files(box):
    policy_file = box / "w" / "policy.toml"
    out = box / "gate-out"
    protect = (str(out),)
    for payload in (
        call("Edit", file_path=str(policy_file), old_string="a", new_string="b"),
        call("Write", file_path=str(policy_file), content="[gate]\n"),
        call("Write", file_path=str(out / "settings.json"), content="{}"),
        call("Bash", command=f"echo x >> {policy_file}"),
        call("Bash", command=f"rm {policy_file}"),
    ):
        assert hook(box, "", payload, *protect) == gate.DENY, payload
    for payload in (
        call("Write", file_path=str(box / "project" / "notes.md"), content="x"),
        call("Bash", command="ls"),
    ):
        assert hook(box, "", payload, *protect) == 0, payload


# --- row 6: staging ----------------------------------------------------------------------------------------------


def test_parity_bulk_staging():
    pol = policy.loads('[guards]\non = ["bulk-staging"]\n')
    for command in ("git add -A", "git add .", "git commit -am wip", "git commit -a -m wip", "git add --all"):
        assert verdict(pol, call("Bash", command=command)) != "allow", command
    for command in ("git add src/a.py", "git commit -m 'git add -A'", "git add -u src/", "git status"):
        assert verdict(pol, call("Bash", command=command)) == "allow", command


# --- row 7: claims -----------------------------------------------------------------------------------------------


def git(cwd, *args) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@needs_git
def test_parity_claims(box):
    repo = box / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("1")
    git(repo, "add", "a")
    git(repo, "commit", "-q", "-m", "first")
    on = git(repo, "rev-parse", "--short=10", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(repo, "checkout", "-q", "-b", "work")
    (repo / "a").write_text("2")
    git(repo, "commit", "-q", "-am", "second")
    off = git(repo, "rev-parse", "--short=10", "HEAD")
    pol = policy.loads('[guards]\non = ["claims"]\n')

    def say(text: str) -> str:
        payload = call("SendMessage", cwd=repo, to="lead", message=text)
        return verdict(pol, payload)

    assert say(f"merged {off} into main") == "deny"
    assert say(f"{off} is on main") == "deny"
    assert say(f"merged {on} into main") == "allow"
    assert say(f"committed {off} on a branch, not merged") == "allow"


@needs_git
@pytest.mark.parametrize("how", ["timeout", "budget", "no-start"])
def test_claims_deny_when_git_cannot_answer_in_time(box, monkeypatch, how):
    """The WSL2 red: a slow git used up the claims budget and the guard let the message through as "can't tell". The
    gate fails closed (ADR 0017), so a claim it can't reach git to check is denied, whatever stopped git."""
    from ws.guards import claims

    repo = box / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "a").write_text("1")
    git(repo, "add", "a")
    git(repo, "commit", "-q", "-m", "first")
    on = git(repo, "rev-parse", "--short=10", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    pol = policy.loads('[guards]\non = ["claims"]\n')

    def say(text: str) -> str:
        return verdict(pol, call("SendMessage", cwd=repo, to="lead", message=text))

    def broken(argv, **k):
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, k["timeout"])
        raise PermissionError("git would not start")

    assert say(f"merged {on} into main") == "allow"  # git answers: a true claim
    real_budget = claims.Budget
    with monkeypatch.context() as m:
        if how == "budget":
            m.setattr(claims, "Budget", lambda: real_budget(0.0))
        else:
            m.setattr(claims.subprocess, "run", broken)
        assert say(f"merged {on} into main") == "deny"  # the same claim, git not reachable in time
        assert say(f"{on} is on main") == "deny"
        assert say("the ed25519 key is on main") == "allow"  # the true negatives never needed git
        assert say(f"committed {on} on a branch, not merged") == "allow"


# --- row 8: session context ---------------------------------------------------------------------------------------


@needs_git
def test_parity_session_context(box):
    repo = box / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    pol = policy.loads('[guards]\non = ["session-context"]\n[guards.session-context]\nwarn_api_key = false\n')
    start = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(repo)})
    code, out, _ = gate.event("claude-code", start, pol, home=box / "state")
    assert code == 0 and "STATE.md" not in out
    (repo / "STATE.md").write_text("where it is\n")
    code, out, _ = gate.event("claude-code", start, pol, home=box / "state")
    assert code == 0 and "STATE.md" in out


# --- row 9: skill gate -------------------------------------------------------------------------------------------


def test_parity_skill_gate(box):
    pol = policy.loads(
        '[guards]\non = ["skill-gate"]\n[[guards.skill-gate]]\nskill = "taste-gate"\npaths = ["**/*.css"]\n'
    )
    state = box / "state"
    css = call("Write", cwd=box, file_path=str(box / "site" / "a.css"), content="a {}")
    assert verdict(pol, css, state) == "ask"  # the skill hasn't been loaded: a person may let it through once
    assert verdict(pol, call("Write", cwd=box, file_path=str(box / "site" / "a.html"), content="x"), state) == "allow"
    assert verdict(pol, call("Skill", cwd=box, skill="taste-gate"), state) == "allow"  # it is loaded now
    assert verdict(pol, css, state) == "allow"


# --- row 10: spawn cap -------------------------------------------------------------------------------------------


def test_parity_spawn_cap(box):
    pol = policy.loads('[guards]\non = ["spawn-cap"]\n[guards.spawn-cap]\nmax_agents = 1\n')
    state = box / "state"
    agent = {"subagent_type": "worker", "prompt": "go", "run_in_background": True}
    assert verdict(pol, call("Agent", cwd=box, **agent), state) == "allow"
    started = json.dumps(
        {
            "hook_event_name": "PostToolUse",
            "tool_name": "Agent",
            "tool_input": agent,
            "session_id": "s1",
            "cwd": str(box),
        }
    )
    gate.event("claude-code", started, pol, home=state)  # the first agent is running
    assert verdict(pol, call("Agent", cwd=box, **agent), state) == "deny"


# --- row 12: tests are not weakened -------------------------------------------------------------------------------


def test_parity_test_weakening(box):
    (box / "tests").mkdir()
    test_file = box / "tests" / "test_x.py"
    test_file.write_text("import pytest\n\n\ndef test_a():\n    assert 1 + 1 == 2\n")
    pol = policy.loads('[guards]\non = ["test-weakening"]\n')
    skip = call(
        "Edit",
        cwd=box,
        file_path=str(test_file),
        old_string="def test_a():",
        new_string="@pytest.mark.skip\ndef test_a():",
    )
    assert verdict(pol, skip) == "ask"  # a person may allow it once
    more = call(
        "Edit",
        cwd=box,
        file_path=str(test_file),
        old_string="    assert 1 + 1 == 2\n",
        new_string="    assert 1 + 1 == 2\n    assert 2 + 2 == 4\n",
    )
    assert verdict(pol, more) == "allow"


# --- row 15: the phone alert -------------------------------------------------------------------------------------


def test_parity_phone_alert(box, monkeypatch, capsys):
    pol = policy.default_policy()
    conf = json.loads(claude_code.generate(pol, ["ws"], box / "out", box / "w" / "policy.toml")["settings.json"])
    for name in ("Notification", "Stop"):
        commands = [h["command"] for g in conf["hooks"][name] for h in g["hooks"]]
        assert any("notify alert --runner claude-code" in c and c.endswith("|| true") for c in commands), name
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Notification", "message": "x"})))
    assert cli.main(["notify", "alert", "--runner", "claude-code", *paths.pin_of().args()]) == 0  # no channel: silent


# --- the page and these tests agree ------------------------------------------------------------------------------


def rows() -> list[list[str]]:
    found = []
    for line in PAGE.read_text(encoding="utf-8").splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if re.fullmatch(r"\d+", cells[0]) and len(cells) == 5:
            found.append(cells)
    return found


def test_every_hook_row_is_covered_by_a_test_or_says_why_not():
    table = rows()
    assert [r[0] for r in table] == [str(n) for n in range(1, 23)]  # the 22 hooks, in order
    mine = {n for n in globals() if n.startswith("test_parity_")}
    named = set()
    for number, what, ws, status, proof in table:
        assert what and ws, number
        if status == "covered":
            test = proof.strip("`")
            assert test in mine, f"row {number} names {test}, which is not in tests/test_hook_parity.py"
            named.add(test)
        else:
            assert status == "estate (personal)" or re.fullmatch(r"dropped \(reason: .+\)", status), (number, status)
            assert proof == "none" or proof.startswith("none:"), number
    assert named == mine, f"tests with no row on the page: {sorted(mine - named)}"


def test_the_page_names_no_person_host_or_private_setup():
    text = PAGE.read_text(encoding="utf-8")
    assert not re.search(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", text) and "@" not in text
    assert "/home/" not in text and "/Users/" not in text
