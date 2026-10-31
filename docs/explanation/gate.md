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
   `ws gate check` on every governed call. The matcher in `src/ws/policy.py` reads the command text one shell segment at
   a time (`a && b`, pipes, `$(...)`, `bash -c "..."`). It normalises flags first, so one `rm -*` rule also covers
   `rm -fr` and `rm -r -f`. Exit code 0 allows the call, and 2 denies it with a reason on standard error.
3. **The wall.** An operating-system sandbox acts on what a process does, not on how the command line is spelled. See
   [The wall](wall.md).

The gate fails closed. A payload it can't read, a policy that doesn't load, a rule it can't parse and any crash are
all a deny. A policy file is used whole or refused: an unknown key stops the load and names the key.

The gate also protects ws's own files. A session can't write the policy, the profile, the secrets registry, the job
files, the logs, the approvals folder or the folder `ws gate generate` wrote, so it can't loosen its own rules.

What the policy holds:

- `deny_commands`: shell command patterns. `*` matches anything. The default list covers recursive deletes of system
  folders, forcing a push, `mkfs`, `dd` to a device, and a shell that reads its script from standard input.
- `deny_read`: paths no session may read, such as `~/.ssh/**`.
- `deny_tools`: neutral tool names to switch off (`web_fetch`, `web_search`).
- `ask_commands`: commands that run only after you approve that exact call. See [Approvals](approvals.md).
- `allow_hosts` and `allow_write`: network hosts and extra writable folders for the wall.

The full list of keys is in the [policy reference](../reference/policy.md).

## What the gate doesn't stop

The matcher reads text, so it can't see inside a program. These calls pass the matcher:

- What an interpreter does inside its own program (`python3 -c`, `perl -e`, `node -e`, a script it runs).
- A script that's downloaded and run in two steps.
- A path that a command builds (`cat "$(echo ~)/.ssh/id_rsa"`) or a variable set outside the command line.
- The version-control tool restoring a protected tracked file.
- OpenCode's `patch` tool, which carries no single file path. Its native `edit` rules still apply.

The wall closes most of these for a shell command. It doesn't close all of them. A `deny_commands` rule isn't enforced
inside an interpreter: `python3 -c 'os.system("...")'` runs the command, and only the wall's file rules apply to it.
`allow_hosts` limits the network on Claude Code only. See [Known limits](known-limits.md).

## Do it

Follow [Use the gate](../how-to/use-the-gate.md).
