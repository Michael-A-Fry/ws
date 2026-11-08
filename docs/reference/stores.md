# Stores

Every log and record file ws writes: where it's kept, which module writes it, what a line holds, its `schema` and how long
it lives. `tests/test_stores.py` fails when a module under `src/ws` writes a JSON-lines file that this page doesn't
list.

## Where ws keeps its files

ws keeps its files in four folders that follow the XDG base directory rules. The defaults are the same on macOS and
Linux, and a relative `XDG_*` value is ignored. Every path on this page is given under one of them.

| Folder | Default | Holds |
|---|---|---|
| Config | `$XDG_CONFIG_HOME/ws` (`~/.config/ws`) | What you write: `policy.toml`, `profile.toml`, `secrets.toml` (the registry), `jobs/`, `release`. |
| State | `$XDG_STATE_HOME/ws` (`~/.local/state/ws`) | What ws writes as it works: `logs/`, `runs/`, `approvals/`, `sessions/`, `gate/`, `wall/`, `backups/`, `run/`, `ratings.jsonl`, `versions.json`, `install-manifest.json`, `migrate-manifest.json`. |
| Data | `$XDG_DATA_HOME/ws` (`~/.local/share/ws`) | What you'd miss: the secrets store (`secrets/`) and the eval tasks and results (`eval/`). |
| Cache | `$XDG_CACHE_HOME/ws` (`~/.cache/ws`) | What ws can rebuild: the memory search index (`index/`). |

Set `WS_HOME` and all four live in that one folder, laid out the way ws always did (`policy.toml`, `logs/` and
`secrets/` side by side). Set `WS_CONFIG_DIR`, `WS_STATE_DIR`, `WS_DATA_DIR` or `WS_CACHE_DIR` to a full path to move one
folder; the most specific wins. `ws config` shows the folders in use and where each came from.

Files from an earlier ws are in `~/.ws`. Nothing moves by itself: while that folder exists and the new ones hold none
of ws's files, ws keeps using it and `ws doctor` (WS010) says to run [`ws migrate`](cli.md#ws-migrate). With both, ws
stops with an error that names the two places, because it can't tell which one holds the truth. The wall makes all four
folders read-only, and the gate refuses a session's writes to each of them in either layout.

## One convention

- Every line of a JSON-lines log carries `schema`, a whole number that's one per log, and `ts`, the time the line was
  written: RFC 3339, UTC, with a `Z` (`2026-11-02T01:02:03.456Z`). A line with no `schema` is schema 1.
- `at` is the older spelling of `ts`. The logs that used it (`gate-calls`, `claims`, `wall-report`) write both for one
  release. One clock read feeds both, so they're the same moment: `at` to the second, `ts` to the millisecond.
  Read a line with `ws.jsonl.when(rec)`: it takes `ts`, then `at`.
- Within one schema number a log only gains fields. A change that removes or reshapes a field takes a new number.
- `ws.jsonl` refuses a line that has no whole-number `schema` or no `ts` (the run record's own time key is
  `started_at`), so a writer that forgets the stamp fails its tests.
- Every log is written through `ws.jsonl`: one `write()` under a lock (`name.lock` beside the log), a text field cut
  at 16 KiB and marked, files private to the owner (0600 in a 0700 folder), no write through a symlink.
- A log rotates when the next line would take it past 5 MiB: the live file becomes `name.jsonl.1`, the older ones shift
  up, and the newest two old files stay. A reader spans the live file and every old one.
- A log has no day files: each is one live file. Where a reader needs a day, it cuts on the UTC date of
  `ts`.

## JSON-lines logs

| Log | Path under its folder | Writer | Fields | Schema | Time key | Kept |
|---|---|---|---|---|---|---|
| Run records | `logs/runs.jsonl` | `src/ws/runrecord.py` (for `ws run`) | The keys of the run-record schema (`ws.runschema.SCHEMA`): job, runner, models, tokens, cost, status, `purpose`, `stop_reason`, `reservation` | 3 | `started_at`, `ended_at` | Rotates at 5 MiB; an old file that holds a record from this or last month stays |
| Run records, fallback | `logs/runs.fallback.jsonl` | `src/ws/runrecord.py` | The same record, when `runs.jsonl` can't be written | 3 | `started_at`, `ended_at` | Never rotated or locked; `read` includes it |
| Ratings | `ratings.jsonl` | `src/ws/rating.py` | `schema`, `ts`, `run_id`, `rating` (`up` or `down`), `note`, `who` | 1 | `ts` | Rotates at 5 MiB; old files and the lock sit in `logs/` |
| Canary | `logs/canary.jsonl` | `src/ws/canary.py` | `schema`, `ts`, `ws_version`, `ok`, `model`, `runners` | 1 | `ts` | Rotates at 5 MiB |
| Eval results | `eval/results.jsonl` | `src/ws/eval.py` | `schema`, `ts`, `batch`, `task`, `task_hash`, `family`, `kind`, `trial`, `arm`, `job`, `job_hash`, `run_id`, `status`, `pass`, `grade`, `note`, `folder` | 1 | `ts` | Rotates at 5 MiB |
| Eval looks | `eval/looks.jsonl` | `src/ws/eval.py` | `schema`, `ts`, `pair`, `look`, `result` | 1 | `ts` | Rotates at 5 MiB |
| Gate calls | `logs/gate-calls.jsonl` | `src/ws/guards/record_calls.py` | `schema`, `ts`, `at`, `runner`, `payload` | 1 | `ts`, `at` | Rotates at 5 MiB |
| Claims | `logs/claims.jsonl` | `src/ws/guards/claims.py` | `schema`, `ts`, `at`, `runner`, `session`, `ref`, `claims` | 1 | `ts`, `at` | Rotates at 5 MiB |
| Gate verdicts | `logs/gate.jsonl` | `src/ws/gate.py` | `schema`, `ts`, `runner`, `session`, `event`, `decision` (`allow`, `deny`, `ask`, `claim`), `layer`, `guards` (each `guard`, `result`, `ms`), `ms`, `via` (`direct` or `daemon`) | 1 | `ts` | Rotates at 5 MiB |
| Wall report | `logs/wall-report.jsonl` | `src/ws/sandbox.py` | `schema`, `ts`, `at`, `project`, `runner`, `incomplete`, `findings`, `notes` | 1 | `ts`, `at` | Rotates at 5 MiB |
| Node footprint | `node/footprint.jsonl` | `src/ws/node.py` | `schema`, `ts`, `type` (`ws.footprint/1`), `node`, `op` (`intent`, `done`, `removed`), `step`, and on an intent `kind`, `target`, `sha256`, `existed` | 1 | `ts` | Never rotated; removed by `ws node remove` once every change is undone |
| OpenCode asks | `.claude-usage/asks/asks.jsonl` | `src/ws/adapters/opencode.py` (the shim it generates writes the lines, from OpenCode's permission events) | `schema`, `ts` (epoch milliseconds), `event` (`added`, `answered`), `id`, and on `added` `repo`, `session`, `question`, `options`, `decision`, `source` | 1 | `ts` | Each line starts with a newline; past 8 MiB the file moves to `asks.jsonl.old`, one generation, so it stays under the 64 MiB `ws home` reads |
| Broker audit | `logs/broker.jsonl` | `src/ws/broker.py` | `t`, `op`, `name`, `purpose`, `host`, `uid`, `pid`, `lease`, `status`, `bytes` | none (reads as 1) | `t` | One file; the broker appends and never rotates it |

Notes:

- Run records keep `started_at` and `ended_at`. The run-record schema lists its keys and allows no others, so a `ts`
  there is the next schema number.
- Schema 3 of the run record added `purpose`, `stop_reason` and `reservation`. Schema 2 stays frozen (the test pins its
  keys) and its lines read as they are: a reader uses `.get` for the three new keys. `status` gained `preempted` and
  `expired`.
- The node footprint is a ledger, not a log: two lines per change (an `intent` before it, a `done` after), so a crash
  between them is still understood. It is never rotated, because a forgotten change could not be undone.
- The OpenCode asks file is the one log outside the four folders: it sits where `ws home` reads asks from, which is
  `[home.sources] asks_jsonl` in the profile, else `.claude-usage/asks/asks.jsonl` below your home folder. Its `ts` is
  epoch milliseconds, not the RFC 3339 text of the other logs; the reader takes either.
- The broker audit log predates the convention. It's the one log that doesn't go through `ws.jsonl`, and it has no
  `schema`. Its time key is `t`. A change to it takes a schema number of its own.
- Ratings sit at the top of the state folder and not in `logs/`. The top holds the control files a session may not write,
  and the person's verdicts sit with them, so an agent can't mark its own work. `logs/` is where a session may be able
  to write.
- The canary log used to be `canary.jsonl` at the top of the state folder. ws renames that file to `canary.jsonl.old` and
  starts `logs/canary.jsonl` empty.

## Record files

One JSON file per record, written to a temporary name and renamed into place (0600 in a 0700 folder).

| Record | Path under its folder | Writer | Fields | Schema | Time key | Kept |
|---|---|---|---|---|---|---|
| Session | `sessions/<id>.json` | `src/ws/session.py` | `schema`, `ts`, `session_id`, `release`, `started_at`, `runner`, `cwd` | 1 | `ts` | Removed 30 days after its last change |
| Approval, pending and granted | `approvals/pending/<id>.json`, `approvals/granted/<id>.json` | `src/ws/approvals.py` | `schema`, `v`, `ts`, `id`, `bound`, `created`, `granted` | 1 | `ts` (when the file was written) | Expires 10 minutes after it's made; a granted one is deleted when it's used |
| Seen versions | `versions.json` | `src/ws/canary.py` | `schema`, `runners`, `default_model` | 1 | none | Rewritten when a passing canary sees a change |
| Subscription limits | `usage/rate_limits.json` | `src/ws/usage.py` | `schema`, `captured_at` (Unix seconds), `five_hour` and `seven_day` (each `used_percentage`, `resets_at`), or `absent` (a tombstone) | 1 | `captured_at` | Replaced by each statusline call; read as unknown after 600 s |

`v` in an approval is the older spelling of `schema`, kept for one release. `created` and `granted` are Unix seconds.

## Where the work folders go

A job with no `cwd` runs in `$XDG_STATE_HOME/ws-work/<run_id>` (default `~/.local/state/ws-work/<run_id>`), beside ws's
folders and not in one, because the wall makes those read-only. `ws uninstall` removes the empty run folders there, then
`ws-work` once it's empty. A run folder that holds a file stays, with the folder above it. An earlier ws used
`~/.local/state/ws/work`; `ws uninstall` tidies that too.

## The queue database

`queue/queue.db` in the state folder (folder 0700, file 0600, WAL mode; `src/ws/queue.py`) is the one place ws keeps
state in SQLite. It holds the queued work (table `work`, the `ws._vendor.sqlqueue` engine) and a receipt for every run
(table `receipts`): its status (`queued`, `running`, `ok`, `error`, `refused`, `preempted`, `skipped`, `interrupted`,
`expired`), the UTC month it counts in, what was reserved (money, turns, seconds, memory, lane), who owns it (process id
and start time), and what it came to when settled (cost, tokens, stop reason). A database file carries its layout
version in `PRAGMA user_version` (now 1); a later change to the tables takes a new number. The money counted against a
cap is the month's records in `logs/runs.jsonl` plus the open reservations here, never both for one run. See
[the queue](queue.md).
