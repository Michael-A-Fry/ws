# Use the wall

Start an AI session inside a sandbox, so that it can write only inside its project and can't read your denied paths.
To understand the rule first, read [The wall](../explanation/wall.md).

## Before you start

- Linux or WSL2 with [bubblewrap](https://github.com/containers/bubblewrap) (`sudo apt install bubblewrap`, or
  `sudo dnf install bubblewrap`). On macOS the wall uses `sandbox-exec`, which is untested in CI; see
  [Known limits](../explanation/known-limits.md).
- A policy and generated runner files: [Use the gate](use-the-gate.md).
- For Claude Code, the settings generated into `~/.local/state/ws/gate/claude-code` (`ws setup` or `ws gate generate`
  writes them).

## Steps

1. Go to the project folder. The wall makes this folder writable, and refuses to start from your home folder or above.

   ```sh
   cd ~/code/my-project
   ```

2. Start the runner through the launcher. Everything after `--` is the runner's own command line.

   ```sh
   ws gate launch --runner opencode -- opencode
   ```

   For Claude Code, which has its own sandbox:

   ```sh
   ws gate launch --runner claude-code -- claude
   ```

   To let a program write one more folder, add it to `allow_write` in `policy.toml`, or use `--allow-write DIR` for one
   run. To make one more path read-only, use `--protect PATH`.

3. Try the wall. Run a harmless command that writes a protected file. This starts a shell in the wall and tries to change
   your policy.

   ```sh
   ws gate launch --runner opencode -- sh -c 'touch ~/.config/ws/policy.toml'
   ```

4. When the runner exits, read the end-of-run report. ws prints it to standard error, and appends a line to
   `~/.local/state/ws/logs/wall-report.jsonl`, only when the session left something that would run later: a new git
   hook, runner config, or a program that loads on its own. It deletes nothing. No report means nothing to report.

   ```sh
   cat ~/.local/state/ws/logs/wall-report.jsonl
   ```

## Success

Step 3 prints `touch: cannot touch '...': Read-only file system` and exits with a non-zero code. The wall is working.

## If it refuses

`ws gate launch` fails closed. With no bubblewrap, or with user namespaces off, it prints the install line. See
[Troubleshoot](troubleshoot.md). `--no-sandbox` starts the runner bare and prints what that gives up. Don't use it for
real work.

## What else the wall settings do

- Bypass mode is off in the settings ws passes to Claude Code and in the ones `ws install` merges into your own file:
  `permissions.disableBypassPermissionsMode` is `"disable"`, so `--dangerously-skip-permissions` is refused while a
  gated session runs. If your own file sets that key to anything else, `ws install` refuses and changes nothing; change
  your value to match.
- To give a class its own operating-system account, see [Set up a class user](set-up-a-class-user.md).

## Shut the network by class

With a `[classes]` table in the policy, a session starts with the network shut. It can reach the hosts in its class's
`allowed_hosts`, the host of the model provider it was started with, and the hosts of any `[[egress_exceptions]]` that
name the class and haven't ended. Nothing else. A policy with no classes is as it was: Claude Code asks about a host it
doesn't know, and OpenCode's network is open.

```toml
[classes.work]
access = "walled"
repos = ["~/code/work/**"]
allowed_providers = ["anthropic"]
allowed_hosts = ["pypi.org", "files.pythonhosted.org", "localhost:11434"]

[[egress_exceptions]]
name = "vendor-docs"
classes = ["work"]
hosts = ["docs.example.net"]
reason = "reads the vendor docs for the migration"
owner = "Sam"
expires = 2026-12-31
```

- **Hosts.** A name (`pypi.org`), a name under a domain (`*.example.org`, which doesn't cover `example.org` itself) or an
  address, each with an optional `:port`. A catch-all (`0.0.0.0`), a wildcard over a whole top-level name (`*.com`) or a
  port outside 1 to 65535 is refused when the policy loads. A name needs a dot, or is `localhost`. A model on this
  machine is `localhost:PORT` or `127.0.0.1:PORT`, and the port is required.
- **Exceptions.** `owner` and `expires` are both required, so a grant always ends and has someone behind it. From the
  moment `expires` is reached the exception covers nothing, and a request it would have covered is refused with its name.
  A date such as `2026-12-31` ends at 00:00Z that day.
- **Providers.** `ws gate launch --provider NAME` says which model provider the session uses (Claude Code means
  `anthropic` unless you say otherwise). A provider outside the class's `allowed_providers` is refused before anything
  starts, and the refusal names the class only. A walled or counts-only class that doesn't say gets a model on this
  machine (`local`, `ollama`, `lmstudio`, `vllm`, `llamacpp`) and nothing else; a `full` class gets any. A folder in no
  class is held to the strictest. `local_only = true` keeps loopback entries and drops the rest.

What enforces it:

- **Claude Code.** The class's list is written as `sandbox.network.allowedDomains` in the settings copy `ws gate launch`
  makes for the project, and an empty list reaches no host. The managed-settings file for a class user
  (`ws install --class NAME`) holds the class's hosts with `allowManagedDomainsOnly`, so a project's own settings file
  can't add one. That file is placed by hand, so it carries the class's hosts and its providers' but not the exceptions,
  which end; run `ws install --class NAME` again after you change the list.
- **OpenCode, on Linux.** `ws gate launch` starts a small proxy of its own on 127.0.0.1 and sets the proxy variables for
  the runner; loopback stays off the proxy, so a model on this machine is reached directly. The proxy refuses a host
  that isn't listed, a name that resolves to a private, link-local or cloud-metadata address, and a name that resolves to
  this machine unless it is a listed model port. It is the route, not a fence: a program that ignores the proxy variables
  and opens a socket itself isn't stopped by the wall alone. ssh, nc, ncat, netcat, telnet, scp, sftp, ftp and socat are
  replaced on `PATH` inside the wall by a script that says so and exits 126, and the end-of-run report counts how many
  requests and tool calls were refused. A tool called by its full path isn't caught. What closes that gap is a rule in
  the host's firewall for the class's OS user; ws doesn't write one (it needs root).
- **OpenCode, on macOS.** The same proxy, and the sandbox profile denies all outbound connections except to the proxy
  and the model ports, so a program that skips the proxy is stopped. That profile hasn't been run on a Mac.

`ws doctor` shows one WS015 line per class: `ok` with the number of hosts, or `warn` with the reason (see
[Doctor checks](../reference/doctor-checks.md)).

## Check the host once

```sh
ws doctor
```

If WS007 warns, set `kernel.yama.ptrace_scope = 1`. The line is in the check's `fix` text.
