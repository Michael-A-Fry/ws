# Set a budget

Set a monthly spend cap, see what a run would be allowed to do, and read this month's spend. To understand the rule first,
read [The budget](../explanation/budget.md).

## Before you start

- ws installed and `ws init` done ([Install ws](install.md)).
- A job file in `~/.config/ws/jobs/` ([Rule of Two how-to](apply-the-rule-of-two.md) shows one).

## Steps

1. Set the cap in your profile, `~/.config/ws/profile.toml` (`profile.toml` in [the config folder](../reference/stores.md#where-ws-keeps-its-files)). The number is US
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

3. Keep each job tight. In the job file, set the limits, and say how much one run may spend. ws reserves that sum before
   the run and settles from what the run used. If less than that's left under a cap, the reservation shrinks to the
   remainder and Claude Code is told so with `--max-budget-usd` (add `strict = true` to refuse instead):

   ```toml
   timeout_s = 600
   max_turns = 8
   budget_usd = 1.5
   ```

4. Optional: give each kind of work its own share of the month, in `policy.toml`, and name it in the job. A job is
   refused at its own purpose's cap whatever room the others have. A purpose the policy doesn't list makes the job a
   bad job (exit 2).

   ```toml
   [budget.purposes]
   jobs = 40
   evals = 60
   ```

   ```toml
   purpose = "jobs"   # in the job file
   ```

5. Optional: let a run's children spend from its budget. A child that names a run in progress as its parent
   (`parent_run` in the job file, or `ws run child --parent RUN_ID`) is checked against what that run reserved, less what
   its other children hold or have spent, and also against the monthly and purpose caps like any run, so a parent never
   raises either. It shrinks to the room left (or is refused with `strict = true`), and is refused if the parent has
   finished or is itself a child (one level only). While both are live the same dollars count twice; that's
   deliberate, to stay on the safe side.

6. Optional: tell the queue about memory you have promised elsewhere. Memory for a VM, a container or a service that
   hasn't taken it yet is still in MemAvailable. Name it, and it's taken off before the check:

   ```toml
   [roles.queue]
   memory_reserved_gb = 6
   ```

7. Optional: hear about a job that stops. With a `[roles.notify]` adapter set up, a run that's refused, goes over a cap,
   times out waiting for a slot or fails sends one short line. A retry sends nothing, and no notify role means no line.

8. Check the cap without starting a run.

   ```sh
   ws run daily --dry-run
   ```

   The output includes `month_spend_usd`, `monthly_cap_usd` and `would_refuse`.

9. Check the setting with the doctor.

   ```sh
   ws doctor
   ```

10. Read the records that add up to the spend.

    ```sh
    ws runs --last 5
    ```

## Success

Step 8 prints `"monthly_cap_usd": 20.0` and `"would_refuse": null`. WS103 shows `ok` with `monthly cap US$20`.

Without a cap, step 8 prints `"would_refuse": "no monthly budget cap (WS103): ..."`, and `ws run daily` exits with
code 3.

## If a run is refused

`ws run` exits 3 and writes a record with status `refused`. The reason is on standard error, and the record's
`stop_reason` names the counter (`refused:profile_cap`, `refused:purpose:jobs`, `refused:memory` and others). It's one of:
no cap, spend plus the runs in progress at the cap, a purpose at its own cap, a slot or memory that's full, a parent
that has finished or has no budget left for the child (`refused:parent_not_running`, `refused:parent_budget`), a Rule of
Two refusal, a model the runner can't price, a deadline that has passed, or a queue database that's locked or full (a run
never starts without a reservation). For the unpriced model, set `allow_unpriced = true` in the job
file to run anyway. The run then counts at your fallback price.

## Limits

A run is capped during the run only where the runner has a limit of its own: Claude Code is passed `--max-budget-usd`
when the reservation was set by `budget_usd` or shrank; OpenCode has no such flag, so there the reservation holds the money
and the settle, from what the run used, corrects it. A run can still spend what its `timeout_s` and `max_turns` allow.
Runs outside `ws run` aren't in the log. See [The budget](../explanation/budget.md) and [the queue](../reference/queue.md).
