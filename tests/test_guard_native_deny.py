"""Every git form bulk-staging blocks that a runner's own deny pattern can express is in that runner's deny list, with no
hook needed. A form a pattern can't express stays with the hook; checkout-pin has none to give (what it blocks depends
on the folder, which a pattern on the command text can't see). Each form is also asked of the guard's own judge, so a
pattern can never deny what the guard would let through."""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

import pytest

from ws import guards, policy
from ws.adapters import Call, claude_code, opencode
from ws.guards import bulk_staging, checkout_pin

SUBS = ("add", "stage")
TREE = (".", "./", "./.", ":/", ":/.", "..")  # each names the top of the tree


def forms() -> list[str]:
    """The whole-tree forms, written out as a session would type them."""
    out = []
    for sub in SUBS:
        out += [f"git {sub} -A", f"git {sub} -Ax", f"git {sub} -A src", f"git {sub} --all", f"git {sub} --al"]
        out += [f"git {sub} --a", f"git {sub} --all src"]
        out += [f"git {sub} {t}" for t in TREE]
        out += [f"git {sub} -- {t}" for t in TREE]
        out += [f"git {sub} -u", f"git {sub} --u"] + [f"git {sub} {'--update'[:n]}" for n in range(3, 9)]
        out += [f"git {sub} -u {t}" for t in TREE]
        out += [f"git -C sub {sub} {t}" for t in (".", "-A")]
        out += [f"git --no-pager -C sub {sub} -u"]
    out += [
        "git commit -a",
        "git commit -am msg",
        "git commit -a -m msg",
        "git commit --all",
        "git commit --all -m msg",
    ]
    out += ["git commit --al -m msg", "git commit --a"]
    out += [f"git commit {t}" for t in TREE]
    out += [f"git commit -- {t}" for t in TREE]
    out += [f"git commit -i {t}" for t in TREE]
    out += ["git -C sub commit -a", "git -C sub commit -am msg", "git -C sub commit --all"]
    out += ["git -c alias.go=add git go", "git -c Alias.go=add git go", "git -C sub -c alias.x=y git x"]
    return out


# Forms the judge blocks that a pattern on the command text can't say, or would over-block: they stay with the hook.
HOOK_ONLY = [
    "git add -vA",  # a cluster with the letter anywhere
    "git add -u -v",  # an option after -u
    "git add *",  # a literal star
    "git add $(pwd)",  # a substitution
    "git add -v -A",  # `-A` not first among the options
    "git add :(top)",  # parentheses are rule syntax
    "git add -- :(top).",
    "git commit -m msg -a",  # a message could hold the same words
    "git commit -m msg --all",
    "git commit -vam msg",
    "GIT_CONFIG_PARAMETERS=x git status",  # the environment
]


@pytest.fixture
def pol():
    return policy.loads('[guards]\non = ["bulk-staging", "checkout-pin"]\n')


def native_match(patterns, command: str) -> list[str]:
    """The patterns a runner matches on the raw command text (`*` is anything, the rest is literal)."""
    return [p for p in patterns if fnmatch.fnmatchcase(command, p.replace("[", "[[]").replace("?", "[?]"))]


def judged(command: str):
    return bulk_staging.judge(Call("command", command), None, guards.Context("claude-code", "pre"))


def test_every_expressible_git_form_is_in_permissions_deny(pol, tmp_path):
    patterns = policy.native_patterns(pol)
    assert patterns == list(dict.fromkeys(patterns))
    # the Claude Code settings carry them as Bash(...) rules, and OpenCode's config as bash entries set to deny
    settings = json.loads(claude_code.generate(pol, ["ws"], tmp_path / "o", tmp_path / "p.toml")["settings.json"])
    deny = settings["permissions"]["deny"]
    config = json.loads(opencode.generate(pol, ["ws"], tmp_path / "o", tmp_path / "p.toml")["opencode.json"])
    bash = config["permission"]["bash"]
    for command in forms():
        assert judged(command) is not None, f"the guard's judge lets {command!r} through"
        hit = native_match(patterns, command)
        assert hit, f"no deny pattern for {command!r}"
        assert all(f"Bash({p})" in deny and bash[p] == "deny" for p in hit)


def test_no_pattern_denies_what_the_guard_would_allow(pol):
    allowed = [
        "git add a.py",
        "git add -- a.py",
        "git add -u src/",
        "git add --update src/",
        "git add -p",
        "git add -N a.py",
        "git add -v a.py",
        "git commit -m msg",
        "git commit -m 'all done'",
        'git commit -m "use -a flag"',
        "git commit --amend",
        "git commit --amend -m msg",
        "git commit --allow-empty -m msg",
        "git commit a.py",
        "git commit -- a.py",
        "git stage a.py",
        "git stage -u src/",
        "git -C sub add a.py",
        "git -C sub commit -m msg",
        "git status",
        "git checkout -b topic",
        "git add -f ignored.txt",
    ]
    patterns = policy.native_patterns(pol)
    for command in allowed:
        assert judged(command) is None, command
        assert not native_match(patterns, command), f"{command!r} is denied by {native_match(patterns, command)}"


def test_a_form_a_pattern_cannot_express_stays_with_the_hook(pol):
    patterns = policy.native_patterns(pol)
    for command in HOOK_ONLY:
        assert judged(command) is not None, f"the guard's judge lets {command!r} through"
        assert not native_match(patterns, command), f"{command!r} is matched by {native_match(patterns, command)}"


def test_every_pattern_is_one_a_runner_can_match(pol):
    for pattern in bulk_staging.NATIVE_DENY:
        assert not any(c in pattern for c in policy._HOOK_ONLY), pattern
        assert pattern.startswith("git "), pattern
    assert len(set(bulk_staging.NATIVE_DENY)) == len(bulk_staging.NATIVE_DENY)


def test_checkout_pin_has_no_native_pattern_because_its_forms_depend_on_the_folder(pol, tmp_path):
    """`git checkout` is denied in the main checkout and is the normal way to work in a linked worktree: a pattern on the
    command text would deny both, so every form stays with the hook, and the module says so."""
    assert not hasattr(checkout_pin, "NATIVE_DENY")
    assert "hook" in checkout_pin.__doc__ and "NATIVE_DENY" in checkout_pin.__doc__
    assert not native_match(policy.native_patterns(pol), "git checkout -b topic")
    assert not native_match(policy.native_patterns(pol), "git reset --hard")


def test_a_guard_in_log_mode_gives_the_runner_no_deny_of_its_own(tmp_path):
    """A runner's own deny can't be logged instead of refused, so a guard asked to log leaves its patterns out: the hook
    judges the call, and refuses it wherever the wall is not proven."""
    on = policy.loads('[guards]\non = ["bulk-staging"]\n')
    log = policy.loads('[guards]\non = ["bulk-staging"]\n[guards.bulk-staging]\nmode = "log"\n')
    assert "git add -A*" in policy.native_patterns(on)
    assert not [p for p in policy.native_patterns(log) if p.startswith("git ")]
    settings = json.loads(claude_code.generate(log, ["ws"], tmp_path / "o", Path(tmp_path / "p.toml"))["settings.json"])
    assert not [r for r in settings["permissions"]["deny"] if r.startswith("Bash(git ")]
    assert guards.native_deny(("bulk-staging",), {"bulk-staging": "log"}) == []
    assert guards.native_deny(("bulk-staging",), {"bulk-staging": "enforce"}) == list(bulk_staging.NATIVE_DENY)
