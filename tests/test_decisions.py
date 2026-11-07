"""docs/decisions holds one MADR record per engine decision. This test keeps each one whole: front matter with a status
and a date, the MADR sections, a line in the index, and a confirmation that names a file that exists."""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DIR = ROOT / "docs" / "decisions"
RECORDS = sorted(p for p in DIR.glob("[0-9][0-9][0-9][0-9]-*.md"))
STATUS = re.compile(r"^(proposed|accepted|rejected|deprecated|superseded by \d{4})$")
SECTIONS = (
    "## Context and Problem Statement",
    "## Considered Options",
    "## Decision Outcome",
    "### Consequences",
    "### Confirmation",
)
FILE_REF = re.compile(r"`((?:tests|scripts|src|docs|\.github)/[\w./-]+?)(?:::[\w]+)?`")


def front_matter(text):
    assert text.startswith("---\n"), "a record starts with front matter"
    head = text.split("\n---\n", 1)[0].splitlines()[1:]
    return dict(line.split(": ", 1) for line in head)


def test_there_are_records():
    assert RECORDS


def test_numbers_are_unique():
    numbers = [p.name[:4] for p in RECORDS]
    assert len(numbers) == len(set(numbers))


@pytest.mark.parametrize("path", RECORDS, ids=lambda p: p.name)
def test_a_record_is_whole(path):
    text = path.read_text(encoding="utf-8")
    meta = front_matter(text)
    assert STATUS.match(meta["status"]), meta["status"]
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", meta["date"])
    assert re.search(r"^# \S", text, re.MULTILINE)
    for section in SECTIONS:
        assert section in text, section
    assert "Chosen option:" in text


@pytest.mark.parametrize("path", RECORDS, ids=lambda p: p.name)
def test_the_confirmation_names_a_file_that_exists(path):
    text = path.read_text(encoding="utf-8")
    confirmation = text.split("### Confirmation", 1)[1]
    if "Judged in review" in confirmation:
        return
    refs = FILE_REF.findall(confirmation)
    assert refs, "name the test or check that keeps this decision true, or say it is judged in review"
    for ref in refs:
        assert (ROOT / ref).exists(), ref


def test_the_index_lists_every_record_once():
    index = (DIR / "README.md").read_text(encoding="utf-8")
    for path in RECORDS:
        assert index.count(f"| [{path.name[:4]}]({path.name}) |") == 1, path.name
