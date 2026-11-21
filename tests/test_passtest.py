"""ws passtest events: the step-in counter over a planted, hash-chained ledger. No clock: `now` is passed in."""

from __future__ import annotations

import hashlib
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from ws import cli, passtest
from ws.output import WsError

NOW = datetime(2030, 1, 9, 12, tzinfo=UTC)  # a Wednesday; its week starts Monday 7 January
MON = datetime(2030, 1, 7, 9, tzinfo=UTC)


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


def plant(path, entries):
    """Write `entries` (ts, kind, data) as a chained ledger, the same bytes the ledger helper writes."""
    prev, out = passtest.GENESIS, b""
    for ts, kind, data in entries:
        raw = (
            json.dumps({"ts": iso(ts), "kind": kind, "data": data, "prev": prev}, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode()
        prev = hashlib.sha256(raw.rstrip(b"\n")).hexdigest()  # the helper hashes a line without its newline
        out += raw
    path.write_bytes(out)
    return path


def prop(ts, pid, kind, area="alpha", confirm=True):
    rows = [(ts, "proposal", {"id": pid, "event_kind": kind, "area": area, "about": "x"})]
    if confirm:
        rows.append((ts + timedelta(hours=1), "confirm", {"proposal_id": pid}))
    return rows


def counted(tmp_path, entries, weeks=4, areas=()):
    lines = passtest.read_ledger(plant(tmp_path / "ledger.jsonl", entries))
    return passtest.count(lines, weeks, NOW, areas)


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "ws"))
    monkeypatch.delenv(passtest.CONFIG_ENV, raising=False)
    monkeypatch.delenv(passtest.LEDGER_ENV, raising=False)
    monkeypatch.setattr(passtest, "_now", lambda: NOW)
    return tmp_path


def test_a_planted_week_prints_the_right_count_per_kind(tmp_path):
    rows = []
    for i, (kind, n) in enumerate({"dig": 3, "reframe": 1, "correct": 2, "follow-up": 1, "not-automatic": 1}.items()):
        for j in range(n):
            rows += prop(MON + timedelta(minutes=10 * i + j), f"p{kind}{j}", kind)
    c = counted(tmp_path, rows)
    assert c.by_kind[-1] == {"dig": 3, "reframe": 1, "correct": 2, "follow-up": 1, "not-automatic": 1}
    assert c.totals == [0, 0, 0, 8]


def test_a_planted_week_across_two_areas_prints_the_right_count_for_each(tmp_path):
    rows = prop(MON, "a1", "dig", "alpha") + prop(MON, "a2", "correct", "alpha") + prop(MON, "b1", "dig", "beta")
    c = counted(tmp_path, rows, areas=("alpha", "beta", "gamma"))
    assert c.by_area[-1] == {"alpha": 2, "beta": 1, "gamma": 0}
    assert c.by_area[0] == {"alpha": 0, "beta": 0, "gamma": 0}


def series(per_week):
    rows = []
    for w, n in enumerate(per_week):
        start = MON - timedelta(weeks=len(per_week) - 1 - w)
        rows += [r for i in range(n) for r in prop(start + timedelta(minutes=i), f"w{w}-{i}", "dig")]
    return rows


def test_a_rising_series_prints_a_rising_trend(tmp_path):
    c = counted(tmp_path, series([1, 2, 4, 6]))
    assert c.trend == "rising" and c.slope == pytest.approx(1.7)


def test_a_falling_series_prints_a_falling_trend(tmp_path):
    c = counted(tmp_path, series([6, 4, 2, 1]))
    assert c.trend == "falling" and c.slope == pytest.approx(-1.7)


def test_a_steady_series_is_flat_and_the_band_is_strict():
    assert passtest.trend_of(passtest.slope([3, 3, 3, 3])) == "flat"
    assert passtest.trend_of(0.5) == "flat" and passtest.trend_of(-0.5) == "flat"
    assert passtest.slope([2]) == 0.0


def test_a_failed_run_yields_one_not_automatic_even_when_logged_twice(tmp_path):
    run = {"job": "nightly", "run_id": "r1", "status": "failed", "expected_at": iso(MON)}
    rows = [(MON, "job-run", run), (MON + timedelta(minutes=5), "job-run", run)]
    rows.append((MON, "job-run", {"job": "nightly", "run_id": "r2", "status": "ok", "expected_at": iso(MON)}))
    c = counted(tmp_path, rows)
    assert c.by_kind[-1]["not-automatic"] == 1 and c.totals[-1] == 1


def test_a_missed_run_counts_in_the_week_it_was_expected(tmp_path):
    expected = MON - timedelta(days=2)  # the week before
    rows = [(MON, "job-run", {"job": "j", "run_id": "m1", "status": "missed", "expected_at": iso(expected)})]
    assert counted(tmp_path, rows).totals == [0, 0, 1, 0]


def test_a_proposal_left_open_7_days_counts_as_confirmed(tmp_path):
    old = NOW - timedelta(days=7)
    young = NOW - timedelta(days=6, hours=23)
    rows = prop(old, "old", "reframe", confirm=False) + prop(young, "young", "dig", confirm=False)
    c = counted(tmp_path, rows)
    assert sum(w["reframe"] for w in c.by_kind) == 1 and sum(c.totals) == 1
    assert c.open == 1


def test_a_dismissed_proposal_does_not_count_and_the_latest_decision_wins(tmp_path):
    rows = prop(MON, "d", "dig", confirm=False) + [(MON, "dismiss", {"proposal_id": "d"})]
    rows += prop(MON, "e", "dig") + [(MON + timedelta(hours=2), "dismiss", {"proposal_id": "e"})]
    rows += prop(MON, "f", "dig", confirm=False) + [
        (MON, "dismiss", {"proposal_id": "f"}),
        (MON, "confirm", {"proposal_id": "f"}),
    ]
    assert counted(tmp_path, rows).totals[-1] == 1


def test_bad_lines_are_skipped_and_said(tmp_path):
    rows = [(MON, "proposal", {"id": "x", "event_kind": "nope", "area": "a"}), (MON, "version-closed", {})]
    c = counted(tmp_path, rows)
    assert c.totals[-1] == 0 and len(c.skipped) == 1 and "unknown event_kind" in c.skipped[0]


def test_a_broken_chain_is_refused_with_its_line_number(tmp_path):
    led = plant(tmp_path / "ledger.jsonl", prop(MON, "a", "dig") + prop(MON, "b", "dig"))
    lines = led.read_bytes().split(b"\n")
    lines[1] = lines[1].replace(b'"proposal_id":"a"', b'"proposal_id":"b"')
    led.write_bytes(b"\n".join(lines))
    with pytest.raises(WsError) as e:
        passtest.read_ledger(led)
    assert e.value.exit_code == 1 and e.value.details == {"line": 3}


def test_a_truncated_ledger_is_refused(tmp_path):
    led = plant(tmp_path / "ledger.jsonl", prop(MON, "a", "dig"))
    led.write_bytes(led.read_bytes()[:-1])
    with pytest.raises(WsError, match="truncated"):
        passtest.read_ledger(led)


def test_the_command_prints_weeks_areas_and_trend(env, capsys, monkeypatch):
    led = plant(env / "ledger.jsonl", series([1, 2, 4, 6]))
    cfg = env / "passtest.toml"
    cfg.write_text('ledger = "ledger.jsonl"\nareas = ["alpha", "beta"]\n', encoding="utf-8")
    monkeypatch.setenv(passtest.CONFIG_ENV, str(cfg))
    assert cli.main(["passtest", "events", "--weeks", "4"]) == 0
    out = capsys.readouterr().out
    assert "2030-01-07" in out and "trend: rising (+1.70 a week" in out and "beta" in out and str(led) in out
    assert cli.main(["passtest", "events", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert [w["total"] for w in data["weeks"]] == [1, 2, 4, 6] and data["trend"]["direction"] == "rising"


def test_the_ledger_env_overrides_the_config(env, capsys, monkeypatch):
    plant(env / "other.jsonl", prop(MON, "a", "dig"))
    cfg = env / "passtest.toml"
    cfg.write_text('ledger = "missing.jsonl"\n', encoding="utf-8")
    monkeypatch.setenv(passtest.LEDGER_ENV, str(env / "other.jsonl"))
    assert cli.main(["passtest", "events", "--config", str(cfg), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["weeks"][-1]["total"] == 1


@pytest.mark.parametrize(
    ("text", "kind", "code"),
    [
        (None, "no_ledger", 1),
        ('ledger = "gone.jsonl"\n', "not_found", 1),
        ("bogus = 1\n", "bad_config", 2),
        ("areas = 3\n", "bad_config", 2),
    ],
)
def test_refusals_are_typed(env, capsys, text, kind, code):
    argv = ["passtest", "events", "--json"]
    if text is not None:
        (env / "p.toml").write_text(text, encoding="utf-8")
        argv += ["--config", str(env / "p.toml")]
    assert cli.main(argv) == code
    assert json.loads(capsys.readouterr().out)["error_kind"] == kind


def test_weeks_out_of_range_is_a_usage_error(env, capsys, monkeypatch):
    monkeypatch.setenv(passtest.LEDGER_ENV, str(plant(env / "l.jsonl", [])))
    assert cli.main(["passtest", "events", "--weeks", "0"]) == 2


def test_nothing_in_it_writes_a_reset_line_or_writes_at_all(env, capsys, monkeypatch):
    led = plant(env / "ledger.jsonl", prop(MON, "a", "dig") + prop(NOW, "b", "dig", confirm=False))
    before = led.read_bytes()
    led.chmod(0o444)
    monkeypatch.setenv(passtest.LEDGER_ENV, str(led))
    assert cli.main(["passtest", "events"]) == 0
    assert led.read_bytes() == before
    src = inspect.getsource(passtest)
    for writer in ("reset", "write_text", "write_bytes", "os.open", '"a"', '"w"', "'a'", "'w'", ".write("):
        assert writer not in src, writer


def test_a_wrong_start_to_the_chain_is_refused_at_line_1(tmp_path):
    led = plant(tmp_path / "ledger.jsonl", prop(MON, "a", "dig"))
    led.write_bytes(led.read_bytes().replace(passtest.GENESIS.encode(), b"1" * 64, 1))
    with pytest.raises(WsError) as e:
        passtest.read_ledger(led)
    assert e.value.details == {"line": 1}


def test_a_blank_line_is_read_past_and_line_numbers_are_the_files_own(tmp_path):
    led = plant(tmp_path / "ledger.jsonl", prop(MON, "a", "dig"))
    first, second, _ = led.read_bytes().split(b"\n")
    led.write_bytes(first.replace(b'"about":"x"', b'"about":"y"') + b"\n\n" + second + b"\n")
    with pytest.raises(WsError) as e:
        passtest.read_ledger(led)
    assert e.value.details == {"line": 3}
    led.write_bytes(first + b"\n\n" + second + b"\n")
    assert [ln.n for ln in passtest.read_ledger(led)] == [1, 3]


def test_the_week_turns_at_monday_midnight_utc_and_the_window_has_edges(tmp_path):
    sun = MON.replace(hour=0) - timedelta(seconds=1)
    rows = prop(sun, "sun", "dig") + prop(MON.replace(hour=0), "mon", "dig")
    rows += prop(MON - timedelta(weeks=4), "early", "dig")  # one week before a 4-week window
    c = counted(tmp_path, rows)
    assert c.totals == [0, 0, 1, 1] and c.weeks[0] == MON.replace(hour=0) - timedelta(weeks=3)
    one = counted(tmp_path, rows, weeks=1)
    assert one.totals == [1] and one.trend == "flat"


def test_a_time_out_of_range_is_skipped_not_a_crash(tmp_path):
    rows = [
        (MON, "job-run", {"job": "j", "run_id": "r", "status": "failed", "expected_at": "9999-12-31T23:59:59-05:00"})
    ]
    assert counted(tmp_path, rows).totals == [0, 0, 0, 1]  # falls back to the line's own ts
    assert passtest._when("0001-01-01T00:00:00+05:00") is None


@pytest.mark.parametrize("weeks", ["-1", str(passtest.MAX_WEEKS + 1)])
def test_weeks_outside_the_range_are_usage_errors(env, capsys, monkeypatch, weeks):
    monkeypatch.setenv(passtest.LEDGER_ENV, str(plant(env / "l.jsonl", [])))
    assert cli.main(["passtest", "events", "--weeks", weeks]) == 2
