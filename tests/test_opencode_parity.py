"""OpenCode gated as fully as Claude Code: the guards run on OpenCode's events through the thin client, the native deny
and ask rules are the floor, and the same neutral call gets the same verdict whichever runner sent it.

Each row of `rows` runs one scenario twice, once as the payload Claude Code sends and once as the payload the generated
OpenCode shim sends, in a state folder of its own each, and the verdicts must be equal and not vacuous."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import types
from pathlib import Path

import pytest

from ws import cli, gate, guards, paths, policy, userconfig
from ws.adapters import claude_code, opencode

ROOT = Path(__file__).resolve().parents[1]
needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.chdir(home)
    return home


# --- the two runners' payloads for one neutral step ----------------------------------------------------------------


CC_EVENT = {v: k for k, v in claude_code.EVENT_NAMES.items()}
OC_EVENT = {v: k for k, v in opencode.EVENT_NAMES.items()}


def _cc_tool(step: dict) -> tuple[str, dict]:
    kind = step["kind"]
    if kind == "command":
        return "Bash", {"command": step["command"]}
    if kind == "write" and "old" in step:
        return "Edit", {"file_path": step["path"], "old_string": step["old"], "new_string": step["new"]}
    if kind == "write":
        return "Write", {"file_path": step["path"], "content": step["content"]}
    if kind == "agent":
        return "Agent", {"subagent_type": "worker", "prompt": "go", "run_in_background": step.get("background", False)}
    if kind == "skill":
        return "Skill", {"skill": step["name"]}
    raise AssertionError(kind)


def _oc_tool(step: dict) -> tuple[str, dict]:
    kind = step["kind"]
    if kind == "command":
        return "bash", {"command": step["command"], "description": "x"}
    if kind == "write" and "old" in step:
        return "edit", {"filePath": step["path"], "oldString": step["old"], "newString": step["new"]}
    if kind == "write":
        return "write", {"filePath": step["path"], "content": step["content"]}
    if kind == "agent":
        args = {"subagent_type": "worker", "prompt": "go", "description": "d"}
        return "task", {**args, **({"background": True} if step.get("background") else {})}
    if kind == "skill":
        return "skill", {"name": step["name"]}
    raise AssertionError(kind)


def payload(runner: str, step: dict, cwd: Path) -> str:
    """The hook payload `runner` sends for a neutral step ({event, kind, ...}); for OpenCode, the one the shim builds."""
    ev = step["event"]
    cc = runner == "claude-code"
    out = {"session_id": "s1", "cwd": str(cwd), "hook_event_name": (CC_EVENT if cc else OC_EVENT)[ev]}
    if ev in ("pre", "post"):
        tool, args = (_cc_tool if cc else _oc_tool)(step)
        out |= {"tool_name": tool, "tool_input": args} if cc else {"tool": tool, "args": args}
        if ev == "post" and not cc:  # what the shim adds, so a guard that reads the tool input sees the same words
            out["tool_input"] = {**args, **({"run_in_background": True} if args.get("background") else {})}
    elif ev == "prompt":
        out["prompt"] = step["prompt"]
    elif ev == "stop":
        out["last_assistant_message"] = step["last"]
    return json.dumps(out)


def verdict(runner: str, step: dict, pol, cwd: Path, state: Path):
    """What the gate says for a step: `gate.check` before a call, `gate.event` for every other event."""
    raw = payload(runner, step, cwd)
    if step["event"] == "pre":
        reason = gate.check(runner, raw, pol, home=state)
        return ("allow" if reason is None else "ask" if isinstance(reason, gate.Ask) else "deny", str(reason or ""))
    return gate.event(runner, raw, pol, home=state)


def kinds_of(seen) -> list:
    """The verdicts reduced to what a row states: allow, deny or ask for a check; the exit code for an event, or "text"
    when a quiet exit printed something."""
    return [
        v[0] if v[0] in ("allow", "deny", "ask") else "text" if v[0] == 0 and (v[1] or v[2]) else v[0] for v in seen
    ]


# --- the rows: one scenario for each event of each guard -------------------------------------------------------------


def S(event: str, kind: str = "", **kw) -> dict:
    return {"event": event, "kind": kind, **kw}


TEST_OLD = "import pytest\n\n\ndef test_a():\n    assert 1 + 1 == 2\n\n\ndef test_b():\n    assert 2 + 2 == 4\n"
SKILL_RULES = '[[guards.skill-gate]]\nskill = "taste-gate"\npaths = ["**/*.css"]\n'
CAP = "[guards.spawn-cap]\nmax_agents = 1\n"


def git(cwd, *args) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z", "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"},
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def plant(tmp: Path) -> str:
    """The folders a row works in: `work` (state files, a test file, a file that does not compile, a repository with one
    commit), `main` (a checkout with a linked worktree). Returns the short id of the commit."""
    work = tmp / "work"
    (work / "tests").mkdir(parents=True)
    (work / "STATE.md").write_text("state\n")
    (work / "tests" / "test_x.py").write_text(TEST_OLD)
    (work / "bad.py").write_text("def f(:\n")
    (tmp / "main" / ".git" / "worktrees" / "wt").mkdir(parents=True)
    if shutil.which("git") is None:
        return "abc1234def"
    git(work, "init", "-q", "-b", "main")
    git(work, "add", "STATE.md")
    git(work, "commit", "-q", "-m", "first")
    return git(work, "rev-parse", "HEAD")[:10]


def rows(tmp: Path, sha: str = "abc1234def") -> dict[tuple[str, str], list[tuple]]:
    """{(guard, event): [(policy text after the guard list, steps, the verdicts expected, the folder)]}. No I/O. A deny a
    person may approve comes back as "ask" here, where no approvals folder is given."""
    work, main = tmp / "work", tmp / "main"
    css = str(work / "site.css")
    test_file = str(work / "tests" / "test_x.py")
    bg = S("post", "agent", background=True)
    return {
        ("bulk-staging", "pre"): [
            ("", [S("pre", "command", command="git add -A"), S("pre", "command", command="git add src/a.py")],
             ["deny", "allow"], work)
        ],
        ("checkout-pin", "pre"): [
            ("", [S("pre", "command", command="git checkout feature"), S("pre", "command", command="git status")],
             ["ask", "allow"], main)
        ],
        ("claims", "pre"): [],  # OpenCode has no send-message tool: nothing can reach this event there
        ("claims", "stop"): [("", [S("stop", last=f"merged {sha} into main")], [0], work)],
        ("dev-server", "pre"): [
            ("", [S("pre", "command", command="python3 -m http.server"), S("pre", "command", command="ls")],
             ["ask", "allow"], work)
        ],
        ("record-calls", "pre"): [("", [S("pre", "command", command="ls")], ["allow"], work)],
        ("session-context", "start"): [("", [S("start")], ["text"], work)],
        ("skill-gate", "pre"): [
            (SKILL_RULES,
             [S("pre", "write", path=css, content="a {}"), S("pre", "skill", name="taste-gate"),
              S("pre", "write", path=css, content="a {}")],
             ["ask", "allow", "allow"], work)
        ],
        ("skill-gate", "prompt"): [
            (SKILL_RULES, [S("prompt", prompt="/taste-gate go"), S("pre", "write", path=css, content="a {}")],
             [0, "allow"], work)
        ],
        ("spawn-cap", "pre"): [(CAP, [bg, S("pre", "agent", background=True)], [0, "deny"], work)],
        ("spawn-cap", "post"): [(CAP, [S("pre", "agent"), bg, S("pre", "agent")], ["allow", 0, "deny"], work)],
        ("spawn-cap", "subagent-stop"): [(CAP, [bg, S("subagent-stop"), S("pre", "agent")], [0, 0, "allow"], work)],
        ("syntax-check", "post"): [
            ("", [S("post", "write", path=str(work / "bad.py"), content="def f(:\n")], [2], work)
        ],
        ("test-weakening", "pre"): [
            ("",
             [S("pre", "write", path=test_file, old="def test_a():", new="@pytest.mark.skip\ndef test_a():"),
              S("pre", "write", path=test_file, old="== 2", new="== 2  # same")],
             ["ask", "allow"], work)
        ],
    }  # fmt: skip


ROW_KEYS = list(rows(Path("/x")))
ROW_PARAMS = [
    pytest.param(g, e, id=f"{g}-{e}", marks=[needs_git] if (g, e) == ("claims", "stop") else []) for g, e in ROW_KEYS
]


def test_every_guard_has_a_parity_row():
    for name in guards.names():
        for event in guards.get(name).EVENTS:
            assert (name, event) in ROW_KEYS, f"guard {name!r} has no row for its {event!r} event in `rows`"
    for name, event in ROW_KEYS:
        assert event in guards.get(name).EVENTS, f"stale row {name}/{event}"


@pytest.mark.parametrize(("guard", "event"), ROW_PARAMS)
def test_guard_verdict_matches_claude_code(guard, event, tmp_path, box):
    table = rows(tmp_path, plant(tmp_path))[(guard, event)]
    if not table:  # no scenario: OpenCode cannot reach this event for the guard, and the adapter names the guard
        assert guard in opencode.UNSUPPORTED_GUARDS
    for n, (extra, steps, expect, cwd) in enumerate(table):
        pol = policy.loads(f'[guards]\non = ["{guard}"]\n{extra}')
        seen = {}
        for runner in ("claude-code", "opencode"):
            state = tmp_path / f"state-{n}-{runner}"
            state.mkdir()
            seen[runner] = [verdict(runner, s, pol, cwd, state) for s in steps]
        assert seen["opencode"] == seen["claude-code"], f"{guard}/{event}: the runners disagree"
        assert kinds_of(seen["opencode"]) == expect, f"{guard}/{event}: {seen['opencode']}"
        if guard in ("record-calls", "claims"):  # what the guard keeps is the same too, but for the runner's name
            log = {"record-calls": "gate-calls.jsonl", "claims": "claims.jsonl"}[guard]
            kept = {}
            for runner in ("claude-code", "opencode"):
                lines = (tmp_path / f"state-{n}-{runner}" / "logs" / log).read_text().splitlines()
                assert len(lines) == 1, (guard, runner, lines)
                kept[runner] = json.loads(lines[0])
                assert kept[runner]["runner"] == runner
            if guard == "claims":
                assert kept["opencode"]["claims"] == kept["claude-code"]["claims"] and kept["opencode"]["claims"]


def test_the_guard_whose_pre_event_opencode_cannot_reach_is_named():
    assert "message" not in opencode.PRODUCES  # OpenCode has no send-message tool
    assert "claims" in opencode.UNSUPPORTED_GUARDS
    why = opencode.guard_gaps(guards.get("claims"))
    assert why and why[0].startswith("pre") and "message" in why[0]
    for name in guards.names():
        if name != "claims":
            assert name not in opencode.UNSUPPORTED_GUARDS, name


def test_parse_makes_the_kinds_the_guards_read():
    args = {"prompt": "p", "description": "d", "subagent_type": "worker"}
    task = opencode.parse({"tool": "task", "args": args})
    cc = claude_code.parse({"tool_name": "Task", "tool_input": args})
    assert (task.kind, task.value) == (cc.kind, cc.value) == ("agent", "worker")
    assert dict(task.data) | {"tool": "Task"} == dict(cc.data)
    skill = opencode.parse({"tool": "skill", "args": {"name": "taste-gate"}})
    cc_skill = claude_code.parse({"tool_name": "Skill", "tool_input": {"skill": "taste-gate"}})
    assert (skill.kind, skill.value, skill.data["name"]) == (cc_skill.kind, cc_skill.value, "taste-gate")
    edit = opencode.parse({"tool": "edit", "args": {"filePath": "/a", "oldString": "x", "newString": "y"}})
    assert (edit.kind, edit.value, edit.data["old"], edit.data["new"]) == ("write", "/a", "x", "y")
    assert opencode.parse({"tool": "write", "args": {"filePath": "/a", "content": "c"}}).data["content"] == "c"
    edits = [{"oldString": "a", "newString": "b"}]
    assert opencode.parse({"tool": "multiedit", "args": {"filePath": "/a", "edits": edits}}).data["edits"] == (
        {"old": "a", "new": "b"},
    )
    assert opencode.parse({"tool": "no_such_tool", "args": {}}) is None
    assert opencode.parse({"tool": "mcp_thing", "args": {}}) is None  # the MCP path is not changed here
    assert set(opencode.PRODUCES) == {"command", "read", "search", "write", "tool", "agent", "skill"}


def test_every_tool_in_the_kind_map_parses_to_its_kind():
    sample = {"filePath": "/a", "path": "/a", "command": "ls", "patchText": "*** Update File: /a", "prompt": "p",
              "name": "n", "content": "c", "oldString": "a", "newString": "b", "edits": []}  # fmt: skip
    for kind, tools in opencode.KIND_TOOLS.items():
        for tool in tools:
            got = opencode.parse({"tool": tool, "args": dict(sample)})
            assert {c.kind for c in (got if isinstance(got, list) else [got])} == {kind}, tool


def test_new_guard_needs_no_edit_here(monkeypatch):
    def fake(name, events, kinds):
        mod = types.ModuleType(name)
        mod.NAME, mod.EVENTS, mod.KINDS = name, events, kinds
        mod.judge = lambda call, cfg, ctx: None
        return mod

    found = dict(guards.all_guards())
    found["fake-post"] = fake("fake-post", ("post", "stop"), ("write",))
    found["fake-skill"] = fake("fake-skill", ("pre", "prompt"), ("skill", "agent"))
    found["fake-message"] = fake("fake-message", ("pre",), ("message",))
    monkeypatch.setattr(guards, "all_guards", lambda: found)
    unsupported = opencode.unsupported_guards()
    assert "fake-message" in unsupported
    assert "fake-post" not in unsupported and "fake-skill" not in unsupported
    found["fake-new-event"] = fake("fake-new-event", ("compaction",), ())  # an event this runner has no name for
    assert "fake-new-event" in opencode.unsupported_guards()


def test_the_event_names_cover_every_event_a_guard_can_ask_for():
    assert set(opencode.EVENT_NAMES.values()) == set(guards.EVENTS)
    assert opencode.EVENT_NAMES["session.idle"] == "stop"
    assert opencode.EVENT_NAMES["session.idle.child"] == "subagent-stop"


# --- the native floor ------------------------------------------------------------------------------------------------


def native(text: str, tmp: Path) -> dict:
    files = opencode.generate(policy.loads(text), ["ws"], tmp / "out", tmp / "policy.toml", pin=paths.pin_of())
    return json.loads(files[opencode.CONFIG])["permission"]


def both(tool: str, name: str, **args) -> tuple[dict, dict]:
    """One call as each runner sends it: Claude Code's tool name and input keys, OpenCode's."""
    return {"tool_name": tool, "tool_input": args["cc"]}, {"tool": name, "args": args["oc"]}


DENY_KINDS = {
    "deny_commands": (
        '[gate]\ndeny_commands = ["make deploy"]\n',
        both("Bash", "bash", cc={"command": "make deploy"}, oc={"command": "make deploy"}),
        lambda p: p["bash"]["make deploy"] == "deny",
    ),
    "deny_read": (
        '[gate]\ndeny_read = ["~/sealed/**"]\n',
        both("Read", "read", cc={"file_path": "~/sealed/a"}, oc={"filePath": "~/sealed/a"}),
        lambda p: p["read"]["~/sealed/**"] == "deny" and p["read"]["**/sealed/**"] == "deny",
    ),
    "deny_tools": (
        '[gate]\ndeny_tools = ["web_fetch"]\n',
        both("WebFetch", "webfetch", cc={"url": "https://example.com"}, oc={"url": "https://example.com"}),
        lambda p: p["webfetch"] == "deny",
    ),
    "protected_write": (
        "",
        both("Write", "write", cc={"file_path": "/pol/policy.toml", "content": "x"},
             oc={"filePath": "/pol/policy.toml", "content": "x"}),
        lambda p: any(v == "deny" for k, v in p["edit"].items() if k.endswith("policy.toml")),
    ),
    "ask_commands": (
        '[gate]\nask_commands = ["make release*"]\n',
        both("Bash", "bash", cc={"command": "make release now"}, oc={"command": "make release now"}),
        lambda p: p["bash"]["make release*"] == "ask",
    ),
}  # fmt: skip


@pytest.mark.parametrize("name", list(DENY_KINDS))
def test_deny_kind_matches_claude_code(name, tmp_path, box):
    text, (cc_payload, oc_payload), carried = DENY_KINDS[name]
    pol = policy.loads(text)
    base = {"session_id": "s1", "cwd": str(tmp_path)}
    cc = gate.check("claude-code", json.dumps(cc_payload | base), pol, ("/pol",), home=tmp_path / "s1")
    oc = gate.check("opencode", json.dumps(oc_payload | base), pol, ("/pol",), home=tmp_path / "s2")
    assert cc is not None and oc is not None, f"{name}: a runner lets it through"
    assert type(cc) is type(oc) and str(cc) == str(oc), (cc, oc)
    assert isinstance(oc, gate.Ask) == (name == "ask_commands")  # a request for a person's yes, not a refusal
    assert carried(native(text, tmp_path)), f"{name}: OpenCode's own rules do not carry it"


def test_rule_order_is_allow_ask_deny(tmp_path, box):
    text = (
        '[gate]\ndeny_commands = ["git push --force*", "make deploy"]\n'
        'ask_commands = ["git push*", "make release*", "make deploy"]\n'
        'deny_read = ["~/sealed/**"]\n'
    )
    perm = native(text, tmp_path)
    bash = list(perm["bash"].items())
    values = [v for _, v in bash]
    assert bash[0] == ("*", "allow") and set(values) == {"allow", "ask", "deny"}
    assert "ask" not in values[values.index("deny") :], bash  # every ask before the first deny
    assert perm["bash"]["make deploy"] == "deny"  # asked and denied: denied, once
    assert [k for k, v in bash if v == "ask"] == ["git push*", "make release*"]
    for tool in ("read", "grep", "glob", "list", "edit"):
        assert next(iter(perm[tool].items())) == ("*", "allow"), tool
        assert set(list(perm[tool].values())[1:]) == {"deny"}, tool


def test_a_pattern_only_the_hook_can_read_is_not_a_native_ask(tmp_path, box):
    perm = native('[gate]\nask_commands = ["make a | tee x", "make b*"]\n', tmp_path)
    assert "make a | tee x" not in perm["bash"] and perm["bash"]["make b*"] == "ask"


def test_a_policy_with_no_ask_commands_writes_no_native_ask(tmp_path, box):
    assert "ask" not in native("", tmp_path)["bash"].values()


def test_skills_folders_are_edit_denied(tmp_path, box):
    perm = native("", tmp_path)["edit"]
    for folder in (
        box / ".config" / "opencode" / "skills",
        box / ".config" / "opencode" / "skill",
        box / ".claude" / "skills",
        box / ".agents" / "skills",
    ):
        f = str(folder)
        tail = "/".join(folder.parts[-2:])
        assert perm[f] == perm[f + "/**"] == "deny", f
        for form in (f"../{tail}", f"../{tail}/**", f"../**/{tail}", f"../**/{tail}/**"):
            assert perm[form] == "deny", form  # how OpenCode sees a folder outside the project
    assert next(iter(perm.items())) == ("*", "allow")


def test_a_projects_own_skills_folders_are_not_caught_by_the_home_level_rules(tmp_path, box):
    """A bare `**/.claude/skills` would deny a project's own; the rules for the home folders start with `../`. The
    project's `.opencode/skills` (the one OpenCode reads itself) is denied in the project and in a folder below it."""
    perm = native("", tmp_path)["edit"]
    for own in (".claude/skills", ".agents/skills"):
        inside = [k for k in perm if own in k and not k.startswith(("/", "../"))]
        assert not inside, (own, inside)
    for folder in (".opencode/skills", ".opencode/skill"):
        for form in (folder, f"{folder}/**", f"**/{folder}", f"**/{folder}/**"):
            assert perm[form] == "deny", form


def test_the_skills_folders_follow_the_config_home_the_environment_names(tmp_path, box, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg"))
    assert native("", tmp_path)["edit"][str(tmp_path / "xdg" / "opencode" / "skills")] == "deny"


def test_a_policies_key_is_emitted_only_when_supported(tmp_path, box, monkeypatch):
    def written() -> dict:
        files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "p.toml", pin=paths.pin_of())
        return json.loads(files[opencode.CONFIG])

    assert opencode.POLICIES_SINCE is None, "set it only to a version whose schema has a `policies` permission key"
    assert "policies" not in written()
    monkeypatch.setattr(opencode, "POLICIES_SINCE", "99.0.0")  # both arms read the constant, so each can fail
    assert "policies" in written()
    doc = (ROOT / "docs" / "reference" / "gate.md").read_text(encoding="utf-8")
    assert "policies" in doc and "1.19.4" in doc  # the page says which version was checked and what it found


# --- the shim ---------------------------------------------------------------------------------------------------------


def shim_for(text: str, tmp: Path) -> str:
    files = opencode.generate(policy.loads(text), ["ws"], tmp / "out", tmp / "policy.toml", pin=paths.pin_of())
    return files[opencode.PLUGIN]


def js_const(shim: str, name: str):
    return json.loads(shim.split(f"const {name} = ")[1].split("\n")[0])


def test_the_shim_carries_an_event_client_beside_the_check_one(tmp_path, box):
    shim = shim_for('[guards]\non = ["syntax-check"]\n', tmp_path)
    assert "__EVENT__" not in shim and "__GOVERNED__" not in shim and "__ASKS__" not in shim
    event, check = js_const(shim, "EVENT"), js_const(shim, "CHECK")
    assert event[:4] == check[:4] and event[4] == "event" and check[4] == "check" and event[5] == check[5]
    assert event[6:11] == ["--", "ws", "gate", "event", "--runner"] and event[11] == "opencode"


def test_the_shim_governs_a_tool_only_when_a_guard_that_is_on_reads_its_kind(tmp_path, box):
    plain = set(js_const(shim_for("", tmp_path), "GOVERNED"))
    assert "task" not in plain and "skill" not in plain and {"bash", "read", "edit", "patch", "webfetch"} <= plain
    both_on = set(js_const(shim_for('[guards]\non = ["spawn-cap", "skill-gate"]\n', tmp_path), "GOVERNED"))
    assert {"task", "skill"} <= both_on and plain <= both_on
    assert set(js_const(shim_for('[guards]\non = ["syntax-check"]\n', tmp_path), "POST_TOOLS")) >= {"edit", "write"}
    assert js_const(shim_for("[guards]\non = []\n", tmp_path), "POST_TOOLS") == []


def test_the_shim_runs_only_the_events_a_guard_that_is_on_wants(tmp_path, box):
    assert js_const(shim_for("[guards]\non = []\n", tmp_path), "ADVICE") == []
    some = shim_for('[guards]\non = ["session-context", "syntax-check"]\n', tmp_path)
    assert set(js_const(some, "ADVICE")) == {"start", "post"}
    every = shim_for(f"[guards]\non = {json.dumps(list(guards.names()))}\n", tmp_path)
    assert set(js_const(every, "ADVICE")) == {"start", "prompt", "post", "stop", "subagent-stop"}


def test_the_check_keeps_its_long_bound_and_advice_gets_a_short_one(tmp_path, box):
    from ws import gateclient

    shim = shim_for('[guards]\non = ["syntax-check"]\n', tmp_path)
    assert f"timeout: {claude_code.HOOK_TIMEOUT * 1000}," in shim  # the check: the same clock as Claude Code's hook
    assert claude_code.HOOK_TIMEOUT > gateclient.WORST_CASE_S
    ms = int(shim.split("const ADVICE_MS = ")[1].split()[0])
    assert (
        0 < ms <= 15000 < claude_code.HOOK_TIMEOUT * 1000
    )  # advice never holds the runner up as long as the check may


def test_the_session_and_folder_in_every_payload_come_from_the_hook_input(tmp_path, box):
    shim = shim_for('[guards]\non = ["syntax-check", "session-context", "skill-gate", "claims"]\n', tmp_path)
    assert "session_id: input.sessionID" in shim
    assert 'resolve(directory ?? process.cwd(), output.args?.workdir ?? ".")' in shim
    # the payload is built from named fields: the tool's arguments only ever sit under `args` and `tool_input`
    assert "...input.args" not in shim and "...output.args" not in shim and "...args," not in shim


def test_a_payload_the_gate_cannot_read_is_a_deny_before_a_call_and_one_line_on_an_event(tmp_path, box):
    pin = paths.pin_of().args()
    for junk in ("not json", "[]", json.dumps({"hook_event_name": "tool.execute.before", "tool": "bash"})):
        assert gate.run_hook("check", ["--runner", "opencode", *pin], junk)[0] == gate.DENY, junk
        code, out, err = gate.run_hook("event", ["--runner", "opencode", *pin], junk)
        assert code == 0 and not out and err.count("\n") == 1, (junk, code, out, err)


def test_a_shim_without_an_event_client_has_nothing_to_run_and_does_not_fail():
    src = opencode.SOURCE
    assert "const EVENT = /*__EVENT__*/ []" in src
    assert "!EVENT.length" in src  # an old file, or an unfilled one, runs no advice and says nothing


# --- the merge into the person's own opencode.json --------------------------------------------------------------------


def opencode_file(box: Path) -> Path:
    return box / ".config" / "opencode" / "opencode.json"


def install(capsys, box: Path):
    code = cli.main(["install", "--yes", "--runner", "opencode", "--notes", str(box / "Notes")])
    out = capsys.readouterr().out
    assert code == 0, out
    return json.loads(opencode_file(box).read_text()), out


def edit_policy(box: Path, old: str, new: str):
    pol = box / ".ws" / "policy.toml"
    text = pol.read_text(encoding="utf-8")
    assert old in text, old
    pol.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_deny_beats_ask_after_merge(box, capsys):
    f = opencode_file(box)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"model": "x/y", "permission": {"bash": {"*": "allow", "ls *": "allow"}}}) + "\n")
    install(capsys, box)
    edit_policy(box, "ask_commands = []", 'ask_commands = ["git push*"]')
    edit_policy(box, "deny_commands = [", 'deny_commands = [\n  "git push --force*",')
    bash = list(install(capsys, box)[0]["permission"]["bash"].items())
    assert bash[:2] == [("*", "allow"), ("ls *", "allow")]  # yours first and untouched
    values = [v for _, v in bash]
    assert "ask" in values and "ask" not in values[values.index("deny") :], bash
    # a later change of policy asks for a broader pattern: a new key goes to the end of the block, so ws puts it ahead
    edit_policy(box, 'ask_commands = ["git push*"]', 'ask_commands = ["git push*", "git *"]')
    bash = list(install(capsys, box)[0]["permission"]["bash"].items())
    values = [v for _, v in bash]
    assert values.count("ask") == 2 and "ask" not in values[values.index("deny") :], bash
    assert bash[:2] == [("*", "allow"), ("ls *", "allow")]
    assert "0 changes" in install(capsys, box)[1]  # and it settles


def test_an_ask_never_lands_after_a_deny_of_yours_that_it_overlaps(box, capsys):
    f = opencode_file(box)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"permission": {"bash": {"git push --force*": "deny"}}}) + "\n")
    (box / ".ws").mkdir()
    (box / ".ws" / "policy.toml").write_text(
        policy.default_toml().replace("ask_commands = []", 'ask_commands = ["git push*", "make release*"]'),
        encoding="utf-8",
    )
    bash = install(capsys, box)[0]["permission"]["bash"]
    assert next(iter(bash.items())) == ("git push --force*", "deny")
    assert "git push*" not in bash  # it would sit after yours and loosen it; the hook still asks
    assert bash["make release*"] == "ask"  # nothing of yours overlaps this one


def test_the_ask_rules_are_recorded_and_come_out_on_uninstall(box, capsys):
    f = opencode_file(box)
    f.parent.mkdir(parents=True)
    f.write_text(json.dumps({"permission": {"bash": {"*": "allow"}}}, indent=2) + "\n")
    original = f.read_bytes()
    (box / ".ws").mkdir()
    (box / ".ws" / "policy.toml").write_text(
        policy.default_toml().replace("ask_commands = []", 'ask_commands = ["git push*"]'), encoding="utf-8"
    )
    assert install(capsys, box)[0]["permission"]["bash"]["git push*"] == "ask"
    cli.main(["uninstall", "--yes"])
    capsys.readouterr()
    assert f.read_bytes() == original


def test_status_names_only_the_guards_opencode_cannot_run(box, capsys):
    install(capsys, box)
    edit_policy(box, "on = [", 'on = ["claims", ')
    status = userconfig.check(dict(os.environ))
    assert "the guard `claims` isn't run by opencode" in status[1]
    assert "syntax-check" not in status[1] and "session-context" not in status[1]


# --- the asks file and the permission events --------------------------------------------------------------------------


def test_the_shim_writes_ask_lines_in_the_format_ws_home_reads(tmp_path, box):
    shim = shim_for("", tmp_path)
    assert js_const(shim, "ASKS") == str(box / ".claude-usage" / "asks" / "asks.jsonl")
    for needle in (
        "permission.asked",
        "permission.replied",
        'event: "added"',
        'event: "answered"',
        'source: "permission"',
    ):
        assert needle in shim, needle


def test_the_asks_path_follows_the_home_sources_rule(box, tmp_path):
    prof = box / ".ws" / "profile.toml"
    prof.parent.mkdir(parents=True)
    prof.write_text('[home.sources]\nasks_jsonl = "~/elsewhere/asks.jsonl"\n', encoding="utf-8")
    assert js_const(shim_for("", tmp_path), "ASKS") == str(box / "elsewhere" / "asks.jsonl")
    prof.write_text("[home\n", encoding="utf-8")  # a profile that does not read: the default, never a failed generate
    assert js_const(shim_for("", tmp_path), "ASKS") == str(box / ".claude-usage" / "asks" / "asks.jsonl")


def test_the_shim_answers_an_ask_of_its_own_once_and_never_always(tmp_path, box):
    shim = shim_for('[gate]\nask_commands = ["make release*"]\n', tmp_path)
    assert 'reply: "once"' in shim and 'response: "once"' in shim and "always" not in shim
    assert js_const(shim, "ASK") == ["make release*"]
    assert js_const(shim_for("", tmp_path), "ASK") == []


def test_the_generated_folder_still_needs_nothing_written_at_start(tmp_path, box):
    files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "policy.toml")
    assert set(files) == {opencode.CONFIG, opencode.PLUGIN, opencode.IGNORE}


NODE = shutil.which("node")
TRUE = shutil.which("true")

# Loads the generated shim in node with a client that records its replies, lets the gate "allow" a bash call (the check
# command is `true`), raises one permission.asked event for it, and prints the replies. The cases come in on stdin.
DRIVER = """
import { pathToFileURL } from "node:url"
const mod = await import(pathToFileURL(process.argv[2]).href)
const read = () => new Promise((ok) => { let t = ""; process.stdin.on("data", (d) => (t += d)); process.stdin.on("end", () => ok(t)) })
const cases = JSON.parse(await read())
const out = []
for (const [i, c] of cases.entries()) {
  const replies = []
  const hooks = await mod.WsGate({ client: { permission: { reply: async (r) => replies.push(r) } }, directory: "/w" })
  if (c.allowed) await hooks["tool.execute.before"]({ tool: "bash", callID: "call" + i, sessionID: "s" }, { args: { command: "x" } })
  const properties = { id: "p" + i, sessionID: "s", permission: c.permission, patterns: c.patterns, tool: { callID: "call" + i } }
  await hooks.event({ event: { type: "permission.asked", properties } })
  out.push(replies)
}
console.log(JSON.stringify(out))
"""


def shim_answers(tmp_path: Path, shim: str, cases: list[dict], asks: Path | None = None) -> list[list[dict]]:
    """Run the generated shim in node and return, for each case, the replies it gave to the ask."""
    plugin = tmp_path / "ws-gate.mjs"
    text = shim.replace(f"const CHECK = {json.dumps(js_const(shim, 'CHECK'))}", f"const CHECK = {json.dumps([TRUE])}")
    assert text != shim
    if asks is not None:
        text = text.replace(
            f"const ASKS = {json.dumps(js_const(shim, 'ASKS'))}", f"const ASKS = {json.dumps(str(asks))}"
        )
    plugin.write_text(text, encoding="utf-8")
    driver = tmp_path / "driver.mjs"
    driver.write_text(DRIVER, encoding="utf-8")
    done = subprocess.run(
        [NODE, str(driver), str(plugin)],
        input=json.dumps(cases),
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return json.loads(done.stdout)


def ask_case(patterns, *, permission="bash", allowed=True) -> dict:
    return {"patterns": patterns, "permission": permission, "allowed": allowed}


@pytest.mark.skipif(NODE is None or TRUE is None, reason="needs node and a `true` command")
def test_an_ask_is_answered_once_only_when_every_segment_is_one_of_ws_asks(tmp_path, box):
    shim = shim_for('[gate]\nask_commands = ["touch *APPROVED"]\n', tmp_path)
    got = shim_answers(
        tmp_path,
        shim,
        [
            ask_case(["touch /w/APPROVED"]),  # 0: the one segment is ws's
            ask_case(["touch /w/APPROVED", "touch /w/MORE_APPROVED"]),  # 1: every segment is ws's
            ask_case(["ls", "touch /w/APPROVED"]),  # 2: `ls` may be the person's own ask rule: left to them
            ask_case(["touch /w/APPROVED", "echo hi", "ls"]),  # 3
            ask_case(["ls", "echo hi"]),  # 4: none of ws's
            ask_case(["touch /w/APPROVED"], allowed=False),  # 5: the gate did not let the call through
            ask_case(["touch /w/APPROVED"], permission="edit"),  # 6: not a shell command
            ask_case([]),  # 7
        ],
    )
    assert got == [[{"requestID": "p0", "reply": "once"}], [{"requestID": "p1", "reply": "once"}]] + [[]] * 6, got


def test_the_ask_check_in_the_shim_is_every_not_some(tmp_path, box):
    """The node test above is skipped without node; this pins the rule in the text itself."""
    shim = shim_for('[gate]\nask_commands = ["touch *APPROVED"]\n', tmp_path)
    assert "patterns.every((x) => ASK_RE.some((re) => re.test(x)))" in shim and "patterns.some(" not in shim


def asks_after(tmp_path: Path, before: bytes) -> Path:
    """Put `before` in an asks file, raise one ask through the shim, and return the file's path."""
    asks = tmp_path / "asks" / "asks.jsonl"
    asks.parent.mkdir()
    asks.write_bytes(before)
    shim = shim_for('[gate]\nask_commands = ["touch *APPROVED"]\n', tmp_path)
    shim_answers(tmp_path, shim, [ask_case(["touch /w/APPROVED"])], asks=asks)
    return asks


@pytest.mark.skipif(NODE is None or TRUE is None, reason="needs node and a `true` command")
def test_the_asks_file_is_rotated_before_it_passes_what_the_reader_accepts(tmp_path, box):
    from ws.home.sources import _files

    asks = asks_after(tmp_path, b"x" * (9 * 1024 * 1024))
    assert (tmp_path / "asks" / "asks.jsonl.old").stat().st_size == 9 * 1024 * 1024
    assert asks.stat().st_size < 4096
    assert [row["event"] for row in _files.iter_jsonl(asks)] == ["added"]  # one reads fine, under the 64 MiB cap
    assert 9 * 1024 * 1024 < _files.FILE_CAP


@pytest.mark.skipif(NODE is None or TRUE is None, reason="needs node and a `true` command")
def test_a_torn_last_line_in_the_asks_file_costs_only_itself(tmp_path, box):
    from ws.home.sources import _files

    asks = asks_after(tmp_path, b'{"event": "added", "id": "old", "q')  # the writer died mid-line, no newline
    rows = list(_files.iter_jsonl(asks))
    assert [row["id"] for row in rows] == ["p0"]
