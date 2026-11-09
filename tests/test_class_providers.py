"""G7: `ws gate launch` refuses a model provider the data class does not allow, and a launch in a class starts with the
class's network list (Claude Code's allowedDomains, or ws's own proxy for the other runners)."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest
from test_run import ws as run_ws  # noqa: F401  (a scratch WS_HOME with a fake claude, for the `ws run` tests below)

from ws import cli, policy, sandbox
from ws.output import WsError


@pytest.fixture
def box(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("WS_HOME", str(tmp_path / "wshome"))
    for name in ("CLAUDE_CONFIG_DIR", "OPENCODE_CONFIG_DIR", "WS_SANDBOXED", "CLAUDE_PROJECT_DIR"):
        monkeypatch.delenv(name, raising=False)
    nowhere = tmp_path / "empty-path"
    nowhere.mkdir()
    monkeypatch.setenv("PATH", str(nowhere))
    (tmp_path / "wshome").mkdir()
    (tmp_path / "work" / "app").mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    monkeypatch.chdir(tmp_path / "work" / "app")
    return tmp_path


def write(box, body: str) -> None:
    (box / "wshome" / "policy.toml").write_text(body, encoding="utf-8")


def klass(box, extra: str = "", access: str = "walled") -> str:
    return f'[classes.work]\naccess = "{access}"\nrepos = ["{box}/work/**"]\n{extra}'


class Ran(list):
    """What ran, as (kind, argv, env); `seen` is the keyword arguments the wall was built with."""

    seen: dict


@pytest.fixture
def started(monkeypatch):
    """Nothing really starts."""
    ran = Ran()
    seen = {}

    def wrap(argv, *a, **k):
        seen.update(k)
        return sandbox.Wrapped(["wall", *argv])

    def broker(cmd, e, fds, root):
        ran.append(("wall", cmd, e))
        return 0

    monkeypatch.setattr(sandbox, "wrap", wrap)
    monkeypatch.setattr(sandbox, "run_with_broker", broker)
    monkeypatch.setattr(sandbox, "run_plain", lambda cmd, e: ran.append(("plain", cmd, e)) or 0)
    monkeypatch.setattr(sandbox.os, "execvpe", lambda exe, argv, e: ran.append(("exec", argv, e)))
    ran.seen = seen
    return ran


def launch(runner, *extra, command=None):
    return cli.main(["gate", "launch", "--runner", runner, *extra, "--", *(command or [runner])])


# --- which provider a class allows ----------------------------------------------------------------------------------------


def cls_of(text: str) -> policy.Class:
    return next(iter(policy.loads(text).classes.values()))


@pytest.mark.parametrize(
    ("body", "provider", "ok"),
    [
        ("", "anthropic", False),  # walled, nothing said: the strictest (a model on this machine)
        ("", "local", True),
        ("", "ollama", True),
        ("", None, False),  # a limited class needs the provider named
        ('allowed_providers = ["anthropic"]\n', "anthropic", True),
        ('allowed_providers = ["anthropic"]\n', "ANTHROPIC", True),  # case does not matter
        ('allowed_providers = ["anthropic"]\n', "openai", False),
        ('allowed_providers = ["anthropic"]\n', None, False),
        ("allowed_providers = []\n", "anthropic", False),  # [] allows none
        ("allowed_providers = []\n", "local", False),
        ('allowed_providers = ["anthropic", "local"]\nlocal_only = true\n', "anthropic", False),  # local_only wins
        ('allowed_providers = ["anthropic", "local"]\nlocal_only = true\n', "local", True),
        ('allowed_providers = ["anthropic"]\n', "an thropic", False),
        ('allowed_providers = ["anthropic"]\n', "../x", False),
    ],
)
def test_a_walled_class_allows_what_it_lists(body, provider, ok):
    cls = cls_of(f'[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\n{body}')
    assert (sandbox.provider_problem(cls, provider) is None) is ok


@pytest.mark.parametrize(
    ("body", "provider", "ok"),
    [
        ("", "anthropic", True),  # a full class that says nothing allows any
        ("", None, True),
        ('allowed_providers = ["anthropic"]\n', "openai", False),
        ("local_only = true\n", "anthropic", False),
        ("local_only = true\n", "local", True),
    ],
)
def test_a_full_class_is_open_unless_it_says_otherwise(body, provider, ok):
    cls = cls_of(f'[classes.c]\naccess = "full"\nrepos = ["/srv/c/**"]\n{body}')
    assert (sandbox.provider_problem(cls, provider) is None) is ok


def test_no_classes_means_no_limit():
    assert sandbox.provider_problem(None, "anything") is None and sandbox.provider_problem(None, None) is None


def test_the_unclassified_class_allows_only_a_local_model():
    assert sandbox.provider_problem(policy.UNCLASSIFIED, "anthropic")
    assert sandbox.provider_problem(policy.UNCLASSIFIED, "local") is None


def test_the_refusal_names_the_class_and_nothing_else():
    cls = cls_of('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_providers = ["local", "ollama"]\n')
    for provider in ("anthropic", None, "../x"):
        text = sandbox.provider_problem(cls, provider)
        assert "'c'" in text and "ollama" not in text and "local" not in text and "anthropic" not in text


# --- the launch ----------------------------------------------------------------------------------------------------


def test_launch_refuses_a_provider_outside_the_class_before_anything_starts(box, started, capsys):
    write(box, klass(box, 'allowed_providers = ["local"]\n'))
    for extra in ((), ("--no-sandbox",)):
        assert launch("opencode", "--provider", "anthropic", *extra) == 3
        err = capsys.readouterr().err
        assert "class 'work'" in err and "does not allow that model provider" in err
        assert "anthropic" not in err and "local" not in err  # neither the choice nor the list is echoed
    assert started == [] and not (box / "wshome" / "wall").exists()


def test_launch_refuses_claude_code_in_a_walled_class_that_does_not_say(box, started, capsys):
    write(box, klass(box))
    assert launch("claude-code") == 3  # claude-code means anthropic unless --provider says otherwise
    assert "class 'work'" in capsys.readouterr().err and started == []


def test_launch_in_a_folder_of_no_class_is_held_to_the_strictest(box, started, monkeypatch, capsys):
    write(box, klass(box, 'allowed_providers = ["anthropic"]\n'))
    monkeypatch.chdir(box / "elsewhere")
    assert launch("claude-code") == 3
    assert "unclassified" in capsys.readouterr().err


def test_launch_without_classes_is_as_it_was(box, started):
    write(box, "")
    assert launch("opencode") == 0
    kind, cmd, env = started[0]
    assert kind == "wall" and cmd == ["wall", "opencode"]
    assert started.seen["net"] is None and "HTTP_PROXY" not in env  # no plan: the network is untouched


def test_launch_in_a_class_runs_the_proxy_beside_the_runner_and_tells_the_wall_the_ports(box, started):
    write(box, klass(box, 'allowed_providers = ["local"]\nallowed_hosts = ["localhost:11434", "api.example.com"]\n'))
    assert launch("opencode", "--provider", "local") == 0
    _, _, env = started[0]
    port = int(env["HTTPS_PROXY"].rsplit(":", 1)[1])
    secret = env["HTTPS_PROXY"].split("@")[0].rsplit(":", 1)[1]
    assert env["HTTP_PROXY"] == env["https_proxy"] == f"http://ws:{secret}@127.0.0.1:{port}" and len(secret) >= 24
    assert env["NO_PROXY"].startswith("localhost,")
    assert started.seen["net"] == (port, 11434)
    shim_dir = env["PATH"].split(":")[0]
    assert shim_dir.endswith("/bin") and "ws-wall-" in shim_dir  # blocked tools first on PATH
    with pytest.raises(OSError):  # the proxy was stopped with the run
        socket.create_connection(("127.0.0.1", port), timeout=2).close()


def test_launch_of_claude_code_gives_its_sandbox_the_class_list(box, started):
    gate = box / "wshome" / "gate" / "claude-code"
    gate.mkdir(parents=True)
    (gate / "settings.json").write_text(json.dumps({"sandbox": {"enabled": True}}), encoding="utf-8")
    write(box, klass(box, 'allowed_providers = ["anthropic"]\nallowed_hosts = ["api.example.com"]\n'))
    assert launch("claude-code", command=["claude"]) == 0
    kind, cmd, env = started[0]
    assert kind == "plain" and cmd[1] == "--settings" and env["MCP_CONNECTION_NONBLOCKING"] == "false"
    conf = json.loads(Path(cmd[2]).read_text())
    assert conf["sandbox"]["network"]["allowedDomains"] == [
        "api.example.com",
        "api.anthropic.com",
        "platform.claude.com",
    ]


def test_launch_of_claude_code_in_a_class_with_no_hosts_reaches_nothing(box, started):
    gate = box / "wshome" / "gate" / "claude-code"
    gate.mkdir(parents=True)
    (gate / "settings.json").write_text(json.dumps({"sandbox": {"enabled": True}}), encoding="utf-8")
    write(box, klass(box, 'allowed_providers = ["local"]\n'))
    assert launch("claude-code", "--provider", "local", command=["claude"]) == 0
    conf = json.loads(Path(started[0][1][2]).read_text())
    assert conf["sandbox"]["network"]["allowedDomains"] == []


def test_a_class_session_without_a_wall_is_refused_not_started_with_an_open_network(box, started, capsys):
    # this used to start the runner with a note; the class's list would have been applied by nothing
    write(box, klass(box, 'allowed_providers = ["local"]\n'))
    for runner in ("opencode", "claude-code"):
        assert launch(runner, "--provider", "local", "--no-sandbox") == 3
        err = capsys.readouterr().err
        assert "class 'work'" in err and "applied by the sandbox" in err
    assert started == []


def test_a_class_session_of_claude_code_is_refused_when_the_policy_has_the_sandbox_off(box, started, capsys):
    gate = box / "wshome" / "gate" / "claude-code"
    gate.mkdir(parents=True)
    (gate / "settings.json").write_text("{}", encoding="utf-8")  # what the generator writes with the sandbox off
    write(box, "[gate]\nsandbox = false\n" + klass(box, 'allowed_providers = ["anthropic"]\n'))
    assert launch("claude-code", command=["claude"]) == 3
    assert "class 'work'" in capsys.readouterr().err and started == []


def test_the_generated_settings_with_no_sandbox_block_are_refused_for_a_class(box, started, capsys):
    gate = box / "wshome" / "gate" / "claude-code"
    gate.mkdir(parents=True)
    (gate / "settings.json").write_text("{}", encoding="utf-8")
    write(
        box, klass(box, 'allowed_providers = ["anthropic"]\n')
    )  # the policy says sandbox on; the file on disk is stale
    assert launch("claude-code", command=["claude"]) == 3
    assert "no sandbox block" in capsys.readouterr().err and started == []


def test_a_policy_without_classes_still_starts_a_session_without_a_wall(box, started):
    write(box, "")
    assert launch("opencode", "--no-sandbox") == 0 and started[0][0] == "exec"


def test_require_provider_raises_exit_3_with_a_hint_that_names_no_provider():
    cls = cls_of('[classes.c]\naccess = "walled"\nrepos = ["/srv/c/**"]\nallowed_providers = ["local"]\n')
    with pytest.raises(WsError) as e:
        sandbox.require_provider(cls, "anthropic")
    assert e.value.exit_code == 3 and e.value.kind == "refused" and "anthropic" not in e.value.hint
    sandbox.require_provider(cls, "local")


# --- ws run ----------------------------------------------------------------------------------------------------------------


@pytest.fixture
def ws(run_ws):  # noqa: F811 (a fixture takes the other as its argument)
    return run_ws


def settings_of(ws) -> dict:
    (rec,) = ws.records()
    return json.loads((ws.home / "runs" / rec["run_id"] / "gate" / "settings.json").read_text(encoding="utf-8"))


def run_policy(ws, tmp_path, body: str, head: str = "") -> Path:
    work = tmp_path / "work"
    work.mkdir(exist_ok=True)
    (ws.home / "policy.toml").write_text(
        head + f'[classes.work]\naccess = "walled"\nrepos = ["{work}/**"]\n{body}', encoding="utf-8"
    )
    return work


def test_ws_run_gives_a_class_job_the_class_list_in_the_runs_settings(ws, tmp_path):
    work = run_policy(ws, tmp_path, 'allowed_providers = ["anthropic"]\nallowed_hosts = ["api.example.com"]\n')
    ws.job(extra=f'cwd = "{work}"')
    assert cli.main(["run", "daily"]) == 0
    net = settings_of(ws)["sandbox"]["network"]
    assert net["allowedDomains"] == ["api.example.com", "api.anthropic.com", "platform.claude.com"]
    assert net["strictAllowlist"] is True


def test_ws_run_refuses_a_provider_the_class_does_not_allow_and_records_why(ws, tmp_path, capsys):
    work = run_policy(ws, tmp_path, 'allowed_providers = ["local"]\n')
    ws.job(extra=f'cwd = "{work}"')
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert rec["status"] == "refused" and rec["stop_reason"] == "refused:egress"
    assert "class 'work'" in rec["error"] and "anthropic" not in rec["error"] and "local" not in rec["error"]
    assert not (ws.home / "fake-saw.json").exists()  # the runner never started


def test_ws_run_holds_a_job_with_no_cwd_to_the_strictest_when_there_are_classes(ws, tmp_path):
    run_policy(ws, tmp_path, 'allowed_providers = ["anthropic"]\n')
    ws.job()  # no cwd: a private folder, in no class
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert "unclassified" in rec["error"] and rec["stop_reason"] == "refused:egress"


def test_ws_run_refuses_a_class_job_when_the_policy_has_the_sandbox_off(ws, tmp_path):
    work = run_policy(ws, tmp_path, 'allowed_providers = ["anthropic"]\n', head="[gate]\nsandbox = false\n")
    ws.job(extra=f'cwd = "{work}"')
    assert cli.main(["run", "daily"]) == 3
    (rec,) = ws.records()
    assert "sandbox is off" in rec["error"] and not (ws.home / "fake-saw.json").exists()


def test_ws_run_with_no_classes_is_as_it_was(ws):
    ws.job()
    assert cli.main(["run", "daily"]) == 0
    assert "network" not in settings_of(ws)["sandbox"]


def test_ws_run_dry_run_says_it_would_refuse(ws, tmp_path, capsys):
    work = run_policy(ws, tmp_path, 'allowed_providers = ["local"]\n')
    ws.job(extra=f'cwd = "{work}"')
    assert cli.main(["run", "daily", "--dry-run"]) == 3
    assert "does not allow that model provider" in capsys.readouterr().out


def test_a_headless_job_names_its_provider_from_the_runner_the_option_or_the_model():
    assert sandbox.job_provider("claude-code", "claude-haiku-4-5") == "anthropic"
    assert sandbox.job_provider("opencode", "ollama/llama3", {}) == "ollama"
    assert sandbox.job_provider("opencode", "ollama/llama3", {"provider": "local"}) == "local"
    assert sandbox.job_provider("opencode", "bare-model") == ""
