"""The spend cap and the models are never changed from an agent session, whatever command reaches the profile write:
`ws install --cap`, `ws setup --cap`, the setup page's install, `ws install --model`, `ws config budget` and `ws config
model ID`. The bare `ws config model` listing still works, and a write that leaves those keys alone is never refused.
A session is planted the way tests/test_admin_in_session.py does: a runner in the process tree, no terminal."""

from __future__ import annotations

import os
import stat
import tomllib
from pathlib import Path

import pytest

from ws import agentsession, cli, config_cmd, install, profile
from ws.output import WsError

UNDER_CLAUDE = ["bash", "claude", "zsh", "tmux"]


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "Notes").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text("#!/bin/sh\necho 2.0.0\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("PATH", str(bin_dir))
    monkeypatch.delenv(agentsession.ENV_MARKER, raising=False)
    monkeypatch.chdir(home)
    return home


@pytest.fixture
def session(monkeypatch):
    """Inside an agent session: a runner above this process and no terminal (pytest's capture is not a tty)."""
    monkeypatch.setattr(agentsession, "read_ancestry", lambda: list(UNDER_CLAUDE))


def prof_of(box: Path) -> dict:
    p = profile.profile_path()
    return tomllib.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}


def guarded(box: Path) -> list:
    doc = prof_of(box)
    return [profile._at(doc, k) for k in profile.GUARDED]


@pytest.mark.parametrize(
    "argv",
    [
        ["install", "--yes", "--cap", "999", "--notes", "{notes}"],
        ["install", "--yes", "--model", "some-model", "--notes", "{notes}"],
        ["setup", "--yes", "--region", "CA", "--cap", "999"],
        ["config", "budget", "999", "--yes"],
        ["config", "model", "some-model", "--yes"],
        ["config", "model", "jobs", "some-model", "--yes"],
    ],
)
@pytest.mark.usefixtures("session")
def test_each_way_to_set_the_cap_or_a_model_fails_from_a_session(box, argv):
    before = guarded(box)
    code = cli.main([a.format(notes=box / "Notes") for a in argv])
    assert code == 3
    assert guarded(box) == before == [None, None, None]


@pytest.mark.usefixtures("session")
def test_setup_page_install_path_refuses_too(box):
    """The setup page (`ws setup --web`) installs through `install.apply`; the check is there, before any write."""
    step = install.Step(profile.profile_path(), "file", profile.dumps({"roles": {"budget": {"monthly_cap_usd": 999}}}))
    with pytest.raises(WsError) as e:
        install.apply([step])
    assert (e.value.kind, e.value.exit_code) == ("refused", 3) and "monthly_cap_usd" in e.value.message
    assert not profile.profile_path().exists()


@pytest.mark.usefixtures("session")
def test_the_profile_layer_refuses_even_when_the_command_guard_is_passed(box):
    args = type("A", (), {"yes": True, "dry_run": False, "json": False})()
    with pytest.raises(WsError) as e:
        config_cmd.set_key(args, "models", "default", "m")
    assert e.value.kind == "refused" and "models" in e.value.message
    assert not profile.profile_path().exists()


@pytest.mark.usefixtures("session")
def test_the_bare_listing_still_works_in_a_session(box, capsys):
    profile.profile_path().parent.mkdir(parents=True, exist_ok=True)
    profile.profile_path().write_text('[models]\ndefault = "d"\n', encoding="utf-8")
    assert cli.main(["config", "model"]) == 0
    assert "default: d" in capsys.readouterr().out


@pytest.mark.usefixtures("session")
def test_a_write_that_leaves_the_guarded_keys_alone_is_not_refused(box):
    p = profile.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('[models]\ndefault = "d"\n[roles.budget]\nmonthly_cap_usd = 5\n', encoding="utf-8")
    doc = prof_of(box) | {"machine": {"region": "CA"}}
    assert profile.write_if_changed(p, doc) is True
    assert prof_of(box)["machine"] == {"region": "CA"} and guarded(box)[:2] == [5, {"default": "d"}]


def test_outside_a_session_the_same_writes_go_through(box):
    assert cli.main(["config", "budget", "30", "--yes"]) == 0
    assert cli.main(["config", "model", "m", "--yes"]) == 0
    assert guarded(box)[:2] == [30, {"default": "m"}]


def test_guarded_changes_counts_added_removed_and_changed_keys():
    old = {"roles": {"budget": {"monthly_cap_usd": 5}, "model": {"default": "a"}}}
    assert profile.guarded_changes(old, old) == []
    assert profile.guarded_changes(old, {}) == ["roles.budget.monthly_cap_usd", "roles.model"]
    assert profile.guarded_changes(None, {"models": {"default": "x"}}) == ["models"]
    new = {"roles": {"budget": {"monthly_cap_usd": 6}, "model": {"default": "a"}, "notes": {"path": "x"}}}
    assert profile.guarded_changes(old, new) == ["roles.budget.monthly_cap_usd"]


# ---- round 4: the text matcher, a profile ws can't read, and the person at the runner's own terminal ----------------

W = "ws"  # built at run time, so this file's own text is not a command line
PREFIXES = ["", "setsid -f ", "env -i ", "env -u WS_GATED_SESSION ", "uv run "]
DENIED = [
    "config budget 999",
    "config model x --yes",
    "config model jobs x",
    "install --yes --cap 999",
    "install --cap=999",
    "install --ca 999",
    "install --model m",
    "install --mod m --yes",
    "setup --yes --cap 999",
    "setup --yes --model m",
]
ALLOWED = ["config model", "config model --json", "config", "install --yes", "setup --yes --region CA", "status"]


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("words", DENIED)
def test_the_gate_matcher_denies_each_spelling(prefix, words):
    from ws import policy

    line = f"{prefix}{W} {words}"
    assert policy.is_denied_command(policy.default_policy(), line), line


@pytest.mark.parametrize("words", ALLOWED)
def test_the_gate_matcher_still_allows_the_reads(words):
    from ws import policy

    line = f"{W} {words}"
    assert not policy.is_denied_command(policy.default_policy(), line), line


@pytest.mark.parametrize("in_session", [False, True])
def test_a_save_over_a_profile_that_does_not_parse_is_refused_and_leaves_it(box, monkeypatch, in_session):
    if in_session:
        monkeypatch.setattr(agentsession, "read_ancestry", lambda: list(UNDER_CLAUDE))
    p = profile.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("monthly_cap_usd = [broken\n", encoding="utf-8")
    with pytest.raises(WsError) as e:
        profile.write_if_changed(p, {"machine": {"region": "CA"}})  # no guarded key: still refused
    assert e.value.kind == "bad_config"
    assert p.read_text(encoding="utf-8") == "monthly_cap_usd = [broken\n"


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="root reads any file")
def test_a_profile_ws_cannot_read_is_a_ws_error_not_an_os_error(box):
    p = profile.profile_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("[machine]\n", encoding="utf-8")
    p.chmod(0)
    try:
        with pytest.raises(WsError) as e:
            profile.write_if_changed(p, {"machine": {"region": "CA"}})
        assert e.value.kind == "io"
    finally:
        p.chmod(0o600)


def test_a_person_at_the_runners_own_terminal_may_set_them(box, monkeypatch):
    monkeypatch.setattr(agentsession, "read_ancestry", lambda: list(UNDER_CLAUDE))
    monkeypatch.setattr(agentsession, "on_runner_terminal", lambda: True)
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    assert cli.main(["config", "budget", "30", "--yes"]) == 0
    assert cli.main(["config", "model", "m", "--yes"]) == 0
    assert guarded(box)[:2] == [30, {"default": "m"}]


def test_install_and_setup_outside_a_session_write_the_cap(box):
    assert cli.main(["install", "--yes", "--cap", "20", "--model", "m", "--notes", str(box / "Notes")]) == 0
    assert guarded(box)[0] == 20 and prof_of(box)["roles"]["model"]["default"] == "m"
    assert cli.main(["setup", "--yes", "--region", "CA", "--cap", "25"]) == 0
    assert guarded(box)[0] == 25
