# ws eval: Your own eval

`ws eval` tells you whether a change to your setup (a model, a skill, a rule, a job) helps on tasks that look like your
own work. It ships the engine, the file format, ten metrics and eight synthetic starter tasks. Your own tasks come from
your own runs through `ws eval harvest`.

Every trial is one `ws run`, so the cap, the Rule of Two, the gate and the run record all apply as they do for any job.
The monthly cap is checked before each trial, not during it: keep `timeout_s` and `max_turns` tight.

## Write a task

One TOML file per task in `~/.local/share/ws/eval/tasks/`, named after its id. The file must belong to you and not be
writable by others, like a job file. A file is used whole or refused: an unknown key, a bad grader or a path outside the
task folder rejects it, and one bad file stops the whole set from loading.

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

[[grader]]
type = "json_path_equals"
path = "answer.json"
json_path = "total"
equals = 14.75
```

A `[hidden.files]` table works like `[setup.files]` but its files are written after the run, over whatever is at those
paths, just before grading: the agent never sees them. Setup and hidden paths may not collide (`x` and `x/y`).

Graders (all must pass; up to five per task):

| type | keys | passes when |
|---|---|---|
| `command` | `command`, `timeout_s` (default 60) | the command exits 0 in the wall AND prints the trial's nonce (below) |
| `file_contains` | `path`, `text`, `ignore_case`, `negate` | the file has the text (or lacks it, with `negate`) |
| `json_path_equals` | `path`, `json_path` (like `a.b.0`), `equals` | the value at that path equals `equals` |
| `regex` | `path`, `pattern` (up to 500 characters), `ignore_case`, `negate` | the pattern matches in the file (multi-line) |
| `rubric` | `rubric` | never yet: a model-judged grader is accepted in the file but refused at run time |

Files are read only if they're regular files (no pipe, device or socket), and at most 1 MiB of each is read.

A rubric task runs only if the profile names a judge model (`[eval] judge_model`), and the judge call isn't built yet,
so for now a task with a rubric grader is skipped. Use a deterministic grader. A trial passes only if the run finished
`ok` and every grader passed.

### Command graders run the agent's code, so they run in the wall

A `command` grader usually imports or runs what the agent wrote. It runs inside ws's own sandbox (`ws.sandbox`): the
whole disk read-only, the trial folder included, Unix sockets shut. The wall doesn't close the network. With no sandbox
on the machine (no bubblewrap on Linux) `ws eval run` refuses a set that has a command grader, with the install hint;
there is no unsandboxed fallback, and `--dry-run` shows "would be refused." Tasks with only file graders need no sandbox.

An exit code alone proves nothing, because agent code can end the checker early (`os._exit(0)`). So ws sends each
trial a random nonce on the command's stdin, and the grade needs the command to exit 0 AND print that nonce as a line.
Write the checker like this, as a hidden file, and run it with `python -I` so the agent can't shadow the modules it
imports:

```toml
[hidden.files]
"check.py" = '''
import ctypes, json, subprocess, sys
if sys.platform.startswith("linux"):           # first: nobody else may read this process's memory
    ok = ctypes.CDLL(None).prctl(4, 0, 0, 0, 0) == 0     # PR_SET_DUMPABLE = 0
elif sys.platform == "darwin":
    ok = ctypes.CDLL(None).ptrace(31, 0, 0, 0) == 0      # PT_DENY_ATTACH
else:
    ok = False
if not ok:
    sys.exit(97)                               # ws fails the grade with that reason
nonce = sys.stdin.readline().strip()           # only then read the nonce, before any agent code runs
code = "from fizzbuzz import fizzbuzz; import json; print(json.dumps(fizzbuzz(15)))"
r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30,
                   stdin=subprocess.DEVNULL)   # agent code only ever runs in a child
assert r.returncode == 0, r.stderr[-300:]
assert json.loads(r.stdout.splitlines()[-1])[2] == "Fizz"
print(nonce)                                   # last: only if every check passed
'''

[[grader]]
type = "command"
command = "{python} -I check.py"
```

The child has no stdin and no environment variable with the nonce, so it never sees it. The nonce does sit in the
checker's memory, and inside the wall a child could read `/proc/<parent>/mem` (the wall's seccomp filter allows `ptrace`
and `process_vm_readv`, and `ptrace_scope` is often 0). That's why the checker first makes itself non-dumpable
(`PR_SET_DUMPABLE`, or `PT_DENY_ATTACH` on macOS) and exits 97 if it can't, which ws reports as "couldn't make its
memory unreadable" and fails the grade (a platform with neither call fails closed). A test checks that a child can't
read the nonce on Linux. A checker that prints the nonce before it has checked anything defeats the scheme. The starter
tasks that use a command grader follow this pattern (see `CHECK_PRELUDE` in `src/ws/eval.py`).

Regex graders run the match in a child Python with a 5 s limit and kill it on overrun (a thread can't be stopped and `re`
can't be interrupted, so a pattern like `(a+)+$` would otherwise hang ws).

Known limits of the grader's wall:

- It has the same network and read access as the runner's wall (it's the same `ws.sandbox` wall), so it gives the agent's
  code nothing it didn't already have in the trial; it doesn't close the network.
- `~/.local/state/ws/approvals/pending` and `~/.local/state/ws/sessions` stay writable inside it. That's by design and
  harmless: a forged pending record grants nothing (see `ws.approvals`).
- A grader run as root gets no nonce protection (root has CAP_SYS_PTRACE, which `PR_SET_DUMPABLE` doesn't stop), so
  don't run evals as root.

## Run

```sh
ws eval run --set starter --dry-run                       # the plan and the number of runs; spends nothing
ws eval run --set starter --arm base --job daily          # the job is the arm under test
ws eval run --set mine --arm tighter --job daily-tight    # the same tasks under another job
ws eval run --task invoice-total --job daily --trials 5
```

The job file supplies the runner, model, options and secrets; the task replaces its prompt, folder, timeout and turn
limit. Folders are made in the system temp folder (not in one of ws's folders, where the sandbox denies shell writes); a
trial that passes is cleaned up, a failed one is kept and its path printed (`--keep` keeps all). Each trial writes a run
record with `eval: {task, trial, arm, pass}` and a line in `~/.local/share/ws/eval/results.jsonl`. The record is written
first, with `pass` null; grading follows, and the result line holds the verdict, the job, and a hash of the task text. A
signal during grading leaves the record and a result line that says grading was cut short. A refused run (no cap, cap
reached) stops the batch.

## Read the report

```sh
ws eval report [--arm NAME] [--since 90d] [--k N] [--json]
```

Ten metrics. One with no data says `not measured` and why; nothing is filled in.

1. Success rate: trials passed over trials run, with a 95% interval. The interval is over tasks, not trials: a task's
   repeats are one cluster, so running the same task more times makes its own rate surer but never makes the arm look
   like it was tested on more tasks.
2. pass^k: the chance that k trials of a task all pass (k defaults to the fewest trials any task has, at most 5). It
   comes with pass@k beside it: the chance that at least one of k trials passes. pass@k is never lower; the distance
   between them is how much a task flips between runs.
3. Harm per 100 runs: refused runs and Rule of Two refusals. Reported alone, never averaged into another score.
4. Rework within 14 days: for tasks that name a `repo` (and optionally `files`), the share of solved trials followed by
   a `fix`, `fixup!` or `Revert` commit on those files within 14 days. Trials under 14 days old aren't judged yet.
5. Tokens per solved task: all tokens of all trials with counts, over the trials solved (failures cost too), by type.
6. Cost per solved task: `runrecord.record_cost`. ws has no price table; a trial with no reported cost counts at your
   `[roles.budget]` fallback and the report says how many did.
7. Time per solved task, p50 and p90. Time to first output isn't in the run record, so it reads `not measured`.
8. Attention minutes and interventions: ws records neither for headless runs, so `not measured`.
9. Quality flag: thumbs-down share of the trials you rated with `ws rate`.
10. Maintenance minutes: commits with a `maintenance` subject in the repos your tasks name; minutes are counted only
    where the subject says them, like `maintenance: rotate keys (20 min)`.

## Compare two arms

```sh
ws eval compare base tighter [--look 1|2] [--allow-mixed]
```

Pairs at task level and runs an exact two-sided McNemar test on the tasks where the arms differ. Trials of one task
share a prompt and a grader, so they're not independent draws; counting them as pairs would overstate the evidence. For
each arm and task, a task passes when more than half of its trials do (a tie, like 1 of 2, fails). Only results from the
job each arm ran last (its name and the hash of its file and prompt), and from the version of the task (its hash) it
ran last, are used, and a task whose text differs
between the two arms isn't paired; the output says how many results were left out. It prints the tasks and the trials
behind them side by side. If the two arms ran different numbers of trials of a task, the comparison is refused (and uses
no look): run both arms with the same `--trials`.

A win needs 6 of 6, 8 of 9, 9 of 10 or 10 of 12 discordant tasks, whatever the size of the gap. The rule is success
first, then a quality veto (the arm with the worse thumbs-down rate by 3 points or more can't win, but only once each
arm has at least 5 ratings; otherwise it says "too few ratings" and doesn't veto), then tokens per solved task among
equals (within 3% is level). If the baseline already passes more than 85%, fix the task set first.

The line that matters says, in plain words, who leads and by how much, the interval, and what it means (it's one line
on screen; it's wrapped here):

```text
base leads new by 30.0 points on 20 paired tasks (95% interval +2.4 to +30.0 points): the difference is real at 95%.
new leads base by 10.0 points on 20 paired tasks (95% interval -12.2 to +19.7 points):
  too few runs to tell (MDE 30.0 points).
```

What each number is:

- **Points**: the leader's passed tasks minus the other arm's, over the paired tasks, in percentage points.
- **95% interval**: where the true gap plausibly lies. It's an exact interval worked from the tasks the arms split on,
  so it excludes zero exactly when the McNemar p-value is under 0.05: the interval and the verdict can't disagree.
  With no split at all it shows the most a hidden gap could be.
- **The difference is real at 95%**: the exact McNemar p-value is under 0.05. This is the only wording for a win.
- **Too few runs to tell**: it isn't. It doesn't say the arms are equal, only that this many tasks can't separate them.
- **MDE** (minimum detectable effect): the smallest lead the test could have called real with this many tasks and this
  many splits. A lead under the MDE is too few runs to tell, by construction. With under 6 tasks no lead can be real,
  and the line says so. To shrink the MDE, add tasks (repeats of a task don't help).
- **Each arm's own interval**: the Wilson 95% interval of its share of tasks passed.
- **pass^k and pass@k**: per arm, when every task has two or more trials.

### How each arm was run (the fingerprint)

Every result line records a fingerprint of the trial: the models the runner actually used (from the run record's
`models_used`, not the one the job asked for), the runner and its version, the ws version, a hash of the policy in
force, a hash of the guards set, a hash of the job file and prompt, the job's limits (time and turns) and the job's
`bare` setting. `compare` pools an arm's results only if they agree: if they differ inside one arm (the model was
swapped part-way, ws was updated, the policy or guards changed), it refuses with a typed error that names the fields
and uses no look. Pass `--allow-mixed` to pool them anyway; the output then says so. A field the run didn't record
(a runner that doesn't report its model, a trial that never started) is never a difference. Results written before
fingerprints existed carry none; they're compared as before and the output counts them.

A job with `bare = true` (the default) runs the runner in its bare mode, which skips hooks. The output flags an arm
that ran that way, because the gate's hook didn't run for it. Set `bare = false` in the job when the gate is part of
what you're testing. `report` shows the same notes.

Each pair of arms gets two looks (kept in `looks.jsonl` under the two names as a JSON array). The first two `compare`
calls are stored; a third is refused, because peeking inflates the
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
trials aren't used. Each proposal is written to `~/.local/share/ws/eval/proposed/<id>.toml` with `approved = false`,
`review_required = true`, `source_run`, `source_job`, a placeholder rubric grader (so it can't run until you give it a
real grader), and the job's prompt. A run record doesn't keep its prompt, so the prompt comes from the job file as it's
now, which may differ from what the run used; `prompt_source` says so. A malformed run id or a proposal that's not a
valid task stops the harvest with an error, and the proposals are written all or none.

`ws eval approve ID` shows the prompt and asks you in the terminal; `--yes` skips the question for scripts (with `--json`
or no terminal, `--yes` is required). Harvest never approves anything.

ws has no prompt scrubber, so harvest refuses to copy a prompt that names a secret in your registry or looks like a key,
token, password, URL with a password, private key or long base64 string, and says which job it skipped. It checks names
and shapes, not secret values: the store belongs to the broker and the registry offers no check that avoids reading
them. That check catches shapes, not meaning: read each proposal before you approve it. Confidential or client data in
a prompt is for you to catch.

Files: tasks in `~/.local/share/ws/eval/tasks/`, proposals in `proposed/`, set-aside ones in `rejected/`, trial results
in `results.jsonl`, stored looks in `looks.jsonl`.
