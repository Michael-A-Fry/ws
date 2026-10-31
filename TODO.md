# TODO

## Phase 0 (repo setup)

- [x] Public repo, Apache-2.0 + NOTICE, squash-only, auto-delete head branches
- [x] Secret scanning + push protection, Dependabot alerts and security updates, immutable releases
- [x] SSH signing key on GitHub; local commits Verified (checked on 8a0064b), squash merges signed by GitHub
- [x] Rulesets: `main` (no force push, no delete, linear, signed) and `v*` tags (no update/delete); no bypass; `ci` required
- [x] CLA bot (CLA Assistant, hosted) linked to CLA.md: the `license/cla` check appeared on PR #5 (not a required check)
- [x] `ws-aitoolkit` 0.0.0 on PyPI and npm (#175); `ws-toolkit` held on PyPI, never released

## Phase 1 (hygiene gate)

- [x] prek + gitleaks + PII regex + private denylist secret; `ci.yml` as the required check (planted key, IP and name each
  failed CI: PRs #2-#4, closed)
- [x] actionlint hook (a workflow YAML error reached CI once)
- [ ] ShellCheck and taplo hooks when the first shell file or a hand-edited TOML lands; licence check with the first
  runtime dependency (the core has none)

## Phase 2 (CLI skeleton, doctor)

- [x] CLI skeleton, `ws doctor` (check IDs, `--json`), OS detection, the 3 questions (`ws init`), Console usage API check
  (WS110; needs an Admin API key, which Anthropic gives organisations only)
- [x] `doctor` exits non-zero listing every role unconfigured on ubuntu-24.04, windows-2025 (WSL) and macos-26
  (`cli.yml` on 778cfdf; native Windows fails WS001 and points at WSL2)

## Phase 2b (globals stay global, #178)

- [x] Shadowed-globals check: `ws doctor` WS005 + a prek hook in `ci`; a repo copy of a global skill, agent, workflow
  or hook fails unless listed in `.ws/overrides.toml` with a reason; listed overrides are reported
- [x] Stale-session nudge: a session records its ws release; after a newer one it gets one line, once
- [x] Pass test: a planted shadow fails; a listed override passes and is reported; an old-release session is nudged once
  (tests/test_globals.py, tests/test_session.py; PR #10)

## Phase 3 (core)

- [x] Policy schema + generated gate for Claude Code and OpenCode (PR #9): native deny lists, a fail-closed hook and an
  OpenCode shim; self-protection of `$WS_HOME`; Rule of Two with named exceptions; known matcher limits in policy.py
- [x] Secrets store + broker (PR #8): per-run leases to a reverse proxy that injects at the allowed host; `ws secret wall`
  moves the store to its own user with a root-owned copy of ws
- [x] `ws run` + `runs.jsonl` (PR #11): scrubbed env, broker leases, budget cap, Rule of Two, one record per run
- [x] Memory + Notes modules: front matter, `ws note`, `ws memory` (git + FTS5 index), `ws mcp serve`; WS105/WS106
- [x] Pass tests in CI (`cli.yml`): `gate e2e` (denied command blocked on Claude Code and OpenCode, stub model),
  `run record e2e` (record complete against the schema, both runners), `secret wall` (no secret readable by `ws-dev`)
- [ ] Secrets wall on Michael's machine: at the phase 6 cut-over (`ws secret wall`, one sudo script, re-paste keys)
- [ ] Trust on first use of the root-owned /opt/ws copy: closes at phase T (install from the signed tag)
- [ ] Sandbox as the real wall behind the command matcher (phase 4); bound single-use approvals (phase 4)
- [x] WS110 usage: off (Michael: "leave it off"); `adapter = "none"`, spend comes from `runs.jsonl`
- [x] CLA signed by Michael (`license/cla` passes on PR #6)

## Phase 4 (setup, adapters, public skills, the wall)

- [x] Installer engine: `ws install` / `ws uninstall` with a manifest; a second install reports 0 changes, uninstall
  returns HOME to its pre-install state (tests/test_install.py, 3-OS matrix; PR #12)
- [x] Plugins by checkbox (#163, #164, #180): `ws plugin list|add|remove`; Finance is NZ-only; Obsidian/Logseq folder
  detection; `ws device add` (PR #12, #14)
- [x] Public skills align, checkpoint, taste-gate, dataviz, scrubbed (#165); `doctor` records runner versions and runs
  `ws canary` on a change; `ws rate <run> up|down` beside runs.jsonl (#162) (PR #14)
- [x] Role adapters anthropic, opencode, markdown-git, systemd, launchd, ntfy, restic, sops-age (PR #15)
- [x] The wall (PR #13): bound single-use approvals, a write-allowlist sandbox behind the matcher (bwrap + seccomp on
  Linux, sandbox-exec on macOS, Claude Code's native sandbox), OpenCode nudge e2e; three Opus cold-review rounds
- [x] Setup (PR #17): inline terminal setup and a localhost web page in the approved look (#182), wired to `ws install`
- [x] From the claw-code assessment (#183, #184): repo AGENTS.md with a generated code map, install.sh, MIT notice
  (PR #18); one CLI contract with typed errors, hints, `ws config`, `ws status` (PR #19)
- [x] Pass test on main: `ws setup --yes` twice reports 0 changes; `ws uninstall --yes` leaves 0 paths behind
- [ ] **Before the tag:** the wall's open class (known limits in policy.py): OpenCode cache binaries writable inside
  the wall; the project lock misses not-yet-existing or renamed `.git/hooks`, `.claude`, `.opencode`
- [ ] Known limit: the notify cap counts ws's own sends only; count notify leases in the broker
- [ ] Known limit: sops-age is named but not read by the broker yet
