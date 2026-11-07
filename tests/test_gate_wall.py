"""Allow-with-log in a proven wall (ws.gate, policy.Soften): a command refused only for a part ws cannot read is allowed
when the sandbox is proven up at that call, and every other refusal stays. The matrix is proven or not (and no proof at
all) by each kind of refusal. A forged "proven" state, set by the session, is never read."""

from __future__ import annotations

import io
import json
import sys

import pytest

from ws import cli, gate, jsonl, paths, policy, wallproof

# a refusal the wall covers, and the kinds the allow is logged under
SOFTENED = {
    "cat $X": {"unknown-bare"},
    "echo x >> $D/ws/policy.toml": {"unknown-protected-write", "unknown-deny-read"},
    "cat ${D}/id_rsa": {"unknown-deny-read"},
}
# a refusal that is not about an unknown part: named paths, the launcher rule, stdin-is-code, ws commands, deny_commands
STAYS = {
    "named deny_read path": "cat ~/.ssh/id_rsa",
    "named protected write": "echo x > {policy}",
    "launcher starting a shell": "taskset 1 bash -c 'echo hi'",
    "pipe into a shell": "cat <<< 'x' | bash",
    "stdin is code": "source /dev/stdin <<< 'cat x'",
    "ws command": "ws install --gate launch",
    "deny_commands rule": "git push --force origin main",
    "unknown program": "$PROG arg",
    "eval of unknown text": "eval $CMD",
}
# an unknown part first, then a refusal that is not about one: the later refusal is what is reported once the wall is proven
MIXED = {
    "cat $X; cat ~/.ssh/id_rsa": "deny_read rule",
    "cat $X && git push --force origin main": "deny_commands rule",
    "echo x >> $D/ws/policy.toml; source /dev/stdin <<< 'cat x'": "here-document",
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    for name in ("WS_SANDBOXED", "CLAUDE_PROJECT_DIR", "WS_GATED_SESSION"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / "home").mkdir()
    (tmp_path / "wshome").mkdir()
    monkeypatch.chdir(tmp_path / "home")
    return tmp_path


def payload(cmd, home):
    return json.dumps(
        {"tool_name": "Bash", "tool_input": {"command": cmd}, "session_id": "s1", "cwd": str(home / "home")}
    )


def protect_of(home):
    return (str(home / "wshome" / "policy.toml"), str(home / "wshome"))


def decide(home, cmd, wall=None, pol=None, softened=None):
    pol = pol or policy.default_policy()
    cmd = cmd.replace("{policy}", str(home / "wshome" / "policy.toml"))
    return gate.check("claude-code", payload(cmd, home), pol, protect_of(home), wall=wall, softened=softened)


class Prover:
    def __init__(self, answer=True):
        self.answer, self.asked = answer, 0

    def __call__(self):
        self.asked += 1
        return self.answer


@pytest.mark.parametrize("cmd, kinds", SOFTENED.items(), ids=list(SOFTENED))
def test_a_refusal_for_a_part_ws_cannot_read_is_an_allow_with_its_kinds_when_the_wall_is_proven(home, cmd, kinds):
    assert decide(home, cmd)  # unsandboxed: refused
    seen: list[str] = []
    assert decide(home, cmd, Prover(True), softened=seen) is None
    assert set(seen) == kinds


@pytest.mark.parametrize("cmd", SOFTENED)
def test_the_same_command_with_no_proof_is_refused_with_the_reason_it_has_without_a_wall(home, cmd):
    strict = decide(home, cmd)
    assert strict
    for wall in (Prover(False), None):
        seen: list[str] = []
        assert decide(home, cmd, wall, softened=seen) == strict and seen == []


@pytest.mark.parametrize("name", list(STAYS))
def test_every_other_kind_of_refusal_stays_whether_or_not_the_wall_is_proven(home, name):
    cmd = STAYS[name]
    strict = decide(home, cmd)
    assert strict, name
    for wall in (Prover(True), Prover(False), None):
        seen: list[str] = []
        assert decide(home, cmd, wall, softened=seen) == strict and seen == []


@pytest.mark.parametrize("cmd, later", MIXED.items())
def test_an_unknown_part_does_not_hide_a_refusal_that_comes_after_it(home, cmd, later):
    assert decide(home, cmd)
    for wall in (Prover(True), Prover(False), None):
        assert decide(home, cmd, wall)
    assert later in decide(home, cmd, Prover(True))
    asked = Prover(True)
    decide(home, cmd, asked)
    assert asked.asked == 0  # a refusal that stays is reached before the wall is asked about anything


def test_a_command_with_nothing_unknown_in_it_never_asks_for_the_proof(home):
    wall = Prover(True)
    assert decide(home, "echo hello", wall) is None
    assert decide(home, "cat ~/.ssh/id_rsa", wall)
    assert wall.asked == 0


def test_the_proof_is_asked_for_each_call_and_never_remembered(home):
    wall = Prover(True)
    assert decide(home, "cat $X", wall) is None and wall.asked == 1
    assert decide(home, "cat $X", wall) is None and wall.asked == 2
    wall.answer = False  # the wall goes away between two calls: the next call is read strictly
    assert decide(home, "cat $X", wall) and wall.asked == 3


def test_a_proof_that_fails_to_run_is_not_a_pass(home):
    def broken():
        raise OSError("the probe could not start")

    with pytest.raises(OSError):  # check() raises on what it cannot judge; the hook entry turns that into a deny
        decide(home, "cat $X", broken)


def test_the_policy_switch_turns_the_allow_off(home):
    pol = policy.loads("[gate]\nsandbox = false\n")
    wall = Prover(True)
    assert decide(home, "cat $X", wall, pol)
    assert wall.asked == 0


def test_a_rule_the_wall_cannot_hide_keeps_the_strict_reading_of_a_path_that_could_reach_it(home):
    pol = policy.loads(
        '[gate]\ndeny_read = ["~/.ssh/**", "~/x/*/key"]\n'
    )  # a wildcard in the middle: the matcher's alone
    assert "~/x/*/key" not in policy.wall_rules(pol) and "~/.ssh/**" in policy.wall_rules(pol)
    assert decide(home, "cat $X", Prover(True), pol)  # could be any file, including one the wall does not hide
    assert decide(home, "cat ${D}/key", Prover(True), pol)
    assert decide(home, "cat ${D}/id_rsa", Prover(True), pol) is None  # a rule the wall does hide: still an allow


def test_the_wall_rules_are_the_ones_the_sandbox_block_hides():
    pol = policy.loads('[gate]\ndeny_read = ["~/a/**", "$HOME/b", "~/c/*/d", "/e/f"]\n')
    assert policy.wall_rules(pol) >= {"~/a/**", "$HOME/b", "/e/f"} and "~/c/*/d" not in policy.wall_rules(pol)
    assert policy.sandbox_read_paths(pol)[:3] == ["~/a", "~/b", "/e/f"]


# --- the hook entry: `ws gate check --wall` -------------------------------------------------------------------------------


def hook(home, monkeypatch, capsys, cmd, *flags):
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload(cmd, home)))
    code = cli.main(["gate", "check", "--runner", "claude-code", *flags, *paths.pin_of().args()])
    return code, capsys.readouterr().err


def proof(monkeypatch, ok=True, kinds=()):
    asked: list[int] = []

    def prove(*a, **k):
        asked.append(1)
        return wallproof.Proof(ok, tuple(kinds))

    monkeypatch.setattr(wallproof, "prove", prove)
    return asked


def test_the_hook_allows_and_logs_the_kind_when_it_is_told_the_wall_is_up_and_the_proof_passes(
    home, monkeypatch, capsys
):
    proof(monkeypatch, True)
    code, err = hook(home, monkeypatch, capsys, "cat $SECRET_FILE", "--wall")
    assert code == 0 and "allowed in a proven sandbox: unknown-bare" in err
    log = jsonl.read(paths.state_dir() / "logs" / gate.ALLOWED_LOG)
    assert len(log) == 1 and log[0]["kinds"] == ["unknown-bare"] and log[0]["runner"] == "claude-code"
    assert log[0]["schema"] == gate.ALLOWED_SCHEMA and "ts" in log[0] and log[0]["session"] == "s1"
    assert "SECRET_FILE" not in json.dumps(log)  # the kind, never the command


def test_a_refused_call_leaves_no_allowed_line(home, monkeypatch, capsys):
    proof(monkeypatch, True)
    code, _ = hook(home, monkeypatch, capsys, "cat $X; cat ~/.ssh/id_rsa", "--wall")
    assert code == gate.DENY
    assert not (paths.state_dir() / "logs" / gate.ALLOWED_LOG).exists()


def test_the_hook_is_strict_when_the_proof_fails(home, monkeypatch, capsys):
    asked = proof(monkeypatch, False, [wallproof.HIDDEN_READABLE])
    code, err = hook(home, monkeypatch, capsys, "cat $X", "--wall")
    assert code == gate.DENY and "ws cannot read" in err and asked == [1]


def test_the_hook_is_strict_when_the_proof_itself_blows_up(home, monkeypatch, capsys):
    def boom(*a, **k):
        raise RuntimeError("probe")

    monkeypatch.setattr(wallproof, "prove", boom)
    code, err = hook(home, monkeypatch, capsys, "cat $X", "--wall")
    assert code == gate.DENY and "blocked to be safe" in err


def test_the_hook_is_strict_without_wall_in_its_command_whatever_the_environment_says(home, monkeypatch, capsys):
    asked = proof(monkeypatch, True)
    monkeypatch.setenv("WS_SANDBOXED", "1")  # the session's environment: a project's settings can set it
    monkeypatch.setenv("WS_WALL_PROVEN", "1")
    (paths.state_dir() / "logs").mkdir(parents=True, exist_ok=True)
    for forged in (
        "proven",
        "wall-proven",
        "wall.json",
        "sandboxed",
    ):  # files a session might plant for the hook to find
        (paths.state_dir() / forged).write_text("1")
        (paths.state_dir() / "logs" / forged).write_text("1")
    code, _ = hook(home, monkeypatch, capsys, "cat $X")
    assert code == gate.DENY and asked == []


def test_the_hook_ignores_a_forged_proven_marker_in_the_environment_even_with_wall(home, monkeypatch, capsys):
    asked = proof(monkeypatch, False)
    monkeypatch.setenv("WS_SANDBOXED", "1")
    monkeypatch.setenv("WS_WALL_PROVEN", "1")
    code, _ = hook(home, monkeypatch, capsys, "cat $X", "--wall")
    assert code == gate.DENY and asked == [1]  # the proof was asked for, and it said no


def test_settings_that_say_they_carry_no_sandbox_block_keep_the_hook_strict(home, monkeypatch, capsys):
    asked = proof(monkeypatch, True)
    monkeypatch.setenv("WS_SANDBOXED", "0")
    code, _ = hook(home, monkeypatch, capsys, "cat $X", "--wall")
    assert code == gate.DENY and asked == []


def test_the_policy_switch_keeps_the_hook_strict_even_with_wall(home, monkeypatch, capsys):
    asked = proof(monkeypatch, True)
    (home / "wshome" / "policy.toml").write_text("[gate]\nsandbox = false\n")
    code, _ = hook(home, monkeypatch, capsys, "cat $X", "--wall")
    assert code == gate.DENY and asked == []


# --- session start ---------------------------------------------------------------------------------------------------


START = json.dumps({"hook_event_name": "SessionStart", "session_id": "s1"})


def test_session_start_says_the_wall_is_proven(home):
    code, out, _ = gate.event("claude-code", START, policy.default_policy(), wall=lambda: wallproof.Proof(True))
    assert code == 0 and "sandbox: the wall is proven" in out


def test_session_start_names_the_kind_of_a_half_pass_and_never_a_path(home):
    bad = wallproof.Proof(False, (wallproof.PROTECTED_WRITABLE,))
    code, out, _ = gate.event("claude-code", START, policy.default_policy(), wall=lambda: bad)
    assert code == 0 and "not proven here (a ws path can still be written)" in out and "strictly" in out
    assert str(home) not in out and "WS012" in out


def test_session_start_says_nothing_about_the_wall_when_the_policy_turns_it_off_or_the_hook_has_no_wall(home):
    asked: list[int] = []
    wall = lambda: asked.append(1) or wallproof.Proof(True)
    _, out, _ = gate.event("claude-code", START, policy.loads("[gate]\nsandbox = false\n"), wall=wall)
    assert "sandbox" not in out
    _, out, _ = gate.event("claude-code", START, policy.default_policy())
    assert "sandbox" not in out and asked == []
