"""The guard-launch and config-guard guards (ws.guards.guard_launch, ws.guards.config_guard), each through the real
hook entry: what they refuse, what they let through, and that a guard which raises or whose process is killed denies.
Repositories are real ones made with git (a main checkout and a linked worktree); a planted `.git` folder is not one."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import ws
from ws import approvals, gate, guards, paths, policy
from ws.adapters import claude_code

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


def real_repos(base: Path) -> tuple[Path, Path]:
    """A main checkout `base/main` with one commit, and a linked worktree of it, `base/wt`."""
    main, wt = base / "main", base / "wt"
    main.mkdir(parents=True)
    git(main, "init", "-q", "-b", "main")
    (main / "f").write_text("1")
    git(main, "add", "f")
    git(main, "commit", "-q", "-m", "first")
    git(main, "worktree", "add", "-q", "-b", "side", str(wt))
    return main, wt


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    if shutil.which("git"):
        real_repos(tmp_path)
    for name in ("plain", "owner", "guarded"):
        (tmp_path / name).mkdir()
    return tmp_path


def payload(tool, cwd, **tool_input):
    return json.dumps({"tool_name": tool, "tool_input": tool_input, "session_id": "s1", "cwd": str(cwd)})


def run(box, text, body, *extra, start=None):
    """The `ws gate check` hook for `body`, with the session started in `start` (default: where the call says it is)."""
    file = box / "w.toml"
    file.write_text(text)
    argv = ["--runner", "claude-code", *paths.pin_of().args(), "--policy", str(file), *extra]
    where = start if start is not None else json.loads(body)["cwd"]
    old = os.environ.get("CLAUDE_PROJECT_DIR")
    os.environ["CLAUDE_PROJECT_DIR"] = str(where)
    try:
        return gate.run_hook("check", argv, body)[0]
    finally:
        if old is None:
            os.environ.pop("CLAUDE_PROJECT_DIR", None)
        else:
            os.environ["CLAUDE_PROJECT_DIR"] = old


LAUNCH = '[guards]\non = ["guard-launch"]\n'


def config_policy(box, extra=""):
    return (
        f'[guards]\non = ["config-guard"]\n[guards.config-guard]\npaths = ["{box / "guarded"}"]\n'
        f'owners = ["{box / "owner"}"]\n{extra}'
    )


@needs_git
@pytest.mark.parametrize(
    "tool, inp, where, want",
    [
        ("Workflow", {"name": "x"}, "main", 0),
        ("Workflow", {"name": "x"}, "wt", 2),
        ("Workflow", {"name": "x"}, "plain", 2),
        ("Agent", {"prompt": "p", "isolation": "worktree"}, "wt", 2),
        ("Agent", {"prompt": "p", "isolation": "worktree"}, "main", 0),
        ("Agent", {"prompt": "p"}, "wt", 0),  # not isolated: not this guard's business
        ("Task", {"prompt": "p", "isolation": "worktree"}, "plain", 2),
    ],
)
def test_guard_launch_only_from_a_main_checkout(box, tool, inp, where, want):
    assert run(box, LAUNCH, payload(tool, box / where, **inp)) == want


@needs_git
def test_guard_launch_refuses_a_main_checkout_kept_under_a_worktrees_folder(box):
    inner = box / "main" / ".claude" / "worktrees" / "x"
    inner.mkdir(parents=True)
    git(inner, "init", "-q", "-b", "main")
    assert run(box, LAUNCH, payload("Workflow", inner, name="x")) == 2


@needs_git
@pytest.mark.parametrize("where", ["plain", "wt"])
def test_a_planted_git_folder_is_not_a_main_checkout(box, where):
    """BLOCKER 2 of the #212 review: `mkdir .git` in a linked worktree's subfolder or a plain folder must not pass."""
    folder = box / where / "sub"
    folder.mkdir()
    (folder / ".git").mkdir()
    assert run(box, LAUNCH, payload("Workflow", folder, name="x")) == 2
    assert run(box, LAUNCH, payload("Agent", folder, prompt="p", isolation="worktree")) == 2


@needs_git
def test_guard_launch_judges_the_folder_the_session_started_in_too(box):
    """BLOCKER 1: a payload folder the model moved (OpenCode's workdir, a `cd`) can't make a launch pass."""
    body = payload("Workflow", box / "main", name="x")
    assert run(box, LAUNCH, body, start=box / "main") == 0
    assert run(box, LAUNCH, body, start=box / "plain") == 2
    assert run(box, LAUNCH, body, start=box / "wt") == 2


@needs_git
def test_guard_launch_denies_an_empty_or_relative_folder(box):
    assert run(box, LAUNCH, payload("Workflow", "", name="x"), start=box / "main") == 2
    assert run(box, LAUNCH, payload("Workflow", "main", name="x"), start=box / "main") == 2


@needs_git
def test_an_agent_type_whose_definition_asks_for_a_worktree_is_isolated(box):
    """SHOULD 4: `isolation: worktree` in a subagent's front matter isolates it with no field on the call."""
    agents = box / "home" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "refactorer.md").write_text("---\nname: refactorer\nisolation: worktree\n---\nDo it.\n")
    (agents / "reader.md").write_text("---\nname: reader\ndescription: reads\n---\nRead.\n")
    for where, want in (("wt", 2), ("main", 0)):
        assert run(box, LAUNCH, payload("Agent", box / where, prompt="p", subagent_type="refactorer")) == want
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="reader")) == 0
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="Explore")) == 0  # built in
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="mystery")) == 2  # no definition
    assert run(box, LAUNCH, payload("Agent", box / "main", prompt="p", subagent_type="mystery")) == 0


@needs_git
def test_a_project_definition_in_a_folder_above_the_session_is_read(box):
    agents = box / "wt" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "iso.md").write_text("---\nisolation: 'worktree'\n---\n")
    sub = box / "wt" / "deep"
    sub.mkdir()
    assert run(box, LAUNCH, payload("Agent", sub, prompt="p", subagent_type="iso")) == 2


@needs_git
def test_a_decoy_definition_cannot_hide_the_one_that_asks_for_a_worktree(box):
    """Re-review blocker 2: Claude Code matches an agent by the `name:` in its front matter, not by its file name."""
    agents = box / "home" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "refactorer.md").write_text("---\nname: other\n---\nDecoy.\n")
    (agents / "zz.md").write_text("---\nname: refactorer\nisolation: worktree\n---\nReal.\n")
    ask = payload("Agent", box / "wt", prompt="p", subagent_type="refactorer")
    assert run(box, LAUNCH, ask) == 2
    assert run(box, LAUNCH, payload("Agent", box / "main", prompt="p", subagent_type="refactorer")) == 0


@needs_git
@pytest.mark.parametrize(
    "front",
    [
        "name: iso\nisolation: worktree  # a comment after the value\n",
        "name: iso\ndescription: |\n  one\n---\n  two\nisolation: worktree\n",
        "name: 'iso'\nIsolation: \"worktree\"\n",
    ],
)
def test_the_isolation_line_is_found_through_comments_and_odd_layouts(box, front):
    agents = box / "wt" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "a.md").write_text(f"---\n{front}---\nbody\n")
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="iso")) == 2


@needs_git
@pytest.mark.parametrize(
    "front",
    [
        'name: iso\n"isolation": worktree\n',
        "name: iso\nisolation: >-\n  worktree\n",
        "name: iso\nisolation: !!str worktree\n",
        "name: iso\ndescription: " + "x" * 70000 + "\nisolation: worktree\n",
    ],
    ids=["quoted-key", "folded-value", "str-tag", "key-past-the-bytes-read"],
)
def test_the_isolation_key_is_found_in_every_yaml_spelling(box, front):
    """Third review: a quoted key, a folded value, a tag and a description long enough to push the key past what is read
    all still ask for a worktree."""
    agents = box / "wt" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "a.md").write_text(f"---\n{front}---\nbody\n")
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="iso")) == 2
    assert run(box, LAUNCH, payload("Agent", box / "main", prompt="p", subagent_type="iso")) == 0


@needs_git
def test_a_body_that_only_mentions_worktrees_does_not_make_an_agent_isolated(box):
    agents = box / "wt" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "a.md").write_text("---\nname: plain\ndescription: reads\n---\nUse a git worktree for scratch work.\n")
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="plain")) == 0


@needs_git
def test_a_definition_that_overrides_a_built_in_name_is_read_first(box):
    agents = box / "home" / ".claude" / "agents"
    agents.mkdir(parents=True)
    (agents / "explore.md").write_text("---\nname: Explore\nisolation: worktree\n---\n")
    assert run(box, LAUNCH, payload("Agent", box / "wt", prompt="p", subagent_type="Explore")) == 2


@needs_git
def test_a_linked_worktree_whose_git_file_was_rewritten_is_not_a_main_checkout(box):
    """Re-review should 3: `.git` rewritten to name the main repository makes git say git-dir equals common-dir."""
    (box / "wt" / ".git").write_text(f"gitdir: {box / 'main' / '.git'}\n")
    assert run(box, LAUNCH, payload("Workflow", box / "wt", name="x")) == 2


def test_a_cd_into_a_symlinked_folder_does_not_make_a_write_through_it_pass(box):
    """Re-review blocker 1: the model's `cd` moves the payload folder; only the start folder's own links are exempt."""
    decoy = box / "plain" / "decoy"
    decoy.mkdir()
    (box / "plain" / "lnk").symlink_to(decoy)
    text = config_policy(box)
    body = payload("Write", box / "plain" / "lnk", file_path=str(box / "plain" / "lnk" / "x"), content="x")
    assert run(box, text, body, start=box / "plain") == 2
    assert run(box, text, payload("Write", box / "plain", file_path=str(box / "plain" / "lnk" / "x"), content="x")) == 2


def test_config_guard_edits_only_by_the_owner(box):
    target = box / "guarded" / "settings.json"
    other = box / "plain" / "notes.txt"
    text = config_policy(box)
    assert run(box, text, payload("Edit", box / "plain", file_path=str(target), old_string="a", new_string="b")) == 2
    assert run(box, text, payload("Write", box / "owner", file_path=str(target), content="x")) == 0
    assert run(box, text, payload("Write", box / "plain", file_path=str(other), content="x")) == 0
    link = box / "plain" / "link"
    link.symlink_to(target)  # a path through a link is the file it points at
    assert run(box, text, payload("Write", box / "plain", file_path=str(link), content="x")) == 2
    assert (
        run(
            box,
            '[guards]\non = ["config-guard"]\n',
            payload("Write", box / "plain", file_path=str(target), content="x"),
        )
        == 0
    )


def test_config_guard_judges_the_folder_the_session_started_in_too(box):
    """BLOCKER 1: a call that says it runs in an owner's folder, from a session that started elsewhere, is refused."""
    target = box / "guarded" / "settings.json"
    body = payload("Write", box / "owner", file_path=str(target), content="x")
    text = config_policy(box)
    assert run(box, text, body, start=box / "owner") == 0
    assert run(box, text, body, start=box / "plain") == 2


def test_config_guard_joins_a_relative_path_to_the_sessions_folder(box):
    """NIT 8: a relative path is the file in the call's folder, not in the gate's."""
    text = config_policy(box)
    assert run(box, text, payload("Write", box / "guarded", file_path="settings.json", content="x"), start=box) == 2
    assert run(box, text, payload("Write", box / "plain", file_path="notes.txt", content="x"), start=box) == 0


def test_config_guard_denies_an_empty_or_relative_folder(box):
    text = config_policy(box)
    target = str(box / "guarded" / "settings.json")
    assert run(box, text, payload("Write", "", file_path=target, content="x"), start=box / "owner") == 2
    assert run(box, text, payload("Write", "owner", file_path=target, content="x"), start=box / "owner") == 2
    assert run(box, text, payload("Write", "", file_path="notes.txt", content="x"), start=box / "owner") == 2


@pytest.mark.parametrize("caseless, want", [(True, 2), (False, 0)])
def test_config_guard_compares_without_case_where_the_platform_does(box, monkeypatch, caseless, want):
    """SHOULD 3: on macOS `Guarded/Settings.json` is the guarded file."""
    monkeypatch.setattr(policy, "CASELESS_PATHS", caseless)
    shouted = str(box / "guarded" / "settings.json").swapcase()
    assert run(box, config_policy(box), payload("Write", box / "plain", file_path=shouted, content="x")) == want


def test_config_guard_refuses_a_write_through_a_link_from_outside_an_owner(box):
    """SHOULD 6: a link on the way can be moved onto a guarded file between the check and the write."""
    decoy = box / "plain" / "decoy"
    decoy.mkdir()
    (box / "plain" / "hop").symlink_to(decoy)
    text = config_policy(box)
    assert run(box, text, payload("Write", box / "plain", file_path=str(box / "plain" / "hop" / "x"), content="x")) == 2
    assert run(box, text, payload("Write", box / "plain", file_path=str(decoy / "x"), content="x")) == 0
    assert run(box, text, payload("Write", box / "owner", file_path=str(box / "plain" / "hop" / "x"), content="x")) == 0


@pytest.mark.skipif(not hasattr(os, "link"), reason="needs hard links")
def test_config_guard_refuses_a_file_with_more_than_one_name(box):
    """SHOULD 5: a hard link to a guarded file isn't under the guarded folder."""
    guarded = box / "guarded" / "settings.json"
    guarded.write_text("a")
    alias = box / "plain" / "alias"
    os.link(guarded, alias)
    assert run(box, config_policy(box), payload("Write", box / "plain", file_path=str(alias), content="x")) == 2


KILL = (
    "import os, signal, sys\n"
    "from ws import cli, guards\n"
    "if os.environ.get('KILL_GUARD'):\n"
    "    guards.get(os.environ['KILL_GUARD']).judge = lambda *a, **k: os.kill(os.getpid(), signal.SIGKILL)\n"
    "sys.exit(cli.main(sys.argv[1:]))\n"
)

CASES = [
    ("guard-launch", lambda b: LAUNCH, lambda b: payload("Workflow", b / "main", name="x")),
    (
        "config-guard",
        lambda b: config_policy(b),
        lambda b: payload("Write", b / "owner", file_path=str(b / "plain" / "f"), content="x"),
    ),
]


@needs_git
@pytest.mark.parametrize("name, text, body", CASES)
def test_a_guard_that_raises_denies_the_call(box, monkeypatch, name, text, body):
    assert run(box, text(box), body(box)) == 0  # the call is fine until the guard breaks

    def boom(*a, **k):
        raise RuntimeError("planted")

    monkeypatch.setattr(guards.get(name), "judge", boom)
    assert run(box, text(box), body(box)) == 2


@needs_git
@pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="needs a POSIX shell")
@pytest.mark.parametrize("name, text, body", CASES)
def test_a_guard_process_killed_with_sigkill_denies_the_call(box, name, text, body):
    """The generated hook command runs the real `ws gate check`, with only this guard's `judge` made to SIGKILL its own
    process. The same call passes with no sabotage, so a kill that denies can only have come from this guard."""
    (box / "w.toml").write_text(text(box))
    pol = policy.loads(text(box))
    conf = json.loads(
        claude_code.generate(pol, [sys.executable, "-c", KILL], box / "out", box / "w.toml")["settings.json"]
    )
    command = conf["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    src = str(Path(ws.__file__).resolve().parent.parent)
    env = {
        **os.environ,
        "HOME": str(box / "home"),
        "WS_GATE_DAEMON": "0",
        "PYTHONPATH": src,
        "CLAUDE_PROJECT_DIR": str(json.loads(body(box))["cwd"]),
    }

    def hook(kill: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["sh", "-c", command],
            input=body(box),
            text=True,
            env={**env, "KILL_GUARD": kill},
            capture_output=True,
            check=False,
        )

    control = hook("")
    assert control.returncode == 0, control.stderr
    assert hook(name).returncode == 2


@needs_git
def test_a_guard_in_log_mode_lets_the_call_go_where_the_wall_is_proven(box, monkeypatch):
    monkeypatch.setattr(gate, "_platform", lambda: "linux")
    real = shutil.which
    monkeypatch.setattr(shutil, "which", lambda n, *a, **k: "/opt/fake/bwrap" if n == "bwrap" else real(n, *a, **k))
    monkeypatch.setenv(approvals.SANDBOX_MARKER, "1")
    body = payload("Workflow", box / "plain", name="x")
    assert run(box, LAUNCH + '[guards.guard-launch]\nmode = "log"\n', body) == 0
    assert run(box, LAUNCH, body) == 2  # the default is enforce
