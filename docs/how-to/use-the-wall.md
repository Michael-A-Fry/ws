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

## Deny the home folder

A Claude Code session started with `ws gate launch --runner claude-code -- claude` can't read your home folder from a
shell command, except the project and what common tools need. This is Claude Code's own sandbox, set by the copy of the
settings that `ws gate launch` writes for the project at each start (`sandbox.filesystem.denyRead` and `allowRead`; see
[decision 0026](../decisions/0026-deny-the-home-in-the-launch-settings.md)). It needs Claude Code 2.1.78 or newer, and
`ws doctor` (WS016) warns when yours is older.

What a shell command can read in your home folder by default:

- the project folder you started in, and its git folders when they sit outside it (a linked worktree, a separate git
  folder)
- three parts of Claude Code's own folder (`~/.claude`, or `CLAUDE_CONFIG_DIR`): `shell-snapshots/`, which every command
  sources, and `skills/` and `plugins/`. Its settings, history, transcripts and the rest stay hidden
- the tool folders `~/.cache/uv`, `~/.npm`, `~/.cargo`, `~/.rustup`, `~/go`, `~/.cache/go-build`, `~/.local/bin`,
  `~/.local/lib` and `~/.local/share/uv`, and git's
  config (`~/.gitconfig`, `~/.config/git`). The XDG variables of the shell you start from decide where the cache, data and
  config ones are. A folder that isn't there is left out.

Everything else is hidden, `~/.local/share` included, because it holds other tools' sign-in files. The credential files
inside the folders above stay hidden too: `~/.cargo/credentials`, `credentials.toml` and git's `credentials` file
(Claude Code's own `.credentials.json` is named as hidden besides). So do your `deny_read` paths. Write access is as before.

To let sessions read another folder, such as a Node version manager or a shared data folder, name it under `[gate]` in
the policy and start the session again (the settings are made at each start):

```toml
[gate]
allow_read = ["~/.nvm", "~/data/shared"]
```

An entry is a folder or file, absolute or under `~/`, with no wildcard. A bad entry stops the policy loading, and the
error names it. An entry can't be your home folder, a folder above it, `/` or a `deny_read` path, or inside one. A
folder above a `deny_read`
path is fine: that path stays hidden.

A session started another way (an OpenCode session, or `claude` without `ws gate launch`) hides only the `deny_read`
paths. With `[gate] sandbox = false` there is no sandbox block, so nothing is hidden.

### Switch it on and off

Run these in a terminal of your own; a session can't (they're admin commands).

- **On.** `ws install --runner claude-code` (add `--yes` to skip the question). It first copies your
  `~/.claude/settings.json` to the `backups/` folder in ws's state folder, then writes the generated settings and merges
  ws's block into your file. Sessions started with `ws gate launch --runner claude-code -- claude` then have the home
  hidden. On a machine that already has ws, install the newer ws, then run `ws upgrade --hooks-only`, which rewrites the
  generated files with it and downloads nothing.
- **Off.** `ws uninstall --yes` puts your `~/.claude/settings.json` back byte for byte when you haven't changed it since
  (if you have, it takes out only ws's entries) and removes the generated settings. `ws gate launch --runner claude-code`
  then refuses until you run `ws install` again. `ws install --gate launch` is the lighter undo: it takes ws's entries out
  of your own file, so plain `claude` sessions are no longer gated, and keeps the generated settings, so launched
  sessions still have the home hidden.

There is no switch for the home alone. To start one session without any wall use `--no-sandbox`, which says what that
gives up; to leave the sandbox out for good, set `sandbox = false` under `[gate]` and run `ws install` again.

`ws canary --runner claude-code` has a `wall` check: it makes the launch settings as `ws gate launch` makes them and fails
if the home is no longer hidden, the project isn't re-allowed, a `deny_read` path is missing, or the sandbox gained an
unsandboxed route (an excluded command, a Unix socket).

### Test notes

The switch on and off, and the upgrade command, were rehearsed in a scratch `HOME` and `WS_HOME` with the sandbox on
(`tests/test_sandbox_deny_home.py`; nothing touched the real home). The steps, in order:

1. a settings file of the test's own at `~/.claude/settings.json`
2. `ws install --yes --runner claude-code`: exit 0, the block is in the file, the first copy is under `backups/`, and the
   launch settings made from the generated file hide the home
3. `ws upgrade --hooks-only`: exit 0, nothing to rewrite, the block still in the file
4. `ws uninstall --yes`: your file is back as it was, and the generated settings are gone

`ws upgrade` refuses to run inside an agent session, so the test runs it as a person at a terminal would.

## When Claude Code asks to read outside the working folders

With the read block on, Claude Code stops before its Read, Grep or Glob tool reads a path outside the folders the
session works in, and asks. One of the answers keeps allowing such reads from then on. That one answer switches this
part of the wall off for every later session, so pick the narrower one:

- **The safe choice.** Answer no, then add the one folder the task needs: `--add-dir PATH` when you start the session,
  or `/add-dir PATH` inside it. The session can read that folder, and nothing else outside its own.
- **To undo an allow you saved.** In the session, run `/permissions`, find the rule the answer saved and remove it.
  `/permissions` also lists the rules in force, and `ws doctor` (WS016) says whether your Claude Code is new enough to
  read the block.

On native Windows the sandbox isn't enforced, so this block is the only wall on reads by Claude's file tools there, and
a shell command can still read anywhere.

## What else the wall settings do

- Bypass mode is off in the settings ws passes to Claude Code and in the ones `ws install` merges into your own file:
  `permissions.disableBypassPermissionsMode` is `"disable"`, so `--dangerously-skip-permissions` is refused while a
  gated session runs. If your own file sets that key to anything else, `ws install` refuses and changes nothing; change
  your value to match.
- File reads stay in the working folders. `permissions.blockReadsOutsideWorkingDirectories` is `true` in the settings ws
  passes to Claude Code (`ws gate launch`, `ws gate generate` and headless `ws run` jobs) and in a class's
  launch file, so Claude's Read, Grep and Glob tools refuse a path outside the folders the session works in,
  in every permission mode. It needs Claude Code 2.1.257 or newer; `ws doctor` (WS016) warns when yours is older. ws
  never writes it into your own settings file, because it would bind every Claude Code session you start. To let a
  gated session read outside its folders, set `read_outside_project = true` under `[gate]` in the policy and
  regenerate. A project's own settings can't lift the block: Claude Code applies a `true` from any settings file.
- Hosts a sandboxed command may reach without asking. When `allow_hosts` names none, the sandbox block carries a short
  default list: `api.anthropic.com`, PyPI (`pypi.org`, `files.pythonhosted.org`), the npm registry
  (`registry.npmjs.org`) and crates.io (`crates.io`, `index.crates.io`, `static.crates.io`). A session started with
  `ws gate launch` also gets GitHub (`github.com`, `api.github.com`, `codeload.github.com`, `raw.githubusercontent.com`,
  `objects.githubusercontent.com`) and `gitlab.com`; a headless `ws run` job doesn't, because a prompt-injected job that
  can reach a forge can push the project there (name the forge in `allow_hosts` if a job needs it). It only spares the
  prompts: a host on neither list still asks in an interactive session and is refused in
  `claude -p`. `allow_hosts` replaces the list, so name every host you want. A class session never carries it: it gets
  its class's list, and a class with none reaches nothing. With `sandbox = false` there is no block and no list. The list
  is for sessions ws starts; `ws install` doesn't add it to your own settings file (Claude Code adds up the lists of
  every settings scope, so a default host there would also reach a class session). Hosts you name in `allow_hosts` are
  merged into your file, yours first, and the undo takes ws's entries out again.
- `ws status` shows the sandbox and permission mode in force, read from the settings files; `ws doctor` (WS016) warns when
  `permissions.defaultMode` is `bypassPermissions`. ws never writes `defaultMode`.
- For OpenCode, ws puts its deny rules after yours in each `permission` block of your config, because OpenCode takes the
  last rule that matches. A broader `allow` of yours can't sit after a deny ws adds.
- To give a class its own operating-system account, see [Set up a class user](set-up-a-class-user.md).

## Shut the network by class

With a `[classes]` table in the policy, a session starts with the network shut. It can reach the hosts in its class's
`allowed_hosts`, the host of the model provider it was started with, and the hosts of any `[[egress_exceptions]]` that
name the class and haven't ended. A policy with no classes is as it was: Claude Code asks about a host it doesn't know,
and OpenCode's network is open.

The list covers what a shell command or another program starts, through the sandbox or the proxy. It doesn't cover the
runner's own `WebFetch` tool, which runs inside the runner and not in the sandbox. That's a stated limit: if a class
must not fetch pages, put `web_fetch` in `deny_tools` in the policy.

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
  `anthropic` unless you say otherwise). `ws run` takes it from the runner (Claude Code is `anthropic`), else, for an
  OpenCode job, its `provider` option or the part of the model name before the slash. A provider outside the class's
  `allowed_providers` is refused before anything starts, and the refusal names the class only. A walled or counts-only
  class that doesn't say gets a model on this machine (`local`, `ollama`, `lmstudio`, `vllm`, `llamacpp`) and nothing
  else; a `full` class gets any. A folder in no class (and a job with no `cwd`) is held to the strictest. `local_only =
  true` keeps loopback entries and drops the rest.
- **No sandbox, no list.** A session of a class is refused (exit 3) with `--no-sandbox`, or with `[gate] sandbox =
  false` on Claude Code, since the list has nowhere to be applied. WS015 warns of the second.

What enforces it:

- **Claude Code.** The class's list is written as `sandbox.network.allowedDomains` in the settings copy `ws gate launch`
  makes for the project (and the run's settings in `ws run`), with `strictAllowlist` on so a host outside it's refused
  and not asked about (Claude Code 2.1.219 or later; an older one asks). An empty list reaches no host. This copy
  can't stop a project's own settings file from adding hosts, so in a class session the preflight hook refuses every
  call from a folder whose `.claude/settings.json` or `settings.local.json` sets `network.allowedDomains`, a proxy
  port, a Unix socket or local binding. A session with no class doesn't apply that rule. The class file for
  a class user (`ws install --class NAME`) holds the class's hosts too, merged into that copy by `ws gate launch`, with
  `strictAllowlist`; a walled session is refused when a project file adds a host. It's placed by hand, so it carries
  the class's hosts and its providers' but not the exceptions, which end; run `ws install --class NAME` again
  after you change the list.
- **Other runners (OpenCode), on Linux.** `ws gate launch` starts a small proxy of its own on 127.0.0.1 and sets the
  proxy variables for the runner, with a password made for that launch, so another session's proxy can't be used from
  inside. The proxy refuses a host that isn't listed, a name that resolves to a private, link-local or cloud-metadata
  address, and a name that resolves to this machine unless it's a listed model port.
  **It's the route, not a fence.** Loopback is kept off the proxy (`NO_PROXY`), so a model on this machine is reached
  directly, and that means any loopback port is open to the session, including ones another process on the machine
  listens on. A program that ignores the proxy variables and opens a socket itself isn't stopped either. ssh, nc, ncat,
  netcat, telnet, scp, sftp, ftp and socat are replaced on `PATH` inside the wall by a script that says so and exits
  126, and the end-of-run report counts how many requests and tool calls were refused; a tool called by its full path
  isn't caught. What closes the gap is a rule in the host's firewall for the class's OS user; ws doesn't write one (it
  needs root). The proxy checks the destination of a request, not what travels inside it: a `CONNECT` tunnel carries
  any `Host` header or name, and so does anything sent through it once it's open. A tunnel that's already open when
  an exception ends keeps running until it closes; only new requests are refused.
- **Other runners, on macOS.** The same proxy, and the sandbox profile denies all outbound connections except to the
  proxy and the model ports, so a program that skips the proxy is stopped (a loopback model port stays open to it).
  That profile hasn't been run on a Mac.
- **`ws run`.** The same list: in the run's settings for Claude Code, through the proxy for other runners. For a
  runner other than Claude Code, `ws run` gives only the proxy: no wall, no `PATH` shims and no macOS fence, so a
  program that ignores the proxy variables isn't stopped. A run that breaks G7 or has the sandbox off is refused and
  recorded with `refused:egress`.

`ws doctor` shows one WS015 line per class: `ok` with the number of hosts, or `warn` with the reason (see
[Doctor checks](../reference/doctor-checks.md)).

## Check the host once

```sh
ws doctor
```

If WS007 warns, set `kernel.yama.ptrace_scope = 1`. The line is in the check's `fix` text.

## Swap note: The runner's sandbox, and the Claude Code version it needs

The wall for Claude Code is Claude Code's own sandbox and permission keys (`sandbox.*`,
`permissions.blockReadsOutsideWorkingDirectories`, `permissions.disableBypassPermissionsMode`). ws writes the keys and
checks them; it has no sandbox of its own for Claude Code. To swap it for another sandbox, follow the steps in
[the decision record](../decisions/0025-adopt-the-runner-sandbox-and-permission-keys.md#swapping-the-runners-sandbox-for-another).

The wall was last tested on Claude Code 2.1.321, and `ws doctor` (WS016) checks the first version each key needs;
nothing makes you run 2.1.321. To roll back after an upgrade, pin the version you had before it. On the machine this was
tested on that was 2.1.311: `claude install 2.1.311`.
