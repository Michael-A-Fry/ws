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


# --- rules from the skills' own front matter ---------------------------------------------------------------------


def skill(home, name, text, where=".claude/skills"):
    d = home / "home" / where / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(text, encoding="utf-8")
    return d


def judge_write(home, rel, cfg, session="s1", runner="claude-code"):
    call = claude_code.parse(json.loads(write(home, rel, session)))
    return sg.judge(call, cfg, Context(runner, "pre", session, str(home), {}, home=home / "wshome"))


def run_skill(home, name, cfg, session="s1"):
    call = claude_code.parse(json.loads(hook(home, "Skill", session, skill=name)))
    sg.judge(call, cfg, Context("claude-code", "pre", session, str(home), {}, home=home / "wshome"))


def cfg_of(toml):
    return policy.loads('[guards]\non = ["skill-gate"]\n' + toml).guards.config["skill-gate"]


NONE = cfg_of("")


@pytest.mark.parametrize(
    "front",
    [
        'gates: ["**/*.css", "**/templates/**"]',
        "gates: ['**/*.css', '**/templates/**']",
        'gates:\n  - "**/*.css"\n  - "**/templates/**"',
    ],
)
def test_a_skills_own_gates_guard_its_paths(home, front):
    skill(home, "taste-gate", f"---\nname: taste-gate\n{front}\ndescription: x\n---\nbody\n")
    found = judge_write(home, "site/a.css", NONE)
    assert found.action == "deny" and found.approvable and "run the taste-gate skill first" in found.reason
    assert judge_write(home, "src/app.py", NONE) is None
    run_skill(home, "taste-gate", NONE)
    assert judge_write(home, "site/a.css", NONE) is None


def test_the_skill_name_is_the_folder_when_the_front_matter_has_none(home):
    skill(home, "dataviz", "---\ngates: [charts/**]\n---\n")
    assert "dataviz" in judge_write(home, "charts/a.py", NONE).reason


def test_a_name_that_is_not_a_plain_skill_name_falls_back_to_the_folder(home):
    skill(home, "odd", "---\nname: bad name!\ngates: [a/**]\n---\n")
    assert "odd" in judge_write(home, "a/x", NONE).reason


def test_the_front_matter_name_wins_over_the_folder(home):
    skill(home, "folder-name", "---\nname: real-name\ngates: [charts/**]\n---\n")
    assert "real-name" in judge_write(home, "charts/a.py", NONE).reason


def test_a_skill_with_no_gates_or_no_front_matter_guards_nothing_and_says_nothing(home):
    skill(home, "a", "---\nname: a\n---\nbody\n")
    skill(home, "b", "just text, no front matter\n")
    skill(home, "c", "---\nname: c\ndescription: gates are described here\n---\n")
    assert judge_write(home, "x.css", NONE) is None


def test_a_policy_rule_for_the_same_skill_replaces_the_front_matter_ones(home):
    skill(home, "taste-gate", "---\ngates: ['**/*.css']\n---\n")
    skill(home, "dataviz", "---\ngates: ['charts/**']\n---\n")
    cfg = cfg_of('[[guards.skill-gate]]\nskill = "taste-gate"\npaths = ["**/*.scss"]\n')
    assert judge_write(home, "a.css", cfg) is None  # the policy's paths replace the front matter's
    assert judge_write(home, "a.scss", cfg).action == "deny"
    assert judge_write(home, "charts/x.py", cfg).action == "deny"  # a skill the policy doesn't name still gates
    cfg = cfg_of('[[guards.skill-gate]]\nskill = "design:taste-gate"\npaths = ["**/*.scss"]\n')
    assert judge_write(home, "a.css", cfg) is None  # a plugin prefix is the same skill


def test_skill_dirs_names_the_folders_and_replaces_the_default(home):
    skill(home, "mine", "---\ngates: [m/**]\n---\n", where="stuff/skills")
    skill(home, "theirs", "---\ngates: [t/**]\n---\n")
    cfg = cfg_of('[guards.skill-gate]\nskill_dirs = ["~/stuff/skills"]\n')
    assert cfg == () and cfg.skill_dirs == ("~/stuff/skills",)
    assert judge_write(home, "m/a", cfg).action == "deny"
    assert judge_write(home, "t/a", cfg) is None


def test_skill_dirs_and_rules_sit_in_one_table(home):
    toml = (
        '[guards.skill-gate]\nskill_dirs = ["~/.claude/skills"]\n'
        '[[guards.skill-gate.rules]]\nskill = "taste-gate"\npaths = ["**/*.css"]\n'
    )
    cfg = cfg_of(toml)
    assert cfg == (sg.Rule("taste-gate", ("**/*.css",)),) and cfg.skill_dirs == ("~/.claude/skills",)
    assert judge_write(home, "a.css", cfg).action == "deny"


def test_another_runner_reads_no_skill_folder_unless_the_policy_names_one(home):
    skill(home, "taste-gate", "---\ngates: ['**/*.css']\n---\n")
    assert judge_write(home, "a.css", NONE, runner="opencode") is None
    assert judge_write(home, "a.css", NONE).action == "deny"


@pytest.mark.parametrize(
    "text",
    [
        "---\ngates: nope\n---\n",
        "---\ngates: []\n---\n",
        "---\ngates: [1, 2]\n---\n",
        "---\ngates:\n  deep:\n    - a\n---\n",
    ],
)
def test_a_skill_file_that_will_not_parse_is_skipped_with_one_line_and_never_a_crash(home, text):
    skill(home, "odd", text)
    skill(home, "fine", "---\ngates: [ok/**]\n---\n")
    found = judge_write(home, "src/app.py", NONE)
    assert found.action == "note" and "odd" in found.reason and "fine" not in found.reason
    assert judge_write(home, "src/app.py", NONE) is None  # said once for the session
    assert judge_write(home, "ok/a", NONE).action == "deny"  # the others still gate
    assert judge_write(home, "src/app.py", NONE, session="s2").action == "note"  # a new session hears it again


def test_a_skill_file_that_is_not_text_is_skipped_with_a_line(home):
    d = skill(home, "bin", "x")
    (d / "SKILL.md").write_bytes(b"---\ngates: [a]\n\xff\xfe\n---\n")
    found = judge_write(home, "a", NONE)
    assert found.action == "note" and "bin/SKILL.md can't be read" in found.reason


def test_a_note_never_stops_a_write_and_a_deny_comes_first(home):
    skill(home, "odd", "---\ngates: nope\n---\n")
    skill(home, "taste-gate", "---\ngates: ['**/*.css']\n---\n")
    assert judge_write(home, "a.css", NONE).action == "deny"


def test_a_skill_folder_that_is_a_link_inside_home_is_read(home):
    real = home / "home" / "AI_Workspace" / "skills" / "taste-gate"
    real.mkdir(parents=True)
    (real / "SKILL.md").write_text("---\ngates: ['**/*.css']\n---\n")
    (home / "home" / ".claude" / "skills").mkdir(parents=True)
    (home / "home" / ".claude" / "skills" / "taste-gate").symlink_to(real)
    assert judge_write(home, "a.css", NONE).action == "deny"


def test_the_skills_folder_itself_may_be_a_link_inside_home(home):
    real = home / "home" / "AI_Workspace" / "skills"
    (real / "taste-gate").mkdir(parents=True)
    (real / "taste-gate" / "SKILL.md").write_text("---\ngates: ['**/*.css']\n---\n")
    (home / "home" / ".claude").mkdir()
    (home / "home" / ".claude" / "skills").symlink_to(real)
    assert judge_write(home, "a.css", NONE).action == "deny"


def test_a_link_that_leads_outside_home_is_never_followed(home):
    outside = home / "outside" / "taste-gate"
    outside.mkdir(parents=True)
    (outside / "SKILL.md").write_text("---\ngates: ['**/*.css']\n---\n")
    skills = home / "home" / ".claude" / "skills"
    skills.mkdir(parents=True)
    (skills / "taste-gate").symlink_to(outside)  # the folder leads out
    folder = skills / "linked-file"
    folder.mkdir()
    (folder / "SKILL.md").symlink_to(outside / "SKILL.md")  # the file leads out
    found = judge_write(home, "a.css", NONE)
    assert found.action == "note" and "leads outside HOME" in found.reason
    assert "taste-gate/SKILL.md" in found.reason and "linked-file/SKILL.md" in found.reason


def test_a_skills_folder_that_is_a_link_outside_home_is_never_read(home):
    outside = home / "outside" / "skills"
    (outside / "taste-gate").mkdir(parents=True)
    (outside / "taste-gate" / "SKILL.md").write_text("---\ngates: ['**/*.css']\n---\n")
    (home / "home" / ".claude").mkdir(parents=True)
    (home / "home" / ".claude" / "skills").symlink_to(outside)
    assert judge_write(home, "a.css", NONE) is None


def test_a_skill_dirs_entry_outside_home_is_ignored(home):
    skill(home, "taste-gate", "---\ngates: ['**/*.css']\n---\n", where="../outside")
    cfg = cfg_of(f'[guards.skill-gate]\nskill_dirs = ["{home / "outside"}"]\n')
    assert judge_write(home, "a.css", cfg) is None


def test_a_slash_prompt_unlocks_a_front_matter_gate(home):
    skill(home, "taste-gate", "---\ngates: ['**/*.css']\n---\n")
    prompt(home, policy.loads('[guards]\non = ["skill-gate"]\n'), "/taste-gate go")
    assert judge_write(home, "a.css", NONE) is None


def test_with_nothing_to_watch_nothing_is_recorded(home):
    prompt(home, policy.loads('[guards]\non = ["skill-gate"]\n'), "/taste-gate go")
    assert not (home / "wshome" / "sessions").exists()


@pytest.mark.parametrize(
    "toml, text",
    [
        ('[guards.skill-gate]\nskill_dirs = "x"', "skill_dirs"),
        ("[guards.skill-gate]\nskill_dirs = [1]", "skill_dirs"),
        ('[guards.skill-gate]\nskill_dirs = ["relative/skills"]', "absolute path"),
        ("[guards.skill-gate]\nskill_dirs = []\nrules = 5", r"skill-gate\.rules"),
        ('[guards.skill-gate]\nskill_dirs = []\n[[guards.skill-gate.rules]]\nskill = "x"', "paths"),
    ],
)
def test_a_bad_skill_dirs_or_rules_table_names_the_key(toml, text):
    with pytest.raises(policy.PolicyError, match=text):
        policy.loads('[guards]\non = ["skill-gate"]\n' + toml)
