"""The scheduled self-audit job in the store-a-secret how-to parses as a job file and meets the run contract."""

from __future__ import annotations

import re
from pathlib import Path

from ws import jobs

DOC = Path(__file__).parent.parent / "docs" / "how-to" / "store-a-secret.md"


def _block() -> str:
    m = re.search(r"## Check them on a schedule.*?```toml\n(.*?)```", DOC.read_text(encoding="utf-8"), re.S)
    assert m, "the how-to has lost its scheduled-audit job"
    return m.group(1)


def test_the_documented_audit_job_loads_and_has_a_complete_run_contract(tmp_path):
    text = _block()
    job = jobs.loads("secrets-audit", text, tmp_path)
    assert job.schedule is not None and job.runner == "claude-code"
    assert "never print" in text.lower() and "secret value" in text.lower()
    import tomllib

    assert jobs.contract_problems(tomllib.loads(text), "secrets-audit") == []
