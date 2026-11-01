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

The IDs are in two ranges. `WS0xx` checks the machine, the profile, the jobs and the gate. `WS1xx` checks the roles,
one per role. You configure a role as a `[roles.NAME]` table in `profile.toml`.

## Machine checks (WS0xx)

| ID | Name | What it checks | Fix |
|---|---|---|---|
| WS001 | platform | The system is macOS, Linux or WSL2. `fail` on native Windows or an unknown system. | Install WSL2 and run ws inside it, or use macOS or Linux. |
| WS002 | python | Python is 3.11 or newer. | Install Python 3.11 or newer. |
| WS003 | profile | `profile.toml` exists and parses. `unconfigured` if the file is missing. `fail` if it doesn't parse. | Run `ws init`, or fix or remove the file. |
| WS004 | machine | The three `ws init` answers are present: `stays_on`, `server` and `notes`. | Run `ws init`. |
| WS005 | globals | No repo copy shadows a global skill, agent, workflow or hook (`.ws/overrides.toml` lists deliberate ones). `ok` when you aren't in a repo. `fail` on a shadow. | Run `ws globals check`. Remove the copy, or list it in `.ws/overrides.toml` with a reason. |
| WS006 | jobs | Every file in `$WS_HOME/jobs/` loads. `fail` names the files ws would refuse and the first error. | Fix or remove that job file. The other jobs still run. |
| WS007 | ptrace | On Linux and WSL2, `kernel.yama.ptrace_scope` is 1 or more, so a walled child can't read its parent's memory. `warn` at 0 or when Yama is absent. `ok` elsewhere. | `echo 'kernel.yama.ptrace_scope = 1' \| sudo tee /etc/sysctl.d/10-ptrace.conf && sudo sysctl --system` |
| WS008 | gate | Where the gate lives. With `gate = "every-session"` in `[roles.runner]`, ws's entries are in the runner's config file (`~/.claude/settings.json`, or `opencode.json` plus the plugin) and match the current policy, and the hook command still starts. `warn` when the policy changed since the last `ws install` (the line also counts the entries the current release no longer asks for, and names their places in the file), when the profile says `launch` but ws's entries are still in the file, and once for each guard that's on in the policy and the runner doesn't run, and once more (same ID, one line each) for each of your `permissions.allow` rules that a ws deny rule overrides. `fail` when ws's entries are gone, the file is gone or won't read, or the hook command no longer starts. With `gate = "launch"` it's `ok`: a session is gated when started with `ws gate launch`. | Run `ws install` to put the entries back or refresh them. `ws install --gate launch` takes ws out of the file. |

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
