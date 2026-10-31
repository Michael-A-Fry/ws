"""Finding an Obsidian vault or Logseq graph in the usual places: shallow, quick, read-only."""

from __future__ import annotations

from ws import notesdetect


def vault(path):
    (path / ".obsidian").mkdir(parents=True)
    return path


def graph(path):
    (path / "logseq").mkdir(parents=True)
    (path / notesdetect.LOGSEQ_CONFIG).write_text("{}")
    return path


def test_nothing_found_proposes_a_plain_notes_folder(tmp_path):
    (tmp_path / "Documents" / "stuff").mkdir(parents=True)
    assert notesdetect.find(tmp_path) == []
    assert notesdetect.suggest(tmp_path) == ("~/Notes", "plain folder")


def test_finds_an_obsidian_vault_and_a_logseq_graph(tmp_path):
    v = vault(tmp_path / "Documents" / "Vault")
    g = graph(tmp_path / "Notes" / "graph")
    found = notesdetect.find(tmp_path)
    assert [(c.path, c.app) for c in found] == [(v, "Obsidian"), (g, "Logseq")]
    assert notesdetect.suggest(tmp_path) == (str(v), "Obsidian")


def test_the_place_itself_can_be_a_vault(tmp_path):
    v = vault(tmp_path / "Obsidian")
    assert [c.path for c in notesdetect.find(tmp_path)] == [v]


def test_looks_two_folders_deep_and_no_further(tmp_path):
    near = vault(tmp_path / "Documents" / "work" / "vault")
    vault(tmp_path / "Documents" / "work" / "deeper" / "vault")
    assert [c.path for c in notesdetect.find(tmp_path)] == [near]


def test_a_vault_is_not_searched_for_more_vaults(tmp_path):
    v = vault(tmp_path / "Documents" / "Vault")
    vault(v / "nested")
    assert [c.path for c in notesdetect.find(tmp_path)] == [v]


def test_icloud_obsidian_folder_lists_each_vault(tmp_path):
    root = tmp_path / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents"
    a, b = vault(root / "a"), vault(root / "b")
    assert [c.path for c in notesdetect.find(tmp_path)] == [a, b]


def test_hidden_folders_and_files_are_ignored(tmp_path):
    vault(tmp_path / "Documents" / ".trash" / "vault")
    (tmp_path / "Documents" / "readme.txt").write_text("x")
    assert notesdetect.find(tmp_path) == []


def test_a_symlink_to_the_same_vault_is_listed_once(tmp_path):
    v = vault(tmp_path / "Documents" / "Vault")
    (tmp_path / "Notes").mkdir()
    link = tmp_path / "Notes" / "same"
    link.symlink_to(v, target_is_directory=True)
    assert [c.path for c in notesdetect.find(tmp_path)] == [v]
