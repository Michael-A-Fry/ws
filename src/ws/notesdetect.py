"""Find notes you already keep: an Obsidian vault (a folder holding `.obsidian/`) or a Logseq graph (a folder holding
`logseq/config.edn`). Setup offers these first so ws writes its `_inbox` into the app you already use.

Only a few usual places are looked at, two folders deep, so it stays quick on a big home folder. Nothing is read
inside a notes folder and nothing is written.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

DEPTH = 2  # folders below a search place that are looked into (the place itself is depth 0)
MAX_ENTRIES = 2000  # per folder: a huge folder of loose files is not scanned to the end
PLAIN = "~/Notes"
SKIP = {"node_modules", "Library", "Applications", "Pictures", "Music", "Movies"}


@dataclass(frozen=True)
class Candidate:
    path: Path
    app: str  # "Obsidian" or "Logseq"


def _subdirs(path: Path) -> list[Path]:
    out: list[Path] = []
    try:
        with os.scandir(path) as it:
            for n, e in enumerate(it):
                if n >= MAX_ENTRIES:
                    break
                if e.is_dir(follow_symlinks=False) and e.name not in SKIP:
                    out.append(Path(e.path))
    except OSError:
        pass
    return sorted(out)


def search_places(home: Path) -> list[Path]:
    """The usual places, in the order they are offered."""
    places = [home / "Documents", home / "Notes", home / "Obsidian"]
    icloud = home / "Library" / "Mobile Documents" / "iCloud~md~obsidian" / "Documents"
    places.extend(_subdirs(icloud))  # every folder here is a vault, so each is a place of its own
    return places


def app_of(folder: Path) -> str | None:
    if (folder / ".obsidian").is_dir():
        return "Obsidian"
    if (folder / "logseq" / "config.edn").is_file():
        return "Logseq"
    return None


def _walk(folder: Path, depth: int, seen: set[Path], found: list[Candidate]) -> None:
    app = app_of(folder)
    if app:
        real = folder.resolve()
        if real not in seen:
            seen.add(real)
            found.append(Candidate(folder, app))
        return  # a vault's own sub-folders are not separate vaults
    if depth >= DEPTH:
        return
    for child in _subdirs(folder):
        if not child.name.startswith("."):
            _walk(child, depth + 1, seen, found)


def find(home: Path | None = None) -> list[Candidate]:
    """Every vault or graph found, in search order, each once."""
    home = Path.home() if home is None else home
    seen: set[Path] = set()
    found: list[Candidate] = []
    for place in search_places(home):
        if place.is_dir():
            _walk(place, 0, seen, found)
    return found


def suggest(home: Path | None = None) -> tuple[str, str]:
    """The notes folder to propose and what it is: the first vault or graph found, else plain `~/Notes`."""
    found = find(home)
    return (str(found[0].path), found[0].app) if found else (PLAIN, "plain folder")
