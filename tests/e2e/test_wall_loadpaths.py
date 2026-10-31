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

import stat
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from e2e import test_gate_runners as gr
from e2e import test_sandbox as sb
from e2e import test_wall_escapes as we
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


def test_a_walled_run_gets_its_own_xdg_folders_that_persist_and_the_real_ones_stay_clean(tmp_path):
    root = world(tmp_path, "w", "")
    home = root / "home"
    probe = 'printf "%s\\n" "$XDG_DATA_HOME" "$XDG_STATE_HOME" "$XDG_CACHE_HOME" > vars.txt; echo one > "$XDG_CACHE_HOME/seen"'
    proc = run(root, probe)
    data, state, cache = (root / "work" / "vars.txt").read_text().split()
    wall = (root / "wshome" / "wall" / "opencode").resolve()
    assert (data, state, cache) == tuple(str(wall / k) for k in ("data", "state", "cache")), proc.stderr
    assert (wall / "cache" / "seen").read_text() == "one\n"
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
    seeded = root / "wshome" / "wall" / "opencode" / "data" / "opencode" / "auth.json"
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
    assert not (root / "wshome" / "wall" / "opencode" / "data" / "opencode" / "auth.json").exists()


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


def test_a_repository_the_session_creates_is_flagged_when_it_ends(tmp_path):
    root = world(tmp_path, "w", "")
    assert sandbox.NEW_REPO_NOTE in run(root, GIT).stderr
    assert sandbox.NEW_REPO_NOTE not in run(root, "true").stderr
