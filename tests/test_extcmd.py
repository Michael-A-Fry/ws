"""The `ws` name: `ws <word>` runs `ws-<word>` from PATH, and every other word goes to the profile's fallback program.

Precedence is built-in, then `ws-<word>` on PATH, then `[cli] fallback`, then the usual usage error. Most tests replace
`extcmd.launch` with a recorder so nothing is started; a few start the real thing (`python -m ws`) to prove that the
argument list, the exit code and the environment reach the program.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from ws import cli, extcmd
from ws.profile import profile_path

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="the programs here are POSIX shell scripts")


def script(path: Path, body: str = "exit 0", mode: int = 0o755) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8")
    path.chmod(mode)
    return path


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (tmp_path / "bin").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv("PATH", str(tmp_path / "bin"))
    monkeypatch.delenv("WS_OUTPUT", raising=False)
    monkeypatch.delenv(extcmd.FALLBACK_ENV, raising=False)
    monkeypatch.setattr(extcmd, "_noted", set())
    monkeypatch.chdir(home)
    return tmp_path


@pytest.fixture
def started(monkeypatch):
    """Records each `Launch` instead of starting it, with the folder ws was in at that moment."""
    seen: list[tuple[extcmd.Launch, str]] = []

    def fake(spec):
        seen.append((spec, os.getcwd()))
        return 0

    monkeypatch.setattr(extcmd, "launch", fake)
    return seen


def fallback_to(box: Path, name: str = "old-ws", body: str = "exit 0") -> Path:
    prog = script(box / name, body)
    assert cli.main(["config", "fallback", str(prog)]) == 0
    return prog


# ---- precedence ---------------------------------------------------------------------------------------------------


def test_a_builtin_shadows_a_ws_program_of_the_same_name(box, started, capsys):
    script(box / "bin" / "ws-version")
    script(box / "bin" / "ws-gate")
    assert cli.main(["version", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "version"
    assert started == []


def test_an_external_gets_the_arguments_after_its_word(box, started):
    prog = script(box / "bin" / "ws-hello")
    assert cli.main(["hello", "a", "--b", "c d"]) == 0
    ((spec, _),) = started
    assert spec.path == str(prog) and spec.argv == ["a", "--b", "c d"]
    assert spec.env.get("WS_OUTPUT") is None and extcmd.FALLBACK_ENV not in spec.env


def test_json_becomes_an_environment_variable_and_c_becomes_the_folder(box, started):
    script(box / "bin" / "ws-hello")
    work = box / "work"
    work.mkdir()
    assert cli.main(["--json", "hello", "-C", str(work), "x"]) == 0
    ((spec, cwd),) = started
    assert spec.argv == ["x"] and spec.env["WS_OUTPUT"] == "json"
    assert Path(cwd) == work.resolve()


def test_a_missing_folder_for_an_external_is_the_usual_invalid_cwd(box, started, capsys):
    script(box / "bin" / "ws-hello")
    assert cli.main(["-C", str(box / "nowhere"), "hello"]) == 2
    assert "does not exist" in capsys.readouterr().err and started == []


def test_an_external_wins_over_the_fallback(box, started):
    prog = script(box / "bin" / "ws-hello")
    fallback_to(box)
    cli.main(["hello"])
    assert started[0][0].path == str(prog)


def test_a_word_outside_the_pattern_is_not_looked_up_on_path(box, started):
    script(box / "bin" / "ws-1x")
    script(box / "bin" / "ws-Up")
    for word in ("1x", "Up", "a/b", "x" * 50):
        assert extcmd.find_external(word) is None
    assert cli.main(["1x"]) == 2 and started == []


def test_path_entries_that_are_not_absolute_are_skipped(box, started, monkeypatch):
    script(box / "rel" / "ws-hello")
    monkeypatch.setenv("PATH", f"rel{os.pathsep}{box / 'bin'}")
    monkeypatch.chdir(box)
    assert extcmd.find_external("hello") is None


def test_an_unknown_word_with_no_fallback_is_a_did_you_mean_error(box, started, capsys):
    assert cli.main(["doctr"]) == 2
    err = capsys.readouterr().err
    assert "did you mean `doctor`?" in err and started == []
    assert cli.main([]) == 2  # a bare `ws` is the usage error too
    assert started == []


# ---- the fallback -------------------------------------------------------------------------------------------------


def test_the_fallback_gets_the_whole_original_argument_list(box, started):
    prog = fallback_to(box)
    work = box / "work"
    work.mkdir()
    for argv in (
        ["dispatch", "x"],
        ["-n", "repo", "tag"],
        [],
        ["--json", "-C", str(work), "plan", "--", "z"],
        ["Not A Word"],
    ):
        started.clear()
        assert cli.main(argv) == 0
        ((spec, cwd),) = started
        assert spec.path == str(prog) and spec.argv == argv, argv
        assert spec.env[extcmd.FALLBACK_ENV] == "1"
        assert Path(cwd) == box / "home"  # the fallback is told -C itself; ws does not change folder first


def test_a_builtin_and_its_own_flags_never_reach_the_fallback(box, started, capsys):
    fallback_to(box)
    started.clear()
    for argv in (["version"], ["--version"], ["--help"], ["help"], ["doctor", "--offline"]):
        try:
            cli.main(argv)
        except SystemExit:
            pass
    capsys.readouterr()
    assert started == []


def test_the_loop_guard_stops_a_second_hand_over(box, started, capsys, monkeypatch):
    fallback_to(box)
    started.clear()
    monkeypatch.setenv(extcmd.FALLBACK_ENV, "1")
    assert cli.main(["dispatch", "x"]) == 2
    assert "fallback program started this run" in capsys.readouterr().err
    assert cli.main(["--json"]) == 2 and started == []
    assert cli.main(["version"]) == 0  # built-ins run as ever
    script(box / "bin" / "ws-hello")
    assert cli.main(["hello"]) == 0 and len(started) == 1  # and so do externals


def test_the_guard_does_not_apply_when_no_fallback_is_set(box, started, capsys, monkeypatch):
    monkeypatch.setenv(extcmd.FALLBACK_ENV, "1")
    assert cli.main(["doctr"]) == 2
    assert "did you mean `doctor`?" in capsys.readouterr().err


@posix_only
def test_the_fallback_must_be_a_real_file_that_is_yours_and_closed_to_others(box, started, capsys, monkeypatch):
    prog = fallback_to(box)
    started.clear()

    prog.chmod(0o775)  # group-writable
    assert cli.main(["x"]) == 1
    assert "can be written by others" in capsys.readouterr().err

    prog.chmod(0o644)  # not executable
    assert cli.main(["x"]) == 1
    assert "not executable" in capsys.readouterr().err

    prog.chmod(0o755)
    with monkeypatch.context() as m:
        m.setattr(os, "geteuid", lambda: os.stat(prog).st_uid + 1)  # a file someone else owns
        assert cli.main(["x"]) == 1
    assert "belongs to another user" in capsys.readouterr().err

    prog.unlink()
    assert cli.main(["--json", "x"]) == 1
    assert json.loads(capsys.readouterr().out)["error_kind"] == "not_found"
    assert started == []


def test_a_fallback_path_must_be_absolute(box, started, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text('[cli]\nfallback = "old-ws"\n', encoding="utf-8")
    assert cli.main(["x", "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error_kind"] == "bad_config" and "absolute" in env["message"] and "ws config fallback" in env["hint"]


@pytest.mark.parametrize("argv", [["doctr"], ["--bogus"], []])
def test_a_broken_profile_leaves_the_usual_usage_error_and_names_the_problem(box, started, capsys, argv):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text("[cli\n", encoding="utf-8")
    script(box / "bin" / "ws-hello")
    assert cli.main(["hello"]) == 0  # an external does not read the profile
    started.clear()
    capsys.readouterr()
    assert cli.main(argv) == 2  # the usage error, not bad_config
    err = capsys.readouterr().err
    assert err.count("[cli] fallback was not looked up") == 1 and str(p) in err
    assert started == []
    if argv == ["doctr"]:
        assert "did you mean `doctor`?" in err


def test_a_fallback_that_is_not_a_path_is_noted_the_same_way(box, started, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text("[cli]\nfallback = 3\n", encoding="utf-8")
    assert cli.main(["x"]) == 2
    assert "[cli] fallback was not looked up" in capsys.readouterr().err and started == []


@posix_only
def test_a_ws_program_that_others_could_have_written_is_skipped_with_one_note(box, started, capsys):
    prog = script(box / "bin" / "ws-hello", mode=0o775)  # group-writable
    assert extcmd.find_external("hello") is None
    assert extcmd.find_external("hello") is None
    err = capsys.readouterr().err
    assert err.count(f"skipping {prog}") == 1 and "written by others" in err
    assert extcmd.list_externals([]) == []  # help does not list what ws would not run
    assert cli.main(["hello"]) == 2 and started == []
    prog.chmod(0o755)
    assert extcmd.find_external("hello") == str(prog)


@posix_only
def test_a_ws_program_owned_by_someone_else_is_skipped(box, monkeypatch, capsys):
    prog = script(box / "bin" / "ws-hello")
    monkeypatch.setattr(os, "geteuid", lambda: os.stat(prog).st_uid + 1)
    assert extcmd.find_external("hello") is None
    assert "belongs to another user" in capsys.readouterr().err


@posix_only
def test_a_ws_program_in_a_world_writable_folder_is_skipped_unless_the_folder_is_sticky(box, monkeypatch, capsys):
    folder = box / "open"
    prog = script(folder / "ws-hello")
    monkeypatch.setenv("PATH", str(folder))
    folder.chmod(0o777)
    assert extcmd.find_external("hello") is None
    assert "its folder" in capsys.readouterr().err
    folder.chmod(0o1777)  # sticky, like /tmp
    assert extcmd.find_external("hello") == str(prog)


@posix_only
def test_a_symlinked_ws_program_is_judged_by_the_file_it_points_at_and_its_folder(box, capsys):
    real = script(box / "tools" / "ws-hello")
    (box / "bin" / "ws-hello").symlink_to(real)
    assert extcmd.find_external("hello") == str(box / "bin" / "ws-hello")
    (box / "tools").chmod(0o777)
    assert extcmd.find_external("hello") is None
    assert "its folder" in capsys.readouterr().err


@posix_only
def test_the_fallback_folder_must_be_closed_to_others_too(box, started, capsys):
    prog = fallback_to(box, name="sub/old-ws")
    started.clear()
    capsys.readouterr()
    prog.parent.chmod(0o777)
    assert cli.main(["x", "--json"]) == 1
    env = json.loads(capsys.readouterr().out)
    assert env["error_kind"] == "refused" and "can be written by anyone" in env["message"]
    prog.parent.chmod(0o1777)
    assert cli.main(["x"]) == 0 and len(started) == 1


def test_a_tilde_path_is_stored_as_typed_and_found_under_home(box, started):
    script(box / "home" / "bin" / "old")
    assert cli.main(["config", "fallback", "~/bin/old"]) == 0
    assert 'fallback = "~/bin/old"' in profile_path().read_text(encoding="utf-8")
    assert cli.main(["x"]) == 0
    assert started[0][0].path == str(box / "home" / "bin" / "old")


# ---- ws config fallback -------------------------------------------------------------------------------------------


def test_config_fallback_sets_shows_and_clears(box, capsys):
    prog = script(box / "old")
    assert cli.main(["config", "fallback"]) == 0
    assert capsys.readouterr().out.strip() == "fallback: none"
    assert cli.main(["config", "fallback", str(prog), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["fallback"] == str(prog) and data["changed"] is True
    assert cli.main(["config", "fallback", str(prog), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["changed"] is False
    assert cli.main(["config", "fallback"]) == 0
    assert capsys.readouterr().out.strip() == f"fallback: {prog}"
    assert cli.main(["config", "fallback", "--clear"]) == 0
    assert "[cli]" not in profile_path().read_text(encoding="utf-8")
    capsys.readouterr()
    assert cli.main(["config", "fallback", "--clear", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"] == {
        "path": str(profile_path()),
        "fallback": None,
        "changed": False,
    }


def test_config_fallback_keeps_the_rest_of_the_profile(box):
    cli.main(["init", "--yes", "--create-notes", "--notes", str(box / "Notes")])
    script(box / "old")
    cli.main(["config", "fallback", str(box / "old")])
    text = profile_path().read_text(encoding="utf-8")
    assert "[machine]" in text and "[cli]" in text
    cli.main(["config", "fallback", "--clear"])
    assert "[machine]" in profile_path().read_text(encoding="utf-8")


def test_config_fallback_refuses_a_program_that_would_not_run(box, capsys):
    assert cli.main(["config", "fallback", str(box / "nope")]) == 1
    assert "is missing" in capsys.readouterr().err
    assert not profile_path().exists()
    both = script(box / "old")
    assert cli.main(["config", "fallback", str(both), "--clear"]) == 2
    assert cli.main(["config", "--clear"]) == 2
    capsys.readouterr()


def test_plain_config_is_unchanged(box, capsys):
    assert cli.main(["config", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "config"


# ---- ws help ------------------------------------------------------------------------------------------------------


def test_help_lists_externals_and_the_fallback(box, capsys):
    script(box / "bin" / "ws-bravo")
    script(box / "bin" / "ws-alpha")
    script(box / "bin" / "ws-gate")  # shadowed by the built-in
    script(box / "bin" / "ws-plain", mode=0o644)  # not executable
    script(box / "bin" / "other")
    assert cli.main(["help"]) == 0
    out = capsys.readouterr().out
    assert "Also on PATH: ws-alpha, ws-bravo" in out and "Other words go to" not in out
    prog = fallback_to(box)
    capsys.readouterr()
    cli.main(["help"])
    assert f"Other words go to {prog}" in capsys.readouterr().out
    cli.main(["help", "--json"])
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["external"] == ["ws-alpha", "ws-bravo"] and data["fallback"] == str(prog)


def test_help_for_a_command_adds_nothing(box, capsys):
    script(box / "bin" / "ws-bravo")
    cli.main(["help", "doctor"])
    assert "Also on PATH" not in capsys.readouterr().out


def test_help_still_works_with_a_broken_profile(box, capsys):
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text("[cli\n", encoding="utf-8")
    assert cli.main(["help"]) == 0
    assert "Other words go to" not in capsys.readouterr().out


# ---- the real thing -----------------------------------------------------------------------------------------------


def ws(box: Path, *argv: str, extra: dict | None = None) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": str(box / "bin"),
        "HOME": str(box / "home"),
        "WS_HOME": str(box / "home" / ".ws"),
        "TERM": "dumb",
        **(extra or {}),
    }
    return subprocess.run(
        [sys.executable, "-m", "ws", *argv],
        check=False,
        env=env,
        cwd=box / "home",
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


@posix_only
def test_an_old_ws_set_as_fallback_gets_the_same_argv_and_the_built_ins_still_run(box):
    old = script(
        box / "old-ws",
        'echo "argc=$#"; for a in "$@"; do echo "arg=$a"; done; echo "guard=$WS_FALLBACK"; exit 5',
    )
    assert ws(box, "config", "fallback", str(old)).returncode == 0
    for argv in (["dispatch", "x"], ["-n", "a", "b"], []):
        r = ws(box, *argv)
        lines = r.stdout.splitlines()
        assert r.returncode == 5, r.stderr
        assert lines[0] == f"argc={len(argv)}" and lines[1:-1] == [f"arg={a}" for a in argv]
        assert lines[-1] == "guard=1"
    r = ws(box, "doctor", "--offline")
    assert "argc=" not in r.stdout and "WS001" in r.stdout


@posix_only
def test_an_external_runs_with_its_exit_code_json_variable_and_folder(box):
    script(box / "bin" / "ws-where", 'echo "out=$WS_OUTPUT"; pwd; echo "n=$#"; exit 7')
    work = box / "work"
    work.mkdir()
    r = ws(box, "--json", "-C", str(work), "where", "a", "b")
    out = r.stdout.splitlines()
    assert r.returncode == 7 and out[0] == "out=json" and Path(out[1]).resolve() == work.resolve() and out[2] == "n=2"


@posix_only
def test_a_fallback_that_calls_ws_again_for_an_unknown_word_does_not_loop(box):
    old = script(box / "old-ws", f'"{sys.executable}" -m ws unknown-thing; echo "inner=$?"; exit 0')
    ws(box, "config", "fallback", str(old))
    r = ws(box, "outer")
    assert "inner=2" in r.stdout and "fallback program started this run" in r.stderr


@posix_only
def test_an_external_that_cannot_start_is_a_typed_error(box, capsys):
    prog = box / "bin" / "ws-broken"
    prog.write_text("#!/no/such/interpreter\n", encoding="utf-8")
    prog.chmod(0o755)
    r = ws(box, "broken", "--json")
    assert r.returncode == 1 and json.loads(r.stdout)["error_kind"] == "io"
