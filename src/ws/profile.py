"""The profile: one TOML file per person (machine answers, roles, caps). Stdlib only: tomllib reads, a small writer
writes the flat shapes the profile uses (tables of strings, numbers, booleans and string lists)."""

from __future__ import annotations

import os
import re
import shutil
import tomllib
from datetime import UTC, datetime
from pathlib import Path

from ws import paths

HEADER = (
    "# ws profile. `ws init` writes [machine]; `ws doctor` checks every [roles.*] table.\n"
    "# ws rewrites this file when it changes it: comments are not kept.\n"
)
_BARE_KEY = re.compile(r"^[A-Za-z0-9_-]+$")

# A key that was renamed: old dotted key -> the dotted key that replaces it. `ws config` warns on the old one. Empty
# until a key is renamed; the loader that renames it must still accept the old spelling for a release.
DEPRECATED: dict[str, str] = {}


class ProfileError(Exception):
    pass


def profile_path(env: dict | None = None) -> Path:
    return paths.config_dir(env) / "profile.toml"


def load(path: Path) -> dict | None:
    """The parsed profile, or None if there is no file. A file that does not parse raises ProfileError."""
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as e:
        raise ProfileError(f"{path}: {e}") from e


def flat_keys(doc: dict, prefix: str = "") -> list[str]:
    """Every dotted key that holds a value (a table is walked, not listed), sorted."""
    out: list[str] = []
    for k, v in doc.items():
        name = f"{prefix}{k}"
        if isinstance(v, dict) and v:
            out += flat_keys(v, name + ".")
        else:
            out.append(name)
    return sorted(out)


def deprecated_in(doc: dict, table: dict[str, str]) -> list[tuple[str, str]]:
    """(old key, replacement) for each key of `doc` that `table` says was renamed."""
    keys = set(flat_keys(doc))
    return [
        (old, new) for old, new in sorted(table.items()) if old in keys or any(k.startswith(old + ".") for k in keys)
    ]


def _key(k: str) -> str:
    return k if _BARE_KEY.match(k) else _string(k)


def _string(s: str) -> str:
    out = s.replace("\\", "\\\\").replace('"', '\\"')
    out = "".join(c if (c >= " " and c != "\x7f") or c == "\t" else f"\\u{ord(c):04x}" for c in out)
    return f'"{out}"'


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, str):
        return _string(v)
    if isinstance(v, list) and all(isinstance(x, (str, int, float, bool)) for x in v):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    raise ProfileError(f"cannot write {type(v).__name__} to the profile")


def dumps(data: dict) -> str:
    """Write a profile: top-level scalars first, then each table, nested tables as dotted headers."""
    lines = HEADER.rstrip("\n").split("\n")

    def table(prefix: list[str], d: dict) -> None:
        scalars = {k: v for k, v in d.items() if not isinstance(v, dict)}
        tables = {k: v for k, v in d.items() if isinstance(v, dict)}
        if prefix and (scalars or not tables):
            lines.append("")
            lines.append("[" + ".".join(_key(p) for p in prefix) + "]")
        for k, v in scalars.items():
            lines.append(f"{_key(k)} = {_value(v)}")
        for k, v in tables.items():
            table([*prefix, k], v)

    table([], data)
    return "\n".join(lines) + "\n"


# The keys an agent session may never change, whatever command reaches the write: the spend cap and which models the
# jobs run on. `ws.agentsession` refuses the commands by name first; this is the check at the write itself.
GUARDED = ("roles.budget.monthly_cap_usd", "models", "roles.model")


def _at(doc: dict | None, dotted: str):
    here = doc
    for part in dotted.split("."):
        if not isinstance(here, dict):
            return None
        here = here.get(part)
    return here


def guarded_changes(old: dict | None, new: dict) -> list[str]:
    """The GUARDED keys whose value differs between `old` and `new` (a key added or removed counts)."""
    return [k for k in GUARDED if _at(old, k) != _at(new, k)]


def _session_refusal() -> str | None:
    import sys

    from ws import agentsession  # here, not at the top: it is only needed when a guarded key changes

    def tty(stream) -> bool:
        try:
            return bool(stream.isatty())
        except (AttributeError, ValueError, OSError):
            return False

    return agentsession.refusal(os.environ, tty(sys.stdin), tty(sys.stdout))


def check_session_write(path: Path, new: dict, refusal=None) -> None:
    """Refuse (WsError kind `refused`, exit 3) a write of `new` over the profile at `path` that changes a GUARDED key,
    when this runs inside an agent session (the same test `ws.agentsession` uses for admin commands). A write that
    leaves those keys as they are is never refused. An unreadable old profile counts as empty."""
    try:
        old = load(path)
    except ProfileError:
        old = None
    changed = guarded_changes(old, new)
    if not changed:
        return
    why = (refusal or _session_refusal)()
    if why:
        from ws.output import WsError

        raise WsError("refused", f"the profile's {', '.join(changed)}: {why}", "change it in your own terminal", 3)


def write_if_changed(path: Path, data: dict) -> bool:
    """Write the profile only if its content would change. True if the file was written. A change to a GUARDED key
    from inside an agent session is refused before anything is written (`check_session_write`)."""
    text = dumps(data)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    check_session_write(path, data)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True


def backup(path: Path, now: datetime | None = None) -> Path | None:
    """Copy the profile beside itself as `profile.toml.bak-<UTC stamp>` before a rewrite (which drops comments), and
    return the copy; None when there is no profile yet. A name already taken gets `-1`, `-2`, ... so nothing is lost."""
    if not path.is_file():
        return None
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    dest, n = path.with_name(f"{path.name}.bak-{stamp}"), 0
    while dest.exists():
        n += 1
        dest = path.with_name(f"{path.name}.bak-{stamp}-{n}")
    shutil.copy2(path, dest)
    return dest
