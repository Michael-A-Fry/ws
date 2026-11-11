"""Deny-home in the Claude Code launch settings (ADR 0026): the copy `ws gate launch --runner claude-code` starts claude
with hides the home folder and re-allows the project and what common tools need. The shared settings, the block merged
into a person's own file and `policy.sandbox_read_paths` stay as they were. Everything runs under a tmp HOME and WS_HOME;
what a real sandbox does with these paths is in tests/e2e/test_sandbox.py."""

from __future__ import annotations

import io
import json
import os
import subprocess
from pathlib import Path

import pytest

from ws import approvals, cli, policy, sandbox, userconfig
from ws.adapters import claude_code
from ws.term import bridge


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    for name in ("CLAUDE_CONFIG_DIR", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME", "WS_SANDBOXED"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.chdir(home)
    from ws import run

    monkeypatch.setattr(run, "exe_version", lambda *a, **k: "99.0.0")  # never the `claude` of the machine running this
    return home


SSH = '[gate]\ndeny_read = ["~/.ssh/**"]\n'


def shared(pol: policy.Policy) -> str:
    files = claude_code.generate(pol, ["ws"], Path("out"), Path("wshome/policy.toml"))
    return files["settings.json"]


def launch(box: Path, project: Path, toml: str = "", hosts=None, env=None, **kw) -> dict:
    """What launch_claude writes: the shared settings through for_project with the policy's allow and deny lists."""
    pol = policy.loads(toml)
    allow, deny = policy.launch_read_rules(pol)
    env = {"HOME": str(box)} if env is None else env
    text = claude_code.for_project(shared(pol), str(project), hosts, allow_read=allow, deny_read=deny, env=env, **kw)
    return json.loads(text)


def fs_of(conf: dict) -> dict:
    return conf["sandbox"]["filesystem"]


def project_in(box: Path) -> Path:
    proj = box / "code" / "proj"
    proj.mkdir(parents=True)
    return proj.resolve()


def make_defaults(box: Path) -> list[str]:
    """The default folders and files a person who uses the common tools has."""
    made = []
    for rel in (
        ".claude/shell-snapshots",
        ".claude/skills",
        ".claude/plugins",
        ".cache/uv",
        ".npm",
        ".cargo",
        ".rustup",
        "go",
        ".cache/go-build",
        ".local/bin",
        ".local/lib",
        ".local/share/uv",
        ".config/git",
    ):
        (box / rel).mkdir(parents=True)
        made.append(str(box / rel))
    (box / ".gitconfig").write_text("[user]\n")
    return [*made, str(box / ".gitconfig")]


# --- the launch copy ----------------------------------------------------------------------------------------------------


def test_launch_copy_denies_the_home_and_reallows_the_project(box):
    proj = project_in(box)
    defaults = make_defaults(box)
    fs = fs_of(launch(box, proj))
    assert str(box.resolve()) in fs["denyRead"]
    assert fs["allowRead"][0] == str(proj)  # the project first
    for d in defaults:
        assert d in fs["allowRead"], d
    assert str(box / ".local" / "share") not in fs["allowRead"]  # other tools' sign-in files live in there
    assert str(box / ".ssh") not in fs["allowRead"]
    assert "allowWrite" not in fs  # write access is as it was
    assert any(p.endswith(".git/hooks") for p in fs["denyWrite"])  # and the locks still hold


def test_exact_deny_read_paths_stay_in_deny_read_under_allow_read(box):
    proj = project_in(box)
    (box / "data" / "keys").mkdir(parents=True)
    toml = '[gate]\ndeny_read = ["~/.ssh/**", "~/data/keys"]\nallow_read = ["~/data"]\n'
    pol = policy.loads(toml)
    fs = fs_of(launch(box, proj, toml))
    exact = policy.sandbox_read_paths(pol)
    assert "~/data/keys" in exact and "~/.ssh" in exact
    assert str(box / "data") in fs["allowRead"]  # the folder above is re-allowed ...
    for path in exact:  # ... and every exact deny path is still there, secrets included
        assert path in fs["denyRead"], path
    assert any(p.endswith("/secrets") for p in fs["denyRead"])  # ws's own secrets folder


def test_credential_files_under_reallowed_folders_stay_denied(box):
    proj = project_in(box)
    make_defaults(box)
    fs = fs_of(launch(box, proj))
    for rel in (
        ".claude/.credentials.json",
        ".cargo/credentials",
        ".cargo/credentials.toml",
        ".config/git/credentials",
    ):
        assert str(box / rel) in fs["denyRead"], rel


def test_credential_files_follow_the_xdg_and_claude_folders(box, tmp_path):
    proj = project_in(box)
    cfg, claude = tmp_path / "xdg-config", tmp_path / "claude-state"
    (cfg / "git").mkdir(parents=True)
    (claude / "shell-snapshots").mkdir(parents=True)
    env = {"HOME": str(box), "XDG_CONFIG_HOME": str(cfg), "CLAUDE_CONFIG_DIR": str(claude)}
    fs = fs_of(launch(box, proj, env=env))
    assert str(cfg / "git") in fs["allowRead"] and str(claude / "shell-snapshots") in fs["allowRead"]
    assert str(cfg / "git" / "credentials") in fs["denyRead"] and str(claude / ".credentials.json") in fs["denyRead"]
    assert str(box / ".config" / "git") not in fs["allowRead"]  # the XDG variable decides, not the default


def test_dot_is_never_written_in_the_launch_settings(box):
    proj = project_in(box)
    make_defaults(box)
    toml = '[gate]\nallow_read = ["~/extra"]\n'
    (box / "extra").mkdir()
    fs = fs_of(launch(box, proj, toml))
    for key in ("denyRead", "allowRead"):
        for entry in fs[key]:
            assert entry not in (".", "./", "") and not entry.startswith("./"), (key, entry)
            assert entry.startswith(("/", "~/")), (
                key,
                entry,
            )  # `.` is ~/.claude in a --settings file: no relative path


def test_gate_launch_writes_the_deny_home_copy_the_session_is_started_with(box, monkeypatch):
    """The wiring in sandbox.launch_claude: the `--settings` file named on the command line carries the home, the
    project, [gate] allow_read and the exact deny_read paths. for_project's own tests cannot see a dropped argument."""
    wshome = box / ".ws"
    (box / "data").mkdir()
    (box / "keys").mkdir()
    toml = '[gate]\ndeny_read = ["~/.ssh/**", "~/keys"]\nallow_read = ["~/data"]\n'
    (wshome / "gate" / "claude-code").mkdir(parents=True)
    (wshome / "policy.toml").write_text(toml)
    pol = policy.loads(toml)
    out = wshome / "gate" / "claude-code"
    # The shared settings were made before the policy gained ~/keys, so only the launch copy can carry it (a policy
    # edit takes effect at the next start).
    for rel, text in claude_code.generate(policy.loads(SSH), ["ws"], out, wshome / "policy.toml").items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    proj = project_in(box)
    monkeypatch.chdir(proj)
    started = []
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: started.append((cmd, e)) or 0)
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
    cmd = started[0][0]
    assert cmd[:2] == ["claude", "--settings"]
    fs = json.loads(Path(cmd[2]).read_text())["sandbox"]["filesystem"]
    assert str(box.resolve()) in fs["denyRead"]  # deny-home
    assert str(proj) in fs["allowRead"]  # the project is let back in
    assert str(box.resolve() / "data") in fs["allowRead"]  # [gate] allow_read, ~ expanded
    for exact in policy.sandbox_read_paths(pol):  # every exact deny_read path stays hidden under the allowRead above it
        assert exact in fs["denyRead"], exact
    assert "~/keys" in fs["denyRead"]


def test_user_scope_block_is_unchanged_and_has_no_home(box):
    pol = policy.default_policy()
    wshome = box / ".ws"
    block = userconfig.expected_sandbox(pol, wshome / "policy.toml", {"HOME": str(box), "WS_HOME": str(wshome)})
    fs = block["filesystem"]
    assert "allowRead" not in fs and str(box) not in fs["denyRead"] and str(box.resolve()) not in fs["denyRead"]
    assert fs["denyRead"] == policy.sandbox_read_paths(pol)  # exactly the deny_read paths, as before
    generated = json.loads(shared(pol))["sandbox"]["filesystem"]
    assert "allowRead" not in generated and str(box.resolve()) not in generated["denyRead"]  # nor the shared file
    assert "allowRead" in fs_of(launch(box, project_in(box)))  # while the launch copy has it


def test_sandbox_off_leaves_no_block(box):
    proj = project_in(box)
    pol = policy.loads("[gate]\nsandbox = false\n")
    text = shared(pol)
    allow, deny = policy.launch_read_rules(pol)
    got = claude_code.for_project(text, str(proj), allow_read=allow, deny_read=deny, env={"HOME": str(box)})
    assert json.loads(got) == json.loads(text)
    assert "sandbox" not in json.loads(got) and json.loads(got)["env"]["WS_SANDBOXED"] == "0"
    assert json.loads(claude_code.for_project("{}", str(proj), allow_read=["/x"])) == {}


def test_sandbox_read_paths_default_output_is_unchanged(box):
    deny = 'deny_read = ["~/.ssh/**", "~/x*/y"]\n'
    with_allow = policy.loads(f'[gate]\n{deny}allow_read = ["~/data"]\n')
    without = policy.loads(f"[gate]\n{deny}")
    got = policy.sandbox_read_paths(with_allow)
    assert got == policy.sandbox_read_paths(without)  # allow_read adds nothing to what OpenCode hides
    assert got == ["~/.ssh", str(box / ".ws" / "secrets")]  # the deny_read path and ws's own secrets, nothing else


def launch_files(box: Path, toml: str) -> tuple[Path, Path]:
    """The generated settings and the policy in $WS_HOME, and a project to start from; returns (project, wshome)."""
    wshome = box / ".ws"
    out = wshome / "gate" / "claude-code"
    for rel, text in claude_code.generate(policy.loads(toml), ["ws"], out, wshome / "policy.toml").items():
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(text)
    (wshome / "policy.toml").write_text(toml)
    return project_in(box), wshome


def test_gate_launch_command_names_a_settings_file_with_the_whole_read_wall(box, monkeypatch):
    """`cli.main(["gate", "launch", ...])` end to end, with the policy in $WS_HOME and the shared settings made from it:
    the file the command names holds the home and the deny path in denyRead, the project and ~/data in allowRead."""
    (box / "data").mkdir()
    proj, _ = launch_files(box, '[gate]\ndeny_read = ["~/vault"]\nallow_read = ["~/data"]\n')
    monkeypatch.chdir(proj)
    started = []
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: started.append(cmd) or 0)
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
    settings = Path(started[0][started[0].index("--settings") + 1])
    fs = json.loads(settings.read_text())["sandbox"]["filesystem"]
    assert str(box.resolve()) in fs["denyRead"] and "~/vault" in fs["denyRead"]
    assert str(proj) in fs["allowRead"] and str(box.resolve() / "data") in fs["allowRead"]
    assert not any(p.startswith("~") for p in fs["allowRead"])  # every allowRead path is absolute
    assert settings.stat().st_mode & 0o777 == 0o600


def test_the_launch_settings_are_written_whole_or_not_at_all(box, monkeypatch):
    """A failure while the copy is made leaves the one the last session used, and no temp file beside it."""
    proj, _ = launch_files(box, "")
    monkeypatch.chdir(proj)
    started = []
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: started.append(cmd) or 0)
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
    target = Path(started[0][started[0].index("--settings") + 1])
    good = target.read_bytes()

    def fail(*_args):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", fail)
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 1
    assert target.read_bytes() == good  # not truncated
    assert [p.name for p in target.parent.iterdir()] == [target.name]  # no temp file left


def test_a_symlinked_deny_read_path_is_denied_by_its_real_path_too(box, tmp_path):
    proj = project_in(box)
    real = tmp_path / "elsewhere" / "vault"
    real.mkdir(parents=True)
    (box / "vault").symlink_to(real)
    toml = '[gate]\ndeny_read = ["~/vault"]\n'
    fs = fs_of(launch(box, proj, toml))
    assert str(real.resolve()) in fs["denyRead"] and "~/vault" in fs["denyRead"]
    # allowed through the folder that holds the real one: the exact deny stays below it
    fs = fs_of(launch(box, proj, toml + f'allow_read = ["{real.parent.resolve()}"]\n'))
    assert str(real.parent.resolve()) in fs["allowRead"] and str(real.resolve()) in fs["denyRead"]
    # and the real folder itself, named as allowed, is dropped
    fs = fs_of(launch(box, proj, toml + f'allow_read = ["{real.resolve()}"]\n'))
    assert str(real.resolve()) not in fs["allowRead"]


def test_only_the_parts_of_the_runners_state_folder_a_command_needs_are_readable(box, tmp_path):
    proj = project_in(box)
    make_defaults(box)
    state = box / ".claude"
    for name in ("projects", "ide", "file-history", "debug"):
        (state / name).mkdir()
    for name in ("settings.json", "history.jsonl", ".credentials.json"):
        (state / name).write_text("{}")
    fs = fs_of(launch(box, proj))
    assert str(state) not in fs["allowRead"]  # never the folder whole
    for name in ("shell-snapshots", "skills", "plugins"):
        assert str(state / name) in fs["allowRead"], name
    for name in ("settings.json", "history.jsonl", "projects", "file-history", "debug", "ide", ".credentials.json"):
        assert not any(p == str(state / name) or p.startswith(str(state / name) + "/") for p in fs["allowRead"]), name
    assert str(state / ".credentials.json") in fs["denyRead"]  # and its sign-in file is named as denied besides
    assert str(box.resolve()) in fs["denyRead"]  # everything else in it falls under the home deny
    other = tmp_path / "state"
    (other / "shell-snapshots").mkdir(parents=True)
    fs = fs_of(launch(box, proj, env={"HOME": str(box), "CLAUDE_CONFIG_DIR": str(other)}))
    assert str(other / "shell-snapshots") in fs["allowRead"] and str(other) not in fs["allowRead"]


def test_the_toolchain_folders_are_readable_only_when_they_exist(box):
    proj = project_in(box)
    wanted = [".cargo", ".rustup", "go", ".cache/go-build"]
    assert not any(str(box / w) in fs_of(launch(box, proj))["allowRead"] for w in wanted)
    for w in wanted:
        (box / w).mkdir(parents=True)
    fs = fs_of(launch(box, proj))
    for w in wanted:
        assert str(box / w) in fs["allowRead"], w
    cache = box / "xdg-cache"
    (cache / "go-build").mkdir(parents=True)
    fs = fs_of(launch(box, proj, env={"HOME": str(box), "XDG_CACHE_HOME": str(cache)}))
    assert str(cache / "go-build") in fs["allowRead"] and str(box / ".cache" / "go-build") not in fs["allowRead"]


# --- the default hosts: forges only for a session at a terminal ------------------------------------------------------------


def test_a_launch_session_adds_the_forges_to_the_default_hosts_and_a_headless_job_does_not(box):
    proj = project_in(box)
    headless = launch(box, proj)["sandbox"]["network"]["allowedDomains"]  # `ws run` reads the generated list as it is
    assert headless == list(claude_code.DEFAULT_DOMAINS)
    assert not set(claude_code.FORGE_DOMAINS) & set(headless)
    session = launch(box, proj, forges=True)["sandbox"]["network"]["allowedDomains"]
    assert session == [*claude_code.DEFAULT_DOMAINS, *claude_code.FORGE_DOMAINS]
    assert {"github.com", "api.github.com", "gitlab.com"} <= set(session)


def test_the_forges_are_not_added_to_a_class_list(box):
    conf = launch(box, project_in(box), hosts=["only.example.org"], forges=True)
    assert conf["sandbox"]["network"]["allowedDomains"] == ["only.example.org"]


def test_gate_launch_passes_the_forges_only_when_the_policy_names_no_hosts(box, monkeypatch):
    wshome = box / ".ws"
    out = wshome / "gate" / "claude-code"
    out.mkdir(parents=True)
    proj = project_in(box)
    monkeypatch.chdir(proj)
    started = []
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: started.append(cmd) or 0)
    monkeypatch.setattr(sandbox, "warn_old_runner", lambda *a: None)
    for toml, forges in (("", True), ('[gate]\nallow_hosts = ["api.example.org"]\n', False)):
        (wshome / "policy.toml").write_text(toml)
        for rel, text in claude_code.generate(policy.loads(toml), ["ws"], out, wshome / "policy.toml").items():
            (out / rel).write_text(text)
        assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
        hosts = json.loads(Path(started[-1][2]).read_text())["sandbox"]["network"]["allowedDomains"]
        assert ("github.com" in hosts) is forges, toml


# --- the runner's version -----------------------------------------------------------------------------------------------


def stub_claude(box, monkeypatch, version):
    """A `claude` on PATH whose version the test decides (run.exe_version is the probe)."""
    from ws import run

    exe = box / "bin" / "claude"
    exe.parent.mkdir(exist_ok=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    monkeypatch.setattr(run, "exe_version", lambda path, env=None, timeout=10.0: version)
    return {"PATH": str(exe.parent)}


def test_launch_warns_when_the_runner_is_older_than_a_key_the_wall_writes(box, monkeypatch, capsys):
    env = stub_claude(box, monkeypatch, "2.1.77")
    sandbox.warn_old_runner("claude", env, policy.loads(""))
    err = capsys.readouterr().err
    assert "WS016" in err and "Claude Code 2.1.77" in err and "sandbox.filesystem.allowRead (needs 2.1.78)" in err
    assert "permissions.blockReadsOutsideWorkingDirectories (needs 2.1.257)" in err  # every key it is too old for


def test_launch_is_quiet_when_the_runner_is_new_enough_or_the_wall_writes_no_key(box, monkeypatch, capsys):
    env = stub_claude(box, monkeypatch, "2.1.400")
    sandbox.warn_old_runner("claude", env, policy.loads(""))
    assert capsys.readouterr().err == ""
    env = stub_claude(box, monkeypatch, "1.0.0")
    sandbox.warn_old_runner("claude", env, policy.loads("[gate]\nsandbox = false\nread_outside_project = true\n"))
    assert capsys.readouterr().err == ""
    sandbox.warn_old_runner("claude", {"PATH": str(box / "empty")}, policy.loads(""))  # not found: the start says so
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("version", [None, "", "dev"])
def test_launch_warns_when_the_runner_version_cannot_be_read(box, monkeypatch, capsys, version):
    env = stub_claude(box, monkeypatch, version)
    sandbox.warn_old_runner("claude", env, policy.loads(""))
    assert "WS016" in capsys.readouterr().err


def test_gate_launch_warns_and_still_starts_an_old_runner(box, monkeypatch, capsys):
    wshome = box / ".ws"
    out = wshome / "gate" / "claude-code"
    out.mkdir(parents=True)
    (wshome / "policy.toml").write_text("")
    for rel, text in claude_code.generate(policy.loads(""), ["ws"], out, wshome / "policy.toml").items():
        (out / rel).write_text(text)
    monkeypatch.chdir(project_in(box))
    path = stub_claude(box, monkeypatch, "2.1.10")["PATH"]
    monkeypatch.setenv("PATH", path)
    started = []
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: started.append(cmd) or 0)
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 0
    assert started and "WS016" in capsys.readouterr().err  # warned, not refused


# --- git ---------------------------------------------------------------------------------------------------------------


def git(root: Path, *args: str) -> None:
    ident = ["-c", "user.name=t", "-c", f"user.email=t{chr(64)}t.invalid"]
    subprocess.run(["git", "-C", str(root), *ident, *args], check=True, capture_output=True)


def test_linked_worktree_git_folder_is_readable(box):
    main = box / "code" / "main"
    main.mkdir(parents=True)
    git(main, "init", "-q")
    git(main, "commit", "-q", "--allow-empty", "-m", "i")
    git(main, "worktree", "add", "-q", str(box / "code" / "wt"))
    wt = (box / "code" / "wt").resolve()
    fs = fs_of(launch(box, wt))
    common = str(main.resolve() / ".git")
    assert common in fs["allowRead"]  # git finds the objects and refs here
    assert f"{common}/worktrees/wt" in fs["allowRead"]  # and this worktree's own folder
    assert str(wt) == fs["allowRead"][0]
    plain = fs_of(launch(box, main.resolve()))
    assert not any(p.endswith(".git") for p in plain["allowRead"])  # a plain checkout's .git is inside the project


def test_separate_git_folder_outside_the_project_is_not_followed(box):
    """A `.git` file is a pointer a repository could forge to name any folder, so only a `worktrees/` folder of a common
    git folder is followed out. A `--separate-git-dir` is left out: git does not work in that project under deny-home
    until the folder is named in `[gate] allow_read` (docs/explanation/known-limits.md)."""
    proj = box / "code" / "p"
    proj.mkdir(parents=True)
    (box / "gitdirs").mkdir()
    subprocess.run(
        ["git", "init", "-q", "--separate-git-dir", str(box / "gitdirs" / "p"), str(proj)],
        check=True,
        capture_output=True,
    )
    fs = fs_of(launch(box, proj.resolve()))
    assert str((box / "gitdirs" / "p").resolve()) not in fs["allowRead"]
    named = fs_of(launch(box, proj.resolve(), f'[gate]\nallow_read = ["{box / "gitdirs" / "p"}"]\n'))
    assert str((box / "gitdirs" / "p").resolve()) in named["allowRead"]


def test_a_git_file_pointing_outside_the_project_is_followed_only_into_a_worktrees_folder(box):
    proj = box / "code" / "p"
    proj.mkdir(parents=True)
    elsewhere = box / "other" / "repo.git"
    elsewhere.parent.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", str(elsewhere)], check=True, capture_output=True)
    (proj / ".git").write_text(f"gitdir: {elsewhere}\n")  # a hand-made pointer, not made by `git worktree add`
    assert sandbox.git_read_folders(str(proj)) == []
    assert str(elsewhere.resolve()) not in fs_of(launch(box, proj.resolve()))["allowRead"]
    # the same repository reached as `<common>/worktrees/<name>` is what a real linked worktree has
    (elsewhere / "worktrees" / "p").mkdir(parents=True)
    (elsewhere / "worktrees" / "p" / "commondir").write_text("../..\n")
    (elsewhere / "worktrees" / "p" / "HEAD").write_text("ref: refs/heads/main\n")
    (proj / ".git").write_text(f"gitdir: {elsewhere}/worktrees/p\n")
    got = sandbox.git_read_folders(str(proj))
    assert str(elsewhere.resolve()) in got and str(elsewhere.resolve() / "worktrees" / "p") in got


# --- where the home and the project are ----------------------------------------------------------------------------------


def test_a_symlinked_home_is_denied_under_both_spellings(box, tmp_path):
    real = tmp_path / "real-home"
    real.mkdir()
    link = tmp_path / "linked-home"
    link.symlink_to(real)
    proj = real / "p"
    proj.mkdir()
    fs = fs_of(launch(box, proj.resolve(), SSH, env={"HOME": str(link)}))
    assert str(real.resolve()) in fs["denyRead"] and str(link) in fs["denyRead"]
    assert all(os.path.isabs(p) for p in fs["denyRead"] if p.startswith("/"))
    # a `~/` rule is also written with the real home in front, since the sandbox may resolve it either way
    assert str(real.resolve() / ".ssh") in fs["denyRead"] and "~/.ssh" in fs["denyRead"]


def test_a_project_outside_the_home_is_still_re_allowed(box, tmp_path):
    proj = tmp_path / "elsewhere" / "proj"
    proj.mkdir(parents=True)
    fs = fs_of(launch(box, proj.resolve()))
    assert fs["allowRead"][0] == str(proj.resolve()) and str(box.resolve()) in fs["denyRead"]


def test_a_default_folder_that_does_not_exist_is_left_out(box):
    proj = project_in(box)
    (box / ".npm").mkdir()
    fs = fs_of(launch(box, proj))
    assert str(box / ".npm") in fs["allowRead"]
    assert str(box / ".cargo") not in fs["allowRead"] and str(box / ".cache" / "uv") not in fs["allowRead"]
    assert str(box / ".cargo" / "credentials") in fs["denyRead"]  # a deny costs nothing where nothing is


def test_a_default_that_is_a_link_to_the_home_opens_nothing(box):
    proj = project_in(box)
    (box / ".cargo").symlink_to(box)  # a link back to the whole home folder must not re-open it
    fs = fs_of(launch(box, proj))
    assert str(box.resolve()) not in fs["allowRead"] and str(box / ".cargo") not in fs["allowRead"]


def test_allow_read_through_a_link_into_a_denied_path_is_dropped(box):
    proj = project_in(box)
    (box / ".ssh").mkdir()
    (box / "shortcut").symlink_to(box / ".ssh")
    fs = fs_of(launch(box, proj, SSH + 'allow_read = ["~/shortcut"]\n'))
    assert not any(p.endswith(("shortcut", ".ssh")) for p in fs["allowRead"])


def test_a_class_session_keeps_its_own_hosts_and_is_denied_the_home_as_well(box):
    proj = project_in(box)
    conf = launch(box, proj, hosts=["only.example.org"])
    assert conf["sandbox"]["network"]["allowedDomains"] == ["only.example.org"]
    assert conf["sandbox"]["network"]["strictAllowlist"] is True
    assert str(box.resolve()) in fs_of(conf)["denyRead"] and fs_of(conf)["allowRead"][0] == str(proj)


# --- the switch ---------------------------------------------------------------------------------------------------------


def test_switch_on_backs_up_and_switch_off_restores_the_settings(box, capsys):
    mine = box / ".claude" / "settings.json"
    mine.parent.mkdir()
    secret = "own-" + os.urandom(4).hex()  # built at run time
    mine.write_bytes(json.dumps({"model": "opus", "env": {"MINE": secret}}, indent=4).encode() + b"\n")
    before = mine.read_bytes()
    assert cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes")]) == 0
    capsys.readouterr()
    assert mine.read_bytes() != before and json.loads(mine.read_text())["sandbox"]["enabled"] is True
    backups = list((box / ".ws" / "backups").rglob("*"))
    assert any(p.is_file() and p.read_bytes() == before for p in backups)  # the first copy, as it was
    generated = box / ".ws" / "gate" / "claude-code" / "settings.json"
    assert generated.is_file()  # what `ws gate launch --runner claude-code` makes its copy from
    proj = project_in(box)
    got = claude_code.for_project(generated.read_text(), str(proj), env={"HOME": str(box)})
    assert str(box.resolve()) in json.loads(got)["sandbox"]["filesystem"]["denyRead"]
    cli.main(["uninstall", "--yes"])
    capsys.readouterr()
    assert mine.read_bytes() == before  # byte for byte
    assert not generated.exists()


def test_the_upgrade_command_that_rewrites_the_hooks_keeps_the_sandbox_and_the_deny_home_copy(box, capsys, monkeypatch):
    """The command the how-to names for a machine that already has ws: `ws upgrade --hooks-only` (it downloads nothing),
    run here in a scratch HOME and WS_HOME with the sandbox on. The user's settings keep the sandbox block, and the
    launch copy made from what it rewrote hides the home."""
    for var in (approvals.ENV_MARKER, approvals.SANDBOX_MARKER, approvals.FD_ENV, *bridge.RUNNER_SESSION_NAMES):
        monkeypatch.delenv(var, raising=False)  # run by a person at a terminal: this suite may itself run in a session
    monkeypatch.setattr("sys.stdin", Tty())
    mine = box / ".claude" / "settings.json"
    mine.parent.mkdir()
    mine.write_text(json.dumps({"model": "opus"}))
    assert cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes")]) == 0
    installed = mine.read_bytes()
    capsys.readouterr()
    assert cli.main(["upgrade", "--hooks-only"]) == 0
    capsys.readouterr()
    assert mine.read_bytes() == installed  # nothing changed: it was current
    assert json.loads(mine.read_text())["sandbox"]["enabled"] is True
    generated = box / ".ws" / "gate" / "claude-code" / "settings.json"
    got = claude_code.for_project(generated.read_text(), str(project_in(box)), env={"HOME": str(box)})
    assert str(box.resolve()) in json.loads(got)["sandbox"]["filesystem"]["denyRead"]
    assert cli.main(["uninstall", "--yes"]) in (0, 1)
    capsys.readouterr()
    assert json.loads(mine.read_text()) == {"model": "opus"}


# --- a repository cannot re-open the home --------------------------------------------------------------------------------


def test_a_project_that_sets_allow_read_is_still_refused_and_the_project_folder_is_never_the_home(box, monkeypatch):
    from ws import sandbox
    from ws.output import WsError

    monkeypatch.setenv("WS_SANDBOXED", "1")
    d = project_in(box)
    (d / ".claude").mkdir()
    (d / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"filesystem": {"allowRead": ["~/"]}}}))
    e = claude_code.preflight_refusal({"cwd": str(d)})
    assert isinstance(e, WsError) and e.kind == "project_settings_widen_sandbox"
    assert e.details["key"] == "sandbox.filesystem.allowRead"
    for bad in (box, box.parent, Path("/")):  # the folder the launch copy re-allows is never the home or above it
        with pytest.raises(sandbox.SandboxError):
            sandbox.project_folder(str(bad), {"HOME": str(box)})
