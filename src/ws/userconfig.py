"""The gate in the runner's own user config: ws's deny rules and hooks added beside yours, recorded, and taken out again.

`ws install` with `gate = "every-session"` merges what `ws gate generate` makes into the file the runner reads in every
session (Claude Code's `settings.json`, OpenCode's `opencode.json`), so a session started the usual way is gated too.
The promises, each pinned by a test:

- Yours stays. Your hooks, permissions, env and keys ws does not use are never touched, and their order is kept, with
  one exception (the OpenCode rule order below). ws appends its items; it refuses, and changes nothing, when one of your
  values would be overridden (an `env` key you set to another value, or `permissions.disableBypassPermissionsMode`
  set to anything but "disable").
- Before the first write the file's own bytes go to `backups/` in ws's state folder (mode 0600).
- The manifest records the items ws added, as exact JSON, the items the release asked for (`wanted_ops`, so doctor can
  see a later release stop asking for one), and the hash of ws's last write.
- Undo is exact only when it can be. If the file is still what ws last wrote, and it was not changed between two
  `ws install` runs, your original bytes come back (or the file goes, when ws made it). If it was changed at any point
  after the first write, only the recorded items come out and your edits stay.
- Writes are checked. ws holds a lock file beside the config while it merges, and reads the file again just before it
  replaces it; a file that changed under it is merged again once, then refused.
- A config that already holds ws's hooks but has no record (the manifest was lost) is adopted: the entries are recorded
  so an undo takes them out, and a hook command from an older ws is replaced in place.
- The wall stays whole. A Claude Code settings file of yours that opens the sandbox (it is off, it excludes commands, it
  allows unsandboxed ones, or it re-opens a path the wall holds) is refused, naming each setting and its file, unless you
  keep them on purpose with `--allow-widened-sandbox`; ws then records the choice (`widened_by_user`), and doctor reports
  the wall as widened.
- The sandbox is not switched on for you. Entries an earlier ws wrote without the block (`WS_SANDBOXED` "0") stay without
  it, on `ws install` and `ws upgrade` alike, unless the policy file itself says `sandbox = true` (`keeps_sandbox_off`).
- OpenCode takes the last rule that matches, so ws's denies come last. In a `permission.<tool>` map every key ws denies
  sits after all of yours, on a first install and on every re-apply. A key of yours that is the same as one ws denies
  moves to the end with ws's keys (it is a deny already, so your rule only gets stricter); it is not recorded as ws's,
  so withdrawing leaves it where it is. ws's `ask` keys go in before its first `deny` key (a later change of policy that
  adds an ask moves it ahead of the denies), and an `ask` that would sit after a `deny` of yours that it overlaps is
  left out: a rule added at the end must never loosen a deny.
- A file ws cannot merge safely (a link, not plain JSON, JSONC with comments) is refused with the reason and the way to
  gate a session without it: `ws gate launch`.

Python 3.11+, standard library only.
"""

from __future__ import annotations

import contextlib
import copy
import dataclasses
import fnmatch
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import adapters, paths, policy, profile, selfcmd, wallsettings
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
    r"\b(?:(?:gate (?:check|event)|session (?:start|nudge)|notify alert)\b.*--runner\b|msg wait --session-hook\b)"
)  # every hook command ws writes (the message wake hook has no --runner)


@dataclass(frozen=True)
class Merge:
    """What to merge into one user config file. `ops` empty means take ws's entries out."""

    runner: str
    path: Path
    ops: tuple = ()
    backup: Path | None = None  # where the file's own bytes go on the first write; None when there is no file yet
    cmd: tuple = ()  # the argv that runs ws in the hooks (recorded, so doctor can see it still starts)
    inputs: str = ""  # a hash of the policy these ops were built from (see `policy_digest`)
    widened: tuple = ()  # the wall-widening settings of yours that you chose to keep (recorded as `widened_by_user`)


@dataclass(frozen=True)
class GatePlan:
    files: dict  # name -> text for gate/<runner>/ in ws's state folder
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


def _state(env: Mapping[str, str]) -> Path:
    return Path(os.path.abspath(paths.state_dir(dict(env))))


def backups_dir(env: Mapping[str, str]) -> Path:
    return _state(env) / "backups"


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
                key = _dotted(path[: i + 1])
                if path[0] == "sandbox":  # the wall's own block: say which key to fix, in which file, and the command
                    raise WsError(
                        "sandbox_shape",
                        f"{where}: `{key}` is not {kind}, so ws can't add the sandbox settings to it; ws changed nothing",
                        f"make `{key}` {kind} in {where}, or delete it, then run `ws install` again; or start gated "
                        "sessions with `ws gate launch`",
                        details={"file": str(where), "key": key},
                    )
                raise WsError(
                    "refused",
                    f"{where}: `{key}` is not {kind}, so ws can't add to it; ws changed nothing",
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
    an op whose item is already there (yours) takes no effect and is not recorded. In an OpenCode `permission.<tool>` map the
    keys ws denies then go after all the others (`_denies_last`). `ops` empty takes ws's items out."""
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
                key = _dotted(op["path"])
                message = (
                    f"{where}: you have `{key}` as {json.dumps(now)}, and ws needs "
                    f"{json.dumps(op['value'])}; ws changed nothing"
                )
                if op["path"][0] == "sandbox" or tuple(op["path"]) == ("permissions", wallsettings.BYPASS_KEY):
                    raise WsError(  # a wall setting of yours that ws would have to change
                        "sandbox_conflict",
                        message,
                        f"set `{key}` to {json.dumps(op['value'])} in {where}, or delete it, then run `ws install` "
                        "again; or start gated sessions with `ws gate launch`",
                        details={"file": str(where), "key": key},
                    )
                raise WsError(
                    "conflict",
                    message,
                    "change that value to match, or start gated sessions with `ws gate launch`",
                )
            added.append(op)
    _sweep(doc)
    _denies_last(doc, ops)
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


def _denies_last(doc: dict, ops) -> None:
    """OpenCode takes the last rule that matches: put the keys ws denies after every other key of the same
    `permission.<tool>` map, in the order of `ops`. A key of yours that equals one of them moves with them (it is the same
    deny, so it only gets stricter), so a broader `allow` of yours can never sit after a deny of ws's."""
    blocks: dict[tuple, list] = {}
    for op in ops:
        path = op["path"]
        if op["op"] == "set" and len(path) == 3 and path[0] == "permission" and op["value"] == "deny":
            blocks.setdefault(tuple(path[:2]), []).append(path[2])
    for path, keys in blocks.items():
        rules = _lookup(doc, list(path))
        if not isinstance(rules, dict):
            continue
        last = [k for k in dict.fromkeys(keys) if k in rules]
        order = [*(k for k in rules if k not in last), *last]
        moved = {k: rules[k] for k in order}
        rules.clear()
        rules.update(moved)


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


def policy_digest(pol: policy.Policy) -> str:
    """A hash of everything in the policy, so doctor can tell a change of policy from a change of release."""
    return _sha(json.dumps(dataclasses.asdict(pol), sort_keys=True, default=str).encode("utf-8"))


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
    ops += [
        {"op": "set", "path": ["env", k], "value": v} for k, v in env.items()
    ]  # WS_SANDBOXED: 1, or 0 with no block
    bypass = conf.get("permissions", {}).get(wallsettings.BYPASS_KEY)  # one value, here as in the launch settings
    if bypass is not None:
        ops.append({"op": "set", "path": ["permissions", wallsettings.BYPASS_KEY], "value": bypass})
    block = conf.get("sandbox")  # the runner's own sandbox: the wall; the policy's `sandbox = false` leaves it out
    if isinstance(block, dict):
        ops += _sandbox_ops(block, ["sandbox"])
    return ops


def _sandbox_ops(node: dict, path: list[str]) -> list[dict]:
    """The ops for the `sandbox` block: a setting is set (and ws refuses, saying so, when yours differs), a list gets
    ws's items appended after yours."""
    ops: list[dict] = []
    for key, value in node.items():
        if isinstance(value, dict):
            ops += _sandbox_ops(value, [*path, key])
        elif isinstance(value, list):
            ops += [{"op": "append", "path": [*path, key], "value": v} for v in value]
        else:
            ops.append({"op": "set", "path": [*path, key], "value": value})
    return ops


def _opencode_ops(files: Mapping[str, str]) -> list[dict]:
    """The `ask` and `deny` rules of the generated config, in its order: OpenCode takes the last rule that matches, so
    the asks are written before the denies and a deny beats an ask on the same command. The catch-all `allow` is yours."""
    conf = json.loads(files["opencode.json"])
    ops: list[dict] = []
    for tool, rules in conf.get("permission", {}).items():
        if isinstance(rules, dict):
            for act in ("ask", "deny"):
                ops += [
                    {"op": "set", "path": ["permission", tool, pat], "value": act}
                    for pat, given in rules.items()
                    if given == act
                ]
        elif rules == "deny":
            ops.append({"op": "set", "path": ["permission", tool], "value": "deny"})
    return ops


def _wild(pattern: str) -> re.Pattern:
    """OpenCode's wildcard as a regex: `*` is anything, `?` one character, and a trailing ` *` also matches the bare
    command."""
    body = re.escape(pattern).replace(r"\*", ".*").replace(r"\?", ".")
    if body.endswith(r"\ .*"):
        body = body[: -len(r"\ .*")] + r"(\ .*)?"
    return re.compile(body, re.DOTALL)


def _asks_yours_would_override(ops: list[dict], where: Path, entry: Mapping | None) -> list[dict]:
    """`ops` without the `ask` rules that would sit after a `deny` rule of yours that they overlap: a rule added at the end
    wins in OpenCode, so the ask would loosen your deny. The hook still asks for the approval, so nothing is lost. A file
    that does not read is left for the merge to refuse."""
    try:
        doc, _ = load(where)
    except WsError:
        return ops
    ours = {tuple(op["path"]) for op in (entry or {}).get("added", [])}
    kept = []
    for op in ops:
        path = op["path"]
        rules = _lookup(doc, path[:2]) if op["value"] == "ask" and len(path) == 3 else None
        yours = (
            [pat for pat, act in rules.items() if act == "deny" and (*path[:2], pat) not in ours]
            if isinstance(rules, dict)
            else []
        )
        if any(_wild(path[2]).fullmatch(k) or _wild(k).fullmatch(path[2]) for k in yours):
            continue
        kept.append(op)
    return kept


def _asks_before_denies(doc: dict, added: list) -> None:
    """Move ws's `ask` keys to just before ws's first `deny` key in the same block when a later change of policy added
    them after it (a new key goes to the end of the block, and the last match wins). Your keys never move past each
    other; only ws's own ask keys change place."""
    blocks = {tuple(op["path"][:2]) for op in added if op["op"] == "set" and len(op["path"]) == 3}
    for path in blocks:
        rules = _lookup(doc, list(path))
        if not isinstance(rules, dict):
            continue
        mine = {op["path"][2]: op["value"] for op in added if op["op"] == "set" and tuple(op["path"][:2]) == path}
        keys = list(rules)
        denies = [keys.index(k) for k, v in mine.items() if v == "deny" and k in rules]
        if not denies:
            continue
        first = min(denies)
        late = [k for i, k in enumerate(keys) if mine.get(k) == "ask" and i > first]
        if not late:
            continue
        rest = [k for k in keys if k not in late]
        at = rest.index(keys[first])
        order = [*rest[:at], *late, *rest[at:]]
        moved = {k: rules[k] for k in order}
        rules.clear()
        rules.update(moved)


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


def _claude_protect(where: Path) -> tuple[str, ...]:
    tree = selfcmd.tree()
    return (str(where), *([str(tree)] if tree else []))


def expected_sandbox(pol: policy.Policy, policy_file: Path, env: Mapping[str, str]) -> dict:
    """The `sandbox` block ws merges into Claude Code's user settings for this policy ({} when the policy leaves it out)."""
    if not pol.gate.sandbox:
        return {}
    where = target("claude-code", env)
    out = _state(env) / "gate" / "claude-code"
    return adapters.get("claude-code").sandbox_block(pol, out, policy_file, _claude_protect(where))


def _kept_widenings(
    where: Path, block: dict, env: Mapping[str, str], entry: Mapping | None, allow: bool
) -> list[wallsettings.Widening]:
    """The settings in your file that widen the wall and that you have agreed to keep: all of them with `allow`, else the
    ones an earlier install recorded. Any other raises the refusal, so nothing is merged beside a wider wall unasked."""
    if not block:
        return []
    doc, _ = load(where)
    found = wallsettings.widenings(doc, where, block, wallsettings._home(env))
    agreed = set(entry.get("widened_by_user", [])) if entry else set()
    new = [w for w in found if not allow and w.label not in agreed]
    if new:
        raise wallsettings.refusal(new, where)
    return found


def had_sandbox(entry: Mapping | None, where: Path) -> bool | None:
    """Did the ws entries already in the runner's user config come with the sandbox block? False for `WS_SANDBOXED` "0"
    (an install made before the block was on by default, or with `sandbox = false`), True for "1", None when it can't be
    told. The manifest record is read first, then the file itself when it holds ws's hooks (a record that was lost). A
    `WS_SANDBOXED` of the person's own, in a file ws has not written to, says nothing."""
    for op in (entry or {}).get("added", []):
        if isinstance(op, dict) and op.get("path") == ["env", "WS_SANDBOXED"]:
            return op.get("value") == "1"
    try:
        doc, _ = load(where)
    except WsError:
        return None
    if not _holds_ws_hooks(doc):
        return None
    env = doc.get("env") if isinstance(doc.get("env"), dict) else {}
    return {"0": False, "1": True}.get(env.get("WS_SANDBOXED"))


def _hook_commands(doc: dict) -> list[str]:
    """Every hook command in a Claude Code settings document, whoever wrote it."""
    groups = [g for gs in (doc.get("hooks") or {}).values() if isinstance(gs, list) for g in gs]
    hooks = [h for g in groups if isinstance(g, dict) for h in g.get("hooks") or [] if isinstance(h, dict)]
    return [h["command"] for h in hooks if isinstance(h.get("command"), str)]


def _holds_ws_hooks(doc: dict) -> bool:
    return isinstance(doc.get("hooks"), dict) and any(_WS_HOOK.search(c) for c in _hook_commands(doc))


def _policy_names_sandbox(policy_file: Path) -> bool:
    """Does the policy file itself have a `sandbox` key under `[gate]`? (`Policy.gate.sandbox` is True by default, and that
    cannot tell a default from a choice.)"""
    try:
        gate = tomllib.loads(policy_file.read_text(encoding="utf-8")).get("gate")
    except (OSError, ValueError):
        return False
    return isinstance(gate, dict) and "sandbox" in gate


def keeps_sandbox_off(pol: policy.Policy, policy_file: Path, entry: Mapping | None, where: Path) -> bool:
    """True when the sandbox stays out of the user config. The block is on by default, but a newer ws must not switch on
    what the person did not have: ws entries that were written without it keep going without it, unless the policy file
    says `sandbox = true` itself. A sandbox that cannot start makes Claude Code refuse to start, which is what an
    unasked-for block did to sessions once."""
    if not pol.gate.sandbox or _policy_names_sandbox(policy_file):
        return False
    return had_sandbox(entry, where) is False


def plan(
    runner: str,
    pol: policy.Policy,
    policy_file: Path,
    env: Mapping[str, str],
    entry: Mapping | None,
    allow_widened: bool = False,
) -> GatePlan:
    """The generated files and the merge for this runner. Pure but for reading the target (to name the backup). Raises
    the refusal in `wallsettings.refusal` when your Claude Code settings widen the wall and `allow_widened` is false (and
    an earlier install didn't record them). Entries an earlier ws wrote without the sandbox block stay without it (see
    `keeps_sandbox_off`)."""
    adapter = adapters.get(runner)
    cmd = selfcmd.argv()
    require_stable_interpreter(cmd)
    where = target(runner, env)
    digest = policy_digest(pol)  # of the policy as written, whatever the sandbox does below
    if runner == "claude-code" and keeps_sandbox_off(pol, policy_file, entry, where):
        pol = dataclasses.replace(pol, gate=dataclasses.replace(pol.gate, sandbox=False))
    out = _state(env) / "gate" / runner
    pin = paths.pin_of(env)  # the hooks carry the layout this install resolved
    if entry is not None:
        backup = Path(entry["backup"]) if entry.get("backup") else None
    else:
        backup = _new_backup(runner, env) if os.path.lexists(where) else None
    copies: dict = {}
    widened: list[wallsettings.Widening] = []
    if runner == "claude-code":
        protect = _claude_protect(where)
        widened = _kept_widenings(where, expected_sandbox(pol, policy_file, env), env, entry, allow_widened)
        notice = _notice(where, backup)
        # `ws gate launch` runs inside the wall, so its file keeps the wide project locks; the user's own config is read
        # by every session, so it gets the narrow set (see USER_LOCKS)
        files = _tune(adapter.generate(pol, cmd, out, policy_file, protect=protect, pin=pin), notice)
        ops = _claude_ops(
            _tune(adapter.generate(pol, cmd, out, policy_file, protect=protect, user_scope=True, pin=pin), notice)
        )
        # a switch of yours that ws would set back (the sandbox off) is left as you have it, once you've agreed to keep it
        own = {w.path for w in widened if not w.item}
        ops = [op for op in ops if tuple(op["path"]) not in own]
    else:
        plug = plugin_path(env)
        files = adapter.generate(pol, cmd, out, policy_file, protect=(str(where), str(plug)), pin=pin)
        ops = _asks_yours_would_override(_opencode_ops(files), where, entry)
        copies[plug] = files[adapter.PLUGIN]
    kept = tuple(sorted({w.label for w in widened}))
    return GatePlan(files, Merge(runner, where, tuple(ops), backup, tuple(cmd), digest, kept), copies)


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
    same_choice = list(entry.get("widened_by_user", [])) == list(m.widened)  # the record of what you chose to keep
    if (
        raw is not None
        and entry.get("wanted") == wanted(m.ops)
        and present(doc, entry.get("added", []))
        and same_choice
    ):
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
    if m.runner == "opencode":
        _asks_before_denies(new, added)
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
        "inputs": m.inputs,
        "widened_by_user": list(m.widened),
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
    same_policy = False
    try:
        want = plan(runner, pol, policy_file.resolve(), env, entry, allow_widened=True)
        stale = wanted(want.merge.ops) != entry.get("wanted")
        dropped = dropped_ops(entry, want.merge.ops)
        same_policy = entry.get("inputs") == want.merge.inputs
    except (WsError, KeyError, ValueError, OSError):
        stale = True
    unsupported = [g for g in getattr(adapters.get(runner), "UNSUPPORTED_GUARDS", ()) if g in pol.guards.on]
    notes = [f"the guard `{g}` isn't run by {runner}" for g in unsupported]
    if stale:
        first = f"ws's entries in {where} are from an earlier policy"
        if dropped and same_policy:
            first += f"; this release no longer asks for {len(dropped)} of them ({_names(dropped)})"
        elif dropped:
            first += f"; the current policy and release no longer ask for {len(dropped)} of them ({_names(dropped)})"
        return WARN, "; ".join([first, *notes]), "run `ws install`"
    detail = f"every session is gated: ws's entries are in {where}"
    return (WARN, "; ".join([detail, *notes]), "") if notes else (OK, detail, "")


def holds_ws_entries(runner: str, env: Mapping[str, str]) -> bool:
    """Does the runner's user config hold ws's entries? Claude Code: its hooks are in the file, whether or not the install
    record is there. OpenCode: its config carries no mark of ws's (the rules are plain `permission` keys) and the plugin
    file stays behind when ws withdraws, so only the install record says so. Never a file beside the config."""
    try:
        if runner == "claude-code":
            return _holds_ws_hooks(load(target(runner, env))[0])
        return _manifest_entry(runner, env) is not None
    except (WsError, OSError):
        return False


@dataclass
class HookState:
    """What `hook_state` found: the runners whose hooks carry no pin, and the ones that could not be looked at (name ->
    why, with the way on)."""

    old: list[str] = field(default_factory=list)
    unreadable: dict[str, str] = field(default_factory=dict)


def hook_state(env: Mapping[str, str] | None = None) -> HookState:
    """Look at the hooks ws put in each runner's user config. `old`: the runners whose ws hooks carry no `--pin` (written
    by a ws older than the one running, which pinned ws's folders in every hook only later), each once, in RUNNERS order.
    That includes OpenCode when it holds a withdrawn plugin of that kind (`withdrawn_plugin`): `ws upgrade` takes it out.
    `unreadable`: a runner ws has a record of whose config ws can't read (a link, JSONC, not JSON), so what its hooks
    carry is not known. Left out of both: a runner with no ws hooks in its file, one that ws has no record of and the
    profile does not name, and an OpenCode plugin with no record (ws withdrew from that config and left the plugin) that
    is current: `ws upgrade` rewrites the runners it knows, and this must not point at it for one it would leave alone."""
    env = os.environ if env is None else env
    try:
        named = _profile_runner(env)[0]
    except profile.ProfileError:
        named = ""
    state = HookState()
    for runner in RUNNERS:
        recorded = _manifest_entry(runner, env) is not None
        if not recorded and (runner != named or runner == "opencode"):
            if runner == "opencode" and withdrawn_plugin(env) is not None:
                state.old.append(runner)
            continue
        try:
            if runner == "claude-code":
                try:
                    doc, _ = load(target(runner, env))
                except WsError as e:
                    if recorded:
                        state.unreadable[runner] = f"{e.message}. {e.hint}".strip()
                    continue
                old = any(_WS_HOOK.search(c) and "--pin" not in c.split() for c in _hook_commands(doc))
            else:
                text = plugin_path(env).read_text(encoding="utf-8")
                old = '"--pin"' not in text
        except (OSError, ValueError, AttributeError, TypeError):
            continue
        if old:
            state.old.append(runner)
    return state


def hooks_without_pin(env: Mapping[str, str] | None = None) -> list[str]:
    """The runners whose ws hooks carry no `--pin` (`hook_state(env).old`)."""
    return hook_state(env).old


def withdrawn_plugin(env: Mapping[str, str]) -> Path | None:
    """OpenCode's plugin file, when it is one ws put there, ws has since withdrawn from OpenCode's config (the install
    record holds the file but no merge into the config), and it is an older one that carries no `--pin`: left in place it
    is still loaded, and on a layout that is not the default every call it judges is refused. None for anything else: no
    such record, a file that is not a plain file, one that ws did not make or one that is not ws's own (it must be as ws
    last wrote it, byte for byte: an edited file is the person's), and one that is current."""
    from ws import install  # late: ws.install imports this module

    plug = plugin_path(env)
    entries, _ = install._read_manifest(env)
    rec = entries.get(str(plug))
    if _manifest_entry("opencode", env) is not None or not rec or rec.get("kind") != "file":
        return None
    if rec.get("created_by_ws") is not True or plug.is_symlink() or not plug.is_file():
        return None
    try:
        data = plug.read_bytes()
        text = data.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    return plug if _sha(data) == rec.get("sha256") and '"--pin"' not in text else None


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
