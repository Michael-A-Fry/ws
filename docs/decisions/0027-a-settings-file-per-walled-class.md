---
status: accepted
date: 2026-11-12
decision-makers: maintainer
---

# A launch file per walled class, beside an unchanged managed-settings file, with managed-only keys left out

## Context and Problem Statement

A walled class (a `[classes.NAME]` with an `os_user`) is meant to be an operating-system boundary: its own account,
folder modes, and the runner's own settings. The class's settings go in a managed-settings drop-in
(`/etc/claude-code/managed-settings.d/50-ws-class-NAME.json`), which Claude Code reads for every account on the machine,
so a bare `claude` run as the class account is bound by it. That file works and is relied on. The question is where to
put the settings it can't hold: keys that are right for one class's sessions and wrong in a file every account reads
(denying `SendMessage` and `ListAgents`, `crossSessionInbound`), and keys that name another class. They need a place
that only that class's sessions get, using only keys Claude Code honours from where ws puts them, and a way to prove it
holds.

## Considered Options

- Keep the managed drop-in, and add the new keys to it. - Keep the managed drop-in exactly as it's, and add one
  root-owned launch file per class in a folder Claude Code doesn't read, which `ws gate launch` merges into the
  per-project launch copy it passes with `--settings`. - Move everything to the per-class launch file and stop writing
  the managed drop-in. - Pass two `--settings` flags, the launch copy and the class file.

## Decision Outcome

Chosen option: keep the managed-settings file unchanged, and add one launch file per class in a `ws-classes` folder
beside the managed-settings folder, merged into the launch copy.

The docs were checked on 2026-11-12 (the Claude Code settings reference,
<https://code.claude.com/docs/en/settings-reference>, the settings page <https://code.claude.com/docs/en/settings>, the
managed settings page (<https://code.claude.com/docs/en/managed-settings>, "Keys only a managed source can set"), the
hooks reference ("Matcher patterns") and the cross-session messaging page
(<https://code.claude.com/docs/en/cross-session-messaging>)). What they say, and what we rely on:

- Claude Code reads managed settings from `managed-settings.json` and the `managed-settings.d` folder, and nowhere else.
  A `ws-classes` folder beside them isn't read, so a launch file there reaches no session until `ws gate launch` merges
  it. The managed-settings file is read whatever starts Claude Code, so it holds what must hold for a bare `claude` run
  as the class account: the sandbox required, no unsandboxed retry, bypass off, writes in the class's repos and scratch
  only, the home folder unreadable except those repos, the network shut but for the class's hosts, and inbound
  lead-only. It's the file 0.1.9 printed, key for key, and `ws install --class NAME` still prints it the same way. -
  `--settings` takes one file (or inline JSON). The docs show no way to pass two, so ws merges: `wallsettings.merge`
  writes the launch file's keys over the launch copy's (objects key by key, lists added to), and the result is written
  whole or not at all, as every launch copy is. - `crossSessionInbound: "refuse"` (Claude Code 2.1.224 or later) drops
  what other sessions send, honoured from a `--settings` file. Denying `SendMessage` and `ListAgents` in
  `permissions.deny` is the control that takes the tools away; `isolatePeerMachines: true` (any file, 2.1.224) only asks
  before a cross-machine send, so it's a belt and not the control. - `OTEL_LOG_USER_PROMPTS`, `OTEL_LOG_TOOL_DETAILS`
  and `OTEL_LOG_TOOL_CONTENT` are set to `0` in the launch file's `env`. A project file may only turn them off too, so
  they can't be turned on from the repository. - `sandbox.network.strictAllowlist` is honoured from `--settings`; the
  class's hosts are written as `allowedDomains`.

**What the launch file holds**, and nothing else: `permissions.deny` (`SendMessage`, `ListAgents`, and `Read` and `Edit`
of the sealed paths: the home folder, ws's own folders as `ws.paths` names them, and the other classes' repos),
`permissions.blockReadsOutsideWorkingDirectories`, `crossSessionInbound` `"refuse"`, `isolatePeerMachines` true,
`disableAllHooks` false, `sandbox.filesystem.denyRead` of the other classes' repos, `sandbox.network` with
`strictAllowlist` and the class's `allowedDomains`, and `env` with `WS_WALLED_CLASS` and the OpenTelemetry content logs
off. A launch with no launch file is refused (exit 3, kind `refused`, naming `ws install --class NAME`).

**Keys Claude Code reads only from managed settings, which ws doesn't write to the launch file.** In a `--settings`
file they do nothing:

| Key | What ws does instead |
|---|---|
| `allowManagedHooksOnly` | the launch check refuses a project hook before the runner starts, and the hook refuses on every call |
| `allowManagedPermissionRulesOnly` | the same check refuses a project `permissions.allow`; the launch file's deny rules are merged in |
| `sandbox.network.allowManagedDomainsOnly` | `strictAllowlist`, and the check refuses a project `network` key |
| `sandbox.filesystem.allowManagedReadPathsOnly` | the check refuses a project `allowRead` |

The managed-settings file is unchanged from 0.1.9, so it keeps the one managed-only key it has always had
(`allowManagedDomainsOnly`). The other three are written nowhere: `allowManagedHooksOnly` in the machine-wide file would
switch off each account's own hooks. `ws install` never writes either file: a person with sudo places them.

### The launch check, an allow-list

A project's `.mcp.json`, `.claude/settings.json` and `.claude/settings.local.json` (in the folder, any above it, and the
main checkout when the folder is a git worktree), and the skills, agents, commands and workflows under its `.claude`
folder, are read by one rule, `wallsettings.project_problem`, and it's an allow-list, so a key Claude Code adds later is
refused until it's named. A `.mcp.json` may start no MCP server. A settings file may hold only these top-level keys: the
schema key, `model`, `permissions` (only `deny`, `ask`, and a `defaultMode` of `plan` or `default`), `attribution`,
`includeCoAuthoredBy`, `outputStyle`, `language`, `cleanupPeriodDays`, `respectGitignore`, and `sandbox`, whose table may
only narrow (below). `env` may name only `NODE_ENV`, `TZ`, `LANG`, `LC_ALL`,
`DISABLE_TELEMETRY`, `DISABLE_AUTOUPDATER`, `BASH_DEFAULT_TIMEOUT_MS`, `BASH_MAX_TIMEOUT_MS` and `MAX_THINKING_TOKENS`,
exact names. Anything else is refused with the key named in the hint (a hook, `disableAllHooks` of any value, a plugin
or plugin source, `fileSuggestion`, a command setting such as `statusLine`, an `env` name such as a base URL, a proxy,
`BASH_ENV`, `LD_PRELOAD` or `NODE_EXTRA_CA_CERTS`). There is one rule and no list of named denials beside it: what's
not on the list is refused. The names were checked against the settings and environment-variable references above.

A project's `sandbox` table may only add denials, and each key is judged by value (`_sandbox_adds`): `enabled` and
`failIfUnavailable` only true, `allowUnsandboxedCommands` and `autoAllowBashIfSandboxed` only false, `filesystem.denyRead`
and `denyWrite` and `network.deniedDomains` only as lists of names, and `credentials` entries only in `deny` mode. Every
other key is refused whatever its value: `excludedCommands`, `ignoreViolations`, `allowRead`, `allowWrite`,
`allowedDomains`, sockets, proxy ports, `enableWeakerNestedSandbox`, `enableWeakerNetworkIsolation`,
`allowNestedUserNamespaces` and any key added later. The launch file sets the two `enableWeaker*` keys false as well, and
`ws walls check` proves both in the case `sandbox-only-narrows`. The names were checked against the sandboxing reference
(<https://code.claude.com/docs/en/sandboxing>).

A skill, agent or command carries YAML frontmatter, and the rule reads its top-level keys with a small strict reader
(`wallsettings.frontmatter_keys`; `.claude/skills/**/SKILL.md`, `.claude/agents/**/*.md`, `.claude/commands/**/*.md`).
It refuses `hooks` in any of them, `mcpServers` and `permissionMode` in an agent, and `allowed-tools` in a skill or a
command, naming the file and the key. Any `.claude/workflows/*.js` is refused. The reader fails closed: a header with no
closing fence, a CRLF line end, a quoted or odd key or an indented line under no key is a refusal, never a skip. So is
any project file that exists but doesn't read as Claude Code would read it: a settings or `.mcp.json` file that's
empty (a `.mcp.json` of `{}` is fine), not a JSON object or not valid JSON, read with a byte order mark dropped; a link
among the files; a `.git` file or a worktree link it can't follow. An absent file is fine. A skill's `!command` body
isn't read, so it stays allowed: the sandbox and the session's permission rules hold it, and `allowed-tools` is
refused so a skill can't pre-approve it (see [Known limits](../explanation/known-limits.md)).

A SessionStart hook can't block, so the refusal is made twice: by `sandbox.launch_claude` before the runner starts (a
typed error, kind `project_load_refused`, exit 3, whose hint names the file and what to delete), and by
`claude_code.preflight_refusal` on every call, which the launch file switches on with `env.WS_WALLED_CLASS`. That value
reaches the hook through the `--settings` file, which a project's settings can't outrank.

`disableAllHooks` is the one that matters most: a project that set it would switch off the gate hook, and with it the
per-call check, so the per-call check alone couldn't catch it. Two things hold it. The allow-list refuses the key
before the runner starts, and the launch file sets `disableAllHooks: false`, which a `--settings` value outranks over a
project's (precedence: managed, then `--settings`, then local, then shared project, then user). `ws walls check` proves
both in the case `hooks-not-disabled`.

### Where the class and the user come from

The class is `policy.class_of` of the real path of the folder the session starts in, and the user is
`sandbox.real_user()`. `$USER`, `$HOME`, a `WS_*` variable, a flag or a project file never choose either. A launch reads
its own class's launch file only, at `wallsettings.launch_file_path`, the one function the launch, `ws walls check` and
`ws install --class` all read; a session of another class and a session of none get none of it. WS014 and WS015 read the
managed-settings file at `wallsettings.class_file_path`, as they did. A launch file that's not owned by root, or is
writable by group or others, or sits in a folder like that, is refused at launch; the managed-settings file is a WS014
`fail` for the same reasons.

### One class per machine

One walled class per machine is the tested shape. Claude Code reads every file in the managed-settings folder for every
account, so the managed-settings files of two classes on one machine are read together, and ws doesn't test that.

### The hook on every tool

The PreToolUse gate hook is registered with the matcher `*`, which the hooks reference gives as matching every tool,
so a tool the runner adds is gated without a list to keep. `claude_code.parse` returns None for a tool it doesn't
know, which the gate allows; the hook still runs and denies under `bypassPermissions` and `auto`, because the gate reads
the call and never the mode. Hook latency now counts for every call.

### Consequences

- Good, because a bare `claude` run as the class account is bound as it was, and a session started with `ws gate launch`
  is bound by the launch file as well. - Good, because a session of one class, or of none, is never given another
  class's launch file. - Good, because ws writes and checks keys and enforces nothing of its own: `ws walls check`
  checks that the settings are in place, without sudo. - Good, because the keys that couldn't be written machine-wide
  can change without touching a file every account reads. - Bad, because the managed-only keys above aren't written to
  the launch file, so a project that adds a hook or an MCP server is refused by ws's check and not by Claude Code
  itself; the check is an allow-list and is only as current as the names on it. - Bad, because a launch file's content
  is read at launch, so a change to the file takes effect at the next start. - Bad, because the OpenCode wall doesn't
  give a session the read rules of the launch file (its wall hides the `deny_read` paths only); for reads the class's
  own account and folder modes are the boundary.

### Confirmation

`tests/test_walls.py` (a mutation test for each named case of `ws walls check`, init, edit, the not-set-up exit, the one
path, the review clock), `tests/test_wallsettings.py` (the managed file key for key, the launch file's keys, no
managed-only key in it, the merge, alpha's file absent from beta's, the launch refusals, the allow-list, WS014 and WS015
reading the managed file, the removal steps run through `bash -n`), `tests/test_policy_classes.py` (owner, review date,
per-repo narrowing, `repo_settings` needing an `os_user`, whole-file refusal), `tests/test_claude_code_matcher.py`
(every tool, every mode) and `tests/test_output_contract.py`.
