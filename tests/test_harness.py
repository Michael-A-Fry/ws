"""The suite's own harness (tests/plugins/ws_harness.py): size limits, random order, flakes, quarantine, tier report.

Each test runs a small inner pytest session through `pytester`, loaded with `-p ws_harness`, in a folder of its own.
The inner session writes its cache and reports under that folder, never into the real repo. The limits in the inner
runs are 0.5 to 1.5 seconds, set with `-o`, so the whole file costs a few seconds.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import textwrap
import tomllib
from datetime import datetime
from pathlib import Path

import pytest
import ws_harness

pytest_plugins = ["pytester"]

ROOT = Path(__file__).resolve().parents[1]
LIMITS = ("-o", "ws_size_small=0.5", "-o", "ws_size_medium=1", "-o", "ws_size_large=1.5")


@pytest.fixture(autouse=True)
def _inner_sessions_import_by_path(pytester, monkeypatch):
    """The inner sessions write files named like this suite's own (`tests/test_gate_corpus.py`). By default pytest
    imports a test file under its bare module name, and in a full run that name is already taken by the real file
    ("import file mismatch"). `importlib` mode gives each inner file a module of its own."""
    monkeypatch.setenv("PYTEST_ADDOPTS", "--import-mode=importlib")


def src(text: str) -> str:
    return textwrap.dedent(text).lstrip("\n")


def write(pytester: pytest.Pytester, rel: str, source: str) -> Path:
    path = pytester.path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source, encoding="utf-8")
    return path


def run(pytester: pytest.Pytester, *args: str) -> pytest.RunResult:
    return pytester.runpytest("-p", "ws_harness", *args)


def harness_dir(pytester: pytest.Pytester) -> Path:
    return pytester.path / ".pytest_cache" / "d" / "ws-harness"


def lines(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# --- the plugin is wired in from pyproject, and the path tables say what the brief says ---------------------------


def test_pyproject_loads_the_plugin_for_every_run():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    ini = config["tool"]["pytest"]["ini_options"]
    assert "-p ws_harness" in ini["addopts"]
    assert "tests/plugins" in ini["pythonpath"]
    assert ini["faulthandler_timeout"] == 120
    dev = config["dependency-groups"]["dev"]
    for name in ("pytest-timeout", "pytest-randomly", "pytest-rerunfailures"):
        assert any(re.fullmatch(rf"{name}==\d+(\.\d+)*", pin) for pin in dev), name


@pytest.mark.parametrize(
    ("path", "size", "tier"),
    [
        ("tests/e2e/test_term_browser.py", "large", "nightly"),
        ("tests/e2e/test_sandbox.py", "large", "nightly"),
        ("tests/test_gate_oracle.py", "large", "nightly"),
        ("tests/test_shellwords_oracle.py", "large", "nightly"),
        ("tests/test_gate_corpus.py", "medium", "gate"),
        ("tests/test_policy_every_executable.py", "medium", "gate"),
        ("tests/test_gate_daemon.py", "medium", "gate"),
        ("tests/test_gate.py", "small", "gate"),
        ("tests/test_policy.py", "small", "gate"),
        ("tests/test_shellwords.py", "small", "gate"),
        ("tests/test_guards.py", "small", "gate"),
        ("tests/test_guard_claims.py", "small", "gate"),
        ("tests/test_hook_parity.py", "small", "gate"),
        ("tests/test_cli.py", "small", "core"),
        ("tests/test_session.py", "small", "core"),
        ("test_loose.py", "small", "core"),
    ],
)
def test_path_tables(path, size, tier):
    assert ws_harness.size_for_path(path) == size
    assert ws_harness.tier_for_path(path) == tier


def test_every_table_pattern_is_a_posix_path_pattern():
    for table in (ws_harness.SIZE_PATHS, ws_harness.TIER_PATHS):
        for pattern, _name in table:
            assert "\\" not in pattern
            assert pattern.startswith("tests/")


# --- 1. size markers and limits -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("rel", "size", "limit", "within"),
    [
        ("tests/test_plain.py", "small", 0.5, 0.1),
        ("tests/test_gate_corpus.py", "medium", 1, 0.7),
        ("tests/e2e/test_slow.py", "large", 1.5, 1.2),
    ],
)
def test_each_size_has_its_own_limit(pytester, rel, size, limit, within):
    """A test that sleeps past its size's limit fails with that limit named; one that sleeps past only a smaller
    size's limit passes."""
    write(
        pytester,
        rel,
        "import time\n\n\n"
        f"def test_within():\n    time.sleep({within})\n\n\n"
        f"def test_over():\n    time.sleep({limit + 0.6})\n",
    )
    result = run(pytester, "-rf", *LIMITS)
    result.assert_outcomes(passed=1, failed=1)
    result.stdout.fnmatch_lines([f"*Timeout (>{float(limit)}s)*"])
    assert ws_harness.size_for_path(rel) == size


def test_explicit_markers_override_the_table_and_a_stricter_timeout_is_kept(pytester):
    write(
        pytester,
        "tests/test_o.py",
        src(
            """
            import time

            import pytest


            @pytest.mark.large
            def test_a():  # small by path, large by marker: 1.5 s
                time.sleep(0.9)


            @pytest.mark.timeout(0.3)
            def test_b():  # stricter than the size: kept
                time.sleep(0.6)


            @pytest.mark.timeout(100)
            def test_c():  # looser than the size: the size's limit wins
                time.sleep(0.8)


            @pytest.mark.timeout(0)
            def test_d():  # 0 means no limit to pytest-timeout: refused
                time.sleep(0.8)


            @pytest.mark.medium
            @pytest.mark.timeout(0.3)
            def test_e():  # a size and a stricter timeout: the stricter is kept
                time.sleep(0.6)
            """
        ),
    )
    recorder = pytester.inline_run("-p", "ws_harness", "-p", "no:randomly", *LIMITS)
    calls = {r.nodeid.split("::")[1]: r for r in recorder.getreports("pytest_runtest_logreport") if r.when == "call"}
    assert calls["test_a"].passed
    for name, seconds in (("test_b", 0.3), ("test_c", 0.5), ("test_d", 0.5), ("test_e", 0.3)):
        assert calls[name].failed, name
        assert f"Timeout (>{seconds}s)" in calls[name].longreprtext, name


ALARM_TEST = """
    import signal
    import time

    import pytest


    {mark}
    def test_alarm_user():
        pending = signal.alarm(20)  # what a test that guards itself with the process alarm does
        signal.alarm(0)
        assert pending == 0, f"the harness left an alarm of {{pending}} s pending"
        time.sleep(0.8)  # past the 0.5 s small limit, so the red run shows the limit is still enforced


    def test_plain():
        pass
"""


def alarm_calls(pytester, *args):
    # The inner run shares this process, so this test's own pytest-timeout alarm would show up as "pending" in the
    # inner test. Park it for the inner run and put it back after.
    left, _ = signal.setitimer(signal.ITIMER_REAL, 0)
    try:
        recorder = pytester.inline_run("-p", "ws_harness", "-p", "no:randomly", *args)
    finally:
        if left:
            signal.setitimer(signal.ITIMER_REAL, left)
    return {r.nodeid.split("::")[1]: r for r in recorder.getreports("pytest_runtest_logreport") if r.when == "call"}


def test_a_test_that_uses_the_process_alarm_gets_the_thread_method_from_the_table(pytester, monkeypatch):
    write(pytester, "tests/test_alarm.py", src(ALARM_TEST).format(mark=""))
    red = alarm_calls(pytester, *LIMITS)  # the signal method leaves the harness's alarm pending
    assert red["test_alarm_user"].failed
    assert "the harness left an alarm of" in red["test_alarm_user"].longreprtext
    monkeypatch.setitem(ws_harness.THREAD_TIMEOUT, "tests/test_alarm.py::test_alarm_user", "arms signal.alarm itself")
    green = alarm_calls(pytester, "-o", "ws_size_small=60")
    assert green["test_alarm_user"].passed and green["test_plain"].passed


def test_the_table_beats_a_signal_method_the_test_asked_for(pytester, monkeypatch):
    write(pytester, "tests/test_alarm.py", src(ALARM_TEST).format(mark="@pytest.mark.timeout(30, method='signal')"))
    monkeypatch.setitem(ws_harness.THREAD_TIMEOUT, "tests/test_alarm.py::test_alarm_user", "arms signal.alarm itself")
    assert alarm_calls(pytester, "-o", "ws_size_small=60")["test_alarm_user"].passed


def test_every_thread_timeout_entry_names_a_real_test_that_still_uses_the_alarm():
    root = Path(__file__).resolve().parent.parent
    assert ws_harness.THREAD_TIMEOUT
    for node, reason in ws_harness.THREAD_TIMEOUT.items():
        path, _, name = node.partition("::")
        text = (root / path).read_text(encoding="utf-8")
        assert re.search(rf"^def {re.escape(name)}\(", text, re.MULTILINE), node
        assert reason.strip(), node
        assert "signal.alarm" in text or "setitimer" in text, f"{node} no longer uses the alarm: drop the entry"


def test_limits_come_from_the_table_defaults_without_overrides(pytester):
    write(pytester, "tests/test_plain.py", "def test_x():\n    pass\n")
    result = run(pytester, "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert ws_harness.DEFAULT_LIMITS == {"small": 60, "medium": 300, "large": 900}


def test_a_bad_limit_is_a_usage_error(pytester):
    write(pytester, "tests/test_plain.py", "def test_x():\n    pass\n")
    result = run(pytester, "-o", "ws_size_small=soon")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*ws_size_small*"])


def test_a_missing_plugin_is_loud_not_silent(pytester):
    """No pytest-timeout means no limits: the run refuses, it does not carry on without them."""
    write(pytester, "tests/test_plain.py", "def test_x():\n    pass\n")
    for off in ("timeout", "rerunfailures"):
        result = run(pytester, "-p", f"no:{off}")
        assert result.ret == pytest.ExitCode.USAGE_ERROR, off
        result.stderr.fnmatch_lines(["*ws_harness*"])
    with pytest.raises(pytest.UsageError, match="no_such_module_for_ws_harness"):
        ws_harness.require("no_such_module_for_ws_harness")


def test_collection_in_a_subdirectory_uses_paths_from_the_rootdir(pytester, monkeypatch):
    write(pytester, "pytest.ini", "[pytest]\naddopts = -p ws_harness\nws_size_small = 0.5\nws_size_large = 1.5\n")
    sub = write(pytester, "tests/e2e/test_sub.py", "import time\n\n\ndef test_x():\n    time.sleep(0.9)\n").parent
    monkeypatch.chdir(sub)
    result = pytester.runpytest()
    result.assert_outcomes(passed=1)  # large (1.5 s), not small (0.5 s), though pytest started in tests/e2e
    (tier,) = lines(harness_dir(pytester) / "tiers.jsonl")
    assert tier["tier"] == "nightly"


# --- 2. random order, the seed and ORDER_PINNED -----------------------------------------------------------------------

TEN = "".join(f"def test_t{i}():\n    pass\n\n\n" for i in range(10))


def collected(pytester: pytest.Pytester, *args: str) -> list[str]:
    result = run(pytester, "--collect-only", "-q", *args)
    return [line for line in result.outlines if "::" in line]


def test_the_seed_is_printed_and_reproduces_the_order(pytester):
    write(pytester, "tests/test_ten.py", TEN)
    result = run(pytester, "--randomly-seed=4242")
    result.stdout.fnmatch_lines(["Using --randomly-seed=4242"])
    assert collected(pytester, "--randomly-seed=4242") == collected(pytester, "--randomly-seed=4242")
    assert collected(pytester, "--randomly-seed=4242") != collected(pytester, "--randomly-seed=4243")
    in_file_order = collected(pytester, "-p", "no:randomly")
    assert in_file_order == [f"tests/test_ten.py::test_t{i}" for i in range(10)]


def test_order_pinned_puts_listed_tests_back_in_file_order(pytester, monkeypatch):
    write(pytester, "tests/test_ten.py", TEN)
    pins = [f"tests/test_ten.py::test_t{i}" for i in (7, 2, 5)]
    seed = next(
        s for s in range(1, 500) if _subsequence(collected(pytester, f"--randomly-seed={s}"), pins) != sorted(pins)
    )
    shuffled = collected(pytester, f"--randomly-seed={seed}")
    for pin in pins:
        monkeypatch.setitem(ws_harness.ORDER_PINNED, pin, "shares a database row with its neighbours")
    pinned = collected(pytester, f"--randomly-seed={seed}")
    assert _subsequence(pinned, pins) == sorted(pins)  # t2, t5, t7: file order
    assert [i for i, n in enumerate(pinned) if n in pins] == [i for i, n in enumerate(shuffled) if n in pins]
    assert [n for n in pinned if n not in pins] == [n for n in shuffled if n not in pins]  # the rest stay shuffled
    result = run(pytester, f"--randomly-seed={seed}")
    result.stdout.fnmatch_lines(["*3 test(s) pinned to file order*"])


def _subsequence(names: list[str], wanted: list[str]) -> list[str]:
    return [n for n in names if n in wanted]


def test_an_order_pin_without_a_reason_is_refused(pytester, monkeypatch):
    write(pytester, "tests/test_ten.py", TEN)
    monkeypatch.setitem(ws_harness.ORDER_PINNED, "tests/test_ten.py::test_t1", " ")
    result = run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*ORDER_PINNED*reason*"])


def test_order_can_be_switched_off(pytester):
    write(pytester, "tests/test_ten.py", TEN)
    result = run(pytester, "-p", "no:randomly")
    assert "randomly-seed" not in result.stdout.str()
    result.assert_outcomes(passed=10)


# --- 3. flakes and quarantine -------------------------------------------------------------------------------------

COUNTER = (
    "import pathlib\n\nimport pytest\n\nCOUNT = pathlib.Path(__file__).with_name('count.txt')\n\n\n"
    "def bump():\n    n = int(COUNT.read_text()) if COUNT.exists() else 0\n    COUNT.write_text(str(n + 1))\n"
    "    return n\n\n\n"
)


def attempts(pytester: pytest.Pytester) -> int:
    return int((pytester.path / "tests" / "count.txt").read_text(encoding="utf-8"))


def test_a_flaky_test_that_fails_then_passes_reruns_once_and_writes_an_event(pytester):
    write(
        pytester,
        "tests/test_f.py",
        COUNTER + "@pytest.mark.flaky\ndef test_flaky():\n    assert bump() >= 1, 'first attempt fails on purpose'\n",
    )
    result = run(pytester, "--randomly-seed=77")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*1 test(s) passed on rerun*tests/test_f.py::test_flaky*"])
    assert attempts(pytester) == 2
    (event,) = lines(harness_dir(pytester) / "flakes.jsonl")
    assert set(event) == {"schema", "ts", "nodeid", "seed", "first_failure"}
    assert event["schema"] == "ws.test-flake/1"
    assert event["nodeid"] == "tests/test_f.py::test_flaky"
    assert event["seed"] == 77
    assert "first attempt fails on purpose" in event["first_failure"]
    assert len(event["first_failure"]) <= 300
    datetime.strptime(event["ts"], "%Y-%m-%dT%H:%M:%SZ")


def test_a_flaky_test_that_fails_twice_fails_and_writes_no_event(pytester):
    write(
        pytester, "tests/test_f.py", COUNTER + "@pytest.mark.flaky\ndef test_flaky():\n    bump()\n    assert False\n"
    )
    result = run(pytester)
    result.assert_outcomes(failed=1)
    assert attempts(pytester) == 2  # one rerun, no more
    assert not (harness_dir(pytester) / "flakes.jsonl").exists()


def test_a_failure_without_the_marker_does_not_rerun(pytester):
    write(pytester, "tests/test_f.py", COUNTER + "def test_plain():\n    bump()\n    assert False\n")
    result = run(pytester)
    result.assert_outcomes(failed=1)
    assert attempts(pytester) == 1
    assert not (harness_dir(pytester) / "flakes.jsonl").exists()


def test_a_flaky_marker_that_asks_for_more_than_one_rerun_is_a_collection_error(pytester):
    for marker in ("flaky(reruns=2)", "flaky(3)"):
        write(
            pytester,
            "tests/test_f.py",
            f"import pytest\n\n\npytestmark = pytest.mark.{marker}\n\n\ndef test_x():\n    pass\n",
        )
        result = run(pytester)
        result.assert_outcomes(errors=1)
        assert result.ret == pytest.ExitCode.INTERRUPTED
        result.stdout.fnmatch_lines(["*tests/test_f.py::test_x*flaky*one rerun*"])


def test_a_flaky_marker_with_one_rerun_or_none_is_fine(pytester):
    write(
        pytester,
        "tests/test_f.py",
        "import pytest\n\n\npytestmark = pytest.mark.flaky(reruns=1)\n\n\ndef test_x():\n    pass\n",
    )
    run(pytester).assert_outcomes(passed=1)


def test_a_command_line_reruns_is_refused(pytester):
    write(pytester, "tests/test_plain.py", "def test_x():\n    pass\n")
    for args in (("--reruns", "2"), ("--reruns=1",), ("--force-reruns", "1")):
        result = run(pytester, *args)
        assert result.ret == pytest.ExitCode.USAGE_ERROR, args
        result.stderr.fnmatch_lines(["*reruns*"])
    pytester.makeini("[pytest]\nreruns = 1\n")
    result = run(pytester)
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*reruns*"])


QUARANTINED = src(
    """
    import pytest

    quarantine = pytest.mark.quarantine


    @quarantine
    def test_q():
        pass


    def test_ok():
        pass
    """
)


def test_a_quarantined_test_is_deselected_by_default(pytester):
    write(pytester, "tests/test_q.py", QUARANTINED)
    result = run(pytester)
    result.assert_outcomes(passed=1, deselected=1)


def test_run_quarantine_runs_it(pytester):
    write(pytester, "tests/test_q.py", QUARANTINED)
    result = run(pytester, "--run-quarantine")
    result.assert_outcomes(passed=2)


def test_dash_m_quarantine_runs_only_it(pytester):
    write(pytester, "tests/test_q.py", QUARANTINED)
    result = run(pytester, "-m", "quarantine")
    result.assert_outcomes(passed=1, deselected=1)
    result.stdout.fnmatch_lines(["*1 passed, 1 deselected*"])


def test_deselected_quarantine_is_reported_through_the_hook(pytester):
    write(pytester, "tests/test_q.py", QUARANTINED)
    recorder = pytester.inline_run("-p", "ws_harness")
    (call,) = recorder.getcalls("pytest_deselected")
    assert [item.name for item in call.items] == ["test_q"]


# --- 4. the tier report ------------------------------------------------------------------------------------------


def tier_files(pytester: pytest.Pytester) -> None:
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n\n\ndef test_2():\n    pass\n")
    write(pytester, "tests/test_gate_b.py", "def test_3():\n    pass\n")
    write(pytester, "tests/e2e/test_n.py", "def test_4():\n    pass\n")
    write(
        pytester,
        "tests/test_q.py",
        "import pytest\n\n\npytestmark = pytest.mark.quarantine\n\n\ndef test_5():\n    pass\n",
    )


def test_the_tier_report_has_one_line_per_tier(pytester):
    tier_files(pytester)
    result = run(pytester, "--randomly-seed=99", "--run-quarantine", "-o", "ws_tier_budget_gate=5")
    result.assert_outcomes(passed=5)
    rows = lines(harness_dir(pytester) / "tiers.jsonl")
    assert [row["tier"] for row in rows] == ["core", "gate", "nightly"]
    for row in rows:
        assert set(row) == {"schema", "ts", "seed", "tier", "duration_s", "tests", "budget_s"}
        assert row["schema"] == "ws.test-tiers/1"
        assert row["seed"] == 99
        assert isinstance(row["duration_s"], float) and row["duration_s"] >= 0
        datetime.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ")
    core, gate, nightly = rows
    assert (core["tests"], gate["tests"], nightly["tests"]) == (2, 1, 2)  # the quarantined test counts as nightly
    assert (core["budget_s"], gate["budget_s"], nightly["budget_s"]) == (900, 5, 3600)
    assert ws_harness.DEFAULT_BUDGETS == {"core": 900, "gate": 1200, "nightly": 3600}


def test_a_second_run_appends_and_a_run_without_randomly_has_a_null_seed(pytester):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    run(pytester)
    run(pytester, "-p", "no:randomly")
    first, second = lines(harness_dir(pytester) / "tiers.jsonl")
    assert isinstance(first["seed"], int)
    assert second["seed"] is None


def test_tier_report_path_option(pytester):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    target = pytester.path / "out" / "deeper" / "t.jsonl"
    result = run(pytester, "--ws-tier-report", str(target), "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert [row["tier"] for row in lines(target)] == ["core"]
    assert not (pytester.path / ".pytest_cache").exists()


def test_without_a_cache_provider_nothing_is_written_and_one_line_says_so(pytester):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    result = run(pytester, "-p", "no:cacheprovider")
    result.assert_outcomes(passed=1)
    assert result.stdout.str().count("tier report not written") == 1
    assert not (pytester.path / ".pytest_cache").exists()
    assert not list(pytester.path.rglob("*.jsonl"))


def test_a_missing_cache_dir_is_made(pytester):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    assert not (pytester.path / ".pytest_cache").exists()
    run(pytester).assert_outcomes(passed=1)
    assert (harness_dir(pytester) / "tiers.jsonl").is_file()


@pytest.mark.filterwarnings("ignore::pytest.PytestCacheWarning")  # the inner cache is the thing that cannot be made
def test_a_cache_dir_that_cannot_be_made_does_not_fail_the_run(pytester):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    (pytester.path / "blocker").write_text("a file where the cache folder should go\n", encoding="utf-8")
    result = run(pytester, "-o", "cache_dir=blocker")
    result.assert_outcomes(passed=1)
    assert result.ret == 0
    assert result.stdout.str().count("tier report not written") == 1


def test_inner_runs_do_not_write_into_the_real_repo_cache(pytester, request):
    cache = request.config.cache
    real = Path(str(cache.mkdir("ws-harness"))) if cache is not None else None
    before = {p: p.stat().st_size for p in real.glob("*.jsonl")} if real else {}
    tier_files(pytester)
    run(pytester)
    assert (harness_dir(pytester) / "tiers.jsonl").is_file()
    assert harness_dir(pytester).resolve() != real
    assert (real is None) or {p: p.stat().st_size for p in real.glob("*.jsonl")} == before


@pytest.mark.parametrize(
    ("ci", "fails"),
    [
        ("true", True),
        ("1", True),
        ("yes", True),
        ("", False),
        ("0", False),
        ("false", False),
        ("False", False),
        (None, False),
    ],
)
def test_over_budget_fails_the_run_only_when_ci_is_set(pytester, monkeypatch, ci, fails):
    write(pytester, "tests/test_a.py", "import time\n\n\ndef test_1():\n    time.sleep(0.05)\n")
    if ci is None:
        monkeypatch.delenv("CI", raising=False)
    else:
        monkeypatch.setenv("CI", ci)
    result = run(pytester, "-o", "ws_tier_budget_core=0")
    result.assert_outcomes(passed=1)
    result.stdout.fnmatch_lines(["*core*over budget*"])
    assert result.ret == (pytest.ExitCode.TESTS_FAILED if fails else pytest.ExitCode.OK)


def test_within_budget_prints_no_over_budget_line_even_in_ci(pytester, monkeypatch):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    monkeypatch.setenv("CI", "true")
    result = run(pytester)
    assert result.ret == 0
    assert "over budget" not in result.stdout.str()


def test_a_bad_budget_is_a_usage_error(pytester):
    write(pytester, "tests/test_a.py", "def test_1():\n    pass\n")
    result = run(pytester, "-o", "ws_tier_budget_gate=-3")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*ws_tier_budget_gate*"])


# --- the lock file -----------------------------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("uv") is None, reason="uv is not installed")
def test_uv_lock_check_passes():
    env = {k: v for k, v in os.environ.items() if k.startswith(("UV_", "PATH", "SYSTEMROOT", "TMP", "TEMP"))}
    done = subprocess.run(
        ["uv", "lock", "--check", "--offline"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=50,
    )
    assert done.returncode == 0, done.stderr
