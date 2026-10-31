"""PLAN section 6, row 3 pass test: no secret is readable by the agent user, and agents still get a working credential.

Needs passwordless sudo and two users, `ws-broker` (owns the store, runs the broker) and `ws-dev` (the agent user),
both in the group `ws-clients`. Without them the test skips; CI sets WS_E2E_REQUIRE=1 so a missing setup fails.
Names can be changed with WS_E2E_BROKER_USER, WS_E2E_DEV_USER and WS_E2E_GROUP.

The planted value is built at run time. It is only ever given to `ws secret set` as ws-broker.
"""

from __future__ import annotations

import grp
import http.client
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from ws import broker

BROKER_USER = os.environ.get("WS_E2E_BROKER_USER", "ws-broker")
DEV_USER = os.environ.get("WS_E2E_DEV_USER", "ws-dev")
GROUP = os.environ.get("WS_E2E_GROUP", "ws-clients")
SRC = str(Path(__file__).resolve().parents[2] / "src")
PY = sys.executable
VALUE = "planted-" + os.urandom(12).hex()


def sudo(*argv, user: str | None = None, env: dict | None = None, **kw) -> subprocess.CompletedProcess:
    cmd = ["sudo", "-n"]
    if user:
        cmd += ["-u", user]
    cmd += ["env", "PYTHONDONTWRITEBYTECODE=1", f"PYTHONPATH={SRC}", *(f"{k}={v}" for k, v in (env or {}).items())]
    return subprocess.run([*cmd, *argv], capture_output=True, text=True, check=False, cwd="/", **kw)


def missing() -> str | None:
    if subprocess.run(["sudo", "-n", "true"], capture_output=True, check=False).returncode != 0:
        return "passwordless sudo is not available"
    for u in (BROKER_USER, DEV_USER):
        if subprocess.run(["id", u], capture_output=True, check=False).returncode != 0:
            return f"user {u} does not exist"
    try:
        grp.getgrnam(GROUP)
    except KeyError:
        return f"group {GROUP} does not exist"
    members = sudo("id", "-nG", DEV_USER).stdout.split()
    if GROUP not in members:
        return f"{DEV_USER} is not in group {GROUP}"
    for u in (BROKER_USER, DEV_USER):
        r = sudo("test", "-x", PY, user=u)
        if r.returncode != 0:
            return f"{u} cannot run {PY} (a home folder closed to others?)"
        r = sudo(PY, "-c", "import ws", user=u)
        if r.returncode != 0:
            return f"{u} cannot import ws from {SRC}"
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
    wb, dev, sock_dir = base / "wb", base / "dev", base / "sock"
    sudo("install", "-d", "-o", BROKER_USER, "-g", BROKER_USER, "-m", "0700", str(wb))
    sudo("install", "-d", "-o", BROKER_USER, "-g", GROUP, "-m", "0750", str(sock_dir))
    sudo("install", "-d", "-o", DEV_USER, "-m", "0700", str(dev))
    registry = base / "registry.toml"
    registry.write_text(f'[anthropic]\nhosts = ["{host}"]\npurposes = ["run"]\n', encoding="utf-8")
    sudo("install", "-o", "root", "-g", "root", "-m", "0644", str(registry), str(wb / "secrets.toml"))
    sock = sock_dir / "b.sock"
    profile = base / "profile.toml"
    profile.write_text(
        f'[roles.secrets]\nadapter = "ws-secret"\nstore = "{wb}/secrets"\nsocket = "{sock}"\n', encoding="utf-8"
    )
    sudo("install", "-o", DEV_USER, "-m", "0600", str(profile), str(dev / "profile.toml"))
    sudo("install", "-o", DEV_USER, "-m", "0600", str(registry), str(dev / "secrets.toml"))
    bw = {"WS_HOME": str(wb)}
    r = sudo(PY, "-m", "ws", "secret", "set", "anthropic", user=BROKER_USER, env=bw, input=VALUE + "\n")
    assert r.returncode == 0, "setting the planted value as the broker user failed"
    srv = subprocess.Popen(
        ["sudo", "-n", "-u", BROKER_USER, "env", "PYTHONDONTWRITEBYTECODE=1", f"PYTHONPATH={SRC}", f"WS_HOME={wb}",
         f"{broker.TEST_HTTP_ENV}=1", PY, "-m", "ws", "broker", "serve", "--socket", str(sock), "--group", GROUP],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/",
    )  # fmt: skip
    for _ in range(100):
        if sock.exists() or srv.poll() is not None:
            break
        time.sleep(0.1)
    if not sock.exists():
        err = srv.communicate(timeout=10)[1] if srv.poll() is not None else "no socket after 10 s"
        sudo("rm", "-rf", str(base))
        pytest.fail(f"the broker did not start as {BROKER_USER}: {err.strip()[-300:]}")
    yield {"base": base, "wb": wb, "dev": dev, "sock": sock, "stub": stub, "host": host, "srv": srv}
    sudo("pkill", "-TERM", "-u", BROKER_USER, "-f", "broker serve")
    try:
        srv.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        srv.kill()
    stub.shutdown()
    stub.server_close()
    sudo("rm", "-rf", str(base))
    shutil.rmtree(base, ignore_errors=True)


def as_dev(wall, *argv, **kw):
    return sudo(*argv, user=DEV_USER, env={"WS_HOME": str(wall["dev"])}, **kw)


def ws_as_dev(wall, *args, **kw):
    return as_dev(wall, PY, "-m", "ws", *args, **kw)


def test_store_is_owned_by_the_broker_user_and_closed(wall):
    store = wall["wb"] / "secrets"
    assert sudo("stat", "-c", "%U %a", str(store)).stdout.strip() == f"{BROKER_USER} 700"
    assert sudo("stat", "-c", "%U %a", str(store / "anthropic")).stdout.strip() == f"{BROKER_USER} 600"
    assert wall["srv"].poll() is None
    assert sudo("ps", "-o", "user=", "-p", str(_broker_pid())).stdout.strip() == BROKER_USER


def _broker_pid() -> int:
    return int(sudo("pgrep", "-u", BROKER_USER, "-f", "broker serve").stdout.split()[0])


def test_the_agent_user_cannot_read_the_store(wall):
    store = wall["wb"] / "secrets"
    for argv in (["cat", str(store / "anthropic")], ["ls", str(store)]):
        r = as_dev(wall, *argv)
        assert r.returncode != 0 and "Permission denied" in r.stderr, argv
        assert VALUE not in r.stdout + r.stderr
    r = as_dev(wall, PY, "-c", f"print(open('{store}/anthropic').read())")
    assert r.returncode != 0 and "PermissionError" in r.stderr and VALUE not in r.stdout + r.stderr


def test_ws_secret_as_the_agent_user_reads_and_changes_nothing(wall):
    r = ws_as_dev(wall, "secret", "list")
    assert r.returncode == 0 and "unknown" in r.stdout and "another user" in r.stdout
    assert VALUE not in r.stdout + r.stderr
    r = ws_as_dev(wall, "secret", "rm", "anthropic")
    assert r.returncode == 1 and "belongs to another user" in r.stderr
    r = ws_as_dev(wall, "secret", "set", "anthropic", input="x" + VALUE)
    assert r.returncode == 1 and "belongs to another user" in r.stderr
    assert sudo("test", "-f", str(wall["wb"] / "secrets" / "anthropic")).returncode == 0  # still there
    assert sudo("cat", str(wall["wb"] / "secrets" / "anthropic")).stdout == VALUE  # and unchanged


def test_doctor_ws104_is_ok_for_the_agent_user(wall):
    r = ws_as_dev(wall, "doctor", "--json", "--offline")
    checks = {c["id"]: c for c in json.loads(r.stdout)["checks"]}
    assert checks["WS104"]["status"] == "ok", checks["WS104"]["detail"]
    assert VALUE not in r.stdout + r.stderr


def test_the_agent_user_gets_a_lease_and_the_upstream_sees_the_injected_header(wall):
    r = ws_as_dev(wall, "broker", "request", "anthropic", "--purpose", "run", "--host", wall["host"])
    assert r.returncode == 0, r.stderr
    assert VALUE not in r.stdout + r.stderr
    lease = broker.Lease(**json.loads(r.stdout))
    hostport, _, prefix = lease.base_url.removeprefix("http://").partition("/")
    # the agent's tool would do this, holding only a placeholder
    c = http.client.HTTPConnection(hostport, timeout=10)
    c.request("POST", f"/{prefix}/v1/messages", body=b"{}", headers={"x-api-key": "placeholder"})
    assert c.getresponse().status == 200
    assert wall["stub"].seen[-1]["x-api-key"] == VALUE
    for refused in (
        ["--host", "other.example.com", "--purpose", "run"],
        ["--host", wall["host"], "--purpose", "wander"],
    ):
        r = ws_as_dev(wall, "broker", "request", "anthropic", *refused)
        assert r.returncode == 1 and VALUE not in r.stdout + r.stderr
    log = sudo("cat", str(wall["wb"] / "logs" / "broker.jsonl")).stdout
    assert VALUE not in log and lease.base_url.rsplit("/", 1)[1] not in log
    assert f'"uid": {_uid(DEV_USER)}' in log


def _uid(user: str) -> int:
    return int(subprocess.run(["id", "-u", user], capture_output=True, text=True, check=True).stdout)


def test_a_user_outside_the_group_cannot_reach_the_broker(wall):
    me = subprocess.run(["id", "-nG"], capture_output=True, text=True, check=True).stdout.split()
    if GROUP in me or os.geteuid() == 0:
        pytest.skip("the test user is in the broker's group, or is root")
    with pytest.raises(broker.BrokerUnavailable):
        broker.status(wall["sock"])
