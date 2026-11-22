"""`ws dispatch`: each flag its own argv item, one window named from --name, --help and --dry-run change nothing."""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from ws import cli, dispatch
from ws.profile import profile_path
from ws.term import tmux

STUB = """#!{python}
import json, os, sys
with open(os.environ["STUB_OUT"], "w") as f:
    json.dump({{"argv": sys.argv[1:], "cwd": os.getcwd()}}, f)
"""


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / "code" / "r").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    for name in ("claude", "opencode"):
        stub = bin_dir / name
        stub.write_text(STUB.format(python=sys.executable))
        stub.chmod(0o755)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("TMUX", "/tmp/fake,1,0")
    monkeypatch.setenv("STUB_OUT", str(tmp_path / "argv.json"))
    monkeypatch.delenv(dispatch.CODE_ROOT_ENV, raising=False)
    return tmp_path


class FakeTmux:
    """Records each tmux command; `new-window ... -- ARGV` really runs ARGV in the -c folder, as tmux would."""

    def __init__(self, version: str = "tmux 3.4"):
        self.calls: list[list[str]] = []
        self.version = version
        self.exit: int | None = None

    def __call__(self, argv: list[str]) -> tuple[int, str]:
        self.calls.append(argv)
        if argv == ["-V"]:
            return 0, self.version
        if argv[0] == "new-window":
            cmd = argv[argv.index("--") + 1 :]
            cwd = argv[argv.index("-c") + 1]
            self.exit = subprocess.run(cmd, cwd=cwd, check=False).returncode
            return 0, "@7\n"
        return 1, "unknown"


@pytest.fixture
def fake(monkeypatch):
    f = FakeTmux()
    monkeypatch.setattr(tmux, "run_tmux", f)
    return f


def windows(f: FakeTmux) -> list[list[str]]:
    return [c for c in f.calls if c[0] == "new-window"]


def test_model_and_prompt_reach_the_runner_as_separate_items(box, fake, capsys):
    assert cli.main(["dispatch", "r", "--name", "x", "--model", "m", "p q"]) == 0
    seen = json.loads((box / "argv.json").read_text())
    assert seen["argv"] == ["--name", "x", "--remote-control=x", "--model=m", "p q"]
    assert seen["cwd"] == str(box / "home" / "code" / "r")
    assert fake.exit == 0
    (win,) = windows(fake)
    assert win[win.index("-n") + 1] == "x" and "-d" in win
    assert "window x" in capsys.readouterr().out


def test_quotes_and_spaces_in_the_prompt_stay_one_item(box, fake):
    prompt = 'fix it; don\'t "quote" --model this  twice'
    assert cli.main(["dispatch", "r", prompt]) == 0
    assert json.loads((box / "argv.json").read_text())["argv"][-1] == prompt


def test_opencode_takes_the_prompt_as_a_flag(box, fake):
    assert cli.main(["dispatch", "r", "--runner", "opencode", "--model", "a/b", "go"]) == 0
    assert json.loads((box / "argv.json").read_text())["argv"] == ["--model=a/b", "--prompt", "go"]


def test_help_prints_and_changes_nothing(box, fake, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["dispatch", "--help"])
    assert e.value.code == 0
    assert "code root" in capsys.readouterr().out
    assert fake.calls == [] and not (box / "argv.json").exists()


def test_dry_run_starts_nothing(box, fake, capsys):
    assert cli.main(["--json", "dispatch", "r", "--name", "Fix The Login", "--dry-run", "go"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert (
        data["window"] == "fix-the-login"
        and data["started"] is False
        and data["argv"][1:] == ["--name", "fix-the-login", "--remote-control=fix-the-login", "go"]
    )
    assert fake.calls == []


def test_window_goes_in_the_named_session(box, fake, monkeypatch):
    monkeypatch.delenv("TMUX")
    assert cli.main(["dispatch", "r", "--session", "work", "go"]) == 0
    (win,) = windows(fake)
    assert win[win.index("-t") + 1] == "=work:"


@pytest.mark.parametrize(
    ("argv", "kind", "code"),
    [
        (["dispatch", "../r", "go"], "bad_repo", 2),
        (["dispatch", "..", "go"], "bad_repo", 2),
        (["dispatch", "nope", "go"], "no_repo", 1),
        (["dispatch", "r", "--", "--model"], "bad_prompt", 2),
        (["dispatch", "r", "--session", "a b", "go"], "bad_session", 2),
    ],
)
def test_refusals_are_typed_and_start_nothing(box, fake, capsys, argv, kind, code):
    assert cli.main(["--json", *argv]) == code
    assert json.loads(capsys.readouterr().out)["error_kind"] == kind
    assert fake.calls == []


def test_outside_tmux_without_a_session_is_refused(box, fake, capsys, monkeypatch):
    monkeypatch.delenv("TMUX")
    assert cli.main(["--json", "dispatch", "r", "go"]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "no_tmux"


def test_a_missing_runner_is_named(box, fake, capsys, monkeypatch):
    monkeypatch.setenv("PATH", str(box / "empty"))
    assert cli.main(["--json", "dispatch", "r", "go"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error_kind"] == "runner_missing" and "claude" in env["message"]


def test_old_tmux_is_refused_before_a_window(box, fake, capsys):
    fake.version = "tmux 2.9a"
    assert cli.main(["--json", "dispatch", "r", "go"]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "old_tmux"
    assert windows(fake) == []


def test_code_root_order(box, monkeypatch):
    env = {"HOME": str(box / "home"), "WS_HOME": str(box / "ws")}
    assert dispatch.code_root(None, env) == box / "home" / "code"
    prof = profile_path(env)
    prof.parent.mkdir(parents=True)
    prof.write_text('[dispatch]\ncode_root = "~/src"\n')
    assert dispatch.code_root(None, env) == box / "home" / "src"
    assert dispatch.code_root(None, {**env, dispatch.CODE_ROOT_ENV: "/r"}) == dispatch.Path("/r")
    assert dispatch.code_root("/flag", {**env, dispatch.CODE_ROOT_ENV: "/r"}) == dispatch.Path("/flag")


def test_a_model_that_starts_with_a_dash_is_not_a_flag(box, fake):
    assert cli.main(["dispatch", "r", "--model=-x", "go"]) == 0
    assert json.loads((box / "argv.json").read_text())["argv"][-2:] == ["--model=-x", "go"]


def test_a_missing_opencode_is_named(box, fake, capsys, monkeypatch):
    (box / "bin" / "opencode").unlink()
    monkeypatch.setenv("PATH", str(box / "bin"))  # only the stubs: a real opencode elsewhere must not be found
    assert cli.main(["--json", "dispatch", "r", "--runner", "opencode", "go"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error_kind"] == "runner_missing" and "opencode" in env["message"]
    assert fake.calls == []


def test_a_bad_profile_is_a_typed_error(box, fake, capsys):
    env = {"HOME": str(box / "home"), "WS_HOME": str(box / "ws")}
    prof = profile_path(env)
    prof.parent.mkdir(parents=True, exist_ok=True)
    prof.write_text("[dispatch\n")
    assert cli.main(["--json", "dispatch", "r", "go"]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "bad_config"
    prof.write_text("[dispatch]\ncode_root = 3\n")
    assert cli.main(["--json", "dispatch", "r", "go"]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "bad_config"


def test_only_your_own_home_expands(box):
    env = {"HOME": str(box / "home"), "WS_HOME": str(box / "ws")}
    assert dispatch.code_root("~", env) == box / "home"
    assert dispatch.code_root("~/x", env) == box / "home" / "x"
    assert dispatch.code_root("~bob/x", env) == dispatch.Path("~bob/x")


def write_policy(box, body: str) -> None:
    path = box / "ws" / "policy.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


def argv_of(box) -> list[str]:
    return json.loads((box / "argv.json").read_text())["argv"]


def test_claude_gets_the_window_name_twice_and_the_prompt_stays_last(box, fake):
    assert cli.main(["dispatch", "r", "--name", "Fix The Login", "--model", "m", "go"]) == 0
    assert argv_of(box) == ["--name", "fix-the-login", "--remote-control=fix-the-login", "--model=m", "go"]
    (win,) = windows(fake)
    assert win[win.index("-n") + 1] == "fix-the-login"


def test_the_label_is_the_window_slug_and_a_middle_dot_does_not_survive(box, fake):
    assert cli.main(["dispatch", "r", "--name", "ws\u00b7lead 22nov", "go"]) == 0
    (win,) = windows(fake)
    label = win[win.index("-n") + 1]
    assert label == "ws-lead-22nov" and "\u00b7" not in label
    assert argv_of(box)[:3] == ["--name", label, f"--remote-control={label}"]


def test_remote_control_is_one_item_so_the_prompt_is_never_its_value(box, fake):
    assert cli.main(["dispatch", "r", "go on"]) == 0
    argv = argv_of(box)
    assert "--remote-control" not in argv and "--remote-control=r" in argv
    assert argv[-1] == "go on" and argv.count("go on") == 1
    assert argv.index("--remote-control=r") == len(argv) - 2


def test_opencode_gets_no_name_flags(box, fake):
    assert cli.main(["dispatch", "r", "--runner", "opencode", "--name", "x", "go"]) == 0
    assert argv_of(box) == ["--prompt", "go"]


def test_a_full_class_or_no_class_keeps_remote_control(box, fake, capsys):
    write_policy(box, f'[classes.open]\naccess = "full"\nrepos = ["{box}/home/code/r"]\n')
    assert cli.main(["dispatch", "r", "go"]) == 0
    assert "--remote-control=r" in argv_of(box)
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("access", ["walled", "counts-only"])
def test_a_class_that_is_not_full_drops_remote_control_but_keeps_the_name(box, fake, capsys, access):
    extra = 'os_user = "svc"\n' if access == "walled" else ""
    write_policy(box, f'[classes.sealed]\naccess = "{access}"\n{extra}repos = ["{box}/home/code/r"]\n')
    assert cli.main(["dispatch", "r", "--name", "w", "go"]) == 0
    assert argv_of(box) == ["--name", "w", "go"]
    assert "remote control left off" in capsys.readouterr().err


def test_a_repo_in_no_class_is_counts_only_once_classes_exist(box, fake, capsys):
    write_policy(box, f'[classes.open]\naccess = "full"\nrepos = ["{box}/elsewhere"]\n')
    assert cli.main(["dispatch", "r", "go"]) == 0
    assert argv_of(box) == ["--name", "r", "go"]
    assert "remote control left off" in capsys.readouterr().err


def test_an_unreadable_policy_drops_remote_control(box, fake, capsys):
    write_policy(box, "[classes\n")
    assert cli.main(["dispatch", "r", "go"]) == 0
    assert argv_of(box) == ["--name", "r", "go"]
    assert "policy could not be read" in capsys.readouterr().err


def test_dry_run_shows_the_flags(box, fake, capsys):
    assert cli.main(["dispatch", "r", "--name", "check", "--dry-run", "hello"]) == 0
    out = capsys.readouterr().out
    assert "'--name', 'check', '--remote-control=check', 'hello'" in out
