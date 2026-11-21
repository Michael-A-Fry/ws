"""The profile's read and write helpers: a [models] table round-trips, and a backup is taken before a rewrite."""

from __future__ import annotations

import tomllib
from datetime import UTC, datetime

from ws import profile

WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def test_a_models_table_round_trips_beside_the_roles():
    doc = {"models": {"default": "a/b", "jobs": "ollama/m:12b"}, "roles": {"budget": {"monthly_cap_usd": 30}}}
    assert tomllib.loads(profile.dumps(doc)) == doc


def test_backup_copies_the_file_with_a_utc_stamp(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text('# a comment the rewrite would drop\n[models]\ndefault = "x"\n', encoding="utf-8")
    saved = profile.backup(p, WHEN)
    assert saved == tmp_path / "profile.toml.bak-20260102T030405Z"
    assert saved.read_text(encoding="utf-8") == p.read_text(encoding="utf-8")


def test_a_second_backup_in_the_same_second_never_overwrites_the_first(tmp_path):
    p = tmp_path / "profile.toml"
    p.write_text("one\n", encoding="utf-8")
    first = profile.backup(p, WHEN)
    p.write_text("two\n", encoding="utf-8")
    second = profile.backup(p, WHEN)
    assert first != second and first.read_text() == "one\n" and second.read_text() == "two\n"


def test_no_profile_means_no_backup(tmp_path):
    assert profile.backup(tmp_path / "profile.toml", WHEN) is None
