"""The dev-server guard (ws.guards.dev_server): a development server listens on this machine only."""

from __future__ import annotations

import json

import pytest

from ws import approvals, gate, guards, policy
from ws.adapters import Call
from ws.guards import dev_server


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "home" / ".ws"))
    (tmp_path / "home" / ".ws").mkdir(parents=True)
    return tmp_path / "home"


def cfg_of(**table):
    return dev_server.config(table, "[guards.dev-server]")


def judge(command, **table):
    return dev_server.judge(Call("command", command), cfg_of(**table), None)


# command -> the flag the reason must name
DENIED = [
    ("python -m http.server", "--bind 127.0.0.1"),
    ("python3 -m http.server 8000", "--bind 127.0.0.1"),
    ("python3.12 -u -m http.server 8000 -d public", "--bind 127.0.0.1"),
    ("python -m http.server --bind 0.0.0.0", "--bind 127.0.0.1"),
    ("python -m http.server 9000 -b 0.0.0.0", "--bind 127.0.0.1"),
    ("python -m http.server -b198.51.100.20", "--bind 127.0.0.1"),
    ("python -m http.server --bind=::", "--bind 127.0.0.1"),
    ("python -m http.server --bind ''", "--bind 127.0.0.1"),
    ("python -mhttp.server", "--bind 127.0.0.1"),
    ("python -m SimpleHTTPServer", "--bind 127.0.0.1"),
    ("php -S 0.0.0.0:8000", "-S localhost:8000"),
    ("php -S 0.0.0.0:8080 -t public", "-S localhost:8080"),
    ("php -S [::]:9000", "-S localhost:9000"),
    ("php -S :8000", "-S localhost:8000"),
    ("php -S198.51.100.5:3000", "-S localhost:3000"),
    ("npx serve", "-l localhost:3000"),
    ("npx serve build", "-l localhost:3000"),
    ("npx --yes serve -s build", "-l localhost:3000"),
    ("serve", "-l localhost:3000"),
    ("serve -p 5000", "-l localhost:3000"),
    ("serve -l 3000", "-l localhost:3000"),
    ("serve --listen tcp://0.0.0.0:5000", "-l localhost:3000"),
    ("serve -l localhost:3000 -l 4000", "-l localhost:3000"),
    ("vite --host", "--host 127.0.0.1"),
    ("vite --host --port 3000", "--host 127.0.0.1"),
    ("vite --host 0.0.0.0", "--host 127.0.0.1"),
    ("vite --host=0.0.0.0", "--host 127.0.0.1"),
    ("vite preview --host", "--host 127.0.0.1"),
    ("npx vite --host", "--host 127.0.0.1"),
    ("./node_modules/.bin/vite --host 0.0.0.0", "--host 127.0.0.1"),
    ("pnpm exec vite --host", "--host 127.0.0.1"),
    ("flask run --host 0.0.0.0", "--host 127.0.0.1"),
    ("flask --app app run --host=0.0.0.0 --port 5000", "--host 127.0.0.1"),
    ("flask run -h 0.0.0.0", "--host 127.0.0.1"),
    ("python -m flask run --host 0.0.0.0", "--host 127.0.0.1"),
    ("FLASK_RUN_HOST=0.0.0.0 flask run", "--host 127.0.0.1"),
    ("jupyter lab --ip 0.0.0.0", "--ip 127.0.0.1"),
    ("jupyter notebook --ip=0.0.0.0 --no-browser", "--ip 127.0.0.1"),
    ("jupyter lab --ip=*", "--ip 127.0.0.1"),
    ("jupyter lab --ip='*'", "--ip 127.0.0.1"),
    ("jupyter server --ServerApp.ip=0.0.0.0", "--ip 127.0.0.1"),
    ("jupyter-lab --NotebookApp.ip=0.0.0.0", "--ip 127.0.0.1"),
    ("jupyter-notebook --ip 198.51.100.4", "--ip 127.0.0.1"),
    ("python -m jupyterlab --ip 0.0.0.0", "--ip 127.0.0.1"),
    ("rails s -b 0.0.0.0", "-b 127.0.0.1"),
    ("rails server --binding=0.0.0.0", "-b 127.0.0.1"),
    ("bin/rails s -p 3001 -b 0.0.0.0", "-b 127.0.0.1"),
    ("bundle exec rails server -b0.0.0.0", "-b 127.0.0.1"),
    ("uvicorn app:app --host 0.0.0.0", "--host 127.0.0.1"),
    ("uvicorn app:app --host=0.0.0.0 --reload", "--host 127.0.0.1"),
    ("python -m uvicorn app:app --host 0.0.0.0", "--host 127.0.0.1"),
    ("uv run uvicorn app:app --host 0.0.0.0", "--host 127.0.0.1"),
    ("hugo server --bind 0.0.0.0", "--bind 127.0.0.1"),
    ("hugo server --bind=0.0.0.0 -D", "--bind 127.0.0.1"),
    ("hugo serve --bind 0.0.0.0", "--bind 127.0.0.1"),
    ("uv run python -m http.server", "--bind 127.0.0.1"),
    ("uvx --from jupyterlab jupyter lab --ip 0.0.0.0", "--ip 127.0.0.1"),
    ("poetry run flask run --host 0.0.0.0", "--host 127.0.0.1"),
    ("sudo python -m http.server 80", "--bind 127.0.0.1"),
    ("nohup python3 -m http.server 8000 &", "--bind 127.0.0.1"),
    ("cd site && python3 -m http.server", "--bind 127.0.0.1"),
    ("bash -c 'python -m http.server 8000'", "--bind 127.0.0.1"),
    ("timeout 60 npx serve", "-l localhost:3000"),
]

ALLOWED = [
    "python -m http.server --bind 127.0.0.1",
    "python -m http.server 8000 --bind 127.0.0.1",
    "python3 -m http.server -b localhost 8000",
    "python -m http.server --bind ::1",
    "python -m http.server --bind=localhost",
    "python -m http.server -b127.0.0.1",
    "python -m http.server --bind 127.0.0.5",
    "python -m http.server --help",
    "python -m http.server -h",
    "php -S localhost:8000",
    "php -S 127.0.0.1:8080 -t public",
    "php -S [::1]:8000",
    "php -v",
    "npx serve -l localhost:3000",
    "npx serve -l tcp://127.0.0.1:5000 build",
    "serve --listen=localhost:5000",
    "serve -l unix:/tmp/site.sock",
    "serve --help",
    "vite",
    "vite --port 3000",
    "vite --host localhost",
    "vite --host 127.0.0.1 --port 3000",
    "vite --host=::1",
    "vite build --host",
    "vite preview",
    "flask run",
    "flask run --host 127.0.0.1 --port 5000",
    "flask run --port 8000",
    "flask --app app routes",
    "FLASK_RUN_HOST=localhost flask run",
    "jupyter lab",
    "jupyter lab --ip 127.0.0.1",
    "jupyter notebook --ip=localhost --no-browser",
    "jupyter lab --ServerApp.ip=127.0.0.1",
    "jupyter --version",
    "jupyter nbconvert --to html a.ipynb",
    "jupyter kernelspec list",
    "rails s",
    "rails server -p 3001",
    "rails s -b 127.0.0.1",
    "rails console",
    "rails db:migrate",
    "uvicorn app:app",
    "uvicorn app:app --host 127.0.0.1 --port 8000",
    "uvicorn app:app --reload --host=localhost",
    "hugo",
    "hugo server",
    "hugo server --bind 127.0.0.1 -D",
    "hugo new post.md",
    "echo python -m http.server",
    "echo 'serve'",
    "grep -r 'vite --host' .",
    "git commit -m 'run python -m http.server'",
    "python script.py --host 0.0.0.0",
    "python -c 'print(1)'",
    "python -m pytest tests",
    "npm run dev",
    "npm install serve",
    "ls",
]


@pytest.mark.parametrize(("command", "flag"), DENIED)
def test_a_server_on_every_interface_is_denied_and_the_reason_names_the_flag(command, flag):
    found = judge(command)
    assert found is not None, command
    assert found.action == "deny" and found.approvable
    assert f"`{flag.split(' (')[0]}`" in found.reason or flag in found.reason, (command, found.reason)
    assert "other machines can reach" in found.reason


@pytest.mark.parametrize("command", ALLOWED)
def test_a_loopback_bind_and_other_commands_go_through(command):
    assert judge(command) is None, command


def test_the_reason_names_where_it_would_listen_and_the_way_out():
    found = judge("python -m http.server 8000 --bind 198.51.100.20")
    assert "198.51.100.20" in found.reason and "allow_hosts" in found.reason
    assert "every interface (the default)" in judge("python -m http.server").reason


def test_allow_hosts_lets_a_chosen_address_through():
    table = {"allow_hosts": ["203.0.113.5", "box.tail.example"]}
    assert judge("python -m http.server --bind 203.0.113.5", **table) is None
    assert judge("python -m http.server 8000 --bind 203.0.113.5:8000", **table) is None  # a port is ignored
    assert judge("vite --host box.tail.example", **table) is None
    assert judge("serve -l tcp://203.0.113.5:3000", **table) is None
    assert judge("php -S 203.0.113.5:8000", **table) is None
    assert judge("python -m http.server --bind 203.0.113.6", **table)  # another address is not allowed
    assert judge("python -m http.server --bind 0.0.0.0", **table)  # and every interface never is
    assert judge("python -m http.server", **table)  # the default still means every interface
    assert judge("vite --host", **table)


@pytest.mark.parametrize(
    ("table", "message"),
    [
        ({"x": 1}, r"unknown key 'x'"),
        ({"allow_hosts": "203.0.113.5"}, r"allow_hosts must be a list of non-empty text"),
        ({"allow_hosts": [""]}, r"allow_hosts must be a list of non-empty text"),
        ({"allow_hosts": ["0.0.0.0"]}, r"must be a host name or an address"),
        ({"allow_hosts": ["0.0.0.0:8000"]}, r"must be a host name or an address"),
        ({"allow_hosts": ["::"]}, r"must be a host name or an address"),
        ({"allow_hosts": ["*"]}, r"must be a host name or an address"),
        ({"allow_hosts": ["a b"]}, r"must be a host name or an address"),
        ({"allow_hosts": ["http://box/x"]}, r"must be a host name or an address"),
    ],
)
def test_a_bad_table_is_refused_naming_the_key(table, message):
    with pytest.raises(policy.PolicyError, match=message):
        dev_server.config(table, "[guards.dev-server]")


@pytest.mark.parametrize(
    ("addr", "host"),
    [
        ("0.0.0.0:8000", "0.0.0.0"),
        ("localhost:3000", "localhost"),
        ("[::1]:8000", "::1"),
        ("[::]:8000", "::"),
        ("::", "::"),
        ("tcp://127.0.0.1:3000", "127.0.0.1"),
        ("3000", ""),
        (":8000", ""),
        ("*", "*"),
        ("Box.Example:80", "box.example"),
    ],
)
def test_a_host_is_read_out_of_an_address(addr, host):
    assert dev_server.host_of(addr) == host


def test_it_reads_commands_before_a_call():
    mod = guards.get("dev-server")
    assert (mod.EVENTS, mod.KINDS) == (("pre",), ("command",))


def test_it_is_on_in_the_default_policy_and_in_no_policy_that_says_nothing():
    assert "dev-server" in policy.default_policy().guards.on
    assert "dev-server" in policy.default_toml()
    assert policy.loads("").guards.on == ()
    assert policy.loads('[guards]\non = ["dev-server"]\n[guards.dev-server]\nallow_hosts = ["203.0.113.5"]\n')


def hook(command, session="s1"):
    return json.dumps({"tool_name": "Bash", "session_id": session, "cwd": "/w", "tool_input": {"command": command}})


def pol(extra=""):
    return policy.loads(f'[guards]\non = ["dev-server"]\n{extra}')


def test_the_gate_denies_a_server_on_every_interface_and_allows_loopback(home):
    reason = gate.check("claude-code", hook("python -m http.server"), pol(), home=home / ".ws")
    assert reason and reason.startswith("dev-server: ") and "--bind 127.0.0.1" in reason
    assert gate.check("claude-code", hook("python -m http.server -b localhost"), pol(), home=home / ".ws") is None
    assert gate.check("claude-code", hook("python -m http.server"), policy.loads(""), home=home / ".ws") is None


def test_the_deny_asks_a_person_and_the_approval_is_used_once(home):
    root = home / ".ws" / "approvals"
    raw = hook("vite --host")

    def check():
        return gate.check("claude-code", raw, pol(), approval_root=root, home=home / ".ws")

    asked = check()
    assert asked and "ws approve" in asked and "dev-server" in asked
    ((ident, bound, _),) = approvals.pending(root)
    assert bound == approvals.Bound("claude-code", "guard:dev-server", "vite --host", "/w", "s1")
    approvals.grant(root, ident[:12])
    assert check() is None  # approved once
    assert check()  # and that was the one use


def test_a_vite_host_with_no_value_says_so_and_is_not_confused_with_the_next_option():
    reason = judge("vite --host --port 3000").reason
    assert "--host with no value" in reason and "--port" not in reason
