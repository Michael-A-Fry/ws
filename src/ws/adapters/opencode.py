"""OpenCode: an `opencode.json` `permission` block (the runner's own rules, the primary layer) plus a shim plugin that
runs `ws gate check` on every governed tool call and throws on a deny (the second layer; it also fails closed: any
error in the shim blocks the call).

Also denied: edits of the policy file and of the generated folder, so a session cannot loosen its own gate.

Point OpenCode at the output so its global config is never touched, and so a project's own `opencode.json` cannot
loosen the gate: the inline config (OPENCODE_CONFIG_CONTENT) is read after the global, OPENCODE_CONFIG and project
files, so it wins.

    OPENCODE_CONFIG_CONTENT="$(cat OUT/opencode.json)"  OPENCODE_CONFIG_DIR=OUT  opencode

OPENCODE_CONFIG_DIR is searched for `plugins/`, which is where the shim sits.

The wall is `ws gate launch --runner opencode -- opencode ...` (ws.sandbox: bubblewrap on Linux, sandbox-exec on macOS),
because OpenCode has no sandbox of its own. The launch hint says so, and launch puts WS_GATED_SESSION in the
environment so `ws approve` knows it is being run from a gated session. The whole generated folder is read-only in the
wall; see IGNORE below for what that needs.
"""

from __future__ import annotations

import json
import math
import shlex
from pathlib import Path

from ws import jobs
from ws.adapters import Call, check_argv, protected_paths
from ws.adapters.opencode_plugin import SOURCE
from ws.policy import Policy, native_patterns

NAME = "opencode"
CONFIG = "opencode.json"
PLUGIN = "plugins/ws-gate.js"
_TOOLS = {"web_fetch": "webfetch", "web_search": "websearch"}
# For ws.sandbox: the env var that names the generated folder. The whole folder is read-only in the wall (OpenCode loads
# plugin/, tool/, agent/, command/ and opencode.jsonc from it, so a planted file there would outlive the session).
CONFIG_DIR_ENV = "OPENCODE_CONFIG_DIR"


# OpenCode writes this file into its config folder at start and will not run if it cannot (1.19.4). Written here at
# generate time, with the content OpenCode writes, so the folder can stay read-only; it needs no node_modules.
def wall_writable(env: dict) -> list[str]:
    """The folders OpenCode itself writes (its database and logs, key-value state, model and package cache), which the
    wall leaves writable besides the project: the `opencode` folder under XDG data, state and cache."""
    home = env.get("HOME") or str(Path.home())
    return [
        f"{env.get(var) or f'{home}/{default}'}/opencode"
        for var, default in (
            ("XDG_DATA_HOME", ".local/share"),
            ("XDG_STATE_HOME", ".local/state"),
            ("XDG_CACHE_HOME", ".cache"),
        )
    ]


def wall_prepare(env: dict) -> None:
    """What OpenCode makes at start in its global config folder, and fails (EROFS) if it cannot: the folder and its
    `.gitignore` (it keeps one in every config folder). Done by the launcher, outside the wall, where the folder is
    writable; inside it stays read-only, so a session cannot plant a global plugin."""
    home = env.get("HOME") or str(Path.home())
    folder = Path(f"{env.get('XDG_CONFIG_HOME') or f'{home}/.config'}/opencode")
    folder.mkdir(parents=True, exist_ok=True)
    ignore = folder / IGNORE
    if not ignore.exists():
        ignore.write_text(IGNORE_TEXT, encoding="utf-8")


IGNORE = ".gitignore"
IGNORE_TEXT = "node_modules\npackage.json\npackage-lock.json\nbun.lock\n.gitignore"
_NEUTRAL = {v: k for k, v in _TOOLS.items()}

# Where this runner keeps what ws installs globally, and where a repo may shadow it (read by ws.globals).
# Both the older singular and the newer plural folder names are listed. kinds: see adapters/claude_code.py.
GLOBALS = {
    "user_dir": "~/.config/opencode",
    "xdg": "opencode",
    "project_dir": ".opencode",
    "kinds": {
        "skills": "dir",
        "skill": "dir",
        "agents": "name",
        "agent": "name",
        "commands": "name",
        "command": "name",
        "plugins": "stem",
        "plugin": "stem",
    },
}


def _read_forms(rule: str) -> list[str]:
    """OpenCode matches read and edit rules against the path as it sees it (relative to the project, so a file in
    another folder arrives as `../../x/y`), and an absolute rule never matches that (tried on 1.19.4: `~/.ssh/**`
    and `/abs/.ssh/**` let a read through; `**/.ssh/**` blocks it). So each rule is also given as `**/` plus the
    path below home, or its last 3 segments if it is absolute. That over-blocks a same-named folder elsewhere,
    which is the safe side."""
    parts = rule.removeprefix("~/").lstrip("/").split("/")
    if rule.startswith("/"):
        parts = parts[-3:]
    return list(dict.fromkeys([rule, "**/" + "/".join(parts)]))


def _protect_forms(path: str, is_out: bool) -> list[str]:
    """Edit rules for a protected file or folder, in the same two spellings (see _read_forms). A folder name like
    `jobs` or `logs` is common, so a pattern never drops the parent folder: a ws control path keeps one
    (`.ws/logs/**`), the generated folder two (`.ws/gate/opencode/**`), whatever else is above it."""
    parts = Path(path).parts
    tail = "/".join(parts[-3:] if is_out else parts[-2:])
    return [path, f"{path}/**", f"**/{tail}", f"**/{tail}/**"]


def generate(policy: Policy, ws_cmd: list[str], out: Path, policy_file: Path) -> dict[str, str]:
    # Last matching rule wins, so the catch-all comes first.
    bash: dict[str, str] = {"*": "allow"}
    bash.update(dict.fromkeys(native_patterns(policy), "deny"))
    permission: dict = {"bash": bash}
    if policy.gate.deny_read:
        rules = {"*": "allow", **dict.fromkeys((f for r in policy.gate.deny_read for f in _read_forms(r)), "deny")}
        permission.update(read=rules, grep=dict(rules), glob=dict(rules), list=dict(rules))
    edit = {"*": "allow"}  # covers write and patch too; a path may be a file or a folder: cover both
    for p in protected_paths(out, policy_file):
        edit.update(dict.fromkeys(_protect_forms(p, p == str(out)), "deny"))
    permission["edit"] = edit
    for t in policy.gate.deny_tools:
        permission[_TOOLS[t]] = "deny"
    config = {"$schema": "https://opencode.ai/config.json", "permission": permission}
    plugin = SOURCE.replace("/*__CHECK__*/ []", json.dumps(check_argv(ws_cmd, NAME, policy_file, out)))
    plugin = plugin.replace("/*__WS__*/ []", json.dumps(ws_cmd))
    return {CONFIG: json.dumps(config, indent=2) + "\n", PLUGIN: plugin, IGNORE: IGNORE_TEXT}


def parse(payload: dict) -> Call | None:
    """What the shim sends: {"tool": "bash", "args": {"command": ...}}."""
    tool, args = payload.get("tool"), payload.get("args")
    if not isinstance(tool, str) or not isinstance(args, dict):
        raise TypeError("payload has no tool and args")
    if tool == "bash":
        return Call("command", _text(args, "command"))
    if tool == "read":
        return Call("read", _text(args, "filePath"))
    if tool in ("edit", "write", "multiedit"):
        return Call("write", _text(args, "filePath"))
    if tool in ("grep", "glob", "list"):
        path = args.get("path")
        if path is not None and not isinstance(path, str):
            raise TypeError("args.path must be text")
        return Call("search", path or ".")
    if tool in _NEUTRAL:
        return Call("tool", _NEUTRAL[tool])
    return None


def _text(d: dict, key: str) -> str:
    v = d.get(key)
    if not isinstance(v, str):
        raise TypeError(f"args.{key} is missing")
    return v


def launch_hint(out: str) -> str:
    q = shlex.quote(out)
    return (
        f'start OpenCode with: OPENCODE_CONFIG_CONTENT="$(cat {q}/{CONFIG})" OPENCODE_CONFIG_DIR={q} '
        "ws gate launch --runner opencode -- opencode"
    )


# --- ws run: the command line, and the event stream read back as a run record --------------------------------------

EXE = "opencode"
PLACEHOLDER_KEY = "ws-lease-placeholder"  # the runner holds this; the broker swaps in the real key on the way out
OPTIONS = {"exe", "env", "provider", "base_path", "offline"}
PROMPT_VIA_STDIN = True  # `opencode run` reads the message from stdin when none is given (checked on 1.19.4)


def priced(job) -> bool:
    """Can OpenCode price this job's model? Its built-in providers come with prices; a provider block in the job
    does only if every model in it carries a `cost`. Otherwise OpenCode reports 0, which is not free."""
    block = job.runner_options.get("provider")
    if not isinstance(block, dict) or not block:
        return True
    models = block.get("models")
    return (
        isinstance(models, dict) and bool(models) and all(isinstance(m, dict) and "cost" in m for m in models.values())
    )


def build_command(job, run_dir: Path, lease) -> tuple[list[str], dict[str, str]]:
    """(argv, env) for `opencode run --format json`. The generated gate config in run_dir/gate is passed as the inline
    config (OPENCODE_CONFIG_CONTENT, which beats a project's own opencode.json) with OPENCODE_CONFIG_DIR as the plugin
    folder. The lease (if any) becomes the provider's baseURL plus runner_options.base_path (default /v1), with a
    placeholder key, added to that inline config. runner_options.provider is merged into the provider block, for a
    provider OpenCode does not know (npm, name, models). Raises ValueError naming the key that is wrong."""
    jobs.check_options(job, OPTIONS)
    exe = jobs.option(job, "exe", str, EXE)
    provider_id, slash, _ = job.model.partition("/")
    if not slash or not provider_id:
        raise ValueError("model must be provider/model for opencode, e.g. anthropic/claude-haiku-4-5")
    if job.max_turns is not None:
        raise ValueError("max_turns: opencode run has no turn limit; remove it or use claude-code")
    extra = jobs.option(job, "provider", dict, {})
    base_path = jobs.option(job, "base_path", str, "/v1")
    offline = jobs.option(job, "offline", bool, False)
    out = Path(run_dir) / "gate"
    argv = [exe, "run", "--model", job.model, "--format", "json"]
    env = jobs.extra_env(job)  # first: what ws sets below always wins
    env.update(OPENCODE_CONFIG_DIR=str(out), OPENCODE_DISABLE_AUTOUPDATE="true")
    if offline:  # no model-list fetch or language-server download: for a stub or an air-gapped machine
        env.update(OPENCODE_DISABLE_MODELS_FETCH="true", OPENCODE_DISABLE_LSP_DOWNLOAD="true")
    gate_file = out / CONFIG
    config = json.loads(gate_file.read_text(encoding="utf-8")) if gate_file.is_file() else {}
    if lease is not None or extra:
        block = json.loads(json.dumps(extra))  # a copy: the job's own table is not touched
        options = block.setdefault("options", {})
        if not isinstance(options, dict):
            raise ValueError("runner_options.provider.options must be a table")
        if lease is not None:
            options.update(baseURL=lease.base_url + base_path, apiKey=PLACEHOLDER_KEY)
        config["provider"] = {**(config.get("provider") or {}), provider_id: block}
    env["OPENCODE_CONFIG_CONTENT"] = json.dumps(config)
    return argv, env


def parse_result(stdout: str, stderr: str, exit_code: int | None) -> dict:
    """The part of a run record that the `--format json` event stream can fill. Never raises. The stream does not
    name the model, so `models_used` has one entry with model None for the caller to fill with the requested model."""
    session = error = None
    steps = 0
    sums = {"input": None, "output": None, "cache_creation": None, "cache_read": None}
    cost = None

    def add(key: str, v) -> None:
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            sums[key] = (sums[key] or 0) + v

    for line in stdout.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue
        if session is None and isinstance(ev.get("sessionID"), str):
            session = ev["sessionID"]
        if ev.get("type") == "error":
            e = ev.get("error") if isinstance(ev.get("error"), dict) else {}
            data = e.get("data") if isinstance(e.get("data"), dict) else {}
            error = str(data.get("message") or e.get("name") or "opencode reported an error")
        part = ev.get("part") if isinstance(ev.get("part"), dict) else {}
        if ev.get("type") == "step_finish":
            steps += 1
            tok = part.get("tokens") if isinstance(part.get("tokens"), dict) else {}
            cache = tok.get("cache") if isinstance(tok.get("cache"), dict) else {}
            add("input", tok.get("input"))
            add("output", tok.get("output"))
            add("cache_creation", cache.get("write"))
            add("cache_read", cache.get("read"))
            c = part.get("cost")
            if isinstance(c, (int, float)) and not isinstance(c, bool) and math.isfinite(c) and c >= 0:
                cost = (cost or 0.0) + c
    counted = any(v is not None for v in sums.values())
    # OpenCode reports cost 0 for a model it has no price for, so a zero beside real tokens is "unknown", not free.
    unpriced = counted and not cost
    if not steps:
        cost = None
    return {
        "session_id": session,
        "models_used": [{"model": None, **sums, "cost_usd": None if unpriced else cost}] if steps else [],
        "tokens": sums,
        "cost_usd": None if unpriced or not steps else cost,
        "cost_basis": None if not steps else "unpriced" if unpriced else "runner",
        "num_turns": steps if steps else None,
        "error": error,
    }


def transcript_path(session_id: str | None, cwd: str, env: dict[str, str]) -> str | None:
    """OpenCode 1.19 keeps every session in one SQLite file, `<XDG_DATA_HOME or ~/.local/share>/opencode/opencode.db`;
    the session id says which rows are this run's. None when the file is not there."""
    if not session_id:
        return None
    data = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")) / ".local" / "share")
    db = Path(data).expanduser() / "opencode" / "opencode.db"
    return str(db) if db.is_file() else None
