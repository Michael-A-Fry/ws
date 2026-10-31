"""The wall against the escapes the first cold review found (phase 4, fix round 1). Each has a control run with the wall
off (`--no-sandbox`), so a pass is not vacuous.

  host sockets    tmux, screen, systemd-run --user and a plain Unix socket reach a daemon outside the wall
  pid namespace   a host process is visible (and so is `/proc/PID/root`, a path around every bind)
  terminal        TIOCSTI types into the person's shell
  parent rename   `mv cfg cfg2; mkdir -p cfg/ws; ...` plants a policy where the hook's fixed path points
  config folder   a planted plugin, tool, agent or opencode.jsonc in the generated OpenCode folder
  approvals       approve once, use once, on OpenCode inside the wall (and not for another folder)
  project config  a repo's .claude/settings.json that opens the Claude Code sandbox

The first five run `ws gate launch -- bash -c ...` directly: the launcher is what is tested, no model is involved.

Linux only. A missing tool skips its test, unless WS_E2E_REQUIRE=1 (CI), when it fails. systemd-run needs a user
manager, which CI runners do not have, so that one skips in CI too.
"""

from __future__ import annotations

import json
import os
import pty
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from e2e import test_gate_runners as gr
from e2e import test_sandbox as sb
from e2e.stubs import AnthropicStub, Call, OpenAIStub
from ws import approvals, policy

ws_home = gr.ws_home
work = gr.work
secrets = gr.secrets
need_wall = sb.need_wall
wshome = sb.wshome


def need(tool: str) -> str:
    path = shutil.which(tool)
    if path:
        return path
    if gr.REQUIRE:
        pytest.fail(f"{tool} is not installed and WS_E2E_REQUIRE=1")
    pytest.skip(f"{tool} is not installed")


def launch(tmp_path, script: str, *flags: str, env: dict | None = None, cwd=None, stdin=subprocess.DEVNULL):
    """`ws gate launch -- bash -c script` as a person's terminal would start it, with HOME and WS_HOME in tmp."""
    e = gr.base_env(tmp_path)
    e.update(env or {})
    cmd = [*sb.WALL, *flags, "--", "bash", "-c", script]
    project = (
        cwd or tmp_path / "work"
    )  # the wall leaves the folder it starts in writable, so not the home folder or above
    project.mkdir(exist_ok=True)
    return subprocess.run(
        cmd, env=e, cwd=project, capture_output=True, text=True, timeout=120, check=False, stdin=stdin
    )


def wait_for(path, seconds=8.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if path.exists():
            return True
        time.sleep(0.1)
    return path.exists()


# --- host sockets -------------------------------------------------------------------------------------------------


@pytest.fixture
def unix_listener(tmp_path):
    path = tmp_path / "host.sock"
    srv = socket.socket(socket.AF_UNIX)
    srv.bind(str(path))
    srv.listen(5)
    seen = []

    def accept():
        while True:
            try:
                c, _ = srv.accept()
            except OSError:
                return
            seen.append(c.recv(16))
            c.close()

    threading.Thread(target=accept, daemon=True).start()
    yield path, seen
    srv.close()


CONNECT = (
    "import socket,sys; s=socket.socket(socket.AF_UNIX); s.connect(sys.argv[1]); s.send(b'hello'); print('connected')"
)


def test_a_plain_unix_socket_to_the_host_is_refused(tmp_path, wshome, unix_listener):
    path, seen = unix_listener
    walled = launch(tmp_path, f'python3 -c "{CONNECT}" {path}')
    assert "connected" not in walled.stdout and "not permitted" in walled.stderr.lower(), walled.stderr
    assert not seen
    bare = launch(tmp_path, f'python3 -c "{CONNECT}" {path}', "--no-sandbox")
    assert "connected" in bare.stdout, bare.stderr
    assert wait_for_list(seen)


def wait_for_list(seen, seconds=5.0) -> bool:
    end = time.time() + seconds
    while time.time() < end and not seen:
        time.sleep(0.1)
    return bool(seen)


@pytest.fixture
def tmux_server(tmp_path):
    exe = need("tmux")
    name = "ws" + os.urandom(4).hex()
    subprocess.run([exe, "-L", name, "new-session", "-d", "sleep 300"], check=True, capture_output=True)
    yield exe, name
    subprocess.run([exe, "-L", name, "kill-server"], check=False, capture_output=True)


def test_tmux_cannot_run_a_command_outside_the_wall(tmp_path, wshome, tmux_server):
    exe, name = tmux_server
    policy_file = wshome / "policy.toml"
    before = policy_file.read_text()
    script = f"{exe} -L {name} run-shell 'echo pwned >> {policy_file}'"
    launch(tmp_path, script)
    time.sleep(1.5)
    assert policy_file.read_text() == before, "tmux ran a command outside the wall and rewrote the policy"
    launch(tmp_path, script, "--no-sandbox")
    end = time.time() + 8
    while time.time() < end and policy_file.read_text() == before:
        time.sleep(0.2)
    assert policy_file.read_text() != before, "control: with no wall the same tmux command does rewrite the policy"


def test_tmux_cannot_open_a_window_for_self_approval(tmp_path, wshome, tmux_server):
    """`tmux new-window 'ws approve ID --yes'` is a real terminal outside the wall: the approval route."""
    exe, name = tmux_server
    mark = tmp_path / "window-ran"
    script = f"{exe} -L {name} new-window 'touch {mark}'"
    launch(tmp_path, script)
    assert not wait_for(mark, 2.0), "a tmux window opened outside the wall"
    launch(tmp_path, script, "--no-sandbox")
    assert wait_for(mark), "control: with no wall the window opens"


def test_screen_cannot_type_into_a_host_session(tmp_path, wshome):
    exe = need("screen")
    name = "ws" + os.urandom(4).hex()
    mark = tmp_path / "screen-ran"
    subprocess.run([exe, "-dmS", name, "bash"], check=True, capture_output=True)
    try:
        time.sleep(1)
        script = f"{exe} -S {name} -X stuff \"touch {mark}$(printf '\\r')\""
        launch(tmp_path, script)
        assert not wait_for(mark, 2.0), "screen ran a command outside the wall"
        launch(tmp_path, script, "--no-sandbox")
        assert wait_for(mark), "control: with no wall the command runs in the host screen"
    finally:
        subprocess.run([exe, "-S", name, "-X", "quit"], check=False, capture_output=True)


def test_systemd_run_user_cannot_start_a_host_unit(tmp_path, wshome):
    exe = need("systemd-run")
    if subprocess.run(
        ["systemctl", "--user", "is-system-running"], check=False, capture_output=True, text=True
    ).stdout.strip() not in (
        "running",
        "degraded",
    ):
        pytest.skip("there is no systemd user manager here")
    mark = tmp_path / "unit-ran"
    script = f"{exe} --user --wait --collect -- touch {mark}"
    env = {"XDG_RUNTIME_DIR": f"/run/user/{os.getuid()}"}  # how systemd-run finds the person's manager
    launch(tmp_path, script, env=env)
    assert not mark.exists(), "systemd-run started a unit outside the wall"
    launch(tmp_path, script, "--no-sandbox", env=env)
    assert wait_for(mark), "control: with no wall the unit runs"


# --- pid namespace and terminal ----------------------------------------------------------------------------------


def test_host_processes_are_not_visible_in_the_wall(tmp_path, wshome):
    me = os.getpid()
    script = f"kill -0 {me} 2>&1; ls /proc/{me}/root 2>&1 | head -1"
    walled = launch(tmp_path, script)
    assert "No such process" in walled.stdout or "no such process" in walled.stdout.lower(), walled.stdout
    assert "/proc/" in walled.stdout and "No such file" in walled.stdout  # /proc/PID/root is not there either
    bare = launch(tmp_path, script, "--no-sandbox")
    assert "No such process" not in bare.stdout and "No such file" not in bare.stdout, bare.stdout


IOCTL = (
    "import fcntl, termios\n"
    "try:\n"
    "    fcntl.ioctl(0, termios.TIOCSTI, b'x'); print('typed')\n"
    "except OSError as e:\n"
    "    print('refused', e.errno)\n"
)


def test_the_wall_refuses_tiocsti_on_the_persons_terminal(tmp_path, wshome):
    master, slave = pty.openpty()
    try:
        script = f'python3 -c "{IOCTL}"'
        walled = launch(tmp_path, script, stdin=slave)
        assert "refused 1" in walled.stdout, walled.stdout + walled.stderr  # EPERM from the filter
        bare = launch(tmp_path, script, "--no-sandbox", stdin=slave)
        assert (
            "typed" in bare.stdout or "refused" in bare.stdout
        )  # whatever the kernel allows; the wall's answer is fixed
    finally:
        os.close(master)
        os.close(slave)


# --- parent rename -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("inside_project", [True, False])
def test_a_protected_folders_parent_cannot_be_renamed_and_replaced(tmp_path, inside_project):
    """Inside the project folder the parent is made a mount point (EBUSY); outside it everything is read-only anyway."""
    base = tmp_path / "work" if inside_project else tmp_path / "away"
    cfg = base / "cfg"
    home = cfg / "ws"
    home.mkdir(parents=True)
    (home / "policy.toml").write_text(policy.default_toml())
    env = {"WS_HOME": str(home)}
    script = f"mv {cfg} {base / 'cfg2'}; mkdir -p {home}; echo pwned > {home}/policy.toml"
    walled = launch(tmp_path, script, env=env)
    assert (home / "policy.toml").read_text() == policy.default_toml(), walled.stderr
    assert not (base / "cfg2").exists(), walled.stderr
    assert ("busy" if inside_project else "read-only") in walled.stderr.lower(), walled.stderr
    launch(tmp_path, script, "--no-sandbox", env=env)
    assert (base / "cfg2").exists(), "control: with no wall the rename works"


def test_a_protected_path_behind_a_replaceable_symlink_is_refused(tmp_path):
    """`~/.ws -> /data/ws`: the bind lands on the target, the link stays writable, and deleting it plants a policy where
    the hook's fixed path points. The launch refuses, and names the real path."""
    real = tmp_path / "data" / "ws"
    real.mkdir(parents=True)
    (real / "policy.toml").write_text(policy.default_toml())
    link = tmp_path / "work" / "ws-link"
    (tmp_path / "work").mkdir()
    link.symlink_to(real)
    walled = launch(tmp_path, "echo started", env={"WS_HOME": str(link)})
    assert "started" not in walled.stdout and "symbolic link" in walled.stderr and str(real) in walled.stderr
    bare = launch(
        tmp_path,
        f"rm {link}; mkdir {link}; echo pwned > {link}/policy.toml",
        "--no-sandbox",
        env={"WS_HOME": str(link)},
    )
    assert (link / "policy.toml").read_text() == "pwned\n", bare.stderr  # control: the attack works without the wall


def test_a_generated_folder_outside_ws_home_is_held_too(tmp_path, wshome):
    out = tmp_path / "elsewhere" / "gate" / "opencode"
    out.mkdir(parents=True)
    (out / "opencode.json").write_text("{}")
    env = {"OPENCODE_CONFIG_DIR": str(out)}
    script = f"mv {tmp_path / 'elsewhere'} {tmp_path / 'gone'}; mkdir -p {out}; echo '{{}}' > {out}/opencode.json"
    walled = launch(tmp_path, script, env=env)
    assert not (tmp_path / "gone").exists(), walled.stderr


# --- the write allow-list --------------------------------------------------------------------------------------------


OUTSIDE = [
    "~/.bashrc",
    "~/.profile",
    "~/.local/bin/evil",
    "~/.config/systemd/user/evil.service",
    "~/.config/opencode/plugins/evil.js",
    "{package}/evil.py",
    "{work}/.git/hooks/pre-commit",
    "{work}/.git/config",
    "{work}/.claude/settings.json",
    "{work}/.opencode/plugins/evil.js",
]


@pytest.mark.parametrize("target", OUTSIDE)
def test_the_wall_writes_only_the_project_and_the_runners_own_folders(tmp_path, wshome, target):
    """Everything not on the allow-list is read-only: shell start-up files, ~/.local/bin, systemd user units, ws's own
    installed source, and inside the project the git hooks and the runner's config."""
    import ws

    work = tmp_path / "work"
    work.mkdir()
    for d in (".gi" + "t/hooks", ".claude", ".opencode/plugins"):
        (work / d).mkdir(parents=True)
    (work / ".git" / "config").write_text("[core]\n")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".bashrc").write_text("# mine\n")
    for d in (".local/bin", ".config/systemd/user", ".config/opencode/plugins"):
        (home / d).mkdir(parents=True, exist_ok=True)
    path = (
        target.replace("~", str(home)).replace("{work}", str(work)).replace("{package}", str(Path(ws.__file__).parent))
    )
    script = f"echo planted >> {path}"
    walled = launch(tmp_path, script)
    assert "planted" not in (Path(path).read_text() if Path(path).exists() else ""), walled.stderr
    assert "read-only" in walled.stderr.lower() or "not permitted" in walled.stderr.lower(), walled.stderr
    launch(tmp_path, script, "--no-sandbox")
    try:
        assert "planted" in Path(path).read_text(), "control: with no wall the append works"
    finally:
        if path.endswith("evil.py"):  # in the real package folder: do not leave it there
            Path(path).unlink(missing_ok=True)


def test_normal_work_in_the_project_and_the_private_tmp_still_works(tmp_path, wshome):
    work = tmp_path / "work"
    work.mkdir()
    script = (
        "echo a > a.txt && mkdir -p sub && echo b > sub/b.txt && mv a.txt c.txt && rm sub/b.txt "
        '&& f=$(mktemp) && echo t > $f && echo "$TMPDIR" > tmpdir.txt && git init -q . && git add c.txt'
    )
    walled = launch(tmp_path, script)
    assert (work / "c.txt").read_text() == "a\n" and not (work / "sub" / "b.txt").exists(), walled.stderr
    assert (work / "tmpdir.txt").read_text().strip().startswith("/tmp/ws-wall-"), walled.stderr
    assert not Path((work / "tmpdir.txt").read_text().strip()).exists()  # the private tmp is gone after the run


def test_the_home_folder_or_above_is_not_a_project_folder(tmp_path, wshome):
    walled = launch(tmp_path, "touch x", cwd=tmp_path / "home")
    assert "project folder" in walled.stderr and not (tmp_path / "home" / "x").exists()
    assert "project folder" in launch(tmp_path, "touch x", cwd=tmp_path).stderr


def test_allow_write_adds_a_folder(tmp_path, wshome):
    extra = tmp_path / "extra"
    extra.mkdir()
    script = f"echo ok > {extra}/f"
    launch(tmp_path, script)
    assert not (extra / "f").exists()
    launch(tmp_path, script, "--allow-write", str(extra))
    assert (extra / "f").read_text() == "ok\n"


# --- the generated OpenCode folder ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rel",
    [
        "plugin/evil.js",
        "plugins/evil.js",
        "tool/sh.js",
        "tools/sh.js",
        "agent/build.md",
        "agents/build.md",
        "opencode.jsonc",
    ],
)
def test_nothing_can_be_planted_in_the_generated_opencode_folder(tmp_path, wshome, work, rel):
    out = gr.generate(tmp_path, "opencode")
    target = out / rel
    script = f"mkdir -p {target.parent}; echo x > {target}"
    walled = launch(tmp_path, script, env={"OPENCODE_CONFIG_DIR": str(out)})
    assert not target.exists(), walled.stderr
    launch(tmp_path, script, "--no-sandbox", env={"OPENCODE_CONFIG_DIR": str(out)})
    assert target.exists(), "control: with no wall the file can be planted"


def test_opencode_starts_from_a_read_only_generated_folder(tmp_path, wshome, work):
    """The folder is wholly read-only in the wall, so OpenCode must need nothing written there (its .gitignore is
    generated). A real run through the wall, shim and permissions on."""
    out = gr.generate(tmp_path, "opencode")
    assert (out / ".gitignore").read_text().splitlines()[-1] == ".gitignore"
    call = Call("bash", {"command": f"touch {work / 'CONTROL'}", "description": "x"})
    with OpenAIStub([call]) as stub:
        proc = gr.opencode_run(tmp_path, stub, out, work, wrap=[*sb.WALL, "--"])
    assert (work / "CONTROL").exists(), gr.where(proc) + str(stub.tool_results())


# --- approvals on OpenCode inside the wall --------------------------------------------------------------------------


def ask_policy(wshome):
    text = policy.default_toml().replace("ask_commands = []", 'ask_commands = ["touch *APPROVED"]')
    assert "APPROVED" in text
    (wshome / "policy.toml").write_text(text)


def oc_step(tmp_path, wshome, work, command: str, n: int, workdir=None, first=False):
    """The n-th scripted bash call in one OpenCode session (--continue after the first): a fresh stub that replays the
    session's earlier tool results and so issues exactly one new call."""
    out = tmp_path / "wshome" / "gate" / "opencode"
    args = {"command": command, "description": "run it"}
    if workdir:
        args["workdir"] = str(workdir)
    with OpenAIStub([Call("bash", args)] * n) as stub:
        proc = gr.opencode_run(
            tmp_path, stub, out, work, wrap=[*sb.WALL, "--"], extra_args=() if first else ("--continue",)
        )
    return stub, proc


def test_an_approval_works_once_on_opencode_inside_the_wall(tmp_path, wshome, work):
    ask_policy(wshome)
    gr.generate(tmp_path, "opencode")
    target = work / "APPROVED"
    cmd = f"touch {target}"
    root = wshome / "approvals"

    stub, proc = oc_step(tmp_path, wshome, work, cmd, 1, first=True)
    assert not target.exists() and any("ws approve" in r for r in stub.tool_results()), stub.tool_results()
    pending = sorted((root / "pending").glob("*.json"))
    assert len(pending) == 1, gr.where(proc)

    approvals.grant(root, pending[0].stem[:12])  # the person, in their own terminal
    assert len(list((root / "granted").glob("*.json"))) == 1

    stub, proc = oc_step(tmp_path, wshome, work, cmd, 2)
    assert target.exists(), f"the approved call did not run: {stub.tool_results()}\n{gr.where(proc)}"
    assert not list((root / "granted").glob("*.json")), "the approval was not used up"

    target.unlink()
    stub, proc = oc_step(tmp_path, wshome, work, cmd, 3)
    assert not target.exists() and "ws approve" in stub.tool_results()[-1], "a second use must be refused"


def test_an_approval_is_bound_to_the_folder_the_command_runs_in(tmp_path, wshome, work):
    ask_policy(wshome)
    gr.generate(tmp_path, "opencode")
    other = tmp_path / "other"
    other.mkdir()
    target = work / "APPROVED"
    cmd = f"touch {target}"
    root = wshome / "approvals"
    oc_step(tmp_path, wshome, work, cmd, 1, first=True)
    approvals.grant(root, next((root / "pending").glob("*.json")).stem[:12])
    stub, _ = oc_step(tmp_path, wshome, work, cmd, 2, workdir=other)  # same text, another folder (args.workdir)
    assert not target.exists() and "ws approve" in stub.tool_results()[-1], stub.tool_results()
    assert len(list((root / "granted").glob("*.json"))) == 1  # and the approval was not spent on it


# --- project settings widening Claude Code's sandbox ----------------------------------------------------------------


def plant_project_settings(work, conf: dict) -> None:
    (work / ".claude").mkdir(exist_ok=True)
    (work / ".claude" / "settings.json").write_text(json.dumps(conf))


def claude_only_sandbox(tmp_path, command: str, sources, work):
    settings = gr.generate(tmp_path, "claude-code") / "settings.json"
    conf = json.loads(settings.read_text())
    del conf["permissions"], conf["hooks"]
    conf["sandbox"]["filesystem"]["allowWrite"] = [str(tmp_path)]
    settings.write_text(json.dumps(conf))
    with AnthropicStub([Call("Bash", {"command": command})]) as stub:
        proc = gr.claude_run(tmp_path, stub, settings, False, work, sources=sources)
    return stub, proc


def test_a_repos_settings_cannot_open_the_sandbox_for_a_gated_headless_run(tmp_path, wshome, work):
    pol = wshome / "policy.toml"
    pol.write_text(sb.STRICT)
    plant_project_settings(work, {"sandbox": {"excludedCommands": ["python3 *"], "filesystem": {"allowWrite": ["/"]}}})
    cmd = f"python3 -c \"open('{pol}', 'w').write('pwned')\"; touch {work / 'CONTROL'}"
    _, proc = claude_only_sandbox(tmp_path, cmd, "user", work)  # --setting-sources user, as ws run passes
    assert (work / "CONTROL").exists(), gr.where(proc)
    assert pol.read_text() == sb.STRICT, "a project's excludedCommands got the command out of the sandbox"


def test_the_hook_denies_a_folder_whose_settings_open_the_sandbox(tmp_path, wshome, work):
    plant_project_settings(work, {"sandbox": {"excludedCommands": ["python3 *"]}})
    stub, _, _ = gr.claude_go(tmp_path, work, "hook", [Call("Bash", {"command": f"touch {work / 'CONTROL'}"})])
    assert not (work / "CONTROL").exists(), "the command ran from a folder with sandbox-opening settings"
    assert any("excludedCommands" in r for r in stub.tool_results()), stub.tool_results()


def test_the_approval_descriptor_is_not_inherited_by_the_models_shell_commands(tmp_path, wshome, work):
    """The shim marks WS_APPROVAL_FD close-on-exec when the plugin loads (bun:ffi), so `bash` and everything it starts
    holds no socket to the launcher. With the plugin and the permission rules on, in the wall."""
    out = gr.generate(tmp_path, "opencode")
    call = Call("bash", {"command": "ls -l /proc/self/fd > " + str(work / "fds.txt"), "description": "x"})
    with OpenAIStub([call]) as stub:
        proc = gr.opencode_run(tmp_path, stub, out, work, wrap=[*sb.WALL, "--"])
    fds = (work / "fds.txt").read_text()
    inherited = [ln for ln in fds.splitlines() if "socket:" in ln and int(ln.split(" -> ")[0].split()[-1]) >= 3]
    assert not inherited, f"a shell command inherited the claim socket:\n{fds}\n{gr.where(proc)}"


def test_a_settings_file_that_is_a_link_to_a_fifo_does_not_hang_the_hook(tmp_path, wshome, work):
    """A committed `.claude/settings.json` symlinked to a FIFO would block the hook's read until it timed out, and a hook
    failure other than exit 2 lets the call through. It is a deny, fast."""
    (work / ".claude").mkdir()
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    (work / ".claude" / "settings.json").symlink_to(fifo)
    start = time.time()
    stub, _, _ = gr.claude_go(tmp_path, work, "hook", [Call("Bash", {"command": f"touch {work / 'CONTROL'}"})])
    assert time.time() - start < 90
    assert not (work / "CONTROL").exists()
    assert any("not a plain readable settings file" in r for r in stub.tool_results()), stub.tool_results()
