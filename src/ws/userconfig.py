"""The gate in the runner's own user config: ws's deny rules and hooks added beside yours, recorded, and taken out again.

`ws install` with `gate = "every-session"` merges what `ws gate generate` makes into the file the runner reads in every
session (Claude Code's `settings.json`, OpenCode's `opencode.json`), so a session started the usual way is gated too.
The promises, each pinned by a test:

- Yours stays. Your hooks, permissions, env and keys ws does not use are never touched, and their order is kept. ws
  appends its items; it refuses, and changes nothing, when one of your values would be overridden (an `env` key you set
  to another value).
- Before the first write the file's own bytes go to `$WS_HOME/backups/` (mode 0600).
- The manifest records the items ws added, as exact JSON, the items the release asked for (`wanted_ops`, so doctor can
  see a later release stop asking for one), and the hash of ws's last write.
- Undo is exact only when it can be. If the file is still what ws last wrote, and it was not changed between two
  `ws install` runs, your original bytes come back (or the file goes, when ws made it). If it was changed at any point
  after the first write, only the recorded items come out and your edits stay.
- Writes are checked. ws holds a lock file beside the config while it merges, and reads the file again just before it
  replaces it; a file that changed under it is merged again once, then refused.
- A config that already holds ws's hooks but has no record (the manifest was lost) is adopted: the entries are recorded
  so an undo takes them out, and a hook command from an older ws is replaced in place.
- A file ws cannot merge safely (a link, not plain JSON, JSONC with comments) is refused with the reason and the way to
  gate a session without it: `ws gate launch`.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import contextlib
import copy
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import adapters, policy, profile, selfcmd
from ws.approvals import ENV_MARKER
from ws.output import WsError

GATES = ("every-session", "launch")
RUNNERS = ("claude-code", "opencode")
KIND = "merge"
OK, WARN, FAIL = "ok", "warn", "fail"

_MAX_BYTES = 4 << 20
_PLUGIN_NAME = "plugins/ws-gate.js"
_HOOK_SUFFIX = " || exit 2"
_MISSING = object()
_LOCK_WAIT = 10.0  # seconds to wait for another ws that holds the lock beside the config
_WS_HOOK = re.compile(
    r"\b(gate (check|event)|session (start|nudge)|notify alert)\b.*--runner\b"
)  # every hook command ws writes


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


def dropped_ops(entry: Mapping, ops) -> list:
    """The ops the record says the release asked for (`wanted_ops`) that `ops`, what it asks for now, no longer holds. A
    record from before `wanted_ops` existed has none to compare, so nothing is reported dropped."""
    before = entry.get("wanted_ops")
    if not isinstance(before, list):
        return []
    now = list(ops)
    return [op for op in before if isinstance(op, dict) and op not in now]


def _names(ops) -> str:
    """The places in the file these ops touch, each once, a few at most."""
    seen: list = []
    for op in ops:
        where = ".".join(str(x) for x in op["path"]) if isinstance(op.get("path"), list) else "?"
        if where not in seen:
            seen.append(where)
    return ", ".join(seen[:4]) + (", ..." if len(seen) > 4 else "")


# ---- what ws wants in each runner's file ------------------------------------------------------------------------------------


def _claude_ops(files: Mapping[str, str]) -> list[dict]:
    conf = json.loads(files["settings.json"])
    ops: list[dict] = []
    for event, groups in conf.get("hooks", {}).items():
        ops += [{"op": "append", "path": ["hooks", event], "value": g} for g in groups]
    deny = conf.get("permissions", {}).get("deny", [])
    ops += [{"op": "append", "path": ["permissions", "deny"], "value": rule} for rule in deny]
    # WS_GATED_SESSION is left out: here it would also be set in the person's own Claude shell and block `ws approve`
    env = {k: v for k, v in conf.get("env", {}).items() if k != ENV_MARKER}
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


def _notice(where: Path, backup: Path | None) -> str:
    out = (
        "ws: the gate did not run, so this call is blocked. To get unstuck, run `ws install --gate launch`, or delete "
        f"the PreToolUse group whose command contains `gate check` from {where}"
    )
    if backup:
        out += f" (a saved copy of your file is at {backup})"
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


def require_stable_interpreter(cmd) -> None:
    """Refuse a hook interpreter that comes and goes (a project `.venv`, a worktree, a temp folder). The hook would stay
    in the person's config long after that folder is gone, and every governed call would then be blocked."""
    why = selfcmd.unstable(list(cmd))
    if why:
        raise WsError(
            "refused",
            f"{why}; hooks that run it would go into every session and break when it goes. ws changed nothing",
            "install ws with install.sh, pipx or `uv tool install` first, then run `ws install` from that copy; or use "
            "`ws install --gate launch`",
        )


def plan(runner: str, pol: policy.Policy, policy_file: Path, env: Mapping[str, str], entry: Mapping | None) -> GatePlan:
    """The generated files and the merge for this runner. Pure but for reading the target (to name the backup)."""
    adapter = adapters.get(runner)
    cmd = selfcmd.argv()
    require_stable_interpreter(cmd)
    where = target(runner, env)
    out = _ws_home(env) / "gate" / runner
    if entry is not None:
        backup = Path(entry["backup"]) if entry.get("backup") else None
    else:
        backup = _new_backup(runner, env) if os.path.lexists(where) else None
    copies: dict = {}
    if runner == "claude-code":
        tree = selfcmd.tree()
        protect = (str(where), *([str(tree)] if tree else []))
        notice = _notice(where, backup)
        # `ws gate launch` runs inside the wall, so its file keeps the wide project locks; the user's own config is read
        # by every session, so it gets the narrow set (see USER_LOCKS)
        files = _tune(adapter.generate(pol, cmd, out, policy_file, protect=protect), notice)
        ops = _claude_ops(_tune(adapter.generate(pol, cmd, out, policy_file, protect=protect, user_scope=True), notice))
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
        earlier = earlier_items(doc, m)  # ws's items with no record: applying records them
        _, added, made = update(doc, earlier, [], m.ops, m.path)
        if not added and not made:
            return "unchanged"
        if earlier:
            return "changed"
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


# ---- one writer at a time, and a write that checks what it replaces ---------------------------------------------------------


class _Changed(Exception):
    """The file was changed by someone else between ws reading it and replacing it."""


def _take_lock(fcntl, lock: Path, path: Path) -> int | None:
    """An open, exclusively locked descriptor on `lock`, or None where a lock can't be made (a read-only folder, a file
    system without locks): the checked write below still protects the file. Waits up to `_LOCK_WAIT` seconds."""
    deadline = time.monotonic() + _LOCK_WAIT
    while True:
        try:
            fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o600)
        except OSError:
            return None
        try:
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (BlockingIOError, PermissionError):
                    if time.monotonic() >= deadline:
                        raise WsError(
                            "conflict",
                            f"another ws holds the lock on {path} ({lock}); ws changed nothing",
                            "wait for it to finish and run the command again",
                        ) from None
                    time.sleep(0.05)
            try:
                if os.fstat(fd).st_ino == os.stat(lock).st_ino:
                    return fd
            except FileNotFoundError:
                pass  # the holder before us removed the lock file: take the new one
        except OSError:
            os.close(fd)
            return None
        except BaseException:
            os.close(fd)
            raise
        os.close(fd)


@contextlib.contextmanager
def _locked(path: Path):
    """Hold an exclusive lock on a file beside `path` while ws reads it, merges and replaces it, so two ws runs (an
    install and a setup screen) take turns. The runner doesn't take the lock, so `_replace` checks the file as well."""
    try:
        import fcntl
    except ImportError:  # native Windows: the doctor runs there, installs don't
        yield
        return
    lock = path.parent / f".{path.name}.ws-lock"
    fd = _take_lock(fcntl, lock, path) if path.parent.is_dir() else None
    try:
        yield
    finally:
        if fd is not None:
            with contextlib.suppress(OSError):
                os.unlink(lock)  # still holding the lock, so nobody else has it
            os.close(fd)


def locked(path: Path):
    """The lock beside `path`, for a caller (the install engine) that must keep it until it has saved its manifest. Pass
    `held=True` to `apply` and `withdraw` while holding it: the lock is not re-entrant."""
    return _locked(path)


def _disk_sha(path: Path) -> str | None:
    try:
        return _sha(path.read_bytes())
    except FileNotFoundError:
        return None
    except OSError:
        return "unreadable"


def _replace(path: Path, data: bytes, based_on: bytes | None) -> None:
    """Replace the file with `data`, if it is still the bytes `based_on` that ws merged into; else raise `_Changed`."""
    if _disk_sha(path) != (None if based_on is None else _sha(based_on)):
        raise _Changed
    _write(path, data)


# ---- adopting what an earlier ws left --------------------------------------------------------------------------------------


def _is_ws_group(group) -> bool:
    hooks = group.get("hooks") if isinstance(group, dict) else None
    return isinstance(hooks, list) and any(
        isinstance(h, dict) and isinstance(h.get("command"), str) and _WS_HOOK.search(h["command"]) for h in hooks
    )


def earlier_items(doc: dict, m: Merge) -> list:
    """The items an earlier ws left in the file when its record is lost, as ops. A hook is ws's when its command runs
    `gate check`, `gate event`, `session start|nudge` or `notify alert` with `--runner`: that holds for a command from an older ws or
    another Python too, so it is swapped in place. With such a hook in the file (or OpenCode's plugin beside it), the
    deny rules and env keys that equal what ws wants now are ws's too. None found means ws was never here."""
    found: list = []
    hooks = doc.get("hooks")
    if isinstance(hooks, dict):
        for event, groups in hooks.items():
            if isinstance(groups, list):
                found += [{"op": "append", "path": ["hooks", event], "value": g} for g in groups if _is_ws_group(g)]
    if not found and not (m.runner == "opencode" and (m.path.parent / _PLUGIN_NAME).is_file()):
        return []
    for op in m.ops:
        if op["path"][0] != "hooks" and present(doc, [op]):
            found.append(op)
    return found


# ---- applying ------------------------------------------------------------------------------------------------------------------


def _emptied_by(doc: dict, items: list, where: Path) -> list:
    """The containers (lists and objects, with the ones above them) that hold nothing once `items` are out of the doc:
    ws made them. Recorded for an adopted file, so an undo tidies them away as it does after a normal install."""
    paths: list = []
    for op in items:
        path = op["path"] if op["op"] == "append" else op["path"][:-1]
        paths += [path[:i] for i in range(1, len(path) + 1) if path[:i] not in paths]
    bare, _, _ = update(doc, items, paths, [], where)
    return [p for p in paths if _lookup(doc, p) is not _MISSING and _lookup(bare, p) is _MISSING]


def _merge_once(m: Merge, entry: Mapping | None, made_dirs: list, new_dirs: list) -> tuple[dict | None, Outcome]:
    doc, raw = load(m.path)
    old_added = list(entry.get("added", [])) if entry else []
    old_made = list(entry.get("made", [])) if entry else []
    adopted = False
    if entry is None:
        old_added = earlier_items(doc, m)
        adopted = bool(old_added)
        old_made = _emptied_by(doc, old_added, m.path)
    new, added, made = update(doc, old_added, old_made, m.ops, m.path)
    if entry is None and not added and not made:
        return None, Outcome("applied")
    first = entry is None and raw is not None and m.backup is not None
    backup = entry.get("backup") if entry else (str(m.backup) if m.backup and raw is not None else None)
    backup_sha = entry.get("backup_sha256") if entry else (_sha(raw) if raw is not None else None)
    if first:
        _write_backup(m, raw)
    if raw is None:
        missing = []
        p = m.path.parent
        while not p.exists():
            missing.append(p)
            p = p.parent
        for d in reversed(missing):
            d.mkdir()
            made_dirs.append(str(d))
            new_dirs.append(d)
    data = dump(new, raw)
    if data != raw:
        try:
            _replace(m.path, data, raw)
        except _Changed:
            if first:
                m.backup.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    m.backup.parent.rmdir()  # only if it is empty now
            raise
    if entry is None:
        exact = not adopted  # an adopted file already held ws's items: its bytes now are not ones to give back
    else:  # exact only if the file is what ws last wrote, and it was not changed between two installs before
        exact = entry.get("exact") is not False and (raw is None or _sha(raw) == entry.get("sha256"))
    kept = {
        "path": str(m.path),
        "kind": KIND,
        "created_by_ws": bool(entry.get("created_by_ws")) if entry else raw is None,  # ws made the file itself
        "runner": m.runner,
        "added": added,
        "made": made,
        "made_dirs": list(made_dirs),
        "wanted": wanted(m.ops),
        "wanted_ops": list(m.ops),
        "cmd": list(m.cmd),
        "sha256": _sha(data),
        "exact": exact,
        "backup": backup,
        "backup_sha256": backup_sha,
    }
    return kept, Outcome("applied")


def apply(m: Merge, entry: Mapping | None, env: Mapping[str, str], held: bool = False) -> tuple[dict | None, Outcome]:
    """Merge into the file. Returns (the manifest entry to keep, or None when there is nothing to record, the outcome).
    Under a lock, and with the file read again just before it is replaced: if it changed meanwhile, ws merges into the
    new bytes once more, then refuses and leaves the file as the other writer left it. `held`: the caller already holds
    `locked(m.path)` (and keeps it until its manifest is saved)."""
    if not m.ops:
        out = withdraw(entry, env, held=held) if entry else Outcome("gone")
        return (dict(entry) if out.action == "pending" else None), out
    _check_jsonc_sibling(m)
    made_dirs = list(entry.get("made_dirs", [])) if entry else []
    new_dirs: list = []
    try:
        for _ in range(2):
            with contextlib.nullcontext() if held else _locked(m.path):
                try:
                    return _merge_once(m, entry, made_dirs, new_dirs)
                except _Changed:
                    continue
        raise WsError(
            "conflict",
            f"{m.path} changed while ws was writing to it, twice; ws changed nothing",
            "close whatever else writes to it (a running session's settings screen?), then run `ws install` again",
        )
    except BaseException:
        for d in reversed(new_dirs):
            with contextlib.suppress(OSError):
                d.rmdir()
        raise


def _backup_ok(entry: Mapping, env: Mapping[str, str]) -> Path | None:
    """The recorded backup, if it is where ws keeps them and still holds the bytes ws saved."""
    b = entry.get("backup")
    if not isinstance(b, str) or not b:
        return None
    p = Path(os.path.normpath(b))
    if p.parent != backups_dir(env) or p.is_symlink() or not p.is_file():
        return None
    return p if _sha(p.read_bytes()) == entry.get("backup_sha256") else None


def _withdraw_once(entry: Mapping, env: Mapping[str, str], path: Path, dry_run: bool) -> Outcome:
    try:
        doc, raw = load(path)
    except WsError as e:
        return Outcome("pending", note=f"{e.message}; ws's entries are still in it. {e.hint}")
    if raw is None:
        return Outcome("gone", note=f"{path} is already gone")
    removed: list = []
    backup = _backup_ok(entry, env)
    as_written = _sha(raw) == entry.get("sha256")
    exact = as_written and entry.get("exact") is not False
    made_it = not entry.get("backup") and entry.get("created_by_ws", True) is not False
    if exact and (backup is not None or made_it):
        if not dry_run:
            if backup is None:
                if _disk_sha(path) != _sha(raw):
                    raise _Changed
                path.unlink()  # ws made the file
            else:
                _replace(path, backup.read_bytes(), raw)
                backup.unlink()
                removed.append(backup)
        word = "removed (ws made it)" if backup is None else "restored exactly"
        return Outcome("removed" if backup is None else "restored", removed, f"{path}: {word}")
    new, _, _ = update(doc, entry.get("added", []), entry.get("made", []), [], path)
    if not dry_run:
        _replace(path, dump(new, raw), raw)
    if not as_written:
        why = "you changed it since"
    elif entry.get("exact") is False:
        why = "it was changed between two installs, so the saved copy is out of date"
    else:
        why = "the saved copy is gone"
    return Outcome("trimmed", removed, f"{path}: {why}, so ws took out only its own entries and kept your edits")


def withdraw(entry: Mapping, env: Mapping[str, str], dry_run: bool = False, held: bool = False) -> Outcome:
    """Take ws's entries out of the file the manifest names. Exact when the file is as ws last wrote it and was never
    changed between two installs; surgical otherwise. The path must be the target for the runner now: the manifest can't
    point this elsewhere. `held`: the caller holds the lock and calls `tidy` itself once it lets go (the lock file sits
    in the folder `tidy` empties)."""
    runner = entry.get("runner")
    if runner not in RUNNERS or Path(os.path.normpath(str(entry.get("path")))) != target(runner, env):
        return Outcome("refused", note=f"{entry.get('path')}: not a user config file ws manages")
    path = target(runner, env)
    if not os.path.lexists(path):
        return Outcome("gone", note=f"{path} is already gone")
    for _ in range(2):
        with contextlib.nullcontext() if dry_run or held else _locked(path):
            try:
                out = _withdraw_once(entry, env, path, dry_run)
            except _Changed:
                continue
        if out.action in ("restored", "removed") and not dry_run and not held:
            out.removed += _tidy(path, entry, backups_dir(env))  # after the lock file is gone, so the folder is empty
        return out
    return Outcome("pending", note=f"{path} changed while ws was writing to it; ws's entries are still in it")


def tidy(path: Path, entry: Mapping, env: Mapping[str, str]) -> list:
    """What `withdraw` tidies up after itself, for a caller that held the lock: the backups folder and the folders ws
    made above the file, each only when empty."""
    return _tidy(path, entry, backups_dir(env))


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
    dropped: list = []
    try:
        want = plan(runner, pol, policy_file.resolve(), env, entry)
        stale = wanted(want.merge.ops) != entry.get("wanted")
        dropped = dropped_ops(entry, want.merge.ops)
    except (WsError, KeyError, ValueError, OSError):
        stale = True
    unsupported = [g for g in getattr(adapters.get(runner), "UNSUPPORTED_GUARDS", ()) if g in pol.guards.on]
    notes = [f"the guard `{g}` isn't run by {runner}" for g in unsupported]
    if stale:
        first = f"ws's entries in {where} are from an earlier policy"
        if dropped:
            first += f"; this release no longer asks for {len(dropped)} of them ({_names(dropped)})"
        return WARN, "; ".join([first, *notes]), "run `ws install`"
    detail = f"every session is gated: ws's entries are in {where}"
    return (WARN, "; ".join([detail, *notes]), "") if notes else (OK, detail, "")


def _rule(text: str) -> tuple[str, str | None]:
    """`Bash(git push:*)` -> ("Bash", "git push*"); `WebFetch` -> ("WebFetch", None). `:*` is Claude Code's prefix form."""
    m = re.fullmatch(r"([A-Za-z_]+)\((.*)\)", text, re.DOTALL)
    if not m:
        return text, None
    spec = m.group(2)
    return m.group(1), spec[:-2] + "*" if spec.endswith(":*") else spec


def _overrides(allow: str, deny: str) -> bool:
    """Does the deny rule take away some or all of what the allow rule grants? A deny wins over an allow in Claude Code.
    Same tool, and either rule's pattern matches the other's text (one is a case of the other), or the deny names the
    whole tool."""
    (a_tool, a_spec), (d_tool, d_spec) = _rule(allow), _rule(deny)
    if a_tool != d_tool:
        return False
    if d_spec is None or a_spec is None:
        return d_spec is None
    return fnmatch.fnmatchcase(a_spec, d_spec) or fnmatch.fnmatchcase(d_spec, a_spec)


def overridden(env: Mapping[str, str] | None = None) -> list[str]:
    """One line for each of your `permissions.allow` rules that a deny rule ws added now overrides (Claude Code lets a
    deny win), for doctor's WS008. [] when ws has added nothing to the file or the file doesn't read."""
    env = os.environ if env is None else env
    entry = _manifest_entry("claude-code", env)
    if entry is None:
        return []
    try:
        doc, _ = load(target("claude-code", env))
    except WsError:
        return []
    perms = doc.get("permissions")
    allow = perms.get("allow") if isinstance(perms, dict) else None
    ours = [op["value"] for op in entry.get("added", []) if op["path"] == ["permissions", "deny"]]
    lines = []
    for rule in allow if isinstance(allow, list) else []:
        hit = next((d for d in ours if isinstance(rule, str) and isinstance(d, str) and _overrides(rule, d)), None)
        if hit:
            lines.append(f"your allow rule `{rule}` is overridden by ws's deny rule `{hit}`")
    return lines
