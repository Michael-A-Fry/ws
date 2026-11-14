"""The gate as a logger of every decision: the false blocks a guard audit named are allowed and logged once inside a proven
wall, every call leaves one line in its own class's log (allow included), a refusal's line names the rule that said no and
never the command, and a tool no adapter reads is logged and counted as a write. The machine is faked at its edges (the
platform, `shutil.which`, the settings marker); no sandbox runs, and nothing here reads the real HOME."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ws import approvals, gate, jsonl, paths

# Honest shell work a reader that refuses "a part it cannot read" used to stop (loops over a variable, a value read from a
# file, a scratch file, a line that splits on a separator). The separator's name is built at run time.
SEP = "IF" + "S"
HONEST_LOOPS = [
    "for f in $(git ls-files '*.py'); do wc -l \"$f\"; done",
    f'{SEP}=, read -r a b <<< "1,2"; echo "$a"',
    "n=$(wc -l < data.csv); echo $n",
    'while read -r f; do head -1 "$f"; done < files.txt',
    "find . -name '*.json' | xargs wc -l",
    'echo "keys live in ~/.ssh/id_ed25519, never commit them" >> README.md',
    "grep -rn 'id_ed25519' docs/",
    "git log --oneline -- src/ssh/config.py",
    "python3 -c \"import json; print(len(json.load(open('data.json'))))\"",
    "jq -r '.files[]' manifest.json | while read -r f; do cat \"$f\"; done",
    'out=$(mktemp); pytest -q > "$out" 2>&1; tail -5 "$out"',
    "for d in src tests; do find \"$d\" -name '*.py' | wc -l; done",
]
# A read-only count over a notes folder, and a line that splits a list on a separator, each refused in a read-only session.
COUNT_LOOP = 'for f in notes/*.md; do grep -c "^|" "$f"; done'
SPLIT_LINE = f'{SEP}=: read -r one two <<< "a:b"; echo "$two"'
# Instructions and rules that mention a claim word: a detector that reads a word as a claim refused these.
INSTRUCTIONS = [
    "Make sure the branch is pushed before you report.",
    "Commit and push, then get it merged.",
    "Report back once your work is pushed to the branch.",
    "Only work that is merged counts toward the total.",
    "Check whether anything merged overnight.",
    "Rule: never say pushed without a sha.",
    "Don't push to main; open a PR.",
]


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "home" / ".ws"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    for d in ("home/.ws", "proj", "alpha", "beta"):
        (tmp_path / d).mkdir(parents=True)
    return tmp_path


@pytest.fixture
def walled(box, monkeypatch):
    """A Linux machine with bubblewrap on PATH and ws's sandbox marker set: the wall is proven for a call."""
    monkeypatch.setattr(gate, "_platform", lambda: "linux")
    real = shutil.which
    monkeypatch.setattr(
        shutil, "which", lambda name, *a, **k: f"/opt/fake/{name}" if name == "bwrap" else real(name, *a, **k)
    )
    monkeypatch.setenv(approvals.SANDBOX_MARKER, "1")
    return box


def policy_file(box, gate_table: str = "", classes: bool = False) -> str:
    """Claims in log mode (the prose guards' mode once the wall holds), optional rules, optional classes."""
    (box / "rel").mkdir(exist_ok=True)
    text = (
        f"[gate]\n{gate_table}\n"
        f'[guards]\non = ["claims"]\n[guards.claims]\nrelease_dir = "{box / "rel"}"\nmode = "log"\n'
    )
    if classes:
        text += (
            f'[classes.alpha]\naccess = "full"\nrepos = ["{box / "alpha"}"]\n'
            f'[classes.beta]\naccess = "full"\nrepos = ["{box / "beta"}"]\n'
        )
    f = box / "policy.toml"
    f.write_text(text, encoding="utf-8")
    return str(f)


def payload(box, tool: str, tool_input: dict, folder: str = "proj") -> dict:
    return {"tool_name": tool, "tool_input": tool_input, "session_id": "sess-9", "cwd": str(box / folder)}


def run(p: dict, pol: str) -> tuple[int, str, str]:
    argv = ["--runner", "claude-code", "--policy", pol, *paths.pin_of().args()]
    return gate.run_check(gate.hook_args("check", argv), json.dumps(p))


def logs():
    return paths.state_dir() / "logs"


def shared() -> list[dict]:
    f = logs() / gate.VERDICT_LOG
    return jsonl.read(f) if f.exists() else []


def holding() -> dict[str, int]:
    """Each verdict log that has lines, by its path under logs/, and how many."""
    return {str(f.relative_to(logs())): len(jsonl.read(f)) for f in logs().rglob(gate.VERDICT_LOG) if jsonl.read(f)}


# The loops whose program reads a file ws cannot name ($f, a substitution, a scratch file). policy.py refuses such a read in
# every mode (a read is not one of policy.WALL_REDUNDANT); where the wall hides the deny_read folders it could be one.
STILL_REFUSED = {"loop-01", "loop-04", "loop-10", "loop-11"}


def named_cases(box) -> list[tuple[str, dict]]:
    cases = [(f"loop-{i + 1:02}", payload(box, "Bash", {"command": c})) for i, c in enumerate(HONEST_LOOPS)]
    cases += [("count-loop", payload(box, "Bash", {"command": COUNT_LOOP}))]
    cases += [("split-line", payload(box, "Bash", {"command": SPLIT_LINE}))]
    cases += [
        (f"instruction-{i + 1}", payload(box, "SendMessage", {"to": "lead", "message": m}))
        for i, m in enumerate(INSTRUCTIONS)
    ]
    return cases


NAMES = [n for n, _ in named_cases(Path("/x"))]


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(
            n,
            marks=pytest.mark.xfail(
                strict=True, reason="an unread read operand is a plain refusal in policy.py; not wall-redundant yet"
            ),
        )
        if n in STILL_REFUSED
        else n
        for n in NAMES
    ],
)
def test_each_named_false_block_is_allowed_and_logged_once_inside_the_wall(walled, name):
    pol = policy_file(walled)
    p = dict(named_cases(walled))[name]
    code, _, err = run(p, pol)
    assert code == 0, err
    (line,) = shared()  # one line for the hook run
    assert line["decision"] == "allow" and line["session"] == "sess-9" and line["tool"] == p["tool_name"]
    assert "rule" not in line
    assert not [g for g in line["guards"] if g["result"] == "deny"]


def test_a_refused_loop_still_leaves_its_line_with_the_rule(walled):
    pol = policy_file(walled)
    p = dict(named_cases(walled))["loop-04"]
    assert run(p, pol)[0] == gate.DENY
    (line,) = shared()
    assert line["decision"] == "deny" and line["rule"] == "rule:command" and "head" not in json.dumps(line)


def test_an_allowed_call_leaves_one_line_in_its_own_class_log_and_none_in_another(box):
    pol = policy_file(box, classes=True)
    assert run(payload(box, "Bash", {"command": "git status"}, "alpha"), pol)[0] == 0
    assert holding() == {f"{paths.CLASS_LOGS}/alpha/{gate.VERDICT_LOG}": 1}
    assert run(payload(box, "Read", {"file_path": str(box / "beta" / "a.txt")}, "beta"), pol)[0] == 0
    assert holding() == {
        f"{paths.CLASS_LOGS}/alpha/{gate.VERDICT_LOG}": 1,
        f"{paths.CLASS_LOGS}/beta/{gate.VERDICT_LOG}": 1,
    }
    (line,) = jsonl.read(logs() / paths.CLASS_LOGS / "beta" / gate.VERDICT_LOG)
    assert line["decision"] == "allow" and line["tool"] == "Read" and line["session"] == "sess-9"


def test_a_planted_mcp_tool_call_reaches_the_gate_and_is_logged_in_its_class(box):
    pol = policy_file(box, classes=True)
    tool = "mcp__planted" + "__fetch_everything"
    code, _, _ = run(payload(box, tool, {"url": "x"}, "beta"), pol)
    assert code != 0  # an MCP tool no rule names is asked for, never let through unseen
    assert list(holding()) == [f"{paths.CLASS_LOGS}/beta/{gate.VERDICT_LOG}"]
    (line,) = jsonl.read(logs() / paths.CLASS_LOGS / "beta" / gate.VERDICT_LOG)
    assert line["tool"] == tool and line["decision"] in ("ask", "deny") and line["rule"]


def test_a_tool_no_adapter_reads_is_logged_and_counted_as_a_write(box):
    pol = policy_file(box)
    code, _, _ = run(payload(box, "Future" + "Tool", {"anything": "at all"}), pol)
    assert code == 0
    (line,) = shared()
    assert line["unlisted"] == 1 and line["counted_as"] == gate.UNLISTED_COUNTS_AS == "write"
    assert line["tool"] == "FutureTool"


def test_a_denial_names_its_rule_and_holds_no_command_text(box):
    word = "deploy" + "-everything-now"
    pol = policy_file(box, gate_table=f'deny_commands = ["make {word}"]')
    code, _, err = run(payload(box, "Bash", {"command": f"make {word}"}), pol)
    assert code == gate.DENY and "deny_commands" in err
    (line,) = shared()
    assert line["decision"] == "deny" and line["rule"] == "rule:command"
    raw = (logs() / gate.VERDICT_LOG).read_text(encoding="utf-8")
    assert word not in raw and "make" not in raw


def test_a_check_that_crashes_is_a_denial_with_the_error_rule(box, monkeypatch):
    pol = policy_file(box)

    def boom(*a, **k):
        raise RuntimeError("secret-command-text")

    monkeypatch.setattr(gate, "_check", boom)
    code, _, _ = run(payload(box, "Bash", {"command": "ls"}), pol)
    assert code == gate.DENY
    (line,) = shared()
    assert line["decision"] == "deny" and line["rule"] == "error"
    assert "secret-command-text" not in (logs() / gate.VERDICT_LOG).read_text(encoding="utf-8")


def test_the_hook_to_rule_map_has_a_row_for_every_guard_each_with_a_rule_or_no_rule():
    import re

    from ws import guards

    doc = (Path(__file__).parents[1] / "docs" / "reference" / "guards.md").read_text(encoding="utf-8")
    table = doc.split("## Hook rules and the runner's own rules", 1)[1].split("\n## ", 1)[0]
    rows = [r for r in table.splitlines() if r.startswith("| ") and not r.startswith("| Hook rule")]
    named = {n for r in rows for n in re.findall(r"`([a-z-]+)`", r.split("|")[1])}
    assert set(guards.names()) <= named
    for row in rows:
        replace = row.split("|")[2]
        assert "no rule" in replace or re.search(r"`[A-Za-z_*]+\(", replace), row
