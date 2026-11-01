# Guards reference

A guard is a small named check that the policy switches on. Guards run beside the `[gate]` rules, at the points in a
session where ws can look: before a tool call, after one, when the session starts, when a prompt is sent and when the
session or a subagent stops. For the idea, read [The gate](../explanation/gate.md). To turn one on, follow
[Use the gate](../how-to/use-the-gate.md).

## Turn guards on

```toml
[guards]
on = ["syntax-check", "session-context"]     # the guards that run, in this order

[guards.syntax-check]
extra = { yaml = ["yamllint", "-d", "relaxed"] }

[guards.session-context]
files = [".claude-state/STATE.md"]
```

A policy file with no `[guards]` table has no guards on, so an upgrade changes nothing until you add the table.
`ws gate init` writes `on = ["syntax-check", "session-context"]` and lists the other guards as comments. A name in `on`
that ws doesn't know stops the load and the message names it. So does a key in `[guards]` that isn't `on` or a guard
name, and a key in a guard's table that guard doesn't read.

## The guards

| Guard | Runs | Reads | What it does |
|---|---|---|---|
| `bulk-staging` | before a call | shell commands | Stops `git add -A`, `--all`, `.`, `./` and `:/`, and `git commit -a`, `-am` and `--all`, with or without git's global options. `git add file` and `git add -- file` go through. Text in quotes and here-document bodies is data, so `git commit -m "git add -A"` goes through. The runner's own deny rules carry the plain spellings too. |
| `syntax-check` | after a call | file writes | Checks the file the session wrote and tells the model the line that doesn't parse. Python, JSON and TOML are read in process. `.sh` and `.bash` go through `sh -n` or `bash -n`, with a note when the machine has no shell. |
| `session-context` | at session start | none | Lists, one line each, which of `files` exist in the project: the git top level above the session's folder, and the main worktree when the session runs in a linked one. |
| `record-calls` | before a call | every kind | Appends `{at, runner, payload}` to `$WS_HOME/logs/gate-calls.jsonl`, one line for each hook run. `ws gate replay` reads that file. |

`bulk-staging` and `record-calls` take no table.

`syntax-check` takes `extra`, a table of lower-case extension to checker command. ws adds the file path as the last
argument and gives the checker 10 seconds. An exit code other than 0 is a finding that quotes the checker's first lines.
A checker that isn't installed, or runs out of time, is a note. An entry in `extra` replaces the built-in check for that
extension.

`session-context` takes `files`, a list of paths relative to the project (default `[".claude-state/STATE.md"]`). A path
must stay inside the project: no leading `/` or `~`, no `..`.

`record-calls` is off unless you name it. A payload holds the command text as the session typed it, so a value typed into
a command is in the log. The log has the bounds of every ws log: a long field is cut and marked, the file rotates, and
writers take a lock. The `logs` folder is one a session can't write.

## Findings

A guard returns nothing, or a finding with an action:

| Action | Before a call | After a call | Other events |
|---|---|---|---|
| `deny` | The call is stopped and the reason goes to the model. | The reason goes to the model on standard error, exit code 2. The tool has already run. | Text for the session or a log line. |
| `warn` | A line on standard error, exit code 0. | As `deny`. | As `deny`. |
| `note` | A line on standard error, exit code 0. | A line on standard error, exit code 0. | Text for the session or a log line. |

A `deny` marked approvable can be overridden once by you with `ws approve ID` in a terminal of your own, the same way
`ask_commands` works: the approval is bound to the runner, the call, the folder and the session. See
[Approve a command](../how-to/approve-a-command.md). The guards that ship stop the call outright.

The first `deny` in the order of `on` wins. A reason is shown as `guard-name: reason`.

## Events

| Event | Claude Code hook | `ws gate event` answers with |
|---|---|---|
| `pre` | `PreToolUse` | Not this command: `ws gate check` answers it. |
| `post` | `PostToolUse` | A `deny` or `warn` as standard error with exit code 2. A `note` as standard error with exit code 0. |
| `start` | `SessionStart` | Findings as text on standard output, which Claude Code adds to the session's context. Exit code 0. |
| `prompt` | `UserPromptSubmit` | Findings as text on standard output. Exit code 0. |
| `stop` | `Stop` | Findings as lines on standard error. Exit code 0. |
| `subagent-stop` | `SubagentStop` | Findings as lines on standard error. Exit code 0. |

`ws gate event` is advice. A payload it can't read, a policy that doesn't load and a guard that crashes are each one line
on standard error and exit code 0. The deny path is `ws gate check`, which fails closed.

## What `ws gate generate` writes for Claude Code

The settings carry a hook for an event only when a guard that is on runs there:

| Hook | Added when |
|---|---|
| `PreToolUse` matcher gains `Agent\|Task`, `Skill` and `SendMessage` | A guard that is on reads agent, skill or message calls before a call. The matcher always has the shell, file, search and web tools. |
| `PostToolUse` running `ws gate event` | A guard that is on runs after a call. The matcher lists the tools for the kinds those guards read. |
| `SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStop` running `ws gate event` | A guard that is on runs at that event. |
| `Notification` and `Stop` running `ws notify alert --runner claude-code \|\| true` | Always. |

The `PreToolUse` hook ends `|| exit 2`. The other hooks carry no `|| exit`: `ws gate event` exits 2 on purpose after a
tool call, and Claude Code treats any other hook failure as non-blocking.

The settings `env` carries `WS_GATED_SESSION=1` and `WS_SANDBOXED=1`. The hook checks a project's own
`.claude/settings*.json` for keys that open the sandbox only when `WS_SANDBOXED` is `1`, because that is the session
that has ws's `sandbox` block.

OpenCode's adapter lists the guard names it doesn't run yet in `UNSUPPORTED_GUARDS`; that is every guard for now.

## Replay

```sh
ws gate replay FILE --runner claude-code [--policy FILE] [--json]
```

Each line of `FILE` is one JSON object: a hook payload, or `{"payload": {...}, "expect": "deny"}`. The lines
`record-calls` writes work as they are. Each call is judged as the live hook would judge it and prints `allow`, `deny`
or `ask` (a call that `ask_commands`, or an approvable guard, holds for your approval). Replay uses up no approval and
keeps no guard state. The exit code is 1 when a line's `expect` differs from the result, and 0 otherwise.

## Write a guard

A guard is one module in `src/ws/guards/`. The folder is the registry, and a name starting with `_` is a helper. The
package docstring in `src/ws/guards/__init__.py` is the contract: `NAME`, `EVENTS`, `KINDS`, `config(table, where)`,
`judge(call, cfg, ctx)` and, optionally, `NATIVE_DENY`. Nothing else changes: the policy loads the new name and the
generated hooks follow the events and kinds it declares.

Each call a guard sees is a neutral `Call` with a `kind`, a `value` and read-only `data`. The kinds are `command`,
`read`, `search`, `write`, `tool`, `agent`, `skill` and `message`. A write carries `old`, `new`, `content` or `edits`
when the tool gives them. A guard keeps its own per-session record with `ctx.state(name, update)`, a locked JSON file at
`$WS_HOME/sessions/<session id>.<name>.json` that goes when the session record is pruned.
