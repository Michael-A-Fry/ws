# Changelog

All notable changes to ws are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The release workflow reads the section whose heading matches the tag (`## [0.1.1]` for `v0.1.1`) and uses it as the
body of the GitHub release.

## [Unreleased]

### Changed

- `ws triage`: the day's summary now names the threads that matter. It keeps, across the day's runs, one line for each
  thread that needs a reply or is worth a check (mailbox, sender, subject cut to about 80 characters, why, and whether
  a draft was saved); a thread the mailbox's owner later answers stays, under "Done". No mail body text is written, and a
  subject, name or reason has its control and bidi characters, markdown links and web addresses removed. Token, cost and
  run counts are no longer in the summary (they are in `logs/triage.jsonl`), and the label counts are in plain words.
  The first line, `Drafts created: N` and the "Worth a look" heading are as before. The day's state keeps up to 100
  lines and reads an older day's `flags` list.
- `ws triage run --dry-run` prints the summary it would write.

### Added

- A rules file label, `check`: the thread is labelled `ws/fyi` and listed in the summary as worth a check, with no model
  call (for example account-security mail). It adds no mailbox label.

## [0.1.11.7] - 2026-11-22

### Added

- `ws triage run`: the inbox job, deterministic first. New mail is sorted by a plain TOML rules file (sender, domain,
  bulk-mail headers, subject; each decision logs its rule id) and only a thread that may need a reply goes to a model:
  one call, no tools, the thread cut to a token cap, an answer that must parse to `{needs_reply, reason, draft}` (two
  retries, then the thread is flagged). A draft that names an amount, a date or a time the thread doesn't carry,
  promises a payment, holds a link the thread doesn't carry, opens with "Dear" or isn't 2 to 6 sentences is refused
  and flagged. Labels and drafts go through `ws mail`; nothing is sent, moved or deleted. The model is `[triage.model]`
  (the `claude-code` runner with a small model by default; `anthropic-api`, a direct Messages API call with the key
  leased through the broker and about 2k tokens in a call instead of 31k; or OpenCode) with a per-run cap in
  calls, tokens and money. Each run adds a line to `logs/triage.jsonl` (counts, tokens, cost, rule ids, never mail text),
  the uid state is `triage/NAME.json`, and `summary_dir` gets a `summary-DATE.md` that adds up across the day.
  `--dry-run` labels and drafts nothing. A mailbox whose data class isn't full access is refused. See
  [Inbox triage](docs/reference/triage.md).

- Jobs: `command = "triage"` schedules `ws triage run --job NAME` from a `[triage]` table in the job file, and a schedule
  may carry a window, `every 30m 05:00-22:00` (whole hours, both ends included; two `OnCalendar=` lines on systemd, one
  calendar entry per run on launchd).

### Fixed

- The loopback page server counted idle time from when it was built, and building it asked DNS for the host name (a
  reverse lookup that can stall for tens of seconds on a macOS runner), so it could close before its first request and
  `tests/test_ui.py` failed with `ConnectionRefusedError` about half the runs there. Idle time now counts from the start
  of `serve()`, and the constructor no longer looks the name up.

- `ws mail`: `Mail.search` passes `after_uid` through as given, so the broker refuses a bool, a string or a fraction
  instead of quietly turning it into a number.

## [0.1.11.6] - 2026-11-22

### Added

- `ws.mailconnect`: connect a mailbox from a page. It tries the app password with one read-only login before it
  writes anything, then stores it, adds `[mail.NAME]` and, for Gmail, Outlook or iCloud, appends a missing registry
  entry. A refusal stores nothing and is typed (`bad_credentials`, `app_password_required`, `imap_off`,
  `web_login_required`) with ws's own words. Three refusals for one account (host and login) in 15 minutes stop the
  next try before it dials. `known()` lists the mailboxes and when each last logged in; `set_on()` and `disconnect()`
  switch one off or take it out. (C666)

- `ws mail connect --web`: the Connect mail page on this machine's loopback, over `ws.mailconnect`. The page lists
  the mailboxes, takes a name, address, provider and app password, and says in place whether it connected. (C666)

## [0.1.11.5] - 2026-11-22

### Added

- The `pane-keys` guard: a `tmux send-keys` (or a `paste-buffer`) aimed at the live tmux server that types a line
  starting with `/` is an approvable deny, because a slash command can open a dialog that holds every later key and
  message until someone presses one (#276). The reason says to start sessions with `--name X --remote-control X` and to
  use `ws msg send`. It reads key names in any case (`M-/` is a slash), and refuses `tmux -C` control mode and
  `tmux pipe-pane -I` (which types a command's output into the pane) on the live server.

- `ws watch panes`: pushes once when a tmux pane has sat on a dialog for 10 minutes (`[watch.panes]` in the profile
  sets that and more footer patterns). It presses `Escape` on the Remote Control status box after a minute and on no
  other dialog. A footer counts only at the foot of the screen, and the dialog's text is quoted in the push only
  for a pane in a `full` data class. `ws-watch-panes.timer` (systemd) and `ws.job.watch-panes` (launchd) run it
  every minute (#276).

- A local provider's models (`runner_options.provider.models.<id>`) may set `tool_call = false` and
  `options.reasoningEffort` of `none`, `minimal` or `low`, so a small model can run a job without tools and with
  little reasoning. `tool_call = true` and the higher efforts are refused (they widen), as is every other new key.
  Checked at `ws job add` and again at `ws run`. (C635)

- A job file's `input` names one file inside the job's `cwd`. `ws run` reads it at each run, for every runner, without
  following a link, at most 64 KiB of UTF-8, and puts it after the prompt as untrusted data in the delimited,
  defused block `ws mail` uses. A missing or unfit file refuses the run. The run record gains `input_sha256`. It
  replaces data kept in a project `AGENTS.md` or `CLAUDE.md`, which an OpenCode job doesn't read (checked on
  OpenCode 1.19.4).

### Changed

- `pane-keys` is on by default: `ws gate init` and the built-in policy now write
  `on = ["syntax-check", "session-context", "dev-server", "pane-keys"]`. An existing policy keeps its own list; add
  `pane-keys` to it (#276). The upgrade does it for you: it adds `pane-keys` to the end of an existing `[guards] on`
  list once, as a text insert that keeps your comments and order, and prints one line saying so; a policy with no
  `[guards]` table or `on` list, or that already names it, isn't touched (#276).

- `ws dispatch` starts a Claude session with `--name LABEL` and `--remote-control=LABEL` (LABEL is the window's name),
  so nobody types `/rename` or `/remote-control` into its pane. Remote control is left off for a repo whose class isn't
  `access = "full"`, or when the policy can't be read (#276).

- `ws msg` wake never types text with a line that starts with `/` into a pane (reason `slash_text`) (#276).

### Fixed

- `ws mail add` asks for the password in place when it runs on a terminal, so a plain ssh login with no tmux works;
  a password it just stored that the server then refuses is taken out again (a network or broker failure keeps it).
  A refused mailbox login now has its own broker code, `login_refused`, apart from `upstream_refused`. (C666)

- A Gmail app password pasted with the spaces Google shows ("abcd efgh ijkl mnop") is stored as the 16 letters Gmail's
  IMAP login wants, by `ws mail add`, `ws secret set` and `ws secret ask`; a Gmail value that isn't 16 letters is
  refused before it's stored. Other hosts keep the value as typed. (C666)

## [0.1.11.4] - 2026-11-22

### Added

- `ws mail`: `add`, `check`, `rm` and `serve` for IMAP mailboxes registered in the secrets registry, and five MCP
  tools (list mailboxes, search, get, label, draft) behind a read-only-by-default connector. Drafts only: nothing is
  sent. (C626)
- `ws sync`: fetch every git repository under the code root, 4 at a time, each fetch killed (with its helpers)
  and named past `--timeout-s` (default 120); exit 1 when any failed or timed out.
- `ws upgrade` writes one line per step to `logs/upgrade.jsonl` (verb, step, Python, version, exit, seconds and the
  last 20 lines of the installer's or hooks step's output); the terminal output is unchanged. (C650)
- An OpenCode job can name a model server on this machine or the local network in `runner_options.provider`, and
  `ws job add` takes it: an allowlisted `@ai-sdk/openai-compatible` table whose `baseURL` is `localhost` or a literal
  loopback, private or CGNAT address. It replaces a provider block in a project `opencode.json`, which a job no longer
  reads.

### Changed

- `ws dispatch` passes the model as one argument, `--model=M`, so a model name that starts with `-` can't become a
  flag, and its code root expands `~` only for your own home (`~` or `~/...`).
- `ws upgrade` runs the hooks step with the ws the installer just wrote, read back from the `ws` command it put in
  place and that script's Python, and records that ws's version in the history; the step's line names the Python and
  version it ran with. (C648)
- `ws upgrade` restarts a broker still running older code once the swap's hooks step and check have passed, when it
  holds no lease and is your user's process (Linux: SO_PEERCRED and a pidfd); otherwise it leaves it running, prints
  the command that finishes it and exits 1 (`--json` status `attention`). (C651)
- A hand-written OpenCode job whose `provider` table names a public or non-literal address, another npm package, or a
  key outside the allowlist (such as `apiKey` or `headers`) is now refused at `ws run`, with the reason. A leased job
  whose `provider` table holds only `models` (the lease gives the address) now also needs
  `npm = "@ai-sdk/openai-compatible"`.
- A job whose provider is on the local network or a tailnet (a literal private, CGNAT or other loopback address) now
  counts as a local credential, so the queue and its timer take it; before, only 127.0.0.1, localhost and ::1 did.

## [0.1.11.3] - 2026-11-22

### Added

- The broker has an `imap` credential kind: it logs in to the registered host only and runs one operation from a fixed
  table (list, search, fetch, label, draft). Nothing sends, deletes, expunges, moves or copies; a draft goes only to the
  Drafts folder, and the mailbox password never leaves the broker. (#268)
- `ws.mail`: search, get, label and draft replies over the broker's `imap` kind. Mail text comes back inside a tagged
  untrusted block with forged delimiters defused; a draft's recipients and threading come from the original's headers,
  never from the caller, and a uid is tied to its mailbox. (#271)
- A job file may leave out `model`: it takes `[models].<purpose>`, else `[models].default`. `ws config model` shows and
  sets the model table, and `ws config budget` sets the spend cap; an agent session can't change either. (#269)

### Changed

- OpenCode jobs start no MCP server: each run gets its own HOME and XDG folders, project config isn't read, and a
  global `/etc/opencode` config is refused, so a policy with `allow_mcp` no longer refuses every OpenCode job. A job's
  per-run folders are used inside the wall too, never the person's persistent ones. (#270)
- A profile ws can't parse or read is never saved over. (#269)

### Known limits

- Under the new OpenCode isolation, a job's provider block or instructions in the project's `opencode.json` or
  `AGENTS.md` aren't read. A supported way for a job to name a local model server and a data input comes next.

### Removed

- Nothing.

## [0.1.11.2] - 2026-11-22

### Added

- `ws connect` (list, walk, `--dry-run --json`, `--revoke`, `--diagnose`): one flow format and engine for the setup
  steps only a person can do, with a JSON Schema, a reference page, a how-to and ADR 0033. The first flow is Telegram.
  A secret goes from the person's terminal to the secrets store only, after a real verify, and is in no state file,
  output or error. The walk and `--revoke` refuse inside an agent session. (#265)
- A secrets-registry entry may set `base_path` (for example `/api`) for a host that serves its API under a prefix; the
  broker adds it on the way out, after the `paths` check, and refuses a hostile request path. (#266)
- `ws passtest events`: a read-only counter of confirmed step-ins per week over a hash-chained pass-test ledger; a
  broken or truncated chain counts nothing and exits 1. (#257)

### Removed

- Nothing.

## [0.1.11.1] - 2026-11-21

### Security

- The gate's command reader reads more variable-setting forms ahead of a command, and env's and sudo's options, as
  bash and GNU env do. A form it can't read for certain is refused, so the checks on the real command are no longer
  skipped. (GHSA-w7j3-x774-963c)
- The gate's command reader reads sudo's options as sudo does, and refuses a sudo form that runs a program named by
  the environment, starts a shell it can't read, or changes folder. After `unset`, it no longer assumes the variable's
  earlier value. (GHSA-24rx-q7mr-rr2v)

### Added

- `ws install --class NAME` warns about the host (stderr, or `warnings` with `--json`): more than one class with an
  `os_user`, a managed-settings file that binds your own sessions too, or a class repo under your own home. (#262)
- `ws cards drop KIND` and `ws cards take`: fixed-kind cards go from a walled class's OS account to the lead's account
  through a spool folder. A card's class comes from the file's OS owner, and any malformed card is refused. (#262)
- A four-part version tag (`vX.Y.Z.N`) is a release tag in the release workflow, `install.sh` and the PyPI README
  pin. (#263)
- The message wall: the gate refuses a send-message call from a session in a walled or counts-only class, or in a
  folder no class names, on every route (this machine or a Remote Control bridge). Such a session talks to others
  through `ws msg`. `ws walls check` gains `no-message-crossings`, which fails on any such call a class's verdict
  log shows allowed.
- `ws gate replay --metrics` gives false blocks per 100 calls and exits 1 when check()'s p95 is over a budget
  (`--p95-budget-ms`, default 50).
- `ws queue report [--weekly] [--weeks N]`: a row a week of gate blocks, slot stalls and hand-offs, with the runs
  that finished and those killed by their own cap.
- `ws queue slot` gives each session a share of a lane while another session waits (`--session`, else
  `WS_SLOT_SESSION`, else the runner's session id); a session alone still takes every slot.
- A slot wait past `slot_max_wait_s` (default 30 minutes) is requeued in its place and counted, not failed. The
  queue store is schema 6 (`session`, `requeues`); an older store gains the columns when it opens.
- `ws dispatch REPO PROMPT`: start a runner's session in a new tmux window in a repository's folder, with
  `--name`, `--model`, `--runner` and `--dry-run`. Each flag and the prompt reach the runner as separate arguments.
  The code root is `WS_CODE_ROOT`, else `[dispatch] code_root` in the profile, else `~/code`.
- `ws queue reap`, run by a new reaper timer every 2 minutes (`ws-queue-reap.timer` on systemd,
  `ws.job.queue-reap` on launchd). ws records and tells a run killed with nothing else starting after
  it within minutes, not at the next run.
- A `monthly D HH:MM` schedule (D from 1 to 28), on both schedulers.
- `command = "script"` jobs: `script` names a reviewed script and `script_sha256` its hash. The timer runs
  `ws schedule script JOB`, which refuses a script that changed or that others could write, and runs the bytes it
  hashed.
- The backup job file takes `extra_sources`, more folders or files the restic backup takes.
- Job keys `coalesce_if_active` and `skip_missed`. Today's `ws queue tick` flags are the defaults, and the flags still
  override them for one tick.
- Vale runs before each commit (the upstream `vale` hook at v3.25.1, the version CI downloads), over the same files and
  with the same style as CI's Vale step. It syncs the style package only when it's missing, so a fresh clone passes.
- The e2e first-task script prints the pending systemd jobs and the process list when a SIGTERM reaches it from
  outside, so a CI runner stopped mid-job says who stopped it.

### Changed

- `inbound = "lead-only"` on a walled class lets the lead in and refuses every other sender; before, it refused the
  lead as well. (#262)
- A job timer with an API key or a local provider runs `ws queue tick JOB --slot schedule --run` in place of
  `ws run JOB`. The slot is worked out from the job's own schedule, the latest time at or before the fire, so a late
  catch-up fire gets the slot it was for. A job on a subscription login keeps `ws run JOB`, since the queue never runs
  one.
- Run records carry the runner's own stop subtype (`stop_subtype`: claude's `subtype`, opencode's last finish reason)
  and the approval switch (`approval.switch`, the policy dial). A run whose process died while it held its job lock
  is recorded as the reaper records one: `outcome_unknown`, under its own run id, with its real duration.
- When a drain unit's kill cuts off a queued run it was running, `ws watch alert` lets the queue's notice, which names
  the cut-off job, stand in for the unit's own push. Any other drain failure is pushed as before.
- CI's macOS core-tier time budget is back to 900 s, the same as Linux. A tier over its budget now warns and only a run
  past twice the budget fails, so macOS warns over 900 s and fails over 1800 s, as before.
- The pre-push cap on a capped set grows with the set: 6 s a file, at least 300 s and at most 1800 s, so a large
  selection doesn't hit 300 s with no failure. `WS_PREPUSH_CAP` still sets it, and the timeout message names it.
- The pre-push hook runs the tests through `ws-test --light --mem 3G --node local --` (6G with e2e tests or over 40
  files in the set; `--full` for `WS_PREPUSH_FULL=1`) when
  `ws-test` is on the PATH and `WS_PREPUSH_WRAP` is unset or only spaces, so a busy machine queues the push's tests
  (for up to `WS_TEST_WAIT`, 45 minutes) and caps their workers instead of timing them out. `WS_PREPUSH_WRAP=` (empty)
  runs them bare.
- `config-guard` decides who owns a protected file by the root of the repository the session is in, not by a folder
  prefix: the main checkout and any folder inside it own it; a linked worktree doesn't, even one kept under the owner,
  and a folder outside git doesn't, nor does a repository whose `core.worktree` points elsewhere. If git can't say,
  the session isn't an owner. A folder kept under `.claude/worktrees/`, or under any worktree git lists, never owns.
  An `owners` entry is now the root of a repository's main checkout.

### Fixed

- `ws install` and an upgrade no longer write `permissions.disableBypassPermissionsMode` into your own Claude Code
  `settings.json`: your bypass setting stays as you have it, and `ws install` prints a line saying so. The entry release
  0.1.11 wrote there comes out on the next `ws install`. The settings `ws gate launch` passes and a class's managed
  file still hold bypass off.
- The pre-push hook refuses a push that carries an unsigned commit (`git log --format='%G?'` shows `N`), before the
  tests run, warns on a signature git can't check, and refuses a bad or revoked one or a push whose commits git can't
  list. It does nothing when `CI` is set (one line goes in the skip log).
- The gate: ssh options that run or load local files are now judged (an `-o` keyword ws doesn't know runs nothing
  here is refused, and so are `-F`, `-I`, `-E` and an askpass program), through any wrapper (`sudo`, `timeout`,
  `command`, a full path). Plain options (`-p`, `-l`, `-J`, `-o Port=...`) run as before.
- The gate: a remote command written wholly in single quotes to a host the policy names is read as remote: the gate no
  longer refuses a `$VAR`, loop or `$(...)` in it. A remote command to this machine is judged as a local one.
- Flaky tests: the two orphan tests (`test_msg.py`, `test_session_registry.py`) write their result file whole, through
  a temporary file and a rename. Before, the test could find the file created but still empty, and fail with `''`.
- The launchd scheduler switches off only the agents it loaded. Each plist now carries `WsWrittenBy: ws`, and
  `ws schedule`, `ws uninstall` and `ws schedule apply` boot out a `ws.job.*` label only when `launchctl print` shows
  it was loaded from a plist under `jobs/units/` or one with that key. A label someone else loaded is left alone and
  named in the problems, as the systemd scheduler already does with a unit it didn't write.
- The command reader's time limit counts its own CPU time, not the wall clock, so a busy or slow machine (a CI runner,
  WSL2) no longer refuses an ordinary long command, such as `touch f{1..4000}`, as too slow to read. The wall clock still
  stops a read that waits, at three times the limit.
- Flaky tests: the reveal-link test waits on the push itself instead of 5 s of polling; the tests that check the reader
  is quick measure its CPU time too, as the limit does; and the live conformance row that drives two real runner
  sessions is sized large, like the end-to-end tests it matches, not small (60 s).
- `ws upgrade`'s step watchdog sends KILL to the step's whole process group after the TERM grace even when the step
  itself has already exited, as `ws run` does. Before, a child that ignored TERM, or that the TERM didn't stop, kept
  running after the upgrade. The kill tests for both now wait on the process's exit (a pidfd where there is one), not
  one check or a 5 s poll.

## [0.1.11] - 2026-11-17

### Removed

- `docs/reference/hooks.md`: the hook table is now data, documented with hook parity (#208). Nothing else is removed;
  this release adds 18,597 lines net (19,001 in, 404 out, 30 commits since 0.1.10).

### Added

- A person can send a `ws msg` from the hub or a plain terminal, recorded as `person:<user>`, behind `[msg] person_send`
  (off by default) and a per-boot token under the secrets folder (`ws msg person-token`); an agent that escapes its
  process ancestry has no token and is refused. The launcher step that makes a session's launch token records who asked
  for it (`requested_by`), and `ws session show` prints it. See ADR 0023.
- `ws ask` is one queue of questions from every session to their person: `add`, `list`, `get`, `answer`, `wait`,
  `done`, `withdraw` and `import` (an older asks file, safe to run twice), in the file `ws home` already reads asks
  from. An ask whose repo isn't in a full-access class, or that hits the sensitive-terms file, is stored with no text;
  a token-shaped secret is refused; the class is checked again on every read. See [Asks](docs/reference/asks.md).
- `ws ask note ID TEXT` appends a note to an open or answered ask (a new `noted` event; earlier text is never
  rewritten), with the checks a question gets: a secret is refused, and a note on a redacted ask, or with a sensitive
  term, keeps no text. Notes show in `ws ask get` and `ws ask list`, JSON included.
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
- The gate's decision log (`logs/gate.jsonl`, or a class's own copy) has a line for every call's check, an allowed one
  included, with the tool's name. A refusal's line names the rule or layer that said no in `rule` (for example
  `rule:command` or `guard:claims`), never the reason or the command. A tool no adapter reads is counted as a write
  (`counted_as: "write"`). A check that fails before the policy loads still leaves a deny line (rule `error`, with no
  session id or tool name), a guard that speaks after a call names itself, the live file turns over at 5 MiB instead of
  1 MiB, and a hook waits at most 0.2 seconds for the log's lock.
- The pre-push hook runs the suite through `WS_PREPUSH_WRAP` when it's set (a command that queues test runs on a busy
  machine), so a push's tests don't race the others.
- The pre-push hook runs the tests a change can affect (`scripts/affected_tests.py`), not the whole suite, which CI runs
  and requires. A big or reach-everything change runs a capped set (300 s); `WS_PREPUSH_FULL=1` runs everything.

### Changed

- A walled class's launch file also blocks file-tool writes outside the working folders, denies the four sign-in token
  variables to shell commands, and sets `disableRemoteControl` and `disableClaudeAiConnectors`. The managed-settings
  file is unchanged. Print the launch file again with `ws install --class NAME` and put it in place.
- A push alert from a session in a walled or counts-only class is always the plain line ("a session needs you"), with
  no folder name and none of the runner's words, whatever `alert_text` says.
- In the decision log, a refusal the wall makes redundant, refused because the wall wasn't proven, now has the layer
  `rule:unknown-command-word`, `rule:sets-ifs` or `rule:unread-write` where it was `rule:command`.
- A test tier over its time budget now warns (a summary line, and a `::warning::` on GitHub Actions); only a tier past
  twice its budget fails the run in CI, so a slow runner can't turn a green suite red.

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

[Unreleased]: https://github.com/Michael-A-Fry/ws/compare/v0.1.11...HEAD
[0.1.11]: https://github.com/Michael-A-Fry/ws/compare/v0.1.10...v0.1.11
[0.1.1]: https://github.com/Michael-A-Fry/ws/commits/v0.1.1
