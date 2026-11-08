"""The recovery tasks in tests/eval_tasks/: each names a shell variable that is not set as the thing a file command
should read, so the natural first command is `cat $REPORT`, `cp "$SRC" ...` or `tar czf ... $DOCS`. Run them with
`ws eval run` against two builds of ws and count the refusals in each trial: an agent that is refused once should write
the path out and finish, not loop. Here: the files are valid, synthetic and ask for what they say they ask for."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ws import eval as ws_eval

FOLDER = Path(__file__).parent / "eval_tasks"
FILES = sorted(FOLDER.glob("recovery-*.toml"))


def test_there_are_three_recovery_tasks():
    assert [f.stem for f in FILES] == ["recovery-archive-variable", "recovery-cat-variable", "recovery-copy-variable"]


@pytest.mark.parametrize("path", FILES, ids=lambda p: p.stem)
def test_a_recovery_task_is_valid_synthetic_and_names_an_unset_variable(path):
    task = ws_eval.parse_task(path.read_text(encoding="utf-8"), str(path), path.stem)
    assert task.trials == 5 and task.graders and not task.needs_judge()
    assert re.search(
        r"variable ([A-Z]+) .*not set", task.prompt
    )  # the file is named in the text, the variable is empty
    assert all(len(text) < 200 for text in task.setup_files.values())  # invented, tiny, nothing real
