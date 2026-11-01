"""Eval arms: the numbers `ws eval report` shows, per arm, from the eval results and the run log.

Each arm gets its pass rate, pass^k, time, tokens and cost per solved task, its trend over batches, how many wins an
A/B needs to be more than noise, and for each figure with no data the reason. Trials that never started are left out,
as the report leaves them out. Nothing is estimated: a figure ws can't work out is `not_measured` with why.
"""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from ws import eval as ws_eval
from ws import evalstats, rating, runrecord
from ws.home.records import EvalArm
from ws.home.sources import UNREADABLE, SourceError, _files

NAME = "ws_eval"
PROVIDES = ("evals",)
ORDER = 60
WINDOW = timedelta(days=90)  # the report's own default
SHOWN = ("success_rate", "pass_hat_k", "tokens_per_solved", "cost_per_solved", "time")


def available(env):
    path = ws_eval.results_path(dict(env.environ))
    return (True, "") if path.is_file() else (False, "no eval results yet")


def _value(metrics: dict, key: str):
    m = metrics[key]
    return m["value"] if m.get("measured") else None


def read(kind, env):
    environ = dict(env.environ)
    cutoff = env.now - WINDOW.total_seconds()
    try:
        results = [
            r for r in ws_eval.read_jsonl(ws_eval.results_path(environ))
            if (evalstats.parse_ts(r.get("ts")) is None or evalstats.parse_ts(r["ts"]).timestamp() >= cutoff)
        ]  # fmt: skip
        all_runs = runrecord.read(runrecord.runs_path(environ))
        ratings = rating.latest(rating.read(rating.ratings_path(environ)))
    except OSError as e:
        raise SourceError(UNREADABLE, f"can't read the eval results: {e.strerror or type(e).__name__}") from e
    runs = {r["run_id"]: r for r in all_runs if isinstance(r.get("run_id"), str)}
    counted, _ = evalstats.split_setup_errors(results, runs)
    out = []
    for arm in sorted({str(r.get("arm")) for r in counted}):
        mine = [r for r in counted if str(r.get("arm")) == arm]
        metrics = ws_eval.metrics_for(environ, mine, runs, ratings, all_runs, WINDOW.days, None)
        time_value = _value(metrics, "time") or {}
        k = metrics["pass_hat_k"].get("k") if metrics["pass_hat_k"].get("measured") else None
        jobs = Counter(str(r.get("job")) for r in mine if r.get("job"))
        out.append(
            EvalArm(
                arm=arm,
                job=jobs.most_common(1)[0][0] if jobs else "",
                trials=len(mine),
                solved=sum(1 for r in mine if r.get("pass") is True),
                pass_rate=_value(metrics, "success_rate"),
                pass_hat_k={"k": k, "value": _value(metrics, "pass_hat_k")},
                time_per_solved={"p50_s": time_value.get("p50_s"), "p90_s": time_value.get("p90_s")},
                tokens_per_solved=_value(metrics, "tokens_per_solved"),
                cost_per_solved_usd=_value(metrics, "cost_per_solved"),
                trend=tuple(evalstats.trend(mine)),
                noise={
                    "points": evalstats.NOISE_POINTS,
                    "wins_needed": evalstats.wins_needed(len({str(r.get("task")) for r in mine})),
                },
                not_measured={
                    key: _files.text(metrics[key].get("note", "").removeprefix("not measured: "), 200)
                    for key in SHOWN
                    if not metrics[key].get("measured")
                },
            )
        )
    return out
