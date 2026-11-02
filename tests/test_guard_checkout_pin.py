"""The checkout-pin guard (ws.guards.checkout_pin): a checkout with linked worktrees, or one the policy names, keeps its
HEAD and working tree. The repositories are planted folders (a `.git` folder with an entry under `worktrees/`, or a
`.git` file), so no git program runs."""

from __future__ import annotations

import json
import os

import pytest

from ws import approvals, gate, guards, policy
from ws.adapters import Call
from ws.guards import Context, checkout_pin


@pytest.fixture
def box(tmp_path, monkeypatch):
    """A shared checkout `main` (it has a linked worktree), the linked worktree `wt`, a lone checkout `solo` and a
    folder with no repository."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "home" / ".ws"))
    (tmp_path / "home" / ".ws").mkdir(parents=True)
    main = tmp_path / "main"
    (main / ".git" / "worktrees" / "wt").mkdir(parents=True)
    (main / "src").mkdir()
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / ".git").write_text(f"gitdir: {main}/.git/worktrees/wt\n")
    inner = main / ".claude" / "worktrees" / "inner"  # a linked worktree kept inside the shared checkout
    inner.mkdir(parents=True)
    (inner / ".git").write_text(f"gitdir: {main}/.git/worktrees/inner\n")
    (tmp_path / "solo" / ".git").mkdir(parents=True)
    (tmp_path / "plain").mkdir()
    return {
        "main": main,
        "wt": wt,
        "inner": inner,
        "solo": tmp_path / "solo",
        "plain": tmp_path / "plain",
        "home": tmp_path / "home",
        "tmp": tmp_path,
    }


def cfg_of(**table):
    return checkout_pin.config(table, "[guards.checkout-pin]")


def judge(command, cwd, cfg=None):
    ctx = Context("claude-code", "pre", "s1", str(cwd))
    return checkout_pin.judge(Call("command", command), cfg or cfg_of(), ctx)


def pol(extra=""):
    return policy.loads(f'[guards]\non = ["checkout-pin"]\n{extra}')


MOVES = [
    "git checkout feature",
    "git checkout origin/main",
    "git checkout abc1234",
    "git checkout -",
    "git checkout -b topic",
    "git checkout -B topic origin/main",
    "git checkout --detach",
    "git checkout --detach HEAD~2",
    "git checkout --orphan fresh",
    "git checkout -f",
    "git checkout -fb topic",
    "git checkout .",
    "git checkout notes.txt",
    "git switch feature",
    "git switch -c topic",
    "git switch -C topic",
    "git switch --detach HEAD~1",
    "git switch -",
    "git reset --hard",
    "git reset --hard origin/main",
    "git reset --merge HEAD~1",
    "git reset --keep abc1234",
    "git reset --har HEAD",
    "git rebase main",
    "git rebase -i HEAD~3",
    "git rebase --continue",
    "git merge feature",
    "git merge --no-commit feature",
    "git pull",
    "git pull --rebase origin main",
    "git cherry-pick abc1234",
    "git cherry-pick -n abc1234",
    "git revert abc1234",
    "git -c core.editor=true checkout feature",
    "git --no-pager pull",
    "sudo git pull",
    "git fetch && git checkout feature",
    "echo go; git switch topic",
    "bash -c 'git checkout feature'",
]
FREE = [
    "git checkout -- notes.txt",
    "git checkout -- a.py b.py",
    "git checkout HEAD -- a.py",
    "git checkout origin/main -- docs/",
    "git checkout -p -- a.py",
    "git checkout",
    "git checkout --help",
    "git restore notes.txt",
    "git restore --staged .",
    "git restore --source=HEAD~1 a.py",
    "git add a.py",
    "git commit -m 'switch the branch'",
    "git fetch",
    "git fetch origin main",
    "git status",
    "git log --oneline -5",
    "git diff HEAD",
    "git stash",
    "git branch -a",
    "git merge-base main feature",
    "git reset HEAD a.py",
    "git reset --soft HEAD~1",
    "git reset --mixed HEAD~1",
    "git rebase --abort",
    "git rebase --quit",
    "git merge --abort",
    "git cherry-pick --abort",
    "git revert --quit",
    "git worktree add ../topic -b topic",
    "git worktree list",
    "git switch --help",
    "echo git checkout feature",
    "git commit -m 'git pull then git reset --hard'",
    "ls",
]


@pytest.mark.parametrize("command", MOVES)
def test_a_command_that_moves_head_is_denied_in_a_checkout_with_worktrees(box, command):
    found = judge(command, box["main"])
    assert found is not None and found.action == "deny" and found.approvable, command


@pytest.mark.parametrize("command", FREE)
def test_files_commits_and_reads_go_through_in_that_checkout(box, command):
    assert judge(command, box["main"]) is None, command


@pytest.mark.parametrize("command", MOVES)
def test_a_branch_switch_inside_a_linked_worktree_goes_through(box, command):
    assert judge(command, box["wt"]) is None, command
    assert judge(command, box["inner"]) is None, command  # a worktree inside the shared checkout is its own checkout


def test_a_checkout_with_no_worktrees_and_no_paths_entry_is_free(box):
    for where in (box["solo"], box["plain"], box["tmp"]):
        assert judge("git checkout feature", where) is None


def test_the_reason_names_the_checkout_and_says_to_use_a_worktree(box):
    found = judge("git checkout feature", box["main"] / "src")  # a folder inside the checkout counts
    assert os.path.realpath(box["main"]) in found.reason
    assert "has linked worktrees" in found.reason
    assert "git worktree add" in found.reason and "worktree" in found.reason


def test_paths_pins_a_checkout_with_no_worktrees(box):
    cfg = cfg_of(paths=[str(box["solo"])])
    found = judge("git switch topic", box["solo"], cfg)
    assert found and os.path.realpath(box["solo"]) in found.reason and "paths" in found.reason
    assert judge("git status", box["solo"], cfg) is None
    assert judge("git switch topic", box["plain"], cfg) is None


def test_paths_reads_a_home_relative_path_and_follows_links(box):
    (box["home"] / "code").mkdir()
    os.symlink(box["solo"], box["home"] / "code" / "app")
    cfg = cfg_of(paths=["~/code/app"])
    assert judge("git pull", box["solo"], cfg)


def test_a_worktree_inside_a_named_checkout_is_not_pinned_with_it(box):
    cfg = cfg_of(paths=[str(box["main"])])
    assert judge("git pull", box["inner"], cfg) is None
    assert judge("git pull", box["main"], cfg)


@pytest.mark.parametrize(
    "command",
    [
        "git -C {main} checkout feature",
        "git -C {main}/src pull",
        "git --work-tree {main} reset --hard",
        "git --work-tree={main} reset --hard",
        "GIT_WORK_TREE={main} git rebase main",
        "cd {main} && git checkout feature",
        "cd {main}/src; git switch topic",
        "cd {main} && cd src && git pull",
        "pushd {main} && git merge feature",
    ],
)
def test_the_folder_a_command_runs_in_is_the_one_that_counts(box, command):
    found = judge(command.format(main=box["main"]), box["plain"])
    assert found and os.path.realpath(box["main"]) in found.reason, command


def test_a_command_in_a_free_folder_is_free_even_from_a_pinned_session_folder(box):
    assert judge(f"git -C {box['wt']} checkout feature", box["main"]) is None
    assert judge(f"cd {box['wt']} && git switch topic", box["main"]) is None
    assert judge(f"git -C {box['main']} -C ../wt checkout x", box["plain"]) is None  # -C is relative to the one before


def test_a_relative_c_is_read_from_the_session_folder(box):
    assert judge("git -C .. checkout feature", box["main"] / "src")
    assert judge("git -C ../main checkout feature", box["wt"])
    assert judge("git -C ../wt checkout feature", box["main"]) is None


def test_a_folder_from_a_variable_cannot_be_read_so_a_move_from_it_is_denied(box):
    for command in (
        'git -C "$DIR" checkout feature',
        'cd "$(mktemp -d)" && git pull',
        "cd - && git pull",
        "cd $TARGET; git rebase main",
    ):
        found = judge(command, box["wt"])
        assert found and found.approvable and "variable or substitution" in found.reason, command
    assert judge('git -C "$DIR" status', box["wt"]) is None  # nothing moves
    assert judge('cd "$X" && cd {} && git pull'.format(box["solo"]), box["wt"]) is None  # a literal cd reads again


def test_worktree_move_into_the_checkout_is_denied_and_outside_it_is_free(box):
    inside = judge(f"git -C {box['main']} worktree move {box['wt']} {box['main']}/sub/wt", box["plain"])
    assert inside and "git worktree move" in inside.reason and "outside" in inside.reason
    assert judge(f"git worktree move {box['wt']} sub/wt", box["main"])  # a relative destination, from inside
    assert judge(f"git worktree move {box['wt']} {box['tmp']}/elsewhere", box["main"]) is None
    assert judge(f"git worktree move {box['inner']} {box['solo']}/x", box["main"]) is None
    assert judge("git worktree move", box["main"]) is None


def test_a_session_folder_is_not_needed_to_judge_an_absolute_path(box, monkeypatch):
    monkeypatch.chdir(box["plain"])
    ctx = Context("claude-code", "pre", "s1", "")
    assert checkout_pin.judge(Call("command", f"git -C {box['main']} pull"), cfg_of(), ctx)
    assert checkout_pin.judge(Call("command", "git pull"), cfg_of(), ctx) is None


# --- the git dir, where the shell is, aliases and whole-tree restores ------------------------------------------------


@pytest.mark.parametrize(
    "command",
    [
        "git --git-dir={main}/.git checkout feature",
        "git --git-dir {main}/.git switch feature",
        "git --git-dir={main}/.git --work-tree={plain} checkout feature",
        "GIT_DIR={main}/.git git checkout feature",
        "GIT_DIR={main}/.git git reset --hard",
        "export GIT_DIR={main}/.git; git checkout feature",
        "declare -x GIT_DIR={main}/.git && git checkout feature",
        "GIT_DIR={main}/.git; export GIT_DIR; git reset --hard",
        "export GIT_WORK_TREE={main}; git pull",
    ],
)
def test_a_git_dir_of_the_pinned_checkout_is_judged_whatever_the_folder(box, command):
    found = judge(command.format(main=box["main"], plain=box["plain"]), box["plain"])
    assert found and found.approvable, command


def test_a_git_dir_is_read_for_the_checkout_it_belongs_to(box):
    main_git = box["main"] / ".git"
    wt_git = main_git / "worktrees" / "wt"  # a linked worktree's git dir names its worktree
    (wt_git / "commondir").write_text("../..\n")
    (wt_git / "gitdir").write_text(f"{box['wt']}/.git\n")
    assert judge(f"git --git-dir={wt_git} checkout feature", box["plain"]) is None
    assert judge(f"GIT_DIR={wt_git} git reset --hard", box["plain"]) is None
    assert judge(f"git --git-dir={box['wt']}/.git checkout feature", box["plain"]) is None  # a `.git` file
    assert judge(f"git --git-dir={box['solo']}/.git checkout feature", box["plain"]) is None
    assert judge("git --git-dir=.git checkout feature", box["main"])  # relative to the folder it runs in
    assert judge(f"git --git-dir={main_git} status", box["plain"]) is None  # nothing moves
    cfg = cfg_of(paths=[str(box["solo"])])
    assert judge(f"git --git-dir={box['solo']}/.git switch topic", box["plain"], cfg)


def test_a_git_dir_that_cannot_be_read_is_a_deny_when_head_would_move(box):
    for command in (
        'git --git-dir="$G" checkout x',
        'GIT_DIR="$(pwd)/.git" git pull',
        "export GIT_DIR=$G; git switch x",
    ):
        found = judge(command, box["wt"])
        assert found and "variable or substitution" in found.reason, command
    assert judge('git --git-dir="$G" status', box["wt"]) is None
    assert judge("export GIT_DIR=/x; git status", box["wt"]) is None
    assert judge("(export GIT_DIR=/x); git checkout x", box["wt"]) is None  # a subshell's export ends with it
    assert judge("export PATH=/x; git checkout x", box["wt"]) is None


@pytest.mark.parametrize(
    "command",
    [
        "bisect start",
        "bisect good",
        "bisect bad HEAD~3",
        "bisect new",
        "bisect skip",
        "bisect reset",
        "bisect run make",
        "am fix.patch",
        "am --continue",
        "am --skip",
    ],
)
def test_bisect_and_am_move_head(box, command):
    assert judge(f"git {command}", box["main"]), command
    assert judge(f"git {command}", box["wt"]) is None


@pytest.mark.parametrize(
    "command",
    [
        "bisect log",
        "bisect view",
        "bisect visualize",
        "bisect help",
        "bisect",
        "am --abort",
        "am --quit",
        "am --show-current-patch",
        "am --show-current-patch=diff",
        "bisect --help",
    ],
)
def test_the_reads_and_the_undo_of_bisect_and_am_go_through(box, command):
    assert judge(f"git {command}", box["main"]) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "(cd {tmp}); git checkout x",
        "cd {tmp} | cat; git checkout x",
        "false && cd {tmp}; git checkout x",
        "true || cd {tmp}; git checkout x",
        "cd /does/not/exist; git checkout x",
        "cd /does/not/exist 2>/dev/null; git checkout x",
        "cd {tmp} & git checkout x",
        "(cd {tmp} && true); git checkout x",
        "cd /does/not/exist && cd {tmp}; git checkout x",
        "bash -c 'cd {tmp}'; git checkout x",
        "echo $(cd {tmp}); git checkout x",
    ],
)
def test_a_cd_that_may_not_have_moved_the_shell_leaves_the_pinned_folder_judged(box, command):
    found = judge(command.format(tmp=box["tmp"], wt=box["wt"]), box["main"])
    assert found and os.path.realpath(box["main"]) in found.reason, command


@pytest.mark.parametrize(
    "command",
    [
        "cd {wt} && git checkout x",
        "cd {wt}; git checkout x",
        "cd {wt} && git status && git checkout x",
        "git status && cd {wt} && git checkout x",
        "(cd {wt} && git checkout x)",
        "cd {wt} || git checkout x",
        "cd {plain} && cd {wt} && git switch x",
        "cd {wt}\ngit checkout x",
        "bash -c 'cd {wt} && git checkout x'",
        "{{ cd {wt}; git checkout x; }}",
    ],
)
def test_a_cd_that_does_move_the_shell_to_a_worktree_goes_through(box, command):
    assert judge(command.format(wt=box["wt"], plain=box["plain"]), box["main"]) is None, command


def test_a_cd_into_the_pinned_checkout_is_judged_the_same_way_from_outside(box):
    assert judge(f"(cd {box['main']}); git checkout x", box["plain"]) is None
    assert judge(f"(cd {box['main']} && git checkout x)", box["plain"])
    assert judge(f"cd {box['main']} | cat; git checkout x", box["plain"]) is None
    assert judge(f"cd {box['main']} && git checkout x", box["plain"])
    assert judge(f"true && cd {box['main']} && git checkout x", box["plain"])
    assert judge(f"cd {box['main']}; cd {box['wt']}; git checkout x", box["plain"]) is None


@pytest.mark.parametrize(
    "command",
    [
        "git -c alias.go=checkout go feature",
        "git -c alias.go='checkout -b' go topic",
        "git -c alias.GO=switch go feature",
        "git -c alias.sync=pull sync",
        "git -c alias.a=reset a --hard",
        "git -c alias.go='!git checkout' go feature",
        "git -c alias.go='!f() { git switch \"$@\"; }; f' go feature",
        "git -c alias.x=go -c alias.go=checkout x feature",
        "git -c alias.go='-c core.editor=true checkout' go feature",
    ],
)
def test_an_alias_given_with_c_is_read_as_the_command_it_names(box, command):
    assert judge(command, box["main"]), command
    assert judge(command, box["wt"]) is None, command


def test_an_alias_for_a_command_that_does_not_move_head_goes_through(box):
    assert judge("git -c alias.st=status st", box["main"]) is None
    assert judge("git -c alias.go=checkout go -- a.py", box["main"]) is None
    assert judge("git -c alias.go=checkout status", box["main"]) is None
    assert judge("git -c alias.checkout=status checkout feature", box["main"])  # git ignores an alias for a command


@pytest.mark.parametrize(
    "command",
    [
        "git checkout HEAD~3 -- .",
        "git checkout main -- .",
        "git checkout origin/main -- :/",
        "git checkout -f main -- ./",
        "git checkout main -- ':(top)'",
        "git checkout main -- ':/*'",
        "git restore --source=HEAD~3 .",
        "git restore --source HEAD~3 .",
        "git restore -s main .",
        "git restore -smain .",
        "git restore --source=main --worktree --staged .",
        "git restore --source=main --staged --worktree :/",
        "git restore --source HEAD~5 -W -S .",
        "git restore -Ws main .",
        "git restore --source=main -- .",
        "git stash pop",
        "git stash apply",
        "git stash pop --index",
        "git stash branch topic",
    ],
)
def test_a_whole_tree_restore_from_another_ref_is_denied(box, command):
    found = judge(command, box["main"])
    assert found and found.approvable, command
    assert judge(command, box["wt"]) is None, command


@pytest.mark.parametrize(
    "command",
    [
        "git checkout main -- a.py",
        "git checkout main -- src/",
        "git checkout main -- ./a.py",
        "git checkout -- .",
        "git restore .",
        "git restore --staged .",
        "git restore --source=main a.py",
        "git restore --source=main src/",
        "git restore -s main a.py b.py",
        "git stash",
        "git stash push -m pop",
        "git stash list",
        "git stash show",
        "git stash drop",
    ],
)
def test_a_single_file_restore_and_the_rest_of_stash_stay_allowed(box, command):
    assert judge(command, box["main"]) is None, command


# --- the policy and the gate -------------------------------------------------------------------------------------


def test_it_reads_commands_before_a_call_and_takes_a_paths_list():
    mod = guards.get("checkout-pin")
    assert (mod.EVENTS, mod.KINDS) == (("pre",), ("command",))
    assert cfg_of() == checkout_pin.Config(())


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"x": 1}, r"unknown key 'x'"),
        ({"paths": "~/code"}, r"paths must be a list of non-empty text"),
        ({"paths": [""]}, r"paths must be a list of non-empty text"),
        ({"paths": ["code/app"]}, r"must be an absolute path or start with ~/"),
    ],
)
def test_a_bad_table_is_refused_naming_the_key(table, message):
    with pytest.raises(policy.PolicyError, match=message):
        checkout_pin.config(table, "[guards.checkout-pin]")


def test_it_is_off_unless_the_policy_names_it():
    assert "checkout-pin" not in policy.default_policy().guards.on
    assert "checkout-pin" in policy.default_toml()
    assert policy.loads("").guards.on == ()
    assert "checkout-pin" in pol().guards.on
    assert pol('[guards.checkout-pin]\npaths = ["~/code/app"]\n').guards.config["checkout-pin"].paths == ("~/code/app",)


def hook(command, cwd, session="s1"):
    return json.dumps({"tool_name": "Bash", "session_id": session, "cwd": str(cwd), "tool_input": {"command": command}})


def test_the_gate_denies_a_move_and_allows_a_file_restore(box):
    reason = gate.check("claude-code", hook("git checkout feature", box["main"]), pol(), home=box["home"] / ".ws")
    assert reason and reason.startswith("checkout-pin: ") and "worktree" in reason
    restore = hook("git checkout -- a.py", box["main"])
    assert gate.check("claude-code", restore, pol(), home=box["home"] / ".ws") is None
    off = policy.loads("")
    assert gate.check("claude-code", hook("git checkout feature", box["main"]), off, home=box["home"] / ".ws") is None


def test_the_deny_asks_a_person_and_the_approval_is_used_once(box):
    root = box["home"] / ".ws" / "approvals"
    raw = hook("git pull", box["main"])

    def check():
        return gate.check("claude-code", raw, pol(), approval_root=root, home=box["home"] / ".ws")

    asked = check()
    assert asked and "ws approve" in asked and "checkout-pin" in asked
    ((ident, bound, _),) = approvals.pending(root)
    assert bound == approvals.Bound("claude-code", "guard:checkout-pin", "git pull", str(box["main"]), "s1")
    approvals.grant(root, ident[:12])
    assert check() is None  # approved once
    assert check()  # and that was the one use
