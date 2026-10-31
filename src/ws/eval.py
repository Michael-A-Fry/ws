"""`ws eval`: your own eval, with TOML tasks, deterministic graders, ten metrics and an exact A/B comparison.

Trials run through `ws run`, and new tasks are harvested from your own failed or rated-down runs.

    ws eval run [--task ID | --set starter|mine] [--arm NAME --job JOB] [--trials N] [--dry-run] [--keep]
    ws eval report [--arm NAME] [--since 90d] [--k N]    the ten metrics; a metric with no data says "not measured"
    ws eval compare ARM_A ARM_B [--look 1|2]             exact McNemar on paired trials, at most two looks per pair
    ws eval harvest [--since 30d]                        propose tasks from failed, timed-out, rated-down, retried runs
    ws eval approve ID | ws eval reject ID               only your command moves a proposal

The product ships the engine, the format, the metrics and a synthetic starter set (no real data); your own set lives
in `$WS_HOME/eval/tasks/<id>.toml`, one task per file, and grows through `ws eval harvest`. A task file is checked
whole or refused: an unknown key, a bad grader or a path outside the task folder rejects the file. Task files decide
what runs on your machine (a `command` grader), so they must belong to you and not be writable by others, like job
files.

Each trial is one `ws run`, by the same code path (`run.run_job`): a fresh temporary folder holds the task's setup
files (not under `$WS_HOME`, where the sandbox denies the agent's shell writes), the job named by `--job` is the arm
under test (its runner, model, options and secrets; the task replaces only its prompt, folder, timeout and turn limit),
and the run record carries `eval: {task, trial, arm, pass}`. The record is written before grading, with `pass` null; the
graded outcome is the line in `$WS_HOME/eval/results.jsonl` (with the task's hash and the job), so a signal during
grading still leaves the record, and a result line saying grading was cut short. A trial passes only if the run finished
`ok` and every grader passed. The task's hidden files are written after the run and before grading, over whatever is at
those paths. A failed trial's folder is kept for you to look at.

Graders: `command`, `file_contains`, `json_path_equals`, `regex` (files are read only if regular, at most 1 MiB). A
`command` grader runs code the agent wrote, so it runs inside ws's own wall (`ws.sandbox.wrap`: disk read-only, the trial
folder included, no Unix sockets) or not at all: with no sandbox on the machine `ws eval run` refuses a task set that has
one (typed error with the install hint), and there is no unsandboxed fallback. The wall does not close the network. A
command passes only if it exits 0 AND prints the trial's nonce: ws sends a random line on the command's stdin, and the
checker must read it first, run the agent's code only in a child process (which has no stdin), and print the nonce last.
Agent code that exits early (`os._exit(0)`) or prints "ok" never held it. Start the checker with `python -I` so the
agent cannot shadow a module it imports. The starter checkers show the pattern (`CHECK_PRELUDE`). A `rubric` grader
(judged by a model) can be declared but is refused at run time unless the profile names a judge model (`[eval]
judge_model`); the judge call itself is not built yet, so for now a rubric task is always skipped.

`compare` pairs at task level, not trial level (`evalstats.paired_tasks`): a task passes for an arm when most of its
trials do, a tie fails, and results count only from the job and the task text (hash) each arm used last.

Harvest never approves anything. It writes each proposal to `$WS_HOME/eval/proposed/` with `approved = false`,
`review_required = true`, the source run id, where the prompt came from and a placeholder rubric grader (so it cannot run
until you give it a real one), all or none. `approve` shows the prompt and asks you (`--yes` for scripts). Prompts that
look like they hold a secret are not proposed: ws has no prompt scrubber, so it refuses a prompt that names a registered
secret, or looks like a key, token, password or URL with a password. It checks names and shapes, not secret values (ws
cannot read the store safely). Read each proposal before you approve it; FIU or client data in a prompt is for you to
catch.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from secrets import token_hex

from ws import evalstats, jobs, output, policy, rating, run, runrecord, sandbox
from ws import secrets as wsecrets
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path, ws_home

try:  # not on native Windows; ws runs there only through WSL2, but the module must still import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
FAMILIES = ("coding", "life-admin", "work-style", "research")
KINDS = ("capability", "regression")
DEFAULT_TRIALS, MAX_TRIALS = 3, 5
DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S = 300, 3600
MAX_PROMPT, MAX_FILE, MAX_FILES = 20_000, 64 * 1024, 20
READ_CAP = 1024 * 1024  # bytes of a graded file that are read
MAX_PATTERN = 500  # characters of a regex grader's pattern
MAX_CHECK_OUTPUT = 64 * 1024  # bytes of a command grader's output that are looked at
TASK_KEYS = {
    "id", "family", "kind", "prompt", "trials", "timeout_s", "max_turns", "setup", "hidden", "grader", "repo", "files",
    "approved", "source_run", "source_job", "reason", "review_required", "prompt_source",
}  # fmt: skip
GRADER_KEYS = {
    "command": ({"command"}, {"timeout_s"}),
    "file_contains": ({"path", "text"}, {"ignore_case", "negate"}),
    "json_path_equals": ({"path", "json_path", "equals"}, set()),
    "regex": ({"path", "pattern"}, {"ignore_case", "negate"}),
    "rubric": ({"rubric"}, set()),
}
GRADER_TIMEOUT_S, MAX_GRADER_TIMEOUT_S = 60, 600
PLACEHOLDER_RUBRIC = (
    "Say what a good result looks like for this prompt, then replace this grader with a command, file_contains, "
    "json_path_equals or regex grader."
)


class TaskError(Exception):
    """A task file that cannot be used; the message names the file and the key."""


# ---- paths --------------------------------------------------------------------------------------------------------


def eval_dir(env: dict | None = None) -> Path:
    return ws_home(env) / "eval"


def tasks_dir(env: dict | None = None) -> Path:
    return eval_dir(env) / "tasks"


def proposed_dir(env: dict | None = None) -> Path:
    return eval_dir(env) / "proposed"


def rejected_dir(env: dict | None = None) -> Path:
    return eval_dir(env) / "rejected"


def results_path(env: dict | None = None) -> Path:
    return eval_dir(env) / "results.jsonl"


def looks_path(env: dict | None = None) -> Path:
    return eval_dir(env) / "looks.jsonl"


# ---- the task format ----------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Task:
    id: str
    family: str
    kind: str
    prompt: str
    trials: int = DEFAULT_TRIALS
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_turns: int | None = None
    setup_files: dict = field(default_factory=dict)
    hidden_files: dict = field(default_factory=dict)
    graders: tuple[dict, ...] = ()
    repo: str | None = None
    files: tuple[str, ...] = ()
    approved: bool | None = None
    source_run: str | None = None
    source_job: str | None = None
    reason: str | None = None
    review_required: bool | None = None
    prompt_source: str | None = None

    def needs_judge(self) -> bool:
        return any(g["type"] == "rubric" for g in self.graders)

    def to_dict(self) -> dict:
        d: dict = {"id": self.id, "family": self.family, "kind": self.kind, "prompt": self.prompt}
        d["trials"], d["timeout_s"] = self.trials, self.timeout_s
        for key, v in (("max_turns", self.max_turns), ("repo", self.repo), ("approved", self.approved),
                       ("source_run", self.source_run), ("source_job", self.source_job), ("reason", self.reason),
                       ("review_required", self.review_required), ("prompt_source", self.prompt_source)):  # fmt: skip
            if v is not None:
                d[key] = v
        if self.files:
            d["files"] = list(self.files)
        if self.setup_files:
            d["setup"] = {"files": dict(self.setup_files)}
        if self.hidden_files:
            d["hidden"] = {"files": dict(self.hidden_files)}
        d["grader"] = [dict(g) for g in self.graders]
        return d


def _str(d: dict, key: str, where: str, required: bool = True, limit: int = 300) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            raise TaskError(f"{where}: {key} is required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise TaskError(f"{where}: {key} must be a non-empty string")
    if len(v) > limit:
        raise TaskError(f"{where}: {key} is longer than {limit} characters")
    return v


def _bool(d: dict, key: str, where: str) -> bool:
    v = d.get(key, False)
    if not isinstance(v, bool):
        raise TaskError(f"{where}: {key} must be true or false")
    return v


def _number(d: dict, key: str, where: str, default, top: float, integer: bool = False):
    v = d.get(key, default)
    if v is None:
        return None
    good = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not good or not 0 < v <= top:
        raise TaskError(f"{where}: {key} must be {'a whole ' if integer else 'a '}number above 0 and at most {top:g}")
    return v


def safe_rel(path, where: str, key: str) -> str:
    """A relative path that stays inside the task folder: no leading slash, no `..`, no backslash."""
    if not isinstance(path, str) or not path.strip() or len(path) > 200:
        raise TaskError(f"{where}: {key} must be a path of up to 200 characters")
    parts = path.split("/")
    if path.startswith("/") or "\\" in path or "\x00" in path or any(p in ("", ".", "..") for p in parts):
        raise TaskError(f"{where}: {key} {path!r} must be a plain relative path inside the task folder")
    return path


def _files(d: dict, key: str, where: str) -> dict:
    block = d.get(key)
    if block is None:
        return {}
    if not isinstance(block, dict) or set(block) - {"files"}:
        raise TaskError(f"{where}: [{key}] has one key, files")
    files = block.get("files", {})
    if not isinstance(files, dict) or len(files) > MAX_FILES:
        raise TaskError(f"{where}: {key}.files must be a table of at most {MAX_FILES} files")
    for path, content in files.items():
        safe_rel(path, where, f"{key}.files path")
        if not isinstance(content, str) or len(content.encode()) > MAX_FILE:
            raise TaskError(f"{where}: {key}.files {path!r} must be text of at most {MAX_FILE // 1024} KiB")
    return dict(files)


def _no_collisions(paths: list[str], where: str) -> None:
    """No path may be a folder that another path sits in (`x` and `x/y`): writing one would fail or replace the other."""
    folders = {"/".join(p.split("/")[:i]) for p in paths for i in range(1, len(p.split("/")))}
    for p in paths:
        if p in folders:
            raise TaskError(f"{where}: {p!r} is both a file and a folder that another file sits in")


def _grader(g, where: str) -> dict:
    if not isinstance(g, dict) or g.get("type") not in GRADER_KEYS:
        raise TaskError(f"{where}: type must be one of: {', '.join(GRADER_KEYS)}")
    need, may = GRADER_KEYS[g["type"]]
    extra = sorted(set(g) - {"type"} - need - may)
    if extra:
        raise TaskError(f"{where}: unknown key {extra[0]!r} for a {g['type']} grader")
    for k in sorted(need):
        if k not in g:
            raise TaskError(f"{where}: {k} is required for a {g['type']} grader")
    out = dict(g)
    if "path" in out:
        safe_rel(out["path"], where, "path")
    for k in ("text", "pattern", "json_path", "rubric"):
        if k in out:
            _str(out, k, where, limit=2000)
    for k in ("ignore_case", "negate"):
        if k in out:
            _bool(out, k, where)
    if g["type"] == "command":
        cmd = out["command"]
        if isinstance(cmd, str) and cmd.strip():
            with contextlib.suppress(ValueError):
                cmd = shlex.split(cmd)
        if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) and x for x in cmd):
            raise TaskError(f"{where}: command must be a command line (text that splits into words) or a list of words")
        _number(out, "timeout_s", where, GRADER_TIMEOUT_S, MAX_GRADER_TIMEOUT_S)
    if g["type"] == "regex":
        try:
            if len(out["pattern"]) > MAX_PATTERN:
                raise TaskError(f"{where}: pattern is longer than {MAX_PATTERN} characters")
            re.compile(out["pattern"])
        except re.error as e:
            raise TaskError(f"{where}: pattern is not a regular expression ({e})") from e
    return out


def parse_task(text: str, where: str, expect_id: str | None = None) -> Task:
    """A task from file text, checked whole: any problem raises TaskError and nothing is returned."""
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise TaskError(f"{where}: {e}") from e
    unknown = sorted(set(d) - TASK_KEYS)
    if unknown:
        raise TaskError(f"{where}: unknown key {unknown[0]!r} (known: {', '.join(sorted(TASK_KEYS))})")
    tid = _str(d, "id", where)
    if not NAME.match(tid):
        raise TaskError(f"{where}: id is letters, digits, - and _ (up to 64)")
    if expect_id is not None and tid != expect_id:
        raise TaskError(f"{where}: id {tid!r} must match the file name {expect_id!r}")
    family, kind = _str(d, "family", where), _str(d, "kind", where)
    if family not in FAMILIES:
        raise TaskError(f"{where}: family must be one of: {', '.join(FAMILIES)}")
    if kind not in KINDS:
        raise TaskError(f"{where}: kind must be one of: {', '.join(KINDS)}")
    raw_graders = d.get("grader")
    if isinstance(raw_graders, dict):
        raw_graders = [raw_graders]
    if not isinstance(raw_graders, list) or not 1 <= len(raw_graders) <= 5:
        raise TaskError(f"{where}: give 1 to 5 graders ([grader] or [[grader]])")
    files = d.get("files", [])
    if not isinstance(files, list):
        raise TaskError(f"{where}: files must be a list of paths in the repo")
    for f in files:
        safe_rel(f, where, "files")
    setup, hidden = _files(d, "setup", where), _files(d, "hidden", where)
    _no_collisions([*setup, *hidden], where)
    return Task(
        id=tid,
        family=family,
        kind=kind,
        prompt=_str(d, "prompt", where, limit=MAX_PROMPT),
        trials=_number(d, "trials", where, DEFAULT_TRIALS, MAX_TRIALS, integer=True),
        timeout_s=_number(d, "timeout_s", where, DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S),
        max_turns=_number(d, "max_turns", where, None, 1000, integer=True),
        setup_files=setup,
        hidden_files=hidden,
        graders=tuple(_grader(g, f"{where}: grader #{i}") for i, g in enumerate(raw_graders, 1)),
        repo=_str(d, "repo", where, required=False, limit=500),
        files=tuple(files),
        approved=_bool(d, "approved", where) if "approved" in d else None,
        source_run=_str(d, "source_run", where, required=False),
        source_job=_str(d, "source_job", where, required=False),
        reason=_str(d, "reason", where, required=False),
        review_required=_bool(d, "review_required", where) if "review_required" in d else None,
        prompt_source=_str(d, "prompt_source", where, required=False),
    )


def _toml_str(s: str) -> str:
    return json.dumps(s, ensure_ascii=False).replace("\x7f", "\\u007f")  # JSON escapes are TOML escapes


def _toml_value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _toml_str(v)
    if isinstance(v, list):
        return "[" + ", ".join(_toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{_toml_str(k)} = {_toml_value(x)}" for k, x in v.items()) + " }"
    raise TaskError(f"cannot write {type(v).__name__} to a task file")


def dumps_task(task: Task) -> str:
    """The task as TOML text that `parse_task` reads back to the same task."""
    d = task.to_dict()
    lines = [f"{k} = {_toml_value(v)}" for k, v in d.items() if k not in ("setup", "hidden", "grader")]
    for table in ("setup", "hidden"):
        if table in d:
            lines += [
                "",
                f"[{table}.files]",
                *(f"{_toml_str(p)} = {_toml_str(c)}" for p, c in d[table]["files"].items()),
            ]
    for g in d["grader"]:
        lines += ["", "[[grader]]", *(f"{k} = {_toml_value(v)}" for k, v in g.items())]
    return "\n".join(lines) + "\n"


# ---- the starter set ----------------------------------------------------------------------------------------------
# Synthetic: invented files and numbers, nothing real, nothing personal. A person's own set comes from `ws eval harvest`.

# What every hidden checker starts with. ws writes a per-trial random line to the checker's stdin; the checker reads it
# first, runs the agent's code only in a child process (which has no stdin, so no nonce), checks what the child printed,
# and prints the nonce back last. A grade needs that line on stdout as well as exit 0, so agent code that exits early
# (`os._exit(0)`) or prints "ok" cannot pass: it never held the nonce.
CHECK_PRELUDE = """\
import json, subprocess, sys

nonce = sys.stdin.readline().strip()


def agent(setup, expr):
    code = f"{setup}\\nimport json\\nprint(json.dumps({expr}))"
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL)
    assert r.returncode == 0, r.stderr[-300:]
    return json.loads(r.stdout.splitlines()[-1])
"""

STARTER: dict[str, str] = {
    "starter-fizzbuzz": """\
id = "starter-fizzbuzz"
family = "coding"
kind = "regression"
prompt = "Create fizzbuzz.py in this folder with a function fizzbuzz(n) that returns a list of strings for 1 to n: 'Fizz' for multiples of 3, 'Buzz' for multiples of 5, 'FizzBuzz' for both, otherwise the number as a string."
trials = 3
timeout_s = 300

[hidden.files]
"check.py" = '''
@PRELUDE@
r = agent("from fizzbuzz import fizzbuzz", "fizzbuzz(15)")
assert len(r) == 15 and r[0] == "1" and r[2] == "Fizz" and r[4] == "Buzz" and r[14] == "FizzBuzz", r
print(nonce)
'''

[[grader]]
type = "command"
command = "{python} -I check.py"
""",
    "starter-fix-mean": """\
id = "starter-fix-mean"
family = "coding"
kind = "capability"
prompt = "stats.py has a bug: mean() gives the wrong answer. Fix it without changing its name or arguments. An empty list should give 0.0."
trials = 3
timeout_s = 300

[setup.files]
"stats.py" = '''
def mean(values):
    return sum(values) / (len(values) + 1)
'''

[hidden.files]
"check.py" = '''
@PRELUDE@
assert agent("from stats import mean", "[mean([1, 2, 3]), mean([5]), mean([])]") == [2, 5, 0.0]
print(nonce)
'''

[[grader]]
type = "command"
command = "{python} -I check.py"
""",
    "starter-rename-variable": """\
id = "starter-rename-variable"
family = "coding"
kind = "regression"
prompt = "Rename the variable usr_nm to user_name everywhere in this folder without changing what the code does."
trials = 3
timeout_s = 300

[setup.files]
"config.py" = '''
usr_nm = "ada"
'''
"app.py" = '''
from config import usr_nm


def greet():
    return "hi " + usr_nm
'''

[hidden.files]
"check.py" = '''
@PRELUDE@
assert agent("import app", "app.greet()") == "hi ada"
print(nonce)
'''

[[grader]]
type = "file_contains"
path = "config.py"
text = "user_name"

[[grader]]
type = "file_contains"
path = "app.py"
text = "usr_nm"
negate = true

[[grader]]
type = "file_contains"
path = "config.py"
text = "usr_nm"
negate = true

[[grader]]
type = "command"
command = "{python} -I check.py"
""",
    "starter-csv-total": """\
id = "starter-csv-total"
family = "life-admin"
kind = "regression"
prompt = 'expenses.csv lists some spending. Work out the total for the food category and write it to answer.json as {"food_total": <number>}.'
trials = 3
timeout_s = 300

[setup.files]
"expenses.csv" = '''
date,category,amount
2026-01-03,food,12.50
2026-01-04,transport,3.20
2026-01-05,food,8.25
2026-01-09,fun,20.00
2026-01-12,food,19.25
'''

[[grader]]
type = "json_path_equals"
path = "answer.json"
json_path = "food_total"
equals = 40.0
""",
    "starter-meeting-slot": """\
id = "starter-meeting-slot"
family = "life-admin"
kind = "capability"
prompt = "availability.txt lists when three people are free. Find the earliest one-hour slot when all three are free and write it to slot.txt as HH:MM-HH:MM."
trials = 3
timeout_s = 300

[setup.files]
"availability.txt" = '''
Ana: 09:00-11:00, 13:00-15:00
Ben: 10:00-12:00, 14:00-16:00
Caro: 10:30-11:30, 14:00-15:30
'''

[[grader]]
type = "regex"
path = "slot.txt"
pattern = '^\\s*14:00\\s*-\\s*15:00\\s*$'
""",
    "starter-plain-english": """\
id = "starter-plain-english"
family = "work-style"
kind = "capability"
prompt = "Rewrite draft.md in plain NZ English, in 30 words or fewer, and save it as plain.md. Do not use the words leverage, synergies or operationalise."
trials = 3
timeout_s = 300

[setup.files]
"draft.md" = '''
We will leverage our synergies to operationalise a paradigm shift in how the team onboards new starters going forward.
'''

[hidden.files]
"check.py" = '''
@PRELUDE@
words = open("plain.md").read().split()
assert 0 < len(words) <= 30, len(words)
print(nonce)
'''

[[grader]]
type = "command"
command = "{python} -I check.py"

[[grader]]
type = "file_contains"
path = "plain.md"
text = "leverage"
ignore_case = true
negate = true

[[grader]]
type = "regex"
path = "plain.md"
pattern = 'synerg|operationalis'
ignore_case = true
negate = true
""",
    "starter-commit-message": """\
id = "starter-commit-message"
family = "work-style"
kind = "regression"
prompt = "Write a commit message for change.diff in msg.txt: a subject line of 50 characters or fewer in the imperative mood, then a blank line, then one sentence on why."
trials = 3
timeout_s = 300

[setup.files]
"change.diff" = '''
--- a/greeting.py
+++ b/greeting.py
@@ -1,2 +1,2 @@
 def greet(name):
-    return "hello " + name
+    return "hello, " + name + "!"
'''

[hidden.files]
"check.py" = '''
@PRELUDE@
lines = open("msg.txt").read().splitlines()
assert lines and 0 < len(lines[0]) <= 50, lines[:1]
assert len(lines) >= 3 and lines[1] == "" and lines[2].strip(), lines
print(nonce)
'''

[[grader]]
type = "command"
command = "{python} -I check.py"
""",
    "starter-cheapest-plan": """\
id = "starter-cheapest-plan"
family = "research"
kind = "capability"
prompt = 'Using only notes.md: which plan is cheapest for a team of 5, per month? Write answer.json as {"plan": <name>, "monthly_total": <number>}.'
trials = 3
timeout_s = 300

[setup.files]
"notes.md" = '''
# Plans we looked at
- Alpha: $15 per month, 5 seats included, extra seats $4 each.
- Beta: $9 per month, 2 seats included, extra seats $1 each.
- Gamma: $30 per month flat, 10 seats included.
'''

[[grader]]
type = "json_path_equals"
path = "answer.json"
json_path = "plan"
equals = "Beta"

[[grader]]
type = "json_path_equals"
path = "answer.json"
json_path = "monthly_total"
equals = 12
""",
}


def starter_tasks() -> list[Task]:
    return [
        parse_task(text.replace("@PRELUDE@\n", CHECK_PRELUDE), f"starter task {tid}", tid)
        for tid, text in STARTER.items()
    ]


# ---- loading tasks ------------------------------------------------------------------------------------------------


def _read_task(path: Path) -> Task:
    try:
        jobs.check_private(path)
        text = path.read_text(encoding="utf-8")
    except jobs.JobError as e:
        raise TaskError(str(e)) from e
    except (OSError, UnicodeDecodeError) as e:
        raise TaskError(f"{path}: {e.strerror if isinstance(e, OSError) else 'not UTF-8 text'}") from e
    return parse_task(text, str(path), path.stem)


def load_dir(folder: Path) -> list[Task]:
    """Every task in a folder (a missing folder is none). One bad file refuses the whole set: no partial loads."""
    if not folder.is_dir():
        return []
    try:
        jobs.check_private(folder)
    except jobs.JobError as e:
        raise TaskError(str(e)) from e
    return [_read_task(p) for p in sorted(folder.glob("*.toml")) if NAME.match(p.stem)]


def known_tasks(env: dict | None = None) -> list[Task]:
    """Your tasks, then the starter ones; a task of yours with a starter's id wins."""
    mine = load_dir(tasks_dir(env))
    ids = {t.id for t in mine}
    return [*mine, *(t for t in starter_tasks() if t.id not in ids)]


def _select(args: argparse.Namespace, env: dict) -> tuple[str, list[Task]]:
    try:
        if args.task:
            for t in known_tasks(env):
                if t.id == args.task:
                    return args.task, [t]
            raise WsError(
                "not_found", f"no task {args.task!r}", f"{tasks_dir(env)} holds yours; the starter ids start 'starter-'"
            )
        if (args.set or "mine") == "starter":
            return "starter", starter_tasks()
        mine = load_dir(tasks_dir(env))
    except TaskError as e:
        raise WsError("bad_config", str(e), "fix or remove that file; a task file is used whole or not at all") from e
    if not mine:
        raise WsError(
            "not_found", f"no tasks of yours in {tasks_dir(env)}", "`ws eval run --set starter`, or `ws eval harvest`"
        )
    return "mine", mine


# ---- graders ------------------------------------------------------------------------------------------------------


def _why(e: OSError) -> str:
    return e.strerror or str(e) or type(e).__name__


@dataclass(frozen=True)
class Verdict:
    type: str
    ok: bool
    detail: str


def _inside(folder: Path, rel: str) -> Path:
    """The file `rel` in the folder, refusing a symlink or path that leaves it. Raises OSError."""
    base = folder.resolve()
    p = (base / rel).resolve()
    if not p.is_relative_to(base):
        raise OSError("the path leaves the task folder")
    return p


def _read_text(folder: Path, rel: str) -> str:
    """At most READ_CAP bytes of a regular file inside the folder. A device, pipe or socket is refused before it is read
    (opening a pipe would wait for ever), and the file is opened without following a link at the last step."""
    p = _inside(folder, rel)
    if not stat.S_ISREG(os.stat(p).st_mode):  # before opening: opening a pipe for reading waits for a writer
        raise OSError(f"{rel} is not a regular file")
    fd = os.open(p, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(f"{rel} is not a regular file")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            return fh.read(READ_CAP).decode("utf-8", errors="replace")
    finally:
        os.close(fd)


class WallUnavailable(Exception):
    """The sandbox that a command grader needs cannot be built here."""


def _wall(argv: list[str], folder: Path) -> sandbox.Wrapped:
    """`argv` inside ws's own wall (ws.sandbox.wrap): the whole disk read-only, nothing writable, the trial folder
    included, and Unix sockets shut. The wall does not close the network on its own (no `unshare-net` in its public API),
    so a grader that must not reach the network is a limit to know about. Raises WallUnavailable; there is no unsandboxed
    fallback."""
    try:
        pol = policy.load()
        return sandbox.wrap(argv, pol, ws_home().absolute(), [str(policy.policy_path().absolute())])
    except (sandbox.SandboxError, policy.PolicyError) as e:
        raise WallUnavailable(str(e)) from e


def require_wall() -> None:
    """Raise WsError (typed, with the install hint) unless command graders can run in the wall on this machine."""
    try:
        wrapped = _wall(["true"], Path(tempfile.gettempdir()))
    except WallUnavailable as e:
        raise WsError(
            "unsupported",
            f"a command grader runs agent-written code, so it must run inside ws's wall, and that is not available: {e}",
            "install the sandbox (bubblewrap on Linux; sandbox-exec ships with macOS), or use only file_contains, "
            "regex and json_path_equals graders",
        ) from e
    for fd in wrapped.pass_fds:
        os.close(fd)


def _grade_command(g: dict, folder: Path, nonce: str) -> Verdict:
    """Run the command in the wall, the folder read-only, with the nonce on its stdin. It passes only if it exits 0 AND
    prints the nonce as a line of its own output: agent code that ends the checker early, or prints its own "ok", never
    held the nonce. (A checker must read the nonce first and run the agent's code in a child with no stdin.)"""
    cmd = g["command"]
    argv = shlex.split(cmd) if isinstance(cmd, str) else list(cmd)
    argv = [sys.executable if a == "{python}" else a for a in argv]
    env = {k: os.environ[k] for k in ("PATH", "LANG", "TZ") if k in os.environ}
    env.update(HOME=str(folder), PYTHONDONTWRITEBYTECODE="1")  # the real home is not passed; the folder is read-only
    try:
        wrapped = _wall(argv, folder)
    except WallUnavailable as e:
        return Verdict("command", False, f"not run: no wall to run it in ({e})")
    try:
        p = subprocess.run(
            wrapped.argv, cwd=folder, env=env, capture_output=True, text=True, input=nonce + "\n", check=False,
            pass_fds=wrapped.pass_fds, timeout=g.get("timeout_s", GRADER_TIMEOUT_S),
        )  # fmt: skip
    except subprocess.TimeoutExpired:
        return Verdict("command", False, "timed out")
    except OSError as e:
        return Verdict("command", False, f"could not start: {_why(e)}")
    finally:
        for fd in wrapped.pass_fds:
            with contextlib.suppress(OSError):
                os.close(fd)
    if p.returncode != 0:
        last = (p.stderr.strip().splitlines() or [""])[-1][:200]
        return Verdict("command", False, f"exit {p.returncode}: {last}".rstrip(": "))
    if nonce not in p.stdout[:MAX_CHECK_OUTPUT].splitlines():
        return Verdict("command", False, "exit 0 but the checker did not print the nonce, so the exit is not trusted")
    return Verdict("command", True, "exit 0 and the nonce")


def _grade_contains(g: dict, folder: Path) -> Verdict:
    text = _read_text(folder, g["path"])
    want = g["text"]
    found = (want.lower() in text.lower()) if g.get("ignore_case") else (want in text)
    return _presence("file_contains", g, found, f"{g['path']} contains {want!r}")


def _grade_regex(g: dict, folder: Path) -> Verdict:
    flags = re.MULTILINE | (re.IGNORECASE if g.get("ignore_case") else 0)
    found = re.search(g["pattern"], _read_text(folder, g["path"]), flags) is not None
    return _presence("regex", g, found, f"{g['path']} matches /{g['pattern']}/")


def _presence(kind: str, g: dict, found: bool, what: str) -> Verdict:
    if g.get("negate"):
        return Verdict(kind, not found, f"{what}: unwanted, {'found' if found else 'absent'}")
    return Verdict(kind, found, f"{what}: {'yes' if found else 'no'}")


_MISSING = object()


def _walk(doc, path: str):
    """`a.b.0.c` into nested objects and lists; _MISSING when any step is absent."""
    cur = doc
    for seg in path.split("."):
        if isinstance(cur, dict) and seg in cur:
            cur = cur[seg]
        elif isinstance(cur, list) and seg.isdigit() and int(seg) < len(cur):
            cur = cur[int(seg)]
        else:
            return _MISSING
    return cur


def _same(a, b) -> bool:
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    return a == b


def _grade_json(g: dict, folder: Path) -> Verdict:
    try:
        doc = json.loads(_read_text(folder, g["path"]))
    except ValueError:
        return Verdict("json_path_equals", False, f"{g['path']} is not JSON")
    got = _walk(doc, g["json_path"])
    if got is _MISSING:
        return Verdict("json_path_equals", False, f"{g['json_path']} is not in {g['path']}")
    good = _same(got, g["equals"])
    return Verdict(
        "json_path_equals", good, f"{g['json_path']} {'equals' if good else 'differs from'} the expected value"
    )


def grade(task: Task, folder: Path, nonce: str | None = None) -> list[Verdict]:
    """Run every grader of the task over the folder. A grader that cannot read its file fails; it never raises. `nonce`
    is the trial's random line for command graders (a fresh one if none is given)."""
    nonce = nonce or token_hex(16)
    graders = {"file_contains": _grade_contains, "regex": _grade_regex, "json_path_equals": _grade_json}
    out = []
    for g in task.graders:
        if g["type"] == "rubric":
            out.append(Verdict("rubric", False, "a rubric grader is not run (the judge call is not built)"))
            continue
        try:
            out.append(_grade_command(g, folder, nonce) if g["type"] == "command" else graders[g["type"]](g, folder))
        except OSError as e:
            out.append(Verdict(g["type"], False, f"cannot read {g.get('path', '')}: {_why(e)}"))
    return out


def write_files(folder: Path, files: dict) -> None:
    """Write task files into the folder. Raises OSError for a destination that leaves it (a planted symlink). Whatever is
    already at a destination is removed first, so a hard link the agent made to another file is not written through."""
    base = folder.resolve()
    for rel, content in files.items():
        dest = base / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.parent.resolve().is_relative_to(base):
            raise OSError(f"{rel} leaves the task folder")
        if dest.is_dir() and not dest.is_symlink():
            raise OSError(f"{rel} is a folder")
        if os.path.lexists(dest):  # a link, or a hard link to a file elsewhere: replace it, never write through it
            dest.unlink()
        dest.write_text(content, encoding="utf-8")


# ---- small file helpers -------------------------------------------------------------------------------------------


def append_jsonl(path: Path, rec: dict) -> None:
    """One write() on an O_APPEND fd under an flock, 0600: the way runs.jsonl and ratings.jsonl are written."""
    line = (json.dumps(rec, ensure_ascii=True, separators=(",", ":")) + "\n").encode()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, line)
    finally:
        os.close(fd)


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    with path.open(encoding="utf-8", errors="replace") as fh:
        for line in fh:
            with contextlib.suppress(ValueError):
                rec = json.loads(line)
                if isinstance(rec, dict):
                    out.append(rec)
    return out


def write_private(path: Path, text: str, exclusive: bool = False) -> None:
    """Write a file of mode 0600 in a 0700 folder, atomically (or, with `exclusive`, only if it is new)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        return
    tmp = path.with_name(f".{path.name}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(tmp, path)


def parse_since(text: str) -> timedelta:
    m = re.fullmatch(r"(\d{1,4})([hdw])", text.strip().lower())
    if not m or int(m[1]) == 0:
        raise WsError("usage", f"--since {text!r} is not a span", "write it like 12h, 30d or 4w", 2)
    return timedelta(**{{"h": "hours", "d": "days", "w": "weeks"}[m[2]]: int(m[1])})


def _profile(env: dict) -> dict:
    try:
        return load(profile_path(env)) or {}
    except ProfileError as e:
        raise WsError("bad_config", str(e), f"fix or remove {profile_path(env)}") from e


def judge_model(prof: dict) -> str | None:
    cfg = prof.get("eval")
    v = cfg.get("judge_model") if isinstance(cfg, dict) else None
    return v if isinstance(v, str) and v.strip() else None


# ---- ws eval run --------------------------------------------------------------------------------------------------


def make_plan(
    tasks: list[Task], trials: int | None, prof: dict, explicit: bool
) -> tuple[list[tuple[Task, int]], list[dict]]:
    """(task, trial count) to run, and the tasks left out with the reason. With `explicit` (one task asked for by id) a
    task that cannot run raises instead."""
    runs, skipped = [], []
    judge = judge_model(prof)
    for t in tasks:
        reason = None
        if t.approved is False or t.review_required is True:
            reason = "not approved yet (`ws eval approve`)"
        elif t.needs_judge():
            reason = (
                "has a rubric grader and the profile names no judge model ([eval] judge_model)"
                if not judge
                else "has a rubric grader, and the judge call is not built yet"
            )
        if reason:
            if explicit:
                raise WsError(
                    "refused",
                    f"task {t.id} {reason}",
                    "give it a command, file_contains, json_path_equals or regex grader",
                )
            skipped.append({"task": t.id, "reason": reason})
            continue
        runs.append((t, min(trials or t.trials, MAX_TRIALS)))
    return runs, skipped


def _find_record(log: Path, before: int, task: str, trial: int, arm: str) -> dict | None:
    try:
        recs = runrecord.read(log)[before:]
    except OSError:
        return None
    for rec in reversed(recs):
        ev = rec.get("eval")
        if isinstance(ev, dict) and (ev.get("task"), ev.get("trial"), ev.get("arm")) == (task, trial, arm):
            return rec
    return None


def task_hash(task: Task) -> str:
    """A short fingerprint of the task as it is (its canonical text), so results of an edited task are never mixed with
    results of the old one."""
    return hashlib.sha256(dumps_task(task).encode()).hexdigest()[:16]


def run_trial(task: Task, trial: int, arm: str, job: jobs.Job, batch: str, keep: bool = False) -> dict:
    """One trial through `ws run`: set up a fresh folder, run the job with the task's prompt, then grade and write the
    result. The run record is on disk before grading starts (run.run_job calls the hook after writing it), so a SIGTERM
    during grading still leaves the record, and this function still leaves a result line saying grading was cut short.
    The record's `eval.pass` stays null: the graded outcome is the result line."""
    env = os.environ
    log = runrecord.runs_path(env)
    # Not under $WS_HOME: the gate's sandbox denies shell writes there, and a trial has to be able to write its answer.
    folder = Path(tempfile.mkdtemp(prefix=f"ws-eval-{task.id}-{trial}-")).resolve()
    write_files(folder, task.setup_files)
    state: dict = {"rec": None, "verdicts": [], "passed": False, "note": None}

    def hook(rec: dict) -> None:  # called with the record after it has been written
        state["rec"] = rec
        if rec.get("status") != "ok":
            return
        state["note"] = "grading was cut short"
        try:
            write_files(folder, task.hidden_files)
            state["verdicts"] = grade(task, folder, token_hex(16))
            state["passed"] = bool(state["verdicts"]) and all(v.ok for v in state["verdicts"])
        except OSError as e:
            state["verdicts"] = [Verdict("setup", False, _why(e))]
        state["note"] = None

    trial_job = dataclasses.replace(
        job, prompt=task.prompt, cwd=str(folder), timeout_s=task.timeout_s,
        max_turns=task.max_turns or job.max_turns, schedule=None,
    )  # fmt: skip
    before = len(runrecord.read(log)) if log.exists() else 0
    marker = {"eval": {"task": task.id, "trial": trial, "arm": arm, "pass": None}}
    code = None
    try:
        code = run.run_job(argparse.Namespace(job=job.name, dry_run=False), trial_job, marker, hook)
    finally:  # also on SIGTERM: whatever got as far as a record gets a result line
        rec = state["rec"] or _find_record(log, before, task.id, trial, arm)
        result = {
            "schema": SCHEMA_VERSION, "ts": runrecord.iso(datetime.now(UTC)), "batch": batch, "task": task.id,
            "task_hash": task_hash(task), "family": task.family, "kind": task.kind, "trial": trial, "arm": arm,
            "job": job.name, "run_id": rec["run_id"] if rec else None,
            "status": rec["status"] if rec else ("refused" if code == 3 else "error"),
            "pass": bool(state["passed"] and not state["note"]),
            "grade": [dataclasses.asdict(v) for v in state["verdicts"]], "note": state["note"], "folder": str(folder),
        }  # fmt: skip
        append_jsonl(results_path(env), result)
    if result["pass"] and not keep:
        shutil.rmtree(folder, ignore_errors=True)
    return result


def _wall_state() -> str:
    try:
        require_wall()
    except WsError as e:
        return f"would be refused: {e.message}"
    return "run inside the wall, the trial folder read-only"


def _plan_text(label: str, arm: str, job_name: str | None, runs, skipped) -> str:
    total = sum(n for _, n in runs)
    lines = [
        f"ws eval: {len(runs)} tasks ({label}), {total} runs, one `ws run` each; arm {arm}, job {job_name or '(none yet)'}"
    ]
    for t, n in runs:
        kinds = ", ".join(sorted({g["type"] for g in t.graders}))
        lines.append(f"  {t.id}  {t.family}  {t.kind}  {n} trials  {t.timeout_s:g} s limit  graders: {kinds}")
    lines += [f"  skipped {s['task']}: {s['reason']}" for s in skipped]
    return "\n".join(lines)


def cmd_run(args: argparse.Namespace) -> int:
    env = os.environ
    label, tasks = _select(args, env)
    prof = _profile(env)
    runs, skipped = make_plan(tasks, args.trials, prof, explicit=bool(args.task))
    if not runs:
        raise WsError("refused", "nothing can run: " + "; ".join(f"{s['task']} {s['reason']}" for s in skipped))
    job = None
    if args.job:
        try:
            job = jobs.load(args.job, env)
        except jobs.JobError as e:
            raise WsError("bad_config", str(e), "`--job` names a file in $WS_HOME/jobs") from e
    elif not args.dry_run:
        raise WsError(
            "usage",
            "--job is needed: it is the arm under test",
            "e.g. `ws eval run --set starter --arm base --job daily`",
            2,
        )
    arm = args.arm or args.job or "default"
    total = sum(n for _, n in runs)
    needs_wall = any(g["type"] == "command" for t, _ in runs for g in t.graders)
    if needs_wall and not args.dry_run:
        require_wall()
    plan = {
        "set": label, "arm": arm, "job": args.job, "runs": total, "dry_run": args.dry_run,
        "tasks": [{"task": t.id, "family": t.family, "kind": t.kind, "trials": n, "timeout_s": t.timeout_s,
                   "graders": [g["type"] for g in t.graders]} for t, n in runs],
        "skipped": skipped,
        "note": "each run is one headless `ws run`; the monthly cap is checked before each, not during it",
        "command_graders": _wall_state() if needs_wall else "none",
    }  # fmt: skip
    if args.dry_run:
        if args.json:
            output.result(plan)
        else:
            print(_plan_text(label, arm, args.job, runs, skipped))
            if needs_wall:
                print(f"command graders: {plan['command_graders']}")
            print(f"{total} runs; this was a plan: nothing started, nothing spent")
        return 0
    return _execute(args, runs, skipped, arm, job, plan)


def _execute(args, runs, skipped, arm: str, job: jobs.Job, plan: dict) -> int:
    batch = runrecord.new_run_id()
    results: list[dict] = []
    refused = False
    if not args.json:
        print(_plan_text(plan["set"], arm, args.job, runs, skipped))
    for task, n in runs:
        for trial in range(1, n + 1):
            r = run_trial(task, trial, arm, job, batch, keep=args.keep)
            results.append(r)
            kept = "" if r["pass"] and not args.keep else f"; folder {r['folder']}"
            verdict = "pass" if r["pass"] else "fail"
            print(f"ws: {task.id} trial {trial}/{n}: {verdict} ({r['status']}, run {r['run_id']}{kept})")
            if r["status"] == "refused":
                refused = True
                break
        if refused:
            break
    passed = sum(1 for r in results if r["pass"])
    done = f"ws: {passed} of {len(results)} trials passed; results in {results_path()}"
    if refused:
        done += "; stopped at a refused run (see `ws runs`)"
    if args.json:
        output.result({**plan, "results": results, "passed": passed, "stopped_on_refusal": refused})
    else:
        print(done)
    return run.EXIT_REFUSED if refused else 0


# ---- ws eval report -----------------------------------------------------------------------------------------------


def _scope(env: dict, arm: str | None, since: timedelta) -> tuple[list[dict], dict, dict, list[dict]]:
    cutoff = datetime.now(UTC) - since
    try:
        runs_list = runrecord.read(runrecord.runs_path(env))
    except OSError as e:
        raise WsError("io", f"cannot read the run log: {e.strerror or type(e).__name__}") from e
    results = [
        r for r in read_jsonl(results_path(env))
        if (arm is None or r.get("arm") == arm) and (evalstats.parse_ts(r.get("ts")) or cutoff) >= cutoff
    ]  # fmt: skip
    runs = {r["run_id"]: r for r in runs_list if isinstance(r.get("run_id"), str)}
    in_window = [r for r in runs_list if (evalstats.parse_ts(r.get("started_at")) or cutoff) >= cutoff]
    return results, runs, rating.latest(rating.read(rating.ratings_path(env))), in_window


def _repos(env: dict) -> dict[str, tuple[str, list[str]]]:
    try:
        tasks = known_tasks(env)
    except TaskError:
        return {}
    return {t.id: (t.repo, list(t.files)) for t in tasks if t.repo}


def metrics_for(env: dict, results, runs, ratings, all_runs, days: int, k: int | None) -> dict:
    _, unknown, fallback = run._budget(_profile(env))
    computed = evalstats.compute(
        results, runs, ratings, all_runs=all_runs, repos=_repos(env), budget=(unknown, fallback), k=k, window_days=days
    )
    labels = dict(evalstats.METRICS)
    return {key: {"label": labels[key], **m} for key, m in computed.items()}


def cmd_report(args: argparse.Namespace) -> int:
    env = os.environ
    since = parse_since(args.since)
    results, runs, ratings, all_runs = _scope(env, args.arm, since)
    days = max(1, since.days)
    overall = metrics_for(env, results, runs, ratings, all_runs, days, args.k)
    arms = sorted({str(r.get("arm")) for r in results})
    by_arm = {}
    if len(arms) > 1:
        for a in arms:
            by_arm[a] = metrics_for(
                env, [r for r in results if r.get("arm") == a], runs, ratings, all_runs, days, args.k
            )
    if args.json:
        output.result({"since": args.since, "trials": len(results), "arms": arms, "metrics": overall, "by_arm": by_arm})
        return 0
    print(f"ws eval report: {len(results)} trials in the last {args.since}; arms: {', '.join(arms) or 'none yet'}")
    for title, block in [("all arms" if by_arm else "", overall), *by_arm.items()]:
        if title:
            print(f"-- {title}")
        for i, m in enumerate(block.values(), 1):
            print(f"{i:>2}. {m['label']}: {m['display']}")
    return 0


# ---- ws eval compare ----------------------------------------------------------------------------------------------


def _arm_results(results: list[dict], arm: str) -> list[dict]:
    return [r for r in results if r.get("arm") == arm]


def _down_rate(results: list[dict], ratings: dict[str, str]) -> tuple[float | None, int]:
    """(thumbs-down share, how many ratings it rests on) for the trials of these results."""
    rated = [ratings[str(r["run_id"])] for r in results if r.get("run_id") and str(r["run_id"]) in ratings]
    return (rated.count("down") / len(rated) if rated else None), len(rated)


def compare_arms(arm_a: str, arm_b: str, results: list[dict], runs: dict, ratings: dict) -> dict:
    """Two arms, paired at task level (see `evalstats.paired_tasks`: McNemar needs independent pairs, and the trials of
    one task are not). The task counts and the trial counts behind them are both in the result."""
    pt = evalstats.paired_tasks(_arm_results(results, arm_a), _arm_results(results, arm_b))
    ra, rb = pt["used_a"], pt["used_b"]
    n, a_only, b_only = pt["tasks"], pt["a_only"], pt["b_only"]
    down_a, rated_a = _down_rate(ra, ratings)
    down_b, rated_b = _down_rate(rb, ratings)
    stats = {
        "pairs": n, "tasks": n, "trials_a": pt["trials_a"], "trials_b": pt["trials_b"], "both_pass": pt["both"],
        "a_only": a_only, "b_only": b_only, "both_fail": pt["neither"], "dropped": pt["dropped"],
        "gap_points": (a_only - b_only) / n * 100 if n else 0.0, "p": evalstats.mcnemar_p(a_only, b_only),
        "pass_rate_a": (pt["both"] + a_only) / n if n else None, "pass_rate_b": (pt["both"] + b_only) / n if n else None,
        "down_a": down_a, "down_b": down_b, "rated_a": rated_a, "rated_b": rated_b,
        "tokens_a": evalstats.tokens_per_solved(ra, runs), "tokens_b": evalstats.tokens_per_solved(rb, runs),
        "wins_needed": evalstats.wins_needed(a_only + b_only),
    }  # fmt: skip
    verdict = evalstats.decide(stats)
    winner = {"A": arm_a, "B": arm_b}.get(verdict["winner"])
    return {
        "arm_a": arm_a, "arm_b": arm_b, **stats, "winner": winner, "why": verdict["why"],
        "quality": evalstats.quality_note(stats),
    }  # fmt: skip


def _show_compare(c: dict, look: int, replay: bool) -> None:
    print(
        f"ws eval compare: A={c['arm_a']} vs B={c['arm_b']}, look {look} of 2{' (stored, not re-run)' if replay else ''}"
    )
    ra, rb = c["pass_rate_a"], c["pass_rate_b"]
    if ra is not None and rb is not None:
        print(
            f"  paired tasks {c['tasks']} (from {c['trials_a']} trials of A, {c['trials_b']} of B): "
            f"A passes {ra * 100:.1f}%, B passes {rb * 100:.1f}% (a task passes when most of its trials do)"
        )
    d = c["dropped"]
    left_out = sum(d.values())
    if left_out:
        print(
            f"  left out: {d['other_job']} results from an earlier job, {d['other_task_text']} from an earlier "
            f"version of a task, {d['task_text_differs']} tasks whose text differs between the arms"
        )
    print(
        f"  A only {c['a_only']}, B only {c['b_only']}, both {c['both_pass']}, neither {c['both_fail']}; "
        f"exact McNemar p={c['p']:.4f}"
    )
    for key, name in (("down_a", "thumbs-down rate A"), ("down_b", "thumbs-down rate B"),
                      ("tokens_a", "tokens per solved A"), ("tokens_b", "tokens per solved B")):  # fmt: skip
        v = c[key]
        print(
            f"  {name}: {'not measured' if v is None else (f'{v * 100:.1f}%' if key.startswith('down') else f'{v:,.0f}')}"
        )
    if c.get("quality"):
        print(f"  quality: {c['quality']}")
    print(f"  {'winner ' + c['winner'] if c['winner'] else 'no winner'}: {c['why']}")


def cmd_compare(args: argparse.Namespace) -> int:
    env = os.environ
    a, b = args.arm_a, args.arm_b
    if a == b:
        raise WsError(
            "usage", "compare two different arms", "an A/A run is two runs of the same job under two arm names", 2
        )
    pair = sorted((a, b))  # stored as a JSON array, so no arm name can be confused with a separator
    stored = [x for x in read_jsonl(looks_path(env)) if x.get("pair") == pair]
    used = len(stored)
    if args.look is not None:
        old = next((x for x in stored if x.get("look") == args.look), None)
        if old is not None:
            if args.json:
                output.result({**old["result"], "look": args.look, "replay": True})
            else:
                _show_compare(old["result"], args.look, True)
            return 0
        if args.look != used + 1:
            raise WsError(
                "refused", f"look {args.look} is not next for {a} and {b}: {used} used", f"the next look is {used + 1}"
            )
    elif used >= 2:
        raise WsError(
            "refused",
            f"{a} and {b} have had their two looks",
            "show a stored one with `--look 1` or `--look 2`; a third look is peeking",
        )
    look = used + 1
    results, runs, ratings, _ = _scope(env, None, timedelta(days=36500))
    c = compare_arms(a, b, results, runs, ratings)
    if c["pairs"] == 0:
        raise WsError(
            "not_found",
            f"no task that both {a} and {b} ran, in the same version of the task and on one job each",
            "run both arms on the same tasks first",
        )
    append_jsonl(
        looks_path(env),
        {"schema": SCHEMA_VERSION, "ts": runrecord.iso(datetime.now(UTC)), "pair": pair, "look": look, "result": c},
    )
    if args.json:
        output.result({**c, "look": look, "replay": False})
    else:
        _show_compare(c, look, False)
    return 0


# ---- ws eval harvest / approve / reject ---------------------------------------------------------------------------

SECRET_PATTERNS = (
    re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(?:ghp|gho|ghs|github_pat)_[A-Za-z0-9_]{20,}"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._~+/=-]{16,}", re.IGNORECASE),
    re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."),
    re.compile(r"/l/[A-Za-z0-9_-]{20,}"),  # a broker lease URL
    re.compile(r"\b(?:api[_-]?key|secret|token)\b\s*[:=]\s*\S{8,}", re.IGNORECASE),
    re.compile(r"\b(?:passw(?:or)?d|passwd|pwd|passphrase)\b\s*[:=]", re.IGNORECASE),
    re.compile(r"\b[a-z][a-z0-9+.-]*://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),  # scheme://user:password@host
    re.compile(r"\b[A-Za-z0-9_-]{40,}\b"),
)
BASE64ISH = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{32,}={0,2}(?![A-Za-z0-9+/=])")
RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")
SECRET_CHECK = "prompts were checked against the names in your secrets registry and for the shapes of keys, tokens and passwords; not against secret values, which ws cannot read safely"


def secret_reason(text: str, env: dict) -> str | None:
    """Why `text` may not be copied into a task, or None. ws has no prompt scrubber, so this refuses: a registered
    secret's name, or anything shaped like a key, token, password, URL with a password or private key. It does not check
    secret values: the store belongs to the broker, and the registry has no check that avoids reading them (SECRET_CHECK
    says so in the output). Raises SecretsError if the registry cannot be read (then nothing is proposed)."""
    for name in wsecrets.load_registry(wsecrets.registry_path(env)):
        if re.search(rf"(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text, re.IGNORECASE):
            return f"names the registered secret {name!r}"
    for pat in SECRET_PATTERNS:
        if pat.search(text):
            return "looks like it holds a key, token or password"
    for m in BASE64ISH.finditer(text):  # mixed case and a digit, so a plain long path or word is not caught
        t = m.group(0)
        if any(c.isdigit() for c in t) and any(c.islower() for c in t) and any(c.isupper() for c in t):
            return "has a long base64-like string in it"
    return None


def candidates(runs: list[dict], ratings: dict[str, str], since: datetime) -> dict[str, tuple[dict, list[str]]]:
    """job -> (its most recent run worth a task, the reasons). A run is worth one when it failed, timed out, was rated
    down, or was an `ok` run that followed a failure of the same job within a day (a retry). Refused runs are the
    policy working, and eval trials are not looked at: neither is a task."""
    by_job: dict[str, list[dict]] = {}
    for r in runs:
        if isinstance(r.get("job"), str) and r.get("eval") is None:
            by_job.setdefault(r["job"], []).append(r)
    found: dict[str, tuple[dict, list[str]]] = {}
    for job, recs in by_job.items():
        recs.sort(key=lambda r: str(r.get("started_at")))
        prev = None
        for r in recs:
            t = evalstats.parse_ts(r.get("started_at"))
            reasons = []
            if t and t >= since:
                reasons += {"error": ["failed"], "timeout": ["timed_out"]}.get(r.get("status"), [])
                if ratings.get(str(r.get("run_id"))) == "down":
                    reasons.append("rated_down")
                pt = evalstats.parse_ts(prev.get("started_at")) if prev else None
                if (
                    r.get("status") == "ok"
                    and pt
                    and prev.get("status") in ("error", "timeout")
                    and t - pt <= timedelta(days=1)
                ):
                    reasons.append("retried")
            if reasons:
                old = found.get(job, (None, []))[1]
                found[job] = (r, sorted({*old, *reasons}))
            if r.get("status") != "refused":
                prev = r
    return found


def _already(env: dict) -> tuple[set[str], set[str]]:
    """(source runs, source jobs) of every task already proposed, approved or rejected."""
    runs_seen, jobs_seen = set(), set()
    for folder in (proposed_dir(env), tasks_dir(env), rejected_dir(env)):
        for p in sorted(folder.glob("*.toml")) if folder.is_dir() else []:
            with contextlib.suppress(OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
                d = tomllib.loads(p.read_text(encoding="utf-8"))
                runs_seen.add(str(d.get("source_run")))
                jobs_seen.add(str(d.get("source_job")))
    return runs_seen, jobs_seen


def propose(rec: dict, reasons: list[str], job: jobs.Job) -> Task:
    """A proposal for a run. The run record keeps no prompt, so the prompt is the job file's as it is now, and the
    proposal says so (`prompt_source`): the job may have changed since the run."""
    return Task(
        id=f"harvest-{rec['run_id']}", family="work-style", kind="capability", prompt=job.prompt, trials=DEFAULT_TRIALS,
        timeout_s=min(job.timeout_s, MAX_TIMEOUT_S), max_turns=job.max_turns,
        graders=({"type": "rubric", "rubric": PLACEHOLDER_RUBRIC},), approved=False,
        source_run=rec["run_id"], source_job=rec["job"], reason=", ".join(reasons), review_required=True,
        prompt_source=f"the job file {rec['job']}.toml as it is now (run records keep no prompt)",
    )  # fmt: skip


def _check_run_id(rec: dict) -> str:
    rid = rec.get("run_id")
    if not isinstance(rid, str) or not RUN_ID.match(rid):
        raise WsError(
            "bad_config",
            f"a run record worth a task has a malformed run_id ({str(rid)[:40]!r})",
            f"fix or remove that line of {runrecord.runs_path()}; nothing was proposed",
        )
    return rid


def harvest(env: dict, since: timedelta, now: datetime | None = None) -> tuple[list[dict], list[dict]]:
    """Write a proposal for each job with a run worth a task. Returns (proposals, skipped with the reason). All or
    nothing: every proposal is checked as a task file first, and if one cannot be written the others are removed again.
    A malformed run id or a proposal that is not a valid task raises WsError."""
    now = now or datetime.now(UTC)
    try:
        runs = runrecord.read(runrecord.runs_path(env))
    except OSError as e:
        raise WsError("io", f"cannot read the run log: {e.strerror or type(e).__name__}") from e
    found = candidates(runs, rating.latest(rating.read(rating.ratings_path(env))), now - since)
    seen_runs, seen_jobs = _already(env)
    proposals, skipped, files = [], [], []
    for name, (rec, reasons) in sorted(found.items()):
        rid = _check_run_id(rec)
        if rid in seen_runs or name in seen_jobs:
            continue
        try:
            job = jobs.load(name, env)
        except jobs.JobError as e:
            skipped.append({"job": name, "run": rid, "reason": f"the job file cannot be used: {str(e)[:120]}"})
            continue
        if len(job.prompt) > MAX_PROMPT:
            skipped.append({"job": name, "run": rid, "reason": "the prompt is too long for a task"})
            continue
        try:
            why = secret_reason(job.prompt, env)
        except wsecrets.SecretsError as e:
            raise WsError(
                "bad_config",
                f"cannot check prompts against the secrets registry: {e}",
                "fix the registry; nothing was proposed",
            ) from e
        if why:
            skipped.append({"job": name, "run": rid, "reason": f"the prompt {why}; not copied"})
            continue
        task = propose(rec, reasons, job)
        path = proposed_dir(env) / f"{task.id}.toml"
        text = dumps_task(task)
        try:
            parse_task(text, str(path), task.id)  # what is written must read back
        except TaskError as e:
            raise WsError(
                "bad_config", f"the proposal for job {name} is not a valid task: {e}", "nothing was proposed"
            ) from e
        files.append((path, text))
        proposals.append({"task": task.id, "job": name, "source_run": rid, "reasons": reasons, "path": str(path)})
    written: list[Path] = []
    try:
        for path, text in files:
            write_private(path, text, exclusive=True)
            written.append(path)
    except OSError as e:
        for path in written:
            path.unlink(missing_ok=True)
        raise WsError(
            "io", f"could not write the proposals ({_why(e)}); none were kept", f"check {proposed_dir(env)}"
        ) from e
    return proposals, skipped


def cmd_harvest(args: argparse.Namespace) -> int:
    proposals, skipped = harvest(os.environ, parse_since(args.since))
    if args.json:
        output.result(
            {"proposed": proposals, "skipped": skipped, "folder": str(proposed_dir()), "secret_check": SECRET_CHECK}
        )
        return 0
    for p in proposals:
        print(f"proposed {p['task']}  job {p['job']}  ({', '.join(p['reasons'])})")
    for s in skipped:
        print(f"skipped job {s['job']}: {s['reason']}")
    if proposals:
        print(f"ws: {len(proposals)} proposals in {proposed_dir()}; each has a placeholder grader and approved = false")
        print("ws: read one, give it a real grader, then `ws eval approve ID` (or `ws eval reject ID`)")
    else:
        print(f"ws: nothing new to propose from the last {args.since}")
    print(f"ws: note: {SECRET_CHECK}", file=sys.stderr)
    return 0


def _proposal(env: dict, task_id: str) -> Path:
    if not NAME.match(task_id):
        raise WsError("usage", f"{task_id!r} is not a task id", "ids are letters, digits, - and _", 2)
    p = proposed_dir(env) / f"{task_id}.toml"
    if not p.is_file():
        raise WsError("not_found", f"no proposal {task_id!r} in {proposed_dir(env)}", "`ws eval harvest` makes them")
    return p


def _confirm(task: Task, args: argparse.Namespace) -> bool:
    """Show what is being approved and ask the person. `--yes` is for scripts; with `--json` or no terminal there is no one
    to ask, so it is refused without it. Returns False when the person says no."""
    if not args.json:
        print(f"ws: proposal {task.id} (job {task.source_job}, run {task.source_run}; {task.reason})")
        print(f"ws: prompt from {task.prompt_source or 'an unknown source'}:")
        print(task.prompt)
    if args.yes:
        return True
    if args.json or not sys.stdin.isatty():
        raise WsError(
            "refused",
            "approving needs your confirmation, and there is no terminal to ask on",
            "run `ws eval approve ID` in a terminal, or pass --yes after reading the proposal",
        )
    try:
        return input("Approve this task? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        return False


def cmd_approve(args: argparse.Namespace) -> int:
    env = os.environ
    src = _proposal(env, args.id)
    try:
        task = _read_task(src)
    except TaskError as e:
        raise WsError("bad_config", str(e), f"fix {src}, then approve again") from e
    dest = tasks_dir(env) / f"{task.id}.toml"
    if dest.exists():
        raise WsError("refused", f"{dest} already exists", "approve under another id, or remove that file")
    if not _confirm(task, args):
        print(f"ws: not approved; {task.id} stays in {src}")
        return 1
    approved = dataclasses.replace(task, approved=True, review_required=False)
    write_private(dest, dumps_task(approved), exclusive=True)
    src.unlink()
    placeholder = approved.needs_judge()
    if args.json:
        output.result({"task": task.id, "path": str(dest), "needs_grader": placeholder, "prompt": task.prompt})
        return 0
    print(f"ws: approved {task.id}; it is now {dest}")
    if placeholder:
        print(
            "ws: its grader is still a rubric, which does not run yet: edit it to a command, file_contains, "
            "json_path_equals or regex grader"
        )
    return 0


def cmd_reject(args: argparse.Namespace) -> int:
    env = os.environ
    src = _proposal(env, args.id)
    dest = rejected_dir(env) / src.name
    dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.replace(src, dest)
    if args.json:
        output.result({"task": args.id, "path": str(dest)})
    else:
        print(f"ws: rejected {args.id}; kept in {dest} so harvest does not propose it again")
    return 0


# ---- the command --------------------------------------------------------------------------------------------------


def _trials(text: str) -> int:
    n = int(text)
    if not 1 <= n <= MAX_TRIALS:
        raise argparse.ArgumentTypeError(f"must be 1 to {MAX_TRIALS}")
    return n


def register(sub) -> None:
    e = sub.add_parser("eval", help="your own eval: run tasks, read ten metrics, compare two setups, harvest tasks")
    esub = e.add_subparsers(dest="eval_command", required=True)

    r = esub.add_parser("run", help="run eval tasks as trials of a job (`--dry-run` shows the plan and spends nothing)")
    pick = r.add_mutually_exclusive_group()
    pick.add_argument("--task", metavar="ID", help="one task, yours or a starter")
    pick.add_argument("--set", choices=("starter", "mine"), help="a whole set (default mine)")
    r.add_argument("--arm", metavar="NAME", help="a label for the setup under test (default: the job name)")
    r.add_argument("--job", metavar="JOB", help="the job file that is the arm under test (a runner, model and options)")
    r.add_argument(
        "--trials", type=_trials, metavar="N", help=f"trials per task, 1 to {MAX_TRIALS} (default: the task's)"
    )
    r.add_argument(
        "--keep", action="store_true", help="keep the folder of a passed trial too (a failed one is always kept)"
    )
    r.add_argument("--dry-run", action="store_true", help="show the plan and the number of runs; start nothing")
    r.set_defaults(func=cmd_run)

    p = esub.add_parser("report", help="the ten metrics; a metric with no data says 'not measured'")
    p.add_argument("--arm", metavar="NAME", help="only this arm")
    p.add_argument("--since", default="90d", metavar="SPAN", help="how far back, like 30d or 4w (default 90d)")
    p.add_argument("--k", type=_trials, metavar="K", help="the k of pass^k (default: the fewest trials any task has)")
    p.set_defaults(func=cmd_report)

    c = esub.add_parser("compare", help="two arms on paired trials: exact McNemar, quality veto, then tokens")
    c.add_argument("arm_a", metavar="ARM_A")
    c.add_argument("arm_b", metavar="ARM_B")
    c.add_argument("--look", type=int, choices=(1, 2), help="declare the look; an earlier one is shown from storage")
    c.set_defaults(func=cmd_compare)

    h = esub.add_parser("harvest", help="propose tasks from your failed, timed-out, rated-down or retried runs")
    h.add_argument("--since", default="30d", metavar="SPAN", help="how far back (default 30d)")
    h.set_defaults(func=cmd_harvest)

    a = esub.add_parser("approve", help="show a proposal's prompt, ask, and move it into your tasks")
    a.add_argument("id")
    a.add_argument("--yes", action="store_true", help="do not ask (for scripts); you have read the proposal")
    a.set_defaults(func=cmd_approve)
    x = esub.add_parser("reject", help="set a proposal aside so harvest does not offer it again")
    x.add_argument("id")
    x.set_defaults(func=cmd_reject)
