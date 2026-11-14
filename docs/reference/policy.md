# Policy reference

`policy.toml` is the one vendor-neutral file that says what a session may do. It lives at `~/.config/ws/policy.toml`
(`policy.toml` in the [config folder](stores.md#where-ws-keeps-its-files)). `ws gate init` writes the commented default.
With no file, ws uses the built-in default. A different file can be named with `--policy FILE` on the `ws gate`
commands.

The file is loaded whole or refused. An unknown key, a wrong type or a bad value stops the load, and the message names the
key. The gate then denies every call (it fails closed). The schema is `1`.

The tables `[classes]`, `[budget.purposes]` and `[autonomy]`, and the `owner` and `expires` keys of `[[exceptions]]`, are
newer than the rest. A policy that leaves them out loads exactly as before. An older ws refuses a policy that uses them,
because it doesn't know the keys: that's the intended fail-closed behaviour, so update ws before you add them.

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
| `deny_tools` | list of text | `[]` | Tools to switch off: the neutral names `web_fetch` and `web_search` (an allowed web tool counts as a way out), and MCP tools by name or glob (`mcp__server__tool`, `mcp__server__*`, `mcp__*__delete_*`). |
| `ask_commands` | list of text | `[]` | Command patterns that run only after you approve that exact call. Same globs as `deny_commands`. A command that also matches `deny_commands` stays denied. |
| `allow_mcp` | list of text | the tools `ws mcp serve` offers | MCP tools a session may call without being asked, by name or glob (`mcp__ws__*`). An MCP tool this list doesn't name is asked for: a person approves that exact call, once, as for `ask_commands`. `deny_tools` beats it. Leave the key out and only `mcp__ws__memory_search`, `mcp__ws__memory_add` and `mcp__ws__note_capture` are allowed; naming it, even as `[]`, replaces them. |
| `allow_hosts` | list of text | `[]` | Hosts a session's shell may reach without asking, for runners whose sandbox filters the network (Claude Code). Empty means the policy names none, and the sandbox block carries a short default list instead (the runner's API host, PyPI, npm and crates.io, with GitHub and GitLab added for a session started with `ws gate launch` but not for a headless job; see [Use the wall](../how-to/use-the-wall.md)). A list here replaces the default. The default is in the settings ws starts a session with, not in your own `~/.claude/settings.json`. With `sandbox = false` there is no list at all. |
| `allow_write` | list of text | `[]` | Folders besides the project that a walled session may write. |
| `sandbox` | true or false | `true` | Switch on the runner's own sandbox in what ws writes (Claude Code), both for a session started with `ws gate launch` and for one started the usual way once `ws install` has merged the settings into your file. `false` leaves the block out and marks the session unsandboxed, and `ws doctor` (WS012) says so. |
| `read_outside_project` | true or false | `false` | The off switch for the read block. With `false`, the settings ws passes to Claude Code (`ws gate launch`, `ws gate generate`, headless `ws run` jobs) set `permissions.blockReadsOutsideWorkingDirectories` to `true`, so Claude's Read, Grep and Glob tools refuse a path outside the working folders in every permission mode (Claude Code 2.1.257 or newer; WS016 checks the version). `true` leaves the key out. It doesn't touch your own settings file, which ws never gives this key, and a class's launch file always carries it. A value that isn't `true` or `false` makes the whole policy fail to load. |
| `write_outside_project` | true or false | `false` | The off switch for the write block. With `false`, the settings ws passes to Claude Code set `permissions.blockWritesOutsideWorkingDirectories` to `true`, so Claude's Write, Edit and NotebookEdit tools refuse a path outside the working folders in every permission mode, bypass too (Claude Code 2.1.314 or newer). It doesn't cover shell commands (the sandbox does), MCP tools or hooks. `true` leaves the key out. It doesn't touch your own settings file. A value that isn't `true` or `false` makes the whole policy fail to load. |
| `allow_read` | list of text | `[]` | Folders or files besides the project that a shell command in a session started with `ws gate launch --runner claude-code` may read. That session can't read your home folder except the project, its git folders and what common tools need; see [Deny the home folder](../how-to/use-the-wall.md#deny-the-home-folder). A `deny_read` path inside one of these stays unreadable. Only that launch reads it: `ws install` doesn't merge it into your own settings file, and OpenCode's wall hides the `deny_read` paths only. |

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
- `allow_write` items must be a folder, absolute or under `~/` (`$HOME` is accepted), with no wildcard, not `/`, and
  neither the home folder nor a folder above it, however it's spelt (an absolute path to the home too), after `.` and
  `..` are resolved. A policy with such an entry doesn't load, and the error names it.
- `allow_read` items must be a folder or file, absolute or under `~/` (`$HOME` is accepted), with no wildcard, not `/`,
  not the home folder or a folder above it (an absolute spelling, or one that `.` and `..` bring there, counts), and
  not equal to or inside a `deny_read` path (a path a
  `deny_read` rule names without a wildcard, with or without a trailing `**`). One bad item refuses the whole policy and
  names the item. A folder above a `deny_read` path is fine: the denied path stays hidden. An older ws refuses a policy
  with this key as an unknown key.
- `deny_tools` items must be a known tool name or an MCP tool name or glob. `allow_mcp` items must be an MCP tool name
  or glob. An MCP name is `mcp__`, then letters, digits and `_ - . * ?`, with no empty part (`mcp__`, `mcp____y` and a
  trailing `__` are refused). A malformed item refuses the whole policy and names the key. A glob matches the full
  tool name; `*` matches anything, including `__`.
- An MCP call's path-like arguments go through `deny_read` (a listing or search tool through the folder rule, so a
  folder above a denied one is refused) and, for a tool whose name says it writes (write, edit, create, delete, remove,
  move, rename, save, append, patch, mkdir), through the protected-write check. Path-like means the value of a key
  that names a path (`path`, `file`, `filename`, `dir`, `directory`, `root`, `source`, `destination`, `target`, `uri`,
  and a key that ends in one of those words), a string that starts with `/`, `~`, `./` or `../`, and a `file://`
  address, found in nested lists and maps. Arguments nested more than 8 levels or holding more than 2000 values are a
  deny.

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
| `[guards.NAME]` `mode` | text | `"enforce"` | `enforce` (refuse as always) or `log` (a refusal becomes a count in the verdict log, only where the OS wall is proven for the call; elsewhere it still refuses). Any other value stops the load. See [the guards reference](guards.md). |
| `[guards.syntax-check]` `extra` | table of extension to command | `{}` | A checker for an extension, such as `{ yaml = ["yamllint", "-d", "relaxed"] }`. The file path is added as the last argument. |
| `[guards.session-context]` `files` | list of text | `["STATE.md", ".state/STATE.md", "HANDOFF.md", "TODO.md"]` | Project-relative files to list at session start, when they exist. |
| `[guards.dev-server]` `allow_hosts` | list of text | `[]` | Hosts and addresses a dev server may bind besides loopback. |
| `[guards.checkout-pin]` `paths` | list of text | `[]` | Checkouts to pin besides those with linked worktrees. Absolute or `~/`. |

A file with no `[guards]` table has no guards on. `bulk-staging` and `record-calls` take no table.

## `[classes.NAME]`

Data classes: which of your repos hold what kind of content, and what a session working in them may do and see. `NAME`
uses letters, digits, `_` and `-` (`unclassified` is taken). A policy with no `[classes]` table has no classes, and
everything behaves as it did before. Every table is checked whole; one bad key refuses the file.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `access` | text | required | `full`, `walled` or `counts-only`. |
| `repos` | list of text | required | Path globs, in the same syntax as `deny_read` (`~/` is your home, `**` crosses folders, a bare name matches at any depth). At least one. A rule that names a folder covers everything under it. |
| `os_user` | text | none | The POSIX user name sessions of this class must run as. A class with one is a walled class with its own settings file: see [Set up a class user](../how-to/set-up-a-class-user.md). |
| `allowed_providers` | list of plain names | not set | The model providers a session of this class may use. Left out, the file says nothing and the consumer applies its own default (a walled class gets the strictest); `[]` allows none. |
| `local_only` | true or false | `false` | Only a model that runs on this machine. |
| `allowed_hosts` | list of text | `[]` | Hosts the class's sessions may reach. The same syntax as `[gate]` `allow_hosts`. |
| `readable_by` | list of text | `[]` | The classes whose sessions may read this class's content, or `["*"]` for all. `[]` is nobody. Every name must be a class in the file. |
| `inbound` | text | `off` for `walled` and `counts-only`, `lead-only` for `full` | Who may send a message in: `off`, `lead-only` or `allowlist`. This is the class's inbound setting (a schedule's `inbound_from` is this pair, not a second key): a walled class stays `off`, so nobody can send in, until you change it. A walled class takes a message only with `allowlist` and the sender's session id in `inbound_allow`; `lead-only` is refused for it too. Each message to it, delivered or refused, leaves one row in the class's own message store: see [Messages](messages.md). |
| `inbound_allow` | list of plain names | `[]` | Who may send in. Required and non-empty when `inbound = "allowlist"`, and refused otherwise. |
| `owner` | non-empty text | none | Who answers for the class. `ws walls check` wants it on a class with an `os_user`. |
| `review_date` | TOML date | none | When the class is looked at again, such as `2027-03-01`. A text or a date with a time is refused. From that day, in UTC, `ws doctor` (WS017) warns and `ws walls check` fails the owner-and-review-date case. |
| `repo_settings` | table of tables | none | One table per repo glob of the class (`[classes.NAME.repo_settings."GLOB"]`) that adds denials to the class's launch file for a session started in that repo: `deny` (permission rules such as `"Bash(curl *)"`), `deny_read` and `deny_write` (paths). It can only narrow the launch file. Any other key (an allow, a host, a bypass setting) would widen it and refuses the whole file, naming the key. The glob must be one of the class's `repos`, and the class must name an `os_user`: a table on a class with none is refused at load, because only a walled class has a launch file. |
| `retention_days` | integer, 1 to 3650 | `30` | How long the class's messages are kept. A true or false is refused. This is separate from the 30 days ws keeps a session record. |

```toml
[classes.alpha]
access = "walled"
os_user = "ws-alpha"
repos = ["/srv/work/alpha/**"]
owner = "Your name"
review_date = 2027-03-01

[classes.alpha.repo_settings."/srv/work/alpha/**"]
deny = ["Bash(curl *)"]
deny_read = ["/srv/work/alpha/secrets"]
```

`ws walls init NAME` prints a table like this, and `ws walls init --write NAME` appends it to your policy: a backup first,
the whole file checked afterwards, then an atomic replace. It writes the `prepare` dial and never `act`.

`ws walls check` runs one named case for each part of a walled class's boundary and exits 1 when one fails; a case that
needs a class's file in place is skipped until there is one. Each case names the setting it depends on (`--json` has it),
so removing that setting makes that case fail.

| Case | Depends on | Needs a class file |
|---|---|---|
| `inbound-refused` | `crossSessionInbound` in the class file, `inbound` under `[classes.NAME]` | yes |
| `sealed-read-refused` | the `Read` and `Edit` denials of the sealed paths | yes |
| `class-file-not-seen-by-other-class` | the launch's class lookup, `denyRead` of each launch file | yes |
| `hooks-under-bypass`, `hooks-under-auto` | the gate hook on every tool, and bypass switched off | yes |
| `project-load-check`, `hooks-not-disabled`, `sandbox-only-narrows` | the class marker, and the keys a project may not set | yes |
| `per-repo-settings` | `repo_settings` only narrows | no |
| `otel-off` | the OpenTelemetry content logs off | yes |
| `owner-and-review-date` | `owner` and `review_date` (the same judge as `ws doctor` WS017) | no |
| `messaging-denied` | `SendMessage` and `ListAgents` denied | yes |
| `class-file-root-owned` | the class file's owner and mode | yes |
| `allowed-sender-delivered` | `inbound = "allowlist"` and `inbound_allow`: a listed sender is delivered, any other is refused, and each leaves a log row | no |
| `bodies-in-class-store` | the class store under the class's repo; the shared store 0700 and 0600, owned by the account, with no body or hash in it | no |
| `walled-sends-fixed-kinds` | the fixed kinds a walled session may send, with no body, key or thread | no |
| `provider-allowlist` | `allowed_providers`, `allowed_hosts` and `local_only`: the launch file's hosts are the class's own and its named providers' | yes |
| `class-push-refused` | `strictAllowlist` and `allowedDomains` in the launch file: a git push to a host outside the class's list (its remote list) is refused | yes |
| `memory-notes-refused` | a walled session can't add memory or capture a note, and a search hides another class's files unless `readable_by` allows | no |
| `walled-card-no-counts` | a walled message's envelope row (what `ws msg watch` and the viewer show) has no count field | no |
| `tracer-stays-in-class` | a value planted in a walled class reaches no shared message store, memory or notes | no |

Which class is a folder in (`class_of(policy, path)` in `src/ws/policy.py`):

- With no `[classes]` table the answer is none, which means "no classes configured."
- ws looks only at where the path really is, with every symbolic link resolved, so a link into a walled repo counts as
  that repo whatever class the link itself sits in. A link out of a walled repo to a folder in no class is `unclassified`.
  It matches without regard to case where the file system ignores it (macOS).
- If several classes match, the most specific wins: the longest literal folder before the first wildcard. A tie goes to
  the stricter access (`counts-only`, then `walled`, then `full`), then to the first name in alphabetical order. The
  file's order never matters.
- If none matches, the folder is `unclassified`: `counts-only`, nothing inbound, readable by nobody, no hosts. A path
  ws can't read as a path is `unclassified` too.

## `[budget.purposes]`

A monthly cap in US dollars for each purpose, such as `chat = 20`. A purpose name uses letters, digits, `_` and `-`; a cap
is a finite number of 0 or more (a true or false is refused). A month is a UTC calendar month, from 00:00Z on the 1st to
the end of the last day, wherever you are (`budget_month(now)` in `src/ws/policy.py` is that window). A policy with no
table has no purpose caps.

## `[autonomy]`

How much a session decides on its own. It's separate from the [`[interrupts]`](#interrupts) counts, which cap how often ws
itself sends you a digest or a push.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `level` | text | `"decide-and-tell"` | `ask`, `decide-and-tell` or `autonomous`. |
| `interrupts` | text | `"when-broken"` | When a session may interrupt you: `never`, `when-broken` or `daily-summary`. |
| `always_ask` | list of plain words | `[]` | Things that are asked first at every level, such as `spending` or `data-deletion`. |

## `[[exceptions]]`

A named Rule of Two exception. The first four keys are required; `owner` and `expires` are optional.

| Key | Type | Meaning |
|---|---|---|
| `name` | text | A unique name. The run record shows `exception:NAME`. |
| `job` | text | The job the exception covers. |
| `sources` | list of text | The sources it covers. At least one. Each must be defined under `[sources]`. |
| `reason` | text | Why. |
| `owner` | text | Who answers for it. |
| `expires` | TOML date or offset date-time | When it stops covering. A date such as `2026-12-31` ends at 00:00Z that day; an offset date-time such as `2026-12-31T09:00:00+13:00` ends at that instant. A local date-time (no offset), a time or text is refused, since ws never assumes a timezone. |

The exception applies only when the job name matches and every source the session reads is in `sources`, and it hasn't
expired. An expired exception covers nothing, and the refusal says when it ended.

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

A policy with data classes, purpose caps, an autonomy setting and an exception that lapses:

```toml
schema = 1
dial = "prepare"

[sources.mail]
untrusted = true

[sources.notes]
sensitive = true

[classes.private]
access = "walled"
repos = ["~/code/private/**"]
allowed_providers = ["anthropic"]
allowed_hosts = ["api.anthropic.com"]
readable_by = []
retention_days = 14

[classes.open]
access = "full"
repos = ["~/code/**"]
readable_by = ["*"]

[budget.purposes]
chat = 20
research = 40.5

[autonomy]
level = "decide-and-tell"
interrupts = "when-broken"
always_ask = ["spending", "data-deletion"]

[[exceptions]]
name = "morning-digest"
job = "digest"
sources = ["mail", "notes"]
reason = "reads mail, writes only to me"
owner = "me"
expires = 2027-03-31
```

Here `~/code/private/x` is `private` (the longer folder beats `~/code/**`), `~/code/other` is `open`, and a folder
outside `~/code` is `unclassified`.
