"""`ws eval harvest`, `approve` and `reject`: proposals come only from the person's own runs, carry `approved = false`
and the source run, never copy a prompt that looks like it holds a secret, and nothing is approved without the command."""

from __future__ import annotations

import dataclasses
import json
import os
import stat
import tomllib
from datetime import UTC, datetime, timedelta

import pytest

from ws import cli, rating, runrecord
from ws import eval as ws_eval

NOW = datetime.now(UTC)


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws" / "jobs").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def ws(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def job(box, name: str, prompt: str = "tidy the notes folder") -> None:
    text = f'runner = "claude-code"\nmodel = "claude-haiku-4-5"\nprompt = {json.dumps(prompt)}\ntimeout_s = 120\nmax_turns = 7\n'
    (box / ".ws" / "jobs" / f"{name}.toml").write_text(text, encoding="utf-8")


def run(name: str, n: int, status: str = "ok", ago: timedelta = timedelta(hours=1), **over) -> dict:
    when = NOW - ago
    rec = runrecord.new(
        run_id=f"{when:%Y%m%dT%H%M%SZ}-{n:06x}", job=name, runner="claude-code", status=status,
        started_at=runrecord.iso(when), ended_at=runrecord.iso(when), duration_ms=1, ws_version="0",
        exit_code=0 if status == "ok" else 1,
    )  # fmt: skip
    rec.update(over)
    runrecord.append(runrecord.runs_path(os.environ), rec)
    return rec


def proposals() -> list:
    folder = ws_eval.proposed_dir()
    return sorted(folder.glob("*.toml")) if folder.is_dir() else []


def test_harvest_proposes_failed_timed_out_rated_down_and_retried_runs(box, capsys):
    for name in ("failed", "slow", "disliked", "flaky", "fine", "blocked", "evaltrial"):
        job(box, name)
    failed = run("failed", 1, "error")
    slow = run("slow", 2, "timeout")
    disliked = run("disliked", 3)
    run("fine", 4)
    run("blocked", 5, "refused")
    run("evaltrial", 6, "error", eval={"task": "t", "trial": 1, "arm": "a", "pass": False})
    run("flaky", 7, "error", ago=timedelta(hours=5))
    retried = run("flaky", 8, "ok", ago=timedelta(hours=4))
    rating.append(rating.ratings_path(os.environ), rating.new(disliked["run_id"], "down"))
    code, out, _ = ws(capsys, "eval", "harvest", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["skipped"] == []
    got = {p["job"]: (p["source_run"], p["reasons"]) for p in data["proposed"]}
    assert got == {
        "failed": (failed["run_id"], ["failed"]),
        "slow": (slow["run_id"], ["timed_out"]),
        "disliked": (disliked["run_id"], ["rated_down"]),
        "flaky": (retried["run_id"], ["failed", "retried"]),  # the retry that worked, and the failure before it
    }
    assert len(proposals()) == 4


def test_a_proposal_is_unapproved_linked_to_its_run_and_cannot_run_yet(box, capsys):
    job(box, "failed", "summarise the inbox")
    rec = run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    doc = tomllib.loads(path.read_text(encoding="utf-8"))
    assert doc["approved"] is False and doc["source_run"] == rec["run_id"] and doc["source_job"] == "failed"
    assert doc["prompt"] == "summarise the inbox" and doc["reason"] == "failed"
    assert doc["timeout_s"] == 120 and doc["max_turns"] == 7 and doc["id"] == path.stem
    assert [g["type"] for g in doc["grader"]] == ["rubric"]
    assert doc["review_required"] is True and "job file failed.toml as it is now" in doc["prompt_source"]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    ws_eval.parse_task(path.read_text(encoding="utf-8"), str(path), path.stem)  # it reads back as a valid task
    # harvest alone approves nothing: your tasks folder is untouched, and the proposal is not a task you can run
    assert not ws_eval.tasks_dir().exists()
    code, _, err = ws(capsys, "eval", "run", "--task", path.stem, "--dry-run")
    assert code == 1 and "no task" in err
    code, _, err = ws(capsys, "eval", "run", "--set", "mine", "--dry-run")
    assert code == 1 and "no tasks of yours" in err


def test_one_proposal_per_job_from_its_latest_run_and_none_twice(box, capsys):
    job(box, "failed")
    run("failed", 1, "error", ago=timedelta(hours=3))
    last = run("failed", 2, "error", ago=timedelta(hours=1))
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    assert path.stem == f"harvest-{last['run_id']}"
    code, out, _ = ws(capsys, "eval", "harvest")
    assert code == 0 and "nothing new" in out and len(proposals()) == 1


def test_the_window_is_respected(box, capsys):
    job(box, "old")
    run("old", 1, "error", ago=timedelta(days=60))
    ws(capsys, "eval", "harvest", "--since", "30d")
    assert proposals() == []
    ws(capsys, "eval", "harvest", "--since", "12w")
    assert len(proposals()) == 1


def test_a_prompt_that_names_a_registered_secret_is_not_copied(box, capsys):
    (box / ".ws" / "secrets.toml").write_text(
        '[crm_login]\nhosts = ["api.example.com"]\npurposes = ["run"]\n', encoding="utf-8"
    )
    job(box, "leaky", "log in with crm_login and export the contacts")
    run("leaky", 1, "error")
    code, out, _ = ws(capsys, "eval", "harvest", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["proposed"] == [] and "crm_login" in data["skipped"][0]["reason"]
    assert proposals() == []


@pytest.mark.parametrize(
    "prompt",
    [
        "call the api with sk-" + "a1B2c3D4e5F6g7H8i9J0",
        "use ghp_" + "x" * 30,
        "header Authorization: Bearer " + "abcdef0123456789abcdef",
        "set password = " + "hunter2hunter2",
        "the key is " + "A" * 44,
        "-----BEGIN RSA PRIVATE KEY-----",
        "post to http://127.0.0.1:9/l/" + "T" * 43,
        "clone https://deploy:" + "s3cret-value" + "@git.example.invalid/repo.git",
        "the password: x",
        "key " + "Abc123/" * 5 + "Zz9+",
    ],
)
def test_a_prompt_that_looks_like_a_secret_is_not_copied(box, capsys, prompt):
    job(box, "leaky", prompt)
    run("leaky", 1, "error")
    _, out, _ = ws(capsys, "eval", "harvest", "--json")
    data = json.loads(out)["data"]
    assert data["proposed"] == [] and "not copied" in data["skipped"][0]["reason"]
    assert proposals() == []


def test_an_ordinary_prompt_is_not_mistaken_for_a_secret():
    for text in (
        "tidy the notes folder and list what changed",
        "write the weekly summary for the team in plain English",
    ):
        assert ws_eval.secret_reason(text, os.environ) is None


def test_a_registry_that_cannot_be_read_stops_the_harvest(box, capsys):
    (box / ".ws" / "secrets.toml").write_text("this is [not toml", encoding="utf-8")
    job(box, "failed")
    run("failed", 1, "error")
    code, _, err = ws(capsys, "eval", "harvest")
    assert code == 1 and "secrets registry" in err and proposals() == []


def test_a_job_that_is_gone_is_skipped_with_the_reason(box, capsys):
    run("ghost", 1, "error")
    _, out, _ = ws(capsys, "eval", "harvest", "--json")
    data = json.loads(out)["data"]
    assert data["proposed"] == [] and "job file cannot be used" in data["skipped"][0]["reason"]


def test_approve_moves_a_proposal_into_your_tasks(box, capsys):
    job(box, "failed")
    run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    code, out, _ = ws(capsys, "eval", "approve", path.stem, "--yes")
    assert code == 0 and "still a rubric" in out
    assert not path.exists()
    approved = ws_eval.tasks_dir() / path.name
    doc = tomllib.loads(approved.read_text(encoding="utf-8"))
    assert doc["approved"] is True and doc["review_required"] is False and doc["source_job"] == "failed"
    assert stat.S_IMODE(approved.stat().st_mode) == 0o600
    # it is a task now, but with only a rubric grader it still cannot run
    code, _, err = ws(capsys, "eval", "run", "--task", path.stem, "--dry-run")
    assert code == 1 and "no judge model" in err


def test_approve_will_not_overwrite_a_task(box, capsys):
    job(box, "failed")
    run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    ws_eval.tasks_dir().mkdir(parents=True)
    (ws_eval.tasks_dir() / path.name).write_text("mine", encoding="utf-8")
    code, _, err = ws(capsys, "eval", "approve", path.stem, "--yes")
    assert code == 1 and "already exists" in err and path.exists()


def test_approve_checks_the_edited_file_whole(box, capsys):
    job(box, "failed")
    run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    path.write_text(path.read_text(encoding="utf-8") + "\nsurprise = 1\n", encoding="utf-8")
    code, _, err = ws(capsys, "eval", "approve", path.stem, "--yes")
    assert code == 1 and "unknown key" in err and path.exists() and not ws_eval.tasks_dir().exists()


def test_a_person_can_edit_in_a_real_grader_and_then_run_it(box, capsys):
    job(box, "failed")
    run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    task = ws_eval.parse_task(path.read_text(encoding="utf-8"), "x", path.stem)
    fixed = ws_eval.Task(**{**task.__dict__, "graders": ({"type": "file_contains", "path": "out.txt", "text": "ok"},)})
    path.write_text(ws_eval.dumps_task(fixed), encoding="utf-8")
    assert ws(capsys, "eval", "approve", path.stem, "--yes")[0] == 0
    code, out, _ = ws(capsys, "eval", "run", "--task", path.stem, "--dry-run")
    assert code == 0 and "3 runs" in out


def test_reject_sets_it_aside_and_harvest_does_not_offer_it_again(box, capsys):
    job(box, "failed")
    run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    code, _, _ = ws(capsys, "eval", "reject", path.stem)
    assert code == 0 and not path.exists() and (ws_eval.rejected_dir() / path.name).is_file()
    assert not ws_eval.tasks_dir().exists()
    ws(capsys, "eval", "harvest")
    assert proposals() == []


@pytest.mark.parametrize("cmd", ["approve", "reject"])
def test_approve_and_reject_name_what_is_missing_and_refuse_odd_ids(box, capsys, cmd):
    code, _, err = ws(capsys, "eval", cmd, "harvest-nothing")
    assert code == 1 and "no proposal" in err
    code, _, _ = ws(capsys, "eval", cmd, "../tasks/x")
    assert code == 2


def one_proposal(box, capsys, prompt="summarise the inbox"):
    job(box, "failed", prompt)
    run("failed", 1, "error")
    ws(capsys, "eval", "harvest")
    (path,) = proposals()
    return path


def test_a_long_path_or_word_is_not_taken_for_a_base64_key():
    assert ws_eval.secret_reason("look in /usr/local/share/applications/something/else/entirely", os.environ) is None


def test_harvest_says_what_its_secret_check_does_and_does_not_cover(box, capsys):
    job(box, "failed")
    run("failed", 1, "error")
    _, out, _ = ws(capsys, "eval", "harvest", "--json")
    assert "not against secret values" in json.loads(out)["data"]["secret_check"]
    job(box, "failed2")
    run("failed2", 2, "error")
    _, _, err = ws(capsys, "eval", "harvest")
    assert "not against secret values" in err


def test_approve_shows_the_prompt_and_asks_in_a_terminal(box, capsys, monkeypatch):
    path = one_proposal(box, capsys, "summarise the inbox for Monday")
    monkeypatch.setattr("sys.stdin", type("Tty", (), {"isatty": lambda self: True})())
    asked = []
    monkeypatch.setattr("builtins.input", lambda q: asked.append(q) or "n")
    code, out, _ = ws(capsys, "eval", "approve", path.stem)
    assert code == 1 and "summarise the inbox for Monday" in out and "not approved" in out and path.exists()
    assert asked and not ws_eval.tasks_dir().exists()
    monkeypatch.setattr("builtins.input", lambda q: "y")
    code, out, _ = ws(capsys, "eval", "approve", path.stem)
    assert code == 0 and not path.exists() and (ws_eval.tasks_dir() / path.name).is_file()


def test_approve_without_a_terminal_or_yes_is_refused(box, capsys):
    path = one_proposal(box, capsys)
    code, _, err = ws(capsys, "eval", "approve", path.stem)  # stdin is not a terminal under pytest
    assert code == 1 and "confirmation" in err and "--yes" in err and path.exists()
    code, _, err = ws(capsys, "eval", "approve", path.stem, "--json")
    assert code == 1 and path.exists()


def test_a_task_that_still_needs_review_does_not_run(box, capsys):
    ws_eval.tasks_dir().mkdir(parents=True)
    task = next(t for t in ws_eval.starter_tasks() if t.id == "starter-csv-total")
    held = dataclasses.replace(task, review_required=True)
    (ws_eval.tasks_dir() / "starter-csv-total.toml").write_text(ws_eval.dumps_task(held), encoding="utf-8")
    code, _, err = ws(capsys, "eval", "run", "--task", "starter-csv-total", "--dry-run")
    assert code == 1 and "not approved yet" in err


def test_a_malformed_run_id_stops_the_harvest_and_writes_nothing(box, capsys):
    job(box, "good")
    job(box, "bad")
    run("good", 1, "error")
    rec = run("bad", 2, "error", ago=timedelta(minutes=5))
    log = runrecord.runs_path(os.environ)
    log.write_text(log.read_text().replace(rec["run_id"], "../../etc/passwd"), encoding="utf-8")
    code, _, err = ws(capsys, "eval", "harvest")
    assert code == 1 and "malformed run_id" in err and proposals() == []


def test_proposals_are_all_or_nothing(box, capsys, monkeypatch):
    for n, name in enumerate(("a", "b", "c"), 1):
        job(box, name)
        run(name, n, "error")
    real = ws_eval.write_private
    calls = []

    def flaky(path, text, exclusive=False):
        calls.append(path)
        if len(calls) == 3:
            raise OSError(28, "No space left on device")
        real(path, text, exclusive)

    monkeypatch.setattr(ws_eval, "write_private", flaky)
    code, _, err = ws(capsys, "eval", "harvest")
    assert code == 1 and "none were kept" in err and len(calls) == 3
    assert proposals() == []
    monkeypatch.setattr(ws_eval, "write_private", real)
    ws(capsys, "eval", "harvest")
    assert len(proposals()) == 3


def test_a_proposal_that_is_not_a_valid_task_is_a_ws_error(box, capsys, monkeypatch):
    job(box, "failed")
    run("failed", 1, "error")
    monkeypatch.setattr(ws_eval, "PLACEHOLDER_RUBRIC", "")  # an empty rubric is not a valid grader
    code, _, err = ws(capsys, "eval", "harvest")
    assert code == 1 and "not a valid task" in err and proposals() == []
