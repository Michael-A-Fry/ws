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

### Reads are denied only for paths that exist at start

The wall hides a `deny_read` path only when it exists when the runner starts and has no wildcard below its first folder.
A `deny_read` rule with a wildcard in the middle is enforced by the matcher only.

- **What it means.** A denied file created after the session starts isn't hidden.
- **Workaround.** Create the folder before you start, or name the folder instead of a pattern.

### Protected paths that don't exist yet can be created

A protected path inside the project folder or an `allow_write` folder that doesn't exist when the runner starts can be
created by the session. A program that writes a hard-coded path outside the project fails.

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

## The gate and approvals

### The matcher reads text

The matcher can't see what an interpreter does inside its program, a script downloaded and run in two steps, a path a
command builds, or the version-control tool restoring a protected tracked file.

- **Workaround.** Rely on the wall for these, and keep `ask_commands` for the actions that matter.

### The default lists name common places only

The matcher reads a path in an option's value, but only against your `deny_read` list. A credential file the list
doesn't name is readable. The env-file rules cover `.env` and `.env.NAME`, and the runner's own deny rules also cover
`.env.example`. A git alias set before the session starts runs as written. The git rules name the settings and
environment names that run a program or force a push; a key or name they don't list, or a setting in a config file the
session didn't write, isn't checked.

- **Workaround.** Add your own paths to `deny_read`, and rely on the wall.

### The environment deny is a speed bump

The default policy denies `env`, `printenv` and a read of `/proc/PID/environ`. The shell builtins `set`, `export -p`,
`declare -x` and `compgen -v` can still print environment values. The deny list is a speed bump, not a wall. The launch
environment allow-list, which passes only the variables a runner needs, is the wall.

- **Workaround.** Launch through `ws gate launch`, and keep secrets out of the environment.

### Approvals aren't a defence against a determined session

A session that allocates its own terminal and clears the session marker can defeat the first two `ws approve` checks.

- **What it means.** Approvals stop a model approving itself by accident or by a plain instruction.
- **Workaround.** Keep the wall on. It's what stands behind the matcher.

### An argument error under `WS_OUTPUT=json` prints no reason to the runner (P3)

Under `WS_OUTPUT=json`, an argparse error or `-C` error before dispatch on `gate check` prints the JSON envelope to
standard output and nothing to standard error. It still exits 2 and still denies. The runner shows no reason.

- **Workaround.** Don't set `WS_OUTPUT=json` in the environment of a hook.

### The gate in every session

With `gate = "every-session"` (the default for a new profile), `ws install` adds ws's deny rules, hooks and env keys to
your runner's own config file, beside yours. It records exactly what it added and saves your file's bytes first. See
[Upgrade and uninstall](../how-to/upgrade-and-uninstall.md) for the undo. What stays outside the gate:

- **No wall in the shared file.** The file carries no `sandbox` block. A global `failIfUnavailable` sandbox would stop
  Claude Code on a machine without bubblewrap, and it would override your own sandbox choices. The wall still comes from
  `ws gate launch`. Where the launch settings and your config file hold the same hook, Claude Code runs it once. OpenCode
  loads the global plugin and the launch plugin, so a launched OpenCode session asks the gate twice.
- **What a session can't edit.** In every session: the config file ws merged into, a project's
  `.claude/settings.json`, `.claude/settings.local.json` and `.mcp.json`, the hooks and config of any repo's git folder,
  ws's own control files and the folder of the Python that runs the hook. Change those in a terminal of your own.
  Everything else under a project's `.claude` folder, including a `.claude/worktrees/` checkout, stays editable. The
  wider project locks (the whole `.claude` folder, `opencode.json` and the rest of the git folder) apply only to a
  session started with `ws gate launch`, inside the wall. A later undo keeps whatever you added to the config file.
- **A project can turn the hooks off.** A project's `disableAllHooks` setting turns off every hook for sessions in that
  project, including ws's gate check. ws's deny rules stay, but they're only part of the gate. Headless jobs from
  `ws run` load only your user settings, so a project's setting doesn't reach them.
- **A running session keeps the hooks it started with.** Claude Code reads its hooks at start. After `ws install`
  changes the file, start a new session to get the change. A session that began before the gate went in stays ungated
  until you restart it, and one that began before an undo keeps calling the hook until you restart it.
- **Some files are refused.** A config file that's a link, isn't plain JSON (comments, a trailing comma), or is
  `opencode.jsonc`, is left alone. So is one where you've set an `env` key, or an OpenCode rule, to a value that
  contradicts ws's. `ws install` names the key and changes nothing. Use `ws gate launch` for those machines.
- **Layout.** ws keeps your indentation and key order. A rewrite lays the JSON out in one style, so hand-aligned inline
  arrays come back expanded. The saved copy in `$WS_HOME/backups/` is exact, and it can hold env values you put in the
  file, so it's mode 0600.
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

The daily limit of 1 digest and 3 pushes counts what ws sends. A session that runs ws with a `WS_HOME` of its own gets a
fresh counter, and one that can reach a topic with no token can post to it directly.

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
- **The plugins `mail-drafts`, `calendar`, `finance` and `knowledge-graph`** are listed and marked "not built yet."
- **The secrets wall on your machine** is a step you take (`ws secret wall`). Until you do, WS104 warns.
- **Trust on first use of the root-owned `/opt/ws` copy** closes when you install from a signed release tag. See
  [Verify a release](../how-to/verify-a-release.md).

## Small defects (P3)

- Tests leak `WS_OUTPUT` and `WS_GATED_SESSION` from the environment. This affects contributors, not users.
- A `canary.jsonl.old` folder planted in `$WS_HOME` silently stops canary lines.
- `PYTHONPYCACHEPREFIX` is set in the OpenCode wall only, so `.pyc` files crowd the Claude Code end-of-run report.
- A `.git/hooks` folder with many files isn't counted against the report's scan budget.
