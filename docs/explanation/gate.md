# The gate

The gate is the rule that no AI session runs a command, reads a path or uses a tool that your policy denies. It's the
first of ws's safety rules and the one you touch most.

## The rule

You write one file, `policy.toml`. It lists the shell commands, file reads and tools that no session may use, whichever
AI tool it runs in. ws turns that file into the native config of each runner (Claude Code and OpenCode), and answers each
runner's hook before a call runs.

## Why it exists

A model that reads untrusted text (a web page, an e-mail, a file in a cloned repository) can be talked into running a
command you never meant. A deny list kept inside each tool drifts: you edit one tool's list and forget the other. One
neutral file, generated into each tool, keeps the rule in one place.

## How ws enforces it

Three layers, each there because the one before it can miss a case.

1. **The runner's own deny rules.** `ws gate generate` writes them from the policy (`src/ws/gate.py`,
   `src/ws/adapters/claude_code.py`, `src/ws/adapters/opencode.py`). The runner blocks the call itself, with no help
   from ws.
2. **The hook or shim.** Claude Code gets a `PreToolUse` hook and OpenCode gets a plugin shim. Both run
   `ws gate check` on every governed call, through a thin client that asks one resident process so a call costs
   milliseconds, and that runs the check itself if the process can't answer (see
   [the resident gate](../reference/gate.md#the-resident-gate)). The matcher in `src/ws/policy.py` reads the command
   text one shell segment at
   a time (`a && b`, pipes, `$(...)`, `bash -c "..."`). It normalises flags first, so one `rm -*` rule also covers
   `rm -fr` and `rm -r -f`. Exit code 0 allows the call, and 2 denies it with a reason on standard error.
3. **The wall.** An operating-system sandbox acts on what a process does, not on how the command line is spelled. See
   [The wall](wall.md).

The gate fails closed. A payload it can't read, a policy that doesn't load, a rule it can't parse and any crash are
all a deny. A policy file is used whole or refused: an unknown key stops the load and names the key.

The gate also protects ws's own files. A session can't write the policy, the profile, the secrets registry, the job
files, the logs, the approvals folder or the folder `ws gate generate` wrote, so it can't loosen its own rules.

## Guards

A guard is a small named check that the policy switches on (`[guards] on = [...]`). The layers above answer "may this
call run?" from the policy's lists. A guard looks at one thing in more detail: it can read what a file write contains,
what a `git` command stages or which state files a project holds. Guards run at the points where a runner calls ws:
before a tool call (through `ws gate check`, so a guard's deny stops the call), after a tool call, when a session
starts, when a prompt is sent and when a session or subagent stops (through `ws gate event`).

A guard's deny before a call is as firm as a `deny_commands` match, and it can be marked approvable: you override it once
with `ws approve`, as for `ask_commands`. After a call the guard can only tell the model what it found, because the tool
has already run. `ws gate generate` writes the runner hooks for the events and tools the guards that are on need, and no
others. `ws gate replay` runs recorded calls against the current policy, so a change to a rule or a guard is tried
before it's relied on. See the [guards reference](../reference/guards.md).

What the policy holds:

- `deny_commands`: shell command patterns. `*` matches anything. The default list covers recursive deletes of system
  folders, forcing a push, `mkfs`, `dd` to a device, and a shell that reads its script from standard input.
- `deny_read`: paths no session may read, such as `~/.ssh/**`.
- `deny_tools`: tools to switch off: the neutral names `web_fetch` and `web_search`, and MCP tools by name or glob.
- `allow_mcp`: the MCP tools a session may call without being asked.
- `ask_commands`: commands that run only after you approve that exact call. See [Approvals](approvals.md).
- `allow_hosts` and `allow_write`: network hosts and extra writable folders for the wall.

The full list of keys is in the [policy reference](../reference/policy.md).

## MCP tools

An MCP tool (a call named `mcp__server__tool`) can do anything its server does, so the gate judges it like any other
call. A `deny_tools` entry or glob that matches its name denies it. A tool that `allow_mcp` doesn't name is asked for,
the way `ask_commands` asks: the call is held, `ws approve` shows the tool and its exact arguments, and the identical
retry runs once. Out of the box only the tools `ws mcp serve` offers are allowed. The arguments are read too: a value
that looks like a path goes through `deny_read`, and through the protected-write check when the tool's name says it
writes, so an allowed file tool still can't read a sealed folder. Arguments too deeply nested or too many to read are a
deny. On Claude Code the hook runs for every MCP tool. OpenCode's hook payload can't tell an MCP tool from a built-in
one, so there only a `deny_tools` entry with a literal server name is enforced (see
[known limits](known-limits.md#opencode-cannot-tell-mcp-tools-from-its-own)). The reasoning is in
[decision 0021](../decisions/0021-the-gate-judges-mcp-tool-calls.md).

## What the gate doesn't stop

The matcher reads text, so it can't see inside a program. These calls pass the matcher:

- What an interpreter does inside its own program (`python3 -c`, `perl -e`, `node -e`, a script it runs).
- A script that's downloaded and run in two steps.
- A path that a command builds (`cat "$(echo ~)/.ssh/id_rsa"`) or a variable set outside the command line.
- The version-control tool restoring a protected tracked file.
- OpenCode's `patch` tool, which carries no single file path. Its native `edit` rules still apply.

The sandbox is the boundary and the gate is defence in depth: the gate explains a refusal in words a person can act
on, and the sandbox is what holds when the text can't be read. A refusal stays a refusal whether or not the sandbox is up.

The wall closes most of these for a shell command. It doesn't close all of them. A `deny_commands` rule isn't enforced
inside an interpreter: `python3 -c 'os.system("...")'` runs the command, and only the wall's file rules apply to it.
`allow_hosts` limits the network on Claude Code only. See [Known limits](known-limits.md).

## Do it

Follow [Use the gate](../how-to/use-the-gate.md).
