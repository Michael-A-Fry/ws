# Set a budget

Set a monthly spend cap, see what a run would be allowed to do, and read this month's spend. To understand the rule first,
read [The budget](../explanation/budget.md).

## Before you start

- ws installed and `ws init` done ([Install ws](install.md)).
- A job file in `$WS_HOME/jobs/` ([Rule of Two how-to](apply-the-rule-of-two.md) shows one).

## Steps

1. Set the cap in your profile, `$WS_HOME/profile.toml` (`~/.ws/profile.toml` unless you set `WS_HOME`). The number is US
   dollars for a calendar month in UTC.

   ```toml
   [roles.budget]
   monthly_cap_usd = 20
   ```

   Or run `ws setup --cap 20` (add `--yes` for no prompts).

2. Optional: set your own fallback prices. ws ships no price list. A run that reports no cost counts at these.

   ```toml
   [roles.budget]
   monthly_cap_usd = 20
   fallback_usd_per_mtok = 15   # US$ per million tokens, for a run that reports tokens but no cost
   unknown_run_usd = 1          # US$ per run that reports neither (default 1)
   ```

   Set them above the real price. Spend is then never undercounted.

3. Keep each job tight. The cap is checked before a run, not during it. In the job file, set the limits:

   ```toml
   timeout_s = 600
   max_turns = 8
   ```

4. Check the cap without starting a run.

   ```sh
   ws run daily --dry-run
   ```

   The output includes `month_spend_usd`, `monthly_cap_usd` and `would_refuse`.

5. Check the setting with the doctor.

   ```sh
   ws doctor
   ```

6. Read the records that add up to the spend.

   ```sh
   ws runs --last 5
   ```

## Success

Step 4 prints `"monthly_cap_usd": 20.0` and `"would_refuse": null`. WS103 shows `ok` with `monthly cap US$20`.

Without a cap, step 4 prints `"would_refuse": "no monthly budget cap (WS103): ..."`, and `ws run daily` exits with
code 3.

## If a run is refused

`ws run` exits 3 and writes a record with status `refused`. The reason is on standard error. It's one of: no cap,
spend at
the cap, a Rule of Two refusal, or a model the runner can't price. For the last, set `allow_unpriced = true` in the job
file to run anyway. The run then counts at your fallback price.

## Limits

A started run can spend what its `timeout_s` and `max_turns` allow. Runs outside `ws run` aren't in the log. See
[The budget](../explanation/budget.md).
