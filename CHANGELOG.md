# Changelog

All notable changes to ws are written here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/)
and the version numbers follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

The release workflow reads the section whose heading matches the tag (`## [0.1.0]` for `v0.1.0`) and uses it as the
body of the GitHub release.

## [Unreleased]

## [0.1.0] - unreleased

The first thin tag: phases 0 to 5 of the plan, installed from the signed release.

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

[Unreleased]: https://github.com/Michael-A-Fry/ws/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/Michael-A-Fry/ws/commits/v0.1.0
