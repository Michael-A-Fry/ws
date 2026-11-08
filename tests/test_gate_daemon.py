"""The resident gate and its thin client, end to end: payload -> client -> daemon -> guards -> reply, for Claude Code and
OpenCode, with the daemon killed mid-session, a policy that changes, ws upgraded under it, and a socket that is not the
daemon's. Every one of these starts real processes in a folder of its own and stops them."""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import ws
from ws import gateclient, gateserve, jsonl, paths, policy
from ws.adapters import claude_code, client_argv, opencode

PY = sys.executable
SRC = Path(ws.__file__).resolve().parent
BOOT = "import sys; sys.path.insert(0, sys.argv[1]); from ws.cli import main; raise SystemExit(main(sys.argv[2:]))"
BENIGN = "ls -la"
SECRET_READ = "cat ~/.ssh/id_rsa"


def wait_for(predicate, timeout=20.0, step=0.05):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    return predicate()


class Box:
    """A scratch HOME and WS_HOME, the hook commands generated for them, and the processes started against them."""

    def __init__(self, tmp_path: Path, monkeypatch):
        self.root = tmp_path
        self.home = tmp_path / "home"
        self.home.mkdir()
        self.ws = tmp_path / "w"
        monkeypatch.setenv("HOME", str(self.home))
        monkeypatch.setenv("WS_HOME", str(self.ws))
        self.env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "HOME": str(self.home),
            "WS_HOME": str(self.ws),
            "LANG": "C.UTF-8",
        }
        self.policy_file = self.ws / "policy.toml"
        self.ws.mkdir()
        self.set_policy(policy.default_toml())
        self.pin = paths.pin_of(self.env)
        self.out = tmp_path / "gate-out"
        self.procs: list[subprocess.Popen] = []

    def set_policy(self, text: str) -> None:
        self.policy_file.write_text(text)

    @property
    def state(self) -> Path:
        return Path(self.pin.state)

    @property
    def run_dir(self) -> Path:
        return self.state / gateclient.RUN

    @property
    def log(self) -> list[dict]:
        path = self.state / "logs" / "gate.jsonl"
        return jsonl.read(path) if path.exists() else []

    def claude_hook(self, ws_cmd=None) -> str:
        files = claude_code.generate(
            policy.load(self.policy_file), ws_cmd or [PY, "-I", "-m", "ws"], self.out, self.policy_file, pin=self.pin
        )
        conf = json.loads(files["settings.json"])
        return conf["hooks"]["PreToolUse"][0]["hooks"][0]["command"]

    def opencode_check(self) -> list[str]:
        files = opencode.generate(
            policy.load(self.policy_file), [PY, "-I", "-m", "ws"], self.out, self.policy_file, pin=self.pin
        )
        return json.loads(files[opencode.PLUGIN].split("const CHECK = ")[1].split("\n")[0])

    def run(self, command: str | list[str], payload, extra_env=None, cwd=None) -> subprocess.CompletedProcess:
        env = {**self.env, **(extra_env or {})}
        argv = ["sh", "-c", command] if isinstance(command, str) else command
        text = payload if isinstance(payload, str) else json.dumps(payload)
        return subprocess.run(
            argv, input=text, text=True, env=env, capture_output=True, cwd=cwd or self.root, timeout=60, check=False
        )

    def daemon_args(self, *extra: str) -> list[str]:
        return [PY, "-I", "-m", "ws", "gate", "serve", *self.pin.args(), *extra]

    def start_daemon(self, *extra: str, argv: list[str] | None = None) -> subprocess.Popen:
        proc = subprocess.Popen(
            argv or self.daemon_args(*extra), env=self.env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL
        )
        self.procs.append(proc)
        assert wait_for(self.serving), "the daemon did not come up"
        return proc

    def serving(self) -> bool:
        fd = gateclient.open_run_dir(str(self.state))
        if fd is None:
            return False
        try:
            conn = gateclient.connect(fd)
        finally:
            os.close(fd)
        if conn is None:
            return False
        conn.close()
        return True

    def pid(self) -> int | None:
        try:
            return int((self.run_dir / gateclient.LOCK).read_text().strip())
        except (OSError, ValueError):
            return None

    def alive(self, pid: int | None) -> bool:
        if pid is None:
            return False
        try:
            os.kill(pid, 0)
        except OSError:
            return False
        try:
            return Path(f"/proc/{pid}/stat").read_text().split()[2] != "Z"  # a zombie is gone
        except OSError:
            return True

    def stop(self) -> None:
        pid = self.pid()
        if self.alive(pid):
            os.kill(pid, signal.SIGKILL)
        for proc in self.procs:
            if proc.poll() is None:
                proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def box(tmp_path, monkeypatch):
    b = Box(tmp_path, monkeypatch)
    yield b
    b.stop()


def claude_payload(tool: str, **tool_input) -> dict:
    return {
        "tool_name": tool,
        "tool_input": tool_input,
        "session_id": "s1",
        "cwd": "/",
        "hook_event_name": "PreToolUse",
    }


def opencode_payload(tool: str, **args) -> dict:
    return {"tool": tool, "args": args, "session_id": "s1", "cwd": "/"}


CLAUDE_CASES = [
    (claude_payload("Bash", command=BENIGN), 0),
    (claude_payload("Bash", command=SECRET_READ), 2),
    (claude_payload("Bash", command="git push --force origin main"), 2),
    (claude_payload("Read", file_path="~/.ssh/id_rsa"), 2),
    (claude_payload("Read", file_path="/etc/hostname"), 0),
    (claude_payload("Grep", pattern="x", path="~/.ssh"), 2),
]
OPENCODE_CASES = [
    (opencode_payload("bash", command=BENIGN), 0),
    (opencode_payload("bash", command=SECRET_READ), 2),
    (opencode_payload("read", filePath="~/.ssh/id_rsa"), 2),
    (opencode_payload("read", filePath="/etc/hostname"), 0),
    (opencode_payload("grep", path="~/.ssh", pattern="x"), 2),
]


# --- the real decision path --------------------------------------------------------------------------------------


def test_the_first_call_is_decided_in_its_own_process_and_starts_the_daemon_for_the_next(box):
    hook = box.claude_hook()
    first = box.run(hook, claude_payload("Bash", command=BENIGN))
    assert first.returncode == 0
    assert wait_for(box.serving), "the first hook call did not start the daemon"
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2
    box.run(hook, claude_payload("Bash", command=BENIGN))
    assert [r["via"] for r in box.log][:1] == ["direct"] and "daemon" in [r["via"] for r in box.log]


@pytest.mark.parametrize("payload, code", CLAUDE_CASES, ids=[json.dumps(p["tool_input"]) for p, _ in CLAUDE_CASES])
def test_claude_code_calls_get_the_same_answer_from_the_daemon_and_from_the_client_alone(box, payload, code):
    hook = box.claude_hook()
    box.start_daemon()
    asked = box.run(hook, payload)
    cold = box.run(hook, payload, {"WS_GATE_DAEMON": "0"})
    assert asked.returncode == cold.returncode == code
    assert (asked.stdout, asked.stderr) == (cold.stdout, cold.stderr)
    assert box.log[0]["via"] == "daemon" and box.log[-1]["via"] == "direct"


@pytest.mark.parametrize("payload, code", OPENCODE_CASES, ids=[json.dumps(p["args"]) for p, _ in OPENCODE_CASES])
def test_opencode_calls_get_the_same_answer_from_the_daemon_and_from_the_client_alone(box, payload, code):
    check = box.opencode_check()
    box.start_daemon()
    asked = box.run(check, payload)
    cold = box.run(check, payload, {"WS_GATE_DAEMON": "0"})
    assert asked.returncode == cold.returncode == code
    assert (asked.stdout, asked.stderr) == (cold.stdout, cold.stderr)
    assert box.log[0]["via"] == "daemon" and box.log[-1]["via"] == "direct"


def test_both_runners_get_the_same_decision_for_the_same_call(box):
    box.start_daemon()
    hook, check = box.claude_hook(), box.opencode_check()
    for claude, opencode_ in (
        (claude_payload("Bash", command=SECRET_READ), opencode_payload("bash", command=SECRET_READ)),
        (claude_payload("Read", file_path="~/.ssh/id_rsa"), opencode_payload("read", filePath="~/.ssh/id_rsa")),
        (claude_payload("Bash", command=BENIGN), opencode_payload("bash", command=BENIGN)),
    ):
        assert box.run(hook, claude).returncode == box.run(check, opencode_).returncode


def test_a_denial_says_why_on_stderr_and_nothing_on_stdout(box):
    box.start_daemon()
    done = box.run(box.claude_hook(), claude_payload("Bash", command="git push --force origin main"))
    assert done.returncode == 2 and done.stdout == ""
    assert "ws gate: blocked: command matches deny_commands rule" in done.stderr


def test_the_daemon_killed_mid_session_changes_no_answer(box):
    hook = box.claude_hook()
    daemon = box.start_daemon()
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2
    pid = box.pid()
    assert pid == daemon.pid and box.alive(pid)
    os.kill(pid, signal.SIGKILL)
    daemon.wait(timeout=10)
    assert (box.run_dir / gateclient.SOCKET).exists()  # the dead daemon's socket file is still there
    for payload, code in CLAUDE_CASES:
        assert box.run(hook, payload).returncode == code, payload
    assert wait_for(box.serving), "a new daemon was not started after the old one died"
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2
    assert box.log[-1]["via"] == "daemon"


def test_opencode_too_when_the_daemon_is_killed_mid_session(box):
    check = box.opencode_check()
    daemon = box.start_daemon()
    os.kill(daemon.pid, signal.SIGKILL)
    daemon.wait(timeout=10)
    for payload, code in OPENCODE_CASES:
        assert box.run(check, payload).returncode == code, payload


def test_no_daemon_and_no_run_folder_still_decides(box):
    hook = box.claude_hook()
    shutil.rmtree(box.run_dir, ignore_errors=True)
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ), {"WS_GATE_DAEMON": "0"}).returncode == 2
    assert box.run(hook, claude_payload("Bash", command=BENIGN), {"WS_GATE_DAEMON": "0"}).returncode == 0
    assert not box.run_dir.exists()  # asked not to use a daemon: none was made


def test_a_hook_whose_command_cannot_run_is_a_deny(box):
    hook = box.claude_hook().replace(str(SRC / "gateclient.py"), str(box.root / "no-such-client.py"))
    assert box.run(hook, claude_payload("Bash", command=BENIGN)).returncode == 2


def test_the_environment_the_hook_runs_in_is_the_one_checked(box):
    """The daemon's own environment is not the hook's: a hook whose WS_HOME points elsewhere than the install pinned is
    refused, by the daemon as by the client alone."""
    hook = box.claude_hook()
    box.start_daemon()
    elsewhere = {"WS_HOME": str(box.root / "other")}
    asked = box.run(hook, claude_payload("Bash", command=BENIGN), elsewhere)
    cold = box.run(hook, claude_payload("Bash", command=BENIGN), {**elsewhere, "WS_GATE_DAEMON": "0"})
    assert asked.returncode == cold.returncode == 2 and "changed since install" in asked.stderr
    assert asked.stderr == cold.stderr


def test_the_folder_the_hook_runs_in_is_the_one_the_daemon_works_in(box):
    work = box.root / "work"
    work.mkdir()
    (work / ".claude").mkdir()
    (work / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"enabled": False}}))
    hook = box.claude_hook()
    box.start_daemon()
    payload = {**claude_payload("Bash", command=BENIGN), "cwd": str(work)}
    asked = box.run(hook, payload, {"CLAUDE_PROJECT_DIR": str(work)}, cwd=work)
    cold = box.run(hook, payload, {"CLAUDE_PROJECT_DIR": str(work), "WS_GATE_DAEMON": "0"}, cwd=work)
    assert asked.returncode == cold.returncode == 2 and "sandbox" in asked.stderr


def test_many_calls_at_once_each_get_their_own_answer(box):
    hook = box.claude_hook()
    box.start_daemon()
    cases = CLAUDE_CASES * 4

    def one(case):
        payload, code = case
        return box.run(hook, payload).returncode == code

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(one, cases))
    assert {r["via"] for r in box.log} == {"daemon"}


def test_event_hooks_go_through_the_same_client(box):
    box.set_policy('[guards]\non = ["session-context", "dev-server"]\n')
    files = claude_code.generate(
        policy.load(box.policy_file), [PY, "-I", "-m", "ws"], box.out, box.policy_file, pin=box.pin
    )
    conf = json.loads(files["settings.json"])
    start = conf["hooks"]["SessionStart"][0]["hooks"][-1]["command"]
    assert "gateclient.py event" in start and start.endswith("|| true")
    box.start_daemon()
    done = box.run(start, {"hook_event_name": "SessionStart", "session_id": "s1", "cwd": str(box.root)})
    assert done.returncode == 0
    assert box.log and box.log[-1]["event"] == "SessionStart" and box.log[-1]["via"] == "daemon"


# --- the daemon's life -------------------------------------------------------------------------------------------


def test_one_daemon_serves_a_user_and_a_second_start_leaves_it_be(box):
    first = box.start_daemon()
    socket_file = box.run_dir / gateclient.SOCKET
    inode = socket_file.stat().st_ino
    second = subprocess.run(box.daemon_args(), env=box.env, capture_output=True, timeout=30, check=False)
    assert second.returncode == 0
    assert first.poll() is None and socket_file.stat().st_ino == inode and box.pid() == first.pid


def test_the_daemon_exits_after_its_idle_time_and_takes_its_socket_with_it(box):
    daemon = box.start_daemon("--idle", "1")
    assert daemon.wait(timeout=15) == 0
    assert not (box.run_dir / gateclient.SOCKET).exists()


def test_a_request_keeps_the_daemon_from_going_idle(box):
    hook = box.claude_hook()
    daemon = box.start_daemon("--idle", "10")
    began = time.monotonic()
    while time.monotonic() - began < 14:  # longer than the idle time, with a request in every gap of 2 seconds
        time.sleep(2)
        assert box.run(hook, claude_payload("Bash", command=BENIGN)).returncode == 0
        assert daemon.poll() is None
    assert daemon.wait(timeout=60) == 0  # and it goes once the requests stop


def test_stop_asks_the_running_daemon_to_exit(box):
    daemon = box.start_daemon()
    done = subprocess.run(box.daemon_args("--stop"), env=box.env, capture_output=True, timeout=30, check=False)
    assert done.returncode == 0 and daemon.wait(timeout=15) == 0
    again = subprocess.run(box.daemon_args("--stop"), env=box.env, capture_output=True, timeout=30, check=False)
    assert again.returncode == 1  # none running


def test_a_policy_change_is_picked_up_without_a_restart(box):
    hook = box.claude_hook()
    daemon = box.start_daemon()
    assert box.run(hook, claude_payload("Bash", command="make deploy")).returncode == 0
    box.set_policy('[gate]\ndeny_commands = ["make deploy"]\n')
    denied = box.run(hook, claude_payload("Bash", command="make deploy"))
    assert denied.returncode == 2 and "deny_commands" in denied.stderr
    box.set_policy("")
    assert box.run(hook, claude_payload("Bash", command="make deploy")).returncode == 0
    assert daemon.poll() is None  # the same daemon answered all three


def test_a_broken_policy_is_a_deny_from_the_daemon_too(box):
    hook = box.claude_hook()
    box.start_daemon()
    box.set_policy("this is not [toml")
    done = box.run(hook, claude_payload("Bash", command=BENIGN))
    assert done.returncode == 2 and "blocked" in done.stderr


def test_ws_upgraded_under_the_daemon_is_picked_up(box):
    """A copy of the package stands for an installed ws: the daemon runs from it, then one of its files changes."""
    pkg = box.root / "site"
    shutil.copytree(SRC, pkg / "ws", ignore=shutil.ignore_patterns("__pycache__"))
    ws_cmd = [PY, "-I", "-c", BOOT, str(pkg)]
    hook = box.claude_hook(ws_cmd)
    daemon = box.start_daemon(argv=[*ws_cmd, "gate", "serve", *box.pin.args()])
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2
    assert box.log[-1]["via"] == "daemon"
    with open(pkg / "ws" / "paths.py", "a", encoding="utf-8") as f:
        f.write("\n# a new release\n")
    after = box.run(hook, claude_payload("Bash", command=SECRET_READ))
    assert after.returncode == 2 and "deny_read" in after.stderr  # the answer is the same, decided by the client
    assert daemon.wait(timeout=15) == 0 and not (box.run_dir / gateclient.SOCKET).exists()
    assert box.run(hook, claude_payload("Bash", command=BENIGN)).returncode == 0
    assert wait_for(box.serving), "a daemon on the new code was not started"
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2
    assert box.log[-1]["via"] == "daemon"


def test_the_code_stamp_changes_when_a_file_does_and_when_one_goes(tmp_path, monkeypatch):
    pkg = tmp_path / "ws"
    (pkg / "sub").mkdir(parents=True)
    (pkg / "a.py").write_text("x = 1\n")
    (pkg / "sub" / "b.py").write_text("y = 1\n")
    (pkg / "sub" / "__pycache__").mkdir()
    (pkg / "sub" / "__pycache__" / "c.py").write_text("ignored\n")
    monkeypatch.setattr(gateserve, "__file__", str(pkg / "gateserve.py"))
    before = gateserve.code_stamp()
    assert gateserve.code_stamp() == before and len(before) == 2
    (pkg / "sub" / "b.py").write_text("y = 22\n")
    assert gateserve.code_stamp() != before
    again = gateserve.code_stamp()
    (pkg / "a.py").unlink()
    assert gateserve.code_stamp() != again
    shutil.rmtree(pkg)
    assert gateserve.code_stamp() == ("gone",)


# --- a socket that is not the daemon's ---------------------------------------------------------------------------


FAKE_SERVER = r"""
import contextlib, fcntl, json, os, socket, sys
run, run_mode, sock_mode, seen, ready, spec = sys.argv[5:11]
spec = json.loads(spec)
os.makedirs(run, exist_ok=True)
os.chmod(run, int(run_mode, 8))
lock = os.open(os.path.join(run, "gate.lock"), os.O_RDWR | os.O_CREAT, 0o600)
fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
os.chdir(run)
with contextlib.suppress(FileNotFoundError):
    os.unlink("gate.sock")
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind("gate.sock")
os.chmod("gate.sock", int(sock_mode, 8))
server.listen(8)
open(ready, "w").close()
while True:
    conn, _ = server.accept()
    with conn, contextlib.suppress(OSError):
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                break
            data += chunk
        request = json.loads(data) if data.strip() else {}
        with open(seen, "a") as f:
            f.write(json.dumps(request) + "\n")
        kind = spec["kind"]
        if kind == "close":
            continue
        if kind == "raw":
            conn.sendall(spec["data"].encode())
            continue
        reply = {"v": 1, "id": request.get("id"), "code": 0, "out": "", "err": ""}
        reply.update(spec.get("set", {}))
        for key in spec.get("drop", []):
            reply.pop(key, None)
        conn.sendall(json.dumps(reply).encode() + b"\n")
"""


class FakeDaemon:
    """A server on the daemon's socket path that answers as `spec` says: `{"kind": "allow"}` (the right id, an exit code
    of 0) with `set` and `drop` changes to the reply, `{"kind": "raw", "data": ...}` or `{"kind": "close"}`. It runs as
    its own process with a command line of `-m ws gate serve`, the same interpreter, so it passes the client's check
    of who is on the other end: the checks that follow are then the ones under test. It holds the lock file, so no real
    daemon is started over it."""

    def __init__(self, box: Box, spec, run_mode=0o700, sock_mode=0o600, argv_head=None):
        self.box = box
        self.seen_file = box.root / "fake-seen.jsonl"
        self.ready = box.root / "fake-ready"
        self.ready.unlink(missing_ok=True)  # the last stand-in's marker would end the wait before this one has bound
        head = argv_head or [PY, "-I", "-c", FAKE_SERVER, "-m", "ws", "gate", "serve"]
        argv = [
            *head,
            str(box.run_dir),
            format(run_mode, "o"),
            format(sock_mode, "o"),
            str(self.seen_file),
            str(self.ready),
            json.dumps(spec),
        ]
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        box.procs.append(self.proc)
        assert wait_for(self.ready.exists), "the stand-in server did not come up"

    @property
    def seen(self) -> list[dict]:
        try:
            return [json.loads(line) for line in self.seen_file.read_text().splitlines()]
        except OSError:
            return []

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)


ALLOW = {"kind": "allow"}


@pytest.fixture
def fake(box):
    made: list[FakeDaemon] = []

    def make(spec=ALLOW, **kw):
        made.append(FakeDaemon(box, spec, **kw))
        return made[-1]

    yield make
    for f in made:
        f.close()


def test_a_server_of_ours_that_allows_everything_is_believed_only_when_it_is_private(box, fake):
    """The control: a private socket of this user's is the daemon, so its allow is the answer. The cases below take
    away one thing at a time, and the deny comes back."""
    hook = box.claude_hook()
    server = fake()
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 0
    assert server.seen and server.seen[0]["kind"] == "check"


def test_a_run_folder_other_users_can_enter_is_not_used(box, fake):
    hook = box.claude_hook()
    server = fake(run_mode=0o755)
    done = box.run(hook, claude_payload("Bash", command=SECRET_READ))
    assert done.returncode == 2 and not server.seen


def test_a_socket_other_users_can_write_to_is_not_used(box, fake):
    hook = box.claude_hook()
    server = fake(sock_mode=0o666)
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2 and not server.seen


def test_a_run_folder_that_is_a_link_is_not_used(box, fake):
    hook = box.claude_hook()
    real = box.root / "real-run"
    box.run_dir.parent.mkdir(parents=True, exist_ok=True)
    real.mkdir(mode=0o700)
    box.run_dir.symlink_to(real)
    assert gateclient.open_run_dir(str(box.state)) is None
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2


def test_a_peer_that_is_not_this_user_is_not_used(box, fake, monkeypatch):
    server = fake()
    fd = gateclient.open_run_dir(str(box.state))
    try:
        conn = gateclient.connect(fd)
        assert conn is not None  # the control
        conn.close()
        monkeypatch.setattr(gateclient, "peer_cred", lambda c: (os.getpid(), os.geteuid() + 1))
        assert gateclient.connect(fd) is None
        monkeypatch.setattr(gateclient, "peer_cred", lambda c: None)  # a system that can't say who is there
        assert gateclient.connect(fd) is None
    finally:
        os.close(fd)
    assert [s for s in server.seen if s] == []  # nothing was sent to it


def test_a_stand_in_that_is_not_running_ws_gate_serve_is_not_used(box, fake):
    """The same user, a private socket and a reply that echoes the id: still not believed when the process behind it
    is not `-m ws gate serve` (Linux asks /proc). The control is the stand-in that is."""
    hook = box.claude_hook()
    stranger = fake(argv_head=[PY, "-I", "-c", FAKE_SERVER, "-m", "ws", "gate", "status"])
    done = box.run(hook, claude_payload("Bash", command=SECRET_READ))
    assert (
        done.returncode == 2 and "deny_read" in done.stderr and not any(stranger.seen)
    )  # it was connected to, and sent nothing
    stranger.close()
    box.run_dir.joinpath(gateclient.SOCKET).unlink()
    believed = fake()
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 0 and believed.seen


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the process is identified through /proc")
def test_a_peer_running_another_interpreter_is_not_the_daemon(box):
    daemon = box.start_daemon()
    fd = gateclient.open_run_dir(str(box.state))
    try:
        ws_cmd = [PY, "-I", "-m", "ws"]
        conn = gateclient.connect(fd, ws_cmd)
        assert conn is not None  # the control: this interpreter, `-m ws gate serve`
        conn.close()
        assert gateclient.is_the_daemon(daemon.pid, ws_cmd)
        assert not gateclient.is_the_daemon(os.getpid(), ws_cmd)  # this test process is not a daemon
        assert not gateclient.is_the_daemon(None, ws_cmd) and not gateclient.is_the_daemon(0, ws_cmd)
        assert not gateclient.is_the_daemon(2**22 + 12345, ws_cmd)  # no such process
        monkey = pytest.MonkeyPatch()
        try:
            monkey.setattr(gateclient, "interpreters", lambda cmd: {"/nowhere/python"})
            assert gateclient.connect(fd, ws_cmd) is None
        finally:
            monkey.undo()
    finally:
        os.close(fd)


def test_a_daemon_started_by_the_hooks_own_command_is_known_by_that_command(box):
    """A launcher of ws that is not `-m ws` (the upgrade test uses one) is the daemon when the command line begins with
    the hook's own `ws_cmd` and then `gate serve`."""
    pkg = box.root / "site"
    shutil.copytree(SRC, pkg / "ws", ignore=shutil.ignore_patterns("__pycache__"))
    ws_cmd = [PY, "-I", "-c", BOOT, str(pkg)]
    daemon = box.start_daemon(argv=[*ws_cmd, "gate", "serve", *box.pin.args()])
    if sys.platform.startswith("linux"):
        assert gateclient.is_the_daemon(daemon.pid, ws_cmd)
        assert not gateclient.is_the_daemon(daemon.pid, [PY, "-I", "-c", BOOT, str(box.root / "elsewhere")])


def test_a_reply_that_drips_in_is_waited_for_only_as_long_as_the_whole_reply_may_take(monkeypatch):
    here, there = socket.socketpair()
    monkeypatch.setattr(gateclient, "REPLY_TIMEOUT", 0.6)

    def drip():
        for _ in range(40):
            try:
                there.sendall(b"x")
            except OSError:
                return
            time.sleep(0.1)

    thread = threading.Thread(target=drip, daemon=True)
    thread.start()
    began = time.monotonic()
    assert gateclient.ask(here, {"id": "abc", "kind": "check"}) is None
    assert time.monotonic() - began < 2.0  # not 40 pieces of 0.6 seconds each
    here.close()
    there.close()
    thread.join(timeout=5)


@pytest.mark.parametrize(
    "spec",
    [
        {"kind": "raw", "data": "not json at all\n"},
        {"kind": "raw", "data": "{}\n"},
        {"kind": "raw", "data": '["a", "list"]\n'},
        {"kind": "close"},  # closes without a word
        {"kind": "allow", "set": {"id": "someone-elses-request"}},
        {"kind": "allow", "set": {"v": 2}},
        {"kind": "allow", "set": {"code": 1}},
        {"kind": "allow", "set": {"code": 3}},
        {"kind": "allow", "set": {"code": True}},
        {"kind": "allow", "set": {"code": "0"}},
        {"kind": "allow", "set": {"out": None}},
        {"kind": "allow", "drop": ["err"]},
        {"kind": "raw", "data": '{"v": 1, "id": "x"'},  # cut off
    ],
    ids=[
        "garbage",
        "empty-object",
        "list",
        "closed",
        "wrong-id",
        "wrong-version",
        "code-1",
        "code-3",
        "code-true",
        "code-text",
        "out-null",
        "err-missing",
        "cut-off",
    ],
)
def test_a_reply_that_is_not_a_well_formed_answer_is_not_an_answer(box, fake, spec):
    hook = box.claude_hook()
    server = fake(spec)
    done = box.run(hook, claude_payload("Bash", command=SECRET_READ))
    assert done.returncode == 2 and "deny_read" in done.stderr  # decided by the client itself
    assert server.seen, "the stand-in was never asked, so the reply was never judged"


def test_a_dead_socket_file_is_not_an_answer(box):
    hook = box.claude_hook()
    box.run_dir.mkdir(parents=True)
    box.run_dir.chmod(0o700)
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    here = os.getcwd()
    os.chdir(box.run_dir)
    try:
        stale.bind(gateclient.SOCKET)
    finally:
        os.chdir(here)
    stale.close()  # bound, never listening: connect is refused
    assert box.run(hook, claude_payload("Bash", command=SECRET_READ)).returncode == 2
    assert box.run(hook, claude_payload("Bash", command=BENIGN)).returncode == 0


def test_a_daemon_that_answers_too_slowly_is_not_waited_for_forever(monkeypatch):
    here, there = socket.socketpair()  # `there` never answers
    monkeypatch.setattr(gateclient, "REPLY_TIMEOUT", 0.3)
    began = time.monotonic()
    assert gateclient.ask(here, {"id": "abc", "kind": "check"}) is None
    assert time.monotonic() - began < 10
    here.close()
    there.close()


def test_asked_not_to_use_the_daemon_the_client_never_connects(box, fake):
    hook = box.claude_hook()
    server = fake()
    done = box.run(hook, claude_payload("Bash", command=SECRET_READ), {"WS_GATE_DAEMON": "0"})
    assert done.returncode == 2 and not server.seen


# --- the client on its own ---------------------------------------------------------------------------------------


def good(ident="abc") -> dict:
    return {"v": 1, "id": ident, "code": 2, "out": "", "err": "no\n"}


def test_what_makes_a_reply_well_formed():
    assert gateclient.valid_reply(good(), "abc")
    assert gateclient.valid_reply({**good(), "code": 0}, "abc") and gateclient.valid_reply({**good(), "code": 4}, "abc")
    assert not gateclient.valid_reply(good(), "other")
    for bad in (
        {"code": 1},
        {"code": -1},
        {"code": 2.0},
        {"code": False},
        {"out": 1},
        {"err": b""},
        {"v": "1"},
        {"v": 0},
    ):
        assert not gateclient.valid_reply({**good(), **bad}, "abc"), bad
    assert not gateclient.valid_reply(None, "abc") and not gateclient.valid_reply([], "abc")


def test_the_options_after_gate_kind_are_what_the_client_splits_off():
    command = [
        "/usr/bin/py",
        "-I",
        "-m",
        "ws",
        "gate",
        "check",
        "--runner",
        "claude-code",
        "--pin",
        "home=/h",
        "--pin",
        "state=/s",
    ]
    assert gateclient.split_command("check", command) == (["/usr/bin/py", "-I", "-m", "ws"], command[6:])
    assert gateclient.split_command("event", command) is None
    assert gateclient.split_command("check", ["gate", "check"]) is None  # nothing runs ws
    assert gateclient.pin_args(command[6:]) == ["--pin", "home=/h", "--pin", "state=/s"]
    assert gateclient.pin_args(["--runner", "x", "--pin"]) == []


def test_a_client_started_wrongly_is_a_deny_with_a_reason(capfd):
    wrong = (
        [],
        ["check"],
        ["bogus", "/s", "--", "ws", "gate", "bogus"],
        ["check", "/s", "ws", "gate", "check"],
        ["check", "/s", "--", "ws"],
        ["check", "/s", "--", "ws", "gate", "event"],
    )
    for argv in wrong:
        assert gateclient.main(argv) == 2, argv
        assert "blocked" in capfd.readouterr().err


def test_the_client_runs_the_fallback_with_the_payload_and_passes_its_code_through(box):
    script = "import sys; sys.stdout.write('out:' + sys.stdin.read()); sys.stderr.write('err'); sys.exit(4)"
    done = box.run(
        [
            PY,
            "-I",
            "-S",
            str(SRC / "gateclient.py"),
            "check",
            str(box.state),
            "--",
            PY,
            "-I",
            "-c",
            script,
            "gate",
            "check",
        ],
        "payload",
        {"WS_GATE_DAEMON": "0"},
    )
    assert (done.returncode, done.stdout, done.stderr) == (4, "out:payload", "err")
    dying = "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"
    done = box.run(
        [
            PY,
            "-I",
            "-S",
            str(SRC / "gateclient.py"),
            "check",
            str(box.state),
            "--",
            PY,
            "-I",
            "-c",
            dying,
            "gate",
            "check",
        ],
        "x",
        {"WS_GATE_DAEMON": "0"},
    )
    assert done.returncode == 2  # killed by a signal: a deny, not a pass
    gone = box.run(
        [
            PY,
            "-I",
            "-S",
            str(SRC / "gateclient.py"),
            "check",
            str(box.state),
            "--",
            str(box.root / "no-such-ws"),
            "gate",
            "check",
        ],
        "x",
        {"WS_GATE_DAEMON": "0"},
    )
    assert gone.returncode == 2 and "could not be run" in gone.stderr


def test_a_payload_that_is_not_text_goes_to_the_fallback(box):
    box.start_daemon()
    hook = box.claude_hook()
    done = subprocess.run(
        ["sh", "-c", hook], input=b"\xff\xfe not utf-8", env=box.env, capture_output=True, timeout=60, check=False
    )
    assert done.returncode == 2 and b"blocked" in done.stderr


def test_when_a_start_is_worth_trying(box):
    state = str(box.state)
    box.state.mkdir(parents=True, exist_ok=True)
    assert gateclient.should_start(None, state)  # no run folder yet, and the state folder is ours to write
    box.run_dir.mkdir(mode=0o700)
    fd = gateclient.open_run_dir(state)
    try:
        assert gateclient.should_start(fd, state)
        (box.run_dir / gateclient.BACKOFF).write_text("")
        assert not gateclient.should_start(fd, state)  # a start failed just now
        old = time.time() - gateclient.SPAWN_BACKOFF - 5
        os.utime(box.run_dir / gateclient.BACKOFF, (old, old))
        assert gateclient.should_start(fd, state)
        import fcntl

        held = os.open(box.run_dir / gateclient.LOCK, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(held, fcntl.LOCK_EX)
        assert not gateclient.should_start(fd, state)  # a daemon holds the lock: one is starting or running
        os.close(held)
        box.run_dir.chmod(0o500)
        assert not gateclient.should_start(fd, state) or os.geteuid() == 0  # a folder this user can't write (the wall)
        box.run_dir.chmod(0o700)
    finally:
        os.close(fd)
    assert not gateclient.should_start(None, str(box.root / "no-state-folder"))


def test_a_private_folder_is_one_of_ours_nobody_else_can_enter(tmp_path):
    ok = tmp_path / "ok"
    ok.mkdir(mode=0o700)
    ok.chmod(0o700)
    for mode, expect in ((0o700, True), (0o750, False), (0o705, False), (0o770, False), (0o755, False)):
        ok.chmod(mode)
        fd = os.open(ok, os.O_RDONLY)
        try:
            assert gateclient.private_dir(fd) is expect, oct(mode)
        finally:
            os.close(fd)


def test_the_daemon_refuses_a_run_folder_that_isnt_private_and_says_why(box):
    box.run_dir.mkdir(parents=True)
    box.run_dir.chmod(0o755)
    done = subprocess.run(box.daemon_args(), env=box.env, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 1 and "must belong to you and be closed to other users" in done.stderr
    assert not (box.run_dir / gateclient.SOCKET).exists()


def test_the_daemon_will_not_take_over_a_path_that_holds_something_else(box):
    box.run_dir.mkdir(parents=True)
    box.run_dir.chmod(0o700)
    (box.run_dir / gateclient.SOCKET).write_text("not a socket")
    done = subprocess.run(box.daemon_args(), env=box.env, capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 1 and "isn't a socket of yours" in done.stderr
    assert (box.run_dir / gateclient.SOCKET).read_text() == "not a socket"
    assert (box.run_dir / gateclient.BACKOFF).exists()  # and clients are told not to keep trying


def test_a_long_state_path_is_no_problem_for_the_socket(tmp_path, monkeypatch):
    deep = tmp_path.joinpath(*["a-folder-with-a-long-name"] * 6)  # far past the 104 bytes a Unix socket path allows
    deep.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(deep / "home"))
    monkeypatch.setenv("WS_HOME", str(deep / "w"))
    (deep / "home").mkdir()
    (deep / "w").mkdir()
    b = Box.__new__(Box)
    b.root, b.home, b.ws = deep, deep / "home", deep / "w"
    b.env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(deep / "home"),
        "WS_HOME": str(deep / "w"),
        "LANG": "C.UTF-8",
    }
    b.policy_file = deep / "w" / "policy.toml"
    b.policy_file.write_text(policy.default_toml())
    b.pin = paths.pin_of(b.env)
    b.out = deep / "out"
    b.procs = []
    try:
        assert len(str(b.run_dir / gateclient.SOCKET)) > 108
        b.start_daemon()
        assert b.run(b.claude_hook(), claude_payload("Bash", command=SECRET_READ)).returncode == 2
        assert b.log[-1]["via"] == "daemon"
    finally:
        b.stop()


def test_the_hook_commands_carry_on_failure_block_where_a_failure_must_block(box):
    box.set_policy('[guards]\non = ["syntax-check", "session-context"]\n')
    files = claude_code.generate(
        policy.load(box.policy_file), [PY, "-I", "-m", "ws"], box.out, box.policy_file, pin=box.pin
    )
    hooks = json.loads(files["settings.json"])["hooks"]
    pre = hooks["PreToolUse"][0]["hooks"][0]
    assert pre["onFailure"] == "block" and pre["command"].endswith("|| exit 2")
    post = hooks["PostToolUse"][0]["hooks"][0]
    assert post["onFailure"] == "block"
    for name in ("SessionStart", "UserPromptSubmit", "Notification"):
        for group in hooks[name]:
            assert all("onFailure" not in h for h in group["hooks"]), name  # advice: never blocks a prompt
    assert client_argv("check", ["ws", "gate", "check"], box.pin)[:3] == [PY, "-I", "-S"]
