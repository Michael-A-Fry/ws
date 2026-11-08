# Gate reference

What `ws gate` does with a call, and how to measure it. For the idea, read [The gate](../explanation/gate.md). For
the rules, see the [policy reference](policy.md) and the [guards reference](guards.md).

## The resident gate

A runner calls its hook before every tool call, and starting Python and loading ws for each call costs about 285
milliseconds. So every generated hook runs a thin client (`ws.gateclient`, standard library only) that asks one resident
process, `ws gate serve`, for the decision. The daemon answers with the same code `ws gate check` runs, in the same
order, so the answer is the same.

```sh
ws gate serve [--idle SECONDS]     # started by the first hook call; one for each user and state folder
ws gate serve --stop               # ask it to exit
```

How it behaves:

- **Start and stop.** The first hook call that finds none running starts one in the background and decides that call
  itself. The daemon exits after 15 minutes without a request (`--idle`), when its socket file is removed, and when ws
  is upgraded under it. Each request reads the policy again, so a policy change needs no restart.
- **Who can answer.** The socket is in `<state>/run`, a folder that must be private to you (mode 0700). The client uses
  the socket only when the folder and the socket both belong to you and nobody else can enter them, no link is followed
  on the way, the kernel says the process on the other end is yours, on Linux that process is your interpreter running
  `-m ws gate serve` (read from `/proc`), and the reply carries the request's random id and an exit code of 0, 2 or 4.
  A process of another user can't answer in the daemon's place. A process of your own user outside the sandbox could
  stop the daemon and start one that looks right: with the sandbox on, the run folder is read-only to the session, and
  without it the gate's static check of writes is the barrier (see [known limits](../explanation/known-limits.md)).
- **When it can't answer.** No daemon, a dead socket, a time-out, a reply that doesn't read, or a daemon from an older ws:
  the client runs `ws gate check` (or `event`) itself with the same payload. The same code gives the same answer, only
  slower. A missing daemon never allows a call.
- **A hook that fails blocks.** Every generated `PreToolUse` hook, and the `PostToolUse` one, sets `onFailure: "block"`,
  so a hook that crashes or times out blocks the call. That needs Claude Code 2.1.295 or newer; `ws doctor` (WS011)
  checks it. The hook's time-out is 30 seconds, more than the client's worst case (1 second to connect, 11 for the
  reply, then 12 for the check in its own process), so the runner doesn't kill a hook that was about to answer.
  The hooks that only add advice (session start, prompt, stop) don't set it, so a slow one can't block a prompt.
- **Turn it off.** `WS_GATE_DAEMON=0` in the hook's environment makes every call decide in its own process.
- **OpenCode.** Its plugin runs the same client, so both runners get the same decision.

Measured on one machine (`scripts/replay_real.py --timing 100`), 100 warm `PreToolUse` calls run as the runner runs a
hook: median 308 ms and 95th percentile 364 ms with a process for each call, 45 ms and 51 ms through the daemon.
The first call of a session, and any call made while the daemon is down, takes the slower path.

What each hook of a typical hand-built set does, and which rule or guard here does the same job, is on the
[hooks page](hooks.md).

## OpenCode

OpenCode is gated by two layers, each enough on its own for what it holds. `ws gate generate --runner opencode` writes
OpenCode's own `permission` rules (the floor, which hold if the plugin never loads) and a plugin, `plugins/ws-gate.js`,
that asks the gate before every call and runs the guards on OpenCode's other events.

**The rules.** Every kind of refusal in the policy is written as OpenCode's own rule: `deny_commands` (with the patterns
the guards that are on add) as `deny` under `bash`, `deny_read` under `read`, `grep`, `glob` and `list`, `deny_tools` under
`webfetch` and `websearch`, the protected paths under `edit`, and `ask_commands` as `ask` under `bash`. OpenCode takes the
last rule that matches, so each block runs allow-all first, then the asks, then the denies, and a deny beats an ask on the
same command. A pattern that is both asked and denied is written once, as a deny. A pattern only the hook can read (a pipe,
a `;`, a `$(`) is left to the hook, as it is for `deny_commands`.

`edit` also covers the skills folders OpenCode reads, because a skill is loaded as instructions and a planted one would
steer the next session: its own (`skills` and `skill` under its config folder) and the ones it shares with Claude Code
(`~/.claude/skills`) and other tools (`~/.agents/skills`). Each is written in both spellings OpenCode matches (the absolute
path, and `**/` and its last two folders). The plugin's own check does not hold these folders; the rule does.

**`policies`.** ws writes no `policies` key. In OpenCode 1.19.4 the only `policies` is `experimental.policies`, whose own
description is "policy statements applied to supported resources, such as provider access": it governs providers, not
tool calls, and has no rule for a command or a path. `POLICIES_SINCE` in `ws.adapters.opencode` is `None`; it takes the
first version whose schema has a permission key of that name.

**Approvals.** The plugin checks first, so a call that needs a person's yes is refused with the `ws approve` line before
OpenCode would ask. Once it is approved, the plugin lets the call through and OpenCode's own `ask` for it would come next:
the plugin answers that ask `once` (never for good), but only when the call is one the gate just allowed and every pattern
in the ask is one of ws's `ask_commands`. Your own `ask` rules are never answered for you. A headless `opencode run`
turns any ask it is not answered for into a refusal ("permission requested ... auto-rejecting"), so with only the rules
and no plugin an `ask_commands` call is refused, which is the floor.

**The guards.** The plugin runs `ws gate event` through the same thin client as the check, on these OpenCode events:

| OpenCode | ws event | Where the text goes |
|---|---|---|
| `tool.execute.before` | `pre` | the check; a deny throws and blocks the call (the only event that can block) |
| `tool.execute.after` | `post` | a deny or warning is added to the tool's own output, so the model reads it |
| `session.created` | `start` | the system prompt of each request the session makes (`experimental.chat.system.transform`) |
| `chat.message` | `prompt` | the same, for that turn |
| `session.idle`, a root session | `stop` | a toast; the guard reads `last_assistant_message`, fetched through the plugin client |
| `session.idle`, a child session | `subagent-stop` | nothing to show |

OpenCode sends one `session.idle` for both kinds of session; the plugin tells them apart (a child has a parent) and sends
`session.idle.child` for the second, a name of ws's own. A call is checked before it runs, and after it runs only for the
tools a guard that is on reads (`ws gate generate` writes both lists). `task` and `skill` are checked before a call only
when a guard that is on reads agents or skills. The call is parsed as the same kinds Claude Code's is (a `task` is an
`agent`, a `skill` a `skill`, an edit a `write` with the old and new text), so a guard gives the same verdict whichever
runner sent it. OpenCode has no send-message tool, so no `message` call is ever made there.

Every event but the check is advice: it has 10 seconds, any error is swallowed, and it never blocks. The check keeps 30
seconds, more than the client's worst case, as Claude Code's hook does. A guard that reads only `message` calls at `pre`
(`claims`) is named by `ws doctor` as not run by OpenCode, though its `stop` event does run. The list is worked out from
what each guard declares (`UNSUPPORTED_GUARDS`), so a new guard needs no edit in the adapter.

**Asks on `ws home`.** On `permission.asked` and `permission.replied` the plugin appends an `added` line and an `answered`
line to the asks file (`[home.sources] asks_jsonl` in the profile, else `~/.claude-usage/asks/asks.jsonl`), in the format
the [home page](home-api.md) reads, so OpenCode's open asks show there. A file it cannot write (the wall keeps most of the
disk read-only) is skipped.

## Replay a corpus

```sh
ws gate replay FILE... --runner claude-code [--policy FILE] [--metrics] [--save OUT.json]
    [--diff OLD.json [--expect-changes FILE]] [--json]
```

A corpus is a set of calls, each labelled with the decision you want. Replay judges every call as the live hook would,
then compares the decision with the label. It treats the gate as a classifier, so you can see how often it stops a
call it should let through (a false block) or lets through a call it should stop (a miss), and what a change to a rule
or a guard does to both.

A run with no `--policy` uses the built-in policy in scratch folders, so the answer is the same on any machine and
nothing is read from your own home folder. Give `--policy FILE` to measure your own.

### The corpus format

A corpus file is JSON lines, schema `ws.gate-corpus/1`: one row to a line. This row is wrapped here to read it:

```json
{
  "id": "oracle-word-01",
  "payload": {"kind": "command", "value": "cat ~/.ssh/id_rsa"},
  "expect": "deny",
  "source": "oracle",
  "why": "a private key"
}
```

| Key | Meaning |
|---|---|
| `id` | A name for the row that's unique across the files you replay. Letters, digits, `.`, `_` and `-`. |
| `payload` | The call, in runner-neutral terms: `kind` is `command`, `read`, `write` or `search`, and `value` is the command or the path. A write may carry `content`. ws writes it as each runner's own hook payload, so one file serves every runner. |
| `expect` | The decision you want: `allow`, `deny` or `ask`. |
| `source` | Where the row came from: `oracle`, `parity`, `regression`, `control` or `recorded`. The report has one line for each. |
| `why` | One line on why the label is right. |
| `known` | Optional. `true` marks a row the gate gets wrong today. See [The ratchet](#the-ratchet). |
| `runner` | Optional. With it, `payload` is that runner's own hook payload, and only that runner judges the row. |
| `guards` | Optional. Names of guards to switch on for this row alone. The row gets a state folder of its own, so its answer doesn't depend on the rows before it. |

Paths use `~`, `$HOME`, `{HOME}` and `{CWD}`. Replay makes two scratch folders, fills `{HOME}` and `{CWD}` with them,
points `HOME` and `WS_HOME` (one folder, `.ws` in it) at the first while it runs, and removes both after. `${HOME}` is
the shell's own and is left alone. A corpus therefore names no real path.

An unknown key, a repeated `id`, a bad `expect` or `source` and a payload that isn't a call are refused with the file
and line. A row that can't be judged (a payload the runner's adapter can't read) counts as a deny, as it does in the
live hook.

### The numbers

`--metrics` prints, in this order:

1. A 3 by 3 confusion matrix: rows are what you expected, columns are what the gate said.
2. The false-block rate: of the calls that should be allowed, how many were denied or held for approval. It shows the
   count, the total and a Wilson 95% interval.
3. The miss rate: of the calls that should be denied or held, how many were allowed.
4. Softened: of the calls that should be denied, how many came back as an ask.
5. One row for each `source`, with rows, differences, known rows, false blocks and misses.
6. The 50th and 95th percentile of the time `check()` took, in milliseconds. It's printed and never gated.

With `--json`, the same numbers are under `metrics`, with every row's decision, reason, the kind of layer that made the
decision (`rule:command`, `guard:NAME`, `ask_commands`, `preflight` or `error`) and its time under `results`.

### The ratchet

The exit code is 1 when a row that isn't `known` gets a decision other than its label. It's also 1 when a `known` row
now gets the decision you wanted: the output says `FIXED` and names it. Remove the `known` mark in the same change. A
fix then shows up as an improvement, and the list of known gaps only ever shrinks. Adding a `known` row needs a `why`
that says what the gate gets wrong today.

A `known` row in the shipped corpus is always a call the gate should let through and doesn't yet (a false block).

### Compare decisions

`--save OUT.json` writes each row's decision and layer kind. Later, `--diff OLD.json` lists every row whose decision or
kind has changed, and the rows added or removed. The run exits 1 unless each changed row is listed in the file named by
`--expect-changes`. Each line of that file is either an `ID` (any change to it) or `ID: OLD -> NEW`, where `OLD` and
`NEW` are the two decisions, or the two kinds when only the layer changed. A line that starts with `#` is a comment. A
listed row that didn't change also fails, because the change you expected didn't happen.

```text
$ ws gate replay tests/gate_corpus/*.jsonl --runner claude-code --save before.json
$ ws gate replay tests/gate_corpus/*.jsonl --runner claude-code --diff before.json --expect-changes expected.txt
```

Decisions saved for one runner can't be compared with another's.

### Check the two ways a hook is answered

`scripts/replay_real.py CALLS.json` takes a JSON list of shell commands or recorded hook payloads and decides each one
twice: through the generated hook command (the thin client asking a resident gate it starts in a scratch folder) and in
this process (`gate.run_hook`). `--baseline FILE` compares with the `--out` of an earlier run, made with `--src` pointing
at another build's `src` folder, and the last lines say `newly allowed N, newly refused M`. It exits 0 only when both are
0, the two ways agree on every call and every call is in the baseline. It prints the position of a call, never its text,
and `--out` keeps a hash and a decision for each call, so a list of real commands stays private. `--timing N` also times
N calls with a daemon and with `WS_GATE_DAEMON=0`.

### The corpus in the repository

`tests/gate_corpus/` holds the corpus ws is measured against. CI replays it on both runners with `--metrics`. The files
are:

| File | Source | What it holds |
|---|---|---|
| `oracle.jsonl`, `oracle-generated.jsonl` | `oracle` | Spellings of a protected path, and the harmless words the shell reads differently. A shell worked out each label. `tests/test_shellwords_oracle.py` checks the same spellings against bash. |
| `parity.jsonl` | `parity` | Rules shared with other gate implementations: protected paths next to ordinary files, public keys and the ssh config, and calls that need a person's yes. |
| `regression.jsonl` | `regression` | Spellings the matcher once missed or once refused wrongly, tool calls for reads, searches and writes, and the ordinary calls it still refuses (marked `known`). |
| `control.jsonl` | `control` | Ordinary commands, reads, searches and writes. They exist to make false blocks measurable. |

To add a case, add a row and run the replay. If the gate gets it wrong, mark it `known` with a `why`; don't change the
label to match what the gate does today.
