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

- **Hosts.** A name (`pypi.org`), a name under a domain (`*.example.org`, which doesn't cover `example.org` itself) or a
  public address, each with an optional `:port`. This is the format Claude Code's `sandbox.network.allowedDomains`
  takes (checked against its settings reference in November 2026), so entries go into it as written. Refused when the
  policy loads: a catch-all (`0.0.0.0`), a wildcard over a whole top-level name (`*.com`) or over an IP, and a port
  outside 1 to 65535. Left out when a session starts (and WS015 counts them): a name with no dot, a private,
  link-local or cloud-metadata address, a wildcard over a registry like `*.co.uk`, and a loopback entry with no port. A
  model on this machine is `localhost:PORT` or `127.0.0.1:PORT`; the port is required. A host written with anything
  but letters, digits, dots and hyphens is refused at the proxy.
- **Exceptions.** `owner` and `expires` are both required, so a grant always ends and has someone behind it. From the
  moment `expires` is reached the exception covers nothing, and a request it would have covered is refused with its
  name. A date such as `2026-12-31` ends at 00:00Z that day. ws's own proxy judges this on every request, so an
  exception that ends mid-session stops at that moment. Claude Code's list is written once when the session starts, so
  there an exception that ends mid-session still holds until the next start.
- **Providers.** `ws gate launch --provider NAME` says which model provider the session uses (Claude Code means
  `anthropic` unless you say otherwise). `ws run` takes it from the runner, the job's `provider` option or the part of
  the model name before the slash. A provider outside the class's `allowed_providers` is refused before anything
  starts, and the refusal names the class only. A walled or counts-only class that doesn't say gets a model on this
  machine (`local`, `ollama`, `lmstudio`, `vllm`, `llamacpp`) and nothing else; a `full` class gets any. A folder in no
  class (and a job with no `cwd`) is held to the strictest. `local_only = true` keeps loopback entries and drops the
  rest.
- **No sandbox, no list.** A session of a class is refused (exit 3) with `--no-sandbox`, or with `[gate] sandbox =
  false` on Claude Code, since the list has nowhere to be applied. WS015 warns of the second.

What enforces it:

- **Claude Code.** The class's list is written as `sandbox.network.allowedDomains` in the settings copy `ws gate launch`
  makes for the project (and the run's settings in `ws run`), with `strictAllowlist` on so a host outside it is refused
  and not asked about (Claude Code 2.1.219 or later; an older one asks). An empty list reaches no host. This copy
  can't stop a project's own settings file from adding hosts, so the preflight hook refuses every call from a folder
  whose `.claude/settings.json` or `settings.local.json` sets `network.allowedDomains`, a proxy port, a Unix socket
  or local binding. The managed-settings file for a class user (`ws install --class NAME`) is the stronger form: it
  holds the class's hosts with `allowManagedDomainsOnly`, so no other file can add one. It is placed by hand, so it
  carries the class's hosts and its providers' but not the exceptions, which end; run `ws install --class NAME` again
  after you change the list.
- **Other runners (OpenCode), on Linux.** `ws gate launch` starts a small proxy of its own on 127.0.0.1 and sets the
  proxy variables for the runner, with a password made for that launch, so another session's proxy can't be used from
  inside. The proxy refuses a host that isn't listed, a name that resolves to a private, link-local or cloud-metadata
  address, and a name that resolves to this machine unless it is a listed model port.
  **It is the route, not a fence.** Loopback is kept off the proxy (`NO_PROXY`), so a model on this machine is reached
  directly, and that means any loopback port is open to the session, including ones another process on the machine
  listens on. A program that ignores the proxy variables and opens a socket itself isn't stopped either. ssh, nc, ncat,
  netcat, telnet, scp, sftp, ftp and socat are replaced on `PATH` inside the wall by a script that says so and exits
  126, and the end-of-run report counts how many requests and tool calls were refused; a tool called by its full path
  isn't caught. What closes the gap is a rule in the host's firewall for the class's OS user; ws doesn't write one (it
  needs root).
- **Other runners, on macOS.** The same proxy, and the sandbox profile denies all outbound connections except to the
  proxy and the model ports, so a program that skips the proxy is stopped (a loopback model port stays open to it).
  That profile hasn't been run on a Mac.
- **`ws run`.** The same list: in the run's settings for Claude Code, through the proxy for other runners. A run that
  breaks G7 or has the sandbox off is refused and recorded with `refused:egress`.

`ws doctor` shows one WS015 line per class: `ok` with the number of hosts, or `warn` with the reason (see
[Doctor checks](../reference/doctor-checks.md)).

## Check the host once

```sh
ws doctor
```

If WS007 warns, set `kernel.yama.ptrace_scope = 1`. The line is in the check's `fix` text.
