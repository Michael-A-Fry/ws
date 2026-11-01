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
| `bulk-staging` | before a call | shell commands | Stops `git add -A`, `--all` (and a prefix such as `--al`), `.`, `./`, `*`, `:/` and `:(top)`, the same with `git stage`, and `git commit -a`, `-am` and `--all`, with or without git's global options. Stops `git -c alias.NAME=...`, which can define a command that stages everything. `git add file` and `git add -- file` go through. Text in quotes, comments and here-document bodies is data, so `git commit -m "git add -A"` goes through; the text after `eval` and in `bash -c` is a command and is read. The runner's own deny rules carry the plain spellings too. |
| `syntax-check` | after a call | file writes | Checks the file the session wrote and tells the model the line that doesn't parse. Python, JSON and TOML are read in process. `.sh` and `.bash` go through `sh -n` or `bash -n`, with a note when the machine has no shell. |
| `session-context` | at session start | none | Lists, one line each, which of `files` exist in the project: the git top level above the session's folder, and the main worktree when the session runs in a linked one. |
| `test-weakening` | before a call | file writes | Applies the edit to the test file on disk and compares before and after. Denies a skip or expected-failure marker added to a test that already exists; the person can approve it once. Warns about a test removed, fewer assertion lines, or an always-true assertion added. Judges Edit, Write and MultiEdit calls only. |
| `claims` | before a message, and when the session stops | messages to other agents | Checks each commit-like token that sits in the same sentence as a claim word (`on main`, `merged into main`, `merged to main`, `pushed to main`) against a ref, and denies the message when the commit isn't on it. On stop, logs the claims in the runner's last message. |
| `spawn-cap` | before a call, and when a subagent stops | subagent starts | Denies a subagent start when `max_agents` (default from the machine) are already running in the session. Counts agents started in the background. |
| `skill-gate` | before a call, and when a prompt is sent | file writes and skill calls | Denies a write to a guarded path until the named skill has run in the session. The person can approve the write once. Judges Edit, Write and MultiEdit calls only. |
| `record-calls` | before a call | every kind | Appends `{at, runner, payload}` to `$WS_HOME/logs/gate-calls.jsonl`, one line for each hook run. A file write keeps its path and records the text as `{"bytes", "sha256"}`. `ws gate replay` reads that file. |

`bulk-staging` and `record-calls` take no table. The four guards below take one.

`test-weakening` and `skill-gate` judge Edit, Write and MultiEdit calls. A change made through the shell, such as
`sed -i` or a redirect, or through a notebook edit, isn't seen by them. The sandbox is what stops a write; these guards
tell the model what it's about to break.

The per-session records under `$WS_HOME/sessions` (`skill-gate`'s record of skills run, `spawn-cap`'s count) are
advice-level. A session that can write that folder can change them, so they keep an honest session honest and don't stop
a hostile one. The sandbox is the wall.

`syntax-check` takes `extra`, a table of lower-case extension to checker command. ws adds the file path as the last
argument and gives the checker 10 seconds. An exit code other than 0 is a finding that quotes the checker's first lines.
A checker that isn't installed, or runs out of time, is a note. An entry in `extra` replaces the built-in check for that
extension.

`session-context` takes `files`, a list of paths relative to the project (default `[".claude-state/STATE.md"]`). A path
must stay inside the project: no leading `/` or `~`, no `..`.

`record-calls` is off unless you name it. A line holds the hook payload as the runner sent it, with one change: the text
a write tool carries (Write `content`, Edit `old_string` and `new_string`, each MultiEdit edit's two strings, NotebookEdit
`new_source`) becomes its size in bytes and a SHA-256, and the path stays. Replay judges a call by its tool, path and
command, so it runs a line as it is. A command is kept as the session typed it, so a value typed into a command is in
the log. The log has the bounds of every ws log: a long field is cut and marked, the file rotates, and
writers take a lock. The `logs` folder is one a session can't write.

### test-weakening

```toml
[guards.test-weakening]
paths = ["tests/**"]               # optional: replaces the default list of test files
except = ["tests/fixtures/**"]     # optional: replaces the default list of files that aren't tests
```

A test file is one under `tests/`, `test/` or `__tests__/`, or named `test_*.py`, `*_test.*`, `*.test.js`, `*.test.jsx`,
`*.test.ts`, `*.test.tsx` or `test-*.R`. Files under `fixtures/`, `testdata/`, `snapshots/` and `golden/` aren't. A
pattern with no `/` matches at any depth, and `**` crosses folders.

The guard builds the file's text after the write (Edit, Write and MultiEdit; not a notebook) and reads both texts by
line. A test is a `def test...` with its decorators, an `it(` or `test(` with a title, `test_that("...")`, a Go
`func TestX(`, a Rust `fn test_x(` or a Java method after `@Test`. Skip markers are counted for each test, so a new test
or a new file can carry one. A marker added to a class, or to the file as a whole, in a file that already had tests is
also a deny. A plain `describe(` block isn't a marker; `xdescribe(` and `describe.skip` are. The deny is approvable: the
reason names `ws approve`, and the person can allow the edit once. A write it can't apply, such as a file that
doesn't exist yet or an edit whose old text isn't in the file, isn't judged.

`ws gate replay` judges a line only when the line holds the text of the edit. A line from `record-calls` keeps a
fingerprint of that text instead, so write the payload by hand to replay this guard.

### claims

```toml
[guards.claims]
words = ["on main", "merged into main", "merged to main", "pushed to main"]   # optional
ref = "origin/main"                        # optional
```

A claim is a phrase from `words` (whole words, any case) in the same sentence as a commit-like token: 7 to 40
lower-case hex characters with at least one digit and one of `a` to `f`. A commit may sit inside the phrase, as in
`merged abc1234 into main`. The default phrases are about `main` only, because the check is against one ref. The guard
runs `git merge-base --is-ancestor TOKEN REF` in the session's folder. A commit that isn't an ancestor of `ref`, or
isn't in the repository, denies the message and the reason names it. If `ref` may be out of date, the reason says to run
`git fetch` first; the guard never fetches. A claim word in quotes or backticks isn't a claim, and neither is one with a
negation or a future form in the four words before it (`not merged`, `will be pushed`, `once it's merged`). A sentence
that names another branch, such as `origin/work` or `pushed to work`, isn't checked either. A folder that isn't a
repository, a machine with no git, a `ref` that doesn't exist there and git taking more than 8 seconds in total for one
message each let the message through with a note. Git runs with `GIT_NO_LAZY_FETCH=1`, so a check never goes to the
network.

When the session stops, the claims in the runner's last message (the stop payload's `last_assistant_message`, when the
runner sends one) are appended to `$WS_HOME/logs/claims.jsonl` with whether each commit was on `ref`.

### spawn-cap

```toml
[guards.spawn-cap]
max_agents = 4      # optional, 1 or more; left out, it comes from the machine (see below)
stale_s = 1800      # optional: seconds before an unfinished start stops counting
```

The guard counts the subagents this session has started and not seen stop. Before the call it only reads the count;
at `max_agents` it denies the start and the reason says how many are running. The start is recorded after the call, in
the `PostToolUse` hook, so a call that was denied or that you declined uses no slot. Only an agent started with
`run_in_background` is recorded: a foreground agent has already stopped when its call returns. The count is an
estimate: agents started together in one message are all checked before any is recorded, a stop clears the oldest start
because the stop payload doesn't say which agent it was, and a start older than `stale_s` drops out. The record is
`$WS_HOME/sessions/<session id>.spawn-cap.json`.

With no `max_agents`, the limit is worked out from the machine each time: the smaller of half the CPUs and half the
memory in GiB, at least 2 and at most 8. The CPUs are the cgroup v2 `cpu.max` quota divided by its period when one is
set (a container or service limit), never more than the machine has, else `os.cpu_count()`. The memory is
`memory.max` when it's set, else it isn't a limit. A file that's missing, says `max` or can't be read counts as no limit.
A 4 CPU, 16 GiB container gets 2; an 8 CPU, 8 GiB machine gets 4; a 32 CPU host with no limit gets 8. The reason a
start is denied names the limit in force.

### skill-gate

```toml
[[guards.skill-gate]]
skill = "taste-gate"
paths = ["**/*.css", "**/templates/**"]
```

One table for each skill. A Skill tool call for the skill, or a prompt that starts with `/taste-gate` (or
`/plugin:taste-gate`), records it for the session. A write to a path in `paths`, before the skill is recorded, is a
deny you can override once with `ws approve ID`. After the skill is recorded the paths are open for the rest of the
session. The record is `$WS_HOME/sessions/<session id>.skill-gate.json`.

## Findings

A guard returns nothing, or a finding with an action:

| Action | Before a call | After a call | Other events |
|---|---|---|---|
| `deny` | The call is stopped and the reason goes to the model. | The reason goes to the model on standard error, exit code 2. The tool has already run. | Text for the session or a log line. |
| `warn` | A line on standard error, exit code 0. | As `deny`. | As `deny`. |
| `note` | A line on standard error, exit code 0. | A line on standard error, exit code 0. | Text for the session or a log line. |

A `deny` marked approvable can be overridden once by you with `ws approve ID` in a terminal of your own, the same way
`ask_commands` works: the approval is bound to the runner, the call, the folder and the session. See
[Approve a command](../how-to/approve-a-command.md). `skill-gate` and `test-weakening` ship with an approvable deny; the others stop the call outright.

The first `deny` in the order of `on` wins. A reason is shown as `guard-name: reason`.

## Events

| Event | Claude Code hook | `ws gate event` answers with |
|---|---|---|
| `pre` | `PreToolUse` | `ws gate check` answers this event. |
| `post` | `PostToolUse` | A `deny` or `warn` as standard error with exit code 2. A `note` as standard error with exit code 0. |
| `start` | `SessionStart` | Findings as text on standard output, which Claude Code adds to the session's context. Exit code 0. |
| `prompt` | `UserPromptSubmit` | Findings as text on standard output. Exit code 0. |
| `stop` | `Stop` | Findings as lines on standard error. Exit code 0. |
| `subagent-stop` | `SubagentStop` | Findings as lines on standard error. Exit code 0. |

`ws gate event` is advice. A payload it can't read, a policy that doesn't load and a guard that crashes are each one line
on standard error and exit code 0. The deny path is `ws gate check`, which fails closed.

## What `ws gate generate` writes for Claude Code

The settings carry a hook for an event only when a guard that's on runs there:

| Hook | Added when |
|---|---|
| `PreToolUse` matcher gains `Agent\|Task`, `Skill` and `SendMessage` | A guard that's on reads agent, skill or message calls before a call. The matcher always has the shell, file, search and web tools. |
| `PostToolUse` running `ws gate event` | A guard that's on runs after a call. The matcher lists the tools for the kinds those guards read. |
| `SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStop` running `ws gate event` | A guard that's on runs at that event. |

The `PreToolUse` hook ends `|| exit 2`, so a missing or crashing ws blocks the call. The `PostToolUse` hook has no
`|| exit`: `ws gate event` exits 2 on purpose after a tool call, and Claude Code shows that message to the model. Every
other hook command (`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStop`) ends `|| true`, so the session carries
on whatever it returns.

The settings `env` carries `WS_GATED_SESSION=1` and `WS_SANDBOXED=1`. The hook checks a project's own
`.claude/settings*.json` for keys that open the sandbox in every session except one whose settings set
`WS_SANDBOXED=0`, the marker of settings written without ws's `sandbox` block. Settings generated before the marker
existed have the block and no marker, so the check runs for them.

Every generated settings file also carries two hooks that aren't guards: `Notification` and `Stop` run
`ws notify alert --runner claude-code || true`, which sends the phone alert. `Stop` runs it after any guard hook for
that event.

OpenCode's adapter lists the guard names it doesn't run yet in `UNSUPPORTED_GUARDS`; that's every guard for now.

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
