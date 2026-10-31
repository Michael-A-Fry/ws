"""Claude Code: a settings file with `permissions.deny` (the runner's own rules, the primary layer) plus a PreToolUse
hook that asks `ws gate check` (the second layer). `claude --bare` skips hooks, so the deny rules must stand alone;
the hook command ends `|| exit 2` so a missing or crashing ws blocks the call instead of letting it through (any
other hook failure is non-blocking in Claude Code).

Also denied: Edit/Write of the policy file and of the generated folder, so a session cannot loosen its own gate.

Pass the file with `claude --settings OUT/settings.json`. Nothing here touches ~/.claude.
"""

from __future__ import annotations

import json
import math
import re
import shlex
from pathlib import Path

from ws import jobs
from ws.adapters import Call, check_argv, protected_paths
from ws.policy import Policy, native_patterns

NAME = "claude-code"
FILE = "settings.json"
MATCHER = "Bash|Read|Grep|Glob|WebFetch|WebSearch|Edit|Write|MultiEdit|NotebookEdit"
_TOOLS = {"web_fetch": "WebFetch", "web_search": "WebSearch"}
_NEUTRAL = {v: k for k, v in _TOOLS.items()}


def _path(path: str) -> str:
    """Claude's path syntax: `~/x` is home, `//x` is an absolute path, anything else is cwd-relative gitignore."""
    return f"/{path}" if path.startswith("/") else path


def generate(policy: Policy, ws_cmd: list[str], out: Path, policy_file: Path) -> dict[str, str]:
    deny = [f"Bash({p})" for p in native_patterns(policy)]
    deny += [f"Read({_path(p)})" for p in policy.gate.deny_read]
    for p in protected_paths(out, policy_file):  # a file or a folder: cover both; Edit rules cover Write too
        deny += [f"Edit({_path(p)})", f"Edit({_path(p)}/**)"]
    deny += [_TOOLS[t] for t in policy.gate.deny_tools]
    hook = shlex.join(check_argv(ws_cmd, NAME, policy_file, out)) + " || exit 2"
    settings = {
        "permissions": {"deny": list(dict.fromkeys(deny))},
        "hooks": {"PreToolUse": [{"matcher": MATCHER, "hooks": [{"type": "command", "command": hook}]}]},
    }
    return {FILE: json.dumps(settings, indent=2) + "\n"}


def parse(payload: dict) -> Call | list[Call] | None:
    """A PreToolUse payload: {"tool_name": "Bash", "tool_input": {"command": ...}, ...}."""
    name = payload.get("tool_name")
    inp = payload.get("tool_input")
    if not isinstance(name, str) or not isinstance(inp, dict):
        raise TypeError("payload has no tool_name and tool_input")
    if name == "Bash":
        return Call("command", _text(inp, "command"))
    if name == "Read":
        return Call("read", _text(inp, "file_path"))
    if name in ("Edit", "Write", "MultiEdit"):
        return Call("write", _text(inp, "file_path"))
    if name == "NotebookEdit":
        return Call("write", _text(inp, "notebook_path"))
    if name in ("Grep", "Glob"):
        calls = [Call("search", _optional(inp, "path") or ".")]
        pattern = inp.get("pattern")
        if name == "Glob" and isinstance(pattern, str) and pattern.startswith(("/", "~")):
            calls.append(Call("read", pattern))  # an absolute glob names its own folder
        return calls
    if name in _NEUTRAL:
        return Call("tool", _NEUTRAL[name])
    return None


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"tool_input.{key} is missing")
    return v


def _optional(d: dict, key: str) -> str | None:
    v = d.get(key)
    if v is not None and not isinstance(v, str):
        raise TypeError(f"tool_input.{key} must be text")
    return v


def launch_hint(out: str) -> str:
    return f"start Claude Code with: claude --settings {shlex.quote(out)}/{FILE}"


# --- ws run: the command line, and the result read back as a run record --------------------------------------------

EXE = "claude"
PLACEHOLDER_KEY = "ws-lease-placeholder"  # the runner holds this; the broker swaps in the real key on the way out
OPTIONS = {"exe", "allowed_tools", "env"}
PROMPT_VIA_STDIN = True  # `claude -p` reads the prompt from stdin: it stays out of argv and `ps`


def priced(job) -> bool:
    """Does the runner report what a run cost? Claude Code always does (at list price)."""
    return True


def build_command(job, run_dir: Path, lease) -> tuple[list[str], dict[str, str]]:
    """(argv, env) for `claude -p`. argv[0] is the executable name (or runner_options.exe); ws finds it on PATH.
    The gate settings in run_dir/gate are passed with --settings, and the lease (if any) becomes the base URL, so
    the key the runner holds is a placeholder. Raises ValueError naming the runner_options key that is wrong."""
    jobs.check_options(job, OPTIONS)
    exe = jobs.option(job, "exe", str, EXE)
    tools = jobs.string_list(job, "allowed_tools")
    argv = [exe, "-p", "--output-format", "json", "--model", job.model, "--settings", str(run_dir / "gate" / FILE)]
    if job.bare:
        argv.append("--bare")
    if job.max_turns is not None:
        argv += ["--max-turns", str(job.max_turns)]
    if tools:
        argv += ["--allowedTools", ",".join(tools)]
    env = jobs.extra_env(job)  # first: what ws sets below always wins
    env.update(DISABLE_AUTOUPDATER="1", CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
    if lease is not None:
        env.update(ANTHROPIC_BASE_URL=lease.base_url, ANTHROPIC_API_KEY=PLACEHOLDER_KEY)
    return argv, env


def _int(v) -> int | None:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _num(v) -> float | None:
    ok = isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0
    return float(v) if ok else None


def parse_result(stdout: str, stderr: str, exit_code: int | None) -> dict:
    """The part of a run record that `claude -p --output-format json` can fill. Never raises."""
    try:
        r = json.loads(stdout)
    except ValueError:
        r = None
    if not isinstance(r, dict) or r.get("type") != "result":
        tail = (stderr or stdout or "").strip().splitlines()
        return {"error": "claude printed no result" + (f": {tail[-1][:150]}" if tail else "")}
    usage = r.get("usage") if isinstance(r.get("usage"), dict) else {}
    models = []
    bases = set()
    for name, m in (r.get("modelUsage") if isinstance(r.get("modelUsage"), dict) else {}).items():
        if isinstance(m, dict):
            models.append(
                {
                    "model": name,
                    "input": _int(m.get("inputTokens")),
                    "output": _int(m.get("outputTokens")),
                    "cache_creation": _int(m.get("cacheCreationInputTokens")),
                    "cache_read": _int(m.get("cacheReadInputTokens")),
                    "cost_usd": _num(m.get("costUSD")),
                }
            )
            if isinstance(m.get("costBasis"), str):
                bases.add(m["costBasis"])
    cost = _num(r.get("total_cost_usd"))
    error = None
    if r.get("is_error"):
        errs = r.get("errors") if isinstance(r.get("errors"), list) else []
        error = str(r.get("result") or (errs[0] if errs else None) or r.get("subtype") or "claude reported an error")
    return {
        "session_id": r.get("session_id") if isinstance(r.get("session_id"), str) else None,
        "models_used": models,
        "tokens": {
            "input": _int(usage.get("input_tokens")),
            "output": _int(usage.get("output_tokens")),
            "cache_creation": _int(usage.get("cache_creation_input_tokens")),
            "cache_read": _int(usage.get("cache_read_input_tokens")),
        },
        "cost_usd": cost,
        "cost_basis": (bases.pop() if len(bases) == 1 else "mixed" if bases else None) if cost is not None else None,
        "duration_api_ms": _int(r.get("duration_api_ms")),
        "num_turns": _int(r.get("num_turns")),
        "error": error,
    }


def transcript_path(session_id: str | None, cwd: str, env: dict[str, str]) -> str | None:
    """Claude keeps `<config dir>/projects/<cwd with every non-alphanumeric as ->/<session id>.jsonl`. None when there
    is no such file (e.g. --no-session-persistence). The slug rule is checked against claude 2.1.308; a very long path
    is shortened by claude, so fall back to looking the session up in any project folder."""
    if not session_id or not re.fullmatch(r"[A-Za-z0-9-]+", session_id):
        return None
    config = Path(env.get("CLAUDE_CONFIG_DIR") or Path(env.get("HOME", "~")) / ".claude").expanduser()
    projects = config / "projects"
    direct = projects / re.sub(r"[^A-Za-z0-9]", "-", cwd) / f"{session_id}.jsonl"
    if direct.is_file():
        return str(direct)
    found = sorted(projects.glob(f"*/{session_id}.jsonl")) if projects.is_dir() else []
    return str(found[0]) if found else None
