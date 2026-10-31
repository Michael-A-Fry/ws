"""The sandbox wall (ws.sandbox, and the `sandbox` block the claude-code adapter writes). What really stops a bypass is
in tests/e2e/test_sandbox.py; here the command lines, the seccomp program (run through a small BPF interpreter), the
profile text and the fail-closed paths are checked without running a sandbox."""

from __future__ import annotations

import json
import os
import struct
import sys
from pathlib import Path

import pytest

from ws import approvals, cli, jobs, policy, sandbox
from ws.adapters import claude_code, opencode

POLICY = policy.loads(
    '[gate]\ndeny_read = ["~/.ssh/**", "~/.aws/credentials", "$HOME/.gnupg/**", "~/.s*/id_rsa", "/"]\n'
)


@pytest.fixture
def home(tmp_path):
    h = tmp_path / "home"
    (h / ".ssh").mkdir(parents=True)
    (h / ".aws").mkdir()
    (h / ".aws" / "credentials").write_text("x")
    return h


@pytest.fixture
def wshome(tmp_path):
    w = tmp_path / "wshome"
    w.mkdir()
    return w


def which(*present):
    return lambda name: f"/usr/bin/{name}" if name in present else None


# --- what gets hidden -------------------------------------------------------------------------------------------


def test_read_rules_become_paths_a_sandbox_can_hide():
    # a trailing ** goes; a wildcard in the middle, the root and a bare ~ are left to the matcher
    assert policy.sandbox_read_paths(POLICY) == ["~/.ssh", "~/.aws/credentials", "~/.gnupg"]
    assert (
        policy.sandbox_read_paths(policy.loads('[gate]\ndeny_read = ["~", "~/**", "**/.ssh/**", "relative"]\n')) == []
    )


def test_only_paths_that_exist_are_hidden_and_home_is_the_runners(home):
    got = sandbox.hidden_paths(POLICY, {"HOME": str(home)})
    assert got == [str(home.resolve() / ".ssh"), str(home.resolve() / ".aws" / "credentials")]  # no .gnupg here


def test_socket_folders_are_hidden_when_they_exist(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    assert str(run.resolve()) in sandbox.socket_dirs({"XDG_RUNTIME_DIR": str(run)})
    assert sandbox.socket_dirs({"XDG_RUNTIME_DIR": str(tmp_path / "nope")}) == [
        p for p in [f"/tmp/tmux-{os.getuid()}"] if os.path.isdir(p)
    ]


# --- the seccomp program ------------------------------------------------------------------------------------------

ALLOW, EPERM = 0x7FFF0000, 0x00050001
X86, ARM = 0xC000003E, 0xC00000B7


def run_bpf(program: bytes, arch: int, nr: int, args: tuple = (0, 0, 0, 0, 0, 0)) -> int:
    """seccomp_data is nr (u32), arch (u32), ip (u64), args (6 x u64): run the filter on one call."""
    data = struct.pack("<IIQ6Q", nr, arch, 0, *args)
    ins = [struct.unpack_from("<HBBI", program, i) for i in range(0, len(program), 8)]
    acc, pc = 0, 0
    while True:
        code, jt, jf, k = ins[pc]
        pc += 1
        if code == 0x20:  # ld word at offset k
            (acc,) = struct.unpack_from("<I", data, k)
        elif code == 0x15:  # jeq
            pc += jt if acc == k else jf
        elif code == 0x35:  # jge
            pc += jt if acc >= k else jf
        elif code == 0x06:  # ret
            return k
        else:
            raise AssertionError(f"unexpected opcode {code:#x}")


@pytest.mark.parametrize(
    "machine, arch, nr_socket, nr_socketpair, nr_ioctl",
    [("x86_64", X86, 41, 53, 16), ("aarch64", ARM, 198, 199, 29)],
)
def test_the_filter_refuses_unix_sockets_io_uring_and_terminal_injection(
    machine, arch, nr_socket, nr_socketpair, nr_ioctl
):
    prog = sandbox.seccomp_program(machine)
    assert len(prog) % 8 == 0
    f = lambda nr, *a: run_bpf(prog, arch, nr, (*a, 0, 0, 0, 0, 0, 0)[:6])
    assert f(nr_socket, 1, 1) == EPERM  # AF_UNIX stream
    assert f(nr_socket, 1 | (1 << 32), 1) == EPERM  # only the low word is the domain: junk above it does not hide it
    assert f(nr_socket, 2, 1) == ALLOW  # AF_INET
    assert f(nr_socket, 10, 2) == ALLOW  # AF_INET6
    assert f(nr_socketpair, 1, 1) == ALLOW  # Node and Bun spawn children through socketpair; it connects to itself
    assert f(425) == EPERM  # io_uring_setup
    assert f(nr_ioctl, 0, 0x5412) == EPERM  # TIOCSTI
    assert f(nr_ioctl, 0, 0x541C) == EPERM  # TIOCLINUX
    assert f(nr_ioctl, 0, 0x5401) == ALLOW  # TCGETS
    assert f(0) == ALLOW  # an ordinary call
    assert run_bpf(prog, arch ^ 1, nr_socket, (2,) * 6) == EPERM  # another architecture
    assert f(0x40000000 | nr_socket, 2) == EPERM  # x32 numbers


def test_no_filter_for_an_unknown_cpu():
    with pytest.raises(sandbox.SandboxError, match="riscv64"):
        sandbox.seccomp_program("riscv64")


# --- the Linux command line ---------------------------------------------------------------------------------------


def wrapped(home, wshome, **kw) -> sandbox.Wrapped:
    kw.setdefault("which", which("bwrap"))
    kw.setdefault("probe", lambda: True)
    kw.setdefault("system", "linux")
    kw.setdefault("machine", "x86_64")
    return sandbox.wrap(
        ["opencode", "run"], POLICY, wshome, [str(wshome / "policy.toml")], env={"HOME": str(home)}, **kw
    )


def test_bwrap_binds_ws_home_read_only_then_reopens_the_two_holes_then_hides_reads(home, wshome):
    (wshome / "policy.toml").write_text("")
    w = wrapped(home, wshome)
    argv = w.argv
    assert argv[:4] == ["bwrap", "--bind", "/", "/"] and argv[-3:] == ["--", "opencode", "run"]
    text = " ".join(argv)
    real = wshome.resolve()
    ro, rw = f"--ro-bind {real} {real}", f"--bind {real}/approvals/pending {real}/approvals/pending"
    assert ro in text and rw in text and f"--bind {real}/sessions {real}/sessions" in text
    assert f"--ro-bind {real}/policy.toml {real}/policy.toml" in text
    assert text.index(ro) < text.index(rw)  # the hole is cut after the read-only mount, or it would be covered
    assert f"--tmpfs {home.resolve()}/.ssh" in text and f"--ro-bind /dev/null {home.resolve()}/.aws/credentials" in text
    assert "--dev-bind /dev /dev" in text and "--die-with-parent" in argv
    assert "--unshare-pid" in argv and "--unshare-ipc" in argv and "--proc /proc" in text
    assert "--new-session" in argv  # no terminal: the escape through the controlling terminal is shut
    assert "--unshare-net" not in argv  # the runner needs the model's API
    assert "granted" not in text  # the folder that holds grants is never reopened
    # the filter goes in by an inherited descriptor, and that descriptor is kept open for the launch
    fd = int(argv[argv.index("--seccomp") + 1])
    assert w.pass_fds == (fd,) and os.get_inheritable(fd)
    assert os.read(fd, 4096) == sandbox.seccomp_program("x86_64")
    os.close(fd)


def test_every_renamable_ancestor_of_a_protected_path_becomes_a_mount_point_first(tmp_path):
    deep = tmp_path / "cfg" / "ws"
    deep.mkdir(parents=True)
    plan = sandbox.Plan(deep, ())
    ancestors = plan.ancestors()
    assert str(tmp_path.resolve() / "cfg") in ancestors and "/" not in ancestors
    assert ancestors == sorted(ancestors, key=len)  # outermost first
    argv = sandbox.bwrap_argv(["x"], plan, False, 9)
    first_ro = argv.index("--ro-bind")
    assert argv.index(f"{tmp_path.resolve() / 'cfg'}") < first_ro  # bound onto itself before the read-only mount
    # a folder whose parent the user cannot write cannot be renamed, so it is left alone
    assert "/usr" not in ancestors and "/tmp" not in ancestors


def test_an_interactive_runner_keeps_its_terminal(home, wshome):
    assert "--new-session" not in wrapped(home, wshome, tty=True).argv


def test_the_holes_exist_before_the_mount(home, wshome):
    wrapped(home, wshome)
    assert (wshome / "approvals" / "pending").is_dir() and (wshome / "sessions").is_dir()


# --- fail closed --------------------------------------------------------------------------------------------------


def test_no_bubblewrap_refuses_with_the_install_line(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="apt install bubblewrap"):
        wrapped(home, wshome, which=which())


def test_a_bubblewrap_that_cannot_start_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="cannot start"):
        wrapped(home, wshome, probe=lambda: False)


def test_a_cpu_without_a_filter_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="no seccomp filter for riscv64"):
        wrapped(home, wshome, machine="riscv64")


def test_another_system_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="no sandbox for windows"):
        wrapped(home, wshome, system="windows")


def test_macos_without_sandbox_exec_refuses(home, wshome):
    with pytest.raises(sandbox.SandboxError, match="sandbox-exec"):
        wrapped(home, wshome, system="darwin", which=which())


def test_macos_profile_denies_writes_to_ws_and_reads_of_the_hidden_paths(home, wshome):
    w = wrapped(home, wshome, system="darwin", which=which("sandbox-exec"))
    argv = w.argv
    assert argv[:2] == ["sandbox-exec", "-p"] and argv[3:] == ["opencode", "run"] and w.pass_fds == ()
    prof = argv[2]
    real = str(wshome.resolve())
    assert "(allow default)" in prof and f'(deny file-write* (subpath "{real}"))' in prof
    assert f'(deny file-read* (subpath "{home.resolve()}/.ssh"))' in prof
    assert f'(deny file-read* (literal "{home.resolve()}/.aws/credentials"))' in prof
    assert "(deny network-outbound (remote unix-socket))" in prof
    assert prof.index(f'(deny file-write* (subpath "{real}"))') < prof.index(
        f'(allow file-write* (subpath "{real}/approvals/pending"))'
    )


def test_the_profile_quotes_awkward_paths(tmp_path):
    odd = tmp_path / 'a "b" \\ c'
    prof = sandbox.sandbox_exec_profile(sandbox.Plan(odd))
    assert '\\"b\\"' in prof and "\\\\" in prof


# --- ws gate launch -----------------------------------------------------------------------------------------------


@pytest.fixture
def env(tmp_path, monkeypatch, wshome):
    monkeypatch.setenv("WS_HOME", str(wshome))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("OPENCODE_CONFIG_DIR", raising=False)
    started = []
    monkeypatch.setattr(sandbox.os, "execvpe", lambda exe, argv, e: started.append((argv, e)))
    monkeypatch.setattr(sandbox, "run_with_broker", lambda cmd, e, fds, root: started.append((cmd, e, root)) or 0)
    return started


def test_launch_wraps_the_runner_marks_the_session_and_keeps_the_broker_beside_it(env, monkeypatch, wshome):
    monkeypatch.setattr(sandbox, "wrap", lambda argv, *a, **k: sandbox.Wrapped(["wall", *argv]))
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode", "run"]) == 0
    argv, e, root = env[0]
    assert argv == ["wall", "opencode", "run"] and e["WS_GATED_SESSION"] == "1"
    assert root == wshome.absolute() / "approvals"


def test_launch_fails_closed_when_the_wall_cannot_be_built(env, monkeypatch, capsys):
    def boom(*a, **k):
        raise sandbox.SandboxError("bubblewrap (bwrap) is not installed; install bubblewrap")

    monkeypatch.setattr(sandbox, "wrap", boom)
    assert cli.main(["gate", "launch", "--runner", "opencode", "--", "opencode"]) == 1
    err = capsys.readouterr().err
    assert "refusing to start" in err and "install bubblewrap" in err and "--no-sandbox" in err
    assert env == []  # the runner never started


def test_no_sandbox_starts_bare_and_says_what_it_gives_up(env, capsys):
    assert cli.main(["gate", "launch", "--runner", "opencode", "--no-sandbox", "--", "opencode"]) == 0
    err = capsys.readouterr().err
    assert "NO wall" in err and "python3 -c" in err and "policy" in err and "tmux" in err
    assert env[0][0] == ["opencode"] and env[0][1]["WS_GATED_SESSION"] == "1"


def test_launch_needs_a_command_and_does_not_wrap_claude_code(env, capsys):
    assert cli.main(["gate", "launch", "--runner", "opencode"]) == 1
    assert cli.main(["gate", "launch", "--runner", "claude-code", "--", "claude"]) == 1
    assert "own sandbox" in capsys.readouterr().err and env == []


def test_the_whole_generated_folder_and_both_control_folders_are_protected(env, monkeypatch, tmp_path, wshome):
    out = wshome / "gate" / "opencode"
    out.mkdir(parents=True)
    other = tmp_path / "elsewhere" / "policy.toml"
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", str(out))
    seen = {}
    monkeypatch.setattr(
        sandbox,
        "wrap",
        lambda argv, pol, home, protect, **k: seen.update(protect=protect, home=home) or sandbox.Wrapped(argv),
    )
    other.parent.mkdir()
    other.write_text("schema = 1\n")
    assert (
        cli.main(["gate", "launch", "--runner", "opencode", "--policy", str(other), "--protect", "x", "--", "x"]) == 0
    )
    prot = seen["protect"]
    assert str(out) in prot  # the folder as a whole: nothing in it is writable, so nothing can be planted
    assert str(other) in prot and str(other.parent / "approvals") in prot  # wherever --policy put it
    assert str(wshome / "approvals") in prot and str(wshome / "policy.toml") in prot  # $WS_HOME, whatever --policy says
    assert seen["home"] == wshome.absolute()


# --- the broker, for real -----------------------------------------------------------------------------------------

CLAIMER = (
    "import os, socket, sys\n"
    "s = socket.socket(fileno=int(os.environ['WS_APPROVAL_FD']))\n"
    "f = s.makefile('rw', newline='\\n')\n"
    "f.write('claim ' + sys.argv[1] + '\\n'); f.flush()\n"
    "sys.exit(0 if f.readline().strip() == 'ok' else 3)\n"
)


def test_a_runner_in_the_wall_can_claim_a_grant_once_through_the_launcher(tmp_path):
    root = tmp_path / "approvals"
    bound = approvals.Bound("opencode", "command", "terraform apply", "/w", "s1")
    approvals.require(root, bound)
    approvals.grant(root, bound.digest()[:12])
    cmd = [sys.executable, "-c", CLAIMER, bound.digest()]
    assert sandbox.run_with_broker(cmd, dict(os.environ), (), root) == 0
    assert sandbox.run_with_broker(cmd, dict(os.environ), (), root) == 3  # used up
    assert not (root / "granted" / f"{bound.digest()}.json").exists()


def test_a_forged_claim_for_nothing_is_refused(tmp_path):
    root = tmp_path / "approvals"
    assert sandbox.run_with_broker([sys.executable, "-c", CLAIMER, "0" * 64], dict(os.environ), (), root) == 3
    assert sandbox.run_with_broker([sys.executable, "-c", CLAIMER, "not-an-id"], dict(os.environ), (), root) == 3


# --- the Claude Code sandbox block --------------------------------------------------------------------------------


def claude_settings(tmp_path, toml: str = "") -> dict:
    pol = policy.loads(toml)
    out = tmp_path / "wshome" / "gate" / "claude-code"
    files = claude_code.generate(pol, ["ws"], out, tmp_path / "wshome" / "policy.toml")
    return json.loads(files["settings.json"])


def test_claude_code_settings_turn_the_sandbox_on_and_fail_closed(tmp_path):
    s = claude_settings(tmp_path, '[gate]\ndeny_read = ["~/.ssh/**", "~/.aws/credentials", "~/x*/y"]\n')
    sb = s["sandbox"]
    assert sb["enabled"] is True and sb["failIfUnavailable"] is True and sb["allowUnsandboxedCommands"] is False
    assert sb["autoAllowBashIfSandboxed"] is False  # the person still sees prompts
    deny = sb["filesystem"]["denyWrite"]
    wshome = str(tmp_path / "wshome")
    assert deny[0] == wshome and str(tmp_path / "wshome" / "policy.toml") in deny
    assert str(tmp_path / "wshome" / "gate" / "claude-code") in deny
    assert all(str(tmp_path / "wshome" / n) in deny for n in policy.CONTROL_NAMES)
    assert sb["filesystem"]["denyRead"] == ["~/.ssh", "~/.aws/credentials"]
    assert "network" not in sb  # the policy names no hosts: it says nothing about the network
    assert s["env"] == {"WS_GATED_SESSION": "1"}


def test_claude_code_network_follows_allow_hosts(tmp_path):
    s = claude_settings(tmp_path, '[gate]\nallow_hosts = ["api.anthropic.com", "*.npmjs.org"]\n')
    assert s["sandbox"]["network"] == {"allowedDomains": ["api.anthropic.com", "*.npmjs.org"]}


def test_only_documented_sandbox_keys_are_used(tmp_path):
    sb = claude_settings(tmp_path, '[gate]\nallow_hosts = ["a.b"]\n')["sandbox"]
    documented = {
        "enabled",
        "autoAllowBashIfSandboxed",
        "failIfUnavailable",
        "allowUnsandboxedCommands",
        "filesystem",
        "network",
    }
    assert set(sb) <= documented
    assert set(sb["filesystem"]) <= {"allowWrite", "denyWrite", "denyRead", "allowRead"}


# --- project settings must not widen it ----------------------------------------------------------------------------


def project(tmp_path, name: str, conf) -> Path:
    d = tmp_path / "proj"
    (d / ".claude").mkdir(parents=True, exist_ok=True)
    (d / ".claude" / name).write_text(conf if isinstance(conf, str) else json.dumps(conf))
    return d


@pytest.mark.parametrize(
    "conf",
    [
        {"sandbox": {"excludedCommands": ["python3 *"]}},
        {"sandbox": {"enabled": False}},
        {"sandbox": {"allowUnsandboxedCommands": True}},
        {"sandbox": {"filesystem": {"allowRead": ["~/.ssh"]}}},
        {"sandbox": {"filesystem": {"allowWrite": ["/"]}}},
        {"sandbox": {"filesystem": {"disabled": True}}},
    ],
)
@pytest.mark.parametrize("name", ["settings.json", "settings.local.json"])
def test_the_hook_denies_a_folder_whose_project_settings_open_the_sandbox(tmp_path, monkeypatch, conf, name):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = project(tmp_path, name, conf)
    (d / "sub").mkdir()
    for cwd in (d, d / "sub"):  # from a folder below the project too
        why = claude_code.preflight({"cwd": str(cwd)})
        assert why and name in why and "sandbox." in why


def test_harmless_project_settings_and_the_users_own_settings_are_fine(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = project(tmp_path, "settings.json", {"sandbox": {"network": {"allowedDomains": ["a.b"]}}, "model": "x"})
    assert claude_code.preflight({"cwd": str(d)}) is None
    assert claude_code.preflight({"cwd": str(tmp_path / "nowhere")}) is None
    assert claude_code.preflight({}) is None and claude_code.preflight({"cwd": "relative"}) is None
    project(tmp_path, "settings.local.json", "{not json")  # Claude Code ignores a file that does not parse
    assert claude_code.preflight({"cwd": str(d)}) is None
    user = tmp_path / "home"
    (user / ".claude").mkdir(parents=True)
    (user / ".claude" / "settings.json").write_text(json.dumps({"sandbox": {"excludedCommands": ["docker *"]}}))
    assert claude_code.preflight({"cwd": str(user)}) is None  # user scope is the person's own


def test_the_gate_check_applies_the_preflight(tmp_path, monkeypatch):
    from ws import gate

    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    d = project(tmp_path, "settings.json", {"sandbox": {"excludedCommands": ["x"]}})
    raw = json.dumps({"tool_name": "Read", "tool_input": {"file_path": "/etc/hostname"}, "cwd": str(d)})
    assert "excludedCommands" in gate.check("claude-code", raw, policy.loads(""))


def test_gated_headless_claude_runs_ignore_project_settings(tmp_path):
    job = jobs.loads("j", 'runner = "claude-code"\nmodel = "m"\nprompt = "-hello"\n', Path("."))
    argv, _ = claude_code.build_command(job, tmp_path, None)
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert argv[argv.index("--settings") + 1] == str(tmp_path / "gate" / "settings.json")


# --- the OpenCode shim and folder ----------------------------------------------------------------------------------


def test_the_opencode_shim_sends_the_session_and_resolved_workdir_and_can_claim(tmp_path):
    files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "policy.toml")
    shim = files[opencode.PLUGIN]
    assert "session_id: input.sessionID" in shim
    assert 'resolve(directory ?? process.cwd(), output.args?.workdir ?? ".")' in shim  # bash runs in args.workdir
    assert "r.status === 4" in shim and "WS_APPROVAL_FD" in shim and "claim ${id}" in shim
    assert "ws gate launch --runner opencode -- opencode" in opencode.launch_hint(str(tmp_path))


def test_the_generated_opencode_folder_needs_nothing_written_at_start(tmp_path):
    files = opencode.generate(policy.loads(""), ["ws"], tmp_path / "out", tmp_path / "policy.toml")
    assert set(files) == {opencode.CONFIG, opencode.PLUGIN, opencode.IGNORE}
    assert files[opencode.IGNORE].splitlines()[-1] == opencode.IGNORE  # what OpenCode itself writes
