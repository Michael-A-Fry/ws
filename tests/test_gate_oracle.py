"""The command reader against a real bash. Every exemption and every "known producer" in `ws.policy` is a claim about what
bash or a GNU tool does; this file tests the claims, not the reading of them.

For each probe line the gate is asked first (as the policy tests ask it). A line the gate allows is then run by a real
`bash -c` inside `bwrap` (no network, a made-up home that holds the protected paths, a time limit of 5 s):

- each protected FILE is a FIFO that the harness holds open for reading and writing from the start, with a canary token
  already in it. Whatever reads the FIFO, whenever it does, gets the token at once; the harness then looks at whether
  the token is still there. A token that is gone or changed means the line read or wrote the protected file, and no
  timing is involved. A reader that has taken the token is then given end-of-file, so that `cat` ends;
- each protected FOLDER (and the regular files ws protects) is compared before and after: a new or changed entry means
  the line wrote there;
- the sandbox is kept until every process the line left behind has ended (a grace of a few seconds), so a background job
  or a job a timer ends early still counts;
- a run that did not start (no start marker from inside the sandbox, a `bwrap:` message, a time-out) is an error that
  names the line, never a clean result. A run is clean only when the script inside says so itself: a start marker, an
  end marker and, after its own check that nothing is left running, a nothing-left marker, each carrying a random
  nonce that is given on standard input (never in the arguments or the environment), so the line cannot write them.
  A run the harness has to kill at its deadline is an error even after the end marker.

The test FAILS when the gate allows a line that bash used to touch a protected path. The probes are the cold reviews'
cases (`fixtures/gate_oracle_probes.txt`) and lines made from the policy's own tables: one for each exempt command in the
forms that make a shell run quoted text, one for each unique prefix of the long options that open a file, and the
"known producer" lines. A line is run under three environments: the unresolved names set to a protected file, to a
protected folder, and a hostile one (`TMPDIR`, `CDPATH`, `PATH` and the names all pointing at the home folder), because a
repository's own settings can put any of them in the environment of the shell a session starts.
Linux only. Without a usable `bwrap` the tests skip; `WS_ORACLE_REQUIRE=1` (set in CI) makes that a failure."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from ws import policy

SSH = ".ss" + "h"  # spelled in two parts so that this file is not itself a path word
HOME = "/home/pat"
TOKEN = b"CANARY-5f1e0c7a\n"
LIMIT = 5.0  # seconds a line may run
GRACE = 3.0  # seconds more for what the line left running (a background job) before the sandbox ends
START = "ws-oracle-start"  # printed from inside the sandbox, just before the line runs
END = "ws-oracle-end"  # printed when the line's shell has ended, with its exit status
LEFT = "ws-oracle-left"  # printed when something the line started is still running when the wait is over
CLEAR = "ws-oracle-clear"  # printed after the check that nothing the line started is still running
# Each marker line is "NAME NONCE" (the end marker adds the exit status). The nonce is made for the run and given to the
# script on standard input, so the line can't know it.
REQUIRE = os.environ.get("WS_ORACLE_REQUIRE") == "1"  # CI sets it: a sandbox that can't start is a failure, not a skip
PROBES = Path(__file__).parent / "fixtures" / "gate_oracle_probes.txt"
# The protected files, as FIFOs: (path under the home, the deny_read rule that covers it).
FIFOS = (f"{SSH}/id_rsa", ".aws/credentials", ".gnupg/private.key", ".ws/secrets/KEY")
# What ws protects against writes: regular files, and the folders that hold the protected paths.
FILES = (".ws/policy.toml", ".ws/gate/claude-code.json")
WATCHED = (SSH, ".aws", ".gnupg", ".ws")
SUBS = {  # the placeholders of the probe files
    "UPTGT": f".{SSH[1:].upper()}/id_rsa",
    "TGT": f"{SSH}/id_rsa",
    "SDIR": SSH,
    "GLOB1": ".ss?/id_rsa",
    "GLOB2": ".ss[h]/id_rsa",
    "BRACE1": "{" + SSH + ",x}/id_rsa",
    "PHOME": HOME,
    "WSD": ".ws",
}
_PLAIN = frozenset(
    [
        "HOME",
        "PATH",
        "PWD",
        "OLDPWD",
        "IFS",
        "TMPDIR",
        "CDPATH",
        "USER",
        "SHELL",
        "TERM",
        "WS_HOME",
        "LANG",
        "BASH_ENV",
        "ENV",
    ]
)


def _sandbox_problem() -> str:
    """Why the sandbox can't run lines here, or an empty string when it can."""
    if not sys.platform.startswith("linux"):
        return "needs Linux"
    if not shutil.which("bwrap"):
        return "needs bwrap (bubblewrap) on PATH"
    try:
        done = subprocess.run(
            [*_bwrap_base(), "/bin/sh", "-c", "exit 0"], capture_output=True, text=True, timeout=20, check=False
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"bwrap did not run: {e}"
    if done.returncode != 0:
        return f"bwrap cannot start a sandbox here (exit {done.returncode}): {done.stderr.strip()[:200]}"
    return ""


def _bwrap_base(home: str | None = None) -> list[str]:
    args = ["bwrap", "--unshare-all", "--die-with-parent", "--new-session"]
    for top in ("/usr", "/bin", "/sbin", "/lib", "/lib32", "/lib64", "/etc", "/opt"):
        if os.path.islink(top):
            args += ["--symlink", os.readlink(top), top]
        elif os.path.isdir(top):
            args += ["--ro-bind", top, top]
    args += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--tmpfs", "/home"]
    if home:
        args += ["--bind", home, HOME]
    return [*args, "--chdir", "/tmp"]


PROBLEM = _sandbox_problem()


@pytest.fixture(autouse=True)
def _sandbox_or_skip(request):
    """Every test needs the sandbox, except the one that says why it isn't there: with `WS_ORACLE_REQUIRE=1` that one
    fails, so a CI cell that can't run the oracle is red, not quietly green."""
    if PROBLEM and request.function.__name__ != "test_the_sandbox_the_oracle_needs_can_start":
        pytest.skip(PROBLEM)


def test_the_sandbox_the_oracle_needs_can_start():
    if not PROBLEM:
        return
    if REQUIRE and sys.platform.startswith("linux"):
        pytest.fail(f"WS_ORACLE_REQUIRE=1 but the oracle can't run here: {PROBLEM}")
    pytest.skip(PROBLEM)


@pytest.fixture
def pol(monkeypatch):
    monkeypatch.setenv("HOME", HOME)
    monkeypatch.setenv("WS_HOME", HOME + "/.ws")
    for var in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "CDPATH", "TMPDIR"):
        monkeypatch.delenv(var, raising=False)
    return policy.default_policy()


def gate(pol, line: str) -> str | None:
    return policy.is_denied_command(pol, line, cwd="/tmp", protect=(HOME + "/.ws",))


# --- the harness -------------------------------------------------------------------------------------------------------


class OracleError(AssertionError):
    """A run that did not prove anything: the sandbox did not start the line, or the line did not end in time. Never a
    clean result."""


def _build(home: Path) -> None:
    for d in (SSH, ".aws", ".gnupg", ".ws/secrets", ".ws/gate"):
        (home / d).mkdir(parents=True, exist_ok=True)
        os.chmod(home / d, 0o700)
    for f in FIFOS:
        os.mkfifo(home / f, 0o600)
    for f in FILES:
        (home / f).write_text("canary " + f + "\n")


def _snapshot(home: Path) -> dict[str, tuple]:
    seen: dict[str, tuple] = {}
    for top in WATCHED:
        root = home / top
        for here, dirs, files in os.walk(root):
            for name in (*dirs, *files):
                p = Path(here) / name
                st = p.lstat()
                key = str(p.relative_to(home))
                if stat.S_ISFIFO(st.st_mode):
                    seen[key] = (
                        st.st_mode,
                        st.st_ino,
                    )  # still there, still the same file; its contents are watched apart
                else:
                    seen[key] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
        st = root.lstat()
        seen[top] = (st.st_mode, st.st_mtime_ns)
    return seen


class Result:
    def __init__(self, opened: list[str], changed: list[str], hung: bool, out: str):
        self.opened, self.changed, self.hung, self.out = opened, changed, hung, out

    @property
    def touched(self) -> bool:
        return bool(self.opened or self.changed or TOKEN.decode().strip() in self.out)

    def __repr__(self) -> str:
        return f"opened={self.opened} changed={self.changed} hung={self.hung}"


class _Fifos:
    """The protected files, each held open by the harness for reading and writing with the canary token inside. A reader
    gets the token at once, so there is nothing to race; the token being gone (or something added) is the record that
    the file was read (or written). The harness then gives a reader that has the token end-of-file, so `cat` ends.
    A write that adds nothing (`: > F`, `cp /dev/null F`, `touch F`, a token read and put back) leaves the token where
    it was, so each file is also watched through inotify, from after the token is in: a change of its contents or
    metadata, or a close of a writer, counts as a touch."""

    _MASK = 0x2 | 0x4 | 0x8  # IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE

    def __init__(self, home: Path):
        import ctypes
        import fcntl
        import termios

        self._ioctl = lambda fd: int.from_bytes(fcntl.ioctl(fd, termios.FIONREAD, b"\0\0\0\0"), sys.byteorder)
        libc = ctypes.CDLL(None, use_errno=True)
        self._ino = libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self._ino < 0:
            raise OracleError(f"inotify is not available here (errno {ctypes.get_errno()})")
        self.paths = {f: str(home / f) for f in FIFOS}
        self.fds: dict[str, int] = {}
        self.touched: set[str] = set()
        self._names: dict[int, str] = {}
        for f, path in self.paths.items():
            fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self.fds[f] = fd
            os.write(fd, TOKEN)
            wd = libc.inotify_add_watch(self._ino, path.encode(), self._MASK)
            if wd < 0:
                raise OracleError(f"can't watch {f} (errno {ctypes.get_errno()})")
            self._names[wd] = f

    def _touch(self, f: str) -> None:
        self.touched.add(f)
        if f in self.fds:  # a reader that has the token, or a writer, is given end-of-file
            os.close(self.fds.pop(f))

    def look(self) -> None:
        for f, fd in list(self.fds.items()):
            if self._ioctl(fd) != len(TOKEN):
                self._touch(f)
        try:
            data = os.read(self._ino, 4096)
        except BlockingIOError:
            return
        at = 0
        while at + 16 <= len(data):
            wd, mask, _, size = struct.unpack_from("iIII", data, at)
            at += 16 + size
            if mask & self._MASK and wd in self._names:
                self._touch(self._names[wd])

    def close(self) -> None:
        self.look()
        for fd in self.fds.values():
            os.close(fd)
        self.fds.clear()
        os.close(self._ino)


# What runs inside the sandbox, as `bash -c SCRIPT bash FD LINE STEPS HUP`, with a nonce on standard input. It reads the
# nonce, says it has started, runs the line in bash with the marker's file descriptor closed and standard input empty, says
# the line has ended, and then waits for what the line left running (a background job, a process a timer ends): the sandbox
# goes when this script does, and so does everything still in it. Last of all it looks once more and says either that
# nothing is left or that something is. HUP is 1 when the line is run with the hang-up signal ignored (see `run_bash`).
_INSIDE = (
    r"""
command -v bash >/dev/null 2>&1 || exit 97
IFS= read -r nonce || exit 96
exec </dev/null
[ "$4" = 1 ] && trap '' HUP
printf '%s %s\n' '@START@' "$nonce" >&"$1" || exit 98
eval "bash -c \"\$2\" $1>&-"
printf '%s %s %s\n' '@END@' "$nonce" $? >&"$1"
alive() {
  busy=
  for p in /proc/[0-9]*; do
    case ${p#/proc/} in 1|$$) ;; *) busy=1; break ;; esac
  done
}
n=0
while [ "$n" -lt "$3" ]; do
  alive
  [ -z "$busy" ] && break
  /bin/sleep 0.05
  n=$((n + 1))
done
alive
if [ -z "$busy" ]; then printf '%s %s\n' '@CLEAR@' "$nonce" >&"$1"; else printf '%s %s\n' '@LEFT@' "$nonce" >&"$1"; fi
""".replace("@START@", START)
    .replace("@END@", END)
    .replace("@LEFT@", LEFT)
    .replace("@CLEAR@", CLEAR)
)


def _said(marks: bytes, nonce: str, name: str) -> bool:
    """Whether the script inside said `name` with this run's nonce, as a whole line."""
    return any(
        line == f"{name} {nonce}" or line.startswith(f"{name} {nonce} ")
        for line in marks.decode(errors="replace").split("\n")
    )


def run_bash(line: str, env: dict[str, str], ignore_hup: bool = False) -> Result:
    """`bash -c line` in the sandbox with a made-up home, watched for a read or write of a protected FIFO and a change
    inside a protected folder. At most LIMIT seconds for the line, and GRACE more for what it left running. Raises
    `OracleError` when the line was not started, did not end, left the script waiting past the deadline, or the script
    never said that nothing was left: that run proved nothing. `ignore_hup` runs the line with the hang-up signal
    ignored, as a terminal-less caller that is not sent one would, for a program that ends its helper with it (`ssh`)."""
    with tempfile.TemporaryDirectory(prefix="ws-oracle-") as tmp:
        home = Path(tmp) / "pat"
        home.mkdir()
        _build(home)
        before = _snapshot(home)
        base = {"HOME": HOME, "WS_HOME": HOME + "/.ws", "PATH": "/usr/bin:/bin", "LC_ALL": "C", **env}
        marks_r, marks_w = os.pipe()
        os.set_blocking(marks_r, False)
        nonce = os.urandom(8).hex()
        in_r, in_w = (
            os.pipe()
        )  # the nonce waits in the pipe, so there is nothing to race and nothing in argv or the environment
        os.write(in_w, f"{nonce}\n".encode())
        os.close(in_w)
        fifos = _Fifos(home)
        out_file = Path(tmp) / "out"
        marks = b""
        killed = False
        try:
            argv = [*_bwrap_base(str(home)), "--clearenv"]
            for k, v in base.items():
                argv += ["--setenv", k, v]
            argv += [
                "/bin/bash",
                "-c",
                _INSIDE,
                "bash",
                str(marks_w),
                line,
                str(round(GRACE / 0.05)),
                "1" if ignore_hup else "0",
            ]
            with out_file.open("wb") as out:
                proc = subprocess.Popen(
                    argv,
                    stdin=in_r,
                    stdout=out,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    pass_fds=(marks_w,),
                )
                os.close(marks_w)
                os.close(in_r)
                started = time.monotonic()
                while proc.poll() is None:
                    with contextlib.suppress(BlockingIOError):
                        marks += os.read(marks_r, 4096)
                    fifos.look()
                    ended = _said(marks, nonce, END)
                    if time.monotonic() - started > LIMIT + (GRACE + 2 if ended else 0):
                        killed = True
                        with contextlib.suppress(OSError):
                            os.killpg(proc.pid, signal.SIGKILL)
                        break
                    time.sleep(0.002)
                proc.wait()
            with contextlib.suppress(BlockingIOError):
                marks += os.read(marks_r, 4096)
            fifos.look()
        finally:
            os.close(marks_r)
            fifos.close()
        text = out_file.read_text(errors="replace")
        if not _said(marks, nonce, START):
            raise OracleError(
                f"the sandbox did not start the line {line!r} (exit {proc.returncode}): {text.strip()[:300] or 'no output'}"
            )
        ended = _said(marks, nonce, END)
        after = _snapshot(home)
        changed = sorted(k for k in {*before, *after} if before.get(k) != after.get(k))
        result = Result(sorted(fifos.touched), changed, killed and not ended, text)
        if result.touched or result.hung:
            return result
        # Nothing was touched, so the run counts as clean only if the script itself got to the end and said so.
        if not ended:
            raise OracleError(f"the line {line!r} did not end (no end marker; exit {proc.returncode})")
        if killed:
            raise OracleError(
                f"the line {line!r} kept the sandbox past the deadline and was killed there; nothing was touched yet"
            )
        if _said(marks, nonce, LEFT):
            raise OracleError(
                f"the line {line!r} left something running after the {GRACE:g} s wait, and nothing was touched yet"
            )
        if not _said(marks, nonce, CLEAR):
            raise OracleError(
                f"the line {line!r} did not report that nothing was left running (exit {proc.returncode})"
            )
        return result


# Lines whose outcome depends on timing: a background job, a kill, a timer, a command another program runs. A program
# that is killed before it opens the file reads nothing in that run, so such a line is run again (up to RACE_TRIES
# times) until one run touches a protected path. A line that is not like this is run once.
RACY = re.compile(
    r"(?<![&>|])&(?![&>])|\bkill\b|\btimeout\b|ProxyCommand|LocalCommand|\bcoproc\b|\bnohup\b|\bsetsid\b|[<>]\("
)
RACE_TRIES = 30


def run_line(line: str, env: dict[str, str], tries: int | None = None, ignore_hup: bool = False) -> Result:
    """`run_bash`, repeated for a line whose outcome is a matter of timing (see `RACY`) until a run touches something."""
    tries = tries or (RACE_TRIES if RACY.search(line) else 1)
    hung = False
    for _ in range(tries):
        r = run_bash(line, env, ignore_hup)
        if r.touched:
            return r
        hung = hung or r.hung
    r.hung = hung  # a try that timed out is not made clean by one that didn't
    return r


def names_of(line: str) -> list[str]:
    return sorted({m for m in re.findall(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)", line) if m not in _PLAIN})


def environments(line: str) -> Iterator[dict[str, str]]:
    names = names_of(line)
    yield {n: f"{HOME}/{SSH}/id_rsa" for n in names}
    yield {n: f"{HOME}/{SSH}" for n in names}
    if names:
        for option in ("--file=", "-F", "-K", "-i", "-T", "-f", "file://"):  # a value that is an option, or an address
            yield {n: f"{option}{HOME}/{SSH}/id_rsa" for n in names}
    yield {  # what a repository's own settings can put in the shell's environment
        **dict.fromkeys(names, HOME),
        "TMPDIR": HOME,
        "CDPATH": HOME,
        "PATH": f"{HOME}/{SSH}:/usr/bin:/bin",
    }


def _run_all(lines: list[str], workers: int) -> tuple[dict[str, str], list[str]]:
    """Every line under each of its environments: what it touched, and the runs that proved nothing (a line that did not
    start, or that was still running at the limit and had touched nothing)."""
    jobs = [(line, env) for line in lines for env in environments(line)]

    def one(job):
        try:
            return run_line(*job)
        except OracleError as e:
            return e

    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(one, jobs))
    bad: dict[str, str] = {}
    errors: list[str] = []
    for (line, _), r in zip(jobs, results, strict=True):
        if isinstance(r, OracleError):
            errors.append(str(r))
        elif r.touched and line not in bad:
            bad[line] = repr(r)
        elif r.hung and not r.touched:
            errors.append(f"the line {line!r} was still running at the {LIMIT:g} s limit and had touched nothing")
    return bad, errors


def oracle(pol, lines: list[str], workers: int = 8) -> list[str]:
    """The lines the gate allows and a real bash used to touch a protected path (each with what it touched). A run that
    proved nothing raises `OracleError` naming the line: it is never counted as clean."""
    allowed = [line for line in lines if gate(pol, line) is None]
    if not allowed:
        return []
    bad, errors = _run_all(allowed, workers)
    if errors:
        raise OracleError(f"{len(errors)} runs proved nothing:\n" + "\n".join(sorted(set(errors))[:20]))
    return [f"{line!r}: {why}" for line, why in bad.items()]


def sections() -> dict[str, list[str]]:
    """The probe file's lines by section. A block that starts with `# ---` opens a section (`consumers`, `limits`); the
    lines before the first one are the plain probes."""
    out: dict[str, list[str]] = {"": []}
    here = ""
    for block in PROBES.read_text().split("\n====\n"):
        line = block.strip("\n")
        if line.startswith("# ---"):
            here = line.split(":")[0].removeprefix("# --- ")
            out.setdefault(here, [])
        elif line.strip():
            for key in ("UPTGT", "TGT", "SDIR", "GLOB1", "GLOB2", "BRACE1", "PHOME", "WSD"):
                line = line.replace(key, SUBS[key])
            out[here].append(line)
    return out


def probes() -> list[str]:
    return [line for lines in sections().values() for line in lines]


# --- the harness is itself tested ----------------------------------------------------------------------------------------


def test_the_harness_sees_a_read_a_write_and_nothing(pol):
    read = run_bash(f"cat {HOME}/{SSH}/id_rsa", {})
    assert read.opened == [f"{SSH}/id_rsa"] and read.touched and not read.hung
    assert run_bash(f"cd {HOME}/.aws && cat credentials", {}).touched
    assert run_bash(f"touch {HOME}/{SSH}/new", {}).changed
    assert run_bash(f"echo x >> {HOME}/.ws/policy.toml", {}).changed
    assert run_bash(f"ls -la {HOME}/{SSH}; echo hi; stat {HOME}/.aws/credentials >/dev/null", {}).touched is False


def test_the_harness_sets_a_named_variable_and_a_time_limit(pol):
    assert run_bash("cat $FOO", {"FOO": f"{HOME}/{SSH}/id_rsa"}).touched
    start = time.monotonic()
    slow = run_bash("sleep 30", {})
    assert slow.hung and not slow.touched and time.monotonic() - start < LIMIT + 5


def test_the_gate_denies_what_the_harness_sees_touched(pol):
    for line in (f"cat {HOME}/{SSH}/id_rsa", f"cd {HOME}/.aws && cat credentials", f"touch {HOME}/{SSH}/new"):
        assert gate(pol, line), line


def test_a_read_is_seen_whatever_its_timing(pol):
    """The protected file holds its token from the start, so a reader that is slow, short-lived or in the background
    still takes it, and a line that is a race is run again until a run takes it. (`cat F & kill $!` is not here: the
    kill ends `cat` before it opens anything, in every run.)"""
    f = f"{HOME}/{SSH}/id_rsa"
    for line in (
        f"cat {f} >&2 &",
        f"(cat {f} > /dev/null &)",
        f"timeout 0.001 cat {f}",
        f"(sleep 0.5; cat {f} >/dev/null) &",
        f"head -c 1 {f}",
        f"cat {f}; cat {f}",
        f"echo x > {f}",
    ):
        assert run_line(line, {}, tries=30).touched, line  # `timeout` can end `cat` before it opens: only now and then


def test_the_ssh_proxy_command_limit_is_seen_to_leak(pol):
    """The row stays a listed limit: the gate allows it and `ssh` runs the command. `ssh` ends its proxy command with the
    hang-up signal at about the time that command starts, so the `>&2` spelling of the listed row would open the file
    only now and then. The harness runs the row with that signal ignored, which makes the read certain, and the row's
    text stays as it is listed."""
    if not shutil.which("ssh"):
        pytest.skip("needs ssh")
    listed = f"ssh -o ProxyCommand='cat {HOME}/{SSH}/id_rsa >&2' x"
    assert listed in LIMITS and gate(pol, listed) is None
    assert run_line(listed, {}, tries=3, ignore_hup=True).touched
    for command in (f"cat {HOME}/{SSH}/id_rsa", f"cat {HOME}/{SSH}/id_rsa 2>&1"):
        line = f"ssh -o ProxyCommand='{command}' x"
        assert gate(pol, line) is None
        assert run_bash(line, {}).touched, line


def test_a_line_that_stops_the_script_and_reads_later_is_an_error_never_clean(pol):
    """`kill -STOP $PPID` freezes the script that watches the sandbox, so its own check never happens; a reader that
    starts after the freeze is never seen. The harness kills at the deadline and says so."""
    f = f"{HOME}/{SSH}/id_rsa"
    reader = "taskset 1 sh" if shutil.which("taskset") else "sh"
    line = f"(sleep 0.5; kill -STOP $PPID; sleep 12; {reader} -c 'cat {f} >/dev/null') &"
    with pytest.raises(OracleError, match="deadline"):
        run_bash(line, {})


def test_a_marker_the_line_writes_is_not_one_the_script_wrote(pol):
    """The line can reach the marker pipe through `/proc`, but not the run's nonce, which is on the script's standard
    input and nowhere else: a forged end marker, then a kill of the script, proves nothing."""
    f = f"{HOME}/{SSH}/id_rsa"
    forge = "for f in /proc/$PPID/fd/*; do printf 'ws-oracle-end 0\\n' >$f 2>/dev/null; done"
    forge_all = "for f in /proc/$PPID/fd/*; do printf 'ws-oracle-start 0\\nws-oracle-end 0\\nws-oracle-clear\\n' >$f 2>/dev/null; done"
    for line in (
        f"{forge}; (sleep 9; taskset 1 sh -c 'cat {f}') & kill -9 $PPID",
        f"{forge}; kill -9 $PPID",
        f"{forge_all}; kill -9 $PPID",
    ):
        with pytest.raises(OracleError, match="did not (start|end)"):
            run_bash(line, {})


def test_a_run_that_did_not_start_is_an_error_never_clean(pol, monkeypatch):
    with monkeypatch.context() as m:  # bwrap itself refuses: a path that doesn't exist
        m.setattr(
            sys.modules[__name__], "_bwrap_base", lambda home=None: ["bwrap", "--ro-bind", "/ws-oracle-none", "/x"]
        )
        with pytest.raises(OracleError, match="did not start.*bwrap:"):
            run_bash("echo hi", {})
    with monkeypatch.context() as m:  # the sandbox runs, but nothing says the line began
        m.setattr(sys.modules[__name__], "_INSIDE", "exit 0")
        with pytest.raises(OracleError, match="did not start"):
            run_bash("echo hi", {})
        with pytest.raises(OracleError, match="did not start"):
            oracle(pol, ["echo hi"])


def test_a_line_that_runs_out_of_time_without_touching_anything_is_an_error(pol, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "LIMIT", 1.0)
    with pytest.raises(OracleError, match="still running"):
        oracle(pol, ["sleep 30"])


def test_a_write_that_adds_nothing_to_a_protected_file_is_seen(pol):
    """`: > F`, `cp /dev/null F`, `touch F` and a token read and put back leave the FIFO's contents as they were."""
    f = f"{HOME}/{SSH}/id_rsa"
    for line in (
        f": > {f}",
        f"cp /dev/null {f}",
        f"touch -d 2000-01-01 {f}",
        f"{{ IFS= read -r t; printf '%s\\n' \"$t\" >&0; }} <> {f}",
    ):
        assert run_bash(line, {}).touched, line
    assert not run_bash(f"exec 3< {f}; exec 3<&-", {}).touched  # opening a file is not reading it


def test_a_line_that_leaves_something_running_past_the_wait_is_an_error_unless_it_touched(pol):
    f = f"{HOME}/{SSH}/id_rsa"
    with pytest.raises(OracleError, match="left something running"):
        run_bash(f"(sleep 4; cat {f} >/dev/null) &", {})
    assert run_bash(f"cat {f} >/dev/null; (sleep 4) &", {}).touched  # a leak is reported, not an error


def test_a_line_that_ends_its_own_shell_is_an_error_never_clean(pol):
    for line in ("kill -9 $PPID", "kill -9 -1"):
        with pytest.raises(OracleError, match="did not end"):
            run_bash(line, {})


def test_a_try_that_timed_out_is_not_made_clean_by_a_later_one(monkeypatch):
    tries = iter([Result([], [], True, ""), Result([], [], False, "")])
    monkeypatch.setattr(sys.modules[__name__], "run_bash", lambda line, env, ignore_hup=False: next(tries))
    assert run_line("sleep 9 &", {}, tries=2).hung


def test_without_bwrap_the_oracle_says_why(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    assert "bwrap" in _sandbox_problem()


def test_the_sandbox_is_kept_for_what_a_line_leaves_running(pol):
    assert run_bash(f"(sleep 1; cat {HOME}/{SSH}/id_rsa >/dev/null) &", {}).touched
    assert not run_bash("(sleep 1; echo done >/dev/null) &", {}).touched


# --- the cold reviews' probes ----------------------------------------------------------------------------------------------


# The lines a real bash does use to read a protected file, through text the gate never reads as shell: a program an
# interpreter runs (python, awk), an option whose value is a command (ssh ProxyCommand), or a script file the line
# writes and then runs. They are a limit of what a command
# gate can see, and the one list of them: a probe that is not here and gets through is a failure. The static gate is not
# a wall against interpreters; the sandbox (`ws gate launch`) is.
LIMITS = (
    f"python3 -c \"print(open('{HOME}/{SSH}/id_rsa').read())\"",
    f"awk 'BEGIN{{while((getline l < \"{HOME}/{SSH}/id_rsa\")>0) print l}}'",
    f"echo 'cat ~/{SSH}/id_rsa' > /tmp/x.sh; bash /tmp/x.sh",
    # an option whose value is a command the tool runs; released main allows it too
    f"ssh -o ProxyCommand='cat {HOME}/{SSH}/id_rsa >&2' x",
    *sections()["limits"],
)
_INTERPRETERS = r"(python3?|perl|node|ruby|php|lua|Rscript)"


def test_every_cold_review_probe_the_gate_allows_is_one_bash_does_not_use_to_touch_a_protected_path(pol):
    lines = probes()
    assert len(lines) > 300
    assert all(limit in lines for limit in LIMITS)
    # The limits are left out on purpose: each is a line the gate allows and bash does use, so this sweep (which must come
    # back empty) can't hold them. They are not run here expecting a leak either, because most need a tool that may not be
    # installed (an interpreter, awk, ssh) and the leak of the ones that run quoted text is the interpreter's, not the
    # gate's; `test_the_ssh_proxy_command_limit_is_seen_to_leak` runs the one that is a timing matter.
    assert oracle(pol, [line for line in lines if line not in LIMITS]) == []


def test_the_limits_are_programs_the_gate_does_not_read_as_shell():
    for line in LIMITS:
        assert re.match(
            rf"(python3 -c|awk |echo .* > /tmp/x\.sh; bash /tmp/x\.sh|ssh -o ProxyCommand=|{_INTERPRETERS} .*<<)",
            line,
        ), line


def test_the_limit_rows_are_interpreters_reading_standard_input_and_the_gate_allows_them(pol):
    rows = sections()["limits"]
    assert len(rows) == 11
    assert all(gate(pol, line) is None for line in rows)


def test_every_consumer_of_standard_input_that_runs_what_it_reads_is_denied(pol):
    """A here-document or here-string fed to a shell, `source`, `eval`, `ssh` or `xargs` is code, and a message is read
    exactly as any other text: no quoting of the body makes it data."""
    rows = sections()["consumers"]
    assert len(rows) > 90
    assert [line for line in rows if gate(pol, line) is None] == []


def _installed(name: str) -> bool:
    return bool(shutil.which(name, path="/usr/bin:/bin"))


@pytest.mark.parametrize(
    "name",
    [
        pytest.param(n, id=f"{n}-{'real-shell' if _installed(n) else 'gate-decision-only'}")
        for n in ("rbash", "mksh", "yash", "posh", "lksh", "oksh", "loksh", "pdksh", "hush", "ksh93")
    ],
)
def test_every_shell_that_runs_its_standard_input_is_denied_and_the_installed_ones_do_run_it(pol, name):
    """The gate denies a here-document or here-string given to each shell it knows. For a shell installed here the real
    shell is then run on the same text and seen to read the protected file; for one that isn't, only the gate's decision
    is tested (the test id says which)."""
    f = f"{HOME}/{SSH}/id_rsa"
    lines = [f"{name} <<'EOF'\ncat {f}\nEOF", f"{name} <<< 'cat {f}'", f"{name} <<EOF\ncat {f}\nEOF"]
    assert [line for line in lines if gate(pol, line) is None] == []
    if _installed(name):
        assert all(run_bash(line, {}).touched for line in lines)


# --- the policy's own tables, as lines -----------------------------------------------------------------------------------


def long_option_prefixes(longs: tuple[str, ...]) -> list[str]:
    """Every abbreviation getopt_long accepts of the options: `--x` of three characters or more, up to the whole name."""
    return sorted({o[:n] for o in longs for n in range(3, len(o) + 1)})


def table_lines() -> list[str]:
    p = f"{HOME}/{SSH}/id_rsa"
    out: list[str] = []
    for name in sorted(policy._NO_FILE_CMDS):
        out += [f"{name} 'a[$(cat {p} >&2)]=1'", f'{name} "$(cat {p})"', f"{name} 'a[$(cat $FOO >&2)]=1'"]
        out += [f"{name} {flag} {p}" for flag in ("-f", "-r", "-i", "-v", "-s", "-n", "-p", "-t", "-u", "-d", "-b")]
        for opt in long_option_prefixes(policy._NO_FILE_CMDS[name][1]):
            out += [f"{name} {opt} {p}", f"{name} {opt}={p}", f"{name} {opt} $FOO"]
    for name, (_, longs) in sorted(policy._FILE_FLAGS.items()):
        url = "http://example.invalid/"
        for opt in long_option_prefixes(longs):
            out += [f"{name} {opt} {p} {url}", f"{name} {opt}={p} {url}", f"{name} {opt} $FOO {url}"]
        for opt in longs:
            out += [f"{name} {opt} $FOO {url}", f"{name} {opt}=$FOO {url}"]
    return sorted(set(out))


def test_the_exempt_commands_and_the_file_options_the_tables_name_hold_against_bash(pol):
    assert oracle(pol, table_lines()) == []


def producer_lines() -> list[str]:
    """The commands whose output counts as a path in a known place, in every form that would let a path out."""
    ssh = f"{HOME}/{SSH}"
    out = [
        f'cd {HOME}; cat "$(pwd)/{SSH}/id_rsa"',
        f'cd {HOME}; cat "$(pwd -P)/{SSH}/id_rsa"',
        f'cat "$(dirname {ssh}/id_rsa)/id_rsa"',
        f'cat "$(dirname {ssh}/id_rsa/)/id_rsa"',
        f'cat "$(basename {ssh})/id_rsa"',
        f'cd {HOME}; cat "$(basename {HOME}/{SSH})/id_rsa"',
        f'cd {HOME}; cat "$(dirname {SSH}/x)/id_rsa"',
        f'cat "$(readlink -f {ssh})/id_rsa"',
        f'cd {HOME}; cat "$(readlink -f {SSH})/id_rsa"',
        f'cat "$(realpath {ssh})/id_rsa"',
        f'cd {HOME}; cat "$(realpath {SSH}/..)/{SSH}/id_rsa"',
        f'cat "$(realpath -m {ssh}/../.ssh)/id_rsa"',
        f'cat "$(mktemp -p {HOME} -d)/../{SSH}/id_rsa"',
        f'cat "$(mktemp -p {HOME} -d)/../{SSH}/id_rsa"; true',
        f'cat "$(mktemp --tmpdir={HOME} -d)/../{SSH}/id_rsa"',
        f'cd "$(mktemp -p {HOME} -d)" && cat ../{SSH}/id_rsa',
        f'cat "$(mktemp -d)/../{SSH}/id_rsa"',
        f'cat "$(mktemp)/../{SSH}/id_rsa"',
        f'cat "$(mktemp -t)/../{SSH}/id_rsa"',
        f'cd "$(mktemp -d)" && cat ../{SSH}/id_rsa',
        'cd "$(mktemp -d)" && cat notes.txt',
        f"cat $TMPDIR/{SSH}/id_rsa",
        f'cat "$TMPDIR/../{SSH[1:]}"',
        f'cd "$TMPDIR" && cat ../{SSH}/id_rsa',
        f'cd "$TMPDIR/.." && cat {SSH}/id_rsa',
        f'export TMPDIR={HOME}; cat "$(mktemp -d)/../{SSH}/id_rsa"',
        f'TMPDIR={HOME}; cd "$(mktemp -d)" && cat ../{SSH}/id_rsa',
        f"export TMPDIR={HOME}; cat $TMPDIR/{SSH}/id_rsa",
        f'cat "$(git rev-parse --show-toplevel)/{SSH}/id_rsa"',
        f'cd "$(git rev-parse --show-toplevel)" && cat ../{SSH}/id_rsa',
        f'git init -q {HOME}; cd {HOME}; cat "$(git rev-parse --show-toplevel)/{SSH}/id_rsa"',
        f'git config core.worktree {HOME}; cat "$(git rev-parse --show-toplevel)/{SSH}/id_rsa"',
        f'export GIT_WORK_TREE={HOME}; cat "$(git rev-parse --show-toplevel)/{SSH}/id_rsa"',
        f'cat "$(which {SSH[1:]})/id_rsa"',
        f'cat "$(command -v ls)/../../{HOME}/{SSH}/id_rsa"',
        'cat "$(command -v id_rsa)"',
        'cat "$(which id_rsa)"',
        f'PATH={ssh}:$PATH; cat "$(command -v id_rsa)"',
        "cat `command -v id_rsa`",
        'cat "$(type -p id_rsa)"',
        f"cd {HOME}; pushd /tmp; popd; cat {SSH}/id_rsa",
        f"cd {HOME}; pushd /tmp; pushd +1; cat {SSH}/id_rsa",
        f"cd {HOME}; cd /tmp; cat ~-/{SSH}/id_rsa",
        f"cd {HOME}; pushd /tmp >/dev/null; cat ~1/{SSH}/id_rsa",
        f"cd {HOME}; pushd /tmp >/dev/null; cat ~+1/{SSH}/id_rsa",
        f"cd {HOME}; pushd /tmp >/dev/null; dirs; cd ~1; cat {SSH}/id_rsa",
        f"cd {SSH} && cat id_rsa",
        f"cd {SSH} && cat id_rsa; cd /tmp",
        "cd /tmp && cd .aws && cat credentials",
        f"mkdir -p /tmp/sub; cd sub && cat {SSH}/id_rsa",
    ]
    return out


def test_the_known_producers_hold_against_bash(pol):
    assert oracle(pol, producer_lines()) == []


# --- a message is read like any other text --------------------------------------------------------------------------------


def message_lines() -> list[str]:
    """A commit message in every form: where the text is only kept, and where it runs. A repository is made first, so
    that the commit itself gets as far as reading what it is given."""
    p = f"{HOME}/{SSH}/id_rsa"
    pre = "git init -q /tmp/r && cd /tmp/r && git -c user.name=a -c user.email=a@b.c commit -q --allow-empty "
    lines = [
        f"-m 'x $(cat {p}) y'",
        f'-m "x $(cat {p}) y"',
        f'-m "x `cat {p}` y"',
        f"-m 'x `cat {p}` y'",
        '-m "$FOO"',
        "-m $FOO",
        "-m 'a $FOO b'",
        '-m"$FOO"',
        "--message='a $FOO'",
        "--message=$FOO",
        "-F $FOO",
        f"-F {p}",
        f"-F - <<'EOF'\nx $(cat {p}) `cat {p}` ${{FOO}}\nEOF",
        f'-F - <<"EOF"\nx $(cat {p})\nEOF',
        f"-F - <<\\EOF\nx $(cat {p})\nEOF",
        f"-F - <<EOF\nx $(cat {p})\nEOF",
        f"-F - <<'EOF'\nok\nEOF\ncat {p}",
        "-F - <<'EOF'\nok\nEOF\ncat $FOO",
        f"-m \"$(cat <<'EOF'\nx $(cat {p})\nEOF\n)\"",
        f'-m "$(cat <<EOF\nx $(cat {p})\nEOF\n)"',
        f"-m \"$(cat <<'EOF'\nx $(cat {p})\nEOF\n)\" -F {p}",
        "-m \"$(cat <<'EOF'\nfix $THING\nEOF\n)\"",
        "-m 'a' --allow-empty-message",
    ]
    return [pre + line for line in lines]


def test_a_message_the_gate_allows_is_one_bash_does_not_read_a_protected_file_for(pol):
    if not shutil.which("git"):
        pytest.skip("needs git")
    assert oracle(pol, message_lines()) == []


# --- every long option and every letter of the programs that take addresses -----------------------------------------------


def _options_of(command: list[str]) -> list[str]:
    try:
        out = subprocess.run(command, capture_output=True, text=True, timeout=20, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return sorted(set(re.findall(r"--[a-z0-9][a-z0-9-]+", out)))


ADDRESS_TOOLS = (("curl", ["curl", "--help", "all"]), ("wget", ["wget", "--help"]))


def address_lines(tool: str, helps: list[str]) -> list[str]:
    options = _options_of(helps)
    url = "https://example.invalid/"
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    lines = []
    for option in [*options, *(f"-{c}" for c in letters)]:
        lines += [f"{tool} {option} $FOO {url}", f"{tool} {option}=$FOO {url}", f"{tool} {option}$FOO {url}"]
    return lines + [f"{tool} $FOO", f"{tool} $FOO {url}", f"{tool} {url} $FOO", f"{tool} -s $FOO", f"{tool} -sS $FOO"]


@pytest.mark.parametrize("tool, helps", ADDRESS_TOOLS)
def test_no_option_of_an_address_program_opens_the_file_it_is_given_through_a_word_the_gate_allows(pol, tool, helps):
    if not shutil.which(tool):
        pytest.skip(f"needs {tool}")
    assert oracle(pol, address_lines(tool, helps)) == []
