# Doctor checks reference

`ws doctor` prints one line per check. Each check has a stable ID. An ID never changes meaning after release. A new check
gets a new ID.

```sh
ws doctor              # all checks
ws doctor --offline    # skip checks that call a provider
ws doctor --json       # the same report for scripts
```

## Status and exit code

| Status | Meaning |
|---|---|
| `ok` | The check passed. |
| `warn` | It works, and something is worth fixing. Doesn't change the exit code. |
| `fail` | It's broken. |
| `unconfigured` | Nothing is set up for this check yet. The `fix:` line shows what to add. |

`ws doctor` exits 0 only when no check is `fail` or `unconfigured`. Otherwise it exits 1. A new machine exits 1 until you
set up each role.

The IDs are in two ranges. `WS0xx` checks the machine, the profile, the jobs, the gate, the plugins, what the runner
needs of the gate's hooks, the OS-user classes and the network of each data class. `WS1xx` checks the roles, one per
role. You configure a role as a `[roles.NAME]` table in `profile.toml`.

## Machine checks (WS0xx)

| ID | Name | What it checks | Fix |
|---|---|---|---|
| WS001 | platform | The system is macOS, Linux or WSL2. `fail` on native Windows or an unknown system. | Install WSL2 and run ws inside it, or use macOS or Linux. |
| WS002 | python | Python is 3.11 or newer. | Install Python 3.11 or newer. |
| WS003 | profile | `profile.toml` exists and parses. `unconfigured` if the file is missing. `fail` if it doesn't parse. | Run `ws init`, or fix or remove the file. |
| WS004 | machine | The three `ws init` answers are present: `stays_on`, `server` and `notes`. | Run `ws init`. |
| WS005 | globals | No repo copy shadows a global skill, agent, workflow or hook (`.ws/overrides.toml` lists deliberate ones). `ok` when you aren't in a repo. `fail` on a shadow. | Run `ws globals check`. Remove the copy, or list it in `.ws/overrides.toml` with a reason. |
| WS006 | jobs | Every file in the `jobs/` folder of the config folder loads. `fail` names the files ws would refuse and the first error. | Fix or remove that job file. The other jobs still run. |
| WS007 | ptrace | On Linux and WSL2, `kernel.yama.ptrace_scope` is 1 or more, so a walled child can't read its parent's memory. `warn` at 0 or when Yama is absent. `ok` elsewhere. | `echo 'kernel.yama.ptrace_scope = 1' \| sudo tee /etc/sysctl.d/10-ptrace.conf && sudo sysctl --system` |
| WS008 | gate | Where the gate lives. With `gate = "every-session"` in `[roles.runner]`, ws's entries are in the runner's config file (`~/.claude/settings.json`, or `opencode.json` plus the plugin) and match the current policy, and the hook command still starts. `warn` when the policy changed since the last `ws install` (the line also counts the entries the current release no longer asks for, and names their places in the file), when the profile says `launch` but ws's entries are still in the file, and once for each guard that's on in the policy and the runner doesn't run, and once more (same ID, one line each) for each of your `permissions.allow` rules that a ws deny rule overrides. `fail` when ws's entries are gone, the file is gone or won't read, or the hook command no longer starts. With `gate = "launch"` it's `ok`: a session is gated when started with `ws gate launch`. | Run `ws install` to put the entries back or refresh them. `ws install --gate launch` takes ws out of the file. |
| WS009 | plugins | The profile switches on a plugin that's still being built. `warn` names it and says it has no effect yet. There's no line when none is on. A profile like that still loads and nothing is lost. | Run `ws plugin remove NAME` to switch it off. |
| WS010 | layout | Where ws keeps its files. `ok` for the XDG folders or the folder you named with `WS_HOME`. `warn` while they're still in `~/.ws`, where an earlier ws kept them (nothing moves by itself). `fail` when `~/.ws` and ws's own files in the XDG folders both exist, naming both. | Run `ws migrate --dry-run` to see the move, then `ws migrate`. |
| WS011 | hooks | Claude Code is 2.1.295 or newer, the first release that lets a hook say a failure blocks the call (`onFailure`). Every PreToolUse hook ws writes sets it, so a hook that crashes or times out blocks and doesn't let the call through. `fail` below 2.1.295. `warn` when the version can't be read. There's no line unless `[roles.runner]` is `claude-code` and `claude` is on `PATH`. | Update Claude Code. |
| WS012 | sandbox | The wall's settings, read from the files; no probe runs. `ok` when your settings file holds the sandbox block, its `denyRead` covers every `deny_read` path the wall can hide, and none of these is set in your settings or a managed-settings file ws can read: `sandbox.enabled` false, a non-empty `excludedCommands`, `allowUnsandboxedCommands` true, `filesystem.disabled`, or an `allowRead` or `allowWrite` entry over a `deny_read` path or one of ws's own, or an `allowRead` entry at or under your home folder (Claude Code merges your file with the settings `ws gate launch` writes, so it would re-open the folder those hide). `warn` and "wall widened by user" with the list (setting and file) when one is set, whether or not you used `ws install --allow-widened-sandbox`; `warn` and "stale" when the policy has a `deny_read` path the file's `denyRead` lacks; `warn` and unsandboxed, with a hint to run under `ws gate launch` for fewer refusals, when the policy sets `sandbox = false`, the file has no sandbox block, the runner has none (OpenCode) or the system has none. `fail` on Linux or WSL2 with `gate = "every-session"` when bubblewrap or socat is missing, because Claude Code then refuses to start with the sandbox on. With a class file in place (WS014), an `allowWrite` entry beyond the class's repos and scratch, an `allowRead` entry beyond its repos, or a `permissions.disableBypassPermissionsMode` of another value in your settings file counts as widening it too; a class file that's exactly what ws renders isn't read as a widening. A `--settings` flag and a managed policy outside the files ws reads can widen the wall without showing here. | Run `ws install` for a stale list. For a missing block, set `sandbox = true` under `[gate]` in the policy, then run `ws install`: a config written without the block keeps going without it until the policy says so. Take out the widening setting, or accept it with `ws install --allow-widened-sandbox`. `sudo apt install bubblewrap socat` (Debian, Ubuntu) or `sudo dnf install bubblewrap socat` (Fedora). |
| WS013 | upgrade | The hooks ws wrote into your runner's config are as new as the ws that runs them. `warn` when a ws hook in `~/.claude/settings.json` carries no `--pin` of ws's folders, or the OpenCode plugin has none: an earlier ws wrote it. It still works (the new ws runs the whole check for it and prints a note on each call), so this doesn't change the exit code. `ok` when every ws hook is pinned or there are none. | Run `ws upgrade --hooks-only` in a terminal of your own (a gated session can't). |
| WS014 | class | One line per part of each class in the policy that names an `os_user`, each judged on its own: the OS user exists (the system's user database, never `$USER`), the managed-settings file `50-ws-class-NAME.json` is present and root-owned (`fail` for a link, a file another account owns or that group or others can write, or a folder like that), the sandbox is required (`enabled` and `failIfUnavailable`), unsandboxed commands are off (`allowUnsandboxedCommands` false), bypass is off (`permissions.disableBypassPermissionsMode` is `"disable"`), writes are limited to the class's repos and scratch, the home folder is hidden but for the repos (`denyRead` `~/`), inbound is lead-only (the file records it, and the policy's `inbound` for the class isn't `allowlist`), and the read block is on (`permissions.blockReadsOutsideWorkingDirectories` is `true`; a file an older ws placed lacks it, which is a `warn` that says to place the file again). A missing user or file is `warn`, and the line says which part; a file that's there and wrong is `fail`. The lines name the class, not the account. There is no line when no class names an `os_user`. | Run `ws install --class NAME`: it prints the commands, sudo included, and the file to place. See [Set up a class user](../how-to/set-up-a-class-user.md). |
| WS015 | egress | One line per data class in the policy, read from the policy and the class file; nothing is probed. `ok` with the number of hosts the class may reach and the number of egress exceptions in force. `warn` (with a count, never a host name) when a host entry can't be read by the proxies (a name with no dot, a loopback entry with no port), when an exception for the class has expired or ends within 7 days, or when the class's managed-settings file in place holds a network list other than the one ws renders now. There is no line when the policy has no `[classes]`, or doesn't load (WS012 says so). | Fix `allowed_hosts` (a name with a dot, or `localhost:PORT`), renew or remove the exception, or run `ws install --class NAME` again to place the current file. See [Use the wall](../how-to/use-the-wall.md). |
| WS016 | wall-keys | Claude Code is new enough to read the settings keys the wall writes. One table (`MIN_CLAUDE` in `ws/wallsettings.py`) gives each key's first version: `permissions.blockReadsOutsideWorkingDirectories` needs 2.1.257, `sandbox.network.strictAllowlist` (written for a data class session) needs 2.1.219, and `sandbox.filesystem.allowRead` (written in the launch settings that hide the home folder, whenever the sandbox is on) needs 2.1.78. The version is probed and read as WS011 does, and `ws gate launch --runner claude-code` prints the same warning to stderr at the start (it doesn't refuse). `ok` when the installed Claude Code reads every key the policy makes the wall write, and `ok` with "not applicable" when `claude` isn't on `PATH`. `warn` when it's older (the line names the key and the version it needs) or when its version can't be read. A second `warn` line, same ID, when `permissions.defaultMode` is `bypassPermissions` in the settings files in force (managed first, then local, project and your own; Claude Code ignores that mode from a project file, so one there isn't counted). ws never writes `defaultMode`. | Update Claude Code. For the bypass line, take `defaultMode` out of the file named, or set it to a mode that asks. |

## Role checks (WS1xx)

| ID | Role | What it checks | Smallest profile table |
|---|---|---|---|
| WS101 | runner | The AI tool named in `adapter` (`claude-code` or `opencode`) is on `PATH` and prints its version. It remembers the version in `versions.json`. When the version or the default model changed since the last look, it runs `ws canary` (or points to it with `--offline`). | `[roles.runner]` with `adapter = "claude-code"` |
| WS102 | model | The default model is named. With `adapter = "anthropic"`, a `key` that names a secret must be in the registry for `api.anthropic.com`. With `adapter = "opencode"`, `default` is `<provider>/<model>` and OpenCode lists it (not checked with `--offline`). | `[roles.model]` with `default = "<model id>"` |
| WS103 | budget | `monthly_cap_usd` is a number above 0. ws refuses paid runs without it. | `[roles.budget]` with `monthly_cap_usd = 20` |
| WS104 | secrets | Where keys live. For `ws-secret`: the registry and store load, the store has safe modes, and the broker answers. `warn` for a store that's yours (run `ws secret wall`) and for a broker whose code isn't locked down. `fail` for a loose store or a walled store with no answering broker. For `sops-age`: the age key file exists and is mode 0600 (`fail` otherwise), and `warn` if the agent's user owns it or the broker doesn't read the role yet. `env` is accepted for trying ws out. | `[roles.secrets]` with `adapter = "ws-secret"` |
| WS105 | memory | The memory folder exists, is a git repository, and its search index builds. `markdown-git` is the default adapter. | `[roles.memory]` with `path = "~/ws-memory"`, then `ws memory init` |
| WS106 | notes | The notes folder exists and `_inbox` is writable. | `[roles.notes]` with `path = "~/Notes"` |
| WS107 | scheduler | The `systemd` or `launchd` adapter matches this machine, and the unit files for jobs with a `schedule` are current. | `[roles.scheduler]` with `adapter = "systemd"`, then `ws schedule apply` |
| WS108 | notify | The `ntfy` topic URL and token are valid, and today's push count against the cap. `none` is OK and sends nothing. | `[roles.notify]` with `adapter = "none"` |
| WS109 | backup | The `restic` repository and password are set, `restic` is on `PATH`, and a restore was verified in the last 30 days. `warn` if never verified. | `[roles.backup]` with `adapter = "restic"` and `repo = "<folder>"`, then `ws backup init`, `ws backup run` and `ws backup verify` |
| WS110 | usage | The provider's own usage report (an Admin API key, for organisations only). `adapter = "none"` is `warn`: spend then comes from ws's own run log. | `[roles.usage]` with `adapter = "anthropic"` or `"none"` |

A role check that crashes reports `fail` with the exception type and withholds the details, in case they hold a secret.

## JSON report

`ws doctor --json` prints the standard envelope. The `data` has these fields:

| Field | Meaning |
|---|---|
| `schema` | The report schema number (1). |
| `version` | The ws version. |
| `os` | The detected system. |
| `profile` | The profile path. |
| `ok` | `true` when no check is `fail` or `unconfigured`. |
| `checks` | A list of `{id, role, status, detail, fix}`. |

See [CLI reference](cli.md) for the envelope and exit codes.
