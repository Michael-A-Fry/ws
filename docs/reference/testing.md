# Testing

How the suite keeps time, order and flakes under control. The plugin that does it lives in `tests/plugins/ws_harness.py`.
pyproject loads it for every `pytest` run in this repo (`addopts = "-p ws_harness"`, with `tests/plugins` on
`pythonpath`), so CI, `ws-test` and the pre-push hook all get it. `tests/test_harness.py` tests it.

The harness builds on three pinned packages in the `dev` group: pytest-timeout (per-test limits), pytest-randomly
(random order, the seed line) and pytest-rerunfailures (the `flaky` marker). It adds the policy they leave open. If any
of them is missing or switched off, the run stops with a usage error. It never carries on without limits.

## Run the suite

```sh
uv run --locked pytest -q                       # the whole suite, in a random order
uv run --locked pytest -q --randomly-seed=1234  # the same order again
uv run --locked pytest -q -p no:randomly        # file order, no shuffle
uv run --locked pytest -q --run-quarantine      # also the quarantined tests
```

## Size markers and limits

Every test has a size. The size sets how long that one test may run before pytest-timeout fails it.

| Marker | Default limit | Which tests |
|---|---|---|
| `small` | 60 s | Everything not listed below. |
| `medium` | 300 s | `test_gate_corpus.py`, `test_policy_every_executable.py`, `test_policy_unknown_every_command.py`, `test_gate_daemon*.py`. |
| `large` | 900 s | `tests/e2e/**`, `test_gate_oracle.py`, `test_shellwords_oracle.py` and the term browser test. |

The paths are in `SIZE_PATHS` in the plugin, relative to the rootdir, first match wins. The plugin gives each test its
size marker, so `-m large` and `-m "not large"` select by size.

- **An explicit marker wins.** `@pytest.mark.large` on a test (or its class or module) beats the path table.
- **A stricter timeout is kept.** `@pytest.mark.timeout(5)` on a small test stays at 5 s. A longer one, or
  `timeout(0)` (which pytest-timeout reads as no limit), is replaced by the size's limit. No test can switch its limit
  off.
- **Change the limits** with the ini options `ws_size_small`, `ws_size_medium` and `ws_size_large` (seconds, more than
  zero). `-o ws_size_small=1` gives every small test one second, which is how the harness's own tests run.
- **Never sleep 60 s or more in a test**, and keep any time a test waits on (a daemon starting, an idle timeout, a
  subprocess `timeout=`) under its size's limit. A wait longer than the limit never gets to finish.
- **`faulthandler_timeout = 120`** in pyproject makes pytest dump every thread's stack once a test has run for two
  minutes. It only prints. For a large test it's noise; the size limit does the stopping.

On Windows pytest-timeout has no signal to use, so it falls back to a thread that ends the whole process on a timeout.

### Tests that use the process alarm

pytest-timeout's default method arms the process alarm (`ITIMER_REAL`) before each test. A test that calls
`signal.alarm` or `setitimer` itself would see that alarm already pending, and `signal.alarm(0)` would cancel the
harness limit. The `THREAD_TIMEOUT` table in the plugin lists such tests by node id (fnmatch pattern), each with its
reason. They get `method="thread"` (no signal; on a timeout it ends the whole process), and the table beats a
`method=` the test asked for. The size limit still applies, and a stricter explicit timeout is still kept. Keep the
table short, and drop an entry when its test stops using the alarm (`tests/test_harness.py` checks that). It starts
with `tests/test_sandbox.py`'s scan test, which uses `signal.alarm(20)` as its own hang guard.

## Random order and the seed

pytest-randomly shuffles modules, then classes, then tests, and prints the seed in the header:

```text
Using --randomly-seed=1170148895
```

To run the same order again, pass that number back: `pytest --randomly-seed=1170148895`. `--randomly-seed=last` reuses
the previous run's seed (it needs the cache). `-p no:randomly` switches the shuffle off. The seed also goes into the
tier and flake lines below, so a slow or flaky run can be replayed.

A test that only passes in one order is a bug. If it can't be fixed yet, add its node id to `ORDER_PINNED` in the
plugin with the reason. A pinned test takes back the slot it was shuffled into, in file order among the pinned tests.
`ORDER_PINNED` is the only way to opt a test out of the shuffle, and it starts empty. An entry with no reason is a
usage error. The run's summary says how many tests were pinned.

## Flakes and quarantine

- **One rerun, only for a marked test.** `@pytest.mark.flaky` (or `flaky(reruns=1)`) reruns the test once if it fails.
  Any other `reruns` value is a collection error. A command-line `--reruns` or `--force-reruns`, and the ini option
  `reruns`, are refused with a usage error, so CI can't quietly retry the suite.
- **A flake event.** When a marked test fails, then passes on the rerun, the harness appends one line to the flake
  report and names the test in the run summary. A test that fails twice fails the run and writes nothing. Fix or
  quarantine a test that shows up here; don't leave the marker on.
- **Quarantine.** `@pytest.mark.quarantine` keeps a known flake out of the run. It's deselected by default (pytest's
  `deselected` count, and the `pytest_deselected` hook, include it). `--run-quarantine` runs it with everything else;
  `-m quarantine` runs only it. A quarantined test counts in the `nightly` tier.

## Tiers and the time report

Each test also belongs to a tier, from a second path table (`TIER_PATHS`, same rules). The harness adds up the time of
every test in a tier, and appends one line per tier that ran.

| Tier | Which tests | Initial budget |
|---|---|---|
| `gate` | `test_gate*`, `test_policy*`, `test_shellwords*`, `test_guard*`, `test_hook_parity*` (except the oracle files). | 1200 s |
| `nightly` | `tests/e2e/**`, `test_gate_oracle.py`, `test_shellwords_oracle.py` and anything marked `quarantine`. | 3600 s |
| `core` | The rest. | 900 s |

The budgets are initial values and generous on purpose: w3-ci-budget tunes them from the report. Set them with the ini
options `ws_tier_budget_core`, `ws_tier_budget_gate` and `ws_tier_budget_nightly` (seconds, zero or more).

A tier over its budget prints a line in the summary. The run fails (exit code 1) only when the `CI` environment variable
is set and isn't empty, `0` or `false` (any case). A partial run (one file, `-k`) adds up only what it ran.

### Where the files go

| File | Default place | Override |
|---|---|---|
| Tier report | `ws-harness/tiers.jsonl` in pytest's cache folder (`.pytest_cache/d/` under the rootdir) | `--ws-tier-report PATH` |
| Flake report | `ws-harness/flakes.jsonl` in the same folder | `--ws-flake-report PATH` |

With `-p no:cacheprovider` and no path option, nothing is written and the summary says so in one line. A folder that
can't be written does the same. Neither fails the run. Parent folders of a path option are made. Each run appends.

### `ws.test-tiers/1`

One JSON object per line, one line per tier that ran (in the order core, gate, nightly). These field names are a
contract: `scripts/ci_time_budget.py` reads them.

| Field | Meaning |
|---|---|
| `schema` | `ws.test-tiers/1`. |
| `ts` | UTC time the run finished, `YYYY-MM-DDTHH:MM:SSZ`. The same on every line of one run. |
| `seed` | pytest-randomly's seed (an integer), or `null` with `-p no:randomly`. |
| `tier` | `core`, `gate` or `nightly`. |
| `duration_s` | Seconds spent in the tier's tests: setup, call and teardown of every attempt, rounded to a millisecond. |
| `tests` | How many tests in the tier were reported (passed, failed or skipped). Deselected tests aren't counted. |
| `budget_s` | The tier's budget in seconds in force for that run. |

```json
{"schema":"ws.test-tiers/1","ts":"2026-11-08T05:21:55Z","seed":1170148895,"tier":"gate","duration_s":212.481,"tests":2310,"budget_s":1200}
```

### `ws.test-flake/1`

One line per marked test that failed and then passed on its rerun. The nightly quarantine job reads these.

| Field | Meaning |
|---|---|
| `schema` | `ws.test-flake/1`. |
| `ts` | UTC time of the pass, same format as above. |
| `nodeid` | The test's node id from the rootdir. |
| `seed` | The run's seed, or `null`. |
| `first_failure` | The first failure's message on one line, cut to 300 characters. |

```json
{"schema":"ws.test-flake/1","ts":"2026-11-08T05:22:01Z","nodeid":"tests/test_x.py::test_y","seed":7,"first_failure":"AssertionError"}
```

## Changing the tables

`SIZE_PATHS`, `TIER_PATHS` and `ORDER_PINNED` are plain lists and a dict at the top of the plugin. A wrong row can time
out a legitimately slow test, so a change to one comes with the evidence (a `--durations` run). `test_path_tables` in
`tests/test_harness.py` pins the rows the table is meant to hold; update it in the same change.
