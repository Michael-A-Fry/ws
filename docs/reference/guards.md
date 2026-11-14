# Guards reference

A guard is a small named check that the policy switches on. Guards run beside the `[gate]` rules, at the points in a
session where ws can look: before a tool call, after one, when the session starts, when a prompt is sent and when the
session or a subagent stops. For the idea, read [The gate](../explanation/gate.md). To turn one on, follow
[Use the gate](../how-to/use-the-gate.md).

## Turn guards on

```toml
[guards]
on = ["syntax-check", "session-context", "dev-server"]     # the guards that run, in this order

[guards.syntax-check]
extra = { yaml = ["yamllint", "-d", "relaxed"] }

[guards.session-context]
files = ["STATE.md", ".state/STATE.md", "HANDOFF.md", "TODO.md"]   # the default
```

A policy file with no `[guards]` table has no guards on, so an upgrade changes nothing until you add the table.
`ws gate init` writes `on = ["syntax-check", "session-context", "dev-server"]` and lists the other guards as comments. A
name in `on` that ws doesn't know stops the load and the message names it. So does a key in `[guards]` that isn't `on`
or a guard name, and a key in a guard's table that guard doesn't read (`mode` is read by ws for every guard, below).

## Modes: Enforce and log

Every guard's table takes one key of ws's own, `mode`:

```toml
[guards.claims]
mode = "log"        # "enforce" (the default) or "log"
```

Any other value, for any guard (on or not), stops the load and the message names the key. In `enforce` a guard refuses
as it always did. In `log` a guard that would refuse a call lets it through and writes a count to the verdict log
(`logs/gate.jsonl`, see [Stores](stores.md)), but **only where the OS wall is proven for that call**. Everywhere else
the guard refuses exactly as in `enforce`, and its entry in the log line carries the reason as one word in `gap`.

The wall is proven for a call only when all of these hold, and a project can set none of them:

| Reason word | What failed |
|---|---|
| `native-windows` | The machine is native Windows, which has no sandbox. Linux, WSL2 and macOS can. |
| `no-sandbox-binary` | `bwrap` isn't on `PATH` (Linux, WSL2), or `sandbox-exec` isn't (macOS). Nothing is run to find out. |
| `sandbox-off` | The session's environment doesn't carry ws's marker `WS_SANDBOXED=1`. ws's own settings set it; `0` marks settings written without the sandbox. |
| `preflight` | The runner's adapter has no preflight, or its preflight refused the call, or a project settings file (in the project folder or any folder above it) puts `WS_SANDBOXED`, ws's gated-session marker or `PATH` in the session's `env`. A project that says the wall is there isn't the wall. |
| `unsandboxed-call` | The call is marked to run outside the sandbox (`dangerouslyDisableSandbox`). |

When the wall is proven, a deny in `log` mode is yielded as a note and the verdict is `would-deny` with a `count`. An
approvable deny asks for no approval and consumes none. A guard in `log` gives the runner's own deny list none of its
`NATIVE_DENY` patterns (the runner would refuse where the guard logs), so with the wall not proven the hook refuses it.
`ws doctor` (WS018) warns when a guard asks for `log` on a machine where the wall can't apply.

**Refusals the wall makes redundant** follow the same proof, for shell commands only (the sandbox wraps the shell, not
the file tools): a program word that holds a part ws can't read (`unknown-command-word`), a line that sets `IFS`
(`sets-ifs`) and a write to a path ws can't read, such as a scratch file `mktemp` made in the project
(`unread-write`). Where the wall is proven these are allowed and logged as `rule:unknown-command-word`,
`rule:sets-ifs` and `rule:unread-write`; the rest of the line is judged as before, so a `deny_commands` match, a
`deny_read` path or a protected write elsewhere in the line is still a deny. A write to a path that spells a protected
name after a part ws can't read, and a line that sets `IFS` where an expansion outside quotes could split on it, stay
denies. So does a program word ws can't read where the policy sets any `deny_commands` or `ask_commands` rule, or a
guard that judges shell commands (`bulk-staging`, `checkout-pin`, `dev-server` and the like) is on in `enforce`: those
match the program's name, which the wall doesn't enforce, and nothing can match a word ws can't read. With the
template's `dev-server` in `enforce`, that refusal stays a deny until `dev-server` is set to `log` as well. Every
other refusal is a deny in every mode. This needs no `mode`: the proof of the wall is the whole condition. The template
ships `syntax-check` in `log`, and the commented tables for `claims` and `test-weakening` carry `mode = "log"`.

## The guards

| Guard | Runs | Reads | What it does |
|---|---|---|---|
| `bulk-staging` | before a call | shell commands | Stops `git add -A`, `--all` (and a prefix such as `--al`), `.`, `./`, `*`, `:/` and `:(top)`, the same with `git stage`, and `git commit -a`, `-am` and `--all`, with or without git's global options. Stops `git -c alias.NAME=...`, which can define a command that stages everything. `git add file` and `git add -- file` go through. Text in quotes, comments and here-document bodies is data, so `git commit -m "git add -A"` goes through; the text after `eval` and in `bash -c` is a command and is read. The runner's own deny rules carry every form a pattern can say (`add -A*`, `add .`, `add -- .`, `add -u`, `commit -a*` and the rest of the whole-tree names, also behind git's global options); a short-option cluster that holds the letter after others, an option after `-u` or after the message, a literal star, a substitution, `:(top)` and the `GIT_CONFIG_*` variables stay with the hook. Not carried natively by a guard in `log` mode. |
| `syntax-check` | after a call | file writes | Checks the file the session wrote and tells the model the line that doesn't parse. Python, JSON and TOML are read in process. `.sh` and `.bash` go through `sh -n` or `bash -n`, with a note when the machine has no shell. `.js`, `.mjs` and `.cjs` go through `node --check`, with a note when node isn't installed. |
| `session-context` | at session start | none | Lists, one line each, which of `files` exist in the project: the git top level above the session's folder, and the main worktree when the session runs in a linked one. Adds a line when an API key bills an interactive session, and once for each ws version in each project, the version and what changed. |
| `test-weakening` | before a call | file writes | Applies the edit to the test file on disk and compares before and after. Denies a skip or expected-failure marker added to a test that already exists; the person can approve it once. Warns about a test removed, fewer assertion lines, or an always-true assertion added. Judges Edit, Write and MultiEdit calls only. |
| `claims` | before a message, and when the session stops | messages to other agents | Checks each commit-like token that sits in the same sentence as a claim word (`on B`, `merged into B`, `merged to B`, `pushed to B`, where B is the repository's default branch) against a ref, and denies the message when the commit isn't on it. With `release_dir` set, also denies a message that calls a release other than the promoted one live. On stop, logs the claims in the runner's last message. |
| `spawn-cap` | before a call, and when a subagent stops | subagent starts | Denies a subagent start when `max_agents` (default from the machine) are already running in the session. Counts agents started in the background. |
| `skill-gate` | before a call, and when a prompt is sent | file writes and skill calls | Denies a write to a guarded path until the named skill has run in the session. The paths come from the policy's rules and from each skill's own `gates` front matter. The person can approve the write once. Judges Edit, Write and MultiEdit calls only. |
| `dev-server` | before a call | shell commands | Denies a development server that listens on every interface or on an address other than loopback: `python -m http.server` with no `--bind`, `php -S 0.0.0.0:PORT`, `serve` with no `-l`, `vite --host`, `flask run --host 0.0.0.0`, `jupyter lab --ip 0.0.0.0`, `rails s -b 0.0.0.0`, `uvicorn --host 0.0.0.0`, `hugo server --bind 0.0.0.0`, `http-server` with no `-a`, `manage.py runserver 0.0.0.0:PORT`, `next dev -H 0.0.0.0`, `streamlit run`, `gunicorn -b 0.0.0.0`, `webpack serve --host 0.0.0.0` and `astro dev --host`. The reason names the flag to add. The person can approve the command once. On by default in the policy `ws gate init` writes. |
| `checkout-pin` | before a call | shell commands | Denies `git checkout` or `switch` to another branch or commit, `reset --hard`, `--merge` and `--keep`, `rebase`, `merge`, `pull`, `cherry-pick`, `revert`, `am`, `bisect`, `stash pop` and `apply`, a whole-tree restore from another ref and `worktree move` into a checkout that has linked worktrees, or that `paths` names, whichever way the checkout is reached (a folder, `-C`, `--git-dir`, `GIT_DIR`, a `cd`). `git checkout -- FILE`, a restore of files, `commit`, `fetch`, `status`, `log` and a branch switch inside a linked worktree go through. The person can approve the command once. Every form depends on the folder, so none is in the runner's own deny list: `git checkout topic` is the normal way to work in a linked worktree. Off until you name it. |
| `inbox` | when a prompt is sent, after a call, and when Claude Code stops | none | Hands the session the messages left for it, as text marked untrusted, at its own turn: a prompt's output, a warning after a tool call, and (for an `urgent` message) a `{"decision":"block"}` that keeps a stopping Claude Code session going, at most three stops in a row and never for an `info` message. A body is cut to 4000 characters, cleaned for a terminal and flagged when it reads like an instruction; the flags only mark. A walled receiver, a caller that can't be shown to be the session, and a store the wall leaves read-only get a body-free "N unread" line instead (after a call, at most once in two minutes). A message grants no permission: the gate judges every later call as before. Off until you name it. |
| `presence` | before and after a call, at session start, on a prompt and on a stop | none | Records `busy` or `idle` in the session registry from those hooks, newest report winning, and the latest message the session sent that was delivered. A hook of a sub-agent is skipped. It never says anything and never fails a hook. |
| `pane-close` | before a call | shell commands | Allows `tmux kill-window` or `kill-pane` on the live tmux server only as one plain command with an `@N` or `%N` target, when the registry says every session in it's idle and the caller is the lead, or it's the caller's own pane after its hand-over was delivered. See [pane-close](#pane-close). |
| `record-calls` | before a call | every kind | Appends `{schema, ts, at, runner, payload}` (`ts` is the time; `at` is its legacy spelling, kept beside it) to `~/.local/state/ws/logs/gate-calls.jsonl`, one line for each hook run. A file write keeps its path and records the text as `{"bytes", "sha256"}`. `ws gate replay` reads that file. |

`bulk-staging`, `record-calls`, `inbox` and `presence` take no table. The other guards take one.

`test-weakening` and `skill-gate` judge Edit, Write and MultiEdit calls. A change made through the shell, such as
`sed -i` or a redirect, or through a notebook edit, isn't seen by them. The sandbox is what stops a write; these guards
tell the model what it's about to break.

The per-session records under `~/.local/state/ws/sessions` (`skill-gate`'s record of skills run, `spawn-cap`'s count) are
advice-level. A session that can write that folder can change them, so they keep an honest session honest and don't stop
a hostile one. The sandbox is the wall.

`syntax-check` takes `extra`, a table of lower-case extension to checker command. ws adds the file path as the last
argument and gives the checker 10 seconds. An exit code other than 0 is a finding that quotes the checker's first lines.
A checker that isn't installed, or runs out of time, is a note. An entry in `extra` replaces the built-in check for that
extension.

`session-context` takes `files`, a list of paths relative to the project. The default is the state, handoff and to-do
files most projects and runners use: `STATE.md`, `.state/STATE.md`, `HANDOFF.md` and `TODO.md`. A path must stay inside
the project: no leading `/` or `~`, no `..`. It also takes `warn_api_key`; see [session-context](#session-context).

`record-calls` is off unless you name it. A line holds the hook payload as the runner sent it, with one change: the text
a write tool carries (Write `content`, Edit `old_string` and `new_string`, each MultiEdit edit's two strings, NotebookEdit
`new_source`) becomes its size in bytes and a SHA-256, and the path stays. Replay judges a call by its tool, path and
command, so it runs a line as it is. A command is kept as the session typed it, so a value typed into a command is in
the log. The log has the bounds of every ws log: a long field is cut and marked, the file rotates, and
writers take a lock. The `logs` folder is one a session can't write.

### test-weakening

```toml
[guards.test-weakening]
paths = ["tests/**"]               # optional: replaces the default list of test files
except = ["tests/fixtures/**"]     # optional: replaces the default list of files that aren't tests
```

A test file is one under `tests/`, `test/` or `__tests__/`, or named `test_*.py`, `*_test.*`, `*.test.js`, `*.test.jsx`,
`*.test.ts`, `*.test.tsx` or `test-*.R`. Files under `fixtures/`, `testdata/`, `snapshots/` and `golden/` aren't. A
pattern with no `/` matches at any depth, and `**` crosses folders.

The guard builds the file's text after the write (Edit, Write and MultiEdit; not a notebook) and reads both texts by
line. A test is a `def test...` with its decorators, an `it(` or `test(` with a title, `test_that("...")`, a Go
`func TestX(`, a Rust `fn test_x(` or a Java method after `@Test`. Skip markers are counted for each test, so a new test
or a new file can carry one. A marker added to a class, or to the file as a whole, in a file that already had tests is
also a deny. A plain `describe(` block isn't a marker; `xdescribe(` and `describe.skip` are. The deny is approvable: the
reason names `ws approve`, and the person can allow the edit once. A write it can't apply, such as a file that
doesn't exist yet or an edit whose old text isn't in the file, isn't judged.

`ws gate replay` judges a line only when the line holds the text of the edit. A line from `record-calls` keeps a
fingerprint of that text instead, so write the payload by hand to replay this guard.

### claims

```toml
[guards.claims]
words = ["on trunk", "merged into trunk", "merged to trunk", "pushed to trunk"]   # optional
ref = "origin/trunk"                       # optional
release_dir = "~/releases"                 # optional: release claims are off without it
release_tags = ["v[0-9]+[.][0-9]+([.][0-9]+)*"]   # optional: the shapes of a release name
```

A claim is a phrase from `words` (whole words, any case) and the commit-like token it's about: 7 to 40 lower-case hex
characters with at least one digit and one of `a` to `f`. A token belongs to a phrase when it sits inside it (`merged
abc1234 into main`) or beside it with nothing between but a few plain function words and other commit ids: `abc1234 is
on main`, `pushed the fix abc1234 to main`, `merged into main: abc1234 and def5678`. Any other word between them means
the sentence is about something else, so the guard doesn't check it: `the ed25519 key is on main`, `session 9f8e7d6c is
on main`, `abc1234 is a draft and the fix is on main`, `abc1234 differs from released main`, and `pushed p8-item1a
abc1234` (a branch name between, so it isn't a claim about the default branch) all go through. Dates, run numbers and
ids with hyphens (`20261107-151146`) have no hex letter or are cut at the hyphen, so they never count as commits. The
default phrases are about the repository's default branch only, because the check is
against one ref. ws reads that branch from `git symbolic-ref refs/remotes/origin/HEAD`. A repository with no
`origin/HEAD` uses its local default branch: `init.defaultBranch`, else `main`. That branch's `origin/` copy is the ref
when it exists, and the branch itself when it doesn't. Setting `words` or `ref` replaces that part of the default. The
guard runs `git merge-base --is-ancestor TOKEN REF` in the session's folder. A commit that isn't an ancestor of `ref`, or
isn't in the repository, denies the message and the reason names it. If `ref` may be out of date, the reason says to run
`git fetch` first; the guard never fetches. A claim word in quotes or backticks isn't a claim, and neither is one with a
negation or a future form in the four words before it (`not merged`, `will be pushed`, `once it's merged`). A sentence
that names another branch, such as `origin/work` or `pushed to work`, isn't checked either. A folder that isn't a
repository, a machine with no git and a `ref` that doesn't exist there each let the message through with a note: there
is nothing to check against. Git that's there but doesn't answer is a deny, because the gate fails closed
([ADR 0017](../decisions/0017-the-gate-fails-closed.md)): git taking more than 8 seconds in total for one message, or
failing to start, denies a message that makes a claim, and the reason names the time-out. If the time ran out before
the default branch was known, any `on B`, `merged into B`, `merged to B` or `pushed to B` that a commit binds to counts
as a claim. A message with no claim, and any of the true negatives above, is never held up by a slow git. Git runs with
`GIT_NO_LAZY_FETCH=1`, so a check never goes to the network.

With `release_dir` set, the guard also checks release claims. `release_dir` is a folder whose `current` link points at
the promoted release tree; the path is absolute or starts with `~/`. A clause says a release is live when it reads
`v0.2 is live`, `v0.2 is now live`, `promoted v0.2 to live`, `live now: v0.2`, `live: v0.2` or `the live release is
v0.2`. A clause ends at a full stop, a new line, `;` or `but`, `while`, `whereas` and `although`, and at a comma that
starts a new clause such as `v0.3 is staged, v0.2 is live`. A release is a name that matches one of `release_tags`
(regular expressions; the default reads `v0.2` and `v0.2.1`). A pattern must be simple: the policy refuses one that can
match nothing (`v?`, `\b`), is over 200 characters, or repeats a group that only holds repeats, such as `(\d+)+` (that
shape can take minutes on a long message). A fixed part inside the group, as in `([.][0-9]+)*`, is fine. The clause may
name only the release that `current` points at: its target folder's name, with or without a leading `v`. A clause that
names another release is denied, and the reason names `current`'s target. A live claim is also denied when `current` is
missing, is a folder or file rather than a link, points at nothing or can't be read. A clause with no release name
isn't checked. Quotes and backticks, negation (`v0.3 isn't live`) and the future (`goes live after the next promote`,
`will be live`, `once v0.3 is live`) make a clause not a claim, as they do for commits, and so does a word joined to
`live` by a hyphen (`v0.3 is live-tested`). The guard reads the link and
nothing else: it doesn't fetch, use the network or write a file. Commit claims and release claims in one message are
judged together and the reason names both.

When the session stops, the claims in the runner's last message (the stop payload's `last_assistant_message`, when the
runner sends one) are appended to `~/.local/state/ws/logs/claims.jsonl` with whether each commit was on `ref`. Each line
has `schema`, `ts` and `at`, like the other logs listed in [Stores](stores.md).

### session-context

```toml
[guards.session-context]
files = ["STATE.md", ".state/STATE.md", "HANDOFF.md", "TODO.md"]   # optional: this is the default
warn_api_key = true                  # optional, true unless you set it
```

Two lines come after the file lines.

- **The API key.** When `ANTHROPIC_API_KEY` is set in an interactive session, one line says the session bills that key,
  not your Claude subscription. The line never holds the key or any part of it. A headless session gets none: Claude Code
  sets `CLAUDE_CODE_ENTRYPOINT` to a value starting `sdk` for `claude -p` and the SDKs, and a `ws run` job holds ws's
  lease placeholder in place of a key. Set `warn_api_key = false` to turn the line off.
- **What changed in ws.** The first time a project's session starts on a ws version, one line gives the version (and the
  version that project last saw), then up to four lines from that version's section of `CHANGELOG.md` when ws ships one
  beside its code. Without one, the version line is all there is. The next start in that project on the same version says
  nothing. The record is `~/.local/state/ws/sessions/project-HASH.session-context.json`, where HASH comes from the project's
  folder (a linked worktree shares its main worktree's record). A start with no session id records nothing. If the record
  can't be written, the line is left out and the rest of the start text goes ahead.

### spawn-cap

```toml
[guards.spawn-cap]
max_agents = 4      # optional, 1 or more; left out, it comes from the machine (see below)
stale_s = 1800      # optional: seconds before an unfinished start stops counting
```

The guard counts the subagents this session has started and not seen stop. Before the call it only reads the count;
at `max_agents` it denies the start and the reason says how many are running. The start is recorded after the call, in
the `PostToolUse` hook, so a call that was denied or that you declined uses no slot. Only an agent started with
`run_in_background` is recorded: a foreground agent has already stopped when its call returns. The count is an
estimate: agents started together in one message are all checked before any is recorded, a stop clears the oldest start
because the stop payload doesn't say which agent it was, and a start older than `stale_s` drops out. The record is
`~/.local/state/ws/sessions/<session id>.spawn-cap.json`.

With no `max_agents`, the limit is worked out from the machine each time: the smaller of half the CPUs and half the
memory in GiB, at least 2 and at most 8. The CPUs are the cgroup v2 `cpu.max` quota divided by its period when one is
set (a container or service limit), never more than the machine has, else `os.cpu_count()`. The memory is
`memory.max` when it's set, else it isn't a limit. A file that's missing, says `max` or can't be read counts as no limit.
A 4 CPU, 16 GiB container gets 2; an 8 CPU, 8 GiB machine gets 4; a 32 CPU host with no limit gets 8. The reason a
start is denied names the limit in force.

### skill-gate

```toml
[[guards.skill-gate]]
skill = "taste-gate"
paths = ["**/*.css", "**/templates/**"]
```

To name `skill_dirs` as well, put the rules under one table:

```toml
[guards.skill-gate]
skill_dirs = ["~/.claude/skills"]     # optional: this is the default for Claude Code

[[guards.skill-gate.rules]]
skill = "taste-gate"
paths = ["**/*.css", "**/templates/**"]
```

One table for each skill. A Skill tool call for the skill, or a prompt that starts with `/taste-gate` (or
`/plugin:taste-gate`), records it for the session. A write to a path in `paths`, before the skill is recorded, is a
deny you can override once with `ws approve ID`. After the skill is recorded the paths are open for the rest of the
session. The record is `~/.local/state/ws/sessions/<session id>.skill-gate.json`.

A skill can carry its own rule. A `gates` list in the front matter of its `SKILL.md` names the globs it guards:

```markdown
---
name: taste-gate
gates: ["**/*.css", "**/templates/**"]
---
```

The guard reads `SKILL.md` in each folder under `skill_dirs` (Claude Code's default is `~/.claude/skills`; another
runner reads none until you name a folder). The skill's name is `name` from the front matter, else the folder's name. A
policy rule for the same skill replaces that skill's front matter rule, so the policy has the last word. A skill folder
or `SKILL.md` that's a link is read through the link, but a link that leads outside your home folder isn't followed,
and a `skill_dirs` entry outside your home folder is a policy error. Only the first 16384 bytes of a `SKILL.md` are
read, so the front matter must close inside them; one that doesn't gives the stderr line.
A `SKILL.md` that can't be read, or whose `gates` isn't a list of globs, adds no rule and gives one line on standard
error, once for each session. It never stops a write. The front matter is read as ws's safe subset of YAML.

### dev-server

```toml
[guards.dev-server]
allow_hosts = ["203.0.113.5"]    # optional: addresses a dev server may bind besides loopback
```

A development server that listens on every interface can be reached by anyone on the same network. The guard reads each
shell command, through `sudo`, `env`, `bash -c`, `python -m`, a path to the program, and the launchers `npx`, `bunx`,
`pnpx`, `uvx`, `npm exec`, `pnpm exec` and `dlx`, `yarn exec` and `dlx`, `bun x`, `uv run`, `poetry run`, `pipenv run`,
`pdm run`, `rye run`, `hatch run`, `conda run`, `pipx run` and `bundle exec`. It denies these, and the reason names the
flag that keeps the server on this machine.

| Command | Denied when | Flag to add |
|---|---|---|
| `python -m http.server` | there is no `--bind`/`-b` (it listens on every interface by default), or the value isn't loopback | `--bind 127.0.0.1` |
| `php -S` | the host isn't loopback (`0.0.0.0:8000`, `[::]:8000`, `:8000`) | `-S localhost:PORT` |
| `serve`, `npx serve` | there is no `-l`/`--listen` with a loopback host (`-l 3000` is every interface) | `-l localhost:3000` |
| `vite` | `--host` has no value, or a value that isn't loopback | `--host 127.0.0.1`, or no `--host` |
| `flask run` | `--host`/`-h` or `FLASK_RUN_HOST` isn't loopback | `--host 127.0.0.1` |
| `jupyter lab`, `notebook`, `server` | `--ip` (or `--ServerApp.ip`, `--NotebookApp.ip`) isn't loopback; `*` and an empty value are every interface | `--ip 127.0.0.1` |
| `rails server`, `rails s` | `-b`/`--binding` isn't loopback | `-b 127.0.0.1` |
| `uvicorn` | `--host` isn't loopback | `--host 127.0.0.1` |
| `hugo server` | `--bind` isn't loopback | `--bind 127.0.0.1` |
| `http-server`, `npx http-server` | there is no `-a`/`--address` (it listens on every interface by default), or the value isn't loopback | `-a 127.0.0.1` |
| `python manage.py runserver`, `django-admin runserver` | the address in `ADDR:PORT` isn't loopback (`0.0.0.0:8000`, `0:8000`, `[::]:8000`); a bare port is fine | `runserver 127.0.0.1:8000` |
| `next dev`, `next start` | there is no `-H`/`--hostname` (it listens on every interface by default), or the value isn't loopback | `-H 127.0.0.1` |
| `streamlit run` | there is no `--server.address` with a loopback value | `--server.address 127.0.0.1` |
| `gunicorn` | a `-b`/`--bind` isn't loopback (`unix:` sockets are fine) | `-b 127.0.0.1:8000` |
| `webpack serve`, `webpack-dev-server` | `--host` isn't loopback | `--host 127.0.0.1` |
| `astro dev`, `astro preview` | `--host` has no value, or a value that isn't loopback | `--host 127.0.0.1`, or no `--host` |

Loopback is `localhost`, any `127.x.x.x` address, `::1` and `[::1]`. A server that listens on loopback unless told
otherwise (everything above except `http.server`, `php -S`, `serve`, `http-server`, `next`, and `streamlit`) is checked
only when a flag says otherwise. `allow_hosts`
lists hosts and addresses that are fine to bind, such as a VPN or tailnet address; a port after the host is ignored,
and a name that means every interface (`0.0.0.0`, `::`, `*`) is a policy error. The deny is approvable, so a person can
allow one command once with `ws approve`. A script that holds the flags (`npm run dev`) isn't read, nor is a host set
by a variable (`env HOST=0.0.0.0 npm run dev`), nor a listener that isn't a development server (`socat`, `nc`); the
sandbox is what limits the network.

### checkout-pin

```toml
[guards.checkout-pin]
paths = ["~/code/app"]    # optional: checkouts to pin even when they have no linked worktree
```

When a repository has linked worktrees, its main checkout is the shared one: a session that switches branch, resets or
rebases there pulls the floor from under the others. The guard pins a checkout that has linked worktrees, and any
checkout named in `paths` (absolute or `~/`, matched by the top of the checkout with links followed). Inside a pinned
checkout it denies `git checkout` and `git switch` to another branch or commit (and `-b`, `-B`, `-c`, `-C`, `--detach`,
`--orphan`, `-f`), `git checkout NAME` with no `--`, `reset --hard`, `--merge` and `--keep`, `rebase`, `merge`, `pull`,
`cherry-pick`, `revert` and `am`. `--abort` and `--quit` of those (and `--show-current-patch` of `am`) go through so a
half-done one can be undone. `git bisect` is denied except `log`, `view`, `visualize`, `help` and `terms`. A whole-tree
restore from another ref is denied: `git checkout REF -- .` (or `:/`), `git restore --source=REF .` (also with
`--worktree --staged`), and `git stash pop`, `apply` and `branch`. `git worktree move` to a destination inside the
checkout is denied as well. The reason names the checkout and says to use a worktree: `git worktree add ../NAME -b
BRANCH`.

These go through: `git checkout -- FILE` and `git checkout TREE -- FILE`, `restore` of files, `add`, `commit`, `fetch`,
`status`, `log`, `diff`, the rest of `stash`, a soft or mixed `reset`, and any command in a linked worktree (whose `.git`
is a file) that `paths` doesn't name. A command is judged by the checkout it acts on: the one the session's folder is
in, `git -C DIR`, `--work-tree DIR`, `GIT_WORK_TREE=DIR`, or the folder of a literal `cd` earlier in the same command
line. `--git-dir DIR` and `GIT_DIR=DIR` count too, since HEAD lives in the git dir: `MAIN/.git` belongs to `MAIN`, and a
linked worktree's git dir (found through its `gitdir` file) to that worktree. A `GIT_DIR` or `GIT_WORK_TREE` set earlier
by `export` or `declare` can't be read, so a command that would move HEAD after one is denied.

The guard keeps the set of folders the shell could be in after a `cd`: a subshell `(cd x)`, a pipe, `&&`, `||`, a
background `&` and a `cd` to a folder that doesn't exist can each leave the shell where it was, and the command is
denied when any folder in that set is pinned. A plain `cd WORKTREE && git checkout x` goes through. A folder that
comes from a variable or substitution can't be read, so a command that would move HEAD from there is denied and the
reason asks for a path. `git -c alias.NAME=COMMAND NAME` is read as `COMMAND`. An alias from a config file or the
environment isn't read, nor is a script the command line runs. The deny is approvable.

## Findings

A guard returns nothing, or a finding with an action:

| Action | Before a call | After a call | Other events |
|---|---|---|---|
| `deny` | The call is stopped and the reason goes to the model. | The reason goes to the model on standard error, exit code 2. The tool has already run. | Text for the session or a log line. |
| `warn` | A line on standard error, exit code 0. | As `deny`. | As `deny`. |
| `note` | A line on standard error, exit code 0. | A line on standard error, exit code 0. | Text for the session or a log line. |

A `deny` marked approvable can be overridden once by you with `ws approve ID` in a terminal of your own, the same way
`ask_commands` works: the approval is bound to the runner, the call, the folder and the session. See
[Approve a command](../how-to/approve-a-command.md). `skill-gate` and `test-weakening` ship with an approvable deny; the others stop the call outright.

The first `deny` in the order below wins, and the guards after it aren't asked. A reason is shown as
`guard-name: reason`.

### inbox

The messages are the ones `ws msg` keeps (see [Messages](messages.md)). The session id in a hook's payload is only a
claim, so a body is handed over only when the hook process descends from the runner of that session. Through the
resident gate that's the process the kernel reports at the other end of the hook's connection, never a field of the
request; where the system reports none (macOS) no body goes through the gate and the reminder is all the session gets. A
hook for another session's id reads no body and marks nothing delivered. The tools a message may be answered with are
pinned (`msg_send`, `msg_list`, `msg_read`, `msg_ack`): if the tool server offers a different set of message tools,
bodies are held back and the reminder is given. A stop is continued at most three times in a row (`stop_hook_active`
carries the count on); only Claude Code has a stop hook that can do it.

### presence

`busy` for a prompt and for the start and end of a tool call, `idle` for a session start and a stop. Each report carries
its hook's own time as `seq` and a `source`; an older report than the one held is dropped, and one of lower authority
doesn't override a fresher one of higher authority. A `busy` older than two minutes (the one window every reader uses,
`presence.STALE_S`) is read as "can't tell," the strictest case; an `idle` stays `idle`. The screen isn't read.

### pane-close

```toml
[guards.pane-close]
live_socket = "default"   # optional: the tmux socket whose panes are guarded (a path, or a name under the tmux folder)
```

A command is judged against the tmux server it will talk to (`-S path`, else `-L name`, else the socket in `$TMUX`,
else the default). Any server but the live one is a test server and is left alone, but only for one plain command: a
tmux call that follows another command (a `cd`, `unset` or `export` can change what a relative `-S`, `-L` or `$TMUX`
means), or is reached through a wrapper, a variable assignment or a shell's text, is taken to be aimed at the live
server. The live one is `live_socket`, by default this user's default socket, so no list of names is needed. Read-only
queries go to the command's own socket with `TMUX` unset. Refused whatever the registry says: `-a`, a target by index
or name, `kill-session` and `kill-server`, a close inside a loop, a command list, a pipe or a command substitution, a
wrapper (`sudo`, `env`, `xargs`) or variables set in front, a tmux command word that's not plain text (a variable or
a glob), a registry or tmux that can't be read, a registry that changed while the check ran, and a pane with no
registered session. The command is read before anything else, so a quote-split spelling is the command it spells. Text
that's run is read too: a shell's `-c` text, `eval`, a pipe into a shell, the text of `new-window`, `split-window`,
`new-session`, `respawn-pane`, `display-popup`, `pipe-pane`, `run-shell`, `if-shell`, `send-keys`, `bind-key`,
`set-hook` and the like, a `#(...)` format, and the file `source-file` names; a kill in any of them is refused. A
script file a shell is told to run isn't read. The caller is `session.resolve_caller` and the lead is the identity
folder's role; a window's name, a tmux option, `TMUX_PANE`, the environment and the payload's session id on its own
decide nothing. A session closes only its own pane, and only once a `done` or `stop-and-hand-over:done` it sent has
been delivered. Without `tmux` installed it says nothing.

## The order guards run in

The order is fixed. It doesn't depend on how `[guards] on` lists the guards, and it's the same for every runner and every
call:

1. The preflight (Claude Code only: a project whose settings open the sandbox).
2. The policy's own rules: `deny_commands`, `deny_read`, `deny_tools`, the protected files, then the search and write
   rules.
3. The guards that are on, lowest priority first, and by name within a priority.
4. `ask_commands`.

The first refusal ends it. Each guard declares a `PRIORITY`:

| Priority | For | Guards |
|---|---|---|
| 0 | A guard that only records, so a call a later guard refuses is on record too | `presence`, `record-calls` |
| 10 | A guard that protects the machine or the repository | `bulk-staging`, `checkout-pin`, `dev-server`, `pane-close`, `test-weakening` |
| 20 | A guard that keeps a session honest about its own work | `skill-gate`, `spawn-cap` |
| 30 | A guard that checks what a message says | `claims` |
| 90 | Advice after the fact | `inbox`, `session-context`, `syntax-check` |

A guard that declares none runs at 50. Each hook run that asks a guard, or refuses a call, writes one line to
`logs/gate.jsonl` in the state folder: the decision, the layer that made it (`rule:command`, `guard:NAME`, ...), each
guard's verdict (`pass`, `note`, `warn`, `deny` or `error`) and how long it took. The line holds no command, path or
message text. See [stores](stores.md).

## Events

| Event | Claude Code hook | `ws gate event` answers with |
|---|---|---|
| `pre` | `PreToolUse` | `ws gate check` answers this event. |
| `post` | `PostToolUse` | A `deny` or `warn` as standard error with exit code 2. A `note` as standard error with exit code 0. |
| `start` | `SessionStart` | Findings as text on standard output, which Claude Code adds to the session's context. Exit code 0. |
| `prompt` | `UserPromptSubmit` | Findings as text on standard output. Exit code 0. |
| `stop` | `Stop` | Findings as lines on standard error. Exit code 0. The one exception is the `inbox` guard's: a stopping Claude Code session with an `urgent` message waiting gets `{"decision":"block","reason":...}` on standard output, so it carries on and reads it. |
| `subagent-stop` | `SubagentStop` | Findings as lines on standard error. Exit code 0. |

`ws gate event` is advice. A payload it can't read, a policy that doesn't load and a guard that crashes are each one line
on standard error and exit code 0. The deny path is `ws gate check`, which fails closed.

## The decision log

Every call's check leaves one line in `logs/gate.jsonl`, an allowed call included, and a session in a data class writes
its own class's copy and no other (see [Stores](stores.md)). A line holds the session id, the tool's name, the decision,
each guard's verdict and the time taken. A decision other than `allow` adds `rule`, the id of what said no:

| `rule` | What said no |
|---|---|
| `preflight` | The adapter's preflight (a project that widens the sandbox). |
| `rule:command`, `rule:read`, `rule:search`, `rule:write`, `rule:tool`, `rule:mcp` | The policy's rules for that kind of call (`deny_commands`, `deny_read`, the protected files, `deny_tools`, the MCP lists). |
| `rule:unknown-command-word`, `rule:sets-ifs`, `rule:unread-write` | A refusal the wall makes redundant, where the wall wasn't proven. |
| `guard:NAME` | A guard in `enforce`, or in `log` without the wall. |
| `ask_commands`, `ask_mcp` | A call that waits for a person's approval. |
| `error` | The check itself failed, so the call is refused to be safe. |

A line never holds the reason text, the command, a path or a message. A tool no adapter reads adds `unlisted: 1` and
`counted_as: "write"`: what it does is unknown, so a count of writes counts it.

## Hook rules and the runner's own rules

Which hook rule a Claude Code permission rule (`Tool(specifier)`) could replace. Where the column says no rule, the
hook stays: what it judges depends on something a pattern on the call can't see.

| Hook rule | Claude Code rule that could replace it |
|---|---|
| `deny_commands` | `Bash(PATTERN)` in `permissions.deny`, which generate writes for each pattern with no hook-only character |
| `deny_read` | `Read(PATH)` in `permissions.deny`, and the sandbox's `denyRead` for the shell |
| `deny_tools` | `WebFetch(*)` or `WebSearch(*)` in `permissions.deny` |
| protected files | `Edit(PATH)` in `permissions.deny`, and the sandbox's write list for the shell |
| MCP lists (`allow_mcp`, `ask_mcp`) | `mcp__SERVER__TOOL(*)` in `permissions.allow` or `permissions.ask` |
| `ask_commands` | no rule: `Bash(PATTERN)` in `permissions.ask` asks, but the hook binds the approval to the session and folder |
| preflight | no rule: it reads the project's settings files |
| `unknown-command-word`, `sets-ifs`, `unread-write` | no rule: the sandbox holds what such a line writes |
| `bulk-staging` | `Bash(git add -A*)` and the rest of its `NATIVE_DENY`; the forms a pattern can't say stay with the hook |
| `checkout-pin` | no rule: what it denies depends on the folder |
| `claims` | no rule: it asks git about the commits a message names |
| `dev-server` | no rule: it reads the flags and the address |
| `pane-close` | no rule: it reads the session registry |
| `spawn-cap` | no rule: it counts the agents running; `Agent(NAME)` can deny a named agent, not a count |
| `skill-gate` | no rule: it waits for a skill run in the session |
| `test-weakening` | no rule: it compares a test file before and after |
| `syntax-check` | no rule: it parses the file written |
| `session-context`, `inbox`, `presence`, `record-calls` | no rule: they never refuse |

## What `ws gate generate` writes for Claude Code

The settings carry a hook for an event only when a guard that's on runs there:

| Hook | Added when |
|---|---|
| `PreToolUse` running `ws gate check`, matcher `*` | Always. Every tool reaches the gate, an MCP tool and a tool no adapter reads included. |
| `PostToolUse` running `ws gate event` | A guard that's on runs after a call. The matcher lists the tools for the kinds those guards read. |
| `SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStop` running `ws gate event` | A guard that's on runs at that event. |

Each of these hooks runs the thin gate client, which asks the [resident gate](gate.md#the-resident-gate) and runs the
`ws gate` command itself when that can't answer. The `PreToolUse` hook ends `|| exit 2`, so a missing or crashing ws
blocks the call. It and the `PostToolUse` hook set `onFailure: "block"`, so a crash or a time-out blocks too. The
`PostToolUse` hook has no `|| exit`: `ws gate event` exits 2 on purpose after a tool call, and Claude Code shows that
message to the model. Every other hook command (`SessionStart`, `UserPromptSubmit`, `Stop`, `SubagentStop`) ends
`|| true`, so the session carries on whatever it returns.

The settings `env` carries `WS_GATED_SESSION=1` and `WS_SANDBOXED=1`. The hook checks a project's own
`.claude/settings*.json` for keys that open the sandbox in every session except one whose settings set
`WS_SANDBOXED=0`, the marker of settings written without ws's `sandbox` block. Settings generated before the marker
existed have the block and no marker, so the check runs for them.

Every generated settings file also carries two hooks that aren't guards: `Notification` and `Stop` run
`ws notify alert --runner claude-code || true`, which sends the phone alert. `Stop` runs it after any guard hook for
that event.

OpenCode's adapter lists the guard names it doesn't run yet in `UNSUPPORTED_GUARDS`; that's every guard for now.

## Replay

```sh
ws gate replay FILE --runner claude-code [--policy FILE] [--json]
```

Each line of `FILE` is one JSON object: a hook payload, or `{"payload": {...}, "expect": "deny"}`. The lines
`record-calls` writes work as they are. Each call is judged as the live hook would judge it and prints `allow`, `deny`
or `ask` (a call that `ask_commands`, or an approvable guard, holds for your approval). Replay uses up no approval and
keeps no guard state. The exit code is 1 when a line's `expect` differs from the result, and 0 otherwise.

## Write a guard

A guard is one module in `src/ws/guards/`. The folder is the registry, and a name starting with `_` is a helper. The
package docstring in `src/ws/guards/__init__.py` is the contract: `NAME`, `EVENTS`, `KINDS`, `config(table, where)`,
`judge(call, cfg, ctx)` and, optionally, `NATIVE_DENY`. Nothing else changes: the policy loads the new name and the
generated hooks follow the events and kinds it declares.

Each call a guard sees is a neutral `Call` with a `kind`, a `value` and read-only `data`. The kinds are `command`,
`read`, `search`, `write`, `tool`, `agent`, `skill` and `message`. A write carries `old`, `new`, `content` or `edits`
when the tool gives them. A guard keeps its own per-session record with `ctx.state(name, update)`, a locked JSON file at
`~/.local/state/ws/sessions/<session id>.<name>.json` that goes when the session record is pruned.
