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
    assert seen["argv"] == ["--model", "m", "p q"]
    assert seen["cwd"] == str(box / "home" / "code" / "r")
    assert fake.exit == 0
    (win,) = windows(fake)
    assert win[win.index("-n") + 1] == "x" and "-d" in win
    assert "window x" in capsys.readouterr().out


def test_quotes_and_spaces_in_the_prompt_stay_one_item(box, fake):
    prompt = 'fix it; don\'t "quote" --model this  twice'
    assert cli.main(["dispatch", "r", prompt]) == 0
    assert json.loads((box / "argv.json").read_text())["argv"] == [prompt]


def test_opencode_takes_the_prompt_as_a_flag(box, fake):
    assert cli.main(["dispatch", "r", "--runner", "opencode", "--model", "a/b", "go"]) == 0
    assert json.loads((box / "argv.json").read_text())["argv"] == ["--model", "a/b", "--prompt", "go"]


def test_help_prints_and_changes_nothing(box, fake, capsys):
    with pytest.raises(SystemExit) as e:
        cli.main(["dispatch", "--help"])
    assert e.value.code == 0
    assert "code root" in capsys.readouterr().out
    assert fake.calls == [] and not (box / "argv.json").exists()


def test_dry_run_starts_nothing(box, fake, capsys):
    assert cli.main(["--json", "dispatch", "r", "--name", "Fix The Login", "--dry-run", "go"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["window"] == "fix-the-login" and data["started"] is False and data["argv"][1:] == ["go"]
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
