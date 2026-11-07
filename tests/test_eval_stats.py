"""The statistics behind `ws eval`, each against hand-worked values or a published worked example: Wilson and exact
intervals, the paired gap and its interval, pass@k beside pass^k, the minimum detectable effect, the sentence that
says who leads, and the fingerprint spread. Pure functions over planted dicts; no model, no network, no HOME."""

from __future__ import annotations

import pytest

from ws import evalstats


def trial(task: str, passed: bool, arm: str = "x") -> dict:
    return {"task": task, "pass": passed, "arm": arm}


# ---- intervals ----------------------------------------------------------------------------------------------------


def test_wilson_matches_the_published_worked_example():
    # Newcombe (1998), "Two-sided confidence intervals for the single proportion", Stat Med 17:857-872: 81 of 263 gives
    # a 95% Wilson interval of 0.2553 to 0.3662.
    low, high = evalstats.wilson(81, 263)
    assert low == pytest.approx(0.2553, abs=5e-5) and high == pytest.approx(0.3662, abs=5e-5)


def test_wilson_at_zero_hand_worked():
    # x = 0: low is 0 and high is z^2 / (n + z^2); with n = 10 and z = 1.96 that is 3.8416 / 13.8416 = 0.27754
    low, high = evalstats.wilson(0, 10)
    assert low == 0.0 and high == pytest.approx(0.27754, abs=1e-4)
    assert evalstats.wilson(0, 0) is None


def test_wilson_never_leaves_zero_to_one_and_is_symmetric():
    low, high = evalstats.wilson(10, 10)
    assert high == pytest.approx(1.0) and low == pytest.approx(1 - 0.27754, abs=1e-4)
    lo5, hi5 = evalstats.wilson(5, 10)
    assert lo5 + hi5 == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("x", "n", "low", "high"),
    [
        (0, 10, 0.0, 0.3085),  # textbook exact limits for 0 of 10: 0 to 30.85%
        (1, 10, 0.0025, 0.4450),
        (5, 10, 0.1871, 0.8129),
        (10, 10, 0.6915, 1.0),
    ],
)
def test_clopper_pearson_matches_the_published_exact_limits(x, n, low, high):
    lo, hi = evalstats.clopper_pearson(x, n)
    assert lo == pytest.approx(low, abs=5e-5) and hi == pytest.approx(high, abs=5e-5)


def test_clopper_pearson_with_nothing_is_none():
    assert evalstats.clopper_pearson(0, 0) is None


def test_binom_cdf_hand_worked_and_survives_a_large_n():
    assert evalstats.binom_cdf(1, 4, 0.5) == pytest.approx(5 / 16)  # (1 + 4) / 16
    assert evalstats.binom_cdf(2, 3, 0.25) == pytest.approx(1 - 0.25**3)
    assert evalstats.binom_cdf(-1, 4, 0.5) == 0.0 and evalstats.binom_cdf(4, 4, 0.5) == 1.0
    assert evalstats.binom_cdf(5000, 10000, 0.5) == pytest.approx(
        0.5, abs=0.01
    )  # math.comb alone would overflow a float


def test_the_pass_rate_is_drawn_over_tasks_not_trials():
    # task a: 2 of 3 trials; task b: 3 of 3. The rate is the mean of the two task rates, (2/3 + 1) / 2, over 2 tasks.
    rs = [trial("a", True), trial("a", True), trial("a", False), *[trial("b", True)] * 3]
    ci = evalstats.cluster_interval(rs)
    assert ci["tasks"] == 2 and ci["trials"] == 6 and ci["rate"] == pytest.approx(5 / 6)
    low, high = evalstats.wilson(2 * 5 / 6, 2)
    assert ci["low"] == pytest.approx(low) and ci["high"] == pytest.approx(high)
    assert evalstats.cluster_interval([]) is None


def test_more_repeats_of_the_same_tasks_do_not_narrow_the_interval():
    three = [trial(f"t{i}", True) for i in range(3)]
    thirty = [trial(f"t{i}", True) for i in range(3) for _ in range(10)]
    one, many = evalstats.cluster_interval(three), evalstats.cluster_interval(thirty)
    assert (one["low"], one["high"]) == (many["low"], many["high"]) and many["trials"] == 30
    # while more tasks do
    wider = evalstats.cluster_interval([trial(f"t{i}", True) for i in range(30)])
    assert wider["low"] > one["low"]


# ---- the paired gap -----------------------------------------------------------------------------------------------


def test_paired_difference_six_to_nothing_hand_worked():
    # 6 of 10 tasks split, all to A. Exact limit for 6 of 6 is 0.025^(1/6) = 0.54074, so the gap is
    # (2 * 0.54074 - 1) * 6 / 10 = 4.889 points at the low end, and 60 at the high.
    d = evalstats.paired_difference(6, 0, 10)
    assert d["diff_points"] == 60.0 and d["low_points"] == pytest.approx(4.889, abs=1e-2)
    assert d["high_points"] == pytest.approx(60.0) and d["p"] == pytest.approx(2 / 64) and d["real"] is True


def test_paired_difference_five_to_nothing_is_not_enough_and_the_interval_says_so():
    d = evalstats.paired_difference(5, 0, 10)
    assert d["p"] == pytest.approx(0.0625) and d["real"] is False and d["low_points"] < 0 < d["high_points"]


def test_paired_difference_with_no_split_is_bounded_by_what_could_hide():
    # no task split in 10: the split rate is below 1 - 0.025^(1/10) = 0.3085, and all of it could have gone either way
    d = evalstats.paired_difference(0, 0, 10)
    assert d["diff_points"] == 0.0 and d["high_points"] == pytest.approx(30.85, abs=1e-2)
    assert d["low_points"] == pytest.approx(-d["high_points"]) and d["real"] is False
    assert evalstats.paired_difference(0, 0, 0) is None


def test_the_interval_and_the_verdict_never_disagree():
    for tasks in (12, 60):
        for a in range(25):
            for b in range(25):
                if a + b == 0 or a + b > tasks:
                    continue
                d = evalstats.paired_difference(a, b, tasks)
                excludes_zero = d["low_points"] > 0 or d["high_points"] < 0
                assert excludes_zero == d["real"], (a, b, tasks)
                assert d["low_points"] <= d["diff_points"] <= d["high_points"]


def test_paired_difference_is_antisymmetric():
    up, down = evalstats.paired_difference(7, 2, 30), evalstats.paired_difference(2, 7, 30)
    assert up["diff_points"] == -down["diff_points"] and up["real"] == down["real"]
    assert up["low_points"] == pytest.approx(-down["high_points"])


# ---- pass@k beside pass^k -----------------------------------------------------------------------------------------


def test_pass_at_k_hand_worked():
    # Chen et al. (2021) estimator 1 - C(n-c, k) / C(n, k). One task with 3 trials, 1 passed, k = 2: 1 - 1/3 = 2/3.
    assert evalstats.pass_at_k([(3, 1)], 2) == pytest.approx(2 / 3)
    assert evalstats.pass_at_k([(3, 0)], 2) == 0.0 and evalstats.pass_at_k([(3, 3)], 2) == 1.0
    # tasks with fewer than k trials are left out, and no task left is None
    assert evalstats.pass_at_k([(3, 1), (1, 1)], 2) == pytest.approx(2 / 3)
    assert evalstats.pass_at_k([(1, 1)], 2) is None


def test_pass_at_k_is_never_below_pass_hat_k_and_both_meet_at_k_one():
    tasks = [(5, 5), (5, 3), (5, 1), (5, 0)]
    for k in (1, 2, 3, 5):
        assert evalstats.pass_at_k(tasks, k) >= evalstats.pass_hat_k(tasks, k) - 1e-12
    assert evalstats.pass_at_k(tasks, 1) == pytest.approx(evalstats.pass_hat_k(tasks, 1)) == pytest.approx(9 / 20)


def test_the_report_shows_pass_at_k_beside_pass_hat_k():
    results = [trial("a", True), trial("a", False), trial("b", True), trial("b", True)]
    m = evalstats.compute(results, {}, {})["pass_hat_k"]
    assert m["k"] == 2 and m["value"] == pytest.approx(0.5) and m["pass_at_k"] == pytest.approx(1.0)
    assert "pass@2 100.0%" in m["display"] and "50.0% at k=2" in m["display"]


def test_the_report_gives_the_success_rate_an_interval_over_tasks():
    results = [trial("a", True), trial("a", False), trial("b", True), trial("b", True)]
    m = evalstats.compute(results, {}, {})["success_rate"]
    low, high = evalstats.wilson(2 * 0.75, 2)
    assert m["value"] == 0.75 and m["interval_tasks"] == 2 and m["interval"] == pytest.approx([low, high])
    assert "(3 of 4 trials)" in m["display"] and "95% interval" in m["display"]


# ---- the minimum detectable effect --------------------------------------------------------------------------------


def test_mde_given_the_split_hand_worked():
    # 6 of 6 is the fewest that count, so with 6 split tasks in 10 a lead of (2*6 - 6) / 10 = 60 points is the least
    assert evalstats.mde_points(10, 6) == pytest.approx(60.0)
    # 9 split tasks need 8 wins (8 of 9: p = 2 * 10 / 512 = 0.039): (2*8 - 9) / 20 = 35 points in 20 tasks
    assert evalstats.mde_points(20, 9) == pytest.approx(35.0)
    # 10 split tasks need 9 wins; 12 need 10
    assert evalstats.mde_points(20, 10) == pytest.approx(40.0) and evalstats.mde_points(24, 12) == pytest.approx(
        33.3, abs=0.1
    )


def test_mde_is_exactly_the_edge_of_what_the_test_calls_real():
    for tasks, split in ((20, 9), (30, 12), (50, 20)):
        mde = evalstats.mde_points(tasks, split)
        need = evalstats.wins_needed(split)
        assert (
            evalstats.mcnemar_p(need, split - need) < evalstats.ALPHA <= evalstats.mcnemar_p(need - 1, split - need + 1)
        )
        assert mde == pytest.approx((need - (split - need)) / tasks * 100)


def test_mde_without_a_split_is_the_best_case_for_this_many_tasks():
    assert evalstats.mde_points(100) == pytest.approx(6.0)  # 6 of 6 split tasks all one way
    assert evalstats.mde_points(10) == pytest.approx(60.0)
    assert evalstats.mde_points(5) is None  # under 6 tasks nothing can be called real
    assert evalstats.mde_points(0) is None
    # a split too small for any win to count falls back to the best case
    assert evalstats.mde_points(20, 4) == pytest.approx(30.0)


# ---- the sentence -------------------------------------------------------------------------------------------------


def test_the_sentence_says_who_leads_by_how_much_and_that_it_is_real():
    s = evalstats.lead_sentence("base", "tight", 20, 6, 0)
    assert s == (
        "base leads tight by 30.0 points on 20 paired tasks (95% interval +2.4 to +30.0 points): "
        "the difference is real at 95%."
    )


def test_the_sentence_says_too_few_runs_with_the_mde():
    s = evalstats.lead_sentence("base", "tight", 20, 1, 3)
    assert s == (
        "tight leads base by 10.0 points on 20 paired tasks (95% interval -12.2 to +19.7 points): "
        "too few runs to tell (MDE 30.0 points)."
    )


def test_the_sentence_reads_the_same_whichever_arm_is_a():
    a, b = evalstats.lead_sentence("one", "two", 20, 6, 0), evalstats.lead_sentence("two", "one", 20, 0, 6)
    assert a == b and a.startswith("one leads two by 30.0 points")


@pytest.mark.parametrize("split", [(6, 0), (0, 6), (2, 5), (5, 2), (0, 0), (3, 3)])
def test_the_sentence_never_names_the_same_arm_twice(split):
    s = evalstats.lead_sentence("alpha", "omega", 20, *split)
    assert s.count("alpha") == 1 and s.count("omega") == 1
    if split[0] == split[1]:
        assert "alpha and omega are level" in s and " leads " not in s
    else:
        lead, trail = ("alpha", "omega") if split[0] > split[1] else ("omega", "alpha")
        assert s.startswith(f"{lead} leads {trail} by ")


def test_the_sentence_refuses_one_arm_against_itself_and_says_when_there_is_nothing():
    with pytest.raises(ValueError, match="two different arms"):
        evalstats.lead_sentence("x", "x", 10, 6, 0)
    assert evalstats.lead_sentence("x", "y", 0, 0, 0) == "no paired tasks, so no comparison"


def test_the_sentence_says_when_too_few_tasks_can_ever_show_a_gap():
    s = evalstats.lead_sentence("x", "y", 4, 3, 0)
    assert "too few runs to tell (MDE: 4 tasks cannot show a gap" in s


# ---- the decision rule has no fixed band --------------------------------------------------------------------------


def test_decide_names_a_winner_on_a_small_gap_when_the_test_says_it_is_real():
    # 8 of 9 split tasks to A out of 400 tasks is a gap of 1.75 points: under the old fixed 3-point band, and real
    stats = {"pairs": 400, "a_only": 8, "b_only": 1, "gap_points": 7 / 400 * 100, "p": evalstats.mcnemar_p(8, 1),
             "down_a": None, "down_b": None, "rated_a": 0, "rated_b": 0, "tokens_a": None, "tokens_b": None}  # fmt: skip
    verdict = evalstats.decide(stats)
    assert verdict["winner"] == "A" and "noise" not in verdict["why"]


def test_decide_does_not_call_a_big_gap_real_when_the_test_does_not():
    stats = {"pairs": 10, "a_only": 5, "b_only": 0, "gap_points": 50.0, "p": evalstats.mcnemar_p(5, 0),
             "down_a": None, "down_b": None, "rated_a": 0, "rated_b": 0, "tokens_a": None, "tokens_b": None}  # fmt: skip
    verdict = evalstats.decide(stats)
    assert verdict["winner"] is None and "too few to separate" in verdict["why"] and "noise" not in verdict["why"]


# ---- the fingerprint spread ---------------------------------------------------------------------------------------


def fp(**over) -> dict:
    base = {"models": ["m1"], "runner": "r", "runner_version": "1", "ws_version": "0.1", "policy_hash": "p",
            "guards_hash": "g", "job_hash": "j", "resources": {"timeout_s": 30.0, "max_turns": None}, "bare": False}  # fmt: skip
    return {**base, **over}


def with_fp(f: dict | None) -> dict:
    return {"task": "t", "pass": True} if f is None else {"task": "t", "pass": True, "fingerprint": f}


def test_one_way_of_running_has_no_spread():
    s = evalstats.fingerprint_spread([with_fp(fp()), with_fp(fp())])
    assert s == {"with_fingerprint": 2, "without": 0, "mixed": {}, "bare": False}


@pytest.mark.parametrize("field", evalstats.FINGERPRINT_FIELDS)
def test_each_field_that_differs_is_named(field):
    other = {
        "models": ["m2"], "runner": "r2", "runner_version": "2", "ws_version": "0.2", "policy_hash": "p2",
        "guards_hash": "g2", "job_hash": "j2", "resources": {"timeout_s": 60.0, "max_turns": None}, "bare": True,
    }  # fmt: skip
    s = evalstats.fingerprint_spread([with_fp(fp()), with_fp(fp(**{field: other[field]}))])
    assert list(s["mixed"]) == [field] and len(s["mixed"][field]) == 2


def test_a_value_that_was_not_recorded_is_not_a_difference():
    s = evalstats.fingerprint_spread(
        [with_fp(fp()), with_fp(fp(models=[], runner_version=None, policy_hash=None)), with_fp(None)]
    )
    assert s["mixed"] == {} and s["with_fingerprint"] == 2 and s["without"] == 1


def test_bare_is_reported_when_any_result_ran_bare():
    assert evalstats.fingerprint_spread([with_fp(fp(bare=True))])["bare"] is True
    assert evalstats.fingerprint_spread([with_fp(None)])["bare"] is None
