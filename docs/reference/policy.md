# Policy reference

`policy.toml` is the one vendor-neutral file that says what a session may do. It lives at `~/.config/ws/policy.toml`
(`policy.toml` in the [config folder](stores.md#where-ws-keeps-its-files)). `ws gate init` writes the commented default.
With no file, ws uses the built-in default. A different file can be named with `--policy FILE` on the `ws gate`
commands.

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
| `deny_read` | list of text | `[]` in a file | Paths no session may read. `~/` is your home. `**` crosses folders. `ws gate init` writes `~/.ssh/**`, `~/.gnupg/**`, `~/.aws/credentials`, `~/.config/ws/secrets/**` and `~/.local/share/ws/secrets/**`. Whatever the file says, ws also denies reads of its own secrets folder (`secrets/` in the data folder, and the `[roles.secrets] store` folder when the profile names one), for every runner. |
| `deny_tools` | list of text | `[]` | Neutral tool names to switch off. Known: `web_fetch`, `web_search`. An allowed web tool counts as a way out. |
| `ask_commands` | list of text | `[]` | Command patterns that run only after you approve that exact call. Same globs as `deny_commands`. A command that also matches `deny_commands` stays denied. |
| `allow_hosts` | list of text | `[]` | Hosts a session's shell may reach, for runners whose sandbox filters the network (Claude Code). Empty means the policy says nothing about the network. |
| `allow_write` | list of text | `[]` | Folders besides the project that a walled session may write. |
| `sandbox` | true or false | `true` | Switch on the runner's own sandbox in what ws writes (Claude Code), both for a session started with `ws gate launch` and for one started the usual way once `ws install` has merged the settings into your file. `false` leaves the block out and marks the session unsandboxed, and `ws doctor` (WS012) says so. |

Rules:

- A pattern is a glob. `*` matches anything and `\*` is a literal star. Each command is checked per shell segment (`a
  && b`,
  pipes, `$(...)`, `bash -c "..."`). Flags are normalised first.
- The body of a here-document is text in one plain shape only: lines of `cat` or `tee` steps, joined by a new line,
  `;` or `&&`, where each step is plain words, at most one `>` or `>>` to a plain path and one quoted opener
  (`<<'EOF'`, `<<"EOF"`, `<<-'EOF'`; the name is letters, digits and `_`, and can't start with a digit), and each body
  ends on its exact closing line. A plain word has letters, digits and `_ . / , : @ % + -` only. Anything else in the
  command (a pipe, a group, a function, a variable, a substitution, a comment, a quote, an unquoted or here-string
  opener, a file descriptor, another program, a backslash-newline) and every body is read as commands. The file a
  `tee` or `>` names goes through the same path checks as any other write.
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

### How a command is read

One reader, `src/ws/shellwords.py`, turns a command line into the words a shell would run. Every check on a command
(`deny_read`, `deny_commands`, `ask_commands`, the protected files and the guards) works from its output, so a path is
recognised however it's spelled. It reads:

- Segments: `;`, `&&`, `||`, `|`, `&`, new lines and parentheses, `$(...)`, backticks, `<(...)`, `bash -c '...'`, `eval`
  and `env -S`. A comment is left out, except on a line that holds a here-document, where a `#` line may be part of the
  text a shell expands.
- Words: quotes and escapes are removed, and a brace group is expanded before anything else, as the shell does. Comma
  lists nest and chain, and `{1..40}` and `{a..e}` sequences (with a step, and zero padding such as `{01..10}`) are
  expanded in full. A quoted `{`, `,` or `}` isn't part of a group, so `'{a,b}'` is one word.
- `~`, `$HOME` and `${HOME}`, `$WS_HOME`, `$WS_CONFIG_DIR`, `$WS_STATE_DIR`, `$WS_DATA_DIR`, `$WS_CACHE_DIR` and the
  variables set earlier on the same line (by `NAME=value`, `export`, `declare`, `typeset`, `local`, `readonly`, `read
  NAME <<< word` and a `for` loop, which is read once for each word of its list), including the `${NAME:-x}`,
  `${NAME:+x}`, `${NAME%/*}`, `${NAME##*/}`, `${NAME/a/b}`, `${NAME:1:3}` and `${!NAME}` forms. The word of a default or
  alternative is expanded like any word. `~+`, `~-`, `$PWD` and `$OLDPWD` follow `cd` on the same line. The case-change
  operators (`${NAME,}`, `,,`, `^`, `^^`, `~` and `~~`) change case as bash does, `${NAME@E}` decodes escapes,
  `${NAME@Q}`, `@U`, `@L`, `@u` and `@l` read as the value, and `${NAME[0]}` is the value. A quoted `~` is the folder
  called `~`, and a single-quoted `$HOME` is text. A variable the line doesn't set is left as written; a path word that
  still holds one after that (`/home/$USER/` and then a protected folder) is refused when what follows the unknown part
  names a protected path under the home folder.
- Redirections: `<`, `>`, `>>`, `>|`, `<>`, `&>`, `&>>`, `>&`, here-strings, and file-descriptor forms such as `3<` and
  `2>`. Each target is expanded like any word, so a brace group in a target names every file it expands to.
- Paths inside a word: the value of an option (`dd if=P`, `--opt=P`, `-oP`, `-sSLo P`), a file argument (`-F f=@P`,
  `-d @P`, `-F 'f=<P'`), a `file://` address, a path followed by a separator (a mount such as `-v P:/in/container`),
  and the files a wrapper takes (`xargs -a P`, `--arg-file=P`). Here `~` and `$HOME` count even inside quotes, because
  the program reads the text itself. A `host:path` remote isn't a local path.
- Wrappers: `sudo`, `doas`, `env`, `xargs`, `timeout`, `nice`, `nohup`, `time` and the like are taken off the front. The
  text that `env -S`, `su -c`, `runuser -c`, `flock -c`, `script -c` and `watch` give to a shell is read as a command
  line of its own.

The reader is bounded. A command may expand to at most 4,096 words in all, including the commands inside it, and
braces may nest 64 deep. A variable that `%`, `#`, `/` or `:` works on may hold at most 4,096 characters, the variables
in one command line may add at most a million characters, a `for` list at most 64 different words (a run of numbers
counts as three), and reading one command line may take one second. A path may have at most 256 parts, and one command
line may name at most 8,192 paths; a word longer than 4,096 characters is matched as text and isn't looked up on disk.
Shells, substitutions and `eval` may nest four deep. A command over a limit, one that uses an expansion the reader
doesn't follow, and one that ends inside a quote, is refused with a reason that says so; it's never let through unread.
The expansions it doesn't follow are a pattern with a variable, a command, a negated or named bracket class
(`[!x]`, `[^x]`, `[[:alpha:]]`) or an extglob group (`@(x)`, `!(x)`), an anchored (`${NAME/%x}`) or globbed replacement,
a replacement that reuses the match (`&`), a case change with a pattern, an arithmetic offset such as `${NAME:0:(-4)}`,
`${NAME@P}`, `${!NAME}` for a name the line doesn't set, and a `su` or `script` with no `-c` text to read.
`tests/test_shellwords_oracle.py` compares the reader with bash on a fixed set of spellings.

A here-document or here-string is text for whatever reads it. When that's a shell (`bash`, `sh`, `zsh`, `dash`, `ksh`,
`fish`), `source`, `.`, `eval`, `ssh` or `xargs`, the text runs as code, so the command is refused with the reason
`stdin-is-code`. Any other body, a commit message included, is read as commands, whatever its quoting.

### The sandbox is the boundary

The sandbox is the boundary, and the static gate is defence in depth. The gate reads the text of a command and refuses
what it can show is wrong, with a reason a person can act on. The sandbox acts on what a program does, whatever its
command line says: ws's own files are read-only and the `deny_read` paths read as empty for every program a shell
command starts. It's on by default (`sandbox = true`), and it's the layer a bypass of the gate meets.

The gate's limits are the kinds of text it can't see into. The first four are the `LIMITS` rows in
`tests/test_gate_oracle.py`:

- An interpreter, and the program it runs from `-c`, a file or standard input.
- An `awk` program.
- A script the line writes and then runs.
- An option whose value is a command the tool runs (ssh's `ProxyCommand`).
- A launcher that starts an interpreter. The gate refuses a launcher that starts a shell, but what an interpreter does
  inside its own program is the same limit as the first.

Only the sandbox closes those, so they're closed only in a session that has one.

### When the sandbox isn't up

The sandbox block is the wall for shell commands. The gate reads the same text with it up or not: allow-with-log, where
a refusal for a path the reader can't resolve becomes a logged allow inside a wall, isn't in 0.1.8. A command refused
for a part ws can't read stays refused.

`ws doctor` (WS012) reads the settings and says which of these holds. `ok` needs the sandbox block in your settings
file, its `denyRead` list covering every `deny_read` path the wall can hide (a stale list says to run `ws install`), and
nothing in your settings or a managed-settings file that widens it. Otherwise it says which, and for an unsandboxed
session it suggests running under `ws gate launch` for fewer refusals. Linux and WSL2 need bubblewrap and socat
(`sudo apt install bubblewrap socat`, or `sudo dnf install bubblewrap socat`). macOS has its own, and native Windows has
none.

The static gate reads shell, so it's a check on shell and not a wall against interpreters. A program that `python`,
`python3`, `perl`, `node`, `ruby`, `php`, `lua` or `Rscript` runs (from `-c`, a file, or standard input), `awk` code,
an option whose value is a command the tool runs (such as ssh's `ProxyCommand`), and a script the line writes and then
runs are text the gate can't see into: it's the same for a here-document or here-string given to one of them. These
are the known limits, and `tests/test_gate_oracle.py` lists each as a `LIMIT` row. The wall is the sandbox:
`ws gate launch --runner R -- argv` starts the runner inside it, and the files and folders ws protects are out of reach
of every program in the session, whatever it runs.

## `[guards]`

Named checks that run beside the `[gate]` rules. The [guards reference](guards.md) describes each one.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `on` | list of text | `[]` | The guards that run, in order. A name ws doesn't know stops the load. `ws gate init` writes `["syntax-check", "session-context", "dev-server"]`. |
| `[guards.syntax-check]` `extra` | table of extension to command | `{}` | A checker for an extension, such as `{ yaml = ["yamllint", "-d", "relaxed"] }`. The file path is added as the last argument. |
| `[guards.session-context]` `files` | list of text | `["STATE.md", ".state/STATE.md", "HANDOFF.md", "TODO.md"]` | Project-relative files to list at session start, when they exist. |
| `[guards.dev-server]` `allow_hosts` | list of text | `[]` | Hosts and addresses a dev server may bind besides loopback. |
| `[guards.checkout-pin]` `paths` | list of text | `[]` | Checkouts to pin besides those with linked worktrees. Absolute or `~/`. |

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

Whatever the policy says, the gate denies a session writing every one of ws's four folders, and these control paths in
them: `profile.toml`, `policy.toml`, `secrets.toml`, `jobs`, `release`, `runs`, `logs`, `sessions`, `approvals`,
`install-manifest.json` and `ratings.jsonl`. It also protects the policy file in use and any path passed with
`--protect` (the generated folder, for example). Reading `logs/runs.jsonl` stays allowed.

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
deny_read = ["~/.ssh/**", "~/.gnupg/**", "~/.aws/credentials", "~/.config/ws/secrets/**", "~/.local/share/ws/secrets/**"]
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
