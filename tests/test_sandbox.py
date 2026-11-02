"""The sandbox wall (ws.sandbox, and the `sandbox` block the claude-code adapter writes). What really stops a bypass is
in tests/e2e/test_sandbox.py; here the command lines, the seccomp program (run through a small BPF interpreter), the
profile text and the fail-closed paths are checked without running a sandbox."""

from __future__ import annotations

import json
import os
import re
import signal
import struct
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from ws import approvals, cli, jobs, jsonl, policy, sandbox
from ws.adapters import claude_code, opencode
from ws.output import WsError


def sandbox_policy():
    """Built when a test asks (loading a policy reads WS_HOME and the profile, which a test sets)."""
    return policy.loads(
        '[gate]\ndeny_read = ["~/.ssh/**", "~/.aws/credentials", "$HOME/.gnupg/**", "~/.s*/id_rsa", "/"]\n'
    )


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".aws").mkdir()
    (h / ".aws" / "credentials").write_text("x")
    return h


@pytest.fixture
def wshome(tmp_path):
    w = tmp_path / "wshome"
    w.mkdir()
    return w


def which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- what gets hidden -------------------------------------------------------------------------------------------


def test_read_rules_become_paths_a_sandbox_can_hide():
    # a trailing ** goes; a wildcard in the middle, the root and a bare ~ are left to the matcher
    toolkit = [r.removesuffix("/**") for r in policy.secret_read_rules()]  # added to every policy that is read
    assert policy.sandbox_read_paths(sandbox_policy()) == ["~/.ssh", "~/.aws/credentials", "~/.gnupg", *toolkit]
    left = policy.loads('[gate]\ndeny_read = ["~", "~/**", "**/.ssh/**", "relative"]\n')
    assert policy.sandbox_read_paths(left) == toolkit


def test_only_paths_that_exist_are_hidden_and_home_is_the_runners(home):
    got = sandbox.hidden_paths(sandbox_policy(), {"HOME": str(home)})
    assert got == [str(home.resolve() / ".ssh"), str(home.resolve() / ".aws" / "credentials")]  # no .gnupg here


def test_socket_folders_are_hidden_when_they_exist(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    assert str(run.resolve()) in sandbox.socket_dirs({"XDG_RUNTIME_DIR": str(run)})
    assert sandbox.socket_dirs({"XDG_RUNTIME_DIR": str(tmp_path / "nope")}) == [
        p for p in [f"/tmp/tmux-{os.getuid()}"] if os.path.isdir(p)
    ]


# --- the seccomp program ------------------------------------------------------------------------------------------

ALLOW, EPERM = 0x7FFF0000, 0x00050001
X86, ARM = 0xC000003E, 0xC00000B7


def run_bpf(program: bytes, arch: int, nr: int, args: tuple = (0, 0, 0, 0, 0, 0)) -> int:
    """seccomp_data is nr (u32), arch (u32), ip (u64), args (6 x u64): run the filter on one call."""
    data = struct.pack("<IIQ6Q", nr, arch, 0, *args)
    ins = [struct.unpack_from("<HBBI", program, i) for i in range(0, len(program), 8)]
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = ins[pc]
        pc += 1
        if code == 0x20:  # ld word at offset k
            (acc,) = struct.unpack_from("<I", data, k)
        elif code == 0x15:  # jeq
            pc += jt if acc == k else jf
        elif code == 0x35:  # jge
            pc += jt if acc >= k else jf
        elif code == 0x06:  # ret
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")


@pytest.mark.parametrize(
    "machine, arch, nr_socket, nr_socketpair, nr_ioctl, peeks",
    [("x86_64", X86, 41, 53, 16, (101, 310, 311)), ("aarch64", ARM, 198, 199, 29, (117, 270, 271))],
)
def test_the_filter_refuses_unix_sockets_io_uring_terminal_injection_and_reaching_into_processes(
    machine, arch, nr_socket, nr_socketpair, nr_ioctl, peeks
):
    prog = sandbox.seccomp_program(machine)
    assert len(prog) % 8 == 0
    f = lambda nr, *a: run_bpf(prog, arch, nr, (*a, 0, 0, 0, 0, 0, 0)[:6])
    assert f(nr_socket, 1, 1) == EPERM  # AF_UNIX stream
    assert f(nr_socket, 1 | (1 << 32), 1) == EPERM  # only the low word is the domain: junk above it does not hide it
    assert f(nr_socket, 2, 1) == ALLOW  # AF_INET
    assert f(nr_socket, 10, 2) == ALLOW  # AF_INET6
    assert f(nr_socketpair, 1, 1) == ALLOW  # Node and Bun spawn children through socketpair; it connects to itself
    assert f(425) == EPERM  # io_uring_setup
    assert f(nr_ioctl, 0, 0x5412) == EPERM  # TIOCSTI
    assert f(nr_ioctl, 0, 0x541C) == EPERM  # TIOCLINUX
    assert f(nr_ioctl, 0, 0x5401) == ALLOW  # TCGETS
    for nr in peeks:  # ptrace, process_vm_readv, process_vm_writev
        assert f(nr) == EPERM
    assert f(0) == ALLOW  # an ordinary call
    assert f(max(peeks) + 1) == ALLOW and f(min(peeks) - 1) == ALLOW  # the neighbours are not caught
    assert run_bpf(prog, arch ^ 1, nr_socket, (2,) * 6) == EPERM  # another architecture
    assert f(0x40000000 | nr_socket, 2) == EPERM  # x32 numbers


def test_no_filter_for_an_unknown_cpu():
    with pytest.raises(sandbox.SandboxError, match="riscv64"):
        sandbox.seccomp_program("riscv64")


# --- the Linux command line ---------------------------------------------------------------------------------------


def wrapped(home, wshome, **kw) -> sandbox.Wrapped:
    kw.setdefault("which", which("bwrap"))
    kw.setdefault("probe", lambda: True)
    kw.setdefault("system", "linux")
    kw.setdefault("machine", "x86_64")
    return sandbox.wrap(
        ["opencode", "run"], sandbox_policy(), wshome, [str(wshome / "policy.toml")], env={"HOME": str(home)}, **kw
    )


def test_bwrap_is_a_write_allow_list_with_ws_files_read_only_inside_it(home, wshome, tmp_path):
    (wshome / "policy.toml").write_text("")
    proj, tmp = tmp_path / "proj", tmp_path / "tmp"
    for d in (proj, tmp, proj / ".git" / "hooks"):
        d.mkdir(parents=True)
    w = wrapped(home, wshome, writable=[str(proj), str(tmp)], locked=[str(proj / ".git" / "hooks")])
    argv = w.argv
    assert argv[:4] == ["bwrap", "--ro-bind", "/", "/"] and argv[-3:] == ["--", "opencode", "run"]  # root is read-only
    text = " ".join(argv)
    real = wshome.resolve()
    rw_proj = f"--bind {proj.resolve()} {proj.resolve()}"
    ro, hole = f"--ro-bind {real} {real}", f"--bind {real}/approvals/pending {real}/approvals/pending"
    lock = f"--ro-bind {proj.resolve()}/.git/hooks"
    assert rw_proj in text and f"--bind {tmp.resolve()} {tmp.resolve()}" in text
    assert text.index(rw_proj) < text.index(ro) < text.index(hole) < text.index(lock)
    assert f"--bind {real}/sessions {real}/sessions" in text and f"--ro-bind {real}/policy.toml" in text
    assert f"--tmpfs {home.resolve()}/.ssh" in text and f"--ro-bind /dev/null {home.resolve()}/.aws/credentials" in text
    assert "--dev-bind /dev /dev" in text and "--die-with-parent" in argv
    assert "--unshare-pid" in argv and "--unshare-ipc" in argv and "--proc /proc" in text
    assert "--new-session" in argv  # no terminal: the escape through the controlling terminal is shut
    assert "--unshare-net" not in argv  # the runner needs the model's API
    assert "granted" not in text  # the folder that holds grants is never reopened
    assert (
        str(home.resolve()) not in rw_proj and f"--bind {home.resolve()} " not in text
    )  # the home folder is not on it
    fd = int(argv[argv.index("--seccomp") + 1])
    assert w.pass_fds == (fd,) and os.get_inheritable(fd)
    assert os.read(fd, 4096) == sandbox.seccomp_program("x86_64")
    os.close(fd)


def test_an_ancestor_inside_a_writable_folder_becomes_a_mount_point_after_it_and_before_the_read_only_mount(tmp_path):
    proj = tmp_path / "proj"
    deep = proj / "cfg" / "ws"
    deep.mkdir(parents=True)
    plan = sandbox.Plan(deep, (), writable=(str(proj),))
    assert plan.ancestors() == [str(proj.resolve() / "cfg")]  # not proj itself (it is bound already), not above it
    argv = sandbox.bwrap_argv(["x"], plan, False, 9)
    cfg = str(proj.resolve() / "cfg")
    assert argv.index(cfg) > argv.index(str(proj.resolve())) and argv.index(cfg) < argv.index("--ro-bind", 4)
    # outside every writable folder everything is read-only already: nothing to bind
    assert sandbox.Plan(deep, (), writable=(str(tmp_path / "elsewhere"),)).ancestors() == []


def test_a_protected_path_behind_a_replaceable_symlink_is_refused(tmp_path, home, wshome):
    real = tmp_path / "data" / "ws"
    real.mkdir(parents=True)
    link = tmp_path / "ws-link"
    link.symlink_to(real)
    assert str(real) in sandbox.Plan(link).symlinked()
    assert sandbox.Plan(real).symlinked() is None
    with pytest.raises(sandbox.SandboxError, match="symbolic link") as e:
        sandbox.wrap(
            ["x"],
            sandbox_policy(),
            link,
            [],
            system="linux",
            which=which("bwrap"),
            probe=lambda: True,
            machine="x86_64",
        )
    assert str(real) in str(e.value)
    # a link whose own folder the user cannot write cannot be replaced: left alone
    assert sandbox.Plan(real / "..").symlinked() is None


def test_a_link_in_an_unwritable_folder_is_fine(tmp_path, monkeypatch):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    monkeypatch.setattr(sandbox.os, "access", lambda path, mode: False)
    assert sandbox.Plan(link).symlinked() is None


def test_the_project_folder_is_never_the_home_folder_or_above(tmp_path, home):
    env = {"HOME": str(home)}
    for bad in (str(home), str(tmp_path), "/"):
        with pytest.raises(sandbox.SandboxError, match="project folder"):
            sandbox.project_folder(bad, env)
    proj = tmp_path / "proj"
    proj.mkdir()
    assert sandbox.project_folder(str(proj), env) == str(proj.resolve())
    assert sandbox.project_folder(str(home / "code" / "x"), env).endswith("/code/x")


def test_the_allow_list_is_project_tmp_and_the_policy_extras_not_the_runners_real_folders(tmp_path, home):
    proj, tmp, extra = tmp_path / "proj", tmp_path / "tmp", home / "extra"
    for d in (proj, tmp, extra):
        d.mkdir(parents=True)
    pol = policy.loads(f'[gate]\nallow_write = ["~/extra", "~/missing", "{tmp_path}/nope"]\n')
    env = {"HOME": str(home), "XDG_CACHE_HOME": str(tmp_path / "cache")}
    got = sandbox.writable_folders(pol, "opencode", str(proj), str(tmp), [str(tmp)], env)
    assert got[:2] == [str(proj), str(tmp)]
    assert str(extra) in got and str(tmp) in got  # --allow-write
    assert not any(p.endswith(("missing", "nope", "opencode")) for p in got)  # nothing the runner writes at home
    assert (home / ".config" / "opencode" / ".gitignore").is_file()  # what OpenCode would have made at start


def test_a_runner_with_state_gets_private_xdg_folders_under_ws_home(tmp_path, home, wshome):
    env = {"HOME": str(home), "XDG_DATA_HOME": str(tmp_path / "data")}
    got = sandbox.private_homes(wshome, "opencode", sandbox_policy(), env, str(tmp_path / "proj"))
    base = wshome.resolve() / "wall" / "opencode"
    key = sandbox.project_key(str(tmp_path / "proj"))
    assert key.startswith("proj-") and sandbox.project_key(str(tmp_path / "other")) != key
    assert got == {
        "XDG_DATA_HOME": f"{base}/data/{key}",  # per project: programs live here
        "XDG_STATE_HOME": f"{base}/state",  # shared: preferences and prompt history, nothing a runner executes
        "XDG_CACHE_HOME": f"{base}/cache/{key}",  # per project
    }
    assert all(os.path.isdir(p) for p in got.values()) and (wshome / "wall" / ".gitignore").read_text() == "*\n"
    assert (wshome / "wall").stat().st_mode & 0o077 == 0
    assert (
        sandbox.private_homes(wshome, "claude-code", sandbox_policy(), env, str(tmp_path / "proj")) == {}
    )  # no wall_writable: nothing of its own


def test_the_sign_in_is_copied_in_each_start_removed_when_gone_and_not_when_hidden(tmp_path, home, wshome):
    real = home / ".local" / "share" / "opencode"
    real.mkdir(parents=True)
    (real / "auth.json").write_text("one")
    env = {"HOME": str(home)}
    seeded = (
        Path(sandbox.private_homes(wshome, "opencode", sandbox_policy(), env, str(tmp_path / "proj"))["XDG_DATA_HOME"])
        / "opencode"
        / "auth.json"
    )
    assert seeded.read_text() == "one" and seeded.stat().st_mode & 0o777 == 0o600
    (real / "auth.json").write_text("two")
    sandbox.private_homes(wshome, "opencode", sandbox_policy(), env, str(tmp_path / "proj"))
    assert seeded.read_text() == "two"
    hide = policy.loads('[gate]\ndeny_read = ["~/.local/share/opencode/**"]\n')
    sandbox.private_homes(wshome, "opencode", hide, env, str(tmp_path / "proj"))
    assert not seeded.exists()  # deny_read wins over the convenience
    sandbox.private_homes(wshome, "opencode", sandbox_policy(), env, str(tmp_path / "proj"))
    assert seeded.exists()
    (real / "auth.json").unlink()
    sandbox.private_homes(wshome, "opencode", sandbox_policy(), env, str(tmp_path / "proj"))
    assert not seeded.exists()


def test_the_private_folders_are_bound_after_ws_home_and_allowed_on_macos(tmp_path, home, wshome):
    (wshome / "policy.toml").write_text("")
    proj = tmp_path / "proj"
    proj.mkdir()
    priv = sandbox.private_homes(wshome, "opencode", sandbox_policy(), {"HOME": str(home)}, str(tmp_path / "proj"))
    text = " ".join(wrapped(home, wshome, writable=[str(proj)], homes=list(priv.values())).argv)
    ro, cache = f"--ro-bind {wshome.resolve()} {wshome.resolve()}", priv["XDG_CACHE_HOME"]
    assert text.index(ro) < text.index(f"--bind {cache} {cache}")
    prof = wrapped(home, wshome, system="darwin", which=which("sandbox-exec"), homes=list(priv.values())).argv[2]
    assert prof.index(f'(deny file-write* (subpath "{wshome.resolve()}"))') < prof.index(
        f'(allow file-write* (subpath "{cache}"))'
    )


def make_project(root: Path, *rel: str) -> Path:
    for r in rel:
        p = root / r
        if r.endswith("/"):
            p.mkdir(parents=True, exist_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text("")
    return root


def repo(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(root)], check=True, capture_output=True)
    return root


def set_config(root: Path, key: str, value: str) -> None:
    subprocess.run(["git", "-C", str(root), "config", key, value], check=True, capture_output=True)


def test_the_projects_own_config_is_locked_when_it_exists_and_made_when_missing(tmp_path):
    proj = make_project(tmp_path / "p", ".git/hooks/", ".git/config", ".claude/", "opencode.json")
    locked, created = sandbox.lock_project(str(proj))
    names = sorted(str(Path(p).relative_to(proj)) for p in locked)
    assert {".git/hooks", ".git/config", ".claude", "opencode.json"} <= set(names)
    assert ".git" not in names  # the folder itself is held by being an ancestor (see Plan.ancestors)
    # what was missing was made: a folder, or a file with valid empty text; what was there was left alone
    assert (proj / ".opencode").is_dir() and (proj / ".mcp.json").read_text() == '{"mcpServers": {}}\n'
    assert (proj / ".opencode" / opencode.IGNORE).read_text() == opencode.IGNORE_TEXT  # what OpenCode writes at start
    assert (proj / "opencode.json").read_text() == ""
    made = sorted(str(Path(p).relative_to(proj)) for p, _ in created)
    assert ".claude" not in made and "opencode.json" not in made
    assert ".opencode" in made and ".git/config.worktree" in made
    sandbox.unlock_project(created)
    assert not (proj / ".opencode").exists() and not (proj / ".mcp.json").exists()
    assert (proj / ".claude").is_dir() and (proj / "opencode.json").exists()


def test_a_project_without_a_repository_leaves_git_alone_and_a_git_file_is_locked_itself(tmp_path):
    bare = tmp_path / "bare"
    bare.mkdir()
    locked, created = sandbox.lock_project(str(bare))
    assert not any(".git" in Path(p).parts for p in [*locked, *(c for c, _ in created)])  # `git init` must keep working
    sandbox.unlock_project(created)
    assert list(bare.iterdir()) == []
    pointer = tmp_path / "wt"
    pointer.mkdir()
    (pointer / ".git").write_text("gitdir: ../elsewhere\n")
    locked, created = sandbox.lock_project(str(pointer))
    assert str(pointer / ".git") in locked and not any(p.endswith((".git/hooks", ".git/config")) for p in locked)
    sandbox.unlock_project(created)


def test_a_folder_the_session_filled_is_not_removed_after_the_run(tmp_path):
    proj = tmp_path / "p"
    proj.mkdir()
    _, created = sandbox.lock_project(str(proj))
    (proj / ".opencode" / "kept").write_text("x")  # as if a path were allowed to write
    (proj / "opencode.json").write_text("changed")
    sandbox.unlock_project(created)
    assert (proj / ".opencode" / "kept").exists() and (proj / "opencode.json").read_text() == "changed"
    assert not (proj / ".claude").exists()


@pytest.mark.parametrize("name", [".claude", ".opencode", "opencode.json", ".mcp.json", ".git/hooks"])
def test_a_locked_path_that_is_a_symbolic_link_is_refused(tmp_path, name):
    proj = make_project(tmp_path / "p", ".git/config")
    target = tmp_path / "real"
    target.mkdir()
    link = proj / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    with pytest.raises(sandbox.SandboxError, match="symbolic link"):
        sandbox.lock_project(str(proj))
    assert link.is_symlink()  # nothing was changed on the way to refusing


def test_git_facts_finds_the_hooks_folder_and_the_config_files_inside_the_project(tmp_path):
    proj = repo(tmp_path / "p")
    (proj / "local.gitconfig").write_text("[core]\n")
    set_config(proj, "core.hooksPath", ".githooks")
    set_config(proj, "include.path", "../local.gitconfig")
    set_config(proj, "includeIf.gitdir:/x/.path", "../later.cfg")
    facts = sandbox.git_facts(str(proj))
    hooks, files = facts.hooks, facts.files
    assert facts.dirs == [f"{proj.resolve()}/.git"]
    real = proj.resolve()
    assert hooks == f"{real}/.githooks"
    assert f"{real}/local.gitconfig" in files and f"{real}/.git/config" in files
    assert f"{real}/later.cfg" in files  # named though it is not there and its condition is false
    set_config(proj, "core.hooksPath", str(tmp_path / "outside"))
    assert sandbox.git_facts(str(proj)).hooks is None  # a hooks folder outside the project is read-only already
    assert sandbox.git_facts(str(tmp_path)) == sandbox.GitFacts(None, [], [])  # not a repository


def test_the_hooks_folder_and_includes_are_made_if_missing_and_removed_after(tmp_path):
    proj = repo(tmp_path / "p")
    set_config(proj, "core.hooksPath", ".husky/_")
    set_config(proj, "include.path", "../local.gitconfig")
    locked, created = sandbox.lock_project(str(proj))
    assert str(proj.resolve() / ".husky" / "_") in locked and str(proj.resolve() / "local.gitconfig") in locked
    sandbox.unlock_project(created)
    assert not (proj / ".husky").exists() and not (proj / "local.gitconfig").exists()


def test_submodule_repositories_are_found_with_their_nested_ones(tmp_path):
    git_dir = tmp_path / ".git"
    for rel in ("modules/a", "modules/a/modules/b", "modules/x/y"):
        (git_dir / rel / "hooks").mkdir(parents=True)
        (git_dir / rel / "HEAD").write_text("")
        (git_dir / rel / "config").write_text("")
    (git_dir / "modules" / "a" / "objects").mkdir()
    got = sandbox.submodule_repos(git_dir)
    for rel in ("a", "a/modules/b", "x/y"):
        assert str(git_dir / "modules" / rel / "hooks") in got and str(git_dir / "modules" / rel / "config") in got


def test_the_folders_above_a_locked_path_are_mount_points_so_they_cannot_be_renamed(tmp_path):
    proj = make_project(tmp_path / "p", ".git/hooks/", ".git/config", ".husky/_/")
    hooks, config, husky = (str(proj / ".git" / "hooks"), str(proj / ".git" / "config"), str(proj / ".husky" / "_"))
    plan = sandbox.Plan(tmp_path / "wshome", writable=(str(proj),), locked=(hooks, config, husky))
    real = proj.resolve()
    assert plan.ancestors() == [f"{real}/.git", f"{real}/.husky"]  # not the project (bound already), not .git/hooks
    text = " ".join(sandbox.bwrap_argv(["x"], plan, False, 9))
    assert text.index(f"--bind {real}/.git {real}/.git") < text.index(f"--ro-bind {real}/.git/hooks")
    assert text.index(f"--bind {real}/.husky {real}/.husky") < text.index(f"--ro-bind {real}/.husky/_")
    prof = sandbox.sandbox_exec_profile(plan)
    assert f'(deny file-write* (literal "{real}/.git"))' in prof
    assert f'(deny file-write* (literal "{real}/.husky"))' in prof
    assert f'(literal "{real}"))' not in prof and f'(deny file-write* (subpath "{real}/.git/hooks"))' in prof


def test_allow_write_in_the_policy_is_checked_and_reaches_the_claude_settings(tmp_path):
    for bad in ('"relative"', '"/"', '"~/"', '"~/."', '"/."', '"~/x/.."', '"/a/.."', '"$HOME"', '"~/x*"', '"/a/b?"'):
        with pytest.raises(policy.PolicyError, match="allow_write"):
            policy.loads(f"[gate]\nallow_write = [{bad}]\n")
    s = claude_settings(tmp_path, '[gate]\nallow_write = ["~/.npm", "$HOME/.cache/uv"]\n')
    assert s["sandbox"]["filesystem"]["allowWrite"] == ["~/.npm", "~/.cache/uv"]
    assert "allowWrite" not in claude_settings(tmp_path)["sandbox"]["filesystem"]


def test_an_interactive_runner_keeps_its_terminal(home, wshome):
    assert "--new-session" not in wrapped(home, wshome, tty=True).argv


def test_the_holes_exist_before_the_mount(home, wshome):
    wrapped(home, wshome)
    assert (wshome / "approvals" / "pending").is_dir() and (wshome / "sessions").is_dir()


# --- fail closed --------------------------------------------------------------------------------------------------


def test_no_bubblewrap_refuses_with_the_install_line(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="apt install bubblewrap"):
        wrapped(home, wshome, which=which())


def test_a_bubblewrap_that_cannot_start_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="cannot start"):
        wrapped(home, wshome, probe=lambda: False)


def test_a_cpu_without_a_filter_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="no seccomp filter for riscv64"):
        wrapped(home, wshome, machine="riscv64")


def test_another_system_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="no sandbox for windows"):
        wrapped(home, wshome, system="windows")


def test_macos_without_sandbox_exec_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="sandbox-exec"):
        wrapped(home, wshome, system="darwin", which=which())


def test_macos_profile_denies_writes_to_ws_and_reads_of_the_hidden_paths(home, wshome):
    w = wrapped(home, wshome, system="darwin", which=which("sandbox-exec"))
    argv = w.argv
    assert argv[:2] == ["sandbox-exec", "-p"] and argv[3:] == ["opencode", "run"] and w.pass_fds == ()
    prof = argv[2]
    real = str(wshome.resolve())
    assert "(allow default)" in prof and "\n(deny file-write*)\n" in prof  # every write refused first
    assert f'(deny file-write* (subpath "{real}"))' in prof
    assert f'(deny file-read* (subpath "{home.resolve()}/.ssh"))' in prof
    assert f'(deny file-read* (literal "{home.resolve()}/.aws/credentials"))' in prof
    assert "(deny network-outbound (remote unix-socket))" in prof
    assert prof.index("(deny file-write*)") < prof.index('(allow file-write* (subpath "/dev"))')
    assert prof.index(f'(deny file-write* (subpath "{real}"))') < prof.index(
        f'(allow file-write* (subpath "{real}/approvals/pending"))'
    )


def test_the_profile_quotes_awkward_paths(tmp_path):
    odd = tmp_path / 'a "b" \\ c'
    prof = sandbox.sandbox_exec_profile(sandbox.Plan(odd))
    assert '\\"b\\"' in prof and "\\\\" in prof


# --- ws gate launch -----------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch, wshome):
    monkeypatch.setenv("WS_HOME", str(wshome))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    nowhere = tmp_path / "empty-path"
    nowhere.mkdir()
    monkeypatch.setenv("PATH", str(nowhere))  # no bwrap, whatever the machine has: these tests must not need it
    started = []
    monkeypatch.setattr(sandbox.os, "execvpe", lambda exe, argv, e: started.append((argv, e)))
    monkeypatch.setattr(sandbox, "run_with_broker", lambda cmd, e, fds, root: started.append((cmd, e, root)) or 0)
    return started


def test_launch_wraps_the_runner_marks_the_session_and_keeps_the_broker_beside_it(env, monkeypatch, wshome):
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: sandbox.Wrapped(["wall", *argv]))
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode", "run"]) == 0
    argv, e, root = env[0]
    assert argv == ["wall", "opencode", "run"] and e["WS_GATED_SESSION"] == "1"
    assert root == wshome.absolute() / "approvals"


def test_launch_gives_the_runner_private_xdg_folders_and_locks_the_project(env, monkeypatch, wshome, tmp_path):
    seen = {}
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: seen.update(k) or sandbox.Wrapped(["wall", *argv]))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode"]) == 0
    _, e, _ = env[0]
    base = wshome.resolve() / "wall" / "opencode"
    key = sandbox.project_key(str(proj.resolve()))
    assert e["XDG_DATA_HOME"] == f"{base}/data/{key}" and e["XDG_STATE_HOME"] == f"{base}/state"
    assert e["XDG_CACHE_HOME"] == f"{base}/cache/{key}"
    assert set(seen["homes"]) == {f"{base}/data/{key}", f"{base}/state", f"{base}/cache/{key}"}
    assert str(proj.resolve() / ".claude") in seen["locked"]  # the placeholder was there for the wall to hold
    assert not (proj / ".claude").exists()  # and is gone once the run has ended


def test_launch_fails_closed_when_the_wall_cannot_be_built(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise sandbox.SandboxError("bubblewrap (bwrap) is not installed; install bubblewrap")

    monkeypatch.setattr(sandbox, "wrap", boom)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode"]) == 1
    err = capsys.readouterr().err
    assert "refusing to start" in err and "install bubblewrap" in err and "--no-sandbox" in err
    assert env == []  # the runner never started


def test_no_sandbox_starts_bare_and_says_what_it_gives_up(env, capsys, monkeypatch):
    def never(*a, **k):
        raise AssertionError("--no-sandbox must not build a wall")

    monkeypatch.setattr(sandbox, "wrap", never)
    monkeypatch.setattr(sandbox, "run_with_broker", never)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--no-sandbox", "--", "opencode"]) == 0
    err = capsys.readouterr().err
    assert "NO wall" in err and "python3 -c" in err and "policy" in err and "tmux" in err
    assert env[0][0] == ["opencode"] and env[0][1]["WS_GATED_SESSION"] == "1"


def test_launch_needs_a_command(env):
    assert cli.main(["gate", "launch", "--runner", "opencode"]) == 1
    assert env == []


def claude_launch_setup(env, monkeypatch, tmp_path, wshome, name: str) -> Path:
    """Generated settings in $WS_HOME as `ws install` leaves them, and a project folder to start from."""
    out = wshome / "gate" / "claude-code"
    for rel, text in claude_code.generate(policy.loads(""), ["ws"], out, wshome / "policy.toml").items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    proj = tmp_path / name
    proj.mkdir()
    monkeypatch.chdir(proj)
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: env.append((cmd, e)) or 0)
    return proj


def test_claude_code_is_started_with_settings_made_for_this_project_and_no_wrap(env, monkeypatch, tmp_path, wshome):
    one = claude_launch_setup(env, monkeypatch, tmp_path, wshome, "one")
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude", "--model", "m"]) == 0
    cmd, e = env[0]
    assert cmd[0] == "claude" and cmd[1] == "--settings" and cmd[3:] == ["--model", "m"]  # no bwrap in front
    conf = json.loads(Path(cmd[2]).read_text())
    deny = conf["sandbox"]["filesystem"]["denyWrite"]
    for rel in sandbox.claude_denied():
        assert f"{one.resolve()}/{rel}" in deny
    assert conf["permissions"]["deny"] and conf["hooks"] and e["WS_GATED_SESSION"] == "1"
    two = tmp_path / "two"
    two.mkdir()
    monkeypatch.chdir(two)
    env.clear()
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
    second = json.loads(Path(env[0][0][2]).read_text())["sandbox"]["filesystem"]["denyWrite"]
    assert f"{two.resolve()}/.opencode" in second and f"{one.resolve()}/.opencode" not in second
    assert Path(cmd[2]) != Path(env[0][0][2])  # one settings file per project
    shared = json.loads((wshome / "gate" / "claude-code" / claude_code.FILE).read_text())
    assert not any(
        str(one.resolve()) in p or str(two.resolve()) in p for p in shared["sandbox"]["filesystem"]["denyWrite"]
    )


def test_claude_code_needs_generated_settings_and_a_project_folder(env, monkeypatch, tmp_path, wshome, capsys):
    monkeypatch.chdir(tmp_path)
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 1
    assert "gate generate" in capsys.readouterr().err and env == []
    claude_launch_setup(env, monkeypatch, tmp_path, wshome, "p")
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.chdir(tmp_path / "home")  # the home folder is never a project
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 1
    assert "project folder" in capsys.readouterr().err and env == []


def test_the_whole_generated_folder_and_both_control_folders_are_protected(env, monkeypatch, tmp_path, wshome):
    out = wshome / "gate" / "opencode"
    out.mkdir(parents=True)
    other = tmp_path / "elsewhere" / "policy.toml"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(out))
    seen = {}
    monkeypatch.setattr(
        sandbox,
        "wrap",
        lambda argv, pol, home, protect, **k: seen.update(protect=protect, home=home) or sandbox.Wrapped(argv),
    )
    other.parent.mkdir()
    other.write_text("schema = 1\n")
    assert (
        cli.main(["gate", "launch", "--runner", "opencode", "--policy", str(other), "--protect", "x", "--", "x"]) == 0
    )
    prot = seen["protect"]
    assert str(out) in prot  # the folder as a whole: nothing in it is writable, so nothing can be planted
    assert str(other) in prot and str(other.parent / "approvals") in prot  # wherever --policy put it
    assert str(wshome / "approvals") in prot and str(wshome / "policy.toml") in prot  # $WS_HOME, whatever --policy says
    assert seen["home"] == wshome.absolute()


# --- the broker, for real -----------------------------------------------------------------------------------------

CLAIMER = (
    "import os, socket, sys\n"
    "s = socket.socket(fileno=int(os.environ['WS_APPROVAL_FD']))\n"
    "f = s.makefile('rw', newline='\\n')\n"
    "f.write('claim ' + sys.argv[1] + ' ' + sys.argv[2] + '\\n'); f.flush()\n"
    "sys.exit(0 if f.readline().strip() == 'ok ' + sys.argv[2] else 3)\n"
)


def test_a_runner_in_the_wall_can_claim_a_grant_once_through_the_launcher(tmp_path):
    root = tmp_path / "approvals"
    bound = approvals.Bound("opencode", "command", "terraform apply", "/w", "s1")
    approvals.require(root, bound)
    approvals.grant(root, bound.digest()[:12])
    cmd = [sys.executable, "-c", CLAIMER, bound.digest(), "nonce-12345678"]
    assert sandbox.run_with_broker(cmd, dict(os.environ), (), root) == 0
    assert sandbox.run_with_broker(cmd, dict(os.environ), (), root) == 3  # used up
    assert not (root / "granted" / f"{bound.digest()}.json").exists()


def test_the_answer_echoes_the_nonce_and_a_claim_without_one_is_refused(tmp_path):
    import socket as _socket

    root = tmp_path / "approvals"
    bound = approvals.Bound("opencode", "command", "terraform apply", "/w", "s1")
    approvals.require(root, bound)
    approvals.grant(root, bound.digest()[:12])
    mine, theirs = _socket.socketpair()
    import threading

    threading.Thread(target=approvals.serve_claims, args=(mine, root), daemon=True).start()
    f = theirs.makefile("rw", newline="\n")
    f.write(f"claim {bound.digest()}\n")  # no nonce
    f.flush()
    assert f.readline().startswith("no - bad request")
    assert approvals.claim(theirs, bound.digest()) is None  # the right form: used, and the answer matched its nonce
    assert approvals.claim(theirs, bound.digest()) == approvals._NONE
    theirs.close()


def test_a_forged_claim_for_nothing_is_refused(tmp_path):
    root = tmp_path / "approvals"
    assert (
        sandbox.run_with_broker([sys.executable, "-c", CLAIMER, "0" * 64, "nonce-12345678"], dict(os.environ), (), root)
        == 3
    )
    assert (
        sandbox.run_with_broker(
            [sys.executable, "-c", CLAIMER, "not-an-id", "nonce-12345678"], dict(os.environ), (), root
        )
        == 3
    )


# --- the Claude Code sandbox block --------------------------------------------------------------------------------


def claude_settings(tmp_path, toml: str = "") -> dict:
    pol = policy.loads(toml)
    out = tmp_path / "wshome" / "gate" / "claude-code"
    files = claude_code.generate(pol, ["ws"], out, tmp_path / "wshome" / "policy.toml")
    return json.loads(files["settings.json"])


def test_claude_code_settings_turn_the_sandbox_on_and_fail_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    s = claude_settings(tmp_path, '[gate]\ndeny_read = ["~/.ssh/**", "~/.aws/credentials", "~/x*/y"]\n')
    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["autoAllowBashIfSandboxed"] is False  # the person still sees prompts
    deny = sb["filesystem"]["denyWrite"]
    wshome = str(tmp_path / "wshome")
    assert deny[0] == wshome and str(tmp_path / "wshome" / "policy.toml") in deny
    assert str(tmp_path / "wshome" / "gate" / "claude-code") in deny
    assert all(str(tmp_path / "wshome" / n) in deny for n in policy.CONTROL_NAMES)
    assert sb["filesystem"]["denyRead"] == ["~/.ssh", "~/.aws/credentials", str(tmp_path / "wshome" / "secrets")]
    assert "network" not in sb  # the policy names no hosts: it says nothing about the network
    assert s["env"] == {"WS_GATED_SESSION": "1", "WS_SANDBOXED": "1"}


def test_claude_code_network_follows_allow_hosts(tmp_path):
    s = claude_settings(tmp_path, '[gate]\nallow_hosts = ["api.anthropic.com", "*.npmjs.org"]\n')
    assert s["sandbox"]["network"] == {"allowedDomains": ["api.anthropic.com", "*.npmjs.org"]}


def test_only_documented_sandbox_keys_are_used(tmp_path):
    sb = claude_settings(tmp_path, '[gate]\nallow_hosts = ["a.b"]\n')["sandbox"]
    documented = {
        "enabled",
        "autoAllowBashIfSandboxed",
        "failIfUnavailable",
        "allowUnsandboxedCommands",
        "filesystem",
        "network",
    }
    assert set(sb) <= documented
    assert set(sb["filesystem"]) <= {"allowWrite", "denyWrite", "denyRead", "allowRead"}


# --- project settings must not widen it ----------------------------------------------------------------------------


def project(tmp_path, name: str, conf) -> Path:
    d = tmp_path / "proj"
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    (d / ".claude" / name).write_text(conf if isinstance(conf, str) else json.dumps(conf))
    return d


@pytest.mark.parametrize(
    "conf",
    [
        {"sandbox": {"excludedCommands": ["python3 *"]}},
        {"sandbox": {"enabled": False}},
        {"sandbox": {"allowUnsandboxedCommands": True}},
        {"sandbox": {"filesystem": {"allowRead": ["~/.ssh"]}}},
        {"sandbox": {"filesystem": {"allowWrite": ["/"]}}},
        {"sandbox": {"filesystem": {"disabled": True}}},
    ],
)
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_the_hook_denies_a_folder_whose_project_settings_open_the_sandbox(tmp_path, monkeypatch, conf, name):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_SANDBOXED", "1")  # the hook checks projects only for a session with the sandbox block
    d = project(tmp_path, name, conf)
    (d / "sub").mkdir()
    for cwd in (d, d / "sub"):  # from a folder below the project too
        why = claude_code.preflight({"cwd": str(cwd)})
        assert why and name in why and "sandbox." in why


def test_harmless_project_settings_and_the_users_own_settings_are_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_SANDBOXED", "1")  # the hook checks projects only for a session with the sandbox block
    d = project(tmp_path, "settings.json", {"sandbox": {"network": {"allowedDomains": ["a.b"]}}, "model": "x"})
    assert claude_code.preflight({"cwd": str(d)}) is None
    assert claude_code.preflight({"cwd": str(tmp_path / "nowhere")}) is None
    assert claude_code.preflight({}) is None and claude_code.preflight({"cwd": "relative"}) is None
    project(tmp_path, "settings.local.json", "{not json")  # Claude Code ignores a file that does not parse
    assert claude_code.preflight({"cwd": str(d)}) is None
    user = tmp_path / "home"
    (user / ".claude").mkdir(parents=True)
    (user / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"excludedCommands": ["docker *"]}}))
    assert claude_code.preflight({"cwd": str(user)}) is None  # user scope is the person's own


def test_a_settings_file_that_is_a_fifo_or_a_link_is_a_deny_and_does_not_hang(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_SANDBOXED", "1")  # the hook checks projects only for a session with the sandbox block
    d = tmp_path / "proj"
    (d / ".claude").mkdir(parents=True)
    os.mkfifo(d / ".claude" / "settings.json")
    why = claude_code.preflight({"cwd": str(d)})  # a blocking read would hang here forever
    assert why and "not a plain readable settings file" in why and "blocked to be safe" in why
    (d / ".claude" / "settings.json").unlink()
    target = tmp_path / "elsewhere.json"
    target.write_text("{}")
    (d / ".claude" / "settings.json").symlink_to(target)
    assert "not a plain readable settings file" in claude_code.preflight({"cwd": str(d)})
    (d / ".claude" / "settings.json").unlink()
    (d / ".claude" / "settings.json").write_text(" " * (2 << 20))  # too big to be settings
    assert "too large" in claude_code.preflight({"cwd": str(d)})


def test_the_project_dir_the_hook_is_given_wins_over_the_cwd(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_SANDBOXED", "1")  # the hook checks projects only for a session with the sandbox block
    bad = project(tmp_path, "settings.json", {"sandbox": {"excludedCommands": ["x"]}})
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(bad))
    assert claude_code.preflight({"cwd": str(other)})  # the session has since moved; the project is still the project
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    assert claude_code.preflight({"cwd": str(bad)}) is None


def test_the_gate_check_applies_the_preflight(tmp_path, monkeypatch):
    from ws import gate

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_SANDBOXED", "1")  # the hook checks projects only for a session with the sandbox block
    d = project(tmp_path, "settings.json", {"sandbox": {"excludedCommands": ["x"]}})
    raw = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}, "cwd": str(d)})
    assert "excludedCommands" in gate.check("claude-code", raw, policy.loads(""))


def test_gated_headless_claude_runs_ignore_project_settings(tmp_path):
    job = jobs.loads("j", 'runner = "claude-code"\nmodel = "m"\nprompt = "-hello"\n', Path("."))
    argv, _ = claude_code.build_command(job, tmp_path, None)
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--settings") + 1] == str(tmp_path / "gate" / "settings.json")


# --- the OpenCode shim and folder ----------------------------------------------------------------------------------


def test_the_opencode_shim_sends_the_session_and_resolved_workdir_and_can_claim(tmp_path):
    files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "policy.toml")
    shim = files[opencode.PLUGIN]
    assert "session_id: input.sessionID" in shim
    assert 'resolve(directory ?? process.cwd(), output.args?.workdir ?? ".")' in shim  # bash runs in args.workdir
    assert "r.status === 4" in shim and "WS_APPROVAL_FD" in shim and "claim ${id} ${nonce}" in shim
    assert "randomUUID()" in shim and "ok ${nonce}" in shim  # a fresh nonce per request, echoed in the answer
    assert "bun:ffi" in shim and "fcntl" in shim  # the descriptor is marked close-on-exec
    assert "ws gate launch --runner opencode -- opencode" in opencode.launch_hint(str(tmp_path))


def test_the_generated_opencode_folder_needs_nothing_written_at_start(tmp_path):
    files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "policy.toml")
    assert set(files) == {opencode.CONFIG, opencode.PLUGIN, opencode.IGNORE}
    assert files[opencode.IGNORE].splitlines()[-1] == opencode.IGNORE  # what OpenCode itself writes


def test_allow_write_may_not_be_the_home_folder_or_above_whatever_it_is_spelled_as(tmp_path, home):
    env = {"HOME": str(home)}
    pol = policy.loads("")
    proj, tmp = tmp_path / "proj", tmp_path / "tmp"
    proj.mkdir()
    tmp.mkdir()
    link = tmp_path / "home-link"
    link.symlink_to(home)
    for bad in (str(home), str(home) + "/.", str(tmp_path), "/", str(home / "sub" / ".."), str(link), "~", "~/."):
        with pytest.raises(sandbox.SandboxError, match="home folder or above"):
            sandbox.writable_folders(pol, "opencode", str(proj), str(tmp), [bad], env)
    ok = home / "extra"
    ok.mkdir()
    assert str(ok) in sandbox.writable_folders(pol, "opencode", str(proj), str(tmp), [str(ok)], env)
    # the same check applies to the policy's own allow_write
    with pytest.raises(sandbox.SandboxError):
        sandbox.writable_folders(
            policy.loads(f'[gate]\nallow_write = ["{tmp_path}"]\n'), "opencode", str(proj), str(tmp), [], env
        )


# --- the git folders, several sessions, the end-of-run report, the Claude Code lock ---------------------------------------


def run_git(root: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def test_every_git_folder_in_the_project_is_locked_with_its_pointers(tmp_path):
    ident = ["-c", "user.name=t", "-c", f"user.email=t{chr(64)}t.invalid"]
    proj = tmp_path / "p"
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(proj / "realgit"), str(proj)], check=True, capture_output=True
    )
    run_git(proj, *ident, "commit", "-q", "--allow-empty", "-m", "i")
    run_git(proj, "worktree", "add", "-q", "wt")
    real = proj.resolve()
    assert sandbox.git_facts(str(proj)).dirs == [f"{real}/realgit"]
    wanted = dict(sandbox.lock_wanted(str(proj)))
    assert wanted[f"{real}/.git"] is None  # the pointer file itself
    for name in ("hooks", "config", "config.worktree", "commondir"):
        assert f"{real}/realgit/{name}" in wanted
    assert wanted[f"{real}/realgit/commondir"] == ".\n"  # an empty file would make git die
    for name in ("commondir", "gitdir", "config.worktree"):
        assert f"{real}/realgit/worktrees/wt/{name}" in wanted


def test_the_shared_claude_code_settings_hold_the_edit_rules_and_no_project_paths(tmp_path, monkeypatch):
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)  # where it is made no longer matters: nothing about the folder is baked in
    out = tmp_path / "gate"
    files = claude_code.generate(policy.loads(""), ["ws"], out, tmp_path / "policy.toml")
    conf = json.loads(files[claude_code.FILE])
    deny, edit = conf["sandbox"]["filesystem"]["denyWrite"], conf["permissions"]["deny"]
    for rel in sandbox.claude_denied():  # the Edit rules are project-independent and reach sub-folders
        assert f"Edit(**/{rel})" in edit and f"Edit(**/{rel}/**)" in edit
    assert not any(str(proj.resolve()) in p for p in deny)
    wanted = {".claude", ".opencode", ".mcp.json", "opencode.json", ".git/hooks", ".git/config", ".git/commondir"}
    assert wanted <= set(sandbox.claude_denied())
    assert "ws gate launch --runner claude-code -- claude" in claude_code.launch_hint(str(out))


def test_only_the_last_launcher_out_takes_the_placeholders_down_and_a_dead_one_is_cleaned_after(tmp_path, wshome):
    proj = tmp_path / "p"
    proj.mkdir()
    mine = os.getpid()
    sandbox.enter_project(wshome, str(proj))
    assert (proj / ".claude").is_dir()
    other = subprocess.Popen(["sleep", "30"])
    record = next((wshome / "wall" / "projects").glob("*.json"))
    try:
        state = json.loads(record.read_text())
        state["pids"].append(other.pid)  # a second launcher, alive
        record.write_text(json.dumps(state))
        sandbox.leave_project(wshome, str(proj))
        assert (proj / ".claude").is_dir(), "the placeholders must stay for the launcher still inside"
        assert json.loads(record.read_text())["pids"] == [other.pid]
    finally:
        other.kill()
        other.wait()
    # the second died without leaving: the next launch cleans up before it makes its own
    sandbox.enter_project(wshome, str(proj))
    assert json.loads(record.read_text())["pids"] == [mine]
    sandbox.leave_project(wshome, str(proj))
    assert not (proj / ".claude").exists() and not record.exists()


def test_scan_and_compare_find_new_and_changed_things_and_skip_the_placeholders(tmp_path):
    proj = tmp_path / "p"
    (proj / "nested" / ".git" / "hooks").mkdir(parents=True)
    (proj / "nested" / ".git" / "config").write_text("[core]\n\tbare = false\n")
    (proj / "mise.toml").write_text("a")
    before, note = sandbox.scan_project(str(proj))
    assert note is None
    (proj / "nested" / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    (proj / "nested" / ".git" / "hooks" / "pre-commit.sample").write_text("x")  # a sample is not a hook
    (proj / "nested" / ".git" / "config").write_text("[core]\n\tbare = false\n\tfsmonitor = x\n[Alias]\n\tzz = y\n")
    (proj / "mise.toml").write_text("b")
    (proj / "sub" / ".claude").mkdir(parents=True)
    (proj / "sub" / ".claude" / "settings.json").write_text("{}")
    (proj / ".vscode").mkdir()
    (proj / ".vscode" / "tasks.json").write_text("{}")
    (proj / ".vscode" / "launch.json").write_text("{}")  # not one of the two files
    (proj / ".venv" / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (proj / ".venv" / "lib" / "python3.12" / "site-packages" / "x.pth").write_text("import os")
    (proj / "node_modules" / ".bin").mkdir(parents=True)
    (proj / "node_modules" / ".bin" / "tool").write_text("x")
    (proj / "node_modules" / "pkg" / "node_modules" / ".bin").mkdir(parents=True)  # deeper ones: not walked into
    (proj / ".opencode").mkdir()  # a placeholder of another session
    after, _ = sandbox.scan_project(str(proj), (str(proj / ".opencode"),))
    found = {(f["path"], f["change"]): f for f in sandbox.compare_scans(before, after)}
    assert set(found) == {
        ("nested/.git", "changed"),
        ("mise.toml", "changed"),
        ("sub/.claude", "new"),
        (".vscode/tasks.json", "new"),
        (".venv/lib/python3.12/site-packages/x.pth", "new"),
        ("node_modules/.bin/tool", "new"),
        ("node_modules/pkg", "new"),
    }
    git = found[("nested/.git", "changed")]["detail"]
    assert list(git["hooks"]) == ["pre-commit"] and git["extra_config"] == ["alias.zz", "core.fsmonitor"]
    assert "hooks: pre-commit" in sandbox.describe(found[("nested/.git", "changed")])
    assert sandbox.compare_scans(after, after) == []


def test_the_scan_is_bounded_and_says_where_it_stopped(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    for i in range(30):
        (proj / f"d{i}").mkdir(parents=True)
    monkeypatch.setattr(sandbox, "SCAN_ENTRIES", 10)
    _, note = sandbox.scan_project(str(proj))
    assert note and "stopped" in note
    monkeypatch.setattr(sandbox, "SCAN_ENTRIES", 10**6)
    ticks = iter(range(0, 1000, 5))
    _, note = sandbox.scan_project(str(proj), clock=lambda: next(ticks))
    assert note and "stopped" in note
    # a first scan that stopped cannot call anything new: only changes are reported
    assert sandbox.compare_scans({}, {"config:x": "1"}, complete=False) == []
    assert sandbox.compare_scans({"config:x": "0"}, {"config:x": "1"}, complete=False)[0]["change"] == "changed"


def test_a_launcher_with_nothing_to_say_prints_and_writes_nothing(tmp_path, wshome, capsys):
    assert sandbox.report_run(wshome, "/p", "opencode", {"a:x": "1"}, {"a:x": "1"}, []) == []
    assert capsys.readouterr().err == "" and not (wshome / "logs").exists()
    got = sandbox.report_run(wshome, "/p", "opencode", {}, {"config:.claude": "h"}, ["a note"])
    err = capsys.readouterr().err
    assert got and ".claude" in err and "note: a note" in err and "removed nothing" in err
    line = json.loads((wshome / "logs" / "wall-report.jsonl").read_text())
    assert line["project"] == "/p" and line["findings"][0]["path"] == ".claude" and line["notes"] == ["a note"]
    assert line["schema"] == 1 and line["ts"].endswith("Z") and line["at"].endswith("Z")  # `at` is the legacy key


# --- the report does not trust the tree it reads ----------------------------------------------------------------------------


def test_the_scan_never_reads_through_a_link_and_a_fifo_does_not_hang_it(tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("TOP-SECRET-" + "x" * 300)  # the kind of file deny_read hides
    real_git = tmp_path / "elsewhere" / ".git"
    (real_git / "hooks").mkdir(parents=True)
    (real_git / "hooks" / "pre-commit").write_text("#!/bin/sh\n")
    (real_git / "config").write_text("[alias]\n\tleak = !echo LEAKED\n")
    proj = tmp_path / "p"
    for d in ("a", "b", "c"):
        (proj / d).mkdir(parents=True)
    (proj / "a" / ".git").symlink_to(secret)  # a link to a file
    (proj / "b" / ".git").symlink_to(real_git)  # a link to a folder with hooks and a config
    os.mkfifo(proj / "c" / ".mcp.json")  # opening this for reading would block forever
    (proj / "c" / "opencode.json").symlink_to("/dev/zero")  # reading this never ends
    (proj / "c" / ".claude").symlink_to(tmp_path / "elsewhere")
    (proj / "c" / "mise.toml").write_text("x" * (sandbox.READ_CAP + 1))  # too big to read
    alarm = signal.alarm(20)  # a hang fails the test, not the run
    try:
        found, note = sandbox.scan_project(str(proj))
    finally:
        signal.alarm(0)
        assert alarm == 0
    text = json.dumps(found)
    assert "TOP-SECRET" not in text and "LEAKED" not in text and "pre-commit" not in text  # nothing read through
    assert found["git:a/.git"] == {"link": str(secret)} and found["git:b/.git"] == {"link": str(real_git)}
    assert found["config:c/.mcp.json"] == {"special": "other"}
    assert found["config:c/opencode.json"] == {"link": "/dev/zero"}
    assert found["config:c/.claude"] == {"link": str(tmp_path / "elsewhere")}
    assert found["config:c/mise.toml"] == "!too big: 2097153" and note is None
    # and what it reports says so
    line = sandbox.describe({"kind": "git", "path": "a/.git", "change": "new", "detail": found["git:a/.git"]})
    assert line == f"new a git folder: a/.git (a link to {secret})"
    assert "not a regular file" in sandbox.describe(
        {"kind": "config", "path": "c/.mcp.json", "change": "new", "detail": {"special": "other"}}
    )


def test_a_read_stops_at_the_time_budget(tmp_path):
    f = tmp_path / "big"
    f.write_bytes(b"x" * 100_000)
    ticks = iter([0.0, 5.0, 10.0, 15.0, 20.0, 25.0])
    with pytest.raises(sandbox._Out):
        sandbox._read(str(f), 1.0, lambda: next(ticks))


def test_a_node_modules_bin_link_is_reported_by_what_it_resolves_to_inside_the_project(tmp_path):
    proj = tmp_path / "p"
    pkg = proj / "node_modules" / "tool" / "bin"
    pkg.mkdir(parents=True)
    (pkg / "run.js").write_text("a")
    (proj / "node_modules" / ".bin").mkdir()
    (proj / "node_modules" / ".bin" / "run").symlink_to("../tool/bin/run.js")
    (proj / "node_modules" / ".bin" / "out").symlink_to(tmp_path / "outside-file")
    before, _ = sandbox.scan_project(str(proj))
    assert before["auto:node_modules/.bin/out"]["resolved"] == "outside"  # not even a stat of what is outside
    (pkg / "run.js").write_text("changed, and longer")  # the link itself is untouched
    after, _ = sandbox.scan_project(str(proj))
    changed = {f["path"] for f in sandbox.compare_scans(before, after)}
    assert "node_modules/.bin/run" in changed and "node_modules/.bin/out" not in changed


def test_venvs_named_venv_and_env_site_packages_py_files_and_new_pyc_files_are_reported(tmp_path):
    proj = tmp_path / "p"
    site = proj / "venv" / "lib" / "python3.12" / "site-packages"
    site.mkdir(parents=True)
    (proj / "env" / "bin").mkdir(parents=True)
    (proj / "pkg" / "__pycache__").mkdir(parents=True)
    before, _ = sandbox.scan_project(str(proj))
    (site / "sitecustomize.py").write_text("import os")
    (proj / "env" / "bin" / "python-evil").write_text("x")
    for i in range(8):
        (proj / "pkg" / "__pycache__" / f"m{i}.cpython-312.pyc").write_bytes(b"\0")
    found = sandbox.compare_scans(before, sandbox.scan_project(str(proj))[0])
    paths = {f["path"] for f in found}
    assert "venv/lib/python3.12/site-packages/sitecustomize.py" in paths and "env/bin/python-evil" in paths
    pyc = [f for f in found if f["kind"] == "pyc"]
    assert len(pyc) == sandbox.PYC_SHOWN + 1 and "3 more" in pyc[-1]["path"]  # five named, the rest counted


def test_a_stopped_scan_is_a_loud_incomplete_header(tmp_path, wshome, capsys):
    note = "INCOMPLETE: the scan stopped after 10 entries in 0.0s, at d3"
    sandbox.report_run(wshome, "/p", "opencode", {}, {"config:.claude": "h"}, [note], complete=False)
    err = capsys.readouterr().err
    assert err.splitlines()[0].startswith("ws: wall report for /p - INCOMPLETE") and note in err
    assert json.loads((wshome / "logs" / "wall-report.jsonl").read_text())["incomplete"] is True


def test_the_report_log_goes_through_ws_jsonl(tmp_path, wshome):
    for i in range(2):
        sandbox.report_run(wshome, "/p", "opencode", {}, {f"config:x{i}": "h"}, [])
    assert [r["findings"][0]["path"] for r in jsonl.read(wshome / "logs" / "wall-report.jsonl")] == ["x0", "x1"]
    assert (wshome / "logs" / "wall-report.jsonl.lock").exists()  # the lock file ws.jsonl keeps


def test_git_is_never_run_on_a_linked_dot_git(tmp_path, monkeypatch):
    proj = tmp_path / "p"
    proj.mkdir()
    (proj / ".git").symlink_to(tmp_path)
    ran = []
    monkeypatch.setattr(sandbox.subprocess, "run", lambda *a, **k: ran.append(a) or None)
    assert sandbox.git_facts(str(proj)) == sandbox.GitFacts(None, [], [])
    assert ran == []
    with pytest.raises(sandbox.SandboxError, match="symbolic link"):
        sandbox.lock_project(str(proj))


def test_walled_python_writes_its_bytecode_to_the_private_tmp_and_the_host_is_told_about_ptrace(tmp_path, monkeypatch):
    e = sandbox.launch_env({"A": "1"}, "/tmp/ws-wall-x")
    assert e["PYTHONPYCACHEPREFIX"] == "/tmp/ws-wall-x/pycache" and e["TMPDIR"] == "/tmp/ws-wall-x"
    assert "PYTHONPYCACHEPREFIX" not in sandbox.launch_env({"A": "1"})  # --no-sandbox is left as it was
    f = tmp_path / "scope"
    assert sandbox.ptrace_scope(str(f)) is None  # no Yama
    f.write_text("0\n")
    assert sandbox.ptrace_scope(str(f)) == 0
    f.write_text("1\n")
    assert sandbox.ptrace_scope(str(f)) == 1


def test_doctor_warns_when_ptrace_scope_is_zero_or_absent_and_gives_the_fix(tmp_path, monkeypatch):
    from ws import doctor, osinfo

    linux = osinfo.OSInfo(kind="linux", release="6", arch="x86_64", scheduler="systemd", supported=True)
    for scope, status in ((0, "warn"), (None, "warn"), (1, "ok"), (2, "ok")):
        monkeypatch.setattr(sandbox, "ptrace_scope", lambda scope=scope: scope)
        c = doctor._ptrace_check(linux)
        assert (c.id, c.status) == ("WS007", status)
        assert (status == "warn") == ("kernel.yama.ptrace_scope = 1" in c.fix and "/etc/sysctl.d" in c.fix)
    mac = osinfo.OSInfo(kind="macos", release="14", arch="arm64", scheduler="launchd", supported=True)
    assert doctor._ptrace_check(mac).status == "ok"
    assert doctor.WARN not in doctor.BAD  # a warning never fails the doctor


def test_the_opencode_shim_marks_the_runner_undumpable():
    shim = opencode.generate(policy.loads(""), ["ws"], Path("/x/out"), Path("/x/policy.toml"))[opencode.PLUGIN]
    assert "prctl" in shim and "symbols.prctl(4, 0, 0, 0, 0)" in shim and "await undumpable()" in shim


# --- the runner's environment -------------------------------------------------------------------------------------

CREDENTIAL_NAMES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_ADMIN_KEY", "OPENAI_API_KEY", "GITHUB_TOKEN", "GH_TOKEN", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "SSH_AUTH_SOCK", "HTTPS_PROXY", "NPM_TOKEN", "WS_ANTHROPIC_API_BASE",
)  # fmt: skip


def planted(monkeypatch) -> dict[str, str]:
    """A made-up value in each credential name, built here so no real one is ever in the file."""
    values = {name: "planted-" + "x" * 6 + f"-{i}" for i, name in enumerate(CREDENTIAL_NAMES)}
    for name, value in values.items():
        monkeypatch.setenv(name, value)
    return values


def all_values(e: dict) -> str:
    return "\n".join(f"{k}={v}" for k, v in e.items())


def test_launch_env_hands_over_only_the_listed_names():
    plain = {"PATH": "/bin", "HOME": "/h", "LC_ALL": "C", "XDG_DATA_HOME": "/d", "TERM": "x"}
    got = sandbox.launch_env({n: "v" for n in CREDENTIAL_NAMES} | plain)
    assert not set(CREDENTIAL_NAMES) & set(got)
    assert set(got) == {*plain, "WS_GATED_SESSION"} and got["WS_GATED_SESSION"] == "1"


def test_launch_env_keeps_the_runners_own_variables():
    parent = {"CLAUDE_CONFIG_DIR": "/c", "OPENCODE_CONFIG_DIR": "/o", "OPENCODE_CONFIG": "/oc", "OTHER": "x"}
    assert "CLAUDE_CONFIG_DIR" in sandbox.launch_env(parent, runner="claude-code")
    got = sandbox.launch_env(parent, runner="opencode")
    assert "CLAUDE_CONFIG_DIR" not in got and "OTHER" not in got
    assert got["OPENCODE_CONFIG_DIR"] == "/o" and got["OPENCODE_CONFIG"] == "/oc"


def test_launch_env_keeps_the_opencode_inline_config_and_folder():
    parent = {"OPENCODE_CONFIG_CONTENT": '{"permission": {}}', "OPENCODE_CONFIG_DIR": "/o", "OTHER": "x"}
    got = sandbox.launch_env(parent, runner="opencode")
    assert got["OPENCODE_CONFIG_CONTENT"] == parent["OPENCODE_CONFIG_CONTENT"] and got["OPENCODE_CONFIG_DIR"] == "/o"
    assert "OTHER" not in got


# Set by `ws run` for a leased key: the base URL and the placeholder key. A launch hands them on only by name.
LEASE_ENV = {"ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY"}


def _own_names(runner: str, tmp_path) -> set[str]:
    """Every variable the adapter's launch hint and `build_command` name, found from the adapter itself."""
    mod = {"claude-code": claude_code, "opencode": opencode}[runner]
    hint = set(re.findall(r"\b([A-Z][A-Z0-9_]+)=", mod.launch_hint(str(tmp_path))))
    model = "m" if runner == "claude-code" else "p/m"
    job = jobs.loads("j", f'runner = "{runner}"\nmodel = "{model}"\nprompt = "-hello"\n', Path("."))
    (tmp_path / "gate").mkdir(exist_ok=True)
    lease = SimpleNamespace(base_url="http://127.0.0.1:1/l/x")
    _, env = mod.build_command(job, tmp_path, lease)
    return hint | set(env)


@pytest.mark.parametrize("runner", ["claude-code", "opencode"])
def test_launch_env_keeps_every_variable_the_adapter_sets_for_its_runner(runner, tmp_path):
    names = _own_names(runner, tmp_path) - LEASE_ENV
    assert names  # the adapter names something to keep
    got = sandbox.launch_env({n: "v" for n in names}, runner=runner)
    assert names <= set(got), sorted(names - set(got))


@pytest.mark.parametrize("runner", ["claude-code", "opencode"])
def test_launch_env_leaves_the_lease_variables_to_pass_env(runner):
    parent = {n: "v" for n in LEASE_ENV}
    assert not set(sandbox.launch_env(parent, runner=runner)) & LEASE_ENV
    assert set(sandbox.launch_env(parent, runner=runner, pass_env=sorted(LEASE_ENV))) >= LEASE_ENV


def test_launch_env_keeps_only_the_named_locale_and_xdg_variables():
    named = ["LANG", "LC_ALL", "LC_CTYPE", "LC_MESSAGES", "LC_COLLATE", "LC_NUMERIC", "LC_TIME", "LC_MONETARY",
             "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"]  # fmt: skip
    other = ["LC_SECRET_NOTE", "XDG_SESSION_TOKEN", "LC_", "XDG_"]
    got = sandbox.launch_env({n: "v" for n in [*named, *other]})
    assert set(named) <= set(got) and not set(other) & set(got)


def test_pass_env_adds_exactly_the_names_given_and_cannot_unmark_the_session():
    parent = {"ANTHROPIC_API_KEY": "k", "GITHUB_TOKEN": "t", "WS_GATED_SESSION": "0"}
    got = sandbox.launch_env(parent, pass_env=["ANTHROPIC_API_KEY", "WS_GATED_SESSION", "NOT_SET"])
    assert got == {"ANTHROPIC_API_KEY": "k", "WS_GATED_SESSION": "1"}


@pytest.mark.parametrize("name", ["", "A=b", "*", "1X", "A B", "A-B", "A\n", "$(id)", "A,B"])
def test_pass_env_takes_a_plain_variable_name_only(name):
    with pytest.raises(WsError) as e:
        sandbox.check_pass_env([name])
    assert e.value.kind == "usage" and e.value.hint


def test_launch_leaves_the_shells_credentials_behind(env, monkeypatch, tmp_path):
    values = planted(monkeypatch)
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: sandbox.Wrapped(["wall", *argv]))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode"]) == 0
    _, e, _ = env[0]
    assert not set(CREDENTIAL_NAMES) & set(e)
    assert not any(v in all_values(e) for v in values.values())
    assert e["PATH"] and e["HOME"] and e["WS_GATED_SESSION"] == "1"


def test_launch_without_a_wall_also_leaves_them_behind(env, monkeypatch, capsys):
    values = planted(monkeypatch)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--no-sandbox", "--", "opencode"]) == 0
    assert not any(v in all_values(env[0][1]) for v in values.values())
    capsys.readouterr()


def test_claude_code_launch_leaves_them_behind_too(env, monkeypatch, tmp_path, wshome):
    values = planted(monkeypatch)
    claude_launch_setup(env, monkeypatch, tmp_path, wshome, "one")
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
    assert not any(v in all_values(env[0][1]) for v in values.values())


def test_pass_env_hands_over_the_named_variable_and_says_so(env, monkeypatch, tmp_path, capsys):
    values = planted(monkeypatch)
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: sandbox.Wrapped(["wall", *argv]))
    proj = tmp_path / "proj"
    proj.mkdir()
    monkeypatch.chdir(proj)
    argv = [
        "gate",
        "launch",
        "--runner",
        "opencode",
        "--pass-env",
        "GH_TOKEN",
        "--pass-env",
        "NPM_TOKEN",
        "--",
        "opencode",
    ]
    assert cli.main(argv) == 0
    _, e, _ = env[0]
    assert e["GH_TOKEN"] == values["GH_TOKEN"] and e["NPM_TOKEN"] == values["NPM_TOKEN"]
    assert "ANTHROPIC_API_KEY" not in e and "GITHUB_TOKEN" not in e
    err = capsys.readouterr().err
    assert "passing GH_TOKEN, NPM_TOKEN" in err and not any(v in err for v in values.values())


def test_a_bad_pass_env_name_stops_the_launch(env, capsys):
    assert cli.main(["gate", "launch", "--runner", "opencode", "--pass-env", "A=b", "--", "opencode"]) == 1
    err = capsys.readouterr().err
    assert "variable name" in err and "hint:" in err
    assert env == []
