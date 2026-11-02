# Send drafts only

Keep an agent from sending messages on your behalf.

**Draft-only sending isn't available as an enforced setting in v0.1.** ws ships no mail connector, and no setting makes
ws block a send. Read [Draft-only sending](../explanation/draft-only-sending.md) first. This page lists the settings that
exist and lower the risk.

## Before you start

- A policy: `ws gate init` ([Use the gate](use-the-gate.md)).
- The commands, tools and connectors that can send from your machine. List them. You can only deny what you know.

## Steps

1. Keep the dial at `prepare`. It's the default, and the only value `ws install`, `ws setup` and `ws gate init` write.

   ```toml
   dial = "prepare"
   ```

2. Mark your mail source as read-only and untrusted. Mail is content that anyone can write to.

   ```toml
   [sources.mail]
   mode = "read"
   untrusted = true
   acts = ["draft"]
   outward = ["send"]
   ```

   With `mode = "read"` and the dial at `prepare`, ws counts no outward act of this source as a way out.

3. Deny the commands that send. Add the ones that exist on your machine to `deny_commands`.

   ```toml
   [gate]
   deny_commands = [
     "sendmail*", "mail *", "mailx *", "mutt *", "msmtp *",
     "curl*", "wget*", "nc *", "ssh *", "scp *",
     # the default rules stay below
   ]
   deny_tools = ["web_fetch", "web_search"]
   ```

   Denying the network commands also closes the shell as a way out for the [Rule of Two](apply-the-rule-of-two.md).

4. Ask before anything that publishes. Commands in `ask_commands` run only after you approve that exact call.

   ```toml
   [gate]
   ask_commands = ["git push*", "gh pr create*", "gh issue comment*"]
   ```

   See [Approve a command](approve-a-command.md).

5. Generate the runner files again and start a new session.

   ```sh
   ws gate generate --runner claude-code --out ~/.ws/gate/claude-code
   ```

6. If you connect a mail tool yourself (an MCP server, a script), give it a draft-only scope at the mail provider. Don't
   give it a send scope. This is the only place a send can be stopped for certain today.

## Success

A denied command is blocked at the gate. Test one:

```sh
echo '{"tool_name":"Bash","tool_input":{"command":"sendmail me@example.com"},"session_id":"s1","cwd":"/tmp"}' \
  | ws gate check --runner claude-code
```

It prints `ws gate: blocked: command matches deny_commands rule 'sendmail*'` and exits 2.

## What this doesn't do

- It doesn't stop a connector that has a send scope. ws can't see inside it.
- It doesn't stop an interpreter running a script that sends. The wall limits what the script can write, not what it can
  send over the network.
- It doesn't make drafts. No ws plugin drafts mail yet.

This gap is in [Known limits](../explanation/known-limits.md).
