# Apply the Rule of Two

Describe your sources, close the shell as a way out, and let a job that holds untrusted input and private data run
only if
nothing can carry the data out. To understand the rule first, read [The Rule of Two](../explanation/rule-of-two.md).

## Before you start

- A policy: `ws gate init` ([Use the gate](use-the-gate.md)).
- A job file in `$WS_HOME/jobs/` that lists `sources`.

## Steps

1. Mark each source in `policy.toml`. Set `untrusted = true` for content that anyone can write to, and
   `sensitive = true` for private data. Keep `mode = "read"` unless the job must act.

   ```toml
   [sources.mail]
   mode = "read"
   untrusted = true
   sensitive = false

   [sources.notes]
   mode = "read"
   untrusted = false
   sensitive = true
   ```

2. List the sources a job reads, in the job file.

   ```toml
   runner = "claude-code"
   model = "claude-haiku-4-5"
   prompt = "Triage my inbox."
   sources = ["mail", "notes"]
   ```

3. Ask ws what it would do. Nothing starts.

   ```sh
   ws run triage --dry-run
   ```

   With the default policy, this job is refused. `would_refuse` says why:

   ```text
   Rule of Two: untrusted input (mail), private data (notes) and a way out (web_fetch, web_search,
   shell:curl/wget/nc/ncat/ssh/scp/sftp/ftp/telnet/socat/python3/python/node/perl/ruby) together
   ```

4. Close the way out. Deny the web tools, and deny the network commands in the `shell:` list. Deny all of them: one open
   command keeps the leg open.

   ```toml
   [gate]
   deny_tools = ["web_fetch", "web_search"]
   deny_commands = [
     "curl*", "wget*", "nc *", "ncat *", "ssh *", "scp *", "sftp *", "ftp *", "telnet *", "socat *",
     "python* -c*", "node* -e*", "perl* -e*", "ruby* -e*",
     # the default rules stay below
   ]
   ```

5. Check again, then regenerate the runner files so the runner enforces the same rules.

   ```sh
   ws run triage --dry-run
   ws gate generate --runner claude-code --out ~/.ws/gate/claude-code
   ```

## Success

Step 3 shows `"rule_of_two": "refused"` and `"would_refuse": "Rule of Two: ..."`. After step 4, the same command shows
`"rule_of_two": "ok"` and `"would_refuse": null`.

## If the job really needs all three

Add a named exception. It applies only when the job name matches and every source the session reads is listed.

```toml
[[exceptions]]
name = "morning-digest"
job = "digest"
sources = ["mail", "notes"]
reason = "reads mail, writes only to me"
```

The run record then shows `exception:morning-digest`. Write a real reason. A future you will read it.

## Limits

The check applies to `ws run` and `ws eval`, not to a session you start by hand. It trusts your `untrusted` and
`sensitive` flags. See [The Rule of Two](../explanation/rule-of-two.md) for what it doesn't stop.
