# CLI reference

Every `ws` command and flag. This page is generated from the command parsers, so it matches the code. For the task each
command is for, see [Usage](../usage.md).

```text
ws [--json] [-C PATH] [--version] COMMAND ...
```

## Global options

These work on every command, before or after the command name.

| Option | Meaning |
|---|---|
| `--json` | Print one JSON envelope on standard output. `WS_OUTPUT=json` does the same. |
| `-C PATH`, `--cwd PATH` | Run as if started in `PATH`. |
| `--version` | Print `ws X.Y.Z` and exit. |
| `-h`, `--help` | Print help for the command and exit. `ws help COMMAND` does the same. |

A mistyped command or flag gets a "did you mean" hint.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Fine. |
| 1 | Needs doing: a check found something to fix, or a run failed or timed out. |
| 2 | Usage error. `ws run` also uses 2 when the job file can't be read. For `ws gate check`, 2 is a deny. |
| 3 | Refused: `ws run` refused to start a job. |
| 4 | `ws gate check` only: a granted approval is waiting to be claimed (OpenCode wall). |

## JSON envelope

With `--json`, a result is `{"schema", "kind", "status", "data"}` and an error is
`{"schema", "kind", "status": "error", "error_kind", "message", "hint", "exit_code"}`. The schema is `ws.cli/1`. `kind`
is the command, such as `doctor` or `gate.generate`. `status` is `ok`, `attention` (a valid result with exit code 1) or
`error`.

Error kinds: `usage`, `invalid_cwd`, `no_profile`, `bad_config`, `bad_input`, `not_found`, `refused`, `denied`,
`unavailable`, `io`, `unsupported`, `failed`.

A command that speaks a protocol on standard output (`ws mcp serve`, `ws gate check`) or asks you a question
(`ws approve`) keeps its own output, and its errors are text on standard error.

## Environment

| Variable | Meaning |
|---|---|
| `WS_HOME` | Where ws keeps its files. Default `~/.ws`. |
| `WS_OUTPUT` | Set to `json` for `--json` on every command. |
| `WS_BROKER_SOCKET` | The broker socket a client uses. |
| `WS_GATED_SESSION` | Set inside a gated session. `ws approve` refuses when it's present. |
| `XDG_STATE_HOME` | Where run folders for jobs with no `cwd` go, under `ws/work`. Default `~/.local/state`. |

## Commands

`ws plugin` has the alias `ws module`.

## `ws help`

Help for a command: `ws help gate generate` (add `--json` for machines).

```text
ws help [-h] [topic ...]
```

| Argument | Meaning |
|---|---|
| `topic ...` | A command, then its subcommand. |

## `ws doctor`

Check this machine and every role; exit 1 if anything needs doing.

```text
ws doctor [-h] [--offline]
```

| Argument | Meaning |
|---|---|
| `--offline` | Skip checks that call a provider. |

## `ws init`

Answer the three setup questions for this machine.

```text
ws init [-h] [--stays-on yes|no] [--server ADDRESS|none] [--notes PATH] [--create-notes] [-y]
```

| Argument | Meaning |
|---|---|
| `--stays-on yes\|no` | Does this machine stay on all the time? (default no). |
| `--server ADDRESS\|none` | A server to connect to (default none). |
| `--notes PATH` | Your notes folder (default ~/Notes). |
| `--create-notes` | Create the notes folder if it's missing. |
| `-y`, `--yes` | Take the defaults for anything not given; no prompts. |

## `ws gate`

The policy, and the deny rules generated from it for each runner.

### `ws gate init`

Write the commented default policy if there is none.

```text
ws gate init [-h] [--policy FILE]
```

| Argument | Meaning |
|---|---|
| `--policy FILE` | Policy file (default $WS_HOME/policy.toml). |

### `ws gate generate`

Write a runner's native deny rules and hook into a folder.

```text
ws gate generate [-h] [--policy FILE] --runner {claude-code,opencode} --out DIR [--ws-cmd CMD]
```

| Argument | Meaning |
|---|---|
| `--policy FILE` | Policy file (default $WS_HOME/policy.toml). |
| `--runner RUNNER` | The runner to write config for: `claude-code` or `opencode`. One of `claude-code`, `opencode`; required. |
| `--out DIR` | Folder to write (never a runner's own config). Required. |
| `--ws-cmd CMD` | How the hook runs ws (default: ws). |

### `ws gate check`

Hook entry: payload on stdin; exit 0 allow, 2 deny.

```text
ws gate check [-h] [--policy FILE] --runner {claude-code,opencode} [--protect PATH]
```

| Argument | Meaning |
|---|---|
| `--policy FILE` | Policy file (default $WS_HOME/policy.toml). |
| `--runner RUNNER` | The runner whose hook payload is on standard input. One of `claude-code`, `opencode`; required. |
| `--protect PATH` | A file or folder of ws's own that a session may not write. |

### `ws gate launch`

Start a runner inside the sandbox wall (bubblewrap on Linux, sandbox-exec on macOS); fails closed.

```text
ws gate launch [-h] --runner {claude-code,opencode} [--policy FILE] [--protect PATH] [--allow-write
    DIR] [--no-sandbox] ...
```

| Argument | Meaning |
|---|---|
| `--runner RUNNER` | The runner to start: `claude-code` or `opencode`. One of `claude-code`, `opencode`; required. |
| `--policy FILE` | Policy file (default $WS_HOME/policy.toml). |
| `--protect PATH` | Another file or folder to make read-only. |
| `--allow-write DIR` | Another folder the session may write (besides the project). |
| `--no-sandbox` | Start the runner with no wall (says what that gives up). |
| `argv ...` | -- then the runner's command line. |

## `ws secret`

The secrets registry and store (values are never shown).

### `ws secret set`

Store a value for a registered name (prompted, or from stdin).

```text
ws secret set [-h] name
```

| Argument | Meaning |
|---|---|
| `name` | A name registered in `secrets.toml`. |

### `ws secret import`

Store a value for a registered name from an env file (VAR=value).

```text
ws secret import [-h] --from-env PATH [--var VAR] [--delete-source] name
```

| Argument | Meaning |
|---|---|
| `name` | A name registered in `secrets.toml`. |
| `--from-env PATH` | The env file to read; required. |
| `--var VAR` | Which variable, when the file has several lines. |
| `--delete-source` | Remove the env file after the value is stored. |

### `ws secret list`

Names, hosts, set/unset and owner; never values.

```text
ws secret list [-h]
```

### `ws secret rm`

Remove a stored value.

```text
ws secret rm [-h] name
```

| Argument | Meaning |
|---|---|
| `name` | The secret's name. |

### `ws secret wall`

Print the one-time sudo steps that put the store behind its own system user.

```text
ws secret wall [-h]
```

## `ws broker`

The secrets broker: agents ask it to use a key and never see the value.

### `ws broker serve`

Run the broker (a Unix socket and a loopback lease proxy).

```text
ws broker serve [-h] [--socket PATH] [--group GROUP] [--port PORT] [--lease-ttl SECONDS]
```

| Argument | Meaning |
|---|---|
| `--socket PATH` | Default $WS_BROKER_SOCKET, else [roles.secrets] socket in the profile, else $WS_HOME/run/broker.sock. |
| `--group GROUP` | Also serve members of this group (socket folder becomes 0750). |
| `--port PORT` | Lease proxy port on 127.0.0.1 (default: any free one). |
| `--lease-ttl SECONDS` | Longest lease (default 3600). |

### `ws broker request`

Ask for a lease and print it as JSON (for scripts).

```text
ws broker request [-h] --purpose PURPOSE --host HOST [--ttl SECONDS] [--socket PATH] name
```

| Argument | Meaning |
|---|---|
| `name` | The secret's name. |
| `--purpose PURPOSE` | What the lease is for; it must be in the registry entry's `purposes`. Required. |
| `--host HOST` | The host the lease may reach. Required. |
| `--ttl SECONDS` | Ask for a shorter lease. |
| `--socket PATH` | The broker's socket (default: from the profile, else `$WS_HOME/run/broker.sock`). |

### `ws broker status`

Ask a running broker how it is.

```text
ws broker status [-h] [--socket PATH]
```

| Argument | Meaning |
|---|---|
| `--socket PATH` | The broker's socket (default: from the profile, else `$WS_HOME/run/broker.sock`). |

## `ws note`

Capture a note into the _inbox of your notes folder.

```text
ws note [-h] [--title TITLE] [--tag TAG] [text ...]
```

| Argument | Meaning |
|---|---|
| `text ...` | The note, or `-` to read it from stdin. |
| `--title TITLE` | A title (also names the file). |
| `--tag TAG` | A tag; repeat for more. |

## `ws memory`

Remember things in Markdown + git, search them and your notes.

### `ws memory init`

Make the memory folder a git repo (0 changes if it already is).

```text
ws memory init [-h]
```

### `ws memory add`

Write one memory file and commit it.

```text
ws memory add [-h] [--title TITLE] [--kind {fact,decision,preference,person,project}] [--tag TAG]
    text [text ...]
```

| Argument | Meaning |
|---|---|
| `text ...` | What to remember, or `-` to read it from stdin. |
| `--title TITLE` | A title (also names the file). |
| `--kind KIND` | Default fact. One of `fact`, `decision`, `preference`, `person`, `project`. |
| `--tag TAG` | A tag; repeat for more. |

### `ws memory search`

Search memory and notes.

```text
ws memory search [-h] [--limit N] query [query ...]
```

| Argument | Meaning |
|---|---|
| `query ...` | The words to search for in memory and your notes. |
| `--limit N` | Most results (default 10). |

### `ws memory reindex`

Update the search index (it's only a cache).

```text
ws memory reindex [-h] [--full]
```

| Argument | Meaning |
|---|---|
| `--full` | Throw it away and rebuild from the files. |

## `ws mcp`

MCP server so any runner can use ws.

### `ws mcp serve`

Serve memory_search, memory_add and note_capture over stdio.

```text
ws mcp serve [-h]
```

## `ws run`

Run a headless job (a file in $WS_HOME/jobs) and record what it did.

Run a headless job. The monthly cap is checked before the run, not enforced during it.

```text
ws run [-h] [--dry-run] job
```

| Argument | Meaning |
|---|---|
| `job` | The job name: $WS_HOME/jobs/<name>.toml. |
| `--dry-run` | Show what would start, and whether it would be refused. |

## `ws runs`

Show the last records of `ws run`.

```text
ws runs [-h] [--last N]
```

| Argument | Meaning |
|---|---|
| `--last N` | How many (default 10). |

## `ws globals`

Keep what ws installs globally global: no quiet repo copies.

### `ws globals check`

Fail on a repo copy of a global skill, agent, workflow or hook.

```text
ws globals check [-h] [--repo DIR] [--manifest FILE] [--strict]
```

| Argument | Meaning |
|---|---|
| `--repo DIR` | The repo to check (default: the current directory). |
| `--manifest FILE` | Globals manifest, used when there is no user config. |
| `--strict` | Also exit 1 when there was nothing to compare. |

### `ws globals manifest`

Print names and sha256 of this machine's globals (no content) as JSON.

```text
ws globals manifest [-h] [--runner {claude-code,opencode}]
```

| Argument | Meaning |
|---|---|
| `--runner RUNNER` | Only this runner. One of `claude-code`, `opencode`. |

## `ws session`

Record the release a session started on; nudge once if ws has moved on.

### `ws session start`

Hook entry: record this session's release (hook JSON on stdin, or --session-id).

```text
ws session start [-h] [--session-id ID] [--runner RUNNER]
```

| Argument | Meaning |
|---|---|
| `--session-id ID` | The session id (default: session_id in the hook JSON). |
| `--runner RUNNER` | Which runner is calling (recorded). Default `unknown`. |

### `ws session nudge`

Hook entry: print one line, once, if ws is newer than this session's release.

```text
ws session nudge [-h] [--session-id ID] [--runner RUNNER]
```

| Argument | Meaning |
|---|---|
| `--session-id ID` | The session id (default: session_id in the hook JSON). |
| `--runner RUNNER` | Which runner is calling (recorded). Default `unknown`. |

## `ws install`

Set up ws on this machine; run it again and it reports 0 changes.

```text
ws install [-h] [--stays-on yes|no] [--server ADDRESS|none] [--notes PATH] [--module NAME] [--runner
    {claude-code,opencode}] [--model ID] [--cap USD] [-y] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--stays-on yes\|no` | Does this machine stay on all the time?. |
| `--server ADDRESS\|none` | A server to connect to. |
| `--notes PATH` | Your notes folder (default: a vault found, else ~/Notes). |
| `--module NAME` | Also switch on a module; repeat for more. |
| `--runner RUNNER` | The AI tool ws runs jobs with. One of `claude-code`, `opencode`. |
| `--model ID` | The default model. |
| `--cap USD` | Your own monthly spend cap. |
| `-y`, `--yes` | No prompts. |
| `--dry-run` | Show what would change; write nothing. |

## `ws uninstall`

Remove what ws installed; your notes and memory are kept.

```text
ws uninstall [-h] [--dry-run] [-y]
```

| Argument | Meaning |
|---|---|
| `--dry-run` | Show what would be removed. |
| `-y`, `--yes` | No prompt. |

## `ws plugin`

Switch parts of ws on or off.

### `ws plugin list`

Every plugin, and whether it's on.

```text
ws plugin list [-h]
```

### `ws plugin add`

Switch a plugin on.

```text
ws plugin add [-h] [--region CC] NAME
```

| Argument | Meaning |
|---|---|
| `NAME` | The plugin (see `ws plugin list`). |
| `--region CC` | Your country (two letters); kept in `[machine] region`. |

### `ws plugin remove`

Switch a plugin off.

```text
ws plugin remove [-h] NAME
```

| Argument | Meaning |
|---|---|
| `NAME` | The plugin (see `ws plugin list`). |

## `ws device`

Link another machine to this one.

### `ws device add`

Print the one line to run on the new machine.

```text
ws device add [-h] [--name NAME]
```

| Argument | Meaning |
|---|---|
| `--name NAME` | What you call the new machine (only used in the message). |

## `ws skills`

The skills ws ships, and where they're installed.

### `ws skills list`

Each skill, per runner.

```text
ws skills list [-h]
```

### `ws skills add`

Put a skill back (one you removed), or add one that's not installed yet.

```text
ws skills add [-h] {align,checkpoint,taste-gate,dataviz}
```

| Argument | Meaning |
|---|---|
| `name` | The skill to install. One of `align`, `checkpoint`, `taste-gate`, `dataviz`. |

## `ws canary`

Check a runner still works with ws after an update (no model is called).

Small checks, no paid model. Each run adds one line to `$WS_HOME/logs/canary.jsonl`. Exit 1 on a failure.

```text
ws canary [-h] [--runner {claude-code,opencode}]
```

| Argument | Meaning |
|---|---|
| `--runner RUNNER` | Default: the profile's runner. One of `claude-code`, `opencode`. |

## `ws rate`

Give a run a thumbs up or down.

```text
ws rate [-h] [--note TEXT] RUN_ID|last {up,down}
```

| Argument | Meaning |
|---|---|
| `RUN_ID|last` | A run id from `ws runs`, or `last`. |
| `rating` | `up` or `down`. One of `up`, `down`. |
| `--note TEXT` | A few words on why. |

## `ws notify`

Push to your phone through ntfy: at most 1 digest and 3 pushes a day.

### `ws notify test`

Send a test push (it counts against the daily cap).

```text
ws notify test [-h]
```

### `ws notify send`

Send a push, or a digest with --digest, if today's cap allows.

```text
ws notify send [-h] [--title TITLE] [--digest] message
```

| Argument | Meaning |
|---|---|
| `message` | The message text. |
| `--title TITLE` | The push title (default `ws`). |
| `--digest` | The one daily digest, not a push. |

### `ws notify status`

Today's count against the cap.

```text
ws notify status [-h]
```

## `ws backup`

Back up memory, notes and profile with restic, and prove a file restores.

### `ws backup init`

Make the restic repository.

```text
ws backup init [-h]
```

### `ws backup run`

Back up now (the backup job's timer runs this).

```text
ws backup run [-h]
```

### `ws backup verify`

Restore one random file to a temporary folder and compare its hash.

```text
ws backup verify [-h]
```

## `ws schedule`

Timers (systemd or launchd) for the job files that have a schedule.

### `ws schedule list`

The scheduled jobs.

```text
ws schedule list [-h]
```

### `ws schedule apply`

Write the unit files and switch the timers on; run again and it reports 0 changes.

```text
ws schedule apply [-h] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--dry-run` | Show what would change; write and run nothing. |

### `ws schedule remove`

Stop one job's timer and delete its unit files.

```text
ws schedule remove [-h] job
```

| Argument | Meaning |
|---|---|
| `job` | The job whose timer to remove. |

## `ws approve`

A person approves one pending call the gate refused (single use, 10 minutes).

```text
ws approve [-h] [--list] [--yes] [id]
```

| Argument | Meaning |
|---|---|
| `[id]` | The id the gate printed (8 or more hex digits). |
| `--list` | Show what's waiting for approval. |
| `--yes` | Don't ask to confirm (still needs a terminal). |

## `ws setup`

Set up ws, step by step; run it again and it reports 0 changes (--web for a page in your browser).

```text
ws setup [-h] [--web] [--no-open] [-y] [--plugin NAME] [--notes PATH]
    [--runner {claude-code,opencode}] [--cap USD] [--region CC]
```

| Argument | Meaning |
|---|---|
| `--web` | A page in your browser instead of the terminal. |
| `--no-open` | With --web, print the address and don't open a browser. |
| `-y`, `--yes` | No prompts: take the defaults and the flags below. |
| `--plugin NAME` | Switch a plugin on; repeat for more (ws plugin list). |
| `--notes PATH` | Your notes folder (default: a vault found, else ~/Notes). |
| `--runner RUNNER` | The AI tool ws runs jobs with. One of `claude-code`, `opencode`. |
| `--cap USD` | Your own monthly spend cap. |
| `--region CC` | Your country (two letters); kept in `[machine] region`. |

## `ws config`

Every file ws reads: where it's, where its path came from, the keys it sets.

```text
ws config [-h]
```

## `ws status`

Per runner, every rules, skill, hook and MCP file it will load here, and its origin.

```text
ws status [-h]
```

## `ws eval`

Your own eval: run tasks, read ten metrics, compare two setups, harvest tasks.

### `ws eval run`

Run eval tasks as trials of a job (`--dry-run` shows the plan and spends nothing).

```text
ws eval run [-h] [--task ID | --set {starter,mine}] [--arm NAME] [--job JOB] [--trials N] [--keep]
    [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--task ID` | One task, yours or a starter. |
| `--set SET` | A whole set (default mine). One of `starter`, `mine`. |
| `--arm NAME` | A label for the setup under test (default: the job name). |
| `--job JOB` | The job file that's the arm under test (a runner, model and options). |
| `--trials N` | Trials per task, 1 to 5 (default: the task's). |
| `--keep` | Keep the folder of a passed trial too (a failed one is always kept). |
| `--dry-run` | Show the plan and the number of runs; start nothing. |

### `ws eval report`

The ten metrics; a metric with no data says 'not measured'.

```text
ws eval report [-h] [--arm NAME] [--since SPAN] [--k K]
```

| Argument | Meaning |
|---|---|
| `--arm NAME` | Only this arm. |
| `--since SPAN` | How far back, like 30d or 4w (default 90d). |
| `--k K` | The k of pass^k (default: the fewest trials any task has). |

### `ws eval compare`

Two arms on paired trials: exact McNemar, quality veto, then tokens.

```text
ws eval compare [-h] [--look {1,2}] ARM_A ARM_B
```

| Argument | Meaning |
|---|---|
| `ARM_A` | The first arm (the baseline). |
| `ARM_B` | The second arm. |
| `--look LOOK` | Declare the look; an earlier one is shown from storage. One of `1`, `2`. |

### `ws eval harvest`

Propose tasks from your failed, timed-out, rated-down or retried runs.

```text
ws eval harvest [-h] [--since SPAN]
```

| Argument | Meaning |
|---|---|
| `--since SPAN` | How far back (default 30d). |

### `ws eval approve`

Show a proposal's prompt, ask, and move it into your tasks.

```text
ws eval approve [-h] [--yes] id
```

| Argument | Meaning |
|---|---|
| `id` | The proposal's id (from `ws eval harvest`). |
| `--yes` | Don't ask (for scripts); you have read the proposal. |

### `ws eval reject`

Set a proposal aside so harvest doesn't offer it again.

```text
ws eval reject [-h] id
```

| Argument | Meaning |
|---|---|
| `id` | The proposal's id. |

## `ws version`

Print which build of ws you run. Available from v0.1. See [`ws version --json`](version-json.md).

```text
ws version [--json]
```

## Job file

A job file is `$WS_HOME/jobs/NAME.toml`. `ws run NAME` runs it. The file and its folder must belong to you and not be
writable by others, because a job decides what a runner starts with. Every error names the key.

| Key | Type | Meaning |
|---|---|---|
| `runner` | text | The adapter: `claude-code` or `opencode`. |
| `model` | text | The model id. |
| `prompt` | text | The prompt. Or use `prompt_file`. |
| `prompt_file` | text | A prompt file, relative to the jobs folder and inside it. |
| `cwd` | text | The working folder. Default: a new empty folder per run. |
| `timeout_s` | number | Time limit. Default 900. |
| `max_turns` | integer | Optional turn limit. |
| `bare` | true or false | Headless default: no hooks, plugins or discovered on their own context. Default `true`. |
| `allow_unpriced` | true or false | Run even if the runner can't price the model. Default `false`. |
| `green` | text | What the job's verification covers: `targeted`, `package`, `full` or `merge_ready`. |
| `sources` | list of text | Policy sources the job reads. The Rule of Two looks at these. |
| `schedule` | text | `hourly`, `daily HH:MM` or `weekly DAY HH:MM`, in local time. `ws schedule apply` turns it into a timer. |
| `command` | text | In place of `runner`, `model` and `prompt`: a ws command that the schedule runs (`backup`). `ws run` refuses these. |
| `[[secrets]]` | table list | Each has `name`, `host` and `purpose` (default `run`). The broker leases it. |
| `[runner_options]` | table | Adapter-specific. Claude Code: `allowed_tools`, `env`. OpenCode: `provider`, `env`. |

## Run record

Each `ws run` appends one JSON line to `$WS_HOME/logs/runs.jsonl`. Every key is always present, and a value that ws couldn't
learn is `null`. The keys are: `schema`, `run_id`, `job`, `runner`, `runner_version`, `exe_path`, `model_requested`,
`models_used`, `started_at`, `ended_at`, `duration_ms`, `duration_api_ms`, `exit_code`, `status`, `error`, `session_id`,
`transcript_path`, `tokens`, `cost_usd`, `cost_basis`, `num_turns`, `cwd`, `policy`, `secrets_used`, `ws_version`,
`output_truncated`, `allow_unpriced`, `green`, `recovered` and `eval`. `status` is `ok`, `error`, `timeout` or `refused`.
The file is mode 0600 and rotates at 5 MB, keeping three old files.
