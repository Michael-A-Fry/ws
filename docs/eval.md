# ws eval: your own eval

`ws eval` tells you whether a change to your setup (a model, a skill, a rule, a job) helps on tasks that look like your
own work. It ships the engine, the file format, ten metrics and eight synthetic starter tasks. Your own tasks come from
your own runs through `ws eval harvest`.

Every trial is one `ws run`, so the cap, the Rule of Two, the gate and the run record all apply as they do for any job.
The monthly cap is checked before each trial, not during it: keep `timeout_s` and `max_turns` tight.

## Write a task

One TOML file per task in `$WS_HOME/eval/tasks/`, named after its id. The file must belong to you and not be writable by
others, like a job file. A file is used whole or refused: an unknown key, a bad grader or a path outside the task folder
rejects it, and one bad file stops the whole set from loading.

```toml
id = "invoice-total"
family = "life-admin"          # coding | life-admin | work-style | research
kind = "regression"            # capability (room to climb) | regression (should stay near 100%)
prompt = "invoices.csv lists this month's invoices. Write the total to answer.json as {\"total\": <number>}."
trials = 3                     # 1 to 5, default 3
timeout_s = 300                # default 300, at most 3600
max_turns = 12                 # optional

[setup.files]                  # written into a fresh temporary folder before the run
"invoices.csv" = '''
id,amount
a,10.50
b,4.25
'''

[hidden.files]                 # written after the run, before grading: the agent never sees these
"check.py" = '''
import json
assert json.load(open("answer.json"))["total"] == 14.75
'''

[[grader]]
type = "command"
command = "{python} check.py"  # exit 0 passes; run in the task folder; {python} is this Python
```

Graders (all must pass; up to five per task):

| type | keys | passes when |
|---|---|---|
| `command` | `command`, `timeout_s` (default 60) | the command exits 0 in the task folder |
| `file_contains` | `path`, `text`, `ignore_case`, `negate` | the file has the text (or lacks it, with `negate`) |
| `json_path_equals` | `path`, `json_path` (like `a.b.0`), `equals` | the value at that path equals `equals` |
| `regex` | `path`, `pattern`, `ignore_case`, `negate` | the pattern matches somewhere in the file (multi-line) |
| `rubric` | `rubric` | never yet: a model-judged grader is accepted in the file but refused at run time |

A rubric task runs only if the profile names a judge model (`[eval] judge_model`), and the judge call is not built yet,
so for now a task with a rubric grader is skipped. Use a deterministic grader. A trial passes only if the run finished
`ok` and every grader passed.

## Run

```sh
ws eval run --set starter --dry-run                       # the plan and the number of runs; spends nothing
ws eval run --set starter --arm base --job daily          # the job is the arm under test
ws eval run --set mine --arm tighter --job daily-tight    # the same tasks under another job
ws eval run --task invoice-total --job daily --trials 5
```

The job file supplies the runner, model, options and secrets; the task replaces its prompt, folder, timeout and turn
limit. Folders are made in the system temp folder (not under `$WS_HOME`, where the sandbox denies shell writes); a
trial that passes is cleaned up, a failed one is kept and its path printed (`--keep` keeps all). Each trial writes a run
record with `eval: {task, trial, arm, pass}` and a line in `$WS_HOME/eval/results.jsonl`. A refused run (no cap, cap
reached) stops the batch.

## Read the report

```sh
ws eval report [--arm NAME] [--since 90d] [--k N] [--json]
```

Ten metrics. One with no data says `not measured` and why; nothing is filled in.

1. Success rate: trials passed over trials run.
2. pass^k: the chance that k trials of a task all pass (k defaults to the fewest trials any task has, at most 5).
3. Harm per 100 runs: refused runs and Rule of Two refusals. Reported alone, never averaged into another score.
4. Rework within 14 days: for tasks that name a `repo` (and optionally `files`), the share of solved trials followed by
   a `fix`, `fixup!` or `Revert` commit on those files within 14 days. Trials under 14 days old are not judged yet.
5. Tokens per solved task: all tokens of all trials with counts, over the trials solved (failures cost too), by type.
6. Cost per solved task: `runrecord.record_cost`. ws has no price table; a trial with no reported cost counts at your
   `[roles.budget]` fallback and the report says how many did.
7. Time per solved task, p50 and p90. Time to first output is not in the run record, so it reads `not measured`.
8. Attention minutes and interventions: ws records neither for headless runs, so `not measured`.
9. Quality flag: thumbs-down share of the trials you rated with `ws rate`.
10. Maintenance minutes: commits with a `maintenance` subject in the repos your tasks name; minutes are counted only
    where the subject says them, like `maintenance: rotate keys (20 min)`.

## Compare two arms

```sh
ws eval compare base tighter [--look 1|2]
```

Pairs trials by task and trial number and runs an exact two-sided McNemar test on the pairs where the arms differ. A
win needs 6 of 6, 8 of 9, 9 of 10 or 10 of 12 discordant pairs. A gap under 3 points is noise. The rule is success
first, then a quality veto (the arm with the worse thumbs-down rate by 3 points or more cannot win), then tokens per
solved task among equals. If the baseline already passes more than 85%, fix the task set first.

Each pair gets two looks. The first two `compare` calls are stored; a third is refused, because peeking inflates the
error rate. `--look 1` or `--look 2` shows that stored look again without using one. For an A/A check, run the same job
under two arm names and compare them.

## Harvest tasks from your own runs

```sh
ws eval harvest [--since 30d]
ws eval approve ID      # moves the proposal into your tasks
ws eval reject ID       # sets it aside; harvest will not offer it again
```

Harvest looks at your runs in the window and proposes one task per job from its most recent run that failed, timed out,
was rated down, or was an `ok` run that followed a failure of the same job within a day (a retry). Refused runs and eval
trials are not used. Each proposal is written to `$WS_HOME/eval/proposed/<id>.toml` with `approved = false`, `source_run`
and `source_job`, the job's prompt, and a placeholder rubric grader, so it cannot run until you give it a real grader.
Harvest never approves anything.

ws has no prompt scrubber, so harvest refuses to copy a prompt that names a secret in your registry or looks like a key,
token, password or private key, and says which job it skipped. That check catches shapes, not meaning: read each proposal
before you approve it. Client or FIU data in a prompt is for you to catch.

Files: tasks in `$WS_HOME/eval/tasks/`, proposals in `proposed/`, set-aside ones in `rejected/`, trial results in
`results.jsonl`, stored looks in `looks.jsonl`.
