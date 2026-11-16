"""commit-wall: a commit hook that refuses a commit whose added lines or message hold a pattern you supplied.

`ws commit-wall check` reads the staged diff (only the lines the commit adds) and the commit message, and exits 1 when a
line matches a pattern in `<config>/commit-wall/patterns.toml`. ws ships no pattern: what must never reach a commit (a
client's name, a host, a key shape) is yours to say, so the wall names none of it. A hit prints the file, the line and the
pattern's name, never the text that matched, so a log or a terminal doesn't carry what the wall exists to keep out.

A merge commit is scanned for the lines new to the merge: a line that came in from the other side in the same file (it is on
that branch, and was scanned or excepted there) is left out; the same text in another file is not. Nothing is skipped: every
file is read as text (`--text --no-textconv`, so a NUL byte, a `-diff` attribute or padding can't hide a line), the lines are split
on newlines only, invalid UTF-8 is read as it is, and the file names are scanned too. Text is matched as written and again
after Unicode NFKC normalisation. A commit message is scanned in full, `#` lines included (`-m` and `--cleanup=verbatim` keep
them). `--stage commit-msg FILE` scans the message in FILE. With no pattern (`patterns.toml` missing or empty) the check
refuses: a wall with nothing to look for is a gap, not a pass.

`<config>/commit-wall/exceptions.toml` lists what to let through, as `[[allow]]` tables with any of `pattern` (its
name), `path` (a glob) and `text` (a substring of the line); at least one, and every one given has to match. Its header
says only a person adds to it. The whole folder is one of ws's control files: the gate and the wall refuse a session's
writes to it, so an agent can't loosen the wall it commits under.

`ws commit-wall install` points one repository's `core.hooksPath` at a folder of ws's own (`<config>/commit-wall/hooks/ID`)
holding `pre-commit`, `pre-merge-commit` and `commit-msg`, and records the value it replaced in `<config>/commit-wall/prior.json`,
keyed on the repository's common git folder (every worktree of it shares one record). The hooks carry the config folder they were
installed with, so another `WS_HOME` in the committing shell can't point them at an empty one. The other
hooks the repository had (in the old `core.hooksPath`, or in `.git/hooks`) are forwarded, and the old `pre-commit` and
`commit-msg` run after the check passes, so installing the wall takes no hook away; a hook added to the old folder later
needs `install` again. `uninstall` puts the recorded value back (or removes the setting, when there was none). A hook
that can't run `ws` blocks the commit: the wall fails closed.
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ws import output, paths, selfcmd
from ws.output import WsError

COMMAND = "commit-wall"

FOLDER = "commit-wall"
GIT_SECONDS = 30
HOOKS = ("pre-commit", "pre-merge-commit", "commit-msg")
EXCEPTIONS_HEADER = (
    "# Only a person adds to this file. A session that edits it is working around the commit wall.\n"
    "# Each [[allow]] needs at least one of: pattern (a pattern's name), path (a glob), text (a substring of the line).\n"
    "# Every one you give has to match for the line to go through.\n"
)
PATTERNS_HEADER = (
    "# What must never reach a commit. ws ships no pattern; each one is yours.\n"
    '# [[pattern]]\n# name = "client-names"\n# terms = ["Example Client"]   # literal, any case\n'
    '# [[pattern]]\n# name = "host"\n# regex = "internal[.]example[.]org"   # a Python regular expression\n'
)


@dataclass(frozen=True)
class Pattern:
    name: str
    rx: re.Pattern


@dataclass(frozen=True)
class Allow:
    pattern: str = ""
    path: str = ""
    text: str = ""


@dataclass(frozen=True)
class Hit:
    where: str
    line: int
    pattern: str


def folder(env=None) -> Path:
    return paths.config_dir(env) / FOLDER


def _load_toml(file: Path) -> dict:
    try:
        text = file.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as e:
        raise WsError("bad_config", f"{file}: {e.strerror or e}", "fix the file's permissions", 1) from e
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise WsError("bad_config", f"{file}: {e}", "fix the file, or move it away", 1) from e


def _base(env=None, base: Path | None = None) -> Path:
    return base if base is not None else folder(env)


def load_patterns(env=None, base: Path | None = None) -> list[Pattern]:
    """The patterns in `patterns.toml`; none when the file isn't there. A bad entry refuses the whole file."""
    file = _base(env, base) / "patterns.toml"
    found: list[Pattern] = []
    for i, entry in enumerate(_load_toml(file).get("pattern", []), 1):
        where = f"{file}: [[pattern]] {i}"
        name = entry.get("name")
        if not isinstance(name, str) or not name or "\n" in name:
            raise WsError("bad_config", f"{where}: name must be text", "give each pattern a name", 1)
        terms, regex = entry.get("terms"), entry.get("regex")
        if (terms is None) == (regex is None):
            raise WsError("bad_config", f"{where}: give exactly one of terms and regex", "see the file's header", 1)
        try:
            if terms is not None:
                if not (isinstance(terms, list) and terms and all(isinstance(t, str) and t for t in terms)):
                    raise WsError("bad_config", f"{where}: terms must be a list of text", "see the file's header", 1)
                rx = re.compile("|".join(re.escape(t) for t in terms), re.IGNORECASE)
            else:
                if not isinstance(regex, str) or not regex:
                    raise WsError("bad_config", f"{where}: regex must be text", "see the file's header", 1)
                rx = re.compile(regex)
        except re.error as e:
            raise WsError("bad_config", f"{where}: {e}", "fix the regular expression", 1) from e
        found.append(Pattern(name, rx))
    return found


def load_exceptions(env=None, base: Path | None = None) -> list[Allow]:
    file = _base(env, base) / "exceptions.toml"
    out: list[Allow] = []
    for i, entry in enumerate(_load_toml(file).get("allow", []), 1):
        got = {k: entry.get(k, "") for k in ("pattern", "path", "text")}
        if not any(got.values()) or not all(isinstance(v, str) for v in got.values()) or set(entry) - set(got):
            raise WsError(
                "bad_config",
                f"{file}: [[allow]] {i} needs pattern, path or text, and nothing else",
                "see its header",
                1,
            )
        out.append(Allow(**got))
    return out


def _allowed(allows: list[Allow], path: str, pattern: str, text: str) -> bool:
    return any(
        (not a.pattern or a.pattern == pattern)
        and (not a.path or fnmatch.fnmatchcase(path, a.path))
        and (not a.text or a.text in text)
        for a in allows
    )


def _run(repo: str, *args: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", "-c", "core.quotePath=false", "-C", repo, *args],
            capture_output=True,
            timeout=GIT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as e:
        raise WsError(
            "git_failed", f"git {args[0]}: {e}", "is git installed, and is this folder a repository?", 1
        ) from e


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def _git(repo: str, *args: str, ok=(0,)) -> str:
    done = _run(repo, *args)
    if done.returncode not in ok:
        raise WsError(
            "not_a_repository",
            (_decode(done.stderr) or _decode(done.stdout)).strip() or f"git {args[0]} failed",
            "run it in a repository",
            1,
        )
    return _decode(done.stdout)


# The same options for every diff the wall reads: text for every file (a NUL byte or a `-diff` attribute can't turn a file into
# "binary"), no filters, no renames, and fixed path prefixes (diff.mnemonicPrefix and diff.noprefix would change the paths).
DIFF_CONFIG = ("-c", "diff.noprefix=false", "-c", "diff.mnemonicPrefix=false")
DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-renames",
    "--text",
    "-U0",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)
_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff: str) -> list[tuple[str, int, str]]:
    """(path, line number, text) of each line a unified diff adds. Lines split on newlines only. A `+++` line is a header only
    between a file's `diff --git` line and its first hunk, so an added line that starts `++` is a line like any other."""
    out: list[tuple[str, int, str]] = []
    path, n, header = "", 0, False
    for line in diff.split("\n"):
        if line.startswith("diff --git "):
            header, path = True, ""
        elif header:
            if line.startswith("+++ "):
                name = line[4:].split("\t")[0]
                path = name.removeprefix("b/") if name != "/dev/null" else path
            elif m := _HUNK.match(line):
                header, n = False, int(m.group(1))
        elif m := _HUNK.match(line):
            n = int(m.group(1))
        elif line.startswith("+"):
            out.append((path, n, line[1:]))
            n += 1
    return out


def _diff(repo: str, cmd: str, *rest: str) -> str:
    """`git <cmd>` with the wall's diff options and `rest` (the revisions); the output is read as it is, never as strict UTF-8."""
    done = _run(repo, *DIFF_CONFIG, cmd, *DIFF_OPTIONS, *rest)
    if done.returncode:
        raise WsError(
            "not_a_repository", _decode(done.stderr).strip() or f"git {cmd} failed", "run it in a repository", 1
        )
    return _decode(done.stdout)


def staged_lines(repo: str) -> list[tuple[str, int, str]]:
    """The lines the commit adds, and the names of the files it changes, all of them: nothing is skipped. A merge in progress leaves
    out the lines the other side added to the same file."""
    if _git(repo, "rev-parse", "-q", "--verify", "HEAD", ok=(0, 1)).strip():
        lines = added_lines(_diff(repo, "diff-index", "--cached", "-p", "HEAD"))
        names = _git(repo, "diff-index", "--cached", "--name-only", "--no-renames", "-z", "HEAD").split("\0")
    else:  # the first commit: everything staged is new (the empty tree isn't an object a fresh repository has)
        lines = added_lines(_diff(repo, "diff", "--cached"))
        names = _git(repo, "ls-files", "--cached", "-z").split("\0")
    named = [(n, 0, n) for n in names if n]
    if _git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD", ok=(0, 1)).strip():
        mb = _git(repo, "merge-base", "HEAD", "MERGE_HEAD", ok=(0, 1)).strip()
        if mb:
            came = {(p, tx) for p, _, tx in added_lines(_diff(repo, "diff", mb, "MERGE_HEAD"))}
            lines = [x for x in lines if (x[0], x[2]) not in came]
    return lines + named


def message_lines(file: str) -> list[tuple[str, int, str]]:
    try:
        text = _decode(Path(file).read_bytes())
    except OSError as e:
        raise WsError(
            "bad_input", f"{file}: {e.strerror or e}", "pass the commit message file git gives the hook", 2
        ) from e
    return [("(commit message)", i, t) for i, t in enumerate(text.split("\n"), 1)]


def scan(lines, patterns: list[Pattern], allows: list[Allow]) -> list[Hit]:
    hits: list[Hit] = []
    for path, n, text in lines:
        folded = unicodedata.normalize("NFKC", text)
        for p in patterns:
            if (p.rx.search(text) or p.rx.search(folded)) and not _allowed(allows, path, p.name, text):
                hits.append(Hit(path, n, p.name))
    return hits


def cmd_check(args: argparse.Namespace) -> int:
    repo = os.path.abspath(args.repo)
    base = Path(args.wall_dir) if args.wall_dir else folder()
    patterns, allows = load_patterns(base=base), load_exceptions(base=base)
    if not patterns:
        raise WsError(
            "no_patterns",
            f"{base / 'patterns.toml'} holds no pattern (missing or empty), so the commit wall can't check anything",
            "add a [[pattern]] to it (see its header), or run `ws commit-wall uninstall`",
            1,
        )
    if args.stage == "commit-msg":
        if not args.file:
            raise WsError(
                "bad_input", "commit-msg needs the message file", "ws commit-wall check --stage commit-msg FILE", 2
            )
        lines = message_lines(args.file)
    else:
        lines = staged_lines(repo)
    hits = scan(lines, patterns, allows)
    data = {
        "patterns": len(patterns),
        "scanned_lines": len(lines),
        "hits": [{"where": h.where, "line": h.line, "pattern": h.pattern} for h in hits],
    }
    if hits:
        shown = "; ".join(f"{h.where}:{h.line} matches {h.pattern!r}" for h in hits[:10])
        more = f" (and {len(hits) - 10} more)" if len(hits) > 10 else ""
        raise WsError(
            "commit_wall_hit",
            f"the commit wall found {len(hits)} line(s): {shown}{more}",
            f"take the text out; a person can add a false hit to {base / 'exceptions.toml'}",
            1,
            data,
        )
    if args.json:
        output.result(data)
    return 0


def ws_argv() -> list[str]:
    """The command a hook runs for ws."""
    found = shutil.which("ws")
    return [found] if found else selfcmd.argv()  # isolated, so a `ws/` folder in the repository is never run


def _repo_id(repo: str) -> str:
    """One id per repository, however many worktrees it has: the real path of its common git folder."""
    common = _git(repo, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    return hashlib.sha256(os.path.realpath(common).encode()).hexdigest()[:12]


def _prior_file(env=None) -> Path:
    return folder(env) / "prior.json"


def _read_prior(env=None) -> dict:
    try:
        return json.loads(_prior_file(env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_prior(data: dict, env=None) -> None:
    file = _prior_file(env)
    file.parent.mkdir(parents=True, exist_ok=True)
    tmp = file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(file)


def _script(name: str, ws: list[str], prior_dir: str, wall_dir: Path) -> str:
    """One hook of ours: the check (for pre-commit, pre-merge-commit and commit-msg), then the hook the repository had. The check
    is told which config folder to read, so a different WS_HOME in the committing shell can't make it read an empty one."""
    run = ""
    check = f"{shlex.join(ws)} commit-wall check --wall-dir {shlex.quote(str(wall_dir))}"
    if name in ("pre-commit", "pre-merge-commit"):
        run = f"{check} --stage pre-commit || exit 1\n"
    elif name == "commit-msg":
        run = f'{check} --stage commit-msg "$1" || exit 1\n'
    return (
        "#!/bin/sh\n# written by `ws commit-wall install`; `ws commit-wall uninstall` removes it\n"
        f"{run}"
        f"old={shlex.quote(prior_dir)}/{name}\n"
        '[ -x "$old" ] && exec "$old" "$@"\nexit 0\n'
    )


def _hook_names(prior_dir: str) -> list[str]:
    try:
        entries = sorted(os.listdir(prior_dir))
    except OSError:
        entries = []
    found = [n for n in entries if not n.endswith(".sample") and os.access(os.path.join(prior_dir, n), os.X_OK)]
    return sorted({*HOOKS, *found})


def cmd_install(args: argparse.Namespace) -> int:
    repo = os.path.realpath(_git(os.path.abspath(args.repo), "rev-parse", "--show-toplevel").strip())
    rid = _repo_id(repo)
    here = folder() / "hooks" / rid
    raw = _git(repo, "config", "--local", "--get", "core.hooksPath", ok=(0, 1)).strip()
    current = _git(repo, "config", "--local", "--type=path", "--get", "core.hooksPath", ok=(0, 1)).strip()  # ~ expanded
    prior = _read_prior()
    if current and os.path.realpath(os.path.join(repo, current)) == os.path.realpath(here):
        raise WsError(
            "already_installed", f"{repo} already uses the commit wall", "run `ws commit-wall uninstall` first", 1
        )
    if rid not in prior:
        prior[rid] = {"repo": repo, "prior": raw or None}
    old_dir = (
        os.path.realpath(os.path.join(repo, current))
        if current
        else os.path.realpath(os.path.join(repo, _git(repo, "rev-parse", "--git-path", "hooks").strip()))
    )
    here.mkdir(parents=True, exist_ok=True)
    ws = ws_argv()
    for name in _hook_names(old_dir):
        file = here / name
        file.write_text(_script(name, ws, old_dir, folder()), encoding="utf-8")
        file.chmod(file.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    for f in (folder() / "patterns.toml", folder() / "exceptions.toml"):
        if not f.exists():
            f.write_text(PATTERNS_HEADER if f.name == "patterns.toml" else EXCEPTIONS_HEADER, encoding="utf-8")
    _write_prior(prior)
    _git(repo, "config", "--local", "core.hooksPath", str(here))
    data = {"repo": repo, "hooks": str(here), "replaced": prior[rid]["prior"]}
    if args.json:
        output.result(data)
    else:
        print(
            f"ws: the commit wall is on for {repo}; it refuses every commit until {folder() / 'patterns.toml'} holds a pattern"
        )
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    repo = os.path.realpath(_git(os.path.abspath(args.repo), "rev-parse", "--show-toplevel").strip())
    rid = _repo_id(repo)
    prior = _read_prior()
    if rid not in prior:
        raise WsError("not_installed", f"the commit wall isn't installed for {repo}", "nothing to undo", 1)
    old = prior[rid]["prior"]
    if old:
        _git(repo, "config", "--local", "core.hooksPath", old)
    else:
        _git(repo, "config", "--local", "--unset", "core.hooksPath", ok=(0, 5))
    shutil.rmtree(folder() / "hooks" / rid, ignore_errors=True)
    del prior[rid]
    _write_prior(prior)
    data = {"repo": repo, "restored": old}
    if args.json:
        output.result(data)
    else:
        print(f"ws: the commit wall is off for {repo}; core.hooksPath is {'back to ' + old if old else 'unset'}")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    patterns, allows = load_patterns(), load_exceptions()
    data = {
        "patterns": len(patterns),
        "exceptions": len(allows),
        "repos": sorted(v["repo"] for v in _read_prior().values()),
    }
    if args.json:
        output.result(data)
    else:
        print(
            f"ws: {data['patterns']} pattern(s), {data['exceptions']} exception(s), on for {len(data['repos'])} repo(s)"
        )
        for r in data["repos"]:
            print(f"  {r}")
    return 0


def register(sub) -> None:
    c = sub.add_parser("commit-wall", help="refuse a commit whose added lines or message match a pattern you supplied")
    cs = c.add_subparsers(dest="commit_wall_command", required=True)
    k = cs.add_parser("check", help="scan the staged diff (or a commit message) now; exit 1 on a hit")
    k.add_argument("--stage", choices=("pre-commit", "commit-msg"), default="pre-commit")
    k.add_argument("--repo", default=".", help="the repository (default: here)")
    k.add_argument("--wall-dir", help="the config folder to read (the hooks pass the one they were installed with)")
    k.add_argument("file", nargs="?", help="the message file, for --stage commit-msg")
    k.set_defaults(func=cmd_check)
    for name, fn, text in (
        ("install", cmd_install, "point a repository's core.hooksPath at the wall, recording the old value"),
        ("uninstall", cmd_uninstall, "put the recorded core.hooksPath back"),
    ):
        i = cs.add_parser(name, help=text)
        i.add_argument("--repo", default=".", help="the repository (default: here)")
        i.set_defaults(func=fn)
    cs.add_parser("status", help="the patterns, the exceptions and the repositories it is on for").set_defaults(
        func=cmd_status
    )
