# Policy reference

`policy.toml` is the one vendor-neutral file that says what a session may do. It lives at `$WS_HOME/policy.toml` (default
`~/.ws/policy.toml`). `ws gate init` writes the commented default. With no file, ws uses the built-in default. A
different file can be named with `--policy FILE` on the `ws gate` commands.

The file is loaded whole or refused. An unknown key, a wrong type or a bad value stops the load, and the message names the
key. The gate then denies every call (it fails closed). The schema is `1`.

## Top-level keys

| Key | Type | Default | Meaning |
|---|---|---|---|
| `schema` | integer | `1` | The schema number. Any other value is refused. |
| `dial` | text | `"prepare"` | How far a timer or session may go: `prepare` (drafts only, no acts), `allowlist` (allow-listed acts, with you at the keyboard) or `act` (timers act within the rules). Anything ws writes for you is `prepare`. |

## `[interrupts]`

| Key | Type | Default | Meaning |
|---|---|---|---|
| `digest_per_day` | integer, 0 or more | `1` | Most digests ws sends you in a day. |
| `pushes_per_day` | integer, 0 or more | `3` | Most pushes ws sends you in a day. |

The `ntfy` notify role enforces these for ws's own sends. See [Known limits](../explanation/known-limits.md).

## `[sources.NAME]`

One table per source of content. `NAME` uses letters, digits, `_` and `-`. The [Rule of Two](../explanation/rule-of-two.md)
reads these.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `mode` | text | `"read"` | `read` (look only), `act` (may act, as far as the dial allows) or `leave` (hands off: a job that reads it's refused). |
| `untrusted` | true or false | `false` | Anyone can write to this content. Leg A of the Rule of Two. |
| `sensitive` | true or false | `false` | Private data. Leg B. |
| `acts` | list of text | `[]` | What ws may do here when `mode = "act"` and the dial allows. |
| `outward` | list of text | `[]` | Acts that reach outside (send, post, publish). They count as a way out (leg C) only when `mode = "act"` and `dial` isn't `prepare`. |

## `[gate]`

A key you leave out is empty. A policy file with no `deny_commands` denies no commands. With no policy file at all, ws uses
the built-in default, which has the list below.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `deny_commands` | list of text | `[]` in a file | Shell command patterns no session may run. `ws gate init` writes the [built-in list](#built-in-deny_commands). |
| `deny_read` | list of text | `[]` in a file | Paths no session may read. `~/` is your home. `**` crosses folders. `ws gate init` writes `~/.ssh/**`, `~/.gnupg/**` and `~/.aws/credentials`. Whatever the file says, ws also denies reads of its own secrets folder (`$WS_HOME/secrets`, and the `[roles.secrets] store` folder when the profile names one), for every runner. |
| `deny_tools` | list of text | `[]` | Neutral tool names to switch off. Known: `web_fetch`, `web_search`. An allowed web tool counts as a way out. |
| `ask_commands` | list of text | `[]` | Command patterns that run only after you approve that exact call. Same globs as `deny_commands`. A command that also matches `deny_commands` stays denied. |
| `allow_hosts` | list of text | `[]` | Hosts a session's shell may reach, for runners whose sandbox filters the network (Claude Code). Empty means the policy says nothing about the network. |
| `allow_write` | list of text | `[]` | Folders besides the project that a walled session may write. |

Rules:

- A pattern is a glob. `*` matches anything and `\*` is a literal star. Each command is checked per shell segment (`a
  && b`,
  pipes, `$(...)`, `bash -c "..."`). Flags are normalised first.
- Every item must be non-empty text.
- `allow_hosts` items are a name, `*.name` or an IP, with an optional `:port`.
- `allow_write` items must be a folder, absolute or under `~/` (`$HOME` is accepted), with no wildcard, not `/` and
  not the
  home folder after `.` and `..` are resolved.
- `deny_tools` items must be a known tool name.

### Built-in `deny_commands`

The default list, which `ws gate init` writes:

- Recursive deletes of `/`, `/*`, `~`, `~/*`, `/home`, `/Users`, and of the system folders `/bin`, `/boot`, `/dev`, `/etc`,
  `/lib`, `/lib64`, `/proc`, `/root`, `/sbin`, `/sys`, `/usr`, `/System` and `/Library`. Written as `rm -* PATH`.
- A forcing push in any spelling: `git push*--force*`, `git push* -f*`, and both with git's global options.
- `mkfs*` and `dd*of=/dev/*`.
- A shell that reads its script from standard input: `sh`, `sh -s*` and `sh -`, and the same three for `bash`, `zsh` and
  `dash`.

## `[guards]`

Named checks that run beside the `[gate]` rules. The [guards reference](guards.md) describes each one.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `on` | list of text | `[]` | The guards that run, in order. A name ws doesn't know stops the load. `ws gate init` writes `["syntax-check", "session-context"]`. |
| `[guards.syntax-check]` `extra` | table of extension to command | `{}` | A checker for an extension, such as `{ yaml = ["yamllint", "-d", "relaxed"] }`. The file path is added as the last argument. |
| `[guards.session-context]` `files` | list of text | `[".claude-state/STATE.md"]` | Project-relative files to list at session start. |

A file with no `[guards]` table has no guards on. `bulk-staging` and `record-calls` take no table.

## `[[exceptions]]`

A named Rule of Two exception. Each entry needs all four keys.

| Key | Type | Meaning |
|---|---|---|
| `name` | text | A unique name. The run record shows `exception:NAME`. |
| `job` | text | The job the exception covers. |
| `sources` | list of text | The sources it covers. At least one. Each must be defined under `[sources]`. |
| `reason` | text | Why. |

The exception applies only when the job name matches and every source the session reads is in `sources`.

## What a session may never write

Whatever the policy says, the gate denies a session writing these ws control paths under `$WS_HOME`: `profile.toml`,
`policy.toml`, `secrets.toml`, `jobs`, `release`, `runs`, `logs`, `sessions`, `approvals`, `install-manifest.json` and
`ratings.jsonl`. It also protects the policy file in use and any path passed with `--protect` (the generated folder,
for example).
Reading `logs/runs.jsonl` stays allowed.

## Example

```toml
schema = 1
dial = "prepare"

[interrupts]
digest_per_day = 1
pushes_per_day = 3

[sources.mail]
mode = "read"
untrusted = true
acts = ["draft"]
outward = ["send"]

[sources.notes]
mode = "read"
sensitive = true
acts = ["write"]

[gate]
deny_read = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/credentials"]
deny_tools = ["web_fetch", "web_search"]
ask_commands = ["git push*"]
allow_hosts = ["api.anthropic.com", "github.com"]
allow_write = ["~/.cache/uv"]

[guards]
on = ["syntax-check", "session-context", "bulk-staging"]

[guards.syntax-check]
extra = { yaml = ["yamllint", "-d", "relaxed"] }

[[exceptions]]
name = "morning-digest"
job = "digest"
sources = ["mail", "notes"]
reason = "reads mail, writes only to me"
```

The `deny_commands` list is left out of this example, so it's empty. Add your rules after copying the built-in list.
