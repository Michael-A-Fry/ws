"""Job files: `$WS_HOME/jobs/<name>.toml`, one headless job each, run with `ws run <name>`.

    runner = "claude-code"              adapter name (see `ws gate generate --help`)
    model = "claude-haiku-4-5"          the smallest model that passes
    prompt = "..."                      or prompt_file = "daily.md" (relative to the jobs folder)
    cwd = "~/work/thing"                default: an empty folder made for the run
    timeout_s = 900
    max_turns = 12                      optional
    bare = true                         headless default: no hooks, plugins or auto-discovered context
    allow_unpriced = false              run even if the runner cannot price the model (counted at your fallback)
    sources = ["notes"]                 policy sources the job reads (the Rule of Two looks at these)

    [[secrets]]                         the provider credential, leased through the broker
    name = "anthropic"
    host = "api.anthropic.com"
    purpose = "run"

    [runner_options]                    adapter-specific (claude-code: allowed_tools, env; opencode: provider, env)

The job files and their folder must belong to you and not be writable by anyone else: a job decides what a runner is
started with, so `ws run` refuses one that someone else could have changed. `prompt_file` must be inside the folder.

Every error names the key.
"""

from __future__ import annotations

import os
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from ws import adapters
from ws.profile import ws_home
from ws.secrets import HOST_RE, NAME_RE

NAME = re.compile(r"^[A-Za-z0-9_-]+$")
DEFAULT_TIMEOUT_S = 900
KEYS = {
    "runner",
    "model",
    "prompt",
    "prompt_file",
    "cwd",
    "timeout_s",
    "max_turns",
    "bare",
    "allow_unpriced",
    "sources",
    "secrets",
    "runner_options",
}


class JobError(Exception):
    pass


@dataclass(frozen=True)
class SecretUse:
    name: str
    host: str
    purpose: str = "run"


@dataclass(frozen=True)
class Job:
    name: str
    runner: str
    model: str
    prompt: str
    cwd: str | None = None
    timeout_s: float = DEFAULT_TIMEOUT_S
    max_turns: int | None = None
    bare: bool = True
    allow_unpriced: bool = False
    sources: tuple[str, ...] = ()
    secrets: tuple[SecretUse, ...] = ()
    runner_options: dict = field(default_factory=dict)


def jobs_dir(env: dict | None = None) -> Path:
    return ws_home(env) / "jobs"


def _string(d: dict, key: str, where: str, required: bool = False) -> str | None:
    v = d.get(key)
    if v is None:
        if required:
            raise JobError(f"{where}: {key} is required")
        return None
    if not isinstance(v, str) or not v.strip():
        raise JobError(f"{where}: {key} must be a non-empty string")
    return v


def _number(d: dict, key: str, where: str, default, integer: bool = False):
    v = d.get(key, default)
    if v is None:
        return None
    ok = isinstance(v, int) if integer else isinstance(v, (int, float))
    if isinstance(v, bool) or not ok or not v > 0:
        raise JobError(f"{where}: {key} must be a {'whole ' if integer else ''}number above 0")
    return v


def loads(name: str, text: str, base: Path, where: str | None = None) -> Job:
    """Parse and check a job file's text. `base` is the folder `prompt_file` is relative to."""
    where = where or f"job {name}"
    if not NAME.match(name):
        raise JobError(f"{where}: a job name is letters, digits, - and _")
    try:
        d = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise JobError(f"{where}: {e}") from e
    unknown = sorted(set(d) - KEYS)
    if unknown:
        raise JobError(f"{where}: unknown key {unknown[0]!r} (known: {', '.join(sorted(KEYS))})")
    runner = _string(d, "runner", where, required=True)
    known = adapters.all_adapters()
    if runner not in known:
        raise JobError(f"{where}: runner {runner!r} is not known (known: {', '.join(known)})")
    model = _string(d, "model", where, required=True)
    prompt, prompt_file = _string(d, "prompt", where), _string(d, "prompt_file", where)
    if (prompt is None) == (prompt_file is None):
        raise JobError(f"{where}: give exactly one of prompt and prompt_file")
    if prompt_file is not None:
        f = (base / prompt_file).resolve()
        if not f.is_relative_to(base.resolve()):
            raise JobError(f"{where}: prompt_file {prompt_file!r} must be inside {base}")
        try:
            check_private(f)
            prompt = f.read_text(encoding="utf-8")
        except JobError as e:
            raise JobError(f"{where}: prompt_file: {e}") from e
        except (OSError, UnicodeDecodeError) as e:
            raise JobError(f"{where}: prompt_file {str(f)!r} cannot be read ({type(e).__name__})") from e
        if not prompt.strip():
            raise JobError(f"{where}: prompt_file {str(f)!r} is empty")
    bare = d.get("bare", True)
    if not isinstance(bare, bool):
        raise JobError(f"{where}: bare must be true or false")
    allow_unpriced = d.get("allow_unpriced", False)
    if not isinstance(allow_unpriced, bool):
        raise JobError(f"{where}: allow_unpriced must be true or false")
    sources = d.get("sources", [])
    if not isinstance(sources, list) or not all(isinstance(s, str) and s for s in sources):
        raise JobError(f"{where}: sources must be a list of source names")
    secrets_raw = d.get("secrets", [])
    if not isinstance(secrets_raw, list) or not all(isinstance(s, dict) for s in secrets_raw):
        raise JobError(f"{where}: secrets must be [[secrets]] tables")
    secrets = []
    for i, s in enumerate(secrets_raw, 1):
        w = f"{where}: secrets #{i}"
        extra = sorted(set(s) - {"name", "host", "purpose"})
        if extra:
            raise JobError(f"{w}: unknown key {extra[0]!r}")
        sname, host = _string(s, "name", w, required=True), _string(s, "host", w, required=True)
        if not NAME_RE.match(sname):
            raise JobError(f"{w}: name {sname!r} is not a valid secret name")
        if not HOST_RE.match(host.lower()):
            raise JobError(f"{w}: host {host!r} is not a host name (no scheme, no path)")
        secrets.append(SecretUse(sname, host.lower(), _string(s, "purpose", w) or "run"))
    options = d.get("runner_options", {})
    if not isinstance(options, dict):
        raise JobError(f"{where}: runner_options must be a table")
    return Job(
        name=name,
        runner=runner,
        model=model,
        prompt=prompt,
        cwd=_string(d, "cwd", where),
        timeout_s=_number(d, "timeout_s", where, DEFAULT_TIMEOUT_S),
        max_turns=_number(d, "max_turns", where, None, integer=True),
        bare=bare,
        allow_unpriced=allow_unpriced,
        sources=tuple(sources),
        secrets=tuple(secrets),
        runner_options=options,
    )


def check_private(path: Path) -> None:
    """Raises JobError unless `path` belongs to this user and nobody else can write to it."""
    try:
        st = path.stat()
    except OSError as e:
        raise JobError(f"{path}: {e.strerror or type(e).__name__}") from e
    if st.st_uid != os.geteuid():
        raise JobError(f"{path} belongs to another user; ws will not run what someone else could have written")
    if st.st_mode & 0o022:
        raise JobError(f"{path} can be written by others (mode {st.st_mode & 0o777:04o}); run `chmod go-w {path}`")


def load(name: str, env: dict | None = None) -> Job:
    """The job `name` from the jobs folder. The folder and the file must be yours and closed to other writers."""
    folder = jobs_dir(env)
    path = folder / f"{name}.toml"
    if not NAME.match(name):
        raise JobError(f"job {name}: a job name is letters, digits, - and _")
    if not path.is_file():
        raise JobError(f"no job {name!r}: expected {path}")
    check_private(folder)
    check_private(path)
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        raise JobError(f"{path}: {e}") from e
    return loads(name, text, folder, where=str(path))


def check_options(job: Job, allowed: set[str]) -> None:
    """Raises ValueError naming the first runner_options key the runner does not know."""
    for k in sorted(set(job.runner_options) - allowed):
        raise ValueError(f"runner_options.{k} is not an option of {job.runner} (known: {', '.join(sorted(allowed))})")


def option(job: Job, key: str, kind: type, default=None):
    """One runner_options value, checked to be a `kind`. Raises ValueError naming runner_options.<key>."""
    v = job.runner_options.get(key, default)
    if v is default:
        return v
    if not isinstance(v, kind) or isinstance(v, bool) and kind is not bool:
        raise ValueError(f"runner_options.{key} must be a {kind.__name__}")
    return v


def string_list(job: Job, key: str) -> list[str]:
    v = option(job, key, list, [])
    if not all(isinstance(x, str) and x for x in v):
        raise ValueError(f"runner_options.{key} must be a list of strings")
    return v


# Names a job's env may not set: they would point the runner away from the broker or the gate, hand it a credential,
# or load code into it. The adapter sets the ones it needs after the job's env, so the job cannot get in the way.
_RESERVED_PREFIXES = ("ANTHROPIC_", "OPENAI_", "OPENCODE_", "CLAUDE_", "WS_", "LD_", "DYLD_", "PYTHON")
_RESERVED_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASSWORD", "_CREDENTIALS")
_RESERVED_NAMES = {"PATH", "HOME", "NODE_OPTIONS", "BASH_ENV", "ENV", "SHELL"}
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def extra_env(job: Job) -> dict[str, str]:
    """runner_options.env: variables the job asks the runner to start with (plain text in the file, so never a
    secret). Raises ValueError naming runner_options.env for a name that is reserved."""
    v = option(job, "env", dict, {})
    if not all(isinstance(k, str) and isinstance(x, str) for k, x in v.items()):
        raise ValueError("runner_options.env must map names to strings")
    for k in v:
        up = k.upper()
        if not _ENV_NAME.match(k):
            raise ValueError(f"runner_options.env: {k!r} is not a variable name")
        if up in _RESERVED_NAMES or up.startswith(_RESERVED_PREFIXES) or up.endswith(_RESERVED_SUFFIXES):
            raise ValueError(
                f"runner_options.env: {k} is reserved (ws sets the runner's credentials, paths and config)"
            )
    return dict(v)
