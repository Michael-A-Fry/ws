---
status: accepted
date: 2026-11-12
decision-makers: maintainer
---

# Adopt the runner's own sandbox and permission keys, and write no wall of our own for Claude Code

## Context and Problem Statement

A walled Claude Code session has three layers: the permission rules and hook (what Claude's own tools may do), the
sandbox (what a shell command's programs may do) and the network list. Claude Code documents settings keys for each,
and ws had adopted some (`sandbox.enabled`, `failIfUnavailable`, `allowUnsandboxedCommands`, `filesystem.denyRead`,
`network.allowedDomains`, `permissions.disableBypassPermissionsMode`) and left others. Three gaps remained: Claude's
Read, Grep and Glob tools could read any path the policy didn't deny, an unattended `claude -p` was refused the hosts
nearly every project needs (the package indexes, the forges) because the policy named none, and nothing showed the
sandbox or the permission mode in force. Writing a parser or a control of our own for any of it would be new code that
has to be kept in step with the runner.

## Considered Options

- Write ws's own read check and network filter for Claude Code.
- Adopt the runner's documented keys: `permissions.blockReadsOutsideWorkingDirectories`, a default
  `sandbox.network.allowedDomains`, and report what the settings files say.
- Leave it: the person sets these keys themselves.

## Decision Outcome

Chosen option: adopt the runner's keys, and add no new parser or control code.

- `permissions.blockReadsOutsideWorkingDirectories: true` goes into the launch settings and the class's managed file,
  never into a person's own settings file (it would bind every session they start). One `[gate]` bool,
  `read_outside_project`, turns it off. Claude Code applies a `true` from any settings file, so a project's settings can't
  lift it. It needs Claude Code 2.1.257, so `ws doctor` WS016 reads one table of key to first version
  (`wallsettings.MIN_CLAUDE`) and warns when the installed runner is older. The e2e test skips from the same table.
- When the policy names no `allow_hosts` and the sandbox is on, the block carries a short generic list: the runner's API
  host, PyPI, npm and crates.io (`claude_code.DEFAULT_DOMAINS`). The forges (GitHub and GitLab,
  `claude_code.FORGE_DOMAINS`) are added only to a session started with `ws gate launch` (`for_project`, `forges=True`),
  where a person is at the terminal and the policy names no `allow_hosts` and no class. A headless job (`ws run`)
  reads the
  generated list as it stands and so has no forge: a prompt-injected job that could reach one could push the project's
  files to a repository the attacker owns, and a job rarely needs more than the package indexes. A job that does needs
  `allow_hosts`. It only pre-allows: another host still asks, or is refused in `-p`. `allow_hosts` replaces it, a
  class session carries its class's list and never this one, and
  a policy with `sandbox = false` (ADR 0024) gets no block and no list. The list isn't merged into a person's own
  settings file: Claude Code concatenates array settings across scopes, so a default host there would add itself to a
  class session's list (the class list replaces only the `--settings` copy).
- `ws status` reads, and never writes, the sandbox and `permissions.defaultMode` in the settings files; WS016 warns when
  the mode is `bypassPermissions`.
- Every refusal ws raises about the wall is a typed error with a kind and a hint that names the file and key to change or
  the command to run. No one branches on message text.

### Swapping the runner's sandbox for another

The wall for Claude Code is one function (`claude_code.sandbox_block`), a table (`wallsettings.MIN_CLAUDE`), the class
file (`wallsettings.class_settings`) and the reader for `ws status` and WS016 (`status.sandbox_state`). To replace Claude
Code's sandbox with another (a container, or a different sandbox runtime): write the new block in `sandbox_block` and
keep the marker (`WS_SANDBOXED`) so the hook and the installer still know whether a wall is up; give the new tool's keys
their own rows in the version table; point `status.sandbox_state` at where it keeps its settings; and keep the three
tests that pin the contract (the block is absent when `sandbox = false`, a class list replaces the default, the user-file
merge copies only the keys it names). About one day of work, most of it the new sandbox's own e2e test.

### Consequences

- Good, because the wall is the runner's own documented behaviour, and what ws adds is a few keys and one reader.
- Good, because file reads by Claude's own tools stop at the working folders by default, in every permission mode.
- Good, because `claude -p` reaches the package indexes without a policy line, and a session at a terminal reaches the
  forges too.
- Bad, because the default hosts are one more list to keep right; a person who wants fewer names every host in
  `allow_hosts`.
- Bad, because a headless job that clones or pulls from a forge is refused until `allow_hosts` names it. That's the
  price of keeping a forge out of reach of a prompt-injected job; the session at a terminal keeps the forges because a
  person sees what it does.
- Bad, because the plain generated file (`ws gate generate`, used for `claude --settings` without `ws gate launch`) has
  no forge either, since it's the one list headless jobs read.
- Bad, because the read block is the runner's: below Claude Code 2.1.257 the key does nothing, and WS016 is the only
  signal.

### Confirmation

`tests/test_wallsettings.py` (the key in the launch settings and the class file, the off switch, the WS014 warning),
`tests/test_userconfig_sandbox.py` (the key never merged into a person's file; the default domains never merged, a
person's own hosts appended and removed on undo; none with the sandbox off; a class session with a merged user file and
its own list), `tests/test_sandbox.py` and `tests/test_class_providers.py` (the default list without the forges, the
forges added at launch only when the policy names no hosts, and a class never
carrying either), `tests/test_doctor_wall_keys.py`, `tests/test_status_sandbox.py`, and
`tests/e2e/test_sandbox.py` (a real `claude -p` is refused a Read outside the working folder with the key on and allowed
with it off).
