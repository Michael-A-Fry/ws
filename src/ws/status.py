"""`ws status`: for each runner, every rules, skill, hook and MCP file it will load here, with where it came from and its size.

It answers "why did the agent do that". Origin is `global` (the runner's user config), `repo` (the repo you are in) or
`override` (a repo copy that `.ws/overrides.toml` lists on purpose). The skill, agent and hook items come from the same
scan as `ws globals check` (src/ws/globals.py); the rules, settings and MCP files are the fixed names in FILES below. A
file that is not there is not listed. Only names and sizes are read, plus the key names of a JSON settings file (which
hooks and MCP servers it sets); no file content is shown.

Two more facts come from the settings files, each as a small object in the data: `sandbox` (is Claude Code's sandbox on,
may a command run outside it, does it refuse to start without it, and which file says so) and `permission_mode` (the
`permissions.defaultMode` in force and its file, the highest-precedence file winning). They are read without following a
link, capped in size and never shown in full; a file that does not read or parse is named under `unreadable`, not a crash.
ws never writes `defaultMode`: this only reports it.

A path is listed once per runner, and a skill folder with no main file is shown as a folder (a trailing `/`). The text
output goes through `style.py`; `--json` is the same data with a `folder` flag on each row.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from ws import adapters, output, paths, wallsettings
from ws import globals as ws_globals
from ws.approvals import SANDBOX_MARKER, ApprovalError
from ws.approvals import _read_regular as read_regular
from ws.style import Style, pad, plural

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
        "folder": path.is_dir(),
        "origin": origin,
        "bytes": _size(path),
    }
    if reason:
        row["reason"] = reason
    row.update(_sets(path))
    return row


def _once(rows: list[dict]) -> list[dict]:
    """The rows with a repeated path dropped (the first stays): a folder that holds several files is scanned as a whole
    and as its parts, and a link can reach the same file twice."""
    seen: set[str] = set()
    out = []
    for r in rows:
        if r["path"] not in seen:
            seen.add(r["path"])
            out.append(r)
    return out


MAX_SETTINGS_BYTES = 1 << 20
BYPASS_MODE = "bypassPermissions"
# Claude Code ignores these modes when a project or local settings file sets them: only the person's own file (or a
# managed one) can switch them on.
_PERSONAL_ONLY_MODES = (BYPASS_MODE, "auto")


def _settings(path: Path) -> tuple[dict | None, str | None]:
    """(the settings in `path`, None), or (None, why not): not a plain file (a link, a FIFO), too big, unreadable or not a
    JSON object. Opened without following a link and without blocking, as the hook does."""
    try:
        doc = json.loads(read_regular(path, MAX_SETTINGS_BYTES))
    except ApprovalError as e:
        return None, str(e)
    except ValueError:
        return None, "not valid JSON"
    return (doc, None) if isinstance(doc, dict) else (None, "not a JSON object")


def _flag(value) -> bool | None:
    return value if isinstance(value, bool) else None


def sandbox_state(env: dict) -> dict:
    """`{enabled, allowUnsandboxedCommands, failIfUnavailable, source}` from the settings ws generated: the runner's user
    settings when they carry ws's entries (the WS_SANDBOXED marker), else `<state>/gate/claude-code/settings.json`, else
    all null. `source` is the file read. A settings file with ws's entries but no sandbox block is `enabled: false`
    (the policy's `sandbox = false`). A file that does not read is listed under `unreadable` and the next one is tried."""
    layout = adapters.get("claude-code").GLOBALS
    user = ws_globals.user_dir(layout, env) / "settings.json"
    generated = paths.state_dir(env) / "gate" / "claude-code" / "settings.json"
    state: dict = {"enabled": None, "allowUnsandboxedCommands": None, "failIfUnavailable": None, "source": None}
    bad: list[str] = []
    for path, needs_marker in ((user, True), (generated, False)):
        if not os.path.lexists(path):
            continue
        doc, why = _settings(path)
        if doc is None:
            bad.append(f"{path}: {why}")
            continue
        marker = doc["env"].get(SANDBOX_MARKER) if isinstance(doc.get("env"), dict) else None
        if needs_marker and marker not in ("0", "1"):
            continue  # the person's own file, with no ws entries: nothing of ws's to report
        block = doc.get("sandbox") if isinstance(doc.get("sandbox"), dict) else None
        state["source"] = str(path)
        if block is None:
            state["enabled"] = False
        else:
            for key in ("enabled", "allowUnsandboxedCommands", "failIfUnavailable"):
                state[key] = _flag(block.get(key))
        break
    return {**state, **({"unreadable": bad} if bad else {})}


def permission_mode(repo: Path, env: dict) -> dict:
    """`{mode, source}`: the `permissions.defaultMode` in force, from the managed, project (local first) and user settings
    files, the highest precedence winning. Both null when no file sets one. A project or local file's `bypassPermissions`
    or `auto` is skipped: Claude Code ignores those modes from there. Files that do not read are under `unreadable`."""
    layout = adapters.get("claude-code").GLOBALS
    ranked = [  # lowest precedence first
        (ws_globals.user_dir(layout, env) / "settings.json", False),
        (repo / ".claude" / "settings.json", True),
        (repo / ".claude" / "settings.local.json", True),
        *((f, False) for f in wallsettings.managed_files()),
    ]
    mode, source, bad = None, None, []
    for path, project in ranked:
        if not os.path.lexists(path):
            continue
        doc, why = _settings(path)
        if doc is None:
            bad.append(f"{path}: {why}")
            continue
        perms = doc.get("permissions")
        value = perms.get("defaultMode") if isinstance(perms, dict) else None
        if isinstance(value, str) and value and not (project and value in _PERSONAL_ONLY_MODES):
            mode, source = value, str(path)
    return {"mode": mode, "source": source, **({"unreadable": bad} if bad else {})}


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
        runners[runner] = _once(rows)
    return {
        "cwd": str(start),
        "repo": str(repo),
        "runners": runners,
        "total_bytes": sum(r["bytes"] for rows in runners.values() for r in rows),
        "sandbox": sandbox_state(env),
        "permission_mode": permission_mode(repo, env),
        "errors": errors,
    }


def render(data: dict, st: Style | None = None) -> str:
    st = st or Style()
    out = [f"{st.muted('repo:')} {data['repo']}"]
    for runner, rows in data["runners"].items():
        total = plural(sum(r["bytes"] for r in rows), "byte")
        out.append(st.heading(f"{runner}:") + f" {plural(len(rows), 'item')}, {total}")
        for r in rows:
            extra = ""
            if r.get("hooks"):
                extra += f"  hooks: {', '.join(r['hooks'])}"
            if r.get("mcp_servers"):
                extra += f"  mcp: {', '.join(r['mcp_servers'])}"
            origin = pad(st.warn(r["origin"]) if r["origin"] == "override" else st.muted(r["origin"]), 8)
            path = r["path"] + ("/" if r.get("folder") else "")
            out.append(f"  {origin} {r['kind']:<9} {r['bytes']:>8,}  {path}{extra}")
    out += _wall_lines(data, st)
    out += [f"{st.status('fail', 'error')}: {e}" for e in data["errors"]]
    return "\n".join(out)


def _wall_lines(data: dict, st: Style) -> list[str]:
    """The sandbox line and the permission mode line (nothing for data from before they existed). Bypass is flagged."""
    out: list[str] = []
    sb = data.get("sandbox")
    if sb:
        words = {True: "on", False: "off", None: "not set"}[sb.get("enabled")]
        retry = {False: "no retry outside it", True: st.warn("a failed command may retry outside it")}
        if sb.get("allowUnsandboxedCommands") in retry:
            words += f", {retry[sb['allowUnsandboxedCommands']]}"
        if sb.get("failIfUnavailable") is True:
            words += ", Claude Code refuses to start without it"
        where = f" ({sb['source']})" if sb.get("source") else ""
        out.append(f"{st.muted('sandbox:')} {words}{where}")
        out += [f"{st.status('warn', 'unreadable')}: {u}" for u in sb.get("unreadable", [])]
    pm = data.get("permission_mode")
    if pm:
        mode = pm.get("mode")
        where = f" ({pm['source']})" if pm.get("source") else ""
        if mode == BYPASS_MODE:
            out.append(f"{st.muted('permission mode:')} {st.warn(mode)}{where}: nothing asks before it runs")
        else:
            out.append(f"{st.muted('permission mode:')} {mode or 'not set (Claude Code asks)'}{where}")
        out += [f"{st.status('warn', 'unreadable')}: {u}" for u in pm.get("unreadable", [])]
    return out


def cmd_status(args: argparse.Namespace) -> int:
    data = collect(Path.cwd())
    if args.json:
        output.result(data)
    else:
        st = Style.detect(sys.stdout, no_color=getattr(args, "no_color", False))
        st.write(render(data, st))
    return 0


def register(sub) -> None:
    s = sub.add_parser(
        "status", help="per runner, every rules, skill, hook and MCP file it will load here, and its origin"
    )
    s.set_defaults(func=cmd_status)
