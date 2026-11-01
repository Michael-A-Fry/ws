"""The skill-gate guard (ws.guards.skill_gate): a write waits for its skill; the record is per session; the deny asks."""

from __future__ import annotations

import json

import pytest

from ws import approvals, gate, guards, policy
from ws.adapters import claude_code
from ws.guards import Context
from ws.guards import skill_gate as sg

TOML = """
[guards]
on = ["skill-gate"]

[[guards.skill-gate]]
skill = "taste-gate"
paths = ["**/*.css", "**/templates/**"]

[[guards.skill-gate]]
skill = "dataviz"
paths = ["charts/**"]
"""


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return tmp_path


@pytest.fixture
def pol():
    return policy.loads(TOML)


def hook(home, tool, session="s1", **inp):
    return json.dumps({"tool_name": tool, "session_id": session, "cwd": str(home), "tool_input": inp})


def write(home, rel, session="s1"):
    return hook(home, "Write", session, file_path=str(home / rel), content="x")


def check(home, raw, pol, **kw):
    return gate.check("claude-code", raw, pol, home=home / "wshome", **kw)


def prompt(home, pol, text, session="s1"):
    raw = json.dumps({"hook_event_name": "UserPromptSubmit", "session_id": session, "cwd": str(home), "prompt": text})
    return gate.event("claude-code", raw, pol, home=home / "wshome")


def test_it_reads_writes_and_skills_before_a_call_and_prompts():
    mod = guards.get("skill-gate")
    assert (mod.EVENTS, mod.KINDS) == (("pre", "prompt"), ("write", "skill"))


def test_the_policy_loads_a_list_of_rules(pol):
    assert pol.guards.config["skill-gate"] == (
        sg.Rule("taste-gate", ("**/*.css", "**/templates/**")),
        sg.Rule("dataviz", ("charts/**",)),
    )
    assert policy.loads('[guards]\non = ["skill-gate"]\n').guards.config["skill-gate"] == ()


def test_a_write_before_the_skill_is_an_approvable_deny(home, pol):
    reason = check(home, write(home, "site/style.css"), pol)
    assert isinstance(reason, gate.Ask)
    assert "taste-gate" in reason and "style.css" in reason and "a person approves it" in reason


def test_the_finding_is_approvable(home, pol):
    call = claude_code.parse(json.loads(write(home, "a/templates/p.html")))
    ctx = Context("claude-code", "pre", "s1", str(home), {})
    found = sg.judge(call, pol.guards.config["skill-gate"], ctx)
    assert found.action == "deny" and found.approvable is True
    assert "run the taste-gate skill first" in found.reason


def test_a_skill_call_unlocks_the_paths_for_the_rest_of_the_session(home, pol):
    assert check(home, write(home, "a.css"), pol)
    assert check(home, hook(home, "Skill", skill="taste-gate"), pol) is None
    assert check(home, write(home, "a.css"), pol) is None
    assert check(home, write(home, "pages/templates/b.html"), pol) is None
    assert check(home, write(home, "charts/c.py"), pol)  # the other rule's skill has not run


def test_a_slash_prompt_unlocks_too_and_other_prompts_do_not(home, pol):
    assert prompt(home, pol, "make the header blue")[0] == 0
    assert check(home, write(home, "a.css"), pol)
    code, out, err = prompt(home, pol, "/taste-gate the new header")
    assert (code, out, err) == (0, "", "")
    assert check(home, write(home, "a.css"), pol) is None


@pytest.mark.parametrize("text", ["please run /taste-gate", "/taste-gated", "taste-gate", "//taste-gate"])
def test_only_a_prompt_that_starts_with_the_skill_counts(home, pol, text):
    prompt(home, pol, text)
    assert check(home, write(home, "a.css"), pol)


def test_a_plugin_prefixed_skill_counts_both_ways(home, pol):
    prompt(home, pol, "/design:taste-gate")
    assert check(home, write(home, "a.css"), pol) is None
    assert check(home, hook(home, "Skill", "s2", skill="design:dataviz"), pol) is None
    assert check(home, write(home, "charts/x.py", "s2"), pol) is None
    assert check(home, write(home, "a.css", "s2"), pol)  # taste-gate has not run in s2


def test_each_session_has_its_own_record(home, pol):
    check(home, hook(home, "Skill", "a", skill="taste-gate"), pol)
    assert check(home, write(home, "x.css", "a"), pol) is None
    assert check(home, write(home, "x.css", "b"), pol)
    record = json.loads((home / "wshome" / "sessions" / "a.skill-gate.json").read_text())
    assert record == {"skills": ["taste-gate"]}


def test_a_path_no_rule_names_is_never_asked_about(home, pol):
    assert check(home, write(home, "src/app.py"), pol) is None
    assert check(home, write(home, "src/style.scss"), pol) is None
    assert not (home / "wshome" / "sessions").exists()


def test_the_deny_asks_a_person_and_the_approval_is_used_once(home, pol):
    root = home / "wshome" / "approvals"
    raw = write(home, "a.css")
    asked = check(home, raw, pol, approval_root=root)
    assert "needs a person's approval" in asked and "taste-gate" in asked
    ((ident, bound, _),) = approvals.pending(root)
    assert bound == approvals.Bound("claude-code", "guard:skill-gate", str(home / "a.css"), str(home), "s1")
    approvals.grant(root, ident[:12])
    assert check(home, raw, pol, approval_root=root) is None  # approved once
    assert check(home, raw, pol, approval_root=root)  # and that was the one use


def test_a_missing_session_id_is_a_deny_not_a_pass(home, pol):
    raw = json.dumps(
        {"tool_name": "Write", "cwd": str(home), "tool_input": {"file_path": str(home / "a.css"), "content": "x"}}
    )
    with pytest.raises(ValueError):
        check(home, raw, pol)  # the gate turns a raise before a call into a deny


@pytest.mark.parametrize(
    "toml, text",
    [
        ('[guards.skill-gate]\nskill = "x"\npaths = ["a"]', "[[guards.skill-gate]]"),
        ('[[guards.skill-gate]]\npaths = ["a"]', "skill"),
        ('[[guards.skill-gate]]\nskill = "taste gate"\npaths = ["a"]', "skill"),
        ('[[guards.skill-gate]]\nskill = "x"', "paths"),
        ('[[guards.skill-gate]]\nskill = "x"\npaths = []', "paths"),
        ('[[guards.skill-gate]]\nskill = "x"\npaths = [1]', "paths"),
        ('[[guards.skill-gate]]\nskill = "x"\npaths = ["a"]\nwhen = 1', "when"),
    ],
)
def test_a_bad_rule_names_the_key(toml, text):
    with pytest.raises(policy.PolicyError, match=text.replace("[", r"\[")):
        policy.loads('[guards]\non = ["skill-gate"]\n' + toml)


def test_the_generated_settings_watch_skills_and_prompts(home, pol):
    hooks = json.loads(claude_code.generate(pol, ["ws"], home / "out", home / "p.toml")["settings.json"])["hooks"]
    assert "Skill" in hooks["PreToolUse"][0]["matcher"]
    assert any("gate event" in h["command"] for g in hooks["UserPromptSubmit"] for h in g["hooks"])
