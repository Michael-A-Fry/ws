# Stores

Every log and record file ws writes: where it's kept, which module writes it, what a line holds, its `schema` and how long
it lives. `tests/test_stores.py` fails when a module under `src/ws` writes a JSON-lines file that this page doesn't
list.

## One convention

- Every line of a JSON-lines log carries `schema`, a whole number that's one per log, and `ts`, the time the line was
  written: RFC 3339, UTC, with a `Z` (`2026-11-02T01:02:03.456Z`). A line with no `schema` is schema 1.
- `at` is the older spelling of `ts`. The logs that used it (`gate-calls`, `claims`, `wall-report`) write both for one
  release, with the same moment in each. Read a line with `ws.jsonl.when(rec)`: it takes `ts`, then `at`.
- Within one schema number a log only gains fields. A change that removes or reshapes a field takes a new number.
- Every log is written through `ws.jsonl`: one `write()` under a lock (`name.lock` beside the log), a text field cut
  at 16 KiB and marked, files private to the owner (0600 in a 0700 folder), no write through a symlink.
- A log rotates when the next line would take it past 5 MiB: the live file becomes `name.jsonl.1`, the older ones shift
  up, and the newest two old files stay. A reader spans the live file and every old one.
- A log has no day files: each is one live file. Where a reader needs a day, it cuts on the UTC date of
  `ts`.

## JSON-lines logs

| Log | Path under `$WS_HOME` | Writer | Fields | Schema | Time key | Kept |
|---|---|---|---|---|---|---|
| Run records | `logs/runs.jsonl` | `src/ws/runrecord.py` (for `ws run`) | The keys of the run-record schema (`ws.runschema.SCHEMA`): job, runner, models, tokens, cost, status | 2 | `started_at`, `ended_at` | Rotates at 5 MiB; an old file that holds a record from this or last month stays |
| Run records, fallback | `logs/runs.fallback.jsonl` | `src/ws/runrecord.py` | The same record, when `runs.jsonl` can't be written | 2 | `started_at`, `ended_at` | Never rotated or locked; `read` includes it |
| Ratings | `ratings.jsonl` | `src/ws/rating.py` | `schema`, `ts`, `run_id`, `rating` (`up` or `down`), `note`, `who` | 1 | `ts` | Rotates at 5 MiB; old files and the lock sit in `logs/` |
| Canary | `logs/canary.jsonl` | `src/ws/canary.py` | `schema`, `ts`, `ws_version`, `ok`, `model`, `runners` | 1 | `ts` | Rotates at 5 MiB |
| Eval results | `eval/results.jsonl` | `src/ws/eval.py` | `schema`, `ts`, `batch`, `task`, `task_hash`, `family`, `kind`, `trial`, `arm`, `job`, `job_hash`, `run_id`, `status`, `pass`, `grade`, `note`, `folder` | 1 | `ts` | Rotates at 5 MiB |
| Eval looks | `eval/looks.jsonl` | `src/ws/eval.py` | `schema`, `ts`, `pair`, `look`, `result` | 1 | `ts` | Rotates at 5 MiB |
| Gate calls | `logs/gate-calls.jsonl` | `src/ws/guards/record_calls.py` | `schema`, `ts`, `at`, `runner`, `payload` | 1 | `ts`, `at` | Rotates at 5 MiB |
| Claims | `logs/claims.jsonl` | `src/ws/guards/claims.py` | `schema`, `ts`, `at`, `runner`, `session`, `ref`, `claims` | 1 | `ts`, `at` | Rotates at 5 MiB |
| Wall report | `logs/wall-report.jsonl` | `src/ws/sandbox.py` | `schema`, `ts`, `at`, `project`, `runner`, `incomplete`, `findings`, `notes` | 1 | `ts`, `at` | Rotates at 5 MiB |
| Broker audit | `logs/broker.jsonl` | `src/ws/broker.py` | `t`, `op`, `name`, `purpose`, `host`, `uid`, `pid`, `lease`, `status`, `bytes` | none (reads as 1) | `t` | One file; the broker appends and never rotates it |

Notes:

- Run records keep `started_at` and `ended_at`. The run-record schema lists its keys and allows no others, so a `ts`
  there is the next schema number.
- The broker audit log predates the convention. It's the one log that doesn't go through `ws.jsonl`, and it has no
  `schema`. Its time key is `t`. A change to it takes a schema number of its own.
- Ratings sit at the top of `$WS_HOME` and not in `logs/`. The top holds the control files a session may not write,
  and the person's verdicts sit with them, so an agent can't mark its own work. `logs/` is where a session may be able
  to write.
- The canary log used to be `canary.jsonl` at the top of `$WS_HOME`. ws renames that file to `canary.jsonl.old` and
  starts `logs/canary.jsonl` empty.

## Record files

One JSON file per record, written to a temporary name and renamed into place (0600 in a 0700 folder).

| Record | Path under `$WS_HOME` | Writer | Fields | Schema | Time key | Kept |
|---|---|---|---|---|---|---|
| Session | `sessions/<id>.json` | `src/ws/session.py` | `schema`, `ts`, `session_id`, `release`, `started_at`, `runner`, `cwd` | 1 | `ts` | Removed 30 days after its last change |
| Approval, pending and granted | `approvals/pending/<id>.json`, `approvals/granted/<id>.json` | `src/ws/approvals.py` | `schema`, `v`, `ts`, `id`, `bound`, `created`, `granted` | 1 | `ts` | Expires 10 minutes after it's made; a granted one is deleted when it's used |
| Seen versions | `versions.json` | `src/ws/canary.py` | `schema`, `runners`, `default_model` | 1 | none | Rewritten when a passing canary sees a change |

`v` in an approval is the older spelling of `schema`, kept for one release. `created` and `granted` are Unix seconds.

## Where the work folders go

A job with no `cwd` runs in `$XDG_STATE_HOME/ws/work/<run_id>` (default `~/.local/state/ws/work/<run_id>`), outside
`$WS_HOME` because the wall makes `$WS_HOME` read-only. `ws uninstall` removes the empty run folders there, then
`work/` and `ws/` once they're empty. A run folder that holds a file stays, with the folders above it.
