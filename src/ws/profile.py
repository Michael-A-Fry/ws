"""The profile: one TOML file per person (machine answers, roles, caps). Stdlib only: tomllib reads, a small writer
writes the flat shapes the profile uses (tables of strings, numbers, booleans and string lists)."""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

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


def ws_home(env: dict | None = None) -> Path:
    env = os.environ if env is None else env
    return Path(env["WS_HOME"]).expanduser() if env.get("WS_HOME") else Path.home() / ".ws"


def profile_path(env: dict | None = None) -> Path:
    return ws_home(env) / "profile.toml"


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


def write_if_changed(path: Path, data: dict) -> bool:
    """Write the profile only if its content would change. True if the file was written."""
    text = dumps(data)
    if path.is_file() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return True
