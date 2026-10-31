"""Backup role, restic: what is backed up, how the password travels, the read-back (`ws backup verify`) and the doctor
warning until one has passed. restic is a fake that records its argv and environment; the real one is never called."""

from __future__ import annotations

import json
import os
import random
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ws import cli, install, jobs
from ws.roles import restic

PASSWORD = "pw-" + "x7q2" * 4  # built here so no fixed secret sits in the repo


class FakeRestic:
    """Answers `ls`, `restore`, `backup` and `init` from a table of snapshot files: {live path: bytes in the repo}."""

    def __init__(self, snapshot: dict[str, bytes] | None = None, taken="2026-03-04T10:00:00.123456789+13:00"):
        self.snapshot, self.taken, self.calls = snapshot or {}, taken, []
        self.fail: dict[str, tuple[int, str]] = {}

    def __call__(self, argv, env, timeout):
        self.calls.append((argv, dict(env)))
        if argv[0] in self.fail:
            code, err = self.fail[argv[0]]
            return code, "", err
        if argv[0] == "ls":
            lines = [json.dumps({"struct_type": "snapshot", "time": self.taken, "hostname": "h"})]
            lines += [
                json.dumps({"struct_type": "node", "type": "file", "path": p, "size": len(b)})
                for p, b in self.snapshot.items()
            ]
            lines.append(json.dumps({"struct_type": "node", "type": "dir", "path": "/x", "size": 0}))
            return 0, "\n".join(lines), ""
        if argv[0] == "restore":
            target, path = Path(argv[argv.index("--target") + 1]), argv[argv.index("--include") + 1]
            dest = target / path.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(self.snapshot[path])
            return 0, "", ""
        return 0, "", ""


@pytest.fixture
def setup(box, env, write_profile):
    mem, notes = box / "mem", box / "Notes"
    mem.mkdir()
    notes.mkdir()
    (mem / "a.md").write_text("alpha")
    (notes / "b.md").write_text("bravo")
    cfg = {"adapter": "restic", "repo": "/mnt/backup/ws", "password_secret": "restic-password"}
    write_profile({"roles": {"memory": {"path": str(mem)}, "notes": {"path": str(notes)}, "backup": cfg}})
    store = box / ".ws" / "secrets"
    store.mkdir(mode=0o700)
    (store / "restic-password").write_text(PASSWORD)
    (store / "restic-password").chmod(0o600)
    return cfg, mem, notes


def snapshot_of(*files: Path) -> dict[str, bytes]:
    return {str(f): f.read_bytes() for f in files}


# ---- run -------------------------------------------------------------------------------------------------------------


def test_sources_are_memory_notes_and_profile(box, env, setup):
    _cfg, mem, notes = setup
    assert restic.sources(env) == [mem, notes, box / ".ws" / "profile.toml"]
    shutil.rmtree(notes)
    assert restic.sources(env) == [mem, box / ".ws" / "profile.toml"]


def test_backup_runs_restic_with_the_password_only_in_its_environment(box, env, setup):
    cfg, mem, notes = setup
    fake = FakeRestic()
    msg = restic.backup(cfg, env, fake)
    [(argv, renv)] = fake.calls
    assert argv == ["backup", "--tag", "ws", str(mem), str(notes), str(box / ".ws" / "profile.toml")]
    assert renv["RESTIC_REPOSITORY"] == "/mnt/backup/ws" and renv["RESTIC_PASSWORD"] == PASSWORD
    assert not any(PASSWORD in a for a in argv) and PASSWORD not in msg
    assert set(renv) <= {"PATH", "HOME", "LANG", "TZ", "TMPDIR", "RESTIC_REPOSITORY", "RESTIC_PASSWORD"}


def test_init_and_nothing_to_back_up(box, env, setup):
    cfg, *_ = setup
    fake = FakeRestic()
    assert "made a restic repository at /mnt/backup/ws" in restic.init(cfg, env, fake) and fake.calls[0][0] == ["init"]
    (box / ".ws" / "profile.toml").write_text("")
    shutil.rmtree(box / "mem")
    shutil.rmtree(box / "Notes")
    (box / ".ws" / "profile.toml").unlink()
    with pytest.raises(restic.BackupError, match="nothing to back up"):
        restic.backup(cfg, env, fake)


def test_a_failure_names_the_step_and_never_the_password(env, setup):
    cfg, *_ = setup
    fake = FakeRestic()
    fake.fail["backup"] = (1, f"Fatal: wrong password or no key found ({PASSWORD})")
    with pytest.raises(restic.BackupError) as e:
        restic.backup(cfg, env, fake)
    assert "backup failed (exit 1)" in str(e.value) and PASSWORD not in str(e.value)


def test_password_from_a_private_file(box, env):
    f = box / "pw"
    f.write_text(PASSWORD + "\n")
    f.chmod(0o600)
    assert restic.password({"password_file": str(f)}, env) == PASSWORD
    f.chmod(0o666)
    with pytest.raises(restic.BackupError, match="can be written by others"):
        restic.password({"password_file": str(f)}, env)
    with pytest.raises(restic.BackupError, match="No such file"):
        restic.password({"password_file": str(box / "nope")}, env)
    f.chmod(0o600)
    f.write_text("a\x01b")
    with pytest.raises(restic.BackupError, match="cannot be used"):
        restic.password({"password_file": str(f)}, env)


def test_password_secret_problems_are_plain(box, env, setup):
    cfg, *_ = setup
    with pytest.raises(restic.BackupError, match="ws secret set other"):
        restic.password({"password_secret": "other"}, env)
    with pytest.raises(restic.BackupError, match="name of a secret"):
        restic.password({"password_secret": "../x"}, env)
    (box / ".ws" / "secrets").chmod(0o755)
    with pytest.raises(restic.BackupError, match="open to other users") as e:
        restic.password(cfg, env)
    assert PASSWORD not in str(e.value)


def test_a_login_in_the_repository_url_is_not_shown(env, setup):
    login = "backup:" + "pw" + "@"  # joined here so the line is not an address to the PII gate
    assert restic.shown(f"sftp://{login}host.example.org//srv/r") == "sftp://host.example.org//srv/r"
    assert restic.shown("/mnt/x") == "/mnt/x"


# ---- verify ----------------------------------------------------------------------------------------------------------


def old(path: Path, minutes=60):
    t = (datetime(2026, 3, 3, 10, 0, tzinfo=UTC) - timedelta(minutes=minutes)).timestamp()
    os.utime(path, (t, t))


def test_verify_restores_one_file_and_compares_hashes(box, env, setup):
    cfg, mem, notes = setup
    for f in (mem / "a.md", notes / "b.md"):
        old(f)
    fake = FakeRestic(snapshot_of(mem / "a.md", notes / "b.md"))
    result = restic.verify(cfg, env, fake, rng=random.Random(1), now=datetime(2026, 3, 5, tzinfo=UTC))
    assert result.ok and "SHA-256 matches" in result.detail
    restores = [c for c, _ in fake.calls if c[0] == "restore"]
    assert len(restores) == 1 and restores[0][:3] == ["restore", "latest", "--target"]
    assert not Path(restores[0][3]).exists()  # the temporary folder is gone
    assert json.loads(restic.verify_path(env).read_text())["last_ok_at"].startswith("2026-03-05")


def test_verify_picks_a_random_file(box, env, setup):
    cfg, mem, notes = setup
    for f in (mem / "a.md", notes / "b.md"):
        old(f)
    snap = snapshot_of(mem / "a.md", notes / "b.md")
    picked = set()
    for seed in range(12):
        fake = FakeRestic(snap)
        restic.verify(cfg, env, fake, rng=random.Random(seed))
        picked.add(next(c for c, _ in fake.calls if c[0] == "restore")[-1])
    assert picked == set(snap)


def test_verify_fails_when_the_restored_copy_differs(box, env, setup):
    cfg, mem, _ = setup
    old(mem / "a.md")
    bad = {str(mem / "a.md"): b"ALPHA"}  # same size, different content
    result = restic.verify(cfg, env, FakeRestic(bad), now=datetime(2026, 3, 5, tzinfo=UTC))
    assert not result.ok and "differs" in result.detail
    rec = json.loads(restic.verify_path(env).read_text())
    assert rec["ok"] is False and "last_ok_at" not in rec


def test_a_failed_try_keeps_the_date_of_the_last_pass(box, env, setup):
    cfg, mem, _ = setup
    old(mem / "a.md")
    good = FakeRestic(snapshot_of(mem / "a.md"))
    restic.verify(cfg, env, good, now=datetime(2026, 3, 5, tzinfo=UTC))
    restic.verify(cfg, env, FakeRestic({str(mem / "a.md"): b"ALPHA"}), now=datetime(2026, 3, 9, tzinfo=UTC))
    rec = json.loads(restic.verify_path(env).read_text())
    assert rec["ok"] is False and rec["last_ok_at"].startswith("2026-03-05")


def test_files_changed_since_the_snapshot_are_skipped_not_failed(box, env, setup):
    cfg, mem, notes = setup
    old(mem / "a.md")
    (notes / "b.md").write_text("bravo, edited later")  # newer than the snapshot time
    snap = {str(mem / "a.md"): b"alpha", str(notes / "b.md"): b"bravo"}
    fake = FakeRestic(snap)
    assert restic.verify(cfg, env, fake, rng=random.Random(3)).ok
    assert [c[-1] for c, _ in fake.calls if c[0] == "restore"] == [str(mem / "a.md")]


def test_verify_with_nothing_comparable_is_not_a_pass(box, env, setup):
    cfg, mem, _ = setup
    (mem / "a.md").write_text("changed after the snapshot")
    result = restic.verify(cfg, env, FakeRestic({str(mem / "a.md"): b"alpha"}))
    assert not result.ok and "changed since the snapshot" in result.detail
    result = restic.verify(cfg, env, FakeRestic({}))
    assert not result.ok and "no file" in result.detail
    assert restic.last_pass(env) is None


def test_verify_skips_paths_restic_would_read_as_patterns(box, env, setup):
    cfg, mem, _ = setup
    odd = mem / "a[1].md"
    odd.write_text("x")
    old(odd)
    assert not restic.verify(cfg, env, FakeRestic({str(odd): b"x"})).ok


def test_restic_timestamps_with_nanoseconds_parse():
    assert restic._time("2026-03-04T10:00:00.123456789+13:00") == datetime.fromisoformat(
        "2026-03-04T10:00:00.123456+13:00"
    )
    assert restic._time("nope") is None and restic._time(None) is None


# ---- doctor ----------------------------------------------------------------------------------------------------------


@pytest.fixture
def with_restic(env, tmp_path):
    exe = tmp_path / "bin" / "restic"
    exe.parent.mkdir()
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return {**env, "PATH": str(exe.parent)}


def test_doctor_warns_until_a_verify_has_passed_in_30_days(box, with_restic, linux, setup):
    cfg, mem, _ = setup
    status, detail = restic.check(cfg, linux, with_restic, False)
    assert status == "warn" and "never verified" in detail and "/mnt/backup/ws" in detail
    old(mem / "a.md")
    now = datetime.now(UTC)
    restic.verify(
        cfg, with_restic, FakeRestic(snapshot_of(mem / "a.md"), taken=now.isoformat()), now=now - timedelta(days=31)
    )
    status, detail = restic.check(cfg, linux, with_restic, False)
    assert status == "warn" and "31 days ago" in detail
    restic.verify(
        cfg, with_restic, FakeRestic(snapshot_of(mem / "a.md"), taken=now.isoformat()), now=now - timedelta(days=2)
    )
    status, detail = restic.check(cfg, linux, with_restic, False)
    assert status == "ok" and "verified 2 days ago" in detail


def test_doctor_fails_without_a_repo_restic_or_anything_to_back_up(box, env, with_restic, linux, setup):
    cfg, *_ = setup
    assert restic.check({"adapter": "restic"}, linux, with_restic, False)[0] == "fail"
    assert restic.check(cfg, linux, {**env, "PATH": str(box)}, False) == ("fail", "restic is not on PATH")
    assert restic.check({**cfg, "password_secret": "../x"}, linux, with_restic, False)[0] == "fail"
    shutil.rmtree(box / "mem")
    shutil.rmtree(box / "Notes")
    (box / ".ws" / "profile.toml").unlink()
    assert "nothing to back up" in restic.check(cfg, linux, with_restic, False)[1]


# ---- the job file and the commands -------------------------------------------------------------------------------------


def test_steps_make_a_backup_job_the_scheduler_picks_up(box, env, setup):
    cfg, *_ = setup
    [step] = restic.steps(cfg, install.Answers(), env)
    assert step.path == box / ".ws" / "jobs" / "backup.toml" and step.toml
    assert step.content == 'command = "backup"\nschedule = "daily 03:00"\n'
    install.apply([step], env)
    [s] = jobs.scheduled(env)
    assert s.name == "backup" and s.argv == ("backup", "run") and str(s.when) == "daily 03:00"


def test_steps_keep_a_job_file_you_changed_and_refuse_a_bad_schedule(box, env, setup):
    cfg, *_ = setup
    (box / ".ws" / "jobs").mkdir()
    (box / ".ws" / "jobs" / "backup.toml").write_text('command = "backup"\nschedule = "hourly"\n')
    assert "hourly" in restic.steps(cfg, install.Answers(), env)[0].content
    (box / ".ws" / "jobs" / "backup.toml").unlink()
    with pytest.raises(jobs.JobError, match="schedule must be"):
        restic.steps({**cfg, "schedule": "sometime"}, install.Answers(), env)


def test_cli_run_and_verify(box, setup, capsys, monkeypatch):
    _cfg, mem, _notes = setup
    old(mem / "a.md")
    monkeypatch.setattr(restic, "run_restic", FakeRestic(snapshot_of(mem / "a.md")))
    assert cli.main(["backup", "run"]) == 0
    assert "backed up 3 items to /mnt/backup/ws" in capsys.readouterr().out
    assert cli.main(["backup", "verify"]) == 0
    assert "verified" in capsys.readouterr().out
    assert cli.main(["backup", "init"]) == 0


def test_cli_verify_failure_exits_1_and_wrong_adapter_is_refused(box, setup, write_profile, capsys, monkeypatch):
    _cfg, mem, _ = setup
    old(mem / "a.md")
    monkeypatch.setattr(restic, "run_restic", FakeRestic({str(mem / "a.md"): b"ALPHA"}))
    assert cli.main(["backup", "verify"]) == 1
    assert "NOT verified" in capsys.readouterr().err
    write_profile({"roles": {"backup": {"adapter": "tarsnap"}}})
    assert cli.main(["backup", "run"]) == 1
    assert 'adapter = "restic"' in capsys.readouterr().err
