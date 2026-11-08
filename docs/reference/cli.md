# CLI reference

Every `ws` command and flag. This page is generated from the command parsers, so it matches the code. For the task each
command is for, see [Usage](../usage.md).

```text
ws [--json] [--no-color] [-C PATH] [--version] COMMAND ...
```

## Global options

These work on every command, before or after the command name.

| Option | Meaning |
|---|---|
| `--json` | Print one JSON envelope on standard output. `WS_OUTPUT=json` does the same. |
| `-C PATH`, `--cwd PATH` | Run as if started in `PATH`. |
| `--no-color` | Print no colour. A non-empty `NO_COLOR` does the same. |
| `--version` | Print `ws X.Y.Z` and exit. |
| `-h`, `--help` | Print help for the command and exit. `ws help COMMAND` does the same. |

A mistyped command or flag gets a "did you mean" hint.

## Your own commands

`ws <word>` looks for a command in this order, and the first match runs.

1. A ws command (`ws doctor`).
2. A program named `ws-<word>` in a folder on `PATH` (`ws hello` runs `ws-hello`). The word is lower case letters,
   digits and `-`, starting with a letter, up to 41 characters. It gets the arguments after the word. `--json` becomes
   `WS_OUTPUT=json` in its environment, and `-C PATH` is its working folder. Absolute `PATH` entries are searched. The
   program has to belong to you or root, be writable by only that owner, and sit in a folder only its owner can write
   to (or one with the sticky bit, like `/tmp`); otherwise ws skips it and says why on stderr.
3. The program in the profile's `[cli] fallback`. It gets the whole original argument list, including flags ws has no
   use for (`ws -n repo tag`) and an empty list (a bare `ws`), and runs with `WS_FALLBACK=1`. A run that already has
   `WS_FALLBACK` set goes no further than that program.
4. The usage error, with a "did you mean" hint. An unreadable profile gets a one-line note beside it.

The fallback is an absolute path (or one that starts with `~/`) to an executable file you own that nobody else can
write to, in a folder only its owner can write to; the file passes the same test as a job file. Set it with
`ws config fallback PATH`, clear it with `ws config fallback --clear`. `ws help` lists the `ws-<word>` programs on
`PATH` and names the fallback.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Fine. |
| 1 | Needs doing: a check found something to fix, or a run failed or timed out. |
| 2 | Usage error. `ws run` also uses 2 when the job file can't be read. For `ws gate check`, 2 is a deny. For `ws gate event`, 2 comes after a PostToolUse event, with the finding on standard error. |
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
| `WS_HOME` | One folder that holds all of ws's files, laid out the way ws always did. Unset by default: ws then uses the XDG folders (see [Stores](stores.md#where-ws-keeps-its-files)). |
| `WS_CONFIG_DIR`, `WS_STATE_DIR`, `WS_DATA_DIR`, `WS_CACHE_DIR` | A full path that moves one of ws's four folders. Beats `WS_HOME` for that folder. |
| `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` | Where the config, data and cache folders go, under `ws/`. Defaults `~/.config`, `~/.local/share`, `~/.cache`. |
| `WS_OUTPUT` | Set to `json` for `--json` on every command. |
| `WS_BROKER_SOCKET` | The broker socket a client uses. |
| `WS_GATED_SESSION` | Set inside a gated session. `ws approve` refuses when it's present. |
| `WS_GATE_DAEMON` | Set to `0` in a hook's environment to make every gate call decide in its own process, with no resident gate (see [The resident gate](gate.md#the-resident-gate)). |
| `WS_FORCE_CASELESS_PATHS` | Set to `1` to match paths without regard to case in every `ws gate` process, as on macOS. It can only turn that on. For testing the gate on a case-sensitive machine. |
| `NO_COLOR` | Any non-empty value turns colour off in every command. An empty value is ignored. `--no-color` does the same. |
| `FORCE_COLOR` | Any value except empty, `0` or `false` (any case) keeps colour on when output goes to a pipe or `TERM=dumb`. `NO_COLOR` and `--no-color` win. |
| `TERM` | `dumb` turns colour off in every command, and makes `ws setup` print each step once, with no cursor-move or erase codes. |
| `COLORFGBG` | Read by every command that prints colour. The default is the full brand palette in 256 colours; a light background (`0;15`) switches to darker violet and mark colours and your terminal's own green, yellow and red. |
| `XDG_STATE_HOME` | Where the state folder goes, under `ws/`, and run folders for jobs with no `cwd`, under `ws-work`. Default `~/.local/state`. |

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
| `--policy FILE` | Policy file (default ~/.config/ws/policy.toml). |

### `ws gate generate`

Write a runner's native deny rules and hook into a folder.

```text
ws gate generate [-h] [--policy FILE] --runner {claude-code,opencode} --out DIR [--ws-cmd CMD]
```

| Argument | Meaning |
|---|---|
| `--policy FILE` | Policy file (default ~/.config/ws/policy.toml). |
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
| `--policy FILE` | Policy file (default ~/.config/ws/policy.toml). |
| `--runner RUNNER` | The runner whose hook payload is on standard input. One of `claude-code`, `opencode`; required. |
| `--protect PATH` | A file or folder of ws's own that a session may not write. |

### `ws gate event`

Hook entry for the guards' other events (after a call, start, stop): payload on stdin. Exit 0, or 2 after a tool call
with a finding on standard error.

```text
ws gate event [-h] [--policy FILE] --runner {claude-code,opencode}
```

| Argument | Meaning |
|---|---|
| `--policy FILE` | Policy file (default ~/.config/ws/policy.toml). |
| `--runner RUNNER` | The runner whose hook payload is on standard input. One of `claude-code`, `opencode`; required. |

An event that `ws gate check` answers, or one ws doesn't read, exits 0 with a line on standard error. A payload it can't
read exits 0 with a line saying it was skipped: this command gives advice and never blocks a session by crashing.
See [Guards](guards.md).

### `ws gate serve`

The resident gate: one process for each user and state folder that answers every hook call of every session. The first
hook call that finds none running starts it. See [The resident gate](gate.md#the-resident-gate).

```text
ws gate serve [-h] [--pin KEY=PATH] [--idle SECONDS] [--stop]
```

| Argument | Meaning |
|---|---|
| `--pin KEY=PATH` | The folders `ws install` pinned (home, config, state, data, cache); the socket goes in `run/` under the state folder. |
| `--idle SECONDS` | Exit after this long without a request (default 900). |
| `--stop` | Ask the running daemon to exit. Exit 0 when one was told to, 1 when none is running. |

It runs in the foreground until stopped, idle, or ws is upgraded under it. Exit 1 when the run folder isn't private to
you or something else sits where the socket goes; the reason is on standard error.

### `ws gate replay`

Run recorded hook payloads against the policy and print allow, deny or ask for each. No approval is used up and no guard
state is kept.

```text
ws gate replay [-h] [--policy FILE] --runner {claude-code,opencode} FILE
```

| Argument | Meaning |
|---|---|
| `FILE` | One JSON object per line: a payload, or `{payload, expect}` where `expect` is `allow`, `deny` or `ask`. |
| `--policy FILE` | Policy file (default ~/.config/ws/policy.toml). |
| `--runner RUNNER` | The runner the payloads came from. One of `claude-code`, `opencode`; required. |

Exit 0 when every line with an `expect` matches (or none has one), and 1 when an `expect` differs. With `--json` the
result is `{results, differ}`, one entry per line. See [Guards](guards.md).

### `ws gate launch`

Start a runner inside the sandbox wall (bubblewrap on Linux, sandbox-exec on macOS); fails closed.

```text
ws gate launch [-h] --runner {claude-code,opencode} [--policy FILE] [--protect PATH] [--allow-write
    DIR] [--no-sandbox] ...
```

| Argument | Meaning |
|---|---|
| `--runner RUNNER` | The runner to start: `claude-code` or `opencode`. One of `claude-code`, `opencode`; required. |
| `--policy FILE` | Policy file (default ~/.config/ws/policy.toml). |
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
| `--socket PATH` | Default $WS_BROKER_SOCKET, else [roles.secrets] socket in the profile, else ~/.local/state/ws/run/broker.sock. |
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
| `--socket PATH` | The broker's socket (default: from the profile, else `~/.local/state/ws/run/broker.sock`). |

### `ws broker status`

Ask a running broker how it is.

```text
ws broker status [-h] [--socket PATH]
```

| Argument | Meaning |
|---|---|
| `--socket PATH` | The broker's socket (default: from the profile, else `~/.local/state/ws/run/broker.sock`). |

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

Run a headless job (a file in ~/.config/ws/jobs) and record what it did.

Run a headless job. The monthly cap is checked before the run, not enforced during it.

```text
ws run [-h] [--dry-run] job
```

| Argument | Meaning |
|---|---|
| `job` | The job name: ~/.config/ws/jobs/<name>.toml. |
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
    {claude-code,opencode}] [--model ID] [--allow-widened-sandbox] [--cap USD] [-y] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--stays-on yes\|no` | Does this machine stay on all the time?. |
| `--server ADDRESS\|none` | A server to connect to. |
| `--notes PATH` | Your notes folder (default: a vault found, else ~/Notes). |
| `--module NAME` | Also switch on a module; repeat for more. |
| `--runner RUNNER` | The AI tool ws runs jobs with. One of `claude-code`, `opencode`. |
| `--model ID` | The default model. |
| `--allow-widened-sandbox` | Keep settings of yours that open the sandbox (an excluded command, say); ws records the choice. |
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

## `ws upgrade`

Swap ws for a newer release and rewrite the runner's hooks in the same step.

```text
ws upgrade [-h] [--version TAG] [--channel {stable,rc,edge}] [--installer FILE] [--hooks-only] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--version TAG` | Install this release (a tag like `v0.2.0`) instead of the latest. |
| `--channel NAME` | The release channel to follow: `stable`, `rc` or `edge`. |
| `--installer FILE` | Run this copy of `install.sh` instead of downloading it. |
| `--hooks-only` | Rewrite the hooks only; a newer ws is already installed. |
| `--dry-run` | Show what would change; change nothing. |

It runs the installer, then rewrites ws's hooks with the new ws in a new process (`--hooks-only`). The rewrite touches the
gate only: each runner whose config already holds ws's entries gets them refreshed. It switches nothing on and takes
nothing out, and a config written without the sandbox block stays without it. If the rewrite fails, the runner's config,
the install record and the OpenCode plugin are put back as they were. Run it in a terminal of your own: a session, gated
or not, is refused (exit 3), and so is a run with no terminal. Exit 0 when done, 1 when the installer or the rewrite
failed, 3 when refused, 4 when files couldn't be put back after a failed rewrite (they're named). See
[Upgrade and uninstall](../how-to/upgrade-and-uninstall.md#upgrade).

## `ws migrate`

Move ws's files from `~/.ws` to the XDG folders. Nothing is deleted.

```text
ws migrate [-h] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--dry-run` | Show the move and change nothing. |

It plans the move file by file, refuses (changing nothing) if a target exists and differs, copies with the modes kept,
checks every copy by its SHA-256, writes `migrate-manifest.json` in the state folder, renames `~/.ws` to
`~/.ws.migrated-DATE`, and refreshes the runner hooks and generated files, because they name the policy by its path.
Run it again and it says it's done. After it, run `ws schedule apply` if you have scheduled jobs. Exit 0 when done or
when there's nothing to do, 1 when it was refused or the refresh failed.

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
| `--no-color` | Print with no colour, the same as a non-empty `NO_COLOR`. |

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
ws skills add [-h] {align,checkpoint,taste-gate,dataviz,resume,ship}
```

| Argument | Meaning |
|---|---|
| `name` | The skill to install. One of `align`, `checkpoint`, `taste-gate`, `dataviz`, `resume`, `ship`. |

### `ws skills adopt`

Use ws's version of a skill you already have: it replaces your skill of the same name with ws's, keeping yours so
`ws skills restore` can put it back.

```text
ws skills adopt [-h] [--runner {claude-code,opencode}] {align,checkpoint,taste-gate,dataviz,resume,ship}
```

| Argument | Meaning |
|---|---|
| `name` | The skill to adopt. One of `align`, `checkpoint`, `taste-gate`, `dataviz`, `resume`, `ship`. |
| `--runner RUNNER` | The runner (default: every runner found). One of `claude-code`, `opencode`. |

### `ws skills restore`

Put back the skill of yours that `ws skills adopt` replaced.

```text
ws skills restore [-h] [--runner {claude-code,opencode}] {align,checkpoint,taste-gate,dataviz,resume,ship}
```

| Argument | Meaning |
|---|---|
| `name` | The skill to restore. One of `align`, `checkpoint`, `taste-gate`, `dataviz`, `resume`, `ship`. |
| `--runner RUNNER` | The runner (default: every runner found). One of `claude-code`, `opencode`. |

## `ws canary`

Check a runner still works with ws after an update (no model is called).

Small checks, no paid model. Each run adds one line to `~/.local/state/ws/logs/canary.jsonl`. Exit 1 on a failure.

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

Push to your phone through ntfy: at most 1 digest and 3 pushes a day, and session alerts on their own daily allowance.

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

Today's count against the caps.

```text
ws notify status [-h]
```

### `ws notify alert`

Hook entry: push `FOLDER needs you` (a Notification event, high priority) or `FOLDER finished` (a Stop event). The hook
JSON is on stdin. Exits 0, including when it sends nothing.

```text
ws notify alert [-h] [--runner RUNNER]
```

| Argument | Meaning |
|---|---|
| `--runner RUNNER` | The runner whose hook is calling, for example `claude-code`. |

It sends nothing for any other event, for a headless session (`claude -p`, or `WS_RUN_ID` set), or when `session_alerts`
is `false`. The send gets 3 seconds. If another send holds the counter it skips that alert, and after a failed send
alerts pause for 5 minutes.

Under `alert_on = "needs-you"` a Stop alerts, as `FOLDER needs you`, when the session's last message asks you
something. It does when the message ends with `?` (spaces, quotes, brackets and markdown marks at the end set aside),
or when its last paragraph names a choice: `which one`, `your call`, `up to you`, `choose`, `pick one`, `reply with`,
`let me know which`, `whether`, `what` or `how`, or `need your decision`, `answer`, `input`, `approval` or
`confirmation`. The message is read from the hook payload (`last_assistant_message`, else the last assistant entry in
the file `transcript_path` names; a tool-call entry has no text and never asks). It's only read: the alert says what
`alert_text` allows, and never holds any of the message. A payload with no last message never asks, and neither does a
message that ends in a code block. A Notification of the type `auth_success` needs nothing and sends nothing under `"needs-you"`.

#### Profile keys

Set these in the `[roles.notify]` table of `profile.toml`, beside `adapter = "ntfy"`, `url` and `token`.

| Key | Meaning |
|---|---|
| `session_alerts` | `true` (default) or `false`. When `false`, `ws notify alert` sends nothing. |
| `alert_text` | What an alert says. `"title"` (default) sends the folder name with the event, such as `FOLDER needs you`. `"full"` adds the runner's own message after it. `"plain"` sends `a session needs you` with no folder name, for private folder names. |
| `alerts_per_day` | The daily allowance for session alerts, a whole number, 0 or more (default 50). Counted apart from pushes and the digest. |
| `alert_on` | Which events alert. `"needs-you"` sends for each Notification (a permission or an answer is wanted) and for a Stop whose last message asks something; a plain finished turn sends nothing. `"every-stop"` sends for every Notification and every Stop. A key that isn't there means `"every-stop"`, so a config from before the key behaves as it did; set `"needs-you"` in a new one. |

The folder name goes to the notify server (ntfy.sh or your own) with the push. Each send writes
`~/.local/state/ws/logs/notify-last.json`.

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
    [--runner {claude-code,opencode}] [--cap USD] [--region CC] [--no-color]
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

Every file ws reads: where it's, which folder layout it came from, the keys it sets. `ws config fallback` shows, sets and
clears the program that gets words ws has no command for (see [Your own commands](#your-own-commands)).

```text
ws config [-h] [--clear] [{fallback}] [PATH]
```

| Argument | Meaning |
|---|---|
| `fallback` | Show the fallback program. With `PATH`, set it; with `--clear`, remove it. |
| `PATH` | With `fallback`: the program (absolute, or `~/...`). It has to be an executable file you own that nobody else can write to. |
| `--clear` | With `fallback`: remove it. |

## `ws status`

Per runner, every rules, skill, hook and MCP file it will load here, and its origin.

```text
ws status [-h]
```

A path is listed once per runner. With `--json`, each row has a `folder` key: `true` when the row is a folder (a
skill folder with no main file), `false` for a file. The text output shows a folder with a trailing `/`.

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

Two arms on paired tasks: who leads, the 95% interval, and whether it's real.

```text
ws eval compare [-h] [--look {1,2}] [--allow-mixed] ARM_A ARM_B
```

| Argument | Meaning |
|---|---|
| `ARM_A` | The first arm (the baseline). |
| `ARM_B` | The second arm. |
| `--look LOOK` | Declare the look; an earlier one is shown from storage. One of `1`, `2`. |
| `--allow-mixed` | Pool results of one arm even when they were made different ways (models, versions, policy, job). |

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

A job file is `~/.config/ws/jobs/NAME.toml`. `ws run NAME` runs it. The file and its folder must belong to you and not be
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

Each `ws run` appends one JSON line to `~/.local/state/ws/logs/runs.jsonl`. Every key is always present, and a value
that ws couldn't learn is `null`. The keys are: `schema`, `run_id`, `job`, `runner`, `runner_version`, `exe_path`,
`model_requested`, `models_used`, `started_at`, `ended_at`, `duration_ms`, `duration_api_ms`, `exit_code`, `status`,
`error`, `session_id`, `transcript_path`, `tokens`, `cost_usd`, `cost_basis`, `num_turns`, `cwd`, `policy`,
`secrets_used`, `ws_version`, `output_truncated`, `allow_unpriced`, `green`, `recovered` and `eval`. `status` is `ok`,
`error`, `timeout` or `refused`. The file is mode 0600 and rotates at 5 MB, keeping three old files.
