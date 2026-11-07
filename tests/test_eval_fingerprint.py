"""The arm fingerprint and the compare line, from recorded result files: one arm made one way, made two ways, run in
bare mode, and made before fingerprints existed. Nothing runs a model; HOME and WS_HOME are in tmp_path."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from ws import cli, evalstats, jobs, policy
from ws import eval as ws_eval

FIXTURES = Path(__file__).parent / "fixtures" / "eval"


@pytest.fixture
def box(tmp_path, monkeypatch):
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


def load(name: str) -> None:
    """Put a recorded results file where `ws eval` reads it."""
    dest = ws_eval.results_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(FIXTURES / name, dest)


def test_the_fixtures_are_what_the_tests_below_say_they_are():
    for name in ("results-same.jsonl", "results-mixed.jsonl", "results-bare.jsonl", "results-legacy.jsonl"):
        rows = [json.loads(x) for x in (FIXTURES / name).read_text().splitlines()]
        assert len(rows) == 16 and {r["arm"] for r in rows} == {"base", "tight"}
    legacy = [json.loads(x) for x in (FIXTURES / "results-legacy.jsonl").read_text().splitlines()]
    assert all("fingerprint" not in r for r in legacy)


# ---- compare: one way of running ----------------------------------------------------------------------------------


def test_compare_of_arms_each_made_one_way_prints_the_plain_line(box, capsys):
    load("results-same.jsonl")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert code == 0
    assert (
        "  base leads tight by 75.0 points on 8 paired tasks (95% interval +"  # 6 of 8 tasks split, all to base
    ) in out
    assert "the difference is real at 95%." in out
    assert "A passes 100.0% (95% interval 67.6% to 100.0%), B passes 25.0% (95% interval 7.1% to 59.1%)" in out
    assert "mixed results" not in out and "bare mode" not in out and "no fingerprint" not in out


def test_the_old_three_point_band_is_gone_from_the_line_and_the_reason(box, capsys):
    load("results-same.jsonl")
    _, out, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert "noise" not in out and "under 3" not in out


def test_the_line_says_too_few_runs_when_the_test_cannot_tell(box, capsys):
    rows = [json.loads(x) for x in (FIXTURES / "results-same.jsonl").read_text().splitlines()]
    keep = [r for r in rows if r["task"] in ("task-1", "task-2", "task-7", "task-8")]  # base 4 of 4, tight 2 of 4
    ws_eval.results_path().parent.mkdir(parents=True)
    ws_eval.results_path().write_text("".join(json.dumps(r) + "\n" for r in keep), encoding="utf-8")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert code == 0 and "too few runs to tell (MDE: 4 tasks cannot show a gap" in out
    assert "is real at 95%" not in out


def test_the_line_reads_the_same_with_the_arms_the_other_way_round(box, capsys):
    load("results-same.jsonl")
    _, out, _ = ws(capsys, "eval", "compare", "tight", "base")
    line = next(x for x in out.splitlines() if " leads " in x)
    assert line.count("base") == 1 and line.count("tight") == 1
    assert line.strip().startswith("base leads tight by 75.0 points")


def test_the_json_carries_the_new_fields(box, capsys):
    load("results-same.jsonl")
    _, out, _ = ws(capsys, "eval", "compare", "base", "tight", "--json")
    d = json.loads(out)["data"]
    assert d["real"] is True and d["gap_points"] == 75.0
    assert len(d["interval_points"]) == 2 and d["interval_points"][0] > 0 and d["interval_points"][1] == 75.0
    assert d["mde_points"] == pytest.approx(75.0)  # 6 split tasks in 8: (12 - 6) / 8
    assert d["statement"].startswith("base leads tight by 75.0 points")
    assert len(d["interval_a"]) == 2 and len(d["interval_b"]) == 2
    assert set(d["pass_at_k"]) == {"base", "tight"} and d["pass_at_k"]["base"] is None  # one trial per task
    fp = d["fingerprint"]
    assert fp["allow_mixed"] is False and fp["mixed"] == {}
    assert fp["arms"]["base"]["with_fingerprint"] == 8 and fp["arms"]["base"]["bare"] is False


# ---- compare: one arm made two ways -------------------------------------------------------------------------------


def test_compare_refuses_an_arm_whose_results_were_made_two_ways(box, capsys):
    load("results-mixed.jsonl")
    code, out, err = ws(capsys, "eval", "compare", "base", "tight")
    assert code == 1 and out == ""
    assert "base: models used, ws version" in err and "tight" not in err.split("differ")[0].split("base:")[1]
    assert "--allow-mixed" in err
    assert not ws_eval.looks_path().exists()  # a refusal spends no look


def test_the_refusal_is_a_typed_error_naming_the_fields(box):
    load("results-mixed.jsonl")
    results = ws_eval.read_jsonl(ws_eval.results_path())
    with pytest.raises(ws_eval.WsError) as caught:
        ws_eval.compare_arms("base", "tight", results, {}, {})
    e = caught.value
    assert e.kind == "refused" and e.exit_code == 1
    assert list(e.details["mixed"]) == ["base"] and set(e.details["mixed"]["base"]) == {"models", "ws_version"}


def test_the_refusal_comes_as_json_too(box, capsys):
    load("results-mixed.jsonl")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight", "--json")
    body = json.loads(out)
    assert code == 1 and body["status"] == "error" and body["error_kind"] == "refused"
    assert set(body["details"]["mixed"]["base"]) == {"models", "ws_version"}


def test_allow_mixed_pools_and_says_so(box, capsys):
    load("results-mixed.jsonl")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight", "--allow-mixed")
    assert code == 0
    assert "mixed results pooled on purpose (--allow-mixed): base differs in models used, ws version" in out
    assert "base leads tight by" in out


def test_allow_mixed_is_in_the_json_and_in_a_stored_look(box, capsys):
    load("results-mixed.jsonl")
    _, out, _ = ws(capsys, "eval", "compare", "base", "tight", "--allow-mixed", "--json")
    fp = json.loads(out)["data"]["fingerprint"]
    assert fp["allow_mixed"] is True and set(fp["mixed"]["base"]) == {"models", "ws_version"}
    _, again, _ = ws(capsys, "eval", "compare", "base", "tight", "--look", "1")  # the stored look says it too
    assert "mixed results pooled on purpose" in again and "stored, not re-run" in again


def test_a_difference_between_the_arms_is_not_a_mix(box, capsys):
    load("results-same.jsonl")  # base and tight use different models; each is one way inside
    code, _, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert code == 0


def test_a_mix_among_results_the_pairing_leaves_out_does_not_refuse(box, capsys):
    rows = [json.loads(x) for x in (FIXTURES / "results-same.jsonl").read_text().splitlines()]
    old = {
        **rows[0],
        "ts": "2026-06-01T12:00:00.000Z",
        "job_hash": "old",
        "fingerprint": {**rows[0]["fingerprint"], "ws_version": "0.0.1"},
    }
    ws_eval.results_path().parent.mkdir(parents=True)
    ws_eval.results_path().write_text("".join(json.dumps(r) + "\n" for r in [old, *rows]), encoding="utf-8")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert code == 0 and "1 results from an earlier job" in out


# ---- compare: bare mode and older results -------------------------------------------------------------------------


def test_an_arm_run_in_bare_mode_is_flagged(box, capsys):
    load("results-bare.jsonl")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert code == 0
    assert "tight ran in bare mode: the runner skipped hooks, so the gate's hook did not run" in out
    assert "base ran in bare mode" not in out


def test_bare_mode_is_in_the_json(box, capsys):
    load("results-bare.jsonl")
    _, out, _ = ws(capsys, "eval", "compare", "base", "tight", "--json")
    arms = json.loads(out)["data"]["fingerprint"]["arms"]
    assert arms["tight"]["bare"] is True and arms["base"]["bare"] is False


def test_results_from_before_fingerprints_compare_and_are_counted(box, capsys):
    load("results-legacy.jsonl")
    code, out, _ = ws(capsys, "eval", "compare", "base", "tight")
    assert (
        code == 0 and "base: 8 results carry no fingerprint" in out and "tight: 8 results carry no fingerprint" in out
    )
    assert "base leads tight by" in out


def test_a_look_stored_before_the_interval_still_shows(box, capsys):
    old = {"arm_a": "a", "arm_b": "b", "pairs": 6, "tasks": 6, "trials_a": 6, "trials_b": 6, "both_pass": 0,
           "a_only": 6, "b_only": 0, "both_fail": 0, "dropped": {"other_job": 0, "other_task_text": 0, "task_text_differs": 0},
           "gap_points": 100.0, "p": 0.03125, "pass_rate_a": 1.0, "pass_rate_b": 0.0, "down_a": None, "down_b": None,
           "tokens_a": None, "tokens_b": None, "winner": "a", "why": "a passes more", "quality": None}  # fmt: skip
    ws_eval.append_jsonl(
        ws_eval.looks_path(),
        {"schema": 1, "ts": "2026-06-15T12:00:00.000Z", "pair": ["a", "b"], "look": 1, "result": old},
    )
    code, out, _ = ws(capsys, "eval", "compare", "a", "b", "--look", "1")
    assert code == 0 and "winner a: a passes more" in out and "stored, not re-run" in out


# ---- report -------------------------------------------------------------------------------------------------------


def test_the_report_lists_each_arms_spread_and_bare_mode(box, capsys):
    shutil.copy(FIXTURES / "results-mixed.jsonl", _results_here())
    _, out, _ = ws(capsys, "eval", "report", "--since", "9999d", "--json")
    prints = json.loads(out)["data"]["fingerprints"]
    assert set(prints["base"]["mixed"]) == {"models", "ws_version"} and prints["tight"]["mixed"] == {}
    _, text, _ = ws(capsys, "eval", "report", "--since", "9999d")
    assert "arm base: its results were made different ways (models used, ws version differ)" in text
    bare = _results_here()
    shutil.copy(FIXTURES / "results-bare.jsonl", bare)
    _, text, _ = ws(capsys, "eval", "report", "--since", "9999d")
    assert "arm tight ran in bare mode" in text and "arm base ran in bare mode" not in text


def _results_here() -> Path:
    dest = ws_eval.results_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


# ---- the fingerprint a trial writes -------------------------------------------------------------------------------


def make_job(**over) -> jobs.Job:
    base = {
        "name": "daily",
        "runner": "claude-code",
        "model": "m-asked",
        "prompt": "p",
        "timeout_s": 30.0,
        "max_turns": 7,
    }
    return jobs.Job(**{**base, **over})


def test_a_fingerprint_records_what_actually_ran(box):
    rec = {"models_used": [{"model": "m-used-b"}, {"model": "m-used-a"}, {"model": "m-used-a"}], "runner_version": "9.9.9",
           "ws_version": "1.2.3"}  # fmt: skip
    fp = ws_eval.fingerprint(make_job(), rec)
    assert fp["models"] == ["m-used-a", "m-used-b"]  # what the runner reported, not "m-asked"
    assert (fp["runner"], fp["runner_version"], fp["ws_version"]) == ("claude-code", "9.9.9", "1.2.3")
    assert fp["resources"] == {"timeout_s": 30.0, "max_turns": 7} and fp["bare"] is True
    assert len(fp["job_hash"]) == 16 and len(fp["policy_hash"]) == 16 and len(fp["guards_hash"]) == 16
    assert set(fp) == set(evalstats.FINGERPRINT_FIELDS)


def test_a_trial_with_no_record_has_empty_fields_not_a_guess(box):
    fp = ws_eval.fingerprint(make_job(bare=False), None)
    assert fp["models"] == [] and fp["runner_version"] is None and fp["ws_version"] is None and fp["bare"] is False


def test_the_policy_and_the_guards_each_change_their_own_hash(box):
    first = ws_eval.fingerprint(make_job(), None)
    path = policy.policy_path()
    text = policy.default_toml()
    path.write_text(
        text.replace('on = ["syntax-check", "session-context", "dev-server"]', 'on = ["syntax-check"]'),
        encoding="utf-8",
    )
    fewer_guards = ws_eval.fingerprint(make_job(), None)
    assert fewer_guards["guards_hash"] != first["guards_hash"] and fewer_guards["policy_hash"] != first["policy_hash"]
    path.write_text(text, encoding="utf-8")
    assert ws_eval.fingerprint(make_job(), None)["guards_hash"] == first["guards_hash"]  # guards back as they were


def test_a_policy_that_cannot_be_read_hashes_to_null(box):
    policy.policy_path().write_text("this is not toml [", encoding="utf-8")
    fp = ws_eval.fingerprint(make_job(), None)
    assert fp["policy_hash"] is None and fp["guards_hash"] is None
