"""`ws canary`: a few small checks that a runner's update did not break what ws relies on. No model is called, so it
costs nothing.

    ws canary [--runner R] [--json]     run the checks; one line goes to `logs/canary.jsonl` in the state folder; exit 1 if any fail

Per runner:
  version      `<runner> --version` runs
  gate         the generated gate still stops a denied command and lets a plain one through, by running the real
               `ws gate check` hook entry on that runner's own payload shape
  hook_config  the config ws generates for the runner parses and still holds the hook
  run_record   a run record still validates against `runschema` (stdlib check, so no extra dependency)
  wall         Claude Code only (OpenCode is skipped): the launch settings ws makes for a project still shut the sandbox
               (on, fail closed, no unsandboxed retry, no excluded commands, no Unix sockets), hide the home folder,
               re-allow the project, keep every exact deny_read path hidden, keep the sign-in tokens out of shell
               commands, and keep Claude's file tools in the working folders
  probe        Claude Code only: the two probes from Claude Code's sandboxing page, run by the real runner inside those
               launch settings with a local stub model (no paid call): `touch ~/sandbox-probe-...` must leave no file
               in the real home (judged on the host: with the home hidden, the wall gives it a throwaway layer), and
               `curl --noproxy '*' https://example.com` must not connect. The same curl runs on the host first: when the
               host can't connect either, the network half proves nothing and the check is a skip. Run once for the default launch settings and
               once per walled class whose launch file this machine has; that file must itself turn the sandbox on,
               fail closed and allow no unsandboxed retry. Skipped when claude, or the sandbox's own tools, are missing
  keys         Claude Code only: the installed runner is at least the first version of every settings key the wall
               writes on the default policy (wallsettings.MIN_CLAUDE, the table WS016 reads); an older one ignores it
  tokens       Claude Code only: the runner's credentials file is readable by its owner alone, this process carries no
               sign-in token (CLAUDE_CODE_OAUTH_TOKEN, ANTHROPIC_OAUTH_TOKEN), and no job file sets one

`ws doctor` records the last version it saw of each runner, and the default model id, in `versions.json` in the state folder.
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
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ws import __version__, adapters, gate, jobs, jsonl, output, paths, policy, runrecord, selfcmd
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path

SCHEMA_VERSION = 1
PASS, FAIL, SKIP = "pass", "fail", "skip"
NO_VERSION = "ran (it printed no version)"
# The tokens a shell command must never see, as data here so the check doesn't trust the generator it checks; the first
# two are the runner's sign-in, which no ws process or job may carry.
TOKEN_NAMES = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
SIGN_IN_TOKENS = TOKEN_NAMES[:2]
PROBE_TIMEOUT = 180
SKIP_KEYS = (SKIP, "")  # a placeholder that keeps the rows in order until the version is known
PROBE_URL = "https://example.com"  # the sandboxing page's own
CURL_PROBE = f"curl --noproxy '*' -sS -m 10 -o curl-probe.out {PROBE_URL} && echo curl=0 || echo curl=1"
DENIED = "git push --force origin main"  # in the default deny list; a plain `git push` is not
ALLOWED = "ls"

# A runner's own hook payload for a shell command: the shape its adapter's parse() reads.
PAYLOADS = {
    "claude-code": lambda cmd: {"tool_name": "Bash", "tool_input": {"command": cmd}},
    "opencode": lambda cmd: {"tool": "bash", "args": {"command": cmd}},
}


def _home(env) -> Path | None:
    """ws's state folder as `env` says it, or None if env names no layout and no HOME (a bare test environment): nothing
    is written then, so a check run with an empty environment never lands in the real home."""
    if not (env.get("HOME") or env.get("WS_HOME") or env.get(paths.ENV_DIRS["state"])):
        return None
    return paths.state_dir(env)


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
    False) remember what is seen now. The first sighting is not a change. Nothing is read or kept unless ws's state
    folder already exists: doctor does not make it."""
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


def log_path(home: Path) -> Path:
    """The canary log. It sits under `logs/`, which the gate denies a session: a canary line a session could write
    would let it fake a passing canary. The log used to be `canary.jsonl` at the top of the state folder, a place a session can write, so
    nothing in it is trusted: it is renamed to `canary.jsonl.old` (kept for a look) and the new log starts empty."""
    old = home / "canary.jsonl"
    if os.path.lexists(old):
        os.replace(old, home / "canary.jsonl.old")
    return home / "logs" / "canary.jsonl"


def _append_line(path: Path, line: dict) -> None:
    """One line through `ws.jsonl`: locked, 0600, rotated at 5 MB."""
    jsonl.append(path, line)


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
    pin = paths.pin_of(child)
    p = subprocess.run(  # the same isolated command the generated hook uses, from a folder that has no ws/ of its own
        [*selfcmd.argv(), "gate", "check", "--runner", runner, "--policy", str(policy_file), *pin.args()],
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
        files = adapter.generate(policy.loads(policy.default_toml()), selfcmd.argv(), work / "gate", policy_file)
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


def _wall_problem(block: dict, home: str, project: str, exact: list[str]) -> str | None:
    """The first way the launch settings' `sandbox` block is open, or None."""
    net = block.get("network") if isinstance(block.get("network"), dict) else {}
    fs = block.get("filesystem") if isinstance(block.get("filesystem"), dict) else {}
    deny, allow = fs.get("denyRead", []), fs.get("allowRead", [])
    if net.get("allowAllUnixSockets") or net.get("allowUnixSockets"):
        return "the network settings allow Unix sockets"
    if block.get("excludedCommands"):
        return "the sandbox excludes commands"
    if block.get("enabled") is not True:
        return "the sandbox is not enabled"
    if block.get("failIfUnavailable") is not True:
        return "the sandbox does not fail closed"
    if block.get("allowUnsandboxedCommands") is not False:
        return "an unsandboxed retry is allowed"
    if home not in deny:
        return "the home folder is not hidden"
    if project not in allow:
        return "the project is not re-allowed"
    missing = [p for p in exact if p not in deny]
    if missing:
        return f"{len(missing)} deny_read path(s) are no longer hidden"
    creds = block.get("credentials") if isinstance(block.get("credentials"), dict) else {}
    entries = creds.get("envVars") if isinstance(creds.get("envVars"), list) else []
    denied = {e.get("name") for e in entries if isinstance(e, dict) and e.get("mode") == "deny"}
    loose = [n for n in TOKEN_NAMES if n not in denied]
    if loose:
        return f"shell commands can see {loose[0]}"
    return None


def _perm_problem(perms: dict) -> str | None:
    """The first file-tool block the launch settings' `permissions` lack, or None."""
    from ws import wallsettings
    from ws.adapters import claude_code

    for key in (wallsettings.READ_BLOCK_KEY, claude_code.WRITE_BLOCK_KEY):
        if perms.get(key) is not True:
            return f"permissions.{key} is not on"
    return None


def check_wall(adapter, env, work: Path) -> tuple[str, str]:
    """Claude Code's launch settings, made as `ws gate launch` makes them (the generated settings through `for_project`
    for a project folder in `work`), still hold the wall. ws's own generator is judged here, not the person's policy, so
    it passes on the default policy. A launch copy that cannot be made is a FAIL that names the kind of error."""
    if adapter.NAME != "claude-code":
        return SKIP, f"{adapter.NAME} has no settings file to read the wall from"
    pol = policy.loads(policy.default_toml())
    project = work / "project"
    project.mkdir(exist_ok=True)
    home = os.path.realpath(env.get("HOME") or str(Path.home()))
    try:
        files = adapter.generate(pol, selfcmd.argv(), work / "gate", work / "policy.toml")
        allow, deny = policy.launch_read_rules(pol)
        text = adapter.for_project(
            files["settings.json"], str(project.resolve()), allow_read=allow, deny_read=deny, env=env
        )
        conf = json.loads(text)
        block = conf["sandbox"]
    except (WsError, policy.PolicyError, ValueError, KeyError, TypeError, OSError) as e:
        return FAIL, f"the launch settings could not be made ({getattr(e, 'kind', type(e).__name__)})"
    problem = _wall_problem(block, home, str(project.resolve()), policy.sandbox_read_paths(pol))
    perms = conf.get("permissions") if isinstance(conf.get("permissions"), dict) else {}
    problem = problem or _perm_problem(perms)
    return (FAIL, problem) if problem else (PASS, "the home is hidden, the project re-allowed, the sandbox shut")


def _launch_text(adapter, env, work: Path, project: Path, pol, cls=None, launch_doc=None) -> str:
    """The launch settings `ws gate launch` would start claude with in `project`, under `pol` (and a walled class)."""
    from ws import sandbox

    files = adapter.generate(pol, selfcmd.argv(), work / "gate", work / "policy.toml")
    return sandbox.claude_launch_text(
        pol, cls, str(project), dict(env), None, files[adapter.FILE], cwd=str(project), launch_doc=launch_doc
    )


def shut_problem(doc: dict) -> str | None:
    """Why a class's launch file doesn't itself shut the sandbox, or None. Read from the file, not the merged settings:
    a key taken out of the class's file is the fault this has to see, and the merge would fill it from the default."""
    box = doc.get("sandbox") if isinstance(doc.get("sandbox"), dict) else {}
    if box.get("enabled") is not True:
        return "its launch file doesn't turn the sandbox on"
    if box.get("failIfUnavailable") is not True:
        return "its launch file doesn't set sandbox.failIfUnavailable"
    if box.get("allowUnsandboxedCommands") is not False:
        return "its launch file allows an unsandboxed retry"
    return None


def probe_settings(text: str) -> str:
    """The launch settings with the hooks and the deny rules taken out, so that it is the sandbox alone that stops a
    probe (a probe the gate refused would pass without testing anything)."""
    conf = json.loads(text)
    conf.pop("hooks", None)
    if isinstance(conf.get("permissions"), dict):
        conf["permissions"].pop("deny", None)
    return json.dumps(conf, indent=2) + "\n"


def _said(said: str, word: str) -> str | None:
    """The value a probe line printed (`touch=1` gives "1"), matched as a whole line, or None."""
    found = re.search(rf"^{word}=([01])$", said, re.MULTILINE)
    return found.group(1) if found else None


def judge_probe(said: str, control: str, wrote: bool, net: bool = True) -> str | None:
    """What the probe's tool results show: None when both probes were stopped and the control ran. `net` False: the
    host itself can't reach the probe's host, so the network half is not judged."""
    if not re.search(rf"^{re.escape(control)}$", said, re.MULTILINE):
        return "the runner ran no command, so the probe could not tell (is the sandbox available here?)"
    if wrote:  # judged on the host: under deny-home the home is a throwaway layer, so `touch` can exit 0 in the wall
        return "a shell command wrote outside the working folder"
    if _said(said, "touch") is None:
        return "the write probe did not run"
    if not net:
        return None
    curl = _said(said, "curl")
    if curl == "0":
        return "a shell command reached the network around the sandbox proxy"
    if curl is None:
        return "the network probe did not run"
    return None


def host_reaches(work: Path, env) -> bool:
    """The network probe's control: the same curl, run on the host. False when the host can't connect either, or has
    no curl."""
    exe = shutil.which("curl", path=env.get("PATH"))
    if not exe:
        return False
    argv = [exe, "--noproxy", "*", "-sS", "-m", "10", "-o", str(work / "host-probe.out"), PROBE_URL]
    try:
        done = subprocess.run(argv, capture_output=True, timeout=30, check=False, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return done.returncode == 0


class NotProbed(Exception):
    """The runner never asked the model for a turn, so no probe ran (a runner that isn't Claude Code's real CLI)."""


def run_probe(exe: str, settings: str, project: Path, work: Path, env, net: bool = True) -> str | None:
    """Run the docs' two probes in the real runner inside `settings`, with a stub model scripting the calls. None when
    both were stopped; otherwise what got through, or why the probe could not tell."""
    from ws.testing.mockprovider import AnthropicStub, Call

    tag = os.urandom(6).hex()  # built at run time, so no earlier run's file can pass for this one
    target = Path(env.get("HOME") or Path.home()) / f"sandbox-probe-{tag}"
    control = f"probe-control-{tag}"
    calls = [
        Call("Bash", {"command": f"echo {control}"}),
        # no shell variable in a probe: outside the sandbox the read block asks before a command it can't analyse,
        # and `-p` then refuses it, so a `$?` would keep the control (no sandbox) from running at all
        Call("Bash", {"command": f"touch {target} && echo touch=0 || echo touch=1"}),
        Call("Bash", {"command": CURL_PROBE}),
    ]
    sfile = work / "probe-settings.json"
    sfile.write_text(probe_settings(settings))
    child = {k: v for k, v in env.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_"))}
    child.update(
        ANTHROPIC_API_KEY="sk-ant-" + "probe" * 8,  # a placeholder built at run time: the stub takes any key
        CLAUDE_CONFIG_DIR=str(work / "claude-config"),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        DISABLE_AUTOUPDATER="1",
    )
    argv = [exe, "-p", "run the probe", "--settings", str(sfile), "--setting-sources", "user"]
    argv += ["--allowedTools", "Bash,Bash(curl *)", "--model", "claude-sonnet-4-5", "--no-session-persistence"]
    results: list[str] = []
    asked = False
    try:
        with AnthropicStub(calls) as stub:
            child["ANTHROPIC_BASE_URL"] = stub.url
            subprocess.run(
                argv, env=child, cwd=project, capture_output=True, text=True, timeout=PROBE_TIMEOUT,
                stdin=subprocess.DEVNULL, check=False,
            )  # fmt: skip
            results = stub.tool_results()
            asked = bool(stub.requests)
    except (OSError, subprocess.TimeoutExpired) as e:
        return f"the runner did not finish the probe ({type(e).__name__})"
    finally:
        wrote = target.exists()
        if wrote:
            target.unlink()
    if not asked and not wrote:
        raise NotProbed
    return judge_probe("\n".join(results), control, wrote, net)


def _classes() -> list:
    """The walled classes of the person's policy (those with an OS user); none when there is no policy to read."""
    from ws import wallsettings

    try:
        return wallsettings.class_users(policy.load())
    except (policy.PolicyError, OSError):
        return []


def check_probe(adapter, env, work: Path) -> tuple[str, str]:
    """The sandbox, tried for real: see the module docstring. The default launch settings, then each walled class."""
    from ws import osinfo, sandbox, wallsettings

    if adapter.NAME != "claude-code":
        return SKIP, f"{adapter.NAME} has no sandbox of its own to probe"
    exe = shutil.which(adapter.EXE, path=env.get("PATH"))
    if not exe:
        return SKIP, f"{adapter.EXE} is not on PATH"
    why = wallsettings.host_problem(osinfo.detect().kind)
    if why:
        return SKIP, f"the sandbox can't run here: {why}"

    project = work / "probe-project"
    project.mkdir(exist_ok=True)
    project = project.resolve()
    pol = policy.loads(policy.default_toml())
    net = host_reaches(work, env)
    cases: list[tuple[str, object, dict | None]] = [("default", None, None)]
    for cls in _classes():
        try:
            doc = sandbox.class_launch_doc(cls)
        except WsError as e:
            return FAIL, f"class {cls.name}: its launch file can't be read ({e.kind})"
        if doc is None:
            continue
        problem = shut_problem(doc)
        if problem:
            return FAIL, f"class {cls.name}: {problem}"
        cases.append((cls.name, cls, doc))
    for name, cls, doc in cases:
        try:
            text = _launch_text(adapter, env, work, project, pol, cls, doc)
        except (WsError, policy.PolicyError, ValueError, KeyError, TypeError, OSError) as e:
            return FAIL, f"{name}: the launch settings could not be made ({getattr(e, 'kind', type(e).__name__)})"
        try:
            problem = run_probe(exe, text, project, work, env, net)
        except NotProbed:
            return SKIP, f"{name}: {adapter.EXE} never asked the model for a turn, so nothing was probed"
        if problem:
            return FAIL, f"{name}: {problem}"
    names = ", ".join(n for n, _, _ in cases)
    if not net:
        return (
            SKIP,
            f"the write probe was stopped ({names}); the host can't reach {PROBE_URL}, so the network probe proves nothing",
        )
    return PASS, f"write and network probes stopped ({names})"


def _credentials_file(env) -> Path:
    base = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME") or str(Path.home()), ".claude")
    return Path(base) / ".credentials.json"


def _keys(value) -> list[str]:
    """Every key in a parsed TOML document, at any depth."""
    if isinstance(value, dict):
        return [k for key, v in value.items() for k in (key, *_keys(v))]
    if isinstance(value, list):
        return [k for v in value for k in _keys(v)]
    return []


def _job_token(text: str) -> str | None:
    """The first sign-in token a job file sets as a key, at any depth, bare or quoted. A file that doesn't parse is
    read as text: a `NAME =` key, bare or in quotes, anywhere in it."""
    import tomllib

    try:
        keys = set(_keys(tomllib.loads(text)))
    except tomllib.TOMLDecodeError:
        keys = None
    for name in SIGN_IN_TOKENS:
        if keys is not None and name in keys:
            return name
        if keys is None and re.search(rf"(^|[{{,\s])[\"']?{name}[\"']?\s*=", text, re.MULTILINE):
            return name
    return None


def check_keys(adapter, version: str | None) -> tuple[str, str]:
    """The runner reads every key the wall writes on the default policy: see the module docstring."""
    from ws import doctor, wallsettings

    if adapter.NAME != "claude-code":
        return SKIP, f"{adapter.NAME} has no table of first versions"
    found = doctor._version_tuple(version or "")
    if found is None:
        return FAIL, "the runner's version can't be read, so the keys it reads aren't known"
    keys = wallsettings.keys_written(policy.loads(policy.default_toml()))
    old = [k for k in keys if found < wallsettings.MIN_CLAUDE[k]]
    if old:
        need = ".".join(map(str, max(wallsettings.MIN_CLAUDE[k] for k in old)))
        return FAIL, f"{version} ignores {', '.join(old)}: update Claude Code to {need} or newer"
    return PASS, f"{len(keys)} wall keys, all read by {version}"


def check_tokens(adapter, env) -> tuple[str, str]:
    """No sign-in token where another program could take it: see the module docstring."""
    if adapter.NAME != "claude-code":
        return SKIP, f"{adapter.NAME} keeps no Claude Code sign-in"
    cred = _credentials_file(env)
    try:
        mode: int | None = cred.stat().st_mode
    except FileNotFoundError:
        mode = None
    except OSError as e:
        return FAIL, f"the credentials file can't be checked ({type(e).__name__})"
    if mode is not None and mode & 0o077:
        return FAIL, f"{cred} is readable by other users (mode {mode & 0o777:o}): chmod 600 {cred}"
    carried = [n for n in SIGN_IN_TOKENS if env.get(n)]
    if carried:
        return FAIL, f"this process carries {carried[0]}: take it out of the environment ws runs in"
    home = _home(env)
    folder = jobs.jobs_dir(dict(env)) if home is not None else None
    try:
        found = sorted(p for p in folder.glob("*.toml") if p.is_file()) if folder else []
    except OSError:
        found = []
    for path in found:
        try:
            hit = _job_token(path.read_text())
        except (OSError, UnicodeDecodeError):
            continue
        if hit:
            return FAIL, f"job {path.stem} sets {hit}: take it out of {path}"
    private = "" if mode is None else ", the credentials file is private"
    return PASS, f"no sign-in token in the environment or a job file{private}"


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
        "wall": check_wall(adapter, env, work),
        "probe": check_probe(adapter, env, work),
        "keys": SKIP_KEYS,
        "tokens": check_tokens(adapter, env),
    }
    checks["keys"] = check_keys(adapter, checks["version"][1]) if checks["version"][0] == PASS else (SKIP, "no version")
    return {
        "version": checks["version"][1]
        if checks["version"][0] == PASS and checks["version"][1] != NO_VERSION
        else None,
        "ok": all(s != FAIL for s, _ in checks.values()),
        "checks": {k: s for k, (s, _) in checks.items()},
        "details": {k: d for k, (s, d) in checks.items()},
    }


def run_canary(runners: list[str], env=None, model: str | None = None) -> dict:
    """Run the checks for each runner and append one line to logs/canary.jsonl (when ws's state folder exists). A runner that passes
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
            _append_line(log_path(home), line)
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
        raise WsError("not_found", "no runner to check", "name one with --runner, or put claude or opencode on PATH")
    roles = _profile(env).get("roles")
    cfg = roles.get("model") if isinstance(roles, dict) else None
    model = cfg.get("default") if isinstance(cfg, dict) and isinstance(cfg.get("default"), str) else None
    result = run_canary(runners, env, model)
    if args.json:
        output.result(result, status=output.OK if result["ok"] else output.ATTENTION)
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
        description="Small checks, no paid model. Each run adds one line to logs/canary.jsonl in the state folder. Exit 1 on a failure.",
    )
    c.add_argument("--runner", choices=list(adapters.all_adapters()), help="default: the profile's runner")
    c.set_defaults(func=cmd_canary)
