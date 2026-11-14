# Changelog

All notable changes to ws are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The release workflow reads the section whose heading matches the tag (`## [0.1.1]` for `v0.1.1`) and uses it as the
body of the GitHub release.

## [Unreleased]

### Added

- `ws session load` puts standing preferences, an area card and, on the first prompt, the records a search finds in front
  of a session, from one folder of plain files (`WS_CONTEXT_DIR`). A walled session sees only its own class's records
  and the core preferences; a record with an unknown, wrong-case or non-text `class`, or front matter ws can't fully
  read, is dropped. The scan runs before the registry lock, so a big folder can't stall other sessions' hooks. A first
  prompt that finds nothing ("continue") is tried again for up to three prompts. `--runner` is accepted and ignored.
- `ws walls canary` runs the walls cases on a planted world, so it exits 0 on a machine with no class set up and fails
  when a setting the wall writes is missing. It lists the items its cases cover.
- `ws walls check` has four more cases: the launch file's write block and token deny, Remote Control and connectors
  off, a headless job's writes held in the class, and a `local_only` class held to a local model.
- `terms` under `[classes.NAME]`: `ws run` refuses a job whose prompt carries one of a walled class's terms (or a
  canary token) when its model is hosted and the job isn't in that class, with one `refused:term` record that names the
  class and not the term. A second net counts name-like word pairs on stderr and refuses nothing.
- `ws doctor` warns (WS020) when the policy names no data class. The guards reference says which guards are off by
  default and why.

### Changed

- A walled class's launch file also blocks file-tool writes outside the working folders, denies the four sign-in token
  variables to shell commands, and sets `disableRemoteControl` and `disableClaudeAiConnectors`. The managed-settings
  file is unchanged. Print the launch file again with `ws install --class NAME` and put it in place.
- A push alert from a session in a walled or counts-only class is always the plain line ("a session needs you"), with
  no folder name and none of the runner's words, whatever `alert_text` says.

### Fixed

- The `class-push-refused` case asked about a listed remote on port 443 even when the entry names its own port.

## [0.1.10] - 2026-11-12

### Added

- A launch file per walled class. `ws install --class NAME` prints a second root-owned file, in a `ws-classes` folder
  beside the managed-settings folder (Claude Code doesn't read it), and `ws gate launch` merges it into the settings it
  passes with `--settings`: `SendMessage` and `ListAgents` denied, `Read` and `Edit` of the sealed paths denied,
  `crossSessionInbound` refused, the read block, `disableAllHooks` false, the other classes' repos unreadable and the
  class's hosts under `strictAllowlist`. A launch with no launch file is refused (exit 3). The managed-settings file is
  unchanged: it's the file 0.1.9 printed, and a bare `claude` run as the class account is bound by it as before. See
  [decision 0027](docs/decisions/0027-a-settings-file-per-walled-class.md).
- A walled class's project is read against an allow-list: a `.claude/settings*.json` may hold only a short list of keys
  and nine named `env` variables, and a `.mcp.json` may start no MCP server. Anything else is refused at launch, with the
  key named.
- A class with `repo_settings` and no `os_user` is refused when the policy loads.
- A `mode` key in every guard's table: `enforce` (the default) or `log`. In `log` a refusal is counted in the verdict
  log instead, and only where the OS wall is proven for the call. See
  [decision 0028](docs/decisions/0028-a-guard-logs-what-the-wall-enforces.md).
- A walled class takes messages only from its allowlist, and every message to it leaves a log row with no body. Memory
  and notes refuse a walled caller. `ws walls check` has eight more named cases.
- A session in a data class writes its gate verdict log and call log to `logs/classes/<name>/`, not the shared files.
  A walled or counts-only class's call log keeps no payload, whether or not the class names an `os_user`, and the
  class is the stricter of the folder the session started in and the folder a call says it ran in. `ws gate replay` of
  the shared call log also reads the class copies. Nothing is written when the class log folder isn't the running
  user's.
- `ws doctor` WS019 warns when more than one ws class drop-in is in the managed-settings folder, or one is for a class
  the policy doesn't name (one class per machine is the tested shape). `ws install --class NAME` prints the same warning
  when another class's drop-in is in place.
- `ws run` jobs start inside the same wall as a launch: a Claude Code job gets its own launch copy for its project, and
  an OpenCode job runs under bubblewrap (or `sandbox-exec`) with private data folders and ws's proxy. See
  [decision 0029](docs/decisions/0029-a-job-runs-inside-the-launch-wall.md).
- A walled OpenCode start reads the class launch file, hides the other classes' folders and the per-folder `deny_read`,
  and refuses a project config that names an MCP server or a plugin, or pulls in a file with `{file:`.

### Changed

- A refusal found while a job's wall is built (a launch file that's missing, a project the runner's check refuses) is
  recorded `refused:wall`, exit 3, and a clean-up step that fails prints a warning without losing the run record.
- A job with no way to build the wall is refused as `refused:wall`: an OpenCode job with no bubblewrap or `sandbox-exec`
  (`[gate] sandbox = false` leaves it bare), a job whose folder is the home folder or above, one whose class launch file
  is missing, or one whose project holds MCP servers or plugins the class doesn't allow. A class with `[gate] sandbox =
  false` refuses a job on any runner.
- An account a class names in `os_user` is refused in a folder of no class or of another class, a job with no folder
  included.
- A Claude Code start makes a placeholder for a missing `.claude` or `.mcp.json` and refuses one that's a symbolic link.
- A policy whose `[gate] allow_write` names the home folder, an absolute spelling of it, or a folder above it
  refuses to load until the entry is edited. The error names the entry.
- The removal steps `ws install --class NAME` prints paste and run in bash, with each note on a `#` line, and take out
  the launch file, any half-placed copy and the launch folder.
- The paths a walled class's launch file seals are built from the ws folders `ws.paths` names, not a fixed `~/.ws`.
- A tool call no rule names is written to the verdict log, and `bulk-staging`'s deny rules in the runner's settings
  carry every whole-tree form a pattern can say.

## [0.1.9] - 2026-11-11

A security release. Upgrade with `ws upgrade` from a terminal of your own. This release removes nothing: no command,
setting or file format goes away, and a queue store from 0.1.8 is migrated in place.

### Added

- `ws` refuses its own admin commands inside an agent session (exit 3, "run it in your own terminal"), as well as the
  gate refusing them on the command line. The admin commands are one table; it now also holds `upgrade`, `node join`
  and `node remove`, and the `queue` commands that add, cancel or run work. See
  [decision 0020](docs/decisions/0020-admin-commands-refuse-inside-an-agent-session.md).
- The gate judges MCP tool calls: `deny_tools` takes MCP names and globs, a tool the new `[gate] allow_mcp` list doesn't
  name is asked for, and path-like arguments are checked against `deny_read`. See
  [decision 0021](docs/decisions/0021-the-gate-judges-mcp-tool-calls.md).
- Queue: `memory_reserved_gb` in `[roles.queue]` keeps memory promised to other machines or services out of the count;
  one notice through the notify role when a job stops for good; a job may spend from a live parent run
  (`parent_run`, `ws run --parent`), one level deep, and still meets the monthly and purpose caps.
- A live test against a local Ollama server, run when `WS_OLLAMA_URL` is set.
- `ws upgrade` swaps ws for a newer release and rewrites the runner's hooks in the same step, from a terminal of your
  own. A failed rewrite puts the runner's config and the install record back, and the message says which files, if any,
  it couldn't. The gate refuses the command in a session. Doctor check WS013 reports hooks older than the ws that
  runs them.
- Messages reach the model. The `inbox` guard hands a session its pending messages at its own turn, as untrusted data
  that grants no permission; `presence` keeps its busy or idle state; an idle Claude Code session is woken by a Stop hook
  that runs `ws msg wait --session-hook` (exit 2 only for a delivered message, silent and exit 0 otherwise, and at once
  under a one-shot `claude -p` run), and an idle OpenCode session by its plugin. A runner with no such hook gets a fixed
  line typed into its tmux pane. New commands `ws msg watch` and `ws msg wait`; see [Messages](docs/reference/messages.md).
- The `pane-close` guard: `tmux kill-window` and `kill-pane` on the live server are allowed only as one plain command,
  by stable id, when the registry says the target is idle (and the caller is the lead) or is the caller's own pane after
  its hand-over. It reads the command before anything else, including a shell's `-c` text, `eval`, a pipe into a shell
  and the text that tmux's own commands run, and takes a tmux call that follows another command to be aimed at the live
  server.
- `ws install --class NAME` prints the commands and the root-owned managed-settings file for a class whose sessions run
  as their own OS user, and writes nothing. Doctor check WS014 reports each part of such a class on its own line. A
  session of the class started as another account is refused (exit 3). See
  [Set up a class user](docs/how-to/set-up-a-class-user.md).
- CI is split into jobs with a time budget each: `ci`, `oracle`, `node`, `term-browser` and a nightly quarantine run. A
  test-integrity check (T1) asks for a `test-change:` line in the pull request body when a test is weakened or the test
  machinery or CI changes, and a `holdout-change:` line for an edit to `tests/holdout/`. See
  [CI](docs/reference/ci.md).
- `ws install --gate every-session|launch` and `ws install --class NAME` are in the CLI reference, with `ws msg`.

### Fixed

- A hook that carries no pin on ws's folders (written by an earlier ws) runs the full check and prints a note that
  names `ws upgrade --hooks-only`, as long as ws's folders are where the account's home and the hook's own policy
  file say they're; otherwise it's refused, with the same command to run. A pin that's partial, malformed
  or different is refused as before.
- `ws install` and `ws upgrade` leave the sandbox out of a runner config whose ws entries were written without it, until
  the policy says `sandbox = true`.

## [0.1.8] - 2026-11-08

A security release. The runner's sandbox is now on in every install mode and is the boundary; the gate reads more
command forms that run other text, and is tested against a real shell. ws's files move to the XDG folders.

Run `ws install` once after upgrading, so the hooks and the sandbox settings are rewritten.

### Added

- The sandbox is on by default when ws installs into the runner's own settings (Claude Code), with ws's protected
  paths hidden from it and ws's own files read-only. `[gate] sandbox = false` turns it off.
- `ws install` refuses a Claude Code settings file that widens the sandbox (the sandbox switched off, excluded
  commands, unsandboxed retries allowed, an allowed read or write that covers a protected path), and names each key
  and why. `--allow-widened-sandbox` keeps your settings and records the choice.
- Doctor check WS012: the sandbox's settings, read from your settings and the managed settings files ws can read.
  `warn` when the wall is widened or its hidden-path list is older than the policy ("run `ws install`").
- `ws migrate [--dry-run]` and doctor check WS010: ws keeps its files in the XDG base folders (`~/.config/ws`,
  `~/.local/state/ws`, `~/.local/share/ws`, `~/.cache/ws`). An existing `~/.ws` keeps working until you migrate.
- Release channels: `install.sh --channel stable|rc|edge` (uv first, pipx as the fallback), release candidates on
  PyPI as pre-releases, and a `channel` field in `ws version`.
- Evals: every result records a fingerprint (models, runner, ws version, policy, guards, job, limits), and
  `ws eval compare` refuses mixed arms unless `--allow-mixed`. Compare reports an exact 95% interval and says whether
  a difference is real or there are too few runs to tell.
- `scripts/oracle_sweep.py`: every row the real-shell oracle knows, through the gate and run in a sandbox, as one
  command with an exit code.

### Changed

- The gate refuses a command it has no entry for when that command is given a shell to run, and treats a shell's
  versioned or static build (`bash5.2`, `bash-static`, `busybox sh`) as that shell.
- A here-document or here-string given to any shell, `source`, `.`, `eval`, `ssh` or `xargs` is read as code.
- The gate hooks run on the folders `ws install` pinned and start ws isolated; a missing named policy file is a deny,
  not the default policy.
- CI runs the real-shell oracle on every Linux job and fails if it can't start a sandbox.
- A copy of the repo without the PII denylist can opt in to the built-in rules only, with a visible warning.
- The local actionlint hook brings its own shellcheck, so it matches CI.
- Docs: decision records, a shorter AGENTS.md, and the code map in the reference.

### Security

- Fixes advisory [GHSA-q765-g7hh-6gw4](https://github.com/Michael-A-Fry/ws/security/advisories/GHSA-q765-g7hh-6gw4):
  a session could steer the gate's state folders, fall back to the default policy, or get a protected-path read past
  the gate through command forms it didn't read. The sandbox is the boundary; the gate is defence in depth, and its
  known limits are listed in the advisory and in the docs.

## [0.1.7] - 2026-11-07

A browser terminal that keeps the screen still, a gate that reads commands the way the shell does, and a labelled
corpus that measures the gate on every change.

### Added

- `ws term`: a terminal in a browser tab, for reaching a session from a phone or another computer. One size owner, so
  windows never fight over the size; a phone key bar (Esc, Tab, Shift+Tab, Ctrl, arrows, a new line, Ctrl+C, Paste);
  Shift+Enter makes a new line; a 10 KB paste arrives intact; the on-screen keyboard doesn't resize the grid. It
  listens on 127.0.0.1 with a per-launch token, takes one window at a time, and reaches other devices through a tunnel
  you already trust (`--bind` names an address on a private network). `--tmux NAME` sets options on that session only.
- `ws gate replay FILE... --metrics`: replay a labelled corpus of tool calls (`ws.gate-corpus/1`) and print the
  confusion matrix, the false-block rate with a 95% interval and the time a check takes. `--save` and `--diff` show
  every decision a change to the policy or the gate moves; `--expect-changes` lists the ones you meant. CI replays the
  shipped corpus on Claude Code and OpenCode.
- Guards: `checkout-pin` (off by default) denies a branch switch, reset, rebase or merge in a checkout that has linked
  worktrees; `dev-server` (on by default) denies a dev server that listens on every interface and names the flag to
  add.
- `[roles.notify] alert_on = "needs-you"`: a phone alert only when a session asks you something or waits on a choice.

### Changed

- The gate reads every command with one shell-aware reader: quotes, braces, `~` and `$HOME`, redirects, option values,
  argument files and the commands it wraps. A form it doesn't follow is refused with a hint on how to write the command
  plainly, and a check has a time limit.
- The session-context guard's default state files are `STATE.md`, `.state/STATE.md`, `HANDOFF.md` and `TODO.md`. A
  project that keeps its state elsewhere sets `files` under `[guards.session-context]`.
- Plugins carry their own region rule; there's no default country, and setup asks for one only when an offered plugin
  needs it.
- The claims guard reads the default branch from `origin/HEAD`, then `init.defaultBranch`, then `main`.
- The default `deny_read` covers `~/.config/ws/secrets`.

### Security

- The gate now refuses command forms that read a protected path through an option value, an argument file, a brace
  group in a redirect target, or a shell expansion the 0.1.6 matcher didn't resolve. See advisory
  [GHSA-5g97-2qf5-8g2w](https://github.com/Michael-A-Fry/ws/security/advisories/GHSA-5g97-2qf5-8g2w). The sandbox
  stays the boundary; the gate is the layer that explains a refusal.

## [0.1.6] - 2026-11-02

A safer launch environment, setup that offers only what's built, and the first part of the ws home data API.

### Added

- `ws home data [--json]`: one read-only snapshot (schema `ws.home/1`) of your sessions, what's waiting on you,
  plugins and eval results, read from scanned sources. A source that can't be read is one row saying why. Sessions in
  folders you list under `[home] opaque_paths` show only kind, place, state and age.
- `ws.ui`: the shared page styles, components and loopback server that the setup page uses, ready for the home page.
- Guards: the claims guard checks claims that a release is live against the promoted release (`release_dir`);
  skill-gate reads a skill's own `gates` front matter; session-context warns when an API key is set in an interactive
  session and lists what changed in ws since the last session.
- `ws gate launch --pass-env NAME`: pass a named variable into the session.
- Doctor check WS009: a profile still switches on a plugin that isn't built yet.

### Changed

- `ws gate launch` passes the session an allow-listed environment (the runner's own variables, locale and paths)
  instead of everything in your shell. A key you want the session to have now needs `--pass-env NAME`, or the broker.
- Setup, `ws plugin list` and `ws install --module` offer only plugins that are built. A profile that already has an
  unbuilt one switched on still loads.
- The usage check sends the admin key only to the provider's own address.
- The backlog lives in GitHub Issues; `TODO.md` holds what's being worked on now.

### Fixed

- Tests no longer set one time zone for the whole run, and pass in any zone.
- A UI test no longer fails on macOS when an oversize request is refused by closing the connection.

### Known limits

- The gate's command matcher is being hardened against more ways of writing a path; that lands in 0.1.7. Until then
  the sandbox (`ws gate launch`) is the boundary, as [known limits](docs/explanation/known-limits.md) says.

## [0.1.5] - 2026-11-02

One token source for the brand, rules enforced by tests, and a stricter gate.

### Added

- `docs/reference/principles.md`: the rules for each layer (brand, terminal colour, UI, writing, front end, CLI, data,
  governance), each naming the test that checks it; `tests/test_principles.py` keeps the page honest.
- `src/ws/brand.py`: one role-named token source (dark, light, 256-colour and basic ANSI) that the setup page and the
  terminal both read, with WCAG contrast tests for every pair.
- `src/ws/style.py`: one shared terminal style for doctor, status and error text; a global `--no-color`.
- `docs/reference/stores.md`: every log ws writes, with its fields, schema and rotation. Log lines carry `ts` and
  `schema`; `at` is kept beside `ts` for one release.
- spawn-cap has a default limit when `max_agents` isn't set, from the machine's CPUs and memory (cgroup-aware).
- syntax-check checks `.js`, `.mjs` and `.cjs` files with `node --check` when node is installed.

### Changed

- PyPI lists Documentation, Changelog, Issues and Security links beside Source, and the README carries version,
  Python, licence and CI badges.
- Colour: a non-empty `NO_COLOR` turns it off; `FORCE_COLOR` and `--no-color` work on every command; `TERM=dumb`
  prints each step once. A light terminal (from `COLORFGBG`) gets darker brand colours and its own status colours.
- Light-mode colours on the setup page meet contrast as text: mint, amber, accent text, borders and the mark.
- The setup page uses a per-response nonce in its Content-Security-Policy.
- `ws doctor` ends with a line pointing at `ws setup` when roles are unconfigured.
- Symbols fall back to ASCII where the terminal's encoding can't print them.
- `ws status` lists each path once and marks folders.
- A plain `ws install` keeps the every-session gate merged; only `--gate launch` or `ws uninstall` removes it.

### Fixed

- An install keeps manifest records it didn't write, under a lock of its own, so a later install can't drop the
  gate's merge record. A file that matches what ws would write is taken back as ws's own.
- The gate denies reads of ws's secrets folder, including through `$WS_HOME`, a linked home and wildcards.
- The gate reads a quoted here-doc's body as file content only for plain `cat` and `tee` steps; every other body is
  read as commands. A command whose here-doc body holds an apostrophe no longer hides the lines after it.
- The gate denies a command too long to check in time, and says to write it to a file and run that.
- The README on PyPI links to the docs, changelog and licence on GitHub at the release's tag; relative links there
  went to a 404. The release fails if a built file's description still holds a relative link or image.
- systemd: ws recognises its own units after the ws home moves, and leaves units it didn't write alone.

## [0.1.4] - 2026-11-02

### Fixed

- The gate no longer blocks a `[ -f path ]` test inside `bash -c` when a dash later in the path reads as a backwards
  glob range; a `[` that doesn't open a valid class is matched as itself.
- syntax-check checks a `.sh` file with bash when its first line asks for bash.

## [0.1.3] - 2026-11-01

### Fixed

- `ws <word>` passes `--json` on to a `ws-<word>` program as well as setting `WS_OUTPUT=json`.
- Every-session install accepts an interpreter under HOME wherever HOME lives, including through a link.

## [0.1.2] - 2026-11-01

The gate and skills ready to run in every session on a person's machine, alongside their own hooks.

### Added

- Guards: named checks beside the policy rules, found by scanning `ws/guards/`. Shipped: `syntax-check`,
  `session-context`, `bulk-staging`, `record-calls`, `test-weakening`, `claims`, `spawn-cap` and `skill-gate`. The
  generated Claude Code config gains PostToolUse, Stop and Notification hooks; `ws gate event` answers them.
- `ws gate replay FILE`: re-runs recorded tool calls through the gate (writes are recorded as path, size and hash).
- `ws install --gate every-session`: adds the gate's hooks, deny rules and env to the user settings of Claude Code and
  OpenCode, keeps the person's own entries, records what it added and undoes exactly that; doctor check WS008.
- `ws <word>` runs `ws-<word>` from PATH when ws has no such command, and `ws config fallback PATH` hands any other
  word to a program of the person's; `install.sh --move-existing-ws-to PATH` moves a different `ws` aside.
- Skills `resume` and `ship`; `ws skills adopt|restore` swaps a person's same-named skill for ws's copy and back.
- `ws notify alert`: a phone alert when a session needs the person or finishes, on its own daily cap.
- `ws secret import NAME --from-env PATH`: stores a value from a `VAR=value` file.

### Fixed

- `ws broker serve` uses `WS_BROKER_SOCKET` and `[roles.secrets] socket`, as the clients do.
- `ws eval report` and `compare` list trials that errored before a run started apart from the metrics.
- The command matcher reads ANSI-C quoting (`$'...'`), here-documents, `${...}` and comments the way bash does.

## [0.1.1] - 2026-11-01

The first thin tag: phases 0 to 5 of the plan, installed from the signed release. It replaces 0.1.0, which was
withdrawn on the day it was published; 0.1.0 isn't available from GitHub or PyPI.

### Added

- `ws` command with `ws doctor` (stable check IDs, `--json`), `ws init`, `ws config`, `ws status` and one output contract
  for every command: typed errors with hints, a JSON envelope, global `--json` and `-C`.
- `ws version [--json]`: version, commit, dirty flag, build date, Python, install path and where the install came from.
- One vendor-neutral `policy.toml`, turned into native deny lists and a fail-closed hook for Claude Code and OpenCode
  (`ws gate`), with the Rule of Two and named exceptions.
- The wall: a write-allowlist sandbox behind the command matcher (bubblewrap and seccomp on Linux, `sandbox-exec` on
  macOS, Claude Code's own sandbox), a load-path table, private per-project XDG homes and a git-aware project lock.
- Bound, single-use approvals for commands the policy marks `ask_commands` (`ws approve`).
- Secrets: a registry and store, a broker that holds the values and leases a proxied call per run, and `ws secret wall`
  to move the store to its own user.
- Headless jobs: `ws run JOB` leaves one complete record per run in `runs.jsonl`, with a scrubbed environment, a budget
  cap and the Rule of Two; `ws rate` gives a run a thumbs up or down.
- Memory and Notes modules: Markdown with front matter in a git repo, an FTS5 index, `ws note`, `ws memory` and an MCP
  server (`ws mcp serve`).
- Setup: `ws setup` (inline in the terminal or a page on localhost), `ws install` and `ws uninstall` with a manifest,
  plugins by checkbox (`ws plugin`), `ws device add`, and Obsidian and Logseq folder detection.
- Role adapters for anthropic, opencode, markdown-git, systemd, launchd, ntfy, restic and sops-age.
- Public skills `align`, `checkpoint`, `taste-gate` and `dataviz`; `ws canary` to catch a runner update that breaks what
  ws relies on; a stale-session nudge after an update.
- Evals: `ws eval` with a TOML task format, graders that run in the wall, ten metrics, an exact McNemar compare and
  harvest; a mock provider with shared scenarios.
- `install.sh`: installs the wheel of the latest release (or `--version TAG`), checks its SHA-256 and, when `cosign` is
  installed, its signature bundle, then runs `ws doctor --offline`. `--pypi`, `--from PATH`, `--dry-run` and
  `--no-verify` are also there.
- Release workflow: on a `v*` tag, a source archive and a wheel, each with a `.sha256`, a CycloneDX SBOM, a GitHub build
  provenance attestation, a keyless cosign bundle per asset, an immutable GitHub release and a PyPI upload by trusted
  publishing.
- CI on Linux (the required check), plus macOS, WSL2 and native Windows cells that time install, first task and
  uninstall against the mock provider.

### Security

- Secret scanning, push protection, a PII gate over every file and the full history, and signed commits on `main`.
- Actions are pinned by commit SHA; tools are pinned at versions at least seven days old; `uv.lock` holds the rest.
- The gate fails closed: a payload it can't read, a policy it can't load or a rule it can't parse is a deny.
- Agents never see a secret value: the broker injects it at the allowed host, and `ws secret wall` puts the store behind
  a separate user.
- A tag or release asset can't be changed after the release (immutable releases, protected `v*` tags).

### Known limits

- The matcher can't see every shell trick; the sandbox is the real wall, and the limits of each are listed in
  `src/ws/policy.py`.
- A repo that a session creates with `git init`, submodules added mid-session and `sub/.claude` config are reported
  after the run, not blocked.
- `/proc/PID/mem` of the runner is readable inside the wall when `ptrace_scope` is 0 and the runner has no
  non-dumpable shim (Claude Code); `ws doctor` warns (WS007).
- The macOS wall profile and the eval checker's `PT_DENY_ATTACH` aren't tested in CI.
- A session that holds the runs log lock blocks every `ws run` (it fails closed and names the lock).
- The end-to-end tests don't exercise `ws run`'s own broker start, a real provider's certificate chain or a real model.
- Harvest checks secret names and shapes, not values; the notify cap counts ws's own sends only; sops-age is named but
  the broker doesn't read it yet.
- The model-judged (rubric) grader isn't built.
- The lab VM cell of the install test hasn't run yet.
- PyPI publishing waits on a trusted publisher being set up; until then the GitHub release is the install route.

[Unreleased]: https://github.com/Michael-A-Fry/ws/compare/v0.1.1...HEAD
[0.1.1]: https://github.com/Michael-A-Fry/ws/commits/v0.1.1
