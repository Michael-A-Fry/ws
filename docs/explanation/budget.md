# The budget

The budget is the rule that ws starts no paid run without a monthly spend cap that you set, and starts none once this
month's spend has reached it.

## The rule

`ws run` refuses a job when any of these is true:

- there is no `monthly_cap_usd` in `[roles.budget]` of your profile;
- this month's spend is at or above the cap;
- the runner can't say what the job's model costs and the job doesn't set `allow_unpriced = true`.

A refusal exits with code 3 and still writes a run record with status `refused`.

## Why it exists

An unattended job that loops can spend real money before you notice. A cap that you set yourself, and that ws checks
before every run, bounds the damage of a bad prompt or a stuck loop. ws ships no price list, so the cap is your number
and your fallback price, not a guess about a provider's rates.

## How ws enforces it

The code is `src/ws/run.py` (the check) and `src/ws/runrecord.py` (the sum). `ws doctor` reports the same setting as
WS103.

1. **A cap is required.** A profile with no positive `monthly_cap_usd` makes `ws run` refuse. WS103 fails.
2. **Spend is counted from the run log.** `ws` adds up `cost_usd` in `$WS_HOME/logs/runs.jsonl` for runs that started in
   the current UTC month. Refused runs cost nothing.
3. **A run with no reported cost still counts.** If the runner gave token counts, they count at your
   `fallback_usd_per_mtok`. If it gave neither, the run counts at `unknown_run_usd` (1.0 by default). Spend is never
   undercounted.
4. **Log trouble fails safe.** A log lock held by another process refuses the run. A record that can't be written goes
   to `logs/runs.fallback.jsonl`, which the cap check reads too.
5. **Evals use it.** Every `ws eval` trial is a `ws run`, so the cap applies to each one.

You can see the check without starting anything: `ws run JOB --dry-run` prints `month_spend_usd`, `monthly_cap_usd` and
`would_refuse`.

## What the budget doesn't stop

- **Overspend inside one run.** The cap is checked before a run, not during it. A run that has started can spend what its
  `timeout_s` and `max_turns` allow. Keep both tight.
- **Spend that ws doesn't see.** Runs started by hand, outside `ws run`, aren't in the log. A session can also write
  the log without the wall; see [Known limits](known-limits.md).
- **The month boundary.** Spend is counted by UTC month. A run near midnight UTC counts in the month it started.
- **A wrong fallback.** The fallback rates are yours. If you set them too low, the cap lets more through.

## Do it

Follow [Set a budget](../how-to/set-a-budget.md).
