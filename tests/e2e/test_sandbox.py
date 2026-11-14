"""The sandbox as the real wall (phase 4): each bypass the command matcher cannot see is stopped by the sandbox alone, on
both runners, with a stub model and no paid provider. These are the "Known limits" of ws.policy:

  interpreter   `python3 -c` writes the policy file
  download-run  a script is written, then run, and it writes the policy file
  built path    `cat "$(echo ~)/.ssh/id_rsa"` reads a denied folder through a path the matcher never sees
  vcs restore   `git -C $WS_HOME checkout -- policy.toml` puts an older policy back

The deny rules and the hook are stripped from the generated config, so only the wall can block: Claude Code's native
`sandbox` block, and for OpenCode `ws gate launch` (bubblewrap). Three checks keep a pass from being vacuous:
  * the matcher itself lets each command through (so it is the wall that stops it, and the docstring is true);
  * a control in the same command line (a write into the project folder) does happen under the wall;
  * the same commands, with the wall off (`ws gate launch --no-sandbox`), do harm: the policy changes, the canary leaks.

Linux only (bubblewrap). A runner or bubblewrap that is missing skips the test, unless WS_E2E_REQUIRE=1 (CI), when it
fails.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

from e2e import test_gate_runners as gr
from e2e.stubs import AnthropicStub, Call, OpenAIStub
from ws import policy, sandbox, wallsettings
from ws.adapters import claude_code

# reuse the other file's fixtures: the built-in policy at WS_HOME, a project folder, a canary under a fake ~/.ssh
ws_home = gr.ws_home
work = gr.work
secrets = gr.secrets

STRICT = policy.default_toml() + "# the policy a person wrote\n"
LOOSE = policy.default_toml() + "# an older, looser policy\n"
WALL = [sys.executable, "-m", "ws", "gate", "launch", "--runner", "opencode"]


def no_wall(test):
    """For a test that needs no bubblewrap (the read block is a permission key, not part of the sandbox)."""
    test.needs_no_wall = True
    return test


@pytest.fixture(autouse=True)
def need_wall(request):
    if getattr(request.function, "needs_no_wall", False):
        return
    if sys.platform != "linux":
        pytest.skip("the wall is tested on Linux (bubblewrap)")
    if not sandbox._bwrap_starts():
        if gr.REQUIRE:
            pytest.fail("bubblewrap cannot start here and WS_E2E_REQUIRE=1")
        pytest.skip("bubblewrap is missing or cannot start")


@pytest.fixture
def wshome(tmp_path):
    """$WS_HOME with a policy a person wrote, kept in git with an older, looser version committed."""
    home = tmp_path / "wshome"
    home.mkdir()
    ident = ["-c", "user.name=t", "-c", "user.email=t" + chr(64) + "t.invalid"]
    (home / "policy.toml").write_text(LOOSE)
    for cmd in (["init", "-q"], ["add", "policy.toml"], [*ident, "commit", "-q", "-m", "older"]):
        subprocess.run(["git", "-C", str(home), *cmd], check=True, capture_output=True)
    (home / "policy.toml").write_text(STRICT)
    return home


def attacks(wshome, home, work) -> dict[str, str]:
    """name -> a command line that, run bare, rewrites the policy or reads the canary. The last part of each is a
    control: it writes into the project folder, which the wall allows."""
    pol = wshome / "policy.toml"
    control = f"touch {work / 'CONTROL'}"
    return {
        "interpreter": f"python3 -c \"open('{pol}', 'w').write('pwned')\"; {control}",
        "download-run": f"printf 'echo pwned > {pol}\\n' > dl.sh && bash dl.sh; {control}",
        "built-path-read": f'cat "$(echo ~)/notes.txt"; cat "$(echo ~)/.ssh/id_rsa"; {control}',
        "vcs-restore": f"git -C {wshome} checkout -- policy.toml; {control}",
    }


CASES = ["interpreter", "download-run", "built-path-read", "vcs-restore"]


# --- the runners with only the wall -------------------------------------------------------------------------------


def claude_wall(tmp_path, wshome, command, sandbox_on=True):
    """Claude Code with the permission rules and hooks stripped: only the generated `sandbox` block is left. The test
    folder is made writable, so that it is the block's denyWrite, not the default write boundary, that protects ws."""
    settings = gr.generate(tmp_path, "claude-code") / "settings.json"
    conf = json.loads(settings.read_text())
    del conf["permissions"], conf["hooks"]
    conf["sandbox"]["filesystem"]["allowWrite"] = [str(tmp_path)]
    if not sandbox_on:
        del conf["sandbox"]
    settings.write_text(json.dumps(conf))
    with AnthropicStub([Call("Bash", {"command": command})]) as stub:
        proc = gr.claude_run(tmp_path, stub, settings, False, tmp_path / "work")
    return stub, proc


def opencode_wall(tmp_path, wshome, command, flags: tuple[str, ...] = ()):
    out = gr.generate(tmp_path, "opencode")
    (out / "plugins" / "ws-gate.js").unlink()
    conf = json.loads((out / "opencode.json").read_text())
    del conf["permission"]
    (out / "opencode.json").write_text(json.dumps(conf))
    call = Call("bash", {"command": command, "description": "run it"})
    with OpenAIStub([call]) as stub:
        proc = gr.opencode_run(tmp_path, stub, out, tmp_path / "work", wrap=[*WALL, *flags, "--"])
    return stub, proc


# --- the read block: Claude's own file tools stay in the working folders ------------------------------------------------

READ_BLOCK = f"permissions.{wallsettings.READ_BLOCK_KEY}"


def need_a_claude_that_reads_the_key():
    """Skip, saying why, when the runner is older than the version the shared table gives for the key. A missing runner
    skips, or fails under WS_E2E_REQUIRE=1, as it does for every other test here (gr.runner_bin)."""
    found = gr.claude_version()
    need = wallsettings.MIN_CLAUDE[READ_BLOCK]
    if found < need:
        have, first = ".".join(map(str, found)), ".".join(map(str, need))
        pytest.skip(f"Claude Code {have} is older than {first}, the first to read {READ_BLOCK}")


def read_outside(tmp_path, work, allow_outside: bool):
    """`claude -p` with a stub model that asks to Read a file planted outside the working folder. Only the generated
    `permissions` block's read key is kept (hooks, deny rules and the sandbox are stripped), so it is the key alone that
    decides. `allow_outside` is the policy's `read_outside_project`, which is the off switch."""
    planted = tmp_path / "outside" / "notes.txt"
    planted.parent.mkdir()
    marker = "outside-" + os.urandom(6).hex()  # built at run time
    planted.write_text(marker)
    text = policy.default_toml()
    if allow_outside:
        assert "read_outside_project = false" in text
        text = text.replace("read_outside_project = false", "read_outside_project = true")
    (tmp_path / "wshome").mkdir(exist_ok=True)
    (tmp_path / "wshome" / "policy.toml").write_text(text)
    settings = gr.generate(tmp_path, "claude-code") / "settings.json"
    perms = json.loads(settings.read_text())["permissions"]
    conf = {"permissions": {k: v for k, v in perms.items() if k == wallsettings.READ_BLOCK_KEY}}
    settings.write_text(json.dumps(conf))
    with AnthropicStub([Call("Read", {"file_path": str(planted)})]) as stub:
        proc = gr.claude_run(tmp_path, stub, settings, False, work)
    return marker, stub, proc, conf


@no_wall
def test_a_read_outside_the_working_folder_is_refused_with_the_key_on(tmp_path, work):
    need_a_claude_that_reads_the_key()
    marker, stub, proc, conf = read_outside(tmp_path, work, allow_outside=False)
    assert conf["permissions"] == {wallsettings.READ_BLOCK_KEY: True}  # the generated settings carry it
    assert stub.tool_results(), f"the runner never reported the Read back to the model\n{gr.where(proc)}"
    assert marker not in " ".join(stub.tool_results()), f"the file outside the folder was read\n{gr.where(proc)}"


@no_wall
def test_the_same_read_is_allowed_with_the_off_switch(tmp_path, work):
    """The control: with `read_outside_project = true` the key is left out and the same stub call reads the file, so the
    refusal above is the key's and not something else's."""
    need_a_claude_that_reads_the_key()
    marker, stub, proc, conf = read_outside(tmp_path, work, allow_outside=True)
    assert conf["permissions"] == {}  # no key
    assert marker in " ".join(stub.tool_results()), f"the control read did not happen\n{gr.where(proc)}"


# --- deny-home in the launch settings (ADR 0026) ------------------------------------------------------------------------

ALLOW_READ = "sandbox.filesystem.allowRead"


def need_a_claude_that_reads_allow_read():
    """The read block's first version (2.1.257) is newer than allowRead's in the shared table, so the one probe above
    serves: a Claude Code that reads the block reads allowRead. A missing runner skips, or fails under WS_E2E_REQUIRE=1."""
    assert wallsettings.MIN_CLAUDE[ALLOW_READ] <= wallsettings.MIN_CLAUDE[READ_BLOCK]
    need_a_claude_that_reads_the_key()


def launch_copy(tmp_path, project, allow_read: tuple[str, ...] = ()) -> dict:
    """The settings `ws gate launch --runner claude-code` would start claude with for `project`, made the way
    `launch_claude` makes them, with the permission rules and hooks stripped so the sandbox block alone decides."""
    (tmp_path / "wshome").mkdir(exist_ok=True)
    text = policy.default_toml()
    assert "# allow_read = []" in text
    (tmp_path / "wshome" / "policy.toml").write_text(
        text.replace("# allow_read = []", f"allow_read = {json.dumps(allow_read)}")
    )
    shared = gr.generate(tmp_path, "claude-code") / "settings.json"
    pol = policy.load(tmp_path / "wshome" / "policy.toml")
    allow, deny = policy.launch_read_rules(pol)
    text = claude_code.for_project(
        shared.read_text(), str(project.resolve()), allow_read=allow, deny_read=deny, env=gr.base_env(tmp_path)
    )
    conf = json.loads(text)
    del conf["permissions"], conf["hooks"]
    return conf


def run_in_launch_copy(tmp_path, project, command, allow_read: tuple[str, ...] = (), mutate=None):
    need_a_claude_that_reads_allow_read()
    conf = launch_copy(tmp_path, project, allow_read)
    if mutate:
        mutate(conf)
    settings = tmp_path / "launch-settings.json"
    settings.write_text(json.dumps(conf))
    with AnthropicStub([Call("Bash", {"command": command})]) as stub:
        proc = gr.claude_run(tmp_path, stub, settings, False, project)
    return conf, stub, proc


def planted(path, text: str | None = None) -> str:
    """A file with a marker built at run time; returns the marker."""
    marker = text or "marker-" + os.urandom(6).hex()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(marker)
    return marker


def test_a_shell_cannot_read_the_home_but_reads_the_project(tmp_path):
    """Inside the tmp HOME (the fake home of the runner): a file planted in it is hidden, the project's file is not. The
    project is inside that home, so it is the re-allow that shows the control read."""
    home = tmp_path / "home"
    project = home / "project"
    secret = planted(home / "private-notes.txt")
    deep = planted(home / "Documents" / "deep" / "notes.txt")
    mine = planted(project / "notes.txt")
    cmd = f"cat {home}/private-notes.txt; cat {home}/Documents/deep/notes.txt; cat {project}/notes.txt"
    _, stub, proc = run_in_launch_copy(tmp_path, project, cmd)
    seen = " ".join(stub.tool_results())
    assert mine in seen, f"the control read in the project did not happen\n{seen}\n{gr.where(proc)}"
    assert secret not in seen and deep not in seen, f"the home was read\n{seen}\n{gr.where(proc)}"


def test_a_shell_cannot_read_the_ws_secrets_folder_by_default_or_inside_a_folder_allow_read_opens(tmp_path):
    """`~/.local/share/ws/secrets` is a deny_read path. By default `~/.local/share` is not reachable at all; with it in
    allow_read the folder is open and the secrets folder inside it stays hidden (an exact deny holds in a wider allow)."""
    home = tmp_path / "home"
    project = home / "project"
    share = home / ".local" / "share"
    key = planted(share / "ws" / "secrets" / "API_KEY")
    other = planted(share / "other-tool" / "readme.txt")
    mine = planted(project / "notes.txt")
    cmd = f"cat {share}/ws/secrets/API_KEY; cat {share}/other-tool/readme.txt; cat {project}/notes.txt"
    _, stub, proc = run_in_launch_copy(tmp_path, project, cmd)
    seen = " ".join(stub.tool_results())
    assert mine in seen and key not in seen and other not in seen, f"{seen}\n{gr.where(proc)}"
    _, stub, proc = run_in_launch_copy(tmp_path, project, cmd, allow_read=("~/.local/share",))
    seen = " ".join(stub.tool_results())
    assert other in seen, (
        f"allow_read did not open the folder\n{seen}\n{gr.where(proc)}"
    )  # the control: it does open it
    assert mine in seen and key not in seen, f"the secrets folder was read through allow_read\n{seen}\n{gr.where(proc)}"


def test_the_same_reads_work_when_the_launch_copy_has_no_deny_home(tmp_path):
    """The control for the two above: take the home out of denyRead (the settings as they were before ADR 0026) and the
    same command reads the planted files, so it is deny-home that hid them and not something else in the test."""
    home = tmp_path / "home"
    project = home / "project"
    secret = planted(home / "private-notes.txt")
    mine = planted(project / "notes.txt")

    def as_before(conf):
        fs = conf["sandbox"]["filesystem"]
        fs["denyRead"] = [p for p in fs["denyRead"] if p not in (str(home), str(home.resolve()))]
        del fs["allowRead"]

    cmd = f"cat {home}/private-notes.txt; cat {project}/notes.txt"
    _, stub, proc = run_in_launch_copy(tmp_path, project, cmd, mutate=as_before)
    seen = " ".join(stub.tool_results())
    assert secret in seen and mine in seen, f"{seen}\n{gr.where(proc)}"


# --- host reach: how a command in the sandbox gets to an allowed host (spike) -------------------------------------------
# The sandbox has no network of its own. Claude Code runs a proxy outside it and bridges the proxy to a port inside, which
# the shell finds in HTTP_PROXY and HTTPS_PROXY. A program that honours those variables reaches the hosts in
# `sandbox.network.allowedDomains` and no other; one that does not (a raw socket) reaches nothing. No Unix socket is
# allowed for any of it: `allowUnixSockets` and `allowAllUnixSockets` are never set. NO_PROXY names the loopback
# addresses, so a command that wants the proxy to reach a loopback stub says `--noproxy ''` (curl).


class Stub:
    """A web server on 127.0.0.1, on a port the kernel picks, that answers every GET with a marker built at run time."""

    def __init__(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        self.marker = "stub-" + os.urandom(6).hex()
        marker = self.marker.encode()

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(marker)))
                self.end_headers()
                self.wfile.write(marker)

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]

    def __enter__(self):
        import threading

        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()


def through_the_proxy(url: str) -> str:
    return f'curl -sS --max-time 8 --noproxy \'\' -x "$HTTP_PROXY" {url}; echo " rc=$?"'


def test_a_command_reaches_a_host_in_allowed_domains_through_the_runners_proxy_and_no_other(tmp_path):
    project = tmp_path / "home" / "project"
    project.mkdir(parents=True)
    with Stub() as allowed, Stub() as other:

        def only_the_first(conf):
            conf["sandbox"]["network"] = {"allowedDomains": [f"127.0.0.1:{allowed.port}"]}

        cmd = "; ".join(
            [
                through_the_proxy(f"http://127.0.0.1:{allowed.port}/"),  # on the list
                through_the_proxy(f"http://127.0.0.1:{other.port}/"),  # same host, a port that is not
                f"curl -sS --max-time 8 --noproxy '*' http://127.0.0.1:{allowed.port}/; echo \" direct rc=$?\"",
            ]
        )
        conf, stub, proc = run_in_launch_copy(tmp_path, project, cmd, mutate=only_the_first)
    seen = " ".join(stub.tool_results())
    net = conf["sandbox"]["network"]
    assert "allowUnixSockets" not in net and "allowAllUnixSockets" not in net  # no socket was needed
    assert f"{allowed.marker} rc=0" in seen, f"the allowed stub was not reached\n{seen}\n{gr.where(proc)}"
    assert other.marker not in seen, f"a port off the list was reached\n{seen}\n{gr.where(proc)}"
    assert "blocked by network allowlist" in seen.lower(), f"the proxy did not say it refused\n{seen}"
    assert "direct rc=0" not in seen and "direct rc=7" in seen, (
        f"a direct connection got out\n{seen}"
    )  # no route at all


REAL_HOSTS = ("github.com", "pypi.org")


def test_the_default_hosts_are_reached_through_the_proxy_with_real_servers(tmp_path):
    """Only with WS_E2E_NETWORK=1 (it needs the internet): the launch copy's default list reaches GitHub and PyPI, and a
    host that is on no list is refused."""
    if os.environ.get("WS_E2E_NETWORK") != "1":
        pytest.skip("WS_E2E_NETWORK=1 is not set: this one talks to github.com and pypi.org")
    project = tmp_path / "home" / "project"
    project.mkdir(parents=True)
    probe = "curl -sS -o /dev/null -w '%s=%%{http_code}\\n' --max-time 20 https://%s/"
    cmd = "; ".join([*(probe % (h, h) for h in REAL_HOSTS), (probe % ("example.org", "example.org")) + " || true"])
    conf, stub, proc = run_in_launch_copy(tmp_path, project, cmd)
    seen = " ".join(stub.tool_results())
    assert set(REAL_HOSTS) <= set(conf["sandbox"]["network"]["allowedDomains"])
    for host in REAL_HOSTS:
        assert re.search(rf"{re.escape(host)}=(200|301|302)", seen), f"{host} was not reached\n{seen}\n{gr.where(proc)}"
    assert not re.search(r"example\.org=(200|301|302)", seen), f"a host on no list was reached\n{seen}"


# --- the matcher does not see them --------------------------------------------------------------------------------


# Closed by the gate's command reader: a built part followed by a protected path is refused before the wall is needed.
READER_CLOSED = ["built-path-read"]


@pytest.mark.parametrize("name", READER_CLOSED)
def test_the_reader_refuses_what_it_has_learned(tmp_path, wshome, work, secrets, monkeypatch, name):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cmd = attacks(wshome, tmp_path / "home", work)[name]
    protect = (str(wshome / "policy.toml"), *policy.control_paths(wshome))
    assert policy.is_denied_command(policy.default_policy(), cmd, protect=protect, cwd=str(work)) is not None


@pytest.mark.parametrize("name", [c for c in CASES if c not in READER_CLOSED])
def test_the_matcher_lets_each_bypass_through(tmp_path, wshome, work, secrets, monkeypatch, name):
    """If the matcher ever learns one of these, ws.policy's "Known limits" is out of date: move it to what is closed."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    cmd = attacks(wshome, tmp_path / "home", work)[name]
    protect = (str(wshome / "policy.toml"), *policy.control_paths(wshome))
    assert policy.is_denied_command(policy.default_policy(), cmd, protect=protect, cwd=str(work)) is None


# --- the wall stops them ------------------------------------------------------------------------------------------


def assert_held(wshome, secrets, stub, proc, work, name):
    where = gr.where(proc)
    assert (work / "CONTROL").exists(), f"the control write did not run: {stub.tool_results()}\n{where}"
    assert (wshome / "policy.toml").read_text() == STRICT, f"the policy changed\n{where}"
    seen = " ".join(stub.tool_results())
    assert secrets["canary"] not in seen, "the canary was read"
    if name == "built-path-read":
        assert secrets["plain"] in seen, f"the allowed file was not read either: {seen!r}"


@pytest.mark.parametrize("name", CASES)
def test_claude_code_sandbox_stops_it(tmp_path, wshome, work, secrets, name):
    stub, proc = claude_wall(tmp_path, wshome, attacks(wshome, tmp_path / "home", work)[name])
    assert_held(wshome, secrets, stub, proc, work, name)


@pytest.mark.parametrize("name", CASES)
def test_opencode_wall_stops_it(tmp_path, wshome, work, secrets, name):
    stub, proc = opencode_wall(tmp_path, wshome, attacks(wshome, tmp_path / "home", work)[name])
    assert_held(wshome, secrets, stub, proc, work, name)


@pytest.mark.parametrize("name", CASES)
def test_without_the_wall_each_bypass_works(tmp_path, wshome, work, secrets, name):
    """The control for the control: the commands are real attacks. `--no-sandbox` also says what it gives up."""
    stub, proc = opencode_wall(tmp_path, wshome, attacks(wshome, tmp_path / "home", work)[name], ("--no-sandbox",))
    assert "NO wall" in proc.stderr, gr.where(proc)
    if name == "built-path-read":
        assert secrets["canary"] in " ".join(stub.tool_results()), gr.where(proc)
    else:
        assert (wshome / "policy.toml").read_text() != STRICT, gr.where(proc)


@pytest.mark.parametrize("name", CASES)
def test_without_its_sandbox_each_bypass_works_on_claude_code(tmp_path, wshome, work, secrets, name):
    stub, proc = claude_wall(tmp_path, wshome, attacks(wshome, tmp_path / "home", work)[name], sandbox_on=False)
    if name == "built-path-read":
        assert secrets["canary"] in " ".join(stub.tool_results()), gr.where(proc)
    else:
        assert (wshome / "policy.toml").read_text() != STRICT, gr.where(proc)


def test_opencode_wall_keeps_the_gate_able_to_ask(tmp_path, wshome, work, secrets):
    """Inside the wall the policy is read-only, but the two folders the gate and the nudge write stay writable and the
    folder that holds grants does not."""
    sub = wshome / "approvals" / "pending"
    cmd = (
        f"touch {sub}/p {wshome}/sessions/s; touch {wshome}/approvals/granted-by-session || true; touch {work}/CONTROL"
    )
    _, proc = opencode_wall(tmp_path, wshome, cmd)
    assert (work / "CONTROL").exists(), gr.where(proc)
    assert (sub / "p").exists() and (wshome / "sessions" / "s").exists(), gr.where(proc)
    assert not (wshome / "approvals" / "granted-by-session").exists()


# --- the docs' own probe, through `ws canary` (C250 adv-A1/A2) --------------------------------------------------------------


def _probe_case(tmp_path, change=None):
    from ws import canary

    exe = gr.runner_bin("claude")
    work = tmp_path / "probe"
    project = work / "project"
    project.mkdir(parents=True)
    project = project.resolve()
    env = gr.base_env(tmp_path)  # a home of the test's own: the probe's touch lands there, never in the real one
    text = canary._launch_text(claude_code, env, work, project, policy.loads(policy.default_toml()))
    if change:
        conf = json.loads(text)
        change(conf)
        text = json.dumps(conf)
    return canary.run_probe(exe, text, project, work, env)


def test_the_docs_probe_is_stopped_in_the_launch_settings(tmp_path):
    assert _probe_case(tmp_path) is None


def test_the_docs_probe_gets_through_with_the_sandbox_taken_out(tmp_path):
    """The control: the same probe with no sandbox block writes into the real home, so the pass above is the wall's."""
    problem = _probe_case(tmp_path, lambda c: c.pop("sandbox"))
    assert problem is not None and "wrote outside" in problem, problem


# --- the write block: Claude's own file tools write only in the working folders (rr-M2) ---------------------------------

WRITE_BLOCK_FIRST = (2, 1, 314)  # the settings reference: blockWritesOutsideWorkingDirectories needs 2.1.314


def write_outside(tmp_path, work, allow_outside: bool):
    """`claude -p` with a stub model that asks to Write a file outside the working folder. Only the generated
    `permissions` block's write key is kept, so it is the key alone that decides; `allow_outside` is the off switch."""
    found = gr.claude_version()
    if found < WRITE_BLOCK_FIRST:
        pytest.skip(f"Claude Code {'.'.join(map(str, found))} is older than 2.1.314, the first to read the write block")
    target = tmp_path / "outside" / ("written-" + os.urandom(6).hex())  # built at run time
    target.parent.mkdir()
    text = policy.default_toml()
    if allow_outside:
        assert "write_outside_project = false" in text
        text = text.replace("write_outside_project = false", "write_outside_project = true")
    (tmp_path / "wshome").mkdir(exist_ok=True)
    (tmp_path / "wshome" / "policy.toml").write_text(text)
    settings = gr.generate(tmp_path, "claude-code") / "settings.json"
    perms = json.loads(settings.read_text())["permissions"]
    conf = {"permissions": {k: v for k, v in perms.items() if k == claude_code.WRITE_BLOCK_KEY}}
    settings.write_text(json.dumps(conf))
    with AnthropicStub([Call("Write", {"file_path": str(target), "content": "x"})]) as stub:
        proc = gr.claude_run(tmp_path, stub, settings, False, work)
    return target, stub, proc, conf


@no_wall
def test_a_write_outside_the_working_folder_is_refused_with_the_key_on(tmp_path, work):
    target, stub, proc, conf = write_outside(tmp_path, work, allow_outside=False)
    assert conf["permissions"] == {claude_code.WRITE_BLOCK_KEY: True}
    assert stub.tool_results(), f"the runner never reported the Write back to the model\n{gr.where(proc)}"
    assert not target.exists(), f"the file outside the folder was written\n{gr.where(proc)}"


@no_wall
def test_the_same_write_happens_with_the_off_switch(tmp_path, work):
    target, _stub, proc, conf = write_outside(tmp_path, work, allow_outside=True)
    assert conf["permissions"] == {}
    assert target.exists(), f"the control write did not happen\n{gr.where(proc)}"
