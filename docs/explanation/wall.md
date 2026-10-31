# The wall

The wall is the rule that a session can write only inside its project folder (and a few folders ws names), and can read
nothing under your `deny_read` paths, whatever program it starts.

## The rule

A walled session sees the whole disk read-only, except the project folder, a private temporary folder, the runner's own
private state and any folder you list in `allow_write`. ws's own files are read-only even when one sits inside the
project. Paths you deny for reading look empty.

## Why it exists

The [gate](gate.md) reads the text of a command. A program that builds a path at run time, an interpreter, a script that's
written and then run, or the version-control tool restoring a file all do their work below the text. A sandbox acts on the
system calls themselves, so a write to a protected file fails with a read-only error whatever program makes it.

The second reason is delayed harm. A session that can write a git hook, a runner plugin or a shell start-up file leaves
something that runs later, outside any wall. The wall treats those files as read-only for the same reason.

## How ws enforces it

The code is `src/ws/sandbox.py`. You reach it with `ws gate launch --runner R -- COMMAND`.

| Where | What ws uses |
|---|---|
| Linux, WSL2 | bubblewrap, with a seccomp filter that ws compiles itself |
| macOS | `sandbox-exec` with a generated profile (untested on a real Mac; see below) |
| Claude Code | Claude Code's own sandbox, switched on by the settings ws generates |

What a walled session can't do:

- **Write outside its allow-list.** Root is read-only. The project folder, a private temporary folder, the runner's
  private folders and `allow_write` are writable.
- **Plant something that runs later.** The folders and files a runner or git loads at start are read-only whether or
  not they exist: `.claude`, `.opencode`, `.mcp.json`, `opencode.json`, `tui.json`, and in every git folder the hooks,
  config and submodule repositories. `mv .git .git2` is refused. OpenCode's data and cache folders are private to the
  wall and to the project, so a program planted there can't run outside the wall.
- **Reach the host.** Unix sockets are shut (no `tmux`, `screen`, `docker` or `systemd-run --user` on the host), the host
  process list is hidden, and typing into your terminal (`TIOCSTI`) is refused. `ptrace` and `process_vm_*` are refused.
- **Read denied paths.** A `deny_read` folder becomes an empty folder and a denied file becomes `/dev/null`.

After every walled run, the launcher compares the project with how it was and reports anything that will run later: a
new or changed git folder, runner config, program loaded on its own in a virtual environment or `node_modules`. It reports
and deletes nothing. The report goes to standard error and to `$WS_HOME/logs/wall-report.jsonl`.

The wall fails closed. With no bubblewrap, or with unprivileged user namespaces switched off, `ws gate launch` refuses and
prints the install line. `--no-sandbox` starts the runner bare and says what that gives up, so skipping the wall is a
visible choice.

## What the wall doesn't stop

- **The network.** The wall leaves the network as it's, because the runner needs its model. `allow_hosts` filters hosts
  on Claude Code only.
- **`/proc/PID/mem` at `ptrace_scope` 0.** On a host where `kernel.yama.ptrace_scope` is 0, a process in the wall can read
  the memory of a runner that doesn't mark itself non-dumpable (Claude Code). `ws doctor` warns (WS007) and prints the
  `sysctl` line that fixes it.
- **The macOS wall is unproven.** The `sandbox-exec` profile is tested as text only. No CI job runs it on a Mac.
- **Things created during the session.** A repository the session makes with `git init`, a submodule it adds and config
  in a sub-folder (`sub/.claude`) are reported after the run, not blocked.
- **A shell command inside an interpreter.** A `deny_commands` rule doesn't apply inside `python3 -c`. Only the file
  rules of the wall do.
- **OpenCode's `patch` tool.** It's not a shell command, so the wall doesn't cover it.
- **A program that writes a hard-coded path outside the allow-list.** It fails. Add the folder to `allow_write`.

See [Known limits](known-limits.md) for the full list.

## Do it

Follow [Use the wall](../how-to/use-the-wall.md).
