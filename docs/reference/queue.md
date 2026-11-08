# The queue

Admission before work: nothing starts until its money, turns, time, a slot and memory are reserved, and every run settles
from what it used. `src/ws/queue.py` is the code; this page is the contract. The reasons are in
[decision 0022](../decisions/0022-reserve-before-a-run.md).

## Commands

| Command | What it does |
|---|---|
| `ws queue add JOB [--key K] [--deadline T]` | Queue a run of `jobs/JOB.toml`. The same `--key` again queues nothing new. |
| `ws queue list [--all] [--last N]` | The receipts: what waits or runs (`--all` adds the finished ones). |
| `ws queue show RUN_ID` | One receipt. |
| `ws queue cancel RUN_ID` | Take a queued run out of the queue. A running one is stopped with SIGTERM to its `ws run`. |
| `ws queue run [--max N]` | Run what is queued, one after another. This is what a timer runs. |
| `ws queue slot --ram-gb N --lane ui --who join\|writer -- CMD...` | Wait for a slot, run `CMD` in it, give the slot back. Raw: the command's output and exit code are its own. |
| `ws node join\|show\|remove` | The node's footprint ledger. See [Nodes](#nodes). |
| `ws usage statusline` | The statusline command that stashes the subscription limits. See [Subscription limits](#subscription-limits). |

`add`, `cancel`, `run` and `ws node join|remove` are refused in a gated session (`WS_GATED_SESSION` set). `list`,
`show` and `slot` are not. No command settles or frees a reservation: that is done by the run that holds it, or by the
reaper when the run is gone.

## What is reserved

Every run, from `ws run`, `ws eval` or the queue, goes through one transaction (`Ledger.admit`):

| Counter | Reserved | Says |
|---|---|---|
| `profile_cap` | the job's estimate, in US$ | refuse: room is month's cap minus the month's records minus open reservations |
| `purpose:NAME` | the same estimate | refuse at the purpose's own cap in `[budget.purposes]`, whatever room the others have |
| `turns` | `max_turns` (default 30) | wait: over `max_open_turns` in flight; refuse if the run alone is over |
| `time` | `timeout_s` | wait: over `max_open_seconds` in flight |
| `concurrency`, `lane:NAME` | one slot | wait: `max_concurrent` runs at once, and `ui_slots` in the `ui` lane |
| `memory` | `resources.ram_gb` | wait: MemAvailable must stay at or above `reserve_pct` of the total after it |
| `job_running` | one run of a job at a time | wait |
| `subscription_*` | none | refuse a job on a login when a fresh capture shows a window used up |

A direct `ws run` that would wait is refused (exit 3, `refused:<counter>`). A queued run stays queued and the next drain
tries again. A system that cannot report memory admits the run, and the reservation says so.

**Resize.** If a money counter has room, but less than the estimate, the reservation shrinks to the room (rounded
down to 0.0001) and `resized` is true. That sum is passed to Claude Code as `--max-budget-usd`. A job with
`strict = true` is refused instead. A reservation that is only the default estimate, with room to spare, passes no flag.
OpenCode has no such flag: the reservation still holds the money, and the settle corrects it from the actuals.

**Windows.** A month is the UTC calendar month (`policy.budget_month`). The reservation's month comes from the one
clock reading of the admission, the run record's `started_at` is that reading, and `runrecord.month_spend` counts by
the same text, so the cap, the purpose caps and the reservations agree at a month boundary.

**Spend counted once.** Spend is the month's records in `runs.jsonl` plus the open reservations. A settled reservation
is not counted again, because its record is. A run whose record could not be written is counted from its receipt until
a reaper writes it. A run with no reported cost counts at `runrecord.record_cost`'s fallback, in the reservation's
settle as in the cap.

## Settling and stop reasons

A run settles once, after its record is written, with the cost that record counts, its tokens and its `stop_reason`:
`completed`, `timeout`, `preempted` (SIGTERM, exit 143), `interrupted` (Ctrl-C), `runner_error`, `broker_unavailable`,
`not_found`, `crashed` (reaped), `expired`, or `refused:<counter>`. Settling again changes nothing. A reservation is
bound to the claim that made it; a settle with another claim is refused.

A reservation whose owner is gone is reaped by the next admission: the owner is gone when its process id is not
running, or is running with a different start time (the id was reused). Where the system cannot tell, the lease decides
(the job's timeout plus two minutes). The reaper writes the run's record if it never got to, at the fallback cost,
and frees the reservation.

## Retries

| Outcome | What the queue does |
|---|---|
| exit 0 | done |
| exit 3 (refused, expired), exit 2 (bad job) | failed; never retried |
| 143 (`preempted`) | settled at its actuals; requeued after 30 s if the job is `preemptible` and its deadline allows; otherwise failed |
| `timeout`, `broker_unavailable`, `interrupted` | retried after 30 s, 1 m, 5 m (three tries); then failed |
| anything else | permanent; failed |
| waited | stays queued, tried again at the next drain |

The class is chosen from the exit code and the typed stop reason, never from message text. A deadline (`deadline` in the
job file or `--deadline`) in the past ends a run as `expired` without running it; a retry that would start after it is
not made.

## Slots and fairness

`ws queue slot` asks for the slot until it is granted, and is never refused for being full. A `join`'s run in a lane is
admitted before any `writer`'s; a `writer`'s only when its memory fits beside `join_headroom_gb`, kept free for a join.
Waiters are ordered by who, then by when they first asked. A request that could never fit on the host (more memory than
the host has beside the reserve) is refused.

## Settings

Defaults are constants in `queue.py`; `[roles.queue]` in the profile overrides them:

| Key | Default | Meaning |
|---|---|---|
| `max_concurrent` | 4 | runs at once |
| `ui_slots` | 1 | runs at once in the `ui` lane |
| `max_open_turns` | 2000 | turns reserved by runs in flight |
| `max_open_seconds` | 86400 | seconds reserved by runs in flight |
| `default_turns` | 30 | turns reserved for a job with no `max_turns` |
| `reserve_pct` | 15 | share of total memory that must stay available |
| `join_headroom_gb` | 2 | memory kept beside a writer's run for a join's |
| `ramp_s` | 60 | how long a new run's declared memory counts on top of MemAvailable |
| `busy_s` | 5 | how long to wait for the store's lock before refusing |
| `poll_s` | 5 | seconds between looks while waiting for a slot |

A wrong key or value is an error that names it (exit 2).

## Job keys

`resources` (`ram_gb`, `slot`), `preemptible`, `grace_s`, `checkpoint`, `deadline`, `purpose`, `budget_usd`, `strict`.
They are described in the `ws.jobs` docstring. `purpose` must be a name in `[budget.purposes]` of the policy or the
job is a bad job (exit 2).

A queued job must use an API-key credential through `[[secrets]]` or a local provider. One that would use a subscription
login is refused (`subscription_login`, exit 3).

## Subscription limits

Claude Code (2.1.80 and later) pipes `rate_limits.five_hour` and `rate_limits.seven_day` to its statusline command. `ws
usage statusline` reads that JSON on stdin and stashes it at `usage/rate_limits.json`, or writes a tombstone when the
payload has none. Admission reads it for a job that uses a login. A capture older than 600 s, a tombstone, a window past
its reset time and a figure that is not a percentage all read as unknown, never as room. ws never calls the provider's
OAuth usage endpoint with a login. Putting the command in your Claude Code settings is yours to do; ws does not yet.

## Nodes

`ws node join` writes the queue folder, a node record and a job that runs `ws queue run` every N minutes (5 to 30), and
with `--timer` on systemd the timer for it. Before each change it writes an `intent` line to the ledger
(`node/footprint.jsonl`, `type` `ws.footprint/1`), and a `done` line after. `ws node remove` undoes the changes newest
first: a file only if it still holds what ws wrote, a folder only if ws made it and it is empty, a timer by switching it
off. What it cannot undo is listed and the exit is 1; run it again after you have looked. When nothing is left the ledger
goes too.

## The engine

`ws._vendor.sqlqueue` is the queue engine under all of this: claim ids that fence a stale holder, idempotency keys,
`retry_expired` as a lease and a maximum size. Its docstring is its API.
