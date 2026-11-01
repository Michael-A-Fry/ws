"""The gate in the runner's own user config: ws's deny rules and hooks added beside yours, recorded, and taken out again.

`ws install` with `gate = "every-session"` merges what `ws gate generate` makes into the file the runner reads in every
session (Claude Code's `settings.json`, OpenCode's `opencode.json`), so a session started the usual way is gated too.
The promises, each pinned by a test:

- Yours stays. Your hooks, permissions, env and keys ws does not use are never touched, and their order is kept. ws
  appends its items; it refuses, and changes nothing, when one of your values would be overridden (an `env` key you set
  to another value).
- Before the first write the file's own bytes go to `$WS_HOME/backups/` (mode 0600).
- The manifest records the items ws added, as exact JSON, and the hash of ws's last write.
- Undo is exact. If the file is still what ws last wrote, your original bytes come back (or the file goes, when ws made
  it). If you changed it since, only the recorded items come out and your edits stay.
- A file ws cannot merge safely (a link, not plain JSON, JSONC with comments) is refused with the reason and the way to
  gate a session without it: `ws gate launch`.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import shlex
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import adapters, policy, profile, selfcmd
from ws.output import WsError

GATES = ("every-session", "launch")
RUNNERS = ("claude-code", "opencode")
KIND = "merge"
OK, WARN, FAIL = "ok", "warn", "fail"

_MAX_BYTES = 4 << 20
_PLUGIN_NAME = "plugins/ws-gate.js"
_HOOK_SUFFIX = " || exit 2"
_MISSING = object()


@dataclass(frozen=True)
class Merge:
    """What to merge into one user config file. `ops` empty means take ws's entries out."""

    runner: str
    path: Path
    ops: tuple = ()
    backup: Path | None = None  # where the file's own bytes go on the first write; None when there is no file yet
    cmd: tuple = ()  # the argv that runs ws in the hooks (recorded, so doctor can see it still starts)


@dataclass(frozen=True)
class GatePlan:
    files: dict  # name -> text for $WS_HOME/gate/<runner>/
    merge: Merge
    copies: dict = field(default_factory=dict)  # more plain files: path -> text (OpenCode's plugin)


@dataclass
class Outcome:
    action: str  # "applied" | "restored" | "removed" | "trimmed" | "gone" | "pending" | "refused"
    removed: list = field(default_factory=list)  # other paths deleted (the backup, folders ws made)
    note: str = ""


# ---- where --------------------------------------------------------------------------------------------------------------


def _home(env: Mapping[str, str]) -> Path:
    return Path(env.get("HOME") or str(Path.home()))


def _ws_home(env: Mapping[str, str]) -> Path:
    return Path(os.path.abspath(profile.ws_home(dict(env))))


def backups_dir(env: Mapping[str, str]) -> Path:
    return _ws_home(env) / "backups"


def target(runner: str, env: Mapping[str, str]) -> Path:
    """The user config file the runner reads in every session."""
    if runner == "claude-code":
        d = env.get("CLAUDE_CONFIG_DIR")
        base = Path(os.path.abspath(os.path.expanduser(d))) if d else _home(env) / ".claude"
        return base / "settings.json"
    if runner == "opencode":
        x = env.get("XDG_CONFIG_HOME", "")
        base = Path(x) if os.path.isabs(x) else _home(env) / ".config"
        return base / "opencode" / "opencode.json"
    raise KeyError(runner)


def plugin_path(env: Mapping[str, str]) -> Path:
    """OpenCode's global plugin file (the shim)."""
    return target("opencode", env).parent / _PLUGIN_NAME


def file_roots(env: Mapping[str, str]) -> list[Path]:
    """Folders besides ws's own where a file ws made (a plain file step) may be removed again."""
    return [plugin_path(env).parent]


def supports(runner: str) -> bool:
    return runner in RUNNERS


# ---- reading and writing the file ----------------------------------------------------------------------------------------


def _refuse(path: Path, why: str) -> WsError:
    return WsError(
        "refused",
        f"{path} {why}; ws changed nothing",
        'start gated sessions with `ws gate launch`, or set `gate = "launch"` in [roles.runner] of your profile',
    )


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load(path: Path) -> tuple[dict, bytes | None]:
    """(the parsed file, its bytes); ({}, None) when there is no file. Raises WsError for what ws will not merge into."""
    if path.is_symlink():
        raise _refuse(path, "is a link")
    if not os.path.lexists(path):
        return {}, None
    if not path.is_file():
        raise _refuse(path, "is not a plain file")
    try:
        if path.stat().st_size > _MAX_BYTES:
            raise _refuse(path, "is too big to merge into")
        raw = path.read_bytes()
        text = raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise _refuse(path, "is not UTF-8 text") from e
    except OSError as e:
        raise _refuse(path, f"can't be read ({e.strerror})") from e
    if not text.strip():
        return {}, raw
    try:
        doc = json.loads(text)
    except ValueError as e:
        raise _refuse(path, "is not plain JSON (comments or a trailing comma?)") from e
    if not isinstance(doc, dict):
        raise _refuse(path, "is not a JSON object")
    return doc, raw


def _style(text: str) -> tuple[str | None, bool]:
    """(indent, ends with a newline) of the file as written, so a rewrite keeps your layout."""
    newline = text.endswith("\n") or not text.strip()
    if "\n" not in text.strip():
        return (None, newline) if text.strip() else ("  ", True)
    for line in text.splitlines()[1:]:
        body = line.lstrip(" \t")
        if body and body != line:
            lead = line[: len(line) - len(body)]
            return ("\t" if lead[0] == "\t" else lead), newline
    return "  ", newline


def dump(doc: dict, raw: bytes | None) -> bytes:
    indent, newline = _style(raw.decode("utf-8")) if raw else ("  ", True)
    text = json.dumps(doc, indent=indent, ensure_ascii=False)
    return (text + ("\n" if newline else "")).encode("utf-8")


def _write(path: Path, data: bytes) -> None:
    from ws import install  # late: ws.install imports this module

    install._write_atomic(path, data)


# ---- merging ---------------------------------------------------------------------------------------------------------------


class _Mark:
    """An item ws added earlier, held in its place while the new items are applied around it."""

    __slots__ = ("value",)

    def __init__(self, value):
        self.value = value


def _dotted(path) -> str:
    return ".".join(path)


def _container(doc: dict, path, make, made: list, where: Path) -> dict | list:
    """The dict or list at `path`, made if missing (ws then owns the empty container and tidies it away later)."""
    cur = doc
    for i, key in enumerate(path):
        want = make if i == len(path) - 1 else dict
        if key in cur:
            if not isinstance(cur[key], want):
                kind = "a list" if want is list else "an object"
                raise WsError(
                    "refused",
                    f"{where}: `{_dotted(path[: i + 1])}` is not {kind}, so ws can't add to it; ws changed nothing",
                    "start gated sessions with `ws gate launch`, or fix that key",
                )
            cur = cur[key]
        else:
            cur[key] = want()
            made.append(list(path[: i + 1]))
            cur = cur[key]
    return cur


def _lookup(doc, path):
    cur = doc
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return _MISSING
        cur = cur[key]
    return cur


def _sweep(node) -> None:
    if isinstance(node, dict):
        for k in [k for k, v in node.items() if isinstance(v, _Mark)]:
            del node[k]
        for v in node.values():
            _sweep(v)
    elif isinstance(node, list):
        node[:] = [x for x in node if not isinstance(x, _Mark)]
        for x in node:
            _sweep(x)


def update(doc: dict, old_added, old_made, ops, where: Path) -> tuple[dict, list, list]:
    """(new doc, the ops that took effect, the containers ws made). Items ws added before are swapped for `ops` in place;
    an op whose item is already there (yours) takes no effect and is not recorded. `ops` empty takes ws's items out."""
    doc = copy.deepcopy(doc)
    for op in old_added:
        if op["op"] == "append":
            lst = _lookup(doc, op["path"])
            if isinstance(lst, list):
                for i in range(len(lst) - 1, -1, -1):
                    if lst[i] == op["value"]:
                        lst[i] = _Mark(op["value"])
                        break
        else:
            parent = _lookup(doc, op["path"][:-1])
            key = op["path"][-1]
            if isinstance(parent, dict) and key in parent and parent[key] == op["value"]:
                parent[key] = _Mark(op["value"])
    made: list = [list(m) for m in old_made]
    added: list = []
    for op in ops:
        if op["op"] == "append":
            lst = _container(doc, op["path"], list, made, where)
            slot = next((i for i, x in enumerate(lst) if isinstance(x, _Mark)), None)
            if slot is not None:
                lst[slot] = op["value"]
            elif op["value"] in lst:
                continue
            else:
                lst.append(op["value"])
            added.append(op)
        else:
            parent = _container(doc, op["path"][:-1], dict, made, where)
            key = op["path"][-1]
            now = parent.get(key, _MISSING)
            if isinstance(now, _Mark) or now is _MISSING:
                parent[key] = op["value"]
            elif now == op["value"]:
                continue
            else:
                raise WsError(
                    "conflict",
                    f"{where}: you have `{_dotted(op['path'])}` as {json.dumps(now)}, and ws needs "
                    f"{json.dumps(op['value'])}; ws changed nothing",
                    "change that value to match, or start gated sessions with `ws gate launch`",
                )
            added.append(op)
    _sweep(doc)
    kept: list = []
    for m in sorted(made, key=len, reverse=True):
        cur = _lookup(doc, m)
        if cur is _MISSING:
            continue
        if cur == {} or cur == []:
            parent = _lookup(doc, m[:-1]) if len(m) > 1 else doc
            del parent[m[-1]]
        else:
            kept.append(m)
    return doc, added, kept


def present(doc: dict, ops) -> bool:
    """True when every op's item is in the doc."""
    for op in ops:
        got = _lookup(doc, op["path"])
        if op["op"] == "append":
            if not isinstance(got, list) or op["value"] not in got:
                return False
        elif got is _MISSING or got != op["value"]:
            return False
    return True


def wanted(ops) -> str:
    return _sha(json.dumps(list(ops), sort_keys=True).encode("utf-8"))


# ---- what ws wants in each runner's file ------------------------------------------------------------------------------------


def _claude_ops(files: Mapping[str, str]) -> list[dict]:
    conf = json.loads(files["settings.json"])
    ops: list[dict] = []
    for event, groups in conf.get("hooks", {}).items():
        ops += [{"op": "append", "path": ["hooks", event], "value": g} for g in groups]
    deny = conf.get("permissions", {}).get("deny", [])
    ops += [{"op": "append", "path": ["permissions", "deny"], "value": rule} for rule in deny]
    env = dict(conf.get("env", {}))
    env["WS_SANDBOXED"] = "0"  # this file carries no sandbox block; the hook then skips the project-settings preflight
    ops += [{"op": "set", "path": ["env", k], "value": v} for k, v in env.items()]
    return ops


def _opencode_ops(files: Mapping[str, str]) -> list[dict]:
    conf = json.loads(files["opencode.json"])
    ops: list[dict] = []
    for tool, rules in conf.get("permission", {}).items():
        if isinstance(rules, dict):
            ops += [
                {"op": "set", "path": ["permission", tool, pat], "value": "deny"}
                for pat, act in rules.items()
                if act == "deny"
            ]
        elif rules == "deny":
            ops.append({"op": "set", "path": ["permission", tool], "value": "deny"})
    return ops


def _notice(runner: str, where: Path, backup: Path | None, cmd: list[str]) -> str:
    out = f"ws: the gate did not run, so this call is blocked. To take ws out of {where}: {shlex.join(cmd)} install --gate launch"
    if backup:
        out += f" (your file from before ws is saved at {backup})"
    return out


def _tune(files: dict, notice: str) -> dict:
    """Claude Code: the PreToolUse hook says why it blocked when ws itself failed to run (any exit but 0 allow, 2 deny)."""
    conf = json.loads(files["settings.json"])
    for group in conf["hooks"]["PreToolUse"]:
        for hook in group["hooks"]:
            cmd = hook["command"]
            if cmd.endswith(_HOOK_SUFFIX):
                body = cmd[: -len(_HOOK_SUFFIX)]
                hook["command"] = (
                    f'{body}; rc=$?; [ "$rc" -eq 0 ] && exit 0; [ "$rc" -eq 2 ] || echo {shlex.quote(notice)} >&2; exit 2'
                )
    return {**files, "settings.json": json.dumps(conf, indent=2) + "\n"}


def _new_backup(runner: str, env: Mapping[str, str]) -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    d = backups_dir(env)
    path = d / f"{runner}-user-config-{stamp}.json"
    n = 1
    while os.path.lexists(path):
        n += 1
        path = d / f"{runner}-user-config-{stamp}-{n}.json"
    return path


def plan(runner: str, pol: policy.Policy, policy_file: Path, env: Mapping[str, str], entry: Mapping | None) -> GatePlan:
    """The generated files and the merge for this runner. Pure but for reading the target (to name the backup)."""
    adapter = adapters.get(runner)
    cmd = selfcmd.argv()
    where = target(runner, env)
    out = _ws_home(env) / "gate" / runner
    if entry is not None:
        backup = Path(entry["backup"]) if entry.get("backup") else None
    else:
        backup = _new_backup(runner, env) if os.path.lexists(where) else None
    copies: dict = {}
    if runner == "claude-code":
        files = adapter.generate(pol, cmd, out, policy_file, protect=(str(where),))
        files = _tune(files, _notice(runner, where, backup, cmd))
        ops = _claude_ops(files)
    else:
        plug = plugin_path(env)
        files = adapter.generate(pol, cmd, out, policy_file, protect=(str(where), str(plug)))
        ops = _opencode_ops(files)
        copies[plug] = files[adapter.PLUGIN]
    return GatePlan(files, Merge(runner, where, tuple(ops), backup, tuple(cmd)), copies)


def withdrawal(runner: str, env: Mapping[str, str]) -> Merge:
    return Merge(runner, target(runner, env))


# ---- applying and undoing --------------------------------------------------------------------------------------------------


def _check_jsonc_sibling(m: Merge) -> None:
    sibling = m.path.with_suffix(".jsonc")
    if m.runner == "opencode" and os.path.lexists(sibling):
        raise _refuse(sibling, "exists, and ws can't merge into JSONC")


def classify(m: Merge, entry: Mapping | None) -> str:
    """created, changed or unchanged: what applying this merge would do. Raises WsError for a file ws won't merge into."""
    if not m.ops:
        return "changed" if entry else "unchanged"
    _check_jsonc_sibling(m)
    doc, raw = load(m.path)
    if entry is None:
        _, added, made = update(doc, [], [], m.ops, m.path)
        if not added and not made:
            return "unchanged"
        return "created" if raw is None else "changed"
    update(
        doc, entry.get("added", []), entry.get("made", []), m.ops, m.path
    )  # a conflict raises here, before any write
    if raw is not None and entry.get("wanted") == wanted(m.ops) and present(doc, entry.get("added", [])):
        return "unchanged"
    return "changed"


def _write_backup(m: Merge, raw: bytes) -> None:
    d = m.backup.parent
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(m.backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())


def apply(m: Merge, entry: Mapping | None, env: Mapping[str, str]) -> tuple[dict | None, Outcome]:
    """Merge into the file. Returns (the manifest entry to keep, or None when there is nothing to record, the outcome)."""
    if not m.ops:
        out = withdraw(entry, env) if entry else Outcome("gone")
        return (dict(entry) if out.action == "pending" else None), out
    _check_jsonc_sibling(m)
    doc, raw = load(m.path)
    old_added = list(entry.get("added", [])) if entry else []
    old_made = list(entry.get("made", [])) if entry else []
    new, added, made = update(doc, old_added, old_made, m.ops, m.path)
    if entry is None and not added and not made:
        return None, Outcome("applied")
    backup = entry.get("backup") if entry else (str(m.backup) if m.backup and raw is not None else None)
    backup_sha = entry.get("backup_sha256") if entry else (_sha(raw) if raw is not None else None)
    if entry is None and raw is not None and m.backup:
        _write_backup(m, raw)
    made_dirs = list(entry.get("made_dirs", [])) if entry else []
    if raw is None:
        missing = []
        p = m.path.parent
        while not p.exists():
            missing.append(p)
            p = p.parent
        for d in reversed(missing):
            d.mkdir()
            made_dirs.append(str(d))
    data = dump(new, raw)
    if data != raw:
        _write(m.path, data)
    kept = {
        "path": str(m.path),
        "kind": KIND,
        "created_by_ws": bool(entry.get("created_by_ws")) if entry else raw is None,  # ws made the file itself
        "runner": m.runner,
        "added": added,
        "made": made,
        "made_dirs": made_dirs,
        "wanted": wanted(m.ops),
        "cmd": list(m.cmd),
        "sha256": _sha(data),
        "backup": backup,
        "backup_sha256": backup_sha,
    }
    return kept, Outcome("applied")


def _backup_ok(entry: Mapping, env: Mapping[str, str]) -> Path | None:
    """The recorded backup, if it is where ws keeps them and still holds the bytes ws saved."""
    b = entry.get("backup")
    if not isinstance(b, str) or not b:
        return None
    p = Path(os.path.normpath(b))
    if p.parent != backups_dir(env) or p.is_symlink() or not p.is_file():
        return None
    return p if _sha(p.read_bytes()) == entry.get("backup_sha256") else None


def withdraw(entry: Mapping, env: Mapping[str, str], dry_run: bool = False) -> Outcome:
    """Take ws's entries out of the file the manifest names. Exact when the file is as ws last wrote it, surgical when
    you have edited it since. The path must be the target for the runner now: the manifest can't point this elsewhere."""
    runner = entry.get("runner")
    if runner not in RUNNERS or Path(os.path.normpath(str(entry.get("path")))) != target(runner, env):
        return Outcome("refused", note=f"{entry.get('path')}: not a user config file ws manages")
    path = target(runner, env)
    if not os.path.lexists(path):
        return Outcome("gone", note=f"{path} is already gone")
    try:
        doc, raw = load(path)
    except WsError as e:
        return Outcome("pending", note=f"{e.message}; ws's entries are still in it. {e.hint}")
    if raw is None:
        return Outcome("gone", note=f"{path} is already gone")
    removed: list = []
    backup = _backup_ok(entry, env)
    as_written = _sha(raw) == entry.get("sha256")
    if as_written and (backup is not None or not entry.get("backup")):
        if not dry_run:
            if backup is None:
                path.unlink()  # ws made the file
            else:
                _write(path, backup.read_bytes())
                backup.unlink()
                removed.append(backup)
            removed += _tidy(path, entry, backups_dir(env))
        word = "removed (ws made it)" if backup is None else "restored exactly"
        return Outcome("removed" if backup is None else "restored", removed, f"{path}: {word}")
    new, _, _ = update(doc, entry.get("added", []), entry.get("made", []), [], path)
    if not dry_run:
        _write(path, dump(new, raw))
    why = "you changed it since" if as_written is False else "the saved copy is gone"
    return Outcome("trimmed", removed, f"{path}: {why}, so ws took out only its own entries and kept your edits")


def _tidy(path: Path, entry: Mapping, backup_dir: Path) -> list:
    """Remove the backups folder and the folders ws made above the file, each only when empty. Returns what went."""
    gone: list = []
    made = [Path(p) for p in reversed(entry.get("made_dirs", [])) if Path(p) in path.parents]
    for d in [backup_dir, *made]:
        try:
            d.rmdir()
        except OSError:
            continue
        gone.append(d)
    return gone


# ---- doctor ----------------------------------------------------------------------------------------------------------------


def _profile_runner(env: Mapping[str, str]) -> tuple[str, str]:
    """(runner adapter name, the gate setting: a value from GATES, or "" when the profile has none)."""
    doc = profile.load(profile.profile_path(dict(env))) or {}
    rls = doc.get("roles") if isinstance(doc.get("roles"), dict) else {}
    tbl = rls.get("runner") if isinstance(rls.get("runner"), dict) else {}
    name = tbl.get("adapter") if isinstance(tbl.get("adapter"), str) else ""
    gate = tbl.get("gate") if tbl.get("gate") in GATES else ""
    return name, gate


def _manifest_entry(runner: str, env: Mapping[str, str]) -> dict | None:
    from ws import install  # late: ws.install imports this module

    entries, _ = install._read_manifest(env)
    e = entries.get(str(target(runner, env)))
    return e if e and e.get("kind") == KIND else None


def _starts(cmd: list[str]) -> bool:
    """Does the recorded way of starting ws still start it?"""
    if not cmd or not os.access(cmd[0], os.X_OK):
        return False
    try:
        done = subprocess.run([cmd[0], "-I", "-c", "import ws"], capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def check(env: Mapping[str, str] | None = None) -> tuple[str, str, str]:
    """(status, detail, fix) for the gate in the runner's user config: doctor's WS008 and the watchdog read this."""
    env = os.environ if env is None else env
    try:
        runner, gate = _profile_runner(env)
    except profile.ProfileError:
        return OK, "the profile doesn't read (see WS003), so there's no gate setting to check", ""
    if runner not in RUNNERS:
        return OK, "gated when started with `ws gate launch`", ""
    where = target(runner, env)
    entry = _manifest_entry(runner, env)
    if gate != "every-session":
        if entry:
            return WARN, f'gate is "launch" but ws\'s entries are still in {where}', "run `ws install`"
        return OK, "gated when started with `ws gate launch`", ""
    again = "run `ws install` to put them back, or `ws install --gate launch` to take ws out"
    if entry is None:
        return FAIL, f'gate is "every-session" but ws hasn\'t added its entries to {where}', "run `ws install`"
    try:
        doc, raw = load(where)
    except WsError as e:
        return FAIL, e.message, e.hint
    if raw is None:
        return FAIL, f"{where} is gone, and with it ws's gate", again
    lost = [op for op in entry.get("added", []) if not present(doc, [op])]
    if lost:
        return FAIL, f"{len(lost)} of ws's entries are gone from {where}", again
    cmd = [str(x) for x in entry.get("cmd", [])]
    if runner == "opencode" and not os.path.isfile(plugin_path(env)):
        return FAIL, f"the plugin {plugin_path(env)} is gone", again
    if not _starts(cmd):
        return FAIL, f"the hook command no longer starts ({cmd[0] if cmd else 'none recorded'})", again
    policy_file = policy.policy_path(dict(env))
    try:
        pol = policy.load(policy_file)
    except (policy.PolicyError, OSError) as e:
        return FAIL, f"the policy doesn't load ({e}), so the gate blocks every call", f"fix {policy_file}"
    try:
        want = plan(runner, pol, policy_file.resolve(), env, entry)
        stale = wanted(want.merge.ops) != entry.get("wanted")
    except (WsError, KeyError, ValueError, OSError):
        stale = True
    unsupported = [g for g in getattr(adapters.get(runner), "UNSUPPORTED_GUARDS", ()) if g in pol.guards.on]
    notes = [f"the guard `{g}` isn't run by {runner}" for g in unsupported]
    if stale:
        return WARN, "; ".join([f"ws's entries in {where} are from an earlier policy", *notes]), "run `ws install`"
    detail = f"every session is gated: ws's entries are in {where}"
    return (WARN, "; ".join([detail, *notes]), "") if notes else (OK, detail, "")
