"""Evaluation: one card per eval arm, with the figures `ws eval report` shows and a trend over batches.

Each card gives the pass rate, pass^k, time per solved (p50 and p90), tokens and cost per solved, a line over the
batches, and the noise band: how many points a gap needs to be more than noise and how many wins settle a head-to-head.
A figure with no data says so and says why, in the words the report uses; a cost is `not measured` until it is.
"""

from __future__ import annotations

from ws.home.areas import _fmt
from ws.home.areas._base import View
from ws.home.records import NOT_MEASURED
from ws.ui import components

ID = "evals"
ORDER = 4
TITLE = "Evaluation"
READS = "evals: per arm, the report's metrics and its trend over batches"
EMPTY = "No eval trials yet. Run `ws eval run --set starter` to make some."
OPAQUE = "Eval figures come from trials, not sessions, so there is nothing to hide."
ACTIONS = "Read the arms side by side; `ws eval compare` settles a head-to-head."
BUILT = True


def _why(arm, key: str) -> str:
    return arm.not_measured.get(key, "no data yet")


def _stat(arm, label: str, key: str, value: str | None, note: str = "") -> str:
    if value is None:
        return components.stat(label, NOT_MEASURED, _why(arm, key))
    return components.stat(label, value, note)


def card(arm) -> str:
    k = arm.pass_hat_k.get("k")
    hat = arm.pass_hat_k.get("value")
    t = arm.time_per_solved
    time_text = (
        None if t.get("p50_s") is None else f"{_fmt.seconds(t['p50_s'])} p50 · {_fmt.seconds(t.get('p90_s'))} p90"
    )
    trend = [p["pass_rate"] for p in arm.trend if p.get("pass_rate") is not None]
    batches = len(arm.trend)
    latest = f", latest {_fmt.percent(trend[-1])}" if trend else ""
    rate_text = _fmt.percent(arm.pass_rate)
    big = (
        f'<span class="big">{_fmt.esc(rate_text)}</span><span class="of">{arm.solved} of {arm.trials} trials solved</span>'
        if arm.pass_rate is not None
        else f'<span class="big off">{NOT_MEASURED}</span><span class="of">{_fmt.esc(_why(arm, "success_rate"))}</span>'
    )
    stats = "".join(
        [
            _stat(
                arm,
                f"pass^{k}" if k else "pass^k",
                "pass_hat_k",
                None if hat is None else f"{hat:.2f}",
                f"all {k} trials of a task pass" if k else "",
            ),
            _stat(arm, "Time per solved", "time", time_text),
            _stat(
                arm,
                "Tokens per solved",
                "tokens_per_solved",
                None if arm.tokens_per_solved is None else _fmt.count(arm.tokens_per_solved),
            ),
            _stat(
                arm,
                "Cost per solved",
                "cost_per_solved",
                None if arm.cost_per_solved_usd is None else _fmt.usd(arm.cost_per_solved_usd),
            ),
        ]
    )
    noise = arm.noise or {}
    points = noise.get("points")
    wins = noise.get("wins_needed")
    band = (
        f"A gap under {points:g} points is noise" + (f"; a head-to-head needs {wins} wins." if wins else ".")
        if points is not None
        else "No noise band yet."
    )
    spark = components.sparkline(trend, width=160, height=32, label=f"{arm.arm} pass rate by batch", lo=0.0, hi=1.0)
    return (
        f'<article class="arm"><header><h3>{_fmt.esc(arm.arm)}</h3>'
        f'<span class="job">{_fmt.esc(arm.job)}</span></header>'
        f'<div class="rate">{big}</div><div class="stats">{stats}</div>'
        f'<div class="trend">{spark}<span class="cap">{batches} batch{"es" if batches != 1 else ""}{_fmt.esc(latest)}</span></div>'
        f'<p class="band">{_fmt.esc(band)}</p></article>'
    )


def _gap(arms: list) -> str:
    """One line over the arms that have a pass rate: how far apart the best and the worst are, against the band."""
    rated = [a for a in arms if a.pass_rate is not None]
    if len(rated) < 2:
        return ""
    hi, lo = max(rated, key=lambda a: a.pass_rate), min(rated, key=lambda a: a.pass_rate)
    gap = (hi.pass_rate - lo.pass_rate) * 100
    points = (hi.noise or {}).get("points")
    if points is None:
        return ""
    verdict = "inside the noise band: call it a tie" if gap < points else "outside the noise band"
    return f'<p class="gap">{_fmt.esc(hi.arm)} and {_fmt.esc(lo.arm)} are {gap:.1f} points apart: {verdict}.</p>'


def verdict(arms, now: str | None = None) -> str:
    """The latest eval result in a few words, for the quiet line on Home: which arm leads and whether the gap is more than
    noise, or why there is nothing to say yet."""
    if not arms:
        return "no eval trials yet"
    rated = sorted((a for a in arms if a.pass_rate is not None), key=lambda a: -a.pass_rate)
    if not rated:
        return f"{len(arms)} arm{'s' if len(arms) != 1 else ''}, no pass rate yet"
    stamps = [p["ts"] for a in arms for p in a.trend if p.get("ts")]
    when = _fmt.age(max(stamps), now) if stamps else ""
    tail = f", {when}" if when else ""
    if len(rated) == 1:
        a = rated[0]
        return f"{a.arm} solved {a.solved} of {a.trials}{tail}"
    best, next_ = rated[0], rated[1]
    gap = (best.pass_rate - next_.pass_rate) * 100
    points = (best.noise or {}).get("points")
    if points is not None and gap < points:
        return f"{best.arm} and {next_.arm} are within noise{tail}"
    return f"{best.arm} leads {next_.arm} by {gap:.0f} points{tail}"


def render(view: View) -> str:
    arms = list(view.snap.evals)
    if not arms:
        return components.panel(TITLE, components.empty(EMPTY))
    cards = "".join(card(a) for a in arms)
    n = len(arms)
    return components.panel(
        TITLE, f'<div class="arms">{cards}</div>{_gap(arms)}', aside=f"{n} arm{'s' if n != 1 else ''}"
    )
