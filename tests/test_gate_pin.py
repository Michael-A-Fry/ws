"""The gate hook runs on the folders `ws install` pinned in its command, never on the ones its environment names.

A project's settings (or any launcher) can set `WS_HOME`, an `XDG_*` variable or a `WS_*_DIR` for the hook. The hook
refuses every call then, and a policy file it was told to read that is missing is a refusal too, not the default policy.
"""

from __future__ import annotations

import io
import json
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

from ws import cli, gate, paths, policy
from ws.adapters import claude_code, opencode

CHANGED = "ws's folders changed since install"


@pytest.fixture
def box(tmp_path, monkeypatch):
    """An XDG layout (no WS_HOME) under tmp_path, with a policy file written where `ws install` would put it."""
    home = tmp_path / "home"
    home.mkdir()
    for var in (
        "WS_HOME",
        *paths.ENV_DIRS.values(),
        "XDG_CONFIG_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "XDG_CACHE_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(paths, "account_home", lambda: home.resolve())  # the account database says this is the home
    cfg = home / ".config" / "ws"
    cfg.mkdir(parents=True)
    (cfg / "policy.toml").write_text(policy.default_toml())
    return tmp_path


@dataclass
class Installed:
    """What `ws install` wrote into the hook: the policy file it names and its `--pin` arguments."""

    policy: Path
    pin: list[str]
    out: str = ""  # the stdout of the last `check`

    def check(self, monkeypatch, capsys, cmd: str, pin: list[str] | None = None) -> tuple[int, str]:
        """The hook run in whatever environment the test has set up by now."""
        payload = {"tool_name": "Bash", "tool_input": {"command": cmd}}
        monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
        code = cli.main(
            [
                "gate",
                "check",
                "--runner",
                "claude-code",
                "--policy",
                str(self.policy),
                *(self.pin if pin is None else pin),
            ]
        )
        seen = capsys.readouterr()
        self.out = seen.out
        return code, seen.err


@pytest.fixture
def installed(box):
    return Installed(paths.config_dir() / "policy.toml", paths.pin_of().args())


def test_the_hook_allows_and_denies_on_the_pinned_layout(box, monkeypatch, capsys, installed):
    assert installed.check(monkeypatch, capsys, "ls")[0] == 0
    code, err = installed.check(monkeypatch, capsys, "git push --force origin main")
    assert code == 2 and "blocked" in err


@pytest.mark.parametrize(
    ("var", "value"),
    [
        ("WS_HOME", "{box}/forged"),
        ("WS_STATE_DIR", "{box}/forged"),
        ("WS_CONFIG_DIR", "{box}/forged"),
        ("WS_DATA_DIR", "{box}/forged"),
        ("WS_CACHE_DIR", "{box}/forged"),
        ("XDG_STATE_HOME", "{box}/forged"),
        ("XDG_CONFIG_HOME", "{box}/forged"),
        ("XDG_DATA_HOME", "{box}/forged"),
        ("XDG_CACHE_HOME", "{box}/forged"),
        ("HOME", "{box}/forged"),
    ],
)
def test_a_changed_variable_refuses_every_call(box, monkeypatch, capsys, installed, var, value):
    monkeypatch.setenv(var, value.format(box=box))
    code, err = installed.check(monkeypatch, capsys, "ls")
    assert code == 2 and CHANGED in err and "ws install" in err


def test_a_variable_that_resolves_to_the_same_folders_changes_nothing(box, monkeypatch, capsys, installed):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(box / "home" / ".config"))  # what the default already is
    assert installed.check(monkeypatch, capsys, "ls")[0] == 0


def test_the_forged_folder_is_never_read(box, monkeypatch, capsys, installed):
    forged = box / "forged"
    (forged / "ws").mkdir(parents=True)
    (forged / "ws" / "policy.toml").write_text("[gate]\ndeny_commands = []\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(forged))
    code, err = installed.check(monkeypatch, capsys, "git push --force origin main")
    assert code == 2 and CHANGED in err


def test_a_hook_with_no_pin_runs_the_full_check_and_names_the_fix(box, monkeypatch, capsys, installed):
    """A hook an earlier ws wrote has no pin. It is not refused (that blocks every call of a session that cannot fix it): it
    allows what the policy allows and denies what it denies, with a note that names `ws upgrade`."""
    code, err = installed.check(monkeypatch, capsys, "ls", pin=[])
    assert code == 0 and "ws upgrade" in err
    note = json.loads(installed.out)  # Claude Code shows the systemMessage of one JSON object on stdout of an exit 0
    assert list(note) == ["systemMessage"] and "ws upgrade" in note["systemMessage"]
    code, err = installed.check(monkeypatch, capsys, "git push --force origin main", pin=[])
    assert code == 2 and "blocked" in err and "ws upgrade" in err and installed.out == ""


def test_a_pinned_hook_prints_no_note(box, monkeypatch, capsys, installed):
    code, err = installed.check(monkeypatch, capsys, "ls")
    assert (code, err, installed.out) == (0, "", "")


def test_the_note_is_on_stdout_for_claude_code_only(box, monkeypatch, capsys, installed):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool": "bash", "args": {"command": "ls"}})))
    code = cli.main(["gate", "check", "--runner", "opencode", "--policy", str(installed.policy)])
    seen = capsys.readouterr()
    assert code == 0 and seen.out == "" and "ws upgrade" in seen.err


def test_a_hook_with_no_pin_is_refused_when_home_is_not_the_accounts(box, monkeypatch, capsys, installed):
    """HOME moves every folder the protected list is built from, so an unpinned hook (which takes its layout from the
    environment) must not run on a HOME of the session's choosing. A pinned hook is refused too (the pin differs)."""
    monkeypatch.setenv("HOME", str(box / "forged"))
    (box / "forged").mkdir()
    code, err = installed.check(monkeypatch, capsys, "ls", pin=[])
    assert code == 2 and "HOME is not the account's home" in err and installed.out == ""
    code, err = installed.check(monkeypatch, capsys, "ls")
    assert code == 2 and CHANGED in err


def test_a_hook_with_no_pin_is_refused_when_the_account_cannot_be_asked(box, monkeypatch, capsys, installed):
    monkeypatch.setattr(paths, "account_home", lambda: None)  # no `pwd` here
    code, err = installed.check(monkeypatch, capsys, "ls", pin=[])
    assert code == 2 and "HOME is not the account's home" in err


def test_the_account_home_is_what_the_system_says_not_what_the_environment_says(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    assert paths.account_home() != tmp_path.resolve()
    import builtins

    real = builtins.__import__

    def no_pwd(name, *a, **k):
        if name == "pwd":
            raise ImportError(name)
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_pwd)
    assert paths.account_home() is None


def test_a_hook_with_no_pin_still_fails_closed_when_the_layout_cannot_be_told(box, monkeypatch, capsys, installed):
    (box / "home" / ".ws").mkdir()  # ~/.ws and the XDG folders both hold ws's files: no single layout
    (box / "home" / ".ws" / "policy.toml").write_text("")
    code, err = installed.check(monkeypatch, capsys, "ls", pin=[])
    assert code == 2 and "cannot be told" in err and installed.out == ""


@pytest.mark.parametrize(
    "bad",
    [
        ["--pin", "home=relative"],
        ["--pin", "nonsense"],
        ["--pin", "elsewhere=/x"],
        ["--pin", "home=/x", "--pin", "home=/y"],
    ],
)
def test_a_pin_that_cannot_be_read_is_refused(box, monkeypatch, capsys, installed, bad):
    code, err = installed.check(monkeypatch, capsys, "ls", pin=bad)
    assert code == 2 and "ws install" in err


def test_a_missing_pin_key_is_refused(box, monkeypatch, capsys, installed):
    code, err = installed.check(monkeypatch, capsys, "ls", pin=installed.pin[2:])  # without the home folder
    assert code == 2 and "incomplete" in err


def test_the_pinned_layout_is_what_the_rest_of_the_process_sees(box, monkeypatch):
    pin = paths.pin_of()
    monkeypatch.setenv("XDG_STATE_HOME", str(box / "forged"))
    with paths.pinned(pin):
        assert paths.state_dir() == pin.state and paths.home_dir() == pin.home
    assert paths.state_dir() == box / "forged" / "ws"


# --- the policy file the hook was told to read -------------------------------------------------------------------


def test_a_policy_that_is_named_and_missing_is_a_refusal_not_the_default_policy(box, monkeypatch, capsys, installed):
    (paths.config_dir() / "policy.toml").unlink()
    code, err = installed.check(monkeypatch, capsys, "ls")
    assert code == 2 and "ws install" in err and "missing" in err


def test_a_policy_that_is_a_folder_or_unreadable_is_a_refusal(box, monkeypatch, capsys, installed):
    target = paths.config_dir() / "policy.toml"
    target.unlink()
    target.mkdir()
    code, err = installed.check(monkeypatch, capsys, "ls")
    assert code == 2 and "ws install" in err


def test_no_policy_named_means_the_default_policy(box, monkeypatch, capsys, installed):
    (paths.config_dir() / "policy.toml").unlink()
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}})))
    assert cli.main(["gate", "check", "--runner", "claude-code", *installed.pin]) == 0
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"tool_name": "Bash", "tool_input": {"command": "git push --force x"}}))
    )
    assert cli.main(["gate", "check", "--runner", "claude-code", *installed.pin]) == 2


def test_load_with_required_raises_for_a_missing_file(tmp_path):
    assert policy.load(tmp_path / "none.toml") == policy.default_policy()
    with pytest.raises(policy.PolicyError):
        policy.load(tmp_path / "none.toml", required=True)


# --- what ws install writes ---------------------------------------------------------------------------------------


def _pins_in(command: str) -> list[str]:
    words = shlex.split(command)
    return [w for i, w in enumerate(words) if i and words[i - 1] == "--pin"]


def test_every_generated_hook_command_carries_the_pin(box):
    pol = policy.default_policy()
    files = claude_code.generate(pol, ["ws"], Path("/x/out"), Path("/x/policy.toml"))
    hooks = json.loads(files["settings.json"])["hooks"]
    seen = 0
    for groups in hooks.values():
        for g in groups:
            for h in g["hooks"]:
                words = shlex.split(h["command"])
                if "ws" in words[:1] or any(w.endswith("gateclient.py") for w in words):
                    seen += 1
                    assert sorted(p.split("=")[0] for p in _pins_in(h["command"])) == sorted(
                        (*paths.PIN_KEYS, paths.TMP_KEY)
                    ), h["command"]
    assert seen >= 5  # the check, the session pair and the alert pair at least


def test_the_opencode_shim_pins_its_check_and_its_session_commands(box):
    pol = policy.default_policy()
    shim = opencode.generate(pol, ["ws"], Path("/x/out"), Path("/x/policy.toml"))["plugins/ws-gate.js"]
    assert "__PIN__" not in shim
    check = json.loads(shim.split("const CHECK = ")[1].split("\n")[0])
    pin = json.loads(shim.split("const PIN = ")[1].split("\n")[0])
    assert check[-12:] == pin and pin == paths.pin_of().args()


def test_the_pin_comes_from_the_environment_install_planned_with_not_the_process(box, tmp_path):
    other = {"HOME": str(tmp_path / "elsewhere"), "WS_HOME": str(tmp_path / "elsewhere" / "ws")}
    pin = paths.pin_of(other)
    files = claude_code.generate(policy.default_policy(), ["ws"], Path("/x/out"), Path("/x/policy.toml"), pin=pin)
    cmd = json.loads(files["settings.json"])["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
    assert f"home={tmp_path / 'elsewhere'}" in shlex.split(cmd)


# --- the hooks that are advice -----------------------------------------------------------------------------------


def test_the_event_hook_skips_and_says_why_when_the_folders_changed(box, monkeypatch, capsys, installed):
    monkeypatch.setenv("XDG_STATE_HOME", str(box / "forged"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "PostToolUse"})))
    code = cli.main(["gate", "event", "--runner", "claude-code", "--policy", str(installed.policy), *installed.pin])
    err = capsys.readouterr().err
    assert code == 0 and CHANGED in err


def test_the_session_hooks_write_nothing_when_the_folders_changed(box, monkeypatch, capsys, installed):
    forged = box / "forged"
    monkeypatch.setenv("XDG_STATE_HOME", str(forged))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"session_id": "s1", "cwd": "/x"})))
    assert cli.main(["session", "start", "--runner", "claude-code", *installed.pin]) == 0
    assert CHANGED in capsys.readouterr().err
    assert not forged.exists()


def test_the_phone_alert_is_not_sent_from_a_profile_the_environment_names(box, monkeypatch, capsys, installed):
    forged = box / "forged" / "ws"
    forged.mkdir(parents=True)
    (forged / "profile.toml").write_text('[roles.notify]\nadapter = "ntfy"\n')
    monkeypatch.setenv("XDG_CONFIG_HOME", str(box / "forged"))
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"hook_event_name": "Notification"})))
    sent = []
    monkeypatch.setattr("ws.roles.ntfy.alert", lambda *a, **k: sent.append(a))
    assert cli.main(["notify", "alert", "--runner", "claude-code", *installed.pin]) == 0
    assert not sent and CHANGED in capsys.readouterr().err


def test_generated_check_commands_are_accepted_by_the_parser(box):
    pol = policy.default_policy()
    files = claude_code.generate(pol, ["ws"], Path("/x/out"), Path("/x/policy.toml"))
    cmd = json.loads(files["settings.json"])["hooks"]["PreToolUse"][0]["hooks"][0]["command"].removesuffix(" || exit 2")
    words = shlex.split(cmd)
    assert words[1:3] == ["-I", "-S"] and words[3].endswith("gateclient.py") and words[4] == "check"  # the thin client
    fallback = words[words.index("--") + 1 :]  # what it runs itself when the daemon can't answer
    assert fallback[:3] == ["ws", "gate", "check"]
    parser = cli.build_parser()
    ns = parser.parse_args(fallback[1:])
    assert ns.func is gate.cmd_check and paths.read_pin(ns.pin) == paths.pin_of()
    assert gate.hook_args("check", fallback[3:]).runner == "claude-code"  # and the daemon reads the same options
    assert claude_code.NAME == "claude-code"
