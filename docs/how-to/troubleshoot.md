# Troubleshoot

Find what's wrong, then fix it. Start with the first section, then go to the symptom you have.

## Start here

1. Run the doctor. Every line has a stable ID, so you can search for it.

   ```sh
   ws doctor
   ```

   Exit code 1 means something needs doing, and every `unconfigured` line is expected on a new machine. The
   [doctor checks reference](../reference/doctor-checks.md) lists every ID with its meaning and fix.

2. See which files ws reads and where each path came from.

   ```sh
   ws config
   ws status
   ```

3. Get a machine-readable result of any command. This works on every command.

   ```sh
   ws doctor --json
   ```

   An error has an `error_kind` and a `hint`. The hint says what to try next.

## Symptoms

### `ws: command not found`

The folder that holds the command isn't on your `PATH`. Run `uv tool update-shell` or `pipx ensurepath`, then open a
new terminal.

### WS001 fails

WS001 fails on native Windows. Install WSL2 and run ws in it. On any other system it says the platform is unsupported:
use macOS, Linux or WSL2.

### WS007 warns about `ptrace_scope`

A process inside the wall can read the runner's memory. Set the sysctl once:

```sh
echo 'kernel.yama.ptrace_scope = 1' | sudo tee /etc/sysctl.d/10-ptrace.conf && sudo sysctl --system
```

The reason is in [The wall](../explanation/wall.md).

### `ws gate launch` refuses to start

The wall fails closed. The message names the cause:

- *bubblewrap (bwrap) isn't installed*: run `sudo apt install bubblewrap` (Debian, Ubuntu) or
  `sudo dnf install bubblewrap` (Fedora).
- *bubblewrap is installed but can't start*: unprivileged user namespaces are probably off. On Ubuntu 24.04, run
  `sudo sysctl kernel.apparmor_restrict_unprivileged_userns=0`.
- *refuses a protected path behind a symbolic link*: use the real path.
- *a start from the home folder or above*: start from the project folder.

`--no-sandbox` starts the runner with no wall. It prints what that gives up. Use it only to test.

### A command was blocked and you didn't expect it

The message reads `ws gate: blocked: ...` and names the rule. Edit the rule in `policy.toml`, then run
`ws gate generate` again for each runner. See [Use the gate](use-the-gate.md).

### A command needs approval

The message gives an id. In a terminal of your own, run `ws approve ID`. See [Approve a command](approve-a-command.md).

### `ws approve` says it's for a person

`ws approve` refuses when its standard input isn't a terminal, and when it runs inside a gated session. Run it yourself,
in a normal terminal.

### `ws run` exits with code 3 (refused)

A refusal is recorded too. `ws run JOB --dry-run` shows `would_refuse` with the reason. The common reasons:

- *no monthly budget cap (WS103)*: see [Set a budget](set-a-budget.md).
- *this month's spend is at the monthly cap*: raise the cap or wait for the next UTC month.
- *Rule of Two*: see [Apply the Rule of Two](apply-the-rule-of-two.md).
- *can't say what the model costs*: set `allow_unpriced = true` in the job file.
- *a lock is held*: a process holds the runs-log lock. Find it and stop it.

### `ws run` fails with a broker error

`ws run` starts a per-user broker once if none is running. If the broker is walled (`ws secret wall`), it's a system
service: check `systemctl status ws-broker`. `ws broker status` asks the broker directly. A socket path longer than the
system limit has its own error: set a shorter `WS_HOME`, or set `WS_BROKER_SOCKET`.

### A job file is refused

Job files and their folder must belong to you and not be writable by others. Run `chmod 700` on the `jobs` folder and
`chmod 600` on the file. WS006 names the file and the key.

### `ws secret set` says the store belongs to another user

You ran the secret wall. Run the command it prints, as the broker's user. See [Store a secret](store-a-secret.md).

### The session shows no reason when the hook denies a call

Unset `WS_OUTPUT` in the hook's environment. Under `WS_OUTPUT=json` an error before dispatch prints to standard output
only. See [Known limits](../explanation/known-limits.md).

## Still stuck

Open an issue and include the output of `ws doctor --json` with secrets removed. See [SUPPORT.md](../../SUPPORT.md). For a
security problem, use [SECURITY.md](../../SECURITY.md) and don't open a public issue.
