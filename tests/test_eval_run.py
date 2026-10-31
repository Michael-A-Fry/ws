"""`ws eval run`: each trial goes through `ws run` (a fake runner on PATH), leaves a run record with an `eval` field
and a line in results.jsonl, and is graded after the run with the task's hidden files written first."""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

import jsonschema
import pytest

from ws import cli, runrecord, sandbox
from ws import eval as ws_eval
from ws.profile import dumps

FIXTURES = Path(__file__).parent / "fixtures"
VALIDATOR = jsonschema.Draft202012Validator(runrecord.schema())

FAKE = f"""#!{sys.executable}
import json, os, sys
if "--version" in sys.argv:
    print("9.9.9 (Fake Code)")
    sys.exit(0)
home = os.environ["WS_HOME"]
prompt = sys.stdin.read()
mode = open(os.path.join(home, "fake-mode")).read().strip()
with open(os.path.join(home, "saw.jsonl"), "a") as f:
    f.write(json.dumps({{"cwd": os.getcwd(), "files": sorted(os.listdir("."))}}) + "\\n")
if "food category" in prompt:
    json.dump({{"food_total": 40 if mode != "wrong" else 41}}, open("answer.json", "w"))
if "fizzbuzz" in prompt and mode == "forge":
    open("fizzbuzz.py", "w").write("import os\\nos._exit(0)\\n")
elif "fizzbuzz" in prompt and mode not in ("wrong", "tamper"):
    open("fizzbuzz.py", "w").write(
        "def fizzbuzz(n):\\n"
        "    return ['FizzBuzz' if i % 15 == 0 else 'Fizz' if i % 3 == 0 else 'Buzz' if i % 5 == 0 else str(i)"
        " for i in range(1, n + 1)]\\n"
    )
if mode == "tamper":
    open("check.py", "w").write("print('I decided I passed')\\n")
if mode == "fail":
    print("boom", file=sys.stderr)
    sys.exit(2)
sys.stdout.write(open({str(FIXTURES / "claude-result-ok.json")!r}).read())
"""

JOB = """
runner = "claude-code"
model = "claude-haiku-4-5"
prompt = "the job's own prompt"
timeout_s = 30
"""


class Rig:
    def __init__(self, tmp_path: Path):
        self.home = tmp_path / "wshome"
        self.log = self.home / "logs" / "runs.jsonl"

    def mode(self, mode: str) -> None:
        (self.home / "fake-mode").write_text(mode, encoding="utf-8")

    def records(self) -> list[dict]:
        return runrecord.read(self.log)

    def results(self) -> list[dict]:
        return ws_eval.read_jsonl(ws_eval.results_path())

    def saw(self) -> list[dict]:
        return [json.loads(x) for x in (self.home / "saw.jsonl").read_text().splitlines()]


@pytest.fixture
def rig(tmp_path, monkeypatch):
    r = Rig(tmp_path)
    (r.home / "jobs").mkdir(parents=True)
    (r.home / "jobs" / "daily.toml").write_text(JOB, encoding="utf-8")
    (r.home / "profile.toml").write_text(dumps({"roles": {"budget": {"monthly_cap_usd": 20}}}), encoding="utf-8")
    exe = tmp_path / "bin" / "claude"
    exe.parent.mkdir()
    exe.write_text(FAKE, encoding="utf-8")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("WS_HOME", str(r.home))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("PATH", f"{exe.parent}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("WS_BROKER_SOCKET", raising=False)
    monkeypatch.setattr(ws_eval, "_wall", lambda argv, folder: sandbox.Wrapped(list(argv)))  # no bubblewrap needed here
    (tmp_path / "tmp").mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path / "tmp"))  # trial folders land here, not in /tmp
    r.mode("good")
    return r


def ws(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_three_trials_leave_three_records_and_three_results(rig, capsys):
    code, out, _ = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--arm", "base", "--job", "daily")
    assert code == 0 and "3 of 3 trials passed" in out
    recs = rig.records()
    assert [r["eval"] for r in recs] == [
        {"task": "starter-csv-total", "trial": n, "arm": "base", "pass": None}
        for n in (1, 2, 3)  # graded after the write
    ]
    for r in recs:
        assert not list(VALIDATOR.iter_errors(r)), r
        assert r["job"] == "daily" and r["status"] == "ok"
    results = rig.results()
    assert [(x["trial"], x["pass"], x["run_id"]) for x in results] == [
        (n, True, recs[n - 1]["run_id"]) for n in (1, 2, 3)
    ]
    assert results[0]["grade"][0]["ok"] is True and results[0]["family"] == "life-admin"
    assert results[0]["job"] == "daily" and len(results[0]["task_hash"]) == 16


def test_the_trial_runs_in_a_fresh_folder_holding_the_setup_files(rig, capsys):
    ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "2")
    saw = rig.saw()
    assert len(saw) == 2 and saw[0]["cwd"] != saw[1]["cwd"]
    for s in saw:
        assert (
            not Path(s["cwd"]).resolve().is_relative_to(rig.home.resolve())
        )  # the sandbox denies writes under WS_HOME
        assert s["files"] == ["expenses.csv"]
    assert [Path(r["cwd"]).resolve() for r in rig.records()] == [Path(s["cwd"]).resolve() for s in saw]


def test_the_job_is_the_arm_but_the_task_sets_the_prompt(rig, capsys):
    ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1")
    # the fake saw the task's prompt on stdin, not the job's: it wrote answer.json only for the task's wording
    assert rig.results()[0]["pass"] is True
    assert rig.results()[0]["arm"] == "daily"  # no --arm: the job name


def test_a_wrong_answer_fails_and_its_folder_is_kept(rig, capsys):
    rig.mode("wrong")
    code, out, _ = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1")
    assert code == 0 and "0 of 1 trials passed" in out
    (r,) = rig.results()
    assert r["pass"] is False and r["status"] == "ok" and "differs" in r["grade"][0]["detail"]
    assert rig.records()[0]["eval"]["pass"] is None  # the record comes first; the verdict is in the result line
    assert (Path(r["folder"]) / "answer.json").is_file()


def test_a_passed_trial_folder_is_removed_unless_kept(rig, capsys):
    ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1")
    assert not Path(rig.results()[0]["folder"]).exists()
    ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1", "--keep")
    assert Path(rig.results()[1]["folder"]).is_dir()


def test_a_runner_that_fails_is_a_failed_trial_not_graded(rig, capsys):
    rig.mode("fail")
    code, _, _ = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1")
    assert code == 0
    (r,) = rig.results()
    assert r["status"] == "error" and r["pass"] is False and r["grade"] == []


def test_hidden_files_arrive_after_the_run_and_cannot_be_forged(rig, capsys):
    ws(capsys, "eval", "run", "--task", "starter-fizzbuzz", "--job", "daily", "--trials", "1")
    assert "check.py" not in rig.saw()[0]["files"]  # the agent never sees the checker
    assert rig.results()[0]["pass"] is True
    rig.mode("tamper")  # the agent writes its own check.py and no solution
    ws(capsys, "eval", "run", "--task", "starter-fizzbuzz", "--job", "daily", "--trials", "1")
    assert rig.results()[1]["pass"] is False


def test_a_refused_run_stops_the_batch_and_is_recorded_as_such(rig, capsys):
    (rig.home / "profile.toml").unlink()  # no monthly cap: ws run refuses
    code, out, _ = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily")
    assert code == 3 and "stopped at a refused run" in out
    (r,) = rig.results()  # not three: it stopped at the first
    assert r["status"] == "refused" and r["pass"] is False
    assert rig.records()[0]["status"] == "refused" and rig.records()[0]["eval"]["pass"] is None


def test_the_report_reads_what_the_run_left(rig, capsys):
    ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--arm", "base")
    _, out, _ = ws(capsys, "eval", "report", "--json")
    m = json.loads(out)["data"]["metrics"]
    assert m["success_rate"]["value"] == 1.0 and m["pass_hat_k"]["value"] == 1.0
    assert m["tokens_per_solved"]["value"] == 30  # the recorded fixture: 20 in, 10 out
    assert m["cost_per_solved"]["value"] == pytest.approx(0.00021)
    assert m["harm_per_100"]["value"] == 0.0 and m["time"]["measured"] is True


def test_a_job_that_does_not_exist_is_named(rig, capsys):
    code, _, err = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "nope")
    assert code == 1 and "nope" in err and not rig.results()


def test_a_task_of_yours_is_run_from_the_tasks_folder(rig, capsys):
    folder = ws_eval.tasks_dir()
    folder.mkdir(parents=True)
    text = ws_eval.dumps_task(ws_eval.starter_tasks()[3])
    (folder / "starter-csv-total.toml").write_text(
        text.replace("starter-csv-total", "starter-csv-total"), encoding="utf-8"
    )
    code, out, _ = ws(capsys, "eval", "run", "--set", "mine", "--job", "daily", "--trials", "1")
    assert code == 0 and "1 of 1 trials passed" in out


def test_agent_code_that_exits_zero_early_fails_the_grade(rig, capsys):
    rig.mode("forge")
    ws(capsys, "eval", "run", "--task", "starter-fizzbuzz", "--job", "daily", "--trials", "1")
    (r,) = rig.results()
    assert r["status"] == "ok" and r["pass"] is False and r["grade"][0]["ok"] is False


def test_a_signal_during_grading_leaves_the_record_and_a_result(rig, capsys, monkeypatch):
    def killed(task, folder, nonce=None):
        raise SystemExit(143)

    monkeypatch.setattr(ws_eval, "grade", killed)
    with pytest.raises(SystemExit):
        cli.main(["eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1"])
    (rec,) = rig.records()  # the record was written before grading began
    assert rec["status"] == "ok" and rec["eval"]["task"] == "starter-csv-total"
    (r,) = rig.results()
    assert r["run_id"] == rec["run_id"] and r["pass"] is False and r["note"] == "grading was cut short"


def test_command_graders_are_refused_up_front_when_there_is_no_wall(rig, capsys, monkeypatch):
    def none(argv, folder):
        raise ws_eval.WallUnavailable("bubblewrap is not installed")

    monkeypatch.setattr(ws_eval, "_wall", none)
    code, _, err = ws(capsys, "eval", "run", "--task", "starter-fizzbuzz", "--job", "daily")
    assert code == 1 and "inside ws's wall" in err and "hint:" in err
    assert rig.records() == [] and rig.results() == []  # nothing started, nothing spent
    code, out, _ = ws(capsys, "eval", "run", "--task", "starter-fizzbuzz", "--dry-run")
    assert code == 0 and "would be refused" in out  # a plan says so without failing
    code, out, _ = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--job", "daily", "--trials", "1")
    assert code == 0 and "1 of 1 trials passed" in out  # file graders need no wall
