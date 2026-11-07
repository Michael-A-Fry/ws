"""The wall canary (ws.wallproof): every hidden path reads empty, every ws path refuses a write, the probe leaves nothing
behind, and anything less is a fail named by kind. The logic is tested with a stand-in wall and probe runner; the real
bubblewrap wall is tested where it runs here (and skipped, with the reason shown, where it does not)."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from ws import policy, sandbox, wallproof


def _wall_works() -> bool:
    return sys.platform == "linux" and bool(sandbox.shutil.which("bwrap")) and sandbox._bwrap_starts()


real_wall = pytest.mark.skipif(not _wall_works(), reason="needs bubblewrap that can start (Linux, user namespaces)")


@pytest.fixture
def box(tmp_path):
    (tmp_path / "hid").mkdir()
    (tmp_path / "hid" / "key").write_text("not a real secret")
    (tmp_path / "hidden.txt").write_text("not a real secret")
    (tmp_path / "ws").mkdir()
    (tmp_path / "ws" / "policy.toml").write_text("")
    (tmp_path / "proj").mkdir()
    pol = policy.loads(f'[gate]\ndeny_read = ["{tmp_path}/hid/**", "{tmp_path}/hidden.txt"]\n')
    return tmp_path, pol, (str(tmp_path / "ws" / "policy.toml"), str(tmp_path / "ws"))


def prove(box, **kw):
    t, pol, protect = box
    return wallproof.prove(pol, protect, state=t / "ws", project=str(t / "proj"), env={"HOME": str(t)}, **kw)


# --- the judgement, with a stand-in probe -----------------------------------------------------------------------------


def stand_in(**reply):
    """(wrap, run) where the probe 'runs' by answering with `reply` over the nonce it was given."""

    def wrap(argv, *a, **k):
        return sandbox.Wrapped(argv)

    def run(argv, **k):
        spec = json.loads(argv[-1])
        body = {"nonce": spec["nonce"], "shown": 0, "written": 0, "control": True, "left": 0, **reply}
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body) + "\n", stderr="")

    return {"wrap": wrap, "run": run}


def test_a_probe_that_finds_everything_held_is_a_pass(box):
    assert prove(box, **stand_in()) == wallproof.Proof(True)


@pytest.mark.parametrize(
    "reply, kind",
    [
        ({"shown": 1}, wallproof.HIDDEN_READABLE),
        ({"written": 1}, wallproof.PROTECTED_WRITABLE),
        ({"left": 1}, wallproof.PROBE_LEFT),
        (
            {"control": False},
            wallproof.PROBE_FAILED,
        ),  # it could not write where the wall allows: its denials mean nothing
    ],
)
def test_one_failed_check_is_a_fail_with_its_kind(box, reply, kind):
    got = prove(box, **stand_in(**reply))
    assert not got.ok and got.kinds == (kind,)


def test_a_half_pass_lists_every_kind_and_never_a_path(box):
    got = prove(box, **stand_in(shown=2, written=1))
    assert got.kinds == (wallproof.HIDDEN_READABLE, wallproof.PROTECTED_WRITABLE)
    text = wallproof.describe(got)
    assert str(box[0]) not in text and "key" not in text


def failing_run(how):
    def run(argv, **k):
        if how == "timeout":
            raise subprocess.TimeoutExpired(argv, 1)
        if how == "oserror":
            raise OSError("no such program")
        if how == "exit":
            return subprocess.CompletedProcess(argv, 1, stdout="", stderr="boom")
        if how == "garbage":
            return subprocess.CompletedProcess(argv, 0, stdout="all fine\n", stderr="")
        if how == "wrong-nonce":
            spec = json.loads(argv[-1])
            body = {"nonce": spec["nonce"] + "x", "shown": 0, "written": 0, "control": True, "left": 0}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")
        if how == "not-numbers":
            spec = json.loads(argv[-1])
            body = {"nonce": spec["nonce"], "shown": False, "written": 0, "control": True, "left": 0}
            return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(body), stderr="")
        raise AssertionError(how)

    return run


@pytest.mark.parametrize("how", ["timeout", "oserror", "exit", "garbage", "wrong-nonce", "not-numbers"])
def test_a_probe_that_does_not_run_or_does_not_answer_properly_is_never_a_pass(box, how):
    got = prove(box, wrap=stand_in()["wrap"], run=failing_run(how))
    assert got == wallproof.Proof(False, (wallproof.PROBE_FAILED,))


def test_a_wall_that_cannot_be_built_is_a_fail(box):
    def wrap(*a, **k):
        raise sandbox.SandboxError("no bwrap")

    assert prove(box, wrap=wrap, run=stand_in()["run"]) == wallproof.Proof(False, (wallproof.WALL_UNAVAILABLE,))


def test_the_answer_is_worked_out_each_time_and_nothing_is_kept(box):
    t = box[0]
    calls = []
    standing = stand_in()

    def run(argv, **k):
        calls.append(1)
        return standing["run"](argv, **k)

    before = sorted(p.name for p in t.iterdir())
    assert prove(box, wrap=standing["wrap"], run=run).ok and prove(box, wrap=standing["wrap"], run=run).ok
    assert len(calls) == 2  # asked twice, answered twice
    assert sorted(p.name for p in t.iterdir()) == before  # no record of a pass, anywhere beside the paths


def test_each_probe_gets_its_own_one_use_number(box):
    seen = []
    standing = stand_in()

    def run(argv, **k):
        seen.append(json.loads(argv[-1])["nonce"])
        return standing["run"](argv, **k)

    prove(box, wrap=standing["wrap"], run=run)
    prove(box, wrap=standing["wrap"], run=run)
    assert len(set(seen)) == 2


# --- the real wall -----------------------------------------------------------------------------------------------------


@real_wall
def test_in_the_real_wall_every_hidden_path_reads_empty_and_every_ws_path_refuses_a_write(box):
    t = box[0]
    assert prove(box) == wallproof.Proof(True)
    assert not list((t / "ws").glob(".ws-wall-probe-*")) and not list(t.glob(".ws-wall-probe-*"))  # nothing left behind
    assert (t / "hid" / "key").read_text() == "not a real secret"  # and the host's own file is as it was


@real_wall
def test_in_the_real_wall_a_hidden_path_the_wall_does_not_hide_is_a_fail(box):
    def wrap(argv, _pol, *a, **k):  # a wall built without the hidden paths: the probe must see the content
        return sandbox.wrap(argv, policy.loads(""), *a, **k)

    got = prove(box, wrap=wrap)
    assert got == wallproof.Proof(False, (wallproof.HIDDEN_READABLE,))


@real_wall
def test_in_the_real_wall_a_ws_path_the_wall_does_not_hold_is_a_fail(box):
    t, pol, _ = box
    held = t / "proj" / "policy.toml"  # a protected file inside the folder the session may write
    held.write_text("")

    def wrap(argv, _pol, home, _protect, **k):  # a wall built without the protected paths
        return sandbox.wrap(argv, _pol, home, [], **k)

    got = wallproof.prove(pol, (str(held),), state=t / "ws", project=str(t / "proj"), env={"HOME": str(t)}, wrap=wrap)
    assert got == wallproof.Proof(False, (wallproof.PROTECTED_WRITABLE,))
    assert held.read_text() == ""  # opened for writing, never truncated or changed
    assert not list((t / "proj").glob(".ws-wall-probe-*"))


@real_wall
def test_in_the_real_wall_a_protected_path_that_does_not_exist_yet_counts_as_one_a_session_could_create(box):
    t, pol, _ = box
    got = wallproof.prove(
        pol, (str(t / "proj" / "later.toml"),), state=t / "ws", project=str(t / "proj"), env={"HOME": str(t)}
    )
    assert got == wallproof.Proof(False, (wallproof.PROTECTED_WRITABLE,))
    assert not (t / "proj" / "later.toml").exists()


# --- ws doctor, WS012 ---------------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    (home / ".ws").mkdir()
    return {"HOME": str(home), "WS_HOME": str(home / ".ws")}


def profile(env, runner="claude-code", gate="every-session"):
    text = f'[roles.runner]\nadapter = "{runner}"\ngate = "{gate}"\n'
    (Path(env["WS_HOME"]) / "profile.toml").write_text(text)


def sandboxed_file(env):
    folder = Path(env["HOME"]) / ".claude"
    folder.mkdir(exist_ok=True)
    (folder / "settings.json").write_text(json.dumps({"sandbox": {"enabled": True}}))


@pytest.fixture
def host(monkeypatch):
    monkeypatch.setattr(wallproof.shutil, "which", lambda name, **k: f"/usr/bin/{name}")


def test_doctor_says_proven_when_the_wall_is_up_and_holds(env, host):
    profile(env)
    sandboxed_file(env)
    status, detail, fix = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: wallproof.Proof(True))
    assert status == "ok" and "proven" in detail and fix == ""


def test_doctor_names_the_kind_of_a_half_pass_and_keeps_the_strict_reading(env, host):
    profile(env)
    sandboxed_file(env)
    bad = wallproof.Proof(False, (wallproof.HIDDEN_READABLE,))
    status, detail, fix = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: bad)
    assert status == "warn" and "not proven" in detail and "can still be read" in detail and "strictly" in detail
    assert env["HOME"] not in detail and fix


def test_doctor_says_unsandboxed_and_how_to_get_fewer_refusals_when_the_user_file_has_no_block(env, host):
    profile(env)
    status, detail, fix = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: wallproof.Proof(True))
    assert status == "warn" and "unsandboxed" in detail and "run under `ws gate launch` for fewer refusals" in detail
    assert fix == "run `ws install`"


def test_doctor_says_unsandboxed_when_the_policy_switches_the_sandbox_off(env, host):
    profile(env)
    (Path(env["WS_HOME"]) / "policy.toml").write_text("[gate]\nsandbox = false\n")
    status, detail, fix = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: pytest.fail("not asked"))
    assert status == "warn" and "unsandboxed" in detail and "sandbox = false" in detail and "sandbox = true" in fix


def test_doctor_says_unsandboxed_for_a_runner_with_no_sandbox_of_its_own(env, host):
    profile(env, runner="opencode")
    status, detail, _ = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: pytest.fail("not asked"))
    assert status == "warn" and "opencode" in detail and "for fewer refusals" in detail


def test_doctor_on_linux_without_bubblewrap_or_socat_gives_the_one_install_command(env, monkeypatch):
    profile(env)
    monkeypatch.setattr(wallproof.shutil, "which", lambda name, **k: "/usr/bin/bwrap" if name == "bwrap" else None)
    status, detail, fix = wallproof.doctor_check(env, "wsl", prover=lambda *a, **k: pytest.fail("not asked"))
    assert status == "fail" and "socat is not installed" in detail and "Claude Code refuses to start" in detail
    assert "sudo apt install bubblewrap socat" in fix


def test_doctor_on_a_launch_profile_without_the_tools_warns_and_does_not_fail(env, monkeypatch):
    profile(env, gate="launch")
    monkeypatch.setattr(wallproof.shutil, "which", lambda name, **k: None)
    status, detail, _ = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: pytest.fail("not asked"))
    assert status == "warn" and "bwrap and socat are not installed" in detail


def test_doctor_on_native_windows_refuses_to_call_it_sandboxed(env):
    status, detail, fix = wallproof.doctor_check(env, "windows")
    assert status == "warn" and "no sandbox" in detail and "WSL2" in fix


def test_doctor_reports_a_wall_that_cannot_be_built_as_unsandboxed(env, host):
    profile(env, gate="launch")
    nope = wallproof.Proof(False, (wallproof.WALL_UNAVAILABLE,))
    status, detail, fix = wallproof.doctor_check(env, "linux", prover=lambda *a, **k: nope)
    assert status == "warn" and "unsandboxed" in detail and "bubblewrap" in fix


def test_the_doctor_runs_the_check_as_WS012(env, host, monkeypatch):
    from ws import doctor, osinfo

    profile(env, gate="launch")
    monkeypatch.setattr(wallproof, "prove", lambda *a, **k: wallproof.Proof(True))
    osi = osinfo.OSInfo(kind="linux", release="x", arch="x86_64", scheduler="systemd", supported=True)
    checks = doctor.run_checks(osi, doctor.profile_path(env), env, network=False)
    ours = [c for c in checks if c.id == "WS012"]
    assert len(ours) == 1 and ours[0].role == "sandbox" and ours[0].status == "ok"
