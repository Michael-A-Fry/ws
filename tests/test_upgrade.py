"""`ws upgrade`: a newer ws over hooks an older ws wrote (C108).

The hooks are built here in the 0.1.6 format (no `--pin`, the check run straight, no thin client) and merged into a
tmp-HOME settings file through the same code `ws install` uses, so the manifest records them as an older install would
have. The hook commands are then run as the runner runs them: through `sh -c`, with the payload on stdin.
"""

from __future__ import annotations

import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from ws import approvals, cli, doctor, install, paths, policy, selfcmd, upgrade, userconfig
from ws.adapters import claude_code
from ws.term import bridge

ALLOWED = {"tool_name": "Bash", "tool_input": {"command": "ls"}}
DENIED = {"tool_name": "Bash", "tool_input": {"command": "git push --force origin main"}}
SESSION_VARS = (approvals.ENV_MARKER, approvals.SANDBOX_MARKER, approvals.FD_ENV, *bridge.RUNNER_SESSION_NAMES)


class Tty(io.StringIO):
    def isatty(self) -> bool:
        return True


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("WS_GATE_DAEMON", "0")  # a hook decides in its own process, and leaves no daemon behind
    for var in SESSION_VARS:
        monkeypatch.delenv(var, raising=False)  # this suite may itself run inside a session
    monkeypatch.chdir(home)
    monkeypatch.setattr(sys, "stdin", Tty())
    return home


def settings_file(box: Path) -> Path:
    return box / ".claude" / "settings.json"


def settings(box: Path) -> dict:
    return json.loads(settings_file(box).read_text(encoding="utf-8"))


def manifest_bytes(box: Path) -> bytes:
    return install.manifest_path().read_bytes()


def v016_settings(box: Path) -> dict:
    """What 0.1.6 merged into the user's settings: the gate check run straight (`gate check --runner ...`), no `--pin`
    anywhere, the PreToolUse command wrapped to say when ws itself failed, `WS_SANDBOXED=0` and no `sandbox` block. The
    shapes are those of `git show v0.1.6:src/ws/adapters/claude_code.py` (`generate`, `session_hooks`, `alert_hook`) and
    `v0.1.6:src/ws/userconfig.py` (`_tune`, `_claude_ops`)."""
    cmd = selfcmd.argv()
    policy_file = paths.config_dir() / "policy.toml"
    out = paths.state_dir() / "gate" / "claude-code"
    where = settings_file(box)
    check = [*cmd, "gate", "check", "--runner", "claude-code", "--policy", str(policy_file), "--protect", str(out)]
    check += ["--protect", str(where)]
    notice = "ws: the gate did not run, so this call is blocked."
    pre = f'{shlex.join(check)}; rc=$?; [ "$rc" -eq 0 ] && exit 0; [ "$rc" -eq 2 ] || echo {shlex.quote(notice)} >&2; exit 2'

    def sub(name: str) -> list[dict]:
        run = f"{shlex.join(cmd)} session {name} --runner claude-code || true"
        return [{"hooks": [{"type": "command", "command": run, "timeout": 5}]}]

    alert = {"type": "command", "command": f"{shlex.join(cmd)} notify alert --runner claude-code || true", "timeout": 5}
    post = {
        "type": "command",
        "command": shlex.join([*cmd, "gate", "event", "--runner", "claude-code", "--policy", str(policy_file)]),
        "timeout": 20,
    }
    pol = policy.load(policy_file)
    deny = json.loads(
        claude_code.generate(pol, cmd, out, policy_file, protect=(str(where),), user_scope=True)["settings.json"]
    )["permissions"]["deny"]
    return {
        "permissions": {"deny": deny},
        "env": {"WS_SANDBOXED": "0"},
        "hooks": {
            "PreToolUse": [{"matcher": claude_code.MATCHER, "hooks": [{"type": "command", "command": pre}]}],
            "PostToolUse": [{"matcher": "Bash", "hooks": [post]}],
            "SessionStart": sub("start"),
            "UserPromptSubmit": sub("nudge"),
            "Notification": [{"hooks": [alert]}],
            "Stop": [{"hooks": [alert]}],
        },
    }


def ops_of(conf: dict) -> list[dict]:
    ops = [{"op": "append", "path": ["hooks", ev], "value": g} for ev, groups in conf["hooks"].items() for g in groups]
    ops += [{"op": "append", "path": ["permissions", "deny"], "value": r} for r in conf["permissions"]["deny"]]
    return ops + [{"op": "set", "path": ["env", k], "value": v} for k, v in conf["env"].items()]


def install_current(box: Path, *extra: str) -> None:
    code = cli.main(["install", "--yes", "--runner", "claude-code", "--notes", str(box / "Notes"), *extra])
    assert code == 0


@pytest.fixture
def old(box, capsys):
    """A machine that `ws install` set up and that is then put back to what 0.1.6 would have left: old hooks, no sandbox."""
    install_current(box)
    pol = paths.config_dir() / "policy.toml"  # a 0.1.6 policy has no `sandbox` key; the current default names it
    pol.write_text("".join(line for line in pol.read_text().splitlines(True) if line.strip() != "sandbox = true"))
    conf = v016_settings(box)
    merge = userconfig.Merge("claude-code", settings_file(box), tuple(ops_of(conf)), None, tuple(selfcmd.argv()), "old")
    install.apply([install.Step(settings_file(box), "merge", None, "0.1.6 hooks", merge=merge)])
    capsys.readouterr()
    assert "--pin" not in settings_file(box).read_text() and "sandbox" not in settings(box)
    return box


def hook_command(box: Path, event: str = "PreToolUse") -> str:
    (cmd,) = [
        h["command"]
        for g in settings(box)["hooks"][event]
        for h in g["hooks"]
        if "gate check" in h["command"] or "gateclient" in h["command"]
    ]
    return cmd


def run_hook(command: str, payload: dict) -> subprocess.CompletedProcess:
    """The hook as the runner runs it."""
    return subprocess.run(
        ["sh", "-c", command],
        check=False,
        input=json.dumps(payload),
        capture_output=True,
        text=True,
        env=dict(os.environ),
        timeout=60,
    )


def upgrade_hooks(capsys, *extra: str) -> tuple[int, str, str]:
    code = cli.main(["upgrade", "--hooks-only", *extra])
    seen = capsys.readouterr()
    return code, seen.out, seen.err


# --- (a) the old hook on the new code ---------------------------------------------------------------------------------


def test_an_old_hook_allows_and_denies_with_a_note(old):
    cmd = hook_command(old)
    assert "--pin" not in cmd
    ok = run_hook(cmd, ALLOWED)
    assert ok.returncode == 0
    assert list(json.loads(ok.stdout)) == ["systemMessage"] and "ws upgrade" in json.loads(ok.stdout)["systemMessage"]
    assert "ws upgrade" in ok.stderr
    no = run_hook(cmd, DENIED)
    assert no.returncode == 2 and "blocked" in no.stderr and "ws upgrade" in no.stderr and no.stdout == ""


def test_an_old_hook_denies_what_the_policy_denies_by_the_same_reason_as_a_new_one(old):
    old_no = run_hook(hook_command(old), DENIED)
    assert upgrade.rewrite(os.environ)[0].changes
    new_no = run_hook(hook_command(old), DENIED)
    assert old_no.returncode == new_no.returncode == 2
    reason = [line for line in new_no.stderr.splitlines() if "blocked" in line]
    assert reason and reason[0] in old_no.stderr


# --- (b) after the rewrite ----------------------------------------------------------------------------------------------


def test_the_rewrite_pins_every_hook_and_the_same_calls_are_allowed_and_denied_without_a_note(old, capsys):
    before = settings(old)
    assert before["env"]["WS_SANDBOXED"] == "0" and "sandbox" not in before
    code, _, err = upgrade_hooks(capsys)
    assert code == 0, err
    after = settings(old)
    commands = [h["command"] for gs in after["hooks"].values() for g in gs for h in g["hooks"]]
    ws_commands = [c for c in commands if "ws" in c.split()[0] or "gateclient" in c or " -m ws " in c]
    assert ws_commands and all("--pin" in c for c in ws_commands)
    assert userconfig.hooks_without_pin(os.environ) == []
    ok = run_hook(hook_command(old), ALLOWED)
    assert (ok.returncode, ok.stdout, ok.stderr) == (0, "", "")
    no = run_hook(hook_command(old), DENIED)
    assert no.returncode == 2 and "blocked" in no.stderr and "ws upgrade" not in no.stderr
    # the sandbox is as it was: off
    assert after["env"]["WS_SANDBOXED"] == "0" and "sandbox" not in after


def test_the_rewrite_keeps_the_persons_own_settings(old, capsys):
    doc = settings(old)
    doc["model"] = "opus"
    doc["hooks"]["Stop"].append({"hooks": [{"type": "command", "command": "echo mine"}]})
    settings_file(old).write_text(json.dumps(doc, indent=2) + "\n")
    assert upgrade_hooks(capsys)[0] == 0
    after = settings(old)
    assert after["model"] == "opus"
    assert {"hooks": [{"type": "command", "command": "echo mine"}]} in after["hooks"]["Stop"]


def test_a_second_upgrade_changes_nothing(old, capsys):
    assert upgrade_hooks(capsys)[0] == 0
    kept = (settings_file(old).read_bytes(), manifest_bytes(old))
    code, out, _ = upgrade_hooks(capsys)
    assert code == 0 and "current" in out
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == kept


def test_dry_run_changes_nothing(old, capsys):
    kept = (settings_file(old).read_bytes(), manifest_bytes(old))
    code, out, _ = upgrade_hooks(capsys, "--dry-run")
    assert code == 0 and "would make" in out
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == kept


def test_a_sandbox_that_was_on_stays_on(box, capsys):
    install_current(box)
    assert settings(box)["env"]["WS_SANDBOXED"] == "1" and settings(box)["sandbox"]["enabled"] is True
    assert upgrade_hooks(capsys)[0] == 0
    assert settings(box)["env"]["WS_SANDBOXED"] == "1" and settings(box)["sandbox"]["enabled"] is True


def test_ws_install_does_not_switch_the_sandbox_on_either(old, capsys):
    install_current(old)
    assert settings(old)["env"]["WS_SANDBOXED"] == "0" and "sandbox" not in settings(old)
    assert userconfig.hooks_without_pin(os.environ) == []  # and it did bring the hooks up to date


def test_a_policy_that_says_sandbox_true_switches_it_on(old, capsys):
    pol = paths.config_dir() / "policy.toml"
    pol.write_text(pol.read_text().replace("[gate]\n", "[gate]\nsandbox = true\n", 1))
    assert upgrade_hooks(capsys)[0] == 0
    assert settings(old)["env"]["WS_SANDBOXED"] == "1" and settings(old)["sandbox"]["enabled"] is True


def test_ws_stays_in_a_config_whose_profile_says_launch_and_is_refreshed_there(old, capsys):
    profile = paths.config_dir() / "profile.toml"
    profile.write_text(profile.read_text().replace('gate = "every-session"', 'gate = "launch"'))
    code, _, err = upgrade_hooks(capsys)
    assert code == 0, err
    assert userconfig.hooks_without_pin(os.environ) == [] and hook_command(old)
    assert 'gate = "launch"' in profile.read_text()  # the profile is the person's; an upgrade does not rewrite it


def test_an_upgrade_never_takes_ws_out_of_a_config(old, capsys, monkeypatch):
    out = install.Step(
        settings_file(old), "merge", None, "take ws out", merge=userconfig.withdrawal("claude-code", os.environ)
    )
    monkeypatch.setattr(install, "gate_steps", lambda env=None: [out])
    before = settings_file(old).read_bytes()
    code, text, _ = upgrade_hooks(capsys)
    assert code == 0 and "left ws's entries" in text
    assert settings_file(old).read_bytes() == before


# --- (c) a tampered pin ------------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "tamper",
    [
        lambda cmd, home: cmd.replace(f"home={home}", "home=/elsewhere"),
        lambda cmd, home: cmd.replace(f"home={home}", "home=relative/path"),
        lambda cmd, home: cmd.replace("--pin tmp=", "--pin nonsense=", 1),
        lambda cmd, home: cmd.replace(f"--pin home={home}", "", 1),  # one key short
        lambda cmd, home: cmd.replace(f"home={home}", f"home={home} --pin home={home}", 1),  # the same key twice
    ],
)
def test_a_tampered_pin_is_still_refused(old, capsys, tamper):
    assert upgrade_hooks(capsys)[0] == 0
    cmd = hook_command(old)
    assert f"home={old}" in cmd
    bad = tamper(cmd, old)
    assert bad != cmd
    for payload in (ALLOWED, DENIED):
        got = run_hook(bad, payload)
        assert got.returncode == 2 and got.stdout == ""
        assert (
            "folders" in got.stderr and "does not pin" not in got.stderr
        )  # refused as a bad pin, not let through as no pin


def test_a_pinned_hook_whose_environment_points_elsewhere_is_refused(old, capsys, monkeypatch):
    assert upgrade_hooks(capsys)[0] == 0
    monkeypatch.setenv("WS_HOME", str(old / "forged"))
    got = run_hook(hook_command(old), ALLOWED)
    assert got.returncode == 2 and "changed since install" in got.stderr


# --- (d) refused inside a session --------------------------------------------------------------------------------------


@pytest.mark.parametrize("var", SESSION_VARS)
def test_a_session_is_refused_whichever_sign_it_shows(old, capsys, monkeypatch, var):
    monkeypatch.setenv(var, "1")
    before = (settings_file(old).read_bytes(), manifest_bytes(old))
    for argv in (["upgrade", "--hooks-only"], ["upgrade"]):
        assert cli.main(argv) == 3
        assert "not for a session" in capsys.readouterr().err
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == before


def test_a_run_without_a_terminal_is_refused(old, capsys, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    assert cli.main(["upgrade", "--hooks-only"]) == 3
    assert "needs a terminal" in capsys.readouterr().err
    assert cli.main(["upgrade", "--hooks-only", "--dry-run"]) == 0  # a dry run only reads


def test_the_signs_are_what_the_user_scope_settings_leave_in_a_session():
    """The user-scope settings leave WS_GATED_SESSION out but set WS_SANDBOXED: that alone must refuse."""
    assert upgrade.refusal({approvals.SANDBOX_MARKER: "0"}, True)
    assert upgrade.refusal({"CLAUDECODE": "1"}, True)
    assert upgrade.refusal({}, True) is None


# --- (e) a rewrite that fails leaves a working pair --------------------------------------------------------------------


def working_old_pair(box: Path) -> None:
    cmd = hook_command(box)
    ok = run_hook(cmd, ALLOWED)
    assert ok.returncode == 0 and "ws upgrade" in ok.stderr
    assert run_hook(cmd, DENIED).returncode == 2


def test_a_refused_merge_leaves_the_old_hooks_untouched_and_working(old, capsys):
    doc = settings(old)
    doc["env"]["WS_SANDBOXED"] = "2"  # a value of the person's own where ws wants to write its marker
    settings_file(old).write_text(json.dumps(doc, indent=2) + "\n")
    before = (settings_file(old).read_bytes(), manifest_bytes(old))
    code, _, err = upgrade_hooks(capsys)
    assert code == 1 and "ws changed nothing" in err or "conflict" in err
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == before
    assert run_hook(hook_command(old), ALLOWED).returncode == 0
    assert run_hook(hook_command(old), DENIED).returncode == 2


def test_a_failure_after_the_write_puts_the_config_and_the_record_back(old, capsys, monkeypatch):
    before = (settings_file(old).read_bytes(), manifest_bytes(old))

    def fail(*a, **k):
        raise install.InstallError("disk full")

    with monkeypatch.context() as m:
        m.setattr(install, "_commit", fail)
        code, _, err = upgrade_hooks(capsys)
    assert code == 1 and "disk full" in err
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == before
    working_old_pair(old)


def test_a_rewrite_that_leaves_a_hook_unpinned_is_put_back(old, capsys, monkeypatch):
    before = settings_file(old).read_bytes()
    monkeypatch.setattr(userconfig, "hooks_without_pin", lambda env=None: ["claude-code"])
    code, _, err = upgrade_hooks(capsys)
    assert code == 1 and "put them back" in err
    assert settings_file(old).read_bytes() == before


# --- the whole command: the installer, then the rewrite in a new process --------------------------------------------------


@pytest.fixture
def swapped(old, monkeypatch):
    """The installer and the new binary's rewrite are stand-ins; the rewrite is the real one, run in this process."""
    calls: list[list[str]] = []
    monkeypatch.setattr(selfcmd, "unstable", lambda cmd=None: None)
    monkeypatch.setattr(upgrade, "swap", lambda args, scratch: calls.append(["swap"]) or 0)

    def run(argv, capture):
        calls.append(["hooks"])
        return cli.main(argv[len(selfcmd.argv()) :])

    monkeypatch.setattr(upgrade, "_run", run)
    return calls


def test_upgrade_swaps_then_rewrites(swapped, old, capsys):
    assert cli.main(["upgrade"]) == 0
    assert swapped == [["swap"], ["hooks"]]
    assert userconfig.hooks_without_pin(os.environ) == []
    assert "upgraded" in capsys.readouterr().out


def test_a_failed_installer_leaves_the_old_binary_and_the_old_hooks(swapped, old, capsys, monkeypatch):
    monkeypatch.setattr(upgrade, "swap", lambda args, scratch: 7)
    before = (settings_file(old).read_bytes(), manifest_bytes(old))
    assert cli.main(["upgrade"]) == 7
    assert "unchanged" in capsys.readouterr().out
    assert swapped == []
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == before
    working_old_pair(old)


def test_a_failed_rewrite_after_the_swap_says_which_pair_the_person_has(swapped, old, capsys, monkeypatch):
    monkeypatch.setattr(upgrade, "_run", lambda argv, capture: 1)
    before = (settings_file(old).read_bytes(), manifest_bytes(old))
    assert cli.main(["upgrade"]) == 1
    out = capsys.readouterr().out
    assert "new ws is installed" in out and "still work" in out and "--hooks-only" in out
    assert (settings_file(old).read_bytes(), manifest_bytes(old)) == before
    working_old_pair(old)


def test_a_checkout_is_not_swapped(old, capsys):
    """Run from the test's own .venv, which the installer does not manage: only the hooks half is offered."""
    assert cli.main(["upgrade"]) == 3
    assert "--hooks-only" in capsys.readouterr().err


def test_the_installer_is_given_what_the_person_asked_for(old):
    ns = type("A", (), {"version": "v0.2.0", "channel": "rc"})()
    argv = upgrade._installer_argv(Path("/x/install.sh"), ns)
    assert argv == ["bash", "/x/install.sh", "--skip-smoke-test", "--version", "v0.2.0", "--channel", "rc"]
    assert upgrade._hooks_argv()[-2:] == ["upgrade", "--hooks-only"] and upgrade._hooks_argv()[:4] == selfcmd.argv()


def test_what_is_not_the_installer_is_not_run(tmp_path, monkeypatch):
    class Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(upgrade.urllib.request, "urlopen", lambda url, timeout: Resp(b"<html>not it</html>"))
    with pytest.raises(upgrade.WsError) as caught:
        upgrade.fetch_installer(tmp_path)
    assert "not the ws installer" in caught.value.message and not (tmp_path / "install.sh").exists()
    monkeypatch.setattr(upgrade.urllib.request, "urlopen", lambda url, timeout: Resp(b"#!/usr/bin/env bash\nmain\n"))
    assert upgrade.fetch_installer(tmp_path).read_bytes().startswith(b"#!/usr/bin/env bash")


# --- doctor: WS013 -----------------------------------------------------------------------------------------------------------


def test_doctor_names_ws_upgrade_for_hooks_older_than_the_binary(old, capsys):
    check = doctor._upgrade_check(os.environ)
    assert (check.id, check.status) == ("WS013", "warn") and "ws upgrade" in check.fix
    assert upgrade_hooks(capsys)[0] == 0
    check = doctor._upgrade_check(os.environ)
    assert (check.id, check.status, check.fix) == ("WS013", "ok", "")


def test_doctor_is_ok_with_no_ws_hooks_at_all(box):
    assert doctor._upgrade_check(os.environ).status == "ok"
