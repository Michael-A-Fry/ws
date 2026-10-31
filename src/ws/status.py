"""`ws status`: for each runner, every rules, skill, hook and MCP file it will load here, with where it came from and its size.

It answers "why did the agent do that". Origin is `global` (the runner's user config), `repo` (the repo you are in) or
`override` (a repo copy that `.ws/overrides.toml` lists on purpose). The skill, agent and hook items come from the same
scan as `ws globals check` (src/ws/globals.py); the rules, settings and MCP files are the fixed names in FILES below. A
file that is not there is not listed. Only names and sizes are read, plus the key names of a JSON settings file (which
hooks and MCP servers it sets); no file content is shown.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from ws import adapters, output
from ws import globals as ws_globals

# runner -> (kind, name below the user config folder, name below the repo). Kinds: rules, settings, mcp.
FILES: dict[str, list[tuple[str, str | None, str | None]]] = {
    "claude-code": [
        ("rules", "CLAUDE.md", "CLAUDE.md"),
        ("rules", None, ".claude/CLAUDE.md"),
        ("settings", "settings.json", ".claude/settings.json"),
        ("settings", None, ".claude/settings.local.json"),
        ("mcp", None, ".mcp.json"),
    ],
    "opencode": [
        ("rules", "AGENTS.md", "AGENTS.md"),
        ("settings", "opencode.json", "opencode.json"),
        ("settings", "opencode.jsonc", "opencode.jsonc"),
        ("settings", None, ".opencode/opencode.json"),
    ],
}


def _size(path: Path) -> int:
    try:
        if path.is_file():
            return path.stat().st_size
        return sum(f.stat().st_size for f in ws_globals._files(path))
    except OSError:
        return 0


def _items(root: Path, kinds: dict[str, str]):
    """One entry per skill, agent, hook ...: its main file, or its folder when it has no main file."""
    entries = ws_globals.scan(root, kinds)
    has_file = {(e.kind, e.name) for e in entries if (root / e.path).is_file()}
    return [e for e in entries if (root / e.path).is_file() or (e.kind, e.name) not in has_file]


def _sets(path: Path) -> dict:
    """The hook events and MCP servers a JSON settings file sets (names only)."""
    if path.suffix != ".json":
        return {}
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    out = {}
    for key, label in (("hooks", "hooks"), ("mcpServers", "mcp_servers"), ("mcp", "mcp_servers")):
        if isinstance(doc.get(key), dict) and doc[key]:
            out[label] = sorted(doc[key])
    return out


def _row(runner: str, kind: str, path: Path, origin: str, name: str = "", reason: str = "") -> dict:
    row = {
        "runner": runner,
        "kind": kind,
        "name": name or path.name,
        "path": str(path),
        "origin": origin,
        "bytes": _size(path),
    }
    if reason:
        row["reason"] = reason
    row.update(_sets(path))
    return row


def collect(start: Path, env: dict | None = None) -> dict:
    env = dict(os.environ) if env is None else env
    repo = ws_globals.find_repo(start) or start
    layouts = {n: m.GLOBALS for n, m in adapters.all_adapters().items() if hasattr(m, "GLOBALS")}
    overrides, errors = ws_globals.load_overrides(repo, list(layouts.values()))
    runners: dict[str, list[dict]] = {}
    for runner, layout in layouts.items():
        user = ws_globals.user_dir(layout, env)
        rows: list[dict] = []
        for kind, in_user, in_repo in FILES.get(runner, []):
            if in_user and (user / in_user).exists():
                rows.append(_row(runner, kind, user / in_user, "global"))
            if in_repo and (repo / in_repo).exists():
                rows.append(_row(runner, kind, repo / in_repo, "repo"))
        for e in _items(user, layout["kinds"]):
            rows.append(_row(runner, e.kind, user / e.path, "global", e.name))
        for proj in ws_globals.project_dirs(repo, layout["project_dir"]):
            if ws_globals._resolved(proj) == ws_globals._resolved(user):
                continue
            for e in _items(proj, layout["kinds"]):
                rel = (proj / e.path).relative_to(repo).as_posix()
                why = next((r for o, r in overrides.items() if ws_globals._covers(o, rel)), "")
                rows.append(_row(runner, e.kind, proj / e.path, "override" if why else "repo", e.name, why))
        runners[runner] = rows
    return {
        "cwd": str(start),
        "repo": str(repo),
        "runners": runners,
        "total_bytes": sum(r["bytes"] for rows in runners.values() for r in rows),
        "errors": errors,
    }


def render(data: dict) -> str:
    out = [f"repo: {data['repo']}"]
    for runner, rows in data["runners"].items():
        out.append(f"{runner}: {len(rows)} file(s), {sum(r['bytes'] for r in rows):,} bytes")
        for r in rows:
            extra = ""
            if r.get("hooks"):
                extra += f"  hooks: {', '.join(r['hooks'])}"
            if r.get("mcp_servers"):
                extra += f"  mcp: {', '.join(r['mcp_servers'])}"
            out.append(f"  {r['origin']:<8} {r['kind']:<9} {r['bytes']:>8,}  {r['path']}{extra}")
    out += [f"error: {e}" for e in data["errors"]]
    return "\n".join(out)


def cmd_status(args: argparse.Namespace) -> int:
    data = collect(Path.cwd())
    if args.json:
        output.result(data)
    else:
        print(render(data))
    return 0


def register(sub) -> None:
    s = sub.add_parser(
        "status", help="per runner, every rules, skill, hook and MCP file it will load here, and its origin"
    )
    s.set_defaults(func=cmd_status)
