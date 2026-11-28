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
| `WS_CODE_ROOT` | The folder `ws dispatch` finds repositories in. Beats `[dispatch] code_root` in the profile; default `~/code`. |
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

## `ws dispatch`

Start a runner's session on a prompt in a new tmux window, in one repository's folder.

```text
ws dispatch [-h] [--name NAME] [--model MODEL] [--runner {claude-code,opencode}] [--session SESSION]
            [--code-root DIR] [--dry-run] REPO PROMPT
```

| Argument | Meaning |
|---|---|
| `REPO` | The repository's folder name under the code root. A name only: a slash, `.` or `..` is a usage error. |
| `PROMPT` | What the session should do, as one argument. A prompt that starts with `-` is refused. |
| `--name NAME` | A few words for the window's name (lower case, dashes for spaces). Default: the repo's name. A `claude-code` session is also started with this label as its name. |
| `--model MODEL` | The model, passed to the runner as one argument, `--model=MODEL`. |
| `--runner R` | `claude-code` (default) runs `claude --name LABEL [--remote-control=LABEL] [--model=M] PROMPT`; `opencode` runs `opencode [--model=M] --prompt PROMPT`. |
| `--session S` | The tmux session for the window. Default: the one this runs in; outside tmux, give one. |
| `--code-root DIR` | Where the repositories are. Default `WS_CODE_ROOT`, else `[dispatch] code_root` in the profile, else `~/code`. |
| `--dry-run` | Print the folder, the window name and the runner's argv, and start nothing. |

The window opens in the background and runs the runner's argv directly, with no shell between (tmux 3.0 or later),
so every flag and the prompt arrive as separate arguments whatever they hold. `--help` changes nothing. If you start
sessions with a script of your own, this can replace it: point `WS_CODE_ROOT` at your repositories.

A Claude session is started named and, where allowed, with remote control on, so nobody types `/rename` or
`/remote-control` into its pane afterwards (the second opens a dialog that holds every queued message until a key is
pressed; the [pane-keys guard](guards.md#pane-keys) refuses such typing). The label is the window's name exactly, so
`--name "Fix the login"` names the window and the session `fix-the-login`; a `·` or any other character outside
`a-z 0-9 _` becomes a dash. `--remote-control=LABEL` is one argument, so it can never take the prompt as its value.
Remote control sends the session's transcript through the vendor's servers, so it's left off (with one line on
stderr, and `--name` still passed) for a repo whose class in `policy.toml` isn't `access = "full"`: walled,
counts-only, or in no class once any class exists. A policy that can't be read leaves it off too.

## `ws sync`

Fetch every git repository under the code root, a few at a time, each with a time limit.

```text
ws sync [-h] [--code-root DIR] [--jobs N] [--timeout-s S]
```

| Argument | Meaning |
|---|---|
| `--code-root DIR` | Where the repositories are. Default as for `ws dispatch`: `WS_CODE_ROOT`, else `[dispatch] code_root`, else `~/code`. |
| `--jobs N` | How many fetches run at once. Default 4. |
| `--timeout-s S` | A fetch that takes longer is killed with its whole process group, and named. Default 120. |

A repository is a folder directly under the code root that holds `.git`. Each gets `git fetch --all --prune --quiet`
with no terminal and `GIT_TERMINAL_PROMPT=0`; nothing in a working tree is touched. Exit 0 when every fetch finished
cleanly, 1 when any failed or timed out, each named with why. If you fetch your repositories with a script of your own,
this can replace it.

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

Given a labelled corpus, `--metrics` prints the gate's error rates, false blocks per 100 and the p95 time it adds to a
tool call, and exits 1 when that p95 is over `--p95-budget-ms MS` (default 50). See
[Replay a corpus](gate.md#replay-a-corpus).

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

### `ws secret ask`

Open a tmux window with a no-echo prompt for a registered name; the value goes straight to the store. Needs a tmux
session. An AI session may run it (it shows no value). The older command `ws-secret` takes the same
arguments.

```text
ws secret ask [-h] name
```

| Argument | Meaning |
|---|---|
| `name` | A name registered in `secrets.toml`. |

### `ws secret wait`

Block until `ask` has stored the value. Exit 1 if the prompt was cancelled, failed or the time ran out.

```text
ws secret wait [-h] [--timeout SECONDS] name
```

| Argument | Meaning |
|---|---|
| `name` | A name registered in `secrets.toml`. |
| `--timeout SECONDS` | How long to wait (default 900). |

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

### `ws secret generate`

Make a strong random value for a `source = "generate"` name (never shown).

```text
ws secret generate [-h] [--replace] name
```

| Argument | Meaning |
|---|---|
| `name` | A name registered in `secrets.toml` with `source = "generate"`. |
| `--replace` | Make a new value even if one is stored. |

### `ws secret reveal`

Show a generated value once, on your own device, through a one-time page.

```text
ws secret reveal [-h] [--owner LOGIN] [--bind ADDR] [--minutes MINUTES] [--again] [--notify] name
```

| Argument | Meaning |
|---|---|
| `name` | A name registered with `source = "generate"` that has a value. |
| `--owner LOGIN` | The tailnet login (or its start) whose device may open it; default `owner` in `[roles.secrets]`, which has no default. |
| `--bind ADDR` | The one tailnet address to listen on (default: `tailscale ip -4`). |
| `--minutes MINUTES` | How long the link works, at most 15 (default 15). |
| `--again` | Show a value that was already shown once. |
| `--notify` | Push the link to your phone through `[roles.notify]`, so you tap it there. The link goes through your ntfy server; only your own device can open it, once. |

Exit 0 when the value was shown, 1 when the link expired unused.

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
ws broker serve [-h] [--socket PATH] [--group GROUP] [--port PORT] [--lease-ttl SECONDS] [--require-run]
```

| Argument | Meaning |
|---|---|
| `--socket PATH` | Default $WS_BROKER_SOCKET, else [roles.secrets] socket in the profile, else ~/.local/state/ws/run/broker.sock. |
| `--group GROUP` | Also serve members of this group (socket folder becomes 0750). |
| `--port PORT` | Lease proxy port on 127.0.0.1 (default: any free one). |
| `--lease-ttl SECONDS` | Longest lease (default 3600). |
| `--require-run` | Every lease needs a run record, scoped name or not (also [roles.secrets] require_run = true). |

### `ws broker request`

Ask for a lease and print it as JSON (for scripts).

```text
ws broker request [-h] --purpose PURPOSE --host HOST [--ttl SECONDS] [--run RUN_ID] [--socket PATH] name
```

| Argument | Meaning |
|---|---|
| `name` | The secret's name. |
| `--purpose PURPOSE` | What the lease is for; it must be in the registry entry's `purposes`. Required. |
| `--host HOST` | The host the lease may reach. Required. |
| `--ttl SECONDS` | Ask for a shorter lease. |
| `--run RUN_ID` | The run the lease is for (its run record says what it may have). |
| `--socket PATH` | The broker's socket (default: from the profile, else `~/.local/state/ws/run/broker.sock`). |

### `ws broker status`

Ask a running broker how it is.

```text
ws broker status [-h] [--socket PATH]
```

| Argument | Meaning |
|---|---|
| `--socket PATH` | The broker's socket (default: from the profile, else `~/.local/state/ws/run/broker.sock`). |

### `ws broker run-record`

Write, show or remove a run record (what one run may lease).

```text
ws broker run-record write [-h] --area AREA --tier {walled,worker,lead,lead-elevated} [--name NAME] [--uid UID]
                           [--ttl SECONDS] [--repo REPO] [--perm NAME=LEVEL] [--dir PATH] run
ws broker run-record show [-h] [--dir PATH] run
ws broker run-record rm [-h] [--dir PATH] run
```

| Argument | Meaning |
|---|---|
| `run` | The run ID. |
| `--area AREA` | The run's area. Required for `write`. |
| `--tier TIER` | The run's tier. Required for `write`. |
| `--name NAME` | A secret name the run declares (repeat). |
| `--uid UID` | Only this caller uid may lease under the run. |
| `--ttl SECONDS` | The run ends after this; its leases with it. |
| `--repo REPO` | A repository a minted GitHub token may cover (repeat). |
| `--perm NAME=LEVEL` | The most a minted token may hold (repeat). |
| `--dir PATH` | The record folder (default `~/.local/state/ws/run/records`). |

### `ws broker audit-copy`

Copy the audit log off this machine (a job runs it daily).

```text
ws broker audit-copy [-h] [--to DEST]
```

| Argument | Meaning |
|---|---|
| `--to DEST` | A folder or user@host:folder (default [roles.secrets] audit_copy). |

## `ws elevate`

The kill switch for elevated secrets: `ws elevate off` refuses them at once.

```text
ws elevate [-h] [--socket PATH] {off,on,status}
```

| Argument | Meaning |
|---|---|
| `{off,on,status}` | Off: refuse and revoke; on: allow again (from your own terminal only); status. |
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

## `ws connect`

Connect an integration: the steps only a person can do, from one flow file, walked the same way each time. The format and
the JSON an agent reads are in [Connect flows](connect.md); to add one, see [Add a connect flow](../how-to/add-a-connect-flow.md).

```text
ws connect [-h] [--dry-run] [--diagnose] [--revoke] [--yes] [target]
```

| Argument | Meaning |
|---|---|
| `target` | A flow id, or `list` (the default): the flows there are. |
| `--dry-run` | Show each step as `done` or `needs_human`. With `--json`, the agent contract. Exit 0 when all done, 3 when a person has something to do. Changes nothing; an agent may run it. |
| `--diagnose` | Step state and what was learned, with the nonce redacted. Changes nothing. |
| `--revoke` | Undo the flow: ask first (default no), delete its secret only if `ws connect` stored it, say what to undo on the other side, clear its state. Refused inside an agent session. |
| `--yes` | With `--revoke`: don't ask. |

With no flag, `ws connect <id>` walks the flow in your own terminal and refuses inside an agent session. A captured
secret goes to the secrets store and nowhere else.

## `ws mail`

Mailboxes for triage (`[mail.NAME]` in the profile): `ws mail add NAME --host H --user U [--kind gmail]`,
`ws mail connect --web` (the same as a local page), `ws mail check`, `ws mail rm NAME`, and `ws mail serve` (the five
mail tools over stdio). See [Mail](mail.md).

## `ws triage`

The inbox job: `ws triage run --job NAME` (the settings are the `[triage]` table of `jobs/NAME.toml`, a job with
`command = "triage"` that a `schedule` runs) or `ws triage run --config FILE`. It sorts the new mail of the listed
mailboxes by rules, calls one small model per thread that may need a reply, checks each draft in code and saves it
through `ws mail`. `--dry-run` makes no label or draft call, saves no state and writes no summary; the model is still
called, so it measures a real run. Exit 0 ran, 1 a mailbox failed, 2 settings, 3 refused. See [Inbox triage](triage.md).

## `ws connector`

Zone MCP servers (`[connectors.NAME]` in the [policy](policy.md#connectorsname)): list them, start a session with one,
and the hook that judges and logs each call. See [Plug in a zone MCP server](../how-to/plug-in-zone-mcp.md).

### `ws connector list`

The policy's connectors, and whether a session in this folder may read each.

```text
ws connector list [-h]
```

### `ws connector launch`

Start Claude Code (through `ws gate launch`) with the named connectors wired in, for this launch only. An admin command:
a session can't run it.

```text
ws connector launch [-h] [-c NAME] ...
```

| Argument | Meaning |
|---|---|
| `-c NAME`, `--connector NAME` | A connector to plug in (repeatable). |
| `cmd` | `--` then the runner's command line (`claude ...`). |

### `ws connector audit`

Hook entry: judge one MCP call against its connector's allowlist and log it. Exit 2 refuses the call.

```text
ws connector audit [-h] [--policy FILE] [--pin KEY=PATH]
```

## `ws run`

Run a headless job (a file in ~/.config/ws/jobs) and record what it did.

Run a headless job. The monthly cap is checked before the run, not enforced during it.

```text
ws run [-h] [--dry-run] [--parent RUN_ID] job
```

| Argument | Meaning |
|---|---|
| `job` | The job name: ~/.config/ws/jobs/<name>.toml. |
| `--dry-run` | Show what would start, and whether it would be refused. |
| `--parent RUN_ID` | Reserve only from what this run in progress has left, on top of the monthly cap. |

A job put in place by `ws job add` is pinned: `ws run` refuses it if its file or prompt_file changed since.

## `ws job`

Add a job file (checked, copied and pinned) or remove one. Every add, replace and remove writes one line to
`jobs-installed.jsonl` in the state folder's `logs/`.

### `ws job add`

Check a job file, copy it into the jobs folder and pin its sha256.

```text
ws job add [-h] [--replace] [--no-apply] path
```

| Argument | Meaning |
|---|---|
| `path` | The job file, `NAME.toml`. Its `prompt_file` (a file name next to it) is copied with it. |
| `--replace` | Replace an installed job of the same name. The diff is shown first. |
| `--no-apply` | Don't run `ws schedule apply` afterwards. |

It refuses (exit 3) a tool outside `[gate] allow_mcp`, the built-in tools that write or run commands (Write, Edit,
MultiEdit, NotebookEdit, Bash), a `[[secrets]]` name, host or purpose the registry doesn't allow,
`runner_options` env or exe, an OpenCode `provider` table that isn't a local model server (see
[A local model server](#a-local-model-server-opencode)), and a `budget_usd` above `monthly_cap_usd` or the purpose's
cap. A job that doesn't load is a usage error (exit 2). Inside an agent session, a job that adds a secret, tool,
schedule, command or higher budget than the installed jobs have needs `ws approve` once; a same-power fix doesn't.

### `ws job remove`

Stop a job's timer, delete what `ws job add` copied and drop its pin.

```text
ws job remove [-h] name
```

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

### `ws session load`

Hook entry: print standing preferences, the area card and the records a search finds.

```text
ws session load [-h] --event {start,prompt} [--message MESSAGE] [--session-id ID] [--runner RUNNER]
```

| Argument | Meaning |
|---|---|
| `--event {start,prompt}` | `start`: SessionStart. `prompt`: the first prompt. |
| `--message MESSAGE` | The first prompt (default: prompt in the hook JSON). |
| `--session-id ID` | The session id (default: session_id in the hook JSON). |
| `--runner RUNNER` | Accepted and ignored. |

It reads one folder of plain files: `WS_CONTEXT_DIR`, else `context` in the data folder. `core/preferences.md` and an
area card print at start, with the index lines of the session's own class, under 10,000 characters. The first prompt
adds up to five current records a full-text search on the message finds, and the session record keeps their ids. A
prompt that finds nothing is tried again on the next one, up to three prompts. A walled session sees only its own
class's records and the core preferences. A record whose `class` isn't a known policy class, or whose front matter ws
can't fully read, is never loaded. Each file is read to 256 KB. It's advice: an unreadable folder or policy loads less,
and it never fails the runner. See `src/ws/context_load.py` for the folder layout.

## `ws msg`

Messages between sessions: send one, list yours, read one, watch the envelope feed, wait for the next. A message is data
for the receiver, never an instruction. See [Messages](messages.md).

```text
ws msg [-h] {send,list,read,watch,wait,export} ...
```

### `ws msg send`

Send a message; the sender is the session you are in, never an argument.

```text
ws msg send [-h] [--kind KIND] [--reply-to ID] [--thread NAME] [--key KEY] [--tier {info,urgent,noise}]
    [--ack-required] SESSION [text]
```

| Argument | Meaning |
|---|---|
| `SESSION` | The recipient's session id. |
| `text` | The message, or `-` to read standard input. At most 16 KiB. |
| `--kind KIND` | `note` (default), `ask`, `stop-and-hand-over`, or a fixed kind (`ack`, `done`, `stop-and-hand-over:done`, `ask:pending`, `counts`). |
| `--reply-to ID` | The message this answers; it joins that thread. |
| `--thread NAME` | A thread name (letters, digits, `.`, `_`, `:`, `-`). |
| `--key KEY` | An idempotency key: sending it again returns the first message. |
| `--tier TIER` | `info`, `urgent` or `noise`. |
| `--ack-required` | Ask the receiver to acknowledge it. |

### `ws msg list`

Your messages (envelopes only), oldest first.

```text
ws msg list [-h] [--sent] [--unread] [--after ID] [--limit N]
```

| Argument | Meaning |
|---|---|
| `--sent` | The ones you sent, not the ones left for you. |
| `--unread` | Only those you haven't read. |
| `--after ID` | Only messages after this id. |
| `--limit N` | At most N (default 50, up to 200). |

### `ws msg read`

Read one message left for you; the body is shown only to the recipient.

```text
ws msg read [-h] [--ack] ID
```

| Argument | Meaning |
|---|---|
| `--ack` | Acknowledge it too. |

### `ws msg watch`

The envelope feed (who, what kind, how big; never a body), from a cursor.

```text
ws msg watch [-h] [--after SEQ] [--position-now] [--limit N]
```

| Argument | Meaning |
|---|---|
| `--after SEQ` | Only rows after this sequence number. |
| `--position-now` | List nothing; print the cursor for the end of the feed. |
| `--limit N` | At most N (up to 200). |

### `ws msg wait`

Block until a message that no hook has delivered lands for you.

```text
ws msg wait [-h] [--timeout SECONDS] [--session-id ID] [--session-hook] [--pin KEY=PATH]
```

| Argument | Meaning |
|---|---|
| `--timeout SECONDS` | Give up after this long (at most 1800; a hook's waiter, 82800). |
| `--session-id ID` | The session, when its runner hosts several (OpenCode). |
| `--session-hook` | What a Claude Code `asyncRewake` hook runs: exit 2 with the envelope on standard error when a message lands, and exit 0, silently, in every other case, including a one-shot `claude -p` run. |
| `--pin KEY=PATH` | Hook entry: the folders `ws install` pinned (`home`, `config`, `state`, `data`, `cache`); refuses if the environment resolves elsewhere. |

### `ws msg export`

Write messages as JSON lines (never a walled body).

```text
ws msg export [-h] [--out PATH] [--after ID] [--limit N]
```

| Argument | Meaning |
|---|---|
| `--out PATH` | A new file to write (default: standard output). An existing path is refused. |
| `--after ID` | Only messages after this id. |
| `--limit N` | At most N (default 10000, up to 100000). |

## `ws ask`

Leave a question for a person and read back the answer: one queue across every session. See [Asks](asks.md).

```text
ws ask [-h] {add,list,get,answer,wait,note,done,withdraw,import} ...
```

### `ws ask add`

Add an ask and print its id; the first `--option` is the recommendation.

```text
ws ask add [-h] [--question TEXT] [--option LABEL|DESCRIPTION] [--decision TEXT] [--free-text | --no-free-text]
    [--repo NAME] [--cwd DIR] [--session NAME] [--tmux TARGET] [--dedupe-key KEY] [--stdin]
```

| Argument | Meaning |
|---|---|
| `--question TEXT` | The question (at most 2000 characters). |
| `--option LABEL\|DESCRIPTION` | An option; repeat for more (at most 6). |
| `--decision TEXT` | What the answer decides. |
| `--free-text`, `--no-free-text` | Whether an answer may be free text (default: it may). |
| `--repo NAME` | The repo it's about (default: the working folder's name). |
| `--cwd DIR` | The folder the ask comes from (default: this one). |
| `--session NAME` | Who asks (default `$WS_SESSION_NAME`). |
| `--tmux TARGET` | The tmux target to go to. |
| `--dedupe-key KEY` | An open ask with this key is returned, not added. |
| `--stdin` | Read the ask from standard input as one JSON object. |

### `ws ask list`

Open asks, oldest first, with a count of their notes.

```text
ws ask list [-h] [--all]
```

| Argument | Meaning |
|---|---|
| `--all` | Every status. |

### `ws ask get`

One ask: its status, answer and notes. An unknown id is exit 2.

```text
ws ask get [-h] id
```

### `ws ask answer`

Answer with an option label (any case), a 1-based number, or free text.

```text
ws ask answer [-h] id text [text ...]
```

### `ws ask wait`

Block until answered: 0 and the answer, 3 timed out, 4 withdrawn or done.

```text
ws ask wait [-h] [--timeout SECONDS] id
```

| Argument | Meaning |
|---|---|
| `--timeout SECONDS` | Give up after this long (default 540). |

### `ws ask note`

Add a note to an open or answered ask; it's appended, and nothing earlier is changed. A done or withdrawn ask is exit 1
(`not_open`), an unknown id exit 2. The note is checked and redacted as a question is.

```text
ws ask note [-h] id text [text ...]
```

### `ws ask done`

The session has acted on it (an open or answered ask); a no-op that prints the status if it's already closed.

```text
ws ask done [-h] id
```

### `ws ask withdraw`

The session no longer needs it (an open ask); a no-op that prints the status if it's already closed.

```text
ws ask withdraw [-h] id
```

### `ws ask import`

Convert an older asks file into events here (safe to run twice).

```text
ws ask import [-h] [--dry-run] path
```

| Argument | Meaning |
|---|---|
| `--dry-run` | Say what would be imported; write nothing. |

## `ws install`

Set up ws on this machine; run it again and it reports 0 changes.

```text
ws install [-h] [--stays-on yes|no] [--server ADDRESS|none] [--notes PATH] [--module NAME] [--runner
    {claude-code,opencode}] [--model ID] [--gate {every-session,launch}] [--allow-widened-sandbox] [--class NAME]
    [--cap USD] [-y] [--dry-run]
```

| Argument | Meaning |
|---|---|
| `--stays-on yes\|no` | Does this machine stay on all the time?. |
| `--server ADDRESS\|none` | A server to connect to. |
| `--notes PATH` | Your notes folder (default: a vault found, else ~/Notes). |
| `--module NAME` | Also switch on a module; repeat for more. |
| `--runner RUNNER` | The AI tool ws runs jobs with. One of `claude-code`, `opencode`. |
| `--model ID` | The default model. |
| `--gate every-session\|launch` | `every-session`: the runner's own user config carries ws's deny rules and hooks, so every session is gated. `launch`: only a session started with `ws gate launch` is. |
| `--allow-widened-sandbox` | Keep settings of yours that open the sandbox (an excluded command, say); ws records the choice. |
| `--class NAME` | Print the commands (`sudo` included), the two root-owned files for a class with an `os_user` (the managed-settings file and the launch file) and the way to take it all out again, and write nothing. ws creates no user and places no file. The launch file goes in a `ws-classes` folder beside the managed-settings one, which `ws gate launch` reads and Claude Code doesn't. See [Set up a class user](../how-to/set-up-a-class-user.md). |
| `--cap USD` | Your own monthly spend cap. With this flag it's an admin command: agent sessions can't set it. |
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
nothing out, and a config written without the sandbox block stays without it. The one edit to your policy: a guard that
became a default since you wrote it (`pane-keys`) is added to the end of an existing `[guards] on` list, once, as a text
insert that keeps your comments and order, and one line says so. A policy with no `[guards]` table or `on` list, or that
already names it, isn't touched. If the rewrite fails, the runner's config,
the install record and the OpenCode plugin are put back as they were. Run it in a terminal of your own: a session, gated
or not, is refused (exit 3), and so is a run with no terminal. Exit 0 when done, 1 when the installer or the rewrite
failed, 3 when refused, 4 when files couldn't be put back after a failed rewrite (they're named). See
[Upgrade and uninstall](../how-to/upgrade-and-uninstall.md#upgrade).

The hooks step runs the ws the installer just wrote: ws reads it back from the `ws` command the installer put in its
folder (`UV_TOOL_BIN_DIR`, `XDG_BIN_HOME`, `PIPX_BIN_DIR` or `~/.local/bin`) and the Python named on that script's `#!`
line, and the step's line says which Python and version it ran with. The version recorded in `upgrade-history.jsonl`
is asked of the same ws.

Every run (`--dry-run`, `--rollback` and `--hooks-only` too) appends one line per step to `logs/upgrade.jsonl` in ws's
state folder: the time, the verb, the step (`installer`, `hooks`, `check`, `plan`, `broker`), the Python and version it
ran with, the exit code, how long it took, and the last 20 lines of the installer's or the hooks step's output. The
output still reaches the terminal line by line. The log isn't rotated, and a line that can't be written is reported and
never fails the upgrade.

After a swap or a rollback whose hooks step and `--check` both passed, ws asks the broker on your own socket
(`run/broker.sock`) for its version. A broker running other code than the ws now installed, holding no lease, and run
by your user (the kernel says so, through `SO_PEERCRED`, with a pid above 0; Linux only) is stopped through a pidfd
opened while it was answering, so the signal can't reach a later process given the same pid, and a new one is started
on the new ws. With a lease open, or a process ws can't tell is yours, it's left running, ws prints the command that
finishes the job, and the exit is 1; `--json` says what happened under `broker`. A one-shot broker call (an IMAP
operation, a token mint, a signature) that's in flight at that moment fails, and so does a lease asked for between the
status reply and the stop; each caller asks again. An automatic rollback never touches the broker.

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

## `ws commit-wall`

Refuse a commit whose added lines or message match a pattern you supplied.

ws ships no pattern: the wall reads `<config>/commit-wall/patterns.toml` (a `[[pattern]]` table has a `name` and either
`terms`, literal and any case, or a Python `regex`) and lets through what `<config>/commit-wall/exceptions.toml` lists
(`[[allow]]` with any of `pattern`, `path` and `text`; only a person adds to it, and the gate refuses a session's writes
to the folder). A hit prints the file, the line and the pattern's name, never the matched text. A merge is scanned for
the lines new to the merge, nothing is skipped (every file is read as text, file names and the whole message included),
a check with no pattern refuses, `git am` and a push are checked too, and a hook that can't run `ws` blocks the commit.

### `ws commit-wall install`

Point a repository's `core.hooksPath` at ws's own `pre-commit` and `commit-msg`, recording the old value. The repository's
other hooks are forwarded and its old `pre-commit` and `commit-msg` run after the check passes.

```text
ws commit-wall install [-h] [--repo REPO]
```

| Argument | Meaning |
|---|---|
| `--repo REPO` | The repository (default: here). |

### `ws commit-wall uninstall`

Put the recorded `core.hooksPath` back, or remove the setting when there was none.

```text
ws commit-wall uninstall [-h] [--repo REPO]
```

| Argument | Meaning |
|---|---|
| `--repo REPO` | The repository (default: here). |

### `ws commit-wall check`

Scan the staged diff, or a commit message with `--stage commit-msg FILE`, now. Exit 1 on a hit.

```text
ws commit-wall check [-h] [--stage {pre-commit,commit-msg,pre-push}] [--repo REPO] [--wall-dir WALL_DIR] [file]
```

| Argument | Meaning |
|---|---|
| `--stage STAGE` | `pre-commit` (the staged diff, default), `commit-msg` or `pre-push` (the commits a push would send, read from stdin). |
| `--repo REPO` | The repository (default: here). |
| `--wall-dir WALL_DIR` | The config folder to read (the hooks pass the one they were installed with). |
| `file` | The message file, for `--stage commit-msg`. |

### `ws commit-wall status`

The number of patterns and exceptions, and the repositories the wall is on for. A repository it recorded whose `core.hooksPath`
no longer points at the wall is listed as not on, and the command exits 1.

```text
ws commit-wall status [-h]
```

## `ws unit`

Install a committed systemd user unit. This is the only way one reaches the user manager. It's a person's command, like
`ws secret`: a gated session can't run it. The gate refuses a session's own write to `~/.config/systemd` and
`~/.local/share/systemd`, and `systemctl --user enable` or `start` for a unit that isn't the committed source.

```text
ws unit install NAME
```

| Argument | Meaning |
|---|---|
| `NAME` | The unit file name, for example `ws-lead-heartbeat.timer`. It's copied, with its drop-ins, from the first folder in `[gate] unit_sources` that holds it. The bytes come from git's `main` branch of the main checkout that holds the folder, never from the working tree. The folder can't be a symlink, hold a nested repository, or sit in a worktree. Everything is checked before anything is written. |

## `ws canary`

Check a runner still works with ws after an update (no model is called).

Small checks, no paid model. Each run adds one line to `~/.local/state/ws/logs/canary.jsonl`. Exit 1 on a failure.

```text
ws canary [-h] [--runner {claude-code,opencode}]
```

| Argument | Meaning |
|---|---|
| `--runner RUNNER` | Default: the profile's runner. One of `claude-code`, `opencode`. |

## `ws walls`

Set up a walled data class as an operating-system boundary, and check that it's in place. A walled class is optional:
it needs a second account on a Linux or macOS machine. ws never runs `sudo`, creates an account or writes under `/etc`;
it prints the commands. See [Set up a class user](../how-to/set-up-a-class-user.md) and
[ADR 0027](../decisions/0027-a-settings-file-per-walled-class.md).

```text
ws walls [-h] {init,edit,check} ...
```

### `ws walls init`

Print a `[classes.NAME]` table for a walled class: its own account, an `inbound` of `off`, an owner and a review date.

```text
ws walls init [-h] [--write] name
```

| Argument | Meaning |
|---|---|
| `name` | The class name: letters, digits, `_` and `-`. A name already in the policy is a usage error (exit 2). |
| `--write` | Append the table to your `policy.toml`: check the whole new file, then, holding the lock `ws install` takes on a file it writes, copy the old one to `policy.toml.bak-STAMP` (a `-1`, `-2` on a name already taken) and replace it atomically. It refuses a policy that's a link, and one that changed since ws read it (exit 1, `conflict`). It writes the `prepare` dial and never `act`. With no policy file, it starts from the default one. |

### `ws walls edit`

Open your `policy.toml` in `$VISUAL` or `$EDITOR`, on a temporary copy. The whole edited file is checked: an invalid one
is refused and the original stays as it was (the edit is kept in the temporary folder and the message names it). A valid
one replaces the file atomically, after a `policy.toml.bak-STAMP` copy, under the same lock and checks as `init --write`:
if the file changed while the editor was open, or is a link, the edit is refused and kept the same way. It's your own
edit of your own file, so a dial you set, `act` included, stays set. The temporary copy is removed in every other case.
Exit 2 with no editor set.

```text
ws walls edit [-h]
```

### `ws walls check`

Run the named cases without `sudo`, each checking that a setting is in place (it's not a live test that the runner
enforces it): inbound refused from outside the class, `Read` or `Edit` of a sealed file refused, a
class's file not given to another class, hooks run under bypass and under auto, the project check (`.mcp.json` and
settings files), a project can't switch the hooks off (`disableAllHooks`) or add plugins, per-repo settings only
narrow, OpenTelemetry content logging off, owner and review date,
`SendMessage` and `ListAgents` denied, and the class file root-owned. Exit 0 when all pass. Exit 1 with a typed error
naming the next command when the host isn't set up (no class user, or no class file: the commands and the way out
are printed, and the host cases are skipped) or when a case fails. With `--json`, the cases are in the envelope
(`data.cases` on success, `details.cases` on an error).

```text
ws walls check [-h]
```

The check also asks that the launch file blocks file-tool writes outside the working folders, denies the four sign-in
token variables to shell commands and switches off Remote Control and the claude.ai connectors, that a headless job's
settings keep its writes in the class, and that a `local_only` class takes only a model on this machine.

### `ws walls canary`

Run every case of `ws walls check` but the one that reads a file's owner on disk, on a world it plants under ws's own
state folder: three walled classes with accounts that don't exist, their managed and launch files as
`ws install --class` renders them, and the settings `ws install` generates. It needs no account, no root file and no
class of yours, so it runs on any machine and in CI. It then lists the items the cases cover (an inbound message, a
headless job, a planted token, a push to a remote outside the class, and the walled-session gaps G3 to G16 that are
built). Exit 0 when every case passes; exit 1 with a typed error naming the cases when a setting the wall writes is
missing. It proves what ws writes, not that a set-up host holds it: that's `ws walls check`.

```text
ws walls canary [-h]
```

## `ws passtest`

The step-in counter: how often a person had to come back and steer an AI tool, week by week. It reads a pass-test
ledger (an append-only, hash-chained jsonl file that a separate helper writes) and never writes to it.

### `ws passtest events`

Print confirmed step-ins per week by kind and by area, and the trend. The ledger's hash chain is checked first; a
broken or truncated chain counts nothing and exits 1 with the line number.

```text
ws passtest events [-h] [--weeks WEEKS] [--config CONFIG]
```

| Argument | Meaning |
|---|---|
| `--weeks WEEKS` | How many weeks (Monday 00:00 UTC), ending with this one (default 4, at most 520). |
| `--config CONFIG` | The settings file (default: `$WS_PASSTEST_CONFIG`). |

The settings file is TOML with two keys: `ledger` (a path, relative to the file) and `areas` (the areas always shown,
in order). `$WS_PASSTEST_LEDGER` overrides `ledger`. ws has no built-in ledger path: with neither set, the command exits
1 (`no_ledger`).

Rules. A proposal counts when a `confirm` names it, or once it has been open 7 days with no `confirm` or `dismiss`; the
latest decision wins, and it falls in the week of the proposal, not of the confirm. A `failed` or `missed` job run
is one `not-automatic` step-in per `run_id`, in the week of its `expected_at`. The trend is the least-squares slope of
the weekly totals: rising above +0.5 a week, falling below −0.5, flat in between. Nothing starts the count over.

### Pass-test ledger lines

Each line is `{"ts", "kind", "data", "prev"}`: `ts` is ISO 8601 UTC, `prev` the sha256 of the previous line's bytes
(without its newline; 64 zeros on the first line). Anything that writes step-ins writes these `data` keys:

| `kind` | `data` |
|---|---|
| `proposal` | `id`, `event_kind` (`dig`, `reframe`, `correct`, `follow-up`, `not-automatic`), `area`, `about` |
| `confirm`, `dismiss` | `proposal_id` |
| `job-run` | `job`, `run_id`, `status` (`ok`, `failed`, `missed`), `expected_at`; `area` optional |

Other kinds aren't step-ins and are read past. A line the counter can't use (no id, an unknown `event_kind` or
`status`) is listed as skipped.

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
ws notify send [-h] [--from-file PATH] [--fresh MINUTES] [--click URL] [--title TITLE] [--digest] [message]
```

| Argument | Meaning |
|---|---|
| `message` | The message text (or `--from-file`). |
| `--from-file PATH` | Send the first line with text in this file, such as the summary a job wrote (at most 300 characters). |
| `--fresh MINUTES` | With `--from-file`: send only if the file changed in the last MINUTES, so an old summary is never sent as news (exit 0, 0 sent). |
| `--click URL` | The link the push opens when tapped, such as your drafts folder (http or https). |
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
| `hub_url` | The hub's address, https only (no login, query or fragment); anything else is refused by `ws doctor` and by the alert. With it, a needs-you or finished alert carries ntfy's Click header, `<hub_url>/chat/<session id>`, so a tap opens the hub at that session. ntfy can't know where the hub is, so this is needed; it replaces no setting. Unset, alerts are as before. No link goes out for a session id that isn't letters, digits and hyphens, or for a session in a walled or counts-only class. Job-end pushes carry no link yet. |

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

### `ws schedule script`

Run a script job's reviewed script now. This is what its timer runs. It's refused (exit 3) unless the script is yours,
closed to other writers and hashes to `script_sha256`. The bytes that were hashed are what runs: ws runs a private copy,
kept under its own state folder, in the script's own folder, with `WS_SCRIPT_PATH` set to its real path. Only the
script is hashed. A file it sources or runs by a relative path is outside the review, so name what it calls by
absolute path, and keep those files closed to other writers too. The output and exit code are the script's.
Past the job's `timeout_s` it's stopped and the exit code is 124.

```text
ws schedule script [-h] job
```

### `ws schedule remove`

Stop one job's timer and delete its unit files.

```text
ws schedule remove [-h] job
```

| Argument | Meaning |
|---|---|
| `job` | The job whose timer to remove. |

## `ws watch`

What watches a machine from outside the sessions. The scheduler role runs these from timers (`ws schedule apply`), so
you rarely type them.

```text
ws watch alert UNIT          what a failed unit's OnFailure= runs: one line in logs/alerts.jsonl and one push
ws watch deadman             one alert for each job with no run record in twice its expected rate
ws watch rotate JOB          rotate the job's log file when it is over the cap
ws watch dropin UNIT... --out DIR   OnFailure= drop-ins for units ws did not write
ws watch panes [--after-minutes N]  push when a pane sat on a dialog or stopped on an API error; Esc only on Remote Control
```

### `ws watch panes`

A Claude Code dialog (a question, a permission, the Remote Control status box) holds every key and queued message until
someone presses one, and a session behind one looks idle. Each run reads the last 20 lines of every pane on the live tmux
server and looks for a dialog's footer: `Enter to select`, `Esc to cancel`, `Esc to continue`, `Enter to confirm`,
`Do you want to proceed?`. A dialog that has stayed the same for `after_minutes` (default 10) gets one push for that
episode through the notify role (the alert cap applies), naming the window, its `@id`, how long it has been up and the
dialog's first line. The episode ends when the dialog goes.

It also catches a Claude Code session that stopped at its input prompt after an API error it gave up on (a line
beginning `API Error:`, `Unable to connect to API`, `Connection dropped` or `Request timed out`, the texts on Claude Code's
[error reference](https://code.claude.com/docs/en/errors), within a few lines above the prompt). One that shows a spinner,
`esc to interrupt` or a retry countdown is still working and isn't reported. After `after_minutes` on the same error it
gets one push (`<window> stopped on an API error for N min`) and an `api-error-stuck` line in `logs/alerts.jsonl`. It
types nothing into the pane. The retry is Claude Code's own: for sessions nobody is watching, set
`CLAUDE_CODE_RETRY_WATCHDOG=1` so it keeps retrying overloaded and transient errors before it ever stops.

The only key it ever sends is `Escape`, to a pane showing the Remote Control status box (it holds Remote Control,
Disconnect this session and Esc to continue) once it has been up a minute; it logs that to `logs/alerts.jsonl` and
pushes once. It never presses anything on any other dialog: a question is yours to answer. `ws-watch-panes.timer` runs it
every minute (systemd, or `ws.job.watch-panes` on launchd) once any job is scheduled; with no scheduled job, run
`ws watch panes` from your own timer or cron. It does nothing without tmux or a running server. State is
`logs/panes-watch.json`.

An optional `[watch.panes]` table in the profile:

| Key | Default | Meaning |
|---|---|---|
| `after_minutes` | `10` | Whole minutes (1 to 1440) on one dialog before the push. `--after-minutes` beats it for one run. |
| `patterns` | `[]` | More footer text to count as a dialog, added to the five above (case ignored). |
| `live_socket` | the policy's | The tmux socket to watch (a path, or a name under the tmux folder). Left out, the `live_socket` of the `pane-keys` or `pane-close` guard, else this user's default socket. |
| `show_text` | `true` | `false` leaves the dialog's text out of the push (the window name and `@id` stay), for a machine where a question must not leave the box. Whatever this says, a pane in a walled or counts-only class (judged by its folder), or a policy that can't be read, never has its text quoted. |

## `ws worktrees`

Removes the git worktrees agent sessions leave under `<repo>/.claude/worktrees/`.

```text
ws worktrees prune [--repo R]... [--only PATH] [--min-idle HOURS] [--dry-run] [--auto]
```

A worktree is kept, and the reason counted, when it's `locked`, `dirty` (a tracked file has a change), `fresh` (a
file in it changed in the last `idle_hours`, default 12), `in-use` (a process has its working folder or an open file
inside it: `/proc` on Linux, `lsof` on macOS; a process of another account can't be seen), `busy-unknown` (the process
list doesn't show the caller's own parents, as in a PID namespace or with `hidepid`, so use can't be ruled out and every
worktree stays), or `unreachable` (HEAD is detached and no branch, remote or tag holds it). Everything else is removed
with `git worktree remove`, never `--force`: git still refuses a folder with untracked files, and that's reported as
`failed` and left registered. Only a folder where `git status` shows nothing to lose is first made writable, and a file
with more than one hard link keeps its mode. Nothing runs `git worktree prune`, so other registered worktrees are left
alone. Branches and commits stay; the main checkout and worktrees elsewhere are never touched;
ignored files (virtualenvs, caches) go with the folder. Each removal is one line in `<state>/worktree-prune.log`. The
run prints `checked N, removed M, kept K (reasons), X GB freed`; `--dry-run` changes nothing and prints the same,
saying would remove. Exit 1 when a removal failed.

| Option | Meaning |
|---|---|
| `--repo R` | A repo to prune, repeatable. Default: the repo the current folder is in. |
| `--only PATH` | One worktree, for a session that has just finished. Its head must also be on a remote ref (`unpushed` otherwise), and the caller's own parents aren't counted as users. |
| `--min-idle HOURS` | Idle hours before a worktree may go (default `idle_hours`). `0` skips the idle check. |
| `--auto` | The timer's run, below. Takes no other option. |

A repo is pruned only from inside its own zone: a caller in a data class may prune that class's repos, and another
class of `full` access when it's `full` itself; a caller in no class (a timer) may prune `full` classes. Any other repo
is refused (exit 3) before git runs in it, and a policy that can't be read refuses all. With no classes in the policy
nothing is refused.

`ws-worktrees-prune.timer` (`ws.job.worktrees-prune` on launchd, written by `ws schedule apply`) runs
`ws worktrees prune --auto` every 30 minutes. It reads the disk (`/`) and prunes when `interval_hours` (6) have passed or
the disk is at `disk_pct` (85) or more. On that pressure it also tells the lead once per interval, with the numbers before
and after: a `ws msg` when the registry lets it send, always a line in `logs/alerts.jsonl` and a push through the notify
role. The repos are those the policy's classes name (a literal folder or a `*` glob of one), `[worktrees] repos`, or
`~/code/*` when there are no classes. `<state>/worktree-prune.json` holds the last run's `worktrees_pruned`, `gb_freed`
and `disk_pct`.

An optional `[worktrees]` table in the profile:

| Key | Default | Meaning |
|---|---|---|
| `idle_hours` | `12` | Hours with no change before a worktree may go. |
| `interval_hours` | `6` | Hours between the timer's prunes. |
| `disk_pct` | `85` | Disk use at which the timer prunes at once and tells the lead. |
| `repos` | `[]` | More repos for `--auto`. |

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
| `--cap USD` | Your own monthly spend cap. With this flag it's an admin command: agent sessions can't set it. |
| `--region CC` | Your country (two letters); kept in `[machine] region`. |

## `ws config`

Every file ws reads: where it's, which folder layout it came from, the keys it sets. `ws config fallback` shows, sets and
clears the program that gets words ws has no command for (see [Your own commands](#your-own-commands)). `ws config model`
and `ws config budget` show and set which model a job runs on and the monthly spend cap (see [Profile](profile.md)).

```text
ws config [-h] [--clear] [--yes] [--dry-run] [{fallback,model,budget}] [VALUE ...]
```

| Argument | Meaning |
|---|---|
| `fallback` | Show the fallback program. With `PATH`, set it; with `--clear`, remove it. |
| `PATH` | With `fallback`: the program (absolute, or `~/...`). It has to be an executable file you own that nobody else can write to. |
| `--clear` | With `fallback`: remove it. |
| `model` | No value: each job, its purpose, its model and where it came from. `ID`: set `[models].default`. `PURPOSE ID`: set `[models].PURPOSE`. With a value it's an admin command (agent sessions can't run it). |
| `budget USD` | Set `[roles.budget] monthly_cap_usd`. A positive number. An admin command (agent sessions can't run it). |
| `--yes` | With `model` or `budget`: write without asking. |
| `--dry-run` | With `model` or `budget`: print the change, write nothing. |

## `ws status`

Per runner, every rules, skill, hook and MCP file it will load here, and its origin.

```text
ws status [-h]
```

A path is listed once per runner. With `--json`, each row has a `folder` key: `true` when the row is a folder (a
skill folder with no main file), `false` for a file. The text output shows a folder with a trailing `/`.

Two more fields come from Claude Code's settings files, with a line each in the text output:

- `.data.sandbox` is `{enabled, allowUnsandboxedCommands, failIfUnavailable, source}`: booleans, or `null` when the file
  doesn't say. `source` is the file they were read from: first the runner's user settings when they carry ws's entries
  (the `WS_SANDBOXED` marker), then `gate/claude-code/settings.json` in ws's state folder, else `null`. Ws entries with
  no sandbox block (the policy's `sandbox = false`) read `enabled: false`.
- `.data.permission_mode` is `{mode, source}`: the `permissions.defaultMode` in force and the file that sets it, the
  highest-precedence file winning (managed, then local and project, then your own). Both are `null` when no file sets
  one. `bypassPermissions` or `auto` in a project file is skipped, because Claude Code ignores those modes from there. The
  text output flags `bypassPermissions`. ws never writes `defaultMode`.

Each object gains an `unreadable` list (`"path: why"`) only when a settings file couldn't be read: a link (never
followed), a file over 1 MB, a file that isn't a JSON object, or a read error. Nothing else of a settings file is shown.

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
| `input` | text | One file inside `cwd`, read at each run and put after the prompt as untrusted data. See [A data file for the job](#a-data-file-for-the-job-input). |
| `timeout_s` | number | Time limit. Default 900. |
| `max_turns` | integer | Optional turn limit. |
| `bare` | true or false | Headless default: no hooks, plugins or discovered on their own context. Default `true`. |
| `allow_unpriced` | true or false | Run even if the runner can't price the model. Default `false`. |
| `green` | text | What the job's verification covers: `targeted`, `package`, `full` or `merge_ready`. |
| `sources` | list of text | Policy sources the job reads. The Rule of Two looks at these. |
| `schedule` | text | `hourly`, `daily HH:MM`, `weekly DAY HH:MM`, `monthly D HH:MM` (D from 1 to 28) or `every Nm`, in local time. `ws schedule apply` turns it into a timer. A job with an API key or a local provider ticks the queue (`ws queue tick JOB --slot schedule --run`). A job on a subscription login keeps `ws run JOB`. |
| `coalesce_if_active` | true or false | A tick while a run of the job waits or runs queues nothing. Default `true`. `ws queue tick --no-coalesce` overrides it. |
| `skip_missed` | number | Seconds: a tick later than this after its slot is skipped. Default: never skipped. `ws queue tick --skip-missed` overrides it. |
| `parent_run` | text | The id of a run in progress whose reservation this run spends from. `ws run --parent` sets it for one run. |
| `command` | text | In place of `runner`, `model` and `prompt`: a ws command that the schedule runs (`backup`, `queue` or `script`). `ws run` refuses these. |
| `extra_sources` | list of text | With `command = "backup"`: more folders or files to back up, absolute or under `~`. |
| `script` | text | With `command = "script"`: the script the timer runs through `ws schedule script JOB`, absolute or under `~`. It must be yours and closed to other writers. |
| `script_sha256` | text | With `command = "script"`: the script's sha256. ws refuses a script that no longer hashes to it, so a change needs a new review. |
| `[[secrets]]` | table list | Each has `name`, `host` and `purpose` (default `run`). The broker leases it. |
| `[runner_options]` | table | Adapter-specific. Claude Code: `allowed_tools`, `env`. OpenCode: `provider`, `env`, `base_path`, `offline`. |

### A local model server (OpenCode)

An OpenCode job can use a model server on your machine or your local network (Ollama, llama.cpp, LM Studio, vLLM) by
naming it in `runner_options.provider`. ws puts the table into the config it hands OpenCode for the run, and nothing
else: each run starts with a fresh home folder and the project's own config switched off, so a project
`opencode.json` is never read, and nor is the project's `AGENTS.md`: give the job its data with
[`input`](#a-data-file-for-the-job-input). This key replaces a provider block kept in a project `opencode.json`.

```toml
runner = "opencode"
model = "ollama/qwen3:8b"           # <provider id>/<model id>: the provider id names the table below
prompt_file = "daily.md"

[runner_options.provider]
npm = "@ai-sdk/openai-compatible"
name = "Ollama"
options = { baseURL = "http://127.0.0.1:11434/v1", timeout = 600000 }
[runner_options.provider.models."qwen3:8b"]
name = "Qwen3 8B"
limit = { context = 32768, output = 4096 }
cost = { input = 0, output = 0 }
tool_call = false                        # optional: turn this model's tools off
options = { reasoningEffort = "none" }   # optional: turn its reasoning down
```

The table is an allowlist, checked by `ws job add` and again by `ws run` (so a job file written by hand meets it too):

- `npm` is `@ai-sdk/openai-compatible`; the other keys are `name`, `options.baseURL`, `options.timeout`
  (milliseconds, or `false`) and, per model, `name`, `limit`, `cost`, `tool_call` and `options.reasoningEffort`. Two
  of those only narrow a model: `tool_call` may only be `false`, and `reasoningEffort` may only be `none`, `minimal`
  or `low`. `true` and the higher efforts (`medium`, `high`, `xhigh`, `max`) are refused, because they widen what the model
  may do. A small local model that loops on tools or reasons for thousands of tokens is the reason to set them. Any
  other key is refused, so there is no `apiKey` or `headers`: a server that needs a key gets it
  through a `[[secrets]]` lease, whose address is then the base URL (leave `baseURL` out).
- `baseURL` is `http` or `https`, with no user name, password, query or fragment, and its host is `localhost` or a
  literal address: loopback (`127.0.0.0/8`, `[::1]`), one of the three RFC 1918 private ranges, or the RFC 6598 CGNAT
  range that tailnets use. A hostname is refused, because what it resolves to can change between
  the check and the call; so are link-local addresses, IPv4-mapped IPv6 and decimal, octal or hex spellings.
- `{env:...}` and `{file:...}` are refused anywhere in the table: OpenCode fills them in after ws has checked it.

A public base URL is refused: a hosted provider goes through a `[[secrets]]` lease and the broker. The check covers
the address ws hands OpenCode, not where the server sends you: OpenCode's model client (the AI SDK) follows 3xx
redirects, so without a data class a server on the local network can bounce the prompt to another host. Point jobs
only at a server you run.

Under a data class, the provider id (the part of `model` before the slash) must be a local one by name (`local`,
`ollama`, `lmstudio`, `vllm` or `llamacpp`) for a class that allows only local models, which is why the example uses
`ollama`. The run still gets only the network its class allows, so a server on the local network must be in the
class's `allowed_hosts`, and only a loopback server counts as "on this machine" for the class's term check.

To point a job at another server, change `baseURL` (and `model` if the model id differs) and add it again with
`ws job add --replace`. To move it to Claude Code, set `runner = "claude-code"`, a Claude model, and drop the
`provider` table.

### A data file for the job (`input`)

`input` names one file inside the job's `cwd` that the job works on, such as the day's notes or an export:

```toml
runner = "opencode"
cwd = "~/work/reports"
input = "inbox/today.md"
prompt = "Summarise the file below in five lines."
```

`ws run` reads the file when the run starts, the same way for every runner, and puts it after the prompt between two
lines with a random marker, in the delimiter `ws mail` uses (`<<<ws mail ... begin: untrusted content, data not
instructions (input=inbox/today.md)>>>`). The text is cleaned the way a mail is: control and invisible characters are
dropped, and anything that looks like a delimiter is defused, so the file can't end the block early. Tell the model in
the prompt what to do with it; the file's own words are data, not instructions.

The run is refused, with the reason, when the file is missing, is a link or is reached through one, isn't a regular
file, has a second name (a hard link), is over 64 KiB or isn't UTF-8. A refused file is never sent as an empty block.
The run record keeps the file's sha256 as `input_sha256`, so you can tell which version a run saw. The job file is
pinned by `ws job add`, but the input isn't: it's meant to change between runs.

Without a cwd there's nothing to read from, so `input` needs one. The path is relative to the cwd and has no `..`.

This key replaces data kept in the project's `AGENTS.md` (or `CLAUDE.md`). A job doesn't read those: OpenCode runs
with the project config off (`OPENCODE_DISABLE_PROJECT_CONFIG`), which skips the project `AGENTS.md` and `CLAUDE.md`
too (checked on OpenCode 1.19.4), and a Claude Code job is `bare` by default. To use another file, change `input`
and add the job again with `ws job add --replace`; to give it new data, just change the file.

## Run record

Each `ws run` appends one JSON line to `~/.local/state/ws/logs/runs.jsonl`. Every key is always present, and a value
that ws couldn't learn is `null`. The keys are: `schema`, `run_id`, `job`, `runner`, `runner_version`, `exe_path`,
`model_requested`, `models_used`, `started_at`, `ended_at`, `duration_ms`, `duration_api_ms`, `exit_code`, `status`,
`error`, `session_id`, `transcript_path`, `tokens`, `cost_usd`, `cost_basis`, `num_turns`, `cwd`, `policy`,
`secrets_used`, `ws_version`, `output_truncated`, `allow_unpriced`, `green`, `recovered` and `eval`. `status` is `ok`,
`error`, `timeout` or `refused`. The file is mode 0600 and rotates at 5 MB, keeping three old files.
