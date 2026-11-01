"""The test-weakening guard (ws.guards.test_weakening): what it reads as a test file, what it denies and what it warns."""

from __future__ import annotations

import json

import pytest

from ws import cli, gate, guards, policy
from ws.adapters import claude_code
from ws.guards import Context
from ws.guards import test_weakening as tw

SKIP = "@pytest.mark.skip\n"  # a line of its own, so no letter sits before the @
OLD = """import pytest


def test_a():
    assert 1 + 1 == 2


class TestB:
    def test_b(self):
        assert "x".upper() == "X"
        assert "y".upper() == "Y"
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_x.py").write_text(OLD)
    return tmp_path


def run(repo, tool: str, rel="tests/test_x.py", **inp):
    payload = {"tool_name": tool, "tool_input": {"file_path": str(repo / rel), **inp}}
    call = claude_code.parse(payload)
    ctx = Context("claude-code", "pre", "s1", str(repo), payload)
    return tw.judge(call, tw.config({}, "[guards.test-weakening]"), ctx)


def edit(repo, old, new, **kw):
    return run(repo, "Edit", old_string=old, new_string=new, **kw)


def test_it_is_a_guard_for_writes_before_a_call():
    assert guards.get("test-weakening").EVENTS == ("pre",) and tw.KINDS == ("write",)


def test_a_skip_marker_added_to_an_existing_test_is_denied(repo):
    f = edit(repo, "def test_a():", "@pytest.mark.skip\ndef test_a():")
    assert f.action == "deny" and not f.approvable and "test_a" in f.reason


@pytest.mark.parametrize(
    "marker",
    [
        "@pytest.mark.skipif(True, reason='x')",
        "@pytest.mark.xfail",
        "@unittest.skip('x')",
        "@unittest.expectedFailure",
    ],
)
def test_each_python_skip_marker_is_denied(repo, marker):
    assert edit(repo, "def test_a():", f"{marker}\ndef test_a():").action == "deny"


def test_a_skip_call_in_the_body_is_denied(repo):
    f = edit(repo, "    assert 1 + 1 == 2", "    pytest.skip('later')\n    assert 1 + 1 == 2")
    assert f.action == "deny"


def test_a_skip_added_to_a_method_names_it(repo):
    f = edit(repo, "    def test_b(self):", "    @pytest.mark.skip\n    def test_b(self):")
    assert f.action == "deny" and "test_b" in f.reason


def test_a_skip_on_a_class_or_for_the_file_is_denied(repo):
    assert edit(repo, "class TestB:", "@pytest.mark.skip\nclass TestB:").action == "deny"
    assert edit(repo, "import pytest\n", "import pytest\n\npytestmark = pytest.mark.skip\n").action == "deny"


def test_a_new_test_may_carry_a_skip_and_so_may_a_new_file(repo):
    new_test = SKIP + "def test_new():\n    assert True is True"
    assert edit(repo, "def test_a():", new_test + "\n\n\ndef test_a():") is None
    fresh = run(
        repo,
        "Write",
        rel="tests/test_new.py",
        content="import pytest\n" + SKIP + "def test_z():\n    pass\n",
    )
    assert fresh is None


def test_a_skip_that_was_already_there_is_not_new(repo):
    path = repo / "tests" / "test_x.py"
    path.write_text(OLD.replace("def test_a():", "@pytest.mark.skip\ndef test_a():"))
    assert edit(repo, "assert 1 + 1 == 2", "assert 2 == 1 + 1") is None


def test_taking_a_skip_away_is_fine(repo):
    (repo / "tests" / "test_x.py").write_text(OLD.replace("def test_a():", "@pytest.mark.skip\ndef test_a():"))
    assert edit(repo, "@pytest.mark.skip\n", "") is None


def test_a_comment_that_says_skip_is_not_a_skip(repo):
    assert edit(repo, "def test_a():", "# pytest.mark.skip was here\ndef test_a():") is None


def test_a_removed_test_is_a_warning(repo):
    old = '    def test_b(self):\n        assert "x".upper() == "X"\n        assert "y".upper() == "Y"\n'
    f = edit(repo, old, "    pass\n")
    assert f.action == "warn" and "test_b" in f.reason


def test_fewer_assertion_lines_is_a_warning(repo):
    f = edit(repo, '        assert "y".upper() == "Y"\n', "")
    assert f.action == "warn" and "fewer assertion lines (3 before, 2 after)" in f.reason


def test_an_always_true_assertion_is_a_warning(repo):
    f = edit(repo, "    assert 1 + 1 == 2", "    assert 1 + 1 == 2\n    assert True")
    assert f.action == "warn" and "always true" in f.reason
    f = edit(repo, "    assert 1 + 1 == 2", "    value = 3\n    assert value == value\n    assert 1 + 1 == 2")
    assert f.action == "warn" and "always true" in f.reason


def test_an_always_true_assertion_that_was_there_is_not_new(repo):
    (repo / "tests" / "test_x.py").write_text(OLD + "\n\ndef test_t():\n    assert True\n")
    assert edit(repo, "def test_a():", "def test_a():\n    pass") is None


def test_a_deny_outranks_a_warning(repo):
    f = edit(repo, "def test_a():\n    assert 1 + 1 == 2", "@pytest.mark.skip\ndef test_a():\n    pass")
    assert f.action == "deny"


def test_a_write_that_replaces_the_whole_file_is_compared(repo):
    new = OLD.replace("def test_a():", "@pytest.mark.skip\ndef test_a():")
    assert run(repo, "Write", content=new).action == "deny"
    assert run(repo, "Write", content=OLD) is None


def test_a_multi_edit_is_applied_in_order(repo):
    edits = [
        {"old_string": "def test_a():", "new_string": "def test_aa():"},
        {"old_string": "def test_aa():", "new_string": "@pytest.mark.skip\ndef test_aa():"},
    ]
    # test_a was renamed: the marker is on a test that did not exist before, but test_a is gone
    assert run(repo, "MultiEdit", edits=edits).action == "warn"
    only = [{"old_string": "def test_a():", "new_string": "@pytest.mark.skip\ndef test_a():"}]
    assert run(repo, "MultiEdit", edits=only).action == "deny"


def test_a_write_it_cannot_apply_is_not_judged(repo):
    assert edit(repo, "text that is not in the file", "@pytest.mark.skip") is None
    assert run(repo, "Edit", old_string="a") is None  # no new text
    nb = {
        "tool_name": "NotebookEdit",
        "tool_input": {"notebook_path": str(repo / "tests" / "n.ipynb"), "new_source": "x"},
    }
    call = claude_code.parse(nb)
    assert tw.judge(call, tw.config({}, "w"), Context("claude-code", "pre", "s", str(repo), nb)) is None


def test_a_file_that_is_not_a_test_file_is_not_judged(repo):
    (repo / "app.py").write_text("def test_a():\n    assert 1\n")
    assert (
        run(repo, "Edit", rel="app.py", old_string="def test_a():", new_string="@pytest.mark.skip\ndef test_a():")
        is None
    )


@pytest.mark.parametrize(
    "rel",
    [
        "tests/test_x.py",
        "pkg/tests/helper.py",
        "test/a.py",
        "web/__tests__/a.js",
        "pkg/test_thing.py",
        "pkg/thing_test.go",
        "web/a.test.ts",
        "web/a.test.jsx",
        "R/tests/testthat/test-a.R",
        "test-b.R",
    ],
)
def test_the_default_test_paths(rel):
    cfg = tw.config({}, "w")
    assert tw.matches(cfg.paths, rel) and not tw.matches(cfg.exclude, rel)


@pytest.mark.parametrize(
    "rel",
    ["src/app.py", "tests/fixtures/test_data.py", "tests/testdata/a.py", "tests/snapshots/a.js", "tests/golden/a"],
)
def test_the_defaults_leave_these_alone(rel):
    cfg = tw.config({}, "w")
    assert not tw.matches(cfg.paths, rel) or tw.matches(cfg.exclude, rel)


def test_paths_and_except_replace_the_defaults(repo):
    (repo / "checks").mkdir()
    (repo / "checks" / "a.py").write_text("def test_a():\n    assert 1\n")
    table = {"paths": ["checks/**"], "except": ["checks/keep/**"]}
    cfg = tw.config(table, "w")
    payload = {
        "tool_name": "Edit",
        "tool_input": {
            "file_path": str(repo / "checks/a.py"),
            "old_string": "def test_a():",
            "new_string": "@pytest.mark.skip\ndef test_a():",
        },
    }
    ctx = Context("claude-code", "pre", "s", str(repo), payload)
    assert tw.judge(claude_code.parse(payload), cfg, ctx).action == "deny"
    # tests/ is no longer a test path once `paths` names only checks/
    other = Context("claude-code", "pre", "s", str(repo), payload)
    p2 = {
        **payload,
        "tool_input": {
            **payload["tool_input"],
            "file_path": str(repo / "tests/test_x.py"),
            "old_string": "def test_a():",
        },
    }
    assert tw.judge(claude_code.parse(p2), cfg, other) is None


@pytest.mark.parametrize(
    "table, key",
    [
        ({"path": ["a"]}, "path"),
        ({"paths": "tests/**"}, "paths"),
        ({"except": [""]}, "except"),
        ({"paths": [1]}, "paths"),
    ],
)
def test_a_bad_table_names_the_key(table, key):
    with pytest.raises(policy.PolicyError, match=key):
        tw.config(table, "[guards.test-weakening]")


def test_javascript_tests_are_read_by_title(tmp_path):
    before = "it('adds', () => {\n  expect(1+1).toBe(2)\n})\n\ntest('subtracts', () => {\n  expect(2-1).toBe(1)\n})\n"
    assert tw.compare(before, before.replace("it('adds'", "it.skip('adds'"), False)[0]
    assert tw.compare(before, before.replace("test('subtracts'", "xit('subtracts'"), False)[0]
    assert tw.compare(before, before + "it.skip('new', () => {})\n", False)[0] == []
    deny, warn = tw.compare(before, before.replace("it('adds', () => {\n  expect(1+1).toBe(2)\n})\n\n", ""), False)
    assert deny == [] and "adds" in warn[0]
    assert tw.compare(before, before + "expect(true).toBe(true)\n", False)[1]


def test_r_go_and_rust_tests(tmp_path):
    r = 'test_that("adds", {\n  expect_equal(1 + 1, 2)\n})\n'
    assert tw.compare(r, r.replace("{\n", "{\n  skip_on_cran()\n", 1), False)[0]
    assert tw.compare(r, r.replace("2)", "2)\n  expect_true(TRUE)"), False)[1]
    go = 'func TestAdd(t *testing.T) {\n\tif 1+1 != 2 {\n\t\tt.Error("x")\n\t}\n}\n'
    assert tw.compare(go, go.replace("{\n\tif", '{\n\tt.Skip("no")\n\tif', 1), False)[0]
    rust = "#[test]\nfn test_add() {\n    assert_eq!(1 + 1, 2);\n}\n"
    assert tw.compare(rust, rust.replace("    assert_eq", "    #[ignore]\n    assert_eq"), False)[0]


def test_a_python_file_that_no_longer_parses_is_still_read_by_its_lines(repo):
    f = edit(repo, "def test_a():", "@pytest.mark.skip(\ndef test_a():")
    assert f is not None and f.action == "deny"


# --- through the gate and replay ---------------------------------------------------------------------------------


def _policy():
    return policy.loads('[guards]\non = ["test-weakening"]\n[guards.test-weakening]\npaths = ["tests/**"]\n')


def test_the_gate_denies_the_edit_and_names_the_guard(repo):
    payload = {
        "tool_name": "Edit",
        "session_id": "s1",
        "cwd": str(repo),
        "tool_input": {
            "file_path": str(repo / "tests/test_x.py"),
            "old_string": "def test_a():",
            "new_string": "@pytest.mark.skip\ndef test_a():",
        },
    }
    reason = gate.check("claude-code", json.dumps(payload), _policy())
    assert reason.startswith("test-weakening: ") and "test_a" in reason
    payload["tool_input"]["new_string"] = "def test_a():  # still runs"
    assert gate.check("claude-code", json.dumps(payload), _policy()) is None


def test_replay_of_an_edit_that_adds_a_skip_marker_is_a_deny(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    pol_file = tmp_path / "policy.toml"
    pol_file.write_text('[guards]\non = ["test-weakening"]\n')
    line = {
        "payload": {
            "tool_name": "Edit",
            "session_id": "s1",
            "cwd": str(repo),
            "tool_input": {
                "file_path": str(repo / "tests/test_x.py"),
                "old_string": "def test_a():",
                "new_string": "@pytest.mark.skip\ndef test_a():",
            },
        },
        "expect": "deny",
    }
    log = tmp_path / "calls.jsonl"
    log.write_text(json.dumps(line) + "\n")
    assert cli.main(["gate", "replay", str(log), "--runner", "claude-code", "--policy", str(pol_file)]) == 0
    assert "deny" in capsys.readouterr().out
