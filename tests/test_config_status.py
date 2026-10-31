"""`ws config` and `ws status`, and the rule behind them: a bad job is listed and the others run; a bad policy or secrets
registry is never half-loaded."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ws import cli, config_cmd, jobs, policy, profile

GOOD_JOB = 'runner = "claude-code"\nmodel = "m"\nprompt = "hi"\n'


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.chdir(home)
    return home


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def put(path: Path, text: str, mode: int = 0o600) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    return path


def files(capsys) -> dict[str, dict]:
    _, out, _ = run(capsys, "config", "--json")
    return {f["name"]: f for f in json.loads(out)["data"]["files"]}


# ---- ws config ---------------------------------------------------------------------------------------------------------


def test_config_lists_every_file_ws_reads_and_the_keys_it_sets(box, capsys):
    put(box / ".ws" / "profile.toml", '[machine]\nos = "linux"\n[roles.notes]\npath = "/x"\n')
    put(box / ".ws" / "secrets.toml", '[anthropic]\nhosts = ["api.anthropic.com"]\nheader = "x-api-key"\n')
    put(box / ".ws" / "jobs" / "daily.toml", GOOD_JOB)
    by = files(capsys)
    assert set(by) == {"profile", "policy", "secrets", "job:daily"}
    assert by["profile"]["keys"] == ["machine.os", "roles.notes.path"] and by["profile"]["status"] == "ok"
    assert by["policy"]["status"] == "missing" and by["policy"]["path"].endswith("policy.toml")
    assert by["secrets"]["keys"] == ["anthropic.header", "anthropic.hosts"]
    assert by["job:daily"]["keys"] == ["model", "prompt", "runner"] and by["job:daily"]["status"] == "ok"
    assert all(f["origin"] == "$WS_HOME" for f in by.values())


def test_config_origin_says_when_the_home_is_the_default(box, capsys, monkeypatch):
    monkeypatch.delenv("WS_HOME")
    by = files(capsys)
    assert by["profile"]["origin"].startswith("default") and by["profile"]["path"] == str(box / ".ws" / "profile.toml")


def test_config_never_shows_a_value(box, capsys):
    put(box / ".ws" / "profile.toml", '[machine]\nserver = "s3cret-looking-value"\n')
    _, out, _ = run(capsys, "config")
    assert "s3cret-looking-value" not in out and "machine.server" in out


def test_config_reports_an_invalid_policy_and_exits_1(box, capsys):
    put(box / ".ws" / "policy.toml", 'dial = "reckless"\n')
    code, out, _ = run(capsys, "config", "--json")
    env = json.loads(out)
    pol = {f["name"]: f for f in env["data"]["files"]}["policy"]
    assert code == 1 and env["status"] == "attention" and pol["status"] == "invalid" and "dial" in pol["error"]


def test_a_bad_job_is_listed_and_the_good_one_is_unaffected(box, capsys):
    put(box / ".ws" / "jobs" / "good.toml", GOOD_JOB)
    put(box / ".ws" / "jobs" / "bad.toml", 'runner = "claude-code"\nmodel = "m"\n')  # no prompt
    os.chmod(box / ".ws" / "jobs", 0o700)
    by = files(capsys)
    assert by["job:good"]["status"] == "ok" and by["job:bad"]["status"] == "invalid"
    assert "prompt" in by["job:bad"]["error"]
    assert list(jobs.problems(os.environ)) == ["bad"]
    assert jobs.load("good").model == "m"  # `ws run good` still loads
    _, out, _ = run(capsys, "doctor", "--offline", "--json")
    ws006 = {c["id"]: c for c in json.loads(out)["data"]["checks"]}["WS006"]
    assert ws006["status"] == "fail" and "bad" in ws006["detail"] and "good" not in ws006["detail"].split(":")[0]


def test_doctor_job_check_is_ok_with_no_jobs_or_good_ones(box, capsys):
    _, out, _ = run(capsys, "doctor", "--offline", "--json")
    assert {c["id"]: c for c in json.loads(out)["data"]["checks"]}["WS006"]["status"] == "ok"
    put(box / ".ws" / "jobs" / "good.toml", GOOD_JOB)
    os.chmod(box / ".ws" / "jobs", 0o700)
    _, out, _ = run(capsys, "doctor", "--offline", "--json")
    assert {c["id"]: c for c in json.loads(out)["data"]["checks"]}["WS006"]["status"] == "ok"


def test_a_schedule_only_job_is_not_a_problem(box):
    put(box / ".ws" / "jobs" / "backup.toml", 'command = "backup"\nschedule = "daily 03:30"\n')
    os.chmod(box / ".ws" / "jobs", 0o700)
    assert jobs.problems(os.environ) == {}


def test_policy_and_secrets_fail_closed_not_partially(box):
    put(box / ".ws" / "policy.toml", 'dial = "prepare"\nbogus = 1\n')
    with pytest.raises(policy.PolicyError):
        policy.load()
    put(box / ".ws" / "secrets.toml", '[good]\nhosts = ["a.example.com"]\nheader = "x"\n[1bad]\nhosts = []\n')
    from ws import secrets

    with pytest.raises(secrets.SecretsError):
        secrets.load_registry(secrets.registry_path())


def test_deprecated_keys_warn_with_their_replacement(box, capsys, monkeypatch):
    monkeypatch.setattr(profile, "DEPRECATED", {"machine.always_on": "machine.stays_on"})
    put(box / ".ws" / "profile.toml", "[machine]\nalways_on = true\n")
    monkeypatch.setattr(config_cmd.profile, "DEPRECATED", profile.DEPRECATED)
    _, out, _ = run(capsys, "config", "--json")
    data = json.loads(out)["data"]
    assert {"key": "machine.always_on", "replacement": "machine.stays_on"} in data["files"][0]["deprecated"]
    assert any("`machine.always_on` is deprecated; use `machine.stays_on`" in w for w in data["warnings"])


def test_the_deprecated_tables_exist_and_are_dicts():
    assert isinstance(profile.DEPRECATED, dict) and isinstance(policy.DEPRECATED, dict)
    assert profile.deprecated_in({"a": {"b": 1}}, {"a.b": "a.c"}) == [("a.b", "a.c")]
    assert profile.deprecated_in({"a": {"b": 1}}, {"x": "y"}) == []


# ---- ws status ---------------------------------------------------------------------------------------------------------


def status(capsys, *extra):
    code, out, _ = run(capsys, "status", "--json", *extra)
    assert code == 0
    return json.loads(out)["data"]


def test_status_lists_global_and_repo_files_with_origin_and_size(box, capsys):
    claude = box / ".claude"
    put(claude / "CLAUDE.md", "global rules\n")
    put(claude / "settings.json", json.dumps({"hooks": {"PreToolUse": []}, "mcpServers": {"ws": {}}}))
    put(claude / "skills" / "align" / "SKILL.md", "x" * 100)
    repo = box / "repo"
    (repo / ".git").mkdir(parents=True)
    put(repo / "CLAUDE.md", "repo rules\n" * 3)
    put(repo / ".mcp.json", "{}")
    put(repo / ".claude" / "skills" / "mine" / "SKILL.md", "y" * 80)
    data = status(capsys, "-C", str(repo))
    rows = {(r["origin"], r["kind"], r["name"]): r for r in data["runners"]["claude-code"]}
    assert rows["global", "rules", "CLAUDE.md"]["bytes"] == len("global rules\n")
    assert rows["global", "settings", "settings.json"]["hooks"] == ["PreToolUse"]
    assert rows["global", "settings", "settings.json"]["mcp_servers"] == ["ws"]
    assert rows["global", "skills", "align"]["bytes"] == 100
    assert rows["repo", "rules", "CLAUDE.md"]["bytes"] == len("repo rules\n") * 3
    assert ("repo", "mcp", ".mcp.json") in rows and ("repo", "skills", "mine") in rows
    assert data["total_bytes"] == sum(r["bytes"] for rs in data["runners"].values() for r in rs)
    assert data["repo"] == str(repo.resolve())


def test_status_marks_a_repo_copy_that_overrides_a_global(box, capsys):
    put(box / ".claude" / "skills" / "align" / "SKILL.md", "g" * 100)
    repo = box / "repo"
    (repo / ".git").mkdir(parents=True)
    put(repo / ".claude" / "skills" / "align" / "SKILL.md", "r" * 100 + "different")
    put(
        repo / ".ws" / "overrides.toml", '[[override]]\npath = ".claude/skills/align"\nreason = "tuned for this repo"\n'
    )
    rows = status(capsys, "-C", str(repo))["runners"]["claude-code"]
    mine = [r for r in rows if r["path"].startswith(str(repo))]
    assert [(r["origin"], r["reason"]) for r in mine] == [("override", "tuned for this repo")]


def test_status_lists_opencode_rules_and_text_mode_works(box, capsys):
    put(box / ".config" / "opencode" / "AGENTS.md", "agents\n")
    code, out, _ = run(capsys, "status")
    assert code == 0 and "opencode:" in out and "AGENTS.md" in out and "global" in out


def test_status_with_nothing_installed_is_empty_not_an_error(box, capsys):
    data = status(capsys)
    assert data["total_bytes"] == 0 and all(rows == [] for rows in data["runners"].values())
