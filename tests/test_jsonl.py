"""`ws.jsonl`: bounded JSONL logs. A long field is cut and marked, a full file rotates into `name.jsonl.N`, reads span the
old files, a file holding this month's records is never dropped, and two writers never lose a line."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading

import pytest

from ws import jsonl, rating, runrecord

SMALL = {"max_bytes": 200, "keep": 3}


def rec(i: int, pad: int = 0) -> dict:
    return {"schema": 1, "ts": "t", "i": i, "pad": "x" * pad}


def ids(path, **kw) -> list[int]:
    return [r["i"] for r in jsonl.read(path, **kw)]


def test_a_long_string_is_cut_with_a_marker_and_its_original_length(tmp_path):
    p = tmp_path / "a.jsonl"
    jsonl.append(p, rec(1) | {"text": "y" * 100, "nested": {"list": ["z" * 100, 5, None]}}, field_cap=10)
    (r,) = jsonl.read(p)
    assert r["text"] == "y" * 10 + "[truncated: 100 chars]"
    assert r["nested"]["list"] == ["z" * 10 + "[truncated: 100 chars]", 5, None]
    assert jsonl.DEFAULT_FIELD_CAP == 16 * 1024 and jsonl.DEFAULT_MAX_BYTES == 5 * 1024 * 1024


def test_a_string_at_the_cap_and_the_keys_are_left_alone(tmp_path):
    out = jsonl.cap_fields({"k" * 50: "a" * 10}, 10)
    assert out == {"k" * 50: "a" * 10}


def test_the_default_cap_is_16_kib(tmp_path):
    p = tmp_path / "a.jsonl"
    jsonl.append(p, rec(1) | {"text": "q" * 20000})
    (r,) = jsonl.read(p)
    assert r["text"].startswith("q" * 16384) and r["text"].endswith("[truncated: 20000 chars]")


def test_a_full_file_rotates_and_a_read_spans_all_of_it_oldest_first(tmp_path):
    p = tmp_path / "a.jsonl"
    for i in range(12):
        jsonl.append(p, rec(i, 40), **SMALL)
    assert (tmp_path / "a.jsonl.1").exists()
    assert ids(p) == sorted(ids(p))
    assert ids(p)[-1] == 11
    for f in jsonl.files(p):
        assert f.stat().st_size <= SMALL["max_bytes"] + 100  # each file stops about at the size, never far past it


def test_only_keep_old_files_are_kept_and_the_oldest_records_go(tmp_path):
    p = tmp_path / "a.jsonl"
    for i in range(40):
        jsonl.append(p, rec(i, 40), **SMALL)
    names = sorted(x.name for x in tmp_path.iterdir() if x.name.startswith("a.jsonl.") and x.name[-1].isdigit())
    assert names == ["a.jsonl.1", "a.jsonl.2", "a.jsonl.3"]
    got = ids(p)
    assert got[-1] == 39 and 0 not in got and got == list(range(got[0], 40))  # a clean tail, nothing missing inside it


def test_a_file_with_a_protected_record_is_kept_past_the_limit(tmp_path):
    p = tmp_path / "a.jsonl"
    keep_if = lambda r: r.get("keep") is True
    jsonl.append(p, rec(0, 40) | {"keep": True}, keep_if=keep_if, **SMALL)
    for i in range(1, 40):
        jsonl.append(p, rec(i, 40), keep_if=keep_if, **SMALL)
    assert 0 in ids(p), "the protected record was dropped"
    assert len(jsonl.archives(p)) > 3  # kept beyond `keep`, because it holds a record that must not be lost


def test_a_gap_in_the_numbers_is_closed_when_rotating(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text(json.dumps(rec(3)) + "\n")
    (tmp_path / "a.jsonl.3").write_text(json.dumps(rec(2)) + "\n")
    (tmp_path / "a.jsonl.4").write_text(json.dumps(rec(1)) + "\n")
    jsonl.append(p, rec(4, 300), **SMALL)  # too big for the live file's room: rotates first
    assert ids(p) == [1, 2, 3, 4]
    assert [n for n, _ in jsonl.archives(p)] == [1, 2, 3]


def test_a_record_bigger_than_the_limit_still_goes_in_alone(tmp_path):
    p = tmp_path / "a.jsonl"
    jsonl.append(p, rec(1, 10), **SMALL)
    jsonl.append(p, rec(2, 1000), **SMALL)
    jsonl.append(p, rec(3, 10), **SMALL)
    assert ids(p) == [1, 2, 3]


def test_files_are_private(tmp_path):
    p = tmp_path / "logs" / "a.jsonl"
    for i in range(6):
        jsonl.append(p, rec(i, 40), **SMALL)
    for f in tmp_path.rglob("a.jsonl*"):
        assert stat.S_IMODE(f.stat().st_mode) == 0o600, f.name
    assert stat.S_IMODE(p.parent.stat().st_mode) == 0o700


def test_a_file_made_with_a_wider_mode_is_narrowed(tmp_path):
    p = tmp_path / "a.jsonl"
    p.write_text("")
    p.chmod(0o644)
    jsonl.append(p, rec(1))
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


def test_a_damaged_line_is_skipped_and_a_missing_log_is_no_records(tmp_path):
    p = tmp_path / "a.jsonl"
    assert jsonl.read(p) == []
    p.write_text('{broken\n[1]\n{"i": 1}\n')
    assert ids(p) == [1]


def test_old_files_and_the_lock_can_live_in_a_side_folder(tmp_path):
    p, side = tmp_path / "a.jsonl", tmp_path / "logs"
    for i in range(12):
        jsonl.append(p, rec(i, 40), side_dir=side, **SMALL)
    assert sorted(x.name for x in side.iterdir()) == ["a.jsonl.1", "a.jsonl.2", "a.jsonl.3", "a.jsonl.lock"]
    assert not (tmp_path / "a.jsonl.1").exists()
    assert ids(p, side_dir=side)[-1] == 11
    # a file planted beside the log is not read as part of it
    (tmp_path / "a.jsonl.9").write_text(json.dumps(rec(99)) + "\n")
    assert 99 not in ids(p, side_dir=side)


def test_ratings_rotate_into_logs_and_read_across_it(tmp_path):
    path = tmp_path / "ratings.jsonl"
    for i in range(8):
        jsonl.append(path, rating.new(f"20260101T00000{i}Z-aaaaaa", "up"), max_bytes=300, side_dir=tmp_path / "logs")
    assert (tmp_path / "logs" / "ratings.jsonl.1").exists()
    assert len(rating.read(path)) == 8


def test_runrecord_append_and_read_cross_rotation_so_spend_is_not_undercounted(tmp_path):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    p = tmp_path / "logs" / "runs.jsonl"
    for i in range(10):
        r = runrecord.new(
            run_id=f"{now:%Y%m%dT%H%M%SZ}-{i:06x}", job="j", started_at=runrecord.iso(now), status="ok",
            exit_code=0, cost_usd=1.0, ws_version="0",
        )  # fmt: skip
        runrecord.append(p, r, max_bytes=800, keep=2)
    assert len(jsonl.archives(p)) > 2, "this month's records must be kept past `keep`"
    assert len(runrecord.read(p)) == 10
    assert runrecord.month_spend(runrecord.read(p), now) == 10.0


def test_a_record_from_an_old_month_is_dropped_when_it_falls_out(tmp_path):
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    old = runrecord.new(run_id="20200101T000000Z-aaaaaa", job="j", started_at="2020-01-01T00:00:00.000Z", status="ok")
    p = tmp_path / "runs.jsonl"
    runrecord.append(p, old, max_bytes=800, keep=2)
    for i in range(12):
        r = runrecord.new(
            run_id=f"{now:%Y%m%dT%H%M%SZ}-{i:06x}", job="j", started_at=runrecord.iso(now), status="ok", ws_version="0"
        )
        runrecord.append(p, r, max_bytes=800, keep=2)
    assert "20200101T000000Z-aaaaaa" not in {r["run_id"] for r in runrecord.read(p)}


def test_two_threads_appending_through_rotation_lose_nothing(tmp_path):
    p = tmp_path / "a.jsonl"
    per = 60

    def work(base):
        for i in range(per):
            jsonl.append(p, rec(base + i, 30), max_bytes=400, keep=1000)

    threads = [threading.Thread(target=work, args=(b,)) for b in (0, 1000, 2000)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert sorted(ids(p)) == sorted(b + i for b in (0, 1000, 2000) for i in range(per))


def test_two_processes_appending_through_rotation_lose_nothing(tmp_path):
    p = tmp_path / "a.jsonl"
    code = (
        "import sys\nfrom pathlib import Path\nfrom ws import jsonl\n"
        "base = int(sys.argv[2])\n"
        "for i in range(40):\n"
        "    jsonl.append(Path(sys.argv[1]), {'schema': 1, 'ts': 't', 'i': base + i, 'pad': 'x' * 30}, max_bytes=400, keep=1000)\n"
    )
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)}
    procs = [subprocess.Popen([sys.executable, "-c", code, str(p), str(b)], env=env) for b in (0, 1000, 2000)]
    for pr in procs:
        assert pr.wait(timeout=120) == 0
    assert sorted(ids(p)) == sorted(b + i for b in (0, 1000, 2000) for i in range(40))


def test_a_lock_held_too_long_is_a_typed_timeout_error(tmp_path):
    import fcntl

    p = tmp_path / "a.jsonl"
    jsonl.append(p, rec(1))
    fd = os.open(tmp_path / "a.jsonl.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        with pytest.raises(jsonl.LockTimeout) as e:
            jsonl.append(p, rec(2), lock_timeout_s=0.1)
        assert isinstance(e.value, OSError) and "locked" in str(e.value)
        with pytest.raises(jsonl.LockTimeout):
            jsonl.read(p, lock_timeout_s=0.1)
    finally:
        os.close(fd)
    jsonl.append(p, rec(2), lock_timeout_s=0.1)  # free again
    assert ids(p) == [1, 2]


def test_a_log_or_lock_that_is_a_symlink_is_refused_and_never_written_through(tmp_path):
    target = tmp_path / "target.txt"
    target.write_text("keep")
    p = tmp_path / "a.jsonl"
    p.symlink_to(target)
    with pytest.raises(OSError):
        jsonl.append(p, rec(1))
    with pytest.raises(OSError):
        jsonl.read(p)
    assert target.read_text() == "keep"
    p.unlink()
    (tmp_path / "a.jsonl.lock").unlink(missing_ok=True)
    (tmp_path / "a.jsonl.lock").symlink_to(target)
    with pytest.raises(OSError):
        jsonl.append(p, rec(1))
    assert target.read_text() == "keep"


def test_rotation_keeps_a_file_with_a_record_from_last_month_too(tmp_path):
    from datetime import UTC, datetime, timedelta

    last = runrecord.iso(datetime.now(UTC).replace(day=1) - timedelta(days=1))
    p = tmp_path / "runs.jsonl"
    old = runrecord.new(run_id="20260101T000000Z-aaaaaa", job="j", started_at=last, status="ok")
    runrecord.append(p, old, max_bytes=800, keep=1)
    for i in range(4):
        now = datetime.now(UTC)
        r = runrecord.new(run_id=f"{now:%Y%m%dT%H%M%SZ}-{i:06x}", job="j", started_at=runrecord.iso(now), status="ok")
        runrecord.append(p, r, max_bytes=800, keep=1)
    assert "20260101T000000Z-aaaaaa" in {r["run_id"] for r in runrecord.read(p)}


def test_stamped_puts_schema_and_ts_first_and_keeps_every_field():
    out = jsonl.stamped({"at": "legacy", "x": 1}, 3, 86400.5)
    assert out == {"schema": 3, "ts": "1970-01-02T00:00:00.500Z", "at": "legacy", "x": 1}
    assert list(out)[:2] == ["schema", "ts"]
    assert jsonl.stamped({}, 1)["ts"].endswith("Z")


def test_when_takes_ts_then_the_legacy_at_and_nothing_else():
    assert jsonl.when({"ts": "a", "at": "b"}) == "a"
    assert jsonl.when({"at": "b"}) == "b"
    assert jsonl.when({"ts": 5, "at": ""}) is None
    assert jsonl.when({"t": "c"}) is None


def test_a_line_without_a_schema_or_a_ts_is_refused_before_anything_is_written(tmp_path):
    p = tmp_path / "x.jsonl"
    for bad in (
        {"i": 1},
        {"schema": 1},
        {"ts": "t"},
        {"schema": "1", "ts": "t"},
        {"schema": True, "ts": "t"},
        {"schema": 1, "ts": ""},
        [1],
    ):
        with pytest.raises(ValueError, match="schema"):
            jsonl.append(p, bad)
        with pytest.raises(ValueError, match="schema"):
            jsonl.append_plain(p, bad)
    assert not p.exists()
    jsonl.append(p, {"schema": 1, "started_at": "t"}, time_keys=("started_at",))  # a log with its own time key
    with pytest.raises(ValueError):
        jsonl.append(p, {"schema": 1, "ts": "t"}, time_keys=("started_at",))
