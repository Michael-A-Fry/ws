"""`ws migrate`: move ws's files from `~/.ws` to the XDG folders (see `ws.paths` for which file goes where).

    ws migrate [--dry-run] [--json]

An earlier ws kept everything in one folder, `~/.ws`. Nothing moves by itself: ws keeps using that folder until you run
this, and `ws doctor` (WS010) says so. The move, in order:

  1. Plan it file by file from the table in `ws.paths`. An entry ws doesn't know stays where it is and is listed.
  2. Refuse, changing nothing, if a target already exists and differs from its source.
  3. Copy each file (the mode and times kept, so the secrets store stays 0600 and the approvals folder 0700), then check
     every copy by its SHA-256 and every link by its target.
  4. Write a manifest of what moved to `migrate-manifest.json` in the state folder (0600), and move the install record's
     paths to the new folders.
  5. Move the secrets store by rename (on one filesystem), so no copy of a secret stays behind. If the data folder is on
     another filesystem it is copied (0700/0600), checked, and the old copy removed. Then rename `~/.ws` to
     `~/.ws.migrated-DATE`. That folder is never deleted: remove it yourself once you've looked. The gate and the wall
     treat it as ws's own, so a session can't read or change what is left in it.
  6. Refresh what ws put on the machine (`ws install`), because the runners' hooks name the policy by its path, and
     switch the timers off before the move and on again after it (they name the old folder).

It is safe to run again: a finished move says so, and an interrupted one picks up where it stopped (files already in
place and equal to their source are left as they are). With `WS_HOME` or a `WS_*_DIR` variable set there is nothing to
migrate: ws uses the folders you named. Exit 0 when done (or nothing to do), 1 when it was refused or stopped. It refuses while the secrets broker or a gated ws
session is running, because those hold files that move.
Python 3.11+, standard library only.
"""

from __future__ import annotations

import argparse
import contextlib
import errno
import hashlib
import json
import os
import socket
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import __version__, output, paths
from ws.output import WsError

SCHEMA = 1
MANIFEST = "migrate-manifest.json"
INSTALL_MANIFEST = "install-manifest.json"
_CHUNK = 1 << 20


@dataclass
class Item:
    """One thing to put in place. `kind` is "dir", "file" or "link"; `action` is "copy", "present" (already there) or
    "renamed" (the secrets store, moved whole)."""

    src: Path
    dst: Path
    kind: str
    mode: int
    action: str = "copy"
    size: int = 0
    sha256: str = ""
    target: str = ""  # a link's target


@dataclass
class Plan:
    state: str  # "move" | "done" | "nothing" | "single"
    source: Path
    items: list[Item] = field(default_factory=list)
    left: list[str] = field(
        default_factory=list
    )  # in the old folder and not moved: unknown names, sockets and the like
    conflicts: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def copies(self) -> list[Item]:
        return [i for i in self.items if i.action == "copy"]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(_CHUNK):
            h.update(chunk)
    return h.hexdigest()


def _entry(src: Path, dst: Path) -> Item | None:
    """The item for one path, or None for something that can't be copied (a socket, a pipe, a device)."""
    st = os.lstat(src)
    mode = stat.S_IMODE(st.st_mode)
    if stat.S_ISLNK(st.st_mode):
        return Item(src, dst, "link", mode, target=os.readlink(src))
    if stat.S_ISDIR(st.st_mode):
        return Item(src, dst, "dir", mode)
    if stat.S_ISREG(st.st_mode):
        return Item(src, dst, "file", mode, size=st.st_size, sha256=_sha256(src))
    return None


def _check_target(item: Item) -> str:
    """ "" when nothing is in the way, "present" when the target already equals the source, else why it can't be used."""
    dst = item.dst
    if not os.path.lexists(dst):
        return ""
    if item.kind == "dir":
        return "present" if dst.is_dir() and not dst.is_symlink() else f"{dst} exists and is not a folder"
    if item.kind == "link":
        same = dst.is_symlink() and os.readlink(dst) == item.target
        return "present" if same else f"{dst} exists and is not the same link"
    if dst.is_symlink() or not dst.is_file():
        return f"{dst} exists and is not a file"
    return "present" if _sha256(dst) == item.sha256 else f"{dst} exists and differs from {item.src}"


def _walk(src: Path, dst: Path, left: list[str]) -> list[Item]:
    top = _entry(src, dst)
    if top is None:
        left.append(f"{src} (not a file, folder or link)")
        return []
    out = [top]
    if top.kind == "dir":
        for child in sorted(src.iterdir()):
            out += _walk(child, dst / child.name, left)
    return out


def plan(env: Mapping[str, str] | None = None) -> Plan:
    """What `ws migrate` would do. Reads the disk and changes nothing."""
    e = os.environ if env is None else env
    pr = paths.probe(e)
    if pr.state == "single":
        return Plan(
            "single",
            pr.legacy,
            message="WS_HOME (or a WS_*_DIR variable) names ws's folders, so there is nothing to migrate",
        )
    if not pr.legacy.is_dir():
        if os.path.lexists(pr.legacy):
            raise WsError(
                "bad_config",
                f"{pr.legacy} exists and is not a folder",
                "move it aside; ws keeps its files elsewhere now",
            )
        if pr.owned:
            return Plan("done", pr.legacy, message="already migrated: ws's files are in the XDG folders")
        return Plan("nothing", pr.legacy, message=f"nothing to migrate: there is no {pr.legacy}")
    lay = pr.xdg
    out = Plan("move", pr.legacy)
    for child in sorted(pr.legacy.iterdir()):
        kind = paths.kind_of(child.name)
        if kind is None:
            out.left.append(f"{child} (ws doesn't know this name)")
            continue
        out.items += _walk(child, lay.dir(kind) / child.name, out.left)
    for item in out.items:
        why = _check_target(item)
        if why == "present":
            item.action = "present"
        elif why:
            out.conflicts.append(why)
    return out


def _unused_name(base: Path) -> Path:
    if not os.path.lexists(base):
        return base
    n = 2
    while os.path.lexists(f"{base}-{n}"):
        n += 1
    return Path(f"{base}-{n}")


def _open_new(tmp: Path) -> int:
    """A new file at `tmp`, private (0600), that is never anything that was there: a name left by an interrupted run, or
    planted (a link, or a second name for another file), is removed first, and `O_EXCL` refuses to open anything but a
    file this call makes."""
    with contextlib.suppress(FileNotFoundError):
        os.unlink(tmp)
    return os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _copy_file(item: Item) -> None:
    dst = item.dst
    tmp = dst.with_name(f".{dst.name}.migrate-tmp")
    fd = _open_new(tmp)  # private until its mode is set
    try:
        with os.fdopen(fd, "wb") as out, open(item.src, "rb") as src:
            while chunk := src.read(_CHUNK):
                out.write(chunk)
        os.chmod(tmp, item.mode)
        st = os.stat(item.src)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
        os.replace(tmp, dst)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _make_roots(lay: paths.Layout) -> None:
    for d in lay.distinct():
        d.mkdir(mode=0o700, parents=True, exist_ok=True)


def _copy(p: Plan, lay: paths.Layout) -> None:
    """Make every copy. Folders are made 0700 first and given their own mode last, so a read-only source folder doesn't
    stop its files going in."""
    _make_roots(lay)
    dirs: list[Item] = []
    for item in p.copies:
        if item.kind == "dir":
            item.dst.mkdir(mode=0o700, parents=True, exist_ok=True)
            dirs.append(item)
        elif item.kind == "link":
            item.dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            os.symlink(item.target, item.dst)
        else:
            item.dst.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            _copy_file(item)
    for item in p.items:
        if item.action == "present" and item.kind == "file":
            os.chmod(item.dst, item.mode)  # an earlier, interrupted run may have left the mode unset
    for item in reversed(dirs):
        os.chmod(item.dst, item.mode)


def _verify(p: Plan) -> list[str]:
    """Why a copy isn't the source, for each one that isn't (an empty list when every file, link and mode matches)."""
    bad = []
    for item in p.items:
        dst = item.dst
        if item.kind == "file":
            if dst.is_symlink() or not dst.is_file() or _sha256(dst) != item.sha256:
                bad.append(f"{dst} does not match {item.src}")
            elif stat.S_IMODE(os.stat(dst).st_mode) != item.mode:
                bad.append(f"{dst} has a different mode from {item.src}")
        elif item.kind == "link":
            if not dst.is_symlink() or os.readlink(dst) != item.target:
                bad.append(f"{dst} is not the link {item.src} was")
        elif not dst.is_dir() or dst.is_symlink():
            bad.append(f"{dst} is not a folder")
    return bad


def _remove_sources(items: list[Item]) -> None:
    """Take the old copies away once the new ones are checked: files and links first, then folders, deepest first."""
    for item in items:
        if item.kind == "dir":
            with contextlib.suppress(OSError):
                os.chmod(item.src, 0o700)  # a read-only folder can't have its files removed
    for item in reversed(items):
        if item.kind == "dir":
            with contextlib.suppress(OSError):  # a folder that still holds something ws can't move stays
                os.rmdir(item.src)
        else:
            os.unlink(item.src)


def _move_secrets(items: list[Item], lay: paths.Layout) -> None:
    """Put the secrets store in the data folder without leaving a copy behind: one rename when the data folder is on the
    same filesystem, else a copy (0700/0600 throughout), a check by hash, then the old copy removed. Raises `WsError`
    when it can't; the old store is then still whole."""
    if not items:
        return
    top = items[0]
    if top.kind == "dir" and top.action == "copy":
        try:
            _make_roots(lay)
            os.rename(top.src, top.dst)
        except OSError as e:
            if e.errno != errno.EXDEV:
                raise WsError(
                    "failed",
                    f"could not move the secrets store: {e.strerror or type(e).__name__} ({e.filename})",
                    "nothing was lost; fix that and run `ws migrate` again",
                ) from e
        else:
            for item in items:
                item.action = "renamed"
            bad = _verify(Plan("move", top.src.parent, items))
            if bad:
                raise WsError("failed", f"the secrets store did not arrive whole. First: {bad[0]}", "report it")
            return
    sub = Plan("move", top.src.parent, items)
    _copy(sub, lay)
    bad = _verify(sub)
    if bad:
        raise WsError(
            "failed",
            f"{len(bad)} secrets file(s) did not match the source; the old store is untouched. First: {bad[0]}",
            "run `ws migrate` again; if it repeats, report it",
            details={"mismatches": bad},
        )
    _remove_sources(items)


def _answers(sock: Path) -> bool:
    """True when something is listening on this Unix socket."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(sock))
    except OSError:
        return False
    finally:
        s.close()
    return True


def busy(source: Path, env: Mapping[str, str]) -> str:
    """Why the move can't start now, or "": this command is running inside a gated ws session, or something (the secrets
    broker) is answering on a socket in the old folder's `run/`. A socket nothing listens on is a leftover and is ignored."""
    from ws import approvals

    if env.get(approvals.ENV_MARKER) or env.get(approvals.SANDBOX_MARKER):
        return "this command is running inside a gated ws session"
    run = source / "run"
    try:
        socks = sorted(p for p in run.iterdir() if stat.S_ISSOCK(os.lstat(p).st_mode)) if run.is_dir() else []
    except OSError:
        socks = []
    from ws import secrets

    with contextlib.suppress(Exception):  # a socket path that can't be worked out is no evidence of a live broker
        socks.append(secrets.socket_path(dict(env)))  # a broker on a socket the profile or $WS_BROKER_SOCKET names
    for sock in dict.fromkeys(socks):
        if _answers(sock):
            return f"the secrets broker (or another process) is answering on {sock}"
    return ""


def _timers(p: Plan) -> list[Path]:
    """The timer (systemd) and agent (launchd) files ws wrote in the old folder: the units are in `jobs/units/`."""
    units = p.source / "jobs" / "units"
    return [
        i.src for i in p.items if i.kind == "file" and i.src.parent == units and i.src.suffix in (".timer", ".plist")
    ]


def _deactivate(timers: list[Path], env: Mapping[str, str], runner=None) -> list[str]:
    from ws import roles

    return roles.deactivate_units(timers, dict(env), runner)


def _reschedule(env: Mapping[str, str], runner=None) -> list[str]:
    """`ws schedule apply`: write the unit files again for the folders now in use and switch the timers on. Returns what
    went wrong."""
    from ws import install, osinfo
    from ws.roles import commands

    sched = commands._scheduler(osinfo.detect())
    install.apply(sched.steps({}, install.answers_from_profile(env), env), env)
    return sched.activate(env, runner)


def _remap(value, mapping: Callable[[str], str]):
    if isinstance(value, str):
        return mapping(value)
    if isinstance(value, list):
        return [_remap(v, mapping) for v in value]
    if isinstance(value, dict):
        return {k: _remap(v, mapping) for k, v in value.items()}
    return value


def remap_install_record(doc: dict, source: Path, lay: paths.Layout) -> dict:
    """The install record with its paths moved to the new folders: each file or folder entry's `path`, and `roots`. A merge entry's recorded items (the runner's hooks) keep their text: `ws install` swaps them for new ones."""

    def move(text: str) -> str:
        try:
            rel = Path(text).relative_to(source)
        except ValueError:
            return text
        kind = paths.kind_of(rel.parts[0]) if rel.parts else None
        return str(lay.dir(kind).joinpath(*rel.parts)) if kind else text

    out = dict(doc)
    if isinstance(doc.get("roots"), list):
        roots = [move(r) if isinstance(r, str) else r for r in doc["roots"] if r != str(source)]
        out["roots"] = list(dict.fromkeys([*roots, *map(str, lay.distinct())]))
    if isinstance(doc.get("entries"), list):
        entries = []
        for entry in doc["entries"]:
            if isinstance(entry, dict) and entry.get("kind") in ("file", "dir") and isinstance(entry.get("path"), str):
                entry = {**entry, "path": move(entry["path"])}
            entries.append(entry)
        out["entries"] = entries
    return out


def _write_private(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.migrate-tmp")
    fd = _open_new(tmp)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _manifest(p: Plan, now: datetime, renamed: Path) -> dict:
    return {
        "schema": SCHEMA,
        "ws_version": __version__,
        "at": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": str(p.source),
        "renamed_to": str(renamed),
        "moved": [
            {
                "from": str(i.src),
                "to": str(i.dst),
                "kind": i.kind,
                "mode": f"{i.mode:04o}",
                **({"sha256": i.sha256, "size": i.size} if i.kind == "file" else {}),
                **({"target": i.target} if i.kind == "link" else {}),
                "already_there": i.action == "present",
            }
            for i in p.items
        ],
        "left": p.left,
    }


def _refresh(env: Mapping[str, str]) -> str:
    """Put the runners' hooks and the generated gate files on the new paths: the same as `ws install` with no flags."""
    from ws import install

    result = install.apply(install.plan(install.answers_from_profile(env), env), env)
    return result.summary()


def migrate(
    env: Mapping[str, str] | None = None,
    *,
    dry_run: bool = False,
    now: datetime | None = None,
    refresh: Callable[[Mapping[str, str]], str] | None = None,
    deactivate: Callable[[list[Path], Mapping[str, str]], list[str]] | None = None,
    reschedule: Callable[[Mapping[str, str]], list[str]] | None = None,
    runner: Callable[[list[str]], tuple[int, str]] | None = None,
) -> dict:
    """Do the move (or plan it, with `dry_run`). Returns the result as a dict; raises `WsError` when it is refused or a
    copy fails its check (nothing has been renamed or deleted by then). `deactivate` switches the given timer files off
    and `reschedule` writes and starts them again after the move; each returns what went wrong. They default to the
    machine's scheduler, which runs its commands through `runner`."""
    e = os.environ if env is None else env
    p = plan(e)
    data: dict = {"state": p.state, "source": str(p.source), "message": p.message, "dry_run": dry_run}
    if p.state != "move":
        return data
    lay = paths.xdg_layout(e)
    data.update(
        folders={k: str(lay.dir(k)) for k in paths.KINDS},
        files=sum(i.kind == "file" for i in p.copies),
        already_there=sum(i.action == "present" for i in p.items),
        left=p.left,
    )
    if p.conflicts:
        raise WsError(
            "refused",
            f"{len(p.conflicts)} target(s) already exist and differ from the old files; nothing was changed. First: {p.conflicts[0]}",
            "look at the two copies and move the one you don't want aside, then run `ws migrate` again",
            details={"conflicts": p.conflicts},
        )
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%d")
    renamed = _unused_name(p.source.with_name(f"{p.source.name}.migrated-{stamp}"))
    data["renamed_to"] = renamed.name if dry_run else str(renamed)
    if dry_run:
        data["would_move"] = [{"from": str(i.src), "to": str(i.dst)} for i in p.copies if i.kind != "dir"]
        return data
    why = busy(p.source, e)
    if why:
        raise WsError(
            "refused",
            f"{why}; nothing was changed",
            "stop the broker and any ws session (run `ws migrate` from a plain terminal), then try again",
        )
    had_install = (p.source / INSTALL_MANIFEST).is_file()
    timers = _timers(p)
    if timers:  # a timer names the old folder: stop them now, start them again from the new one
        problems = deactivate(timers, e) if deactivate else _deactivate(timers, e, runner)
        if problems:
            raise WsError(
                "refused",
                f"could not switch ws's timers off: {problems[0]}; nothing was moved",
                "run `ws schedule apply` to put them back, fix that, then run `ws migrate` again",
                details={"problems": problems},
            )
    try:
        secrets_root = p.source / "secrets"
        secret_items = [i for i in p.items if i.src == secrets_root or secrets_root in i.src.parents]
        held = {id(i) for i in secret_items}
        rest = Plan(p.state, p.source, [i for i in p.items if id(i) not in held])
        try:
            _copy(rest, lay)
        except OSError as e2:
            raise WsError(
                "failed",
                f"could not copy to the new folders: {e2.strerror or type(e2).__name__} ({e2.filename})",
                "nothing in the old folder was changed; fix that and run `ws migrate` again",
            ) from e2
        bad = _verify(rest)
        if bad:
            raise WsError(
                "failed",
                f"{len(bad)} copy(ies) did not match the source; the old folder is untouched. First: {bad[0]}",
                "run `ws migrate` again; if it repeats, report it",
                details={"mismatches": bad},
            )
        record = lay.state / INSTALL_MANIFEST
        if had_install and record.is_file():
            try:
                doc = json.loads(record.read_text(encoding="utf-8"))
                if isinstance(doc, dict):
                    _write_private(record, json.dumps(remap_install_record(doc, p.source, lay), indent=2) + "\n")
            except (OSError, ValueError) as e2:
                raise WsError(
                    "failed",
                    f"could not update the install record in {record}: {e2}",
                    "the old folder is untouched; fix the file and run `ws migrate` again",
                ) from e2
        try:
            _move_secrets(
                secret_items, lay
            )  # the last of the files, by rename: no plaintext copy stays in the old folder
        except OSError as e2:
            raise WsError(
                "failed",
                f"could not move the secrets store: {e2.strerror or type(e2).__name__} ({e2.filename})",
                "the old store is whole; fix that and run `ws migrate` again",
            ) from e2
    except WsError as err:
        if timers:  # they were switched off for the move: say how to put them back
            err.hint = f"{err.hint}; ws's timers are off: run `ws schedule apply` to switch them back on".lstrip("; ")
        raise
    when = now or datetime.now(UTC)
    _write_private(lay.state / MANIFEST, json.dumps(_manifest(p, when, renamed), indent=2) + "\n")
    os.rename(p.source, renamed)
    data.update(state="moved", renamed_to=str(renamed), manifest=str(lay.state / MANIFEST))
    if had_install:
        try:
            data["refreshed"] = (refresh or _refresh)(e)
        except Exception as e2:  # noqa: BLE001 (the move is done; say what is left)
            data["refresh_error"] = str(e2)
    if timers:
        try:
            problems = reschedule(e) if reschedule else _reschedule(e, runner)
        except Exception as e2:  # noqa: BLE001 (the move is done; say what is left)
            problems = [str(e2)]
        if problems:
            data["next"] = "ws schedule apply"
            data["schedule_error"] = problems[0]
        else:
            data["rescheduled"] = len(timers)
    return data


def render(data: dict) -> str:
    state = data["state"]
    if state in ("done", "nothing", "single"):
        return f"ws: {data['message']}"
    out = []
    verb = "would move" if data["dry_run"] else "moved"
    out.append(f"ws: {verb} {data['files']} file(s) from {data['source']} to the XDG folders:")
    out += [f"  {k:<7} {v}" for k, v in data["folders"].items()]
    if data["already_there"]:
        out.append(f"  {data['already_there']} item(s) were already in place and left as they are")
    for line in data.get("left", []):
        out.append(f"  not moved: {line}")
    if data["dry_run"]:
        out.append(
            f"ws: dry run: nothing changed. A real run renames {data['source']} to {data['renamed_to']} (it deletes nothing)."
        )
        return "\n".join(out)
    out.append(f"ws: {data['source']} is now {data['renamed_to']} (nothing was deleted; remove it when you've looked)")
    out.append(f"ws: what moved is listed in {data['manifest']}")
    if "refreshed" in data:
        out.append(f"ws: refreshed the runner hooks and generated files: {data['refreshed']}")
    if "refresh_error" in data:
        out.append(f"ws: could not refresh the runner hooks ({data['refresh_error']}); run `ws install` to finish")
    if data.get("rescheduled"):
        out.append("ws: the timers were switched off for the move and are on again from the new folders")
    if data.get("next"):
        out.append(
            f"ws: your timers are off or still name the old folder ({data['schedule_error']}): run `{data['next']}`"
        )
    return "\n".join(out)


def cmd_migrate(args: argparse.Namespace) -> int:
    data = migrate(dry_run=args.dry_run)
    failed = "refresh_error" in data or "schedule_error" in data
    if args.json:
        output.result(data, status=output.ATTENTION if failed else output.OK)
    else:
        print(render(data))
    return 1 if failed else 0


def register(sub) -> None:
    m = sub.add_parser(
        "migrate",
        help="move ws's files from ~/.ws to the XDG config, state, data and cache folders (nothing is deleted)",
    )
    m.add_argument("--dry-run", action="store_true", help="show the move and change nothing")
    m.set_defaults(func=cmd_migrate)
