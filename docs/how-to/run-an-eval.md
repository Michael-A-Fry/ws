# Run an eval

Find out whether a change to your setup (a model, a skill, a rule, a job) helps on tasks that look like your own work.
The [eval reference](../reference/eval.md) has the task format, the graders and the ten metrics.

## Before you start

- A monthly cap ([Set a budget](set-a-budget.md)). Every trial is a `ws run`, so the cap applies to each one.
- A job file for the setup you want to test, in `$WS_HOME/jobs/`. Set tight `timeout_s` and `max_turns`.
- For tasks with a `command` grader: bubblewrap on Linux ([Use the wall](use-the-wall.md)). Without a sandbox, `ws eval run`
  refuses those tasks.
- A normal user. Don't run evals as root.

## Steps

1. See the plan and the number of runs. Nothing starts and nothing is spent.

   ```sh
   ws eval run --set starter --job daily --arm base --dry-run
   ```

   The starter set has eight synthetic tasks (`starter-fizzbuzz`, `starter-csv-total` and others). They hold no real data.

2. Run a task, or the whole set, against your job. The job is the arm under test.

   ```sh
   ws eval run --task starter-fizzbuzz --job daily --arm base
   ws eval run --set starter --job daily --arm base
   ```

   A passed trial's folder is cleaned up. A failed trial's folder is kept and its path is printed. `--keep` keeps all.

3. Read the report. Ten metrics. One with no data says `not measured` and why.

   ```sh
   ws eval report --arm base
   ```

4. Change one thing (a different model, a tighter prompt), save it as another job, and run the same tasks as another arm.

   ```sh
   ws eval run --set starter --job daily-tight --arm tighter
   ```

5. Compare the two arms. Run both with the same `--trials`.

   ```sh
   ws eval compare base tighter
   ```

   Each pair of arms gets two looks. A third `compare` is refused. `--look 1` or `--look 2` shows a stored look again.

6. Build your own set from your own runs. Harvest proposes tasks from runs that failed, timed out, were rated down or were
   retried. It approves nothing.

   ```sh
   ws eval harvest --since 30d
   ws eval approve ID
   ws eval reject ID
   ```

   `ws eval approve` shows the prompt and asks. Read it first: the check catches key shapes, not meaning.

## Success

Step 1 prints the plan and the run count. Step 3 prints ten metrics with a success rate and pass^k. Step 5 prints the tasks
and trials side by side, and a verdict. A win needs 6 of 6, 8 of 9, 9 of 10 or 10 of 12 discordant tasks.

## If it doesn't work

- *refused: no cap*: see [Set a budget](set-a-budget.md).
- *no sandbox*: install bubblewrap, or use tasks with file graders only.
- *the file is writable by others*: tasks and jobs must belong to you. Run `chmod 600` on the file.
- A refused run (no cap, cap reached) stops the whole batch.
