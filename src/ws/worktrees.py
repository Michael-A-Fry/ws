"""`ws worktrees prune`: remove finished agent worktrees, and keep the disk from filling with them.

    ws worktrees prune [--repo R]... [--only PATH] [--min-idle HOURS] [--dry-run] [--auto]

Why. Agent sessions each get a git worktree under `<repo>/.claude/worktrees/` and nothing removes it when the work is
pushed; a busy repo collects dozens in days, each with its own virtualenv and caches, and the disk fills within hours.
Built, because git can list, check and remove a worktree but no maintained tool checks the lock, the changes, the idle
time and the live processes together.

A worktree under `<repo>/.claude/worktrees/` (never the main checkout or one elsewhere) is kept, with a reason, when it
is `locked`, `dirty` (a tracked file has a change), `fresh` (a file in it changed in the last `idle_hours`, 12),
`in-use` (a process has its working folder or an open file inside it: /proc on Linux, one `lsof` on macOS; another
account's processes can't be seen), `busy-unknown` (the process list does not show the caller's own parents, as in a
PID namespace or with `hidepid`, so use can't be ruled out and every worktree stays), or `unreachable` (HEAD detached
and no branch, remote or tag holds it). `--only` (the close-out path) also needs its head on a remote ref (`unpushed`)
and counts the caller's own parents as not using it; every run ignores itself and its own children. The rest are
removed with `git worktree remove`, never `--force`, so git still refuses a folder with untracked files: that is
`failed`, the worktree stays registered and the run goes on. Only a folder where `git status` shows nothing to lose
(untracked files included, ignored ones not) is first made writable (`chmod -R u+w`), and a file with more than one
hard link is left as it is, so a refused worktree keeps its modes and uv's shared cache is not touched. Only these
worktrees are removed: nothing runs `git worktree prune`, which would also drop other registered worktrees whose folder
has moved. Branches and commits stay. Ignored files (virtualenvs, caches) go with the folder, by design. Each removal
adds a line (time, path, branch, head) to `<state>/worktree-prune.log`. The run prints one line, `checked N, removed M,
kept K (by reason), X GB freed`; `--dry-run` prints the same and changes nothing.

Zones. A repo is pruned only from inside its own zone: a caller in a data class may prune that class's repos, and
another `full` class if it is `full` itself; a caller in no class (a timer, run from the home folder) may prune `full`
classes. Any other repo is refused before git runs in it, and a policy that can't be read refuses all. With no classes,
nothing is refused. A walled class is pruned by a job running inside it.

`--auto` is the timer's call (`ws-worktrees-prune.timer`, every 30 minutes): it reads the disk, prunes when
`interval_hours` (6) have passed or at once at `disk_pct` (85), and then tells the lead once per interval (a `ws msg` when
the registry lets it send, an alerts line and a push). The repos are those the policy's classes name, `[worktrees] repos`,
or `~/code/*` with no classes. `<state>/worktree-prune.json` holds `worktrees_pruned`, `gb_freed` and `disk_pct`, the
counts card a conductor reads. Settings (`[worktrees]`, profile): `idle_hours`, `interval_hours`, `disk_pct`,
`temp_age_hours` 6, `temp_warn_gb` 10 (both for `ws.tmpclean`) and `repos`.

Swap note: the removal is plain `git worktree remove`; delete the call to use another tool.
"""

from __future__ import annotations

import argparse
import contextlib
import glob
import json
import os
import shutil
import stat
import subprocess
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from ws import jobwatch, output, paths
from ws.output import WsError
from ws.profile import ProfileError, load, profile_path

COMMAND = "worktrees"
SUBDIR = Path(".claude") / "worktrees"
GB = 1024**3
# setting: (default, lowest, highest)
LIMITS = {
    "idle_hours": (12, 0, 24 * 365),
    "interval_hours": (6, 0.5, 24 * 30),
    "disk_pct": (85, 1, 100),
    "temp_age_hours": (6, 1, 24 * 30),
    "temp_warn_gb": (10, 0.1, 100_000),
}


@dataclass(frozen=True)
class Settings:
    idle_hours: float = 12
    interval_hours: float = 6
    disk_pct: float = 85
    temp_age_hours: int = 6
    temp_warn_gb: float = 10
    repos: tuple[str, ...] = ()


def load_settings(env=None) -> Settings:
    """`[worktrees]` of the profile; every key is optional and a bad value is a typed error naming the key."""
    try:
        prof = load(profile_path(dict(os.environ if env is None else env))) or {}
    except ProfileError as err:
        raise WsError("bad_config", str(err), "fix the profile", 1) from err
    table = prof.get("worktrees") if isinstance(prof.get("worktrees"), dict) else {}
    values = {}
    for key, (default, low, high) in LIMITS.items():
        v = table.get(key, default)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not low <= v <= high:
            raise WsError(
                "bad_config", f"[worktrees] {key} must be a number from {low:g} to {high:g}", "fix the profile"
            )
        values[key] = v
    repos = table.get("repos", [])
    if not isinstance(repos, list) or not all(isinstance(r, str) and r.strip() and "\0" not in r for r in repos):
        raise WsError("bad_config", "[worktrees] repos must be a list of folders", "fix the profile")
    return Settings(**values, repos=tuple(repos))


# --- git and the files -----------------------------------------------------------------------------------------------


def _git(repo: str | Path, *args: str) -> tuple[int, str]:
    """(exit code, output) of `git -C repo ...` with git's repository variables out (a hook sets them), no prompts and no
    optional locks; 127 when git cannot run."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_OPTIONAL_LOCKS="0")
    try:
        p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL, env=env)  # fmt: skip
    except (OSError, subprocess.SubprocessError) as e:
        return 127, type(e).__name__
    return p.returncode, p.stdout if p.returncode == 0 else p.stdout + p.stderr


@dataclass(frozen=True)
class Worktree:
    path: str
    head: str = ""
    branch: str = ""  # "" when detached
    locked: bool = False


def list_worktrees(repo: str | Path) -> list[Worktree]:
    """The linked worktrees git lists for the repo (`git worktree list --porcelain`, without the main checkout)."""
    code, out = _git(repo, "worktree", "list", "--porcelain")
    if code != 0:
        raise WsError("failed", f"git could not list the worktrees of {repo}", "is it a git repository?")
    found = []
    for block in out.split("\n\n")[1:]:
        f = dict(line.partition(" ")[::2] for line in block.strip().splitlines())
        if "worktree" in f and "bare" not in f:
            found.append(
                Worktree(
                    f["worktree"], f.get("HEAD", ""), f.get("branch", "").removeprefix("refs/heads/"), "locked" in f
                )
            )
    return found


def main_checkout(folder: str | Path) -> Path:
    """The main checkout of the repo `folder` is in (a linked worktree resolves to its main one)."""
    code, out = _git(folder, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if code != 0 or not out.strip():
        raise WsError("bad_usage", f"{folder} is not in a git repository", "name a repo with `--repo`", 2)
    common = Path(out.strip())
    return common.parent if common.name == ".git" else common


def _under(path: str, root: str) -> bool:
    return path == root or path.startswith(root.rstrip("/") + "/")


def walk(root: str | Path):
    """Every (path, lstat) under `root`, links not followed, unreadable folders skipped."""
    stack = [str(root)]
    while stack:
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    st = entry.stat(follow_symlinks=False)
                    yield entry.path, st
                    if stat.S_ISDIR(st.st_mode):
                        stack.append(entry.path)
        except OSError:
            continue


def newer_than(root: str | Path, cutoff: float) -> bool:
    """Is anything under `root` newer than `cutoff`? Stops at the first one."""
    return any(st.st_mtime > cutoff for _, st in walk(root))


def tree_bytes(root: str | Path) -> int:
    """Disk used under `root`: allocated blocks, so sparse files are not over-counted."""
    return sum(getattr(st, "st_blocks", 0) * 512 or st.st_size for _, st in walk(root) if not stat.S_ISDIR(st.st_mode))


def make_writable(root: str | Path) -> None:
    """`chmod -R u+w`, links not followed: a read-only folder (Go's module cache) blocks its own removal. A root that is
    a link or not a folder is left alone, and so is a file with more than one name (uv's cache hard-links its files into
    every virtualenv: changing the mode here would change it for the other names too)."""
    try:
        st = os.lstat(root)
    except OSError:
        return
    if not stat.S_ISDIR(st.st_mode):
        return
    with contextlib.suppress(OSError):
        os.chmod(root, st.st_mode | 0o700)
    for path, st in walk(root):
        if stat.S_ISLNK(st.st_mode) or (not stat.S_ISDIR(st.st_mode) and st.st_nlink > 1):
            continue
        with contextlib.suppress(OSError):
            os.chmod(path, st.st_mode | (0o700 if stat.S_ISDIR(st.st_mode) else 0o200))


def untouched(path: str | Path) -> bool:
    """Does git see nothing to lose in `path`: no change, and no untracked file (ignored files do not count)?"""
    code, out = _git(path, "status", "--porcelain")
    return code == 0 and not out.strip()


def _reaches(path: str, head: str, *namespaces: str) -> bool:
    """Does any ref in these namespaces contain `head`? False when git cannot say."""
    code, out = _git(path, "for-each-ref", "--count=1", "--contains", head, "--format=%(refname)", *namespaces)
    return code == 0 and bool(out.strip())


# --- live processes --------------------------------------------------------------------------------------------------


def _ppids() -> dict[int, int]:
    """{pid: parent pid} for every process: from /proc on Linux, from `ps` elsewhere."""
    out: dict[int, int] = {}
    if os.path.isdir("/proc/self"):
        for name in filter(str.isdigit, os.listdir("/proc")):
            with contextlib.suppress(OSError, ValueError, IndexError):
                out[int(name)] = int(
                    Path(f"/proc/{name}/stat").read_text(errors="replace").rsplit(")", 1)[1].split()[1]
                )
        return out
    try:
        text = subprocess.run(["ps", "-axo", "pid=,ppid="], capture_output=True, text=True, timeout=30, check=False, stdin=subprocess.DEVNULL).stdout  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return out
    for parts in (line.split() for line in text.splitlines()):
        if len(parts) == 2 and all(x.isdigit() for x in parts):
            out[int(parts[0])] = int(parts[1])
    return out


def sees_own_parents(ppid: dict[int, int], me: int | None = None) -> bool:
    """Does the process list show this process and every parent above it, up to init? A caller in its own PID namespace,
    or on a /proc mounted `hidepid`, sees only part of the tree, and then "nothing is using this folder" can't be said."""
    cur = os.getpid() if me is None else me
    while cur in ppid:
        cur = ppid[cur]
        if cur <= 1:
            return True
    return False


def own_tree(parents: bool, me: int | None = None, ppid: dict[int, int] | None = None) -> set[int]:
    """This process and everything it started; with `parents`, the processes that started it as well."""
    me = os.getpid() if me is None else me
    ppid = _ppids() if ppid is None else ppid
    tree, grew = {me}, True
    while grew:
        more = {pid for pid, parent in ppid.items() if parent in tree} - tree
        tree |= more
        grew = bool(more)
    cur = me
    while parents and ppid.get(cur, 0) > 1 and ppid[cur] not in tree:
        cur = ppid[cur]
        tree.add(cur)
    return tree


def parse_lsof(text: str) -> list[tuple[int, str]]:
    """(pid, path) pairs from `lsof -Fpn` output: `p123` starts a process, each `n/path` is a file or folder it holds."""
    out, pid = [], None
    for line in text.splitlines():
        if line.startswith("p") and line[1:].isdigit():
            pid = int(line[1:])
        elif line.startswith("n/") and pid is not None:
            out.append((pid, line[1:]))
    return out


def _held_paths() -> list[tuple[int, str]]:
    """Every (pid, path) a process holds as working folder or open file, as far as this account can see."""
    if not os.path.isdir("/proc/self"):  # macOS: one lsof for everything
        try:
            return parse_lsof(subprocess.run(["lsof", "-nP", "-w", "-Fpn"], capture_output=True, text=True, timeout=120, check=False, stdin=subprocess.DEVNULL).stdout)  # fmt: skip
        except (OSError, subprocess.SubprocessError):
            return []
    held = []
    for name in filter(str.isdigit, os.listdir("/proc")):
        with contextlib.suppress(OSError):
            held.append((int(name), os.readlink(f"/proc/{name}/cwd")))
        with contextlib.suppress(OSError):
            for fd in os.listdir(f"/proc/{name}/fd"):
                with contextlib.suppress(OSError):
                    held.append((int(name), os.readlink(f"/proc/{name}/fd/{fd}")))
    return held


def users_of(roots: list[str], ignore: set[int]) -> dict[str, list[int]]:
    """{root: pids} for the processes (not in `ignore`) with a working folder or open file inside a root. One scan."""
    real = {r: os.path.realpath(r) for r in roots}
    found: dict[str, list[int]] = {}
    for pid, target in _held_paths():
        target = target.removesuffix(" (deleted)")
        for root, rr in real.items():
            if pid not in ignore and (_under(target, rr) or _under(target, root)):
                found.setdefault(root, []).append(pid)
    return found


# --- one repo --------------------------------------------------------------------------------------------------------


@dataclass
class Item:
    repo: str
    path: str
    branch: str
    head: str
    action: str = "kept"  # removed | would-remove | kept | failed
    reason: str = ""
    bytes: int = 0


@dataclass
class Report:
    items: list[Item] = field(default_factory=list)
    refused: list[tuple[str, str]] = field(default_factory=list)  # (repo, why)
    dry_run: bool = False

    @property
    def removed(self) -> list[Item]:
        return [i for i in self.items if i.action in ("removed", "would-remove")]

    @property
    def kept(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for i in self.items:
            if i.action in ("kept", "failed"):
                key = "failed" if i.action == "failed" else i.reason
                out[key] = out.get(key, 0) + 1
        return out

    @property
    def freed(self) -> int:
        return sum(i.bytes for i in self.removed)

    def line(self) -> str:
        why = ", ".join(f"{n} {k}" for k, n in sorted(self.kept.items()))
        did, gone = ("would remove", "would be freed") if self.dry_run else ("removed", "freed")
        refused = f", refused {len(self.refused)} repo(s)" if self.refused else ""
        kept = f"kept {sum(self.kept.values())}" + (f" ({why})" if why else "")
        return f"ws: checked {len(self.items)}, {did} {len(self.removed)}, {kept}, {self.freed / GB:.1f} GB {gone}{refused}"

    def data(self) -> dict:
        return {
            "checked": len(self.items),
            "would_remove" if self.dry_run else "removed": len(self.removed),
            "kept": self.kept,
            "gb_freed": round(self.freed / GB, 3),
            "dry_run": self.dry_run,
            "refused": [{"repo": r, "why": w} for r, w in self.refused],
        }


def judge(wt: Worktree, idle_s: float, now: float, busy: dict[str, list[int]], require_pushed: bool, blind: bool = False) -> str:  # fmt: skip
    """Why this worktree must stay, or "" when it can go. The cheap checks come first. `blind` is a process list that
    does not show the caller's own parents: use can't be ruled out, so every worktree stays."""
    if wt.locked:
        return "locked"
    if blind:
        return "busy-unknown"
    if busy.get(wt.path):
        return "in-use"
    code, out = _git(wt.path, "status", "--porcelain", "--untracked-files=no")
    if code != 0 or out.strip():
        return "dirty"
    if not wt.branch and not _reaches(wt.path, wt.head, "refs/heads", "refs/remotes", "refs/tags"):
        return "unreachable"
    if require_pushed and not _reaches(wt.path, wt.head, "refs/remotes"):
        return "unpushed"
    if idle_s > 0 and newer_than(wt.path, now - idle_s):
        return "fresh"
    return ""


def prune_repo(
    repo: Path, idle_s: float, now: float, dry_run: bool, only: str | None, ignore: set[int], log, blind: bool = False
) -> list[Item]:
    """Check each worktree under `<repo>/.claude/worktrees` and remove the ones nothing needs. `only` limits it to one
    and also asks for its head to be pushed."""
    root = os.path.realpath(repo / SUBDIR)
    wts = [
        w for w in list_worktrees(repo) if os.path.realpath(w.path) != root and _under(os.path.realpath(w.path), root)
    ]
    if only is not None:
        wts = [w for w in wts if os.path.realpath(w.path) == os.path.realpath(only)]
        if not wts:
            raise WsError(
                "not_found", f"{only} is not a worktree of {repo} under {SUBDIR}", "`git worktree list` shows them"
            )
    busy = users_of([w.path for w in wts], ignore) if wts else {}
    items = []
    for wt in wts:
        item = Item(str(repo), wt.path, wt.branch, wt.head[:12])
        item.reason = judge(wt, idle_s, now, busy, only is not None, blind)
        if not item.reason:
            item.bytes = tree_bytes(wt.path)
            item.action = "would-remove" if dry_run else "removed"
            if not dry_run:
                if untouched(wt.path):  # git decides first: a worktree it will refuse keeps its modes
                    make_writable(wt.path)
                code, out = _git(repo, "worktree", "remove", wt.path)
                if code != 0:
                    item.action, item.bytes = "failed", 0
                    item.reason = (out.strip().splitlines() or ["git refused"])[-1][:160]
                else:
                    log(item)
        items.append(item)
    return items  # no closing `git worktree prune`: it would act on every registered worktree, not just these


# --- zones -----------------------------------------------------------------------------------------------------------


def _policy(env):
    from ws import policy

    try:
        return policy.load(policy.policy_path(dict(env)))
    except policy.PolicyError:
        return None


def zone_refusal(pol, caller: str, repo: str | Path) -> str | None:
    """Why a caller working in `caller` may not prune `repo`, or None. Fails closed on a policy that cannot be read."""
    from ws import policy

    if pol is None:
        return "the policy cannot be read"
    mine, theirs = policy.class_of(pol, caller), policy.class_of(pol, repo)
    if mine is None or theirs is None or theirs.name == mine.name:
        return None
    if theirs.access == "full" and (mine.access == "full" or mine.name == policy.CLASS_RESERVED):
        return None
    return f"it is in the {theirs.name} zone, outside this caller's"


def auto_repos(env, settings: Settings) -> list[Path]:
    """The repos `--auto` looks at: the folders the policy's classes name, `[worktrees] repos`, or `~/code/*` when there
    are no classes; only main checkouts that have a `.claude/worktrees` folder."""
    pol = _policy(env)
    globs = list(settings.repos)
    if pol is not None and pol.classes:
        globs += [r for cls in pol.classes.values() for r in cls.repos if r[:1] in ("/", "~")]
    elif not globs:
        globs = ["~/code/*"]
    home = dict(env).get("HOME") or os.path.expanduser("~")
    hits = (h for g in globs for h in glob.glob(g.replace("~", home, 1) if g.startswith("~") else g, recursive=True))
    found = {
        os.path.realpath(h): Path(h)
        for h in hits
        if os.path.isdir(os.path.join(h, ".git")) and os.path.isdir(os.path.join(h, SUBDIR))
    }
    return sorted(found.values())


# --- the log, the state and the lead ---------------------------------------------------------------------------------


def log_path(env=None) -> Path:
    return paths.state_dir(env) / "worktree-prune.log"


def state_path(env=None) -> Path:
    return paths.state_dir(env) / "worktree-prune.json"


def _log_removal(env, now: float):
    """A function that adds one tab-separated line (time, path, branch, head) per removal to `<state>/worktree-prune.log`."""

    def write(item: Item) -> None:
        path = log_path(env)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        when = datetime.fromtimestamp(now, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as f:
            f.write("\t".join([when, item.path, item.branch or "(detached)", item.head]) + "\n")

    return write


def read_state(env=None) -> dict:
    try:
        doc = json.loads(state_path(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return doc if isinstance(doc, dict) else {}


def write_state(env, doc: dict) -> None:
    path = state_path(env)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def disk_pct(where: str = "/") -> int:
    u = shutil.disk_usage(where)
    return round(100 * u.used / u.total)


def tell_lead(env, text: str) -> dict:
    """One alerts line, one push, and a `ws msg` to each lead session when the registry lets this caller send. Never raises."""
    sent = False
    with contextlib.suppress(Exception):  # a missing registry or store must not stop the prune
        from ws import msg, session

        for rec in (r for r in session.records() if r.role == "lead"):
            with contextlib.suppress(WsError, OSError):
                msg.send(rec.session_id, "note", text, tier="urgent", allow_person=True)
                sent = True
    pushed = jobwatch.push(env, text, "the disk is filling", title="ws worktrees") is True
    with contextlib.suppress(OSError):
        jobwatch._write_line(env, {"event": "worktrees-disk", "msg": sent, "pushed": pushed})
    return {"msg": sent, "pushed": pushed}


# --- the command -----------------------------------------------------------------------------------------------------


def run_prune(env, folders: list[Path], settings: Settings, now: float, dry_run: bool, only: str | None = None, min_idle: float | None = None, caller: str | None = None) -> Report:  # fmt: skip
    """Prune the repo each folder is in, when the caller's zone allows it; refuse the others without running git there."""
    report, pol, caller = Report(dry_run=dry_run), _policy(env), caller or os.getcwd()
    idle_s = (settings.idle_hours if min_idle is None else min_idle) * 3600
    ppid = _ppids()
    ignore, blind = own_tree(parents=only is not None, ppid=ppid), not sees_own_parents(ppid)
    for folder in folders:
        why = zone_refusal(pol, caller, folder)  # judged on the path alone, before git reads anything there
        if why:
            report.refused.append((str(folder), why))
            continue
        main = main_checkout(folder)
        why = zone_refusal(pol, caller, main)  # a folder in one zone can belong to a repo in another
        if why:
            report.refused.append((str(main), why))
            continue
        log = _log_removal(env, now)
        report.items += prune_repo(main, idle_s, now, dry_run, only, ignore, log, blind)
    return report


def run_auto(env, settings: Settings, now: float, dry_run: bool, disk=disk_pct, notify=tell_lead) -> dict:
    """The timer's call: look at the disk, prune when it is due or the disk is at the limit, tell the lead on pressure."""
    before, state = disk(), read_state(env)
    last, alerted = (state[k] if isinstance(state.get(k), (int, float)) else 0 for k in ("last_run", "last_alert"))
    pressure, every = before >= settings.disk_pct, settings.interval_hours * 3600
    if now - last < every and not pressure:
        return {"ran": False, "disk_pct": before, "worktrees_pruned": 0, "gb_freed": 0.0}
    report = run_prune(env, auto_repos(env, settings), settings, now, dry_run)
    card = {"worktrees_pruned": len(report.removed), "gb_freed": round(report.freed / GB, 3), "disk_pct": disk()}
    out = {"ran": True, "pressure": pressure, **card, "report": report}
    if not dry_run:
        if pressure and now - alerted >= every:
            text = f"ws: the disk is at {before}% (limit {settings.disk_pct:g}%). Pruned {card['worktrees_pruned']} agent worktree(s), {card['gb_freed']} GB; now {card['disk_pct']}%."
            out["told"], alerted = notify(env, text), now
        write_state(env, {"last_run": now, "last_alert": alerted, **card})
    return out


def cmd_prune(args: argparse.Namespace) -> int:
    env, now = os.environ, time.time()
    settings = load_settings(env)
    if args.min_idle is not None and args.min_idle < 0:
        raise WsError("bad_usage", "--min-idle must be 0 or more hours", "give hours, e.g. 0 or 12", 2)
    if args.auto:
        if args.only or args.repo or args.min_idle is not None:
            raise WsError(
                "bad_usage", "--auto chooses its own repos and idle time", "leave out --repo, --only and --min-idle", 2
            )
        res = run_auto(env, settings, now, args.dry_run)
        report = res.get("report")
        print(report.line() if report else f"ws: disk at {res['disk_pct']}%: nothing due")
        if args.json:
            output.result({k: v for k, v in res.items() if k != "report"} | (report.data() if report else {}))
        return 0
    folders = [Path(r) for r in args.repo] if args.repo else [Path(args.only or os.getcwd())]
    report = run_prune(env, folders, settings, now, args.dry_run, args.only, args.min_idle)
    if report.refused and not report.items:
        repo, why = report.refused[0]
        raise WsError("refused", f"{repo}: {why}", "a repo is pruned from inside its own zone", 3)
    print(report.line())
    for repo, why in report.refused:
        print(f"ws: refused {repo}: {why}")
    failed = [i for i in report.items if i.action == "failed"]
    for i in failed:
        print(f"ws: could not remove {i.path}: {i.reason}")
    if args.json:
        output.result(report.data())
    return 1 if failed else 0


def register(sub) -> None:
    w = sub.add_parser("worktrees", help="remove finished agent worktrees (prune)")
    p = w.add_subparsers(dest="worktrees_command", required=True).add_parser(
        "prune",
        help="remove idle, clean agent worktrees under .claude/worktrees whose commits are safe; keep the branches",
    )
    p.add_argument(
        "--repo", action="append", metavar="R", help="a repo to prune (repeatable; default: the current one)"
    )
    p.add_argument("--only", metavar="PATH", help="prune this one worktree, which must also be pushed")
    p.add_argument("--min-idle", type=float, metavar="HOURS", help="idle hours before a worktree may go (default 12)")
    p.add_argument("--dry-run", action="store_true", help="change nothing; print what would happen")
    p.add_argument(
        "--auto", action="store_true", help="the timer's run: the policy's repos, the disk check, the lead told"
    )
    p.set_defaults(func=cmd_prune)
