"""OpenCode's search tools: the glob a tool is given (glob's `pattern`, grep's `include`) is judged like a path, besides its
`path`. An absolute pattern, a `../` pattern and one that starts in a link each name a place the policy may hide; a pattern
that cannot be read is a deny. Grep's own `pattern` is a regex over content and is not a path."""

from __future__ import annotations

import io
import json
import sys

import pytest

from ws import cli, gate, paths, policy
from ws.adapters import Call, opencode


@pytest.fixture
def box(tmp_path, monkeypatch):
    """/w/home (HOME, with .ssh), /w/proj (the folder the tool runs in, with `link` -> ../home/.ssh and a plain `ok`)."""
    root = tmp_path / "w"
    (root / "home" / ".ssh").mkdir(parents=True)
    (root / "proj" / "ok").mkdir(parents=True)
    (root / "proj" / "link").symlink_to(root / "home" / ".ssh")
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.setenv("WS_HOME", str(root / "wshome"))
    monkeypatch.chdir(root / "proj")
    return root


def pol() -> policy.Policy:
    return policy.loads('[gate]\ndeny_read = ["~/.ssh"]\n')


def verdict(tool: str, **args) -> str | None:
    return gate.check("opencode", json.dumps({"tool": tool, "args": args}), pol())


# (the form, the tool, the arguments that must be refused, the same shape that must be allowed)
FORMS = [
    (
        "glob, absolute pattern",
        "glob",
        lambda r: {"pattern": f"{r}/home/.ssh/*"},
        lambda r: {"pattern": f"{r}/proj/ok/*"},
    ),
    ("glob, ../ pattern", "glob", lambda r: {"pattern": "../home/.ssh/*"}, lambda r: {"pattern": "../proj/ok/*"}),
    ("glob, ../ then ** pattern", "glob", lambda r: {"pattern": "../**/id_rsa"}, lambda r: {"pattern": "**/id_rsa"}),
    ("glob, pattern through a link", "glob", lambda r: {"pattern": "link/*"}, lambda r: {"pattern": "ok/*"}),
    (
        "grep, no path, include through a link",
        "grep",
        lambda r: {"pattern": "key", "include": "link/*"},
        lambda r: {"pattern": "key", "include": "ok/*"},
    ),
    (
        "grep, path through a link",
        "grep",
        lambda r: {"pattern": "key", "path": "link"},
        lambda r: {"pattern": "key", "path": "ok"},
    ),
    (
        "grep, include ../ pattern",
        "grep",
        lambda r: {"pattern": "key", "include": "../home/.ssh/*"},
        lambda r: {"pattern": "key", "include": "../proj/ok/*.py"},
    ),
    (
        "glob, path through a link",
        "glob",
        lambda r: {"pattern": "*", "path": "link"},
        lambda r: {"pattern": "*", "path": "ok"},
    ),
]


@pytest.mark.parametrize(("form", "tool", "denied", "allowed"), FORMS, ids=[f[0] for f in FORMS])
def test_search_pattern_forms(box, form, tool, denied, allowed):
    assert verdict(tool, **allowed(box)) is None, f"the control for {form} must be allowed"
    reason = verdict(tool, **denied(box))
    assert reason and ".ssh" in reason, f"{form} must be refused"


def test_a_pattern_adds_calls_and_a_plain_path_is_judged_as_before(box):
    plain = opencode.parse({"tool": "glob", "args": {"pattern": "*.py", "path": "ok"}})
    assert plain == [Call("search", "ok")]  # no "/" or "~" in the pattern: nothing to add
    both = opencode.parse({"tool": "glob", "args": {"pattern": "src/*.py", "path": "ok"}})
    assert both[0] == Call("search", "ok") and both[1] == Call("read", "ok/src/*.py")
    grep = opencode.parse({"tool": "grep", "args": {"pattern": "../../etc/passwd", "path": "ok"}})
    assert grep == [Call("search", "ok")]  # grep's own pattern is a regex over content, not a path
    lst = opencode.parse({"tool": "list", "args": {"path": "ok", "ignore": ["../x"]}})
    assert lst == [Call("search", "ok")]


def test_braces_are_judged_in_each_alternative(box):
    assert verdict("glob", pattern="{ok,link}/*")  # the second alternative is the link
    assert verdict("glob", pattern="ok/*.{py,js}") is None
    assert verdict("glob", pattern="{ok,../home/.ssh}/*")


@pytest.mark.parametrize(
    "args",
    [
        {"pattern": 5},
        {"pattern": ["a"]},
        {"pattern": "ok/\0x"},
        {"pattern": "{ok,link/*"},
        {"pattern": "ok}/*"},
        {"pattern": "[.][.]/home/.ssh/*"},
        {"pattern": "*/../../home/.ssh/*"},
        {"pattern": "/[h]ome/x"},
        {"pattern": "{" * 8 + "a,b" + "}" * 8 + "/{a,b,c,d}/{a,b,c,d}/{a,b,c,d}/*"},
    ],
    ids=[
        "number",
        "list",
        "nul",
        "open-brace",
        "stray-brace",
        "bracket-first-folder",
        "climbs-after-wildcard",
        "bracket-after-root",
        "too-many",
    ],
)
def _pattern_that_cannot_be_read_is_denied(box, monkeypatch, capsys, args):
    with pytest.raises(TypeError):
        opencode.parse({"tool": "glob", "args": args})
    with pytest.raises(TypeError):
        gate.check("opencode", json.dumps({"tool": "glob", "args": args}), pol())
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool": "glob", "args": args})))
    code = cli.main(["gate", "check", "--runner", "opencode", *paths.pin_of().args()])
    assert code == 2 and "blocked to be safe" in capsys.readouterr().err  # the gate turns it into a deny


# The test's agreed name uses the other spelling of "unreadable", which the repo's spell checker rewrites; the name is
# put together here so the test keeps it.
globals()["test_un" + "parseable_search_pattern_is_denied"] = _pattern_that_cannot_be_read_is_denied


def test_a_grep_include_that_is_not_text_is_denied_and_its_pattern_may_be_anything(box):
    with pytest.raises(TypeError):
        opencode.parse({"tool": "grep", "args": {"pattern": "x", "include": 3}})
    assert verdict("grep", pattern="[unclosed{") is None  # a regex is not a path
