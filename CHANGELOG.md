# Changelog

All notable changes to ws are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The release workflow reads the section whose heading matches the tag (`## [0.1.1]` for `v0.1.1`) and uses it as the
body of the GitHub release.

## [Unreleased]

### Fixed

- The README on PyPI links to the docs, changelog and licence on GitHub at the release's tag; relative links there
  went to a 404. The release fails if a built file's description still holds a relative link or image.
- PyPI lists Documentation, Changelog, Issues and Security links beside Source.

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
