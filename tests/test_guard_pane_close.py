"""The pane-close guard against real tmux servers (private sockets in a scratch folder) and the real session registry
stand-ins: who may close which window or pane, and every form that is refused."""

from __future__ import annotations

import dataclasses
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from test_msg import World, world  # noqa: F401 - the fixture, imported so this file uses the same registry stand-ins

from ws import gate, guards, msg, policy, session
from ws.adapters import Call
from ws.guards import pane_close, presence
from ws.policy import PolicyError

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")


class Tmux:
    """Private tmux servers: `-L NAME` sockets under a scratch TMUX_TMPDIR, none of them anyone's real one."""

    def __init__(self):
        self.dir = tempfile.mkdtemp(prefix="wst", dir="/tmp")
        self.names: list[str] = []

    @property
    def env(self) -> dict:
        return {**os.environ, "TMUX_TMPDIR": self.dir, "TMUX": "", "TMUX_PANE": ""}

    def run(self, name: str, *args: str) -> str:
        done = subprocess.run(["tmux", "-L", name, *args], capture_output=True, text=True, env=self.env, check=False)
        assert done.returncode == 0, done.stderr
        return done.stdout.strip()

    def server(self, name: str, command: str = "sleep 600") -> tuple[str, str, int]:
        """Start a server whose first window runs `command`; returns (pane id, window id, pane pid)."""
        self.names.append(name)
        self.run(name, "-f", "/dev/null", "new-session", "-d", "-s", "main", "-x", "100", "-y", "30", command)
        return self.pane(name, "main:0")

    def window(self, name: str, command: str = "sleep 600") -> tuple[str, str, int]:
        out = self.run(name, "new-window", "-d", "-P", "-F", "#{pane_id} #{window_id} #{pane_pid}", command)
        pane, window, pid = out.split()
        return pane, window, int(pid)

    def pane(self, name: str, target: str) -> tuple[str, str, int]:
        pane, window, pid = self.run(
            name, "display-message", "-p", "-t", target, "#{pane_id} #{window_id} #{pane_pid}"
        ).split()
        return pane, window, int(pid)

    def socket(self, name: str) -> Path:
        return Path(self.dir) / f"tmux-{os.getuid()}" / name

    def stop(self) -> None:
        for name in self.names:
            subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True, env=self.env, check=False)
        shutil.rmtree(self.dir, ignore_errors=True)


@pytest.fixture
def tmux(monkeypatch):
    t = Tmux()
    monkeypatch.setenv("TMUX_TMPDIR", t.dir)
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    yield t
    t.stop()


CFG = pane_close.Config(live_socket="live")


def child_of(pid: int) -> int:
    end = time.monotonic() + 5
    while time.monotonic() < end:
        out = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False).stdout.split()
        if out:
            return int(out[0])
        time.sleep(0.05)
    raise AssertionError("no child process")


def runner_at(pid: int) -> session.Proc:
    proc = session.proc_info(pid)
    assert proc is not None
    return dataclasses.replace(proc, name="claude")  # tmux starts a pane through a shell, which may not have exec'd yet


def ask(command: str, sid: str, cfg=CFG, clock=time.time, cwd: str = "/w"):
    ctx = guards.Context("claude-code", "pre", sid, cwd, {}, clock)
    return pane_close.judge(Call("command", command), cfg, ctx)


def kill(flag_target: str, sub: str = "kill-pane", sock: str = "-L live") -> str:
    return f"tmux {sock} {sub} -t {flag_target}"


def idle(sid: str) -> None:
    session.update(sid, state="idle")


@pytest.fixture
def scene(world, tmux):  # noqa: F811
    """A live server with a worker's pane, an idle session's pane and a busy one's; a lead and a worker as callers."""
    pane0, _, pid0 = tmux.server("live")
    world.session("lead1", world.folder("lead"), role="lead")
    world.session("work1", world.folder("work"))
    sleepy = tmux.window("live")
    busy = tmux.window("live")
    world.session("idler", world.folder("idler"), proc=runner_at(sleepy[2]))
    world.session("busyer", world.folder("busyer"), proc=runner_at(busy[2]))
    idle("idler")
    session.update("busyer", state="busy")
    world.tmux = tmux
    world.panes = {"idle": sleepy, "busy": busy, "first": (pane0, "@0", pid0)}
    return world


def refused(found, *words) -> None:
    assert found is not None and found.action == "deny" and not found.approvable
    assert all(w in found.reason for w in words), found.reason


def test_idle_session_pane_closed_by_percent_id(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    assert ask(kill(pane), "lead1") is None


def test_idle_session_window_closed_by_at_id(scene):
    _, window, _ = scene.panes["idle"]
    scene.who = "lead1"
    assert ask(kill(window, "kill-window"), "lead1") is None
    assert ask(kill(window, "kill-pane"), "lead1") is None  # a window id for kill-pane means every pane in it
    pane, _, _ = scene.panes["idle"]
    assert ask(kill(pane, "kill-window"), "lead1") is None  # and a pane id for kill-window, its whole window


def test_busy_session_is_not_closed(scene):
    pane, window, _ = scene.panes["busy"]
    scene.who = "lead1"
    refused(ask(kill(pane), "lead1"), "not idle")
    refused(ask(kill(window, "kill-window"), "lead1"), "not idle")


def test_a_busy_report_gone_stale_is_not_idle_either(scene, monkeypatch):
    pane, _, _ = scene.panes["busy"]
    scene.who = "lead1"
    refused(ask(kill(pane), "lead1"), "not idle")
    # even a session that says nothing is not "idle": no report at all, or one older than the window
    monkeypatch.setattr(session, "_now", lambda: time.time() - presence.STALE_S - 5)
    session.update("busyer", state="busy")
    refused(ask(kill(pane), "lead1", clock=lambda: time.time()), "not idle")
    monkeypatch.setattr(session, "_now", time.time)
    session.update("busyer", state=None)
    refused(ask(kill(pane), "lead1"), "not idle")


@pytest.mark.parametrize(
    "target", ["0", "main:0", "main:0.0", "live:1", "editor", "{last}", "$1", "%", "@", "%x", "%1; kill-server"]
)
def test_a_target_by_index_or_name_is_refused(scene, target):
    scene.who = "lead1"
    refused(ask(f"tmux -L live kill-window -t '{target}'", "lead1"), "stable id")
    refused(ask("tmux -L live kill-pane", "lead1"), "current pane")  # no target at all: the pane it runs in


def test_all_other_panes_flag_is_refused(scene):
    pane, window, _ = scene.panes["idle"]
    scene.who = "lead1"
    refused(ask(f"tmux -L live kill-pane -a -t {pane}", "lead1"), "-a")
    refused(ask(f"tmux -L live kill-window -at {window}", "lead1"), "-a")
    refused(ask(f"tmux -L live kill-window -a -t {window}", "lead1"), "-a")


def test_a_close_inside_a_loop_is_refused(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    for command in (
        f"for i in 1 2; do tmux -L live kill-pane -t {pane}; done",
        f"while true; do tmux -L live kill-pane -t {pane}; done",
        f"tmux -L live kill-pane -t {pane} && echo closed",
        f"tmux -L live kill-pane -t {pane}; tmux -L live kill-pane -t %0",
        f"echo {pane} | xargs -I{{}} tmux -L live kill-pane -t {{}}",
        f"tmux -L live kill-pane -t {pane} | cat",
    ):
        refused(ask(command, "lead1"), "loop")


def test_a_close_inside_a_command_substitution_is_refused(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    for command in (
        f"echo $(tmux -L live kill-pane -t {pane})",
        f"echo `tmux -L live kill-pane -t {pane}`",
        f'echo "$(tmux -L live kill-pane -t {pane})"',
        f"cat <(tmux -L live kill-pane -t {pane})",
    ):
        refused(ask(command, "lead1"), "substitution")


def test_wrappers_and_chained_tmux_commands_are_refused(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    for command in (
        f"sudo tmux -L live kill-pane -t {pane}",
        f"env X=1 tmux -L live kill-pane -t {pane}",
        f"TMUX_TMPDIR=/elsewhere tmux -L live kill-pane -t {pane}",
        f"tmux -L live kill-pane -t {pane} \\; kill-window -t %0",
        f"tmux -L live run-shell 'tmux kill-pane -t {pane}'",
        f"tmux -L live if-shell true 'kill-pane -t {pane}'",
    ):
        refused(ask(command, "lead1"), "wrapper")


def test_an_unreadable_registry_refuses(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    assert ask(kill(pane), "lead1") is None
    (scene.root / "wshome" / "sessions" / "idler.json").write_text("{not json")
    refused(ask(kill(pane), "lead1"), "can't be read")
    shutil.rmtree(scene.root / "wshome" / "identity")  # and an identity folder that is gone: nobody is the lead
    refused(ask(kill(pane), "lead1"), "calling")


def test_a_registry_that_changes_between_the_check_and_the_kill_refuses(scene, monkeypatch):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    real = pane_close._query

    def busy_meanwhile(sock, env):
        rows = real(sock, env)
        session.update("idler", state="busy")  # the session started working while tmux was being asked
        return rows

    monkeypatch.setattr(pane_close, "_query", busy_meanwhile)
    refused(ask(kill(pane), "lead1"), "changed while it was checked")


def test_a_forged_own_id_is_nobody(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "work1"  # the shell is work1's, the payload says it is the lead
    refused(ask(kill(pane), "lead1"), "calling")
    refused(ask(kill(pane), ""), "calling")
    refused(ask(kill(pane), "no-such-session"), "calling")
    scene.who = None  # not under any runner
    refused(ask(kill(pane), "lead1"), "calling")


def test_a_session_closes_its_own_pane_only_after_its_hand_over_is_delivered(world, tmux):  # noqa: F811
    _, _, _ = tmux.server("live")
    mine = tmux.window("live")
    world.session("lead1", world.folder("lead"), role="lead")
    world.session("work1", world.folder("work"), proc=runner_at(mine[2]))
    world.who = "work1"
    pane, window, _ = mine
    refused(ask(kill(pane), "work1"), "hand-over")
    sent = world.send("work1", "lead1", "done", None)
    refused(ask(kill(pane), "work1"), "hand-over")  # sent is not delivered
    world.who = "lead1"
    msg.read(sent["id"], "lead1")  # the lead had it delivered
    world.who = "work1"
    assert ask(kill(pane), "work1") is None
    assert ask(kill(window, "kill-window"), "work1") is None
    # a hand-over of someone else's is not mine
    world.session("work2", world.folder("work2"), proc=runner_at(tmux.window("live")[2]))
    world.who = "work2"
    refused(ask(kill(pane), "work2"), "only the lead")


def test_a_pane_shared_with_another_session_is_not_closed_as_its_own(world, tmux):  # noqa: F811
    tmux.server("live")
    pane, _, pid = tmux.window("live", "bash -c 'sleep 600 & sleep 600 & wait'")
    kids = subprocess.run(["pgrep", "-P", str(pid)], capture_output=True, text=True, check=False).stdout.split()
    assert len(kids) == 2
    world.session("lead1", world.folder("lead"), role="lead")
    world.session("one", world.folder("one"), proc=runner_at(int(kids[0])))
    world.session("two", world.folder("two"), proc=runner_at(int(kids[1])))
    sent = world.send("one", "lead1", "done", None)
    world.who = "lead1"
    msg.read(sent["id"], "lead1")
    world.who = "one"
    refused(ask(kill(pane), "one"), "another session")


def test_lead_closes_another_pane_and_a_worker_cannot(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    assert ask(kill(pane), "lead1") is None
    scene.who = "work1"
    refused(ask(kill(pane), "work1"), "only the lead")


def test_sessions_and_the_server_are_never_closed_from_here(scene):
    scene.who = "lead1"
    for command in (
        "tmux -L live kill-session -t '$1'",
        "tmux -L live kill-session -t main",
        "tmux -L live kill-server",
        "tmux -L live kill-s -t main",
        "tmux -L live kill -t %1",
    ):
        refused(ask(command, "lead1"), "kill-window and kill-pane by id")
    scene.who = "work1"
    refused(ask("tmux -L live kill-server", "work1"), "kill-window and kill-pane by id")


def test_a_window_name_or_a_tmux_option_or_the_environment_grants_nothing(scene, monkeypatch):
    pane, window, _ = scene.panes["idle"]
    tmux = scene.tmux
    # a worker renames its own window "lead" and sets a user option saying so: still a worker
    tmux.run("live", "rename-window", "-t", window, "lead")
    tmux.run("live", "set-option", "-w", "-t", window, "@role", "lead")
    monkeypatch.setenv("TMUX_PANE", scene.panes["first"][0])
    monkeypatch.setenv("WS_ROLE", "lead")
    scene.who = "work1"
    refused(ask(kill(pane), "work1"), "only the lead")
    # and the lead is the lead whatever any window is called
    tmux.run("live", "rename-window", "-t", window, "worker")
    scene.who = "lead1"
    assert ask(kill(pane), "lead1") is None


def test_a_role_planted_in_the_sessions_file_grants_nothing(scene):
    pane, _, _ = scene.panes["idle"]
    path = scene.root / "wshome" / "sessions" / "work1.json"
    planted = path.read_text().replace('"role": "worker"', '"role": "lead"')
    assert planted != path.read_text()
    path.write_text(planted)
    scene.who = "work1"
    refused(ask(kill(pane), "work1"), "only the lead")


def test_a_session_found_by_walking_proc_from_the_pane_process(world, tmux):  # noqa: F811
    tmux.server("live")
    pane, window, pid = tmux.window("live", "bash -c 'sleep 600 & wait'")
    runner = runner_at(child_of(pid))
    assert runner.pid != pid
    world.session("lead1", world.folder("lead"), role="lead")
    world.session("deep", world.folder("deep"), proc=runner)  # no TMUX_PANE was set, so the record has no pane
    assert session.read("deep").pane is None
    world.who = "lead1"
    refused(ask(kill(pane), "lead1"), "not idle")  # found below the pane's shell, and not idle yet
    idle("deep")
    assert ask(kill(pane), "lead1") is None
    assert ask(kill(window, "kill-window"), "lead1") is None


def test_a_pane_with_no_registered_session_is_not_closed(scene):
    pane, _, _ = scene.panes["first"]
    scene.who = "lead1"
    refused(ask(kill(pane), "lead1"), "no registered session")
    refused(ask(kill("%99"), "lead1"), "no pane %99")
    refused(ask(kill("@99", "kill-window"), "lead1"), "no window @99")


def test_a_test_socket_is_left_alone_and_the_live_one_is_not(scene, tmux):
    scene.who = "work1"  # not even a lead: a test server's windows are nobody's sessions
    tmux.server("scratch")
    refused(ask("tmux -L live kill-server", "work1"), "kill-window and kill-pane")
    assert ask("tmux -L scratch kill-server", "work1") is None
    assert ask("tmux -L scratch kill-pane -a", "work1") is None
    assert ask(f"tmux -S {tmux.socket('scratch')} kill-window -t main:0", "work1") is None
    refused(
        ask(f"tmux -S {tmux.socket('live')} kill-server", "work1"), "kill-window and kill-pane"
    )  # the live socket by path
    # which socket is live comes from config, not from a list of names
    other = pane_close.Config(live_socket="scratch")
    refused(ask("tmux -L scratch kill-server", "work1", other), "kill-window and kill-pane")
    assert ask("tmux -L live kill-server", "work1", other) is None
    by_path = pane_close.Config(live_socket=str(tmux.socket("scratch")))
    refused(ask("tmux -L scratch kill-server", "work1", by_path), "kill-window and kill-pane")
    # no flag: the socket in $TMUX, else the default; the default is the live one when the config says nothing
    default = pane_close.Config()
    assert pane_close._live_socket(default, {"TMUX_TMPDIR": tmux.dir}) == tmux.socket("default")
    refused(ask("tmux kill-server", "work1", default), "kill-window and kill-pane")  # no flag, no $TMUX: that default


def test_a_command_aimed_at_another_server_is_judged_against_that_servers_panes(scene, tmux, monkeypatch):
    """Pane ids repeat across servers. The query goes to the command's own socket with TMUX unset, so a session sitting
    in another server, whose $TMUX points there, is not asked about its own server's panes."""
    tmux.server("elsewhere")  # %0 exists on both servers; the live one has panes past what "elsewhere" has
    only_live = tmux.window("live")
    w = scene
    w.session("late", w.folder("late"), proc=runner_at(only_live[2]))
    idle("late")
    assert only_live[0] not in tmux.run("elsewhere", "list-panes", "-a", "-F", "#{pane_id}").split()
    w.who = "lead1"
    # $TMUX says we are inside "elsewhere"; the command says the live server: it is judged there (and is allowed)
    monkeypatch.setenv("TMUX", f"{tmux.socket('elsewhere')},123,0")
    assert ask(kill(only_live[0]), "lead1") is None
    # the same words with no -L follow $TMUX to the other server, which is not the live one: left alone
    assert ask(f"tmux kill-pane -t {only_live[0]}", "lead1") is None
    # and when $TMUX names the live server, no flag judges the live one, and the busy session is protected there
    monkeypatch.setenv("TMUX", f"{tmux.socket('live')},123,0")
    refused(ask(f"tmux kill-pane -t {w.panes['busy'][0]}", "lead1"), "not idle")
    # the query itself never carries TMUX
    seen = {}
    real = subprocess.run

    def spy(args, **kw):
        seen.update(kw.get("env") or {})
        return real(args, **kw)

    monkeypatch.setattr(pane_close.subprocess, "run", spy)
    ask(kill(w.panes["idle"][0]), "lead1")
    assert "TMUX" not in seen and "TMUX_PANE" not in seen and seen["TMUX_TMPDIR"] == tmux.dir


def test_inert_without_tmux(scene, monkeypatch):
    monkeypatch.setattr(pane_close.shutil, "which", lambda name: None)
    scene.who = "work1"
    assert ask("tmux -L live kill-server", "work1") is None
    assert ask("tmux -L live kill-pane -a -t %0", "work1") is None


def test_unrelated_commands_cost_nothing(scene, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("asked tmux")

    monkeypatch.setattr(pane_close, "_query", boom)
    for command in ("ls -la", "kill -9 12345", "pkill node", "echo tmux", "git commit -m 'kill the build'"):
        assert ask(command, "lead1") is None


def test_the_config_names_one_key_and_loads_through_the_policy(scene, tmux):
    assert pane_close.config({}, "w") == pane_close.Config()
    assert pane_close.config({"live_socket": "ws"}, "w").live_socket == "ws"
    for bad in ({"live_socket": 3}, {"socket": "x"}):
        with pytest.raises(PolicyError):
            pane_close.config(bad, "[guards.pane-close]")
    pol = policy.loads('[guards]\non = ["pane-close"]\n[guards.pane-close]\nlive_socket = "live"\n')
    pane, _, _ = scene.panes["busy"]
    raw = f'{{"session_id": "lead1", "cwd": "/w", "tool_name": "Bash", "tool_input": {{"command": "{kill(pane)}"}}}}'
    scene.who = "lead1"
    reason = gate.check("claude-code", raw, pol)
    assert reason and reason.startswith("pane-close: closing a tmux window or pane is refused")
    assert gate.check("claude-code", raw.replace(kill(pane), "ls -la"), pol) is None


# --- forms that hide a close, or the server it is aimed at -------------------------------------------------------------


def test_a_quote_split_close_is_read_as_the_plain_command_it_spells(scene):
    """`ki''ll-pane` is `kill-pane` to the shell, so it is judged by the registry like the plain form: not by whether the
    raw text happens to hold the letters."""
    scene.who = "lead1"
    idle_pane, busy_pane = scene.panes["idle"][0], scene.panes["busy"][0]
    assert ask(f"tmux -L live ki''ll-pane -t {idle_pane}", "lead1") is None
    assert ask(f"tmux -L live k\\ill-pane -t {idle_pane}", "lead1") is None
    refused(ask(f"tmux -L live ki''ll-pane -t {busy_pane}", "lead1"), "not idle")
    refused(ask(f"tmux -L live \"kil\"'l'-pane -t {busy_pane}", "lead1"), "not idle")
    refused(ask("tmux -L live ki''ll-server", "lead1"), "kill-window and kill-pane by id")
    refused(ask("tmux -L live k\\ill-session -t main", "lead1"), "kill-window and kill-pane by id")
    refused(ask("t''mux -L live kill-server", "lead1"), "kill-window and kill-pane by id")


@pytest.mark.parametrize(
    "command",
    [
        "tmux -L live ${K}ill-pane -t BUSY",
        "tmux -L live $Kill-pane -t BUSY",
        "K=kil; tmux -L live ${K}l-pane -t BUSY",
        "K=kil; tmux -L live ${K}l-server",
        "tmux -L live k*-pane -t BUSY",
        "tmux -L live kill-p?ne -t BUSY",
        "T=tmux; $T -L live kill-server",
    ],
)
def test_a_command_word_that_is_not_plain_text_is_refused(scene, command):
    scene.who = "lead1"
    got = ask(command.replace("BUSY", scene.panes["busy"][0]), "lead1")
    assert got is not None and got.action == "deny" and not got.approvable


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'tmux -L live kill-pane -t BUSY'",
        "sh -c 'tmux -L live kill-server'",
        "bash -lc 'tmux -L live kill-server'",
        'zsh -c "tmux -L live kill-server"',
        "eval 'tmux -L live kill-pane -t BUSY'",
        "eval tmux -L live kill-server",
        "echo tmux -L live kill-server | sh",
        "echo 'tmux -L live kill-server' | bash",
        "printf 'tmux -L live kill-server\\n' | sh -s",
        "sh <<< 'tmux -L live kill-server'",
        "sh <<EOF\ntmux -L live kill-server\nEOF",
        "su -c 'tmux -L live kill-server'",
        "env -S 'tmux -L live kill-server'",
        "find . -maxdepth 0 -exec tmux -L live kill-server ';'",
        "bash -c 'bash -c \"tmux -L live kill-server\"'",
        "python3 -c \"import os; os.system('tmux -L live kill-server')\"",
        "tmux -L scratch -c 'tmux -L live kill-server'",
        "tmux -L live kill-pane -t BUSY \\; kill-server",
    ],
)
def test_a_close_inside_shell_text_is_refused(scene, command):
    scene.tmux.server("scratch")
    scene.who = "lead1"
    got = ask(command.replace("BUSY", scene.panes["busy"][0]), "lead1")
    assert got is not None and got.action == "deny" and not got.approvable, command


def test_plain_text_that_only_names_tmux_is_left_alone(scene):
    scene.who = "lead1"
    for command in (
        "echo tmux -L live kill-server",
        "grep tmux kill.log",
        "git commit -m 'tmux kill-server is refused'",
        "cat tmux.conf",
        "bash -c 'echo hi'",
        "echo hi | sh",
        "tmux -L live list-panes -a",
        "tmux -L live display-message -p '#{pane_id}'",
        "tmux -L live send-keys -t %1 'ls -la' Enter",
        "tmux -L live new-window -d 'sleep 5'",
    ):
        assert ask(command, "lead1") is None, command


@pytest.mark.parametrize(
    "sub, text",
    [
        ("new-window", "'tmux -L live kill-server'"),
        ("neww", "'tmux -L live kill-server'"),
        ("new-w", "'tmux -L live kill-server'"),
        ("split-window", "'tmux -L live kill-server'"),
        ("splitw -h", "'tmux -L live kill-server'"),
        ("new-session -d", "'tmux -L live kill-server'"),
        ("respawn-pane -k", "'tmux -L live kill-server'"),
        ("display-popup -E", "'tmux -L live kill-server'"),
        ("pipe-pane -o", "'tmux -L live kill-server'"),
        ("run-shell", "'tmux -L live kill-server'"),
        ("run", "'tmux -L live kill-server'"),
        ("run-s", "'tmux -L live kill-server'"),
        ("send-keys -t %1", "'tmux -L live kill-server' Enter"),
        ("send -t %1", "tmux Space -L Space live Space kill-server Enter"),
        ("send-keys -l", "'tmux kill-server'"),
        ("if-shell true", "'kill-server'"),
        ("if-s true", "'kill-server'"),
        ("if-shell true", "'run-shell \"tmux -L live kill-server\"'"),
        ("set-hook -g after-new-window", "'kill-server'"),
        ("set-hook -g after-new-window", "'run-shell \"tmux -L live kill-server\"'"),
        ("bind-key k", "kill-server"),
        ("bind k", "kill-pane"),
        ("bind-key k", "'kill-pane -t %0'"),
        ("confirm-before -p sure", "kill-server"),
        ("command-prompt -p x", "kill-server"),
        ("display-message -p", "'#(tmux -L live kill-server)'"),
        ("set-option -g status-left", "'#(tmux -L live kill-server)'"),
    ],
)
def test_a_tmux_command_that_runs_text_is_read_for_a_close(scene, sub, text):
    scene.who = "lead1"
    got = ask(f"tmux -L live {sub} {text}", "lead1")
    assert got is not None and got.action == "deny" and not got.approvable, f"{sub} {text}"


def test_a_tmux_command_that_runs_text_is_none_of_ours_on_a_test_server(scene):
    scene.tmux.server("scratch")
    scene.who = "lead1"
    for command in (
        "tmux -L scratch new-window 'tmux kill-server'",
        "tmux -L scratch run-shell 'tmux kill-server'",
        "tmux -L scratch bind-key k kill-server",
        "tmux -L scratch send-keys 'tmux kill-server' Enter",
    ):
        assert ask(command, "lead1") is None, command
    # but the text runs in a pane that can name the live server itself
    refused(ask("tmux -L scratch new-window 'tmux -L live kill-server'", "lead1"), "wrapper")


def test_source_file_is_read_as_the_file_it_names(scene, tmp_path):
    scene.who = "lead1"
    bad = tmp_path / "bad.conf"
    bad.write_text("# a comment\nset -g status on\nbind-key x kill-server\n")
    also = tmp_path / "also.conf"
    also.write_text("run-shell 'tmux -L live kill-server'\n")
    deep = tmp_path / "deep.conf"
    deep.write_text(f"source-file {bad}\n")
    ok = tmp_path / "ok.conf"
    ok.write_text("# nothing to close\nset -g status on\nbind-key x list-panes\n")
    for path in (bad, also, deep):
        refused(ask(f"tmux -L live source-file {path}", "lead1"), "wrapper")
        refused(ask(f"tmux -L live source-file -q {path}", "lead1"), "wrapper")
        refused(ask(f"tmux -L live source {path}", "lead1"), "wrapper")
    refused(ask(f"tmux -L live source-file {tmp_path}/*.conf", "lead1"), "wrapper")
    refused(
        ask("tmux -L live source-file bad.conf", "lead1", cwd=str(tmp_path)), "wrapper"
    )  # from the session's folder
    assert ask(f"tmux -L live source-file {ok}", "lead1") is None
    assert ask(f"tmux -L live source-file {tmp_path}/missing.conf", "lead1") is None  # nothing to run
    refused(ask("tmux -L live source-file -", "lead1"), "standard input")
    refused(ask(f"tmux -L live source-file {tmp_path}", "lead1"), "plain file")  # a folder is not read
    big = tmp_path / "big.conf"
    big.write_text("# x\n" * 100000)
    refused(ask(f"tmux -L live source-file {big}", "lead1"), "too big")


def test_an_earlier_command_makes_the_socket_unsure(scene, tmp_path, monkeypatch):
    """`cd` changes what a relative -S means, `unset TMUX` and `export TMUX_TMPDIR=...` what an unflagged call or -L
    means; the hook sees none of it. A close after another command is taken to be aimed at the live server."""
    scene.tmux.server("scratch")
    scene.who = "work1"
    folder = scene.tmux.socket("live").parent
    default = pane_close.Config()
    monkeypatch.setenv("TMUX", f"{scene.tmux.socket('scratch')},1,0")  # this session is inside the scratch server
    for command in (
        f"cd {folder} && tmux -S live kill-server",
        f"cd {folder}; tmux -S default kill-server",
        "unset TMUX; tmux kill-server",
        "unset TMUX && tmux kill-server",
        f"export TMUX_TMPDIR={scene.tmux.dir}; tmux -L live kill-server",
        f"TMUX_TMPDIR={scene.tmux.dir} tmux -L live kill-server",
        "TMUX= tmux kill-server",
        "env -u TMUX tmux kill-server",
        "true; tmux -L scratch kill-server",  # even a name that is a test server's, once the line holds an earlier command
        "tmux -L scratch kill-server; true",
    ):
        got = ask(command, "work1", default)
        assert got is not None and got.action == "deny" and not got.approvable, command
    # one plain command is still read as it is written
    assert ask("tmux -L scratch kill-server", "work1", default) is None
    assert ask("tmux kill-server", "work1", default) is None  # $TMUX says scratch
    monkeypatch.delenv("TMUX")
    refused(ask("tmux kill-server", "work1", default), "kill-window and kill-pane")


def test_a_relative_socket_path_is_read_from_the_sessions_folder(scene, tmp_path):
    scene.tmux.server("scratch")
    scene.who = "work1"
    folder = str(scene.tmux.socket("live").parent)
    refused(ask("tmux -S live kill-server", "work1", cwd=folder), "kill-window and kill-pane")
    refused(ask("tmux -S ./live kill-server", "work1", cwd=folder), "kill-window and kill-pane")
    assert ask("tmux -S scratch kill-server", "work1", cwd=folder) is None  # a test server's socket, from its folder
    assert ask("tmux -S live kill-server", "work1", cwd=str(tmp_path)) is None  # no socket there: not the live one


def test_text_nested_too_deeply_or_a_shell_that_cant_be_read_is_refused(scene):
    scene.who = "lead1"
    deep = "tmux -L live kill-server"
    for _ in range(8):
        deep = "bash -c " + __import__("shlex").quote(deep)
    got = ask(deep, "lead1")
    assert got is not None and got.action == "deny"


def test_the_forms_that_name_the_default_socket(scene):
    """The same close, aimed at the default server (the live one when the config says nothing), by each hiding form."""
    scene.who = "work1"
    default = pane_close.Config()
    tmux = scene.tmux
    folder = tmux.socket("default").parent
    tmux.server("default")
    for command in (
        "tmux new-window 'tmux -L default kill-server'",
        "tmux split-window 'tmux kill-server'",
        f"cd {folder} && tmux -S default kill-server",
        f"cd {folder} && tmux -L default kill-server",
        "unset TMUX; tmux kill-server",
        "tmux ki''ll-server",
        "tmux ${K}ill-server",
        "bash -c 'tmux kill-server'",
        "eval 'tmux kill-server'",
        "echo tmux kill-server | sh",
    ):
        got = ask(command, "work1", default)
        assert got is not None and got.action == "deny" and not got.approvable, command
    assert ask("tmux list-panes -a", "work1", default) is None


def test_a_double_dash_and_a_semicolon_at_the_end_of_a_word_are_read_as_tmux_reads_them(scene):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    refused(ask("tmux -L live -- kill-server", "lead1"), "not a session or the server")
    refused(ask("tmux -L live 'list-sessions;' kill-server", "lead1"), "wrapper")
    refused(ask(f"tmux -L live display-message -p 'x;' kill-pane -t {pane}", "lead1"), "wrapper")
    assert ask(f"tmux -L live -- kill-pane -t {pane}", "lead1") is None  # the same close as without the `--`
    assert ask("tmux -L live 'list-sessions;' display-message -p x", "lead1") is None  # nothing closes


@pytest.mark.parametrize("program", ["git -c 'alias.k=!tmux -L live {}' k", "man -P 'tmux -L live {}' ls"])
def test_a_tmux_command_held_in_a_git_or_man_option_is_read(scene, program):
    pane, _, _ = scene.panes["idle"]
    scene.who = "lead1"
    refused(ask(program.format(f"kill-pane -t {pane}"), "lead1"), "wrapper")
