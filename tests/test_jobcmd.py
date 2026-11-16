"""`ws job add` and `ws job remove` (ws.jobcmd), and the pin `ws run` checks (ws.jobs.check_pin)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ws import cli, jobcmd, jobs, policy

MCP = "mcp__mail__search_threads"


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "wshome"
    h.mkdir(mode=0o700)
    monkeypatch.setenv("WS_HOME", str(h))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("WS_GATED_SESSION", raising=False)
    monkeypatch.setattr(jobcmd, "_in_session", lambda env: False)
    (h / "policy.toml").write_text(f'[gate]\nallow_mcp = ["{MCP}", "mcp__mail__get_*"]\n', encoding="utf-8")
    (h / "profile.toml").write_text("[roles.budget]\nmonthly_cap_usd = 20\n", encoding="utf-8")
    (h / "secrets.toml").write_text('[vendor]\nhosts = ["api.example.com"]\npurposes = ["run"]\n', encoding="utf-8")
    return h


JOB = f"""runner = "claude-code"
model = "m"
prompt_file = "triage.md"
max_turns = 10
budget_usd = 1.0
[runner_options]
allowed_tools = ["{MCP}", "Read"]
"""


def src(tmp_path: Path, text: str = JOB, name: str = "triage", prompt: str | None = "Sort the mail.\n") -> Path:
    d = tmp_path / "src"
    d.mkdir(exist_ok=True)
    (d / f"{name}.toml").write_text(text, encoding="utf-8")
    if prompt is not None:
        (d / "triage.md").write_text(prompt, encoding="utf-8")
    return d / f"{name}.toml"


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def log_lines(home: Path) -> list[dict]:
    p = home / "logs" / jobcmd.LOG_NAME
    candidates = [p, *home.rglob(jobcmd.LOG_NAME)]
    for c in candidates:
        if c.is_file():
            return [json.loads(x) for x in c.read_text(encoding="utf-8").splitlines() if x.strip()]
    return []


# ---- add ------------------------------------------------------------------------------------------------------------


def test_add_copies_pins_and_logs(home, tmp_path, capsys):
    s = src(tmp_path)
    code, out, err = run(capsys, "job", "add", str(s))
    assert code == 0, err
    folder = jobs.jobs_dir()
    dest = folder / "triage.toml"
    assert dest.read_text(encoding="utf-8") == JOB and not dest.is_symlink()
    assert (folder / "triage.md").read_text(encoding="utf-8") == "Sort the mail.\n"
    assert stat.S_IMODE(dest.stat().st_mode) == 0o600
    pin = jobs.read_pins(folder)["triage"]
    assert pin["sha256"] == jobs.file_digest(folder, "triage")
    assert jobs.load("triage").name == "triage"
    (line,) = log_lines(home)
    assert line["action"] == "add" and line["job"] == "triage" and line["sha256"] == pin["sha256"]
    assert "Sort the mail" not in json.dumps(line)
    assert "pinned" in out


def test_a_copy_not_a_link_so_editing_the_source_changes_nothing(home, tmp_path, capsys):
    s = src(tmp_path)
    assert run(capsys, "job", "add", str(s))[0] == 0
    s.write_text(JOB.replace("max_turns = 10", "max_turns = 99"), encoding="utf-8")
    assert jobs.load("triage").max_turns == 10


@pytest.mark.parametrize("which", ["toml", "prompt"])
def test_run_refuses_a_pinned_job_that_changed(home, tmp_path, capsys, which):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0
    folder = jobs.jobs_dir()
    f = folder / ("triage.toml" if which == "toml" else "triage.md")
    f.write_text(f.read_text(encoding="utf-8") + "\n# more\n", encoding="utf-8")
    with pytest.raises(jobs.JobError, match="changed after `ws job add` pinned it"):
        jobs.load("triage")
    assert "triage" in jobs.problems()
    code, _, err = run(capsys, "run", "triage")
    assert code != 0 and "pinned" in err


def test_a_damaged_pins_file_refuses_every_job(home, tmp_path, capsys):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0
    p = jobs.jobs_dir() / jobs.PINS
    p.write_text("{not json", encoding="utf-8")
    with pytest.raises(jobs.JobError, match="every job is refused"):
        jobs.load("triage")


def test_an_unpinned_hand_written_job_still_loads(home, tmp_path):
    folder = jobs.jobs_dir()
    folder.mkdir(mode=0o700)
    (folder / "hand.toml").write_text('runner = "claude-code"\nmodel = "m"\nprompt = "hi"\n', encoding="utf-8")
    assert jobs.load("hand").prompt == "hi"


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda t: t.replace('"Read"', '"Write"'), "Write can write files or run commands"),
        (lambda t: t.replace('"Read"', '"Edit"'), "Edit can write files"),
        (lambda t: t.replace('"Read"', '"Bash"'), "Bash can write files"),
        (lambda t: t.replace('"Read"', '"Bash(git status)"'), "Bash(git status) can write"),
        (lambda t: t.replace('"Read"', '"MultiEdit"'), "MultiEdit can write"),
        (
            lambda t: t.replace('"Read"', '"mcp__mail__send_message"'),
            "mcp__mail__send_message is not in [gate] allow_mcp",
        ),
        (lambda t: t + 'env = { FOO = "1" }\n', "runner_options.env"),
        (lambda t: t + 'colour = "red"\n', "runner_options.colour is not an option of claude-code"),
        (lambda t: t + 'exe = "/tmp/other"\n', "runner_options.exe"),
        (
            lambda t: t + '\n[[secrets]]\nname = "nope"\nhost = "api.example.com"\n',
            "'nope' is not in the secrets registry",
        ),
        (
            lambda t: t + '\n[[secrets]]\nname = "vendor"\nhost = "evil.example.com"\n',
            "may not be used at evil.example.com",
        ),
        (
            lambda t: t + '\n[[secrets]]\nname = "vendor"\nhost = "api.example.com"\npurpose = "x"\n',
            "may not be used for 'x'",
        ),
        (lambda t: t.replace("budget_usd = 1.0", "budget_usd = 50.0"), "above the profile's monthly_cap_usd 20"),
    ],
)
def test_add_refuses(home, tmp_path, capsys, change, why):
    code, _, err = run(capsys, "job", "add", str(src(tmp_path, change(JOB))))
    assert code == 3, err
    assert why in err
    assert not (jobs.jobs_dir() / "triage.toml").exists()
    assert log_lines(home) == []


def test_add_refuses_a_command_outside_the_allowed_list(home, tmp_path, capsys):
    code, _, err = run(
        capsys, "job", "add", str(src(tmp_path, 'command = "rm-rf"\nschedule = "daily 03:00"\n', "nightly", None))
    )
    assert code == 2 and "command 'rm-rf' is not known" in err


def test_add_accepts_an_allowed_command_job(home, tmp_path, capsys):
    s = src(tmp_path, 'command = "backup"\nschedule = "daily 03:00"\n', "nightly", None)
    code, _out, err = run(capsys, "job", "add", str(s), "--no-apply")
    assert code == 0, err
    assert jobs.read_pins(jobs.jobs_dir())["nightly"]["files"] == ["nightly.toml"]


def test_add_refuses_with_no_monthly_cap(home, tmp_path, capsys):
    (home / "profile.toml").write_text("", encoding="utf-8")
    code, _, err = run(capsys, "job", "add", str(src(tmp_path)))
    assert code == 3 and "monthly_cap_usd" in err


def test_add_refuses_a_purpose_cap_overrun(home, tmp_path, capsys):
    (home / "policy.toml").write_text(
        f'[gate]\nallow_mcp = ["{MCP}"]\n[budget.purposes]\njobs = 0.5\n', encoding="utf-8"
    )
    code, _, err = run(
        capsys,
        "job",
        "add",
        str(src(tmp_path, JOB + "")),
    )
    assert code == 0, err  # no purpose named: only the monthly cap applies
    code, _, err = run(capsys, "job", "add", str(src(tmp_path, 'purpose = "jobs"\n' + JOB)), "--replace")
    assert code == 3 and "jobs purpose's cap 0.5" in err


def test_add_refuses_a_job_that_does_not_load(home, tmp_path, capsys):
    code, _, err = run(capsys, "job", "add", str(src(tmp_path, "nonsense = 1\n" + JOB)))
    assert code == 2 and "nonsense" in err


def test_add_refuses_a_prompt_file_outside_its_folder(home, tmp_path, capsys):
    code, _, err = run(capsys, "job", "add", str(src(tmp_path, JOB.replace('"triage.md"', '"../x.md"'))))
    assert code == 2 and "prompt_file" in err


def test_the_shipped_mcp_list_applies_when_the_policy_names_none(home, tmp_path, capsys):
    (home / "policy.toml").write_text("[gate]\n", encoding="utf-8")
    code, _, err = run(capsys, "job", "add", str(src(tmp_path)))
    assert code == 3 and MCP in err
    assert policy.MCP_SHIPPED  # the list it was checked against


# ---- replace --------------------------------------------------------------------------------------------------------


def test_replace_needs_the_flag_and_shows_a_diff(home, tmp_path, capsys):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0
    s = src(tmp_path, JOB.replace("max_turns = 10", "max_turns = 12"))
    code, out, err = run(capsys, "job", "add", str(s))
    assert code == 1 and "--replace" in err
    assert "-max_turns = 10" in out and "+max_turns = 12" in out
    assert jobs.load("triage").max_turns == 10
    code, out, err = run(capsys, "job", "add", str(s), "--replace")
    assert code == 0, err
    assert "+max_turns = 12" in out
    assert jobs.load("triage").max_turns == 12
    assert [x["action"] for x in log_lines(home)] == ["add", "replace"]


def test_replace_with_the_same_files_is_zero_changes(home, tmp_path, capsys):
    s = src(tmp_path)
    assert run(capsys, "job", "add", str(s))[0] == 0
    code, out, _ = run(capsys, "job", "add", str(s), "--replace")
    assert code == 0 and "0 changes" in out
    assert len(log_lines(home)) == 1


# ---- power and approval ---------------------------------------------------------------------------------------------


def P(**k):
    return jobcmd.Power(**k)


def test_widening_against_the_job_replaced():
    base = P(secrets={"a@h"}, tools={"Read"}, schedule="daily 07:00", budget=1.0)
    assert jobcmd.widening(P(secrets={"a@h"}, tools={"Read"}, schedule="daily 07:00", budget=1.0), base, []) == []
    assert jobcmd.widening(P(tools={"Read"}, budget=0.5), base, []) == []  # less power is never asked about
    got = jobcmd.widening(P(secrets={"a@h", "b@h"}, tools={"Read", "X"}, schedule="hourly", budget=2.0), base, [])
    assert got == ["secret b@h", "tool X", "schedule hourly", "budget_usd 2"]


def test_widening_of_a_new_job_against_every_installed_one():
    installed = [P(tools={"Read"}, budget=1.0), P(secrets={"a@h"}, budget=3.0)]
    assert jobcmd.widening(P(tools={"Read"}, budget=1.0), None, installed) == []
    assert jobcmd.widening(P(secrets={"a@h"}, budget=2.0), None, installed) == []
    # one job's tool and another's secret together is a combination no installed job has
    assert jobcmd.widening(P(tools={"Read"}, secrets={"a@h"}, budget=1.0), None, installed) != []
    assert jobcmd.widening(P(schedule="daily 07:00"), None, installed) == ["schedule daily 07:00"]
    assert jobcmd.widening(P(command="backup"), None, installed) == ["command backup"]


@pytest.fixture
def in_session(home, monkeypatch):
    monkeypatch.setattr(jobcmd, "_in_session", lambda env: True)


def test_in_a_session_a_wider_job_needs_approval_then_goes_through_once(home, tmp_path, capsys, in_session):
    from ws import approvals

    s = src(tmp_path)
    code, _, err = run(capsys, "job", "add", str(s))
    assert code == 3 and "ws approve" in err and f"tool {MCP}" in err
    assert not (jobs.jobs_dir() / "triage.toml").exists()
    root = approvals.approvals_dir()
    ((ident, _bound, _left),) = approvals.pending(root)
    approvals.grant(root, ident)
    code, _out, err = run(capsys, "job", "add", str(s))
    assert code == 0, err
    assert log_lines(home)[-1]["approved"] is True
    # the grant was single use: a different, wider job asks again
    code, _, err = run(
        capsys, "job", "add", str(src(tmp_path, JOB.replace("budget_usd = 1.0", "budget_usd = 2.0"))), "--replace"
    )
    assert code == 3 and "budget_usd 2" in err


def test_in_a_session_a_same_power_fix_needs_no_approval(home, tmp_path, capsys, monkeypatch):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0  # the person added it
    monkeypatch.setattr(jobcmd, "_in_session", lambda env: True)
    code, _, err = run(capsys, "job", "add", str(src(tmp_path, prompt="Sort the mail, kindly.\n")), "--replace")
    assert code == 0, err
    assert log_lines(home)[-1]["widened"] == []


def test_an_approval_is_bound_to_the_exact_job(home, tmp_path, capsys, in_session):
    from ws import approvals

    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 3
    root = approvals.approvals_dir()
    ((ident, _b, _l),) = approvals.pending(root)
    approvals.grant(root, ident)
    code, _, err = run(capsys, "job", "add", str(src(tmp_path, prompt="Something else.\n")))
    assert code == 3 and "ws approve" in err  # a changed prompt is another sha256, so another approval


# ---- remove ---------------------------------------------------------------------------------------------------------


def test_remove_undoes_add(home, tmp_path, capsys):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0
    code, out, err = run(capsys, "job", "remove", "triage")
    assert code == 0, err
    folder = jobs.jobs_dir()
    assert not (folder / "triage.toml").exists() and not (folder / "triage.md").exists()
    assert "triage" not in jobs.read_pins(folder)
    assert [x["action"] for x in log_lines(home)] == ["add", "remove"]
    code, out, _ = run(capsys, "job", "remove", "triage")
    assert code == 0 and "0 changes" in out


def test_add_runs_schedule_apply_for_a_scheduled_job(home, tmp_path, capsys, monkeypatch):
    calls = []
    monkeypatch.setattr(jobcmd, "_apply", lambda: (calls.append(1), ("2 unit file(s), 2 written", 0))[1])
    s = src(tmp_path, JOB + "")
    s.write_text('schedule = "daily 07:00"\n' + JOB, encoding="utf-8")
    code, out, err = run(capsys, "job", "add", str(s))
    assert code == 0, err and calls == [1]
    assert "2 written" in out
    assert run(capsys, "job", "add", str(s), "--replace", "--no-apply")[0] == 0
    assert calls == [1]


# ---- the review's cases -----------------------------------------------------------------------------------------------


def test_a_prompt_file_may_not_name_another_jobs_file(home, tmp_path, capsys):
    folder = jobs.jobs_dir()
    folder.mkdir(mode=0o700)
    victim = 'runner = "claude-code"\nmodel = "m"\nprompt = "hi"\n'
    (folder / "victim.toml").write_text(victim, encoding="utf-8")
    code, _, err = run(
        capsys, "job", "add", str(src(tmp_path, JOB.replace('"triage.md"', '"victim.toml"'), prompt=None))
    )
    assert code == 2 and "not a .toml" in err
    assert (folder / "victim.toml").read_text(encoding="utf-8") == victim
    for bad in (".pins.json", ".hidden"):
        code, _, err = run(
            capsys, "job", "add", str(src(tmp_path, JOB.replace('"triage.md"', f'"{bad}"'), prompt=None))
        )
        assert code == 2, bad


def test_a_prompt_file_may_not_be_another_jobs_prompt(home, tmp_path, capsys):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0
    other = src(tmp_path, JOB, name="second")  # names triage.md, which job triage owns
    code, _, err = run(capsys, "job", "add", str(other))
    assert code == 3 and "another job's file" in err
    assert (jobs.jobs_dir() / "triage.md").read_text(encoding="utf-8") == "Sort the mail.\n"


def test_a_damaged_pins_file_on_replace_is_a_typed_error(home, tmp_path, capsys):
    s = src(tmp_path)
    assert run(capsys, "job", "add", str(s))[0] == 0
    (jobs.jobs_dir() / jobs.PINS).write_text("{nope", encoding="utf-8")
    code, _, err = run(capsys, "job", "add", str(s), "--replace")
    assert code == 1 and "every job is refused" in err


def test_replace_repairs_a_tampered_copy(home, tmp_path, capsys):
    s = src(tmp_path)
    assert run(capsys, "job", "add", str(s))[0] == 0
    f = jobs.jobs_dir() / "triage.toml"
    f.write_text(JOB + "# changed\n", encoding="utf-8")
    code, out, err = run(capsys, "job", "add", str(s), "--replace")
    assert code == 0 and "0 changes" not in out, err
    assert jobs.load("triage").name == "triage"


def test_replace_with_a_new_prompt_name_removes_the_old_prompt(home, tmp_path, capsys):
    assert run(capsys, "job", "add", str(src(tmp_path)))[0] == 0
    s = src(tmp_path, JOB.replace('"triage.md"', '"sort.md"'), prompt=None)
    (s.parent / "sort.md").write_text("Sort.\n", encoding="utf-8")
    assert run(capsys, "job", "add", str(s), "--replace")[0] == 0
    folder = jobs.jobs_dir()
    assert not (folder / "triage.md").exists() and (folder / "sort.md").is_file()
    assert jobs.read_pins(folder)["triage"]["files"] == ["triage.toml", "sort.md"]


def test_a_purpose_change_counts_as_more_power(home):
    a = jobcmd._power({"secrets": [{"name": "v", "host": "h", "purpose": "run"}]})
    b = jobcmd._power({"secrets": [{"name": "v", "host": "h", "purpose": "review"}]})
    assert jobcmd.widening(b, a, []) != []


def test_a_link_is_refused_before_any_approval_is_used(home, tmp_path, capsys, monkeypatch):
    folder = jobs.jobs_dir()
    folder.mkdir(mode=0o700)
    (folder / "triage.toml").symlink_to(tmp_path / "elsewhere.toml")
    monkeypatch.setattr(jobcmd, "_in_session", lambda env: True)
    code, _, err = run(capsys, "job", "add", str(src(tmp_path)))
    assert code == 3 and "is a link" in err
    from ws import approvals

    assert approvals.pending(approvals.approvals_dir()) == []
