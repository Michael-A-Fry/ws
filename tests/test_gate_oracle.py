"""The command reader against a real bash. Every exemption and every "known producer" in `ws.policy` is a claim about what
bash or a GNU tool does; this file tests the claims, not the reading of them.

For each probe line the gate is asked first (as the policy tests ask it). A line the gate allows is then run by a real
`bash -c` inside `bwrap` (no network, a made-up home that holds the protected paths, a time limit of 5 s):

- each protected FILE is a FIFO that the harness watches with a non-blocking `open(O_WRONLY | O_NONBLOCK)` loop. That open
  only succeeds while something has the FIFO open to read it, so success means the line opened a protected file; the
  harness then writes a canary token into the FIFO, and the token turns up in the line's output if it was read;
- each protected FOLDER (and the regular files ws protects) is compared before and after: a new or changed entry means
  the line wrote there.

The test FAILS when the gate allows a line that bash used to touch a protected path. The probes are the cold reviews'
cases (`fixtures/gate_oracle_probes.txt`) and lines made from the policy's own tables: one for each exempt command in the
forms that make a shell run quoted text, one for each unique prefix of the long options that open a file, and the
"known producer" lines. A line is run under three environments: the unresolved names set to a protected file, to a
protected folder, and a hostile one (`TMPDIR`, `CDPATH`, `PATH` and the names all pointing at the home folder), because a
repository's own settings can put any of them in the environment of the shell a session starts.
Linux only; skipped without `bwrap`."""

from __future__ import annotations

import contextlib
import os
import re
import shutil
import signal
import stat
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


def _linux_with_bwrap() -> bool:
    if not sys.platform.startswith("linux") or not shutil.which("bwrap"):
        return False
    try:
        done = subprocess.run([*_bwrap_base(), "/bin/sh", "-c", "exit 0"], capture_output=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


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


pytestmark = pytest.mark.skipif(not _linux_with_bwrap(), reason="needs Linux with bwrap (bubblewrap)")


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
                if stat.S_ISFIFO(st.st_mode):
                    continue  # the harness itself writes to these: a read of one is seen by the watcher
                seen[str(p.relative_to(home))] = (st.st_mode, st.st_size, st.st_mtime_ns, st.st_ino)
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


def run_bash(line: str, env: dict[str, str]) -> Result:
    """`bash -c line` in the sandbox with a made-up home, watched for a read of a protected FIFO and a change inside a
    protected folder. At most LIMIT seconds."""
    with tempfile.TemporaryDirectory(prefix="ws-oracle-") as tmp:
        home = Path(tmp) / "pat"
        home.mkdir()
        _build(home)
        before = _snapshot(home)
        argv = [*_bwrap_base(str(home)), "--clearenv"]
        base = {"HOME": HOME, "WS_HOME": HOME + "/.ws", "PATH": "/usr/bin:/bin", "LC_ALL": "C", **env}
        for k, v in base.items():
            argv += ["--setenv", k, v]
        out_file = Path(tmp) / "out"
        with out_file.open("wb") as out:
            proc = subprocess.Popen(
                [*argv, "bash", "-c", line],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            opened: set[str] = set()
            deadline = time.monotonic() + LIMIT
            fifos = [str(home / f) for f in FIFOS]
            while proc.poll() is None and time.monotonic() < deadline:
                for f in fifos:
                    try:
                        fd = os.open(f, os.O_WRONLY | os.O_NONBLOCK)
                    except OSError:
                        continue  # no reader: nothing has the FIFO open
                    opened.add(os.path.relpath(f, home))
                    try:
                        os.write(fd, TOKEN)
                    except OSError:
                        pass
                    finally:
                        os.close(fd)
                time.sleep(0.001)
            hung = proc.poll() is None
            if hung:
                with contextlib.suppress(OSError):
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
        after = _snapshot(home)
        changed = sorted(k for k in {*before, *after} if before.get(k) != after.get(k))
        return Result(sorted(opened), changed, hung, out_file.read_text(errors="replace"))


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


def oracle(pol, lines: list[str], workers: int = 8) -> list[str]:
    """The lines the gate allows and a real bash used to touch a protected path (each with what it touched)."""
    allowed = [line for line in lines if gate(pol, line) is None]
    if not allowed:
        return []
    jobs = [(line, env) for line in allowed for env in environments(line)]
    with ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda j: run_bash(*j), jobs))
    bad: dict[str, str] = {}
    for (line, _), r in zip(jobs, results, strict=True):
        if r.touched and line not in bad:
            bad[line] = repr(r)
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


@pytest.mark.parametrize("tool, helps", [("curl", ["curl", "--help", "all"]), ("wget", ["wget", "--help"])])
def test_no_option_of_an_address_program_opens_the_file_it_is_given_through_a_word_the_gate_allows(pol, tool, helps):
    if not shutil.which(tool):
        pytest.skip(f"needs {tool}")
    options = _options_of(helps)
    url = "https://example.invalid/"
    letters = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    lines = []
    for option in [*options, *(f"-{c}" for c in letters)]:
        lines += [f"{tool} {option} $FOO {url}", f"{tool} {option}=$FOO {url}", f"{tool} {option}$FOO {url}"]
    lines += [f"{tool} $FOO", f"{tool} $FOO {url}", f"{tool} {url} $FOO", f"{tool} -s $FOO", f"{tool} -sS $FOO"]
    assert oracle(pol, lines) == []
