"""The wall against the open class the phase 4 cold reviews found three rounds running: something written inside the wall
that runs later outside it. The wall was a list of paths; this is the table of what a runner or git LOADS OR EXECUTES at
start, and for each row a session inside `ws gate launch` plants it, and the plant must not reach the place the loader
reads. Each row also runs bare (`--no-sandbox`) as a control, where the plant must land, so a pass is not vacuous.

How the table was built (so the next runner is added the same way):
  * Claude Code 2.1 and OpenCode 1.19: the runners' own documentation of what a project can configure (Claude Code:
    `.claude/settings*.json` and its hooks, agents, commands and skills folders, `.mcp.json`; OpenCode: `opencode.json(c)`,
    `tui.json(c)`, and the `plugins`, `tools`, `agents`, `commands` folders of `.opencode/`, global and per project), and
    `strings` on the OpenCode binary for the XDG variables it reads (DATA, STATE, CACHE, CONFIG) and the sign-in files in
    its data folder. Both runners also read their config from every folder up to the repository root, but only the
    project folder is writable in the wall, so the rows are about the project folder and the person's home.
  * git: git-config(1) and githooks(5), and `git config --list --show-origin` to see which files a start reads: the
    hooks folder, `.git/config` (with `core.hooksPath`, `core.fsmonitor`, aliases, credential helpers), an `[include]`
    of a file in the project, `config.worktree`, the submodule repositories under `.git/modules`, and `.git` itself
    (a rename moves hooks and config out and a copy back in).
  * To add a runner: start it in an empty project with an empty HOME, list every project and HOME path it reads
    (`strace -f -e trace=file,execve`, or inotify on the folders; strace is not installed on the machine this was
    written on, so the lists above come from the documentation and the binaries), keep the ones that carry code or
    settings that give a session more power, and add a `Row` for each, in the "missing" and "present" form.
  * CLAUDE.md, AGENTS.md and `.gitmodules` are content, not code, and stay writable (rows marked `open`). So are a repo
    the session creates itself (`git init`) and a config folder in a sub-folder of the project: both are known limits,
    pinned here so a change that closes them is noticed and the docstring in ws.policy is updated.

`ws gate launch` refuses Claude Code (it has its own sandbox), so every row runs `--runner opencode -- bash -c`: the
project lock is the same for every runner, because the person can start the other one in the same folder later.

Linux only (bubblewrap). A missing tool skips, unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import ast
import json
import signal
import stat
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from e2e import test_gate_runners as gr
from e2e import test_sandbox as sb
from e2e import test_wall_escapes as we
from e2e.stubs import AnthropicStub, Call, OpenAIStub
from ws import policy, sandbox

need_wall = sb.need_wall
PLANT = "ws-plant"
RAN = "ws-ran"  # touched at the end of every plant script: proof the script ran to its end
GIT = "git init -q ."


def has(*rel: str, under: str = "work") -> Callable[[Path, Path], bool]:
    """True if any of these paths (a file, or a folder with a file in it) holds the plant."""

    def check(work: Path, home: Path) -> bool:
        for r in rel:
            p = (work if under == "work" else home) / r
            files = [p] if p.is_file() else [f for f in p.rglob("*") if f.is_file()] if p.is_dir() else []
            if any(PLANT in f.read_text(errors="replace") for f in files):
                return True
        return False

    return check


def moved(*rel: str) -> Callable[[Path, Path], bool]:
    return lambda work, home: any((work / r).exists() for r in rel)


@dataclass(frozen=True)
class Row:
    id: str
    loader: str  # who reads or runs it, and when
    plant: str  # bash, run in the project inside the wall (and bare, for the control)
    landed: Callable[[Path, Path], bool]  # did the plant reach the place the loader reads?
    setup: str = ""  # bash, run in the project before the wall
    open: bool = False  # True: a documented gap or content; the plant is expected to land inside the wall


def write(path: str, text: str = PLANT) -> str:
    return f"mkdir -p $(dirname {path}) && printf '%s\\n' {text} > {path}"


def variants(row_id: str, loader: str, plant: str, landed, present: str, setup: str = "") -> list[Row]:
    """The same plant when the folder or file is missing and when it is already there."""
    return [
        Row(f"{row_id}-missing", loader, plant, landed, setup),
        Row(f"{row_id}-present", loader, plant, landed, f"{setup}; {present}" if setup else present),
    ]


ROWS: list[Row] = []

# --- Claude Code, in the project ---------------------------------------------------------------------------------
for name, path in {
    "claude-settings": ".claude/settings.json",
    "claude-settings-local": ".claude/settings.local.json",
    "claude-hook-script": ".claude/hooks/h.sh",
    "claude-command": ".claude/commands/x.md",
    "claude-agent": ".claude/agents/x.md",
    "claude-skill": ".claude/skills/x/SKILL.md",
}.items():
    ROWS += variants(name, "Claude Code at start", write(path), has(path), "mkdir -p .claude")
ROWS += variants(
    "mcp-json",
    "Claude Code at start (starts the servers)",
    write(".mcp.json"),
    has(".mcp.json"),
    "echo '{}' > .mcp.json",
)
ROWS.append(
    Row("claude-folder-rename", "Claude Code", "mv .claude moved && mkdir .claude", moved("moved"), "mkdir .claude")
)

# --- OpenCode, in the project ------------------------------------------------------------------------------------
for name, path in {
    "opencode-plugin": ".opencode/plugins/x.js",
    "opencode-plugin-singular": ".opencode/plugin/x.js",
    "opencode-tool": ".opencode/tools/x.js",
    "opencode-agent": ".opencode/agents/x.md",
    "opencode-command": ".opencode/commands/x.md",
    "opencode-package": ".opencode/package.json",
}.items():
    ROWS += variants(name, "OpenCode at start", write(path), has(path), "mkdir -p .opencode")
for name in ("opencode.json", "opencode.jsonc", "tui.json", "tui.jsonc"):
    ROWS += variants(f"project-{name}", "OpenCode at start", write(name), has(name), f"echo '{{}}' > {name}")
ROWS.append(
    Row(
        "opencode-folder-rename", "OpenCode", "mv .opencode moved && mkdir .opencode", moved("moved"), "mkdir .opencode"
    )
)

# --- OpenCode, in the person's home: its config folder (read-only) and its XDG folders (private in the wall) -----
ROWS += [
    Row(
        "global-config-tool",
        "OpenCode at start",
        write("$HOME/.config/opencode/tools/x.js"),
        has(".config/opencode/tools/x.js", under="home"),
    ),
    Row(
        "cache-program",
        "OpenCode, when it needs ripgrep",
        write("$HOME/.cache/opencode/bin/rg"),
        has(".cache/opencode/bin/rg", under="home"),
    ),
    Row(
        "cache-program-present",
        "OpenCode, when it needs ripgrep",
        write("$HOME/.cache/opencode/bin/rg"),
        has(".cache/opencode/bin/rg", under="home"),
        "mkdir -p $HOME/.cache/opencode/bin",
    ),
    Row(
        "cache-plugin-bin",
        "OpenCode, a package plugin",
        write("$HOME/.cache/opencode/node_modules/.bin/plugin"),
        has(".cache/opencode/node_modules/.bin/plugin", under="home"),
    ),
    Row(
        "data-program",
        "OpenCode, a language server",
        write("$HOME/.local/share/opencode/bin/lsp"),
        has(".local/share/opencode/bin/lsp", under="home"),
    ),
    Row(
        "state-file",
        "OpenCode at start",
        write("$HOME/.local/state/opencode/x"),
        has(".local/state/opencode/x", under="home"),
    ),
    Row(
        "xdg-by-variable",
        "OpenCode, which writes where $XDG_CACHE_HOME says; walled, that is a private folder",
        'd="${XDG_CACHE_HOME:-$HOME/.cache}/opencode"; ' + write('"$d/bin/rg"'),
        has(".cache/opencode/bin/rg", under="home"),
    ),
]

# --- git ---------------------------------------------------------------------------------------------------------
ROWS += variants(
    "git-hook",
    "git commit",
    write(".git/hooks/pre-commit"),
    has(".git/hooks"),
    "mkdir -p .git/hooks",
    GIT + "; rm -rf .git/hooks",
)
ROWS += [
    Row("git-hook-sample-folder", "git commit", write(".git/hooks/pre-commit"), has(".git/hooks"), GIT),
    Row(
        "git-config-append",
        "every git command",
        "printf '[alias]\\n\\tst = ws-plant\\n' >> .git/config",
        has(".git/config"),
        GIT,
    ),
    Row("git-config-by-git", "every git command", "git config core.fsmonitor ws-plant", has(".git/config"), GIT),
    Row("git-config-hookspath", "every git command", "git config core.hooksPath ws-plant", has(".git/config"), GIT),
    Row(
        "git-config-replaced",
        "every git command",
        "rm -f .git/config; echo ws-plant > .git/config",
        has(".git/config"),
        GIT,
    ),
    Row(
        "git-config-worktree",
        "git with extensions.worktreeConfig",
        write(".git/config.worktree"),
        has(".git/config.worktree"),
        GIT,
    ),
    Row("git-folder-rename", "every git command", "mv .git .git2", moved(".git2"), GIT),
    Row(
        "git-folder-rename-and-replant",
        "git commit",
        "mv .git .git2 && mkdir -p .git/hooks && echo ws-plant > .git/hooks/pre-commit",
        lambda w, h: moved(".git2")(w, h) or has(".git/hooks")(w, h),
        GIT,
    ),
    Row(
        "git-pointer-file",
        "every git command in a linked worktree or submodule",
        "echo 'gitdir: ws-plant' > .git",
        has(".git"),
        "git init -q --separate-git-dir=../realgit .",
    ),
    Row(
        "git-pointer-file-rename",
        "every git command in a linked worktree or submodule",
        "mv .git .git2",
        moved(".git2"),
        "git init -q --separate-git-dir=../realgit .",
    ),
    Row(
        "git-submodule-hook",
        "git commit in a submodule",
        write(".git/modules/sub/hooks/post-checkout"),
        has(".git/modules/sub/hooks"),
        f"{GIT}; mkdir -p .git/modules/sub/hooks; touch .git/modules/sub/HEAD .git/modules/sub/config",
    ),
    Row(
        "git-submodule-config",
        "git in a submodule",
        "printf '[alias]\\n\\tst = ws-plant\\n' >> .git/modules/sub/config",
        has(".git/modules/sub/config"),
        f"{GIT}; mkdir -p .git/modules/sub/hooks; touch .git/modules/sub/HEAD .git/modules/sub/config",
    ),
]
# the core.hooksPath route: the hooks live in the project (a husky or .githooks folder), outside .git
ROWS += variants(
    "git-hookspath-folder",
    "git commit, with core.hooksPath set in .git/config",
    write(".githooks/pre-commit"),
    has(".githooks"),
    "mkdir -p .githooks",
    f"{GIT}; git config core.hooksPath .githooks",
)
ROWS += variants(
    "git-hookspath-nested",
    "git commit, with core.hooksPath set in .git/config",
    write(".husky/_/pre-commit"),
    has(".husky"),
    "mkdir -p .husky/_",
    f"{GIT}; git config core.hooksPath .husky/_",
)
ROWS.append(
    Row(
        "git-hookspath-rename",
        "git commit, with core.hooksPath set in .git/config",
        "mv .githooks moved && mkdir .githooks && echo ws-plant > .githooks/pre-commit",
        lambda w, h: moved("moved")(w, h) or has(".githooks")(w, h),
        f"{GIT}; git config core.hooksPath .githooks; mkdir .githooks",
    )
)
ROWS.append(
    Row(
        "git-include-file",
        "every git command, with an [include] of a file in the project",
        "printf '[alias]\\n\\tst = ws-plant\\n' >> local.gitconfig",
        has("local.gitconfig"),
        f"{GIT}; echo '[core]' > local.gitconfig; git config include.path ../local.gitconfig",
    )
)

# --- git: which folder git reads its hooks and config from (the pointers), and folders other than `.git` ----------------
IDENT = f"-c user.name=t -c user.email=t{chr(64)}t.invalid"  # built here: the PII gate is for real addresses
COMMIT = f"git {IDENT} commit -q --allow-empty -m first"
SEPARATE = "git init -q --separate-git-dir=realgit ."  # the git folder is in the project, `.git` is a pointer file
WORKTREE = f"{GIT}; {COMMIT}; git worktree add -q wt"
REDIRECT = "git init -q --bare fake && printf '[alias]\\n\\tzz = !touch alias-ran\\n' >> fake/config && echo ../fake > .git/commondir"


def text_is(rel: str, value: str) -> Callable[[Path, Path], bool]:
    return lambda work, home: (work / rel).is_file() and (work / rel).read_text().strip() == value


ROWS += variants(
    "git-commondir",
    "every git command (commondir names the folder git reads its hooks and config from)",
    write(".git/commondir"),
    has(".git/commondir"),
    "echo . > .git/commondir",
    GIT,
)
ROWS += [
    Row(
        "git-commondir-redirect",
        "every git command: a git folder made in the project, named by commondir, supplies the config",
        REDIRECT,
        text_is(".git/commondir", "../fake"),
        GIT,
    ),
    Row("git-separate-dir-hook", "git commit", write("realgit/hooks/pre-commit"), has("realgit/hooks"), SEPARATE),
    Row(
        "git-separate-dir-config",
        "every git command",
        "printf '[alias]\\n\\tst = ws-plant\\n' >> realgit/config",
        has("realgit/config"),
        SEPARATE,
    ),
    Row(
        "git-separate-dir-commondir",
        "every git command",
        write("realgit/commondir"),
        has("realgit/commondir"),
        SEPARATE,
    ),
    Row("git-separate-dir-rename", "every git command", "mv realgit moved", moved("moved"), SEPARATE),
    Row(
        "git-worktree-commondir",
        "git in a linked worktree",
        write(".git/worktrees/wt/commondir"),
        has(".git/worktrees/wt/commondir"),
        WORKTREE,
    ),
    Row(
        "git-worktree-gitdir",
        "git in a linked worktree",
        write(".git/worktrees/wt/gitdir"),
        has(".git/worktrees/wt/gitdir"),
        WORKTREE,
    ),
    Row(
        "git-worktree-config",
        "git in a linked worktree, with extensions.worktreeConfig",
        write(".git/worktrees/wt/config.worktree"),
        has(".git/worktrees/wt/config.worktree"),
        WORKTREE,
    ),
    Row(
        "git-include-missing-parent",
        "every git command, with an [include] of a file in a folder that is not there",
        "mkdir -p cfg && printf '[alias]\\n\\tst = ws-plant\\n' > cfg/local.gitconfig",
        has("cfg/local.gitconfig"),
        f"{GIT}; git config include.path ../cfg/local.gitconfig",
    ),
]

# --- written on purpose, or a known limit: the plant lands inside the wall --------------------------------------------
ROWS += [
    Row("content-claude-md", "Claude Code: instructions, not code", write("CLAUDE.md"), has("CLAUDE.md"), open=True),
    Row("content-agents-md", "OpenCode: instructions, not code", write("AGENTS.md"), has("AGENTS.md"), open=True),
    Row(
        "content-gitmodules",
        "git submodule: read on request, runs nothing",
        write(".gitmodules"),
        has(".gitmodules"),
        GIT,
        True,
    ),
    Row(
        "limit-subfolder-config",
        "a runner started in a sub-folder (known limit: only the project folder's own config is locked)",
        write("sub/.claude/settings.json"),
        has("sub/.claude/settings.json"),
        open=True,
    ),
    Row(
        "limit-fresh-repo",
        "git commit in a repository the session created (known limit: `git init` must work; the launcher says so)",
        f"{GIT} && echo ws-plant > .git/hooks/pre-commit",
        has(".git/hooks"),
        open=True,
    ),
]


def world(tmp_path: Path, name: str, setup: str) -> Path:
    """A throwaway person: HOME, WS_HOME with the built-in policy, a project folder prepared by `setup` (run bare)."""
    root = tmp_path / name
    (root / "wshome").mkdir(parents=True)
    (root / "wshome" / "policy.toml").write_text(policy.default_toml())
    work = root / "work"
    work.mkdir()
    env = gr.base_env(root)
    if setup:
        done = subprocess.run(
            ["bash", "-c", setup], cwd=work, env=env, capture_output=True, text=True, check=False, timeout=60
        )
        assert done.returncode == 0, f"setup failed: {setup}\n{done.stderr}"
    return root


def run(root: Path, script: str, *flags: str, env: dict | None = None) -> subprocess.CompletedProcess:
    return we.launch(root, script, *flags, env=env)


@pytest.mark.parametrize("row", ROWS, ids=[r.id for r in ROWS])
def test_a_load_path_cannot_be_planted_inside_the_wall(tmp_path, row):
    script = f"{row.plant}; touch {RAN}"
    walled, bare = world(tmp_path, "walled", row.setup), world(tmp_path, "bare", row.setup)
    proc = run(walled, script)
    assert (walled / "work" / RAN).exists(), f"the plant script did not run to its end\n{proc.stderr}"
    got = row.landed(walled / "work", walled / "home")
    assert got == row.open, (
        f"[{row.loader}] "
        + ("the plant landed inside the wall" if got else "the documented gap is closed: flip the row")
        + f"\n{proc.stderr}"
    )
    run(bare, script, "--no-sandbox")
    assert row.landed(bare / "work", bare / "home"), "control: with no wall the same plant must land"


# --- the runner's own folders are private in the wall -----------------------------------------------------------


def key(root: Path) -> str:
    return sandbox.project_key(str((root / "work").resolve()))


def test_a_walled_run_gets_its_own_xdg_folders_that_persist_and_the_real_ones_stay_clean(tmp_path):
    root = world(tmp_path, "w", "")
    home = root / "home"
    probe = 'printf "%s\\n" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" > vars.txt; echo one > "$XDG_CACHE_HOME/seen"'
    proc = run(root, probe)
    data, state, cache = (root / "work" / "vars.txt").read_text().split()
    wall = (root / "wshome" / "wall" / "opencode").resolve()
    # data and cache are kept per project, state is shared: see sandbox.private_homes
    assert (data, state, cache) == (
        str(wall / "data" / key(root)),
        str(wall / "state"),
        str(wall / "cache" / key(root)),
    ), proc.stderr
    assert (wall / "cache" / key(root) / "seen").read_text() == "one\n"
    assert not (home / ".cache" / "opencode").exists() and not (home / ".local" / "share" / "opencode").exists()
    assert (root / "wshome" / "wall" / ".gitignore").read_text() == "*\n"
    assert stat.S_IMODE(wall.parent.stat().st_mode) == 0o700
    again = run(root, 'cat "$XDG_CACHE_HOME/seen" > again.txt')  # a second run sees the first one's state
    assert (root / "work" / "again.txt").read_text() == "one\n", again.stderr
    # the unwalled run is never pointed there
    run(root, '[ -z "${XDG_CACHE_HOME+x}" ] && echo unset > bare.txt', "--no-sandbox")
    assert (root / "work" / "bare.txt").read_text() == "unset\n"


def test_the_sign_in_is_copied_in_and_the_real_one_cannot_be_written(tmp_path):
    root = world(tmp_path, "w", "")
    real = root / "home" / ".local" / "share" / "opencode"
    real.mkdir(parents=True)
    (real / "auth.json").write_text('{"k": 1}')
    proc = run(
        root, 'cp "$XDG_DATA_HOME/opencode/auth.json" copy.json; echo x >> "$HOME/.local/share/opencode/auth.json"'
    )
    assert (root / "work" / "copy.json").read_text() == '{"k": 1}', proc.stderr
    assert (real / "auth.json").read_text() == '{"k": 1}'
    seeded = root / "wshome" / "wall" / "opencode" / "data" / key(root) / "opencode" / "auth.json"
    assert stat.S_IMODE(seeded.stat().st_mode) == 0o600
    (real / "auth.json").unlink()  # logged out: the copy goes too
    run(root, "true")
    assert not seeded.exists()


def test_a_sign_in_the_policy_hides_is_not_copied(tmp_path):
    root = world(tmp_path, "w", "")
    (root / "wshome" / "policy.toml").write_text(
        policy.default_toml().replace('deny_read = ["', 'deny_read = ["~/.local/share/opencode/**", "', 1)
    )
    real = root / "home" / ".local" / "share" / "opencode"
    real.mkdir(parents=True)
    (real / "auth.json").write_text("secret")
    run(root, "true")
    assert not (root / "wshome" / "wall" / "opencode" / "data" / key(root) / "opencode" / "auth.json").exists()


# --- nothing is left in the person's project --------------------------------------------------------------------


def snapshot(work: Path) -> list[str]:
    return sorted(str(p.relative_to(work)) for p in work.rglob("*"))


@pytest.mark.parametrize("setup", ["", GIT, f"{GIT}; rm -rf .git/hooks", f"{GIT}; git config core.hooksPath .husky/_"])
def test_the_placeholders_are_gone_after_the_run(tmp_path, setup):
    root = world(tmp_path, "w", setup)
    before = snapshot(root / "work")
    seen = run(root, "ls -A > listing.txt")
    after = snapshot(root / "work")
    assert [p for p in after if p != "listing.txt"] == before, seen.stderr
    inside = (root / "work" / "listing.txt").read_text().split()
    assert ".claude" in inside and ".opencode" in inside  # they were there for the run


def test_a_project_config_that_is_a_symbolic_link_is_refused(tmp_path):
    root = world(tmp_path, "w", "mkdir -p ../shared && ln -s ../shared .claude")
    proc = run(root, "touch ran")
    assert proc.returncode != 0 and "symbolic link" in proc.stderr and not (root / "work" / "ran").exists()
    assert (root / "work" / ".claude").is_symlink()  # and nothing was changed


def test_a_repository_the_session_creates_is_reported_when_it_ends(tmp_path):
    root = world(tmp_path, "w", "")
    assert "new a git folder: .git" in run(root, GIT).stderr
    assert "wall report" not in run(root, "true").stderr


def test_another_project_gets_its_own_data_and_cache_but_shares_state(tmp_path):
    one, two = world(tmp_path, "one", ""), world(tmp_path, "two", "")
    seen = {}
    for name, root in (("one", one), ("two", two)):
        run(root, 'printf "%s\\n" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" > vars.txt')
        seen[name] = (root / "work" / "vars.txt").read_text().split()
    assert seen["one"][0] != seen["two"][0] and seen["one"][2] != seen["two"][2]
    assert Path(seen["one"][0]).name == key(one) and Path(seen["two"][2]).name == key(two)


def test_a_link_planted_in_the_private_folders_is_not_followed_when_the_sign_in_is_copied(tmp_path):
    root = world(tmp_path, "w", "")
    real = root / "home" / ".local" / "share" / "opencode"
    real.mkdir(parents=True)
    (real / "auth.json").write_text("sign-in")
    run(root, "true")  # makes the private folders
    app = root / "wshome" / "wall" / "opencode" / "data" / key(root) / "opencode"
    victim = root / "home" / "victim"
    victim.write_text("keep")
    # what a walled session could do: leave links where the launcher will write next time
    run(root, f'ln -s {victim} "$XDG_DATA_HOME/opencode/auth.json.new"')
    assert (app / "auth.json.new").is_symlink()
    run(root, "true")
    assert victim.read_text() == "keep" and (app / "auth.json").read_text() == "sign-in"
    assert not (app / "auth.json.new").exists() and not (app / "auth.json").is_symlink()
    other = root / "home" / "elsewhere"
    other.mkdir()
    run(root, f'rm -rf "$XDG_DATA_HOME/opencode"; ln -s {other} "$XDG_DATA_HOME/opencode"')
    run(root, "touch ran")  # the link is removed (not followed) and the folder made again
    assert not any(other.iterdir()) and (root / "work" / "ran").exists()
    assert not app.is_symlink() and (app / "auth.json").read_text() == "sign-in"


# --- Claude Code: no launcher, so its own sandbox and Edit rules carry the same project lock ----------------------------


def py_write(path: str) -> str:
    """A write the text rules cannot read: only the kernel layer of Claude Code's sandbox can stop it."""
    return (
        f"python3 -c \"import os; os.makedirs(os.path.dirname('{path}') or '.', exist_ok=True); "
        f"open('{path}', 'w').write('ws-plant')\""
    )


# Not here: `.claude` itself. Claude Code asks before it writes there whatever the settings say, so `claude -p` cannot even
# attempt it (the control would fail); the rule for it is in the settings and tested as text in tests/test_sandbox.py.
CLAUDE_BASH = {  # id: (setup, what the Bash tool runs, where it must not land)
    "opencode-plugin": ("", py_write(".opencode/plugins/x.js"), ".opencode"),
    "opencode-json": ("", py_write("opencode.json"), "opencode.json"),
    "mcp-json": ("", py_write(".mcp.json"), ".mcp.json"),
    "git-hook": (GIT, py_write(".git/hooks/pre-commit"), ".git/hooks"),
    "git-config": (GIT, py_write(".git/config"), ".git/config"),
    "git-commondir": (GIT, py_write(".git/commondir"), ".git/commondir"),
    "separate-git-dir-hook": (SEPARATE, py_write("realgit/hooks/pre-commit"), "realgit/hooks"),
    "separate-git-dir-commondir": (SEPARATE, py_write("realgit/commondir"), "realgit/commondir"),
}
CLAUDE_WRITE = {  # id: (setup, the file the Write tool is asked to create, relative to the project)
    "opencode-plugin": ("", ".opencode/plugins/x.js"),
    "opencode-json": ("", "opencode.json"),
    "nested-opencode-plugin": ("", "sub/.opencode/plugins/x.js"),  # the Edit rules say **/, so a sub-folder is held too
    "nested-opencode-json": ("", "sub/opencode.json"),
}


def claude_world(tmp_path, monkeypatch, name: str, setup: str) -> Path:
    root = world(tmp_path, name, setup)
    monkeypatch.setenv("WS_HOME", str(root / "wshome"))
    monkeypatch.setenv("HOME", str(root / "home"))
    monkeypatch.chdir(tmp_path)  # not the project: the shared settings are made somewhere else
    return root


def claude_try(root: Path, call, layers: tuple[str, ...]) -> Path:
    """Claude Code (the real runner, a stub model) with only the named layers of the generated settings: `sandbox`
    (shell commands) and `permissions` (the Edit rules, which cover Write). The hooks are always taken out, so only the
    layer under test can stop the call."""
    settings = gr.generate(root, "claude-code") / "settings.json"
    conf = json.loads(settings.read_text())
    del conf["hooks"]
    for layer in ("sandbox", "permissions"):
        if layer not in layers:
            del conf[layer]
    settings.write_text(json.dumps(conf))
    with AnthropicStub([call]) as stub:
        if "sandbox" in layers:  # the way a person starts it: the settings are made for this project at the start
            proc = claude_via_launcher(root, stub)
        else:
            proc = gr.claude_run(root, stub, settings, False, root / "work")
    assert stub.tool_results(), f"the runner never reported the call back\n{gr.where(proc)}"
    return root / "work"


def claude_via_launcher(root: Path, stub) -> subprocess.CompletedProcess:
    """`ws gate launch --runner claude-code -- claude -p ...` against the stub model, as gr.claude_run starts claude."""
    env = gr.base_env(root)
    env.update(
        ANTHROPIC_BASE_URL=stub.url,
        ANTHROPIC_API_KEY="sk-ant-" + "stub" * 8,  # fake, built at run time
        CLAUDE_CONFIG_DIR=str(root / "claude-config"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    cmd = [sys.executable, "-m", "ws", "gate", "launch", "--runner", "claude-code", "--", gr.runner_bin("claude")]
    cmd += ["-p", "run the command", "--allowedTools", "Bash,Read,Write,Edit,Grep,Glob"]
    cmd += ["--model", "claude-sonnet-4-5", "--no-session-persistence", "--setting-sources", "user"]
    return gr.run(cmd, env, root / "work")


@pytest.mark.parametrize("name", CLAUDE_BASH)
def test_claude_codes_sandbox_holds_the_project_config_too(tmp_path, monkeypatch, name):
    """Started with `ws gate launch --runner claude-code`: the settings carry THIS project's paths, whatever folder
    `ws install` or `ws gate generate` ran in (the tests run them in a different one)."""
    setup, command, where = CLAUDE_BASH[name]
    call = Call("Bash", {"command": f"{command}; touch {RAN}"})
    walled = claude_try(claude_world(tmp_path, monkeypatch, "walled", setup), call, ("sandbox",))
    assert (walled / RAN).exists(), "the control write in the same command did not run"
    assert not has(where)(walled, walled), f"the plant landed past Claude Code's sandbox: {where}"
    bare = claude_try(claude_world(tmp_path, monkeypatch, "bare", setup), call, ())
    assert has(where)(bare, bare), "control: with no sandbox the same plant must land"


@pytest.mark.parametrize("name", CLAUDE_WRITE)
def test_claude_codes_edit_rules_hold_the_project_config_for_the_write_tool(tmp_path, monkeypatch, name):
    setup, rel = CLAUDE_WRITE[name]
    root = claude_world(tmp_path, monkeypatch, "walled", setup)
    call = Call("Write", {"file_path": str(root / "work" / rel), "content": PLANT})
    claude_try(root, call, ("permissions",))
    assert not has(rel)(root / "work", root), "the Write tool planted past the Edit deny rules"
    other = claude_world(tmp_path, monkeypatch, "bare", setup)
    claude_try(other, Call("Write", {"file_path": str(other / "work" / rel), "content": PLANT}), ())
    assert has(rel)(other / "work", other), "control: with no rules the Write tool must create it"


# --- the loader actually runs it: a control per class, with real repos and real runners --------------------------------

FIRE = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_COMMITTER_NAME": "t",
    "GIT_AUTHOR_EMAIL": f"t{chr(64)}t.invalid",
    "GIT_COMMITTER_EMAIL": f"t{chr(64)}t.invalid",
}


def fire(root: Path, command: str) -> None:
    """Run `command` in the project the way the person would next: bare, outside the wall, in their HOME."""
    env = {**gr.base_env(root), **FIRE}
    subprocess.run(
        ["bash", "-c", command], cwd=root / "work", env=env, capture_output=True, text=True, timeout=60, check=False
    )


def test_a_planted_git_hook_fires_on_the_next_commit_outside_the_wall_only(tmp_path):
    plant = "printf '#!/bin/sh\\ntouch hook-ran\\n' > .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit"
    walled, bare = world(tmp_path, "walled", GIT), world(tmp_path, "bare", GIT)
    run(walled, plant)
    run(bare, plant, "--no-sandbox")
    for root in (walled, bare):
        fire(root, "git commit -q --allow-empty -m x")
    assert (bare / "work" / "hook-ran").exists(), "control: the planted hook must fire on a real commit"
    assert not (walled / "work" / "hook-ran").exists()


@pytest.mark.parametrize(
    "plant, setup",
    [
        ("printf '[alias]\\n\\tzz = !touch alias-ran\\n' >> .git/config", GIT),
        (REDIRECT, GIT),
        (
            "git init -q --bare fake; printf '[alias]\\n\\tzz = !touch alias-ran\\n' >> fake/config; echo ../fake > realgit/commondir",
            SEPARATE,
        ),
        ("printf '[alias]\\n\\tzz = !touch alias-ran\\n' >> realgit/config", SEPARATE),
    ],
    ids=["config", "commondir-redirect", "separate-dir-commondir", "separate-dir-config"],
)
def test_a_planted_git_alias_runs_on_the_next_git_command_outside_the_wall_only(tmp_path, plant, setup):
    walled, bare = world(tmp_path, "walled", setup), world(tmp_path, "bare", setup)
    run(walled, plant)
    run(bare, plant, "--no-sandbox")
    for root in (walled, bare):
        fire(root, "git zz")
    assert (bare / "work" / "alias-ran").exists(), "control: the planted alias must run on a real git command"
    assert not (walled / "work" / "alias-ran").exists()


def test_git_still_commits_inside_the_wall_with_the_placeholders_in_place(tmp_path):
    """The commondir placeholder ('.') and the empty config.worktree must not break git itself."""
    root = world(tmp_path, "w", GIT)
    proc = run(
        root, f"echo a > a && git add a && git {IDENT} commit -q -m one && git log --oneline | wc -l > n", env=FIRE
    )
    assert (root / "work" / "n").read_text().strip() == "1", proc.stderr


def test_a_planted_opencode_plugin_loads_on_the_next_unwalled_start_only(tmp_path):
    plugin = (
        'import {writeFileSync} from "fs"; export const P = async () => { writeFileSync("plugin-ran", "1"); return {} }'
    )
    plant = f"mkdir -p .opencode/plugins && cat > .opencode/plugins/x.js <<'EOF'\n{plugin}\nEOF"
    outcomes = {}
    for name, flags in (("walled", ()), ("bare", ("--no-sandbox",))):
        root = world(tmp_path, name, "")
        run(root, plant, *flags)
        out = gr.generate(root, "opencode")
        (out / "plugins" / "ws-gate.js").unlink()
        conf = json.loads((out / "opencode.json").read_text())
        del conf["permission"]
        (out / "opencode.json").write_text(json.dumps(conf))
        with OpenAIStub([]) as stub:
            gr.opencode_run(root, stub, out, root / "work")  # unwalled: the next start, as the person would
        outcomes[name] = (root / "work" / "plugin-ran").exists()
    assert outcomes == {"walled": False, "bare": True}, outcomes


def test_a_planted_claude_hook_runs_on_the_next_unwalled_start_only(tmp_path, monkeypatch):
    settings = '{"hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "touch hook-ran"}]}]}}'
    plant = f"mkdir -p .claude && echo '{settings}' > .claude/settings.json"
    outcomes = {}
    for name, flags in (("walled", ()), ("bare", ("--no-sandbox",))):
        root = claude_world(tmp_path, monkeypatch, name, "")
        run(root, plant, *flags)
        plain = root / "plain.json"
        plain.write_text("{}")
        with AnthropicStub([Call("Bash", {"command": "true"})]) as stub:
            gr.claude_run(root, stub, plain, False, root / "work", sources="user,project")
        outcomes[name] = (root / "work" / "hook-ran").exists()
    assert outcomes == {"walled": False, "bare": True}, outcomes


# --- several sessions, a launcher that dies, the signals ----------------------------------------------------------------


def start(root: Path, script: str, *flags: str) -> subprocess.Popen:
    env = gr.base_env(root)
    return subprocess.Popen(
        [*sb.WALL, *flags, "--", "bash", "-c", script], cwd=root / "work", env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )  # fmt: skip


def wait_for(path: Path, seconds: float = 15.0) -> bool:
    return we.wait_for(path, seconds)


def test_two_sessions_in_one_project_the_first_out_leaves_the_placeholders_for_the_second(tmp_path):
    root = world(tmp_path, "w", "")
    work = root / "work"
    first = start(root, "touch first-in; sleep 4")
    assert wait_for(work / "first-in")
    second = start(root, "touch second-in; sleep 9; mkdir .claude/x 2> mkdir-err; touch second-out")
    assert wait_for(work / "second-in")
    first.communicate(timeout=30)  # the first leaves while the second is inside
    assert (work / ".claude").is_dir(), "the first launcher took the placeholders down under the second session"
    second.communicate(timeout=60)
    assert (work / "second-out").exists() and "ead-only" in (work / "mkdir-err").read_text()
    assert not (work / ".claude").exists() and not (work / ".opencode").exists()  # the last one out tidied up


def test_a_hangup_still_tidies_up_and_a_killed_launcher_is_cleaned_by_the_next_one(tmp_path):
    root = world(tmp_path, "w", "")
    work = root / "work"
    hung = start(root, "touch in; sleep 60")
    assert wait_for(work / "in") and (work / ".claude").is_dir()
    hung.send_signal(signal.SIGHUP)
    hung.communicate(timeout=30)
    assert not (work / ".claude").exists(), "SIGHUP left the placeholders behind"
    killed = start(root, "touch in2; sleep 60")
    assert wait_for(work / "in2")
    killed.kill()
    killed.communicate(timeout=30)
    assert (work / ".claude").is_dir()  # nothing could tidy up after a SIGKILL...
    run(root, "true")
    assert not (work / ".claude").exists(), "...but the next launch must clean up what the dead one left"


# --- the end-of-run report ----------------------------------------------------------------------------------------------

LEFT = (
    "mkdir -p sub/.claude sub/.opencode/plugins nested && echo ws-plant > sub/.claude/settings.json "
    "&& echo ws-plant > sub/.opencode/plugins/x.js && echo ws-plant > sub/opencode.json "
    "&& git init -q nested && echo '#!/bin/sh' > nested/.git/hooks/pre-commit "
    "&& git -C nested config alias.zz '!true' && mkdir -p .vscode .venv/bin node_modules/.bin "
    "&& echo ws-plant > .vscode/tasks.json && echo ws-plant > .venv/bin/evil && echo ws-plant > node_modules/.bin/evil "
    "&& echo ws-plant > mise.toml && echo ws-plant > .pre-commit-config.yaml"
)


def test_the_launcher_reports_what_the_session_left_that_the_next_command_would_run(tmp_path):
    root = world(tmp_path, "w", "")
    proc = run(root, LEFT)
    said = proc.stderr
    for path in (
        "sub/.claude",
        "sub/.opencode",
        "sub/opencode.json",
        "nested/.git",
        ".vscode/tasks.json",
        ".venv/bin/evil",
        "node_modules/.bin/evil",
        "mise.toml",
        ".pre-commit-config.yaml",
    ):
        assert path in said, f"{path} is not in the report:\n{said}"
    assert "hooks: pre-commit" in said and "alias.zz" in said and "removed nothing" in said
    assert (root / "work" / "sub" / ".claude" / "settings.json").exists()  # report only, nothing deleted
    log = [json.loads(line) for line in (root / "wshome" / "logs" / "wall-report.jsonl").read_text().splitlines()]
    assert len(log) == 1 and {f["path"] for f in log[0]["findings"]} >= {"sub/.claude", "nested/.git", "mise.toml"}


def test_a_clean_session_reports_nothing_and_the_second_run_does_not_repeat_the_first(tmp_path):
    root = world(tmp_path, "w", "")
    assert "wall report" not in run(root, "echo a > a.txt").stderr
    assert not (root / "wshome" / "logs" / "wall-report.jsonl").exists()
    run(root, LEFT)
    again = run(root, "echo b > b.txt")  # what is there already is not new
    assert "wall report" not in again.stderr, again.stderr


def test_a_changed_hook_in_an_existing_nested_repo_is_reported(tmp_path):
    root = world(tmp_path, "w", "git init -q nested")
    proc = run(root, "echo '#!/bin/sh' > nested/.git/hooks/post-commit")
    assert "changed a git folder: nested/.git" in proc.stderr and "post-commit" in proc.stderr


# --- reaching into another process: ptrace and process_vm_* are refused by the seccomp filter ---------------------------

PROBE = r"""
import ctypes
import os
import sys

ppid = os.getppid()
libc = ctypes.CDLL(None, use_errno=True)
out = {}

# 1. /proc/PPID/mem
try:
    start = next(line.split("-")[0] for line in open(f"/proc/{ppid}/maps") if "r-" in line.split()[1])
    fd = os.open(f"/proc/{ppid}/mem", os.O_RDONLY)
    os.pread(fd, 8, int(start, 16))
    out["procmem"] = "read"
except OSError as e:
    out["procmem"] = f"errno {e.errno}"

# 2. PTRACE_ATTACH
libc.ptrace.restype = ctypes.c_long
r = libc.ptrace(0x4206, ppid, 0, 0)  # PTRACE_SEIZE: the ATTACH permission check, without stopping the target
out["ptrace"] = "attached" if r == 0 else f"errno {ctypes.get_errno()}"
if r == 0:
    libc.ptrace(17, ppid, 0, 0)  # detach

# 3. process_vm_readv


class Iov(ctypes.Structure):
    _fields_ = [("base", ctypes.c_void_p), ("len", ctypes.c_size_t)]


buf = ctypes.create_string_buffer(8)
local, remote = Iov(ctypes.cast(buf, ctypes.c_void_p).value, 8), Iov(int(start, 16), 8)
n = libc.process_vm_readv(ppid, ctypes.byref(local), 1, ctypes.byref(remote), 1, 0)
out["process_vm_readv"] = "read" if n == 8 else f"errno {ctypes.get_errno()}"
print(out)
"""


def yama_allows() -> bool:
    try:
        return Path("/proc/sys/kernel/yama/ptrace_scope").read_text().strip() == "0"
    except OSError:
        return True  # no Yama: same-user attach is allowed


def probe(tmp_path: Path, name: str, *flags: str) -> dict:
    root = world(tmp_path, name, "")
    (root / "work" / "probe.py").write_text(PROBE)
    proc = run(root, "python3 probe.py > out.txt; true", *flags)
    return ast.literal_eval((root / "work" / "out.txt").read_text()), proc


def test_a_walled_child_cannot_ptrace_or_process_vm_read_the_process_that_started_it(tmp_path):
    seen, proc = probe(tmp_path, "walled")
    assert seen["ptrace"] == "errno 1" and seen["process_vm_readv"] == "errno 1", (seen, proc.stderr)
    if not yama_allows():
        pytest.skip("ptrace_scope is above 0 on this host: the bare control cannot attach either")
    bare, _ = probe(tmp_path, "bare", "--no-sandbox")
    assert bare["ptrace"] == "attached" and bare["process_vm_readv"] == "read", f"control: {bare}"
    assert bare["procmem"] == "read"


def test_proc_pid_mem_of_the_parent_is_still_reachable_a_known_limit(tmp_path):
    """Reading (and writing) /proc/PID/mem of a process of the same user is the same reach as ptrace, by file: it is
    governed by Yama and the dumpable flag, not by a system call, so the seccomp filter cannot refuse it, and bubblewrap
    drops the capability that would let a non-dumpable target be read, but the runner is dumpable. Pinned here so that a
    change which closes it (a runner started non-dumpable, say) is noticed and ws.policy's limits are updated."""
    seen, _ = probe(tmp_path, "walled")
    if yama_allows():  # ptrace_scope 0 (or no Yama): the known limit
        assert seen["procmem"] == "read"
    else:  # ptrace_scope 1 or more (CI's Ubuntu): a child cannot read its parent, which is the good outcome
        assert seen["procmem"].startswith("errno"), seen


def test_started_without_the_launcher_claude_codes_interpreter_is_not_held_but_the_edit_rules_are(
    tmp_path, monkeypatch
):
    """What the docs say: plain `claude --settings shared.json` has no project paths in denyWrite."""
    root = claude_world(tmp_path, monkeypatch, "plain", GIT)
    settings = gr.generate(root, "claude-code") / "settings.json"
    conf = json.loads(settings.read_text())
    del conf["hooks"], conf["permissions"]
    settings.write_text(json.dumps(conf))
    call = Call("Bash", {"command": py_write(".opencode/plugins/x.js")})
    with AnthropicStub([call]) as stub:
        gr.claude_run(root, stub, settings, False, root / "work")
    assert has(".opencode")(root / "work", root), (
        "if this now fails the shared settings hold the project: update the docs"
    )


# --- the report does not read through what the session planted ------------------------------------------------------------


def test_the_report_does_not_copy_a_denied_file_through_a_planted_link_and_a_fifo_does_not_hang_it(tmp_path):
    root = world(tmp_path, "w", "mkdir -p ../home/.ssh && echo CANARY-KEY-MATERIAL > ../home/.ssh/id_rsa")
    plant = (
        "mkdir -p sub sub2 && ln -s $HOME/.ssh/id_rsa sub/.git && ln -s $HOME/.ssh sub2/.git && mkfifo sub/.mcp.json "
        "&& ln -s /dev/zero sub/opencode.json && echo hidden > /dev/null"
    )
    proc = run(root, plant)  # the run's own timeout is the hang check
    assert "CANARY-KEY-MATERIAL" not in proc.stderr
    assert (root / "work" / "sub" / ".git").is_symlink()
    assert "a link to" in proc.stderr and "not a regular file" in proc.stderr, proc.stderr
    log = (root / "wshome" / "logs" / "wall-report.jsonl").read_text()
    assert "CANARY-KEY-MATERIAL" not in log and "id_rsa" in log  # the link's own text, nothing read through it
    again = run(root, "true")  # the pre-run scan meets the same entries
    assert "CANARY" not in again.stderr


# --- OpenCode marks itself undumpable: its memory is not readable from a shell command it starts ---------------------------

ANCESTORS = """
import os
out = []
pid = os.getppid()
while pid > 1:
    try:
        comm = open(f"/proc/{pid}/comm").read().strip()
        ppid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
    except OSError:
        break
    try:
        start = next(l.split("-")[0] for l in open(f"/proc/{pid}/maps") if "r-" in l.split()[1])
        fd = os.open(f"/proc/{pid}/mem", os.O_RDONLY)
        os.pread(fd, 8, int(start, 16))
        out.append(f"{comm}=read")
    except OSError as e:
        out.append(f"{comm}=errno{e.errno}")
    pid = ppid
print("ANCESTORS", " ".join(out))
"""


def opencode_reads(tmp_path: Path, name: str, keep_shim: bool) -> list[str]:
    root = world(tmp_path, name, "")
    (root / "work" / "ancestors.py").write_text(ANCESTORS)
    out = gr.generate(root, "opencode")
    if not keep_shim:
        (out / "plugins" / "ws-gate.js").unlink()
    conf = json.loads((out / "opencode.json").read_text())
    del conf["permission"]
    (out / "opencode.json").write_text(json.dumps(conf))
    call = Call("bash", {"command": "python3 ancestors.py", "description": "look"})
    with OpenAIStub([call]) as stub:
        gr.opencode_run(root, stub, out, root / "work", wrap=[*sb.WALL, "--"])
    seen = " ".join(stub.tool_results())
    assert "ANCESTORS" in seen, seen
    return seen.split("ANCESTORS", 1)[1].split()


def test_opencode_is_not_readable_through_proc_mem_from_its_shell_commands_with_the_shim_only(tmp_path):
    if not yama_allows():
        pytest.skip("ptrace_scope is above 0 on this host: a child cannot read its parent whatever the shim does")
    with_shim = opencode_reads(tmp_path, "shim", True)
    without = opencode_reads(tmp_path, "noshim", False)
    mine = [r for r in with_shim if r.split("=")[0].lower().startswith(("opencode", ".opencode", "bun"))]
    assert mine and all("=errno" in r for r in mine), f"the runner is readable with the shim: {with_shim}"
    control = [r for r in without if r.split("=")[0].lower().startswith(("opencode", ".opencode", "bun"))]
    assert control and any(r.endswith("=read") for r in control), f"control: without the shim it must be: {without}"
