# Use the gate

Write a policy, turn it into your AI tool's rules, and prove that a denied command is blocked. To understand the rule
first, read [The gate](../explanation/gate.md).

## Before you start

- ws installed ([Install ws](install.md)).
- Claude Code or OpenCode installed.

## Steps

1. Write the default policy. ws writes it only if you have none.

   ```sh
   ws gate init
   ```

   It prints `ws: wrote .../policy.toml`. The file is `$WS_HOME/policy.toml` (`~/.ws/policy.toml` unless you set
   `WS_HOME`). It's commented. Run the command again and it prints `ws: 0 changes to ...`.

2. Add a rule. Open `policy.toml` and add a line inside the existing `deny_commands` list. This one blocks `curl`.

   ```toml
   [gate]
   deny_commands = [
     "curl*",
     # the default rules stay below it
   ]
   ```

   Keep the default rules. `*` matches anything. Flags are normalised, so `rm -*` covers `rm -fr` and `rm -r -f`.

3. Generate the runner's own config into a folder of your own. Never generate into the runner's config folder
   (`~/.claude` or `~/.config/opencode`): ws refuses.

   ```sh
   ws gate generate --runner claude-code --out ~/.ws/gate/claude-code
   ```

   Use this folder for Claude Code: `ws gate launch` in step 5 reads its settings from `$WS_HOME/gate/claude-code`
   (`ws setup` writes the same place). For OpenCode, use `--runner opencode` and another folder. The command prints
   how to start the runner with the output.
   It's idempotent: a second run prints `0 changes`.

4. Check the rule before you use it. `ws gate check` reads the runner's hook payload on standard input. Exit code 2 is a
   deny and the reason is on standard error.

   ```sh
   echo '{"tool_name":"Bash","tool_input":{"command":"curl http://example.com"},"session_id":"s1","cwd":"/tmp"}' \
     | ws gate check --runner claude-code
   echo $?
   ```

5. Start the runner with the rules, inside the wall. This starts Claude Code with the settings you generated plus this
   project's locked paths.

   ```sh
   ws gate launch --runner claude-code -- claude
   ```

   For OpenCode: `ws gate launch --runner opencode -- opencode`, with the environment that `ws gate generate` printed.

## Success

Step 4 prints `ws gate: blocked: command matches deny_commands rule 'curl*'` and the exit code is 2. The same payload with
`ls` in place of `curl` prints nothing and exits 0.

## Change a rule later

Edit `policy.toml`, then run `ws gate generate` again for each runner. A policy with an unknown key or a bad value is
refused whole and the message names the key. The runner keeps the rules it loaded at start, so start a new session.

## Turn on a guard

Guards are small named checks. `ws gate init` turns on `syntax-check` and `session-context`. To add one, name it in the
`[guards]` table of `policy.toml`, regenerate and start a new session.

```toml
[guards]
on = ["syntax-check", "session-context", "bulk-staging"]
```

With `bulk-staging` on, `git add -A` and `git commit -am` are stopped and `git add file.py` goes through. With
`syntax-check` on, a Python file written with a syntax error comes back to the model with the line number. The
[guards reference](../reference/guards.md) lists every guard and its keys.

## Replay recorded calls

Name `record-calls` in `on` and each tool call a session makes is appended to `$WS_HOME/logs/gate-calls.jsonl`. After you
change a rule, run the recorded calls against it.

```sh
ws gate replay ~/.ws/logs/gate-calls.jsonl --runner claude-code
```

Each line prints `allow`, `deny` or `ask`. To pin an answer, make a file of `{"payload": {...}, "expect": "deny"}` lines.
The command exits 1 when a result differs from its `expect`.

## Other keys

| To do this | Set |
|---|---|
| Block reading a path | `deny_read = ["~/.ssh/**"]` |
| Switch off the web tools | `deny_tools = ["web_fetch", "web_search"]` |
| Ask before a command runs | `ask_commands`: see [Approve a command](approve-a-command.md) |
| Limit network hosts (Claude Code) | `allow_hosts = ["api.anthropic.com"]` |
| Let a walled session write a folder | `allow_write = ["~/.cache/uv"]`: see [Use the wall](use-the-wall.md) |

The [policy reference](../reference/policy.md) has every key.
