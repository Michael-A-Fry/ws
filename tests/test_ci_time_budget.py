"""The CI time budget: a warning above 75% of a job's limit, quiet below it, never a failure, and the limit it is given
is the job's own `timeout-minutes` in ci.yml (read as text)."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "ci_time_budget.py"
CI = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")

spec = importlib.util.spec_from_file_location("ci_time_budget", SCRIPT)
assert spec and spec.loader
budget = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = budget
spec.loader.exec_module(budget)

START = 1_000_000


def run_at(tmp_path: Path, minutes_used: float, timeout: float = 20, capsys=None) -> tuple[int, str]:
    stamp = tmp_path / "job-start"
    stamp.write_text(f"{START}\n", encoding="utf-8")
    code = budget.main(
        ["--job", "ci", "--stamp", str(stamp), "--timeout-minutes", str(timeout)], now=START + minutes_used * 60
    )
    return code, capsys.readouterr().out


def test_warns_at_80_percent(tmp_path, capsys):
    code, out = run_at(tmp_path, 16, capsys=capsys)  # 16 of 20 minutes
    assert code == 0  # a warning, not a failure
    assert out.startswith("::warning ") and "16.0 of 20 min (80%)" in out


def test_quiet_at_50_percent(tmp_path, capsys):
    code, out = run_at(tmp_path, 10, capsys=capsys)
    assert code == 0
    assert "::warning" not in out and "10.0 of 20 min (50%)" in out


def test_the_line_is_exactly_75_percent_still_quiet_and_just_above_it_warns(tmp_path, capsys):
    assert "::warning" not in run_at(tmp_path, 15, capsys=capsys)[1]
    assert "::warning" in run_at(tmp_path, 15.2, capsys=capsys)[1]


@pytest.mark.parametrize("content", [None, "", "yesterday", "-5", "1.5"])
def test_a_missing_or_bad_stamp_is_one_line_and_exit_0(tmp_path, capsys, content):
    stamp = tmp_path / "job-start"
    if content is not None:
        stamp.write_text(content, encoding="utf-8")
    code = budget.main(["--job", "ci", "--stamp", str(stamp), "--timeout-minutes", "20"], now=START)
    out = capsys.readouterr().out
    assert code == 0 and len(out.splitlines()) == 1 and "no job-start stamp" in out and "::warning" not in out


def test_a_clock_that_went_back_is_not_a_negative_time(tmp_path, capsys):
    code, out = run_at(tmp_path, -5, capsys=capsys)
    assert code == 0 and "0.0 of 20 min (0%)" in out


def test_a_zero_limit_is_a_usage_error(tmp_path, capsys):
    stamp = tmp_path / "s"
    stamp.write_text("1", encoding="utf-8")
    assert budget.main(["--job", "ci", "--stamp", str(stamp), "--timeout-minutes", "0"]) == 2
    assert "more than zero" in capsys.readouterr().err


def test_the_tier_report_adds_one_line_for_each_tier(tmp_path, capsys):
    tiers = tmp_path / "tiers.jsonl"
    tiers.write_text(
        '{"schema":"ws.test-tiers/1","ts":"2026-11-08T05:21:55Z","seed":1,"tier":"gate","duration_s":212.481,'
        '"tests":2310,"budget_s":1200}\nnot json\n',
        encoding="utf-8",
    )
    stamp = tmp_path / "s"
    stamp.write_text(str(START), encoding="utf-8")
    assert budget.main(["--job", "ci", "--stamp", str(stamp), "--timeout-minutes", "20", "--tiers", str(tiers)]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out[1] == "  tier gate: 212.481 s of 1200 s, 2310 tests"
    assert budget.tier_lines(tmp_path / "missing") == []  # a report that isn't there adds nothing


def test_the_script_runs_as_a_program(tmp_path):
    stamp = tmp_path / "s"
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--job", "x", "--stamp", str(stamp), "--timeout-minutes", "5"],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert r.returncode == 0 and "no job-start stamp" in r.stdout


def jobs(text: str) -> dict[str, str]:
    """The text of each top-level job in a workflow."""
    body = text.split("\njobs:\n", 1)[1]
    parts = re.split(r"^  ([A-Za-z0-9_-]+):\n", body, flags=re.MULTILINE)
    return dict(zip(parts[1::2], parts[2::2], strict=True))


def test_budget_step_timeout_matches_each_jobs_timeout():
    found = jobs(CI)
    assert found, "no jobs in ci.yml"
    for name, text in found.items():
        limit = re.search(r"^    timeout-minutes:\s*(\d+)\s*(?:#.*)?$", text, re.MULTILINE)
        assert limit, f"{name} has no timeout-minutes"
        given = re.findall(r"scripts/ci_time_budget\.py\b[^\n]*?--timeout-minutes\s+(\d+)\b", text)
        assert given == [limit.group(1)], (
            f"{name}: the budget step is given {given}, the job's limit is {limit.group(1)}"
        )
