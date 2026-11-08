"""A node's footprint: everything `ws node join` puts on this machine is written in a ledger as it is made, and `ws node remove` undoes it.

    ws node join [--every N] [--timer]   record and make the node's files: the queue folder, a node record and a queue-draining job
    ws node show                         the ledger: every change, and whether it is still in place
    ws node remove                       undo every change, newest first; what cannot be undone is listed and the exit is 1

The ledger is `<state>/node/footprint.jsonl`, written through `ws.jsonl` (an integer `schema`, 1, and the field `type`:
`ws.footprint/1`). Each change gets two lines: an `intent` before ws touches anything, then a `done` once it is made, so a
crash between them leaves an intent that `remove` still understands. The ledger is never rebuilt from what is found on
the machine afterwards. It does not rotate.

`remove` undoes in reverse order and can be run again: a step already undone is skipped. It deletes a file only if its
content is still the content ws wrote (the SHA-256 in the intent), so a file you changed is left, named, and the exit is
1; a folder only if ws made it and it is empty; a timer by switching it off. When every step is undone it removes the
ledger itself, and the node folder if that is empty, so an install followed by a remove leaves the machine as it was.
Anything that cannot be undone stays in the ledger and is listed, so the next `remove` takes it up again.

`join` and `remove` are refused in a gated session. Removing a node from a controller is a later step; this is the local
half.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import os
import stat
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from ws import approvals, jobs, jsonl, output, paths
from ws import queue as wsqueue
from ws.output import WsError

COMMAND = "node"
SCHEMA_VERSION = 1
TYPE = "ws.footprint/1"
EXIT_FAILED = 1
DRAIN_JOB = "queue-drain"
_NO_ROTATE = 1 << 40  # bytes: a footprint ledger is never rotated, so no change is ever forgotten

_DRAIN_TEXT = (
    "# Written by `ws node join`: runs what is queued. `ws node remove` takes it away.\n"
    'command = "queue"\n'
    'schedule = "every {every}m"\n'
)


class NodeError(WsError):
    def __init__(self, kind: str, message: str, hint: str, exit_code: int = EXIT_FAILED) -> None:
        super().__init__(kind, message, hint, exit_code)


@dataclass
class Step:
    """One change, as the ledger folds it: the intent's fields and where it got to (`intent`, `done`, `removed`)."""

    id: str
    kind: str
    target: str
    state: str
    detail: dict = field(default_factory=dict)


@dataclass
class Result:
    removed: list[Step] = field(default_factory=list)
    left: list[tuple[Step, str]] = field(default_factory=list)


def ledger_path(env=None) -> Path:
    return paths.state_dir(env) / "node" / "footprint.jsonl"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _line(node: str, op: str, step: str, **fields) -> dict:
    return jsonl.stamped({"type": TYPE, "node": node, "op": op, "step": step, **fields}, SCHEMA_VERSION)


def _write(path: Path, rec: dict) -> None:
    jsonl.append(path, rec, max_bytes=_NO_ROTATE)


def read_steps(path: Path) -> tuple[str | None, list[Step]]:
    """(node id, steps in the order they were first written) folded from the ledger. A line that is not a footprint line
    is ignored."""
    node: str | None = None
    order: dict[str, Step] = {}
    for rec in jsonl.read(path):
        if rec.get("type") != TYPE or not isinstance(rec.get("step"), str):
            continue
        node = node or rec.get("node")
        sid, op = rec["step"], rec.get("op")
        if op == "intent":
            detail = {
                k: v
                for k, v in rec.items()
                if k not in ("schema", "ts", "type", "node", "op", "step", "kind", "target")
            }
            order[sid] = Step(sid, str(rec.get("kind")), str(rec.get("target")), "intent", detail)
        elif sid in order and op in ("done", "removed"):
            order[sid].state = op
    return node, list(order.values())


# ---------------------------------------------------------------------------------------------------------------------
# What each kind of step does, and undoes. A test may swap these to watch the ledger at the moment of the change.


def _do_dir(step: Step) -> None:
    if not step.detail.get("existed"):
        Path(step.target).mkdir(mode=0o700, parents=True, exist_ok=True)


class Blocked(str):
    """Why a folder could not be removed, with the names it still holds."""

    names: list[str]


def _undo_dir(step: Step) -> str | None:
    if step.detail.get("existed"):
        return None
    p = Path(step.target)
    try:
        p.rmdir()
    except FileNotFoundError:
        return None
    except OSError:
        why = Blocked("not empty, or not ws's to remove: " + ", ".join(sorted(x.name for x in p.iterdir())[:5]))
        why.names = sorted(x.name for x in p.iterdir())
        return why
    return None


def _do_file(step: Step) -> None:
    """Make the file whole under a temporary name and put it in place without replacing anything, so a crash leaves the
    file complete or not there, never half written. A file that appeared at the name meanwhile is left as it is (`remove`
    deletes it only if its content is the one recorded)."""
    if step.detail.get("existed"):
        return
    p = Path(step.target)
    tmp = p.with_name(f".{p.name}.{os.getpid()}.tmp")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(step.detail["content"].encode("utf-8"))
        with contextlib.suppress(FileExistsError):
            os.link(tmp, p)
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink()


def _undo_file(step: Step) -> str | None:
    if step.detail.get("existed"):
        return None
    p = Path(step.target)
    try:
        st = os.lstat(p)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(st.st_mode):
        return "no longer a plain file; left alone"
    if _sha(p.read_bytes()) != step.detail.get("sha256"):
        return "changed since ws wrote it; left alone"
    p.unlink()
    return None


def _do_timer(step: Step, runner: Callable | None, env) -> None:
    """Link and enable the one timer this step names (the queue drain's), and nothing else: the other jobs that have a
    schedule on this machine are not ws node's to switch on."""
    from ws.roles import _schedule, systemd

    run = runner or _schedule.run_cmd
    files = systemd.files_for(env, step.target)
    clash = [f.name for f in files if _schedule.is_foreign(env, f.name, systemd.MARKER)]
    if clash:
        raise NodeError(
            "timer",
            f"{', '.join(clash)} already exists in {_schedule.user_unit_dir(env)} and isn't ws's",
            "rename that unit or leave --timer out",
        )
    timers = [f.name for f in files if f.suffix == ".timer"]
    for argv in (
        ["systemctl", "--user", "link", *map(str, files)],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "--now", *timers],
    ):
        code, out = run(argv)
        if code:
            raise NodeError(
                "timer",
                f"`{' '.join(argv[:4])}` failed: {out[:200] or code}",
                "look at `systemctl --user status`, then `ws node remove`",
            )


def _undo_timer(step: Step, runner: Callable | None, env) -> str | None:
    from ws.roles import systemd

    problems = systemd.deactivate([step.target], env, runner)
    return "; ".join(problems) if problems else None


_DO: dict[str, Callable] = {"dir": _do_dir, "file": _do_file}
_UNDO: dict[str, Callable] = {"dir": _undo_dir, "file": _undo_file}


# ---------------------------------------------------------------------------------------------------------------------


def _plan(env, node_id: str, every: int, timer: bool) -> list[tuple[str, str, dict]]:
    """(kind, target, detail) for each change, in the order they are made."""
    state = paths.state_dir(env)
    jobs_dir = jobs.jobs_dir(env)
    plan: list[tuple[str, str, dict]] = []
    planned: set[Path] = set()  # folders an earlier step in this plan makes

    def folder(p: Path) -> None:
        """The folder, and each missing folder above it, outermost first, so the undo takes them away innermost first."""
        missing = []
        while not p.is_dir() and p not in planned and p != p.parent:
            missing.append(p)
            p = p.parent
        planned.update(missing)
        plan.extend(("dir", str(d), {"existed": False}) for d in reversed(missing))

    def file(p: Path, text: str) -> None:
        data = text.encode("utf-8")
        existed = p.is_file()
        if existed and _sha(p.read_bytes()) != _sha(data):
            return  # a file of the same name that is yours: ws neither overwrites it nor claims it
        plan.append(("file", str(p), {"content": text, "sha256": _sha(data), "existed": existed}))

    folder(state / "queue")
    file(state / "node" / "node.json", f'{{"schema": 1, "node": "{node_id}", "ws_version": "{wsqueue._version()}"}}\n')
    folder(jobs_dir)
    file(jobs_dir / f"{DRAIN_JOB}.toml", _DRAIN_TEXT.format(every=every))
    if timer:
        from ws.roles import _schedule, systemd

        job = jobs.Scheduled(DRAIN_JOB, jobs.When("every", every=every), ("queue", "run"))
        folder(_schedule.units_dir(env))
        for name, text in systemd.render(job, env, _schedule.ws_command(env)).items():
            file(_schedule.units_dir(env) / name, text)
        plan.append(("timer", DRAIN_JOB, {}))
    return plan


def _gated() -> None:
    if os.environ.get(approvals.ENV_MARKER):
        raise NodeError(
            "refused", f"ws node is not for a gated session ({approvals.ENV_MARKER} is set here)",
            "run it yourself, in your own terminal", 3,
        )  # fmt: skip


def join(env=None, *, every: int = 10, timer: bool = False, runner: Callable | None = None) -> list[Step]:
    """Make the node's footprint, writing the intent of each change before it and its `done` after. Returns the steps. A
    node that has joined already (a step still in place) is left as it is."""
    env = os.environ if env is None else env
    path = ledger_path(env)
    known, steps = read_steps(path)
    live = [s for s in steps if s.state != "removed"]
    node_id = known if live and known else f"n-{os.urandom(4).hex()}"
    plan = _plan(env, node_id, every, timer)
    if timer:
        from ws import osinfo

        if osinfo.detect().scheduler != "systemd" and runner is None:
            raise NodeError(
                "no_timer", "--timer needs systemd on this machine", "leave --timer out and run `ws schedule apply`"
            )
    have = {(s.kind, s.target): s for s in live}
    numbers = [int(s.id[1:]) for s in steps if s.id[1:].isdigit()]
    next_id = max(numbers, default=0) + 1
    made: list[Step] = []
    # a step whose intent is written and whose `done` is not (the join stopped there) is done again: every kind is safe to
    # repeat; then each step of the plan the ledger doesn't hold yet is added after the ones it has
    for step in (s for s in live if s.state == "intent"):
        _make(step, runner, env)
        _write(path, _line(node_id, "done", step.id))
        step.state = "done"
    for kind, target, detail in plan:
        if (kind, target) in have:
            continue
        step = Step(f"s{next_id}", kind, target, "intent", detail)
        next_id += 1
        _write(path, _line(node_id, "intent", step.id, kind=kind, target=target, **detail))
        _make(step, runner, env)
        _write(path, _line(node_id, "done", step.id))
        step.state = "done"
        made.append(step)
    return live + made


def _make(step: Step, runner: Callable | None, env) -> None:
    if step.kind == "timer":
        _do_timer(step, runner, env)
    else:
        _DO[step.kind](step)


def remove(env=None, *, runner: Callable | None = None) -> Result:
    """Undo every step still in place, newest first. Returns what was undone and what is left, with why. A step that is
    left stays in the ledger. When none is left the ledger goes too."""
    env = os.environ if env is None else env
    path = ledger_path(env)
    node, steps = read_steps(path)
    result = Result()
    deferred: list[Step] = []
    for step in reversed(steps):
        if step.state == "removed":
            continue
        try:
            if step.kind == "timer":
                why = _undo_timer(step, runner, env)
            else:
                why = _UNDO[step.kind](step)
        except (OSError, WsError) as e:
            why = getattr(e, "message", None) or f"{type(e).__name__}: {e.strerror or e}"
        if why and step.state == "intent" and step.kind in ("dir", "file"):
            why = None  # its `done` was never written, so ws can't claim what is there: it is not ws's, and not in the way
        if isinstance(why, Blocked) and _only_ledger_or_deferred(step, why.names, deferred):
            deferred.append(step)  # it holds nothing but the ledger's folder, or a folder that is itself waiting
            continue
        if why:
            result.left.append((step, why))
            continue
        _write(path, _line(node or "", "removed", step.id))
        result.removed.append(step)
    if result.left:
        result.left += [(s, "waits for the steps above") for s in deferred]
        return result
    _drop_ledger(path)  # nothing is left to undo: the ledger goes, and then the folders that held only it
    for step in deferred:
        why = _undo_dir(step)
        if why:
            result.left.append((step, why))
        else:
            result.removed.append(step)
    return result


def _only_ledger_or_deferred(step: Step, names: list[str], deferred: list[Step]) -> bool:
    """Does the folder hold only the ledger's folder (`node`) and folders that wait for the ledger to go too?"""
    waiting = {Path(d.target).name for d in deferred if Path(d.target).parent == Path(step.target)}
    return bool(names) and set(names) <= {"node"} | waiting


def _drop_ledger(path: Path) -> None:
    """Nothing is left to undo: the ledger, its lock and the node folder (if that is then empty) go too."""
    for p in (path, path.with_name(path.name + ".lock"), *[a for _, a in jsonl.archives(path)]):
        with contextlib.suppress(OSError):
            p.unlink()
    with contextlib.suppress(OSError):
        path.parent.rmdir()


def _data(steps: list[Step], left: list[tuple[Step, str]] | None = None) -> dict:
    out = [{"step": s.id, "kind": s.kind, "target": s.target, "state": s.state} for s in steps]
    return {"steps": out, "left": [{"step": s.id, "target": s.target, "why": why} for s, why in left or []]}


def cmd_join(args: argparse.Namespace) -> int:
    _gated()
    steps = join(every=args.every, timer=args.timer)
    if args.json:
        output.result(_data(steps))
    else:
        print(f"ws: node joined: {len(steps)} change(s) recorded in {ledger_path()}")
        for s in steps:
            print(f"  {s.id}  {s.kind:<6} {s.target}")
        if not args.timer:
            print(
                "ws: no timer yet; `ws node join --timer` (systemd) or `ws schedule apply` switches on the queue drain"
            )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    node, steps = read_steps(ledger_path())
    if args.json:
        output.result({"node": node, **_data(steps)})
        return 0
    if not steps:
        print("ws: this node has no footprint (it has not joined)")
        return 0
    print(f"node {node}")
    for s in steps:
        print(f"  {s.id}  {s.state:<8} {s.kind:<6} {s.target}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    _gated()
    result = remove()
    if args.json:
        output.result(
            {"removed": [s.id for s in result.removed], "left": _data([], result.left)["left"]},
            status=output.ATTENTION if result.left else output.OK,
        )
    else:
        print(f"ws: removed {len(result.removed)} change(s)")
        for s, why in result.left:
            print(f"  left: {s.target} ({why})")
    return EXIT_FAILED if result.left else 0


def _every(text: str) -> int:
    n = int(text)
    if not jobs.EVERY_MIN <= n <= jobs.EVERY_MAX:
        raise argparse.ArgumentTypeError(f"must be from {jobs.EVERY_MIN} to {jobs.EVERY_MAX}")
    return n


def register(sub) -> None:
    n = sub.add_parser(
        "node", help="this machine's footprint: what `ws node join` put here, and the way to take it away"
    )
    ns = n.add_subparsers(dest="node_command", required=True)
    j = ns.add_parser("join", help="make the node's files and record each change before it is made")
    j.add_argument(
        "--every", type=_every, default=10, metavar="N", help="minutes between queue drains, 5 to 30 (default 10)"
    )
    j.add_argument("--timer", action="store_true", help="also switch on the systemd timer that drains the queue")
    j.set_defaults(func=cmd_join)
    ns.add_parser("show", help="the ledger: every change, and whether it is in place").set_defaults(func=cmd_show)
    ns.add_parser("remove", help="undo every change, newest first; exit 1 and a list if any is left").set_defaults(
        func=cmd_remove
    )
