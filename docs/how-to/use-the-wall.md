# Use the wall

Start an AI session inside a sandbox, so that it can write only inside its project and can't read your denied paths.
To understand the rule first, read [The wall](../explanation/wall.md).

## Before you start

- Linux or WSL2 with [bubblewrap](https://github.com/containers/bubblewrap) (`sudo apt install bubblewrap`, or
  `sudo dnf install bubblewrap`). On macOS the wall uses `sandbox-exec`, which is untested in CI; see
  [Known limits](../explanation/known-limits.md).
- A policy and generated runner files: [Use the gate](use-the-gate.md).
- For Claude Code, the settings generated into `$WS_HOME/gate/claude-code` (`ws setup` or `ws gate generate` writes them).

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
   ws gate launch --runner opencode -- sh -c 'touch "${WS_HOME:-$HOME/.ws}/policy.toml"'
   ```

4. When the runner exits, read the end-of-run report. ws prints it to standard error, and appends a line to
   `$WS_HOME/logs/wall-report.jsonl`, only when the session left something that would run later: a new git hook, runner
   config, or a program that loads on its own. It deletes nothing. No report means nothing to report.

   ```sh
   cat "${WS_HOME:-$HOME/.ws}/logs/wall-report.jsonl"
   ```

## Success

Step 3 prints `touch: cannot touch '...': Read-only file system` and exits with a non-zero code. The wall is working.

## If it refuses

`ws gate launch` fails closed. With no bubblewrap, or with user namespaces off, it prints the install line. See
[Troubleshoot](troubleshoot.md). `--no-sandbox` starts the runner bare and prints what that gives up. Don't use it for
real work.

## Check the host once

```sh
ws doctor
```

If WS007 warns, set `kernel.yama.ptrace_scope = 1`. The line is in the check's `fix` text.
