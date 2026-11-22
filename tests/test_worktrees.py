"""`ws worktrees prune`: only idle, clean worktrees under .claude/worktrees go; the branches stay; zones are kept.

Each test builds a scratch repo with real git, so the safety rules are judged by git itself, not by a stand-in.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import tomllib
from pathlib import Path

import pytest

from ws import cli, doctor, install, osinfo, paths, policy, tmpclean, worktrees
from ws.output import WsError
from ws.profile import dumps, profile_path

needs_git = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
needs_proc = pytest.mark.skipif(not Path("/proc/self").exists(), reason="reads /proc")
HOUR = 3600
IDENT = {"GIT_AUTHOR_NAME": "T", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "T", "GIT_COMMITTER_EMAIL": "t@example.com"}  # fmt: skip


def git(cwd, *args) -> str:
    done = subprocess.run(["git", "-c", "commit.gpgsign=false", *args], cwd=cwd, capture_output=True, text=True, env={**os.environ, **IDENT}, check=False)  # fmt: skip
    assert done.returncode == 0, f"git {args}: {done.stderr}{done.stdout}"
    return done.stdout.strip()


def age(path: Path, hours: float = 48) -> None:
    """Make every file under `path` look `hours` old."""
    then = time.time() - hours * HOUR
    for root, dirs, files in os.walk(path):
        for name in [*dirs, *files]:
            with contextlib.suppress(OSError):
                os.utime(os.path.join(root, name), (then, then), follow_symlinks=False)
    os.utime(path, (then, then))


@pytest.fixture
def box(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    e = {"HOME": str(home), "WS_HOME": str(home / ".ws")}
    for k, v in e.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(home)
    monkeypatch.setattr(worktrees, "_readlink", ROOT_NS)  # the runner may itself be in a sandbox or container
    return tmp_path


@pytest.fixture
def repo(box):
    root = box / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "main")
    (root / "README").write_text("one\n")
    (root / ".gitignore").write_text("cache/\n.claude/\n")  # caches and virtualenvs are ignored, as in a real repo
    git(root, "add", "README", ".gitignore")
    git(root, "commit", "-q", "-m", "first")
    return root


def add(repo: Path, name: str, idle: bool = True) -> Path:
    """A worktree under .claude/worktrees on its own branch; `idle` ages it (and git's admin files for it) two days."""
    wt = repo / ".claude" / "worktrees" / name
    git(repo, "worktree", "add", "-q", "-b", f"b-{name}", str(wt))
    if idle:
        age(wt)
        age(repo / ".git" / "worktrees" / name)
    return wt


def prune(repo: Path, dry_run: bool = False, **kw) -> worktrees.Report:
    return worktrees.run_prune(
        os.environ, [repo], worktrees.Settings(), time.time(), dry_run, caller=str(repo), **kw
    )  # fmt: skip


def registered(repo: Path) -> set[str]:
    return {Path(w.path).name for w in worktrees.list_worktrees(repo)}


def reasons(report: worktrees.Report) -> dict[str, str]:
    return {Path(i.path).name: i.reason or i.action for i in report.items}


@needs_git
def test_only_idle_clean_worktrees_go(repo):
    add(repo, "locked")
    git(repo, "worktree", "lock", str(repo / ".claude/worktrees/locked"))
    dirty = add(repo, "dirty")
    (dirty / "README").write_text("changed\n")
    age(dirty)
    add(repo, "fresh", idle=False)
    add(repo, "idle")
    report = prune(repo)
    assert reasons(report) == {"locked": "locked", "dirty": "dirty", "fresh": "fresh", "idle": "removed"}
    assert registered(repo) == {"locked", "dirty", "fresh"}
    assert not (repo / ".claude/worktrees/idle").exists()
    assert report.line().startswith("ws: checked 4, removed 1, kept 3 (1 dirty, 1 fresh, 1 locked), ")
    assert report.line().endswith("GB freed")


@needs_git
def test_a_read_only_cache_inside_still_removes(repo):
    wt = add(repo, "cache")
    mod = wt / "cache" / "mod"
    mod.mkdir(parents=True)
    (mod / "x.go").write_text("x")
    os.chmod(mod / "x.go", 0o444)
    os.chmod(mod, 0o555)
    os.chmod(wt / "cache", 0o555)
    age(wt)
    assert reasons(prune(repo)) == {"cache": "removed"}
    assert not wt.exists() and "cache" not in registered(repo)


@needs_git
@needs_proc
def test_a_process_inside_keeps_it(repo):
    wt = add(repo, "busy")
    proc = subprocess.Popen(["sleep", "60"], cwd=wt)
    try:
        # the caller's own children are not counted (a prune's own git calls run inside the folder), so ignore nothing here
        items = worktrees.prune_repo(repo, 12 * HOUR, time.time(), False, None, set(), lambda i: None)
        assert [(Path(i.path).name, i.reason) for i in items] == [("busy", "in-use")] and wt.exists()
        assert worktrees.own_tree(parents=False) >= {os.getpid(), proc.pid}
        assert reasons(prune(repo)) == {"busy": "removed"}  # the default run ignores its own tree: this child is ours
    finally:
        proc.kill()
        proc.wait()


@needs_git
def test_dry_run_changes_nothing_and_says_the_same(repo, box):
    wt = add(repo, "idle")
    before = prune(repo, dry_run=True)
    assert wt.exists() and "idle" in registered(repo) and not worktrees.log_path(os.environ).exists()
    assert before.line().startswith("ws: checked 1, would remove 1, kept 0,") and "would be freed" in before.line()
    after = prune(repo)
    assert reasons(after) == {"idle": "removed"} and not wt.exists()


@needs_git
def test_a_detached_head_is_kept_unless_a_ref_reaches_it(repo):
    lost = add(repo, "lost")
    git(lost, "checkout", "-q", "--detach")
    (lost / "new").write_text("n")
    git(lost, "add", "new")
    git(lost, "commit", "-q", "-m", "only here")  # reached by no branch, remote or tag
    age(lost)
    age(repo / ".git" / "worktrees" / "lost")
    safe = add(repo, "safe")
    git(safe, "checkout", "-q", "--detach")  # the same commit as main: a branch reaches it
    age(safe)
    age(repo / ".git" / "worktrees" / "safe")
    assert reasons(prune(repo)) == {"lost": "unreachable", "safe": "removed"}
    assert lost.exists() and "lost" in registered(repo)


@needs_git
def test_a_second_run_removes_nothing_and_the_branch_survives(repo):
    add(repo, "once")
    first, second = prune(repo), prune(repo)
    assert len(first.removed) == 1 and second.items == [] and second.removed == []
    assert "b-once" in git(repo, "branch", "--list", "b-once")
    line = worktrees.log_path(os.environ).read_text().splitlines()
    assert len(line) == 1 and line[0].split("\t")[1:3] == [str(repo / ".claude/worktrees/once"), "b-once"]


@needs_git
def test_a_removal_git_refuses_is_reported_and_leaves_the_worktree_registered(repo, capsys):
    wt = add(repo, "untracked")
    (wt / "notes.txt").write_text("not in git, not ignored")  # git will not remove a folder holding this
    age(wt)
    other = add(repo, "fine")
    report = prune(repo)
    assert (
        reasons(report)["fine"] == "removed"
        and {Path(i.path).name: i.action for i in report.items}["untracked"] == "failed"
    )
    assert wt.exists() and "untracked" in registered(repo) and not other.exists()
    assert report.kept == {"failed": 1}
    code = cli.main(["worktrees", "prune", "--repo", str(repo)])  # the command says so and exits 1
    assert code == 1 and "could not remove" in capsys.readouterr().out


@needs_git
def test_only_wants_the_head_pushed_and_counts_the_callers_parents_as_its_own(repo, box):
    remote = box / "remote.git"
    git(box, "init", "-q", "--bare", str(remote))
    git(repo, "remote", "add", "origin", str(remote))
    wt = add(repo, "done")
    (wt / "f").write_text("f")
    git(wt, "add", "f")
    git(wt, "commit", "-q", "-m", "work")
    age(wt)
    age(repo / ".git" / "worktrees" / "done")
    kept = prune(repo, only=str(wt), min_idle=0)
    assert reasons(kept) == {"done": "unpushed"} and wt.exists()
    git(wt, "push", "-q", "origin", "b-done")
    gone = prune(repo, only=str(wt), min_idle=0)
    assert reasons(gone) == {"done": "removed"} and not wt.exists()
    assert os.getppid() in worktrees.own_tree(parents=True) and os.getppid() not in worktrees.own_tree(parents=False) - {os.getpid()}  # fmt: skip
    with pytest.raises(WsError) as err:
        prune(repo, only=str(repo))  # the main checkout is not a worktree to prune
    assert err.value.kind == "not_found"


@needs_git
def test_a_repo_outside_the_callers_zone_is_refused_before_git_runs_in_it(repo, box, monkeypatch):
    other = box / "walled"
    other.mkdir()
    pol = paths.config_dir(os.environ) / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(f'[classes.open]\naccess = "full"\nrepos = ["{repo}/**"]\n[classes.w]\naccess = "walled"\nos_user = "svc"\nrepos = ["{other}/**"]\n')  # fmt: skip
    monkeypatch.setattr(worktrees, "main_checkout", lambda f: pytest.fail("git was run in a refused repo"))
    report = worktrees.run_prune(os.environ, [other], worktrees.Settings(), time.time(), False, caller=str(repo))
    assert report.items == [] and report.refused and "w zone" in report.refused[0][1]
    assert not worktrees.log_path(os.environ).exists()
    code = cli.main(["worktrees", "prune", "--repo", str(other), "--json"])
    assert code == 3  # `refused`
    # the walled zone's own job is let in; a caller in no class (a timer) reaches `full` classes only
    pol_ = policy.load(pol)
    assert worktrees.zone_refusal(pol_, str(other), other) is None
    assert worktrees.zone_refusal(pol_, str(box / "elsewhere"), repo) is None
    assert worktrees.zone_refusal(pol_, str(box / "elsewhere"), other)
    pol.write_text("[not valid")
    assert worktrees.zone_refusal(worktrees._policy(os.environ), str(repo), repo) == "the policy cannot be read"


@needs_git
def test_auto_prunes_when_due_or_on_pressure_and_tells_the_lead_once(repo, box):
    add(repo, "a")
    told: list[str] = []

    def notify(env, text):
        told.append(text)
        return {}

    st = worktrees.Settings(repos=(str(repo),))
    now = time.time()
    quiet = worktrees.run_auto(os.environ, st, now, False, disk=lambda: 40, notify=notify)
    assert quiet["ran"] and quiet["worktrees_pruned"] == 1 and told == []  # the first run is due
    again = worktrees.run_auto(os.environ, st, now + HOUR, False, disk=lambda: 40, notify=notify)
    assert again == {"ran": False, "disk_pct": 40, "worktrees_pruned": 0, "gb_freed": 0.0}
    add(repo, "b")
    hot = worktrees.run_auto(os.environ, st, now + 2 * HOUR, False, disk=lambda: 90, notify=notify)  # fmt: skip
    assert hot["ran"] and hot["pressure"] and hot["worktrees_pruned"] == 1 and "90%" in told[0]
    worktrees.run_auto(os.environ, st, now + 3 * HOUR, False, disk=lambda: 90, notify=notify)  # fmt: skip
    assert len(told) == 1  # at most one message per interval
    saved = worktrees.read_state(os.environ)
    assert {"worktrees_pruned", "gb_freed", "disk_pct"} <= set(saved)


def test_settings_come_from_the_profile_and_a_bad_one_names_its_key(box):
    assert worktrees.load_settings(os.environ) == worktrees.Settings()
    p = profile_path(os.environ)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(dumps({"worktrees": {"idle_hours": 1, "disk_pct": 70}}), encoding="utf-8")
    assert (worktrees.load_settings(os.environ).idle_hours, worktrees.load_settings(os.environ).disk_pct) == (1, 70)
    p.write_text(dumps({"worktrees": {"idle_hours": "soon"}}), encoding="utf-8")
    with pytest.raises(WsError, match="idle_hours"):
        worktrees.load_settings(os.environ)


def test_lsof_output_is_read_as_process_and_path_pairs():
    text = "p12\nfcwd\nn/work/a\nf4\nn/work/b/c\np99\nn/other\nnpipe\n"
    assert worktrees.parse_lsof(text) == [(12, "/work/a"), (12, "/work/b/c"), (99, "/other")]


def test_the_launchd_agent_runs_the_auto_prune_every_half_hour(box):
    from ws.roles import launchd

    plist = launchd.render_worktrees(dict(os.environ), ["/opt/ws"])
    assert ">worktrees<" in plist and ">--auto<" in plist and "<integer>1800</integer>" in plist


def stat_mode(path: Path) -> int:
    return os.lstat(path).st_mode & 0o777


@needs_git
def test_a_worktree_outside_the_folder_keeps_its_record_even_when_its_folder_moved(repo, box):
    add(repo, "idle")
    elsewhere = box / "elsewhere"
    git(repo, "worktree", "add", "-q", "-b", "b-away", str(elsewhere))
    (elsewhere / "staged").write_text("s")
    git(elsewhere, "add", "staged")  # staged work lives in git's admin folder for that worktree
    elsewhere.rename(box / "moved")  # git can't find the folder now, so a `git worktree prune` would drop the record
    assert reasons(prune(repo)) == {"idle": "removed"}
    assert "elsewhere" in registered(repo)
    assert (repo / ".git" / "worktrees" / "elsewhere" / "index").exists()


@needs_git
def test_a_full_zone_folder_whose_main_checkout_is_walled_is_refused(box):
    full, walled = box / "open", box / "walled"
    full.mkdir()
    walled.mkdir()
    git(walled, "init", "-q", "-b", "main")
    (walled / "f").write_text("f")
    git(walled, "add", "f")
    git(walled, "commit", "-q", "-m", "first")
    inside = add(walled, "idle")  # a worktree the walled zone owns, idle and clean
    linked = full / "linked"
    git(walled, "worktree", "add", "-q", "-b", "b-linked", str(linked))  # in the full zone, but of a walled repo
    pol = paths.config_dir(os.environ) / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text(f'[classes.open]\naccess = "full"\nrepos = ["{full}/**"]\n[classes.w]\naccess = "walled"\nos_user = "svc"\nrepos = ["{walled}/**"]\n')  # fmt: skip
    report = worktrees.run_prune(os.environ, [linked], worktrees.Settings(), time.time(), False, caller=str(full))
    assert report.items == [] and report.refused and "w zone" in report.refused[0][1]
    assert inside.exists() and "idle" in registered(walled)


def ROOT_NS(path):
    return {"/proc/self/ns/pid": "pid:[4026531836]", "/proc/self/ns/user": "user:[4026531837]"}.get(path)


def test_a_caller_in_its_own_pid_or_user_namespace_is_blind():
    me = os.getpid()
    tree = {me: 1}  # inside a sandbox the chain stops at its own pid 1
    pid_ns = {"/proc/self/ns/pid": "pid:[4026532001]", "/proc/self/ns/user": "user:[4026531837]"}.get
    user_ns = {"/proc/self/ns/pid": "pid:[4026531836]", "/proc/self/ns/user": "user:[4026532002]"}.get
    unreadable = {}.get
    if Path("/proc/self").exists():
        assert not worktrees.sees_own_parents(tree, me, readlink=pid_ns)
        assert not worktrees.sees_own_parents(tree, me, readlink=user_ns)
        assert not worktrees.sees_own_parents(tree, me, readlink=unreadable)
    assert worktrees.sees_own_parents(tree, me, readlink=ROOT_NS)


@pytest.mark.skipif(not Path("/proc/self/ns/pid").exists(), reason="Linux namespaces only")
def test_the_real_namespace_links_are_read():
    import os as _os

    real = (
        _os.readlink("/proc/self/ns/pid") == "pid:[4026531836]"
        and _os.readlink("/proc/self/ns/user") == "user:[4026531837]"
    )
    assert worktrees.in_root_namespaces() is real


@needs_git
def test_a_process_list_that_hides_the_callers_parents_keeps_every_worktree(repo, monkeypatch):
    wt = add(repo, "idle")
    me = os.getpid()
    assert worktrees.sees_own_parents({me: 4242, 4242: 1}, me, readlink=ROOT_NS)
    assert worktrees.sees_own_parents({me: 1}, me, readlink=ROOT_NS)
    assert not worktrees.sees_own_parents({me: 4242}, me, readlink=ROOT_NS)  # the parent is not listed
    assert not worktrees.sees_own_parents({}, me, readlink=ROOT_NS)
    with monkeypatch.context() as m:
        m.setattr(worktrees, "_ppids", lambda: {me: 4242})  # a PID namespace or `hidepid`: part of the tree only
        assert reasons(prune(repo)) == {"idle": "busy-unknown"} and wt.exists()
        assert reasons(prune(repo, dry_run=True)) == {"idle": "busy-unknown"}
    assert reasons(prune(repo)) == {"idle": "removed"}


@needs_git
def test_a_caller_in_a_sandbox_keeps_every_worktree(repo, monkeypatch):
    wt = add(repo, "idle")
    sandbox = {"/proc/self/ns/pid": "pid:[4026532999]", "/proc/self/ns/user": "user:[4026531837]"}.get
    # Namespaces are a Linux idea: with no /proc (macOS) the links are never read and `ps` lists every process, so
    # prune goes ahead there (in_root_namespaces' docstring); the sandbox case is Linux's.
    sandboxed = "busy-unknown" if os.path.isdir("/proc/self") else "removed"
    with monkeypatch.context() as m:
        m.setattr(worktrees, "_readlink", sandbox)  # the module's reader, as prune uses it
        assert reasons(prune(repo)) == {"idle": sandboxed} and wt.exists() == (sandboxed == "busy-unknown")
    if sandboxed == "busy-unknown":
        assert reasons(prune(repo)) == {"idle": "removed"}


@needs_git
def test_a_worktree_git_refuses_keeps_its_modes(repo):
    wt = add(repo, "untracked")
    cache = wt / "cache"
    cache.mkdir()
    for f in (wt / "notes.txt", cache / "x"):  # one untracked and not ignored, so git refuses; one ignored
        f.write_text("x")
        os.chmod(f, 0o444)
    os.chmod(cache, 0o555)
    age(wt)
    try:
        assert {Path(i.path).name: i.action for i in prune(repo).items} == {"untracked": "failed"}
        assert [stat_mode(p) for p in (wt / "notes.txt", cache / "x", cache)] == [0o444, 0o444, 0o555]
    finally:
        os.chmod(cache, 0o755)


@needs_git
def test_files_with_two_names_and_links_pointing_out_are_not_changed(repo, box):
    shared, target = box / "shared", box / "target"
    for f in (shared, target):
        f.write_text("x")
        os.chmod(f, 0o444)
    wt = add(repo, "cache")
    (wt / "cache").mkdir()
    os.link(shared, wt / "cache" / "shared")  # uv's cache hard-links files into every virtualenv
    os.symlink(target, wt / "cache" / "link")
    age(wt)
    assert reasons(prune(repo)) == {"cache": "removed"} and not wt.exists()
    assert (stat_mode(shared), stat_mode(target)) == (0o444, 0o444) and shared.exists() and target.exists()


def test_make_writable_leaves_a_link_for_its_root_alone(box):
    real, link = box / "real", box / "link"
    real.mkdir()
    os.chmod(real, 0o555)
    os.symlink(real, link)
    try:
        worktrees.make_writable(link)
        assert stat_mode(real) == 0o555
        worktrees.make_writable(real)
        assert stat_mode(real) == 0o755
    finally:
        os.chmod(real, 0o755)


# --- the test-temp clean-up -------------------------------------------------------------------------------------------

LINUX = osinfo.OSInfo("linux", "debian 13", "x86_64", "systemd", True)
MAC = osinfo.OSInfo("macos", "26.0", "arm64", "launchd", True)


def test_pytest_keeps_only_the_last_runs_failed_folders():
    cfg = tomllib.loads((Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8"))
    ini = cfg["tool"]["pytest"]["ini_options"]
    assert ini["tmp_path_retention_policy"] == "failed"
    assert ini["tmp_path_retention_count"] == "1"  # text: the pinned pytest reads a TOML integer here as an error


def test_the_tmpfiles_rule_is_installed_on_systemd_and_not_elsewhere(box):
    env = dict(os.environ)
    step = tmpclean.step(env, LINUX, worktrees.Settings(temp_age_hours=3))
    assert step.path == box / "home" / ".config" / "user-tmpfiles.d" / "ws-pytest.conf"
    assert step.content == "e /tmp/pytest-of-%u - - - 3h\n"
    assert tmpclean.step(env, MAC, worktrees.Settings()) is None
    assert any(s.path == step.path for s in install.plan(install.Answers(), env, osi=LINUX))


def test_the_clean_up_timer_is_enabled_only_on_the_accounts_own_home(box, monkeypatch):
    calls: list[list[str]] = []
    env = dict(os.environ)
    assert tmpclean.enable_timer(env, LINUX, lambda argv: (calls.append(argv), (0, ""))[1]) == []
    assert calls == []  # HOME here is a stand-in: the user's real manager is not touched
    monkeypatch.setattr(tmpclean, "_real_home", lambda e: True)
    assert tmpclean.enable_timer(env, LINUX, lambda argv: (calls.append(argv), (0, ""))[1]) == []
    assert calls == [["systemctl", "--user", "enable", "--now", "systemd-tmpfiles-clean.timer"]]
    assert tmpclean.enable_timer(env, MAC, lambda argv: pytest.fail("macOS has no such timer")) == []
    assert "failed" in tmpclean.enable_timer(env, LINUX, lambda argv: (1, "no bus"))[0]


def test_the_doctor_line_for_temp(box):
    env = dict(os.environ)
    assert tmpclean.check(env, MAC, worktrees.Settings())[0] == "ok"
    assert "not handled" in tmpclean.check(env, MAC, worktrees.Settings())[1]
    status, detail, fix = tmpclean.check(env, LINUX, worktrees.Settings())
    assert status == "warn" and "ws install" in fix  # no rule yet
    rule = tmpclean.rule_path(env)
    rule.parent.mkdir(parents=True)
    rule.write_text(tmpclean.rule(worktrees.Settings()))
    big = box / "tmp"
    big.mkdir()
    (big / "blob").write_bytes(b"x" * 2_000_000)
    env["TMPDIR"] = str(big)
    assert tmpclean.check(env, LINUX, worktrees.Settings())[0] == "ok"
    status, detail, _ = tmpclean.check(env, LINUX, worktrees.Settings(temp_warn_gb=0.001))
    assert status == "warn" and "your files in /tmp hold" in detail
    ids = [c.id for c in doctor.run_checks(LINUX, box / "p.toml", env=env, network=False)]
    assert "WS021" in ids


def test_the_uninstall_note_names_the_timer_only_when_it_is_enabled_and_the_rule_went(box, monkeypatch):
    env = dict(os.environ)
    rule, calls = tmpclean.rule_path(env), []

    def enabled(code):
        return lambda argv: (calls.append(argv), (code, ""))[1]

    monkeypatch.setattr(tmpclean, "_real_home", lambda e: True)
    assert "left systemd-tmpfiles-clean.timer enabled" in tmpclean.uninstall_notes(env, [rule], LINUX, enabled(0))[0]
    assert calls == [["systemctl", "--user", "is-enabled", "systemd-tmpfiles-clean.timer"]]  # asked, never changed
    assert tmpclean.uninstall_notes(env, [rule], LINUX, enabled(1)) == []  # not enabled: nothing to say
    assert tmpclean.uninstall_notes(env, [box / "other"], LINUX, enabled(0)) == []  # the rule was not removed
    assert tmpclean.uninstall_notes(env, [rule], MAC, enabled(0)) == []
    monkeypatch.setattr(tmpclean, "_real_home", lambda e: False)
    assert tmpclean.uninstall_notes(env, [rule], LINUX, enabled(0)) == []  # a stand-in home: not the user's manager
