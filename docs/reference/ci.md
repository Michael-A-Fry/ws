# CI

What `.github/workflows/ci.yml` runs, how long each job may take, and the two lines a pull request body needs when it
changes the tests. The suite itself (sizes, tiers, quarantine) is in [Testing](testing.md). The other workflows
(`cli.yml`, `install-test.yml`, `lab.yml`, `release.yml`) are not covered here.

## The jobs

| Job | Runs on | What it does | Limit |
|---|---|---|---|
| `ci` | every push to `main` and every pull request | Test-integrity check, gitleaks, PII gate, hooks (ruff, typos, rumdl, actionlint, zizmor, pinact), docs integrity, Vale, gate replay, then the suite without the two oracle files. `tests/holdout/` runs with the suite. | 15 min |
| `oracle` | the same | `tests/test_gate_oracle.py` and `tests/test_shellwords_oracle.py`: the gate and the command reader against a real bash, the gate one inside bubblewrap. `WS_ORACLE_REQUIRE=1` turns a sandbox that can't start into a failure. | 20 min |
| `node` | the same | `tests/test_node.py` with a container: an image pulled by digest and used locally (`WS_TEST_NODE_IMAGE`). `WS_TEST_NODE_REQUIRE=1` turns a missing runtime or image into a failure instead of the fallback to the host. | 10 min |
| `term-browser` | the same | Every `tests/e2e/test_*_browser.py` with Chromium. `WS_E2E_REQUIRE=1` turns a missing browser into a failure. | 15 min |
| `nightly-quarantine` | a daily schedule, and by hand (`workflow_dispatch`) | `pytest -q -rs --run-quarantine -m quarantine`: only the tests marked `quarantine`. Pytest exits 5 when nothing is collected, which here means an empty quarantine, and the job treats that as a pass. | 30 min |

The other four jobs have `if: github.event_name != 'schedule'`, so the schedule runs only the nightly one.

Bubblewrap is installed in `ci` as well as in `oracle`: `tests/test_sandbox.py` and the eval tests use it and skip
quietly without it.

The oracle and node jobs don't gate a merge until each is added as a required check in the repository's branch rules.
That is a repository setting, not a file. `ci` is already required, so the test-integrity check and the holdout, which
sit inside it, run on every pull request. Neither can stop a pull request that sets out to get past it: on a
`pull_request` event GitHub runs the workflow and the script from the pull request's own tree, so a change can edit
them, and the `test-change:` line is written by the author. Read them as a check on honest mistakes, and review changes
to the files they watch.

## The time budget

Each job's first step writes the time it started into `$RUNNER_TEMP/job-start`. Its last step (`if: always()`) runs

```sh
python3 scripts/ci_time_budget.py --job NAME --stamp "$RUNNER_TEMP/job-start" --timeout-minutes N
```

and prints how much of the limit the job used. Above 75% it prints a `::warning`, which shows on the run's summary page.
It never fails a job. A missing, empty or unreadable stamp prints one line and exits 0. `--tiers PATH` adds one line for
each tier of the harness's time report ([Testing](testing.md)).

`N` is a second copy of the job's own `timeout-minutes`. The two have to agree, or the warning is measured against a
window the runner doesn't use. `tests/test_ci_time_budget.py` reads `ci.yml` as text and fails if they differ, and
`tests/test_workflows.py` checks that every job starts with the stamp and ends with the budget step.

A job that reaches its limit is stopped by the runner, so its budget step doesn't run. The warning on the runs before it
is the early sign, which is why it starts at 75% and not at 95%. The limits above are first values: before the split the
one `ci` job had 20 minutes, about half of it the oracle. Change one when the budget lines show where a job has settled,
and change `N` in the same edit.

## Test integrity and the holdout

`scripts/test_integrity.py` runs as a step of the `ci` job on pull requests. It compares the tests at the pull request's
base with the tests at its head and lists:

- an assertion removed, a test removed, or a test file deleted;
- a tolerance widened, added, or changed to another kind (`approx`, `abs=`, `rel=`, `atol=`, `rtol=`);
- a skip or expected-failure marker added to a test file that already existed;
- an edit to a `conftest.py`, `tests/plugins/`, the `[tool.pytest...]` tables of `pyproject.toml`,
  `.github/workflows/`, `.pre-commit-config.yaml`, the script itself, or the code that defines a weakened test
  (`src/ws/guards/test_weakening.py` and `_paths.py`);
- a test file that isn't text, so it couldn't be compared.

What counts as an assertion, a test or a skip is the same code the test-weakening guard uses
([Guards](guards.md)), imported by the script, so the two can't drift apart.

Any of those needs a non-empty `test-change:` line in the pull request body that says why. An edit to anything in
`tests/holdout/` needs a non-empty `holdout-change:` line. The pull request template has both lines. The script reads
the body from the `PR_BODY` environment variable, never from the command line, and ignores `<!-- ... -->` comments. An
empty body counts as no lines. The pull request trigger includes `edited`, so adding the line to the body runs the check
again; without it, a failed run would stay red until another commit was pushed, and a re-run of the same run reads the
body as it was when the run started.

The holdout, `tests/holdout/test_holdout_core.py`, is a small set of core promises checked only by running `ws` as a
program in a scratch home: the gate refuses a write to a path ws protects, refuses a payload it can't read, and refuses
to generate rules from a policy that doesn't parse, and `ws doctor --offline` runs. It is kept apart so a change is
never shaped to pass it.

Exit codes of the script: 0 nothing to say or every hit has its line, 1 a hit with no line, 2 the check couldn't run.
A base commit that isn't in the clone is a failure with exit 2 (the `ci` job fetches the full history).

## Pinning and the workflow audit

Every action is pinned by commit SHA with its version in a comment, and tools are pinned at versions at least 7 days
old. Two dev-only tools, both MIT licensed and in active use, run in the prek hooks and so in `ci`:

- pinact v5.0.2 fails when an action in `.github/workflows/` isn't pinned by SHA. `pinact run` fixes it.
- zizmor v1.31.1 audits `.github/workflows/ci.yml` for injection through expressions, over-wide permissions and cache
  poisoning. It runs offline in the hook, so the audits that need the GitHub API are skipped there.

zizmor is scoped to `ci.yml`. The other workflows have findings that are not fixed yet:

| Workflow | Finding |
|---|---|
| `cli.yml` | `excessive-permissions` (the top-level permissions), `adhoc-packages` |
| `install-test.yml` | `cache-poisoning`, `unsound-ternary` |
| `lab.yml` | `cache-poisoning` |
| `release.yml` | `superfluous-actions` (informational) |

`ci.yml` itself has top-level `permissions: {}` and `contents: read` on each job.
