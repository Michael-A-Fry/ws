"""The gate's thin hook client: ask the resident gate for the decision, and decide in this process when it can't answer.

A runner's hook runs `python -I -S gateclient.py KIND STATE -- WS_CMD gate KIND OPTIONS...`. This file imports the
standard library and nothing else, so a hook call costs a few milliseconds of start-up and not the whole of ws.

It sends the payload, the environment and the folder to `ws gate serve` over a Unix socket in `<state>/run`, and prints
what comes back: the exit code (0 allow, 2 deny, 4 a claim), stdout and stderr. The socket is not a channel another
local process of another user can answer on. The client uses it only when the folder and the socket both belong to
this user and nobody else can enter them (mode 0700 and 0600, no links followed), and when the kernel says the process
on the other end is this user's. On Linux it also asks /proc who that process is: the same interpreter ws is started
with, and a command line of `-m ws gate serve` (or the hook's own ws command, then `gate serve`). The reply must carry
the request's random id and an exit code from that short list. Anything else is not an answer.

What this does not stop: a process of the same user that is not in a sandbox can stop the daemon and run something
that looks like it, as it can edit the policy. With the sandbox on, the run folder is read-only to the session; without
it, the gate's check of writes is what keeps a session out of the state folder.

When there is no answer (no daemon, a dead socket, a time-out, a reply that can't be read, a daemon from an older ws, a
run folder that isn't private, a peer that isn't the daemon) the client runs `WS_CMD gate KIND OPTIONS...` itself with the same payload: the same
code, the same answer, slower. It never allows because the daemon is missing. If no daemon is running it also starts one
for the next call, unless this user can't write the run folder (the sandbox wall) or a start failed in the last half
minute. `WS_GATE_DAEMON=0` skips the daemon and always decides in-process.

The module is also imported by `ws gate serve`, which shares the wire format below. Wire format, one line each way:
request `{"v":1,"id","kind","argv","env","cwd","stdin"}`, reply `{"v":1,"id","code","out","err"}`.
"""

from __future__ import annotations

import json
import os
import socket
import stat
import struct
import sys
import time

PROTOCOL = 1
PACKAGE = "ws"  # what `python -m` is given to start the gate
RUN = "run"
SOCKET = "gate.sock"
LOCK = "gate.lock"
BACKOFF = "gate.backoff"
DISABLE_ENV = "WS_GATE_DAEMON"
CODES = (0, 2, 4)  # allow, deny, claim: the only exit codes a reply may carry
KINDS = ("check", "event")
CONNECT_TIMEOUT = 1.0  # seconds
REPLY_TIMEOUT = 11.0  # in all, for the whole reply: longer than the daemon's own limit on a request (10)
FALLBACK_S = 12.0  # the longest the in-process check takes: the claims guard's git time (8) or a syntax checker (10), plus start-up
WORST_CASE_S = CONNECT_TIMEOUT + REPLY_TIMEOUT + FALLBACK_S  # a hook must be allowed longer than this (HOOK_TIMEOUT)
MAX_LINE = 64 << 20  # bytes in one request or reply
SPAWN_BACKOFF = 30.0  # seconds without another start after one failed


def peer_cred(conn: socket.socket) -> tuple[int | None, int] | None:
    """(the process id, the user id) of the process on the other end of a Unix socket, or None when this system can't say
    (so the socket is not used). The process id is None where the system gives only the user (macOS)."""
    try:
        if sys.platform.startswith("linux"):
            pid, uid, _gid = struct.unpack("3i", conn.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
            return pid, uid
        if sys.platform == "darwin":  # LOCAL_PEERCRED returns a struct xucred: version, then the user id
            return None, struct.unpack_from("II", conn.getsockopt(0, 1, 76))[1]
    except (OSError, struct.error):
        return None
    return None


def peer_uid(conn: socket.socket) -> int | None:
    """The user id of the process on the other end of a Unix socket, or None when this system can't say."""
    cred = peer_cred(conn)
    return None if cred is None else cred[1]


def interpreters(ws_cmd: list[str]) -> set[str]:
    """The real paths a daemon started by `ws_cmd` may be running as: this interpreter, the program `ws_cmd` starts and the
    interpreter on its first line when that is a script."""
    found = {os.path.realpath(sys.executable)}
    if ws_cmd:
        found.add(os.path.realpath(ws_cmd[0]))
        try:
            with open(ws_cmd[0], "rb") as f:
                first = f.readline(256)
        except OSError:
            first = b""
        if first.startswith(b"#!"):
            words = first[2:].decode("utf-8", "replace").split()
            if words:
                found.add(os.path.realpath(words[0]))
    return found


def is_the_daemon(pid: int | None, ws_cmd: list[str]) -> bool:
    """On Linux: is process `pid` the interpreter ws is started with, running `-m ws gate serve`? Reads /proc, so it costs
    two small reads. Where /proc can't say (or there is no process id) the answer is no on Linux and yes elsewhere: the
    user id was the only check there."""
    if not sys.platform.startswith("linux"):
        return True
    if pid is None or pid <= 0:
        return False
    try:
        exe = os.path.realpath(os.readlink(f"/proc/{pid}/exe"))
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            args = f.read().decode("utf-8", "replace").split("\0")
    except OSError:
        return False
    if exe not in interpreters(ws_cmd):
        return False
    if args[: len(ws_cmd) + 2] == [*ws_cmd, "gate", "serve"]:  # started by this very command, as the client starts it
        return True
    for i in range(2, len(args) - 1):  # or by anyone running ws as a module: `python -m ws gate serve`
        if args[i] == "gate" and args[i + 1] == "serve":
            return args[i - 2 : i] == ["-m", PACKAGE]  # not spelled out: a literal "-m ws" is what test_selfcmd forbids
    return False


def private_dir(fd: int) -> bool:
    """Is the folder open as `fd` ours, and closed to everyone else?"""
    st = os.fstat(fd)
    return stat.S_ISDIR(st.st_mode) and st.st_uid == os.geteuid() and not st.st_mode & 0o077


def open_run_dir(state: str) -> int | None:
    """The run folder `<state>/run` open for use, or None unless it's a private folder of ours (never a link)."""
    try:
        fd = os.open(
            os.path.join(state, RUN), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        )
    except OSError:
        return None
    if private_dir(fd):
        return fd
    os.close(fd)
    return None


def connect(dir_fd: int, ws_cmd: list[str] | None = None) -> socket.socket | None:
    """A connection to the daemon's socket in the run folder, or None unless the socket is a private socket of ours and
    the process behind it is this user's and, on Linux, is `ws_cmd`'s interpreter running `-m ws gate serve` (pass the
    command the hook starts ws with; with None only the user is checked). The path to a socket can be longer than the system takes, so the connect is made
    from inside the folder by name (the folder is open, so nothing can swap the path under it)."""
    try:
        st = os.lstat(SOCKET, dir_fd=dir_fd)
    except OSError:
        return None
    if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077:
        return None
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    conn.settimeout(CONNECT_TIMEOUT)
    try:
        here = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        conn.close()
        return None
    try:
        os.fchdir(dir_fd)
        try:
            conn.connect(SOCKET)
        finally:
            os.fchdir(here)
    except OSError:
        conn.close()
        return None
    finally:
        os.close(here)
    cred = peer_cred(conn)
    if cred is None or cred[1] != os.geteuid() or (ws_cmd is not None and not is_the_daemon(cred[0], ws_cmd)):
        conn.close()
        return None
    return conn


def read_line(conn: socket.socket, limit: int = MAX_LINE, end: float | None = None) -> bytes | None:
    """One newline-ended line from `conn`, or None on a close before the end, a time-out, or more than `limit` bytes. With
    `end` (a time.monotonic() value) the whole line is due by then, however slowly the other side sends it."""
    chunks: list[bytes] = []
    size = 0
    try:
        while True:
            if end is not None:
                left = end - time.monotonic()
                if left <= 0:
                    return None
                conn.settimeout(left)
            data = conn.recv(1 << 16)
            if not data:
                return None
            chunks.append(data)
            size += len(data)
            if b"\n" in data:
                break
            if size > limit:
                return None
    except OSError:
        return None
    line = b"".join(chunks)
    end = line.index(b"\n")
    return line[:end] if end <= limit else None


def valid_reply(reply: object, ident: str) -> bool:
    """Is this a well-formed answer to the request `ident`?"""
    return (
        isinstance(reply, dict)
        and reply.get("v") == PROTOCOL
        and reply.get("id") == ident
        and type(reply.get("code")) is int
        and reply["code"] in CODES
        and isinstance(reply.get("out"), str)
        and isinstance(reply.get("err"), str)
    )


def ask(conn: socket.socket, request: dict) -> dict | None:
    """The daemon's answer to `request`, or None if there isn't a well-formed one."""
    try:
        end = time.monotonic() + REPLY_TIMEOUT  # one limit for sending and reading, not one for each piece
        conn.settimeout(REPLY_TIMEOUT)
        conn.sendall(json.dumps(request).encode("ascii") + b"\n")
        line = read_line(conn, end=end)
        reply = json.loads(line) if line is not None else None
    except (OSError, ValueError):
        return None
    return reply if valid_reply(reply, request["id"]) else None


def should_start(dir_fd: int | None, state: str) -> bool:
    """Should the client start a daemon: no run folder yet and this user can make it, or a private one that this user
    can write, that no daemon holds the lock of, and where a start didn't fail just now."""
    try:
        import fcntl
    except ImportError:
        return False
    run = os.path.join(state, RUN)
    if dir_fd is None:
        return os.path.isdir(state) and os.access(state, os.W_OK) and not os.path.lexists(run)
    if not os.access(run, os.W_OK):
        return False
    try:
        if time.time() - os.lstat(BACKOFF, dir_fd=dir_fd).st_mtime < SPAWN_BACKOFF:
            return False
    except OSError:
        pass
    try:
        lock = os.open(LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
    except OSError:
        return False
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False  # a daemon holds it: it's starting, or it can't be reached right now
    finally:
        os.close(lock)
    return True


def start_daemon(ws_cmd: list[str], pins: list[str]) -> None:
    """Start `ws gate serve` detached. Nothing waits for it: this call is answered without it."""
    import subprocess

    try:
        subprocess.Popen(
            [*ws_cmd, "gate", "serve", *pins],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd="/",
            start_new_session=True,
            close_fds=True,
        )
    except (OSError, ValueError):
        pass


def split_command(kind: str, command: list[str]) -> tuple[list[str], list[str]] | None:
    """(the argv that runs ws, the options after `gate KIND`) from the whole fallback command, or None."""
    for i in range(len(command) - 1):
        if command[i] == "gate" and command[i + 1] == kind and i > 0:
            return command[:i], command[i + 2 :]
    return None


def pin_args(options: list[str]) -> list[str]:
    """The `--pin KEY=PATH` pairs among the options."""
    return [x for i, a in enumerate(options[:-1]) if a == "--pin" for x in (a, options[i + 1])]


def _write(fd: int, text: str) -> None:
    data = text.encode("utf-8", "surrogateescape")
    while data:
        data = data[os.write(fd, data) :]


def decide_here(command: list[str], raw: bytes) -> int:
    """Run the fallback command with the payload on its stdin, its output going straight through: its exit code. A
    command that can't start, or dies on a signal, is a deny."""
    import subprocess

    try:
        done = subprocess.run(command, input=raw, check=False)
    except (OSError, ValueError) as e:
        _write(2, f"ws gate: blocked: the gate could not be run ({type(e).__name__})\n")
        return 2
    return done.returncode if done.returncode >= 0 else 2


def main(argv: list[str]) -> int:
    """The hook entry: `KIND STATE -- WS_CMD gate KIND OPTIONS...`. Returns the exit code the hook gives the runner."""
    if len(argv) < 5 or argv[0] not in KINDS or argv[2] != "--":
        _write(2, "ws gate: blocked: the gate client was started wrongly (KIND STATE -- COMMAND)\n")
        return 2
    kind, state, command = argv[0], argv[1], argv[3:]
    split = split_command(kind, command)
    if split is None:
        _write(2, "ws gate: blocked: the gate client's command has no `gate " + kind + "`\n")
        return 2
    ws_cmd, options = split
    raw = sys.stdin.buffer.read()
    if os.environ.get(DISABLE_ENV) != "0":
        answer = None
        dir_fd = open_run_dir(state)
        conn = connect(dir_fd, ws_cmd) if dir_fd is not None else None
        if conn is not None:
            try:
                request = {
                    "v": PROTOCOL,
                    "id": os.urandom(8).hex(),
                    "kind": kind,
                    "argv": options,
                    "env": dict(os.environ),
                    "cwd": _cwd(),
                    "stdin": raw.decode("utf-8"),
                }
                answer = ask(conn, request)
            except (UnicodeDecodeError, ValueError):
                answer = None  # a payload that isn't text: the in-process check says what to do with it
            finally:
                conn.close()
        if answer is None and should_start(dir_fd, state):
            start_daemon(ws_cmd, pin_args(options))
        if dir_fd is not None:
            os.close(dir_fd)
        if answer is not None:
            _write(1, answer["out"])
            _write(2, answer["err"])
            return answer["code"]
    return decide_here(command, raw)


def _cwd() -> str:
    try:
        return os.getcwd()
    except OSError:
        return ""


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
