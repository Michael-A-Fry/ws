"""ws memory: add (with a commit), search over memory and notes, the rebuildable FTS5 index, the no-FTS5 fallback."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from ws import cli, doctor, frontmatter, memory, notes, osinfo
from ws.profile import dumps, profile_path

WHEN = datetime(2026, 10, 31, 9, 30, 5, tzinfo=timezone(timedelta(hours=13)))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A WS_HOME, a notes folder and a memory folder (not yet a repo), and a git that knows no one."""
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    monkeypatch.setenv("HOME", str(tmp_path / "userhome"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setattr(memory, "_warned", False)
    (tmp_path / "vault").mkdir()
    p = profile_path()
    p.parent.mkdir(parents=True)
    p.write_text(
        dumps({"roles": {"notes": {"path": str(tmp_path / "vault")}, "memory": {"path": str(tmp_path / "mem")}}})
    )
    return tmp_path


@pytest.fixture
def mem(home):
    memory.init(home / "mem")
    return home / "mem"


def run_git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout


def paths(hits):
    return [h.path for h in hits]


def test_init_makes_a_repo_and_a_second_run_changes_nothing(home, capsys):
    assert cli.main(["memory", "init"]) == 0
    assert "made" in capsys.readouterr().out
    assert (home / "mem" / ".git").exists()
    assert cli.main(["memory", "init"]) == 0
    assert "0 changes" in capsys.readouterr().out


def test_init_leaves_a_repo_with_its_own_identity_alone(home):
    (home / "mem").mkdir()
    subprocess.run(["git", "init", "-q"], cwd=home / "mem", check=True)
    subprocess.run(["git", "config", "user.name", "Repo Owner"], cwd=home / "mem", check=True)
    subprocess.run(["git", "config", "user.email", "owner@example.com"], cwd=home / "mem", check=True)
    assert memory.init(home / "mem") is False
    memory.add("a fact", env=None)
    assert run_git(home / "mem", "log", "-1", "--format=%an <%ae>").strip() == "Repo Owner <owner@example.com>"


def test_add_writes_a_file_and_one_clean_commit(mem):
    path = memory.add("We chose SQLite for the index.", "Use SQLite", "decision", ["db", "search"], now=WHEN)
    assert path == mem / "decisions" / "use-sqlite.md"
    doc = frontmatter.parse(path.read_text(encoding="utf-8"))
    assert doc.clean and list(doc.meta) == ["id", "created", "kind", "title", "tags", "source"]
    assert doc.meta["kind"] == "decision" and doc.meta["source"] == "cli" and doc.meta["tags"] == ["db", "search"]
    assert doc.body == "We chose SQLite for the index.\n"
    assert run_git(mem, "log", "--format=%B").strip() == "memory: add use-sqlite"
    assert run_git(mem, "log", "--format=%(trailers)").strip() == ""  # no Co-authored-by, Signed-off-by or similar
    assert run_git(mem, "status", "--porcelain") == ""
    assert run_git(mem, "log", "-1", "--format=%an").strip() == "ws"  # the fallback identity when git has none


def test_add_never_overwrites_and_each_kind_has_its_folder(mem):
    first = memory.add("one", "Same title")
    second = memory.add("two", "Same title")
    assert (first.name, second.name) == ("same-title.md", "same-title-2.md")
    assert "one" in first.read_text(encoding="utf-8")
    for kind in memory.KINDS:
        assert memory.add("x", kind=kind).parent == mem / f"{kind}s"
    assert len(run_git(mem, "log", "--format=%s").splitlines()) == 2 + len(memory.KINDS)


def test_add_refuses_bad_input_and_a_folder_that_is_not_a_repo(home):
    with pytest.raises(notes.WsError, match="not a git repo") as e:
        memory.add("x")
    assert e.value.kind == "not_found" and "memory init" in e.value.hint
    memory.init(home / "mem")
    with pytest.raises(notes.WsError, match="kind"):
        memory.add("x", kind="secret")
    with pytest.raises(notes.WsError, match="empty"):
        memory.add("  ")


def test_cli_add_and_search_with_a_source_column(home, mem, capsys):
    assert cli.main(["memory", "add", "Prefers", "tea", "over", "coffee", "--kind", "preference", "--tag", "food"]) == 0
    added = Path(capsys.readouterr().out.strip())
    assert added.parent == mem / "preferences"
    notes.capture("Buy tea bags on Friday", home / "vault", now=WHEN)
    assert cli.main(["memory", "search", "tea"]) == 0
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if not ln.startswith(" ")]
    assert sorted(ln.split()[0] for ln in lines) == ["memory", "notes"]
    assert "[tea]" in out
    assert cli.main(["memory", "search", "tea", "--json", "--limit", "1"]) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert len(data) == 1 and set(data[0]) == {"source", "path", "title", "snippet", "score"}


def test_search_ranks_title_over_body_then_all_words_then_any(mem):
    memory.add("nothing relevant at all", "Postgres upgrade plan")
    memory.add("we talked about postgres once, in passing", "Lunch")
    hits = memory.search("postgres")
    assert [h.title for h in hits] == ["Postgres upgrade plan", "Lunch"]
    assert [h.title for h in memory.search("upgrade plan")] == ["Postgres upgrade plan"]  # all words
    assert sorted(h.title for h in memory.search("upgrade lunch")) == ["Lunch", "Postgres upgrade plan"]  # any word
    assert memory.search("zzzznothing") == []
    assert len(memory.search("postgres", limit=1)) == 1


def test_search_survives_query_syntax_and_rejects_an_empty_one(mem):
    memory.add("a-b c:d e", "Odd one")
    for q in ('"unbalanced', "a-b", "c:d", "NEAR(", "x AND OR NOT", "title:odd", "'; DROP TABLE docs; --"):
        memory.search(q)  # must not raise
    assert memory.search("a-b")[0].title == "Odd one"
    with pytest.raises(notes.WsError, match="no words"):
        memory.search("!!! ---")


def test_search_covers_notes_inbox_and_existing_vault_files(home, mem):
    vault = home / "vault"
    (vault / "Projects").mkdir()
    (vault / "Projects" / "Kitchen.md").write_text("# Kitchen reno\n\nQuote from the joiner: 4200.\n", encoding="utf-8")
    (vault / ".obsidian").mkdir()
    (vault / ".obsidian" / "junk.md").write_text("joiner secret config", encoding="utf-8")
    (vault / "readme.txt").write_text("joiner in a txt file", encoding="utf-8")
    notes.capture("Ring the joiner back", vault, now=WHEN)
    hits = memory.search("joiner")
    assert {h.source for h in hits} == {"notes"}
    assert sorted(Path(h.path).name for h in hits) == ["2026-10-31-093005-ring-the-joiner-back.md", "Kitchen.md"]
    assert next(h for h in hits if h.path.endswith("Kitchen.md")).title == "Kitchen reno"


def test_front_matter_ws_cannot_read_still_indexes(home, mem):
    (home / "vault" / "odd.md").write_text("---\nnested:\n  a: 1\ntitle: Odd\n---\nfindme body\n", encoding="utf-8")
    (home / "vault" / "bad.md").write_bytes(b"\xff\xfe findme \x00 binary-ish")
    hits = memory.search("findme")
    assert {Path(h.path).name for h in hits} == {"odd.md", "bad.md"}
    assert next(h for h in hits if h.path.endswith("odd.md")).title == "Odd"


def test_reindex_is_incremental_and_drops_deleted_files(home, mem, capsys):
    a = memory.add("alpha", "First")
    memory.add("beta", "Second")
    stats, db = memory.reindex()
    assert (stats.indexed, stats.unchanged, stats.removed) == (2, 0, 0) and db.is_file()
    stats, _ = memory.reindex()
    assert (stats.indexed, stats.unchanged, stats.removed) == (0, 2, 0)
    # same size, newer mtime, same bytes: re-hashed but not re-indexed
    os.utime(a, ns=(1_000_000_000, 1_000_000_000))
    stats, _ = memory.reindex()
    assert (stats.indexed, stats.unchanged) == (0, 2)
    # changed content
    a.write_text(a.read_text(encoding="utf-8").replace("alpha", "gamma"), encoding="utf-8")
    stats, _ = memory.reindex()
    assert stats.indexed == 1
    assert [h.title for h in memory.search("gamma")] == ["First"] and memory.search("alpha") == []
    # deleted
    a.unlink()
    stats, _ = memory.reindex()
    assert stats.removed == 1 and memory.search("gamma") == []
    assert cli.main(["memory", "reindex"]) == 0
    assert "removed 0" in capsys.readouterr().out


def test_a_rebuild_from_scratch_gives_the_same_results(home, mem):
    for i in range(6):
        memory.add(f"note number {i} about gardening and tomatoes {'tomatoes ' * i}", f"Garden {i}")
    notes.capture("tomatoes need water", home / "vault", now=WHEN)
    before = [(h.source, h.path, h.title, h.snippet, h.score) for h in memory.search("tomatoes", limit=20)]
    assert len(before) == 7
    memory.index_path().unlink()
    assert [(h.source, h.path, h.title, h.snippet, h.score) for h in memory.search("tomatoes", limit=20)] == before
    memory.reindex(full=True)
    assert [(h.source, h.path, h.title, h.snippet, h.score) for h in memory.search("tomatoes", limit=20)] == before


def test_a_damaged_index_file_is_rebuilt(home, mem):
    memory.add("keep calm", "Calm")
    assert memory.search("calm")
    memory.index_path().write_bytes(b"this is not a sqlite database" * 50)
    assert [h.title for h in memory.search("calm")] == ["Calm"]


def test_index_has_the_documented_shape(home, mem):
    memory.add("hello", "Hi")
    memory.reindex()
    conn = sqlite3.connect(memory.index_path())
    cols = [r[1] for r in conn.execute("PRAGMA table_info(docs)")]
    assert cols == ["title", "body", "tags", "path", "source"]
    assert conn.execute("SELECT count(*) FROM files WHERE length(sha256) = 64").fetchone()[0] == 1
    conn.close()


def test_no_fts5_falls_back_to_a_scan_and_says_so_once(home, mem, monkeypatch, capsys):
    memory.add("nothing relevant at all", "Postgres upgrade plan")
    memory.add("we talked about postgres once, in passing", "Lunch")
    notes.capture("postgres backups", home / "vault", now=WHEN)
    indexed = [(h.source, h.path, h.title) for h in memory.search("postgres")]
    capsys.readouterr()
    monkeypatch.setattr(memory, "has_fts5", lambda: False)
    first = memory.search("postgres")
    memory.search("lunch")
    err = capsys.readouterr().err
    assert err.count("no FTS5") == 1
    assert sorted((h.source, h.path, h.title) for h in first) == sorted(indexed)  # same files; the order may differ
    assert [h.title for h in memory.search("upgrade plan")] == ["Postgres upgrade plan"]
    assert len(memory.search("upgrade lunch")) == 2 and memory.search("zzzznothing") == []
    assert cli.main(["memory", "reindex"]) == 0
    assert "without FTS5" in capsys.readouterr().out
    assert memory.check_index(home / "mem")[0] is True


def test_search_needs_a_folder_to_search(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    assert cli.main(["memory", "search", "anything"]) == 1
    assert "ws:" in capsys.readouterr().err


def linux():
    return osinfo.OSInfo("linux", "test", "x86_64", "systemd", True)


def check(role_name, cfg):
    role = next(r for r in doctor.ROLES if r.name == role_name)
    return role.check(cfg, linux(), {}, False)


def test_doctor_memory_ok_warn_fail(home):
    status, detail = check("memory", {"path": str(home / "mem")})  # not made yet
    assert status == "fail" and "ws memory init" in detail
    (home / "mem").mkdir()
    status, detail = check("memory", {"path": str(home / "mem")})
    assert status == "warn" and "ws memory init" in detail
    memory.init(home / "mem")
    memory.add("x", "One")
    status, detail = check("memory", {"path": str(home / "mem")})
    assert status == "ok" and "git repo" in detail and "1 file" in detail
    assert check("memory", {})[0] == "fail"
    assert check("memory", {"path": str(home / "nope")})[0] == "fail"


def test_doctor_notes_ok_and_fail(home):
    assert check("notes", {"path": str(home / "vault")})[0] == "ok"  # _inbox can be made
    (home / "vault" / "_inbox").mkdir()
    assert check("notes", {"path": str(home / "vault")})[0] == "ok"
    assert check("notes", {"path": str(home / "nope")})[0] == "fail"
    assert check("notes", {})[0] == "fail"
    (home / "vault" / "_inbox").rmdir()
    (home / "vault" / "_inbox").write_text("a file, not a folder")
    assert check("notes", {"path": str(home / "vault")})[0] == "fail"
    if os.geteuid() != 0:
        (home / "vault" / "_inbox").unlink()
        (home / "vault").chmod(0o500)
        try:
            assert check("notes", {"path": str(home / "vault")})[0] == "fail"
        finally:
            (home / "vault").chmod(0o700)


def set_roles(**paths):
    profile_path().write_text(dumps({"roles": {name: {"path": str(p)} for name, p in paths.items()}}))


def test_paths_are_absolute_so_the_current_directory_does_not_matter(home, mem, monkeypatch):
    memory.add("tomato soup recipe", "Soup")
    (home / "sub").mkdir()
    monkeypatch.chdir(home)
    set_roles(memory="./mem", notes="vault")
    first = memory.search("soup")
    assert [h.path for h in first] == [str(home / "mem" / "facts" / "soup.md")]
    monkeypatch.chdir(home / "sub")
    set_roles(memory="../mem", notes="../vault")  # the same folders, spelled from somewhere else
    assert memory.search("soup") == first
    stats, _ = memory.reindex()
    assert (stats.indexed, stats.removed) == (0, 0)  # the rows stayed
    conn = sqlite3.connect(memory.index_path())
    assert [r[0] for r in conn.execute("SELECT path FROM files")] == [str(home / "mem" / "facts" / "soup.md")]
    conn.close()


def test_a_file_that_moves_between_roots_is_relabelled(home, mem):
    shared = home / "shared"
    shared.mkdir()
    (shared / "a.md").write_text("zucchini bake", encoding="utf-8")
    set_roles(notes=shared, memory=mem)
    assert [(h.source, Path(h.path).name) for h in memory.search("zucchini")] == [("notes", "a.md")]
    set_roles(memory=shared, notes=home / "vault")  # same file, same bytes, now memory
    assert [(h.source, Path(h.path).name) for h in memory.search("zucchini")] == [("memory", "a.md")]
    conn = sqlite3.connect(memory.index_path())
    assert conn.execute("SELECT count(*) FROM files").fetchone()[0] == 1
    assert conn.execute("SELECT source FROM docs").fetchall() == [("memory",)]
    conn.close()


def test_only_a_damaged_index_is_deleted(home, mem, monkeypatch):
    memory.add("keep calm", "Calm")
    memory.reindex()
    db = memory.index_path()
    real_open = memory._open

    def not_corrupt(path):
        raise sqlite3.OperationalError("attempt to write a readonly database")

    monkeypatch.setattr(memory, "_open", not_corrupt)
    with pytest.raises(notes.WsError, match="index failed"):
        memory.search("calm")
    assert db.is_file() and db.stat().st_size > 0  # left alone
    monkeypatch.setattr(memory, "_open", real_open)
    assert [h.title for h in memory.search("calm")] == ["Calm"]
    assert memory._corrupt(sqlite3.DatabaseError("database disk image is malformed"))
    assert memory._corrupt(sqlite3.DatabaseError("file is not a database"))
    assert not memory._corrupt(sqlite3.OperationalError("disk I/O error"))
    assert not memory._corrupt(sqlite3.OperationalError("no such table: docs"))


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no named pipes on this platform")
def test_a_named_pipe_called_x_md_does_not_hang_the_index(home, mem):
    memory.add("real file about quinces", "Quinces")
    os.mkfifo(home / "vault" / "x.md")
    result = []
    t = threading.Thread(target=lambda: result.append(memory.search("quinces")), daemon=True)
    t.start()
    t.join(15)
    assert not t.is_alive(), "reading the FIFO blocked the index"
    assert [h.title for h in result[0]] == ["Quinces"]
    assert list(memory.walk(home / "vault")) == []


def test_user_version_is_set_once(home, mem):
    memory.add("x", "One")
    memory.reindex()
    conn = sqlite3.connect(memory.index_path())
    assert conn.execute("PRAGMA user_version").fetchone()[0] == memory.SCHEMA
    conn.close()
    stats, _ = memory.reindex()
    assert stats.unchanged == 1
