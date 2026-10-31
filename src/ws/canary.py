"""`ws canary`: a few small checks that a runner's update did not break what ws relies on. No model is called, so it
costs nothing.

    ws canary [--runner R] [--json]     run the checks; one line goes to `$WS_HOME/canary.jsonl`; exit 1 if any fail

Per runner:
  version      `<runner> --version` runs
  gate         the generated gate still stops a denied command and lets a plain one through, by running the real
               `ws gate check` hook entry on that runner's own payload shape
  hook_config  the config ws generates for the runner parses and still holds the hook
  run_record   a run record still validates against `runschema` (stdlib check, so no extra dependency)

`ws doctor` records the last version it saw of each runner, and the default model id, in `$WS_HOME/versions.json`.
When one changes it says so in WS101 and runs this (or, with --offline, points here). A change is remembered only when
a canary passes, whether doctor or `ws canary` ran it, so the note stays until then. ws never forces an update: people
update when they update, and this is how ws finds out whether it still works after they did.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ws import __version__, adapters, gate, policy, runrecord
from ws.profile import ProfileError, load, profile_path

try:  # not on native Windows; ws runs there only through WSL2, but the module must still import
    import fcntl
except ImportError:  # pragma: no cover
    fcntl = None  # type: ignore[assignment]

SCHEMA_VERSION = 1
PASS, FAIL, SKIP = "pass", "fail", "skip"
NO_VERSION = "ran (it printed no version)"
DENIED = "git push --force origin main"  # in the default deny list; a plain `git push` is not
ALLOWED = "ls"

# A runner's own hook payload for a shell command: the shape its adapter's parse() reads.
PAYLOADS = {
    "claude-code": lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}},
    "opencode": lambda cmd: {"tool": "bash", "args": {"command": cmd}},
}


def _home(env) -> Path | None:
    """ws's home as `env` says it, or None if env names neither WS_HOME nor HOME (a bare test environment): nothing is
    written then, so a check run with an empty environment never lands in the real home."""
    if env.get("WS_HOME"):
        return Path(env["WS_HOME"]).expanduser()
    return Path(env["HOME"]) / ".ws" if env.get("HOME") else None


# ---- versions.json -----------------------------------------------------------------------------------------------------


def versions_path(env) -> Path | None:
    home = _home(env)
    return home / "versions.json" if home else None


def _read_versions(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def observe(env, runner: str, version: str | None, model: str | None, record: bool = True) -> list[str]:
    """What changed since the last sighting, as short phrases ("claude-code 2.1.0, was 2.0.9"), and (unless record is
    False) remember what is seen now. The first sighting is not a change. Nothing is read or kept unless ws's home
    already exists: doctor does not make it."""
    path = versions_path(env)
    if path is None or not path.parent.is_dir():
        return []
    data = _read_versions(path)
    runners = data["runners"] if isinstance(data.get("runners"), dict) else {}
    changes = []
    old = runners.get(runner)
    old_version = old.get("version") if isinstance(old, dict) else None
    if version and old_version and old_version != version:
        changes.append(f"{runner} {version}, was {old_version}")
    if version:
        runners[runner] = {"version": version}
    old_model = data.get("default_model") if isinstance(data.get("default_model"), str) else None
    if model and old_model and old_model != model:
        changes.append(f"default model {model}, was {old_model}")
    if not record:
        return changes
    doc = {"schema": SCHEMA_VERSION, "runners": runners, "default_model": model or old_model}
    text = json.dumps(doc, indent=2) + "\n"
    try:
        if not path.is_file() or path.read_text(encoding="utf-8") != text:
            _replace(path, text)
    except OSError:
        pass  # doctor reports; a record it cannot keep is not a reason to fail it
    return changes


def _replace(path: Path, text: str) -> None:
    """Write beside the target under a unique name, then rename over it: two doctors at once never share a temp file."""
    fd, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(name, path)
    except BaseException:
        Path(name).unlink(missing_ok=True)
        raise


def _append_line(path: Path, line: dict) -> None:
    """One write() on an O_APPEND fd under an flock, 0600: the same way runs.jsonl and ratings.jsonl are written."""
    data = (json.dumps(line, separators=(",", ":")) + "\n").encode()
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        os.write(fd, data)
    finally:
        os.close(fd)


# ---- a small JSON Schema check (the subset runschema uses) -----------------------------------------------------------


def _is(value, kind: str) -> bool:
    if kind == "null":
        return value is None
    if kind == "boolean":
        return isinstance(value, bool)
    if kind in ("integer", "number"):
        return (
            isinstance(value, int | float) and not isinstance(value, bool) and (kind == "number" or value == int(value))
        )
    return isinstance(value, {"string": str, "array": list, "object": dict}[kind])


def validate(value, schema: dict, where: str = "$") -> list[str]:
    """Problems found in `value` against the keywords runschema uses: type, const, enum, pattern, minimum, minLength,
    maxLength, required, properties, additionalProperties false and items. An empty list means it validates."""
    out: list[str] = []
    if "const" in schema and value != schema["const"]:
        out.append(f"{where}: must be {schema['const']!r}")
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{where}: {value!r} is not one of {schema['enum']}")
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is(value, k) for k in kinds):
            return [*out, f"{where}: {type(value).__name__} is not {'/'.join(kinds)}"]
    if isinstance(value, str):
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{where}: {value!r} does not match {schema['pattern']}")
        if len(value) < schema.get("minLength", 0):
            out.append(f"{where}: too short")
        if len(value) > schema.get("maxLength", len(value)):
            out.append(f"{where}: too long")
    if isinstance(value, int | float) and not isinstance(value, bool) and value < schema.get("minimum", value):
        out.append(f"{where}: {value} is below {schema['minimum']}")
    if isinstance(value, dict):
        out += [f"{where}: missing {k}" for k in schema.get("required", []) if k not in value]
        props = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            out += [f"{where}: unexpected {k}" for k in value if k not in props]
        for k, sub in props.items():
            if k in value:
                out += validate(value[k], sub, f"{where}.{k}")
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            out += validate(item, schema["items"], f"{where}[{i}]")
    return out


# ---- the checks --------------------------------------------------------------------------------------------------------
# Each returns (PASS | FAIL | SKIP, a few words).


def check_version(adapter, env) -> tuple[str, str]:
    from ws import run  # the one place that knows how to read a runner's version

    exe = shutil.which(adapter.EXE, path=env.get("PATH"))
    if not exe:
        return FAIL, f"`{adapter.EXE}` is not on PATH"
    version = run.exe_version(exe, dict(env))
    if version is None:
        return FAIL, f"`{adapter.EXE} --version` did not run"
    return PASS, version or NO_VERSION


def _hook(runner: str, payload: dict, policy_file: Path, home: Path, env) -> int:
    """The exit code of the real hook entry for one payload."""
    child = {k: env[k] for k in ("PATH", "HOME", "LANG") if k in env}
    child["WS_HOME"] = str(home)
    child["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])  # `-m ws` finds this ws even if it is not installed
    p = subprocess.run(
        [sys.executable, "-m", "ws", "gate", "check", "--runner", runner, "--policy", str(policy_file)],
        input=json.dumps(payload), env=child, capture_output=True, text=True, timeout=30, check=False,
    )  # fmt: skip
    return p.returncode


def check_gate(adapter, env, work: Path) -> tuple[str, str]:
    make = PAYLOADS.get(adapter.NAME)
    if make is None:
        return SKIP, f"no canned payload for {adapter.NAME}"
    policy_file = work / "policy.toml"
    policy_file.write_text(policy.default_toml(), encoding="utf-8")
    try:
        denied = _hook(adapter.NAME, make(DENIED), policy_file, work, env)
        allowed = _hook(adapter.NAME, make(ALLOWED), policy_file, work, env)
    except (OSError, subprocess.SubprocessError) as e:
        return FAIL, f"could not run `ws gate check` ({type(e).__name__})"
    if denied != gate.DENY:
        return FAIL, f"a denied command was not blocked (exit {denied}, wanted {gate.DENY})"
    if allowed != 0:
        return FAIL, f"a plain command was blocked (exit {allowed})"
    return PASS, "blocks a denied command, allows a plain one"


def check_hook_config(adapter, work: Path) -> tuple[str, str]:
    policy_file = work / "policy.toml"
    policy_file.write_text(policy.default_toml(), encoding="utf-8")
    try:
        files = adapter.generate(policy.loads(policy.default_toml()), ["ws"], work / "gate", policy_file)
        parsed = {name: json.loads(text) for name, text in files.items() if name.endswith(".json")}
    except (policy.PolicyError, ValueError, KeyError, TypeError) as e:
        return FAIL, f"the generated config does not parse ({type(e).__name__})"
    if not parsed:
        return FAIL, "the adapter generated no JSON config"
    if adapter.NAME == "claude-code":
        settings = parsed.get("settings.json", {})
        try:
            command = settings["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        except (KeyError, IndexError, TypeError):
            return FAIL, "settings.json has no PreToolUse hook"
        if "gate check" not in command or not settings.get("permissions", {}).get("deny"):
            return FAIL, "settings.json lost the gate hook or the deny rules"
    else:
        extra = [name for name, text in files.items() if not name.endswith(".json") and not text.strip()]
        if extra:
            return FAIL, f"{extra[0]} is empty"
    return PASS, ", ".join(sorted(files))


def sample_record() -> dict:
    now = datetime.now(UTC)
    return runrecord.new(
        run_id=runrecord.new_run_id(now), job="canary", runner="canary", started_at=runrecord.iso(now),
        ended_at=runrecord.iso(now), duration_ms=0, status="ok", ws_version=__version__,
    )  # fmt: skip


def check_run_record(work: Path) -> tuple[str, str]:
    schema = runrecord.schema()
    if set(runrecord.KEYS) != set(schema["required"]) or set(runrecord.KEYS) != set(schema["properties"]):
        return FAIL, "runrecord.KEYS and runschema disagree"
    log = work / "runs.jsonl"
    try:
        runrecord.append(log, sample_record())
        (rec,) = runrecord.read(log)
    except (OSError, ValueError) as e:
        return FAIL, f"a record would not write and read back ({type(e).__name__})"
    problems = validate(rec, schema)
    return (FAIL, problems[0]) if problems else (PASS, f"{len(runrecord.KEYS)} fields")


def canary_runner(runner: str, env, work: Path) -> dict:
    adapter = adapters.get(runner)
    checks = {
        "version": check_version(adapter, env),
        "gate": check_gate(adapter, env, work),
        "hook_config": check_hook_config(adapter, work),
        "run_record": check_run_record(work),
    }
    return {
        "version": checks["version"][1]
        if checks["version"][0] == PASS and checks["version"][1] != NO_VERSION
        else None,
        "ok": all(s != FAIL for s, _ in checks.values()),
        "checks": {k: s for k, (s, _) in checks.items()},
        "details": {k: d for k, (s, d) in checks.items()},
    }


def run_canary(runners: list[str], env=None, model: str | None = None) -> dict:
    """Run the checks for each runner and append one line to canary.jsonl (when ws's home exists). A runner that passes
    has its version (and the default model) remembered in versions.json, so a change is reported until a canary passes.
    Returns the line."""
    env = os.environ if env is None else env
    results = {}
    with tempfile.TemporaryDirectory(prefix="ws-canary-") as tmp:
        for r in runners:
            work = Path(tmp) / r
            work.mkdir()
            results[r] = canary_runner(r, env, work)
    line = {
        "schema": SCHEMA_VERSION,
        "ts": runrecord.iso(datetime.now(UTC)),
        "ws_version": __version__,
        "ok": all(r["ok"] for r in results.values()),
        "model": model,
        "runners": {r: {k: v for k, v in res.items() if k != "details"} for r, res in results.items()},
    }
    for r, res in results.items():  # a passing canary is what makes a new version the one ws remembers
        if res["ok"]:
            observe(env, r, res["version"], model)
    home = _home(env)
    if home and home.is_dir():
        try:
            _append_line(home / "canary.jsonl", line)
        except OSError:
            pass
    return {**line, "details": {r: res["details"] for r, res in results.items()}}


def failures(result: dict) -> list[str]:
    """`runner check: why` for every check that failed."""
    return [
        f"{r} {k}: {result['details'][r][k]}"
        for r, res in result["runners"].items()
        for k, s in res["checks"].items()
        if s == FAIL
    ]


# ---- the command -------------------------------------------------------------------------------------------------------


def _profile(env) -> dict:
    try:
        return load(profile_path(env)) or {}
    except ProfileError:
        return {}


def pick_runners(requested: str | None, env) -> list[str]:
    """The runner asked for, else the profile's, else every runner found on PATH."""
    known = adapters.all_adapters()
    if requested:
        return [requested]
    roles = _profile(env).get("roles")
    cfg = roles.get("runner") if isinstance(roles, dict) else None
    chosen = cfg.get("adapter") if isinstance(cfg, dict) else None
    if chosen in known:
        return [chosen]
    return [n for n, m in known.items() if shutil.which(m.EXE, path=env.get("PATH"))]


def cmd_canary(args: argparse.Namespace) -> int:
    env = os.environ
    runners = pick_runners(args.runner, env)
    if not runners:
        print("ws: no runner to check: name one with --runner, or put claude or opencode on PATH", file=sys.stderr)
        return 1
    roles = _profile(env).get("roles")
    cfg = roles.get("model") if isinstance(roles, dict) else None
    model = cfg.get("default") if isinstance(cfg, dict) and isinstance(cfg.get("default"), str) else None
    result = run_canary(runners, env, model)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        for r, res in result["runners"].items():
            print(f"{r}  {'ok' if res['ok'] else 'FAILED'}  {res['version'] or 'version unknown'}")
            for k, s in res["checks"].items():
                print(f"  {s:<5} {k:<12} {result['details'][r][k]}")
    return 0 if result["ok"] else 1


def register(sub) -> None:
    c = sub.add_parser(
        "canary",
        help="check a runner still works with ws after an update (no model is called)",
        description="Small checks, no paid model. Each run adds one line to $WS_HOME/canary.jsonl. Exit 1 on a failure.",
    )
    c.add_argument("--runner", choices=list(adapters.all_adapters()), help="default: the profile's runner")
    c.add_argument("--json", action="store_true", help="the result as JSON")
    c.set_defaults(func=cmd_canary)
