"""`ws config model` and `ws config budget`: one profile key each, shown, asked, backed up, written; and the model a
job without one resolves to is the model its run starts on, under the same provider, class and pricing checks."""

from __future__ import annotations

import hashlib
import json
import tomllib

import pytest
from test_run import ws as run_ws  # noqa: F401  (a scratch WS_HOME with a fake claude on PATH)

from ws import agentsession, cli, config_cmd
from ws.output import WsError
from ws.profile import dumps


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "wshome"
    (h / "jobs").mkdir(parents=True)
    h.chmod(0o700)
    (h / "jobs").chmod(0o700)
    monkeypatch.setenv("WS_HOME", str(h))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.delenv("WS_OUTPUT", raising=False)
    return h


def prof(home) -> dict:
    return tomllib.loads((home / "profile.toml").read_text(encoding="utf-8"))


def digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def baks(home) -> list:
    return sorted(home.glob("profile.toml.bak-*"))


def test_model_sets_the_default_and_backs_up_the_old_file(home, capsys):
    (home / "profile.toml").write_text("# mine\n[roles.budget]\nmonthly_cap_usd = 5\n", encoding="utf-8")
    assert cli.main(["config", "model", "claude-haiku-4-5", "--yes"]) == 0
    out = capsys.readouterr().out
    assert '[models] default: (unset) -> "claude-haiku-4-5"' in out and "drops comments" in out
    assert prof(home) == {"models": {"default": "claude-haiku-4-5"}, "roles": {"budget": {"monthly_cap_usd": 5}}}
    (bak,) = baks(home)
    assert bak.read_text(encoding="utf-8").startswith("# mine\n")


def test_model_with_a_purpose_sets_that_purpose_only(home):
    assert cli.main(["config", "model", "review", "ollama/gemma3:12b", "--yes"]) == 0
    assert prof(home) == {"models": {"review": "ollama/gemma3:12b"}}
    assert baks(home) == []  # there was no profile to keep


def test_dry_run_writes_nothing(home, capsys):
    p = home / "profile.toml"
    p.write_text('[models]\ndefault = "a"\n', encoding="utf-8")
    before, mtime = digest(p), p.stat().st_mtime_ns
    assert cli.main(["config", "model", "b", "--dry-run"]) == 0
    assert cli.main(["config", "budget", "30", "--dry-run"]) == 0
    out = capsys.readouterr().out
    assert '[models] default: "a" -> "b" (dry run' in out and "[roles.budget] monthly_cap_usd: (unset) -> 30" in out
    assert digest(p) == before and p.stat().st_mtime_ns == mtime and baks(home) == []


def test_setting_the_value_it_has_writes_nothing(home, capsys):
    p = home / "profile.toml"
    p.write_text('[models]\ndefault = "a"\n', encoding="utf-8")
    before = digest(p)
    assert cli.main(["config", "model", "a", "--yes"]) == 0
    assert "(no change)" in capsys.readouterr().out and digest(p) == before and baks(home) == []


def test_without_yes_and_no_terminal_nothing_is_written(home, monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli.main(["config", "budget", "30"]) == 1
    assert "not written" in capsys.readouterr().out and not (home / "profile.toml").exists()


def test_budget_writes_the_monthly_cap_keeping_a_whole_number_an_int(home):
    assert cli.main(["config", "budget", "30", "--yes"]) == 0
    assert prof(home)["roles"]["budget"]["monthly_cap_usd"] == 30
    assert cli.main(["config", "budget", "12.5", "--yes"]) == 0
    assert prof(home)["roles"]["budget"]["monthly_cap_usd"] == 12.5


@pytest.mark.parametrize("bad", ["0", "-5", "-0.01", "nan", "inf", "thirty", ""])
def test_budget_rejects_anything_but_a_positive_number(home, bad, capsys):
    assert cli.main(["config", "budget", bad, "--yes"]) == 2
    assert "positive number" in capsys.readouterr().err
    assert not (home / "profile.toml").exists()


def test_bad_model_arguments_are_usage_errors(home):
    for argv in (["config", "model", "a", "b", "c"], ["config", "model", "bad key", "m"], ["config", "budget"]):
        assert cli.main(argv) == 2
    assert cli.main(["config", "--yes"]) == 2  # setter flags need a setter
    assert not (home / "profile.toml").exists()


def test_a_profile_key_that_is_not_a_table_is_never_overwritten(home):
    (home / "profile.toml").write_text('models = "x"\n', encoding="utf-8")
    assert cli.main(["config", "model", "m", "--yes"]) == 1
    assert (home / "profile.toml").read_text(encoding="utf-8") == 'models = "x"\n'


def test_the_table_lists_each_job_its_model_and_where_it_came_from(home, capsys):
    (home / "profile.toml").write_text('[models]\ndefault = "d"\njobs = "p"\n', encoding="utf-8")

    def write(n, t):
        (home / "jobs" / f"{n}.toml").write_text(t, encoding="utf-8")

    write("a", 'runner = "claude-code"\nmodel = "own"\nprompt = "p"\n')
    write("b", 'runner = "claude-code"\npurpose = "jobs"\nprompt = "p"\n')
    write("c", 'runner = "claude-code"\nprompt = "p"\n')
    assert cli.main(["config", "model"]) == 0
    rows = [line.split() for line in capsys.readouterr().out.splitlines()]
    assert rows[0] == ["default:", "d"] and ["purpose", "jobs:", "p"] in rows
    assert ["a", "-", "own", "job"] in rows and ["b", "jobs", "p", "purpose"] in rows
    assert ["c", "-", "d", "default"] in rows
    assert cli.main(["--json", "config", "model"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["models"] == {"default": "d", "jobs": "p"}
    assert {r["job"]: r["from"] for r in data["jobs"]} == {"a": "job", "b": "purpose", "c": "default"}


def test_the_table_says_none_when_nothing_resolves(home, capsys):
    (home / "jobs" / "c.toml").write_text('runner = "claude-code"\nprompt = "p"\n', encoding="utf-8")
    assert cli.main(["config", "model"]) == 0
    out = capsys.readouterr().out
    assert "default: (unset)" in out and ["c", "-", "(none)", "none"] in [ln.split() for ln in out.splitlines()]


def test_bare_config_is_still_the_status_view(home, capsys):
    assert cli.main(["config"]) == 0
    assert "ws folder" in capsys.readouterr().out


def test_a_session_may_not_raise_its_own_cap_but_may_read_models():
    budget = cli.build_parser().parse_args(["config", "budget", "30"])
    model = cli.build_parser().parse_args(["config", "model"])
    assert agentsession.admin_name("config", budget) == "config budget"
    assert agentsession.admin_name("config", model) is None


def test_parse_usd():
    assert config_cmd.parse_usd("30") == 30 and isinstance(config_cmd.parse_usd("30.0"), int)
    with pytest.raises(WsError):
        config_cmd.parse_usd("0")


# ---- a run of a job that names no model ----------------------------------------------------------------------------

NO_MODEL_JOB = (
    'runner = "claude-code"\nprompt = "ok"\ntimeout_s = 30\n{extra}\n'
    '[[secrets]]\nname = "anthropic"\nhost = "api.anthropic.com"\n'
)


@pytest.fixture
def ws(run_ws):  # noqa: F811 (a fixture takes the other as its argument)
    return run_ws


def profile_of(ws, models: dict) -> None:
    (ws.home / "profile.toml").write_text(
        dumps({"models": models, "roles": {"budget": {"monthly_cap_usd": 20}}}), encoding="utf-8"
    )


def test_a_run_records_the_model_its_purpose_resolved_to(ws):
    (ws.home / "policy.toml").write_text("[budget.purposes]\njobs = 10\n", encoding="utf-8")
    profile_of(ws, {"default": "claude-haiku-4-5", "jobs": "claude-sonnet-4-5"})
    (ws.home / "jobs" / "daily.toml").write_text(NO_MODEL_JOB.format(extra='purpose = "jobs"'), encoding="utf-8")
    assert cli.main(["run", "daily"]) == 0
    (rec,) = ws.records()
    assert rec["model_requested"] == "claude-sonnet-4-5"
    saw = ws.saw()
    assert saw["argv"][saw["argv"].index("--model") + 1] == "claude-sonnet-4-5"


def test_a_run_with_no_model_anywhere_is_refused_with_the_exact_text(ws, capsys):
    (ws.home / "jobs" / "daily.toml").write_text(NO_MODEL_JOB.format(extra=""), encoding="utf-8")
    assert cli.main(["run", "daily"]) != 0
    want = "no model for job daily: set model in the job file, or run: ws config model ID"
    assert want in capsys.readouterr().err
    (rec,) = ws.records()
    assert rec["status"] != "ok" and want in rec["error"]
    assert not (ws.home / "fake-saw.json").exists()


def test_a_resolved_model_is_held_to_the_class_provider_list(ws, tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (ws.home / "policy.toml").write_text(
        f'[classes.work]\naccess = "walled"\nrepos = ["{work}/**"]\nallowed_providers = ["local"]\n', encoding="utf-8"
    )
    profile_of(ws, {"default": "claude-haiku-4-5"})
    (ws.home / "jobs" / "daily.toml").write_text(NO_MODEL_JOB.format(extra=f'cwd = "{work}"'), encoding="utf-8")
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert rec["stop_reason"] == "refused:egress" and rec["model_requested"] == "claude-haiku-4-5"
    assert not (ws.home / "fake-saw.json").exists()


def test_an_unpriced_resolved_model_still_needs_allow_unpriced(ws, capsys):
    profile_of(ws, {"default": "stub/m"})
    block = 'provider = { npm = "@ai-sdk/openai-compatible", models = { m = { name = "m" } } }'
    (ws.home / "jobs" / "oc.toml").write_text(
        f'runner = "opencode"\nprompt = "p"\n\n[runner_options]\n{block}\n', encoding="utf-8"
    )
    assert cli.main(["run", "oc"]) == 3
    assert "allow_unpriced" in capsys.readouterr().err
    (rec,) = ws.records()
    assert rec["status"] == "refused" and rec["model_requested"] == "stub/m"
