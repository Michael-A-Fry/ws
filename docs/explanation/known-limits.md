# Known limits

This page lists what ws doesn't do, or does only partly, as of v0.1. Each entry says what it means for you and what to
do about it. The source of truth is `TODO.md` in the repository, and the module docstrings that the entries come from.

If you find a limit that's not here, report it through [SUPPORT.md](../../SUPPORT.md) or, for a security gap, through
[SECURITY.md](../../SECURITY.md).

## Platforms

### Native Windows isn't supported

ws runs on macOS, Linux and Windows through WSL2. On native Windows, `ws doctor` fails WS001 and points you at WSL2.

- **What it means.** Install WSL2 and run ws inside it. The `ws` command doesn't start in a native Windows shell.
- **Workaround.** `wsl --install`, then install ws in the Linux shell.

### The macOS wall is untested in CI

The `sandbox-exec` profile that walls a session on macOS is checked as text only. No CI job runs it on a real Mac, and
neither does the `PT_DENY_ATTACH` call in the eval checker.

- **What it means.** On a Mac, treat the wall as unproven. The gate (the runner's deny rules and the hook) still works.
- **Workaround.** Run sensitive work on Linux or WSL2, or use Claude Code, which has its own sandbox on macOS.

### The sandbox covers shell commands only

Claude Code's sandbox confines the programs a shell command starts. Its own Read, Edit and Write tools, MCP tools and
hooks don't run in it. They're covered by the permission rules (`permissions.deny`, written from the policy) and by the
gate. Reads by Claude's file tools outside the working folders are held by
`permissions.blockReadsOutsideWorkingDirectories` (Claude Code 2.1.257 and newer), which ws sets in the launch
settings and in a class's launch file, and never in your own settings file. `[gate] read_outside_project =
true` leaves it out. `ws doctor` (WS016) warns when your Claude Code is too old to read it.

- **What it means.** A Read of a path the policy doesn't deny, inside the working folders, is allowed whatever the
  sandbox says. A hook or an MCP server runs with your account's reach.
- **Workaround.** Put the paths you care about in `deny_read`, which ws writes as both a Read rule and a sandbox
  `denyRead`. Keep MCP tools to the ones `allow_mcp` names.

### What a walled class's settings can't hold, and what an OpenCode session lacks

Claude Code reads `allowManagedHooksOnly`, `allowManagedPermissionRulesOnly` and `sandbox.network.allowManagedDomainsOnly`
(and `sandbox.filesystem.allowManagedReadPathsOnly`) only from managed settings. In a `--settings` file, which is how
`ws gate launch` passes a class's launch file, they do nothing, and in the machine-wide managed file they would switch off
every account's own hooks and permission rules. So ws doesn't write them to the launch file (ADR 0027).

- **What it means.** A project that adds a hook, an MCP server or an allow rule isn't stopped by Claude Code itself. ws
  refuses to start a walled session, and refuses each call, when a project's `.mcp.json` or `.claude/settings*.json`
  holds anything outside a short allow-list (the keys and `env` names are in the wall page); the refusal reads the
  repository's files, so a file created by the session after it starts is caught on the next call, not before it. The
  check reads a skill's, agent's and command's frontmatter and refuses the keys that run code; it doesn't read their
  bodies, so a skill's `!command` line stays allowed. The sandbox and the session's permission rules hold it,
  with `allowed-tools` refused so a skill can't pre-approve it; this check doesn't judge the command.
- **What it means for OpenCode.** An OpenCode session in a class is walled by ws's own wall: writes only in the project
  and scratch, the network through ws's proxy (loopback only under `local_only`) and the same MCP rules. That wall hides
  the `deny_read` paths, the class's per-folder `deny_read` and the other classes' folders, and not the whole home, so
  for the rest of the home the class's own account and the folder modes are the boundary. A project config that names an
  MCP server or a plugin, or pulls in a file with `{file:`, is refused at start. A `deny_read` entry with a wildcard is
  left to the matcher, and a per-folder `deny_write` isn't applied to OpenCode.
- **A class's own home folder.** When a class's repos sit inside its home folder, the launch file denies Claude's Read
  and Edit tools on the dot-folders and the top-level dotfiles there (`~/.*/**` and `~/.*`, so `~/.claude.json` and the
  shell start files too). The rest of that home, the non-dot files outside the repos, is covered by the class's own
  account and the folder modes only. The sandbox hides the whole home from shell commands either way.
- **One class per machine.** One walled class on a machine is the tested shape. Claude Code reads every file in the
  managed-settings folder for every account, so the managed-settings files of two classes on one machine are read
  together, and ws doesn't test that. Each launch file is read by its own class's sessions only. `ws doctor` (WS019)
  and `ws install --class NAME` warn when more than one is in place.
- **Workaround.** Keep each walled class's repos in a folder only its account can reach, and keep your own home folder
  closed (`chmod 700`).

### The secret wall is Linux with systemd only

`ws secret wall` prints the steps for Linux with systemd. On macOS and WSL2 it refuses and says it's not built yet.

- **What it means.** The store stays protected by file modes only, and `ws doctor` keeps WS104 at warn.
- **Workaround.** Keep the store under your own user and accept that a program running as you can read it.

### The lab VM cell hasn't run

CI runs the timed first-task check on Ubuntu, macOS and WSL2. The fourth cell, a lab virtual machine, has no machine
behind it yet, and `scripts/lab-host-cycle.sh` hasn't run on a real host. A lab run waits in the queue and GitHub cancels
it after 24 hours.

- **What it means.** The release is timed on hosted runners, not yet on a machine that's not a disposable cloud image.
- **Workaround.** None needed to use ws. See the [lab reference](../reference/lab.md).

## The wall

### `/proc/PID/mem` is readable at `ptrace_scope` 0

On a host where `kernel.yama.ptrace_scope` is 0 or absent, a process inside the wall can read the memory of a runner that
doesn't mark itself non-dumpable (Claude Code). As root it can always do so. The OpenCode shim closes this for OpenCode.

- **What it means.** A walled child of the runner could read the runner's memory, including the in-process gate.
- **Workaround.** Run `echo 'kernel.yama.ptrace_scope = 1' | sudo tee /etc/sysctl.d/10-ptrace.conf && sudo sysctl --system`.
  `ws doctor` warns (WS007) until you do. Don't run evals as root.

### Things the session creates are reported, not blocked

A repository the session makes with `git init`, a submodule it adds during the session and config in a sub-folder
(`sub/.claude`) are named in the end-of-run report (`logs/wall-report.jsonl`), not blocked.

- **What it means.** Something planted this way could run on your next unwalled start.
- **Workaround.** Read the end-of-run report after each walled run.

### A held runs-log lock blocks every run

A session that keeps the runs-log lock held blocks every `ws run`. ws fails closed and names the lock. Without the
wall, a
program the session starts can also write the `logs/` folder.

- **What it means.** A stuck process can stop your jobs until you clear it.
- **Workaround.** Find and stop the process that holds the lock, then run again. Use the wall.

### Where the home folder is hidden

In the settings that `ws gate launch --runner claude-code` writes, Claude Code's sandbox hides your home folder from every
shell command, except the project, its git folders and the folders common tools need (`[gate] allow_read` adds more). A
session started another way hides only the `deny_read` paths: an OpenCode session, and a Claude Code session started
without `ws gate launch`, whose settings are the ones `ws install` merged into your own file.

- **What it means.** Start Claude Code through `ws gate launch --runner claude-code -- claude` to have the home folder
  hidden. A tool kept in a folder the defaults don't name can't be read from there until it's in `allow_read`.
- **Workaround.** Add the folder to `allow_read` in the policy; the next launch reads it.
- **Claude Code's own folder.** A shell command reads only `shell-snapshots/`, `skills/` and `plugins/` of it. A script
  kept elsewhere in that folder can't be read until it's in `allow_read`.
- **A tool's cache folder made during a session.** A folder a tool creates in the home folder after the session starts
  (a new cache, say) can't be read until the session restarts, because the allow list is read at the start.
- **Where the wall applies.** The home block and `deny_read` cover shell commands through the sandbox and Claude's file
  tools through the permission rules, as in "The sandbox covers shell commands only"; a hook or an MCP server isn't held.
- **A git folder outside the project.** A project whose `.git` is a file is followed out of the project only into the
  `worktrees/` folder of its common git folder (a linked worktree). A `--separate-git-dir` folder is left out, so git
  fails in that project until the folder is in `allow_read`.

### Reads are denied only for paths that exist at start

The wall hides a `deny_read` path only when it exists when the runner starts and has no wildcard below its first folder.
A `deny_read` rule with a wildcard in the middle is enforced by the matcher only.

- **What it means.** A denied file created after the session starts isn't hidden.
- **Workaround.** Create the folder before you start, or name the folder instead of a pattern.

### Protected paths that don't exist yet can be created

A protected path inside the project folder or an `allow_write` folder that doesn't exist when the runner starts can be
created by the session. The runner's own load paths are the exception: ws makes an empty placeholder for a missing
`.claude` or `.mcp.json` (and OpenCode's names, for OpenCode) before the start, so the session can't create one, and
takes it down after. A load path that's a symbolic link is refused. A program that writes a hard-coded path outside the
project fails.

- **Workaround.** Create protected files before you start. Add the folder a program needs to `allow_write`.

### Some write-time config is out of reach

`.git/hooks` and `.git/config` are read-only inside the wall, so `git config` and installing a git hook fail inside a
walled session. `CLAUDE.md`, `AGENTS.md` and `.gitmodules` are content and stay writable. A sign-in that the walled
OpenCode refreshes isn't carried back to the real data folder. A killed launcher leaves empty placeholder files until
the next launch in that project.

- **Workaround.** Run those steps outside the wall, or sign in again outside it.

### Interpreters and `patch`

A `deny_commands` rule isn't enforced inside an interpreter (`python3 -c 'os.system("...")'`). Only the wall's file
rules apply there. OpenCode's `patch` tool isn't a shell command, so the wall doesn't cover it, and only its native
`edit` rules apply.

- **Workaround.** Deny the interpreter itself, or run without it.

### The network is open in the wall

The wall doesn't cut the network, because the runner needs its model. `allow_hosts` filters hosts on Claude Code only.
The grader's wall in `ws eval` has the same network as the runner's wall.

- **Workaround.** Use Claude Code if you need host filtering, or filter at your firewall.

### The runner gets a short list of environment variables

`ws gate launch` hands the runner `PATH`, `HOME`, the locale and terminal names, the named `LC_` and `XDG_` variables,
the variables that say where ws's folders are (`WS_HOME`, or the four `WS_*_DIR` ones), the broker socket and the
runner's own configuration variables. The rest of the launching shell's environment stays behind.

- **What it means.** A key you export in that shell isn't in the session unless you name it with `--pass-env NAME`.
  A name you pass is readable by the session and by every program it starts. A runner behind a proxy needs the proxy
  variable passed too.
- **Workaround.** Pass only what the runner needs, or use a lease from the [broker](broker.md) so the session never
  holds the value.

## The gate and approvals

### The matcher reads text

The matcher can't see what an interpreter does inside its program, a script downloaded and run in two steps, a path a
command builds, or the version-control tool restoring a protected tracked file.

- **Workaround.** Rely on the wall for these, and keep `ask_commands` for the actions that matter.

### OpenCode can't tell MCP tools from its own

OpenCode names an MCP tool `<server>_<tool>`, and the hook payload is that name and the arguments, with nothing that
marks it as an MCP tool. So the OpenCode shim checks no MCP call: `allow_mcp` and the path checks on arguments don't
apply there, and the shim leaves an MCP tool to OpenCode's own permissions.

- **What it means.** A `deny_tools` entry for a literal server (`mcp__server__tool`, `mcp__server__*`) is written into
  OpenCode's `permission` block as `server_tool` or `server_*`, and OpenCode enforces it. A glob with a wildcard server
  (`mcp__*__delete_*`) isn't written, because `*_delete_*` would also catch OpenCode's own tools.
- **Workaround.** Use Claude Code for MCP servers that matter, or set `permission` rules for them in OpenCode's own
  config.

### Approvals aren't a defence against a determined session

A command that has left the session's process tree and cleared the session marker can defeat the `ws approve` checks.

- **What it means.** Approvals stop a model approving itself by accident or by a plain instruction. The same holds for
  the other admin commands (secrets, `gate generate`, `install --gate`): under an agent runner, `ws` accepts only the
  terminal the runner holds, with the command in its foreground. A command started through a daemon (`tmux new -d`, a
  double fork, a service manager) has no runner above it, so `ws` can't tell it from your own pane; the matcher reads
  the spellings it knows. `session start` (the runner's own hook runs it) and `broker serve` (`ws run` starts it) are
  left to the matcher.
- **Workaround.** Keep the wall on. It's what stands behind the matcher.

### The resident gate is private to your user, not sealed against your own processes

The hook's client trusts the resident gate (`ws gate serve`) only on a private folder and socket, from a process of your
user id, and on Linux one that's your interpreter running `-m ws gate serve`. Another user can't answer in its place.
A process of your own user that isn't in the sandbox can stop the daemon and start one that looks right.

- **What it means.** With the sandbox on, the run folder is read-only to the session, so it can't. Without the sandbox,
  the gate's static check of writes into the state folder is the barrier, and it reads command text. A stopped or
  missing daemon only makes the hook decide in its own process.
- **Workaround.** Keep the sandbox on (the default for `every-session`; WS012 reports when it isn't).

### An argument error under `WS_OUTPUT=json` prints no reason to the runner (P3)

Under `WS_OUTPUT=json`, an argparse error or `-C` error before dispatch on `gate check` prints the JSON envelope to
standard output and nothing to standard error. It still exits 2 and still denies. The runner shows no reason.

- **Workaround.** Don't set `WS_OUTPUT=json` in the environment of a hook.

### The gate in every session

With `gate = "every-session"` (the default for a new profile), `ws install` adds ws's deny rules, hooks and env keys to
your runner's own config file, beside yours. It records exactly what it added and saves your file's bytes first. See
[Upgrade and uninstall](../how-to/upgrade-and-uninstall.md) for the undo. What stays outside the gate:

- **The wall in the shared file.** The file carries the `sandbox` block, and that block is the wall for shell commands
  (`sandbox = false` under `[gate]` leaves it out). Allow-with-log, where a refusal for a part ws can't read becomes a
  logged allow inside a proven wall, isn't in 0.1.8: the gate refuses the same lines with the block up or not. Run under
  `ws gate launch` for fewer refusals. Settings you or a team add can widen the block. `ws install` refuses to merge
  beside them unless you pass `--allow-widened-sandbox`, and `ws doctor` (WS012) then reports the wall as widened by
  you. A `--settings` flag or a managed policy outside the files doctor reads can widen it without showing there. Where
  the launch settings and your config file hold the same hook, Claude Code runs it once. OpenCode loads the global plugin
  and the launch plugin, so a launched OpenCode session asks the gate twice.
- **What a session can't edit.** In every session: the config file ws merged into, a project's
  `.claude/settings.json`, `.claude/settings.local.json` and `.mcp.json`, the hooks and config of any repo's git folder,
  ws's own control files and the folder of the Python that runs the hook. Change those in a terminal of your own.
  Everything else under a project's `.claude` folder, including a `.claude/worktrees/` checkout, stays editable. The
  wider project locks (the whole `.claude` folder, `opencode.json` and the rest of the git folder) apply only to a
  session started with `ws gate launch`, inside the wall. A later undo keeps whatever you added to the config file.
- **A project can turn the hooks off.** A project's `disableAllHooks` setting turns off every hook for sessions in that
  project, including ws's gate check. ws's deny rules stay, but they're only part of the gate. A walled class is the
  exception: the launch file sets `disableAllHooks` false, and the launch check refuses a project that sets it. Headless
  jobs from `ws run` load only your user settings, so a project's setting doesn't reach them.
- **A running session keeps the hooks it started with.** Claude Code reads its hooks at start. After `ws install`
  changes the file, start a new session to get the change. A session that began before the gate went in stays ungated
  until you restart it, and one that began before an undo keeps calling the hook until you restart it.
- **Some files are refused.** A config file that's a link, isn't plain JSON (comments, a trailing comma), or is
  `opencode.jsonc`, is left alone. So is one where you've set an `env` key, or an OpenCode rule, to a value that
  contradicts ws's. `ws install` names the key and changes nothing. Use `ws gate launch` for those machines.
- **Layout.** ws keeps your indentation and key order. A rewrite lays the JSON out in one style, so hand-aligned inline
  arrays come back expanded. The saved copy in `~/.local/state/ws/backups/` is exact, and it can hold env values you put
  in the file, so it's mode 0600.
- **OpenCode runs none of the guards yet.** `ws doctor` (WS008) lists each guard that's on in your policy and the runner
  doesn't run.
- **A broken ws blocks calls, not skips them.** The hook fails closed. When ws can't start, each governed call is
  blocked and the message names the two ways out: `ws install --gate launch`, or deleting the PreToolUse group whose
  command contains `gate check` from the file. See
  [When the gate can't start](../how-to/upgrade-and-uninstall.md#when-the-gate-cant-start).
- **ws must be installed to stay.** `ws install` won't put the hook in your config if the Python that runs the hook
  sits in a project's `.venv`, under `.claude/worktrees/` or in a temp folder, because that folder goes away and the
  hook then blocks every call. Install ws with `install.sh`, pipx or `uv tool install` first.
- **Your allow rules lose to ws's deny rules.** Claude Code lets a deny rule win. `ws doctor` (WS008) lists each of your
  `permissions.allow` rules that a ws deny rule overrides, one line each.

## Secrets

### The `sops-age` role isn't read by the broker

`sops-age` is a valid secrets adapter name and `ws doctor` checks its files, but the broker doesn't read from it.

- **Workaround.** Use `ws-secret`, the default.

### The notify cap counts ws's own sends only

The daily limit of 1 digest and 3 pushes counts what ws sends. A session that runs ws with a `WS_HOME` or `WS_STATE_DIR`
of its own gets a fresh counter, and one that can reach a topic with no token can post to it directly.

- **Workaround.** Give the topic a token (`token` in `[roles.notify]`) so the broker holds it.

### The e2e doesn't use a real provider

The end-to-end test doesn't exercise `ws run`'s own broker autostart, a real provider's certificate chain or a real
model. It uses a mock provider.

- **What it means.** Those paths aren't tested end to end. Your first real run is the first test of them.

## Evals

### The grader runs in the same wall as the runner

The `command` grader of `ws eval` has the runner wall's network and read access. A grader run as root gets no nonce
protection.

- **Workaround.** Don't run evals as root.

### Harvest checks names and shapes, not values

`ws eval harvest` refuses a prompt that names a secret in your registry or looks like a key, token, password or URL
with a
password. It doesn't compare against secret values, because the store belongs to the broker.

- **Workaround.** Read each proposal before you approve it. Client or personal data in a prompt is for you to catch.

### The rubric grader isn't built

A task with a `rubric` grader is accepted in the file and skipped at run time. Two metrics read "not measured": time to
first output and attention minutes, because the run record has no fields for them.

- **Workaround.** Use a deterministic grader.

## Features not built

- **Draft-only sending isn't enforced.** ws ships no mail connector and doesn't block a send. See
  [Draft-only sending](draft-only-sending.md).
- **Only the plugins that are built are listed.** `ws plugin list` and the setup screens show `memory`, `notes` and
  `skills`. A plugin still in development stays out of both until it's built.
- **The secrets wall on your machine** is a step you take (`ws secret wall`). Until you do, WS104 warns.
- **Trust on first use of the root-owned `/opt/ws` copy** closes when you install from a signed release tag. See
  [Verify a release](../how-to/verify-a-release.md).

## Small defects (P3)

- Tests leak `WS_OUTPUT` and `WS_GATED_SESSION` from the environment. This affects contributors, not users.
- A `canary.jsonl.old` folder planted in the state folder silently stops canary lines.
- `PYTHONPYCACHEPREFIX` is set in the OpenCode wall only, so `.pyc` files crowd the Claude Code end-of-run report.
- A `.git/hooks` folder with many files isn't counted against the report's scan budget.
