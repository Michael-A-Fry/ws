"""`ws eval run` on a starter task through the real Claude Code, Tier 0 (a stub model on 127.0.0.1, no paid provider).

The stub model "solves" the starter task starter-csv-total by running one shell command in the trial's folder; ws then
grades the folder with the task's grader, and the run record carries the `eval` field. A missing runner skips the test,
unless WS_E2E_REQUIRE=1 (CI), when it fails.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import jsonschema

from e2e.stubs import Call
from e2e.test_gate_runners import runner_bin
from e2e.test_run_record import SeenAnthropic, host_of, rig  # noqa: F401  (rig is a fixture)
from ws import cli, runrecord
from ws import eval as ws_eval

VALIDATOR = jsonschema.Draft202012Validator(runrecord.schema())
SOLVE = """printf '{"food_total": 40}' > answer.json"""


def test_a_starter_task_is_run_graded_and_recorded(rig):  # noqa: F811
    exe = runner_bin("claude")
    with SeenAnthropic([Call("Bash", {"command": SOLVE})]) as stub:
        rig.start(host_of(stub), "anthropic", "x-api-key")
        rig.job(
            "arm-job", "claude-code", "claude-sonnet-4-5", host_of(stub), "anthropic",
            f'exe = {json.dumps(exe)}\nallowed_tools = ["Bash"]',
        )  # fmt: skip
        code = cli.main(
            [
                "eval",
                "run",
                "--task",
                "starter-csv-total",
                "--arm",
                "stubbed",
                "--job",
                "arm-job",
                "--trials",
                "1",
                "--keep",
            ]
        )
        assert code == 0
        (rec,) = rig.records()
        assert not list(VALIDATOR.iter_errors(rec)), rec
        assert rec["status"] == "ok" and rec["eval"] == {
            "task": "starter-csv-total",
            "trial": 1,
            "arm": "stubbed",
            "pass": True,
        }
        assert len(stub.tool_results()) == 1  # the runner ran the command and told the model
        (res,) = ws_eval.read_jsonl(ws_eval.results_path())
        assert res["pass"] is True and res["run_id"] == rec["run_id"] and res["grade"][0]["ok"] is True
        assert (Path(res["folder"]) / "answer.json").read_text(encoding="utf-8") == '{"food_total": 40}'
        shutil.rmtree(res["folder"], ignore_errors=True)  # --keep left it for this check
