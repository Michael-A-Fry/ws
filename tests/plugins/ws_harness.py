"""The suite's own pytest plugin: size limits, random order, one marked rerun, quarantine and the tier report.

Loaded by `addopts = "-p ws_harness"` in pyproject.toml, so every `pytest` run in this repo gets it (CI, ws-test, the
pre-push hook). It builds on pytest-timeout (per-test limits), pytest-randomly (random order and the seed line) and
pytest-rerunfailures (the `flaky` marker). It holds the policy those three leave open: which test gets which limit,
that only a marked test reruns and only once, which tests may not be shuffled, and how long each tier took.

Everything it promises is in docs/reference/testing.md, with the JSONL formats. The path tables, `ORDER_PINNED` and the
defaults are plain module data, so a change to one is a one-line diff with a reason beside it.

State lives on the `pytest.Config`, not in module globals, so a `pytester` inner run is its own session and writes only
under its own folder.
"""

from __future__ import annotations

import fnmatch
import importlib
import itertools
import json
import math
import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest


def require(module: str) -> None:
    """Import a dependency or refuse to start. A missing limit plugin must never mean "no limits"."""
    try:
        importlib.import_module(module)
    except ImportError as exc:
        raise pytest.UsageError(
            f"ws_harness needs {module} and cannot import it ({exc}); run `uv sync --locked` to install the dev group"
        ) from exc


require("pytest_timeout")
require("pytest_rerunfailures")

SIZES = ("small", "medium", "large")
DEFAULT_LIMITS = {"small": 60, "medium": 300, "large": 900}  # seconds per test; ini ws_size_<name> overrides

TIERS = ("core", "gate", "nightly")
# Initial values, deliberately generous: w3-ci-budget tunes them from the tier report. Ini ws_tier_budget_<name>.
DEFAULT_BUDGETS = {"core": 900, "gate": 1200, "nightly": 3600}

# Paths are relative to the pytest rootdir, with "/" and fnmatch patterns ("*" also matches "/"). The first match wins.
# A test in no row is small. An explicit size marker on the test beats this table.
SIZE_PATHS: list[tuple[str, str]] = [
    ("tests/e2e/*", "large"),  # real runners, real browsers
    ("tests/test_gate_oracle.py", "large"),  # compares the gate with the real shell
    ("tests/test_shellwords_oracle.py", "large"),
    ("tests/*test_term_browser.py", "large"),
    ("tests/test_gate_corpus.py", "medium"),  # the whole corpus, every row
    ("tests/test_policy_every_executable.py", "medium"),  # every executable on PATH
    ("tests/test_policy_unknown_every_command.py", "medium"),
    ("tests/test_gate_daemon*.py", "medium"),  # starts and stops a resident gate
]

# Same rules. A test in no row is core. A test marked quarantine is nightly whatever its path.
TIER_PATHS: list[tuple[str, str]] = [
    ("tests/e2e/*", "nightly"),
    ("tests/test_gate_oracle.py", "nightly"),
    ("tests/test_shellwords_oracle.py", "nightly"),
    ("tests/test_gate*", "gate"),
    ("tests/test_policy*", "gate"),
    ("tests/test_shellwords*", "gate"),
    ("tests/test_guard*", "gate"),
    ("tests/test_hook_parity*", "gate"),
]

# Node ids that must run in file order even when the rest is shuffled, each with the reason. A test that only passes
# in one order is a bug; an entry here is a debt with a name on it, not a fix. Empty until the first real case.
ORDER_PINNED: dict[str, str] = {}

# Node ids (fnmatch patterns, "*" also matches "/") whose test uses the process alarm itself (signal.alarm or
# setitimer). pytest-timeout's default method arms that same timer, so the test would see the harness's alarm and could
# cancel it. These tests get the thread method instead, which uses no signal (on a timeout it ends the whole process,
# so keep this table short). Each entry has its reason.
THREAD_TIMEOUT: dict[str, str] = {
    "tests/test_sandbox.py::test_the_scan_never_reads_through_a_link_and_a_fifo_does_not_hang_it": (
        "arms signal.alarm(20) as its own hang guard and asserts that no alarm was pending before it"
    ),
}

SCHEMA_TIERS = "ws.test-tiers/1"
SCHEMA_FLAKE = "ws.test-flake/1"
SUMMARY_LIMIT = 300  # characters of the first failure kept in a flake event

_QUARANTINE_IN_EXPR = re.compile(r"\bquarantine\b")
_SIZE_KEY = pytest.StashKey[str]()
_TIER_KEY = pytest.StashKey[str]()
_ORDER_KEY = pytest.StashKey[int]()
_HARNESS_KEY = pytest.StashKey["Harness"]()


# --- pure helpers (the tests call these directly) -------------------------------------------------------------------


def _first_match(table: list[tuple[str, str]], path: str, default: str) -> str:
    for pattern, name in table:
        if fnmatch.fnmatchcase(path, pattern):
            return name
    return default


def size_for_path(path: str) -> str:
    """The size the path table gives a test file (a posix path from the rootdir)."""
    return _first_match(SIZE_PATHS, path, "small")


def tier_for_path(path: str) -> str:
    """The tier the path table gives a test file (a posix path from the rootdir)."""
    return _first_match(TIER_PATHS, path, "core")


def ci_is_set(env: dict[str, str] | os._Environ[str] = os.environ) -> bool:
    """CI counts as set when it is not empty, `0` or `false` (any case)."""
    return env.get("CI", "").strip().lower() not in ("", "0", "false")


def _rel(config: pytest.Config, path: Path) -> str:
    try:
        return path.relative_to(config.rootpath).as_posix()
    except ValueError:
        return path.as_posix()


def _number(config: pytest.Config, name: str, default: float, *, allow_zero: bool) -> float:
    raw = str(config.getini(name)).strip()
    if not raw:
        return float(default)
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if not math.isfinite(value) or value < 0 or (value == 0 and not allow_zero):
        what = "zero or more" if allow_zero else "more than zero"
        raise pytest.UsageError(f"ini option {name} must be a number of seconds, {what}; got {raw!r}")
    return value


def _tidy(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _timeout_of(marker: pytest.Mark) -> tuple[float | None, dict[str, object]]:
    """The seconds an explicit `timeout` marker asks for (None when it asks for none, or is not a number), and the
    method and func_only it carries, which are kept when the harness replaces it."""
    names = ("timeout", "method", "func_only")
    given = dict(zip(names, marker.args, strict=False))
    given.update({k: v for k, v in marker.kwargs.items() if k in names})
    raw = given.pop("timeout", None)
    try:
        seconds = float(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None, given
    return (seconds if math.isfinite(seconds) and seconds > 0 else None), given


def _flaky_problem(item: pytest.Item) -> str | None:
    """Why this test's `flaky` marker breaks the policy (one rerun, never more), or None."""
    for marker in item.iter_markers("flaky"):
        reruns = marker.kwargs["reruns"] if "reruns" in marker.kwargs else (marker.args[0] if marker.args else 1)
        if isinstance(reruns, bool) or reruns not in (0, 1):
            return f"{item.nodeid}: @flaky asks for {reruns!r} reruns; ws allows one rerun (bare @flaky, or reruns=1)"
    return None


def _summary(report: pytest.TestReport) -> str:
    crash = getattr(report.longrepr, "reprcrash", None)
    text = " ".join(str(getattr(crash, "message", None) or report.longreprtext).split())
    return text if len(text) <= SUMMARY_LIMIT else text[: SUMMARY_LIMIT - 3] + "..."


def _now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _append(path: Path, row: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(row, separators=(",", ":")) + "\n")  # one write per line, so appends do not interleave


def _check_order_pinned() -> None:
    for node, reason in ORDER_PINNED.items():
        if not isinstance(reason, str) or not reason.strip():
            raise pytest.UsageError(f"ORDER_PINNED in ws_harness needs a reason for {node!r}; none given")


# --- options and setup ----------------------------------------------------------------------------------------------


def pytest_addoption(parser: pytest.Parser) -> None:
    group = parser.getgroup("ws-harness", "the ws test harness")
    group.addoption("--run-quarantine", action="store_true", default=False, help="Also run tests marked quarantine.")
    group.addoption("--ws-tier-report", metavar="PATH", default=None, help="Append the tier JSONL lines to PATH.")
    group.addoption("--ws-flake-report", metavar="PATH", default=None, help="Append flake JSONL events to PATH.")
    for size, seconds in DEFAULT_LIMITS.items():
        parser.addini(f"ws_size_{size}", f"Seconds a {size} test may run (default {seconds}).", default="")
    for tier, seconds in DEFAULT_BUDGETS.items():
        parser.addini(f"ws_tier_budget_{tier}", f"Seconds the {tier} tier may take (default {seconds}).", default="")


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config: pytest.Config) -> None:
    pm = config.pluginmanager
    for name, what in (("timeout", "pytest-timeout"), ("rerunfailures", "pytest-rerunfailures")):
        if not pm.hasplugin(name):
            raise pytest.UsageError(f"ws_harness needs {what}, but it is switched off (-p no:{name}) or not installed")
    for line in (
        "small: a test that finishes well inside the small limit (default 60 s); the default for every test",
        "medium: a heavy test, limit 300 s by default",
        "large: a test that needs real runners or a browser, limit 900 s by default",
        "quarantine: a known flake, kept out of the run until --run-quarantine or -m quarantine",
    ):
        config.addinivalue_line("markers", line)
    refused = None
    if config.getoption("reruns", None) is not None or config.getoption("force_reruns", None) is not None:
        refused = "--reruns and --force-reruns"
    elif str(config.getini("reruns")).strip() not in ("", "0"):
        refused = "the ini option reruns"
    if refused:
        raise pytest.UsageError(
            f"{refused} would retry every failing test; ws allows one rerun, and only for a test marked "
            "@pytest.mark.flaky. Mark the test, or fix it."
        )
    _check_order_pinned()
    config.stash[_HARNESS_KEY] = harness = Harness(config)
    pm.register(harness, "ws_harness_state")


class Harness:
    """One session's limits, tiers, flake log and notes. Registered as a plugin so it sees every hook."""

    def __init__(self, config: pytest.Config) -> None:
        self.config = config
        self.limits = {s: _number(config, f"ws_size_{s}", DEFAULT_LIMITS[s], allow_zero=False) for s in SIZES}
        self.budgets = {t: _number(config, f"ws_tier_budget_{t}", DEFAULT_BUDGETS[t], allow_zero=True) for t in TIERS}
        self.order = itertools.count()
        self.tier_by_node: dict[str, str] = {}
        self.seconds = dict.fromkeys(TIERS, 0.0)
        self.nodes: dict[str, set[str]] = {t: set() for t in TIERS}
        self.rerun_first_failure: dict[str, str] = {}
        self.attempt: dict[str, dict[str, bool]] = {}
        self.flaked: list[str] = []
        self.pinned = 0
        self.notes: list[str] = []
        self.over: list[str] = []

    # -- where the files go
    def _target(self, option: str, name: str) -> Path | None:
        given = self.config.getoption(option)
        if given:
            return Path(given)
        cache = getattr(self.config, "cache", None)
        if cache is None:
            return None
        return Path(cache.mkdir("ws-harness")) / name

    @property
    def seed(self) -> int | None:
        seed = self.config.getoption("randomly_seed", None)  # the option exists only while pytest-randomly is loaded
        return seed if isinstance(seed, int) and not isinstance(seed, bool) else None

    # -- the report header
    def pytest_report_header(self) -> str:
        shown = ", ".join(f"{s} {_tidy(self.limits[s])} s" for s in SIZES)
        return f"ws-harness: per-test limits {shown}"

    # -- collection: sizes, tiers, file order, a bad flaky marker
    def pytest_itemcollected(self, item: pytest.Item) -> None:
        item.stash[_ORDER_KEY] = next(self.order)
        rel = _rel(self.config, item.path)
        explicit = next((m.name for m in item.iter_markers() if m.name in SIZES), None)
        size = explicit or size_for_path(rel)
        if explicit is None:
            item.add_marker(getattr(pytest.mark, size))
        item.stash[_SIZE_KEY] = size
        tier = "nightly" if item.get_closest_marker("quarantine") else tier_for_path(rel)
        item.stash[_TIER_KEY] = tier
        self.tier_by_node[item.nodeid] = tier
        self._limit(item, self.limits[size])

    @staticmethod
    def _limit(item: pytest.Item, limit: float) -> None:
        """Give the test the size's limit, unless it set a stricter one. A limit of 0 (none) or a bigger one is
        replaced, so no test can switch its limit off."""
        marker = item.get_closest_marker("timeout")
        extra: dict[str, object] = {}
        asked = None
        if marker is not None:
            asked, extra = _timeout_of(marker)
        by_table = any(fnmatch.fnmatchcase(item.nodeid, pattern) for pattern in THREAD_TIMEOUT)
        if by_table:
            extra["method"] = "thread"  # the table beats a method the test asked for: it cannot use the alarm
        if asked is not None and asked <= limit:
            if not by_table:
                return
            limit = asked
        item.add_marker(pytest.mark.timeout(limit, **extra), append=False)

    @pytest.hookimpl(wrapper=True)
    def pytest_make_collect_report(self, collector: pytest.Collector):
        report = yield
        if report.outcome == "passed":
            problems = [p for node in report.result if isinstance(node, pytest.Item) and (p := _flaky_problem(node))]
            if problems:
                report.outcome = "failed"
                report.longrepr = "\n".join(problems)
        return report

    @pytest.hookimpl(trylast=True)
    def pytest_collection_modifyitems(self, config: pytest.Config, items: list[pytest.Item]) -> None:
        # Quarantine: out unless asked for. A -m expression that names it does its own selecting.
        if not config.getoption("run_quarantine") and not _QUARANTINE_IN_EXPR.search(config.option.markexpr or ""):
            kept = [i for i in items if i.get_closest_marker("quarantine") is None]
            if len(kept) != len(items):
                config.hook.pytest_deselected(items=[i for i in items if i not in kept])
                items[:] = kept
        # After the shuffle (and any other reordering): pinned tests take back the slots they hold, in file order.
        slots = [n for n, item in enumerate(items) if item.nodeid in ORDER_PINNED]
        for slot, item in zip(slots, sorted((items[n] for n in slots), key=lambda i: i.stash[_ORDER_KEY]), strict=True):
            items[slot] = item
        self.pinned = len(slots)

    # -- the run: time per tier, and the flake log
    def _tier_of(self, nodeid: str) -> str:
        return self.tier_by_node.get(nodeid) or tier_for_path(nodeid.split("::", 1)[0])

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        tier = self._tier_of(report.nodeid)
        self.seconds[tier] += report.duration
        self.nodes[tier].add(report.nodeid)
        if report.outcome == "rerun":
            self.rerun_first_failure.setdefault(report.nodeid, _summary(report))
            self.attempt[report.nodeid] = {"call_passed": False, "failed": False}
        elif report.nodeid in self.rerun_first_failure:
            state = self.attempt.setdefault(report.nodeid, {"call_passed": False, "failed": False})
            state["failed"] |= report.outcome == "failed"
            state["call_passed"] |= report.when == "call" and report.outcome == "passed"

    def pytest_runtest_logfinish(self, nodeid: str) -> None:
        state = self.attempt.get(nodeid)
        if state is None or state["failed"] or not state["call_passed"]:
            return  # still to be rerun, or failed for good
        self.attempt.pop(nodeid)
        first = self.rerun_first_failure.pop(nodeid)
        self.flaked.append(nodeid)
        row = {"schema": SCHEMA_FLAKE, "ts": _now(), "nodeid": nodeid, "seed": self.seed, "first_failure": first}
        self._write("ws_flake_report", "flakes.jsonl", [row], "flake event")

    def _write(self, option: str, name: str, rows: list[dict[str, object]], what: str) -> None:
        try:
            target = self._target(option, name)
            if target is None:
                self.notes.append(f"{what} not written (no cache provider, and no --{option.replace('_', '-')})")
                return
            for row in rows:
                _append(target, row)
        except OSError as exc:
            self.notes.append(f"{what} not written ({exc})")

    @pytest.hookimpl(tryfirst=True)
    def pytest_sessionfinish(self, session: pytest.Session) -> None:
        if hasattr(self.config, "workerinput"):
            return  # an xdist worker: the controller reports
        ran = [t for t in TIERS if self.nodes[t]]
        if not ran:
            return
        ts, seed = _now(), self.seed
        rows = [
            {
                "schema": SCHEMA_TIERS,
                "ts": ts,
                "seed": seed,
                "tier": t,
                "duration_s": round(self.seconds[t], 3),
                "tests": len(self.nodes[t]),
                "budget_s": _tidy(self.budgets[t]),
            }
            for t in ran
        ]
        self._write("ws_tier_report", "tiers.jsonl", rows, "tier report")
        for row in rows:
            if row["duration_s"] > row["budget_s"]:  # type: ignore[operator]
                self.over.append(f"{row['tier']} tier over budget ({row['duration_s']} s of {row['budget_s']} s)")
        if self.over and ci_is_set() and session.exitstatus == pytest.ExitCode.OK:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED

    def pytest_terminal_summary(self, terminalreporter) -> None:
        line = terminalreporter.write_line
        if self.pinned:
            line(f"ws-harness: {self.pinned} test(s) pinned to file order (ORDER_PINNED)")
        if self.flaked:
            line(f"ws-harness: {len(self.flaked)} test(s) passed on rerun (flaky): {', '.join(self.flaked)}")
        ran = [t for t in TIERS if self.nodes[t]]
        if ran:
            line("ws-harness: " + "; ".join(f"{t} {len(self.nodes[t])} tests {self.seconds[t]:.1f} s" for t in ran))
        for message in self.notes:
            line(f"ws-harness: {message}")
        for message in self.over:
            failing = "failing the run (CI is set)" if ci_is_set() else "not failing the run (CI is not set)"
            line(f"ws-harness: {message}; {failing}", red=ci_is_set(), yellow=not ci_is_set())
