"""`ws eval`: the task format (whole file or refused), each grader, the exact statistics, the ten metrics from planted
records, and the A/B comparison with its two looks. Records are built at run time; nothing reads the real HOME."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ws import cli, evalstats, rating, runrecord, sandbox
from ws import eval as ws_eval

NOW = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)


@pytest.fixture
def passthrough_wall(monkeypatch):
    """Command graders run unwrapped in these tests, so they do not need bubblewrap; the tests marked `real_wall` below
    do, and skip without it."""
    monkeypatch.setattr(ws_eval, "_wall", lambda argv, folder: sandbox.Wrapped(list(argv)))


@pytest.fixture
def box(tmp_path, monkeypatch, passthrough_wall):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def ws(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def task_text(**over) -> str:
    base = {
        "id": "t1", "family": "coding", "kind": "capability", "prompt": "do it",
        "grader": '[[grader]]\ntype = "file_contains"\npath = "a.txt"\ntext = "x"\n',
    }  # fmt: skip
    base.update(over)
    head = "".join(f"{k} = {json.dumps(v)}\n" for k, v in base.items() if k != "grader")
    return head + base["grader"]


# ---- the task format ----------------------------------------------------------------------------------------------


def test_every_starter_task_is_valid_and_synthetic():
    tasks = ws_eval.starter_tasks()
    assert 5 <= len(tasks) <= 8 and len({t.id for t in tasks}) == len(tasks)
    assert {t.family for t in tasks} == set(ws_eval.FAMILIES) and {t.kind for t in tasks} == set(ws_eval.KINDS)
    assert all(t.trials == 3 and t.graders and not t.needs_judge() for t in tasks)


def test_a_task_survives_writing_and_reading_back():
    for t in ws_eval.starter_tasks():
        assert ws_eval.parse_task(ws_eval.dumps_task(t), "x", t.id) == t


def test_a_valid_task_has_the_defaults():
    t = ws_eval.parse_task(task_text(), "f", "t1")
    assert (t.trials, t.timeout_s, t.max_turns, t.setup_files, t.approved) == (3, 300, None, {}, None)
    assert t.graders == ({"type": "file_contains", "path": "a.txt", "text": "x"},)


@pytest.mark.parametrize(
    ("text", "needle"),
    [
        (task_text() + "surprise = 1\n", "unknown key 'surprise'"),
        (task_text(family="pets"), "family must be one of"),
        (task_text(kind="vibes"), "kind must be one of"),
        (task_text(trials=6), "trials must be"),
        (task_text(trials=0), "trials must be"),
        (task_text(trials=True), "trials must be"),
        (task_text(prompt="  "), "prompt must be a non-empty string"),
        (task_text(id="a b"), "id is letters"),
        (task_text(id="other"), "must match the file name"),
        (task_text(grader=""), "give 1 to 5 graders"),
        (task_text(grader='[[grader]]\ntype = "magic"\n'), "type must be one of"),
        (task_text(grader='[[grader]]\ntype = "file_contains"\npath = "a"\n'), "text is required"),
        (
            task_text(grader='[[grader]]\ntype = "file_contains"\npath = "a"\ntext = "x"\nextra = 1\n'),
            "unknown key 'extra'",
        ),
        (task_text(grader='[[grader]]\ntype = "file_contains"\npath = "../a"\ntext = "x"\n'), "plain relative path"),
        (task_text(grader='[[grader]]\ntype = "file_contains"\npath = "/etc/a"\ntext = "x"\n'), "plain relative path"),
        (task_text(grader='[[grader]]\ntype = "regex"\npath = "a"\npattern = "("\n'), "not a regular expression"),
        (task_text(grader='[[grader]]\ntype = "command"\ncommand = ""\n'), "command must be"),
        (task_text(grader='[[grader]]\ntype = "command"\ncommand = "ls"\ntimeout_s = 9999\n'), "timeout_s must be"),
        (task_text() + '[setup.files]\n"../x" = "y"\n', "plain relative path"),
        (task_text() + "[setup]\nfiles = {}\nmore = 1\n", "has one key"),
        (task_text() + '[setup.files]\n"a" = 3\n', "must be text"),
        ("this = is not toml [", "f:"),
    ],
)
def test_a_bad_task_file_is_refused_whole(text, needle):
    with pytest.raises(ws_eval.TaskError, match=needle):
        ws_eval.parse_task(text, "f", "t1")


def test_a_set_with_one_bad_file_loads_nothing(box):
    folder = ws_eval.tasks_dir()
    folder.mkdir(parents=True)
    (folder / "good.toml").write_text(task_text(id="good"), encoding="utf-8")
    (folder / "bad.toml").write_text(task_text(id="bad", family="pets"), encoding="utf-8")
    with pytest.raises(ws_eval.TaskError, match="family"):
        ws_eval.load_dir(folder)


def test_a_task_file_others_can_write_is_refused(box):
    folder = ws_eval.tasks_dir()
    folder.mkdir(parents=True)
    p = folder / "t1.toml"
    p.write_text(task_text(), encoding="utf-8")
    p.chmod(0o666)
    with pytest.raises(ws_eval.TaskError, match="can be written by others"):
        ws_eval.load_dir(folder)


# ---- the graders --------------------------------------------------------------------------------------------------


def graded(tmp_path, grader: dict, files: dict | None = None):
    for rel, content in (files or {}).items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(content, encoding="utf-8")
    task = ws_eval.parse_task(
        ws_eval.dumps_task(
            ws_eval.Task(
                id="g", family="coding", kind="capability", prompt="p", graders=(ws_eval._grader(grader, "g"),)
            )
        ),
        "g",
    )
    (v,) = ws_eval.grade(task, tmp_path)
    return v


ECHO = "import sys; print(sys.stdin.readline().strip())"


def test_command_grader_needs_exit_zero_and_the_nonce(tmp_path, passthrough_wall):
    echo = {"type": "command", "command": ["{python}", "-c", ECHO + "; import os; assert os.path.exists('a.txt')"]}
    assert graded(tmp_path, echo, {"a.txt": "x"}).ok  # runs in the task folder, and printed the nonce it was sent
    quiet = graded(tmp_path, {"type": "command", "command": "{python} -c 'pass'"})
    assert not quiet.ok and "did not print the nonce" in quiet.detail  # exit 0 alone is not trusted
    wrong = graded(tmp_path, {"type": "command", "command": ["{python}", "-c", "print('not it')"]})
    assert not wrong.ok
    failing = graded(tmp_path, {"type": "command", "command": "{python} -c 'raise SystemExit(3)'"})
    assert not failing.ok and "exit 3" in failing.detail


def test_command_grader_times_out_and_survives_a_missing_program(tmp_path, passthrough_wall):
    slow = {"type": "command", "command": "{python} -c 'import time; time.sleep(30)'", "timeout_s": 0.3}
    assert graded(tmp_path, slow).detail == "timed out"
    missing = graded(tmp_path, {"type": "command", "command": "no-such-program-here"})
    assert not missing.ok and "could not start" in missing.detail


def test_command_grader_does_not_pass_provider_keys_on(tmp_path, monkeypatch, passthrough_wall):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "planted-" + os.urandom(4).hex())
    probe = {
        "type": "command",
        "command": ["{python}", "-c", ECHO + "; import os; sys.exit(1 if 'ANTHROPIC_API_KEY' in os.environ else 0)"],
    }
    assert graded(tmp_path, probe).ok


def test_the_nonce_is_fresh_for_every_grade_and_never_in_argv_or_env(tmp_path, passthrough_wall):
    seen = tmp_path / "seen.txt"
    probe = (
        "import os, sys; n = sys.stdin.readline().strip(); "
        f"open({str(seen)!r}, 'a').write(n + ' ' + str(any(n in a for a in sys.argv) or any(n in v for v in os.environ.values())) + chr(10)); "
        "print(n)"
    )
    g = {"type": "command", "command": ["{python}", "-c", probe]}
    assert graded(tmp_path, g).ok and graded(tmp_path, g).ok
    first, second = seen.read_text().splitlines()
    assert first.split()[0] != second.split()[0] and first.endswith("False") and second.endswith("False")


def test_without_a_wall_a_command_grader_fails_instead_of_running_unsandboxed(tmp_path, monkeypatch):
    def none(argv, folder):
        raise ws_eval.WallUnavailable("bubblewrap is not installed")

    monkeypatch.setattr(ws_eval, "_wall", none)
    v = graded(tmp_path, {"type": "command", "command": ["{python}", "-c", ECHO]})
    assert not v.ok and "no wall" in v.detail
    with pytest.raises(ws_eval.WsError, match="inside ws's wall") as e:
        ws_eval.require_wall()
    assert e.value.kind == "unsupported" and "bubblewrap" in e.value.hint


REAL_WALL = ws_eval._wall


def wall_works() -> bool:
    try:
        w = ws_eval._wall(["true"], Path("/"))
    except ws_eval.WallUnavailable:
        return False
    for fd in w.pass_fds:
        os.close(fd)
    return True


@pytest.fixture
def real_wall(tmp_path, monkeypatch):
    """The real `_wall`, in a scratch home; skips where there is no bubblewrap or it cannot start."""
    home = tmp_path / ".wshome"
    home.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / ".userhome"))
    monkeypatch.setenv("WS_HOME", str(home))
    monkeypatch.setattr(ws_eval, "_wall", REAL_WALL)
    if not wall_works():
        pytest.skip("no sandbox (bubblewrap) here")


def run_starter(tmp_path, tid: str, files: dict) -> list:
    """Setup, the agent's files, the hidden files, then the real wall, as a trial does."""
    task = next(t for t in ws_eval.starter_tasks() if t.id == tid)
    ws_eval.write_files(tmp_path, task.setup_files)
    ws_eval.write_files(tmp_path, files)
    ws_eval.write_files(tmp_path, task.hidden_files)
    return ws_eval.grade(task, tmp_path)


def test_agent_code_that_exits_zero_early_does_not_pass_in_the_real_wall(real_wall, tmp_path):
    forged = {"fizzbuzz.py": "import os\nos._exit(0)\n"}
    assert not all(v.ok for v in run_starter(tmp_path, "starter-fizzbuzz", forged))
    honest = tmp_path / "honest"
    honest.mkdir()
    assert all(v.ok for v in run_starter(honest, "starter-fizzbuzz", SOLUTIONS["starter-fizzbuzz"]))


def test_agent_files_that_shadow_the_checkers_own_imports_do_not_get_into_it(real_wall, tmp_path):
    """The checker runs under `python -I`, so a subprocess.py planted in the folder is never imported by it."""
    files = {"subprocess.py": "import os\nos._exit(0)\n", **SOLUTIONS["starter-fizzbuzz"]}
    v = run_starter(tmp_path, "starter-fizzbuzz", files)
    assert all(x.ok for x in v), v
    other = tmp_path / "other"
    other.mkdir()
    broken = {"json.py": "import os\nos._exit(0)\n", **SOLUTIONS["starter-fizzbuzz"]}  # only the child imports it
    assert not all(x.ok for x in run_starter(other, "starter-fizzbuzz", broken))


def test_the_real_wall_leaves_the_trial_folder_read_only(real_wall, tmp_path):
    g = {"type": "command", "command": ["{python}", "-c", ECHO + "; open('planted.txt', 'w').write('x')"]}
    v = graded(tmp_path, g)
    assert not v.ok and not (tmp_path / "planted.txt").exists()


def test_file_contains(tmp_path):
    files = {"a.txt": "Hello World"}
    assert graded(tmp_path, {"type": "file_contains", "path": "a.txt", "text": "World"}, files).ok
    assert not graded(tmp_path, {"type": "file_contains", "path": "a.txt", "text": "world"}).ok
    assert graded(tmp_path, {"type": "file_contains", "path": "a.txt", "text": "world", "ignore_case": True}).ok
    assert graded(tmp_path, {"type": "file_contains", "path": "a.txt", "text": "absent", "negate": True}).ok
    assert not graded(tmp_path, {"type": "file_contains", "path": "a.txt", "text": "World", "negate": True}).ok


def test_a_missing_file_fails_the_grader_instead_of_raising(tmp_path):
    v = graded(tmp_path, {"type": "file_contains", "path": "nope.txt", "text": "x"})
    assert not v.ok and "cannot read nope.txt" in v.detail


def test_a_symlink_out_of_the_folder_is_not_read(tmp_path):
    secret = tmp_path / "outside.txt"
    secret.write_text("sentinel", encoding="utf-8")
    folder = tmp_path / "task"
    folder.mkdir()
    (folder / "a.txt").symlink_to(secret)
    v = graded(folder, {"type": "file_contains", "path": "a.txt", "text": "sentinel"})
    assert not v.ok and "leaves the task folder" in v.detail


def test_regex_is_multiline_and_can_be_negated(tmp_path):
    files = {"s.txt": "first\n14:00-15:00\nlast\n"}
    assert graded(tmp_path, {"type": "regex", "path": "s.txt", "pattern": r"^14:00-15:00$"}, files).ok
    assert not graded(tmp_path, {"type": "regex", "path": "s.txt", "pattern": r"^15:00"}).ok
    assert graded(tmp_path, {"type": "regex", "path": "s.txt", "pattern": "LAST", "ignore_case": True}).ok
    assert graded(tmp_path, {"type": "regex", "path": "s.txt", "pattern": "nothing", "negate": True}).ok


@pytest.mark.parametrize(
    ("json_path", "equals", "expect"),
    [
        ("a.b", 2, True),
        ("a.b", 3, False),
        ("c.1.d", "x", True),
        ("c.0.d", "x", False),
        ("a.b", True, False),  # 2 is not true
        ("flag", True, True),
        ("flag", 1, False),  # true is not 1
        ("a.zzz", 1, False),
        ("c.9", 1, False),
        ("price", 40, True),  # 40.0 in the file equals 40
    ],
)
def test_json_path_equals(tmp_path, json_path, equals, expect):
    doc = {"a": {"b": 2}, "c": [{"d": "y"}, {"d": "x"}], "flag": True, "price": 40.0}
    g = {"type": "json_path_equals", "path": "r.json", "json_path": json_path, "equals": equals}
    assert graded(tmp_path, g, {"r.json": json.dumps(doc)}).ok is expect


def test_json_path_equals_on_a_file_that_is_not_json(tmp_path):
    v = graded(
        tmp_path, {"type": "json_path_equals", "path": "r.json", "json_path": "a", "equals": 1}, {"r.json": "{nope"}
    )
    assert not v.ok and "not JSON" in v.detail


def test_a_rubric_task_does_not_run_without_a_judge_model(box, capsys):
    rubric = '[[grader]]\ntype = "rubric"\nrubric = "is it kind"\n'
    folder = ws_eval.tasks_dir()
    folder.mkdir(parents=True)
    (folder / "soft.toml").write_text(task_text(id="soft", grader=rubric), encoding="utf-8")
    code, _, err = ws(capsys, "eval", "run", "--task", "soft", "--dry-run")
    assert code == 1 and "no judge model" in err
    (box / ".ws" / "profile.toml").write_text('[eval]\njudge_model = "some-model"\n', encoding="utf-8")
    code, _, err = ws(capsys, "eval", "run", "--task", "soft", "--dry-run")
    assert code == 1 and "judge call is not built" in err


def test_hidden_files_cannot_be_planted_through_a_symlink(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    folder = tmp_path / "task"
    folder.mkdir()
    (folder / "sub").symlink_to(outside)
    with pytest.raises(OSError, match="leaves the task folder"):
        ws_eval.write_files(folder, {"sub/check.py": "x"})
    assert not (outside / "check.py").exists()


# ---- the statistics -----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("wins", "losses", "significant"),
    [
        (6, 0, True),
        (5, 0, False),
        (8, 1, True),
        (7, 1, False),
        (9, 1, True),
        (8, 2, False),
        (10, 2, True),
        (9, 3, False),
        (7, 0, True),
    ],
)
def test_mcnemar_thresholds(wins, losses, significant):
    p = evalstats.mcnemar_p(wins, losses)
    assert (p < 0.05) is significant
    assert p == evalstats.mcnemar_p(losses, wins)


def test_mcnemar_exact_values():
    assert evalstats.mcnemar_p(6, 0) == pytest.approx(2 / 64)
    assert evalstats.mcnemar_p(0, 0) == 1.0
    assert evalstats.mcnemar_p(3, 3) == 1.0


def test_wins_needed_matches_the_published_table():
    assert [evalstats.wins_needed(n) for n in (6, 9, 10, 12)] == [6, 8, 9, 10]
    assert evalstats.wins_needed(5) is None


def test_percentile_and_pass_hat_k():
    assert evalstats.percentile([1, 2, 3, 4, 5], 50) == 3
    assert evalstats.percentile([10, 20], 90) == pytest.approx(19)
    assert evalstats.percentile([], 50) is None
    # task one passes 3 of 3, task two 2 of 3: pass^3 = (1 + 0) / 2; pass^2 = (1 + 1/3) / 2
    assert evalstats.pass_hat_k([(3, 3), (3, 2)], 3) == pytest.approx(0.5)
    assert evalstats.pass_hat_k([(3, 3), (3, 2)], 2) == pytest.approx((1 + 1 / 3) / 2)
    assert evalstats.pass_hat_k([(2, 2)], 3) is None


# ---- the metrics from planted records -----------------------------------------------------------------------------


def run_rec(run_id, *, status="ok", tokens=None, cost=None, ms=2000, rule="ok", started=NOW) -> dict:
    rec = runrecord.new(
        run_id=run_id, job="j", runner="claude-code", status=status, started_at=runrecord.iso(started),
        duration_ms=ms, cost_usd=cost, exit_code=0 if status == "ok" else 1, ws_version="0",
        policy={"dial": "prepare", "rule_of_two": rule},
    )  # fmt: skip
    if tokens:
        rec["tokens"] = {"input": tokens[0], "output": tokens[1], "cache_creation": 0, "cache_read": 0}
    return rec


def result(run_id, task, trial, arm, passed, *, status="ok", ts=NOW, job="j", task_hash="h1") -> dict:
    return {"schema": 1, "ts": runrecord.iso(ts), "task": task, "trial": trial, "arm": arm, "run_id": run_id,
            "status": status, "pass": passed, "job": job, "task_hash": task_hash}  # fmt: skip


def rid(n: int) -> str:
    return f"20260615T1200{n:02d}Z-{n:06x}"


def test_metrics_from_planted_trials():
    runs = {
        rid(1): run_rec(rid(1), tokens=(1000, 500), cost=0.10, ms=1000),
        rid(2): run_rec(rid(2), tokens=(2000, 500), cost=0.20, ms=3000),
        rid(3): run_rec(rid(3), tokens=(500, 100), cost=0.05, ms=2000),
        rid(4): run_rec(rid(4), status="refused", rule="refused", ms=0),
    }
    results = [
        result(rid(1), "a", 1, "x", True),
        result(rid(2), "a", 2, "x", True),
        result(rid(3), "b", 1, "x", False),
        result(rid(4), "b", 2, "x", False, status="refused"),
    ]
    m = evalstats.compute(results, runs, {rid(1): "up", rid(3): "down"}, all_runs=list(runs.values()), now=NOW)
    assert m["success_rate"]["value"] == 0.5
    assert m["pass_hat_k"]["k"] == 2 and m["pass_hat_k"]["value"] == pytest.approx(0.5)  # a: 1; b: 0
    assert m["harm_per_100"]["value"] == 25.0 and m["harm_per_100"]["all_runs_events"] == 1
    assert m["tokens_per_solved"]["value"] == (1500 + 2500 + 600) / 2  # failures cost too; the refused trial has none
    assert m["tokens_per_solved"]["by_type"]["output"] == (500 + 500 + 100) / 2
    assert m["cost_per_solved"]["value"] == pytest.approx((0.10 + 0.20 + 0.05 + 1.0 * 0) / 2)  # refused spent nothing
    assert m["cost_per_solved"]["estimated"] == 1  # the refused record reports no cost
    assert m["time"]["value"]["p50_s"] == 2.0 and m["time"]["value"]["first_output_s"] is None
    assert m["quality"]["down"] == 1 and m["quality"]["up"] == 1 and m["quality"]["trials"] == 4
    assert m["attention"]["measured"] is False
    assert m["rework_14d"]["measured"] is False and m["maintenance"]["measured"] is False


def test_every_metric_is_not_measured_with_nothing():
    m = evalstats.compute([], {}, {}, now=NOW)
    assert list(m) == [k for k, _ in evalstats.METRICS] and len(m) == 10
    assert all(v["measured"] is False and v["value"] is None and v["display"] == "not measured" for v in m.values())


def test_cost_with_no_reported_cost_counts_at_the_persons_fallback():
    runs = {rid(1): run_rec(rid(1), tokens=(1_000_000, 0), cost=None)}
    m = evalstats.compute([result(rid(1), "a", 1, "x", True)], runs, {}, budget=(None, 3.0), now=NOW)
    assert m["cost_per_solved"]["value"] == pytest.approx(3.0) and m["cost_per_solved"]["estimated"] == 1


def test_per_solved_figures_are_not_measured_when_nothing_was_solved():
    runs = {rid(1): run_rec(rid(1), tokens=(10, 10), cost=0.01)}
    m = evalstats.compute([result(rid(1), "a", 1, "x", False)], runs, {}, now=NOW)
    assert not m["tokens_per_solved"]["measured"] and not m["cost_per_solved"]["measured"] and not m["time"]["measured"]
    assert m["success_rate"]["value"] == 0.0


def test_rework_counts_fixups_in_the_14_days_after_a_solved_trial():
    old = NOW - timedelta(days=30)
    results = [result(rid(1), "a", 1, "x", True, ts=old), result(rid(2), "a", 2, "x", True, ts=old),
               result(rid(3), "a", 3, "x", True, ts=NOW - timedelta(days=2))]  # fmt: skip
    seen = []

    def fake_git(repo, since, until=None, paths=None):
        if until is not None:  # the rework question; maintenance asks with no end
            seen.append((Path(repo).name, paths, until - since))
        return ["Revert the thing", "tidy"] if until is not None and len(seen) == 1 else ["tidy"]

    m = evalstats.compute(results, {}, {}, repos={"a": ("/some/repo", ["src/a.py"])}, now=NOW, git=fake_git)
    assert m["rework_14d"]["value"] == 0.5 and m["rework_14d"]["pending"] == 1  # the young trial is not judged yet
    assert seen[0] == ("repo", ["src/a.py"], timedelta(days=14))
    # and with no repo named, or one that cannot be read, it is not measured
    assert not evalstats.compute(results, {}, {}, now=NOW)["rework_14d"]["measured"]

    def broken(*a, **k):
        raise OSError("no repo")

    assert not evalstats.compute(results, {}, {}, repos={"a": ("/x", [])}, now=NOW, git=broken)["rework_14d"][
        "measured"
    ]


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_rework_and_maintenance_read_a_real_repository(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    env = {**os.environ, "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull, "HOME": str(tmp_path)}

    def commit(subject: str, when: datetime, name="f.txt") -> None:
        (repo / name).write_text(subject, encoding="utf-8")
        stamp = when.isoformat()
        who = {
            "NAME": "t",
            "EMAIL": "t" + chr(64) + "example.invalid",
            "DATE": stamp,
        }  # built here: the PII gate reads source
        e = {**env, **{f"GIT_{role}_{k}": v for role in ("AUTHOR", "COMMITTER") for k, v in who.items()}}
        subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True, env=e)
        subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", subject, "--no-gpg-sign"], check=True, env=e)

    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=env)
    trial = NOW - timedelta(days=30)
    commit("add feature", trial - timedelta(days=1))
    commit("fix: the feature broke", trial + timedelta(days=3))
    commit("fix: an unrelated file", trial + timedelta(days=4), name="other.txt")
    commit("maintenance: rotate things (20 min)", NOW - timedelta(days=2), name="m.txt")
    commit("fix: too late to count", trial + timedelta(days=20), name="late.txt")
    res = [result(rid(1), "a", 1, "x", True, ts=trial)]
    on_f = evalstats.compute(res, {}, {}, repos={"a": (str(repo), ["f.txt"])}, now=NOW, window_days=28)
    assert on_f["rework_14d"]["value"] == 1.0
    on_late = evalstats.compute(res, {}, {}, repos={"a": (str(repo), ["late.txt"])}, now=NOW, window_days=28)
    assert on_late["rework_14d"]["value"] == 0.0  # the fix-up on that file came after the 14 days
    assert on_f["maintenance"]["value"] == {"commits": 1, "minutes": 20}


# ---- comparing arms -----------------------------------------------------------------------------------------------


def plant(box, rows: list[tuple[str, int, str, bool]], runs: dict | None = None) -> None:
    """rows are (task, trial, arm, pass); a run record is planted for each if `runs` maps its position to one."""
    for i, (task, trial, arm, passed) in enumerate(rows, 1):
        run_id = rid(i)
        runrecord.append(runrecord.runs_path(os.environ), (runs or {}).get(i) or run_rec(run_id))
        ws_eval.append_jsonl(ws_eval.results_path(), result(run_id, task, trial, arm, passed, ts=datetime.now(UTC)))


def rows_for(a_wins: int, b_wins: int, both: int = 0, neither: int = 0):
    rows, n = [], 0
    for kind, count in (("a", a_wins), ("b", b_wins), ("both", both), ("neither", neither)):
        for _ in range(count):
            n += 1
            rows += [(f"t{n}", 1, "A", kind in ("a", "both")), (f"t{n}", 1, "B", kind in ("b", "both"))]
    return rows


def test_compare_names_a_winner_at_six_to_nothing(box, capsys):
    plant(box, rows_for(6, 0, both=4))
    code, out, _ = ws(capsys, "eval", "compare", "A", "B")
    assert code == 0 and "winner A" in out and "p=0.0312" in out and "look 1 of 2" in out
    assert "paired tasks 10 (from 10 trials of A, 10 of B)" in out


def test_compare_five_to_nothing_is_not_enough(box, capsys):
    plant(box, rows_for(5, 0, both=45))  # a 10-point gap, past the noise floor, but 5-0 is not significant
    code, out, _ = ws(capsys, "eval", "compare", "A", "B")
    assert code == 0 and "no winner" in out and "p=0.062" in out and "too few" in out


def test_a_gap_under_three_points_is_noise(box, capsys):
    plant(box, rows_for(2, 0, both=98))
    _, out, _ = ws(capsys, "eval", "compare", "A", "B", "--json")
    data = json.loads(out)["data"]
    assert data["gap_points"] == 2.0 and data["winner"] is None and "noise" in data["why"]


def test_quality_vetoes_a_winner_and_tokens_decide_among_equals():
    base = {"pairs": 20, "a_only": 6, "b_only": 0, "gap_points": 30.0, "p": evalstats.mcnemar_p(6, 0),
            "down_a": None, "down_b": None, "rated_a": 10, "rated_b": 10, "tokens_a": None, "tokens_b": None}  # fmt: skip
    assert evalstats.decide(base)["winner"] == "A"
    assert (
        evalstats.decide({**base, "down_a": 0.5, "down_b": 0.1})["winner"] is None
    )  # A passes more but is rated worse
    even = {**base, "a_only": 2, "b_only": 2, "gap_points": 0.0, "p": 1.0}
    assert evalstats.decide({**even, "tokens_a": 900, "tokens_b": 1000})["winner"] == "A"
    assert evalstats.decide({**even, "tokens_a": 1000, "tokens_b": 990})["winner"] is None  # under 3%: equal
    assert evalstats.decide({**even, "tokens_a": 1000})["winner"] is None  # not measured for both
    assert evalstats.decide({**even, "down_a": 0.4, "down_b": 0.0, "tokens_a": 1, "tokens_b": 1000})["winner"] == "B"
    assert evalstats.decide({**base, "pairs": 0})["winner"] is None


def test_only_two_looks_at_a_pair(box, capsys):
    plant(box, rows_for(6, 0))
    assert ws(capsys, "eval", "compare", "A", "B")[0] == 0
    assert ws(capsys, "eval", "compare", "B", "A")[0] == 0  # the same pair, either way round
    code, _, err = ws(capsys, "eval", "compare", "A", "B")
    assert code == 1 and "two looks" in err
    code, out, _ = ws(capsys, "eval", "compare", "A", "B", "--look", "1")  # a stored look can be shown again
    assert code == 0 and "stored, not re-run" in out
    code, _, err = ws(capsys, "eval", "compare", "A", "B", "--look", "3")
    assert code == 2


def test_a_look_must_be_declared_in_order(box, capsys):
    plant(box, rows_for(6, 0))
    code, _, err = ws(capsys, "eval", "compare", "A", "B", "--look", "2")
    assert code == 1 and "next look is 1" in err


def test_a_stored_look_does_not_change_when_more_trials_arrive(box, capsys):
    plant(box, rows_for(6, 0))
    _, first, _ = ws(capsys, "eval", "compare", "A", "B", "--json")
    ws_eval.append_jsonl(ws_eval.results_path(), result(rid(99), "t", 99, "A", False, ts=datetime.now(UTC)))
    ws_eval.append_jsonl(ws_eval.results_path(), result(rid(98), "t", 99, "B", True, ts=datetime.now(UTC)))
    _, again, _ = ws(capsys, "eval", "compare", "A", "B", "--look", "1", "--json")
    a, b = json.loads(first)["data"], json.loads(again)["data"]
    assert a["pairs"] == b["pairs"] == 6 and b["replay"] is True


def test_compare_with_nothing_paired_spends_no_look(box, capsys):
    plant(box, [("t", 1, "A", True), ("u", 1, "B", True)])
    code, _, err = ws(capsys, "eval", "compare", "A", "B")
    assert code == 1 and "no task that both" in err
    assert not ws_eval.looks_path().exists()


def test_compare_an_arm_with_itself_is_a_usage_error(box, capsys):
    assert ws(capsys, "eval", "compare", "A", "A")[0] == 2


def test_the_report_runs_over_planted_trials(box, capsys):
    plant(box, rows_for(2, 0, both=2, neither=1), {1: run_rec(rid(1), tokens=(100, 50), cost=0.01)})
    rating.append(rating.ratings_path(os.environ), rating.new(rid(1), "up"))
    code, out, _ = ws(capsys, "eval", "report", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["trials"] == 10 and data["arms"] == ["A", "B"] and set(data["by_arm"]) == {"A", "B"}
    assert data["metrics"]["success_rate"]["n"] == 10
    assert data["by_arm"]["A"]["success_rate"]["value"] == 0.8
    code, text, _ = ws(capsys, "eval", "report", "--arm", "B")
    assert code == 0 and "1. Success rate: 40.0% (2 of 5 trials)" in text


def test_a_bad_span_is_a_usage_error(box, capsys):
    assert ws(capsys, "eval", "report", "--since", "soon")[0] == 2
    assert ws(capsys, "eval", "harvest", "--since", "0d")[0] == 2


def test_the_dry_run_makes_no_record_and_counts_the_calls(box, capsys):
    code, out, _ = ws(capsys, "eval", "run", "--set", "starter", "--dry-run", "--json")
    data = json.loads(out)["data"]
    assert code == 0 and data["runs"] == 24 and len(data["tasks"]) == 8 and data["dry_run"] is True
    code, out, _ = ws(capsys, "eval", "run", "--set", "starter", "--dry-run", "--trials", "1")
    assert "8 runs" in out
    assert (
        not (box / ".ws" / "logs").exists()
        and not ws_eval.results_path().exists()
        and not (box / ".ws" / "work").exists()
    )
    assert ws(capsys, "eval", "run", "--set", "starter")[0] == 2  # a real run needs --job


def test_the_python_running_the_tests_is_what_python_means_in_a_grader():
    assert sys.executable


# ---- a right answer for each starter task, and the checks that harden the graders -------------------------------------

SOLUTIONS = {
    "starter-fizzbuzz": {
        "fizzbuzz.py": (
            "def fizzbuzz(n):\n"
            "    return ['FizzBuzz' if i % 15 == 0 else 'Fizz' if i % 3 == 0 else 'Buzz' if i % 5 == 0 else str(i)"
            " for i in range(1, n + 1)]\n"
        )
    },
    "starter-fix-mean": {"stats.py": "def mean(values):\n    return sum(values) / len(values) if values else 0.0\n"},
    "starter-rename-variable": {
        "config.py": 'user_name = "ada"\n',
        "app.py": 'from config import user_name\n\n\ndef greet():\n    return "hi " + user_name\n',
    },
    "starter-csv-total": {"answer.json": '{"food_total": 40}'},
    "starter-meeting-slot": {"slot.txt": "14:00-15:00\n"},
    "starter-plain-english": {"plain.md": "We will work together to change how new starters are welcomed.\n"},
    "starter-commit-message": {"msg.txt": "Add comma and bang to greeting\n\nIt reads friendlier.\n"},
    "starter-cheapest-plan": {"answer.json": '{"plan": "Beta", "monthly_total": 12}'},
}


def test_there_is_a_right_answer_for_every_starter_task():
    assert set(SOLUTIONS) == {t.id for t in ws_eval.starter_tasks()}


@pytest.mark.parametrize("tid", sorted(SOLUTIONS))
def test_each_starter_task_passes_with_a_right_answer_and_not_without(tmp_path, passthrough_wall, tid):
    right = tmp_path / "right"
    right.mkdir()
    verdicts = run_starter(right, tid, SOLUTIONS[tid])
    assert verdicts and all(v.ok for v in verdicts), verdicts
    nothing = tmp_path / "nothing"
    nothing.mkdir()
    assert not all(v.ok for v in run_starter(nothing, tid, {}))


def test_a_checker_that_does_not_print_the_nonce_cannot_pass_a_starter_task(tmp_path, passthrough_wall):
    task = next(t for t in ws_eval.starter_tasks() if t.id == "starter-fizzbuzz")
    ws_eval.write_files(tmp_path, SOLUTIONS["starter-fizzbuzz"])
    ws_eval.write_files(tmp_path, {"check.py": "pass\n"})  # exits 0 and says nothing
    (v,) = ws_eval.grade(task, tmp_path)
    assert not v.ok and "nonce" in v.detail


def test_a_file_that_is_not_a_regular_file_is_refused_before_it_is_opened(tmp_path):
    os.mkfifo(tmp_path / "answer.json")  # opening this for reading would wait for ever
    v = graded(tmp_path, {"type": "json_path_equals", "path": "answer.json", "json_path": "a", "equals": 1})
    assert not v.ok and "not a regular file" in v.detail


def test_a_grader_reads_at_most_a_megabyte(tmp_path):
    big = "x" * (ws_eval.READ_CAP + 10) + "needle"
    v = graded(tmp_path, {"type": "file_contains", "path": "big.txt", "text": "needle"}, {"big.txt": big})
    assert not v.ok  # the needle is past the cap
    assert graded(tmp_path, {"type": "regex", "path": "big.txt", "pattern": "^x+$"}).ok


def test_a_regex_pattern_has_a_length_cap():
    g = '[[grader]]\ntype = "regex"\npath = "a"\npattern = "' + "a" * (ws_eval.MAX_PATTERN + 1) + '"\n'
    with pytest.raises(ws_eval.TaskError, match="longer than"):
        ws_eval.parse_task(task_text(grader=g), "f", "t1")


def test_a_hard_link_the_agent_made_is_replaced_not_written_through(tmp_path):
    outside = tmp_path / "outside.txt"
    outside.write_text("keep me", encoding="utf-8")
    folder = tmp_path / "task"
    folder.mkdir()
    os.link(outside, folder / "check.py")
    ws_eval.write_files(folder, {"check.py": "new"})
    assert outside.read_text() == "keep me" and (folder / "check.py").read_text() == "new"
    assert (folder / "check.py").stat().st_nlink == 1


@pytest.mark.parametrize(
    "extra",
    [
        '[setup.files]\n"x" = "a"\n"x/y" = "b"\n',
        '[setup.files]\n"x" = "a"\n[hidden.files]\n"x/y" = "b"\n',
        '[setup.files]\n"a/b" = "a"\n"a/b/c/d" = "b"\n',
    ],
)
def test_a_path_that_is_also_a_folder_is_refused_when_the_task_is_read(extra):
    with pytest.raises(ws_eval.TaskError, match="both a file and a folder"):
        ws_eval.parse_task(task_text() + extra, "f", "t1")


def test_the_same_path_in_setup_and_hidden_is_fine():
    t = ws_eval.parse_task(task_text() + '[setup.files]\n"x" = "a"\n[hidden.files]\n"x" = "b"\n', "f", "t1")
    assert t.hidden_files == {"x": "b"}


# ---- comparing at task level --------------------------------------------------------------------------------------


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


def rows(arm: str, task: str, passes: list[bool], *, job="j", task_hash="h1", start=0) -> list[dict]:
    return [
        result(rid(start + i), task, i + 1, arm, p, ts=at(start + i), job=job, task_hash=task_hash)
        for i, p in enumerate(passes)
    ]


def test_trials_of_one_task_are_one_pair_not_three():
    pt = evalstats.paired_tasks(rows("A", "x", [True] * 3), rows("B", "x", [False] * 3))
    assert (pt["tasks"], pt["a_only"], pt["b_only"], pt["trials_a"], pt["trials_b"]) == (1, 1, 0, 3, 3)


@pytest.mark.parametrize(
    ("passes", "task_passed"),
    [
        ([True], True),
        ([False], False),
        ([True, False], False),
        ([True, True, False], True),
        ([True, False, False], False),
    ],
)
def test_a_task_passes_when_most_of_its_trials_do_and_a_tie_fails(passes, task_passed):
    pt = evalstats.paired_tasks(rows("A", "x", passes), rows("B", "x", [True] * 3))
    assert (pt["both"] == 1) is task_passed and (pt["b_only"] == 1) is (not task_passed)


def test_only_the_latest_result_of_a_trial_number_counts():
    a = rows("A", "x", [False, False, False]) + rows("A", "x", [True, True, True], start=10)  # run again, later
    pt = evalstats.paired_tasks(a, rows("B", "x", [False] * 3))
    assert pt["a_only"] == 1 and pt["trials_a"] == 3


def test_results_from_an_earlier_job_or_task_text_are_left_out_and_counted():
    old_job = rows("A", "x", [False] * 3, job="old", start=0)
    new_job = rows("A", "x", [True] * 3, job="new", start=10)
    old_text = rows("A", "y", [False] * 3, job="new", task_hash="h0", start=20)
    new_text = rows("A", "y", [True] * 3, job="new", task_hash="h1", start=30)
    b = rows("B", "x", [False] * 3, job="j", start=40) + rows("B", "y", [False] * 3, job="j", start=50)
    pt = evalstats.paired_tasks(old_job + new_job + old_text + new_text, b)
    assert pt["tasks"] == 2 and pt["a_only"] == 2  # only A's latest job and latest text were used
    assert pt["dropped"] == {"other_job": 3, "other_task_text": 3, "task_text_differs": 0}


def test_a_task_whose_text_differs_between_the_arms_is_not_paired():
    pt = evalstats.paired_tasks(rows("A", "x", [True], task_hash="h1"), rows("B", "x", [False], task_hash="h2"))
    assert pt["tasks"] == 0 and pt["dropped"]["task_text_differs"] == 1


def test_compare_says_how_much_it_left_out(box, capsys):
    for r in [*rows("A", "x", [False] * 3, job="old"), *rows("A", "x", [True] * 3, job="new", start=10),
              *rows("B", "x", [False] * 3, start=20)]:  # fmt: skip
        ws_eval.append_jsonl(ws_eval.results_path(), r)
    code, out, _ = ws(capsys, "eval", "compare", "A", "B")
    assert code == 0 and "3 results from an earlier job" in out and "paired tasks 1 (from 3 trials of A, 3 of B)" in out


def test_few_ratings_cannot_veto_and_the_report_says_so():
    base = {"pairs": 20, "a_only": 6, "b_only": 0, "gap_points": 30.0, "p": evalstats.mcnemar_p(6, 0),
            "down_a": 0.9, "down_b": 0.0, "rated_a": 4, "rated_b": 10, "tokens_a": None, "tokens_b": None}  # fmt: skip
    assert evalstats.decide(base)["winner"] == "A"  # 4 ratings is too few to veto the winner
    assert "too few ratings" in evalstats.quality_note(base)
    assert evalstats.decide({**base, "rated_a": 5})["winner"] is None  # 5 on each arm: the veto stands
    assert evalstats.quality_note({**base, "rated_a": 5}) is None


def test_compare_prints_the_quality_note(box, capsys):
    plant(box, rows_for(6, 0))
    _, out, _ = ws(capsys, "eval", "compare", "A", "B")
    assert "quality: too few ratings to veto" in out


def test_the_pair_of_a_look_is_stored_as_a_json_array(box, capsys):
    plant(box, rows_for(6, 0))
    ws(capsys, "eval", "compare", "B", "A")
    (look,) = ws_eval.read_jsonl(ws_eval.looks_path())
    assert look["pair"] == ["A", "B"]


def test_arm_names_with_a_bar_are_not_confused(box, capsys):
    for r in [*rows("a|b", "x", [True]), *rows("c", "x", [False], start=1), *rows("a", "x", [True], start=2),
              *rows("b|c", "x", [False], start=3)]:  # fmt: skip
        ws_eval.append_jsonl(ws_eval.results_path(), r)
    for _ in range(2):
        assert ws(capsys, "eval", "compare", "a|b", "c")[0] == 0
    assert ws(capsys, "eval", "compare", "a", "b|c")[0] == 0  # a different pair: its own two looks


# ---- the nonce cannot be read by a child, and the other round-2 hardening ----------------------------------------------

ATTACK = """
import json, os, re, sys

ppid = os.getppid()
hits = set()
try:
    mem = open(f"/proc/{ppid}/mem", "rb", 0)
    for line in open(f"/proc/{ppid}/maps").read().splitlines():
        span, perms = line.split()[:2]
        if "r" not in perms or "w" not in perms:
            continue
        lo, hi = (int(x, 16) for x in span.split("-"))
        try:
            mem.seek(lo)
            hits.update(m.decode() for m in re.findall(rb"[0-9a-f]{32}", mem.read(hi - lo)))
        except (OSError, ValueError, OverflowError):
            continue
except OSError:
    pass
print(json.dumps(sorted(hits)))
"""


def attack_task(prelude: str, expect_found: bool) -> ws_eval.Task:
    """A checker that lets a child try to read the checker's memory for the nonce. It prints the nonce (so the grade
    passes) only if the child's result is the one expected."""
    check = (
        prelude
        + f"r = subprocess.run([sys.executable, '-c', {ATTACK!r}], capture_output=True, text=True, stdin=subprocess.DEVNULL)\n"
        + f"assert (nonce in json.loads(r.stdout or '[]')) is {expect_found}, r.stdout[:200]\n"
        + "print(nonce)\n"
    )
    return ws_eval.Task(
        id="attack", family="coding", kind="capability", prompt="p", hidden_files={"check.py": check},
        graders=({"type": "command", "command": "{python} -I check.py"},),
    )  # fmt: skip


def grade_in_wall(folder, task) -> bool:
    ws_eval.write_files(folder, task.hidden_files)
    (v,) = ws_eval.grade(task, folder)
    return v.ok


@pytest.mark.skipif(not Path("/proc/self/mem").exists(), reason="needs /proc")
def test_a_child_of_the_checker_cannot_read_the_nonce_from_its_memory(real_wall, tmp_path):
    unprotected = ws_eval.CHECK_PRELUDE.replace("\nprotect()\n", "\n")
    assert unprotected != ws_eval.CHECK_PRELUDE
    control = tmp_path / "control"
    control.mkdir()
    if not grade_in_wall(control, attack_task(unprotected, expect_found=True)):
        pytest.skip("this machine does not let a child read the checker's memory anyway")
    guarded = tmp_path / "guarded"
    guarded.mkdir()
    assert grade_in_wall(guarded, attack_task(ws_eval.CHECK_PRELUDE, expect_found=False))


def test_a_checker_that_cannot_protect_itself_fails_with_that_reason(tmp_path, passthrough_wall):
    broken = ws_eval.CHECK_PRELUDE.replace("\nprotect()\n", "\nsys.exit(97)\n")
    task = attack_task(broken, expect_found=False)
    ws_eval.write_files(tmp_path, task.hidden_files)
    (v,) = ws_eval.grade(task, tmp_path)
    assert not v.ok and "memory unreadable" in v.detail and ws_eval.EXIT_UNPROTECTED == 97


def test_a_pattern_that_backtracks_for_ever_is_stopped(tmp_path, monkeypatch):
    monkeypatch.setattr(ws_eval, "REGEX_TIMEOUT_S", 0.5)
    files = {"a.txt": "a" * 60 + "b"}
    v = graded(tmp_path, {"type": "regex", "path": "a.txt", "pattern": "(a+)+$"}, files)
    assert not v.ok and "was stopped" in v.detail


def test_a_case_only_difference_in_setup_paths_is_a_collision():
    for extra in (
        '[setup.files]\n"Data" = "a"\n"data/y" = "b"\n',
        '[setup.files]\n"Data.txt" = "a"\n"data.TXT" = "b"\n',
    ):
        with pytest.raises(ws_eval.TaskError, match="folder|differ only in case"):
            ws_eval.parse_task(task_text() + extra, "f", "t1")


def test_results_from_before_an_edit_to_the_job_file_are_left_out():
    old = [{**r, "job_hash": "edit1"} for r in rows("A", "x", [False] * 3)]
    new = [{**r, "job_hash": "edit2"} for r in rows("A", "x", [True] * 3, start=10)]
    pt = evalstats.paired_tasks(old + new, rows("B", "x", [False] * 3, start=20))
    assert pt["a_only"] == 1 and pt["dropped"]["other_job"] == 3


def test_a_compare_where_the_arms_ran_different_numbers_of_trials_is_refused(box, capsys):
    for r in [*rows("A", "x", [True] * 3), *rows("B", "x", [False] * 2, start=5)]:
        ws_eval.append_jsonl(ws_eval.results_path(), r)
    code, _, err = ws(capsys, "eval", "compare", "A", "B")
    assert code == 1 and "3 trials under A and 2 under B" in err and "same --trials" in err
    assert not ws_eval.looks_path().exists()  # a refusal does not use up a look
