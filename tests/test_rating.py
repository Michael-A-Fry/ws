"""`ws rate`: one line per rating in ratings.jsonl, only for a run that exists, and `ws run` prints the one-tap line."""

from __future__ import annotations

import json
import os
import re
import stat
import sys
from datetime import UTC, datetime

import pytest

from ws import cli, rating, runrecord


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".ws").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("WS_HOME", str(home / ".ws"))
    monkeypatch.chdir(home)
    return home


def add_run(box, run_id: str) -> str:
    rec = runrecord.new(
        run_id=run_id,
        job="daily",
        runner="claude-code",
        status="ok",
        ws_version="0",
        started_at="2026-01-01T00:00:00.000Z",
    )
    runrecord.append(runrecord.runs_path(os.environ), rec)
    return run_id


def run(capsys, *argv):
    code = cli.main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def ratings(box) -> list[dict]:
    p = box / ".ws" / "ratings.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.is_file() else []


def test_a_rating_is_one_line_with_the_run_the_time_and_who(box, capsys):
    rid = add_run(box, "20260101T000000Z-aaaaaa")
    code, out, _ = run(capsys, "rate", rid, "up")
    assert code == 0 and out.strip() == f"ws: rated {rid} up"
    (r,) = ratings(box)
    assert r == {"schema": 1, "run_id": rid, "ts": r["ts"], "rating": "up", "note": None, "who": "person"}
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", r["ts"])
    assert datetime.fromisoformat(r["ts"]).tzinfo is not None
    assert oct((box / ".ws" / "ratings.jsonl").stat().st_mode & 0o777) == "0o600"


def test_last_means_the_newest_record_in_the_run_log(box, capsys):
    add_run(box, "20260101T000000Z-aaaaaa")
    newest = add_run(box, "20260102T000000Z-bbbbbb")
    code, out, _ = run(capsys, "rate", "last", "down", "--note", "  wrong\n  file  ")
    assert code == 0 and newest in out
    (r,) = ratings(box)
    assert r["run_id"] == newest and r["rating"] == "down" and r["note"] == "wrong file"


def test_rating_again_adds_a_line_and_the_latest_counts(box, capsys):
    rid = add_run(box, "20260101T000000Z-aaaaaa")
    run(capsys, "rate", rid, "up")
    code, out, _ = run(capsys, "rate", rid, "down")
    assert code == 0 and "(was up)" in out
    assert [r["rating"] for r in ratings(box)] == ["up", "down"]
    assert rating.latest(ratings(box)) == {rid: "down"}


def test_an_unknown_run_or_an_empty_log_is_refused_and_writes_nothing(box, capsys):
    code, _, err = run(capsys, "rate", "last", "up")
    assert code == 1 and "no runs yet" in err
    add_run(box, "20260101T000000Z-aaaaaa")
    code, _, err = run(capsys, "rate", "20260101T000000Z-ffffff", "up")
    assert code == 1 and "no run" in err and "ws runs" in err
    assert ratings(box) == []


def test_only_up_or_down_and_a_long_note_is_cut(box, capsys):
    rid = add_run(box, "20260101T000000Z-aaaaaa")
    assert cli.main(["rate", rid, "meh"]) == 2
    assert "invalid choice" in capsys.readouterr().err
    assert run(capsys, "rate", rid, "up", "--note", "x" * 2000)[0] == 0
    assert len(ratings(box)[0]["note"]) == rating.NOTE_MAX


def test_a_damaged_line_is_skipped_not_fatal(box, capsys):
    rid = add_run(box, "20260101T000000Z-aaaaaa")
    (box / ".ws" / "ratings.jsonl").write_text("{broken\n[1]\n")
    assert run(capsys, "rate", rid, "up")[0] == 0
    assert rating.latest(rating.read(box / ".ws" / "ratings.jsonl")) == {rid: "up"}


def test_rating_new_rejects_what_the_command_would(box):
    with pytest.raises(ValueError):
        rating.new("x", "sideways")
    assert rating.new("x", "up", now=datetime(2026, 1, 1, tzinfo=UTC))["ts"] == "2026-01-01T00:00:00.000Z"


# ---- ws run prints the line --------------------------------------------------------------------------------------------


def test_ws_run_ends_its_hand_back_with_the_one_tap_line(box, capsys, monkeypatch):
    bin_dir = box.parent / "bin"
    bin_dir.mkdir()
    exe = bin_dir / "claude"
    out = json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "done", "session_id": "s"})
    exe.write_text(
        f"#!{sys.executable}\nimport sys\nif '--version' in sys.argv:\n    print('1.0.0')\n    sys.exit(0)\nprint({out!r})\n"
    )
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    (box / ".ws" / "profile.toml").write_text("[roles.budget]\nmonthly_cap_usd = 5\n")
    (box / ".ws" / "jobs").mkdir()
    (box / ".ws" / "jobs" / "daily.toml").write_text(
        'runner = "claude-code"\nmodel = "m"\nprompt = "hi"\nallow_unpriced = true\n'
    )
    code, out_text, _ = run(capsys, "run", "daily")
    assert code == 0, out_text
    (rec,) = runrecord.read(runrecord.runs_path(os.environ))
    lines = out_text.rstrip("\n").splitlines()
    assert lines[-1] == f"ws: rate: ws rate {rec['run_id']} up | down"
    assert run(capsys, "rate", "last", "up")[0] == 0
    assert ratings(box)[0]["run_id"] == rec["run_id"]
