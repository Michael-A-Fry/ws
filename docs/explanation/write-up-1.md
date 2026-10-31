# Write-up 1: What phases 0 to 5 taught

This is the first learning write-up. ws is built as a thin toolkit in phases, and each phase ends with a pass test that
runs in CI. This page says what was built, what each phase taught, what failed and why, and what comes next. Every claim
links to a merged pull request, so you can check it. Numbers are from the pull request bodies and from `TODO.md`.

All 22 merged pull requests (#1 to #25, less the three closed test pull requests) landed on October 31, 2026, between
00:44 and 13:22 UTC. They add about 42,800 lines. On the commit that closed phase 5, the suite ran 2,368 tests with 40
skipped (the skips are end-to-end tests that need a real runner, and platform-specific ones).

## What was built

| Phase | Built | Pull requests |
|---|---|---|
| 0 | A public repository: Apache-2.0, signed commits, squash-only merges, rulesets on `main` and `v*` tags, secret scanning | no pull request ([`TODO.md`](../../TODO.md)) |
| 1 | The hygiene gate: gitleaks, a PII gate, ruff, typos, rumdl, actionlint as the required check `ci` | [#1](https://github.com/Michael-A-Fry/ws/pull/1) |
| 2 | The CLI skeleton and `ws doctor` | [#6](https://github.com/Michael-A-Fry/ws/pull/6) |
| 2b | Globals stay global: a shadow check and a stale-session nudge | [#10](https://github.com/Michael-A-Fry/ws/pull/10) |
| 3 | The core: memory and notes, the secrets broker, the policy and gate, `ws run` and the run record | [#7](https://github.com/Michael-A-Fry/ws/pull/7), [#8](https://github.com/Michael-A-Fry/ws/pull/8), [#9](https://github.com/Michael-A-Fry/ws/pull/9), [#11](https://github.com/Michael-A-Fry/ws/pull/11) |
| 4 | The installer, role adapters, skills, the wall, approvals, setup, one CLI contract | [#12](https://github.com/Michael-A-Fry/ws/pull/12) to [#19](https://github.com/Michael-A-Fry/ws/pull/19) |
| 5 | The mock provider, bounded logs, the eval engine, the wall's open class, the launch matrix | [#21](https://github.com/Michael-A-Fry/ws/pull/21) to [#25](https://github.com/Michael-A-Fry/ws/pull/25) |

## What each phase taught

### Phase 1: Prove the gate before you rely on it

Pull requests [#2](https://github.com/Michael-A-Fry/ws/pull/2), [#3](https://github.com/Michael-A-Fry/ws/pull/3) and
[#4](https://github.com/Michael-A-Fry/ws/pull/4) were never merged. Each planted one thing the gate must refuse: a key, a
private IP and a personal name. Each failed `ci`, and each was closed. A check you haven't seen fail is a check you
haven't tested. A workflow YAML error reached CI once before the actionlint hook caught the next one.

### Phase 2: A skeleton with stable IDs is worth building first

[#6](https://github.com/Michael-A-Fry/ws/pull/6) gave `ws doctor` one line per check, each with an ID that never changes
meaning. Every later phase added a check instead of a new report. The pass test ran on Ubuntu, macOS and Windows with
WSL2, and showed that native Windows fails WS001 and points at WSL2.

### Phase 3: Make the dangerous parts boring and small

- **The broker** ([#8](https://github.com/Michael-A-Fry/ws/pull/8)) isn't a man-in-the-middle proxy. Tools that
  intercept TLS need a trusted certificate authority, and one had no Windows path. Both runners honour a base URL, so a
  lease gives the tool a local URL and the broker adds the key at the allowed host.
- **The policy** ([#9](https://github.com/Michael-A-Fry/ws/pull/9)) is one neutral file that becomes each runner's own
  rules. Neither runner's native list could express a literal `*`, so two rule kinds live in the hook or shim only.
- **`ws run`** ([#11](https://github.com/Michael-A-Fry/ws/pull/11)) writes one record per run and refuses, with a record,
  when there is no cap, the cap is reached or the Rule of Two says no.

The lesson is that each safety rule is small enough to state in one sentence and to test with one planted failure.

### Phase 4: A text matcher isn't a wall

The matcher reads command text, so an interpreter, a built path or a restored file gets past it. [#13](https://github.com/Michael-A-Fry/ws/pull/13)
added bound, single-use approvals and a sandbox behind the matcher, and an end-to-end test that shows each known
matcher bypass stopped by the wall alone, on both runners. It took three Opus cold-review rounds.

Other lessons from this phase:

- A third-party guard (custodian-claude-guard v0.1.0) was trialled and not adopted: it covers one runner, and ws is
  runner-neutral. The reasons are in `src/ws/approvals.py`.
- One output contract ([#19](https://github.com/Michael-A-Fry/ws/pull/19)) came from reading a reference project: a
  JSON envelope, typed errors with hints, and "did you mean." Errors are never classified by matching their message.
- Setup ([#17](https://github.com/Michael-A-Fry/ws/pull/17)) passed its own pass test: run twice, it reports 0 changes.

### Phase 5: A list of paths is always one path short

[#23](https://github.com/Michael-A-Fry/ws/pull/23) closed the wall's open class: something written inside the wall that
runs later, outside it. The root cause was that the wall was a list of paths. The fix started with a tool: a table of
what Claude Code, OpenCode and git load at start, with a control row for each. Run against the old wall, 46 rows failed.
Then the wall was rebuilt around the class, with private data and cache folders per project, a git-aware project lock,
and an end-of-run report for what a list can't name. Three Opus cold-review rounds were needed.

[#21](https://github.com/Michael-A-Fry/ws/pull/21) found that a planted `ws/` package in the working folder could replace
the gate hook, because ws started itself with a plain `-m ws`. Every self-start now runs `python -I -m ws`, and a source
scan fails any new plain one.

[#22](https://github.com/Michael-A-Fry/ws/pull/22) made evals per person: eight synthetic starter tasks, ten metrics
where a metric with no data says "not measured," and an exact McNemar test paired at task level, because trials of one
task aren't independent draws.

[#24](https://github.com/Michael-A-Fry/ws/pull/24) timed the person's path, install to first task to return. The first
task takes about 5 seconds on Ubuntu, 6 on macOS and 8 on WSL2. It also found a real bug: a job with no `cwd` ran in a
folder that the wall makes read-only.

## What failed, and why

| What | Why | What changed |
|---|---|---|
| Native Windows broke on import three times | A module used a POSIX-only name at import time (`fcntl`, `os.O_NOFOLLOW`) | `tests/test_windows_import.py` imports every module with those names removed and fails on the first that dies |
| The WSL2 cell went red on `main` after [#24](https://github.com/Michael-A-Fry/ws/pull/24) ([#25](https://github.com/Michael-A-Fry/ws/pull/25)) | That cell runs the tests as root, and root reads `/proc/PID/mem` whatever `ptrace_scope` says | The test's expectation changed, and the limit is now documented |
| The combined tree of two back-to-back merges was untested | [#23](https://github.com/Michael-A-Fry/ws/pull/23) and [#24](https://github.com/Michael-A-Fry/ws/pull/24) each passed alone | After two merges land together, run `cli.yml` on `main` and wait |
| A rebase dropped commit signatures | The ruleset requires signed commits, and a rebase rewrites them unsigned | Re-sign with `rebase --exec` |
| Shell scripts got CRLF line endings in a Windows checkout | No `eol=lf` rule | `.gitattributes` sets it |
| The phase 5 cost 5 million tokens against an estimate of 2 million | Each security writer took 2 fix rounds, and a resumed writer carries a big context | Use a fresh writer with a short brief for a fix round |

## Numbers

- 22 merged pull requests, 3 closed on purpose (the planted failures).
- About 42,800 lines added.
- 2,368 tests passing and 40 skipped at the end of phase 5.
- First task timed at about 5, 6 and 8 seconds on Ubuntu, macOS and WSL2.
- Three Opus cold-review rounds on each of the wall ([#13](https://github.com/Michael-A-Fry/ws/pull/13)) and the open
  class ([#23](https://github.com/Michael-A-Fry/ws/pull/23)); two on [#21](https://github.com/Michael-A-Fry/ws/pull/21).
- Runner versions pinned in the end-to-end tests: Claude Code 2.1.308 and OpenCode 1.19.4.

## What's still open

These are in [Known limits](known-limits.md): the macOS wall is untested on a real Mac, `/proc/PID/mem` is readable at
`ptrace_scope` 0, the lab cell hasn't run, `sops-age` isn't read by the broker, and draft-only sending isn't enforced.

## What's next

Phase T is the v0.1 tag: a release workflow with a checksum, a software bill of materials, build provenance and a cosign
signature for each file; `ws version --json`; these docs; and this write-up. The pass test is a fresh install from the tag
on CI and the lab, twice. After the tag, phase 6 moves the secret wall onto a real machine.
