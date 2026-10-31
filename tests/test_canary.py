"""`ws canary`, versions.json, and doctor's WS101 saying so when a runner or the default model changed.

The runners are small fakes on PATH that print a version; no model and no network are involved."""

from __future__ import annotations

import copy
import json
import os
import stat
import sys

import jsonschema
import pytest

from ws import canary, cli, doctor, osinfo, runrecord
from ws.adapters import claude_code
from ws.profile import dumps

VALIDATOR = jsonschema.Draft202012Validator(runrecord.schema())


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.setenv(
        "PATH", str(bin_dir)
    )  # only the fakes: a real claude or opencode on this machine is not the test
    monkeypatch.chdir(home)
    return home


def fake(box, exe: str, version: str | None = "1.0.0") -> None:
    """A runner on PATH that prints `<version> (Fake)` for --version; None = it exits 1."""
    path = box.parent / "bin" / exe
    body = "sys.exit(1)" if version is None else f'print("{version} (Fake)")'
    path.write_text(f"#!{sys.executable}\nimport sys\n{body}\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def profile(box, runner="claude-code", model="model-a") -> None:
    cfg = {"roles": {"runner": {"adapter": runner}, "model": {"default": model}}}
    (box / ".ws" / "profile.toml").write_text(dumps(cfg))


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def lines(box, name="canary.jsonl") -> list[dict]:
    p = box / ".ws" / name
    return [json.loads(x) for x in p.read_text().splitlines()] if p.is_file() else []


# ---- the schema check ---------------------------------------------------------------------------------------------------


def _mutations():
    rec = canary.sample_record()
    out = {"valid": rec}
    for name, change in {
        "extra key": lambda r: r.update(surprise=1),
        "missing key": lambda r: r.pop("cost_usd"),
        "bad run id": lambda r: r.update(run_id="nope"),
        "bad status": lambda r: r.update(status="done"),
        "wrong schema": lambda r: r.update(schema=2),
        "negative tokens": lambda r: r["tokens"].update(input=-1),
        "token not int": lambda r: r["tokens"].update(input="3"),
        "float token": lambda r: r["tokens"].update(output=1.5),
        "bool for int": lambda r: r.update(duration_ms=True),
        "long error": lambda r: r.update(error="x" * 301),
        "bad rule of two": lambda r: r["policy"].update(rule_of_two="maybe"),
        "model item missing": lambda r: r.update(models_used=[{"model": "m"}]),
        "empty runner": lambda r: r.update(runner=""),
        "not truncated flag": lambda r: r.update(output_truncated="no"),
        "good model item": lambda r: r.update(
            models_used=[
                {"model": "m", "input": 1, "output": 2, "cache_creation": None, "cache_read": 0, "cost_usd": 0.5}
            ]
        ),
        "secret item": lambda r: r.update(secrets_used=[{"name": "k", "host": "h"}]),
        "secret item extra": lambda r: r.update(secrets_used=[{"name": "k", "host": "h", "value": "v"}]),
    }.items():
        r = copy.deepcopy(rec)
        change(r)
        out[name] = r
    return out


@pytest.mark.parametrize(("name", "rec"), _mutations().items())
def test_the_small_validator_agrees_with_jsonschema(name, rec):
    assert (canary.validate(rec, runrecord.schema()) == []) == VALIDATOR.is_valid(rec), name


def test_a_record_ws_would_write_validates():
    assert canary.validate(json.loads(json.dumps(canary.sample_record())), runrecord.schema()) == []


def test_the_run_record_check_notices_when_the_keys_and_schema_part_ways(tmp_path, monkeypatch):
    assert canary.check_run_record(tmp_path)[0] == canary.PASS
    monkeypatch.setattr(runrecord, "KEYS", runrecord.KEYS[:-1])
    status, why = canary.check_run_record(tmp_path)
    assert status == canary.FAIL and "disagree" in why


# ---- the checks --------------------------------------------------------------------------------------------------------


def test_a_canary_run_passes_and_leaves_one_line(box, capsys):
    fake(box, "claude", "2.5.1")
    profile(box)
    code, out, _ = run(capsys, "canary")
    assert code == 0 and "claude-code  ok  2.5.1" in out
    (line,) = lines(box)
    assert line["ok"] is True and line["model"] == "model-a" and line["schema"] == 1
    res = line["runners"]["claude-code"]
    assert res["version"] == "2.5.1" and res["ok"] is True
    assert res["checks"] == {"version": "pass", "gate": "pass", "hook_config": "pass", "run_record": "pass"}
    code, out, _ = run(capsys, "canary", "--json")
    assert code == 0 and json.loads(out)["data"]["details"]["claude-code"]["gate"].startswith("blocks")
    assert len(lines(box)) == 2


def test_opencode_is_checked_too(box, capsys):
    fake(box, "opencode", "1.19.4")
    code, out, _ = run(capsys, "canary", "--runner", "opencode")
    assert code == 0 and "opencode  ok  1.19.4" in out
    assert lines(box)[0]["runners"]["opencode"]["checks"]["gate"] == "pass"


def test_every_runner_ws_ships_has_a_canned_payload():
    from ws import adapters

    assert set(adapters.all_adapters()) <= set(canary.PAYLOADS)


def test_a_runner_that_will_not_run_fails_with_exit_1(box, capsys):
    code, out, _ = run(capsys, "canary", "--runner", "claude-code")
    assert code == 1 and "FAILED" in out and "not on PATH" in out
    assert lines(box)[0]["ok"] is False
    fake(box, "claude", None)
    code, out, _ = run(capsys, "canary", "--runner", "claude-code")
    assert code == 1 and "did not run" in out


def test_no_runner_to_check_is_an_error(box, capsys):
    code, _, err = run(capsys, "canary")
    assert code == 1 and "no runner to check" in err


def test_without_a_runner_flag_it_takes_the_profiles_then_whatever_is_on_path(box, capsys):
    fake(box, "claude")
    fake(box, "opencode")
    assert set(canary.pick_runners(None, os.environ)) == {"claude-code", "opencode"}
    profile(box, runner="opencode")
    assert canary.pick_runners(None, os.environ) == ["opencode"]
    assert canary.pick_runners("claude-code", os.environ) == ["claude-code"]


def test_the_gate_check_fails_if_a_denied_command_gets_through(box, monkeypatch):
    # an update that renames the tool: the payload parses to "not governed", so nothing is blocked
    monkeypatch.setitem(
        canary.PAYLOADS, "claude-code", lambda cmd: {"tool_name": "Shell", "tool_input": {"command": cmd}}
    )
    status, why = canary.check_gate(claude_code, os.environ, box)
    assert status == canary.FAIL and "not blocked" in why


def test_the_gate_check_fails_if_everything_is_blocked(box, monkeypatch):
    # an update that changes the payload shape: ws fails closed and blocks even `ls`
    monkeypatch.setitem(canary.PAYLOADS, "claude-code", lambda cmd: {"tool_name": "Bash", "tool_input": {"cmd": cmd}})
    status, why = canary.check_gate(claude_code, os.environ, box)
    assert status == canary.FAIL and "plain command was blocked" in why


def test_the_hook_config_check_fails_on_a_config_that_does_not_parse_or_lost_its_hook(box, monkeypatch):
    assert canary.check_hook_config(claude_code, box)[0] == canary.PASS
    monkeypatch.setattr(claude_code, "generate", lambda *a, **k: {"settings.json": "{not json"})
    assert canary.check_hook_config(claude_code, box)[0] == canary.FAIL
    monkeypatch.setattr(claude_code, "generate", lambda *a, **k: {"settings.json": json.dumps({"permissions": {}})})
    status, why = canary.check_hook_config(claude_code, box)
    assert status == canary.FAIL and "PreToolUse" in why


def test_a_failing_check_makes_the_command_exit_1_and_is_recorded(box, capsys, monkeypatch):
    fake(box, "claude")
    monkeypatch.setattr(claude_code, "generate", lambda *a, **k: {"settings.json": "{"})
    code, out, _ = run(capsys, "canary", "--runner", "claude-code")
    assert code == 1 and "fail  hook_config" in out
    assert lines(box)[0]["runners"]["claude-code"]["checks"]["hook_config"] == "fail"


def test_nothing_is_written_when_ws_has_no_home_yet(tmp_path, monkeypatch, capsys):
    fake_home = tmp_path / "nohome"
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("WS_HOME", str(fake_home / ".ws"))
    monkeypatch.setenv("PATH", str(tmp_path))
    result = canary.run_canary(["claude-code"], os.environ)
    assert result["runners"]["claude-code"]["checks"]["version"] == "fail"
    assert not fake_home.exists()


# ---- versions.json ------------------------------------------------------------------------------------------------------


def test_observe_remembers_and_reports_a_change(box):
    env = dict(os.environ)
    assert canary.observe(env, "claude-code", "1.0.0", "m1") == []  # first sighting is not a change
    path = box / ".ws" / "versions.json"
    assert json.loads(path.read_text())["runners"]["claude-code"]["version"] == "1.0.0"
    assert canary.observe(env, "claude-code", "1.0.0", "m1") == []
    assert canary.observe(env, "claude-code", "1.1.0", "m1", record=False) == ["claude-code 1.1.0, was 1.0.0"]
    assert json.loads(path.read_text())["runners"]["claude-code"]["version"] == "1.0.0"  # not recorded
    assert canary.observe(env, "claude-code", "1.1.0", "m2") == [
        "claude-code 1.1.0, was 1.0.0",
        "default model m2, was m1",
    ]
    assert canary.observe(env, "claude-code", "1.1.0", "m2") == []
    data = json.loads(path.read_text())
    assert data["default_model"] == "m2" and data["runners"]["claude-code"]["version"] == "1.1.0"


def test_observe_keeps_each_runner_apart_and_ignores_a_missing_version(box):
    env = dict(os.environ)
    canary.observe(env, "claude-code", "1.0.0", None)
    canary.observe(env, "opencode", "9.0.0", None)
    assert canary.observe(env, "claude-code", None, None) == []
    runners = json.loads((box / ".ws" / "versions.json").read_text())["runners"]
    assert runners == {"claude-code": {"version": "1.0.0"}, "opencode": {"version": "9.0.0"}}


def test_observe_does_not_make_ws_home_or_write_for_a_bare_environment(box, tmp_path):
    assert canary.observe({"WS_HOME": str(tmp_path / "missing")}, "claude-code", "1", "m") == []
    assert not (tmp_path / "missing").exists()
    assert canary.observe({"PATH": "/usr/bin"}, "claude-code", "1", "m") == []


def test_a_damaged_versions_file_is_replaced_not_fatal(box):
    (box / ".ws" / "versions.json").write_text("{nope")
    assert canary.observe(dict(os.environ), "claude-code", "1.0.0", "m") == []
    assert json.loads((box / ".ws" / "versions.json").read_text())["runners"]["claude-code"]["version"] == "1.0.0"


# ---- doctor ------------------------------------------------------------------------------------------------------------


def ws101(box, network):
    checks = doctor.run_checks(osinfo.detect(), box / ".ws" / "profile.toml", env=dict(os.environ), network=network)
    return next(c for c in checks if c.id == "WS101")


def test_doctor_records_versions_and_only_reports_versions_when_nothing_changed(box):
    fake(box, "claude", "1.0.0")
    profile(box)
    c = ws101(box, network=True)
    assert c.status == "ok" and "claude-code 1.0.0" in c.detail and "changed" not in c.detail
    assert json.loads((box / ".ws" / "versions.json").read_text()) == {
        "schema": 1,
        "runners": {"claude-code": {"version": "1.0.0"}},
        "default_model": "model-a",
    }
    assert "changed" not in ws101(box, network=True).detail
    assert lines(box) == []  # the canary has not run


def test_doctor_says_so_and_runs_the_canary_when_the_runner_changed(box):
    fake(box, "claude", "1.0.0")
    profile(box)
    ws101(box, network=True)
    fake(box, "claude", "1.1.0")
    c = ws101(box, network=True)
    assert c.status == "ok" and "changed since last seen (claude-code 1.1.0, was 1.0.0): canary passed" in c.detail
    (line,) = lines(box)
    assert line["ok"] is True and line["runners"]["claude-code"]["version"] == "1.1.0"
    assert "changed" not in ws101(box, network=True).detail  # said once


def test_a_changed_default_model_is_noticed_too(box):
    fake(box, "claude")
    profile(box, model="model-a")
    ws101(box, network=True)
    profile(box, model="model-b")
    c = ws101(box, network=True)
    assert "default model model-b, was model-a" in c.detail and "canary passed" in c.detail


def test_offline_doctor_points_to_the_command_instead_of_running_it(box):
    fake(box, "claude", "1.0.0")
    profile(box)
    ws101(box, network=False)
    fake(box, "claude", "2.0.0")
    c = ws101(box, network=False)
    assert c.status == "ok" and "changed since last seen (claude-code 2.0.0, was 1.0.0): run `ws canary`" in c.detail
    assert lines(box) == []
    # not remembered, so the note stays until a canary passes
    assert "run `ws canary`" in ws101(box, network=False).detail
    assert "run `ws canary`" in ws101(box, network=False).detail
    assert json.loads((box / ".ws" / "versions.json").read_text())["runners"]["claude-code"]["version"] == "1.0.0"


def test_ws_canary_passing_is_what_remembers_the_new_version(box, capsys, monkeypatch):
    fake(box, "claude", "1.0.0")
    profile(box)
    ws101(box, network=False)
    fake(box, "claude", "2.0.0")
    assert "run `ws canary`" in ws101(box, network=False).detail
    with monkeypatch.context() as broken:
        broken.setattr(claude_code, "generate", lambda *a, **k: {"settings.json": "{"})
        assert run(capsys, "canary")[0] == 1
    assert "run `ws canary`" in ws101(box, network=False).detail  # a failing canary remembers nothing
    assert run(capsys, "canary")[0] == 0
    versions = json.loads((box / ".ws" / "versions.json").read_text())
    assert versions["runners"]["claude-code"]["version"] == "2.0.0"
    assert "changed" not in ws101(box, network=False).detail


def test_canary_jsonl_is_private_and_versions_json_leaves_no_temp_file(box, capsys, monkeypatch):
    fake(box, "claude", "1.0.0")
    assert run(capsys, "canary", "--runner", "claude-code")[0] == 0
    assert oct((box / ".ws" / "canary.jsonl").stat().st_mode & 0o777) == "0o600"
    canary.observe(dict(os.environ), "claude-code", "3.0.0", "m")
    # the lock file is the one extra file ws.jsonl keeps beside the log; no temp file is left behind
    assert sorted(p.name for p in (box / ".ws").iterdir()) == ["canary.jsonl", "canary.jsonl.lock", "versions.json"]
    names = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (names.append(os.path.basename(a)), real(a, b))[1])
    canary.observe(dict(os.environ), "claude-code", "3.1.0", "m")
    canary.observe(dict(os.environ), "claude-code", "3.2.0", "m")
    assert len(set(names)) == 2 and all(n != "versions.json.tmp" for n in names)


def test_a_failed_write_of_versions_json_cleans_up_its_temp_file(box, monkeypatch):
    def boom(a, b):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    canary.observe(dict(os.environ), "claude-code", "1.0.0", "m")  # does not raise
    assert list((box / ".ws").iterdir()) == []


def test_a_failing_canary_fails_ws101_and_is_run_again_next_time(box, monkeypatch):
    fake(box, "claude", "1.0.0")
    profile(box)
    ws101(box, network=True)
    fake(box, "claude", "1.1.0")
    with monkeypatch.context() as broken:
        broken.setattr(claude_code, "generate", lambda *a, **k: {"settings.json": "{"})
        c = ws101(box, network=True)
        assert c.status == "fail" and "canary failed" in c.detail and "hook_config" in c.detail
        assert json.loads((box / ".ws" / "versions.json").read_text())["runners"]["claude-code"]["version"] == "1.0.0"
        assert ws101(box, network=True).status == "fail"  # still not remembered
    c = ws101(box, network=True)
    assert c.status == "ok" and "canary passed" in c.detail


def test_doctor_does_not_write_without_a_ws_home(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    exe.write_text(f'#!{sys.executable}\nprint("1.0.0")\n')
    exe.chmod(0o755)
    status, detail = doctor._runner({"adapter": "claude-code"}, osinfo.detect(), {"PATH": str(bin_dir)}, True)
    assert status == "ok" and "1.0.0" in detail
    home = tmp_path / "h"
    env = {"PATH": str(bin_dir), "HOME": str(home)}
    assert doctor._runner({"adapter": "claude-code"}, osinfo.detect(), env, True)[0] == "ok"
    assert not home.exists()
