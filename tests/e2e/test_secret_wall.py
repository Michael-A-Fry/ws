"""PLAN section 6, row 3 pass test: no secret is readable by the agent user, and agents still get a working credential.

It runs against what `ws secret wall` builds, not against a setup of its own. CI runs the printed commands first (see
the secret-wall job in .github/workflows/cli.yml), then creates the agent user `ws-dev` and an outsider `ws-other`.
Without that setup (passwordless sudo, the wall installed, those users) the test skips; CI sets WS_E2E_REQUIRE=1 so a
missing setup fails instead.

The service the wall installs is the real, https-only broker. For a call that reaches a local stub, the test also runs
`launch_broker.py`: the same installed code and store, on its own socket, allowed to speak plain http to 127.0.0.1.
The planted value is built at run time and is only ever given to `ws secret set` as ws-broker.
"""

from __future__ import annotations

import http.client
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ws import broker

BROKER_USER, DEV_USER, OTHER_USER, GROUP = "ws-broker", "ws-dev", "ws-other", "ws-clients"
WALL_HOME = Path("/var/lib/ws-broker")
WALL_BIN = "/opt/ws/bin/ws"
SERVICE_SOCK = Path("/run/ws-broker/broker.sock")
LAUNCHER = Path(__file__).resolve().parent / "launch_broker.py"
VALUE = "planted-" + os.urandom(12).hex()


def sudo(*argv, user: str | None = None, env: dict | None = None, **kw) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n"]
    if user:
        cmd += ["-u", user]
    cmd += ["env", *(f"{k}={v}" for k, v in (env or {}).items())]
    return subprocess.run([*cmd, *argv], capture_output=True, text=True, check=False, cwd="/", **kw)


def missing() -> str | None:
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0:
        return "passwordless sudo is not available"
    for u in (BROKER_USER, DEV_USER, OTHER_USER):
        if subprocess.run(["id", u], capture_output=True, check=False).returncode != 0:
            return f"user {u} does not exist"
    if GROUP not in sudo("id", "-nG", DEV_USER).stdout.split():
        return f"{DEV_USER} is not in group {GROUP} (is the wall installed?)"
    if GROUP in sudo("id", "-nG", OTHER_USER).stdout.split():
        return f"{OTHER_USER} must not be in group {GROUP}"
    if subprocess.run(["systemctl", "is-active", "--quiet", "ws-broker"], check=False).returncode != 0:
        return "the ws-broker service is not running (run the commands `ws secret wall` prints)"
    if sudo(WALL_BIN, "--version", user=DEV_USER).returncode != 0:
        return f"{DEV_USER} cannot run {WALL_BIN}"
    return None


class Stub(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), Handler)
        self.seen: list[dict[str, str]] = []


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        self.server.seen.append({k.lower(): v for k, v in self.headers.items()})
        self.send_response(200)
        self.send_header("Content-Length", "2")
        self.end_headers()
        self.wfile.write(b"ok")


@pytest.fixture(scope="module")
def wall():
    why = missing()
    if why:
        if os.environ.get("WS_E2E_REQUIRE") == "1":
            pytest.fail(f"WS_E2E_REQUIRE=1 but the secret-wall setup is missing: {why}")
        pytest.skip(why)
    base = Path(tempfile.mkdtemp(prefix="wsw"))
    base.chmod(0o711)  # others may pass through to the folders below, not list it
    stub = Stub()
    threading.Thread(target=stub.serve_forever, args=(0.05,), daemon=True).start()
    host = f"127.0.0.1:{stub.server_address[1]}"
    dev, sock_dir, sock = base / "dev", base / "sock", base / "sock" / "b.sock"
    original_registry = sudo("cat", str(WALL_HOME / "secrets.toml")).stdout
    srv = None
    try:
        # the person edits the root-owned registry to allow the stub; the service reads it on every request
        registry = base / "registry.toml"
        registry.write_text(
            f'[anthropic]\nhosts = ["api.anthropic.com", "{host}"]\npurposes = ["run"]\n', encoding="utf-8"
        )
        sudo("install", "-o", "root", "-g", "root", "-m", "0644", str(registry), str(WALL_HOME / "secrets.toml"))
        # the agent user's own profile and registry copy, pointing at the walled store and the service socket
        sudo("install", "-d", "-o", DEV_USER, "-m", "0700", str(dev))
        profile = base / "profile.toml"
        profile.write_text(
            f'[roles.secrets]\nadapter = "ws-secret"\nstore = "{WALL_HOME}/secrets"\nsocket = "{SERVICE_SOCK}"\n',
            encoding="utf-8",
        )
        sudo("install", "-o", DEV_USER, "-m", "0600", str(profile), str(dev / "profile.toml"))
        sudo("install", "-o", DEV_USER, "-m", "0600", str(registry), str(dev / "secrets.toml"))
        r = sudo(
            WALL_BIN,
            "secret",
            "set",
            "anthropic",
            user=BROKER_USER,
            env={"WS_HOME": str(WALL_HOME)},
            input=VALUE + "\n",
        )
        assert r.returncode == 0, "setting the planted value as the broker user failed"
        # the test broker: same installed code and store, its own socket, plain http to the stub allowed
        sudo("install", "-d", "-o", BROKER_USER, "-g", GROUP, "-m", "0750", str(sock_dir))
        launcher = base / "launch_broker.py"
        shutil.copy(LAUNCHER, launcher)
        launcher.chmod(0o644)
        srv = subprocess.Popen(
            ["sudo", "-n", "-u", BROKER_USER, "env", "PYTHONPATH=/opt/ws/lib", "PYTHONDONTWRITEBYTECODE=1",
             f"WS_HOME={WALL_HOME}", "/usr/bin/python3", "-s", str(launcher), str(sock), GROUP],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/",
        )  # fmt: skip
        # the runner user is outside the group, so it cannot even stat the socket: ask as the broker user
        for _ in range(100):
            if sudo("test", "-S", str(sock), user=BROKER_USER).returncode == 0 or srv.poll() is not None:
                break
            time.sleep(0.1)
        if sudo("test", "-S", str(sock), user=BROKER_USER).returncode != 0:
            err = srv.communicate(timeout=10)[1] if srv.poll() is not None else "no socket after 10 s"
            pytest.fail(f"the test broker did not start as {BROKER_USER}: {err.strip()[-300:]}")
        yield {"base": base, "dev": dev, "sock": sock, "sock_dir": sock_dir, "stub": stub, "host": host, "srv": srv}
    finally:
        sudo("pkill", "-TERM", "-u", BROKER_USER, "-f", "launch_broker.py")
        if srv is not None:
            try:
                srv.communicate(timeout=15)
            except subprocess.TimeoutExpired:
                srv.kill()
        sudo("rm", "-f", str(WALL_HOME / "secrets" / "anthropic"))
        if original_registry:
            restore = base / "original.toml"
            restore.write_text(original_registry, encoding="utf-8")
            sudo("install", "-o", "root", "-g", "root", "-m", "0644", str(restore), str(WALL_HOME / "secrets.toml"))
        stub.shutdown()
        stub.server_close()
        sudo("rm", "-rf", str(base))
        shutil.rmtree(base, ignore_errors=True)


def as_user(user, *argv, home: Path | None = None, **kw):
    return sudo(*argv, user=user, env={"WS_HOME": str(home)} if home else None, **kw)


def ws_as_dev(wall, *args, **kw):
    return as_user(DEV_USER, WALL_BIN, *args, home=wall["dev"], **kw)


def test_the_wall_is_built_the_way_the_command_says(wall):
    store = WALL_HOME / "secrets"
    assert sudo("stat", "-c", "%U %a", str(store)).stdout.strip() == f"{BROKER_USER} 700"
    assert sudo("stat", "-c", "%U %a", str(store / "anthropic")).stdout.strip() == f"{BROKER_USER} 600"
    assert sudo("stat", "-c", "%U %a", WALL_BIN).stdout.strip() == "root 755"
    assert sudo("stat", "-c", "%U", "/opt/ws/lib/ws/broker.py").stdout.strip() == "root"
    assert sudo("stat", "-c", "%U %a", str(WALL_HOME / "secrets.toml")).stdout.strip() == "root 644"
    pid = subprocess.run(
        ["systemctl", "show", "-p", "MainPID", "--value", "ws-broker"], capture_output=True, text=True, check=False
    ).stdout.strip()
    assert sudo("ps", "-o", "user=", "-p", pid).stdout.strip() == BROKER_USER


def test_the_agent_user_cannot_read_the_store(wall):
    store = WALL_HOME / "secrets"
    for argv in (["cat", str(store / "anthropic")], ["ls", str(store)]):
        r = as_user(DEV_USER, *argv)
        assert r.returncode != 0 and "Permission denied" in r.stderr, argv
        assert VALUE not in r.stdout + r.stderr
    r = as_user(DEV_USER, "/usr/bin/python3", "-c", f"print(open('{store}/anthropic').read())")
    assert r.returncode != 0 and "PermissionError" in r.stderr and VALUE not in r.stdout + r.stderr


def test_the_agent_user_cannot_change_the_code_the_broker_runs(wall):
    for argv in (["touch", "/opt/ws/lib/ws/broker.py"], ["touch", "/opt/ws/lib/ws/planted.py"], ["touch", WALL_BIN]):
        r = as_user(DEV_USER, *argv)
        assert r.returncode != 0 and "Permission denied" in r.stderr, argv


def test_a_planted_ws_package_in_the_agent_users_folder_is_not_imported(wall):
    planted = wall["dev"] / "planted"
    setup = f"mkdir -p {planted}/ws && : > {planted}/ws/__init__.py && echo 'print(\"PLANTED RAN\")' > {planted}/ws/__main__.py"
    assert as_user(DEV_USER, "sh", "-c", setup).returncode == 0
    r = as_user(DEV_USER, "sh", "-c", f"cd {planted} && exec {WALL_BIN} --version")
    assert r.returncode == 0 and r.stdout.startswith("ws ") and "PLANTED" not in r.stdout + r.stderr


def test_ws_secret_as_the_agent_user_reads_and_changes_nothing(wall):
    r = ws_as_dev(wall, "secret", "list")
    assert r.returncode == 0 and "unknown" in r.stdout and "another user" in r.stdout
    assert VALUE not in r.stdout + r.stderr
    r = ws_as_dev(wall, "secret", "rm", "anthropic")
    assert r.returncode == 1 and "belongs to another user" in r.stderr
    r = ws_as_dev(wall, "secret", "set", "anthropic", input="x" + VALUE)
    assert r.returncode == 1 and "belongs to another user" in r.stderr
    assert sudo("cat", str(WALL_HOME / "secrets" / "anthropic")).stdout == VALUE  # still there, unchanged


def test_doctor_ws104_is_ok_for_the_agent_user(wall):
    r = ws_as_dev(wall, "doctor", "--json", "--offline")
    checks = {c["id"]: c for c in json.loads(r.stdout)["data"]["checks"]}
    assert checks["WS104"]["status"] == "ok", checks["WS104"]["detail"]  # includes: the broker's code is root-only
    assert VALUE not in r.stdout + r.stderr


def test_the_agent_user_gets_a_lease_and_the_upstream_sees_the_injected_header(wall):
    r = ws_as_dev(
        wall,
        "broker",
        "request",
        "anthropic",
        "--purpose",
        "run",
        "--host",
        wall["host"],
        "--socket",
        str(wall["sock"]),
    )
    assert r.returncode == 0, r.stderr
    assert VALUE not in r.stdout + r.stderr
    lease = broker.Lease(**json.loads(r.stdout))
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)  # the agent's tool, holding only a placeholder
    c.request("POST", f"/{prefix}/v1/messages", body=b"{}", headers={"x-api-key": "placeholder"})
    assert c.getresponse().status == 200
    assert wall["stub"].seen[-1]["x-api-key"] == VALUE
    for refused in (
        ["--host", "other.example.com", "--purpose", "run"],
        ["--host", wall["host"], "--purpose", "wander"],
    ):
        r = ws_as_dev(wall, "broker", "request", "anthropic", *refused, "--socket", str(wall["sock"]))
        assert r.returncode == 1 and VALUE not in r.stdout + r.stderr
    log = sudo("cat", str(WALL_HOME / "logs" / "broker.jsonl")).stdout
    assert VALUE not in log and lease.base_url.rsplit("/", 1)[1] not in log
    dev_uid = subprocess.run(["id", "-u", DEV_USER], capture_output=True, text=True, check=True).stdout.strip()
    assert f'"uid": {dev_uid}' in log


def test_the_service_itself_never_speaks_plain_http(wall):
    seen = len(wall["stub"].seen)
    r = ws_as_dev(wall, "broker", "request", "anthropic", "--purpose", "run", "--host", wall["host"])  # profile socket
    assert r.returncode == 0, r.stderr
    lease = broker.Lease(**json.loads(r.stdout))
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request("POST", f"/{prefix}/v1/messages", body=b"{}")
    assert c.getresponse().status == 502
    assert len(wall["stub"].seen) == seen


def test_a_user_outside_the_group_is_refused_by_the_peer_check_itself(wall):
    sock, sock_dir = str(wall["sock"]), str(wall["sock_dir"])
    # as built, the folder and the socket file already keep the outsider out
    r = as_user(OTHER_USER, WALL_BIN, "broker", "status", "--socket", sock)
    assert r.returncode == 1 and "no broker answering" in r.stderr
    # open the folder and the socket file to everyone, so that only the broker's own check is left
    sudo("chmod", "0755", sock_dir)
    sudo("chmod", "0666", sock)
    try:
        r = as_user(
            OTHER_USER,
            WALL_BIN,
            "broker",
            "request",
            "anthropic",
            "--purpose",
            "run",
            "--host",
            wall["host"],
            "--socket",
            sock,
        )
        assert r.returncode == 1 and "caller_refused" in r.stderr, r.stderr
        assert VALUE not in r.stdout + r.stderr
        r = as_user(OTHER_USER, WALL_BIN, "broker", "status", "--socket", sock)
        assert r.returncode == 1 and "caller_refused" in r.stderr
        r = ws_as_dev(wall, "broker", "status", "--socket", sock)  # the agent user is still served
        assert r.returncode == 0, r.stderr
    finally:
        sudo("chmod", "0750", sock_dir)
        sudo("chmod", "0660", sock)
