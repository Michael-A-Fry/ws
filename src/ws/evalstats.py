"""The numbers behind `ws eval`: exact McNemar, pass^k, percentiles and the ten report metrics.

Everything here is a pure function over records already on disk (the eval results, the run log, the ratings), except the
two git questions (rework, maintenance), which call `git log` and nothing else. A metric with no data is
"not measured", with the reason; it is never filled in with a guess, and a harm count is never averaged into another
score. Cost is `runrecord.record_cost` as it stands: ws ships no price table (ADR 0008).
"""

from __future__ import annotations

import math
import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ws import runrecord

ALPHA = 0.05  # two-sided, exact
NOISE_POINTS = 3.0  # a gap under this many percentage points is noise
REWORK_DAYS = 14
GIT_TIMEOUT_S = 20
FIXUP = re.compile(r"^(fixup!|squash!|revert\b|fix\b)", re.IGNORECASE)
MAINTENANCE = re.compile(r"^maintenance\b", re.IGNORECASE)
DECLARED_MINUTES = re.compile(r"\b(\d{1,4})\s*(?:min|mins|minutes)\b", re.IGNORECASE)

METRICS = (
    ("success_rate", "Success rate"),
    ("pass_hat_k", "pass^k (every trial of a task passes)"),
    ("harm_per_100", "Harm per 100 runs"),
    ("rework_14d", "Rework within 14 days"),
    ("tokens_per_solved", "Tokens per solved task"),
    ("cost_per_solved", "Cost per solved task"),
    ("time", "Time to first output; time per solved task"),
    ("attention", "Attention minutes and interventions"),
    ("quality", "Quality flag (ws rate)"),
    ("maintenance", "Maintenance minutes"),
)


# ---- statistics ---------------------------------------------------------------------------------------------------


def binom_tail(k: int, n: int) -> float:
    """P(X <= k) for X ~ Binomial(n, 1/2), exactly."""
    if k < 0:
        return 0.0
    return sum(math.comb(n, i) for i in range(min(k, n) + 1)) / 2**n


def mcnemar_p(a_only: int, b_only: int) -> float:
    """Exact two-sided McNemar p from the discordant pairs: a_only (A passed, B failed) and b_only."""
    n = a_only + b_only
    if n == 0:
        return 1.0
    return min(1.0, 2 * binom_tail(min(a_only, b_only), n))


def wins_needed(discordant: int) -> int | None:
    """The fewest wins out of `discordant` pairs that give p below ALPHA (6 of 6, 8 of 9, 9 of 10, 10 of 12); None when
    even a clean sweep is not enough."""
    for wins in range(math.ceil(discordant / 2), discordant + 1):
        if mcnemar_p(wins, discordant - wins) < ALPHA:
            return wins
    return None


def percentile(values: list[float], q: float) -> float | None:
    """The q-th percentile (0 to 100) by linear interpolation; None for no values."""
    if not values:
        return None
    v = sorted(values)
    pos = (len(v) - 1) * q / 100
    lo, hi = math.floor(pos), math.ceil(pos)
    return v[lo] + (v[hi] - v[lo]) * (pos - lo)


def pass_hat_k(per_task: list[tuple[int, int]], k: int) -> float | None:
    """pass^k over tasks given as (trials, passes): the chance that k trials drawn from a task all pass, averaged
    over the tasks that have at least k trials. None when no task does."""
    shares = [math.comb(c, k) / math.comb(n, k) for n, c in per_task if n >= k]
    return sum(shares) / len(shares) if shares else None


# ---- small helpers ------------------------------------------------------------------------------------------------


def parse_ts(text) -> datetime | None:
    try:
        t = datetime.fromisoformat(str(text))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def ok(value, display: str, n: int, **detail) -> dict:
    return {"measured": True, "value": value, "display": display, "n": n, **detail}


def not_measured(reason: str) -> dict:
    return {"measured": False, "value": None, "display": "not measured", "note": f"not measured: {reason}"}


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _tokens_by_type(rec: dict | None) -> dict[str, int] | None:
    """The four token counts of a record, or None when it reports none of them."""
    t = (rec or {}).get("tokens")
    if not isinstance(t, dict):
        return None
    counts = {k: _int(t.get(k)) for k in runrecord.TOKEN_KEYS}
    if all(v is None for v in counts.values()):
        return None
    return {k: v or 0 for k, v in counts.items()}


def _pct(x: float) -> str:
    return f"{x * 100:.1f}%"


# ---- the git questions --------------------------------------------------------------------------------------------


def git_subjects(
    repo: Path, since: datetime, until: datetime | None = None, paths: list[str] | None = None
) -> list[str]:
    """Commit subjects in `repo` between two times, newest first, limited to `paths` when given. Raises OSError when
    `repo` is not a readable repository or git does not answer."""
    cmd = ["git", "-c", "core.fsmonitor=false", "-C", str(repo), "log", "--format=%s", f"--since={since.isoformat()}"]
    if until is not None:
        cmd.append(f"--until={until.isoformat()}")
    if paths:
        cmd += ["--", *paths]
    try:
        p = subprocess.run(
            cmd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise OSError(f"git did not answer for {repo}") from e
    if p.returncode != 0:
        raise OSError(f"{repo} is not a git repository")
    return [line for line in p.stdout.splitlines() if line.strip()]


GitSubjects = Callable[..., list[str]]


# ---- the ten metrics ----------------------------------------------------------------------------------------------


def _success(results: list[dict]) -> dict:
    if not results:
        return not_measured("no eval trials yet")
    passed = sum(1 for r in results if r.get("pass") is True)
    return ok(passed / len(results), f"{_pct(passed / len(results))} ({passed} of {len(results)} trials)", len(results))


def _pass_k(results: list[dict], k: int | None) -> dict:
    by_task: dict[str, list[bool]] = {}
    for r in results:
        by_task.setdefault(str(r.get("task")), []).append(r.get("pass") is True)
    if not by_task:
        return not_measured("no eval trials yet")
    use = k or min(5, min(len(v) for v in by_task.values()))
    value = pass_hat_k([(len(v), sum(v)) for v in by_task.values()], use)
    if value is None:
        return not_measured(f"no task has {use} trials")
    return ok(value, f"{_pct(value)} at k={use}", len(by_task), k=use)


def _is_harm(r: dict, rec: dict | None) -> bool:
    rec = rec or {}
    pol = rec.get("policy") if isinstance(rec.get("policy"), dict) else {}
    return r.get("status") == "refused" or rec.get("status") == "refused" or pol.get("rule_of_two") == "refused"


def _harm(results: list[dict], runs: dict[str, dict], all_runs: list[dict]) -> dict:
    """Refused runs and runs the Rule of Two refused: what the records show of the gate saying no. Reported on its own."""
    if not results:
        return not_measured("no eval trials yet")
    harm = sum(1 for r in results if _is_harm(r, runs.get(str(r.get("run_id")))))
    per_100 = harm / len(results) * 100
    every = sum(1 for r in all_runs if _is_harm({}, r))
    return ok(
        per_100,
        f"{per_100:.1f} per 100 trials ({harm} of {len(results)}); {every} of {len(all_runs)} runs in the log",
        len(results),
        events=harm,
        all_runs_events=every,
        all_runs=len(all_runs),
    )


def _rework(results: list[dict], repos: dict[str, tuple[str, list[str]]], now: datetime, git: GitSubjects) -> dict:
    """Of the solved trials of tasks that name a repo and are at least 14 days old, the share followed by a fix-up or
    revert commit on the task's files (any file, if the task names none) inside the 14 days after the trial."""
    eligible, pending, unreadable = [], 0, 0
    for r in results:
        repo, files = repos.get(str(r.get("task")), ("", []))
        ended = parse_ts(r.get("ts"))
        if not repo or r.get("pass") is not True or ended is None:
            continue
        if now - ended < timedelta(days=REWORK_DAYS):
            pending += 1
            continue
        eligible.append((r, Path(repo).expanduser(), files, ended))
    if not eligible:
        why = f"{pending} solved trials are under {REWORK_DAYS} days old" if pending else "no task names a repo"
        return not_measured(why)
    redone = 0
    for _r, repo, files, ended in eligible:
        try:
            subjects = git(repo, ended, ended + timedelta(days=REWORK_DAYS), files)
        except OSError:
            unreadable += 1
            continue
        redone += any(FIXUP.match(s) for s in subjects)
    counted = len(eligible) - unreadable
    if counted == 0:
        return not_measured("the named repos could not be read")
    return ok(
        redone / counted, f"{_pct(redone / counted)} ({redone} of {counted} solved trials)", counted, pending=pending
    )


def _tokens(results: list[dict], runs: dict[str, dict]) -> dict:
    total = dict.fromkeys(runrecord.TOKEN_KEYS, 0)
    solved = trials = 0
    for r in results:
        counts = _tokens_by_type(runs.get(str(r.get("run_id"))))
        if counts is None:
            continue
        trials += 1
        solved += r.get("pass") is True
        for k, v in counts.items():
            total[k] += v
    if trials == 0:
        return not_measured("no trial has token counts")
    if solved == 0:
        return not_measured("nothing was solved, so there is no per-solved figure")
    per = {k: v / solved for k, v in total.items()}
    value = sum(total.values()) / solved
    return ok(value, f"{value:,.0f} per solved task", solved, by_type=per, trials_counted=trials)


def tokens_per_solved(results: list[dict], runs: dict[str, dict]) -> float | None:
    """Tokens spent over trials with counts, per solved trial; None when that cannot be worked out."""
    return _tokens(results, runs).get("value")


def _cost(results: list[dict], runs: dict[str, dict], unknown_run_usd, fallback_usd_per_mtok) -> dict:
    spent, solved, estimated, counted = 0.0, 0, 0, 0
    for r in results:
        rec = runs.get(str(r.get("run_id")))
        if rec is None:
            continue
        counted += 1
        solved += r.get("pass") is True
        estimated += not isinstance(rec.get("cost_usd"), (int, float))
        spent += runrecord.record_cost(rec, unknown_run_usd, fallback_usd_per_mtok)
    if counted == 0:
        return not_measured("no trial has a run record")
    if solved == 0:
        return not_measured("nothing was solved, so there is no per-solved figure")
    note = f"; {estimated} of {counted} trials had no reported cost and count at your fallback" if estimated else ""
    return ok(
        spent / solved, f"US${spent / solved:.4f} per solved task{note}", solved, total_usd=spent, estimated=estimated
    )


def _time(results: list[dict], runs: dict[str, dict]) -> dict:
    secs = []
    for r in results:
        ms = _int((runs.get(str(r.get("run_id"))) or {}).get("duration_ms"))
        if r.get("pass") is True and ms is not None:
            secs.append(ms / 1000)
    if not secs:
        return not_measured("no solved trial has a duration")
    p50, p90 = percentile(secs, 50), percentile(secs, 90)
    return ok(
        {"first_output_s": None, "p50_s": p50, "p90_s": p90},
        f"p50 {p50:.1f} s, p90 {p90:.1f} s per solved task (time to first output: not measured, the record has no field)",
        len(secs),
    )


def _quality(results: list[dict], ratings: dict[str, str]) -> dict:
    ids = {str(r.get("run_id")) for r in results if r.get("run_id")}
    rated = {i: ratings[i] for i in ids if i in ratings}
    if not rated:
        return not_measured("no trial has been rated with `ws rate`")
    down = sum(1 for v in rated.values() if v == "down")
    return ok(
        down / len(rated),
        f"{down} down of {len(rated)} rated ({_pct(down / len(rated))}); {len(rated)} of {len(ids)} trials rated",
        len(rated),
        down=down,
        up=len(rated) - down,
        trials=len(ids),
    )


def _maintenance(repos: set[str], now: datetime, days: int, git: GitSubjects) -> dict:
    if not repos:
        return not_measured("no task names a repo")
    commits = minutes = 0
    declared = False
    read = 0
    for repo in sorted(repos):
        try:
            subjects = git(Path(repo).expanduser(), now - timedelta(days=days))
        except OSError:
            continue
        read += 1
        for s in subjects:
            if MAINTENANCE.match(s):
                commits += 1
                m = DECLARED_MINUTES.search(s)
                if m:
                    declared = True
                    minutes += int(m[1])
    if read == 0:
        return not_measured("the named repos could not be read")
    mins = minutes if declared else None
    shown = f"{minutes} minutes declared" if declared else "minutes not declared in the subjects"
    return ok({"commits": commits, "minutes": mins}, f"{commits} maintenance commits in {days} days, {shown}", commits)


def compute(
    results: list[dict],
    runs: dict[str, dict],
    ratings: dict[str, str],
    *,
    all_runs: list[dict] | None = None,
    repos: dict[str, tuple[str, list[str]]] | None = None,
    budget: tuple[float | None, float | None] = (None, None),
    now: datetime | None = None,
    k: int | None = None,
    window_days: int = 90,
    git: GitSubjects = git_subjects,
) -> dict[str, dict]:
    """The ten metrics for these results. `runs` maps run id to its record, `ratings` run id to its latest rating,
    `repos` task id to (repo, files), `budget` is (unknown_run_usd, fallback_usd_per_mtok)."""
    now = now or datetime.now(UTC)
    repos = repos or {}
    named = {repo for repo, _ in repos.values() if repo}
    return {
        "success_rate": _success(results),
        "pass_hat_k": _pass_k(results, k),
        "harm_per_100": _harm(results, runs, all_runs or []),
        "rework_14d": _rework(results, repos, now, git),
        "tokens_per_solved": _tokens(results, runs),
        "cost_per_solved": _cost(results, runs, *budget),
        "time": _time(results, runs),
        "attention": not_measured("ws records no attention time or interventions for headless runs"),
        "quality": _quality(results, ratings),
        "maintenance": _maintenance(named, now, window_days, git),
    }


# ---- comparing two arms -------------------------------------------------------------------------------------------


MIN_RATED = 5  # thumbs a quality flag needs, per arm, before it can veto anything


def paired_tasks(a: list[dict], b: list[dict]) -> dict:
    """Pair two arms at task level, which is what McNemar's independence needs: trials of one task share a prompt, a
    setup and a grader, so they are not independent draws, and counting them as pairs inflates the evidence.

    The rule: for each task and arm, the latest result of each trial number counts; the task is *passed* if more than half
    of those trials passed (a tie, such as 1 of 2, is a fail). Only tasks both arms ran are paired, and only on matching
    versions: an arm's results count only if they come from the job it ran last, and from the task text it ran last
    (`task_hash`); a task whose text differs between the two arms is left out. Whatever is left out is counted in
    `dropped`, never hidden.

    Returns tasks (the n of the test), trials_a and trials_b (the trials behind it), both, a_only, b_only, neither (task
    counts), dropped, and used_a / used_b (the results that were used)."""
    dropped = {"other_job": 0, "other_task_text": 0, "task_text_differs": 0}

    def usable(rs: list[dict]) -> dict[str, dict]:
        rs = sorted(rs, key=lambda x: str(x.get("ts")))
        if not rs:
            return {}
        job = rs[-1].get("job")
        kept = [r for r in rs if r.get("job") == job]
        dropped["other_job"] += len(rs) - len(kept)
        latest_hash: dict = {}
        for r in kept:
            latest_hash[r.get("task")] = r.get("task_hash")
        same = [r for r in kept if r.get("task_hash") == latest_hash[r.get("task")]]
        dropped["other_task_text"] += len(kept) - len(same)
        by_task: dict[str, dict] = {}
        for r in same:
            t = by_task.setdefault(str(r.get("task")), {"hash": r.get("task_hash"), "trials": {}, "results": []})
            t["results"].append(r)
            t["trials"][r.get("trial")] = r.get("pass") is True  # sorted by time: the latest of a trial number wins
        return by_task

    ua, ub = usable(a), usable(b)
    both = a_only = b_only = neither = trials_a = trials_b = 0
    used_a: list[dict] = []
    used_b: list[dict] = []
    for task in ua.keys() & ub.keys():
        if ua[task]["hash"] != ub[task]["hash"]:
            dropped["task_text_differs"] += 1
            continue
        passed = []
        for side in (ua[task], ub[task]):
            outcomes = list(side["trials"].values())
            passed.append(sum(outcomes) * 2 > len(outcomes))
        used_a += ua[task]["results"]
        used_b += ub[task]["results"]
        trials_a += len(ua[task]["trials"])
        trials_b += len(ub[task]["trials"])
        pa, pb = passed
        both += pa and pb
        a_only += pa and not pb
        b_only += pb and not pa
        neither += not pa and not pb
    return {
        "tasks": both + a_only + b_only + neither, "trials_a": trials_a, "trials_b": trials_b, "both": both,
        "a_only": a_only, "b_only": b_only, "neither": neither, "dropped": dropped, "used_a": used_a,
        "used_b": used_b,
    }  # fmt: skip


def decide(stats: dict) -> dict:
    """The decision rule over a comparison's numbers: success first (exact McNemar, a gap under 3 points is noise), a
    quality veto, then tokens among equals. Returns {"winner": "A"|"B"|None, "why": text}.

    `stats` has: pairs (paired tasks), a_only, b_only, gap_points (A minus B), p, down_a, down_b (thumbs-down rates or
    None), rated_a, rated_b (how many ratings behind them), tokens_a, tokens_b (per solved task or None). A quality
    flag with fewer than MIN_RATED ratings on either arm cannot veto."""
    if stats["pairs"] == 0:
        return {
            "winner": None,
            "why": "no paired tasks: both arms need the same tasks, in the same version, on one job",
        }
    discordant = stats["a_only"] + stats["b_only"]
    gap = stats["gap_points"]
    significant = abs(gap) >= NOISE_POINTS and stats["p"] < ALPHA
    veto = _veto(stats)
    if significant:
        lead = "A" if gap > 0 else "B"
        if veto == lead:
            return {
                "winner": None,
                "why": f"{lead} passes more (p={stats['p']:.3f}) but its quality flag is worse: veto",
            }
        return {"winner": lead, "why": f"{lead} passes more: {abs(gap):.1f} points, exact McNemar p={stats['p']:.3f}"}
    if abs(gap) < NOISE_POINTS:
        gap_text = f"a gap of {abs(gap):.1f} points is under {NOISE_POINTS:g}: noise"
    else:
        need = wins_needed(discordant)
        wins = f"a win needs {need} of them" if need else "too few to separate the arms"
        gap_text = f"p={stats['p']:.3f} with {discordant} discordant pairs, {wins}"
    if veto:
        other = "B" if veto == "A" else "A"
        return {"winner": other, "why": f"equal on success ({gap_text}); {veto} is vetoed by its quality flag"}
    ta, tb = stats["tokens_a"], stats["tokens_b"]
    if ta is None or tb is None:
        return {"winner": None, "why": f"equal on success ({gap_text}); tokens per solved task not measured for both"}
    if abs(ta - tb) / max(ta, tb) * 100 < NOISE_POINTS:
        return {"winner": None, "why": f"equal on success ({gap_text}) and tokens differ by under {NOISE_POINTS:g}%"}
    lean = "A" if ta < tb else "B"
    return {"winner": lean, "why": f"equal on success ({gap_text}); {lean} uses fewer tokens per solved task"}


def quality_note(stats: dict) -> str | None:
    """Why the quality flag could not be used, or None when it could (or when nobody rated anything)."""
    ra, rb = stats.get("rated_a", 0), stats.get("rated_b", 0)
    if ra >= MIN_RATED and rb >= MIN_RATED:
        return None
    return f"too few ratings to veto (A {ra}, B {rb}; {MIN_RATED} needed on each arm)"


def _veto(stats: dict) -> str | None:
    """The arm whose thumbs-down rate is worse by NOISE_POINTS or more, once both arms have MIN_RATED ratings."""
    da, db = stats["down_a"], stats["down_b"]
    if quality_note(stats) or da is None or db is None or abs(da - db) * 100 < NOISE_POINTS:
        return None
    return "A" if da > db else "B"
