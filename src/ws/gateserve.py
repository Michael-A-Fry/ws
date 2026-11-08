"""`ws gate serve`: the resident gate, one process per user that answers every hook call from every session.

A hook's thin client (ws.gateclient) sends one request over a Unix socket in `<state>/run`: the payload, the
environment and the folder the hook was started in. The daemon forks a child for the request, gives the child that
environment and folder, and has it run exactly what `ws gate check` or `ws gate event` runs (ws.gate.run_hook): the
policy's rules first, then the guards that are on in their fixed order, the first refusal ending it. The child's exit code, stdout and
stderr go back as the reply. A fork for each request keeps one call's environment, folder and state out of the next, and
the modules are loaded once, in the parent, so a call costs a fork and not an import of ws.

The daemon is started by the first hook call that finds none running. There is one for each state folder; it holds an
exclusive lock on `<state>/run/gate.lock` (with its process id in it) for as long as it lives. It exits after `--idle`
seconds without a request (15 minutes by default), when its socket file is removed or replaced, and when ws is
upgraded under it: a request that arrives after any of ws's own `.py` files changed is closed without an answer (the
client then decides in its own process) and the daemon exits, so the next call starts one that runs the new code. A
policy change needs no restart: each request reads the policy again.

It only ever serves its own user. The folder must be private (0700, ours) or the daemon refuses to start; the socket is
0600; each connection's peer must be this user's. A request it can't read, an option the command doesn't take, or a child
that runs past its limit gets no reply, and the client decides in its own process.

    ws gate serve [--pin KEY=PATH ...] [--idle SECONDS]    serve in the foreground until stopped
    ws gate serve --stop                                   ask the running daemon to exit
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import stat
import sys
import time
from pathlib import Path

from ws import gateclient, paths

IDLE = 900.0  # seconds without a request before the daemon exits
REQUEST_LIMIT = 5.0  # seconds a client has to send its request
HARD_LIMIT = 10  # seconds one request may take before its child is stopped (the client waits 11: ws.gateclient)
MAX_CHILDREN = 32  # requests in flight at once; the next is closed unanswered and its client decides itself
TICK = 1.0  # seconds between looks at the idle clock and the socket file


class ServeError(Exception):
    """The daemon can't start here (the run folder isn't private, something else sits where the socket goes)."""


def code_stamp() -> tuple:
    """A fingerprint of ws's own code: (path, mtime, size) of every `.py` file in the package. It changes when ws is
    upgraded or edited, and a missing file counts as a change."""
    root = Path(__file__).resolve().parent
    found: list[tuple[str, int, int]] = []

    def walk(folder: Path) -> None:
        with os.scandir(folder) as it:
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    if e.name != "__pycache__":
                        walk(Path(e.path))
                elif e.name.endswith(".py"):
                    st = e.stat(follow_symlinks=False)
                    found.append((e.path, st.st_mtime_ns, st.st_size))

    try:
        walk(root)
    except OSError:
        return ("gone",)
    return tuple(sorted(found))


def _run_dir(state: Path) -> int:
    """The run folder open, made private if it's new. A folder that exists must already be ours and closed to others:
    it might be somewhere another user can write, and it is never chmod-ed."""
    run = state / gateclient.RUN
    if not run.exists():
        run.mkdir(mode=0o700, parents=True)
        run.chmod(0o700)  # past the umask
    try:
        fd = os.open(run, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    except OSError as e:
        raise ServeError(f"{run} can't be opened ({type(e).__name__})") from e
    if not gateclient.private_dir(fd):
        os.close(fd)
        raise ServeError(f"{run} must belong to you and be closed to other users (mode 0700)")
    return fd


def _bind(dir_fd: int) -> tuple[socket.socket, int]:
    """(the listening socket, its inode), bound in the run folder. A stale socket of ours is replaced; anything else
    where the socket goes is a refusal."""
    try:
        st = os.lstat(gateclient.SOCKET, dir_fd=dir_fd)
    except FileNotFoundError:
        pass
    else:
        if not stat.S_ISSOCK(st.st_mode) or st.st_uid != os.geteuid():
            raise ServeError(f"{gateclient.SOCKET} in the run folder isn't a socket of yours")
        os.unlink(gateclient.SOCKET, dir_fd=dir_fd)
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    here = os.open(".", os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    old = os.umask(0o177)
    try:
        os.fchdir(dir_fd)
        listener.bind(gateclient.SOCKET)
    except OSError as e:
        listener.close()
        raise ServeError(f"can't bind the socket ({type(e).__name__}: {e})") from e
    finally:
        os.umask(old)
        os.fchdir(here)
        os.close(here)
    listener.listen(64)
    return listener, os.lstat(gateclient.SOCKET, dir_fd=dir_fd).st_ino


def _still_ours(dir_fd: int, inode: int) -> bool:
    try:
        st = os.lstat(gateclient.SOCKET, dir_fd=dir_fd)
    except OSError:
        return False
    return stat.S_ISSOCK(st.st_mode) and st.st_ino == inode


def _request(conn: socket.socket) -> dict | None:
    """The request on `conn`, or None unless it's well-formed and from this user."""
    if gateclient.peer_uid(conn) != os.geteuid():
        return None
    conn.settimeout(REQUEST_LIMIT)
    line = gateclient.read_line(conn)
    if line is None:
        return None
    try:
        req = json.loads(line)
    except ValueError:
        return None
    if not isinstance(req, dict) or req.get("v") != gateclient.PROTOCOL:
        return None
    env, argv = req.get("env"), req.get("argv")
    ok = (
        isinstance(req.get("id"), str)
        and 0 < len(req["id"]) <= 64
        and req.get("kind") in gateclient.KINDS
        and isinstance(argv, list)
        and all(isinstance(a, str) for a in argv)
        and isinstance(env, dict)
        and all(isinstance(k, str) and isinstance(v, str) for k, v in env.items())
        and isinstance(req.get("cwd"), str)
        and isinstance(req.get("stdin"), str)
    )
    return req if ok else None


def answer(conn: socket.socket) -> None:
    """Read one request from `conn`, run it as the hook command would and write the reply. Runs in the forked child,
    which never goes back to the loop. No reply for anything it can't run (the client then decides itself)."""
    from ws import gate

    req = _request(conn)
    if req is None:
        return
    os.environ.clear()
    os.environ.update(req["env"])
    try:
        if req["cwd"]:
            os.chdir(req["cwd"])
        code, out, err = gate.run_hook(req["kind"], req["argv"], req["stdin"], via="daemon")
    except (OSError, SystemExit):
        return
    reply = {"v": gateclient.PROTOCOL, "id": req["id"], "code": code, "out": out, "err": err}
    conn.settimeout(REQUEST_LIMIT)
    conn.sendall(json.dumps(reply).encode("ascii") + b"\n")


def _child(conn: socket.socket, listener: socket.socket, lock: int) -> None:
    import signal

    try:
        with contextlib.suppress(BaseException):  # whatever goes wrong, the child ends and the client decides itself
            listener.close()
            os.close(lock)
            for sig in (signal.SIGCHLD, signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, signal.SIG_DFL)
            signal.alarm(HARD_LIMIT)
            answer(conn)
    finally:
        os._exit(0)


def serve(pin: paths.Pin, idle: float = IDLE) -> int:
    """Serve until idle, stopped, replaced or outdated. 0 on a clean exit (including "a daemon is already serving"),
    1 when it can't start (and a marker tells clients not to try again for half a minute)."""
    import fcntl
    import signal

    state = Path(pin.state)
    try:
        dir_fd = _run_dir(state)
    except (ServeError, OSError) as e:
        print(f"ws gate serve: {e}", file=sys.stderr)
        return 1
    lock = os.open(gateclient.LOCK, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600, dir_fd=dir_fd)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return 0  # another daemon is serving
    os.ftruncate(lock, 0)
    os.write(lock, f"{os.getpid()}\n".encode())
    try:
        listener, inode = _bind(dir_fd)
    except (ServeError, OSError) as e:
        print(f"ws gate serve: {e}", file=sys.stderr)
        try:
            os.close(os.open(gateclient.BACKOFF, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600, dir_fd=dir_fd))
        except OSError:
            pass
        return 1
    _preload()
    children = [0]
    stopping = [False]

    def reap(*_: object) -> None:
        while True:
            try:
                pid, _status = os.waitpid(-1, os.WNOHANG)
            except ChildProcessError:
                return
            if pid == 0:
                return
            children[0] = max(0, children[0] - 1)

    def on_term(*_: object) -> None:
        stopping[0] = True

    signal.signal(signal.SIGCHLD, reap)
    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    stamp = code_stamp()
    last = time.monotonic()
    try:
        import select

        while not stopping[0]:
            ready, _, _ = select.select([listener], [], [], TICK)
            now = time.monotonic()
            if not ready:
                if now - last > idle or not _still_ours(dir_fd, inode):
                    break
                continue
            try:
                conn, _addr = listener.accept()
            except OSError:
                continue
            last = now
            if code_stamp() != stamp:  # ws changed under this daemon: leave the request to the client and go
                conn.close()
                break
            if children[0] >= MAX_CHILDREN:
                conn.close()
                continue
            try:
                pid = os.fork()
            except OSError:  # no room for another process: this client decides itself
                conn.close()
                continue
            if pid == 0:
                _child(conn, listener, lock)
            children[0] += 1
            conn.close()
    finally:
        if _still_ours(dir_fd, inode):
            try:
                os.unlink(gateclient.SOCKET, dir_fd=dir_fd)
            except OSError:
                pass
        listener.close()
    return 0


def _preload() -> None:
    """Load what a request runs, once, so each forked child starts with it."""
    from ws import adapters, gate, guards, policy, shellwords, unknownpath  # noqa: F401

    adapters.all_adapters()
    guards.all_guards()


def stop(pin: paths.Pin) -> int:
    """Ask the running daemon to exit: 0 if one was running and was told to, 1 if none is."""
    import fcntl
    import signal

    state = Path(pin.state)
    fd = gateclient.open_run_dir(str(state))
    if fd is None:
        return 1
    try:
        lock = os.open(gateclient.LOCK, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), dir_fd=fd)
    except OSError:
        return 1
    finally:
        os.close(fd)
    try:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:  # held: the process whose id is in the file is the daemon
            try:
                pid = int(os.read(lock, 32).decode().strip())
                os.kill(pid, signal.SIGTERM)
            except (ValueError, OSError):
                return 1
            return 0
        return 1  # nobody held it
    finally:
        os.close(lock)


def cmd_serve(args: argparse.Namespace) -> int:
    pin = paths.read_pin(args.pin)
    if pin is None:
        pin = paths.pin_of()
    if args.stop:
        return stop(pin)
    return serve(pin, args.idle)


def register(sub) -> None:
    p = sub.add_parser("serve", help="the resident gate: answer every hook call of a user's sessions from one process")
    paths.add_pin_option(p)
    p.add_argument("--idle", type=float, default=IDLE, metavar="SECONDS", help="exit after this long without a request")
    p.add_argument("--stop", action="store_true", help="ask the running daemon to exit")
    p.set_defaults(func=cmd_serve, raw=True)  # stays up until stopped
